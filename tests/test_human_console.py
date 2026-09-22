"""Consola humana local → tarea → Human Gate → publicación gobernada: la cadena, demostrada.

Cubre lo que el encargo pide demostrar (A a N) **sin tocar producción real**: el push va
a un remoto Git local (bare) y la comprobación de producción se inyecta. Lo que no se
simula es la autoridad: los gates son los ``HumanGate`` del motor, la decisión de publicar
es una ``PolicyDecision`` real sobre ``deploy_production`` y la publicación llama a
``assert_executable`` antes de empujar nada.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto.api.app import create_app
from punto.api.console import (
    PRODUCTION_ACTION,
    ConsoleDependencies,
    register_human_console,
)
from punto.api.dashboard import register_dashboard
from punto.audit.logger import AuditLogger
from punto.orchestrator.dev_cycle import DevelopmentConfig, DevelopmentCycle
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers.base import ModelCompletion
from punto.providers.contract import ModelUsage, ProviderRole
from punto.providers.router import ProviderRouter
from punto.providers.transport import REDACTED
from punto.publish.production import (
    GitPublisher,
    ProductionProbe,
    PublicationService,
)
from punto.schemas.authority import TargetAuthority
from punto.schemas.build import BuildValidationIssue
from punto.schemas.dev import (
    AuthorityDecisionRecord,
    DevelopmentResult,
    DevelopmentStatus,
    RepositoryOperation,
)
from punto.schemas.enums import RiskLevel, TaskStatus
from punto.workspace.target import (
    DevelopmentTarget,
    DevelopmentTargetRegistry,
    VerificationCommand,
    load_development_targets,
)

#: Destino sintético de las pruebas: NUNCA el id de un proyecto real (aislamiento fixture ↔ real).
TARGET_ID = "fixture-target"
WORK_BRANCH = "ai/console-fixture"

FOCUSED = (
    "import pathlib,sys;"
    "texto=pathlib.Path('src/lib/tipos.ts').read_text(encoding='utf-8');"
    "sys.exit(0 if 'Apartamento' in texto else 1)"
)


# --------------------------------------------------------------------------- montaje
def _git(root: Path, *args: str) -> str:
    """Git en el repositorio del montaje."""
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


def _repos(tmp_path: Path) -> tuple[Path, Path]:
    """Repositorio de trabajo con su ``origin`` local (bare), ya empujado.

    Returns:
        ``(repositorio, remoto)``: el destino real de la publicación es el remoto local, así que la
        cadena entera (push incluido) se ejecuta de verdad sin tocar producción.
    """
    remoto = tmp_path / "origin.git"
    remoto.mkdir(parents=True)
    _git(remoto, "init", "--bare", "--initial-branch=main")

    repo = tmp_path / "destino"
    (repo / "src" / "lib").mkdir(parents=True)
    (repo / "src" / "components").mkdir(parents=True)
    (repo / "src" / "lib" / "tipos.ts").write_text(
        "export const TIPOS = ['Casa'];\n", encoding="utf-8"
    )
    (repo / "src" / "lib" / "obsoleto.ts").write_text("export const VIEJO = 1;\n", encoding="utf-8")
    (repo / "src" / "components" / "Rejilla.tsx").write_text(
        "const tipos = ['Casa'];\nexport function Rejilla() { return tipos.length; }\n",
        encoding="utf-8",
    )
    # obsoleto.ts NO está versionado: borrarlo no lo restaura Git, y eso sí es una frontera
    # (un borrado de un fichero versionado es reversible y autónomo: autonomía preautorizada).
    (repo / ".gitignore").write_text("node_modules\nsrc/lib/obsoleto.ts\n", encoding="utf-8")
    _git(repo, "init", "-b", "main")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=console",
        "-c",
        "user.email=console@punto.local",
        "commit",
        "-m",
        "base",
    )
    _git(repo, "remote", "add", "origin", str(remoto))
    _git(repo, "push", "origin", "main")
    _git(repo, "checkout", "-b", WORK_BRANCH)
    return repo, remoto


class _Guion:
    """Cliente guionizado: devuelve las respuestas del guion y apunta los prompts."""

    def __init__(self, router: ProviderRouter, responses: Sequence[Any]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []
        router.register_provider("guionizado", self._factory, model="guionizado-1")

    def _factory(self, model: str) -> _Guion:
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

    def complete_json(self, **kwargs: Any) -> ModelCompletion:
        """Devuelve la siguiente respuesta del guion."""
        self.prompts.append(str(kwargs.get("system_prompt", "")))
        item = self._responses.pop(0) if self._responses else {"changes": []}
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


def _plan(*, cierre: bool = False) -> dict[str, Any]:
    """Plan válido: modifica la fuente canónica (o pide borrar algo existente)."""
    payload: dict[str, Any] = {
        "summary": "unificar la lista de tipos en una sola fuente",
        "files_to_read": ["src/lib/tipos.ts"],
        "files_to_modify": ["src/lib/tipos.ts"],
        "files_to_create": [],
        "files_to_delete": [],
        "verification_commands": ["focused"],
        "risks": ["cambiar la interfaz sin querer"],
        "acceptance_mapping": ["una sola fuente de tipos"],
        "functional_chain": [
            {
                "step": "fuente canónica",
                "description": "tipos en un sitio",
                "verification": "focused",
            }
        ],
    }
    if cierre:
        payload["files_to_delete"] = ["src/lib/obsoleto.ts"]
        payload["files_to_modify"] = ["src/lib/tipos.ts", "src/lib/obsoleto.ts"]
        payload["summary"] = "retirar el fichero obsoleto de tipos"
    return payload


def _cambio(*, borrado: bool = False) -> dict[str, Any]:
    """Cambio válido (o un borrado destructivo, que exige persona)."""
    changes: list[dict[str, Any]] = [
        {
            "path": "src/lib/tipos.ts",
            "operation": "MODIFY",
            "content": "export const TIPOS = ['Casa', 'Apartamento'];\n",
            "reason": "la verificación focalizada mide este fichero",
            "acceptance_criterion": "una sola fuente de tipos",
        }
    ]
    if borrado:
        changes.append(
            {
                "path": "src/lib/obsoleto.ts",
                "operation": "DELETE",
                "reason": "el fichero obsoleto ya no se usa",
                "acceptance_criterion": "una sola fuente de tipos",
            }
        )
    return {"summary": "fuente canónica", "changes": changes}


def _target(repo: Path, *, remoto: Path | str, publicable: bool = True) -> DevelopmentTarget:
    """Destino con verificación real y datos de publicación."""
    return DevelopmentTarget(
        target_id=TARGET_ID,
        repository=repo,
        baseline_sha=_git(repo, "rev-parse", "HEAD"),
        scope_roots=("src",),
        allowed_operations=frozenset(
            {
                RepositoryOperation.READ,
                RepositoryOperation.WRITE,
                RepositoryOperation.CREATE,
                RepositoryOperation.DELETE,
                RepositoryOperation.EXECUTE,
                RepositoryOperation.COMMIT,
            }
        ),
        verification=(
            VerificationCommand(
                name="focused", argv=("python", "-c", FOCUSED), timeout_seconds=60.0
            ),
        ),
        work_branch=WORK_BRANCH,
        max_repair_rounds=2,
        command_timeout_seconds=60.0,
        production_branch="main" if publicable else "",
        production_url="https://produccion.local/" if publicable else "",
        production_marker="PUNTO-OK" if publicable else "",
        publish_remote=str(remoto),
    )


def _ciclo(
    target: DevelopmentTarget, respuestas: Sequence[Any], audit: AuditLogger
) -> DevelopmentCycle:
    """Ciclo de desarrollo real con proveedor guionizado."""
    router = ProviderRouter()
    _Guion(router, respuestas)
    for role in ProviderRole:
        router.assign_role(role, "guionizado")
    return DevelopmentCycle(
        router=router,
        targets=DevelopmentTargetRegistry({TARGET_ID: target}),
        config=DevelopmentConfig(max_repair_rounds=2, max_structural_corrections=2),
        audit=audit,
        policy_engine=PolicyEngine.from_config(),
    )


def _fetch_ok(url: str, timeout: float) -> tuple[int, str]:
    """Producción que responde con el marcador esperado."""
    del timeout
    return 200, "<html>PUNTO-OK</html>"


def _fetch_sin_marcador(url: str, timeout: float) -> tuple[int, str]:
    """Producción que responde, pero sin el cambio esperado."""
    del timeout
    return 200, "<html>versión antigua</html>"


def _app(
    *,
    target: DevelopmentTarget,
    respuestas: Sequence[Any],
    fetch: Any = _fetch_ok,
    allow_remote: bool = True,
    targets: Mapping[str, DevelopmentTarget] | None = None,
) -> tuple[TestClient, AuditLogger, DevelopmentTarget, Any]:
    """Aplicación con la consola montada, ejecución en línea y sonda de producción inyectada.

    ``targets`` permite registrar exactamente lo que devuelve la configuración local (la cadena
    configuración → registro → consola → selector), en vez del destino suelto de la prueba.
    """
    audit = AuditLogger()
    cycle = _ciclo(target, respuestas, audit)
    gates = HumanGate()
    policy = PolicyEngine.from_config()

    def fabrica(destino: DevelopmentTarget) -> PublicationService:
        return PublicationService(
            target_id=destino.target_id,
            repository=destino.repository,
            branch=destino.production_branch,
            url=destino.production_url,
            remote=destino.publish_remote,
            marker=destino.production_marker,
            publisher=GitPublisher(destino.repository),
            probe=ProductionProbe(
                url=destino.production_url,
                marker=destino.production_marker,
                attempts=2,
                delay_seconds=0.0,
                fetch=fetch,
                sleeper=lambda _seconds: None,
            ),
            allow_remote_push=allow_remote,
            audit=audit,
        )

    dependencies = ConsoleDependencies(
        dev_cycle=cycle,
        gates=gates,
        audit=audit,
        policy=policy,
        targets=dict(targets) if targets is not None else {TARGET_ID: target},
        publisher_factory=fabrica,
        run_inline=True,
        environ={},
    )
    application = FastAPI()
    register_dashboard(application)
    register_human_console(application, dependencies)
    return TestClient(application), audit, target, dependencies


@pytest.fixture()
def consola(tmp_path: Path) -> Iterator[tuple[TestClient, Path, Path]]:
    """Consola con un destino publicable y un ciclo que cierra el cambio a la primera."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, _deps = _app(target=target, respuestas=[_plan(), _cambio()])
    yield client, repo, remoto


