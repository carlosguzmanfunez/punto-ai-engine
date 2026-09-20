"""Decisión de release autónomo: autoridad persistente del destino + condiciones verificables.

AP000-R01. Esta capa **no** sustituye a la autoridad del motor: la usa. Para una publicación:

1. el ``PolicyEngine`` decide, como siempre, sobre la acción (riesgo, nivel, prohibición);
2. el sobre persistente del destino dice si esa operación está **previamente concedida**;
3. quince condiciones verificables dicen si lo concedido se cumple **de verdad** en esta operación.

Solo entonces la publicación continúa sin Human Gate. La regla es asimétrica a propósito:

- una denegación de política **no** se levanta nunca con autoridad persistente (la autoridad no
  puede conceder lo que la política prohíbe);
- cualquier condición ``UNKNOWN``/``MISSING``/``UNTRUSTED`` (falta el dato, no se puede demostrar)
  falla cerrado y vuelve a exigir persona;
- cualquier condición insatisfecha es una desviación material y vuelve a exigir persona.

Nada de esto se evalúa con datos del proveedor ni del navegador: las señales vienen del resultado
real del ciclo, del repositorio del destino y de la configuración confiable del target.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from punto.policy.envelope import AdaptiveAuthorityEnvelope, ResourceClass
from punto.schemas.authority import KNOWN_DEPLOY_MECHANISMS, ReleaseOperation, TargetAuthority
from punto.schemas.dev import ChangeOperation, DevelopmentResult
from punto.schemas.policy import PolicyDecision
from punto.workspace.target import DevelopmentTarget

__all__ = [
    "ConditionState",
    "ReleaseCondition",
    "ReleaseContext",
    "ReleaseDecision",
    "ReleaseDisposition",
    "evaluate_release",
]

#: Códigos con los que el ciclo rechaza un texto que contiene una credencial.
SECRET_ISSUE_CODES = frozenset({"PLAN_SECRET_TEXT", "CHANGE_SECRET_TEXT"})

#: Estado real que significa «desarrollo completado».
COMPLETED_STATUS = "DEVELOPMENT_COMPLETED"

#: Estado real de la cadena funcional verificada.
CHAIN_VERIFIED = "VERIFIED"

#: Mecanismo que PUNTO ejecuta de verdad: el push gobernado a la rama de producción.
GIT_PUSH_MECHANISM = "git-push"


class ReleaseDisposition(StrEnum):
    """Desenlace de la evaluación: se ejecuta solo, exige persona o queda denegado."""

    AUTO = "AUTO"
    HUMAN_GATE = "HUMAN_GATE"
    DENIED = "DENIED"


class ConditionState(StrEnum):
    """Estado de una condición verificable (``UNKNOWN`` falla cerrado)."""

    SATISFIED = "SATISFIED"
    UNSATISFIED = "UNSATISFIED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ReleaseCondition:
    """Condición evaluada, con la evidencia real que la sostiene."""

    name: str
    state: str
    evidence: str

    @property
    def satisfied(self) -> bool:
        """True solo si la condición está demostrada."""
        return self.state == ConditionState.SATISFIED.value

    @property
    def blocking(self) -> bool:
        """True si la condición impide la autonomía (insatisfecha o sin dato)."""
        return not self.satisfied

    def as_dict(self) -> dict[str, str]:
        """Vista serializable."""
        return {"name": self.name, "state": self.state, "evidence": self.evidence}


@dataclass(frozen=True, slots=True)
class ReleaseContext:
    """Señales reales de una operación de release, ya recogidas por quien la va a ejecutar.

    Todo campo ``None`` significa «no se pudo demostrar»: la condición correspondiente quedará
    ``UNKNOWN`` y la decisión fallará cerrado.
    """

    task_id: str
    target: DevelopmentTarget | None
    policy_decision: PolicyDecision | None
    result: DevelopmentResult | None
    commit_sha: str = ""
    branch: str = ""
    repository: Path | None = None
    destination_branch: str = ""
    destination_url: str = ""
    destination_remote: str = ""
    mechanism: str = GIT_PUSH_MECHANISM
    commit_present: bool | None = None
    operation: str = ReleaseOperation.PRODUCTION_RELEASE.value


@dataclass(frozen=True, slots=True)
class ReleaseDecision:
    """Decisión de release: desenlace, condiciones evaluadas y por qué."""

    disposition: str
    operation: str
    target_id: str
    conditions: tuple[ReleaseCondition, ...] = ()
    reasons: tuple[str, ...] = ()
    risk: str = ""
    policy_outcome: str = ""
    policy_decision_id: str = ""
    authority_class: str = ""
    authorized_operations: tuple[str, ...] = field(default=())

    @property
    def autonomous(self) -> bool:
        """True si la operación puede ejecutarse sin Human Gate."""
        return self.disposition == ReleaseDisposition.AUTO.value

    @property
    def denied(self) -> bool:
        """True si la operación queda denegada (no es una mera falta de autorización)."""
        return self.disposition == ReleaseDisposition.DENIED.value

    @property
    def blockers(self) -> tuple[ReleaseCondition, ...]:
        """Condiciones que impiden la autonomía, en orden de evaluación."""
        return tuple(item for item in self.conditions if item.blocking)

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable, sin contenido de ficheros ni credenciales."""
        return {
            "disposition": self.disposition,
            "autonomous": self.autonomous,
            "operation": self.operation,
            "target_id": self.target_id,
            "risk": self.risk,
            "policy_outcome": self.policy_outcome,
            "policy_decision_id": self.policy_decision_id,
            "authority_class": self.authority_class,
            "authorized_operations": list(self.authorized_operations),
            "reasons": list(self.reasons),
            "conditions": [item.as_dict() for item in self.conditions],
            "blockers": [item.name for item in self.blockers],
        }


