"""Policy Engine: evaluación determinista de acciones.

Orden de evaluación (fijo y auditable):

1. **DEFAULT DENY** — acción no catalogada: rechazo inmediato.
2. **Protección constitucional** — modificación de ``constitution.yaml`` o
   ``permissions.yaml``: rechazo inmediato e inapelable, aunque la acción sea
   técnica, reversible y de riesgo LOW.
3. **Autoelevación de autoridad** — CAMUS no puede elevar su propia autoridad:
   rechazo inmediato.
4. **Riesgo** — cálculo del riesgo efectivo (máximo entre declarado y calculado).
   HIGH/CRITICAL detienen la ejecución en el Human Gate.
5. **Presupuesto** — costo, tiempo y número de archivos dentro de los límites del
   nivel. Exceder cualquiera rechaza la acción.
6. **Autoridad** — decisión final según el nivel: 0 autónomo, 1 autónomo con
   revisión, 2 decisión de CAMUS sujeta a las reglas anteriores, 3 Human Gate.
7. **Regla de autonomía** — si una acción técnica y reversible no cumple las
   condiciones estrictas de autonomía, no se ejecuta de forma autónoma.

Regla de autonomía (Constitución, prioridad 1)::

    technical AND reversible AND risk <= MEDIUM
    AND dentro de presupuesto, tiempo y máximo de archivos
    AND production_impact == false
    AND legal_impact == false
    AND business_impact == false
    => el sistema continúa autónomamente según el Authority Level de la acción.

Las prohibiciones constitucionales también existen como **piso en código**
(``punto.policy.permissions``), de modo que CAMUS no puede desactivar su propia
protección manipulando la configuración.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final
from uuid import UUID

from punto.common import normalize_path
from punto.policy.authority import (
    AuthorityCatalog,
    AuthorityRule,
)
from punto.policy.autonomy import (
    AutonomyContext,
    AutonomyPolicy,
    AutonomyQuery,
    AutonomyVerdict,
)
from punto.policy.budgets import BudgetBreach, BudgetPolicy
from punto.policy.config_loader import ConfigLoader
from punto.policy.permissions import (
    CONSTITUTIONAL_PROTECTED_PATHS,
    PROTECTED_FILE_REJECTION_REASON,
    is_constitutionally_blocked,
    protected_paths_in,
)
from punto.policy.risk import RiskAssessment, RiskEngine
from punto.schemas.decision import ActionRequest
from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.policy import PolicyDecision, PolicyOutcome

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

#: Operaciones que escriben o pueden escribir en el sistema de archivos.
WRITE_OPERATION_PREFIXES: Final[tuple[str, ...]] = (
    "create_",
    "modify_",
    "delete_",
    "remove_",
    "install_",
    "replace_",
    "deploy_",
    "refactor_",
    "fix_",
)

#: Sufijos que indican operaciones de escritura.
WRITE_OPERATION_SUFFIXES: Final[tuple[str, ...]] = (
    "_file",
    "_files",
    "_branch",
    "_commit",
    "_dependency",
    "_module",
    "_schema",
    "_api",
    "_library",
    "_component",
)

#: Resultado usado cuando se rechaza por protección constitucional.
PROTECTED_REJECTION_OUTCOME: Final[PolicyOutcome] = PolicyOutcome.REJECT


@dataclass(frozen=True, slots=True)
class PolicyEvaluationContext:
    """Contexto adicional para una evaluación de política.

    Permite declarar límites efectivos de una tarea ya existente sin alterar la
    :class:`ActionRequest` original.
    """

    task_max_cost_usd: float | None = None
    task_max_execution_minutes: float | None = None
    task_max_files_changed: int | None = None
    actor: str = "camus"
    #: Hechos de confianza del destino, fijados por código de PUNTO. Sin él no hay autonomía
    #: delegada: un riesgo HIGH sigue exigiendo persona (fail-closed).
    autonomy: AutonomyContext | None = None


@dataclass(frozen=True, slots=True)
class PolicyConfigBundle:
    """Configuración consolidada que consume el Policy Engine."""

    constitution: dict[str, Any] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    risk_rules: dict[str, Any] = field(default_factory=dict)
    budgets: dict[str, Any] = field(default_factory=dict)
    models: dict[str, Any] = field(default_factory=dict)
    environments: dict[str, Any] = field(default_factory=dict)
    autonomy: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_loader(cls, loader: ConfigLoader) -> PolicyConfigBundle:
        """Carga el bundle completo desde el :class:`ConfigLoader`."""
        return cls(
            constitution=loader.load("constitution"),
            permissions=loader.load("permissions"),
            risk_rules=loader.load("risk_rules"),
            budgets=loader.load("budgets"),
            models=loader.load("models"),
            environments=loader.load("environments"),
            autonomy=loader.load_optional("autonomy"),
        )

    @property
    def protected_files(self) -> tuple[str, ...]:
        """Rutas protegidas declaradas en la configuración, normalizadas."""
        declared: list[str] = []
        raw = self.constitution.get("protected_files", [])
        if isinstance(raw, list):
            for entry in raw:
                if isinstance(entry, dict) and "path" in entry:
                    declared.append(str(entry["path"]))
                elif isinstance(entry, str):
                    declared.append(entry)
        raw_extra = self.constitution.get("additional_protected_paths", [])
        if isinstance(raw_extra, list):
            declared.extend(str(item) for item in raw_extra)
        return tuple(declared)


class PolicyEngine:
    """Motor de política determinista del núcleo constitucional."""

    def __init__(
        self,
        *,
        catalog: AuthorityCatalog,
        risk_engine: RiskEngine,
        budget_policy: BudgetPolicy,
        constitution: dict[str, Any] | None = None,
        permissions: dict[str, Any] | None = None,
        environments: dict[str, Any] | None = None,
        environment: str = "local",
        autonomy: AutonomyPolicy | None = None,
    ) -> None:
        self._autonomy = autonomy or AutonomyPolicy()
        self._catalog = catalog
        self._risk = risk_engine
        self._budgets = budget_policy
        self._constitution = dict(constitution or {})
        self._permissions = dict(permissions or {})
        self._environments = dict(environments or {})
        self._environment = environment
        self._decisions: list[PolicyDecision] = []
        #: Índice por identificador. Permite recuperar *la* decisión que originó
        #: un artefacto concreto (por ejemplo un Human Gate) sin recurrir a la
        #: posición en el historial, que con tareas concurrentes es ambiguo.
        self._decisions_by_id: dict[UUID, PolicyDecision] = {}

    # ------------------------------------------------------------------ loaders
    @classmethod
    def from_config(
        cls,
        config_dir: Path | None = None,
        *,
        environment: str = "local",
    ) -> PolicyEngine:
        """Construye el Policy Engine desde los archivos YAML de ``config/``."""
        loader = ConfigLoader(config_dir)
        bundle = PolicyConfigBundle.from_loader(loader)
        return cls.from_bundle(bundle, environment=environment)

    @classmethod
    def from_bundle(
        cls,
        bundle: PolicyConfigBundle,
        *,
        environment: str = "local",
    ) -> PolicyEngine:
        """Construye el Policy Engine desde un bundle de configuración."""
        return cls(
            catalog=_catalog_from_permissions(bundle.permissions),
            risk_engine=RiskEngine.from_config(bundle.risk_rules),
            budget_policy=BudgetPolicy.from_config(bundle.budgets),
            constitution=bundle.constitution,
            permissions=bundle.permissions,
            environments=bundle.environments,
            environment=environment,
            autonomy=AutonomyPolicy.from_config(bundle.autonomy),
        )

    # ---------------------------------------------------------------- accessors
    @property
    def catalog(self) -> AuthorityCatalog:
        """Catálogo de autoridad en uso."""
        return self._catalog

    @property
    def risk_engine(self) -> RiskEngine:
        """Risk Engine en uso."""
        return self._risk

    @property
    def budget_policy(self) -> BudgetPolicy:
        """Política de presupuesto en uso."""
        return self._budgets

    @property
    def autonomy(self) -> AutonomyPolicy:
        """Política de autonomía preautorizada en uso (vacía ⇒ sin autonomía delegada)."""
        return self._autonomy

    @property
    def environment(self) -> str:
        """Entorno lógico activo."""
        return self._environment

    @property
    def decisions(self) -> tuple[PolicyDecision, ...]:
        """Historial en memoria de decisiones emitidas."""
        return tuple(self._decisions)

    def decision_by_id(self, decision_id: UUID) -> PolicyDecision | None:
        """Recupera una decisión emitida por su identificador.

        Es la vía de acceso **exclusiva** para asociar una decisión a un
        artefacto derivado (Human Gate). Devuelve ``None`` si la decisión no
        pertenece a este motor, de modo que el llamante no pueda caer de vuelta
        en "la última decisión emitida".
        """
        return self._decisions_by_id.get(decision_id)

    @property
    def protected_paths(self) -> tuple[str, ...]:
        """Rutas protegidas: unión del piso en código y la configuración explícita."""
        declared = PolicyConfigBundle(constitution=self._constitution).protected_files
        return tuple(sorted({*CONSTITUTIONAL_PROTECTED_PATHS, *declared}))

    def environment_config(self) -> dict[str, Any]:
        """Configuración del entorno activo, si está declarada."""
        raw = self._environments.get("environments", {})
        if isinstance(raw, dict):
            entry = raw.get(self._environment)
            if isinstance(entry, dict):
                return {str(key): value for key, value in entry.items()}
        return {}

    # ---------------------------------------------------------------- evaluation
    def evaluate(
        self,
        request: ActionRequest,
        context: PolicyEvaluationContext | None = None,
    ) -> PolicyDecision:
        """Evalúa una petición y emite una :class:`PolicyDecision` inmutable."""
        ctx = context or PolicyEvaluationContext()
        reasons: list[str] = []

        # 1. DEFAULT DENY.
        rule = self._catalog.get(request.action)
        if rule is None:
            decision = PolicyDecision(
                allowed=False,
                authority_level=AuthorityLevel.LEVEL_3_HUMAN,
                requires_review=False,
                requires_human=True,
                reason=(
                    f"DEFAULT DENY: la acción '{request.action}' no está catalogada. "
                    "No se concede autoridad autónoma a acciones desconocidas."
                ),
                outcome=PolicyOutcome.REJECT,
                effective_risk=RiskLevel.CRITICAL,
                action=request.action,
                reasons=(f"acción desconocida: {request.action}",),
            )
            self._record(decision)
            return decision

        # 2. Protección constitucional.
        protected = protected_paths_in(request.files_changed)
        if protected and is_constitutionally_blocked(request.action, request.files_changed):
            decision = PolicyDecision(
                allowed=False,
                authority_level=AuthorityLevel.LEVEL_3_HUMAN,
                requires_review=False,
                requires_human=True,
                reason=PROTECTED_FILE_REJECTION_REASON,
                outcome=PROTECTED_REJECTION_OUTCOME,
                effective_risk=RiskLevel.CRITICAL,
                action=request.action,
                reasons=(
                    "archivo constitucionalmente protegido: " + ", ".join(protected),
                    "CAMUS no puede modificar sus propias reglas de autoridad",
                ),
                protected_files=protected,
            )
            self._record(decision)
            return decision

        # 3. Autoelevación de autoridad prohibida.
        if self._is_self_elevation(rule, request.files_changed):
            decision = PolicyDecision(
                allowed=False,
                authority_level=AuthorityLevel.LEVEL_3_HUMAN,
                requires_review=False,
                requires_human=True,
                reason=(
                    "REJECT: autoelevación de autoridad prohibida. CAMUS no puede "
                    "modificar su catálogo de permisos ni sus presupuestos."
                ),
                outcome=PolicyOutcome.REJECT,
                effective_risk=RiskLevel.CRITICAL,
                action=request.action,
                reasons=("autoelevación de autoridad",),
                protected_files=protected,
            )
            self._record(decision)
            return decision

        # 4. Riesgo efectivo.
        assessment = self._risk.assess(request)
        reasons.extend(assessment.reasons)

        # 5. Presupuesto.
        breaches = self._effective_breaches(request, rule.level, ctx)
        budget_exceeded = bool(breaches)

        # 6. Autonomía preautorizada: un riesgo HIGH por sí solo no es una frontera de autoridad.
        preauthorized = self._preauthorize(request, rule, assessment, ctx, reasons)

        # 7. Regla de autonomía.
        autonomy_violations = self._autonomy_violations(
            request, rule.level, assessment, preauthorized=preauthorized is not None
        )

        if budget_exceeded:
            reason = "REJECT: presupuesto excedido; " + "; ".join(
                breach.detail for breach in breaches
            )
            reasons.extend(breach.detail for breach in breaches)
            decision = PolicyDecision(
                allowed=False,
                authority_level=rule.level,
                requires_review=False,
                requires_human=False,
                reason=reason,
                outcome=PolicyOutcome.REJECT,
                effective_risk=assessment.effective,
                action=request.action,
                reasons=tuple(reasons),
                protected_files=protected,
            )
            self._record(decision)
            return decision

        if (assessment.requires_human_gate and preauthorized is None) or rule.level.requires_human:
            human_reasons = [
                f"autoridad {rule.level.name}",
                f"riesgo {assessment.effective.name}",
            ]
            if assessment.requires_human_gate:
                reasons.append(
                    f"riesgo {assessment.effective.name} requiere Human Gate por defecto"
                )
            if rule.level.requires_human:
                reasons.append(f"nivel de autoridad {rule.level.name} requiere Human Gate")
            decision = PolicyDecision(
                allowed=False,
                authority_level=rule.level,
                requires_review=False,
                requires_human=True,
                reason=(
                    "HUMAN GATE: " + " y ".join(human_reasons) + " requieren aprobación humana."
                ),
                outcome=PolicyOutcome.REQUIRE_HUMAN,
                effective_risk=assessment.effective,
                action=request.action,
                reasons=tuple(reasons),
                protected_files=protected,
            )
            self._record(decision)
            return decision

        if autonomy_violations:
            reasons.extend(autonomy_violations)
            decision = PolicyDecision(
                allowed=False,
                authority_level=rule.level,
                requires_review=False,
                requires_human=False,
                reason=(
                    "REJECT: la acción no cumple la regla de autonomía constitucional ("
                    + "; ".join(autonomy_violations)
                    + ")."
                ),
                outcome=PolicyOutcome.REJECT,
                effective_risk=assessment.effective,
                action=request.action,
                reasons=tuple(reasons),
                protected_files=protected,
            )
            self._record(decision)
            return decision

        if rule.requires_review:
            reasons.append("nivel 1 exige revisión posterior obligatoria")
            decision = PolicyDecision(
                allowed=True,
                authority_level=rule.level,
                requires_review=True,
                requires_human=False,
                reason=(
                    "ALLOW_WITH_REVIEW: acción autorizada de forma autónoma "
                    "con revisión posterior obligatoria."
                ),
                outcome=PolicyOutcome.ALLOW_WITH_REVIEW,
                effective_risk=assessment.effective,
                action=request.action,
                reasons=tuple(reasons),
                protected_files=protected,
            )
            self._record(decision)
            return decision

        reasons.append("acción autorizada de forma autónoma dentro de permisos y presupuesto")
        if preauthorized is not None:
            reasons.append(
                f"riesgo {assessment.effective.name} sin frontera de autoridad: "
                + "; ".join(preauthorized.reasons)
            )
        decision = PolicyDecision(
            allowed=True,
            authority_level=rule.level,
            requires_review=False,
            requires_human=False,
            reason="ALLOW: acción técnica reversible dentro de permisos, presupuesto y riesgo.",
            outcome=PolicyOutcome.ALLOW,
            effective_risk=assessment.effective,
            action=request.action,
            reasons=tuple(reasons),
            protected_files=protected,
        )
        self._record(decision)
        return decision

    # ------------------------------------------------------------------ helpers
    def _record(self, decision: PolicyDecision) -> None:
        """Registra la decisión en el historial y en el índice por identificador."""
        self._decisions.append(decision)
        self._decisions_by_id[decision.id] = decision

    def _effective_breaches(
        self,
        request: ActionRequest,
        level: AuthorityLevel,
        context: PolicyEvaluationContext,
    ) -> tuple[BudgetBreach, ...]:
        """Comprueba presupuesto aplicando además los límites de la tarea."""
        effective = request
        overrides: dict[str, float] = {}
        if context.task_max_cost_usd is not None:
            overrides["max_cost_usd"] = min(request.max_cost_usd, context.task_max_cost_usd)
        if context.task_max_execution_minutes is not None:
            overrides["max_execution_minutes"] = min(
                request.max_execution_minutes, context.task_max_execution_minutes
            )
        if context.task_max_files_changed is not None:
            overrides["max_files_changed"] = min(
                request.max_files_changed, context.task_max_files_changed
            )
        if overrides:
            effective = request.model_copy(update=overrides)
        return self._budgets.evaluate(effective, level)

    @staticmethod
    def _autonomy_violations(
        request: ActionRequest,
        level: AuthorityLevel,
        assessment: RiskAssessment,
        *,
        preauthorized: bool = False,
    ) -> tuple[str, ...]:
        """Devuelve las condiciones de autonomía incumplidas, en orden fijo.

        Con autonomía preautorizada, ``HIGH`` deja de ser por sí mismo una violación: lo es cruzar
        una frontera (producción, legal, negocio, irreversible, nivel 3), que se sigue comprobando.
        """
        violations: list[str] = []
        if not request.technical:
            violations.append("la acción no es técnica")
        if not request.reversible and not preauthorized:
            violations.append("la acción no es reversible")
        if assessment.effective > RiskLevel.MEDIUM and not preauthorized:
            violations.append(f"riesgo {assessment.effective.name} superior a MEDIUM")
        if request.production_impact:
            violations.append("impacto en producción")
        if request.legal_impact:
            violations.append("impacto legal")
        if request.business_impact:
            violations.append("impacto de negocio")
        if level >= AuthorityLevel.LEVEL_3_HUMAN:
            violations.append("nivel de autoridad 3 reservado a decisión humana")
        return tuple(violations)

    def _preauthorize(
        self,
        request: ActionRequest,
        rule: AuthorityRule,
        assessment: RiskAssessment,
        context: PolicyEvaluationContext,
        reasons: list[str],
    ) -> AutonomyVerdict | None:
        """Veredicto de autonomía si un riesgo HIGH está preautorizado; ``None`` si no aplica.

        Solo actúa sobre ``HIGH`` (``CRITICAL`` sigue siendo Human Gate), solo con contexto de
        confianza del destino y solo si la acción no es de nivel 3. Cuando no está preautorizado,
        las **fronteras concretas** quedan en las razones de la decisión: el Human Gate nunca dice
        solo «riesgo HIGH».
        """
        if (
            not assessment.requires_human_gate
            or assessment.effective is not RiskLevel.HIGH
            or rule.level.requires_human
            or context.autonomy is None
            or not self._autonomy.enabled
        ):
            return None
        from punto.policy.envelope import AdaptiveAuthorityEnvelope

        classifier = AdaptiveAuthorityEnvelope(constitutional_paths=self.protected_paths)
        verdict = self._autonomy.evaluate(
            AutonomyQuery(
                operation=request.action,
                resource_classes=tuple(
                    item.value for item in classifier.classes_of(request.files_changed)
                ),
                technical=request.technical,
                reversible=request.reversible,
                destructive=request.action.startswith(("delete_", "remove_")),
                production_impact=request.production_impact,
                legal_impact=request.legal_impact,
                business_impact=request.business_impact,
                cost_usd=request.estimated_cost,
                minutes=request.estimated_minutes,
                files=request.files_changed_count,
            ),
            context.autonomy,
        )
        if verdict.preauthorized:
            return verdict
        reasons.append(
            "riesgo HIGH con frontera de autoridad: "
            + "; ".join(item.value for item in verdict.boundaries)
        )
        return None

    def _is_self_elevation(
        self,
        rule: AuthorityRule,
        files_changed: list[str] | tuple[str, ...],
    ) -> bool:
        """True si la acción intenta modificar las reglas de autoridad del motor.

        Un objetivo es "regla de autoridad" si figura a la vez en
        ``self_elevation.targets`` (presupuestos, reglas de riesgo, permisos y
        constitución) y entre las rutas efectivamente declaradas en la petición.
        Se normalizan las rutas para que ``.\\config\\budgets.yaml`` no evada la
        comprobación.
        """
        raw = self._permissions.get("self_elevation", {})
        if not isinstance(raw, dict) or not raw.get("forbidden", False):
            return False

        raw_actions = raw.get("actions", [])
        raw_targets = raw.get("targets", [])
        if not isinstance(raw_actions, list) or not isinstance(raw_targets, list):
            return False

        guarded_actions = {str(item).strip().lower() for item in raw_actions}
        guarded_targets = {normalize_path(str(item)) for item in raw_targets}
        touched = {normalize_path(path) for path in files_changed if path}
        return rule.action in guarded_actions and bool(guarded_targets.intersection(touched))

    def is_write_operation(self, action: str) -> bool:
        """True si la acción puede escribir en el sistema de archivos."""
        normalized = action.strip().lower()
        if normalized.startswith(WRITE_OPERATION_PREFIXES):
            return True
        return normalized.endswith(WRITE_OPERATION_SUFFIXES)


def _catalog_from_permissions(permissions: dict[str, Any]) -> AuthorityCatalog:
    """Construye el catálogo de autoridad desde ``config/permissions.yaml``."""
    raw_actions = permissions.get("actions", {})
    if not isinstance(raw_actions, dict):
        msg = "config/permissions.yaml: 'actions' debe ser un mapeo"
        raise ValueError(msg)

    entries: list[AuthorityRule] = []
    for raw_name, raw_entry in raw_actions.items():
        name = str(raw_name).strip().lower()
        if not isinstance(raw_entry, dict):
            msg = f"config/permissions.yaml: la acción '{name}' debe ser un mapeo"
            raise ValueError(msg)
        raw_level = raw_entry.get("level")
        if raw_level is None:
            msg = f"config/permissions.yaml: la acción '{name}' no declara 'level'"
            raise ValueError(msg)
        entries.append(
            AuthorityRule(
                action=name,
                level=_coerce_authority_level(raw_level),
                description=str(raw_entry.get("description", "")),
                category=str(raw_entry.get("category", "")),
            )
        )

    return AuthorityCatalog.from_entries(
        entries,
        never_autonomous=_string_items(permissions.get("never_autonomous")),
        review_required_levels=_levels_from_items(permissions.get("review_required_levels")),
        human_required_levels=_levels_from_items(permissions.get("human_required_levels")),
    )


def _coerce_authority_level(raw_level: object) -> AuthorityLevel:
    """Convierte un valor YAML en :class:`AuthorityLevel`."""
    if isinstance(raw_level, str):
        stripped = raw_level.strip().upper()
        if stripped.startswith("LEVEL_"):
            try:
                return AuthorityLevel(int(stripped.split("_")[1]))
            except (IndexError, ValueError):
                pass
        try:
            return AuthorityLevel(int(stripped))
        except ValueError:
            pass
    if isinstance(raw_level, int):
        return AuthorityLevel(raw_level)
    msg = f"nivel de autoridad inválido en config/permissions.yaml: {raw_level!r}"
    raise ValueError(msg)


def _string_items(raw: object) -> tuple[str, ...]:
    """Extrae una tupla de cadenas normalizadas de un valor YAML."""
    if not isinstance(raw, list):
        return ()
    return tuple(str(item).strip().lower() for item in raw)


def _levels_from_items(raw: object) -> tuple[AuthorityLevel, ...]:
    """Extrae una tupla de niveles de autoridad de un valor YAML."""
    if not isinstance(raw, list):
        return ()
    return tuple(_coerce_authority_level(item) for item in raw)


__all__ = [
    "PROTECTED_REJECTION_OUTCOME",
    "PolicyConfigBundle",
    "PolicyEngine",
    "PolicyEvaluationContext",
]