# ------------------------------------------------------------------- A · crear la tarea
def test_a_una_persona_crea_la_tarea_desde_la_consola(
    consola: tuple[TestClient, Path, Path],
) -> None:
    """A: la consola acepta la solicitud y devuelve una tarea con identidad propia."""
    client, _repo, _remoto = consola

    respuesta = client.post(
        "/console/tasks",
        json={
            "objective": (
                "unificar los tipos de propiedad y que los filtros usen la fuente canónica"
            ),
            "target_id": TARGET_ID,
            "acceptance_criteria": ["una sola fuente de tipos"],
            "scope_paths": ["src"],
        },
    )

    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["target_id"] == TARGET_ID
    assert cuerpo["stage"] == "DEVELOPMENT_COMPLETED"
    assert cuerpo["task_id"] == cuerpo["request_id"], "una sola identidad, una sola traza"


# --------------------------------------------------- B y C · entra en PUNTO y se ve el estado
def test_b_c_la_tarea_entra_en_punto_y_su_estado_es_visible(
    consola: tuple[TestClient, Path, Path],
) -> None:
    """B/C: el ciclo real corre (PELL, plan, autoridad, proveedor, verificación) y se puede leer."""
    client, repo, _remoto = consola

    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()
    detalle = client.get(f"/console/tasks/{tarea['task_id']}").json()

    desarrollo = detalle["development"]
    assert desarrollo["status"] == "DEVELOPMENT_COMPLETED"
    assert desarrollo["functional_chain_result"] == "VERIFIED"
    assert desarrollo["verification"] == [{"name": "focused", "passed": True, "exit_code": 0}]
    assert desarrollo["applied"] == ["src/lib/tipos.ts"]
    assert desarrollo["commit_sha"], "el cambio quedó en un commit local"
    assert desarrollo["published"] is False, "el motor no publica por su cuenta"
    assert (repo / "src" / "lib" / "tipos.ts").read_text(encoding="utf-8").find("Apartamento") >= 0

    listado = client.get("/console/tasks").json()
    assert [item["task_id"] for item in listado["items"]] == [tarea["task_id"]]


# ----------------------------------------------------------------- D · avanza sin gate
def test_d_una_operacion_sin_gate_avanza_normalmente(
    consola: tuple[TestClient, Path, Path],
) -> None:
    """D: sin nada que autorizar, la tarea completa su desarrollo y no hay gates pendientes."""
    client, _repo, _remoto = consola

    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED"
    assert tarea["gates"] == []
    assert client.get("/console/human-gates").json()["pending"] == 0


# ------------------------------------------- E y F · se detiene y el gate real se muestra
def test_e_f_una_operacion_que_necesita_persona_se_detiene_y_el_gate_es_real(
    tmp_path: Path,
) -> None:
    """E/F: un borrado destructivo exige persona; la consola lo detiene y muestra el gate real."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, deps = _app(
        target=target, respuestas=[_plan(cierre=True), _cambio(borrado=True)]
    )

    tarea = client.post(
        "/console/tasks",
        json={"objective": "retirar el fichero obsoleto", "target_id": TARGET_ID},
    ).json()

    assert tarea["stage"] == "WAITING_HUMAN"
    assert tarea["development"]["repair_rounds"] == 0, "no se gastan rondas: decide persona"
    assert len(tarea["gates"]) == 1
    assert (repo / "src" / "lib" / "obsoleto.ts").is_file(), "nada se borró sin autorización"

    gates = client.get("/console/human-gates").json()
    assert gates["pending"] == 1
    gate = gates["items"][0]
    assert gate["is_pending"] is True
    assert gate["task_id"] == tarea["task_id"]
    assert gate["action"] in {"CHANGE_REQUIRES_HUMAN", "CHANGE_OUTSIDE_AUTHORITY"}
    assert gate["risk"] in {"HIGH", "CRITICAL"}
    assert gate["reason"], "el gate explica por qué hace falta una persona"
    assert deps.gates.get(UUID(gate["approval_id"])) is not None


# ---------------------------------------------------------------- G · rechazar impide
def test_g_rechazar_impide_la_operacion(tmp_path: Path) -> None:
    """G: REJECT deja la tarea rechazada, no reanuda y no publica nada."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, _deps = _app(
        target=target, respuestas=[_plan(cierre=True), _cambio(borrado=True)]
    )
    tarea = client.post(
        "/console/tasks",
        json={"objective": "retirar el fichero obsoleto", "target_id": TARGET_ID},
    ).json()
    approval_id = tarea["gates"][0]

    rechazo = client.post(
        f"/console/human-gates/{approval_id}/reject",
        json={"resolved_by": "humano-local", "note": "no se toca ese fichero todavía"},
    )

    assert rechazo.status_code == 200, rechazo.text
    assert rechazo.json()["status"] == "REJECTED"
    assert rechazo.json()["task"]["stage"] == "REJECTED"
    # La operación rechazada no se ejecuta: el fichero sigue ahí y no se reanuda la tarea.
    assert (repo / "src" / "lib" / "obsoleto.ts").is_file()
    reanudar = client.post(f"/console/tasks/{tarea['task_id']}/run")
    assert reanudar.status_code == 409
    # Y publicar sigue siendo imposible: no hay gate de publicación aprobado.
    publicar = client.post(f"/console/tasks/{tarea['task_id']}/publish")
    assert publicar.status_code == 409


