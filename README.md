# PUNTO AI ENGINE

**Núcleo constitucional determinista — Base Constitucional V0.1**

| Campo | Valor |
| --- | --- |
| Fase | **ENGINE-1** (capa de ejecución controlada) |
| Fase anterior | **ENGINE-0 — CERRADA** (núcleo constitucional) |
| Versión | `0.1.0` |
| Python | `>= 3.12` |
| Persistencia | En memoria (sin base de datos) |
| IA / LLM | **Deshabilitada**: ENGINE-1 no conecta ningún modelo |
| Integraciones externas | **Ninguna** |
| Red | No requerida |

**Estado de las fases**

| Fase | Contenido | Estado |
| --- | --- | --- |
| **ENGINE-0** | Núcleo constitucional determinista: autoridad, política, riesgo, presupuesto, Human Gate, máquina de estados, auditoría, CAMUS y API mínima. | ✅ **CERRADA** |
| **ENGINE-1** | Capa de ejecución controlada: `DeveloperRunner`, `ExecutionContext`, Filesystem, Shell, Git, Validator, `LocalDeveloperRunner`, auditoría de ejecución, fixture y demo real. | ✅ Implementada |
| **ENGINE-1.R1** | Frontera de ejecución confiable: separación `TRUSTED_LOCAL` / `UNTRUSTED_MODEL`, `ExecutionBackend`, `TrustedLocalBackend`, contrato `SandboxedBackend`, entorno saneado y fallo cerrado. | ✅ Implementada |
| **ENGINE-1.R3** | Sandbox **real**: WSL2 + Podman, `ContainerSandboxBackend`, verificación de capacidades por sondas, imagen reproducible y los **cuatro aislamientos demostrados**. | ✅ Implementada |
| **ENGINE-2** | **DeepSeek Developer Integration.** Integración real del modelo mediante `DeepSeekDeveloperRunner`, sobre la misma interfaz y **exigiendo sandbox**. | ⏳ Futura |

ENGINE-0 no es un agente inteligente: es el **esqueleto de gobernanza**. ENGINE-1
tampoco: es la **capa de ejecución controlada**, que permite ejecutar trabajo real
sobre un repositorio local de prueba sin ningún modelo de IA. Todo el
comportamiento es local, reproducible y verificable por pruebas.

> **Proveedor de IA.** El Developer AI principal de PUNTO AI ENGINE será
> **DeepSeek**, integrado en ENGINE-2. ENGINE-1 no conecta ningún proveedor de
> modelo: su ejecutor es determinista.


---

## 1. Principios constitucionales

Definidos en `config/constitution.yaml` y aplicados en código determinista
(`src/punto/policy/`). No son documentación decorativa: cada principio declara
los símbolos que lo hacen cumplir.

| # | Principio | Enunciado | Aplicado por |
| --- | --- | --- | --- |
| 1 | **Autonomía sin interrupción innecesaria** | Las decisiones técnicas reversibles dentro de permisos, presupuesto y riesgo autorizado se ejecutan sin intervención humana. Solo se detiene ante Human Gate, límite excedido, riesgo alto/crítico o imposibilidad de continuar con seguridad. | `PolicyEngine.evaluate`, `Camus` |
| 2 | **Núcleo determinista** | Sin IA, sin proveedores externos, sin red. Toda decisión es reproducible a partir de sus entradas. | `Camus` |
| 3 | **Default Deny** | Toda acción no catalogada se rechaza. Ninguna acción recibe autoridad autónoma por defecto. | `AuthorityCatalog.level_for_action` |
| 4 | **Inmutabilidad constitucional** | CAMUS no puede modificar sus reglas de autoridad ni sus permisos, ni siquiera mediante acciones técnicas, reversibles y de riesgo bajo. | `permissions.is_protected_path`, `PolicyEngine.evaluate` |
| 5 | **Human Gate para lo irreversible** | Las acciones de nivel 3 y las de riesgo alto o crítico requieren aprobación humana explícita antes de cualquier ejecución. | `HumanGate`, `RiskEngine.requires_human_gate` |
| 6 | **Auditar todo** | Cada creación de tarea, transición, bloqueo y decisión de política genera un evento de auditoría inmutable. | `AuditLogger` |

### Prohibiciones duras de CAMUS

`config/constitution.yaml` las declara y el código las verifica:

- no puede modificar `config/constitution.yaml`;
- no puede modificar `config/permissions.yaml`;
- no puede autoelevar su nivel de autoridad;
- no puede saltarse el Human Gate;
- no puede desplegar producción sin aprobación humana;
- no puede autorizar acciones constitucionalmente prohibidas.

Estas prohibiciones existen **también como piso en código**
(`punto.policy.permissions`), de modo que manipular los YAML no desactiva la
protección: la suite incluye una prueba que verifica que el piso sobrevive a una
configuración adulterada.

---

## 2. Arquitectura

```
src/punto/
├── _version.py            Identidad y versión (sin ciclos de importación)
├── common.py              Utilidades puras: utc_now, normalize_path, deep_freeze
├── schemas/               Modelos Pydantic v2 y enumeraciones
│   ├── enums.py           AuthorityLevel, RiskLevel, TaskStatus, BlockedReason, ...
│   ├── task.py            Task
│   ├── decision.py        ActionRequest, HumanApprovalRequest
│   ├── policy.py          PolicyDecision, PolicyOutcome
│   ├── result.py          ExecutionResult, TaskExecutionRecord
│   ├── execution.py       DeveloperTask, DeveloperExecutionResult, CommandResult, ...
│   └── audit.py           AuditEvent, AuditEventType
├── policy/                Gobernanza determinista
│   ├── config_loader.py   Carga de YAML con caché y errores duros
│   ├── authority.py       Catálogo de autoridad (default deny)
│   ├── permissions.py     Piso constitucional en código
│   ├── risk.py            Risk Engine (riesgo efectivo)
│   ├── budgets.py         Límites de costo, tiempo, archivos e intentos
│   ├── human_gate.py      Aprobaciones humanas
│   └── policy_engine.py   Evaluación en orden fijo y auditable
├── orchestrator/
│   ├── state_machine.py   Tabla de transiciones (autoridad única)
│   ├── planner.py         Plan determinista (sin IA)
│   └── camus.py           Orquestador CAMUS V0.1
├── tasks/
│   ├── manager.py         TaskManager en memoria
│   └── transitions.py     Reexportación de la máquina de estados
├── developer/             ENGINE-1: frontera de ejecución
│   ├── base.py            DeveloperRunner (interfaz abstracta)
│   ├── context.py         ExecutionContext (workspace, rama, límites)
│   └── local.py           LocalDeveloperRunner (determinista, sin IA)
├── tools/                 ENGINE-1: Tool Layer confinada
│   ├── errors.py          Taxonomía de errores (módulo hoja)
│   ├── filesystem.py      FilesystemTool (confinado al workspace)
│   ├── shell.py           ShellRunner (allowlist, default deny)
│   ├── git.py             GitWorkspace (local, sin remoto)
│   └── validator.py       Validator (checks reales)
├── audit/
│   ├── events.py          Catálogo de eventos obligatorios
│   └── logger.py          AuditLogger append-only en memoria
└── api/
    └── app.py             API FastAPI + contenedor `Engine`

fixtures/
└── minimal-python-project/   Proyecto Python mínimo usado como target de prueba
```

### Contenedor `Engine`

