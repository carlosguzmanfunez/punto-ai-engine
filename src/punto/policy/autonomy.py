"""Autonomía preautorizada: qué NO necesita una persona, decidido por autoridad y no por etiqueta.

Principio (``autonomy_without_unnecessary_interruption``): una clasificación de riesgo ``HIGH`` por
sí sola **no** abre un Human Gate ni concede nada. El Human Gate se reserva para una **frontera de
autoridad** real; todo lo demás, dentro del alcance autorizado y reversible, es autónomo.

La decisión es una función de cinco cosas, ninguna de ellas una etiqueta de riesgo:

- **operación** (qué se hace);
- **recurso** (qué clase de recurso toca);
- **alcance** (destino registrado y rutas dentro del alcance concedido);
- **reversibilidad** (¿se puede deshacer? ¿Git puede restaurarlo?);
- **autoridad configurada** (``config/autonomy.yaml`` + el sobre del destino para push/deploy).

Este módulo es la **única** fuente de esa decisión: la consumen el Policy Engine (acciones del
catálogo) y el sobre adaptativo (operaciones del ciclo), de modo que dos capas no puedan discrepar.

Reglas de diseño que impiden la autoelevación:

- hay un **piso en código** (:data:`FLOOR_GATED_CLASSES`, :data:`FLOOR_NEVER_OPERATIONS`): el YAML
  solo puede **sumar** fronteras, nunca quitar las del piso ni conceder lo que el piso reserva;
- el contexto de la decisión (:class:`AutonomyContext`) lo construye **código de PUNTO** a partir
  del destino registrado; ni un proveedor ni una petición pueden declararlo;
- sin contexto no hay autonomía delegada: se falla cerrado y manda el comportamiento previo;
- ``config/autonomy.yaml`` es un fichero de autoridad protegido; PUNTO no puede modificarlo.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

__all__ = [
    "FLOOR_GATED_CLASSES",
    "FLOOR_NEVER_OPERATIONS",
    "AutonomyContext",
    "AutonomyPolicy",
    "AutonomyQuery",
    "AutonomyVerdict",
    "Boundary",
]


class Boundary(StrEnum):
    """Frontera de autoridad real: lo que **sí** exige a una persona."""

    SECRETS = "secrets"
    PAYMENT = "payment"
    UNBUDGETED_COST = "unbudgeted-cost"
    PRODUCTION_DATA = "production-data"
    DESTRUCTIVE_MIGRATION = "destructive-migration"
    IRREVERSIBLE = "irreversible"
    INFRASTRUCTURE = "infrastructure"
    UNAUTHORIZED_TARGET = "unauthorized-target"
    AUTHORITY_EXPANSION = "authority-expansion"
    HISTORY_REWRITE = "remote-history-rewrite"
    LEGAL_CONTRACTUAL = "legal-contractual"
    RELEASE_WITHOUT_ENVELOPE = "release-without-envelope"
    ENVELOPE_EXCEEDED = "envelope-exceeded"
    GATED_RESOURCE = "gated-resource"
    NOT_PREAUTHORIZED = "not-preauthorized"


#: Clases de recurso que son frontera **siempre** (el YAML solo puede sumar).
FLOOR_GATED_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "secret_store",
        "constitutional_config",
        "authority_code",
        "security_control",
        "payment_code",
        "production_data",
        "infrastructure",
    }
)

#: Operaciones que ningún YAML puede preautorizar: cada una es una frontera por sí misma.
FLOOR_NEVER_OPERATIONS: Final[Mapping[str, Boundary]] = {
    "payment": Boundary.PAYMENT,
    "financial_action": Boundary.PAYMENT,
    "secret_rotation": Boundary.SECRETS,
    "master_secret_change": Boundary.SECRETS,
    "production_database": Boundary.PRODUCTION_DATA,
    "production_database_delete": Boundary.PRODUCTION_DATA,
    "db_destructive_apply": Boundary.DESTRUCTIVE_MIGRATION,
    "db_mass_data_change": Boundary.DESTRUCTIVE_MIGRATION,
    "irreversible_delete": Boundary.IRREVERSIBLE,
    "authority_change": Boundary.AUTHORITY_EXPANSION,
    "force_push": Boundary.HISTORY_REWRITE,
    "history_rewrite": Boundary.HISTORY_REWRITE,
    "legal_change": Boundary.LEGAL_CONTRACTUAL,
    "business_model_change": Boundary.LEGAL_CONTRACTUAL,
    "external_resource_create": Boundary.INFRASTRUCTURE,
    "high_security_risk": Boundary.SECRETS,
}

#: Operaciones de release y la clase de sobre del destino que las concede (piso en código).
RELEASE_OPERATIONS: Final[Mapping[str, str]] = {
    "publish": "push",
    "deploy": "deploy",
    "deploy_production": "production_release",
    "production_release": "production_release",
}

_DEFAULT_MAX_FILES: Final[int] = 20
_DEFAULT_MAX_COST: Final[float] = 10.0
_DEFAULT_MAX_MINUTES: Final[float] = 60.0


@dataclass(frozen=True, slots=True)
class AutonomyContext:
    """Hechos de confianza sobre el destino, fijados por código de PUNTO (nunca por el proveedor).

    Sin contexto no hay autonomía delegada. Cada campo es un hecho comprobado por quien construye el
    contexto, no una declaración de la operación que se evalúa.
    """

    #: El destino está registrado en PUNTO (no es un repositorio cualquiera).
    target_registered: bool = False
    #: Todos los recursos de la operación caen dentro del alcance concedido al destino.
    in_scope: bool = False
    #: Git puede restaurar los recursos (están versionados en el baseline).
    git_recoverable: bool = False
    #: Clases de release que el destino declara explícitamente (``push``, ``deploy``, …).
    release_envelope: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class AutonomyQuery:
    """Lo que se pregunta a la política: operación, recursos y atributos, sin etiqueta de riesgo."""

    operation: str
    resource_classes: tuple[str, ...] = ()
    technical: bool = True
    reversible: bool = True
    destructive: bool = False
    created_by_cycle: bool = False
    production_impact: bool = False
    legal_impact: bool = False
    business_impact: bool = False
    secret_exposure: bool = False
    cost_usd: float = 0.0
    minutes: float = 0.0
    files: int = 0


@dataclass(frozen=True, slots=True)
class AutonomyVerdict:
    """Desenlace: preautorizada, o las fronteras concretas que exigen a una persona."""

    preauthorized: bool
    rule: str
    reasons: tuple[str, ...] = ()
    boundaries: tuple[Boundary, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable para auditoría."""
        return {
            "preauthorized": self.preauthorized,
            "rule": self.rule,
            "reasons": list(self.reasons),
            "boundaries": [item.value for item in self.boundaries],
        }


