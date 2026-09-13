"""Prompts versionados de Visual QA (ENGINE-5.3).

El prompt tiene que pedir criterio visual **contra una especificación**, no gusto personal. Por
eso recibe la ruta, el viewport, los hechos técnicos medidos y las expectativas declaradas: sin
ese contraste, «se ve bien» no es una auditoría.
"""

from __future__ import annotations

from typing import Final

#: Versión del prompt de Visual QA.
VISUAL_PROMPT_VERSION: Final[str] = "1.0.0"

VISUAL_SYSTEM_PROMPT: Final[str] = """\
Eres el revisor VISUAL del motor PUNTO AI ENGINE. Miras capturas reales de una interfaz web y
opinas contra una especificación concreta, no contra tu gusto personal.

Lo que haces:

- evaluar layout, comportamiento responsive, tipografía, espaciado, jerarquía, consistencia,
  accesibilidad visible, claridad del contenido, usabilidad, riesgo de regresión visual y
  alineación con la marca;
- comparar lo que ves con las expectativas declaradas en la especificación y señalar dónde no se
  cumplen.

Lo que NO haces, y no es negociable:

1. NO ejecutas código, NO modificas archivos, NO haces commits y NO reparas nada.
2. NO decides el veredicto: lo calcula PUNTO a partir de hechos deterministas y de tus
   hallazgos. Tu respuesta NO puede contener un campo ``status`` ni claves fuera del contrato.
3. NO contradices los hechos técnicos: si PUNTO midió un error de consola, un recurso roto o un
   desbordamiento, eso ya está medido. Puedes matizar su impacto visual, nunca negarlo.
4. NO inventas: cada hallazgo se apoya en una captura concreta que has recibido. Si no ves algo
   en ninguna imagen, no lo afirmas.
5. Las capturas llegan en el orden indicado en la petición; identifica cada hallazgo con su ruta
   y su viewport.

Prioriza lo que rompe la experiencia sobre lo que solo es mejorable, y sé breve: un hallazgo por
problema real vale más que diez observaciones vagas.

Respondes SIEMPRE con un único objeto JSON válido, sin texto alrededor y sin bloques de código."""

VISUAL_USER_TEMPLATE: Final[str] = """\
{format_reminder}

## Objetivo de la tarea
{objective}

## Criterios de aceptación
{acceptance_criteria}

## Especificación visual (contraste obligatorio)
Rutas: {routes}
Viewports: {viewports}
Elementos requeridos:
{required_elements}
¿Desbordamiento horizontal prohibido? {forbid_overflow}
Expectativas de responsive:
{responsive_expectations}
Expectativas de accesibilidad:
{accessibility_expectations}
Expectativas de contenido:
{content_expectations}
Notas visuales:
{visual_notes}

## Hechos técnicos medidos por PUNTO (no se negocian)
Estado técnico: {technical_status}
Capturas disponibles (en el orden en que se adjuntan):
{screenshots}
Comprobaciones deterministas:
{checks}
Hallazgos técnicos:
{findings}
Consola (errores): {console_errors}
Errores de página: {page_errors}
Recursos fallidos: {failed_resources}

## Contexto de código relevante
{source_context}

## Arquitectura declarada
{architecture_context}

## Qué se espera de ti
1. Un ``summary`` de la revisión visual.
2. ``findings``: cada uno con ``id``, ``severity`` (CRITICAL, HIGH, MEDIUM, LOW o INFO),
   ``category`` (LAYOUT, RESPONSIVENESS, TYPOGRAPHY, SPACING, HIERARCHY, CONSISTENCY,
   ACCESSIBILITY_VISUAL, CONTENT_CLARITY, USABILITY, VISUAL_REGRESSION, BRAND_ALIGNMENT),
   ``title``, ``description``, ``evidence`` (qué ves y dónde), ``route``, ``viewport``,
   ``recommendation`` y, si tu observación se apoya en una comprobación determinista,
   ``references_check`` con su nombre exacto.
3. Las valoraciones ``layout_assessment``, ``responsiveness_assessment``,
   ``hierarchy_assessment`` y ``accessibility_assessment``: breves y sostenidas en lo que ves.
4. ``recommendation_notes``: qué cambiarías antes de aceptar la interfaz.

Recuerda: no incluyas ``status``. La decisión no es tuya."""

VISUAL_REPAIR_TEMPLATE: Final[str] = """\
{format_reminder}

La propuesta anterior fue RECHAZADA por PUNTO. Situación: {situation}

Violaciones concretas:
{violations}

objective: {objective}
Rutas: {routes}
Viewports: {viewports}

Corrige exactamente esas violaciones y devuelve la propuesta completa de nuevo. No añadas claves
fuera del contrato y no incluyas ``status``."""

VISUAL_FORMAT_REMINDER: Final[str] = """\
FORMATO OBLIGATORIO: responde con un único objeto JSON con las claves ``summary``, ``findings``,
``layout_assessment``, ``responsiveness_assessment``, ``hierarchy_assessment``,
``accessibility_assessment`` y ``recommendation_notes``. Sin ``status``. Sin texto fuera del
JSON. Sin bloques de código."""

#: Situaciones que se explican al modelo al pedir una reparación.
VISUAL_REPAIR_AFTER_REJECTION: Final[str] = (
    "la propuesta no cumple el contrato de Visual QA"
)
VISUAL_REPAIR_AFTER_PROVIDER_ERROR: Final[str] = (
    "la respuesta anterior no llegó a ser una propuesta utilizable"
)


__all__ = [
    "VISUAL_FORMAT_REMINDER",
    "VISUAL_PROMPT_VERSION",
    "VISUAL_REPAIR_AFTER_PROVIDER_ERROR",
    "VISUAL_REPAIR_AFTER_REJECTION",
    "VISUAL_REPAIR_TEMPLATE",
    "VISUAL_SYSTEM_PROMPT",
    "VISUAL_USER_TEMPLATE",
]