# ---------------------------------------------------------------- H · aprobar autoriza
def test_h_aprobar_autoriza_solo_esa_operacion(tmp_path: Path) -> None:
    """H: APPROVE resuelve **ese** gate y no publica nada en producción."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, _deps = _app(
        target=target, respuestas=[_plan(cierre=True), _cambio(borrado=True)]
    )
    tarea = client.post(
        "/console/tasks",
        json={"objective": "retirar el fichero obsoleto", "target_id": TARGET_ID},
    ).json()
    approval_id = tarea["gates"][0]

    aprobacion = client.post(
        f"/console/human-gates/{approval_id}/approve",
        json={"resolved_by": "humano-local", "note": "adelante"},
    )

    assert aprobacion.status_code == 200, aprobacion.text
    cuerpo = aprobacion.json()
    assert cuerpo["status"] == "APPROVED"
    assert cuerpo["task"]["stage"] == "HUMAN_APPROVED"
    # Autorizar una operación de desarrollo no publica ni borra nada.
    assert cuerpo["task"]["publication"] is None
    assert (repo / "src" / "lib" / "obsoleto.ts").is_file()
    assert _refs(remoto) == {"main": _git(repo, "rev-parse", "main")}


# ------------------------------------ I y J · gate de publicación; sin aprobar, sin push
def test_i_j_el_gate_de_publicacion_aparece_y_sin_aprobar_no_se_publica(
    consola: tuple[TestClient, Path, Path],
) -> None:
    """I/J: una tarea lista pide gate de publicación; sin aprobación no se empuja nada."""
    client, repo, remoto = consola
    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()

    gate = client.post(f"/console/tasks/{tarea['task_id']}/production-gate")
    assert gate.status_code == 200, gate.text
    cuerpo = gate.json()
    assert cuerpo["stage"] == "WAITING_PRODUCTION_APPROVAL"
    assert cuerpo["publication"]["approval_id"]
    assert cuerpo["publication"]["commit_sha"] == tarea["development"]["commit_sha"]
    assert _git(repo, "rev-parse", WORK_BRANCH) == tarea["development"]["commit_sha"]

    detalle = client.get(f"/console/tasks/{tarea['task_id']}").json()
    vista = detalle["gates_detail"][0]
    assert vista["kind"] == "publication"
    assert vista["action"] == PRODUCTION_ACTION
    assert vista["destination"]["kind"] == "production"
    assert vista["destination"]["production_branch"] == "main"
    assert vista["destination"]["production_url"].startswith("https://")
    assert vista["verification"], "el gate muestra el resultado de la verificación"

    # Sin aprobación no hay publicación: el remoto de producción sigue en su sitio.
    assert _refs(remoto)["main"] == _git(repo, "rev-parse", "main")
    assert detalle["stage"] == "WAITING_PRODUCTION_APPROVAL"


def test_j_publicar_con_gate_pendiente_no_empuja(consola: tuple[TestClient, Path, Path]) -> None:
    """J: pedir publicar con el gate pendiente no mueve la rama de producción."""
    client, repo, remoto = consola
    antes = _refs(remoto)["main"]
    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()
    client.post(f"/console/tasks/{tarea['task_id']}/production-gate")

    resultado = client.post(f"/console/tasks/{tarea['task_id']}/publish").json()

    assert resultado["stage"] == "PUBLICATION_FAILED"
    assert _refs(remoto)["main"] == antes, "sin aprobación, producción no cambia"
    assert _git(repo, "rev-parse", "main") == antes


# -------------------- K y L · con aprobación, cadena gobernada y verificación real
def test_k_con_aprobacion_se_publica_y_produccion_queda_validada(
    consola: tuple[TestClient, Path, Path],
) -> None:
    """K/L: APPROVE ejecuta la cadena (push real al remoto local) y producción se valida."""
    client, _repo, remoto = consola
    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()
    commit = tarea["development"]["commit_sha"]
    gate = client.post(f"/console/tasks/{tarea['task_id']}/production-gate").json()
    approval_id = gate["publication"]["approval_id"]

    aprobado = client.post(
        f"/console/human-gates/{approval_id}/approve",
        json={"resolved_by": "humano-local", "note": "publicar el cambio validado"},
    ).json()

    assert aprobado["stage"] == "PRODUCTION_VALIDATED", aprobado
    publicacion = aprobado["publication"]
    assert publicacion["stage"] == "PRODUCTION_VALIDATED"
    assert publicacion["push"]["pushed"] is True
    assert publicacion["push"]["ref"] == "refs/heads/main"
    assert publicacion["push"]["argv"][:3] == ["git", "push", "--porcelain"]
    assert publicacion["push"]["sha"] == commit
    assert publicacion["production"]["validated"] is True
    assert publicacion["production"]["status_code"] == 200
    # La evidencia es la del remoto real: la rama de producción apunta al commit aprobado.
    assert _refs(remoto)["main"] == commit
    assert _git(remoto, "log", "-1", "--format=%s", "main") == "feat(punto): unificar los tipos"
    # Y el historial de etapas deja la secuencia completa.
    etapas = [item["stage"] for item in publicacion["history"]]
    assert etapas == [
        "WAITING_PRODUCTION_APPROVAL",
        "PUBLISHING",
        "DEPLOYMENT_VERIFICATION",
        "PRODUCTION_VALIDATED",
    ]


def test_l_publicado_no_es_lo_mismo_que_produccion_validada(tmp_path: Path) -> None:
    """L: el push puede ir bien y producción no confirmarlo: no se declara validado."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, _deps = _app(
        target=target, respuestas=[_plan(), _cambio()], fetch=_fetch_sin_marcador
    )
    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()
    gate = client.post(f"/console/tasks/{tarea['task_id']}/production-gate").json()

    aprobado = client.post(
        f"/console/human-gates/{gate['publication']['approval_id']}/approve",
        json={"resolved_by": "humano-local", "note": "adelante"},
    ).json()

    assert aprobado["stage"] == "DEPLOYMENT_NOT_VERIFIED"
    assert aprobado["stage"] != "PRODUCTION_VALIDATED"
    assert aprobado["publication"]["push"]["pushed"] is True, "el push sí ocurrió"
    assert aprobado["publication"]["production"]["validated"] is False
    assert _refs(remoto)["main"] == tarea["development"]["commit_sha"]


def test_m_el_resultado_final_queda_visible_en_la_consola(
    consola: tuple[TestClient, Path, Path],
) -> None:
    """M: el listado y el detalle muestran la etapa final y la evidencia de la publicación."""
    client, _repo, _remoto = consola
    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()
    gate = client.post(f"/console/tasks/{tarea['task_id']}/production-gate").json()
    client.post(
        f"/console/human-gates/{gate['publication']['approval_id']}/approve",
        json={"resolved_by": "humano-local", "note": "adelante"},
    )

    listado = client.get("/console/tasks").json()["items"][0]
    detalle = client.get(f"/console/tasks/{tarea['task_id']}").json()

    for vista in (listado, detalle):
        assert vista["stage"] == "PRODUCTION_VALIDATED"
        assert vista["publication_stage"] == "PRODUCTION_VALIDATED"
        assert vista["publication"]["production"]["marker_found"] is True


# ------------------------------------------------------- N · los proveedores siguen igual
def test_n_la_configuracion_de_proveedores_sigue_funcionando() -> None:
    """N: la superficie de proveedores del dashboard no cambia con la consola."""
    client = TestClient(create_app())

    catalogo = client.get("/providers")
    roles = client.get("/roles")
    pagina = client.get("/dashboard")

    assert catalogo.status_code == 200
    assert "providers" in catalogo.json()
    assert roles.status_code == 200
    assert pagina.status_code == 200
    assert "Consola humana" in pagina.text
    assert "/console/tasks" in pagina.text, "la consola vive en la misma página"


# --------------------------------------------------- invariantes de la frontera de publicación
def test_la_publicacion_no_empuja_a_un_remoto_no_local_sin_autorizacion(
    tmp_path: Path,
) -> None:
    """Invariante: sin autorización del operador, un remoto remoto no se toca."""
    repo, _remoto = _repos(tmp_path)
    target = _target(repo, remoto=Path("https://github.com/ejemplo/repo.git"))
    client, _audit, _target_obj, _deps = _app(
        target=target, respuestas=[_plan(), _cambio()], allow_remote=False
    )
    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()
    gate = client.post(f"/console/tasks/{tarea['task_id']}/production-gate").json()

    aprobado = client.post(
        f"/console/human-gates/{gate['publication']['approval_id']}/approve",
        json={"resolved_by": "humano-local", "note": "adelante"},
    ).json()

    assert aprobado["stage"] == "PUBLICATION_FAILED"
    assert aprobado["publication"]["error_kind"] == "REMOTE_PUSH_NOT_AUTHORIZED"
    assert aprobado["publication"]["push"] is None, "no se intentó ningún push"


