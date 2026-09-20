"""Autoridad persistente del destino y release autónomo condicional (AP000-R01).

Casos deterministas de A a L sobre la decisión: qué se ejecuta solo, qué exige persona y qué queda
denegado. Todo se evalúa con señales reales (resultado del ciclo, política real y configuración del
destino); ninguna prueba publica nada: la cadena completa se prueba con remoto local y sonda
inyectada en ``tests/test_human_console.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from punto.policy.policy_engine import PolicyEngine
from punto.policy.target_authority import (
    ConditionState,
    ReleaseContext,
    ReleaseDisposition,
    evaluate_release,
)
from punto.schemas.authority import ReleaseOperation, TargetAuthority
from punto.schemas.decision import ActionRequest
from punto.schemas.dev import (
    AppliedChange,
    ChangeOperation,
    CommandEvidence,
    DevelopmentResult,
    DevelopmentStatus,
)
from punto.schemas.enums import RiskLevel
from punto.schemas.policy import PolicyDecision, PolicyOutcome
from punto.workspace.target import DevelopmentTarget, DevelopmentTargetError, target_from_mapping

REPO = Path("C:/destinos/punto-inmobiliario-hn")
SHA = "a" * 40
OTRO_SHA = "b" * 40
URL = "https://punto-inmobiliario-hn.vercel.app"


def _autoridad(**extra: object) -> TargetAuthority:
    """Sobre completo (push + despliegue + publicación) por defecto."""
    base: dict[str, object] = {
        "local_changes": True,
        "commit": True,
        "push": True,
        "deploy": True,
        "production_release": True,
        "deploy_mechanism": "git-push",
        "allowed_branches": ("main",),
    }
    base.update(extra)
    return TargetAuthority(**base)  # type: ignore[arg-type]


def _destino(authority: TargetAuthority | None = None) -> DevelopmentTarget:
    """Destino publicable con el sobre indicado (sin sobre ⇒ fail closed)."""
    return DevelopmentTarget(
        target_id="punto-inmobiliario-hn",
        repository=REPO,
        baseline_sha=SHA,
        scope_roots=("src",),
        work_branch="ai/tarea",
        production_branch="main",
        production_url=URL,
        authority=authority if authority is not None else TargetAuthority(),
    )


def _resultado(**extra: object) -> DevelopmentResult:
    """Resultado real de un ciclo completado, verde."""
    base: dict[str, object] = {
        "status": DevelopmentStatus.COMPLETED,
        "commit_sha": SHA,
        "branch": "ai/tarea",
        "functional_chain_result": "VERIFIED",
        "final_scope": ("src/lib/honduras.ts",),
        "initial_scope": ("src/lib/honduras.ts",),
        "applied": (
            AppliedChange(
                path="src/lib/honduras.ts",
                operation=ChangeOperation.MODIFY,
                bytes_written=10,
                sha256="c" * 64,
                verified=True,
            ),
        ),
        "verification": (
            CommandEvidence(
                name="typecheck", argv=("node", "tsc"), exit_code=0, duration_ms=10, passed=True
            ),
            CommandEvidence(
                name="property-types",
                argv=("node", "--test"),
                exit_code=0,
                duration_ms=5,
                passed=True,
            ),
        ),
    }
    base.update(extra)
    return DevelopmentResult(**base)  # type: ignore[arg-type]


def _politica(engine: PolicyEngine | None = None) -> PolicyDecision:
    """Decisión **real** del PolicyEngine sobre publicar en producción."""
    policy = engine or PolicyEngine.from_config()
    return policy.evaluate(
        ActionRequest(
            action="deploy_production",
            technical=True,
            reversible=False,
            risk_level=RiskLevel.HIGH,
            production_impact=True,
            files_changed=["src/lib/honduras.ts"],
        )
    )


_UNSET: object = object()


def _contexto(
    *,
    target: object = _UNSET,
    result: object = _UNSET,
    policy: object = _UNSET,
    **extra: object,
) -> ReleaseContext:
    """Contexto real de la operación de publicación (``None`` explícito = no registrado)."""
    base: dict[str, object] = {
        "task_id": "3eabedb2-5385-41fb-98bf-6f846107dabc",
        "target": _destino(_autoridad()) if target is _UNSET else target,
        "policy_decision": _politica() if policy is _UNSET else policy,
        "result": _resultado() if result is _UNSET else result,
        "commit_sha": SHA,
        "branch": "ai/tarea",
        "repository": REPO,
        "destination_branch": "main",
        "destination_url": URL,
        "destination_remote": "origin",
        "mechanism": "git-push",
        "commit_present": True,
    }
    base.update(extra)
    return ReleaseContext(**base)  # type: ignore[arg-type]


def _estado(decision: object, nombre: str) -> str:
    """Estado de una condición por su nombre."""
    condiciones = decision.conditions  # type: ignore[attr-defined]
    return next(item.state for item in condiciones if item.name == nombre)


# ------------------------------------------------------------------ A · autorizado y verde
def test_case_a_target_autorizado_con_condiciones_verdes_es_autonomo() -> None:
    """A: autoridad persistente + condiciones demostradas ⇒ release autónomo, sin Human Gate."""
    decision = evaluate_release(_contexto())

    assert decision.disposition == ReleaseDisposition.AUTO.value
    assert decision.autonomous is True
    assert decision.blockers == ()
    assert len(decision.conditions) == 15, "se evalúan las quince condiciones"
    assert {item.state for item in decision.conditions} == {ConditionState.SATISFIED.value}
    # La política real sigue diciendo que hace falta persona: la autoridad persistente la sustituye.
    assert decision.policy_outcome == PolicyOutcome.REQUIRE_HUMAN.value
    assert decision.risk == _politica().effective_risk.name, "el riesgo es el de la política real"
    assert set(decision.authorized_operations) >= {"commit", "push", "deploy", "production_release"}


def test_case_a_sin_sobre_declarado_no_hay_autonomia() -> None:
    """A (fail closed): un destino sin sobre explícito no recibe autorización por omisión."""
    decision = evaluate_release(_contexto(target=_destino()))

    assert decision.disposition == ReleaseDisposition.HUMAN_GATE.value
    assert decision.authorized_operations == ()
    assert any("no autoriza la cadena completa" in item for item in decision.reasons)


# --------------------------------------------------------------------- B · no registrado
def test_case_b_target_no_registrado_falla_cerrado() -> None:
    """B: sin destino registrado no hay autoridad que consultar: DENIED."""
    decision = evaluate_release(_contexto(target=None))

    assert decision.disposition == ReleaseDisposition.DENIED.value
    assert decision.denied is True
    assert _estado(decision, "target_registered") == ConditionState.UNSATISFIED.value
    assert "no registrado" in decision.reasons[0]


# -------------------------------------------------------------------------- C · rama
def test_case_c_rama_distinta_de_la_autorizada_no_es_autonoma() -> None:
    """C: una rama que no es la declarada es una desviación material: Human Gate."""
    decision = evaluate_release(_contexto(branch="ai/otra-rama"))

    assert decision.disposition == ReleaseDisposition.HUMAN_GATE.value
    assert _estado(decision, "branch_authorized") == ConditionState.UNSATISFIED.value
    assert "branch_authorized" in [item.name for item in decision.blockers]


# -------------------------------------------------------------------------- D · commit
def test_case_d_un_commit_que_no_es_de_la_tarea_no_se_libera() -> None:
    """D: el commit debe ser el que produjo esta Task gobernada."""
    decision = evaluate_release(_contexto(commit_sha=OTRO_SHA))

    assert decision.disposition == ReleaseDisposition.HUMAN_GATE.value
    assert _estado(decision, "commit_from_governed_task") == ConditionState.UNSATISFIED.value


def test_case_d_sin_poder_comprobar_el_commit_falla_cerrado() -> None:
    """D (fail closed): si no se puede demostrar que el commit existe, no hay autonomía."""
    decision = evaluate_release(_contexto(commit_present=None))

    assert decision.disposition == ReleaseDisposition.HUMAN_GATE.value
    assert _estado(decision, "commit_from_governed_task") == ConditionState.UNKNOWN.value


# ------------------------------------------------------------------ E · cadena funcional
def test_case_e_sin_cadena_funcional_verificada_no_hay_release() -> None:
    """E: la cadena funcional del plan tiene que estar VERIFIED."""
    decision = evaluate_release(
        _contexto(result=_resultado(functional_chain_result="FAILED"))
    )

    assert decision.disposition == ReleaseDisposition.HUMAN_GATE.value
    assert _estado(decision, "functional_chain_verified") == ConditionState.UNSATISFIED.value
    assert _estado(decision, "qa_required_green") == ConditionState.UNSATISFIED.value


# ------------------------------------------------------------------------ F · QA verde
def test_case_f_qa_o_verificaciones_en_rojo_no_liberan() -> None:
    """F: una verificación fallida (o ninguna ejecutada) impide el release autónomo."""
    fallida = _resultado(
        verification=(
            CommandEvidence(
                name="typecheck", argv=("node", "tsc"), exit_code=1, duration_ms=10, passed=False
            ),
        )
    )
    sin_ejecutar = _resultado(verification=())

    decision_fallida = evaluate_release(_contexto(result=fallida))
    decision_vacia = evaluate_release(_contexto(result=sin_ejecutar))

    assert decision_fallida.disposition == ReleaseDisposition.HUMAN_GATE.value
    assert _estado(decision_fallida, "required_verifications_green") == (
        ConditionState.UNSATISFIED.value
    )
    assert decision_vacia.disposition == ReleaseDisposition.HUMAN_GATE.value
    assert _estado(decision_vacia, "required_verifications_green") == ConditionState.UNKNOWN.value


# ------------------------------------------------------------ G · cambio destructivo
def test_case_g_un_borrado_no_autorizado_exige_persona() -> None:
    """G: un borrado dentro de la Task vuelve a exigir persona salvo autorización explícita."""
    borrado = _resultado(
        applied=(
            AppliedChange(
                path="src/lib/obsoleto.ts",
                operation=ChangeOperation.DELETE,
                bytes_written=0,
                sha256="d" * 64,
                verified=True,
            ),
        ),
        final_scope=("src/lib/obsoleto.ts",),
    )

    con_persona = evaluate_release(_contexto(result=borrado))
    autorizado = evaluate_release(
        _contexto(target=_destino(_autoridad(allow_destructive=True)), result=borrado)
    )

    assert con_persona.disposition == ReleaseDisposition.HUMAN_GATE.value
    assert _estado(con_persona, "no_unauthorized_destructive_change") == (
        ConditionState.UNSATISFIED.value
    )
    assert autorizado.disposition == ReleaseDisposition.AUTO.value


# ------------------------------------------------------------------- H · secretos
def test_case_h_un_cambio_con_secretos_exige_persona() -> None:
    """H: ni texto con credenciales ni rutas del almacén de secretos se liberan solos."""
    from punto.schemas.build import BuildValidationIssue

    con_texto = _resultado(
        plan_issues=(
            BuildValidationIssue(code="PLAN_SECRET_TEXT", detail="el plan contiene una credencial"),
        )
    )
    con_ruta = _resultado(
        applied=(
            AppliedChange(
                path="src/.env.production",
                operation=ChangeOperation.MODIFY,
                bytes_written=10,
                sha256="e" * 64,
                verified=True,
            ),
        ),
        final_scope=("src/.env.production",),
    )

    decision_texto = evaluate_release(_contexto(result=con_texto))
    decision_ruta = evaluate_release(_contexto(result=con_ruta))

    assert decision_texto.disposition == ReleaseDisposition.HUMAN_GATE.value
    assert _estado(decision_texto, "no_sensitive_exposure") == ConditionState.UNSATISFIED.value
    assert decision_ruta.disposition == ReleaseDisposition.HUMAN_GATE.value
    assert _estado(decision_ruta, "no_sensitive_exposure") == ConditionState.UNSATISFIED.value


# ------------------------------------------------------- I · destino de producción
def test_case_i_un_destino_de_produccion_distinto_no_se_libera() -> None:
    """I: publicar en otro destino que el declarado es una desviación material."""
    otra_rama = evaluate_release(_contexto(destination_branch="prod"))
    otra_url = evaluate_release(_contexto(destination_url="https://otro.example/"))

    for decision in (otra_rama, otra_url):
        assert decision.disposition == ReleaseDisposition.HUMAN_GATE.value
        assert _estado(decision, "production_destination_matches_config") == (
            ConditionState.UNSATISFIED.value
        )


# ------------------------------------------------------ J · despliegue no verificable
def test_case_j_un_despliegue_no_verificable_no_cierra_produccion() -> None:
    """J: sin URL de producción no hay forma de comprobar el despliegue: no se cierra."""
    decision = evaluate_release(_contexto(destination_url=""))

    assert decision.disposition == ReleaseDisposition.HUMAN_GATE.value
    assert _estado(decision, "deployment_verifiable") == ConditionState.UNKNOWN.value


# ---------------------------------------- K · autorizado para commit, no para producción
def test_case_k_commit_autorizado_y_produccion_no_exige_persona_para_publicar() -> None:
    """K: el commit es del ciclo; publicar sin autorización persistente exige persona."""
    autoridad = _autoridad(
        push=False,
        deploy=False,
        production_release=False,
        deploy_mechanism="",
        allowed_branches=(),
    )
    decision = evaluate_release(_contexto(target=_destino(autoridad)))

    assert autoridad.allows(ReleaseOperation.COMMIT) is True
    assert autoridad.allows(ReleaseOperation.PRODUCTION_RELEASE) is False
    assert decision.disposition == ReleaseDisposition.HUMAN_GATE.value
    assert "commit" in decision.authorized_operations
    assert "production_release" not in decision.authorized_operations
    assert any("sin autorización persistente para" in item for item in decision.reasons)
    assert _estado(decision, "development_completed") == ConditionState.SATISFIED.value
    assert _estado(decision, "commit_from_governed_task") == ConditionState.SATISFIED.value


# ------------------------------------------------------------------ L · cadena completa
def test_case_l_autorizado_del_todo_libera_la_cadena_sin_gate() -> None:
    """L: con autoridad completa y condiciones verdes, la cadena entera es autónoma."""
    decision = evaluate_release(_contexto())

    assert decision.autonomous is True
    assert decision.disposition == ReleaseDisposition.AUTO.value
    assert decision.blockers == ()
    assert _estado(decision, "production_destination_matches_config") == (
        ConditionState.SATISFIED.value
    )
    assert _estado(decision, "deploy_mechanism_authorized") == ConditionState.SATISFIED.value
    assert _estado(decision, "deployment_verifiable") == ConditionState.SATISFIED.value


def test_una_denegacion_de_politica_no_se_levanta_con_autoridad_persistente() -> None:
    """Invariante: el sobre concede dentro de la política; nunca por encima de una denegación."""
    rechazo = _politica().model_copy(update={"outcome": PolicyOutcome.REJECT, "allowed": False})
    decision = evaluate_release(_contexto(policy=rechazo))

    assert decision.disposition == ReleaseDisposition.DENIED.value
    assert any("no puede levantar una denegación" in item for item in decision.reasons)


# ------------------------------------------------------------- configuración del sobre
def test_el_sobre_se_declara_en_la_configuracion_del_destino(tmp_path: Path) -> None:
    """El sobre entra por la configuración confiable del destino y se interpreta entero."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    destino = target_from_mapping(
        "punto-inmobiliario-hn",
        {
            "repository": str(repo),
            "baseline_sha": SHA,
            "work_branch": "ai/tarea",
            "production_branch": "main",
            "production_url": URL,
            "authority": {
                "local_changes": True,
                "commit": True,
                "push": True,
                "deploy": True,
                "production_release": True,
                "deploy_mechanism": "git-push",
            },
        },
    )

    assert destino.authority.declared is True
    assert destino.authority.release_authorized is True
    assert destino.authority.allows(ReleaseOperation.COMMIT) is True
    assert destino.authority.deploy_mechanism == "git-push"
    assert destino.authority.allowed_branches == ("main",), "por defecto, la rama de producción"
    assert destino.authority.require_qa is True, "el QA se exige salvo declaración en contra"


