# PROVIDER CONFIGURATION DASHBOARD v0 — la interfaz de administración de proveedores

Una sola página para **administrar los proveedores de IA del motor**: verlos, ver su estado real,
elegir transporte, conectar la cuenta, configurar la clave, elegir modelo, probar la conexión,
asignar roles y dar de alta proveedores nuevos.

La página **no implementa lógica paralela**: todo lo que muestra y todo lo que cambia pasa por las
capas que ya existían —`ProviderRouter`, contrato de proveedor, transportes de suscripción,
`health_check`, `auth_status`, transportes disponibles, transporte seleccionado y asignación de
roles—. No hay estado simulado, ni botón verde falso, ni modelo inventado, ni `PASS` escrito a mano.

```
navegador -> GET /dashboard           (HTML autocontenido, mismo origen que la API)
          -> GET /providers           PROVIDER | TRANSPORT | AUTH | MODEL | ROLE | STATUS
          -> POST /providers/{id}/...  configuración (transporte, modelo, clave, conectar, probar)
          -> POST /roles/{role}        asignación de rol -> ProviderRouter real
```

## Cómo se abre

```powershell
.\.venv\Scripts\python.exe -m uvicorn punto.api.app:app --host 127.0.0.1 --port 8000
# dashboard:  http://127.0.0.1:8000/dashboard
# openapi:    http://127.0.0.1:8000/docs
```

No hay build, ni frontend, ni dependencias nuevas: `src/punto/api/static/dashboard.html` es un
documento único con su CSS y su JS, servido desde el mismo origen que la API.

## La tabla

`GET /providers` devuelve, por proveedor:

| Columna | De dónde sale |
| --- | --- |
| `PROVIDER` | catálogo: los conocidos (`openai`, `deepseek`, `anthropic`) y los dados de alta |
| `TRANSPORT` | `selected_transport` de la configuración vigente, dentro de los `available_transports` |
| `AUTH` | `auth_status` real: `NOT_INSTALLED`, `NOT_AUTHENTICATED`, `AUTHENTICATED`, `UNAVAILABLE` |
| `MODEL` | modelo configurado (nunca inferido) |
| `ROLE` | roles asignados según el `ProviderRouter` |
| `STATUS` | estado normalizado, derivado de la autenticación real y de la configuración |

`STATUS` no inventa nada: `NOT_INSTALLED` (el binario oficial no está), `NOT_AUTHENTICATED` (no hay
sesión ni credencial), `CONNECTED` (hay sesión o credencial utilizable), `UNAVAILABLE` (no se pudo
leer el estado), `NOT_CONFIGURED` (falta la URL o la clave de un proveedor nuevo), `LIMIT_REACHED` y
`ERROR` (los declara la sonda al probar la conexión). **Ningún estado se afirma sin haberlo leído.**

## El recorrido completo

| Capacidad | En la página | En la API |
| --- | --- | --- |
| Ver proveedores | tarjetas de `Proveedores` | `GET /providers` |
| Ver estado | punto de color + `STATUS` por tarjeta | `GET /providers/{id}` (`status`, `auth`, `usage`) |
| Seleccionar transporte | desplegable `Transport` | `POST /providers/{id}/transport` |
| Conectar / autenticar | botón `Connect` | `POST /providers/{id}/connect` |
| Configurar API | campo `API key` + `Guardar clave` | `POST /providers/{id}/api-key` (borrar: `DELETE`) |
| Seleccionar modelo | campo `Model` + `Guardar` | `POST /providers/{id}/model` |
| Probar conexión | botón `Test connection` | `POST /providers/{id}/test` |
| Asignar roles | desplegables `ARCHITECT`/`BUILDER`/`VISUAL_QA` | `POST /roles/{role}` |
| Añadir proveedores futuros | formulario `Agregar proveedor` | `POST /providers/custom` |

### Conectar y autenticar