def evaluate_release(context: ReleaseContext) -> ReleaseDecision:
    """Evalúa si una operación de release puede ejecutarse sin Human Gate.

    Args:
        context: Señales reales de la operación (target, resultado del ciclo, política y destino).

    Returns:
        La decisión con **todas** las condiciones evaluadas y sus motivos.
    """
    target = context.target
    if target is None:
        sin_destino: tuple[ReleaseCondition, ...] = (
            _condition(
                "target_registered",
                ConditionState.UNSATISFIED,
                "el destino de la operación no está registrado en PUNTO",
            ),
        )
        return _decision(
            context,
            ReleaseDisposition.DENIED,
            sin_destino,
            ("destino no registrado: no hay autoridad que consultar (fail closed)",),
        )

    authority = target.authority
    conditions: tuple[ReleaseCondition, ...] = _conditions(context, target)
    policy = context.policy_decision
    authorized = tuple(
        operation.value for operation in ReleaseOperation if authority.allows(operation)
    )
    if policy is not None and policy.is_rejected:
        return _decision(
            context,
            ReleaseDisposition.DENIED,
            conditions,
            (
                "la política rechaza la operación: la autoridad persistente del destino no puede "
                "levantar una denegación de política",
                *policy.reasons,
            ),
            authority_operations=authorized,
        )
    if not authority.release_authorized:
        return _decision(
            context,
            ReleaseDisposition.HUMAN_GATE,
            conditions,
            (
                "el sobre persistente del destino no autoriza la cadena completa de publicación "
                "(push + despliegue + release): hace falta una persona",
                *_missing_grants(authority),
            ),
            authority_operations=authorized,
        )
    blockers = [item for item in conditions if item.blocking]
    if blockers:
        return _decision(
            context,
            ReleaseDisposition.HUMAN_GATE,
            conditions,
            tuple(f"{item.name}: {item.evidence}" for item in blockers),
            authority_operations=authorized,
        )
    return _decision(
        context,
        ReleaseDisposition.AUTO,
        conditions,
        (
            "operación dentro de la autoridad persistente del destino y con todas las condiciones "
            "demostradas: continúa sin Human Gate",
        ),
        authority_operations=authorized,
    )


