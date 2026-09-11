"""Risk Engine determinista.

El riesgo efectivo de una acción es el **máximo** entre:

1. el riesgo declarado en la :class:`ActionRequest`, y
2. el riesgo calculado a partir de umbrales objetivos (costo, tiempo, archivos)
   y de los escaladores configurados (producción, legal, negocio, reversibilidad).

Esta regla garantiza que una petición nunca pueda *rebajar* su riesgo: declarar
``LOW`` no reduce un riesgo que los umbrales sitúan en ``HIGH``.

HIGH y CRITICAL requieren Human Gate por defecto.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from punto.schemas.decision import ActionRequest
from punto.schemas.enums import RiskLevel

#: Escaladores por defecto. Se sobrescriben con ``config/risk-rules.yaml``.
DEFAULT_ESCALATORS: dict[str, RiskLevel] = {
    "production_impact": RiskLevel.CRITICAL,
    "legal_impact": RiskLevel.HIGH,
    "business_impact": RiskLevel.HIGH,
    "irreversible": RiskLevel.HIGH,
}

#: Orden determinista de evaluación de escaladores.
ESCALATOR_ORDER: tuple[str, ...] = (
    "production_impact",
    "legal_impact",
    "business_impact",
    "irreversible",
)


@dataclass(frozen=True, slots=True)
class RiskThreshold:
    """Umbral de riesgo: techo de costo, tiempo y archivos para un nivel."""

    max_cost_usd: float
    max_estimated_minutes: float
    max_files_changed: int


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    """Resultado del cálculo de riesgo de una acción."""

    declared: RiskLevel
    computed: RiskLevel
    effective: RiskLevel
    reasons: tuple[str, ...]
    requires_human_gate: bool

    @property
    def escalated(self) -> bool:
        """True si el cálculo elevó el riesgo por encima del declarado."""
        return self.computed > self.declared


class RiskEngine:
    """Motor de riesgo determinista.

    Args:
        thresholds: umbrales por nivel (``low``, ``medium``, ``high``).
        escalators: niveles mínimos forzados por disparador.
        require_human_for: niveles que exigen Human Gate.
        force_medium_min_files: número de archivos a partir del cual una acción
            técnica y reversible se eleva como mínimo a ``MEDIUM``.
    """

    def __init__(
        self,
        *,
        thresholds: dict[RiskLevel, RiskThreshold] | None = None,
        escalators: dict[str, RiskLevel] | None = None,
        require_human_for: tuple[RiskLevel, ...] = (RiskLevel.HIGH, RiskLevel.CRITICAL),
        force_medium_min_files: int = 20,
    ) -> None:
        self._thresholds = dict(thresholds) if thresholds else _default_thresholds()
        self._escalators = dict(escalators) if escalators else dict(DEFAULT_ESCALATORS)
        self._require_human_for = frozenset(require_human_for)
        self._force_medium_min_files = force_medium_min_files

    @property
    def thresholds(self) -> dict[RiskLevel, RiskThreshold]:
        """Copia de los umbrales configurados."""
        return dict(self._thresholds)

    @property
    def escalators(self) -> dict[str, RiskLevel]:
        """Copia de los escaladores configurados."""
        return dict(self._escalators)

    def compute(self, request: ActionRequest) -> RiskLevel:
        """Calcula el riesgo objetivo de una petición, sin considerar lo declarado."""
        computed = RiskLevel.LOW
        for level in (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH):
            threshold = self._thresholds[level]
            if self._within_threshold(request, threshold):
                computed = level
                break
        else:
            computed = RiskLevel.CRITICAL

        for trigger in ESCALATOR_ORDER:
            forced = self._escalators.get(trigger)
            if forced is not None and self._trigger_active(trigger, request):
                computed = max(computed, forced)

        if (
            request.files_changed_count >= self._force_medium_min_files
            and computed < RiskLevel.MEDIUM
        ):
            computed = RiskLevel.MEDIUM

        return computed

    def assess(self, request: ActionRequest) -> RiskAssessment:
        """Evalúa el riesgo declarado, el calculado y el efectivo."""
        declared = request.risk_level
        computed = self.compute(request)
        effective = max(declared, computed)

        reasons: list[str] = []
        reasons.append(f"riesgo declarado: {declared.name}")
        reasons.append(f"riesgo calculado: {computed.name}")
        if effective > declared:
            reasons.append(f"riesgo efectivo elevado a {effective.name} por umbrales objetivos")
        if self.requires_human_gate(effective):
            reasons.append(f"riesgo {effective.name} requiere Human Gate por defecto")

        return RiskAssessment(
            declared=declared,
            computed=computed,
            effective=effective,
            reasons=tuple(reasons),
            requires_human_gate=self.requires_human_gate(effective),
        )

    def requires_human_gate(self, level: RiskLevel) -> bool:
        """True si el nivel exige Human Gate (HIGH y CRITICAL por defecto)."""
        return level in self._require_human_for

    def is_autonomous_allowed(self, level: RiskLevel) -> bool:
        """True si el nivel permite ejecución autónoma."""
        return level.is_autonomous_allowed

    @staticmethod
    def _within_threshold(request: ActionRequest, threshold: RiskThreshold) -> bool:
        """True si la petición cabe dentro del umbral indicado."""
        return (
            request.estimated_cost <= threshold.max_cost_usd
            and request.estimated_minutes <= threshold.max_estimated_minutes
            and request.files_changed_count <= threshold.max_files_changed
        )

    @staticmethod
    def _trigger_active(trigger: str, request: ActionRequest) -> bool:
        """Evalúa un disparador de escalado sobre una petición."""
        if trigger == "production_impact":
            return request.production_impact
        if trigger == "legal_impact":
            return request.legal_impact
        if trigger == "business_impact":
            return request.business_impact
        if trigger in {"irreversible", "not_reversible"}:
            return not request.reversible
        return False

    @classmethod
    def from_config(cls, risk_config: dict[str, Any]) -> RiskEngine:
        """Construye un Risk Engine desde ``config/risk-rules.yaml``."""
        raw_thresholds = risk_config.get("thresholds", {})
        thresholds = _default_thresholds()
        if isinstance(raw_thresholds, dict):
            mapping = {
                "low": RiskLevel.LOW,
                "medium": RiskLevel.MEDIUM,
                "high": RiskLevel.HIGH,
            }
            for key, level in mapping.items():
                entry = raw_thresholds.get(key)
                if isinstance(entry, dict):
                    thresholds[level] = RiskThreshold(
                        max_cost_usd=float(entry.get("max_cost_usd", 0.0)),
                        max_estimated_minutes=float(entry.get("max_estimated_minutes", 0.0)),
                        max_files_changed=int(entry.get("max_files_changed", 0)),
                    )

        escalators = dict(DEFAULT_ESCALATORS)
        raw_escalators = risk_config.get("escalators", {})
        if isinstance(raw_escalators, dict):
            for key, value in raw_escalators.items():
                if key == "files_changed_on_protected_path":
                    continue  # Se gestiona en el Policy Engine.
                if key == "unknown_action":
                    continue  # Se gestiona como DEFAULT DENY.
                if key == "not_reversible":
                    escalators["irreversible"] = _level_from_name(value, RiskLevel.HIGH)
                    continue
                escalators[key] = _level_from_name(value, RiskLevel.HIGH)

        raw_rules = risk_config.get("rules", {})
        require_human_for: tuple[RiskLevel, ...] = (RiskLevel.HIGH, RiskLevel.CRITICAL)
        force_medium_min_files = 20
        if isinstance(raw_rules, dict):
            raw_levels = raw_rules.get("require_human_for")
            if isinstance(raw_levels, list) and raw_levels:
                require_human_for = tuple(
                    _level_from_name(item, RiskLevel.HIGH) for item in raw_levels
                )
            raw_force = raw_rules.get("force_medium_when")
            if isinstance(raw_force, dict):
                force_medium_min_files = int(raw_force.get("min_files_changed", 20))

        return cls(
            thresholds=thresholds,
            escalators=escalators,
            require_human_for=require_human_for,
            force_medium_min_files=force_medium_min_files,
        )


def _level_from_name(value: object, default: RiskLevel) -> RiskLevel:
    """Convierte un nombre de nivel de riesgo en :class:`RiskLevel`."""
    if isinstance(value, str):
        try:
            return RiskLevel[value.strip().upper()]
        except KeyError:
            return default
    return default


def _default_thresholds() -> dict[RiskLevel, RiskThreshold]:
    """Umbrales por defecto, alineados con ``config/budgets.yaml``."""
    return {
        RiskLevel.LOW: RiskThreshold(
            max_cost_usd=1.0, max_estimated_minutes=15.0, max_files_changed=5
        ),
        RiskLevel.MEDIUM: RiskThreshold(
            max_cost_usd=10.0, max_estimated_minutes=60.0, max_files_changed=20
        ),
        RiskLevel.HIGH: RiskThreshold(
            max_cost_usd=100.0, max_estimated_minutes=240.0, max_files_changed=100
        ),
    }


__all__ = [
    "DEFAULT_ESCALATORS",
    "ESCALATOR_ORDER",
    "RiskAssessment",
    "RiskEngine",
    "RiskThreshold",
]