`Connect` **no pide credenciales ni inicia sesión**: muestra el comando oficial del transporte
seleccionado (`codex login`, `claude auth login`) y el estado real de la sesión, para que la persona
lo ejecute en su terminal. PUNTO nunca automatiza la web del proveedor, ni copia cookies, ni guarda
tokens: la sesión la administra el cliente oficial.

### Configurar la API

La clave se envía por `POST` y **nunca vuelve**: la respuesta solo dice `api_key_configured`. En la
página, el campo se vacía al guardar y el estado se pinta como `clave configurada` / `sin clave`.

### Probar conexión

Usa la sonda real más barata del transporte seleccionado: `--version` / `auth status --json` para los
transportes de suscripción y el `health_check` del proveedor para la API. Devuelve un estado del
vocabulario y el detalle saneado; no hay comprobaciones de red en la carga de la página (el estado
que se ve al abrir es el que se puede afirmar sin salir a la red).

### Asignar roles

Cada rol declara la capacidad que necesita (`ARCHITECT` → `STRUCTURED_OUTPUT`, `BUILDER` → `CODING`,
`VISUAL_QA` → `VISION`). Asignar un proveedor que no la declara **avisa**, no bloquea: la asignación
se guarda y la advertencia se muestra. La asignación va al `ProviderRouter` real, así que el motor la
ve; lo que **no** hace es conceder autoridad: ninguna capacidad del motor cambia y las acciones
sensibles siguen pasando por el Human Gate.

### Añadir proveedores futuros

El alta admite `provider_id`, nombre, URL base, modelo, tipo de adaptador
(`openai_compatible`, `anthropic`, `deepseek`, `custom`), capacidades y clave opcional. Se persiste en
la **configuración local del dashboard**, no en el `config/providers.yaml` del repositorio. Un tipo de
adaptador sin implementación se registra y se muestra, y no se puede usar hasta que exista su
adaptador: la arquitectura ya lo admite sin tocar la interfaz.

## Protección del secreto

- Las claves viven en un almacén **fuera del repositorio** (`~/.punto/secrets.json` por defecto,
  `PUNTO_SECRETS_FILE` para reubicarlo), con escritura atómica y permisos restrictivos.
- La interfaz **solo** publica `api_key_configured`; ninguna respuesta devuelve el valor.
- Todo texto que sale hacia el navegador (detalles de error incluidos) pasa por un borrado de
  patrones de credencial.
- La configuración local (`providers.local.yaml`) guarda transporte, modelo, roles y proveedores
  nuevos: **nunca** claves.

## Lo que el dashboard no hace

- No concede autoridad ni añade rutas de aprobación: no hay forma de saltarse el Human Gate desde la
  interfaz.
- No inventa estado: sin Codex instalado, sin sesión de Claude Code y sin claves, la página lo dice.
- No llama a la API de pago por su cuenta: la sonda de `Test connection` es la más barata disponible
  y no ejecuta ninguna tarea del motor.
- No duplica el router: los roles y los proveedores se leen y se escriben en el `ProviderRouter`.

## Estado real de esta máquina

| PROVEEDOR | TRANSPORTE | AUTH | MOTIVO |
| --- | --- | --- | --- |
| `openai` | `codex` | `NOT_INSTALLED` | `codex` no está en el `PATH` |
| `deepseek` | `existing` | `NOT_AUTHENTICATED` | no hay clave configurada |
| `anthropic` | `claude_code` | `NOT_AUTHENTICATED` | la CLI 2.1.263 está instalada y declara `loggedIn: false` |

La página muestra exactamente esto. No se inicia ninguna sesión y no se pide ninguna credencial.

## Verificación

```powershell
pytest tests/test_provider_dashboard.py -q     # backend y secreto (sin navegador)
pytest tests/dashboard_qa -q                   # navegador real -> dashboard -> backend -> DOM
pytest tests/cases -q                          # CASE-019 (secreto) y CASE-020 (autoridad)
```