def _conditions(context: ReleaseContext, target: DevelopmentTarget) -> tuple[ReleaseCondition, ...]:
    """Las quince condiciones aplicables, evaluadas con datos reales."""
    authority = target.authority
    result = context.result
    return (
        _target_registered(target),
        _repository_matches(context, target),
        _branch_authorized(context, target),
        _production_branch_authorized(context, target),
        _commit_from_task(context, result),
        _development_completed(result),
        _functional_chain_verified(result),
        _verifications_green(result),
        _qa_green(result, authority.require_qa),
        _no_unauthorized_destructive(result, authority.allow_destructive),
        _no_sensitive_exposure(result),
        _scope_within_authority(target, result),
        _destination_matches_config(context, target),
        _deploy_mechanism_authorized(context, target),
        _deployment_verifiable(context),
    )


def _condition(name: str, state: ConditionState, evidence: str) -> ReleaseCondition:
    """Condición con su estado y su evidencia."""
    return ReleaseCondition(name=name, state=state.value, evidence=evidence[:300])


def _target_registered(target: DevelopmentTarget) -> ReleaseCondition:
    """1. El destino está registrado (si no, ni siquiera habría evaluación)."""
    return _condition(
        "target_registered",
        ConditionState.SATISFIED,
        f"destino {target.target_id!r} registrado en la configuración confiable",
    )


def _repository_matches(context: ReleaseContext, target: DevelopmentTarget) -> ReleaseCondition:
    """2. El repositorio de la operación es el repositorio del destino."""
    if context.repository is None:
        return _condition(
            "repository_matches_target", ConditionState.UNKNOWN, "no se declaró el repositorio"
        )
    if Path(context.repository) != Path(target.repository):
        return _condition(
            "repository_matches_target",
            ConditionState.UNSATISFIED,
            "el repositorio de la operación no es el del destino",
        )
    return _condition(
        "repository_matches_target",
        ConditionState.SATISFIED,
        "el repositorio de la operación es el declarado por el destino",
    )


def _branch_authorized(context: ReleaseContext, target: DevelopmentTarget) -> ReleaseCondition:
    """3. La rama de trabajo o de release está autorizada."""
    branch = context.branch.strip() or target.work_branch
    if branch and target.work_branch and branch != target.work_branch:
        return _condition(
            "branch_authorized",
            ConditionState.UNSATISFIED,
            f"la rama {branch!r} no es la rama de trabajo declarada {target.work_branch!r}",
        )
    if not branch:
        return _condition(
            "branch_authorized", ConditionState.UNKNOWN, "no se declaró la rama de la operación"
        )
    return _condition(
        "branch_authorized", ConditionState.SATISFIED, f"rama de trabajo {branch!r} autorizada"
    )


def _production_branch_authorized(
    context: ReleaseContext, target: DevelopmentTarget
) -> ReleaseCondition:
    """4. La rama de producción está declarada y autorizada por el sobre."""
    allowed = target.authority.allowed_branches
    destination = context.destination_branch.strip() or target.production_branch
    if not destination:
        return _condition(
            "production_branch_authorized",
            ConditionState.UNKNOWN,
            "el destino no declara rama de producción",
        )
    if destination != target.production_branch:
        return _condition(
            "production_branch_authorized",
            ConditionState.UNSATISFIED,
            f"la rama de destino {destination!r} no es la de producción declarada "
            f"{target.production_branch!r}",
        )
    if allowed and destination not in allowed:
        return _condition(
            "production_branch_authorized",
            ConditionState.UNSATISFIED,
            f"la rama {destination!r} no está entre las autorizadas por el destino",
        )
    return _condition(
        "production_branch_authorized",
        ConditionState.SATISFIED,
        f"rama de producción {destination!r} autorizada por el destino",
    )