def test_un_destino_sin_datos_de_produccion_no_es_publicable(tmp_path: Path) -> None:
    """Invariante: PUNTO no adivina dónde vive producción."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto, publicable=False)
    client, _audit, _target_obj, _deps = _app(target=target, respuestas=[_plan(), _cambio()])
    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()

    respuesta = client.post(f"/console/tasks/{tarea['task_id']}/production-gate")

    assert respuesta.status_code == 409
    assert "no adivina" in respuesta.json()["detail"]
    destinos = client.get("/console/targets").json()["targets"]
    assert destinos[0]["publishable"] is False


def test_el_plan_de_push_no_admite_opciones_peligrosas() -> None:
    """Invariante: el ``argv`` del push se construye por lista blanca."""
    from punto.publish.production import PublicationRefused, PushPlan

    with pytest.raises(PublicationRefused):
        PushPlan(remote="origin", branch="main", sha="no-es-un-sha").argv()
    with pytest.raises(PublicationRefused):
        PushPlan(remote="-origin", branch="main", sha="a" * 40).argv()
    with pytest.raises(PublicationRefused):
        PushPlan(remote="origin", branch="ma*in", sha="a" * 40).argv()
    with pytest.raises(PublicationRefused):
        PushPlan(remote="--force", branch="main", sha="a" * 40).argv()
    with pytest.raises(PublicationRefused):
        PushPlan(remote="origin", branch="main/../otra", sha="a" * 40).argv()
    plan = PushPlan(remote="origin", branch="main", sha="b" * 40)
    assert plan.argv() == ("git", "push", "--porcelain", "origin", f"{'b' * 40}:refs/heads/main")
    assert plan.refspec.startswith("b" * 40)


def test_un_remoto_con_espacios_es_valido() -> None:
    """Defecto corregido: una ruta real de repositorio lleva espacios y no es un argumento."""
    from punto.publish.production import PushPlan

    plan = PushPlan(
        remote=r"C:\Users\Carlos Funez\Desktop\repo destino\origin.git",
        branch="main",
        sha="c" * 40,
    )

    assert plan.argv()[3] == r"C:\Users\Carlos Funez\Desktop\repo destino\origin.git"


def test_los_gates_no_se_pueden_resolver_dos_veces(
    consola: tuple[TestClient, Path, Path],
) -> None:
    """Invariante: una solicitud resuelta no vuelve a resolverse."""
    client, _repo, _remoto = consola
    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()
    gate = client.post(f"/console/tasks/{tarea['task_id']}/production-gate").json()
    approval_id = gate["publication"]["approval_id"]

    primera = client.post(
        f"/console/human-gates/{approval_id}/reject", json={"resolved_by": "humano-local"}
    )
    segunda = client.post(
        f"/console/human-gates/{approval_id}/approve", json={"resolved_by": "humano-local"}
    )

    assert primera.status_code == 200
    assert segunda.status_code == 409
    # Rechazado ⇒ la publicación no se ejecuta ni por la vía directa.
    directa = client.post(f"/console/tasks/{tarea['task_id']}/publish")
    assert directa.status_code == 409


# ------------------------------------------------ progreso visual · A a H de la representación
def _paso(vista: dict[str, Any], clave: str) -> dict[str, Any]:
    """Etapa del recorrido por su clave."""
    return next(paso for paso in vista["progress"]["steps"] if paso["key"] == clave)


def _estados(vista: dict[str, Any]) -> dict[str, str]:
    """Estado de cada etapa del recorrido por su clave."""
    return {paso["key"]: paso["state"] for paso in vista["progress"]["steps"]}


def test_a_b_c_el_recorrido_de_una_tarea_real_sale_de_estados_reales(
    consola: tuple[TestClient, Path, Path],
) -> None:
    """A/B/C: recorrido visible, porcentaje derivado de etapas reales y tiempo real."""
    client, _repo, _remoto = consola
    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()

    progreso = tarea["progress"]
    assert progreso["total"] == 10
    assert progreso["production_required"] is True
    assert progreso["percent"] == 60
    assert [paso["state"] for paso in progreso["steps"]][:6] == ["COMPLETED"] * 6
    assert _paso(tarea, "APROBACION")["state"] == "CURRENT"
    assert _paso(tarea, "APROBACION")["mark"] == "●"
    assert _paso(tarea, "VALIDACION")["state"] == "PENDING"
    # El porcentaje se recalcula desde las etapas: no hay ningún valor estimado.
    assert progreso["percent"] == progreso["completed"] * 100 // progreso["total"]
    assert progreso["elapsed_seconds"] >= 0
    assert progreso["time_label"].startswith("Tiempo:")
    assert progreso["finished"] is False
    # El detalle y el listado cuentan el mismo recorrido (el reloj es lo único que avanza).
    detalle = client.get(f"/console/tasks/{tarea['task_id']}").json()["progress"]
    listado = client.get("/console/tasks").json()["items"][0]["progress"]
    for vista in (detalle, listado):
        assert vista["percent"] == progreso["percent"]
        assert vista["steps"] == progreso["steps"]
        assert vista["total"] == progreso["total"]


def test_a_cada_etapa_completada_tiene_su_evento_real_en_la_auditoria(tmp_path: Path) -> None:
    """A: lo que el recorrido da por hecho está en la auditoría real de esa tarea."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, deps = _app(target=target, respuestas=[_plan(), _cambio()])
    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()
    auditoria = {evento.event_type.value for evento in deps.audit.by_resource(tarea["task_id"])}

    assert {
        "DEV_PLAN_VALIDATED",
        "DEV_CHANGE_VALIDATED",
        "DEV_VERIFICATION_COMPLETED",
        "DEV_FUNCTIONAL_CHAIN_VERIFIED",
    } <= auditoria
    completadas = {
        paso["key"] for paso in tarea["progress"]["steps"] if paso["state"] == "COMPLETED"
    }
    assert completadas == {
        "SOLICITUD",
        "PLANIFICACION",
        "CONSTRUCCION",
        "VERIFICACION",
        "QA",
        "DESARROLLO",
    }


def test_el_tiempo_avanza_sin_mover_el_porcentaje(
    consola: tuple[TestClient, Path, Path],
) -> None:
    """C/B: el reloj corre, el porcentaje no se mueve por tiempo."""
    client, _repo, _remoto = consola
    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()

    primero = client.get(f"/console/tasks/{tarea['task_id']}").json()["progress"]
    time.sleep(2.0)
    segundo = client.get(f"/console/tasks/{tarea['task_id']}").json()["progress"]

    assert segundo["elapsed_seconds"] > primero["elapsed_seconds"]
    assert segundo["percent"] == primero["percent"]
    assert segundo["completed"] == primero["completed"]


