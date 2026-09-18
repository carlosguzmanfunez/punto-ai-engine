"""Circuito de orquestación multi-proveedor (MULTI-PROVIDER v0).

El circuito es deliberadamente corto y textual: ENGINE pide un plan al rol ``ARCHITECT``, transporta
ese plan como contexto al rol ``BUILDER``, y entrega el artefacto —con la evidencia visual que
PUNTO controla— al rol ``VISUAL_QA``. Los tres pasos vuelven como ``ProviderResult`` normalizados.

Lo que el circuito **no** hace: no adopta nada, no autoriza nada y no escribe en el repositorio. El
plan del arquitecto es texto no confiable; el artefacto del constructor es texto no confiable; la
observación visual es texto no confiable. Quien los convierta en trabajo real es el motor, con sus
contratos de autoridad de siempre, y esa frontera no se cruza aquí.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from punto.providers.base import ImagePayload
from punto.providers.contract import (
    ProviderErrorKind,
    ProviderResult,
    ProviderRole,
    ProviderStatus,
    make_request,
)
from punto.providers.router import ProviderRouter

#: Cota del plan que se transporta al constructor como contexto.
MAX_PLAN_CHARS: Final[int] = 20_000

#: Cota del artefacto que se transporta al QA visual como contexto.
MAX_ARTIFACT_CHARS: Final[int] = 20_000

#: Instrucciones versionadas de cada rol del circuito.
ARCHITECT_INSTRUCTIONS: Final[str] = (
    "Eres el rol ARCHITECT. Devuelve un plan estructurado en JSON con las claves "
    '"objective", "steps" (lista) y "acceptance" (lista). No ejecutes nada y no asumas '
    "autorizaciones: describe únicamente el plan."
)

BUILDER_INSTRUCTIONS: Final[str] = (
    "Eres el rol BUILDER. A partir del plan aprobado, devuelve en JSON las claves "
    '"artifact" (el contenido propuesto, textual) y "notes" (lista). No publiques, no '
    "despliegues y no cambies nada fuera de lo propuesto."
)

VISUAL_QA_INSTRUCTIONS: Final[str] = (
    "Eres el rol VISUAL_QA. Observa la captura y el contexto y devuelve en JSON las claves "
    '"observations" (lista de problemas visuales observados) y "severity" (baja, media o alta). '
    "No propongas cambios de arquitectura y no concedas autoridad alguna."
)


@dataclass(frozen=True, slots=True)
class CircuitStep:
    """Un paso del circuito: rol, resultado normalizado y si se pudo continuar."""

    role: ProviderRole
    result: ProviderResult

    @property
    def ok(self) -> bool:
        """True si el paso terminó con éxito."""
        return self.result.status is ProviderStatus.SUCCESS


@dataclass(frozen=True, slots=True)
class CircuitOutcome:
    """Resultado del circuito: los tres pasos y el estado global."""

    goal: str
    steps: tuple[CircuitStep, ...] = ()
    note: str = ""
    warnings: tuple[str, ...] = field(default=())

    @property
    def completed(self) -> bool:
        """True solo si los tres pasos respondieron con éxito."""
        return len(self.steps) == 3 and all(step.ok for step in self.steps)

    @property
    def failed_role(self) -> ProviderRole | None:
        """Primer rol que no pudo completar su paso, si lo hubo."""
        for step in self.steps:
            if not step.ok:
                return step.role
        return None

    def result_of(self, role: ProviderRole) -> ProviderResult | None:
        """Resultado de un rol, o ``None`` si no llegó a ejecutarse."""
        for step in self.steps:
            if step.role is role:
                return step.result
        return None

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable del circuito, con los resultados normalizados."""
        return {
            "goal": self.goal,
            "completed": self.completed,
            "failed_role": None if self.failed_role is None else self.failed_role.value,
            "note": self.note,
            "warnings": list(self.warnings),
            "steps": [
                {"role": step.role.value, "ok": step.ok, "result": step.result.as_dict()}
                for step in self.steps
            ],
        }