def _commit_from_task(
    context: ReleaseContext, result: DevelopmentResult | None
) -> ReleaseCondition:
    """5. El commit es el que produjo **esta** Task gobernada y existe en el repositorio."""
    if result is None or not result.commit_sha:
        return _condition(
            "commit_from_governed_task",
            ConditionState.UNKNOWN,
            "la tarea no tiene commit local del ciclo",
        )
    if not context.commit_sha or context.commit_sha != result.commit_sha:
        return _condition(
            "commit_from_governed_task",
            ConditionState.UNSATISFIED,
            "el commit de la operación no es el que produjo esta tarea",
        )
    if context.commit_present is None:
        return _condition(
            "commit_from_governed_task",
            ConditionState.UNKNOWN,
            "no se pudo comprobar que el commit exista en el repositorio",
        )
    if not context.commit_present:
        return _condition(
            "commit_from_governed_task",
            ConditionState.UNSATISFIED,
            f"el commit {context.commit_sha[:12]}… no está en el repositorio del destino",
        )
    return _condition(
        "commit_from_governed_task",
        ConditionState.SATISFIED,
        f"commit {context.commit_sha[:12]}… de esta tarea, presente en el repositorio",
    )


def _development_completed(result: DevelopmentResult | None) -> ReleaseCondition:
    """6. El desarrollo terminó bien."""
    if result is None:
        return _condition(
            "development_completed", ConditionState.UNKNOWN, "no hay resultado del ciclo"
        )
    if result.status.value != COMPLETED_STATUS:
        return _condition(
            "development_completed",
            ConditionState.UNSATISFIED,
            f"el ciclo terminó en {result.status.value}",
        )
    return _condition(
        "development_completed",
        ConditionState.SATISFIED,
        "el ciclo terminó en DEVELOPMENT_COMPLETED",
    )


def _functional_chain_verified(result: DevelopmentResult | None) -> ReleaseCondition:
    """7. La cadena funcional del plan quedó verificada eslabón a eslabón."""
    if result is None:
        return _condition(
            "functional_chain_verified", ConditionState.UNKNOWN, "no hay resultado del ciclo"
        )
    if result.functional_chain_result != CHAIN_VERIFIED:
        return _condition(
            "functional_chain_verified",
            ConditionState.UNSATISFIED,
            f"la cadena funcional quedó en {result.functional_chain_result or 'sin verificar'}",
        )
    return _condition(
        "functional_chain_verified", ConditionState.SATISFIED, "cadena funcional VERIFIED"
    )


def _verifications_green(result: DevelopmentResult | None) -> ReleaseCondition:
    """8. Se ejecutó al menos una verificación y todas pasaron."""
    if result is None:
        return _condition(
            "required_verifications_green", ConditionState.UNKNOWN, "no hay resultado del ciclo"
        )
    if not result.verification:
        return _condition(
            "required_verifications_green",
            ConditionState.UNKNOWN,
            "no se ejecutó ninguna verificación del catálogo del destino",
        )
    failed = [item.name for item in result.verification if not item.passed]
    if failed:
        return _condition(
            "required_verifications_green",
            ConditionState.UNSATISFIED,
            f"verificaciones en rojo: {', '.join(failed)}",
        )
    names = ", ".join(item.name for item in result.verification)
    return _condition(
        "required_verifications_green",
        ConditionState.SATISFIED,
        f"verificaciones en verde: {names}",
    )


def _qa_green(result: DevelopmentResult | None, require_qa: bool) -> ReleaseCondition:
    """9. El QA del ciclo (cadena funcional) está verde, si el destino lo exige."""
    if not require_qa:
        return _condition(
            "qa_required_green",
            ConditionState.SATISFIED,
            "el destino declara que no exige QA adicional",
        )
    if result is None:
        return _condition("qa_required_green", ConditionState.UNKNOWN, "no hay resultado del ciclo")
    if result.functional_chain_result != CHAIN_VERIFIED:
        return _condition(
            "qa_required_green",
            ConditionState.UNSATISFIED,
            "el destino exige QA y la cadena funcional no está VERIFIED",
        )
    return _condition(
        "qa_required_green",
        ConditionState.SATISFIED,
        "QA del ciclo verde (cadena funcional VERIFIED)",
    )


