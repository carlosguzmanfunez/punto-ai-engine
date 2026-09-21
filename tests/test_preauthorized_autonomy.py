"""Autonomía preautorizada: un riesgo HIGH por sí solo no abre un Human Gate ni concede nada.

Política general y declarativa (``config/autonomy.yaml``): la decisión es función de **operación +
recurso + alcance + reversibilidad + autoridad configurada**, no de una etiqueta de riesgo. El Human
Gate queda para una frontera de autoridad real (secretos, pagos o coste fuera del presupuesto,
borrado/migración irreversible, infraestructura, destino no autorizado, ampliar la propia autoridad,
reescribir historial remoto, actos legales, evidencia sin resolver o exceder el sobre).

    pytest tests/test_preauthorized_autonomy.py -q
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from punto.policy.autonomy import (
    FLOOR_GATED_CLASSES,
    AutonomyContext,
    AutonomyPolicy,
    AutonomyQuery,
    Boundary,
)
from punto.policy.envelope import (
    AdaptiveAuthorityEnvelope,
    DataSensitivity,
    EnvelopeOperation,
    Environment,
    OperationRisk,
)
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine, PolicyEvaluationContext
from punto.schemas.decision import ActionRequest
from punto.schemas.enums import RiskLevel, TaskStatus
from punto.schemas.policy import PolicyOutcome

REPO_ROOT = Path(__file__).resolve().parents[1]
DENTRO = AutonomyContext(target_registered=True, in_scope=True)
CONTEXTO = PolicyEvaluationContext(autonomy=DENTRO)


def _politica() -> AutonomyPolicy:
    """La política real que gobierna el motor (``config/autonomy.yaml``)."""
    return PolicyEngine.from_config().autonomy


def _pedido(**cambios: Any) -> ActionRequest:
    base: dict[str, Any] = {
        "action": "modify_file",
        "technical": True,
        "reversible": True,
        "risk_level": RiskLevel.HIGH,
        "files_changed": ["src/components/Mapa.tsx"],
    }
    return ActionRequest(**(base | cambios))


def _sobre() -> AdaptiveAuthorityEnvelope:
    """Sobre construido como el del ciclo: rutas constitucionales de la configuración real."""
    motor = PolicyEngine.from_config()
    return AdaptiveAuthorityEnvelope(
        constitutional_paths=motor.protected_paths, autonomy=motor.autonomy
    )


def _perfil(**cambios: Any) -> OperationRisk:
    base: dict[str, Any] = {
        "operation": EnvelopeOperation.PLAN_APPLY,
        "resources": ("src/components/Mapa.tsx", "src/app/globals.css"),
        "evidence": ("criterio de aceptación",),
    }
    return OperationRisk(**(base | cambios))


# ============================== 1 · HIGH + preautorizada + reversible + en alcance → ALLOW
def test_1_high_preautorizado_reversible_y_en_alcance_es_allow_automatico() -> None:
    """Una petición HIGH con contexto de confianza del destino pasa sola (clasificación intacta)."""
    motor = PolicyEngine.from_config()

    con_contexto = motor.evaluate(_pedido(), CONTEXTO)

    assert con_contexto.outcome is PolicyOutcome.ALLOW and not con_contexto.requires_human
    assert con_contexto.effective_risk is RiskLevel.HIGH, "la clasificación HIGH se conserva"
    assert any("sin frontera de autoridad" in razon for razon in con_contexto.reasons)


# ============================== 2 · HIGH por sí solo no concede autoridad
def test_2_high_por_si_solo_no_concede_nada() -> None:
    """Sin contexto de confianza, con destino no registrado, con acción no listada o CRITICAL."""
    motor = PolicyEngine.from_config()

    sin_contexto = motor.evaluate(_pedido())
    no_registrado = motor.evaluate(
        _pedido(), PolicyEvaluationContext(autonomy=AutonomyContext(in_scope=True))
    )
    critico = motor.evaluate(_pedido(risk_level=RiskLevel.CRITICAL), CONTEXTO)
    no_listada = motor.evaluate(_pedido(action="install_dependency"), CONTEXTO)

    for decision in (sin_contexto, no_registrado, critico):
        assert decision.outcome is PolicyOutcome.REQUIRE_HUMAN and decision.requires_human
    assert no_listada.requires_human, "install_dependency no figura como preautorizada"
    assert any("frontera de autoridad" in razon for razon in no_registrado.reasons), (
        "el gate dice la frontera concreta, no solo «riesgo HIGH»"
    )


# ============================== 3 · secretos → gate / falla cerrado
def test_3_un_secreto_es_frontera_y_nunca_se_preautoriza() -> None:
    motor = PolicyEngine.from_config()

    decision = motor.evaluate(_pedido(files_changed=[".env.production"]), CONTEXTO)
    sobre = _sobre().assess(_perfil(resources=(".env.production",)))

    assert decision.outcome is PolicyOutcome.REQUIRE_HUMAN
    assert any("secrets" in razon for razon in decision.reasons)
    assert sobre.prohibited, "el almacén de credenciales falla cerrado en el sobre"
    veredicto = _politica().evaluate(AutonomyQuery(operation="secret_rotation"), DENTRO)
    assert not veredicto.preauthorized and Boundary.SECRETS in veredicto.boundaries


# ============================== 4 · destructivo sobre producción → gate
def test_4_lo_destructivo_sobre_produccion_exige_persona() -> None:
    motor = PolicyEngine.from_config()

    decision = motor.evaluate(_pedido(production_impact=True, reversible=False), CONTEXTO)
    sobre = _sobre().assess(
        _perfil(
            operation=EnvelopeOperation.PRODUCTION_DATABASE,
            environment=Environment.PRODUCTION,
            destructive=True,
            reversible=False,
        )
    )
    veredicto = _politica().evaluate(
        AutonomyQuery(operation="production_database", destructive=True, reversible=False), DENTRO
    )

    assert decision.requires_human
    assert sobre.requires_human or sobre.prohibited
    assert not veredicto.preauthorized and Boundary.PRODUCTION_DATA in veredicto.boundaries


# ============================== 5 · destino fuera de alcance → gate
def test_5_un_destino_fuera_de_alcance_exige_persona() -> None:
    motor = PolicyEngine.from_config()
    fuera = AutonomyContext(target_registered=True, in_scope=False)

    decision = motor.evaluate(_pedido(), PolicyEvaluationContext(autonomy=fuera))
    veredicto = _politica().evaluate(AutonomyQuery(operation="modify_file"), fuera)

    assert decision.requires_human
    assert veredicto.boundaries == (Boundary.UNAUTHORIZED_TARGET,)


# ============================== 6 · coste fuera del sobre → gate
def test_6_el_coste_fuera_del_sobre_exige_persona() -> None:
    motor = PolicyEngine.from_config()

    decision = motor.evaluate(_pedido(estimated_cost=50.0), CONTEXTO)
    dentro = motor.evaluate(_pedido(estimated_cost=0.5), CONTEXTO)
    veredicto = _politica().evaluate(AutonomyQuery(operation="modify_file", cost_usd=50.0), DENTRO)

    assert not decision.allowed, "coste fuera del presupuesto: no se ejecuta sin persona"
    assert dentro.outcome is PolicyOutcome.ALLOW
    assert Boundary.UNBUDGETED_COST in veredicto.boundaries


def test_6b_exceder_el_techo_del_sobre_es_frontera_aunque_cada_cambio_sea_reversible() -> None:
    veredicto = _politica().evaluate(AutonomyQuery(operation="plan_apply", files=45), DENTRO)

    assert Boundary.ENVELOPE_EXCEEDED in veredicto.boundaries


# ============================== 7 · plan_apply local reversible → sin gate
def test_7_plan_apply_local_y_reversible_no_abre_gate() -> None:
    decision = _sobre().assess(_perfil())

    assert decision.autonomous and not decision.requires_human
    assert decision.outcome is PolicyOutcome.ALLOW
    assert (
        _politica().evaluate(AutonomyQuery(operation="plan_apply", files=3), DENTRO).preauthorized
    )


# ============================== 8 · QA visual → sin gate
@pytest.mark.parametrize("operacion", ["visual_qa", "browser_qa", "run_tests", "execute"])
def test_8_verificar_y_hacer_qa_visual_es_autonomo(operacion: str) -> None:
    veredicto = _politica().evaluate(AutonomyQuery(operation=operacion), DENTRO)

    assert veredicto.preauthorized, veredicto.reasons


def test_8b_el_ciclo_con_qa_visual_efectivo_no_abre_ningun_gate(tmp_path: Path) -> None:
    """Con el ciclo real: la evidencia visual se produce sin pedir persona."""
    from test_noop_reconciliation import _con_criterio_visual, _consola_visual

    client, _audit, _gpt, captura = _consola_visual(tmp_path, veredicto="PASS")

    tarea = client.post("/console/tasks", json=_con_criterio_visual()).json()

    assert tarea["gates"] == [] and tarea["stage"] == "DEVELOPMENT_COMPLETED"
    assert captura.llamadas, "se hizo QA visual real"


# ============================== 9 · commit local → sin gate
def test_9_un_commit_local_no_abre_gate() -> None:
    motor = PolicyEngine.from_config()

    decision = motor.evaluate(_pedido(action="create_commit"), CONTEXTO)
    sobre = _sobre().assess(_perfil(operation=EnvelopeOperation.COMMIT))
    rama = _sobre().assess(_perfil(operation=EnvelopeOperation.BRANCH, resources=()))

    assert decision.outcome is PolicyOutcome.ALLOW
    assert sobre.autonomous and rama.autonomous


# ============================== 10 · la autoridad no se autoamplía
def test_10_modificar_la_politica_de_autonomia_esta_protegido() -> None:
    motor = PolicyEngine.from_config()

    decision = motor.evaluate(
        _pedido(files_changed=["config/autonomy.yaml"], risk_level=RiskLevel.LOW), CONTEXTO
    )
    sobre = _sobre().assess(_perfil(resources=("config/autonomy.yaml",)))

    assert decision.outcome is PolicyOutcome.REJECT, "es un fichero de autoridad protegido"
    assert sobre.prohibited


def test_10b_el_yaml_no_puede_quitar_fronteras_ni_conceder_lo_reservado() -> None:
    """Aunque el YAML lo pida, el piso en código manda: solo puede SUMAR fronteras."""
    manipulada = AutonomyPolicy.from_config(
        {
            "preauthorized": {
                "actions": [
                    "modify_file",
                    "payment",
                    "irreversible_delete",
                    "db_destructive_apply",
                ],
                "operations": ["write", "force_push", "history_rewrite", "publish", "deploy"],
                "git_reversible_delete": True,
            },
            "gated_resource_classes": [],
            "budget": {"max_files": 100000, "max_cost_usd": 1e9},
        }
    )

    assert manipulada.gated_classes >= FLOOR_GATED_CLASSES
    for operacion in ("payment", "irreversible_delete", "db_destructive_apply", "force_push"):
        assert not manipulada.evaluate(AutonomyQuery(operation=operacion), DENTRO).preauthorized
    # publish/deploy no se conceden por YAML: exigen el sobre explícito del destino.
    assert not manipulada.evaluate(AutonomyQuery(operation="publish"), DENTRO).preauthorized


def test_10c_sin_configuracion_no_hay_autonomia_delegada() -> None:
    """Sin ``autonomy.yaml`` (o vacío) se conserva el comportamiento previo: fail-closed."""
    vacio = AutonomyPolicy.from_config({})
    motor = PolicyEngine.from_config()

    assert not vacio.enabled
    assert not vacio.evaluate(AutonomyQuery(operation="modify_file"), DENTRO).preauthorized
    assert motor.evaluate(_pedido()).requires_human


def test_10d_un_pedido_no_puede_declarar_su_propio_alcance() -> None:
    """``ActionRequest`` no admite campos de autoridad: el contexto solo lo fija código de PUNTO."""
    with pytest.raises(ValueError):
        ActionRequest(action="modify_file", in_scope=True, target_registered=True)  # type: ignore[call-arg]


# ============================== push / deploy / producción: solo con sobre explícito
def test_11_push_deploy_y_produccion_exigen_el_sobre_explicito_del_destino() -> None:
    politica = _politica()
    sin_sobre = AutonomyContext(target_registered=True, in_scope=True)

    for operacion, clase in (
        ("publish", "push"),
        ("deploy", "deploy"),
        ("production_release", "production_release"),
    ):
        cerrada = politica.evaluate(AutonomyQuery(operation=operacion), sin_sobre)
        con_sobre = politica.evaluate(
            AutonomyQuery(operation=operacion),
            AutonomyContext(
                target_registered=True, in_scope=True, release_envelope=frozenset({clase})
            ),
        )
        assert Boundary.RELEASE_WITHOUT_ENVELOPE in cerrada.boundaries
        assert con_sobre.preauthorized, (operacion, con_sobre.reasons)
    otra_clase = AutonomyContext(
        target_registered=True, in_scope=True, release_envelope=frozenset({"push"})
    )
    assert not politica.evaluate(AutonomyQuery(operation="deploy"), otra_clase).preauthorized
    # en el sobre del ciclo local, publicar sigue exigiendo persona
    assert _sobre().assess(_perfil(operation=EnvelopeOperation.PUBLISH)).requires_human


# ============================== borrar: reversible por Git ⇒ autónomo; si no, frontera
def test_12_borrar_un_fichero_versionado_es_autonomo_y_sin_esa_garantia_es_frontera() -> None:
    sobre = _sobre()
    borrar = {
        "operation": EnvelopeOperation.DELETE,
        "resources": ("src/lib/obsoleto.ts",),
        "destructive": True,
    }

    recuperable = sobre.assess(_perfil(**borrar, git_recoverable=True))
    no_versionado = sobre.assess(_perfil(**borrar, git_recoverable=False))
    semilla = sobre.assess(
        _perfil(**(borrar | {"resources": ("db/seed.sql",)}), git_recoverable=True)
    )
    personal = sobre.assess(
        _perfil(**borrar, git_recoverable=True, data_sensitivity=DataSensitivity.PERSONAL)
    )

    assert recuperable.autonomous and "git-reversible-delete" in recuperable.rule_names
    assert recuperable.risk is RiskLevel.HIGH, "la clasificación HIGH se conserva…"
    assert recuperable.authority_level.name != "LEVEL_3_HUMAN", "…pero no es decisión humana"
    assert no_versionado.requires_human and "destructive-local-data" in no_versionado.rule_names
    assert semilla.requires_human and personal.requires_human


def test_12b_la_politica_es_declarativa_no_esta_cableada_a_un_proyecto_ni_proveedor() -> None:
    """Quitar una acción del YAML cambia el desenlace; el código no nombra proyecto ni proveedor."""
    sin_modify = AutonomyPolicy.from_config(
        {"preauthorized": {"actions": ["create_file"], "operations": ["read"]}}
    )
    fuente = (REPO_ROOT / "src" / "punto" / "policy" / "autonomy.py").read_text(encoding="utf-8")

    assert not sin_modify.evaluate(AutonomyQuery(operation="modify_file"), DENTRO).preauthorized
    assert sin_modify.evaluate(AutonomyQuery(operation="create_file"), DENTRO).preauthorized
    for prohibido in ("inmobiliario", "honduras", "deepseek", "anthropic", "openai", "2e7822a0"):
        assert prohibido not in fuente.lower()


def test_13_los_gates_historicos_se_conservan_como_auditoria() -> None:
    """Aplicar la política nueva no reabre, aprueba ni reescribe un gate ya emitido."""
    from uuid import uuid4

    gates = HumanGate()
    tarea = uuid4()
    historico = gates.request(
        task_id=tarea,
        action="EVIDENCE_REQUIRED",
        risk=RiskLevel.HIGH,
        reason="gate histórico previo a la política",
        resume_status=TaskStatus.IN_PROGRESS,
    )
    antes = (historico.id, historico.action, historico.risk, historico.reason, historico.status)

    PolicyEngine.from_config().evaluate(_pedido(), CONTEXTO)

    ahora = gates.get(historico.id)
    assert (ahora.id, ahora.action, ahora.risk, ahora.reason, ahora.status) == antes
    assert ahora.status.name == "PENDING"


# ============================== ciclo real: borrar lo versionado no abre gate; lo no versionado sí
def _tarea_de_borrado(tmp_path: Path, *, versionado: bool) -> dict[str, Any]:
    from test_console_rerun import SOLICITUD
    from test_human_console import (
        _app,
        _cambio,
        _git,
        _plan,
        _repos,
        _target,
    )

    repo, remoto = _repos(tmp_path)
    if versionado:
        _git(repo, "add", "-f", "src/lib/obsoleto.ts")
        _git(
            repo,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@punto.local",
            "commit",
            "-m",
            "versionar el fichero obsoleto",
        )
    client, _audit, _t, _d = _app(
        target=_target(repo, remoto=remoto),
        respuestas=[_plan(cierre=True), _cambio(borrado=True)],
    )
    tarea: dict[str, Any] = client.post("/console/tasks", json=SOLICITUD).json()
    return tarea


def test_14_ciclo_real_borrar_un_fichero_versionado_completa_sin_gate(tmp_path: Path) -> None:
    tarea = _tarea_de_borrado(tmp_path, versionado=True)

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea["development"]
    assert tarea["gates"] == []
    assert "src/lib/obsoleto.ts" in tarea["development"]["applied"]


def test_14b_ciclo_real_borrar_un_fichero_no_versionado_sigue_exigiendo_persona(
    tmp_path: Path,
) -> None:
    tarea = _tarea_de_borrado(tmp_path, versionado=False)

    assert tarea["stage"] == "WAITING_HUMAN"
    assert len(tarea["gates"]) == 1
