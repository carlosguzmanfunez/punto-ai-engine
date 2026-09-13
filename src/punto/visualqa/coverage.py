"""Cobertura visual: qué se exigía y qué se envió de verdad (ENGINE-5.3.1, V53-02).

El fallo que cierra este módulo era tautológico: Visual QA derivaba las capturas **requeridas** de
las capturas **producidas** (``task.screenshots``), así que una sesión que solo hubiera medido una
ruta de tres aparecía completa. La fuente de verdad es la **especificación**: el producto cartesiano
exacto ``spec.routes x spec.viewports``.

La identidad de una captura no es su nombre de archivo, que es cosmético y puede repetirse con
distinto contenido: es el par ``(route, viewport)``. Los nombres lógicos siguen existiendo porque
son la clave del artefacto y del ``ImagePayload``, pero no deciden nada.

Reglas de presencia, todas explícitas:

- un par está presente solo si existe su ``ScreenshotArtifact``, existe su ``ImagePayload``, y los
  bytes **enlazan** con lo que el artefacto declara (tamaño y ``sha256``) y con su media type y su
  nombre lógico. Un enlace roto no es una captura presente: es un par ausente con su motivo;
- un artefacto **sin** payload no cuenta como presente;
- un payload **sin** artefacto se ignora: no puede satisfacer cobertura, porque nadie verificó de
  dónde salió;
- un par repetido es un error de contrato y bloquea: dos capturas del mismo par significan que algo
  se capturó dos veces y el modelo no sabría cuál mirar;
- un par extra que nadie pidió se informa, pero **no** puede tapar un par requerido ausente.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from punto.providers.base import ImagePayload
from punto.schemas.visual import VisualSpec
from punto.schemas.web import ScreenshotArtifact, ViewportName

#: Identidad semántica de una captura: la ruta lógica y el viewport, no el nombre del archivo.
CoverageKey = tuple[str, ViewportName]


def _label(key: CoverageKey) -> str:
    """Etiqueta legible de un par ruta x viewport."""
    return f"{key[0]} @ {key[1].value}"


@dataclass(frozen=True, slots=True)
class VisualCoverage:
    """Resultado de comparar la cobertura exigida con la realmente disponible."""

    expected: tuple[CoverageKey, ...] = ()
    present: tuple[CoverageKey, ...] = ()
    missing: tuple[CoverageKey, ...] = ()
    unexpected: tuple[CoverageKey, ...] = ()
    duplicates: tuple[CoverageKey, ...] = ()
    artifacts_without_payload: tuple[str, ...] = ()
    payloads_without_artifact: tuple[str, ...] = ()
    invalid_bindings: tuple[str, ...] = ()
    #: Payloads **canónicos**, reconstruidos desde el artefacto, en el orden de la especificación.
    payloads: tuple[tuple[CoverageKey, ImagePayload], ...] = ()

    @property
    def complete(self) -> bool:
        """True si la cobertura exigida está entera y sin contradicciones."""
        return (
            bool(self.expected)
            and not self.missing
            and not self.duplicates
            and not self.invalid_bindings
        )

    @property
    def expected_payloads(self) -> tuple[tuple[CoverageKey, ImagePayload], ...]:
        """Pares **exigidos** que están presentes, en el orden de la especificación.

        Es la superficie de evaluación: lo que se envía al modelo. Un par extra que nadie pidió se
        informa en ``unexpected``, pero no se envía: la especificación define qué se evalúa.
        """
        expected = set(self.expected)
        return tuple((key, payload) for key, payload in self.payloads if key in expected)

    @property
    def images(self) -> tuple[ImagePayload, ...]:
        """Payloads canónicos que se envían al modelo, en orden de especificación."""
        return tuple(payload for _, payload in self.expected_payloads)

    @property
    def names(self) -> tuple[str, ...]:
        """Nombres lógicos de las capturas presentes."""
        return tuple(payload.logical_name for payload in self.images)

    @property
    def routes(self) -> tuple[str, ...]:
        """Rutas realmente presentes, en orden de especificación."""
        return tuple(dict.fromkeys(route for route, _ in self.present))

    @property
    def viewports(self) -> tuple[ViewportName, ...]:
        """Viewports realmente presentes, en orden de especificación."""
        return tuple(dict.fromkeys(viewport for _, viewport in self.present))

    def missing_labels(self) -> tuple[str, ...]:
        """Pares ausentes, en formato legible."""
        return tuple(_label(key) for key in self.missing)

    def detail(self) -> str:
        """Motivo determinista del gate de capturas, sin omitir nada."""
        if self.complete:
            return (
                f"{len(self.present)} par(es) ruta x viewport disponibles de "
                f"{len(self.expected)} exigidos"
            )
        parts: list[str] = []
        if self.missing:
            parts.append(
                f"faltan {len(self.missing)} par(es) exigido(s): "
                + ", ".join(self.missing_labels()[:5])
                + ("…" if len(self.missing) > 5 else "")
            )
        if self.duplicates:
            parts.append(
                "pares repetidos: "
                + ", ".join(_label(key) for key in self.duplicates[:5])
            )
        if self.artifacts_without_payload:
            parts.append(
                "artefacto(s) sin imagen: " + ", ".join(self.artifacts_without_payload[:5])
            )
        if self.invalid_bindings:
            parts.append("imagen(es) que no enlazan: " + "; ".join(self.invalid_bindings[:3]))
        if self.unexpected:
            parts.append(
                "par(es) no pedido(s): " + ", ".join(_label(key) for key in self.unexpected[:5])
            )
        if self.payloads_without_artifact:
            parts.append(
                "imagen(es) sin artefacto (ignoradas): "
                + ", ".join(self.payloads_without_artifact[:5])
            )
        return "; ".join(parts) if parts else "cobertura visual incompleta"


def expected_visual_coverage(spec: VisualSpec) -> tuple[CoverageKey, ...]:
    """Producto cartesiano exacto ``spec.routes x spec.viewports``, en orden determinista.

    El orden es el de la especificación: ruta mayor, viewport menor. Es el orden en el que las
    capturas se piden, se verifican y se envían, así que dos ejecuciones del mismo caso producen la
    misma secuencia.
    """
    return tuple(
        (route, viewport.name) for route in spec.routes for viewport in spec.viewports
    )


def evaluate_visual_coverage(
    spec: VisualSpec,
    artifacts: Sequence[ScreenshotArtifact],
    payloads: Mapping[str, ImagePayload],
) -> VisualCoverage:
    """Compara la cobertura exigida por la especificación con la realmente disponible.

    Args:
        spec: Especificación visual, única fuente de verdad de lo que se exige.
        artifacts: Artefactos de la sesión técnica, ya verificados por el host.
        payloads: Imágenes disponibles, indexadas por nombre lógico.

    Returns:
        La cobertura con los pares presentes, ausentes, repetidos, extra y los enlaces inválidos.
        Los payloads devueltos son **canónicos**: se reconstruyen desde el artefacto, así que lo que
        viaja al modelo no depende de la metadata que haya pasado un llamante.
    """
    expected = expected_visual_coverage(spec)
    expected_set = set(expected)

    seen: set[CoverageKey] = set()
    present: list[CoverageKey] = []
    duplicates: list[CoverageKey] = []
    unexpected: list[CoverageKey] = []
    without_payload: list[str] = []
    invalid: list[str] = []
    bound: list[tuple[CoverageKey, ImagePayload]] = []
    used_names: set[str] = set()

    for artifact in artifacts:
        key = (artifact.route, artifact.viewport)
        if key in seen:
            duplicates.append(key)
            continue
        seen.add(key)

        payload = payloads.get(artifact.logical_name)
        if payload is None:
            without_payload.append(artifact.logical_name)
            continue
        used_names.add(artifact.logical_name)

        if payload.logical_name != artifact.logical_name:
            invalid.append(
                f"{artifact.logical_name!r} llega identificada como {payload.logical_name!r}"
            )
            continue
        if payload.media_type != artifact.media_type:
            invalid.append(
                f"{artifact.logical_name!r} declara {payload.media_type!r} y el artefacto "
                f"{artifact.media_type!r}"
            )
            continue
        try:
            canonical = artifact.as_image_payload(payload.data)
        except ValueError as exc:
            # El enlace artefacto/imagen se revalida **aquí**, en la frontera del runner: no se
            # depende de que el llamante haya usado el helper. Un mismo tamaño con distinto sha256
            # cae por esta rama y no llega al proveedor.
            invalid.append(f"{artifact.logical_name!r}: {exc}")
            continue

        present.append(key)
        bound.append((key, canonical))
        if key not in expected_set:
            unexpected.append(key)

    present_set = set(present)
    orphans = tuple(
        sorted(name for name in payloads if name not in used_names)
    )
    return VisualCoverage(
        expected=expected,
        present=tuple(present),
        missing=tuple(key for key in expected if key not in present_set),
        unexpected=tuple(unexpected),
        duplicates=tuple(dict.fromkeys(duplicates)),
        artifacts_without_payload=tuple(without_payload),
        payloads_without_artifact=orphans,
        invalid_bindings=tuple(invalid),
        payloads=tuple(bound),
    )


__all__ = [
    "CoverageKey",
    "VisualCoverage",
    "evaluate_visual_coverage",
    "expected_visual_coverage",
]
