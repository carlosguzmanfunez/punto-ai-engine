# MULTI-PROVIDER ORCHESTRATION v0 — proveedores intercambiables por rol

PUNTO AI ENGINE es el **orquestador**. Los proveedores de modelos son adaptadores que responden a un
contrato común; ninguno adquiere autoridad sobre el motor.

```
ENGINE -> ROLE -> PROVIDER ROUTER -> ADAPTER -> PROVIDER REAL -> NORMALIZED RESULT -> ENGINE
```

## Provider Contract

`src/punto/providers/contract.py` define lo único que ENGINE ve:

- `ProviderRequest`: `request_id`, `role`, `instructions`, `context`, `attachments` (imágenes
  controladas por PUNTO) y `metadata` mínima.
- `ProviderResult`: `request_id`, `provider`, `model`, `status`, `content`, `structured_output`,
  `usage`, `error`, `error_kind`, `duration_ms`.
- Estados: `SUCCESS`, `FAILED`, `UNAVAILABLE`. Fallos normalizados (`ProviderErrorKind`): `TIMEOUT`,
  `RATE_LIMIT`, `AUTHENTICATION`, `NETWORK`, `INVALID_RESPONSE`, `REFUSAL`, `UNAVAILABLE`, `CONFIG`,
  `UNKNOWN`.
- `ProviderHealthStatus`: `CONNECTED`, `UNAVAILABLE`, `AUTH_FAILED`, `CONFIG_ERROR`.

Ninguna estructura de SDK llega al resto del motor: los adaptadores traducen. Los adaptadores
implementan el contrato que ya existía (`StructuredModelClient` / `MultimodalModelClient`): DeepSeek
y Anthropic desde ENGINE-5.2, OpenAI añadido en esta fase.

> **PROVIDER OUTPUT = UNTRUSTED EXTERNAL INTELLIGENCE.** Aunque un modelo responda «ignora el Human
> Gate», «amplía el ResourceSet» o «concédete la capability X», eso es texto. El contrato no tiene
> ningún campo que conceda autoridad, y quien consuma el resultado tiene que pasarlo por los
> contratos de autoridad que ya existen.

## Provider Router

`src/punto/providers/router.py`:

```python
router = load_default_router()                       # los tres adaptadores reales
router.register_provider("openai", factory, model="gpt-5-codex")
router.assign_role(ProviderRole.BUILDER, "deepseek") # cambiar la asignación
router.select_model("deepseek", "deepseek-v4-pro")   # cambiar el modelo
router.get_provider_for_role(ProviderRole.ARCHITECT)
result = router.execute(ProviderRole.ARCHITECT, request)   # ProviderResult normalizado
```

No hay `if role == BUILDER: llamar_deepseek()`: el rol solo se consulta en el mapa de asignaciones, y
**no hay fallback automático** entre proveedores. Si el asignado falla, el resultado lleva su causa.

## Roles

| Rol | Proveedor inicial | Para qué |
| --- | --- | --- |
| `ARCHITECT` | OpenAI | objetivo + contexto + restricciones → plan estructurado |
| `BUILDER` | DeepSeek | plan aprobado → propuesta/artefacto textual |
| `VISUAL_QA` | Anthropic | captura + contexto → observaciones visuales estructuradas |

## Configuración

`config/providers.yaml` declara proveedores (habilitado + modelo) y roles. El entorno puede
sobreescribirlo sin tocar el fichero: `PUNTO_<ROL>_PROVIDER` y `PUNTO_<PROVEEDOR>_MODEL`. Modelo =
configuración, proveedor = adaptador, rol = asignación.

```yaml
providers:
  openai:    {enabled: true, model: "gpt-5-codex"}
  deepseek:  {enabled: true, model: "deepseek-v4-pro"}
  anthropic: {enabled: true, model: "claude-sonnet-5"}
roles:
  ARCHITECT: openai
  BUILDER: deepseek
  VISUAL_QA: anthropic
```