def test_un_destino_sin_sobre_no_declara_autoridad(tmp_path: Path) -> None:
    """Sin bloque ``authority`` el destino no autoriza nada: fail closed."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    destino = target_from_mapping(
        "punto-inmobiliario-hn",
        {
            "repository": str(repo),
            "baseline_sha": SHA,
            "production_branch": "main",
            "production_url": URL,
        },
    )

    assert destino.authority.declared is False
    assert destino.authority.release_authorized is False


@pytest.mark.parametrize(
    ("authority", "mensaje"),
    [
        ({"production_release": True}, "no declara deploy_mechanism"),
        ({"deploy": True, "deploy_mechanism": "vercel-api"}, "que PUNTO no ejecuta"),
        ({"commit": "sí"}, "debe ser booleano"),
        ("no-es-un-objeto", "debe ser un objeto"),
    ],
)
def test_un_sobre_invalido_no_concede_autoridad(
    tmp_path: Path, authority: object, mensaje: str
) -> None:
    """Un sobre mal declarado es un error de configuración: no se interpreta ni se adivina."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(DevelopmentTargetError, match=mensaje):
        target_from_mapping(
            "destino",
            {
                "repository": str(repo),
                "baseline_sha": SHA,
                "production_branch": "main",
                "production_url": URL,
                "work_branch": "ai/tarea",
                "authority": authority,
            },
        )


def test_la_rama_de_produccion_debe_estar_entre_las_autorizadas(tmp_path: Path) -> None:
    """Si el sobre declara ramas y no incluye la de producción, la declaración es incoherente."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(DevelopmentTargetError, match="no incluye su rama de producción"):
        target_from_mapping(
            "destino",
            {
                "repository": str(repo),
                "baseline_sha": SHA,
                "production_branch": "main",
                "production_url": URL,
                "authority": {"production_release": True, "deploy_mechanism": "git-push",
                              "allowed_branches": ["prod"]},
            },
        )
