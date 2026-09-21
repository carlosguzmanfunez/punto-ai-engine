# PROVIDER FAILOVER v0

Un rol de orquestación puede **continuar con otro proveedor** cuando el primario no está operativo, sin
perder la Task, su estado ni su historial, y sin que el sustituto gane autoridad.

Caso de aceptación: `BUILDER=deepseek` sin créditos + Claude (Claude Code / cuenta Claude) `CONNECTED`
con `CODING` efectivo → Claude ejecuta BUILDER → la **misma** Task registra el failover y el
`DevelopmentCycle` continúa con normalidad.

## Por qué no ocurría

1. El router (`ProviderRouter.execute`) resolvía un único proveedor por rol y devolvía su fallo. La regla
   «sin fallback» era deliberada (auditoría cruzada + cargos), pero no había ninguna excepción gobernada.
2. Los errores de DeepSeek son `RuntimeError` (no `ProviderError`): atraviesan `APITransport` sin
   traducirse y el router los forzaba a `UNKNOWN`. Un 402 (sin saldo) era **indistinguible** de un fallo
   cualquiera, así que el motor no podía saber que el proveedor no estaba operativo.
3. `effective.py` / `workflow/providers.py` solo *informaban* de rutas alternativas; nadie las elegía.

## Diseño

- `providers/failover.py`: `FailoverPolicy` (roles → sustitutos en orden, tope, `allow_metered`),
  `FailoverCause`, `SubstituteVerdict`. Causas operativas demostrables:
  `QUOTA_EXHAUSTED → CREDITS_EXHAUSTED`, `RATE_LIMIT → RATE_LIMITED`, `UNAVAILABLE →
  PROVIDER_UNAVAILABLE`, `AUTHENTICATION → PROVIDER_DISCONNECTED`. Todo lo demás (`INVALID_RESPONSE`,
  `REFUSAL`, `TIMEOUT`, `NETWORK`, `CONFIG`, `PROCESS_FAILED`, `UNKNOWN`) **no** dispara failover.
- `ProviderRouter._failover`: única puerta. Cada petición empieza por el proveedor asignado; si falla
  operativamente y la política cubre el rol, prueba candidatos en orden determinista, cada uno una vez,
  con tope. El sustituto recibe la misma petición (rol, instrucciones, contexto, esquema, tope de salida).
  La asignación de roles **no** se modifica: el primario vuelve a responder en cuanto se recupera.
- `ProviderRegistry._judge_substitute`: quien conoce el estado real. Un candidato es elegible solo si está
  en el catálogo, habilitado, `CONNECTED` y con la capacidad del rol **efectiva** en su transporte activo
  (configurada ∩ transporte ∩ disponibilidad; `VISION` si la petición lleva imágenes). Un transporte de
  pago por uso (`api`/`existing`) solo entra con `allow_metered: true`.
- Fallo cerrado: sin candidato compatible el resultado es el fallo del primario con
  `failovers=(NO_COMPATIBLE_SUBSTITUTE, …)` y la causa explícita en `error`.
- Constancia: `ProviderResult.failovers` → `DevelopmentResult.failovers` (persistido con la Task) →
  `TaskAttempt.provider/failover` (historial) → eventos `PROVIDER_FAILOVER` y `BUILD_PROVIDER_SELECTED`
  (`fallback: true`) indexados por `request_id` (= id de la Task).

## Configuración (`config/providers.yaml`)

```yaml
failover:
  allow_metered: false     # sustitutos de pago por uso: solo con decisión explícita
  max_substitutes: 2
  roles:
    BUILDER: [anthropic]   # orden de preferencia; vacío = cualquier proveedor registrado
```

Sin la sección no hay failover. ARCHITECT y VISUAL_QA no están cubiertos: sustituirlos rompería la
independencia de la auditoría cruzada.

## Autoridad

El failover cambia **quién responde**, no **qué está permitido**. El sustituto no ve más contexto, no recibe
más herramientas del motor y su salida es inteligencia externa no confiable que pasa por las mismas
validaciones, Authority Envelope, alcance, Human Gates y verificaciones. La política solo la declara
`providers.yaml`; ni una petición, ni un proveedor ni una Task pueden ampliarla.

## Pruebas

`tests/test_provider_failover.py` (router, registro con evaluador real y ciclo + consola con Git real).