Un único ensamblado explícito. Todas las dependencias se inyectan por
constructor: no hay estado global oculto ni singletons implícitos.

```
Engine
├── AuditLogger
├── StateMachine
├── TaskManager(state_machine, audit)
├── PolicyEngine.from_config(environment)
├── HumanGate
├── Planner
└── Camus(task_manager, policy_engine, human_gate, audit, state_machine, planner)
```

### Higiene de importaciones (`__init__.py` ligeros)

Los `__init__.py` de los paquetes internos **no** provocan cargas eager de módulos
de alto nivel. `punto.orchestrator.__init__` reexporta únicamente los módulos hoja
(`planner` y `state_machine`); **no** reexporta `camus`, porque `camus` importa
`punto.tasks.manager` y `punto.tasks.manager` importa
`punto.orchestrator.state_machine`. Reexportarlo eagermente cerraba un ciclo en
frío (`tasks.manager → orchestrator → camus → tasks.manager`).

CAMUS se importa siempre desde su módulo concreto:

```python
from punto.orchestrator.camus import Camus   # correcto
```

Garantía verificada: **cada módulo principal se importa desde un intérprete
limpio**, en cualquier orden (ver `tests/test_cold_imports.py`).

---

## 3. Niveles de autoridad

| Nivel | Nombre | Significado |
| --- | --- | --- |
| 0 | `LEVEL_0_AUTONOMOUS` | Trabajo técnico reversible dentro de presupuesto y riesgo autorizado. Ejecución autónoma directa. |
| 1 | `LEVEL_1_AUTONOMOUS_REVIEW` | Cambios técnicos con **revisión posterior obligatoria**. |
| 2 | `LEVEL_2_CAMUS` | Decisiones arquitectónicas relevantes evaluadas por CAMUS. |
| 3 | `LEVEL_3_HUMAN` | Acciones irreversibles, de producción, legales o financieras. **Human Gate obligatorio.** |

**Default deny:** `default_authority_level: null` y `unknown_action_policy: DENY`.
Una acción ausente del catálogo se rechaza; nunca se le asigna nivel 0.

`never_autonomous` enumera las acciones que jamás se ejecutan de forma autónoma,
sea cual sea su riesgo declarado: `deploy_production`,
`production_database_delete`, `irreversible_delete`, `payment`,
`financial_action`, `legal_change`, `business_model_change`,
`master_secret_change`, `high_security_risk`.

---

## 4. Policy Engine

Orden de evaluación **fijo y auditable** (`punto/policy/policy_engine.py`):

1. **DEFAULT DENY** — acción no catalogada → rechazo inmediato.
2. **Protección constitucional** — modificar `config/constitution.yaml` o
   `config/permissions.yaml` → rechazo inmediato e inapelable, aunque la acción
   sea técnica, reversible y de riesgo LOW.
3. **Autoelevación de autoridad** — CAMUS no puede reescribir sus reglas de
   autoridad, presupuesto o riesgo → rechazo inmediato.
4. **Riesgo** — cálculo del riesgo efectivo (máximo entre declarado y calculado).
   HIGH/CRITICAL derivan al Human Gate.
5. **Presupuesto** — costo, tiempo y número de archivos dentro de los límites del
   nivel. Exceder cualquiera rechaza la acción.
6. **Autoridad** — decisión final según nivel (0 autónomo, 1 con revisión,
   2 decisión de CAMUS sujeta a lo anterior, 3 Human Gate).
7. **Regla de autonomía** — si una acción técnica y reversible no cumple las
   condiciones estrictas, no se ejecuta autónomamente.

### Regla de autonomía

```
technical AND reversible AND risk <= MEDIUM
AND dentro de presupuesto, tiempo y máximo de archivos
AND production_impact == false
AND legal_impact      == false
AND business_impact   == false
  => el motor continúa autónomamente según el Authority Level de la acción
```

---

## 5. Risk Engine

El riesgo efectivo es el **máximo** entre el riesgo declarado por quien solicita
y el riesgo calculado a partir de umbrales objetivos. Una acción **nunca** puede
reducir su riesgo declarándose conservadora.

Umbrales (`config/risk-rules.yaml`):

| Nivel | Costo máx. | Tiempo máx. | Archivos máx. | Human Gate |
| --- | --- | --- | --- | --- |
| LOW | 1.0 USD | 15 min | 5 | No |
| MEDIUM | 10.0 USD | 60 min | 20 | No |
| HIGH | 100.0 USD | 240 min | 100 | **Sí** |
| CRITICAL | por encima de HIGH | | | **Sí** |

Escaladores que fuerzan un riesgo mínimo: `production_impact → CRITICAL`,
`legal_impact → HIGH`, `business_impact → HIGH`, `irreversible → HIGH`,
`files_changed_on_protected_path → CRITICAL`, `unknown_action → CRITICAL`.

---

## 6. Presupuestos

Límites duros por nivel (`config/budgets.yaml`). Exceder cualquiera convierte la
acción en no autorizada.

| Nivel | Costo | Tiempo | Archivos | Intentos |
| --- | --- | --- | --- | --- |
| 0 | 1.0 USD | 15 min | 5 | 3 |
| 1 | 5.0 USD | 30 min | 15 | 3 |
| 2 | 25.0 USD | 120 min | 50 | 4 |
| 3 | 100.0 USD | 240 min | 100 | 5 |
| **Global** | **100.0 USD** | **240 min** | **100** | **5** |

Si la petición declara un presupuesto más estricto, **prevalece el más
restrictivo**. Sin margen de tolerancia (`allow_soft_warning_margin: 0.0`):
determinista y estricto. Cada exceso se bloquea con su `BlockedReason`:
`MAX_COST_EXCEEDED`, `MAX_TIME_EXCEEDED`, `MAX_FILES_CHANGED`,
`MAX_ATTEMPTS_EXCEEDED`.

---

## 7. Máquina de estados

La tabla `TRANSITION_TABLE` es la **autoridad única** de las transiciones
**normales**: no hay transiciones implícitas ni "cualquier estado a cualquier
estado".

```
NEW → ANALYZING → PLANNING → READY → IN_PROGRESS → QA → SECURITY → REVIEW → APPROVED → COMPLETED
```

Estados auxiliares: `REPAIRING`, `FAILED`, `BLOCKED`, `HUMAN_APPROVAL`,
`CANCELLED`. Terminales: `COMPLETED`, `CANCELLED`.

**Invariante constitucional: `NEW → COMPLETED` es imposible.** Una tarea debe
recorrer análisis, planificación, ejecución, QA, seguridad, revisión y
aprobación antes de completarse. Bloquear una tarea exige un motivo explícito
(`BlockedReason`).

### Transición normal vs. transición autorizada por Human Gate

Son dos cosas distintas y viven en **tablas separadas**:

| Tabla | Contenido | Quién puede invocarla |
| --- | --- | --- |
| `TRANSITION_TABLE` | Transiciones normales. Desde `HUMAN_APPROVAL` solo `BLOCKED` y `CANCELLED` (abortar). | `TaskManager.transition_task()` |
| `HUMAN_GATE_RESUME_TABLE` | Reanudaciones desde `HUMAN_APPROVAL`: `APPROVED`, `IN_PROGRESS`, `READY`, `REVIEW`. | `TaskManager.resume_from_human_approval()` **con autorización** |

