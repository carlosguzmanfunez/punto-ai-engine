"""REPAIR PROPOSAL PREFLIGHT — frontera determinista de la propuesta de reparación.

Cubre, sin proveedor real y sin repositorio remoto:

1. los invariantes medibles de la frontera (CREATE/MODIFY/DELETE/RENAME contra el estado real,
   duplicados, contradicciones, cambios sin efecto, reintroducción de lo ya aplicado);
2. la **contabilidad**: corregir una inconsistencia estructural no consume una ronda funcional de
   reparación, y hay un techo explícito para las correcciones;
3. **D-7** reproducido en determinista: propuesta con ampliación aprobada + cambio del recurso
   causal + hermano estructuralmente inválido ⇒ no se aplica nada; la propuesta corregida ⇒ PASS;
4. la frontera de alcance: denegada + cambio, aprobada + cambio válido, aprobada + hermano inválido;
5. el defecto de ``ScopeExpansionRecord``: superar límites da una salida gobernada, nunca un
   ``ValidationError``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from punto.audit.logger import AuditLogger
from punto.orchestrator.dev_cycle import DevelopmentConfig, DevelopmentCycle
from punto.orchestrator.proposal_preflight import (
    CHANGE_ALREADY_APPLIED,
    CHANGE_ALREADY_EXISTS,
    CHANGE_CONFLICTING,
    CHANGE_DUPLICATED,
    CHANGE_MISSING_FILE,
    CHANGE_MISSING_SOURCE,
    CHANGE_WITHOUT_EFFECT,
    PROPOSAL_FEEDBACK_LABEL,
    correction_feedback,
    proposal_preflight,
)
from punto.policy.policy_engine import PolicyEngine
from punto.providers.base import ModelCompletion
from punto.providers.contract import ModelUsage, ProviderRole
from punto.providers.router import ProviderRouter
from punto.schemas.build import BuildRequest
from punto.schemas.dev import (
    ChangeOperation,
    DevelopmentPlan,
    DevelopmentStatus,
    FileChangeProposal,
    FunctionalChainStep,
    RepositoryOperation,
)
from punto.workspace.target import (
    DevelopmentTarget,
    DevelopmentTargetRegistry,
    VerificationCommand,
)

TARGET_ID = "preflight-fixture"
WORK_BRANCH = "ai/skill-layer-baseline"

FOCUSED = (
    "import pathlib,sys;"
    "texto=pathlib.Path('src/lib/tipos.ts').read_text(encoding='utf-8');"
    "print('TIPOS:', texto.strip()[:80]);"
    "sys.exit(0 if 'Apartamento' in texto else 1)"
)
CHAIN = (
    "import pathlib,sys;"
    "fuente=pathlib.Path('src/lib/tipos.ts').read_text(encoding='utf-8');"
    "consumidores=[pathlib.Path(p) for p in "
    "('src/components/Rejilla.tsx','src/components/Buscador.tsx')];"
    "ok=all('@/lib/tipos' in c.read_text(encoding='utf-8') for c in consumidores);"
    "print('CADENA:', ok);"
    "sys.exit(0 if ok and 'Apartamento' in fuente else 1)"
)

TIPOS = "export const TIPOS = ['Casa'];\n"
CONSUMIDOR = "const tipos = ['Casa'];\nexport function X() { return tipos.length; }\n"
CONSUMIDOR_FUENTE = (
    "import { TIPOS } from '@/lib/tipos';\nexport function X() { return TIPOS.length; }\n"
)
TEST_NUEVO = "// cadena de tipos\n"

_POLICY_ENGINE = PolicyEngine.from_config()


# --------------------------------------------------------------- 1. frontera estructural (unidad)
def _estado(paths: Mapping[str, str]) -> tuple[Any, Any]:
    """Estado del workspace como mapa ruta → contenido (las rutas ausentes no están)."""

    def exists(path: str) -> bool:
        return path in paths

    def read_text(path: str) -> str:
        return paths[path]

    return exists, read_text


def _cambio(
    path: str, operation: str, content: str = "", source: str = ""
) -> FileChangeProposal:
    """Cambio del contrato real."""
    payload: dict[str, Any] = {"path": path, "operation": operation}
    if content:
        payload["content"] = content
    if source:
        payload["source_path"] = source
    return FileChangeProposal.model_validate(payload)


def _preflight(
    cambios: Sequence[FileChangeProposal], estado: Mapping[str, str]
) -> Any:
    """Preflight contra un estado dado (ruta → contenido)."""
    exists, read_text = _estado(estado)
    return proposal_preflight(cambios, exists=exists, read_text=read_text)


def test_1_create_sobre_algo_que_existe_se_detecta_antes_de_aplicar() -> None:
    """CREATE + EXISTS: el hecho que se puede medir, dicho con el estado real."""
    resultado = _preflight(
        [_cambio("src/lib/tipos.ts", "CREATE", "x\n")], {"src/lib/tipos.ts": TIPOS}
    )

    assert resultado.valid is False
    assert resultado.correctable is True
    issue = resultado.blocking[0]
    assert issue.code == CHANGE_ALREADY_EXISTS
    assert (issue.actual_state, issue.expected_state) == ("EXISTS", "MISSING")
    assert issue.path == "src/lib/tipos.ts"
    assert issue.operation == "CREATE"


def test_2_create_sobre_algo_que_no_existe_pasa() -> None:
    """CREATE + MISSING: no hay nada que corregir."""
    assert _preflight([_cambio("src/nuevo.ts", "CREATE", "x\n")], {}).valid is True


def test_3_modify_sobre_algo_que_existe_pasa() -> None:
    """MODIFY + EXISTS con contenido distinto: propuesta válida."""
    estado = {"src/lib/tipos.ts": TIPOS}

    assert _preflight([_cambio("src/lib/tipos.ts", "MODIFY", "otra cosa\n")], estado).valid


def test_4_modify_sobre_algo_que_no_existe_se_detecta() -> None:
    """MODIFY + MISSING: la operación correcta es CREATE."""
    resultado = _preflight([_cambio("src/nuevo.ts", "MODIFY", "x\n")], {})

    assert [item.code for item in resultado.blocking] == [CHANGE_MISSING_FILE]
    assert resultado.blocking[0].actual_state == "MISSING"
    assert resultado.blocking[0].expected_state == "EXISTS"


def test_5_delete_sobre_algo_que_existe_pasa() -> None:
    """DELETE + EXISTS: no hay nada que corregir."""
    assert _preflight(
        [_cambio("src/lib/opciones.ts", "DELETE")], {"src/lib/opciones.ts": "x\n"}
    ).valid


def test_6_delete_sobre_algo_que_no_existe_se_detecta() -> None:
    """DELETE + MISSING: no hay nada que borrar (antes se descubría al aplicar)."""
    resultado = _preflight([_cambio("src/fantasma.ts", "DELETE")], {})

    assert [item.code for item in resultado.blocking] == [CHANGE_MISSING_FILE]


def test_7_el_mismo_cambio_dos_veces_se_detecta() -> None:
    """Duplicado exacto: una operación por ruta."""
    cambios = [
        _cambio("src/lib/tipos.ts", "MODIFY", "a\n"),
        _cambio("src/lib/tipos.ts", "MODIFY", "a\n"),
    ]

    resultado = _preflight(cambios, {"src/lib/tipos.ts": TIPOS})

    assert [item.code for item in resultado.blocking] == [CHANGE_DUPLICATED]


def test_8_dos_operaciones_contradictorias_sobre_la_misma_ruta_se_detectan() -> None:
    """Contradicción: MODIFY y DELETE del mismo recurso en la misma propuesta."""
    cambios = [
        _cambio("src/lib/tipos.ts", "MODIFY", "a\n"),
        _cambio("src/lib/tipos.ts", "DELETE"),
    ]

    resultado = _preflight(cambios, {"src/lib/tipos.ts": TIPOS})

    assert [item.code for item in resultado.blocking] == [CHANGE_CONFLICTING]
    assert resultado.blocking[0].operation == "DELETE|MODIFY"
    assert resultado.blocking[0].actual_state == "DELETE,MODIFY"


def test_9_rename_sin_origen_y_con_destino_ocupado_se_detectan() -> None:
    """RENAME/MOVE: el origen tiene que estar y el destino libre."""
    sin_origen = _preflight(
        [_cambio("src/nuevo.ts", "MOVE", source="src/fantasma.ts")], {}
    )
    ocupado = _preflight(
        [_cambio("src/ocupado.ts", "MOVE", source="src/lib/tipos.ts")],
        {"src/lib/tipos.ts": TIPOS, "src/ocupado.ts": "x\n"},
    )

    assert [item.code for item in sin_origen.blocking] == [CHANGE_MISSING_SOURCE]
    assert [item.code for item in ocupado.blocking] == [CHANGE_ALREADY_EXISTS]


def test_10_un_cambio_ya_aplicado_avisa_pero_no_bloquea() -> None:
    """Reintroducir un cambio ya aplicado: se registra, no se cobra una corrección."""
    cambios = [
        _cambio("src/lib/tipos.ts", "MODIFY", TIPOS),
        _cambio("src/components/Rejilla.tsx", "MODIFY", "cambio real\n"),
    ]
    estado = {"src/lib/tipos.ts": TIPOS, "src/components/Rejilla.tsx": "viejo\n"}

    resultado = _preflight(cambios, estado)

    assert resultado.valid is True
    assert [item.code for item in resultado.advisory] == [CHANGE_ALREADY_APPLIED]
    assert resultado.advisory[0].blocking is False


def test_11_una_propuesta_que_no_cambia_nada_se_detecta() -> None:
    """Si todos los cambios dejarían el árbol igual, no merece una ronda: se bloquea con hecho."""
    cambios = [_cambio("src/lib/tipos.ts", "MODIFY", TIPOS)]

    resultado = _preflight(cambios, {"src/lib/tipos.ts": TIPOS})

    assert [item.code for item in resultado.blocking] == [CHANGE_WITHOUT_EFFECT]
    assert resultado.correctable is True


def test_12_el_feedback_es_compacto_y_estructurado() -> None:
    """La corrección que viaja al proveedor es corta y lleva el hecho, no el prompt entero."""
    resultado = _preflight(
        [_cambio("tests/tipos-chain.test.ts", "CREATE", TEST_NUEVO)],
        {"tests/tipos-chain.test.ts": TEST_NUEVO},
    )

    feedback = correction_feedback(resultado)

    assert feedback.startswith(PROPOSAL_FEEDBACK_LABEL)
    assert '"code":"CHANGE_ALREADY_EXISTS"' in feedback.replace(" ", "")
    assert '"actual_state":"EXISTS"' in feedback.replace(" ", "")
    assert len(feedback) < 900, "el feedback de corrección no es un ensayo"
    assert correction_feedback(_preflight([_cambio("src/nuevo.ts", "CREATE", "x\n")], {})) == ""


def test_13_el_preflight_es_determinista_y_no_transforma_operaciones() -> None:
    """Mismos hechos, mismo resultado; y PUNTO no cambia CREATE por MODIFY por su cuenta."""
    cambios = [_cambio("src/lib/tipos.ts", "CREATE", "x\n")]
    estado = {"src/lib/tipos.ts": TIPOS}

    primera = _preflight(cambios, estado)
    segunda = _preflight(cambios, estado)

    assert primera.as_dict() == segunda.as_dict()
    assert primera.changes[0].operation == ChangeOperation.CREATE.value, "la operación no se toca"
    assert primera.as_dict()["changes"][0]["path"] == "src/lib/tipos.ts"


# ----------------------------------------------------- 2. ciclo real: contabilidad y D-7 (e2e)
def _git(root: Path, *args: str) -> str:
    """Git para preparar el repositorio del montaje."""
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
    """Repositorio fixture: fuente canónica, dos consumidores y un ``.gitignore`` ya tocado."""
    repo = tmp_path / "destino"
    (repo / "src" / "lib").mkdir(parents=True)
    (repo / "src" / "components").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "lib" / "tipos.ts").write_text(TIPOS, encoding="utf-8")
    (repo / "src" / "components" / "Rejilla.tsx").write_text(
        CONSUMIDOR.replace("function X", "function Rejilla"), encoding="utf-8"
    )
    (repo / "src" / "components" / "Buscador.tsx").write_text(
        CONSUMIDOR.replace("function X", "function Buscador"), encoding="utf-8"
    )
    (repo / ".gitignore").write_text("node_modules\n", encoding="utf-8")
    _git(repo, "init", "-b", "main")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=preflight",
        "-c",
        "user.email=preflight@punto.local",
        "commit",
        "-m",
        "base",
    )
    _git(repo, "checkout", "-b", WORK_BRANCH)
    (repo / ".gitignore").write_text("node_modules\n.env.local\n", encoding="utf-8")
    return repo


class _Cliente:
    """Cliente guionizado que guarda los prompts recibidos."""

    def __init__(self, router: ProviderRouter, responses: Sequence[Any]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []
        router.register_provider("guionizado", self._factory, model="guionizado-1")

    def _factory(self, model: str) -> _Cliente:
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
        item = self._responses.pop(0) if self._responses else {"changes": []}
        import json as _json

        content = item if isinstance(item, str) else _json.dumps(item)
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


def _plan() -> dict[str, Any]:
    """Plan con los dos consumidores y el fichero de prueba por crear."""
    return {
        "summary": "unificar la lista de tipos en una sola fuente",
        "files_to_read": ["src/lib/tipos.ts", "src/components/Rejilla.tsx"],
        "files_to_modify": ["src/components/Rejilla.tsx", "src/components/Buscador.tsx"],
        "files_to_create": ["tests/tipos-chain.test.ts"],
        "files_to_delete": [],
        "verification_commands": ["focused", "chain"],
        "risks": ["cambiar la interfaz sin querer"],
        "acceptance_mapping": ["una sola fuente de tipos"],
        "functional_chain": [
            {
                "step": "fuente canónica",
                "description": "tipos en un sitio",
                "verification": "focused",
            },
            {"step": "consumidores", "description": "la usan", "verification": "chain"},
        ],
    }


def _impl_inicial() -> dict[str, Any]:
    """Ronda 0: consumidores + fichero de prueba (el CREATE que luego será inválido)."""
    return {
        "summary": "consumidores y prueba",
        "changes": [
            {
                "path": "src/components/Rejilla.tsx",
                "operation": "MODIFY",
                "content": CONSUMIDOR_FUENTE.replace("function X", "function Rejilla"),
                "reason": "consumir la fuente",
                "acceptance_criterion": "una sola fuente de tipos",
            },
            {
                "path": "src/components/Buscador.tsx",
                "operation": "MODIFY",
                "content": CONSUMIDOR_FUENTE.replace("function X", "function Buscador"),
                "reason": "consumir la fuente",
                "acceptance_criterion": "una sola fuente de tipos",
            },
            {
                "path": "tests/tipos-chain.test.ts",
                "operation": "CREATE",
                "content": TEST_NUEVO,
                "reason": "dejar la cadena cubierta",
                "acceptance_criterion": "una sola fuente de tipos",
            },
        ],
    }


def _propuesta_d7(incluir_expansion: bool = True) -> dict[str, Any]:
    """La propuesta de D-7: recurso causal en la misma respuesta y hermano inválido."""
    payload: dict[str, Any] = {
        "summary": "resolver la causa: la fuente no declara el tipo",
        "changes": [
            {
                "path": "src/lib/tipos.ts",
                "operation": "MODIFY",
                "content": "export const TIPOS = ['Casa', 'Apartamento'];\n",
                "reason": "la verificación focalizada mide este fichero",
                "acceptance_criterion": "una sola fuente de tipos",
            },
            {
                "path": "tests/tipos-chain.test.ts",
                "operation": "CREATE",
                "content": TEST_NUEVO,
                "reason": "reintroducir la prueba",
                "acceptance_criterion": "una sola fuente de tipos",
            },
        ],
        "root_cause": "la fuente canónica no declara Apartamento",
        "evidence": ["focused exit 1: TIPOS: export const TIPOS = ['Casa'];"],
        "expected_effect": "la verificación focalizada encuentra el tipo exigido",
    }
    if incluir_expansion:
        payload["scope_expansion"] = {
            "trigger": "evidencia de la verificación focalizada",
            "evidence": ["focused mide src/lib/tipos.ts y el plan no lo autoriza"],
            "root_cause": "es la fuente canónica del dato que la verificación exige",
            "resources": ["src/lib/tipos.ts"],
            "operations": ["MODIFY"],
            "relationship": "fuente canónica de la misma cadena funcional",
        }
    return payload


def _propuesta_corregida() -> dict[str, Any]:
    """La misma reparación, con la operación del hermano ajustada al estado real."""
    return {
        "summary": "resolver la causa con la operación correcta",
        "changes": [
            {
                "path": "src/lib/tipos.ts",
                "operation": "MODIFY",
                "content": "export const TIPOS = ['Casa', 'Apartamento'];\n",
                "reason": "la verificación focalizada mide este fichero",
                "acceptance_criterion": "una sola fuente de tipos",
            },
            {
                "path": "tests/tipos-chain.test.ts",
                "operation": "MODIFY",
                "content": "// cadena de tipos revisada\n",
                "reason": "la prueba ya existe: se modifica",
                "acceptance_criterion": "una sola fuente de tipos",
            },
        ],
        "root_cause": "la fuente canónica no declara Apartamento",
        "evidence": ["focused exit 1: TIPOS: export const TIPOS = ['Casa'];"],
        "expected_effect": "la verificación focalizada encuentra el tipo exigido",
    }


def _run_e2e(
    tmp_path: Path,
    responses: Sequence[Any],
    *,
    max_repair_rounds: int = 2,
    max_structural_corrections: int = 2,
) -> tuple[Any, list[dict[str, Any]], _Cliente]:
    """Ejecuta el ciclo real contra el fixture."""
    repo = _repo(tmp_path)
    target = DevelopmentTarget(
        target_id=TARGET_ID,
        repository=repo,
        baseline_sha=_git(repo, "rev-parse", "HEAD"),
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
            VerificationCommand(name=name, argv=tuple(argv), timeout_seconds=60.0)
            for name, argv in {
                "focused": ("python", "-c", FOCUSED),
                "chain": ("python", "-c", CHAIN),
            }.items()
        ),
        work_branch=WORK_BRANCH,
        max_repair_rounds=max_repair_rounds,
        command_timeout_seconds=60.0,
    )
    router = ProviderRouter()
    client = _Cliente(router, responses)
    for role in ProviderRole:
        router.assign_role(role, "guionizado")
    audit = AuditLogger()
    cycle = DevelopmentCycle(
        router=router,
        targets=DevelopmentTargetRegistry({TARGET_ID: target}),
        config=DevelopmentConfig(
            max_repair_rounds=max_repair_rounds,
            max_structural_corrections=max_structural_corrections,
        ),
        audit=audit,
        policy_engine=_POLICY_ENGINE,
    )
    request = BuildRequest(
        objective="diseñar la fuente canónica de tipos y su cadena funcional completa",
        target_repository=TARGET_ID,
        requested_role=ProviderRole.BUILDER,
        acceptance_criteria=("una sola fuente de tipos",),
        scope_paths=("src",),
    )
    result = cycle.run(request)
    detalle = [
        {"event": event.event_type.value, **dict(event.metadata)}
        for event in audit.by_resource(str(request.request_id))
    ]
    return result, detalle, client


def _meta(detalle: Sequence[Mapping[str, Any]], evento: str) -> list[dict[str, Any]]:
    """Metadatos de un tipo de evento, en orden."""
    return [dict(item) for item in detalle if item["event"] == evento]


def test_14_d7_la_propuesta_invalida_no_llega_a_aplicarse_ni_gasta_ronda(tmp_path: Path) -> None:
    """§12: ampliación + MODIFY del recurso causal + hermano inválido ⇒ corrección estructural."""
    responses = [
        _plan(),
        _impl_inicial(),
        _propuesta_d7(),  # inválida: CREATE sobre el fichero de prueba que ya existe
        _propuesta_corregida(),  # corregida en la misma ronda funcional
    ]
    result, detalle, client = _run_e2e(tmp_path, responses)

    # La ampliación se aprobó (el orden autoridad → cambio se mantiene) y el cambio no se aplicó.
    assert len(_meta(detalle, "DEV_SCOPE_EXPANSION_APPROVED")) == 1
    fallos = _meta(detalle, "DEV_PROPOSAL_PREFLIGHT_FAILED")
    assert len(fallos) == 1
    assert fallos[0]["issue_codes"] == (CHANGE_ALREADY_EXISTS,)
    assert fallos[0]["repair_round_consumed"] is False
    assert fallos[0]["structural_correction"] == 1
    assert result.structural_corrections == 1
    # El feedback llega al proveedor en la invocación siguiente, y es corto.
    assert PROPOSAL_FEEDBACK_LABEL in client.prompts[3]
    assert "tests/tipos-chain.test.ts" in client.prompts[3]
    # Y el caso cierra en **una** ronda funcional: la corrección no consumió ronda.
    assert result.status is DevelopmentStatus.COMPLETED
    assert result.repair_rounds == 1
    assert result.functional_chain_result == "VERIFIED"
    assert {item.path for item in result.applied} >= {"src/lib/tipos.ts"}


def test_15_d7_sin_la_correccion_el_ciclo_no_gira_sin_limite(tmp_path: Path) -> None:
    """El techo de correcciones es explícito: agotado, se vuelve al camino de siempre."""
    responses = [_plan(), _impl_inicial(), _propuesta_d7(), _propuesta_d7(), _propuesta_d7()]
    result, detalle, _client = _run_e2e(tmp_path, responses, max_structural_corrections=2)

    assert result.structural_corrections == 2, "ni una más que el techo"
    assert len(_meta(detalle, "DEV_PROPOSAL_PREFLIGHT_FAILED")) == 2
    # Agotado el techo, la tercera propuesta inválida sigue el camino normal (con su código real).
    assert result.status is not DevelopmentStatus.COMPLETED
    assert any(
        code == "CHANGE_ALREADY_EXISTS" for code in result.change_issues
    ) or any(
        item["issue_codes"] == ("CHANGE_ALREADY_EXISTS",)
        for item in _meta(detalle, "DEV_CHANGE_REJECTED")
    )
    assert result.repair_rounds <= 2


def test_16_con_el_techo_en_cero_el_ciclo_se_comporta_como_antes(tmp_path: Path) -> None:
    """Compatibilidad: sin correcciones estructurales, la ruta es la de siempre."""
    responses = [_plan(), _impl_inicial(), _propuesta_d7(), _propuesta_corregida()]
    result, detalle, _client = _run_e2e(
        tmp_path, responses, max_structural_corrections=0
    )

    assert _meta(detalle, "DEV_PROPOSAL_PREFLIGHT_FAILED") == []
    assert result.structural_corrections == 0
    rechazos = _meta(detalle, "DEV_CHANGE_REJECTED")
    assert rechazos and rechazos[0]["issue_codes"] == ("CHANGE_ALREADY_EXISTS",)
    assert rechazos[0]["round"] == 1


def test_16b_source_path_ambiguo_vuelve_al_builder_y_conserva_la_causa(tmp_path: Path) -> None:
    """MOVE sin origen se corrige antes de apply y no degenera en VERIFICATION_FAILED genérico."""
    ambiguo = _propuesta_d7()
    ambiguo["changes"] = [
        {
            "path": "src/lib/tipos.ts",
            "operation": "MOVE",
            "source_path": "",
            "reason": "mover la fuente",
            "acceptance_criterion": "una sola fuente de tipos",
        }
    ]
    responses = [_plan(), _impl_inicial(), ambiguo, _propuesta_corregida()]

    result, detalle, client = _run_e2e(tmp_path, responses)

    fallos = _meta(detalle, "DEV_PROPOSAL_PREFLIGHT_FAILED")
    assert any(item["issue_codes"] == ("CHANGE_SOURCE_PATH_REQUIRED",) for item in fallos)
    assert "CHANGE_SOURCE_PATH_REQUIRED" in client.prompts[3]
    assert "no inventará una ruta" in client.prompts[3]
    assert result.status is DevelopmentStatus.COMPLETED


def test_16c_source_path_ambiguo_agotado_termina_con_codigo_causal(tmp_path: Path) -> None:
    """Sin presupuesto de corrección, el resultado expone la causa útil, nunca el genérico."""
    ambiguo = _propuesta_corregida()
    ambiguo["changes"] = [
        {
            "path": "src/lib/tipos.ts",
            "operation": "RENAME",
            "reason": "renombrar la fuente",
            "acceptance_criterion": "una sola fuente de tipos",
        }
    ]
    responses = [_plan(), _impl_inicial(), ambiguo]

    result, _detalle, _client = _run_e2e(
        tmp_path, responses, max_structural_corrections=0
    )

    assert result.status is DevelopmentStatus.CHANGE_REJECTED
    assert result.error_kind == "CHANGE_SOURCE_PATH_REQUIRED"
    assert result.change_issues[0].code == "CHANGE_SOURCE_PATH_REQUIRED"
    assert "no inventará una ruta" in result.error


def test_17_una_propuesta_repetida_ya_aplicada_no_gasta_ronda(tmp_path: Path) -> None:
    """Reintroducir exactamente lo ya aplicado se detecta antes de verificar."""
    responses = [
        _plan(),
        _impl_inicial(),
        {
            "summary": "lo mismo otra vez",
            "changes": [
                {
                    "path": "src/components/Rejilla.tsx",
                    "operation": "MODIFY",
                    "content": CONSUMIDOR_FUENTE.replace("function X", "function Rejilla"),
                    "reason": "repetir",
                    "acceptance_criterion": "una sola fuente de tipos",
                },
                {
                    "path": "src/components/Buscador.tsx",
                    "operation": "MODIFY",
                    "content": CONSUMIDOR_FUENTE.replace("function X", "function Buscador"),
                    "reason": "repetir",
                    "acceptance_criterion": "una sola fuente de tipos",
                },
            ],
            "root_cause": "los consumidores no consumen la fuente",
            "evidence": ["chain exit 1"],
            "expected_effect": "consumir la fuente",
        },
        # Corrige de verdad: cambia la fuente (ya autorizada por la ampliación de la ronda anterior
        # no existe aquí, así que se pide en esta misma respuesta).
        {
            "summary": "la fuente",
            "changes": [
                {
                    "path": "src/lib/tipos.ts",
                    "operation": "MODIFY",
                    "content": "export const TIPOS = ['Casa', 'Apartamento'];\n",
                    "reason": "la verificación mide este fichero",
                    "acceptance_criterion": "una sola fuente de tipos",
                }
            ],
            "root_cause": "la fuente no declara el tipo",
            "evidence": ["focused exit 1"],
            "expected_effect": "focused pasa",
            "scope_expansion": {
                "trigger": "evidencia",
                "evidence": ["focused mide src/lib/tipos.ts"],
                "root_cause": "fuente canónica",
                "resources": ["src/lib/tipos.ts"],
                "operations": ["MODIFY"],
                "relationship": "misma cadena funcional",
            },
        },
    ]
    result, detalle, _client = _run_e2e(tmp_path, responses)

    fallos = _meta(detalle, "DEV_PROPOSAL_PREFLIGHT_FAILED")
    assert [item["issue_codes"] for item in fallos] == [(CHANGE_WITHOUT_EFFECT,)]
    assert result.structural_corrections == 1
    assert result.status is DevelopmentStatus.COMPLETED
    assert result.repair_rounds == 1


# ------------------------------------------------------------ 3. frontera de alcance y §13
def test_18_alcance_denegado_con_cambio_no_aplica_nada(tmp_path: Path) -> None:
    """Ampliación sin evidencia ⇒ DENIED; el cambio del recurso no autorizado no se aplica."""
    propuesta = _propuesta_corregida()
    propuesta["scope_expansion"] = {
        "trigger": "evidencia",
        "evidence": [],
        "root_cause": "fuente canónica",
        "resources": ["src/lib/tipos.ts"],
        "operations": ["MODIFY"],
        "relationship": "misma cadena funcional",
    }
    result, detalle, _client = _run_e2e(
        tmp_path, [_plan(), _impl_inicial(), propuesta], max_repair_rounds=1
    )

    assert _meta(detalle, "DEV_SCOPE_EXPANSION_DENIED") != []
    assert _meta(detalle, "DEV_SCOPE_EXPANSION_APPROVED") == []
    assert _meta(detalle, "DEV_PLAN_REVISED") == []
    assert "src/lib/tipos.ts" not in {item.path for item in result.applied}
    assert "CHANGE_NOT_IN_PLAN" in {issue.code for issue in result.change_issues}
    assert result.status is not DevelopmentStatus.COMPLETED


def test_19_una_ampliacion_grande_da_una_salida_gobernada_y_no_un_error(tmp_path: Path) -> None:
    """§13: superar límites produce DENIED/HUMAN_GATE con totales, nunca un ``ValidationError``."""
    from punto.orchestrator.dev_cycle import DevelopmentCycle as _Cycle

    ciclo = _Cycle(
        router=ProviderRouter(),
        targets=DevelopmentTargetRegistry({}),
        config=DevelopmentConfig(),
        audit=AuditLogger(),
    )
    plan = DevelopmentPlan(
        summary="unificar la fuente",
        files_to_modify=("src/components/Rejilla.tsx", "src/components/Buscador.tsx"),
        verification_commands=("focused", "chain"),
        acceptance_mapping=("una sola fuente de tipos",),
        functional_chain=(
            FunctionalChainStep(step="fuente", verification="focused"),
            FunctionalChainStep(step="consumidores", verification="chain"),
        ),
    )
    request = BuildRequest(
        objective="unificar",
        target_repository=TARGET_ID,
        requested_role=ProviderRole.BUILDER,
        acceptance_criteria=("una sola fuente de tipos",),
    )

    class _Repo:
        def exists(self, path: str) -> bool:
            del path
            return False

    with_evidence = {
        "trigger": "evidencia",
        "evidence": ["focused exit 1"],
        "root_cause": "fuente canónica",
        "resources": [f"src/lib/nuevo-{indice:03d}.ts" for indice in range(45)],
        "operations": ["MODIFY"],
        "relationship": "misma cadena funcional",
    }
    devuelto, estado = ciclo._handle_scope_expansion(
        request=request,
        plan=plan,
        repository=_Repo(),  # type: ignore[arg-type]
        payload=with_evidence,
        round_index=1,
    )

    assert estado == "HUMAN_GATE", "sin crash: la frontera responde con una decisión gobernada"
    assert devuelto is plan, "sin autorización no hay plan nuevo"
    detalle = [
        {"event": event.event_type.value, **dict(event.metadata)}
        for event in ciclo.audit.by_resource(str(request.request_id))  # type: ignore[union-attr]
    ]
    negadas = _meta(detalle, "DEV_SCOPE_EXPANSION_DENIED")
    assert negadas and negadas[0]["outcome"] == "REQUIRE_HUMAN"
    # El contrato acota la petición a 40 rutas: la evidencia dice cuántas se declararon y cuántas
    # entraron, para que un recorte de límite no sea silencioso.
    assert negadas[0]["requested_declared"] == 45
    assert negadas[0]["requested_total"] == 40
    assert negadas[0]["cumulative_total"] == 42, "40 pedidas + 2 del plan"
    assert len(negadas[0]["resources"]) == 40


def test_20_una_ampliacion_grande_sin_evidencia_se_deniega_sin_error() -> None:
    """§13: la denegación por falta de evidencia tampoco puede reventar el registro."""
    from punto.orchestrator.dev_cycle import DevelopmentCycle as _Cycle

    ciclo = _Cycle(
        router=ProviderRouter(),
        targets=DevelopmentTargetRegistry({}),
        config=DevelopmentConfig(),
        audit=AuditLogger(),
    )
    plan = DevelopmentPlan(
        summary="unificar la fuente",
        files_to_modify=("src/components/Rejilla.tsx",),
        verification_commands=("focused",),
        acceptance_mapping=("una sola fuente de tipos",),
    )
    request = BuildRequest(
        objective="unificar",
        target_repository=TARGET_ID,
        requested_role=ProviderRole.BUILDER,
        acceptance_criteria=("una sola fuente de tipos",),
    )

    class _Repo:
        def exists(self, path: str) -> bool:
            del path
            return False

    payload = {
        "trigger": "evidencia",
        "evidence": [],
        "resources": [f"src/lib/nuevo-{indice:03d}.ts" for indice in range(45)],
        "relationship": "misma cadena funcional",
    }
    devuelto, estado = ciclo._handle_scope_expansion(
        request=request,
        plan=plan,
        repository=_Repo(),  # type: ignore[arg-type]
        payload=payload,
        round_index=1,
    )

    assert estado == "DENIED"
    assert devuelto is plan


@pytest.mark.parametrize(
    ("operacion", "existe", "codigo"),
    [
        ("CREATE", True, CHANGE_ALREADY_EXISTS),
        ("MODIFY", False, CHANGE_MISSING_FILE),
        ("DELETE", False, CHANGE_MISSING_FILE),
    ],
)
def test_21_la_frontera_resume_los_hechos(
    operacion: str, existe: bool, codigo: str
) -> None:
    """Cada rechazo lleva operación, estado real y estado esperado: el hecho, no una opinión."""
    estado = {"src/lib/tipos.ts": TIPOS} if existe else {}
    resultado = _preflight(
        [_cambio("src/lib/tipos.ts", operacion, "x\n" if operacion != "DELETE" else "")], estado
    )

    issue = resultado.blocking[0]
    assert issue.code == codigo
    assert issue.operation == operacion
    assert issue.actual_state in {"EXISTS", "MISSING"}
    assert issue.expected_state in {"EXISTS", "MISSING"}
    assert issue.correctable is True
