"""Authority Levels 0-3 y catálogo determinista de acciones.

Regla suprema del módulo: **DEFAULT DENY**. Ninguna acción obtiene autoridad
autónoma por el hecho de no estar catalogada. La ausencia de entrada se
interpreta como prohibición, nunca como permiso.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from punto.schemas.enums import AuthorityLevel

if TYPE_CHECKING:
    from collections.abc import Iterable


class UnknownActionError(LookupError):
    """Se lanza cuando una acción no existe en el catálogo de autoridad."""

    def __init__(self, action: str) -> None:
        self.action = action
        super().__init__(
            f"Acción desconocida '{action}': DEFAULT DENY. "
            "No se concede autoridad autónoma a acciones no catalogadas."
        )


@dataclass(frozen=True, slots=True)
class AuthorityRule:
    """Entrada del catálogo de autoridad."""

    action: str
    level: AuthorityLevel
    description: str = ""
    category: str = ""

    @property
    def requires_review(self) -> bool:
        """Nivel 1 exige revisión posterior obligatoria."""
        return self.level.requires_review

    @property
    def requires_human(self) -> bool:
        """Nivel 3 exige aprobación humana previa."""
        return self.level.requires_human


@dataclass(frozen=True, slots=True)
class AuthorityCatalog:
    """Catálogo inmutable acción -> nivel de autoridad.

    Se construye una sola vez desde ``config/permissions.yaml`` y no expone
    ninguna operación de mutación: CAMUS no puede alterar su propio catálogo en
    tiempo de ejecución.
    """

    rules: dict[str, AuthorityRule] = field(default_factory=dict)
    never_autonomous: frozenset[str] = frozenset()
    review_required_levels: frozenset[AuthorityLevel] = frozenset()
    human_required_levels: frozenset[AuthorityLevel] = frozenset()

    def __post_init__(self) -> None:
        """Congela los diccionarios internos para impedir mutación posterior."""
        object.__setattr__(self, "rules", dict(self.rules))

    def has(self, action: str) -> bool:
        """True si la acción está catalogada."""
        return action.strip().lower() in self.rules

    def get(self, action: str) -> AuthorityRule | None:
        """Devuelve la regla de una acción, o ``None`` si no está catalogada."""
        return self.rules.get(action.strip().lower())

    def level_for_action(self, action: str) -> AuthorityLevel:
        """Devuelve el nivel de autoridad de una acción.

        Raises:
            UnknownActionError: si la acción no está catalogada (DEFAULT DENY).
        """
        rule = self.get(action)
        if rule is None:
            raise UnknownActionError(action)
        return rule.level

    def is_never_autonomous(self, action: str) -> bool:
        """True si la acción nunca puede ejecutarse autónomamente."""
        return action.strip().lower() in self.never_autonomous

    def actions_by_level(self, level: AuthorityLevel) -> tuple[str, ...]:
        """Acciones catalogadas en un nivel, en orden alfabético determinista."""
        return tuple(sorted(name for name, rule in self.rules.items() if rule.level == level))

    def all_actions(self) -> tuple[str, ...]:
        """Todas las acciones catalogadas, ordenadas alfabéticamente."""
        return tuple(sorted(self.rules))

    @classmethod
    def from_entries(
        cls,
        entries: Iterable[AuthorityRule],
        *,
        never_autonomous: Iterable[str] = (),
        review_required_levels: Iterable[AuthorityLevel] = (),
        human_required_levels: Iterable[AuthorityLevel] = (),
    ) -> AuthorityCatalog:
        """Construye un catálogo a partir de entradas explícitas."""
        rules = {entry.action: entry for entry in entries}
        return cls(
            rules=rules,
            never_autonomous=frozenset(name.lower() for name in never_autonomous),
            review_required_levels=frozenset(review_required_levels),
            human_required_levels=frozenset(human_required_levels),
        )


__all__ = ["AuthorityCatalog", "AuthorityRule", "UnknownActionError"]
