"""Sobre de autoridad adaptativo (PILOT-05).

Este módulo **no** es un segundo PolicyEngine. El :class:`~punto.policy.policy_engine.PolicyEngine`
sigue siendo la autoridad constitucional: catálogo de acciones, niveles, presupuestos y protección
de los recursos que definen las reglas del propio motor. Lo que añade este sobre es la pieza que
faltaba para cumplir el principio constitucional nº1
(``autonomy_without_unnecessary_interruption``): decidir
si una operación concreta puede ejecutarse autónomamente **por su riesgo efectivo**, y no porque un
contador de archivos cruzara un número.

Cómo se compone la autoridad, de más restrictivo a más permisivo:

1. **PolicyEngine** (constitución): si deniega, deniega. El sobre nunca relaja ese veredicto.
2. **Sobre adaptativo** (este módulo): evalúa la operación con atributos auditables y devuelve
   ``ALLOW`` / ``ALLOW_WITH_REVIEW`` / ``REQUIRE_HUMAN`` / ``REJECT`` **con las reglas nombradas que
   se dispararon**. Cada regla que se dispara queda en la decisión con su motivo; no hay ninguna
   puntuación opaca.

El número de archivos deja de ser la frontera de autoridad y pasa a ser lo que debe ser:

- una **señal de blast radius** que eleva el riesgo (como ya hacía ``config/risk-rules.yaml``);
- un **presupuesto anti-runaway** con techo duro (``max_files``), para que ninguna tarea se
  convierta en un barrido del repositorio.

La distinción que sostiene todo lo demás:

- **SCOPE EXPANSION**: añadir recursos de la *misma* clase de riesgo, con evidencia causal, sigue
  siendo autónomo. Es trabajar más en el mismo problema.
- **AUTHORITY ESCALATION**: cruzar hacia una clase protegida (producción, identidad/auth, secretos,
  datos destructivos, coste externo, publicación, recursos constitucionales) exige autorización
  humana, aunque sea **un solo archivo**.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any, Final

from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.policy import PolicyOutcome

__all__ = [
    "AUTONOMOUS_MAX_FILES",
    "CONSTITUTIONAL_CODE_PATHS",
    "DEFAULT_FILE_THRESHOLDS",
    "AdaptiveAuthorityEnvelope",
    "AuthorityClass",
    "Confidence",
    "DataSensitivity",
    "EnvelopeDecision",
    "EnvelopeOperation",
    "Environment",
    "ExpansionDecision",
    "ExternalEffect",
    "FiredRule",
    "OperationRisk",
    "Provenance",
    "ResourceClass",
    "RiskDelta",
    "VerificationStrength",
]

#: Techo duro de archivos por operación agregada (presupuesto anti-runaway).
#:
#: No es la frontera de autoridad: es el punto a partir del cual **cualquier** tarea local, por
#: reversible que sea, deja de ser autónoma porque ya no se parece a un cambio sino a un barrido.
#: Coincide con el umbral ``medium.max_files_changed`` de ``config/risk-rules.yaml``, de modo que el
#: presupuesto y el modelo de riesgo dicen lo mismo en vez de contradecirse.
AUTONOMOUS_MAX_FILES: Final[int] = 20

#: Umbrales de archivos por nivel de riesgo (espejo de ``config/risk-rules.yaml``).
DEFAULT_FILE_THRESHOLDS: Final[Mapping[RiskLevel, int]] = {
    RiskLevel.LOW: 5,
    RiskLevel.MEDIUM: AUTONOMOUS_MAX_FILES,
    RiskLevel.HIGH: 100,
}

#: Código que **implementa** la autoridad del motor. Modificarlo cambia las reglas con las que PUNTO
#: decide cuánto puede modificar: es autoelevación, no ingeniería ordinaria. La lista de
#: constitucional no se repite aquí: se toma de ``config/constitution.yaml``
#: (``protected_files`` + ``additional_protected_paths``) y de ``config/permissions.yaml``
#: (``self_elevation.targets``), que ya la declaran.
CONSTITUTIONAL_CODE_PATHS: Final[tuple[str, ...]] = (
    "src/punto/policy/",
    "src/punto/security/",
    "src/punto/tools/security/",
)


class EnvelopeOperation(StrEnum):
    """Operación concreta que se evalúa (vocabulario del sobre, no del catálogo)."""

    READ = "read"
    WRITE = "write"
    CREATE = "create"
    RENAME = "rename"
    MOVE = "move"
    DELETE = "delete"
    EXECUTE = "execute"
    LOCAL_DATABASE = "local_database"
    MIGRATION_GENERATE = "migration_generate"
    MIGRATION_APPLY_LOCAL = "migration_apply_local"
    COMMIT = "commit"
    BRANCH = "branch"
    CHECKPOINT = "checkpoint"
    ROLLBACK = "rollback"
    REPAIR = "repair"
    PLAN_APPLY = "plan_apply"
    CONTEXT_EXPANSION = "context_expansion"
    SCOPE_EXPANSION = "scope_expansion"
    PUBLISH = "publish"
    DEPLOY = "deploy"
    PRODUCTION_DATABASE = "production_database"
    PAYMENT = "payment"
    SECRET_ROTATION = "secret_rotation"
    AUTHORITY_CHANGE = "authority_change"
    FORCE_PUSH = "force_push"
    HISTORY_REWRITE = "history_rewrite"


class ResourceClass(StrEnum):
    """Clase de recurso tocado, deducida de la ruta de forma determinista."""

    APPLICATION_CODE = "application_code"
    TEST_CODE = "test_code"
    DOCUMENTATION = "documentation"
    PROJECT_CONFIGURATION = "project_configuration"
    DEPENDENCY_MANIFEST = "dependency_manifest"
    DATA_SEED = "data_seed"
    LOCAL_DATABASE = "local_database"
    BUILD_ARTIFACT = "build_artifact"
    SECRET_STORE = "secret_store"
    CONSTITUTIONAL_CONFIG = "constitutional_config"
    AUTHORITY_CODE = "authority_code"
    SECURITY_CONTROL = "security_control"
    IDENTITY_AUTH = "identity_auth"
    PAYMENT_CODE = "payment_code"
    PRODUCTION_DATA = "production_data"
    INFRASTRUCTURE = "infrastructure"
    UNKNOWN = "unknown"


class Environment(StrEnum):
    """Entorno donde ocurre la operación."""

    LOCAL = "local"
    CI = "ci"
    STAGING = "staging"
    PRODUCTION = "production"


class ExternalEffect(StrEnum):
    """Efecto hacia fuera del proyecto."""

    NONE = "none"
    NETWORK = "network"
    PUBLISH = "publish"
    COST = "cost"
    DATA_TRANSFER = "data_transfer"


class DataSensitivity(StrEnum):
    """Sensibilidad de los datos tocados."""

    NONE = "none"
    INTERNAL = "internal"
    PERSONAL = "personal"
    SECRET = "secret"


class VerificationStrength(IntEnum):
    """Fuerza de la verificación disponible para esta operación."""

    NONE = 0
    WEAK = 1
    MODERATE = 2
    STRONG = 3


class Provenance(StrEnum):
    """De dónde sale la operación. La procedencia decide si hace falta evidencia."""

    EVIDENCE = "evidence"
    PUNTO_POLICY = "punto_policy"
    OPERATOR_CONFIG = "operator_config"
    PROVIDER_PROPOSAL = "provider_proposal"
    PELL_MEMORY = "pell_memory"


class Confidence(IntEnum):
    """Confianza declarada en la operación (nunca sustituye a la evidencia)."""

    LOW = 0
    MEDIUM = 1
    HIGH = 2


class AuthorityClass(StrEnum):
    """Clase de autoridad resultante, en el vocabulario que pide el encargo.

    Es una **vista** de la decisión: el nivel de autoridad sigue siendo
    :class:`~punto.schemas.enums.AuthorityLevel` y el resultado
    :class:`~punto.schemas.enums.PolicyOutcome`. Se mantiene esta etiqueta porque es la que el
    operador usa para leer el resultado de un vistazo.
    """

    AUTONOMOUS_LOCAL = "AUTONOMOUS_LOCAL"
    AUTONOMOUS_VERIFIED = "AUTONOMOUS_VERIFIED"
    HUMAN_GATE_REQUIRED = "HUMAN_GATE_REQUIRED"
    PROHIBITED = "PROHIBITED"


_VERDICT_RANK: Final[Mapping[PolicyOutcome, int]] = {
    PolicyOutcome.ALLOW: 0,
    PolicyOutcome.ALLOW_WITH_REVIEW: 1,
    PolicyOutcome.REQUIRE_HUMAN: 2,
    PolicyOutcome.REJECT: 3,
}

_WRITING: Final[frozenset[EnvelopeOperation]] = frozenset(
    {
        EnvelopeOperation.WRITE,
        EnvelopeOperation.CREATE,
        EnvelopeOperation.RENAME,
        EnvelopeOperation.MOVE,
        EnvelopeOperation.DELETE,
        EnvelopeOperation.REPAIR,
        EnvelopeOperation.PLAN_APPLY,
        EnvelopeOperation.SCOPE_EXPANSION,
        EnvelopeOperation.MIGRATION_APPLY_LOCAL,
        EnvelopeOperation.MIGRATION_GENERATE,
        EnvelopeOperation.SECRET_ROTATION,
        EnvelopeOperation.AUTHORITY_CHANGE,
        EnvelopeOperation.PRODUCTION_DATABASE,
        EnvelopeOperation.DEPLOY,
        EnvelopeOperation.PUBLISH,
        EnvelopeOperation.PAYMENT,
        EnvelopeOperation.FORCE_PUSH,
        EnvelopeOperation.HISTORY_REWRITE,
    }
)

#: Operaciones que nunca son autónomas, sea cual sea el resto de atributos.
_NEVER_AUTONOMOUS: Final[Mapping[EnvelopeOperation, str]] = {
    EnvelopeOperation.PUBLISH: "publicar no es una operación del ciclo local",
    EnvelopeOperation.DEPLOY: "el despliegue es producción",
    EnvelopeOperation.PRODUCTION_DATABASE: "mutar datos de producción es irreversible",
    EnvelopeOperation.PAYMENT: "una acción de pago real no es ingeniería",
    EnvelopeOperation.SECRET_ROTATION: "rotar credenciales reales es una decisión humana",
    EnvelopeOperation.AUTHORITY_CHANGE: "cambiar la autoridad es autoelevación",
    EnvelopeOperation.FORCE_PUSH: "reescribir historial remoto es irreversible",
    EnvelopeOperation.HISTORY_REWRITE: "reescribir historial es irreversible",
}


@dataclass(frozen=True, slots=True)
class OperationRisk:
    """Perfil de riesgo de **una** operación, con atributos auditables.

    No hay campos derivados de texto de proveedor: cada atributo lo fija PUNTO a partir del plan
    validado, de la configuración del destino o del resultado real de una verificación.
    """

    operation: EnvelopeOperation
    resources: tuple[str, ...] = ()
    environment: Environment = Environment.LOCAL
    reversible: bool = True
    verification_strength: VerificationStrength = VerificationStrength.MODERATE
    data_sensitivity: DataSensitivity = DataSensitivity.NONE
    external_effect: ExternalEffect = ExternalEffect.NONE
    production_impact: bool = False
    security_impact: bool = False
    business_impact: bool = False
    cost_impact: bool = False
    identity_auth_impact: bool = False
    destructive: bool = False
    #: ``True`` si el recurso lo creó este mismo ciclo: borrarlo es revertir, no destruir.
    created_by_cycle: bool = False
    provenance: Provenance = Provenance.EVIDENCE
    confidence: Confidence = Confidence.HIGH
    #: Evidencia observable que sostiene la operación (vacía ⇒ una propuesta del proveedor sin
    #: respaldo, que no concede nada).
    evidence: tuple[str, ...] = ()
    description: str = ""

    @property
    def blast_radius(self) -> int:
        """Número de recursos distintos que toca la operación (señal, no frontera)."""
        return len({item for item in self.resources if item})

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable para auditoría y contrato de resultado."""
        return {
            "operation": self.operation.value,
            "resources": list(self.resources),
            "blast_radius": self.blast_radius,
            "environment": self.environment.value,
            "reversible": self.reversible,
            "verification_strength": self.verification_strength.name,
            "data_sensitivity": self.data_sensitivity.value,
            "external_effect": self.external_effect.value,
            "production_impact": self.production_impact,
            "security_impact": self.security_impact,
            "business_impact": self.business_impact,
            "cost_impact": self.cost_impact,
            "identity_auth_impact": self.identity_auth_impact,
            "destructive": self.destructive,
            "created_by_cycle": self.created_by_cycle,
            "provenance": self.provenance.value,
            "confidence": self.confidence.name,
            "evidence": list(self.evidence),
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class FiredRule:
    """Regla que se disparó, con su veredicto y su motivo legible."""

    name: str
    verdict: PolicyOutcome
    reason: str

    def as_dict(self) -> dict[str, str]:
        """Vista serializable."""
        return {"rule": self.name, "verdict": self.verdict.value, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class EnvelopeDecision:
    """Decisión del sobre: veredicto, riesgo, evidencia exigida y reglas que la sostienen."""

    outcome: PolicyOutcome
    risk: RiskLevel
    authority_level: AuthorityLevel
    authority_class: AuthorityClass
    fired: tuple[FiredRule, ...] = ()
    required_evidence: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    profile: OperationRisk | None = None
    resource_classes: tuple[ResourceClass, ...] = ()

    @property
    def autonomous(self) -> bool:
        """True si PUNTO puede ejecutarla sin autorización humana."""
        return self.outcome in (PolicyOutcome.ALLOW, PolicyOutcome.ALLOW_WITH_REVIEW)

    @property
    def requires_human(self) -> bool:
        """True si exige autorización humana explícita."""
        return self.outcome is PolicyOutcome.REQUIRE_HUMAN

    @property
    def prohibited(self) -> bool:
        """True si está prohibida para la autonomía del ciclo (falla cerrado)."""
        return self.outcome is PolicyOutcome.REJECT

    @property
    def rule_names(self) -> tuple[str, ...]:
        """Nombres de las reglas disparadas, en orden."""
        return tuple(rule.name for rule in self.fired)

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable, sin contenido de ficheros."""
        return {
            "outcome": self.outcome.value,
            "authority_class": self.authority_class.value,
            "authority_level": self.authority_level.name,
            "risk": self.risk.name,
            "rules": [rule.as_dict() for rule in self.fired],
            "required_evidence": list(self.required_evidence),
            "reasons": list(self.reasons),
            "resource_classes": [item.value for item in self.resource_classes],
            "profile": self.profile.as_dict() if self.profile is not None else None,
        }


@dataclass(frozen=True, slots=True)
class RiskDelta:
    """Diferencia de riesgo entre dos sobres, con lo que cambió."""

    previous_risk: RiskLevel
    new_risk: RiskLevel
    added_resources: tuple[str, ...] = ()
    added_classes: tuple[ResourceClass, ...] = ()
    escalations: tuple[str, ...] = ()
    crossed_boundary: tuple[str, ...] = ()

    @property
    def same_class(self) -> bool:
        """True si el riesgo no subió ni se cruzó ninguna frontera protegida."""
        return self.new_risk <= self.previous_risk and not self.crossed_boundary

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable."""
        return {
            "previous_risk": self.previous_risk.name,
            "new_risk": self.new_risk.name,
            "added_resources": list(self.added_resources),
            "added_classes": [item.value for item in self.added_classes],
            "escalations": list(self.escalations),
            "crossed_boundary": list(self.crossed_boundary),
            "same_class": self.same_class,
        }


@dataclass(frozen=True, slots=True)
class ExpansionDecision:
    """Resultado de evaluar una expansión de alcance (scope expansion)."""

    outcome: PolicyOutcome
    delta: RiskDelta
    decision: EnvelopeDecision
    reasons: tuple[str, ...] = ()
    record: Mapping[str, Any] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        """True si la expansión puede aplicarse de forma autónoma."""
        return self.outcome in (PolicyOutcome.ALLOW, PolicyOutcome.ALLOW_WITH_REVIEW)

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable."""
        return {
            "outcome": self.outcome.value,
            "approved": self.approved,
            "delta": self.delta.as_dict(),
            "decision": self.decision.as_dict(),
            "reasons": list(self.reasons),
            "record": dict(self.record),
        }


# ---------------------------------------------------------------------------
# Clasificación determinista de recursos
# ---------------------------------------------------------------------------
_SECRET_MARKERS: Final[tuple[str, ...]] = (
    ".env",
    ".npmrc",
    ".netrc",
    "credentials",
    "id_rsa",
    "id_ed25519",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
)
_AUTH_MARKERS: Final[tuple[str, ...]] = (
    "auth",
    "session",
    "login",
    "oauth",
    "permission",
    "authorization",
    "identity",
    "rbac",
)
_PAYMENT_MARKERS: Final[tuple[str, ...]] = ("payment", "billing", "checkout", "stripe", "invoice")
_INFRA_MARKERS: Final[tuple[str, ...]] = (
    "dockerfile",
    "containerfile",
    "terraform",
    "k8s",
    "kubernetes",
    "helm",
    ".github/workflows",
)
_MANIFESTS: Final[tuple[str, ...]] = (
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "requirements.txt",
    "pyproject.toml",
    "go.mod",
    "cargo.toml",
)
_DOC_SUFFIXES: Final[tuple[str, ...]] = (".md", ".rst", ".txt", ".adoc")


class AdaptiveAuthorityEnvelope:
    """Evalúa operaciones concretas dentro de la autoridad constitucional.

    Args:
        constitutional_paths: rutas cuya modificación cambia las reglas del motor. Se toma de la
            constitución (``protected_files`` + ``additional_protected_paths`` +
            ``self_elevation``).
        max_files: techo duro de archivos por operación agregada (presupuesto anti-runaway).
        file_thresholds: umbrales de archivos por nivel de riesgo (espejo de ``risk-rules.yaml``).
        autonomous_max_risk: riesgo máximo que sigue siendo autónomo.
    """

    def __init__(
        self,
        *,
        constitutional_paths: Iterable[str] = (),
        max_files: int = AUTONOMOUS_MAX_FILES,
        file_thresholds: Mapping[RiskLevel, int] | None = None,
        autonomous_max_risk: RiskLevel = RiskLevel.MEDIUM,
        session_ceiling: int | None = None,
    ) -> None:
        self._constitutional = frozenset(
            self._normalize(item) for item in (*CONSTITUTIONAL_CODE_PATHS, *constitutional_paths)
        )
        self._max_files = max(1, max_files)
        self._thresholds = dict(file_thresholds or DEFAULT_FILE_THRESHOLDS)
        self._autonomous_max_risk = autonomous_max_risk
        #: Techo **acumulado** de la sesión. Sin él, una escalada grande se podría fragmentar en
        #: revisiones de plan pequeñas que, cada una por separado, parecen razonables: es el aviso
        #: que dio el ARCHITECT y la razón de que la fragmentación se detecte aquí, no en el plan.
        self._session_ceiling = max(self._max_files, session_ceiling or self._max_files * 3)

    @property
    def session_ceiling(self) -> int:
        """Techo acumulado de recursos distintos para toda la sesión del ciclo."""
        return self._session_ceiling

    def fragmentation(
        self, cumulative_resources: Sequence[str], added_resources: Sequence[str]
    ) -> FiredRule | None:
        """Detecta que varias revisiones juntas cruzan el techo acumulado.

        Returns:
            La regla disparada si la acumulación cruza el techo (o si una sola revisión lo cruza),
            o ``None`` si la sesión sigue dentro de presupuesto.
        """
        cumulative = {item for item in cumulative_resources if item}
        added = {item for item in added_resources if item}
        total = len(cumulative | added)
        if total <= self._session_ceiling:
            return None
        return FiredRule(
            name="session-fragmentation",
            verdict=PolicyOutcome.REQUIRE_HUMAN,
            reason=(
                f"la sesión acumula {total} recursos distintos y excede el techo de sesión de "
                f"{self._session_ceiling}: fragmentar una escalada grande en revisiones pequeñas "
                "no la convierte en autónoma"
            ),
        )

    # ------------------------------------------------------------------ consulta
    @property
    def constitutional_paths(self) -> tuple[str, ...]:
        """Rutas constitucionales declaradas, ordenadas."""
        return tuple(sorted(self._constitutional))

    @property
    def max_files(self) -> int:
        """Techo duro de archivos por operación agregada."""
        return self._max_files

    def is_constitutional(self, path: str) -> bool:
        """True si la ruta define las reglas con las que el motor decide su propia autoridad."""
        relative = self._normalize(path)
        if not relative:
            return False
        return any(
            relative == item or relative.startswith(item)
            for item in self._constitutional
        )

    def classify(self, path: str) -> ResourceClass:
        """Clase de recurso de una ruta, por reglas deterministas y en orden fijo."""
        relative = self._normalize(path)
        if not relative:
            return ResourceClass.UNKNOWN
        if self.is_constitutional(relative):
            return (
                ResourceClass.CONSTITUTIONAL_CONFIG
                if relative.endswith((".yaml", ".yml", ".json", ".toml"))
                else ResourceClass.AUTHORITY_CODE
            )
        name = relative.rsplit("/", 1)[-1]
        lowered = relative.lower()
        if any(marker in lowered for marker in _SECRET_MARKERS):
            return ResourceClass.SECRET_STORE
        if any(marker in lowered for marker in _INFRA_MARKERS):
            return ResourceClass.INFRASTRUCTURE
        if any(marker in name for marker in _MANIFESTS) or name in _MANIFESTS:
            return ResourceClass.DEPENDENCY_MANIFEST
        if any(marker in lowered for marker in _PAYMENT_MARKERS):
            return ResourceClass.PAYMENT_CODE
        if any(marker in lowered for marker in _AUTH_MARKERS):
            return ResourceClass.IDENTITY_AUTH
        if name.startswith("seed") or lowered.endswith(".sql"):
            return ResourceClass.DATA_SEED
        if (
            "/test" in lowered
            or lowered.startswith("test")
            or name.endswith((".test.ts", ".test.mjs"))
        ):
            return ResourceClass.TEST_CODE
        if name.endswith(_DOC_SUFFIXES):
            return ResourceClass.DOCUMENTATION
        if lowered.startswith(("config/", ".config/", "settings/")):
            return ResourceClass.PROJECT_CONFIGURATION
        if lowered.endswith((".json", ".yaml", ".yml", ".toml", ".ini", ".cfg")):
            return ResourceClass.PROJECT_CONFIGURATION
        if lowered.startswith((".next/", "dist/", "build/", "node_modules/", "__pycache__/")):
            return ResourceClass.BUILD_ARTIFACT
        if lowered.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rs")):
            return ResourceClass.APPLICATION_CODE
        return ResourceClass.UNKNOWN

    def classes_of(self, resources: Sequence[str]) -> tuple[ResourceClass, ...]:
        """Clases de un conjunto de recursos, sin repetir y en orden estable."""
        seen: list[ResourceClass] = []
        for item in resources:
            candidate = self.classify(item)
            if candidate not in seen:
                seen.append(candidate)
        return tuple(seen)

    # ---------------------------------------------------------------- evaluación
    def assess(self, profile: OperationRisk) -> EnvelopeDecision:
        """Evalúa el perfil de una operación y devuelve la decisión con sus reglas.

        El veredicto es el **más restrictivo** de todas las reglas disparadas, y el riesgo es el
        máximo de las escaladas aplicadas. Ambas cosas quedan explicadas en la propia decisión.
        """
        fired: list[FiredRule] = []
        classes = self.classes_of(profile.resources)

        fired.extend(self._protected_resource_rules(profile, classes))
        fired.extend(self._operation_rules(profile))
        fired.extend(self._environment_rules(profile))
        fired.extend(self._destructive_rules(profile, classes))
        fired.extend(self._effect_rules(profile))
        fired.extend(self._evidence_rules(profile))
        fired.extend(self._verification_rules(profile))
        fired.extend(self._blast_radius_rules(profile))
        fired.append(
            FiredRule(
                name="local-technical-reversible",
                verdict=PolicyOutcome.ALLOW,
                reason="operación técnica, local, reversible y dentro del proyecto autorizado",
            )
        )

        outcome = max((rule.verdict for rule in fired), key=lambda item: _VERDICT_RANK[item])
        risk = self._risk_level(profile)
        risk = max(risk, self._risk_from_verdict(outcome))
        required = self._required_evidence(outcome, profile, fired)
        return EnvelopeDecision(
            outcome=outcome,
            risk=risk,
            authority_level=self._authority_level(outcome, risk),
            authority_class=self._authority_class(outcome),
            fired=tuple(fired),
            required_evidence=required,
            reasons=tuple(rule.reason for rule in fired if rule.verdict is outcome),
            profile=profile,
            resource_classes=classes,
        )

    def expansion(
        self,
        previous: OperationRisk,
        requested: OperationRisk,
        *,
        evidence: Sequence[str],
        relationship: str,
        trigger: str = "",
        root_cause: str = "",
        cumulative_resources: Sequence[str] = (),
    ) -> ExpansionDecision:
        """Evalúa una expansión de alcance: causal ⇒ autónoma; cruzar frontera ⇒ humano.

        Una expansión sin evidencia o sin relación declarada con el objetivo original **no** es una
        expansión: es alcance nuevo sin causa, y se rechaza. Es la frontera que impide convertir la
        autonomía adaptativa en ``prompt-driven escalation``. La evaluación mira el alcance
        **acumulado** del plan revisado, no solo lo añadido, así que fragmentar una escalada en
        revisiones pequeñas no la vuelve autónoma.
        """
        before = self.assess(previous)
        after = self.assess(requested)
        added = tuple(
            item for item in requested.resources if item not in set(previous.resources)
        )
        added_classes = tuple(
            item for item in after.resource_classes if item not in before.resource_classes
        )
        crossed = self._crossed_boundaries(requested, after)
        fragmentation = self.fragmentation(cumulative_resources, requested.resources)
        delta = RiskDelta(
            previous_risk=before.risk,
            new_risk=after.risk,
            added_resources=added,
            added_classes=added_classes,
            escalations=tuple(
                rule.reason
                for rule in after.fired
                if rule.name in _ESCALATION_RULES and rule.verdict is not PolicyOutcome.ALLOW
            ),
            crossed_boundary=crossed,
        )
        record: dict[str, Any] = {
            "trigger": trigger or "evidencia de verificación",
            "evidence": list(evidence),
            "root_cause": root_cause,
            "new_resources": list(added),
            "operations": [requested.operation.value],
            "relationship_to_original_objective": relationship,
            "risk_before": before.risk.name,
            "risk_after": after.risk.name,
            "verification_required": list(after.required_evidence) or ["verificación del destino"],
            "cumulative_resources": sorted(
                {item for item in cumulative_resources if item} | set(requested.resources)
            ),
            "session_ceiling": self._session_ceiling,
        }

        if not evidence or not relationship.strip():
            return ExpansionDecision(
                outcome=PolicyOutcome.REJECT,
                delta=delta,
                decision=after,
                reasons=(
                    "expansión sin evidencia causal o sin relación declarada con el objetivo: "
                    "no se amplía el alcance por texto del proveedor",
                ),
                record={**record, "authority_decision": PolicyOutcome.REJECT.value},
            )
        if after.prohibited:
            return ExpansionDecision(
                outcome=PolicyOutcome.REJECT,
                delta=delta,
                decision=after,
                reasons=tuple(
                    rule.reason for rule in after.fired if rule.verdict is PolicyOutcome.REJECT
                ),
                record={**record, "authority_decision": PolicyOutcome.REJECT.value},
            )
        if after.requires_human or crossed:
            return ExpansionDecision(
                outcome=PolicyOutcome.REQUIRE_HUMAN,
                delta=delta,
                decision=after,
                reasons=tuple(crossed)
                or tuple(
                    rule.reason
                    for rule in after.fired
                    if rule.verdict is PolicyOutcome.REQUIRE_HUMAN
                ),
                record={**record, "authority_decision": PolicyOutcome.REQUIRE_HUMAN.value},
            )
        if fragmentation is not None:
            return ExpansionDecision(
                outcome=PolicyOutcome.REQUIRE_HUMAN,
                delta=delta,
                decision=after,
                reasons=(fragmentation.reason,),
                record={
                    **record,
                    "authority_decision": PolicyOutcome.REQUIRE_HUMAN.value,
                    "fragmentation_rule": fragmentation.name,
                },
            )
        if after.risk > before.risk and after.risk > self._autonomous_max_risk:
            return ExpansionDecision(
                outcome=PolicyOutcome.REQUIRE_HUMAN,
                delta=delta,
                decision=after,
                reasons=(
                    f"la expansión eleva el riesgo de {before.risk.name} a {after.risk.name}, "
                    f"por encima de {self._autonomous_max_risk.name}",
                ),
                record={**record, "authority_decision": PolicyOutcome.REQUIRE_HUMAN.value},
            )
        return ExpansionDecision(
            outcome=PolicyOutcome.ALLOW,
            delta=delta,
            decision=after,
            reasons=(
                "expansión causal dentro de la misma clase de riesgo: "
                f"{before.risk.name} → {after.risk.name}, reversible y verificable",
            ),
            record={**record, "authority_decision": PolicyOutcome.ALLOW.value},
        )

    # ------------------------------------------------------------------- reglas
    def _protected_resource_rules(
        self, profile: OperationRisk, classes: Sequence[ResourceClass]
    ) -> list[FiredRule]:
        rules: list[FiredRule] = []
        constitutional = [item for item in profile.resources if self.is_constitutional(item)]
        if constitutional:
            rules.append(
                FiredRule(
                    name="constitutional-resource",
                    verdict=PolicyOutcome.REJECT,
                    reason=(
                        "autoelevación de autoridad: "
                        + ", ".join(constitutional)
                        + " define las reglas con las que PUNTO decide su propia autoridad; "
                        "modificarlo exige una persona, no el ciclo"
                    ),
                )
            )
        if ResourceClass.SECRET_STORE in classes:
            rules.append(
                FiredRule(
                    name="secret-store",
                    verdict=PolicyOutcome.REJECT,
                    reason="un almacén de credenciales no cruza esta frontera",
                )
            )
        if ResourceClass.PAYMENT_CODE in classes:
            rules.append(
                FiredRule(
                    name="payment-surface",
                    verdict=PolicyOutcome.REQUIRE_HUMAN,
                    reason="tocar la superficie de pago es una decisión humana",
                )
            )
        if ResourceClass.IDENTITY_AUTH in classes or profile.identity_auth_impact:
            rules.append(
                FiredRule(
                    name="identity-auth",
                    verdict=PolicyOutcome.REQUIRE_HUMAN,
                    reason="cambiar identidad o autorización es escalada de autoridad",
                )
            )
        if ResourceClass.PRODUCTION_DATA in classes or profile.production_impact:
            rules.append(
                FiredRule(
                    name="production-surface",
                    verdict=PolicyOutcome.REQUIRE_HUMAN,
                    reason="producción no es territorio del ciclo local",
                )
            )
        if ResourceClass.UNKNOWN in classes:
            rules.append(
                FiredRule(
                    name="unknown-resource",
                    verdict=PolicyOutcome.REQUIRE_HUMAN,
                    reason="recurso de clase desconocida: se falla cerrado",
                )
            )
        return rules

    def _operation_rules(self, profile: OperationRisk) -> list[FiredRule]:
        never = _NEVER_AUTONOMOUS.get(profile.operation)
        if never is None:
            return []
        verdict = (
            PolicyOutcome.REJECT
            if profile.operation
            in {
                EnvelopeOperation.FORCE_PUSH,
                EnvelopeOperation.HISTORY_REWRITE,
                EnvelopeOperation.AUTHORITY_CHANGE,
            }
            else PolicyOutcome.REQUIRE_HUMAN
        )
        name = f"operation-{profile.operation.value}"
        return [FiredRule(name=name, verdict=verdict, reason=never)]

    def _environment_rules(self, profile: OperationRisk) -> list[FiredRule]:
        if profile.environment is Environment.PRODUCTION:
            return [
                FiredRule(
                    name="production-environment",
                    verdict=PolicyOutcome.REQUIRE_HUMAN,
                    reason="la operación ocurre en producción",
                )
            ]
        if profile.environment is Environment.STAGING:
            return [
                FiredRule(
                    name="staging-environment",
                    verdict=PolicyOutcome.ALLOW_WITH_REVIEW,
                    reason="entorno compartido: exige verificación antes de confirmar",
                )
            ]
        return []

    def _destructive_rules(
        self, profile: OperationRisk, classes: Sequence[ResourceClass]
    ) -> list[FiredRule]:
        rules: list[FiredRule] = []
        if profile.destructive and profile.created_by_cycle:
            rules.append(
                FiredRule(
                    name="revert-of-own-change",
                    verdict=PolicyOutcome.ALLOW,
                    reason="borrar un recurso que este mismo ciclo creó es revertir, no destruir",
                )
            )
            return rules
        if profile.destructive and profile.data_sensitivity in (
            DataSensitivity.PERSONAL,
            DataSensitivity.SECRET,
        ):
            rules.append(
                FiredRule(
                    name="destructive-sensitive-data",
                    verdict=PolicyOutcome.REQUIRE_HUMAN,
                    reason="borrado destructivo sobre datos personales o secretos",
                )
            )
        elif profile.destructive:
            rules.append(
                FiredRule(
                    name="destructive-local-data",
                    verdict=PolicyOutcome.REQUIRE_HUMAN,
                    reason="borrado destructivo de datos o configuración preexistentes",
                )
            )
        if ResourceClass.DATA_SEED in classes and profile.destructive:
            rules.append(
                FiredRule(
                    name="destructive-seed-data",
                    verdict=PolicyOutcome.REQUIRE_HUMAN,
                    reason="borrar datos semilla no es una operación reversible del ciclo",
                )
            )
        return rules

    def _effect_rules(self, profile: OperationRisk) -> list[FiredRule]:
        rules: list[FiredRule] = []
        if profile.external_effect in (ExternalEffect.PUBLISH, ExternalEffect.DATA_TRANSFER):
            rules.append(
                FiredRule(
                    name="external-publish",
                    verdict=PolicyOutcome.REQUIRE_HUMAN,
                    reason="publicar o transferir datos fuera del proyecto es un efecto externo",
                )
            )
        if profile.external_effect is ExternalEffect.COST or profile.cost_impact:
            rules.append(
                FiredRule(
                    name="external-cost",
                    verdict=PolicyOutcome.REQUIRE_HUMAN,
                    reason="crear coste es una decisión humana",
                )
            )
        if profile.business_impact:
            rules.append(
                FiredRule(
                    name="business-impact",
                    verdict=PolicyOutcome.REQUIRE_HUMAN,
                    reason="impacto de negocio declarado",
                )
            )
        if profile.security_impact:
            rules.append(
                FiredRule(
                    name="security-impact",
                    verdict=PolicyOutcome.REQUIRE_HUMAN,
                    reason="relajar un control de seguridad es una decisión humana",
                )
            )
        if not profile.reversible and not profile.created_by_cycle:
            rules.append(
                FiredRule(
                    name="irreversible",
                    verdict=PolicyOutcome.REQUIRE_HUMAN,
                    reason="la operación no es reversible",
                )
            )
        return rules

    def _evidence_rules(self, profile: OperationRisk) -> list[FiredRule]:
        rules: list[FiredRule] = []
        if profile.provenance is Provenance.PROVIDER_PROPOSAL and not profile.evidence:
            rules.append(
                FiredRule(
                    name="provider-claim-without-evidence",
                    verdict=PolicyOutcome.REJECT,
                    reason="el proveedor propone sin evidencia: su texto no concede autoridad",
                )
            )
        if profile.provenance is Provenance.PELL_MEMORY and profile.operation in _NEVER_AUTONOMOUS:
            rules.append(
                FiredRule(
                    name="pell-authority-claim",
                    verdict=PolicyOutcome.REJECT,
                    reason="una experiencia de PELL no puede conceder autoridad protegida",
                )
            )
        if profile.confidence is Confidence.LOW and profile.operation in _WRITING:
            rules.append(
                FiredRule(
                    name="low-confidence-write",
                    verdict=PolicyOutcome.ALLOW_WITH_REVIEW,
                    reason="confianza baja: exige verificación antes de confirmar",
                )
            )
        return rules

    def _verification_rules(self, profile: OperationRisk) -> list[FiredRule]:
        if profile.operation not in _WRITING:
            return []
        if profile.verification_strength is VerificationStrength.NONE:
            return [
                FiredRule(
                    name="no-verification",
                    verdict=PolicyOutcome.REQUIRE_HUMAN,
                    reason="escribir sin ninguna verificación disponible no es autónomo",
                )
            ]
        if profile.verification_strength is VerificationStrength.WEAK:
            return [
                FiredRule(
                    name="weak-verification",
                    verdict=PolicyOutcome.ALLOW_WITH_REVIEW,
                    reason=(
                        "verificación débil: se aplica solo con evidencia del entorno antes de "
                        "confirmar"
                    ),
                )
            ]
        return []

    def _blast_radius_rules(self, profile: OperationRisk) -> list[FiredRule]:
        if profile.blast_radius > self._max_files:
            return [
                FiredRule(
                    name="runaway-blast-radius",
                    verdict=PolicyOutcome.REQUIRE_HUMAN,
                    reason=(
                        f"{profile.blast_radius} recursos exceden el techo anti-runaway de "
                        f"{self._max_files}: un cambio así deja de ser una tarea y pasa a ser "
                        "un barrido"
                    ),
                )
            ]
        return []

    # ------------------------------------------------------------------ internos
    def _crossed_boundaries(
        self, profile: OperationRisk, decision: EnvelopeDecision
    ) -> tuple[str, ...]:
        boundaries: list[str] = []
        protected = {
            ResourceClass.CONSTITUTIONAL_CONFIG: "recursos constitucionales",
            ResourceClass.AUTHORITY_CODE: "código de autoridad",
            ResourceClass.SECURITY_CONTROL: "controles de seguridad",
            ResourceClass.SECRET_STORE: "almacén de secretos",
            ResourceClass.IDENTITY_AUTH: "identidad/autorización",
            ResourceClass.PAYMENT_CODE: "superficie de pago",
            ResourceClass.PRODUCTION_DATA: "datos de producción",
            ResourceClass.INFRASTRUCTURE: "infraestructura",
        }
        for item in decision.resource_classes:
            if item in protected:
                boundaries.append(f"cruce de frontera: {protected[item]}")
        if decision.prohibited:
            boundaries.append("la operación está prohibida para la autonomía del ciclo")
        if profile.environment is Environment.PRODUCTION:
            boundaries.append("cruce de frontera: entorno de producción")
        return tuple(boundaries)

    def _risk_level(self, profile: OperationRisk) -> RiskLevel:
        """Riesgo por atributos, con la razón de cada escalada disponible en las reglas."""
        risk = RiskLevel.LOW
        if profile.production_impact or profile.environment is Environment.PRODUCTION:
            risk = max(risk, RiskLevel.CRITICAL)
        if profile.identity_auth_impact or profile.security_impact:
            risk = max(risk, RiskLevel.HIGH)
        if profile.business_impact or profile.cost_impact:
            risk = max(risk, RiskLevel.HIGH)
        if profile.external_effect in (ExternalEffect.PUBLISH, ExternalEffect.DATA_TRANSFER):
            risk = max(risk, RiskLevel.HIGH)
        if profile.data_sensitivity is DataSensitivity.SECRET:
            risk = max(risk, RiskLevel.HIGH)
        if profile.destructive and not profile.created_by_cycle:
            risk = max(risk, RiskLevel.HIGH)
        if not profile.reversible and not profile.created_by_cycle:
            risk = max(risk, RiskLevel.HIGH)
        if profile.data_sensitivity is DataSensitivity.PERSONAL:
            risk = max(risk, RiskLevel.MEDIUM)
        if profile.blast_radius > self._thresholds.get(RiskLevel.MEDIUM, AUTONOMOUS_MAX_FILES):
            risk = max(risk, RiskLevel.HIGH)
        elif profile.blast_radius > self._thresholds.get(RiskLevel.LOW, 5):
            risk = max(risk, RiskLevel.MEDIUM)
        if (
            profile.verification_strength <= VerificationStrength.WEAK
            and profile.operation in _WRITING
        ):
            risk = max(risk, RiskLevel.MEDIUM)
        if profile.provenance is Provenance.PROVIDER_PROPOSAL and not profile.evidence:
            risk = max(risk, RiskLevel.HIGH)
        return risk

    @staticmethod
    def _risk_from_verdict(outcome: PolicyOutcome) -> RiskLevel:
        if outcome is PolicyOutcome.REJECT:
            return RiskLevel.CRITICAL
        if outcome is PolicyOutcome.REQUIRE_HUMAN:
            return RiskLevel.HIGH
        if outcome is PolicyOutcome.ALLOW_WITH_REVIEW:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    def _authority_level(self, outcome: PolicyOutcome, risk: RiskLevel) -> AuthorityLevel:
        if outcome is PolicyOutcome.REJECT or risk.requires_human_gate:
            return AuthorityLevel.LEVEL_3_HUMAN
        if outcome is PolicyOutcome.ALLOW_WITH_REVIEW:
            return AuthorityLevel.LEVEL_1_AUTONOMOUS_REVIEW
        return AuthorityLevel.LEVEL_0_AUTONOMOUS

    @staticmethod
    def _authority_class(outcome: PolicyOutcome) -> AuthorityClass:
        if outcome is PolicyOutcome.REJECT:
            return AuthorityClass.PROHIBITED
        if outcome is PolicyOutcome.REQUIRE_HUMAN:
            return AuthorityClass.HUMAN_GATE_REQUIRED
        if outcome is PolicyOutcome.ALLOW_WITH_REVIEW:
            return AuthorityClass.AUTONOMOUS_VERIFIED
        return AuthorityClass.AUTONOMOUS_LOCAL

    @staticmethod
    def _required_evidence(
        outcome: PolicyOutcome, profile: OperationRisk, fired: Sequence[FiredRule]
    ) -> tuple[str, ...]:
        if outcome is PolicyOutcome.REJECT:
            return ()
        if outcome is PolicyOutcome.REQUIRE_HUMAN:
            return ("autorización humana explícita de la operación concreta",)
        evidence: list[str] = []
        if profile.verification_strength < VerificationStrength.STRONG:
            evidence.append("verificación focalizada del cambio en el entorno")
        if outcome is PolicyOutcome.ALLOW_WITH_REVIEW:
            evidence.append("regresión relacionada antes de confirmar")
        if any(rule.name == "staging-environment" for rule in fired):
            evidence.append("evidencia del entorno compartido")
        return tuple(evidence)

    @staticmethod
    def _normalize(path: str) -> str:
        relative = str(path).strip().replace("\\", "/")
        while relative.startswith("./"):
            relative = relative[2:]
        return relative.lstrip("/")


#: Reglas cuyo disparo se considera una escalada al comparar dos sobres.
_ESCALATION_RULES: Final[frozenset[str]] = frozenset(
    {
        "constitutional-resource",
        "secret-store",
        "payment-surface",
        "identity-auth",
        "production-surface",
        "production-environment",
        "destructive-sensitive-data",
        "destructive-local-data",
        "external-publish",
        "external-cost",
        "business-impact",
        "security-impact",
        "irreversible",
        "no-verification",
        "runaway-blast-radius",
    }
)