@dataclass(frozen=True, slots=True)
class AutonomyPolicy:
    """Política declarativa de autonomía preautorizada (``config/autonomy.yaml``)."""

    actions: frozenset[str] = frozenset()
    operations: frozenset[str] = frozenset()
    gated_classes: frozenset[str] = FLOOR_GATED_CLASSES
    git_reversible_delete: bool = False
    max_files: int = _DEFAULT_MAX_FILES
    max_cost_usd: float = _DEFAULT_MAX_COST
    max_minutes: float = _DEFAULT_MAX_MINUTES

    # ------------------------------------------------------------------ carga
    @classmethod
    def from_config(cls, raw: Mapping[str, Any] | None) -> AutonomyPolicy:
        """Construye la política desde el YAML aplicando el piso en código.

        El YAML **suma** fronteras (clases gobernadas) y **concede** operaciones, pero jamás lo que
        el piso reserva: una operación del piso se descarta aunque el YAML la liste, y las clases
        del piso siempre están gobernadas.
        """
        if not raw:
            return cls()
        section = raw.get("preauthorized")
        section = section if isinstance(section, Mapping) else {}
        budget = raw.get("budget")
        budget = budget if isinstance(budget, Mapping) else {}
        return cls(
            actions=_names(section.get("actions")) - frozenset(FLOOR_NEVER_OPERATIONS),
            operations=_names(section.get("operations"))
            - frozenset(FLOOR_NEVER_OPERATIONS)
            - frozenset(RELEASE_OPERATIONS),
            gated_classes=FLOOR_GATED_CLASSES | _names(raw.get("gated_resource_classes")),
            git_reversible_delete=bool(section.get("git_reversible_delete", False)),
            max_files=_positive_int(budget.get("max_files"), _DEFAULT_MAX_FILES),
            max_cost_usd=_positive_float(budget.get("max_cost_usd"), _DEFAULT_MAX_COST),
            max_minutes=_positive_float(budget.get("max_minutes"), _DEFAULT_MAX_MINUTES),
        )

    @classmethod
    def from_file(cls, path: Path) -> AutonomyPolicy:
        """Carga la política desde un YAML (vacía si no existe: sin autonomía delegada)."""
        import yaml

        if not path.is_file():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_config(data if isinstance(data, Mapping) else {})

    # ---------------------------------------------------------------- consulta
    @property
    def enabled(self) -> bool:
        """True si la política concede alguna operación."""
        return bool(self.actions or self.operations)

    def git_recoverable_delete(self, resource_classes: Iterable[str], recoverable: bool) -> bool:
        """True si un borrado es autónomo porque Git lo puede restaurar y no toca una frontera."""
        classes = set(resource_classes)
        return (
            self.git_reversible_delete
            and recoverable
            and not (classes & self.gated_classes)
            and "data_seed" not in classes
        )

    def evaluate(self, query: AutonomyQuery, context: AutonomyContext | None) -> AutonomyVerdict:
        """Decide si la operación está preautorizada o qué frontera la reserva a una persona.

        Devuelve **todas** las fronteras que se cruzan, no solo la primera: el motivo del Human Gate
        debe ser la causa real y completa, nunca «riesgo HIGH».
        """
        operation = query.operation.strip().lower()
        boundaries: list[Boundary] = []
        reasons: list[str] = []

        def cross(boundary: Boundary, reason: str) -> None:
            if boundary not in boundaries:
                boundaries.append(boundary)
            reasons.append(reason)

        never = FLOOR_NEVER_OPERATIONS.get(operation)
        if never is not None:
            cross(never, f"la operación {operation!r} es una frontera de autoridad por sí misma")
        release = RELEASE_OPERATIONS.get(operation)
        if release is not None and (context is None or release not in context.release_envelope):
            cross(
                Boundary.RELEASE_WITHOUT_ENVELOPE,
                f"{operation!r} exige que el destino declare un sobre explícito para {release!r}",
            )
        if context is None or not context.target_registered:
            cross(Boundary.UNAUTHORIZED_TARGET, "el destino no está registrado en PUNTO")
        elif not context.in_scope:
            cross(Boundary.UNAUTHORIZED_TARGET, "algún recurso queda fuera del alcance concedido")

        classes = set(query.resource_classes)
        for item in sorted(classes & self.gated_classes):
            cross(_class_boundary(item), f"el recurso es de clase {item!r}, frontera de autoridad")
        if query.secret_exposure:
            cross(Boundary.SECRETS, "la operación expone o maneja una credencial")
        if query.production_impact:
            cross(Boundary.PRODUCTION_DATA, "impacto declarado en producción")
        if query.legal_impact or query.business_impact:
            cross(Boundary.LEGAL_CONTRACTUAL, "impacto legal, contractual o de negocio")
        if not query.technical:
            cross(Boundary.NOT_PREAUTHORIZED, "la acción no es puramente técnica")

        if query.files > self.max_files:
            cross(
                Boundary.ENVELOPE_EXCEEDED,
                f"{query.files} recursos exceden el techo de {self.max_files} del sobre",
            )
        if query.minutes > self.max_minutes:
            cross(Boundary.ENVELOPE_EXCEEDED, f"{query.minutes:g} min exceden el sobre")
        if query.cost_usd > self.max_cost_usd:
            cross(
                Boundary.UNBUDGETED_COST,
                f"coste {query.cost_usd:g} USD fuera del presupuesto autorizado",
            )

        recovered = bool(context and context.git_recoverable) and self.git_reversible_delete
        if query.destructive and "data_seed" in classes and not query.created_by_cycle:
            cross(Boundary.IRREVERSIBLE, "borrar datos semilla no lo restaura un simple revert")
        elif (
            (not query.reversible or query.destructive)
            and not query.created_by_cycle
            and not recovered
        ):
            cross(
                Boundary.DESTRUCTIVE_MIGRATION
                if "migration" in operation
                else Boundary.IRREVERSIBLE,
                "la operación no es reversible y Git no puede restaurarla",
            )

        granted = operation in self.actions or operation in self.operations
        if not granted and never is None and release is None:
            cross(Boundary.NOT_PREAUTHORIZED, f"{operation!r} no figura como preautorizada")

        if boundaries:
            return AutonomyVerdict(
                preauthorized=False,
                rule="authority-boundary",
                reasons=tuple(reasons),
                boundaries=tuple(boundaries),
            )
        return AutonomyVerdict(
            preauthorized=True,
            rule="preauthorized-autonomy",
            reasons=(
                f"{operation!r} dentro del alcance del destino registrado, reversible y sin "
                "cruzar ninguna frontera de autoridad",
            ),
        )


def _class_boundary(resource_class: str) -> Boundary:
    """Frontera concreta que representa una clase de recurso."""
    return {
        "secret_store": Boundary.SECRETS,
        "payment_code": Boundary.PAYMENT,
        "production_data": Boundary.PRODUCTION_DATA,
        "infrastructure": Boundary.INFRASTRUCTURE,
        "constitutional_config": Boundary.AUTHORITY_EXPANSION,
        "authority_code": Boundary.AUTHORITY_EXPANSION,
        "security_control": Boundary.AUTHORITY_EXPANSION,
    }.get(resource_class, Boundary.GATED_RESOURCE)


def _names(raw: object) -> frozenset[str]:
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return frozenset()
    return frozenset(str(item).strip().lower() for item in raw if str(item).strip())


def _positive_int(raw: object, default: int) -> int:
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _positive_float(raw: object, default: float) -> float:
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default