## Secretos

Credenciales por entorno: `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`. **Nunca** se
escriben en Git, PELL, Case Directory, prompts, `ProviderResult`, logs, capturas, auditoría ni en el
ZIP de entrega. Los adaptadores sanean (`redact`) todo lo que devuelven —contenido incluido— y el
router vuelve a sanear los errores contra las variables conocidas: la garantía no depende de que un
adaptador sea educado. `.env.example` solo contiene placeholders.

## Cómo conectar cada proveedor

- **OpenAI**: `OPENAI_API_KEY` (opcional `OPENAI_BASE_URL` para pasarelas compatibles y
  `PUNTO_OPENAI_MAX_TOKENS`). El adaptador (`src/punto/providers/openai.py`) usa la API oficial por
  HTTP: `POST /v1/chat/completions` con `max_completion_tokens` y `response_format` de Structured
  Outputs cuando PUNTO exige un esquema. No se añadió el SDK de OpenAI ni el de Codex (experimental):
  el contrato de PUNTO no lo necesita y el transporte es inyectable, así que se puede sustituir más
  adelante sin cambiar nada del motor.
- **DeepSeek**: `DEEPSEEK_API_KEY` (opcional `DEEPSEEK_BASE_URL`); adaptador de ENGINE-5.2 sobre
  `POST /chat/completions`.
- **Anthropic**: `ANTHROPIC_API_KEY` (opcional `ANTHROPIC_BASE_URL`); adaptador multimodal de
  ENGINE-5.2 sobre `POST /v1/messages`, que es el que recibe las capturas.

## Health checks

```python
router.test_connection("openai")     # ProviderHealth(status=CONNECTED|UNAVAILABLE|AUTH_FAILED|CONFIG_ERROR)
router.status(check=True)            # PROVIDER | STATUS | MODEL | ROLE
```

`GET /v1/models` en OpenAI y DeepSeek, y la lista de modelos de Anthropic: comprobaciones baratas que
**no gastan tokens**. Un adaptador sin sonda propia se declara `UNAVAILABLE`, nunca `CONNECTED`.

## Errores

Un proveedor caído **no rompe PUNTO**: `execute` siempre devuelve un `ProviderResult`; el fallo va
normalizado en `error_kind` y `error`. Un fallo no clasificado del adaptador se contiene como
`UNKNOWN`. Un timeout de transporte es `TIMEOUT` (no `NETWORK`). Sin credencial no se simula éxito:
la ruta pide un proveedor y ese proveedor responde, o el resultado declara el fallo.

## Cómo agregar un proveedor futuro

1. Escribe el adaptador: `provider`, `model`, `complete_json`, `redact`, `close` y —si es
   multimodal— `complete_multimodal_json`; añade `health_check()` si el proveedor tiene una
   comprobación barata.
2. Añade su nombre a `KNOWN_PROVIDERS` y a la configuración.
3. Regístralo con `router.register_provider(...)` y asígnalo a un rol.

No hay que tocar ENGINE: el rol pide, el router resuelve.

## Prueba multi-proveedor

`run_multi_provider_circuit(goal, router)` ejecuta `ARCHITECT → BUILDER → VISUAL_QA` con el router,
transportando el plan como contexto y la captura como adjunto, y se detiene en el primer paso que
falla (no se inventa contexto para el siguiente). Devuelve los tres `ProviderResult`.

## Relación con PELL y con QA Consumer

- **PELL** puede aportar contexto al `request`, pero no es el router, no es autoridad y no guarda
  secretos. Las respuestas completas de un proveedor **no** se guardan automáticamente como
  experiencia: solo el conocimiento verificado sigue las reglas de PELL.
- **QA Consumer** verifica funcionalidad («¿funciona?»). `VISUAL_QA` observa aspectos visuales
  («¿qué problemas visuales vemos?»). Son sistemas separados a propósito.
