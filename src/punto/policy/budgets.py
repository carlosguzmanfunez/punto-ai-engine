"""Política de presupuesto determinista.

Traduce ``config/budgets.yaml`` a límites duros por nivel de autoridad. Un
límite excedido rechaza la acción (no se ejecuta) y el Task Manager la bloquea
con el :class:`BlockedReason` correspondiente.

Los límites efectivos son el **mínimo** entre el techo del nivel de autoridad y
el presupuesto declarado en la :class:`ActionRequest`: una petición nunca puede
ampliar su propio presupuesto, solo restringirlo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from punto.schemas.decision import ActionRequest
from punto.schemas.enums import AuthorityLevel, BlockedReason

#: Límites por defecto si no se aporta configuración.
DEFAULT_LIMITS: dict[AuthorityLevel, LimitTuple] = {}


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Límites máximos de una tarea o acción."""

    max_cost_usd: float
    max_execution_minutes: float
    max_files_changed: int
    max_attempts: int

    @classmethod
    def from_mapping(
        cls, data: dict[str, Any], fallback: BudgetLimits | None = None
    ) -> BudgetLimits:
        """Construye límites desde un mapeo YAML, con respaldo opcional."""
        base = fallback
        return cls(
            max_cost_usd=float(data.get("max_cost_usd", base.max_cost_usd if base else 1.0)),
            max_execution_minutes=float(
                data.get("max_execution_minutes", base.max_execution_minutes if base else 15.0)
            ),
            max_files_changed=int(
                data.get("max_files_changed", base.max_files_changed if base else 5)
            ),
            max_attempts=int(data.get("max_attempts", base.max_attempts if base else 3)),
        )


@dataclass(frozen=True, slots=True)
class BudgetBreach:
    """Incumplimiento concreto de un límite de presupuesto."""

    reason: BlockedReason
    detail: str
    limit: float
    observed: float

    def __str__(self) -> str:
        return self.detail


#: Alias interno usado por los valores por defecto.
LimitTuple = BudgetLimits


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    """Presupuestos del motor por nivel de autoridad."""

    global_limits: BudgetLimits
    level_limits: dict[AuthorityLevel, BudgetLimits]
    defaults: BudgetLimits

    def limits_for(self, level: AuthorityLevel) -> BudgetLimits:
        """Límites del nivel de autoridad indicado."""
        return self.level_limits.get(level, self.defaults)

    def evaluate(self, request: ActionRequest, level: AuthorityLevel) -> tuple[BudgetBreach, ...]:
        """Comprueba una petición contra los límites de su nivel.

        Returns:
            Tupla de incumplimientos en orden determinista: costo, tiempo,
            archivos. Vacía si la petición cabe en el presupuesto.
        """
        limits = self.limits_for(level)
        breaches: list[BudgetBreach] = []

        allowed_cost = min(
            limits.max_cost_usd, request.max_cost_usd, self.global_limits.max_cost_usd
        )
        if request.estimated_cost > allowed_cost:
            breaches.append(
                BudgetBreach(
                    reason=BlockedReason.MAX_COST_EXCEEDED,
                    detail=(
                        f"costo estimado {request.estimated_cost:.4f} USD excede el "
                        f"presupuesto autorizado de {allowed_cost:.4f} USD"
                    ),
                    limit=allowed_cost,
                    observed=request.estimated_cost,
                )
            )

        allowed_minutes = min(
            limits.max_execution_minutes,
            request.max_execution_minutes,
            self.global_limits.max_execution_minutes,
        )
        if request.estimated_minutes > allowed_minutes:
            breaches.append(
                BudgetBreach(
                    reason=BlockedReason.MAX_TIME_EXCEEDED,
                    detail=(
                        f"tiempo estimado {request.estimated_minutes:.2f} min excede el "
                        f"presupuesto autorizado de {allowed_minutes:.2f} min"
                    ),
                    limit=allowed_minutes,
                    observed=request.estimated_minutes,
                )
            )

        allowed_files = min(
            limits.max_files_changed,
            request.max_files_changed,
            self.global_limits.max_files_changed,
        )
        if request.files_changed_count > allowed_files:
            breaches.append(
                BudgetBreach(
                    reason=BlockedReason.MAX_FILES_CHANGED,
                    detail=(
                        f"{request.files_changed_count} archivos exceden el máximo "
                        f"autorizado de {allowed_files}"
                    ),
                    limit=float(allowed_files),
                    observed=float(request.files_changed_count),
                )
            )

        return tuple(breaches)

    @classmethod
    def from_config(cls, budgets_config: dict[str, Any]) -> BudgetPolicy:
        """Construye la política desde ``config/budgets.yaml``."""
        default_limits = BudgetLimits(
            max_cost_usd=1.0, max_execution_minutes=15.0, max_files_changed=5, max_attempts=3
        )

        raw_defaults = budgets_config.get("defaults")
        defaults = (
            BudgetLimits.from_mapping(raw_defaults, default_limits)
            if isinstance(raw_defaults, dict)
            else default_limits
        )

        raw_global = budgets_config.get("global")
        global_limits = (
            BudgetLimits.from_mapping(raw_global, defaults)
            if isinstance(raw_global, dict)
            else defaults
        )

        level_limits: dict[AuthorityLevel, BudgetLimits] = {}
        raw_levels = budgets_config.get("levels", {})
        if isinstance(raw_levels, dict):
            for raw_key, raw_value in raw_levels.items():
                if not isinstance(raw_value, dict):
                    continue
                try:
                    level = AuthorityLevel(int(raw_key))
                except (TypeError, ValueError):
                    continue
                level_limits[level] = BudgetLimits.from_mapping(raw_value, defaults)

        if not level_limits:
            level_limits = _default_level_limits()

        return cls(
            global_limits=global_limits,
            level_limits=level_limits,
            defaults=defaults,
        )


def _default_level_limits() -> dict[AuthorityLevel, BudgetLimits]:
    """Límites por nivel usados si la configuración no los define."""
    return {
        AuthorityLevel.LEVEL_0_AUTONOMOUS: BudgetLimits(1.0, 15.0, 5, 3),
        AuthorityLevel.LEVEL_1_AUTONOMOUS_REVIEW: BudgetLimits(5.0, 30.0, 15, 3),
        AuthorityLevel.LEVEL_2_CAMUS: BudgetLimits(25.0, 120.0, 50, 4),
        AuthorityLevel.LEVEL_3_HUMAN: BudgetLimits(100.0, 240.0, 100, 5),
    }


DEFAULT_LIMITS = _default_level_limits()


__all__ = ["DEFAULT_LIMITS", "BudgetBreach", "BudgetLimits", "BudgetPolicy"]
