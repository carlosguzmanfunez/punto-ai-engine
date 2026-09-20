---
name: punto-focused-resolution
version: 0.1.0
role: BUILDER
description: Procedimiento compacto para convertir una verificación fallida en una reparación discriminante.
---

# Resolución focalizada de un fallo

Solo actúas cuando una verificación real ya falló. No rediseñes ni repitas lo que ya se intentó.

1. **OBSERVE**: qué verificación exacta falló, con su código de salida y su salida.
2. **LOCALIZE**: qué recurso mide esa verificación (RELEVANT RESOURCES). Ese recurso es la causa
   candidata; el síntoma suele estar en otro sitio.
3. **COMPARE**: qué tocó el parche anterior. Cambiar de estrategia sin tocar el recurso que la
   verificación mide no es progreso.
4. **HYPOTHESIS**: una frase operacional para `root_cause`: qué está mal en ese recurso y por qué el
   fallo persiste. Susténtala en la evidencia citada.
5. **DISCRIMINATING PATCH**: el cambio mínimo que confirma o refuta esa hipótesis.
6. **EXPLAIN OR ADDRESS**: para cada recurso relevante, o lo cambias, o lo declaras en
   `unchanged_resources` con evidencia de por qué no necesita cambio, o pides `scope_expansion` si
   está fuera del alcance. Que una verificación lea un fichero no obliga a tocarlo.
7. **VERIFY**: reutiliza las verificaciones que el plan ya declara; no añadas pruebas para aparentar
   rigor.
8. Si recibes CAUSAL_STAGNATION, no repitas la estrategia anterior: cambia la hipótesis o aborda el
   recurso omitido.

Devuelve solo el JSON del contrato, sin prosa.
