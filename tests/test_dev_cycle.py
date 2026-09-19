"""PILOT-04 — ciclo de desarrollo gobernado: frontera, aplicación, verificación y rollback.

La suite usa un **repositorio fixture real** (Git, en un directorio temporal) y un proveedor
guionizado que pasa por el ``ProviderRouter`` real: lo que se comprueba es el ciclo de verdad,
no una simulación de sus piezas. El repositorio destino del piloto no se toca aquí.

Cubre, del encargo de PILOT-04: lectura y escritura gobernadas, contención de rutas, frontera de
secretos, peticiones de contexto, validación de plan y de cambios, checkpoint, aplicación, ejecución
segura, bucle de reparación con su límite, rollback que preserva los cambios preexistentes del
usuario, commit local aislado, rechazo de autoridad del proveedor, influencia de PELL y correlación
de la auditoría por ``request_id``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from punto.audit.logger import AuditLogger
from punto.memory.experience import ExperienceMemory, ExperienceResult, ExperienceStatus
from punto.memory.retrieval import MemoryRetriever
from punto.memory.store import ExperienceStore
from punto.orchestrator.dev_cycle import DevelopmentConfig, DevelopmentCycle
from punto.providers.base import ModelCompletion
from punto.providers.contract import (
    ModelUsage,
    ProviderResult,
    ProviderRole,
)
from punto.providers.router import ProviderRouter
from punto.schemas.build import BuildRequest
from punto.schemas.dev import (
    ChangeOperation,
    DevelopmentStatus,
    RepositoryOperation,
)
from punto.workspace.repository import (
    GovernedRepository,
    OperationNotAuthorizedError,
    RepositoryPolicy,
    ScopeViolation,
    SecretBoundaryViolation,
    StaleWriteError,
)
from punto.workspace.target import (
    DevelopmentTarget,
    DevelopmentTargetRegistry,
    VerificationCommand,
)

TARGET_ID = "destino-fixture"
WORK_BRANCH = "ai/pilot-04-fixture"
SOURCE = "export const TIPOS = ['Casa', 'Apartamento'];\n"


def _git(root: Path, *args: str) -> str:
    """Ejecuta Git en el repositorio fixture (solo para prepararlo)."""
    completed = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} falló: {completed.stderr}")
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    """Repositorio fixture con los tipos y un ``.gitignore`` ya tocado por el usuario."""
    root = tmp_path / "destino"
    (root / "src" / "lib").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "src" / "lib" / "property-types.ts").write_text(SOURCE, encoding="utf-8")
    (root / "src" / "lib" / "opciones.ts").write_text(
        "export const TIPOS_UI = ['Casa', 'Oficina'];\n", encoding="utf-8"
    )
    for index in range(1, 7):
        (root / "src" / "lib" / f"relleno-{index:02d}.ts").write_text(
            f"export const RELLENO_{index} = {index};\n", encoding="utf-8"
        )
    (root / ".gitignore").write_text("node_modules\n", encoding="utf-8")
    (root / ".env.local").write_text("DATABASE_URL=postgresql://canary:canary@host/db\n",
                                     encoding="utf-8")
    # Un almacén de credenciales **dentro** del alcance: la denegación tiene que ser por secreto.
    (root / "src" / ".env.local").write_text("API_KEY=sk-0123456789abcdef0123456789ab\n",
                                             encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=PUNTO Fixture",
        "-c",
        "user.email=fixture@punto.local",
        "commit",
        "-m",
        "base",
    )
    # Cambio **preexistente del usuario**: el ciclo nunca debe incluirlo en su commit ni revertirlo.
    (root / ".gitignore").write_text("node_modules\n.env.local\n", encoding="utf-8")
    return root


def _check(requires: str = "Apartamento") -> tuple[str, ...]:
    """Línea de verificación real: exige que el tipo pedido esté en el fichero de opciones.

    Es un ``python -c`` de verdad: el comando se ejecuta en el repositorio y su código de salida es
    la evidencia que el ciclo usa para decidir si sigue o repara.
    """
    code = (
        "import pathlib,sys;"
        "texto=pathlib.Path('src/lib/opciones.ts').read_text(encoding='utf-8');"
        f"sys.exit(0 if {requires!r} in texto else 1)"
    )
    return ("python", "-c", code)


def _target(
    root: Path, *, verification: dict[str, tuple[str, ...]] | None = None
) -> DevelopmentTarget:
    """Destino fixture con el baseline real del repositorio y el catálogo de verificación."""
    head = _git(root, "rev-parse", "HEAD")
    return DevelopmentTarget(
        target_id=TARGET_ID,
        repository=root,
        baseline_sha=head,
        scope_roots=("src", "tests"),
        allowed_operations=frozenset(
            {
                RepositoryOperation.READ,
                RepositoryOperation.WRITE,
                RepositoryOperation.CREATE,
                RepositoryOperation.EXECUTE,
                RepositoryOperation.COMMIT,
            }
        ),
        verification=tuple(
            VerificationCommand(name=name, argv=argv, timeout_seconds=60.0)
            for name, argv in (verification or {"focused": _check()}).items()
        ),
        work_branch=WORK_BRANCH,
        max_repair_rounds=3,
        command_timeout_seconds=60.0,
    )


class ScriptedClient:
    """Cliente que devuelve respuestas guionizadas y apunta lo que recibió."""

    def __init__(self, router: ProviderRouter, responses: Sequence[Any]) -> None:
        self._router = router
        self._responses = list(responses)
        self.prompts: list[str] = []
        self.calls = 0
        router.register_provider("guionizado", self._factory, model="guionizado-1")

    def _factory(self, model: str) -> ScriptedClient:
        del model
        return self

    @property
    def provider(self) -> str:
        """Identificador del proveedor."""
        return "guionizado"

    @property
    def model(self) -> str:
        """Modelo configurado."""
        return "guionizado-1"

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: Any = None,
        max_output_tokens: int | None = None,
    ) -> ModelCompletion:
        """Devuelve la siguiente respuesta del guion."""
        del user_prompt, json_schema, max_output_tokens
        self.prompts.append(system_prompt)
        self.calls += 1
        item = self._responses.pop(0) if self._responses else {"changes": []}
        if isinstance(item, Exception):
            raise item
        if isinstance(item, ProviderResult):
            content = item.content
        else:
            content = item if isinstance(item, str) else json.dumps(item)
        return ModelCompletion(
            content=content,
            model="guionizado-1",
            usage=ModelUsage(prompt_tokens=5, completion_tokens=7, total_tokens=12),
            latency_ms=1,
        )

    def redact(self, text: str) -> str:
        """No sanea: el ciclo no puede fiarse de la educación del adaptador."""
        return text

    def close(self) -> None:
        """No hay recursos que liberar."""


def _plan(**overrides: Any) -> dict[str, Any]:
    """Plan válido por defecto."""
    payload: dict[str, Any] = {
        "summary": "unificar la lista de tipos",
        "files_to_read": ["src/lib/property-types.ts", "src/lib/opciones.ts"],
        "files_to_modify": ["src/lib/opciones.ts"],
        "files_to_create": [],
        "verification_commands": ["focused"],
        "risks": ["cambiar la UI sin querer"],
        "acceptance_mapping": ["una sola fuente de tipos"],
    }
    payload.update(overrides)
    return payload


def _change(**overrides: Any) -> dict[str, Any]:
    """Cambio válido por defecto."""
    payload: dict[str, Any] = {
        "summary": "opciones desde la fuente única",
        "changes": [
            {
                "path": "src/lib/opciones.ts",
                "operation": "MODIFY",
                "content": "export const TIPOS_UI = ['Casa', 'Apartamento', 'Oficina'];\n",
                "reason": "alinear con el catálogo real",
                "acceptance_criterion": "una sola fuente de tipos",
            }
        ],
    }
    payload.update(overrides)
    return payload

def _cycle(
    root: Path,
    *,
    responses: Sequence[Any],
    verification: dict[str, tuple[str, ...]] | None = None,
    retriever: Any | None = None,
    store: ExperienceStore | None = None,
    audit: AuditLogger | None = None,
    max_context_files: int | None = None,
) -> tuple[DevelopmentCycle, ScriptedClient, AuditLogger, DevelopmentTarget]:
    """Ciclo compuesto con router real, proveedor guionizado y auditoría en memoria."""
    router = ProviderRouter()
    client = ScriptedClient(router, responses)
    for role in ProviderRole:
        router.assign_role(role, "guionizado")
    target = _target(root, verification=verification)
    logger = audit if audit is not None else AuditLogger()
    options: dict[str, Any] = {}
    if max_context_files is not None:
        options["max_context_files"] = max_context_files
    cycle = DevelopmentCycle(
        router=router,
        targets=DevelopmentTargetRegistry({TARGET_ID: target}),
        config=DevelopmentConfig(**options),
        retriever=retriever,
        store=store,
        audit=logger,
    )
    return cycle, client, logger, target


def _request(**overrides: Any) -> BuildRequest:
    """Solicitud de desarrollo válida."""
    payload: dict[str, Any] = {
        "objective": "unificar la lista de tipos de propiedad en una sola fuente",
        "target_repository": TARGET_ID,
        "requested_role": ProviderRole.BUILDER,
        "acceptance_criteria": ("una sola fuente de tipos",),
        "constraints": ("no tocar el esquema de base de datos",),
        "scope_paths": ("src/lib",),
        "context": "El buscador ofrece tipos que no existen en el catálogo.",
    }
    payload.update(overrides)
    return BuildRequest(**payload)


def _events(logger: AuditLogger, request_id: Any) -> list[str]:
    """Tipos de evento del ciclo, en orden."""
    return [event.event_type.value for event in logger.by_resource(str(request_id))]


# ===========================================================================
# Camino feliz: plan, checkpoint, aplicación, verificación, commit local
# ===========================================================================
def test_ciclo_completo_aplica_verifica_y_confirma(tmp_path: Path) -> None:
    """El proveedor propone; PUNTO valida, aplica, verifica y confirma solo sus rutas."""
    root = _repo(tmp_path)
    cycle, client, _, target = _cycle(root, responses=[_plan(), _change()])

    result = cycle.run(_request())

    assert result.status is DevelopmentStatus.COMPLETED
    assert result.completed
    assert result.authority == "LOCAL_APPLY_ONLY"
    assert result.published is False
    assert [item.path for item in result.applied] == ["src/lib/opciones.ts"]
    assert all(item.passed for item in result.verification)
    assert result.checkpoint_id
    assert result.branch == WORK_BRANCH
    assert result.commit_sha
    assert client.calls == 2, "una invocación para el plan y otra para los cambios"
    # El cambio está en el fichero y en el commit, y el commit no arrastra nada más.
    assert "Apartamento" in (root / "src" / "lib" / "opciones.ts").read_text(encoding="utf-8")
    committed = _git(root, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["src/lib/opciones.ts"]
    # El cambio preexistente del usuario sigue intacto y **no** entró en el commit.
    assert ".env.local" in (root / ".gitignore").read_text(encoding="utf-8")
    assert "M .gitignore" in _git(root, "status", "--porcelain")
    assert _git(root, "rev-parse", "HEAD") != target.baseline_sha


def test_la_auditoria_reconstruye_el_ciclo_por_request_id(tmp_path: Path) -> None:
    """Todos los eventos del ciclo comparten recurso y siguen el orden documentado."""
    root = _repo(tmp_path)
    cycle, _, logger, _ = _cycle(
        root,
        responses=[_plan(), _change()],
        store=ExperienceStore(tmp_path / "pell.jsonl"),
    )
    request = _request()

    cycle.run(request)

    events = _events(logger, request.request_id)
    assert events == [
        "BUILD_REQUEST_ACCEPTED",
        "BUILD_REQUEST_NORMALIZED",
        "DEV_PELL_RETRIEVED",
        "DEV_REPOSITORY_DISCOVERED",
        "BUILD_PROVIDER_SELECTED",
        "DEV_PLAN_CREATED",
        "DEV_PLAN_VALIDATED",
        "BUILD_PROVIDER_SELECTED",
        "DEV_CHANGE_VALIDATED",
        "DEV_CHECKPOINT_CREATED",
        "FILE_CHANGED",
        "DEV_VERIFICATION_STARTED",
        "COMMAND_EXECUTED",
        "DEV_VERIFICATION_COMPLETED",
        "DEV_PELL_INFLUENCE",
        "GIT_COMMIT_CREATED",
        "BUILD_CYCLE_COMPLETED",
    ]
    assert logger.by_resource("otro-id") == ()


# ===========================================================================
# Validación del plan (antes de escribir nada)
# ===========================================================================
def test_un_plan_fuera_de_alcance_se_rechaza_sin_escribir(tmp_path: Path) -> None:
    """El plan que sale del alcance no llega a tocar el disco."""
    root = _repo(tmp_path)
    before = (root / "src" / "lib" / "opciones.ts").read_text(encoding="utf-8")
    cycle, _, logger, _ = _cycle(
        root, responses=[_plan(files_to_modify=["/etc/passwd", "src/lib/opciones.ts"])]
    )
    request = _request()

    result = cycle.run(request)

    assert result.status is DevelopmentStatus.PLAN_REJECTED
    assert result.plan_status.value == "PLAN_REJECTED"
    assert result.applied == ()
    assert result.commit_sha == ""
    assert (root / "src" / "lib" / "opciones.ts").read_text(encoding="utf-8") == before
    assert "DEV_PLAN_REJECTED" in _events(logger, request.request_id)
    assert "DEV_CHECKPOINT_CREATED" not in _events(logger, request.request_id)


def test_un_plan_sin_verificacion_no_se_aplica(tmp_path: Path) -> None:
    """Un plan que no declara cómo se verifica no se acepta."""
    root = _repo(tmp_path)
    cycle, _, _, _ = _cycle(
        root, responses=[_plan(verification_commands=[])]
    )

    result = cycle.run(_request())

    assert result.status is DevelopmentStatus.PLAN_REJECTED
    assert "PLAN_WITHOUT_VERIFICATION" in [issue.code for issue in result.plan_issues]


def test_una_verificacion_desconocida_no_se_acepta(tmp_path: Path) -> None:
    """El proveedor no puede inventarse un comando: solo nombrar los del catálogo."""
    root = _repo(tmp_path)
    cycle, _, _, _ = _cycle(
        root, responses=[_plan(verification_commands=["focused", "rm -rf /"])]
    )

    result = cycle.run(_request())

    assert result.status is DevelopmentStatus.PLAN_REJECTED
    assert "PLAN_UNKNOWN_VERIFICATION" in [issue.code for issue in result.plan_issues]


# ===========================================================================
# Validación de los cambios
# ===========================================================================
def test_un_cambio_no_declarado_en_el_plan_se_rechaza(tmp_path: Path) -> None:
    """Solo se aplica lo que el plan validado declaró."""
    root = _repo(tmp_path)
    cycle, _, logger, _ = _cycle(
        root,
        responses=[
            _plan(),
            _change(
                changes=[
                    {
                        "path": "src/lib/otro.ts",
                        "operation": "CREATE",
                        "content": "export const X = 1;\n",
                    }
                ]
            ),
        ],
    )
    request = _request()

    result = cycle.run(request)

    assert result.status is DevelopmentStatus.CHANGE_REJECTED
    assert "CHANGE_NOT_IN_PLAN" in [issue.code for issue in result.change_issues]
    assert not (root / "src" / "lib" / "otro.ts").exists()
    assert "DEV_CHANGE_REJECTED" in _events(logger, request.request_id)


def test_un_cambio_con_secreto_no_se_aplica(tmp_path: Path) -> None:
    """Un contenido con forma de credencial se rechaza; nunca se escribe ni se guarda."""
    root = _repo(tmp_path)
    cycle, _, _, _ = _cycle(
        root,
        responses=[
            _plan(),
            _change(
                changes=[
                    {
                        "path": "src/lib/opciones.ts",
                        "operation": "MODIFY",
                        "content": "export const K = 'sk-0123456789abcdef0123456789ab';\n",
                    }
                ]
            ),
        ],
    )

    result = cycle.run(_request())

    assert result.status is DevelopmentStatus.CHANGE_REJECTED
    assert "CHANGE_SECRET" in [issue.code for issue in result.change_issues]
    assert "sk-0123456789abcdef" not in (root / "src" / "lib" / "opciones.ts").read_text(
        encoding="utf-8"
    )


def test_un_borrado_no_autorizado_se_rechaza(tmp_path: Path) -> None:
    """El destino no autoriza DELETE en esta fase: la propuesta se rechaza."""
    root = _repo(tmp_path)
    cycle, _, _, _ = _cycle(
        root,
        responses=[
            _plan(files_to_modify=["src/lib/opciones.ts"]),
            _change(
                changes=[{"path": "src/lib/opciones.ts", "operation": "DELETE"}]
            ),
        ],
    )

    result = cycle.run(_request())

    assert result.status is DevelopmentStatus.CHANGE_REJECTED
    assert "CHANGE_DELETE_NOT_AUTHORIZED" in [issue.code for issue in result.change_issues]
    assert (root / "src" / "lib" / "opciones.ts").exists()


def test_una_escritura_con_huella_obsoleta_se_rechaza(tmp_path: Path) -> None:
    """Si el fichero cambió desde que se leyó, el cambio no se aplica."""
    root = _repo(tmp_path)
    cycle, _, _, _ = _cycle(
        root,
        responses=[
            _plan(),
            _change(
                changes=[
                    {
                        "path": "src/lib/opciones.ts",
                        "operation": "MODIFY",
                        "content": "export const TIPOS_UI = ['Casa', 'Oficina'];\n",
                        "expected_sha256": "0" * 64,
                    }
                ]
            ),
        ],
    )

    result = cycle.run(_request())

    # La huella obsoleta la detecta la validación del ciclo y, si llegara a la escritura, la
    # frontera la volvería a detectar: en ambos casos no se escribe.
    assert result.status is DevelopmentStatus.CHANGE_REJECTED
    assert "CHANGE_STALE" in [issue.code for issue in result.change_issues]


# ===========================================================================
# Peticiones de contexto
# ===========================================================================
def test_una_peticion_de_contexto_dentro_de_autoridad_se_concede(tmp_path: Path) -> None:
    """El proveedor puede pedir otro fichero y PUNTO se lo da sin Human Gate."""
    root = _repo(tmp_path)
    cycle, client, logger, _ = _cycle(
        root,
        responses=[
            _plan(),
            _change(
                context_requests=[{"path": "src/lib/relleno-01.ts", "reason": "ver el resto"}]
            ),
            _change(),
        ],
        max_context_files=2,
    )
    request = _request()

    result = cycle.run(request)

    assert result.status is DevelopmentStatus.COMPLETED
    assert result.context_requests_granted == ("src/lib/relleno-01.ts",)
    assert result.context_requests_denied == ()
    assert "DEV_CONTEXT_GRANTED" in _events(logger, request.request_id)
    assert client.calls == 3, "plan, primera propuesta y propuesta con el contexto concedido"
    assert "relleno-01.ts" in client.prompts[-1], "el fichero concedido llega al contexto"


def test_una_peticion_de_contexto_sobre_un_secreto_se_deniega(tmp_path: Path) -> None:
    """Pedir un fichero de credenciales no se concede y no detiene el ciclo."""
    root = _repo(tmp_path)
    cycle, _, logger, _ = _cycle(
        root,
        responses=[
            _plan(),
            _change(
                context_requests=[{"path": "src/.env.local", "reason": "ver la configuración"}]
            ),
            _change(),
        ],
        max_context_files=2,
    )
    request = _request()

    result = cycle.run(request)

    assert result.status is DevelopmentStatus.COMPLETED
    assert result.context_requests_denied == ("src/.env.local",)
    assert result.context_requests_granted == ()
    denied = [
        event
        for event in logger.by_resource(str(request.request_id))
        if event.event_type.value == "DEV_CONTEXT_DENIED"
    ]
    assert denied and dict(denied[0].metadata)["code"] == "SECRET_BOUNDARY_VIOLATION"


# ===========================================================================
# Reparación autónoma y su límite
# ===========================================================================
def test_el_fallo_de_verificacion_se_repara_en_una_ronda(tmp_path: Path) -> None:
    """El ciclo entrega la evidencia del fallo, recibe la corrección y vuelve a verificar."""
    root = _repo(tmp_path)
    cycle, client, logger, _ = _cycle(
        root,
        responses=[
            _plan(),
            # Primera propuesta: no arregla nada, así que la verificación fallará.
            _change(
                changes=[
                    {
                        "path": "src/lib/opciones.ts",
                        "operation": "MODIFY",
                        "content": "export const TIPOS_UI = ['Casa'];\n",
                    }
                ]
            ),
            # Segunda: añade el tipo que la verificación exige.
            _change(),
        ],
    )
    request = _request()

    result = cycle.run(request)

    assert result.status is DevelopmentStatus.COMPLETED
    assert result.repair_rounds == 1
    assert all(item.passed for item in result.verification)
    events = _events(logger, request.request_id)
    assert "DEV_REPAIR_STARTED" in events
    assert "DEV_REPAIR_COMPLETED" in events
    # La evidencia del fallo llegó al proveedor en la segunda ronda.
    assert "MUST BE FIXED" in client.prompts[-1]


def test_la_reparacion_tiene_limite_y_entonces_revierte(tmp_path: Path) -> None:
    """Agotadas las rondas, el ciclo revierte y lo dice, sin dejar el árbol a medias."""
    root = _repo(tmp_path)
    before = (root / "src" / "lib" / "opciones.ts").read_text(encoding="utf-8")
    cycle, _, logger, _ = _cycle(
        root,
        responses=[
            _plan(),
            *[
                _change(
                    changes=[
                        {
                            "path": "src/lib/opciones.ts",
                            "operation": "MODIFY",
                            "content": f"export const TIPOS_UI = ['Casa']; // intento {index}\n",
                        }
                    ]
                )
                for index in range(5)
            ],
        ],
    )
    request = _request()

    result = cycle.run(request)

    assert result.status is DevelopmentStatus.VERIFICATION_FAILED
    assert result.rolled_back is True
    assert result.applied == ()
    assert result.commit_sha == ""
    assert (root / "src" / "lib" / "opciones.ts").read_text(encoding="utf-8") == before
    events = _events(logger, request.request_id)
    assert "DEV_REPAIR_EXHAUSTED" in events
    assert "DEV_ROLLBACK_COMPLETED" in events
    # El rollback no toca el cambio preexistente del usuario.
    assert "M .gitignore" in _git(root, "status", "--porcelain")


# ===========================================================================
# Fallos del proveedor y de la memoria
# ===========================================================================
def test_un_fallo_del_proveedor_queda_contenido(tmp_path: Path) -> None:
    """Un proveedor que no responde no rompe el ciclo ni escribe nada."""
    root = _repo(tmp_path)
    cycle, _, _, _ = _cycle(
        root,
        responses=[
            _plan(),
            {"changes": [], "summary": "no pude"},
            {"changes": [], "summary": "tampoco"},
            {"changes": [], "summary": "sigo sin poder"},
            {"changes": [], "summary": "nada"},
        ],
    )

    result = cycle.run(_request())

    assert result.status in {
        DevelopmentStatus.CHANGE_REJECTED,
        DevelopmentStatus.VERIFICATION_FAILED,
        DevelopmentStatus.BLOCKED,
    }
    assert result.applied == () or result.rolled_back


def test_una_memoria_caida_no_impide_el_ciclo(tmp_path: Path) -> None:
    """Si PELL falla, el ciclo sigue sin conocimiento previo y lo declara."""

    class _FailingStore:
        def search(self, *_args: Any, **_kwargs: Any) -> Sequence[ExperienceMemory]:
            raise OSError("memoria ilegible")

    root = _repo(tmp_path)
    cycle, _, _, _ = _cycle(
        root,
        responses=[_plan(), _change()],
        retriever=MemoryRetriever(_FailingStore()),  # type: ignore[arg-type]
    )

    result = cycle.run(_request())

    assert result.status is DevelopmentStatus.COMPLETED
    assert result.pell_status == "FAILED"


# ===========================================================================
# Autoridad
# ===========================================================================
def test_el_proveedor_no_puede_ejecutar_comandos(tmp_path: Path) -> None:
    """La frontera solo ejecuta líneas del catálogo del destino."""
    root = _repo(tmp_path)
    repository = GovernedRepository(
        root=root,
        task_id=_request().request_id,
        policy=RepositoryPolicy(
            allowed_operations=frozenset({RepositoryOperation.EXECUTE}),
            allowed_commands=frozenset({"python", "git", "npm"}),
            allowed_command_lines=(("python", "-c", "print(1)"),),
            scope_roots=("src",),
        ),
        branch=WORK_BRANCH,
    )

    with pytest.raises(OperationNotAuthorizedError):
        repository.run(("npm", "install"), name="instalar")
    with pytest.raises(OperationNotAuthorizedError):
        repository.run(("git", "push"), name="publicar")


def test_el_proveedor_no_puede_ampliar_el_alcance(tmp_path: Path) -> None:
    """Ni leer ni escribir fuera del alcance declarado, aunque lo pida el proveedor."""
    root = _repo(tmp_path)
    outside = tmp_path / "fuera.txt"
    outside.write_text("secreto vecino", encoding="utf-8")
    repository = GovernedRepository(
        root=root,
        task_id=_request().request_id,
        policy=RepositoryPolicy(
            allowed_operations=frozenset(
                {RepositoryOperation.READ, RepositoryOperation.WRITE, RepositoryOperation.CREATE}
            ),
            allowed_commands=frozenset({"git", "python"}),
            allowed_command_lines=(("python", "-c", "print(1)"),),
            scope_roots=("src",),
        ),
        branch=WORK_BRANCH,
    )

    for candidate in ("../fuera.txt", "/etc/passwd", "C:/Windows/win.ini", "file:///etc/passwd"):
        with pytest.raises(ScopeViolation):
            repository.read_text(candidate)
    with pytest.raises(ScopeViolation):
        repository.read_text("tests/otro.txt")
    with pytest.raises(ScopeViolation):
        repository.write_text(
            "../fuera.txt", "contenido", operation=__import__(
                "punto.schemas.dev", fromlist=["ChangeOperation"]
            ).ChangeOperation.MODIFY
        )


def test_un_fichero_de_secretos_no_se_lee(tmp_path: Path) -> None:
    """`.env.local` no cruza la frontera de lectura, ni siquiera existiendo."""
    root = _repo(tmp_path)
    repository = GovernedRepository(
        root=root,
        task_id=_request().request_id,
        policy=RepositoryPolicy(
            allowed_operations=frozenset({RepositoryOperation.READ}),
            allowed_commands=frozenset({"git", "python"}),
            allowed_command_lines=(("python", "-c", "print(1)"),),
        ),
        branch=WORK_BRANCH,
    )

    with pytest.raises(SecretBoundaryViolation):
        repository.read_text(".env.local")


def test_la_escritura_verifica_la_huella(tmp_path: Path) -> None:
    """La frontera de escritura rechaza una huella que no es la del fichero."""
    root = _repo(tmp_path)
    repository = GovernedRepository(
        root=root,
        task_id=_request().request_id,
        policy=RepositoryPolicy(
            allowed_operations=frozenset({RepositoryOperation.WRITE}),
            allowed_commands=frozenset({"git", "python"}),
            allowed_command_lines=(("python", "-c", "print(1)"),),
        ),
        branch=WORK_BRANCH,
    )

    with pytest.raises(StaleWriteError):
        repository.write_text(
            "src/lib/opciones.ts",
            "otra cosa\n",
            operation=ChangeOperation.MODIFY,
            expected_sha256="0" * 64,
        )


# ===========================================================================
# PELL: influencia conductual observable
# ===========================================================================
def test_la_experiencia_recuperada_cambia_el_contexto(tmp_path: Path) -> None:
    """La experiencia VERIFIED ordena el descubrimiento y su efecto queda registrado."""
    root = _repo(tmp_path)
    store = ExperienceStore(tmp_path / "pell.jsonl")
    experience = store.add(
        ExperienceMemory(
            problem="unificar la lista de tipos de propiedad en el vertical inmobiliario",
            context="destino-fixture",
            solution="la fuente única vive en src/lib/property-types.ts y las vistas la importan",
            procedure=["leer src/lib/property-types.ts", "sustituir la lista duplicada"],
            result=ExperienceResult.SUCCESS,
            verification=["PILOT-04: comprobado en el fixture"],
            tags=["property-types", "opciones", "tipos"],
            status=ExperienceStatus.VERIFIED,
        )
    )
    cycle, _, logger, _ = _cycle(
        root,
        responses=[_plan(), _change()],
        retriever=MemoryRetriever(store),
        store=store,
    )
    request = _request()

    result = cycle.run(request)

    assert result.pell_status == "HIT"
    assert result.pell_influence, "la influencia debe quedar registrada con su efecto observable"
    influence = result.pell_influence[0]
    assert influence.experience_id == experience.id
    assert influence.decision_point
    assert influence.observable_effect
    events = _events(logger, request.request_id)
    assert "DEV_PELL_INFLUENCE" in events
    # La experiencia usada en la decisión de contexto es la recuperada de verdad.
    discovered = [
        event
        for event in logger.by_resource(str(request.request_id))
        if event.event_type.value == "DEV_REPOSITORY_DISCOVERED"
    ]
    metadata = dict(discovered[0].metadata)
    assert "property-types.ts" in json.dumps(metadata["pell_ranked"])
    # Y el ciclo registra lo aprendido: una experiencia nueva y verificable.
    assert len(store.list()) >= 2


def test_el_aprendizaje_del_ciclo_se_registra_con_evidencia(tmp_path: Path) -> None:
    """Un ciclo completado deja conocimiento VERIFIED con la evidencia de sus comandos."""
    root = _repo(tmp_path)
    store = ExperienceStore(tmp_path / "pell.jsonl")
    cycle, _, _, _ = _cycle(root, responses=[_plan(), _change()], store=store)

    result = cycle.run(_request())

    assert result.status is DevelopmentStatus.COMPLETED
    learned = [item for item in store.list() if item.status is ExperienceStatus.VERIFIED]
    assert learned
    assert any("PILOT-04" in item for item in learned[-1].verification)


# ===========================================================================
# Frontera de recursos: contención real de rutas
# ===========================================================================
def test_la_contencion_del_repositorio_es_real(tmp_path: Path) -> None:
    """Resolver la ruta no basta: se resuelven enlaces y se exige seguir dentro."""
    root = _repo(tmp_path)
    repository = GovernedRepository(
        root=root,
        task_id=_request().request_id,
        policy=RepositoryPolicy(
            allowed_operations=frozenset({RepositoryOperation.READ}),
            allowed_commands=frozenset({"git", "python"}),
            allowed_command_lines=(("python", "-c", "print(1)"),),
        ),
        branch=WORK_BRANCH,
    )

    assert repository.exists("src/lib/property-types.ts")
    assert not repository.exists("src/lib/no-existe.ts")
    with pytest.raises(ScopeViolation):
        repository.resolve("src/../../fuera")
    with pytest.raises(ScopeViolation):
        repository.resolve(".git/config")


def test_el_destino_debe_declarar_el_baseline(tmp_path: Path) -> None:
    """Si el repositorio no está en el baseline declarado, el ciclo no empieza."""
    root = _repo(tmp_path)
    target = _target(root)
    moved = DevelopmentTarget(
        target_id=target.target_id,
        repository=target.repository,
        baseline_sha="0" * 40,
        scope_roots=target.scope_roots,
        allowed_operations=target.allowed_operations,
        verification=target.verification,
        work_branch=target.work_branch,
    )
    router = ProviderRouter()
    ScriptedClient(router, [_plan(), _change()])
    for role in ProviderRole:
        router.assign_role(role, "guionizado")
    cycle = DevelopmentCycle(
        router=router,
        targets=DevelopmentTargetRegistry({TARGET_ID: moved}),
        audit=AuditLogger(),
    )

    result = cycle.run(_request())

    assert result.status is DevelopmentStatus.BLOCKED
    assert result.error_kind == "REPOSITORY_DENIED"


@pytest.fixture
def _reference() -> Iterator[None]:
    """Fixture de referencia para que el módulo declare al menos un fixture explícito."""
    yield None
