"""Recuperación activa de evidencia (CAUSAL ACTION LOOP v1): actúa sobre la causa, no repite.

Un intento ``INCONCLUSIVE`` no se recupera pidiendo la misma evidencia otra vez: eso repite la
observación, no la cambia. Este módulo decide, para un criterio pendiente, **qué acción concreta**
cambiaría materialmente lo que se le entrega al revisor (más contenido visible, evidencia de una
interacción real), deriva esa acción del criterio y de la configuración declarada del destino —
nunca de un proyecto concreto— y se detiene cuando ya no queda ninguna estrategia distinta que
probar, en vez de seguir gastando presupuesto en observaciones equivalentes.

Es puro: decide, no captura ni evalúa. ``DevelopmentCycle`` ejecuta la acción que devuelve.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from punto.acceptance import ClaimRecord
    from punto.workspace.target import DevelopmentTarget

__all__ = [
    "ACTION_EXPAND_FRAMING",
    "ACTION_GUIDED_RECAPTURE",
    "ACTION_RUN_INTERACTION",
    "EvidenceAction",
    "EvidenceGap",
    "next_action",
]

#: Vocabulario cerrado de acciones (lo que ``DevelopmentCycle`` sabe ejecutar). Una acción nueva se
#: añade aquí y en el ejecutor; el planificador nunca inventa una que el ciclo no pueda cumplir.
ACTION_GUIDED_RECAPTURE: Final[str] = "GUIDED_RECAPTURE"
ACTION_EXPAND_FRAMING: Final[str] = "EXPAND_FRAMING"
ACTION_RUN_INTERACTION: Final[str] = "RUN_INTERACTION"

#: Factor de ampliación del encuadre por intento (más alto que el declarado: más contenido visible
#: en una sola captura, sin adivinar dónde está lo que falta). Acotado para no pedir un viewport
#: absurdo.
_FRAMING_GROWTH: Final[float] = 1.6
_MAX_FRAMING_HEIGHT: Final[int] = 4000


@dataclass(frozen=True, slots=True)
class EvidenceGap:
    """Qué falta demostrar y por qué la evidencia actual no basta, de forma estructurada."""

    claim: str
    evidence_class: str
    #: Etiqueta determinista y corta del hueco (no texto libre): identifica la CAUSA, no la repite.
    signal: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable (auditoría/grafo)."""
        return {
            "claim": self.claim,
            "evidence_class": self.evidence_class,
            "signal": self.signal,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class EvidenceAction:
    """Acción concreta que cerraría el hueco, con su **materialidad** (qué cambia de verdad)."""

    kind: str
    #: Firma de lo que cambia respecto al intento anterior (viewport, interacción...). Dos acciones
    #: con la misma firma son la misma observación: la regla anti-repetición se apoya en esto.
    materiality: tuple[Any, ...]
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable (auditoría/grafo)."""
        return {"kind": self.kind, "materiality": list(self.materiality), "detail": self.detail}


def diagnose(record: ClaimRecord) -> EvidenceGap:
    """Hueco estructurado de un registro ``INCONCLUSIVE`` (única clase reintentable)."""
    return EvidenceGap(
        claim=record.sentence,
        evidence_class=record.evidence_class,
        signal="unclear-evidence",
        detail=record.evidence[:300],
    )


def next_action(
    *,
    target: DevelopmentTarget,
    is_interaction_claim: bool,
    tried: frozenset[tuple[Any, ...]],
) -> EvidenceAction | None:
    """La siguiente acción **materialmente distinta** de todo lo ya probado, o ``None`` si se agotó.

    La escalera es genérica y se deriva de lo que el destino ya declara (su viewport, sus
    interacciones), nunca de un proyecto: primero se amplía el encuadre (más contenido visible en
    una sola captura, para "fuera de viewport" / "requiere scroll" / "encuadre insuficiente");
    luego, si el destino declara una interacción real, se combina evidencia de interacción (para
    "estado/control no activado"); agotadas las dos, no hay más estrategia que una nueva captura no
    pueda repetir por sí sola y se devuelve ``None`` (el llamante escala, justificado).
    """
    base_w, base_h = target.visual_viewport
    framing = (
        "viewport",
        base_w,
        min(int(base_h * _FRAMING_GROWTH), _MAX_FRAMING_HEIGHT),
    )
    if framing not in tried and framing != ("viewport", base_w, base_h):
        return EvidenceAction(
            kind=ACTION_EXPAND_FRAMING,
            materiality=framing,
            detail=f"encuadre ampliado a {framing[1]}x{framing[2]} (más contenido en una captura)",
        )
    if not is_interaction_claim and target.visual_interactions:
        signature = ("interaction", tuple(item.name for item in target.visual_interactions))
        if signature not in tried:
            return EvidenceAction(
                kind=ACTION_RUN_INTERACTION,
                materiality=signature,
                detail=(
                    "evidencia de la interacción declarada del destino, combinada con la captura "
                    "estática (el estado del control puede ser lo que faltaba demostrar)"
                ),
            )
    return None