def _no_unauthorized_destructive(
    result: DevelopmentResult | None, allow_destructive: bool
) -> ReleaseCondition:
    """10. No hay cambios destructivos ni cambios rechazados sin autorizar."""
    if result is None:
        return _condition(
            "no_unauthorized_destructive_change",
            ConditionState.UNKNOWN,
            "no hay resultado del ciclo",
        )
    if result.change_issues:
        codes = ", ".join(sorted({item.code for item in result.change_issues}))
        return _condition(
            "no_unauthorized_destructive_change",
            ConditionState.UNSATISFIED,
            f"el ciclo dejó cambios rechazados: {codes}",
        )
    deleted = [
        item.path for item in result.applied if item.operation is ChangeOperation.DELETE
    ]
    if deleted and not allow_destructive:
        return _condition(
            "no_unauthorized_destructive_change",
            ConditionState.UNSATISFIED,
            f"borrados sin autorización en el destino: {', '.join(sorted(deleted))}",
        )
    return _condition(
        "no_unauthorized_destructive_change",
        ConditionState.SATISFIED,
        "sin borrados sin autorizar ni cambios rechazados",
    )


def _no_sensitive_exposure(result: DevelopmentResult | None) -> ReleaseCondition:
    """11. Ni el plan ni los cambios tocan credenciales o texto con secretos."""
    if result is None:
        return _condition("no_sensitive_exposure", ConditionState.UNKNOWN, "no hay resultado")
    issues = (*result.plan_issues, *result.change_issues)
    if any(item.code in SECRET_ISSUE_CODES for item in issues):
        return _condition(
            "no_sensitive_exposure",
            ConditionState.UNSATISFIED,
            "el ciclo detectó texto con credenciales en el plan o en el cambio",
        )
    classifier = AdaptiveAuthorityEnvelope()
    sensitive = [
        item.path
        for item in result.applied
        if classifier.classify(item.path) is ResourceClass.SECRET_STORE
    ]
    if sensitive:
        return _condition(
            "no_sensitive_exposure",
            ConditionState.UNSATISFIED,
            f"el cambio toca el almacén de secretos: {', '.join(sorted(sensitive))}",
        )
    return _condition(
        "no_sensitive_exposure", ConditionState.SATISFIED, "sin credenciales ni rutas sensibles"
    )


def _scope_within_authority(
    target: DevelopmentTarget, result: DevelopmentResult | None
) -> ReleaseCondition:
    """12. Todo lo aplicado cae dentro del alcance y del presupuesto declarados por el destino."""
    if result is None:
        return _condition("scope_within_granted_authority", ConditionState.UNKNOWN, "sin resultado")
    scope = tuple(result.final_scope or result.initial_scope)
    if not scope:
        return _condition(
            "scope_within_granted_authority", ConditionState.UNKNOWN, "el ciclo no declaró alcance"
        )
    roots = target.scope_roots
    outside = [path for path in scope if not _within(path, roots)]
    if outside:
        return _condition(
            "scope_within_granted_authority",
            ConditionState.UNSATISFIED,
            f"fuera de las raíces autorizadas: {', '.join(sorted(outside))}",
        )
    if len(result.applied) > target.max_files_changed:
        return _condition(
            "scope_within_granted_authority",
            ConditionState.UNSATISFIED,
            f"{len(result.applied)} ficheros aplicados y el tope del destino es "
            f"{target.max_files_changed}",
        )
    return _condition(
        "scope_within_granted_authority",
        ConditionState.SATISFIED,
        f"{len(result.applied)} fichero(s) aplicados dentro de {len(scope)} recurso(s) del alcance",
    )


def _destination_matches_config(
    context: ReleaseContext, target: DevelopmentTarget
) -> ReleaseCondition:
    """13. El destino de producción de la operación es el de la configuración confiable."""
    expected = (target.production_branch, target.production_url, target.publish_remote)
    actual = (
        context.destination_branch or target.production_branch,
        context.destination_url or target.production_url,
        context.destination_remote or target.publish_remote,
    )
    if not all(expected):
        return _condition(
            "production_destination_matches_config",
            ConditionState.UNKNOWN,
            "el destino no declara rama, URL y remoto de producción",
        )
    if actual != expected:
        return _condition(
            "production_destination_matches_config",
            ConditionState.UNSATISFIED,
            "el destino de la operación no coincide con la configuración del target",
        )
    return _condition(
        "production_destination_matches_config",
        ConditionState.SATISFIED,
        f"destino de producción {target.production_branch}@{target.publish_remote}",
    )