Consecuencia: desde `HUMAN_APPROVAL` la vía genérica **solo permite abortar**.
Ninguna llamada genérica puede conseguir el efecto de una reanudación, ni
siquiera desde dentro del proceso. `StateMachine.assert_can_transition` distingue
ambos casos y lanza `HumanGateAuthorizationRequired` cuando el par es una
reanudación.

Los estados de reanudación válidos se declaran una sola vez, en
`punto.schemas.enums.HUMAN_GATE_RESUME_STATUSES`, y de ahí los consumen tanto la
máquina de estados como el Human Gate.

---

## 8. CAMUS (orquestador determinista)

CAMUS **no usa IA** en esta fase. Es un orquestador determinista que:

1. recibe un objetivo;
2. crea la tarea correspondiente;
3. consulta el Policy Engine;
4. avanza por estados válidos de la máquina de estados;
5. genera eventos de auditoría en cada paso;
6. se bloquea cuando corresponde;
7. genera un Human Gate cuando corresponde;
8. reanuda la tarea tras la resolución humana, **desde el estado autorizado**.

Resultados posibles (`CamusOutcome`):

| Outcome | Significado |
| --- | --- |
| `COMPLETED` | La tarea se ejecutó y completó autónomamente. |
| `HUMAN_APPROVAL_REQUIRED` | La tarea quedó esperando aprobación humana. |
| `REJECTED` | Default deny, archivo protegido o presupuesto excedido. |
| `BLOCKED` | La tarea quedó bloqueada por un motivo determinista. |

La acción catalogada `simulate_failure` produce siempre un fallo determinista:
existe para poder ejercitar y auditar la ruta de reparación (`REPAIRING`) sin
introducir aleatoriedad ni dependencias externas.

### Continuación tras un Human Gate

`Camus.resume()` no muta `status` ni usa `transition_task()` para salir de
`HUMAN_APPROVAL`: obtiene la autorización del gate y la aplica por la vía
protegida. La continuación la decide **una única rutina basada en el estado
actual** (`Camus._continue_after_authorization`), nunca una suposición fija, de
modo que una fase ya superada no se repite:

| Estado autorizado | Continuación |
| --- | --- |
| `IN_PROGRESS` | `IN_PROGRESS → QA → SECURITY → REVIEW → APPROVED → COMPLETED` |
| `SECURITY` | `SECURITY → REVIEW → APPROVED → COMPLETED` |
| `REVIEW` | `REVIEW → APPROVED → COMPLETED` |
| `READY` | `READY → IN_PROGRESS` y, desde ahí, la primera secuencia |

Nunca hay camino hacia atrás: **`REVIEW → QA` es imposible**.

### Validación placeholder (`DETERMINISTIC_PLACEHOLDER_VALIDATION`)

**ENGINE-0 no tiene agentes reales.** Los estados `QA`, `SECURITY` y `REVIEW` se
recorren mediante una **validación placeholder determinista**, encapsulada en
`Camus._run_placeholder_validation()` y `Camus._close_with_placeholder_review()`
y marcada con la constante `DETERMINISTIC_PLACEHOLDER_VALIDATION`.

Qué hace y qué **no** hace:

- **Sí**: comprueba que la máquina de estados admita la secuencia y que el riesgo
  efectivo no exija Human Gate.
- **No**: no ejecuta pruebas reales, no inspecciona artefactos y ningún revisor
  evalúa el resultado. No es QA real, ni Security real, ni Reviewer real.

La marca aparece en el motivo de cada transición simulada, de modo que la
auditoría distingue sin ambigüedad una validación simulada de una real. Los PASS
reales de QA, Security y Reviewer se implementarán en fases posteriores y
sustituirán a este placeholder.

---

## 9. Aprobación humana (Human Gate)

- Las acciones de nivel 3 y el riesgo HIGH/CRITICAL **no se ejecutan** sin
  aprobación explícita.
- Cada solicitud es `PENDING`, `APPROVED` o `REJECTED`; no puede resolverse dos
  veces (segunda resolución → error de dominio).
- Al aprobar, la tarea reanuda un estado coherente con su estado previo
  (`RESUMABLE_STATUSES`). Al rechazar, la tarea se cancela.
- `assert_executable` impide ejecutar una acción cuyo gate siga sin aprobar:
  `PENDING` y `REJECTED` **no** autorizan ejecución; solo `APPROVED` lo hace.
- **La resolución no se expone por HTTP en ENGINE-0.** La única vía autorizada es
  la lógica de dominio `Camus.resume()`. La API solo ofrece introspección `GET`.

### Aislamiento de la decisión (R1.2)

Cada `HumanApprovalRequest` conserva el `policy_decision_id` de la
`PolicyDecision` que originó el gate, y el `PolicyEngine` mantiene un índice
`decision_by_id()`. Al reanudar, `Camus.resume()` recupera **exclusivamente** esa
decisión.

```python
# Correcto: la decisión vinculada a ESTA solicitud.
decision = self._policy.decision_by_id(approval.policy_decision_id)

# Prohibido: la última decisión global pertenece a otra tarea si hay
# varias tareas concurrentes, y mezclaría sus datos.
decision = self._policy.decisions[-1]
```

Si una solicitud no está vinculada a ninguna decisión, `resume()` falla de forma
determinista y **sin efectos** (no hay respaldo por posición en el historial).

### Invariante de superficie (R1.1)

Ninguna ruta HTTP puede sacar una tarea de `HUMAN_APPROVAL`. El endpoint genérico
de transiciones fue **eliminado**: un estado solo cambia a través del dominio.

### Invariante de dominio (R2.1)

La garantía **no** depende de que la API esté reducida: también es imposible
saltarse el Human Gate **desde dentro del proceso**.

```
HumanGate.authorize_resume()        comprueba APPROVED y emite la autorización
        ↓
HumanApprovalProof                  objeto inmutable, no fabricable
        ↓
TaskManager.resume_from_human_approval()
        ↓
StateMachine.resume_transition()    única vía hacia un estado de continuación
```

- `HumanApprovalProof` lleva exactamente `approval_id`, `task_id`,
  `policy_decision_id` y `resume_status`. Un centinela privado en su
  construcción hace que **no pueda fabricarse** fuera del gate.
- `PENDING` y `REJECTED` no producen autorización: `authorize_resume()` delega en
  `assert_executable()` y falla antes de emitir nada.
- El destino lo fija la **solicitud**, no quien reanuda: `resume_from_human_approval()`
  no acepta un estado destino como parámetro. Redirigir la reanudación es
  imposible por construcción.
- Una autorización emitida para TASK-A no puede aplicarse a TASK-B: se verifica
  el `task_id` tanto al emitirla como al consumirla.
- `TaskManager.transition_task()` lanza `HumanGateAuthorizationRequired` si se
  intenta cualquier reanudación desde `HUMAN_APPROVAL`.

```python
# Correcto: autorización emitida por el gate y aplicada por la vía protegida.
authorization = human_gate.authorize_resume(approval.id, task_id=task.id)
task = task_manager.resume_from_human_approval(task.id, authorization=authorization)

# Imposible: la vía genérica no puede reanudar.
task_manager.transition_task(task.id, TaskStatus.APPROVED)
# -> HumanGateAuthorizationRequired
```

---

## 10. Auditoría

`AuditLogger` es **append-only** y en memoria. Cada evento es inmutable (los
metadatos se congelan con `deep_freeze`), está ordenado y lleva actor, acción,
recurso y resultado.

Eventos mínimos obligatorios (`REQUIRED_EVENT_TYPES`):