def test_d_el_gate_pendiente_se_ve_como_espera_humana_y_no_como_error(tmp_path: Path) -> None:
    """D: con un gate pendiente, la etapa se marca «!» y la página lo dice con esas palabras."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, _deps = _app(
        target=target, respuestas=[_plan(cierre=True), _cambio(borrado=True)]
    )
    tarea = client.post(
        "/console/tasks",
        json={"objective": "retirar el fichero obsoleto", "target_id": TARGET_ID},
    ).json()

    progreso = tarea["progress"]
    assert progreso["waiting_human"] is True
    assert progreso["waiting_kind"] == "development"
    assert progreso["failed"] is False
    assert "aprobación" in progreso["headline"]
    esperando = [paso for paso in progreso["steps"] if paso["state"] == "WAITING_HUMAN"]
    assert len(esperando) == 1
    assert esperando[0]["key"] == "CONSTRUCCION", "lo que exige persona es el cambio"
    assert esperando[0]["mark"] == "!"
    assert _paso(tarea, "PLANIFICACION")["state"] == "COMPLETED"
    assert _paso(tarea, "DESARROLLO")["state"] == "PENDING", "el ciclo no completó el desarrollo"
    assert "FAILED" not in set(_estados(tarea).values())
    # Y la página lo representa como espera humana, con los botones de decisión.
    pagina = client.get("/console").text
    assert "Esperando tu aprobación" in pagina
    assert 'class="human-wait"' in pagina
    assert "data-approve" in pagina and "data-reject" in pagina


def test_e_aprobar_hace_avanzar_el_recorrido_hasta_el_final(
    consola: tuple[TestClient, Path, Path],
) -> None:
    """E/G: APPROVE mueve el recorrido de «esperando» a producción validada al 100 %."""
    client, _repo, _remoto = consola
    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()
    assert tarea["progress"]["percent"] == 60

    con_gate = client.post(f"/console/tasks/{tarea['task_id']}/production-gate").json()
    assert _paso(con_gate, "APROBACION")["state"] == "WAITING_HUMAN"
    assert _paso(con_gate, "APROBACION")["mark"] == "!"
    assert con_gate["progress"]["waiting_kind"] == "publication"
    assert con_gate["progress"]["percent"] == 60, "esperar no avanza el porcentaje"

    aprobado = client.post(
        f"/console/human-gates/{con_gate['publication']['approval_id']}/approve",
        json={"resolved_by": "humano-local", "note": "adelante"},
    ).json()

    progreso = aprobado["progress"]
    assert aprobado["stage"] == "PRODUCTION_VALIDATED"
    assert progreso["percent"] == 100
    assert progreso["completed"] == progreso["total"] == 10
    assert {paso["state"] for paso in progreso["steps"]} == {"COMPLETED"}
    assert progreso["finished"] is True
    assert progreso["time_label"].startswith("Finalizada en:")
    assert progreso["headline"] == "Producción validada"
    assert progreso["waiting_human"] is False


def test_f_rechazar_deja_el_recorrido_en_fallo_y_nunca_en_100(
    consola: tuple[TestClient, Path, Path],
) -> None:
    """F: REJECTED se ve como fallo del recorrido, con el porcentaje congelado bajo el 100."""
    client, _repo, _remoto = consola
    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()
    con_gate = client.post(f"/console/tasks/{tarea['task_id']}/production-gate").json()

    rechazo = client.post(
        f"/console/human-gates/{con_gate['publication']['approval_id']}/reject",
        json={"resolved_by": "humano-local", "note": "todavía no"},
    ).json()

    progreso = rechazo["task"]["progress"]
    assert rechazo["task"]["stage"] == "REJECTED"
    assert _paso(rechazo["task"], "APROBACION")["state"] == "FAILED"
    assert _paso(rechazo["task"], "APROBACION")["mark"] == "×"  # noqa: RUF001
    assert progreso["rejected"] is True
    assert progreso["failed"] is True
    assert progreso["finished"] is True
    assert progreso["percent"] == 60 < 100
    # Y publicar sigue siendo imposible: el rechazo no se convierte en autorización.
    assert client.post(f"/console/tasks/{tarea['task_id']}/publish").status_code == 409


def test_f_una_produccion_que_no_verifica_muestra_el_fallo_del_despliegue(
    tmp_path: Path,
) -> None:
    """F: el push va bien y la comprobación falla: la etapa real de deployment sale en fallo."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, _deps = _app(
        target=target, respuestas=[_plan(), _cambio()], fetch=_fetch_sin_marcador
    )
    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()
    con_gate = client.post(f"/console/tasks/{tarea['task_id']}/production-gate").json()
    aprobado = client.post(
        f"/console/human-gates/{con_gate['publication']['approval_id']}/approve",
        json={"resolved_by": "humano-local", "note": "adelante"},
    ).json()

    progreso = aprobado["progress"]
    assert aprobado["stage"] == "DEPLOYMENT_NOT_VERIFIED"
    assert _paso(aprobado, "PUBLICACION")["state"] == "COMPLETED"
    assert _paso(aprobado, "DEPLOYMENT")["state"] == "FAILED"
    assert _paso(aprobado, "VALIDACION")["state"] == "PENDING"
    assert progreso["percent"] == 80 < 100
    assert progreso["finished"] is True
    assert "no quedó verificada" in progreso["headline"]