def _deploy_mechanism_authorized(
    context: ReleaseContext, target: DevelopmentTarget
) -> ReleaseCondition:
    """14. El mecanismo de despliegue está autorizado por el sobre y PUNTO sabe ejecutarlo."""
    declared = target.authority.deploy_mechanism.strip()
    mechanism = (context.mechanism or GIT_PUSH_MECHANISM).strip()
    if not declared:
        return _condition(
            "deploy_mechanism_authorized",
            ConditionState.UNKNOWN,
            "el destino no declara mecanismo de despliegue autorizado",
        )
    if mechanism != declared:
        return _condition(
            "deploy_mechanism_authorized",
            ConditionState.UNSATISFIED,
            f"la operación usa {mechanism!r} y el destino autoriza {declared!r}",
        )
    if mechanism not in KNOWN_DEPLOY_MECHANISMS:
        return _condition(
            "deploy_mechanism_authorized",
            ConditionState.UNSATISFIED,
            f"PUNTO no ejecuta el mecanismo {mechanism!r}",
        )
    return _condition(
        "deploy_mechanism_authorized",
        ConditionState.SATISFIED,
        f"mecanismo autorizado y ejecutable: {mechanism}",
    )


def _deployment_verifiable(context: ReleaseContext) -> ReleaseCondition:
    """15. El despliegue se podrá comprobar después (hay URL de producción declarada)."""
    url = (context.destination_url or "").strip()
    if not url:
        return _condition(
            "deployment_verifiable",
            ConditionState.UNKNOWN,
            "no hay URL de producción declarada que permita comprobar el despliegue",
        )
    return _condition(
        "deployment_verifiable",
        ConditionState.SATISFIED,
        "URL de producción declarada y comprobable",
    )


def _within(path: str, roots: Sequence[str]) -> bool:
    """True si la ruta cae dentro de alguna de las raíces autorizadas."""
    normalized = path.replace("\\", "/").strip("/")
    for root in roots:
        candidate = root.replace("\\", "/").strip("/")
        if not candidate:
            continue
        if normalized == candidate or normalized.startswith(f"{candidate}/"):
            return True
    return False


def _missing_grants(authority: TargetAuthority) -> tuple[str, ...]:
    """Operaciones de la cadena de release que el sobre **no** autoriza."""
    missing = [
        operation.value
        for operation in (
            ReleaseOperation.PUSH,
            ReleaseOperation.DEPLOY,
            ReleaseOperation.PRODUCTION_RELEASE,
        )
        if not authority.allows(operation)
    ]
    return (f"sin autorización persistente para: {', '.join(missing)}",) if missing else ()


def _decision(
    context: ReleaseContext,
    disposition: ReleaseDisposition,
    conditions: tuple[ReleaseCondition, ...],
    reasons: tuple[str, ...],
    *,
    authority_operations: tuple[str, ...] = (),
) -> ReleaseDecision:
    """Decisión con la evidencia de la política real."""
    policy = context.policy_decision
    return ReleaseDecision(
        disposition=disposition.value,
        operation=context.operation,
        target_id=context.target.target_id if context.target is not None else "",
        conditions=conditions,
        reasons=tuple(item[:400] for item in reasons if item),
        risk=policy.effective_risk.name if policy is not None else "",
        policy_outcome=policy.outcome.value if policy is not None else "",
        policy_decision_id=str(policy.id) if policy is not None else "",
        authority_class=(
            "HUMAN_GATE_REQUIRED"
            if disposition is ReleaseDisposition.HUMAN_GATE
            else disposition.value
        ),
        authorized_operations=authority_operations,
    )
