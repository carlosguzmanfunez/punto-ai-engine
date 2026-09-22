"""STRUCTURAL FAILED → REPAIR PRODUCES CHANGES → BUILDER TAKEOVER.

Causa real (Task 0983a418, continuación de EVIDENCE MODALITY ROUTING): con la clasificación ya
corregida, un criterio ``STRUCTURAL_CONSISTENCY`` llegaba a ``FAILED`` (correcto) y entraba en el
bucle de reparación ya existente (correcto), pero la ronda de reparación terminaba en
``CHANGES_EMPTY`` — el BUILDER no proponía ningún cambio — hasta agotar el presupuesto y terminar
en ``VERIFICATION_FAILED``.

Primera causa (710abcf, ya cerrada): la evidencia llegaba incompleta y engañosa al BUILDER
(faltaba el remedio, y el texto fijo presuponía un dataset). Corregida: el remedio ya llega.

Segunda causa (710abcf → 3c48db6, ya cerrada): con la evidencia ya accionable, PUNTO seguía
dándole al MISMO BUILDER una segunda oportunidad para la misma causa. Corregida con BUILDER
TAKEOVER: cuando el asignado responde con éxito (``CHANGES_EMPTY``) ante un criterio ``FAILED``
con remedio accionable, PUNTO prueba con el siguiente candidato de recuperación.

Tercera causa (esta ronda): ese takeover reutilizaba la MISMA política que el failover operativo
(``FailoverPolicy``), mezclando dos causas distintas (indisponibilidad demostrable vs. respuesta
exitosa pero inútil) bajo una sola lista de preferencia. Corregida: ``TakeoverPolicy``
(``punto.providers.takeover``), declarada en su propia sección (``providers.yaml``:
``takeover:``), independiente de ``failover:`` — la política real prioriza Codex (``openai``)
como recuperación de calidad para BUILDER, sin tocar su prioridad de ARCHITECT ni la lista de
failover operativo.

    pytest tests/test_structural_repair_evidence.py -q
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto.api.console import ConsoleDependencies, register_human_console
from punto.api.console_state import default_console_state_path
from punto.api.dashboard import register_dashboard
from punto.audit.logger import AuditLogger
from punto.orchestrator.dev_cycle import DevelopmentConfig, DevelopmentCycle
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers.base import ModelCompletion
from punto.providers.contract import ModelUsage, ProviderRole
from punto.providers.router import ProviderRouter
from punto.providers.takeover import TakeoverPolicy
from punto.schemas.audit import AuditEventType
from punto.workspace.target import DevelopmentTargetRegistry
from test_human_console import TARGET_ID, _git, _plan, _target
from test_noop_reconciliation import _repo_ya_satisfecho
from test_provider_failover import _conectados
from test_visual_qa_effective import _Multimodal

CRITERIO = "Unificar los tipos de propiedad en una sola fuente de verdad."


def _solicitud(criterio: str = CRITERIO) -> dict[str, Any]:
    return {
        "objective": criterio,
        "target_id": TARGET_ID,
        "acceptance_criteria": [criterio],
        "scope_paths": ["src"],
    }


class _Espia:
    """Cliente guionizado con NOMBRE propio: responde el guion y apunta el prompt recibido.

    A diferencia de ``_Guion`` (siempre «guionizado»), esta clase permite registrar dos
    proveedores BUILDER distintos en el mismo router — el primario y el sustituto de TAKEOVER —
    y comprobar el prompt exacto que recibió cada uno por separado.
    """

    def __init__(self, router: ProviderRouter, *, name: str, model: str, script: list[Any]) -> None:
        self._name = name
        self._model = model
        self._responses = list(script)
        self.prompts: list[str] = []
        router.register_provider(name, self._factory, model=model)

    def _factory(self, model: str) -> _Espia:
        del model
        return self

    @property
    def provider(self) -> str:
        """Identificador del proveedor."""
        return self._name

    @property
    def model(self) -> str:
        """Modelo configurado."""
        return self._model

    def complete_json(self, **kwargs: Any) -> ModelCompletion:
        """Devuelve la siguiente respuesta del guion, tras apuntar el prompt recibido."""
        self.prompts.append(str(kwargs.get("system_prompt", "")))
        item = self._responses.pop(0) if self._responses else {"changes": []}
        content = item if isinstance(item, str) else json.dumps(item)
        return ModelCompletion(
            content=content,
            model=self._model,
            usage=ModelUsage(prompt_tokens=5, completion_tokens=7, total_tokens=12),
            latency_ms=1,
        )

    def redact(self, text: str) -> str:
        """No sanea: el ciclo no puede fiarse de la educación del adaptador."""
        return text

    def close(self) -> None:
        """No hay recursos que liberar."""


def _consola_con_takeover(
    tmp_path: Path,
    *,
    architect_plan: dict[str, Any],
    primary_script: list[Any],
    substitute_script: list[Any] | None = None,
    max_builder_takeovers: int = 1,
    with_duplicate: bool = True,
    extra_setup: Any = None,
) -> tuple[TestClient, AuditLogger, Path, _Espia, _Espia | None, ProviderRouter]:
    """Consola real: BUILDER primario, y sustituto de TAKEOVER si ``substitute_script`` se da.

    ``extra_setup(repo)``, si se da, corre y se comitea ANTES de fijar ``baseline_sha``: el
    ciclo tiene que empezar sobre el árbol final, no uno anterior.
    """
    repo, remoto = _repo_ya_satisfecho(tmp_path)
    if with_duplicate:
        (repo / "src" / "lib" / "tipos_legacy.ts").write_text(
            "export const TIPOS = ['Duplicado'];\n", encoding="utf-8"
        )
        _git(repo, "add", "-A")
        _git(
            repo,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@punto.local",
            "commit",
            "-m",
            "fuente duplicada",
        )
    if extra_setup is not None:
        extra_setup(repo)
        _git(repo, "add", "-A")
        _git(
            repo,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@punto.local",
            "commit",
            "-m",
            "preparación adicional del fixture",
        )
    target = _target(repo, remoto=remoto)
    audit = AuditLogger()
    architect = _Multimodal("architect", "architect-1", lambda n: json.dumps(architect_plan))
    router = ProviderRouter()
    router.register_provider(architect.provider, lambda _m, c=architect: c, model=architect.model)
    router.assign_role(ProviderRole.ARCHITECT, "architect")
    primario = _Espia(router, name="primario", model="primario-1", script=primary_script)
    router.assign_role(ProviderRole.BUILDER, "primario")
    sustituto: _Espia | None = None
    if substitute_script is not None:
        sustituto = _Espia(router, name="sustituto", model="sustituto-1", script=substitute_script)
        router.configure_takeover(
            TakeoverPolicy(roles={ProviderRole.BUILDER: ("sustituto",)}),
            _conectados("sustituto"),
        )
    ciclo = DevelopmentCycle(
        router=router,
        targets=DevelopmentTargetRegistry({TARGET_ID: target}),
        config=DevelopmentConfig(
            max_repair_rounds=2,
            max_structural_corrections=2,
            max_builder_takeovers=max_builder_takeovers,
        ),
        audit=audit,
        policy_engine=PolicyEngine.from_config(),
    )
    dependencias = ConsoleDependencies(
        dev_cycle=ciclo,
        gates=HumanGate(),
        audit=audit,
        policy=PolicyEngine.from_config(),
        targets={TARGET_ID: target},
        run_inline=True,
        environ={},
    )
    application = FastAPI()
    register_dashboard(application)
    register_human_console(application, dependencias)
    return TestClient(application), audit, repo, primario, sustituto, router


def _plan_duplicado() -> dict[str, Any]:
    plan = _plan()
    plan["files_to_modify"] = ["src/lib/tipos.ts", "src/lib/tipos_legacy.ts"]
    return plan


def _reparacion(*, content: str = "export { TIPOS } from './tipos';\n") -> dict[str, Any]:
    return {
        "summary": "reexportar desde la fuente canónica",
        "root_cause": "dos ficheros declaran una constante TIPOS por separado",
        "evidence": ["src/lib/tipos_legacy.ts declara su propia versión de TIPOS"],
        "expected_effect": "el criterio de consistencia estructural pasa a SATISFIED",
        "changes": [
            {
                "path": "src/lib/tipos_legacy.ts",
                "operation": "MODIFY",
                # Reproduce la forma cruda real del takeover Codex: un opcional de texto
                # materializado como cadena vacía. Para MODIFY significa «sin origen».
                "source_path": "",
                "content": content,
                "reason": "fuente duplicada: reexporta la canónica en vez de declarar la suya",
                "acceptance_criterion": CRITERIO,
            }
        ],
    }


# ===================================== A · CHANGES_EMPTY accionable → takeover automático
def test_a_changes_empty_accionable_dispara_takeover_automatico(tmp_path: Path) -> None:
    """El primario responde con éxito pero vacío; el sustituto toma la reparación de inmediato."""
    client, audit, _repo, primario, sustituto, _router = _consola_con_takeover(
        tmp_path,
        architect_plan=_plan_duplicado(),
        primary_script=[{"changes": []}],
        substitute_script=[_reparacion()],
    )

    tarea = client.post("/console/tasks", json=_solicitud()).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea["development"]
    assert len(primario.prompts) == 1, "el primario solo se gastó una vez para esta causa"
    assert sustituto is not None and len(sustituto.prompts) == 1
    eventos = audit.by_type(AuditEventType.DEV_BUILDER_TAKEOVER)
    assert len(eventos) == 2, "un evento al pedir el takeover, otro con el resultado"


# ===================================== B · sustituto produce el cambio → verify → SATISFIED
def test_b_el_sustituto_produce_el_cambio_y_llega_a_satisfied(tmp_path: Path) -> None:
    """La evidencia accionable llega al sustituto (REMEDY, ficheros) y corrige de verdad."""
    client, audit, repo, _primario, sustituto, _router = _consola_con_takeover(
        tmp_path,
        architect_plan=_plan_duplicado(),
        primary_script=[{"changes": []}],
        substitute_script=[_reparacion()],
    )

    tarea = client.post("/console/tasks", json=_solicitud()).json()

    assert tarea["development"]["claims_result"] == "SATISFIED"
    assert sustituto is not None
    (prompt,) = sustituto.prompts
    assert "REMEDY" in prompt and "tipos_legacy.ts" in prompt and "tipos.ts" in prompt
    contenido = (repo / "src" / "lib" / "tipos_legacy.ts").read_text(encoding="utf-8")
    assert "export const TIPOS" not in contenido
    eventos = [
        dict(e.metadata)["result"] for e in audit.by_type(AuditEventType.DEV_CLAIMS_EVALUATED)
    ]
    assert eventos == ["FAILED", "SATISFIED"]
    normalizaciones = [
        dict(e.metadata) for e in audit.by_type(AuditEventType.DEV_PROPOSAL_NORMALIZED)
    ]
    assert any(
        tuple(item.get("actions", ())) == ("EMPTY_SOURCE_PATH_TO_ABSENT",)
        for item in normalizaciones
    ), "el payload reparable del takeover se normaliza y queda auditado"


# =========================================== D · el sustituto puede SALVAGE
def test_d_el_sustituto_puede_salvage(tmp_path: Path) -> None:
    """Un MODIFY que conserva la mayor parte del fichero se clasifica SALVAGE."""

    def _con_duplicado_largo(repo: Path) -> None:
        (repo / "src" / "lib" / "tipos_legacy.ts").write_text(
            "export const TIPOS = ['Duplicado', 'Casa', 'Oficina', 'Terreno'];\n",
            encoding="utf-8",
        )

    client, audit, _repo, _p, _s, _router = _consola_con_takeover(
        tmp_path,
        architect_plan=_plan_duplicado(),
        primary_script=[{"changes": []}],
        substitute_script=[
            _reparacion(
                content="export const TIPOS = ['Duplicado', 'Casa', 'Oficina', 'Terreno', "
                "'Local'];\n"
            )
        ],
        with_duplicate=False,
        extra_setup=_con_duplicado_largo,
    )

    client.post("/console/tasks", json=_solicitud())

    resultados = [
        dict(e.metadata)
        for e in audit.by_type(AuditEventType.DEV_BUILDER_TAKEOVER)
        if dict(e.metadata).get("outcome")
    ]
    assert resultados and resultados[-1]["outcome"] == "SALVAGE"


# =========================================== E · el sustituto puede REWRITE
def test_e_el_sustituto_puede_rewrite(tmp_path: Path) -> None:
    """Un DELETE (sin «antes» comparable con lo escrito) se clasifica siempre REWRITE."""
    plan = _plan_duplicado()
    plan["files_to_delete"] = ["src/lib/tipos_legacy.ts"]
    borrado = {
        "summary": "retirar la fuente duplicada",
        "root_cause": "dos ficheros declaran una constante TIPOS por separado",
        "evidence": ["src/lib/tipos_legacy.ts declara su propia versión de TIPOS"],
        "expected_effect": "el criterio de consistencia estructural pasa a SATISFIED",
        "changes": [
            {
                "path": "src/lib/tipos_legacy.ts",
                "operation": "DELETE",
                "reason": "fuente duplicada: se retira en vez de reexportar la canónica",
                "acceptance_criterion": CRITERIO,
            }
        ],
    }
    client, audit, _repo, _p, _s, _router = _consola_con_takeover(
        tmp_path,
        architect_plan=plan,
        primary_script=[{"changes": []}],
        substitute_script=[borrado],
    )

    client.post("/console/tasks", json=_solicitud())

    resultados = [
        dict(e.metadata)
        for e in audit.by_type(AuditEventType.DEV_BUILDER_TAKEOVER)
        if dict(e.metadata).get("outcome")
    ]
    assert resultados and resultados[-1]["outcome"] == "REWRITE"


# ===================== F · no se reintenta el mismo builder para la misma causa
def test_f_no_se_reintenta_el_mismo_builder_para_la_misma_causa(tmp_path: Path) -> None:
    """En el ciclo real, el primario recibe UNA sola oportunidad para la causa del takeover."""
    client, _audit, _repo, primario, sustituto, _router = _consola_con_takeover(
        tmp_path,
        architect_plan=_plan_duplicado(),
        primary_script=[{"changes": []}],
        substitute_script=[_reparacion()],
    )

    client.post("/console/tasks", json=_solicitud())

    assert len(primario.prompts) == 1, "el primario nunca vuelve a intentarlo para esta causa"
    assert sustituto is not None and len(sustituto.prompts) == 1


def test_f2_execute_alternative_nunca_reelige_a_quien_ya_esta_excluido() -> None:
    """Router-level: con dos sustitutos declarados, el ya excluido no se vuelve a elegir."""
    from punto.providers.contract import ProviderStatus
    from test_provider_failover import Fake, _conectados, _pedir, _registrar

    router = ProviderRouter()
    a = Fake("a", "a-1", {"changes": []})
    b = Fake("b", "b-1", {"changes": []})
    c = Fake("c", "c-1", {"changes": ["c respondió"]})
    _registrar(router, a, b, c)
    router.assign_role(ProviderRole.BUILDER, "a")
    router.configure_takeover(
        TakeoverPolicy(roles={ProviderRole.BUILDER: ("b", "c")}), _conectados("a", "b", "c")
    )

    result = router.execute_alternative(
        ProviderRole.BUILDER, _pedir(ProviderRole.BUILDER), exclude=frozenset({"a", "b"})
    )

    assert result.status is ProviderStatus.SUCCESS and result.provider == "c"
    assert a.calls == [] and b.calls == [], "ni el primario ni el ya excluido se vuelven a gastar"


# ==================== G · el takeover no reasigna permanentemente el BUILDER primario
def test_g_el_takeover_no_reasigna_permanentemente_el_primario(tmp_path: Path) -> None:
    """Tras el takeover, el rol sigue asignado al primario configurado (no cambia el Dashboard)."""
    client, _audit, _repo, _p, _s, router = _consola_con_takeover(
        tmp_path,
        architect_plan=_plan_duplicado(),
        primary_script=[{"changes": []}],
        substitute_script=[_reparacion()],
    )

    client.post("/console/tasks", json=_solicitud())

    assert router.get_provider_for_role(ProviderRole.BUILDER) == "primario"


# ========================================= H · misma Task, mismo ciclo causal
def test_h_el_takeover_ocurre_en_la_misma_task_y_el_mismo_ciclo(tmp_path: Path) -> None:
    """La reparación por takeover no crea otra Task ni pide una persona."""
    client, _audit, _repo, _p, _s, _router = _consola_con_takeover(
        tmp_path,
        architect_plan=_plan_duplicado(),
        primary_script=[{"changes": []}],
        substitute_script=[_reparacion()],
    )

    tarea = client.post("/console/tasks", json=_solicitud()).json()

    assert tarea["runs"] == 1
    listado = client.get("/console/tasks").json()
    assert listado["total"] == 1 and listado["items"][0]["task_id"] == tarea["task_id"]
    assert tarea["gates"] == [], "la reparación por takeover no pidió una persona"


# ==================================== I · reinicio conserva el resultado del takeover
def test_i_reinicio_conserva_el_resultado_del_takeover(tmp_path: Path) -> None:
    """El estado durable persiste el resultado final (SATISFIED) y el rastro del takeover."""
    client, _audit, _repo, _p, _s, _router = _consola_con_takeover(
        tmp_path / "montaje-1",
        architect_plan=_plan_duplicado(),
        primary_script=[{"changes": []}],
        substitute_script=[_reparacion()],
    )
    tarea = client.post("/console/tasks", json=_solicitud()).json()
    task_id = tarea["task_id"]
    assert default_console_state_path().is_file()

    otro, _audit2, _repo2, _p2, _s2, _router2 = _consola_con_takeover(
        tmp_path / "montaje-2",
        architect_plan=_plan_duplicado(),
        primary_script=[{"changes": []}],
        substitute_script=[_reparacion()],
    )
    recuperada = otro.get(f"/console/tasks/{task_id}").json()

    assert recuperada["task_id"] == task_id and recuperada["recovered"] is True
    assert recuperada["development"]["claims_result"] == "SATISFIED"
    assert recuperada["development"]["commit_sha"]


# =================== J · el grafo reconstruye: primario → takeover → sustituto → resultado
def test_j_el_grafo_reconstruye_primario_takeover_sustituto_resultado(tmp_path: Path) -> None:
    """``DEV_BUILDER_TAKEOVER`` trae todo lo necesario para reconstruir la cadena causal."""
    client, audit, _repo, _p, _s, _router = _consola_con_takeover(
        tmp_path,
        architect_plan=_plan_duplicado(),
        primary_script=[{"changes": []}],
        substitute_script=[_reparacion()],
    )

    client.post("/console/tasks", json=_solicitud())

    peticion, resultado = (
        dict(e.metadata) for e in audit.by_type(AuditEventType.DEV_BUILDER_TAKEOVER)
    )
    assert list(peticion["excluded"]) == ["primario"]
    assert "STRUCTURAL_CONSISTENCY" in peticion["claims_failed"]
    assert resultado["provider"] == "sustituto"
    assert resultado["outcome"] in {"SALVAGE", "REWRITE"}
    assert list(resultado["changed"]) == ["src/lib/tipos_legacy.ts"]


# ================= K · ningún sustituto disponible → fallo explícito tras agotar rutas
def test_k_sin_sustituto_disponible_falla_explicito_tras_agotar_rutas(tmp_path: Path) -> None:
    """Sin política de failover para BUILDER, el takeover no tiene a quién recurrir: falla claro."""
    client, _audit, repo, primario, sustituto, _router = _consola_con_takeover(
        tmp_path,
        architect_plan=_plan_duplicado(),
        primary_script=[{"changes": []}],
        substitute_script=None,
    )

    tarea = client.post("/console/tasks", json=_solicitud()).json()

    assert sustituto is None
    assert tarea["stage"] != "DEVELOPMENT_COMPLETED"
    assert tarea["development"]["status"] == "DEVELOPMENT_PROVIDER_FAILED"
    assert "ningún proveedor autorizado y capaz" in tarea["development"]["error"]
    assert len(primario.prompts) == 1, "no se le vuelve a preguntar al mismo con la misma causa"
    assert "export const TIPOS" in (repo / "src" / "lib" / "tipos_legacy.ts").read_text(
        encoding="utf-8"
    ), "nada se reparó de verdad: no se acepta silenciosamente"


# ================================================== 4 · sin duplicación: SATISFIED sin reparación
def test_4_sin_duplicacion_real_es_satisfied_sin_reparacion(tmp_path: Path) -> None:
    """Falso positivo evitado: si la fuente ya es única, no hay FAILED ni takeover."""
    client, audit, _repo, primario, sustituto, _router = _consola_con_takeover(
        tmp_path,
        architect_plan=_plan(),
        primary_script=[{"changes": []}],
        substitute_script=[{"changes": []}],
        with_duplicate=False,
    )

    tarea = client.post("/console/tasks", json=_solicitud()).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea["development"]
    assert tarea["development"]["claims_result"] == "SATISFIED"
    assert len(primario.prompts) == 1, "una sola invocación: no hizo falta ninguna reparación"
    assert sustituto is not None and sustituto.prompts == []
    assert audit.by_type(AuditEventType.DEV_BUILDER_TAKEOVER) == ()


# =========== MUTACIÓN · sin remedio no hay takeover posible (documenta la condición de disparo)
def test_mutacion_sin_remedy_no_dispara_takeover(tmp_path: Path) -> None:
    """Sin ``ClaimRecord.remedy`` (regresión de 710abcf), ``accionable`` sería falso y el

    takeover nunca se pediría: el mismo BUILDER se reintenta indefinidamente hasta agotar
    ``max_repair_rounds`` sin que nadie más tome el trabajo. Esta prueba fija, sobre la función
    pura, que un registro FAILED con remedio se considera accionable — la condición exacta que
    ``dev_cycle.py`` usa para decidir el takeover.
    """
    from punto.acceptance import ClaimRecord

    con_remedio = ClaimRecord(
        sentence=CRITERIO,
        kind="STRUCTURAL_CONSISTENCY",
        result="UNSATISFIED",
        evidence="existen 2 definiciones paralelas",
        remedy="consolida las definiciones paralelas en una única fuente canónica",
        evidence_class="FAILED",
    )
    sin_remedio = ClaimRecord(
        sentence=CRITERIO,
        kind="STRUCTURAL_CONSISTENCY",
        result="UNSATISFIED",
        evidence="existen 2 definiciones paralelas",
        evidence_class="FAILED",
    )

    # Misma condición que dev_cycle.py usa para decidir un takeover.
    assert any(item.unsatisfied and item.remedy for item in (con_remedio,))
    assert not any(item.unsatisfied and item.remedy for item in (sin_remedio,))