`TASK_CREATED`, `TASK_TRANSITION`, `TASK_BLOCKED`, `POLICY_DECISION`,
`HUMAN_GATE_CREATED`.

Además: `TASK_COMPLETED`, `TASK_CANCELLED`, `HUMAN_GATE_RESOLVED`,
`HUMAN_GATE_RESUME_AUTHORIZED`, `ACTION_EXECUTED`.

### Trazabilidad de la cadena constitucional

`HUMAN_GATE_RESUME_AUTHORIZED` se registra en el punto de enforcement
(`TaskManager.resume_from_human_approval`) y lleva `task_id`,
`policy_decision_id`, `from_status` y `resume_status`. Con él, la auditoría
permite reconstruir la cadena completa sin persistencia externa:

```
tarea → gate → decisión de política → aprobación → autorización de reanudación → estado retomado
```

| Paso | Evento | Recurso |
| --- | --- | --- |
| tarea | `TASK_CREATED` | `task` |
| decisión | `POLICY_DECISION` (+ `policy_decision_id`) | `task` |
| gate | `HUMAN_GATE_CREATED` (+ `task_id`) | `human_approval` |
| aprobación | `HUMAN_GATE_RESOLVED` | `human_approval` |
| autorización | `HUMAN_GATE_RESUME_AUTHORIZED` (+ `policy_decision_id`) | `human_approval` |
| estado retomado | `TASK_TRANSITION` (`HUMAN_APPROVAL` → `resume_status`) | `task` |

---

## 11. API HTTP

Superficie pública **deliberadamente reducida**. ENGINE-0 no expone ninguna ruta
capaz de manipular el estado de una tarea ni de resolver un Human Gate.

| Método | Ruta | Descripción |
| --- | --- | --- |
| `GET` | `/health` | Estado del motor y versión. |
| `GET` | `/engine` | Entorno, contadores e informe de integridad constitucional. |
| `POST` | `/tasks` | Crea una tarea y la procesa con CAMUS (`201`). |
| `GET` | `/tasks` | Lista tareas en memoria (filtros `status`, `project_id`, `limit`). |
| `GET` | `/tasks/{task_id}` | Detalle de una tarea (solo lectura). |
| `GET` | `/human-gate` | Introspección de solicitudes de aprobación (`pending_only`). |
| `GET` | `/human-gate/{approval_id}` | Detalle de una solicitud (solo lectura). |
| `POST` | `/policy/evaluate` | Evalúa una acción sin crear tarea ni ejecutar nada. |
| `GET` | `/policy/authority` | Catálogo de autoridad activo por nivel. |
| `GET` | `/audit/events` | Eventos de auditoría (`limit`, `resource_id`). |

Documentación interactiva en `/docs`; esquema OpenAPI en `/openapi.json`.

### Rutas eliminadas en ENGINE-0.R1

| Ruta eliminada | Motivo |
| --- | --- |
| `POST /tasks/{task_id}/transitions` | Permitía forzar `HUMAN_APPROVAL → APPROVED` saltándose el Human Gate. |
| `POST /human-gate/{approval_id}/resolve` | Mutaba el gate sin pasar por la lógica de dominio; el gate no necesita interfaz externa en esta fase. |

Ninguna de las dos existe ya: responden `404`/`405` según el enrutado, y el
estado de la tarea permanece intacto. La única vía autorizada para resolver un
Human Gate es `Camus.resume()`, que exige un gate `APPROVED` y recupera la
decisión vinculada a esa solicitud concreta.

### Códigos de error

| Código | Significado |
| --- | --- |
| `404` | Tarea, solicitud de aprobación o ruta inexistente. |
| `405` | Método no permitido sobre una ruta de solo lectura. |
| `409` | Conflicto de dominio. |
| `422` | Petición inválida (validación de esquema). |
| `503` | Configuración del motor no disponible. |

Una tarea bloqueada o pendiente de aprobación **no** es un error HTTP: la tarea
se creó correctamente y su estado se informa en el campo `outcome`.

---

## 12. Instalación y uso

### Requisitos

Python 3.12 o superior. En Windows, si `python` apunta al alias de Microsoft
Store, use la ruta real del intérprete.

### Crear el entorno virtual

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

En Linux/macOS:

```bash
python3.12 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e ".[dev]"
```

### Levantar la API

```powershell
.\.venv\Scripts\python.exe -m uvicorn punto.api.app:app --host 127.0.0.1 --port 8000
```

```bash
./.venv/bin/python -m uvicorn punto.api.app:app --host 127.0.0.1 --port 8000
```