def test_una_tarea_sin_produccion_termina_su_recorrido_en_el_desarrollo(tmp_path: Path) -> None:
    """B/G: sin producción declarada, el objetivo real es el desarrollo y ahí sí llega al 100 %."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto, publicable=False)
    client, _audit, _target_obj, _deps = _app(target=target, respuestas=[_plan(), _cambio()])
    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()

    progreso = tarea["progress"]
    assert progreso["production_required"] is False
    assert progreso["total"] == 6
    assert progreso["percent"] == 100
    assert progreso["finished"] is True
    assert progreso["time_label"].startswith("Finalizada en:")
    assert [paso["key"] for paso in progreso["steps"]][-1] == "DESARROLLO"


def test_h_la_pagina_lleva_el_recorrido_y_los_proveedores_siguen_igual() -> None:
    """H: la página trae el stepper y el tiempo sin tocar la configuración de proveedores."""
    client = TestClient(create_app())

    pagina = client.get("/console").text

    assert 'class="stepper"' in pagina and 'class="bar' in pagina
    assert "progressBlock" in pagina and "tickElapsed" in pagina
    assert "data-elapsed" in pagina
    assert "sk-" not in pagina, "ni claves ni estados con forma de secreto"
    assert client.get("/providers").status_code == 200
    assert client.get("/dashboard").status_code == 200


# ------------------------------------------- registro del destino real en la consola
def test_el_selector_lista_el_destino_por_su_nombre_humano_sin_exponer_la_ruta(
    tmp_path: Path,
) -> None:
    """El destino se ofrece por nombre; la ruta del repositorio no viaja a la interfaz."""
    repo, remoto = _repos(tmp_path)
    target = replace(_target(repo, remoto=remoto), display_name="Punto Inmobiliario HN")
    client, _audit, _target_obj, _deps = _app(target=target, respuestas=[_plan(), _cambio()])

    destinos = client.get("/console/targets").json()["targets"]

    assert len(destinos) == 1
    destino = destinos[0]
    assert destino["name"] == "Punto Inmobiliario HN"
    assert destino["target_id"] == TARGET_ID, "la clave es lo único que viaja en la solicitud"
    assert "repository" not in destino and "path" not in destino
    assert str(repo) not in json.dumps(destinos), "ni la ruta absoluta ni el nombre del puesto"
    # El selector del dashboard pinta ese nombre humano.
    pagina = client.get("/console").text
    assert "target.name || target.target_id" in pagina


def test_la_configuracion_local_llega_al_selector_y_resuelve_al_repositorio_declarado(
    tmp_path: Path,
) -> None:
    """configuración → registro → consola: la clave declarada resuelve al repositorio declarado."""
    repo, remoto = _repos(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "targets.local.yaml").write_text(
        yaml.safe_dump(
            {
                "targets": {
                    TARGET_ID: {
                        "display_name": "Punto Inmobiliario HN",
                        "repository": str(repo),
                        "baseline_sha": _git(repo, "rev-parse", "HEAD"),
                        "scope_roots": ["src"],
                        "allowed_operations": [
                            "READ",
                            "WRITE",
                            "CREATE",
                            "DELETE",
                            "EXECUTE",
                            "COMMIT",
                        ],
                        "work_branch": WORK_BRANCH,
                        "production_branch": "main",
                        "production_url": "https://produccion.local/",
                        "production_marker": "PUNTO-OK",
                        "publish_remote": str(remoto),
                        "verification": {
                            "focused": {"argv": ["python", "-c", FOCUSED], "timeout_seconds": 60.0}
                        },
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    registrados = load_development_targets({"PUNTO_CONFIG_DIR": str(config_dir)})
    client, _audit, _target_obj, deps = _app(
        target=_target(repo, remoto=remoto), respuestas=[_plan(), _cambio()], targets=registrados
    )

    destino = deps.targets[TARGET_ID]
    destinos = client.get("/console/targets").json()["targets"]

    assert destino.repository == repo, "la clave resuelve a la ruta declarada, no a otra"
    assert destino.human_name == "Punto Inmobiliario HN"
    assert destino.baseline_sha == _git(repo, "rev-parse", "HEAD")
    assert destino.command_names() == ("focused",)
    assert [item["name"] for item in destinos] == ["Punto Inmobiliario HN"]
    assert destinos[0]["publishable"] is True, "el destino declara su producción; no se adivina"


def test_un_destino_no_registrado_no_concede_acceso_a_otro_directorio(tmp_path: Path) -> None:
    """Un valor arbitrario del navegador no abre ningún directorio: solo hay claves registradas."""
    repo, remoto = _repos(tmp_path)
    ajeno, _ajeno_remoto = _repos(tmp_path / "ajeno")
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, _deps = _app(target=target, respuestas=[_plan(), _cambio()])

    intentos = (
        "otro-repo",
        "../otro",
        f"{TARGET_ID}/../ajeno",
        "C:/Windows",
        "punto-inmobiliario-hn ",
    )
    for intento in intentos:
        respuesta = client.post(
            "/console/tasks", json={"objective": "tocar otro repositorio", "target_id": intento}
        )
        assert respuesta.status_code == 400, intento
        assert str(repo) not in respuesta.text, "el rechazo no revela el repositorio registrado"
        assert str(ajeno) not in respuesta.text, "ni la ruta del repositorio ajeno"
    # Una ruta arbitrariamente larga ni siquiera entra: el campo del selector está acotado.
    larga = str(ajeno)
    assert len(larga) > 80
    acotado = client.post(
        "/console/tasks", json={"objective": "tocar otro repositorio", "target_id": larga}
    )
    assert acotado.status_code == 422

    # Nada se creó y nada se ejecutó: la frontera no concede trabajo sobre el repositorio ajeno.
    assert client.get("/console/tasks").json()["total"] == 0
    assert client.get("/console/targets").json()["targets"][0]["target_id"] == TARGET_ID
    assert _git(ajeno, "rev-parse", "--abbrev-ref", "HEAD") == "ai/console-fixture"


# ------------------------------------------- evidencia del Human Gate · A a G
def _repo_con_recurso_desconocido(tmp_path: Path) -> tuple[Path, Path]:
    """Repositorio del montaje con un recurso de clase **desconocida** para el sobre de autoridad.

    Un fichero de datos binario ordinario (``src/app/recursos.bin``) no cae en ninguna clase
    conocida del clasificador de recursos, así que el sobre aplica su regla de fallo cerrado y exige
    persona. Es el mismo camino que produjo el gate real: no se fuerza nada desde fuera.

    Las hojas de estilo **sí** son una clase conocida desde AP000-OBS-05 (``application_code``), así
    que el montaje las incluye como recurso normal —el del caso real— junto al que de verdad es
    desconocido.
    """
    repo, remoto = _repos(tmp_path)
    (repo / "src" / "app").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "app" / "globals.css").write_text("body { margin: 0 }\n", encoding="utf-8")
    (repo / "src" / "app" / "recursos.bin").write_bytes(b"\x00\x01datos\x02")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=console",
        "-c",
        "user.email=console@punto.local",
        "commit",
        "-m",
        "estilos base",
    )
    return repo, remoto


def _plan_con_recurso_desconocido() -> dict[str, Any]:
    """Plan válido que toca un recurso de clase desconocida: el sobre responde REQUIRE_HUMAN."""
    payload = _plan()
    payload["files_to_modify"] = [
        "src/lib/tipos.ts",
        "src/app/globals.css",
        "src/app/recursos.bin",
    ]
    payload["summary"] = "reemplazar el mapa de cobertura por uno real"
    return payload


def _cambio_con_css() -> dict[str, Any]:
    """Cambio que acompaña al plan (no llega a aplicarse: el plan exige persona)."""
    payload = _cambio()
    payload["changes"].append(
        {
            "path": "src/app/globals.css",
            "operation": "MODIFY",
            "content": "body { margin: 0; }\n",
            "reason": "el mapa necesita su hoja de estilos",
            "acceptance_criterion": "una sola fuente de tipos",
        }
    )
    return payload


def _tarea_con_plan_que_exige_persona(
    tmp_path: Path, *, objective: str = "poner el mapa real de Honduras"
) -> tuple[TestClient, Path, Path, Any, dict[str, Any]]:
    """Tarea real detenida en ``PLAN_REQUIRES_HUMAN`` con su gate pendiente.

    ``objective`` distingue escenarios que comparten el mismo estado durable (mismo destino): dos
    llamadas con el objetivo por defecto son, a propósito, la misma Task para PUNTO (deduplicación
    general); para una segunda Task realmente distinta en la misma prueba, se pasa un objetivo
    distinto.
    """
    repo, remoto = _repo_con_recurso_desconocido(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, deps = _app(
        target=target,
        respuestas=[
            _plan_con_recurso_desconocido(),
            _plan_con_recurso_desconocido(),
            _cambio_con_css(),
        ],
    )
    tarea = client.post(
        "/console/tasks",
        json={
            "objective": objective,
            "target_id": TARGET_ID,
            "scope_paths": ["src"],
        },
    ).json()
    return client, repo, remoto, deps, tarea


def test_a_el_gate_muestra_la_causa_gobernada_real(tmp_path: Path) -> None:
    """A: el gate HIGH dice qué condición concreta disparó el REQUIRE_HUMAN, no una frase vacía."""
    client, _repo, _remoto, _deps, tarea = _tarea_con_plan_que_exige_persona(tmp_path)

    assert tarea["stage"] == "WAITING_HUMAN", tarea
    gate = client.get("/console/human-gates").json()["items"][0]
    evidencia = gate["evidence"]

    assert gate["action"] == "PLAN_REQUIRES_HUMAN"
    assert gate["risk"] == "HIGH"
    assert gate["policy_outcome"] == "REQUIRE_HUMAN"
    # La causa: el problema real que PUNTO encontró, con su código y su detalle.
    assert evidencia["cause"]["code"] == "PLAN_REQUIRES_HUMAN"
    assert "recurso de clase desconocida" in evidencia["cause"]["detail"]
    # La condición gobernada: la regla que se disparó y la clase de autoridad resultante.
    condicion = evidencia["conditions"][0]
    assert "unknown-resource" in condicion["rules"]
    assert condicion["risk"] == "HIGH"
    assert condicion["outcome"] == "REQUIRE_HUMAN"
    assert condicion["authority_class"] == "HUMAN_GATE_REQUIRED"
    assert condicion["required_evidence"], "el sobre declara qué evidencia exige"
    # El motivo del gate ya no es la frase genérica.
    assert "recurso de clase desconocida" in gate["reason"]
    assert gate["reason"] != (
        "el ciclo se detuvo en PLAN_REQUIRES_HUMAN: hace falta una persona antes de seguir "
        f"con {TARGET_ID}"
    )
    # Y el resultado real del ciclo trae la causa (antes salía sin código ni detalle).
    assert tarea["development"]["error_kind"] == "PLAN_REQUIRES_HUMAN"
    assert tarea["development"]["plan_issues"][0]["code"] == "PLAN_REQUIRES_HUMAN"


def test_b_el_gate_muestra_la_operacion_y_los_recursos_afectados(tmp_path: Path) -> None:
    """B: se ve qué quiere hacer PUNTO, sobre qué recursos y qué autoriza aprobar."""
    client, _repo, _remoto, _deps, _tarea = _tarea_con_plan_que_exige_persona(tmp_path)
    gate = client.get("/console/human-gates").json()["items"][0]
    evidencia = gate["evidence"]

    assert evidencia["operation"]["summary"]
    assert evidencia["operation"]["planned"]["modify"] == [
        "src/lib/tipos.ts",
        "src/app/globals.css",
        "src/app/recursos.bin",
    ]
    # La hoja de estilos entra como recurso normal del plan; el que exige persona es el .bin.
    assert "src/app/globals.css" in evidencia["resources"]["paths"]
    assert "src/app/recursos.bin" in evidencia["resources"]["paths"]
    assert evidencia["resources"]["total"] >= 2
    # El alcance de la autorización, explícito en las dos direcciones.
    assert "esta" in evidencia["authorizes"] or "este" in evidencia["authorizes"]
    assert any("producción" in texto.lower() for texto in evidencia["does_not_authorize"])
    assert all(texto.strip() for texto in evidencia["does_not_authorize"])
    # Y el gate sigue sin resolver: nada se aprobó ni se rechazó solo.
    assert gate["is_pending"] is True
    assert gate["resolved_by"] == ""


def test_c_aprobar_y_rechazar_siguen_ligados_al_mismo_gate(tmp_path: Path) -> None:
    """C: los botones resuelven **ese** gate real; la evidencia no cambia el vínculo."""
    client, _repo, _remoto, _deps, tarea = _tarea_con_plan_que_exige_persona(tmp_path)
    approval_id = tarea["gates"][0]

    aprobacion = client.post(
        f"/console/human-gates/{approval_id}/approve",
        json={"resolved_by": "humano-local", "note": "el mapa entra en el alcance"},
    )

    assert aprobacion.status_code == 200, aprobacion.text
    assert aprobacion.json()["approval_id"] == approval_id
    assert aprobacion.json()["status"] == "APPROVED"
    assert aprobacion.json()["task"]["stage"] == "HUMAN_APPROVED"
    assert client.get("/console/human-gates").json()["pending"] == 0
    # Resolver dos veces sigue siendo imposible.
    doble = client.post(
        f"/console/human-gates/{approval_id}/approve", json={"resolved_by": "humano-local"}
    )
    assert doble.status_code == 409

    # Y en otra tarea (trabajo distinto: mismo destino no la deduplica con la anterior), REJECT
    # deja la tarea rechazada y el gate sin autorización.
    client2, _repo2, _remoto2, _deps2, tarea2 = _tarea_con_plan_que_exige_persona(
        tmp_path / "dos", objective="retirar el widget de clima del panel lateral"
    )
    rechazo = client2.post(
        f"/console/human-gates/{tarea2['gates'][0]}/reject",
        json={"resolved_by": "humano-local", "note": "todavía no"},
    ).json()

    assert rechazo["status"] == "REJECTED"
    assert rechazo["task"]["stage"] == "REJECTED"
    assert rechazo["task"]["progress"]["rejected"] is True


def test_d_la_evidencia_es_la_decision_real_y_no_reescribe_policy_ni_risk(tmp_path: Path) -> None:
    """D: lo que muestra el gate es la decisión que PUNTO ya tomó, sin recalcular nada."""
    client, _repo, _remoto, deps, tarea = _tarea_con_plan_que_exige_persona(tmp_path)
    eventos = [
        dict(evento.metadata)
        for evento in deps.audit.by_resource(tarea["task_id"])
        if evento.event_type.value == "DEV_RISK_EVALUATED"
    ]
    gate = client.get("/console/human-gates").json()["items"][0]
    condicion = gate["evidence"]["conditions"][0]

    assert eventos, "el ciclo dejó registrada su evaluación de riesgo"
    ultimo = eventos[-1]
    assert condicion["risk"] == ultimo["risk"]
    assert condicion["outcome"] == ultimo["outcome"]
    assert condicion["authority_class"] == ultimo["authority_class"]
    assert list(condicion["rules"]) == list(ultimo["rules"])
    assert ultimo["risk"] == "HIGH" and ultimo["outcome"] == "REQUIRE_HUMAN"
    # La decisión del PolicyEngine que liga el gate sigue siendo la suya, intacta.
    assert gate["policy_decision_id"]
    assert gate["policy_outcome"] == "REQUIRE_HUMAN"


def test_e_el_gate_no_filtra_secretos(tmp_path: Path) -> None:
    """E: ni el detalle del ciclo ni el texto del proveedor publican una credencial."""
    secreto = "sk-live-0123456789abcdef"
    dsn = "postgres://usuario:clave@host/base"

    class _CicloConSecreto:
        """Ciclo falso: lo único que la consola le pide es ``run``."""

        def run(self, request: Any) -> DevelopmentResult:
            return DevelopmentResult(
                request_id=request.request_id,
                status=DevelopmentStatus.PLAN_REJECTED,
                error_kind="PLAN_REQUIRES_HUMAN",
                error=f"el sobre devuelve REQUIRE_HUMAN con la clave {secreto}",
                plan_issues=(
                    BuildValidationIssue(
                        code="PLAN_REQUIRES_HUMAN",
                        detail=f"recurso fuera del alcance autónomo: token {secreto}",
                    ),
                ),
                authority_decisions=(
                    AuthorityDecisionRecord(
                        operation="write",
                        outcome="REQUIRE_HUMAN",
                        authority_class="HUMAN_GATE_REQUIRED",
                        risk="HIGH",
                        rules=("unknown-resource",),
                        reasons=(f"la conexión {dsn} no es una comprobación", f"clave {secreto}"),
                        resources=("src/app/globals.css",),
                        required_evidence=("autorización humana explícita",),
                    ),
                ),
                final_scope=("src/app/globals.css",),
            )

    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, deps = _app(target=target, respuestas=[_plan()])
    deps.dev_cycle = _CicloConSecreto()  # type: ignore[assignment]

    tarea = client.post(
        "/console/tasks",
        json={
            "objective": "una tarea cualquiera",
            "target_id": TARGET_ID,
            "scope_paths": ["src"],
        },
    ).json()
    gate = client.get("/console/human-gates").json()["items"][0]

    serializado = json.dumps(tarea, ensure_ascii=False) + json.dumps(gate, ensure_ascii=False)
    assert secreto not in serializado
    assert "clave@host" not in serializado
    assert REDACTED in gate["reason"]
    assert REDACTED in gate["evidence"]["cause"]["detail"]
    assert all(REDACTED in item for item in gate["evidence"]["conditions"][0]["reasons"])
    # Y el gate sigue siendo real y resoluble pese a la redacción.
    assert gate["is_pending"] is True
    assert deps.gates.get(UUID(gate["approval_id"])) is not None


def test_f_un_gate_de_riesgo_menor_o_sin_resultado_no_se_rompe(tmp_path: Path) -> None:
    """F: sin evidencia que mostrar el gate se degrada, no falla."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, deps = _app(target=target, respuestas=[_plan(), _cambio()])
    deps.gates.request(
        task_id=uuid4(),
        action="modify_docs",
        risk=RiskLevel.LOW,
        reason="actualizar la documentación del módulo",
        resume_status=TaskStatus.IN_PROGRESS,
        policy_outcome="ALLOW_WITH_REVIEW",
    )

    vista = client.get("/console/human-gates").json()["items"][0]

    assert vista["risk"] == "LOW"
    assert vista["evidence"]["cause"] == {"code": "", "detail": ""}
    assert vista["evidence"]["conditions"] == []
    assert vista["evidence"]["resources"] == {"paths": [], "total": 0}
    assert vista["evidence"]["authorizes"]
    assert vista["evidence"]["does_not_authorize"]
    assert vista["is_pending"] is True
    assert client.get("/console").status_code == 200, "la página sigue sirviéndose"


