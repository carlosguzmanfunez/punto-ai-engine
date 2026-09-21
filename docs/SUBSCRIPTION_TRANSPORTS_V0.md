# SUBSCRIPTION TRANSPORTS v0 — la suscripción debajo del provider

PUNTO AI ENGINE sigue viendo **el mismo contrato de proveedor**. Lo que cambia es lo que hay debajo:
cada proveedor puede hablar con su servicio por la API con clave o por el **cliente oficial de
suscripción** (Codex con cuenta ChatGPT, Claude Code con cuenta Claude). El router no lo sabe.

```
ENGINE -> ProviderRouter -> Provider -> Transport seleccionable -> Codex / Claude Code / API
                                    -> ProviderResult normalizado
```

## Transport abstraction

`src/punto/providers/transport.py` define `ProviderTransport`:

```
kind                                  codex | claude_code | api | existing
auth_mode                             chatgpt | claude_account | api_key
provider / model                      declarados, nunca inferidos
execute(request) -> ProviderResult    ejecuta y normaliza
auth_status() -> TransportAuthStatus  NOT_INSTALLED | NOT_AUTHENTICATED | AUTHENTICATED | UNAVAILABLE
health_check() -> ProviderHealth      comprobación mínima
usage_status() -> TransportUsage      límites oficiales, o UNKNOWN
capabilities() -> TransportCapabilities
```

`TransportBackedClient` (`src/punto/providers/transport_registry.py`) envuelve un transporte para que
cumpla el contrato que el router ya conocía: **el router no aprende nada nuevo**. El vocabulario no
está cerrado a Codex y Claude: cualquier proveedor futuro (una API compatible, un modelo local)
implementa la misma interfaz y se registra.

## Codex transport

`src/punto/providers/transports/codex.py`. Interfaz oficial:

- `codex --version` — ¿está instalado?
- `codex login status` — ¿hay sesión? (lo declara el cliente)
- `codex exec --json --sandbox read-only --skip-git-repo-check --model <m> <prompt>` — ejecución

Nunca se automatiza `chatgpt.com`, ni se extraen cookies, ni se copian tokens: la sesión la
administra Codex. El **App Server** (`codex app-server`, JSON-RPC) es la vía preferente para una
integración programática rica y puede sustituir a la CLI dentro de este transporte sin cambiar el
contrato. **Limitación declarada**: `codex exec` es texto; no acepta nuestras imágenes (con
`transport: api` sí).

## Claude Code transport

`src/punto/providers/transports/claude_code.py`. Interfaz oficial (comprobada contra la CLI 2.1.263
instalada en esta máquina):

- `claude --version` — ¿está instalado?
- `claude auth status --json` — `{"loggedIn": true|false, "authMethod": "oauth|apiKey|none", ...}`
- `claude --print --output-format json --model <m> <prompt>` — ejecución no interactiva

Nunca se automatiza `claude.ai`, ni se copia la sesión web, ni se extraen cookies. **Limitación
declarada**: `--print` es texto; para evidencia visual con adjuntos se usa `transport: api`
(multimodal).

## API alternatives

`src/punto/providers/transports/api.py` envuelve los adaptadores `httpx` que ya existían (OpenAI,
Anthropic, DeepSeek). Es el camino de pago explícito y el único que acepta imágenes hoy.

## auth states

| Estado | Qué significa |
| --- | --- |
| `NOT_INSTALLED` | el binario oficial no está en el `PATH` |
| `NOT_AUTHENTICATED` | está instalado, pero el cliente declara que no hay sesión |
| `AUTHENTICATED` | el cliente declara sesión iniciada |
| `UNAVAILABLE` | no se pudo leer el estado (proceso caído, timeout o salida ininteligible) |

Una salida que no se entiende **nunca** se interpreta como sesión válida.

## usage limits

Si el cliente oficial declara el límite agotado (por ejemplo «usage limit … resets at»), el
transporte lo marca como `LIMIT_REACHED` y lo conserva en `usage_status()`. Si el cliente **no**
expone límites de plan por una interfaz oficial —es el caso de Claude Code hoy— el estado es
`UNKNOWN`: no se inventan métricas ni se hace scraping de la cuenta.

## errores

`TransportErrorKind`: `NOT_INSTALLED`, `NOT_AUTHENTICATED`, `LIMIT_REACHED`, `TIMEOUT`,
`PROCESS_FAILED`, `INVALID_RESPONSE`, `UNAVAILABLE`. Se proyectan sobre el vocabulario del contrato
(`ProviderErrorKind`) y llegan al motor como `ProviderResult`, sin romperlo. `PROCESS_FAILED` se
conserva porque no es lo mismo que «no se sabe qué pasó».

## seguridad

- **Sin credenciales**: no se leen, copian ni guardan cookies, tokens OAuth, contraseñas ni cabeceras.
  La sesión la administra el cliente oficial. `claude auth status` solo devuelve estado.
- **Entorno mínimo**: los procesos reciben una lista blanca (`PATH`, `HOME`, …) y **nunca** una
  variable que contenga `API_KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `COOKIE`, `CREDENTIAL` o
  `AUTHORIZATION`.
- **Sin shell**: `argv` controlado, `shell=False`. Lo único que viaja como dato es el texto de la
  petición; ninguna opción se construye a partir de la respuesta de un modelo.
- **Saneado**: todo texto que sale de un transporte (contenido y error) pasa por un borrado de
  patrones de credencial antes de llegar a un resultado, un evento o un log.
- **Sin fallback**: un límite agotado o un fallo del transporte de suscripción **no** cambia a la API
  de pago. Eso podría generar cargos; el cambio de transporte es configuración explícita. El failover
  entre **proveedores** (`docs/PROVIDER_FAILOVER_V0.md`) tampoco usa un sustituto de pago por uso salvo
  `allow_metered: true`.

## Configuración

```yaml
providers:
  openai:
    model: "gpt-5-codex"
    transport: "codex"        # o "api"
    auth_mode: "chatgpt"      # o "api_key" con transport api
  anthropic:
    model: "claude-sonnet-5"
    transport: "claude_code"  # o "api"
    auth_mode: "claude_account"
  deepseek:
    model: "deepseek-v4-pro"
    transport: "existing"
    auth_mode: "api_key"
```

Sobreescrituras por entorno: `PUNTO_<PROVEEDOR>_TRANSPORT`, `PUNTO_<PROVEEDOR>_AUTH_MODE`,
`PUNTO_<PROVEEDOR>_MODEL`. Una combinación incoherente (por ejemplo `codex` con `api_key`) se
rechaza en configuración, no se degrada en silencio.

## Cómo lo usará el dashboard

Sin construir UI, la capa de configuración ya puede consultar:

```python
available_transports("openai")   # ("codex", "api")
selected_transport("openai")     # "codex"
auth_status("openai")            # TransportAuthStatus
health_check("openai")           # ProviderHealth
transport_status_table()         # PROVIDER | TRANSPORT | AUTH MODE | AUTH | HEALTH
```

Con eso se puede pintar `OpenAI / Codex · Auth: Not authenticated · [Connect]` y ejecutar
`test_connection(provider)`, `assign_role(role, provider)` y `select_model(provider, model)` sin
tocar el motor. La conexión real la hace el cliente oficial (`codex login`, `claude auth login`):
PUNTO nunca pide una contraseña ni inicia un login por su cuenta.
