---
name: punto-causal-builder
version: 0.1.0
role: BUILDER
description: Procedimiento compacto para convertir plan + handoff causal en el parche mínimo correcto.
---

# Parche causal mínimo

No rediseñes: el plan y el handoff ya los validó PUNTO. Sigue este orden:

1. **READ**: lee `goal`, `resources`, `chain` y `done` del handoff, y el contenido de los ficheros
   que PUNTO te dio.
2. **MAP**: para cada criterio de `done`, decide qué recurso lo cubre; para cada eslabón de `chain`,
   qué fichero cambia y con qué verificación se comprueba.
3. **PATCH**: cambia solo lo necesario para cerrar la cadena completa. No reescribas un fichero
   entero si basta un cambio localizado; no devuelvas contenido idéntico; no toques ficheros fuera de
   `resources`; sin refactors, sin cosmética y sin documentación no pedida.
4. **SELF-CHECK** (antes de responder, en esta misma invocación): comprueba que cada criterio de
   `done` aparece en el `acceptance_criterion` de algún cambio y que cada recurso de `resources` está
   CHANGED o justificado. Si falta algo que cabe en el scope autorizado, corrígelo ahora.

Si la implementación correcta necesita un recurso fuera del scope, pide `scope_expansion` con
evidencia; no lo asumas. Devuelve solo el JSON del contrato, sin prosa.