def test_g_la_tarea_en_espera_no_se_autoriza_sola(tmp_path: Path) -> None:
    """G: la tarea se queda esperando decisión humana; no se aprueba, rechaza ni continúa sola."""
    client, repo, remoto, _deps, tarea = _tarea_con_plan_que_exige_persona(tmp_path)
    antes = _git(repo, "log", "-1", "--format=%H")

    for _ in range(3):
        detalle = client.get(f"/console/tasks/{tarea['task_id']}").json()
        assert detalle["stage"] == "WAITING_HUMAN"
        assert detalle["publication"] is None
        assert detalle["gates_detail"][0]["is_pending"] is True

    assert client.get("/console/human-gates").json()["pending"] == 1
    # Nada se aplicó ni se confirmó: el repositorio sigue en el mismo commit, sin push.
    assert _git(repo, "log", "-1", "--format=%H") == antes
    assert _git(repo, "status", "--porcelain") == ""
    assert _refs(remoto)["main"] == _git(repo, "rev-parse", "main")
    # Publicar sigue siendo imposible: no hay gate de publicación ni commit aprobado.
    assert client.post(f"/console/tasks/{tarea['task_id']}/publish").status_code == 409


# ------------------------------------------- producción del destino · C, D, E y F
def test_c_un_destino_no_registrado_no_puede_declarar_produccion(tmp_path: Path) -> None:
    """C: la producción solo puede venir de la configuración; el navegador no la reescribe."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, _deps = _app(target=target, respuestas=[_plan(), _cambio()])

    for campo in ("production_branch", "production_url", "production_marker", "publish_remote"):
        intento = client.post(
            "/console/tasks",
            json={
                "objective": "x y z",
                "target_id": TARGET_ID,
                campo: "https://otro.example/",
            },
        )
        assert intento.status_code == 422, f"el cuerpo no admite {campo}"

    # Un destino no registrado no se puede elegir, ni con la producción "a mano".
    ajeno = client.post("/console/tasks", json={"objective": "x y z", "target_id": "otro-destino"})
    assert ajeno.status_code == 400
    # El listado de destinos es de solo lectura: no hay forma de declarar producción desde la API.
    assert client.post("/console/targets", json={}).status_code == 405
    assert client.put("/console/targets", json={}).status_code == 405
    # Y la producción del destino sigue siendo la de su configuración.
    assert (
        client.get("/console/targets").json()["targets"][0]["production_url"]
        == "https://produccion.local/"
    )


def test_d_e_f_el_gate_de_publicacion_usa_la_produccion_declarada_y_no_publica_nada(
    tmp_path: Path,
) -> None:
    """D/E/F: el gate sale con la producción de la configuración, no publica y exige persona."""
    repo, remoto = _repos(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "targets.local.yaml").write_text(
        yaml.safe_dump(
            {
                "targets": {
                    TARGET_ID: {
                        "display_name": "Punto Inmobiliario HN",
                        "repository": str(repo),
                        "baseline_sha": _git(repo, "rev-parse", "HEAD"),
                        "scope_roots": ["src"],
                        "allowed_operations": [
                            "READ",
                            "WRITE",
                            "CREATE",
                            "DELETE",
                            "EXECUTE",
                            "COMMIT",
                        ],
                        "work_branch": WORK_BRANCH,
                        # Producción declarada **solo** aquí: es la configuración confiable del
                        # destino. La URL es a propósito distinta de la del destino de la prueba,
                        # para poder demostrar de dónde sale el dato.
                        "production_branch": "main",
                        "production_url": "https://punto-inmobiliario-hn.example/",
                        "publish_remote": str(remoto),
                        "verification": {
                            "focused": {
                                "argv": ["python", "-c", FOCUSED],
                                "timeout_seconds": 60.0,
                            }
                        },
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    registrados = load_development_targets({"PUNTO_CONFIG_DIR": str(config_dir)})
    client, audit, _target_obj, deps = _app(
        target=_target(repo, remoto=remoto), respuestas=[_plan(), _cambio()], targets=registrados
    )
    assert deps.targets[TARGET_ID].production_url == "https://punto-inmobiliario-hn.example/"

    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()
    assert tarea["stage"] == "DEVELOPMENT_COMPLETED"
    antes = _refs(remoto)

    # D: pedir el gate ya no falla por falta de producción declarada.
    respuesta = client.post(f"/console/tasks/{tarea['task_id']}/production-gate")

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["stage"] == "WAITING_PRODUCTION_APPROVAL"
    assert cuerpo["publication"]["approval_id"]
    vista = client.get("/console/human-gates").json()["items"][0]
    assert vista["kind"] == "publication"
    assert vista["destination"]["production_branch"] == "main"
    assert vista["destination"]["production_url"] == "https://punto-inmobiliario-hn.example/"
    assert vista["is_pending"] is True

    # E: crear el gate no publica ni empuja nada.
    assert _refs(remoto) == antes
    tipos = {evento.event_type.value for evento in audit.by_resource(tarea["task_id"])}
    assert "PUBLICATION_REQUESTED" not in tipos, "crear el gate no arranca la publicación"
    assert "PUBLICATION_PUSHED" not in tipos
    assert "PRODUCTION_VERIFIED" not in tipos
    # El gate real quedó solicitado y auditado **a nombre de la aprobación** (su propio recurso).
    del_gate = {
        evento.event_type.value
        for evento in audit.by_resource(cuerpo["publication"]["approval_id"])
    }
    assert "HUMAN_GATE_CREATED" in del_gate

    # F: sin aprobación humana no hay publicación.
    publicar = client.post(f"/console/tasks/{tarea['task_id']}/publish").json()
    assert publicar["stage"] == "PUBLICATION_FAILED"
    assert _refs(remoto) == antes, "sin persona, la rama de producción no cambia"
    assert client.get("/console/human-gates").json()["pending"] == 1
    despues = {evento.event_type.value for evento in audit.by_resource(tarea["task_id"])}
    assert "PUBLICATION_PUSHED" not in despues
    assert "PRODUCTION_VERIFIED" not in despues


# ------------------------------------------- AP000-R01 · release autónomo condicional
def _autoridad_completa() -> TargetAuthority:
    """Sobre persistente que autoriza la cadena completa (push + despliegue + publicación)."""
    return TargetAuthority(
        local_changes=True,
        commit=True,
        push=True,
        deploy=True,
        production_release=True,
        deploy_mechanism="git-push",
        allowed_branches=("main",),
        declared_fields=(
            "local_changes",
            "commit",
            "push",
            "deploy",
            "production_release",
            "deploy_mechanism",
        ),
    )


def _autoridad_solo_commit() -> TargetAuthority:
    """Sobre que autoriza commit local pero **no** la publicación."""
    return TargetAuthority(
        local_changes=True,
        commit=True,
        declared_fields=("local_changes", "commit"),
    )


def test_ap000_release_autonomo_publica_sin_human_gate(tmp_path: Path) -> None:
    """A/L: autoridad persistente + condiciones verdes ⇒ commit, push, despliegue y verificación."""
    repo, remoto = _repos(tmp_path)
    target = replace(_target(repo, remoto=remoto), authority=_autoridad_completa())
    client, audit, _target_obj, _deps = _app(target=target, respuestas=[_plan(), _cambio()])

    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()

    assert tarea["stage"] == "PRODUCTION_VALIDATED", tarea
    assert tarea["release"]["disposition"] == "AUTO"
    assert tarea["release"]["autonomous"] is True
    assert tarea["release"]["blockers"] == []
    assert tarea["gates"] == [], "la cadena autorizada no pide aprobación humana"
    assert client.get("/console/human-gates").json()["pending"] == 0
    publicacion = tarea["publication"]
    assert publicacion["push"]["pushed"] is True
    assert publicacion["production"]["validated"] is True
    assert publicacion["authority"]["disposition"] == "AUTO"
    assert _refs(remoto)["main"] == tarea["development"]["commit_sha"]
    tipos = {evento.event_type.value for evento in audit.by_resource(tarea["task_id"])}
    assert "RELEASE_AUTHORITY_EVALUATED" in tipos
    assert "AUTONOMOUS_RELEASE_AUTHORIZED" in tipos
    assert "PUBLICATION_PUSHED" in tipos and "PRODUCTION_VERIFIED" in tipos
    assert "HUMAN_GATE_CREATED" not in tipos


def test_ap000_commit_automatico_y_publicacion_con_persona(tmp_path: Path) -> None:
    """K: si el destino autoriza commit pero no producción, el commit sale y publica una persona."""
    repo, remoto = _repos(tmp_path)
    target = replace(_target(repo, remoto=remoto), authority=_autoridad_solo_commit())
    client, _audit, _target_obj, _deps = _app(target=target, respuestas=[_plan(), _cambio()])

    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED"
    assert tarea["development"]["commit_sha"], "el commit del ciclo es automático"
    assert tarea["release"]["disposition"] == "HUMAN_GATE"
    assert tarea["release"]["autonomous"] is False
    assert "production_release" not in tarea["release"]["authorized_operations"]
    assert _refs(remoto)["main"] == _git(repo, "rev-parse", "main"), "no se empujó nada"
    # Y la vía humana sigue disponible para desviaciones materiales.
    assert client.post(f"/console/tasks/{tarea['task_id']}/production-gate").status_code == 200


def test_ap000_el_release_autonomo_falla_cerrado_sin_autoridad(tmp_path: Path) -> None:
    """Fail closed: sin sobre persistente, la ruta autónoma devuelve 409 con las condiciones."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)  # sin bloque authority
    client, _audit, _target_obj, _deps = _app(target=target, respuestas=[_plan(), _cambio()])
    tarea = client.post(
        "/console/tasks",
        json={"objective": "unificar los tipos", "target_id": TARGET_ID, "scope_paths": ["src"]},
    ).json()

    respuesta = client.post(f"/console/tasks/{tarea['task_id']}/release")

    assert respuesta.status_code == 409
    detalle = respuesta.json()["detail"]
    assert detalle["disposition"] == "HUMAN_GATE"
    assert any("sin autorización persistente" in motivo for motivo in detalle["reasons"])
    assert _refs(remoto)["main"] == _git(repo, "rev-parse", "main")
    assert client.get("/console/human-gates").json()["pending"] == 0


def _refs(remoto: Path) -> dict[str, str]:
    """Ramas del remoto con su commit, como evidencia de lo que llegó de verdad."""
    salida = _git(remoto, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
    return {line.split()[0]: line.split()[1] for line in salida.splitlines() if line.strip()}