Verificación en vivo:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
# status  engine           version
# ------  ---------------  -------
# ok      PUNTO AI ENGINE  0.1.0
```

### Ejemplo de uso

```powershell
$body = @{
  objective        = "Crear el modulo de facturacion"
  action           = "create_file"
  files_changed    = @("src/punto/billing.py")
  risk_level       = "LOW"
  technical        = $true
  reversible       = $true
  estimated_cost   = 0.5
  estimated_minutes = 5
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/tasks `
  -ContentType "application/json" -Body $body
```

Una acción de nivel 3 (por ejemplo `deploy_production`) devuelve
`outcome = "HUMAN_APPROVAL_REQUIRED"` junto con un `human_approval_id`. La
aprobación de esa tarea se realiza por dominio (`Camus.resume()`), no por HTTP:

```python
from punto.api.app import Engine

engine = Engine(environment="local")
result = engine.camus.process_request(objective="Desplegar", action="deploy_production")
assert result.human_approval is not None
resumed = engine.camus.resume(result.human_approval.id, approved=True, resolved_by="carlos")
assert resumed.task.status.value == "COMPLETED"
```

---

## 13. Calidad: pruebas, lint y tipado

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
```

Configuración declarada en `pyproject.toml`:

- **pytest** — `testpaths = ["tests"]`, `pythonpath = ["src"]`,
  `--strict-markers --strict-config`, y las advertencias se tratan como errores
  (con excepciones acotadas y documentadas para dependencias de terceros).
- **ruff** — `line-length = 100`, `target-version = "py312"`, con las reglas
  `E, W, F, I, N, UP, B, A, C4, SIM, RUF`.
- **mypy** — `strict = true`, `warn_unreachable`, `warn_unused_ignores`,
  `show_error_codes`. Solo se relaja `disallow_untyped_decorators` para los
  decoradores de ruta de FastAPI.

La suite ejercita el **motor real** (YAML reales, código real), sin dobles de
prueba, y cubre los casos numerados del contrato constitucional:

| Caso | Verifica |
| --- | --- |
| 1 | Nivel 0 técnico, reversible y LOW es autónomo. |
| 2 | Nivel 1 exige revisión. |
| 3 | Nivel 3 exige humano. |
| 4–5 | Riesgo HIGH/CRITICAL exige Human Gate. |
| 6 | `NEW → ANALYZING` es válido. |
| 7 | `NEW → COMPLETED` es inválido. |
| 8 | Creación de tarea. |
| 9 | Bloqueo de tarea. |
| 10 | Generación de evento de auditoría. |
| 11–12 | `/health` y `POST /tasks` funcionan. |
| 13–14 | Constitución y permisos están protegidos. |
| 15 | Default deny para acción desconocida. |
| 16–18 | Presupuesto de costo, tiempo y archivos. |

Además, `tests/test_human_gate_invariants.py` (ENGINE-0.R1) sostiene las
garantías constitucionales del gate:

| Invariante | Verifica |
| --- | --- |
| R1.1 | Ningún endpoint genérico permite sacar una tarea de `HUMAN_APPROVAL`. |
| R1.1 | El endpoint de transiciones y el de resolución de gate ya no existen (`404`/`405`). |
| R1.2/R1.3 | TASK-A se reanuda con **su** decisión aunque TASK-B haya emitido otra después. |
| R1.2 | Sin vínculo de decisión, `resume()` falla sin efectos (no hay respaldo por posición). |
| R1.4 | `PENDING` no autoriza ejecución. |
| R1.4 | `REJECTED` no autoriza ejecución. |
| R1.4 | `APPROVED` sí autoriza la reanudación. |
| R1.4 | La aprobación de TASK-A no autoriza ni altera TASK-B. |
| R1.5 | La validación simulada queda marcada como placeholder en la auditoría. |

`tests/test_domain_hardening.py` (ENGINE-0.R2) cierra el invariante en el dominio:

| Invariante | Verifica |
| --- | --- |
| R2.1 | `transition_task()` genérico no puede salir de `HUMAN_APPROVAL` a ningún destino de continuación. |
| R2.1 | Desde `HUMAN_APPROVAL` la vía genérica solo permite abortar (`BLOCKED`/`CANCELLED`). |
| R2.1 | Una autorización no puede fabricarse fuera del Human Gate. |
| R2.1 | `PENDING` no produce autorización de reanudación. |
| R2.1 | `REJECTED` no produce autorización de reanudación. |
| R2.1 | Una aprobación válida sí autoriza la reanudación por la vía protegida. |
| R2.1 | Una autorización de TASK-A no sirve para TASK-B. |
| R2.1 | El destino de reanudación no puede redirigirse. |
| R2.3 | Un gate de nivel 3 reanuda en `IN_PROGRESS` y recorre las fases restantes. |
| R2.4 | Un gate creado desde `SECURITY` reanuda en `REVIEW` y termina en `COMPLETED`, sin `REVIEW → QA`. |
| R2.7 | La auditoría reconstruye tarea → gate → decisión → aprobación → autorización → estado retomado. |

`tests/test_cold_imports.py` (ENGINE-0.R3) vigila la higiene de importaciones.
Cada caso se ejecuta en un **subproceso Python nuevo**, porque dentro del mismo
intérprete el orden de imports previos ocultaría el ciclo:

| Invariante | Verifica |
| --- | --- |
| R3 | Cada módulo principal se importa desde un intérprete limpio (exit code 0). |
| R3 | `tasks.manager → orchestrator.camus` funciona en un proceso nuevo. |
| R3 | `orchestrator.camus → tasks.manager` funciona en un proceso nuevo. |
| R3 | Todos los módulos, en orden inverso y en un proceso nuevo, importan sin fallar. |
| R3 | `punto.orchestrator` no carga `camus` ni `tasks.manager` de forma eager. |

`tests/test_workspace_isolation.py`, `test_shell_policy.py`, `test_git_workspace.py`,
`test_validator.py` y `test_developer_runner.py` (ENGINE-1) verifican la capa de
ejecución con operaciones **reales** (archivos reales, Git real, `pytest` real),
sin sustituir las operaciones centrales por mocks:

| Invariante | Verifica |
| --- | --- |
| §25.1-2 | Leer y escribir dentro del workspace funciona. |
| §25.3-5 | `..`, ruta absoluta externa y escape por enlace (junction/symlink) se bloquean. |
| §25.6-7 | `config/constitution.yaml` y `config/permissions.yaml` se bloquean y no se modifican. |
| §25.8-9 | Comando allowlisted se ejecuta; desconocido se deniega por default deny. |
| §25.10 | No se usa `shell=True` (verificado por AST) y los metacaracteres no se interpretan. |
| §25.11-13 | Timeout detectado; `stdout` y `stderr` capturados y nunca ocultos. |
| §25.14-15 | No se escribe en `main`; la rama `ai/...` se crea correctamente. |
| §25.16-18 | `git diff`, `git diff --stat` y `git commit` funcionan. |
| §25.19 | Misión 1 (`hello.txt`) completa: archivo, contenido exacto, rama y commit. |
| §25.20 | Misión 2 (`add()` + test) pasa `pytest` y commitea. |
| §25.21-22 | El Validator falla si un check falla y solo pasa si todos se ejecutan bien. |
| §25.23-24 | `max_files_changed` y `max_execution_minutes` producen BLOCKED/TIMEOUT. |
| §25.25-26 | No existe ruta de `git push` ni de mutación de remotos. |
| §25.27 | Coste = 0.00 USD. |
| §25.28 | La ejecución deja traza de auditoría completa ligada al `task_id`. |
| §25.29-30 | `DeveloperRunner` es inyectable; CAMUS sin runner conserva ENGINE-0. |
| §25.31 | Cold imports de todos los módulos nuevos en subprocesos independientes. |

`tests/test_execution_trust.py` (ENGINE-1.R1) sostiene la frontera de confianza:

| Invariante | Verifica |
| --- | --- |
| §15.1 | `TRUSTED_LOCAL` + `LocalDeveloperRunner` sigue funcionando. |
| §15.2 | `UNTRUSTED_MODEL` + `TrustedLocalBackend` → BLOCK (incluso llamando al backend directamente). |
| §15.3 | Runner que genera código con IA + contexto `TRUSTED_LOCAL` → BLOCK. |
| §15.4 | Runner de IA sin sandbox → BLOCK. |
| §15.5 | No existe degradación sandbox → local. |
| §15.6 | Un secreto del proceso padre no llega al hijo. |
| §15.7 | El `PATH` del hijo se reconstruye, no se hereda. |
| §15.8/§15.10 | `python` y `pytest` funcionan en ejecución local confiable. |
| §15.9/§15.11 | `python` y `pytest` quedan bloqueados para el backend local con trabajo no confiable. |
| §15.12 | Capacidades incompletas no acreditan `UNTRUSTED_MODEL`. |
| §15.13 | Capacidades completas sí lo acreditan. |
| §15.14 | La auditoría no registra valores secretos. |
| §15.16 | Cold imports de los módulos nuevos. |

---

## 14. Configuración

Todos los archivos viven en `config/` y se cargan una sola vez, de forma
síncrona, sin red. Un archivo ausente o inválido es un **error duro**: el motor
nunca arranca con configuración parcial (si falta la configuración, `/health`
responde `503`).

| Archivo | Contenido | Protegido |
| --- | --- | --- |
| `constitution.yaml` | Principios no negociables, prohibiciones y reglas duras. | **Sí** |
| `permissions.yaml` | Catálogo de autoridad, acciones y autoelevación. | **Sí** |
| `risk-rules.yaml` | Niveles, umbrales y escaladores de riesgo. | **Sí** |
| `budgets.yaml` | Límites por nivel y política de bloqueo. | **Sí** |
| `environments.yaml` | Entornos lógicos e integraciones (todas deshabilitadas). | **Sí** |
| `models.yaml` | Contrato de modelos y agentes (inerte en ENGINE-0). | No |

Resolución del directorio de configuración, en orden:

1. `$PUNTO_CONFIG_DIR`;
2. búsqueda ascendente desde `punto/policy/config_loader.py` hasta encontrar un
   directorio con `config/constitution.yaml`;
3. `$PUNTO_REPO_ROOT`;
4. `./config` relativo al directorio de trabajo.

Variables de entorno disponibles: ver `.env.example`. Copiar a `.env` solo si se
necesita sobreescribir la configuración por defecto.

---

## 15. Restricciones del motor (ENGINE-0 / ENGINE-1)

Explícitas y verificadas, no aspiracionales:

- **Sin IA.** `llm_enabled: false` y `model_router_enabled: false`. El Model
  Router está prohibido. ENGINE-1 no conecta ningún modelo: su ejecutor es
  determinista.
- **Sin integraciones externas.** Todas deshabilitadas en
  `config/environments.yaml`: OpenAI, Neon, GitHub API, Vercel, Redis, Temporal.
- **Sin persistencia externa.** Todo vive en memoria; se pierde al reiniciar el
  proceso.
- **Sin secretos de terceros.** ENGINE-0 no requiere ninguna clave.
- **Sin red.** Ninguna ruta de código realiza llamadas de red.
- **Un solo entorno activo:** `local` (y `test` para la suite). `staging` y
  `production` existen como contrato declarativo y están desconectados.

El endpoint `GET /engine` expone este estado de forma verificable:
`default_deny: true`, `llm_enabled: false`, `external_integrations: []`,
`constitutional_floor: true`.

---

## 16. Alcance y hoja de ruta

**ENGINE-0 entrega:** el núcleo constitucional determinista con autoridad,
política, riesgo, presupuesto, Human Gate, máquina de estados, auditoría, CAMUS
y una API HTTP mínima, con su suite de pruebas. **Cerrada.**

**ENGINE-1 entrega:** la capa de ejecución controlada descrita en la sección 17.
**No entrega** ningún agente inteligente, ninguna conexión externa y ninguna
persistencia.

Las fases posteriores habilitarán capacidades de forma progresiva y siempre bajo
aprobación humana, según lo declarado en `config/environments.yaml`:
`github_api` y `vercel` (ENGINE-1), LLM y Model Router (ENGINE-2), Neon, Redis y
Temporal (ENGINE-3).

**ENGINE-2 será DEEPSEEK DEVELOPER INTEGRATION.** La integración del modelo se
hará implementando `DeepSeekDeveloperRunner` sobre la interfaz `DeveloperRunner`
que ya existe, sin tocar el núcleo, el Policy Engine ni el Human Gate.

---

## 17. ENGINE-1 — Controlled Developer Execution Layer

Capa segura capaz de ejecutar trabajo **real** sobre un repositorio local de
prueba, todavía **sin IA externa**.

```
CAMUS
  ↓
DeveloperRunner            interfaz abstracta, provider-agnostic
  ↓
ExecutionContext           workspace, rama, comandos, confianza y límites
  ↓
Tool Layer
  ├── Filesystem
  ├── Shell
  ├── Git
  └── Validator
        ↓
   ExecutionBackend        ENGINE-1.R1: frontera de confianza
        ├── TrustedLocalBackend   (host; solo TRUSTED_LOCAL)
        └── SandboxedBackend      (aislado; obligatorio para UNTRUSTED_MODEL)
```

ENGINE-1 implementa `LocalDeveloperRunner` (determinista, sin IA). ENGINE-2
añadirá `DeepSeekDeveloperRunner` sobre la misma frontera:

```
CAMUS → DeveloperRunner → DeepSeekDeveloperRunner → DeepSeek → Tool Layer
```

### Componentes

| Componente | Módulo | Responsabilidad |
| --- | --- | --- |
| `DeveloperRunner` | `punto/developer/base.py` | Interfaz abstracta `execute(task, context) -> DeveloperExecutionResult`. No conoce ningún proveedor. |
| `LocalDeveloperRunner` | `punto/developer/local.py` | Ejecutor determinista de recetas. **No genera código con IA.** |
| `ExecutionContext` | `punto/developer/context.py` | Autoridad de seguridad: resuelve el workspace, confina rutas, protege los archivos constitucionales y bloquea escrituras fuera de `ai/`. |
| `FilesystemTool` | `punto/tools/filesystem.py` | `read_text`, `write_text`, `create_file`, `replace_text`, `list_files`, `delete_file`. Verifica por relectura. |
| `ShellRunner` | `punto/tools/shell.py` | Comandos estructurados, allowlist, **default deny**, `shell=False`, timeout determinista. |
| `GitWorkspace` | `punto/tools/git.py` | `status`, `current_branch`, `create_branch`, `diff`, `diff_stat`, `add`, `commit`, `head_sha`. **Sin remoto.** |
| `Validator` | `punto/tools/validator.py` | Ejecuta los checks declarados; PASS solo si todos se ejecutaron de verdad. |
| `DeveloperExecutionResult` | `punto/schemas/execution.py` | Evidencia estructurada: estado, workspace, rama, archivos, comandos, validación y commit. |

### Flujo de una tarea

```
recibir tarea estructurada
→ preparar workspace (copia del fixture)
→ crear rama ai/<task-id>-<slug>       (nunca main)
→ modificar archivos                   (confinados al workspace)
→ ejecutar comandos                    (allowlist)
→ ejecutar tests                       (Validator)
→ validar resultado
→ crear commit local
→ producir evidencia estructurada
```

### Seguridad del workspace

- **Ninguna herramienta opera fuera de `workspace_path`.** La comprobación se
  hace sobre la ruta **resuelta** (`Path.resolve()`), de modo que detecta `..`,
  rutas absolutas externas y escapes por enlace —junction en Windows o symlink en
  POSIX—. No se compara texto sin resolver.
- Los archivos constitucionales (`config/constitution.yaml`,
  `config/permissions.yaml`) se bloquean **con independencia del workspace**, por
  ruta normalizada y por nombre base.
- El tool de archivos no escribe dentro de `.git`.
- `GitWorkspace` verifica que el repositorio sea exactamente el workspace: un
  workspace anidado en otro repositorio no puede operar sobre el padre.

### Seguridad del shell

- **Nunca `shell=True`**: los comandos son listas de argumentos, sin
  interpretación de metacaracteres.
- **Default deny** con allowlist: `python`, `pytest`, `ruff`, `mypy`, `git`.
- Prohibidos explícitamente: `powershell`, `pwsh`, `cmd`, `bash`, `sh`, `curl`,
  `wget`, `ssh`, `scp`, `reg`, `format`, `shutdown` y otros.
- No se admiten rutas de ejecutable (solo nombres), y `python` se resuelve al
  intérprete actual para no depender del `PATH`.
- `git` no puede hablar con un remoto (`push`, `fetch`, `pull`, `clone`,
  `remote`), ni reconfigurarse (`config`), ni redirigir el repositorio (`-C`,
  `--git-dir`, `--work-tree`).
- Timeout determinista por comando; `stdout` y `stderr` se capturan siempre y el
  `stderr` nunca se oculta.

### Seguridad de Git

- Cada tarea trabaja en `ai/<task-id>-<slug>`.
- Escribir código en `main`/`master` está **bloqueado**. Crear la rama de tarea
  desde `main` sí está permitido: lo prohibido es escribir en `main`.
- **No existe** ninguna operación de remoto en la API de `GitWorkspace`. No es que
  esté deshabilitada: no está implementada, y la política de shell la rechaza
  igualmente por la vía del comando.

> **Nota sobre niveles.** El push de *este* repositorio PUNTO AI ENGINE a `main`
> lo realiza el ejecutor técnico de la sesión de desarrollo, no el
> `DeveloperRunner` como capacidad del motor. Son dos niveles distintos.

### Autoridad

`DeveloperRunner` **no decide** si una acción está permitida. La jerarquía es:

```
Policy Engine → CAMUS → DeveloperRunner
```

El runner rechaza lo que viole restricciones técnicas, pero **nunca eleva
permisos**. No puede ejecutar acciones de nivel 3, desplegar, pagar, cambiar el
modelo de negocio, rotar secretos maestros, modificar la constitución ni
autoelevar autoridad.

### Integración con CAMUS (opt-in)

`DeveloperRunner` es una **dependencia inyectable**. Si no se inyecta, CAMUS se
comporta exactamente como en ENGINE-0:

```python
from punto.developer.local import LocalDeveloperRunner

camus = Camus(..., developer_runner=LocalDeveloperRunner(audit=audit))
resultado = camus.execute_developer_task(task, context)
```

La API HTTP **no** expone la capa de desarrollo: no existe `POST /developer/run`
ni `POST /execute`. La superficie HTTP sigue siendo la de ENGINE-0.

### Límites y coste

Se aplican `max_files_changed`, `max_execution_minutes` y `max_attempts`. Superar
un límite produce `BLOCKED` (o `TIMEOUT` cuando la causa es el tiempo), nunca una
continuación silenciosa.

El coste es **0.00 USD**: `LocalDeveloperRunner` no consume ningún modelo externo.

### Validación placeholder

ENGINE-1 introduce un **Validator real** para los checks declarados en la receta
(`pytest`, `ruff`, `mypy`…). No obstante, los estados `QA`, `SECURITY` y `REVIEW`
del flujo de CAMUS siguen siendo `DETERMINISTIC_PLACEHOLDER_VALIDATION`: ENGINE-1
valida la máquina de estados y los checks declarados, pero **no** incorpora
todavía agentes reales de QA, seguridad o revisión. Llegarán en fases posteriores.

### Auditoría de ejecución

Eventos añadidos (no forman parte de `REQUIRED_EVENT_TYPES`, para no romper el
contrato de ENGINE-0): `DEVELOPER_RUN_STARTED`, `FILE_CHANGED`,
`COMMAND_EXECUTED`, `COMMAND_BLOCKED`, `VALIDATION_COMPLETED`,
`GIT_COMMIT_CREATED`, `DEVELOPER_RUN_COMPLETED`, `DEVELOPER_RUN_FAILED`,
`DEVELOPER_RUN_BLOCKED`.

Todos se registran sobre el `task_id`, de modo que
`AuditLogger.by_resource(task_id)` reconstruye la ejecución completa con el
workspace, la acción y el resultado de cada paso.

### Limitaciones residuales (declaradas)

No se pretende construir un sandbox de sistema operativo. La allowlist admite
`python`, y `python -c "<código>"` puede ejecutar código arbitrario: ENGINE-1
impide la **ejecución irrestricta accidental**, no el abuso deliberado desde
dentro del proceso. Un aislamiento fuerte requeriría contenedores o un usuario
sin privilegios, fuera del alcance de esta fase.

---

## 18. ENGINE-1.R1 — Untrusted Execution Boundary

`ShellRunner` ejecuta en el host mediante `subprocess`. Aunque use `shell=False`,
confine rutas y aplique una allowlist, **no es un sandbox**: `python` y `pytest`
ejecutan código, y ese código podría leer archivos del usuario, variables de
entorno, tokens del proceso o abrir sockets. Antes de conectar un modelo hay que
separar dos cosas que hasta ahora eran la misma.

### Niveles de confianza

| Nivel | Origen | Backend admitido |
| --- | --- | --- |
| `TRUSTED_LOCAL` | Trabajo determinista declarado por nosotros | `TrustedLocalBackend` (host) |
| `UNTRUSTED_MODEL` | Código originado por un modelo externo | **Solo** un `SandboxedBackend` apto |

`ExecutionContext.trust_level` declara el nivel. El valor por defecto es
`TRUSTED_LOCAL`, que es correcto para las recetas deterministas de
`LocalDeveloperRunner`; un runner que genere código con IA **no** puede confiar en
ese valor, porque el *enforcement* le obliga a exigir `UNTRUSTED_MODEL`.

```
DeveloperRunner.resolve_backend(context, backend)
        │
        ├── generates_code_with_ai = True  → exige UNTRUSTED_MODEL + SandboxedBackend
        │                                     si falta → BLOCK (SANDBOX_REQUIRED)
        └── generates_code_with_ai = False → exige TRUSTED_LOCAL
```

### Backends

| Backend | Aislamiento | Niveles | Estado |
| --- | --- | --- | --- |
| `TrustedLocalBackend` | **Ninguno**, declarado honestamente | Solo `TRUSTED_LOCAL` | Implementado |
| `SandboxedBackend` | Los cuatro aislamientos exigidos | Ambos | **Contrato, sin implementación real** |

`TrustedLocalBackend` **rechaza** `UNTRUSTED_MODEL` antes de mirar la allowlist: la
denegación no depende de qué comando sea. La comprobación vive en el backend, así
que tampoco se esquiva llamándolo directamente.

**No existe degradación** de sandbox a ejecución local. Si se requiere
aislamiento y no hay ninguno apto: `SandboxUnavailableError` / `SandboxRequiredError`
y `BLOCKED` con razón `SANDBOX_REQUIRED`. No se finge un sandbox que no existe.

### Capacidades del sandbox

`SandboxCapabilities` exige **los cuatro** para acreditar trabajo no confiable:

| Aislamiento | Requerido |
| --- | --- |
| `filesystem_isolated` | ✅ |
| `environment_isolated` | ✅ |
| `network_isolated` | ✅ |
| `process_isolated` | ✅ |

Si falta uno, `satisfies_untrusted()` es `False`, `assert_sandbox_capabilities()`
falla y un backend concreto no puede declararse sandbox.

Sobre la red: `TrustedLocalBackend` declara `network_isolated: False` porque **no
puede** garantizarlo a nivel de sistema operativo. Bloquear `curl`/`wget` por
allowlist no impide que Python abra un socket, así que no se presenta como
protección. `ExecutionContext.network_access` es `False` por defecto, y un
contexto `UNTRUSTED_MODEL` no puede declararlo en `True` (falla en construcción).

### Entorno del proceso hijo

Se eliminó por completo la herencia de `dict(os.environ)`. El entorno se
construye con **allowlist**:

- se copian solo `SYSTEMROOT`, `SYSTEMDRIVE`, `WINDIR`, `PATHEXT`, `COMSPEC`,
  `LANG`, `LC_ALL`, `TZ` (las que existan);
- `PATH` se **reconstruye**: directorio del ejecutable + directorios del sistema;
- `TEMP`/`TMP` se **redirigen** a una zona propia (`punto-exec-*`);
- se fijan `PYTHONIOENCODING=utf-8` y `PYTHONDONTWRITEBYTECODE=1`.

Los patrones sensibles (`*_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`,
`DATABASE_URL`, `AWS_*`, `AZURE_*`, `GOOGLE_*`, `SSH_*`) son una **segunda
barrera**: aunque una variable estuviera en la allowlist por error, no se
propaga. La defensa principal es la allowlist, no la lista negra.

### Detección de runtimes de contenedor

`detect_container_runtimes()` informa de `docker_available` y `podman_available`
usando solo `shutil.which` (no ejecuta nada ni modifica el sistema). Servirá para
elegir la implementación real del sandbox más adelante.

### Alcance de lo que protegen los guards de filesystem

Los guards de workspace (traversal, rutas absolutas, enlaces, archivos
constitucionales, `.git`) protegen las **llamadas a las herramientas**. **No**
garantizan que código Python ejecutado localmente no abra directamente
`C:\Users\...` o `/home/...`. Esa garantía corresponde al `SandboxedBackend`, y
por eso el trabajo no confiable no se ejecuta en el host.

### Invariante para ENGINE-2

`DeepSeekDeveloperRunner` declarará `generates_code_with_ai = True`, y con ello
quedará obligado por código a: exigir `UNTRUSTED_MODEL`, requerir un
`SandboxedBackend` apto, y devolver `BLOCKED` con razón `SANDBOX_REQUIRED` si no
lo hay. Sin excepciones y sin fallback.

---

## 19. ENGINE-1.R3 — Sandbox real (WSL2 + Podman)

ENGINE-1.R1 dejó la frontera, pero **sin sandbox**: `SandboxedBackend` era solo un
contrato y el trabajo no confiable quedaba bloqueado. ENGINE-1.R3 entrega la
implementación real y **demuestra** los cuatro aislamientos.

### Sustrato

| Componente | Valor |
| --- | --- |
| Runtime | **Podman** 5.8.3 (Apache-2.0, rootless, daemonless) |
| Backend | **WSL2** — kernel 6.18.33.2, versión predeterminada 2 |
| Almacenamiento | VHDX en **`D:\wsl\podman-machine-default`** (movido con `wsl --manage --move`) |
| Imagen | **`localhost/punto-sandbox-python:0.1`** (345 MB) |
| Recursos | 2 CPU, 4 GB RAM, 30 GB disco |

**Podman y WSL son dependencias locales de desarrollo.** No forman parte del
código; el motor las detecta y **falla cerrado** si no están.

### Verificación de capacidades (no se declara, se demuestra)

`ContainerSandboxBackend` nace **NO VERIFICADO**: `capabilities` no acredita nada
hasta que `verify_capabilities()` ejecuta **sondas reales** dentro de contenedores:

| Sonda | Qué demuestra |
| --- | --- |
| `probe_filesystem` | El workspace montado es accesible; `/mnt/c`, `/mnt/d`, `/Users`… **no existen**; la raíz es de solo lectura |
| `probe_network` | TCP, UDP y DNS **fallan** realmente (no basta con que falte `curl`) |
| `probe_environment` | La variable canario del host **no** llega al contenedor |
| `probe_process` | Solo se ven los procesos del contenedor; ningún proceso de Windows |
| `probe_hardening` | Capacidades vacías, `NoNewPrivs=1`, límites de cgroups aplicados |

Si una sonda falla: `SandboxUnavailableError` y el backend queda **NOT READY**.
La verificación se guarda con la **huella** del runtime (versión e imagen): si la
huella cambia, se vuelve a verificar en lugar de arrastrar un PASS caducado.

Las sondas viven **dentro de la imagen** (`/opt/punto/probes/`), así que la
verificación versiona con ella y no necesita un segundo montaje.

### Modelo de aislamiento

Cada ejecución usa exactamente estos parámetros:

```
--network none
--read-only
--cap-drop ALL
--security-opt no-new-privileges
--pids-limit 128 --memory 512m --cpus 1
--tmpfs /tmp:rw,size=64m
--user 10001:10001
--rm --name punto-sbx-<id> --label punto.sandbox=1
--mount type=bind,source=<workspace de la tarea>,target=/workspace,rw
```

**Nunca** se usa `--privileged`, `--pid host`, `--network host`, `--ipc host`,
`--uts host`, `--device` ni el socket del runtime. El **único** montaje es el
workspace de la tarea.

### Red

`--network none` en toda ejecución de código. Ninguna sonda de red puede
establecer una conexión, resolver un nombre ni enviar un datagrama.

### Modelo de secretos

`DEEPSEEK_API_KEY` (cuando llegue ENGINE-2) vivirá **solo** en el proceso que
llama al modelo. **No** entra al sandbox: `ContainerSandboxBackend` nunca pasa
secretos con `-e`, y el contenedor solo recibe un entorno explícito y mínimo.
**No** se escribe en el repositorio, **no** aparece en los logs de auditoría y
**no** se envía al código generado. Hay pruebas que lo verifican con canarios.

### Recuperar el runtime y diagnosticar

```powershell
# ¿Está Podman instalado?
where.exe podman
podman --version

# ¿Está la máquina en ejecución?
podman machine inspect --format "{{.State}}"

# Si está detenida, el motor BLOQUEA. Recupérala con:
podman machine start

# Si la imagen no existe:
podman build -t punto-sandbox-python:0.1 sandbox/

# Ver la configuración efectiva
podman machine inspect
podman info --format "{{.Host.CPUs}} {{.Host.MemTotal}}"
```

Los límites de la VM viven en `%USERPROFILE%\.wslconfig` (`processors=2`,
`memory=4GB`): sin ese archivo, WSL2 ignora lo que declara `podman machine init`.

**Comprobación de extremo a extremo:**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_sandbox_backend.py -q
```

### Limitación declarada

La **máquina** WSL2 ve el sistema de archivos del host (`/mnt/c`, `/mnt/d`) porque
el automount de WSL2 es lo que permite traducir las rutas del host al montar el
workspace. El **contenedor no** los ve: esa es la frontera que importa, y está
probada. En consecuencia, el runtime de Podman es parte de la base de confianza.

### Cierre de fronteras (ENGINE-1.R3.1)

Dos endurecimientos que no dependen del aislamiento del contenedor:

**1. El cliente de Podman tampoco hereda el host.** El proceso hijo de la CLI es
también un proceso hijo: si heredara el entorno, `DEEPSEEK_API_KEY` o
`GITHUB_TOKEN` viajarían con él. `build_runtime_client_environment()` construye un
entorno **mínimo por allowlist** para **todas** las invocaciones del runtime. No se
usa `dict(os.environ)`, ni `os.environ.copy()`, ni `env=None`.

| Grupo | Variables |
| --- | --- |
| Allowlist general | `SYSTEMROOT`, `SYSTEMDRIVE`, `WINDIR`, `COMSPEC`, `PATHEXT`, `LANG`, `LC_ALL`, `TZ` |
| Reconstruidas | `PATH` (dir del binario + sistema) y `TEMP`/`TMP` (zona propia) |
| Fijadas | `PYTHONDONTWRITEBYTECODE` |
| Específicas del runtime | `USERPROFILE`, `HOME`, `HOMEDRIVE`, `HOMEPATH`, `LOCALAPPDATA`, `APPDATA`, `USERNAME` |

Las específicas están ahí por **necesidad demostrada**: sin ellas Podman falla con
`cannot determine user's homedir`. Son rutas, nunca credenciales, y solo las ve el
proceso del cliente — el contenedor no las recibe.

**2. El workspace se deriva solo del contexto.** Se eliminó el override
`workspace_path`: la única fuente es `ExecutionContext.workspace_path`. Antes de
construir `--mount type=bind`, `assert_mountable_workspace()` rechaza la ruta si no
existe, no es directorio, es raíz de unidad, es el home del usuario o un ancestro
suyo, es un ancestro del repositorio del motor, contiene archivos constitucionales,
o es `.git`.

---

## 20. Licencia

Propietario — Punto Inmobiliario HN. `Private :: Do Not Upload`.