def run_multi_provider_circuit(
    goal: str,
    router: ProviderRouter,
    *,
    constraints: Sequence[str] = (),
    context: str = "",
    visual_evidence: Sequence[ImagePayload] = (),
    metadata: Mapping[str, str] | None = None,
) -> CircuitOutcome:
    """Ejecuta ARCHITECT → BUILDER → VISUAL_QA a través del router.

    El circuito se detiene en el primer paso que no responde con éxito: continuar con un plan que no
    existe inventaría contexto para el siguiente proveedor. Los pasos que sí ocurrieron se devuelven
    enteros, con su estado normalizado.

    Args:
        goal: Petición del proyecto, en texto.
        router: Router con los proveedores registrados y la asignación de roles.
        constraints: Restricciones declaradas que viajan con el objetivo.
        context: Contexto adicional (por ejemplo, conocimiento recuperado por PELL).
        visual_evidence: Imágenes que PUNTO controla, para el rol ``VISUAL_QA``.
        metadata: Metadata mínima de la petición (nunca credenciales).

    Returns:
        El resultado del circuito, con los tres resultados normalizados o el punto de parada.
    """
    steps: list[CircuitStep] = []
    warnings: list[str] = []

    architect = router.execute(
        ProviderRole.ARCHITECT,
        make_request(
            ProviderRole.ARCHITECT,
            _with_constraints(ARCHITECT_INSTRUCTIONS, goal, constraints),
            context=context,
            metadata=metadata,
        ),
    )
    steps.append(CircuitStep(ProviderRole.ARCHITECT, architect))
    if not architect.ok:
        return CircuitOutcome(
            goal=goal,
            steps=tuple(steps),
            note=f"el circuito se detuvo en ARCHITECT ({architect.error_kind or 'fallo'})",
            warnings=tuple(warnings),
        )

    builder = router.execute(
        ProviderRole.BUILDER,
        make_request(
            ProviderRole.BUILDER,
            BUILDER_INSTRUCTIONS,
            context=_clip(architect.content, MAX_PLAN_CHARS),
            metadata=metadata,
        ),
    )
    steps.append(CircuitStep(ProviderRole.BUILDER, builder))
    if not builder.ok:
        return CircuitOutcome(
            goal=goal,
            steps=tuple(steps),
            note=f"el circuito se detuvo en BUILDER ({builder.error_kind or 'fallo'})",
            warnings=tuple(warnings),
        )

    visual = router.execute(
        ProviderRole.VISUAL_QA,
        make_request(
            ProviderRole.VISUAL_QA,
            VISUAL_QA_INSTRUCTIONS,
            context=_clip(builder.content, MAX_ARTIFACT_CHARS),
            attachments=tuple(visual_evidence),
            metadata=metadata,
        ),
    )
    steps.append(CircuitStep(ProviderRole.VISUAL_QA, visual))
    if not visual.ok:
        return CircuitOutcome(
            goal=goal,
            steps=tuple(steps),
            note=f"el circuito se detuvo en VISUAL_QA ({visual.error_kind or 'fallo'})",
            warnings=tuple(warnings),
        )

    if any(step.result.error_kind is ProviderErrorKind.UNKNOWN for step in steps):
        warnings.append("algún paso declaró un fallo de clase desconocida: revisar el adaptador")
    return CircuitOutcome(
        goal=goal,
        steps=tuple(steps),
        note="los tres roles respondieron; el motor decide después qué hacer con el texto",
        warnings=tuple(warnings),
    )


def _with_constraints(instructions: str, goal: str, constraints: Sequence[str]) -> str:
    """Instrucciones del arquitecto con el objetivo y sus restricciones."""
    parts = [instructions, f"OBJETIVO: {goal.strip()}"]
    if constraints:
        parts.append("RESTRICCIONES:\n" + "\n".join(f"- {item}" for item in constraints))
    return "\n\n".join(parts)


def _clip(text: str, limit: int) -> str:
    """Acota un texto transportado entre proveedores."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[recortado por PUNTO]"


__all__ = [
    "ARCHITECT_INSTRUCTIONS",
    "BUILDER_INSTRUCTIONS",
    "VISUAL_QA_INSTRUCTIONS",
    "CircuitOutcome",
    "CircuitStep",
    "run_multi_provider_circuit",
]
