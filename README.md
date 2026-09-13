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
| **ENGINE-2** | **DeepSeek Developer Integration.** Integración real del modelo mediante `DeepSeekDeveloperRunner`, sobre la misma interfaz y **exigiendo sandbox**. | ✅ Implementada |
| **ENGINE-3** | **Architect + Planner.** Diseño de arquitectura y planificación del proyecto antes de escribir código. | ✅ Implementada |
| **ENGINE-4** | **Independent QA Agent.** QA determinista más evaluación independiente, con gates que el modelo no puede anular. | ✅ Implementada |
| **ENGINE-5** | **Security + Reviewer.** Análisis de seguridad, revisión independiente, frontera de contexto del modelo y evaluación completa de la tarea. | ✅ Implementada |
| **ENGINE-5.2** | **Multi-Provider + Anthropic/Claude + Cross-Model Audit.** Contrato provider-neutral, `output_config` estructurado, auditoría cruzada real y fundación multimodal. | ✅ Implementada |
| **ENGINE-5.3** | **Web + Visual Execution Foundation.** Perfil web, sandbox con Chromium real, once checks deterministas, capturas verificadas y Visual QA con gates no anulables. | ✅ Implementada |
| **ENGINE-5.3.1** | **Trusted Web Evidence + Visual Completeness Hardening.** Frontera de dos contenedores para la medición, cobertura visual exigida por la especificación, aplicabilidad explícita de los checks y gates vivos de Visual QA. | ✅ Implementada (certificación live `PENDING_API_KEY`) |

ENGINE-0 no es un agente inteligente: es el **esqueleto de gobernanza**. ENGINE-1
tampoco: es la **capa de ejecución controlada**, que permite ejecutar trabajo real
sobre un repositorio local de prueba sin ningún modelo de IA. Todo el
comportamiento es local, reproducible y verificable por pruebas.

> **Proveedor de IA.** Desde ENGINE-2 el motor conecta proveedores reales: **DeepSeek** para los
> roles de ingeniería y **Anthropic/Claude** para la auditoría cruzada y Visual QA. Ninguno de los
> dos es necesario para que la suite estándar pase: sin credencial, la operación que la requiere
> queda `BLOCKED` / `PENDING_API_KEY`, nunca sustituida por otro proveedor.


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

## 20. ENGINE-2 — DeepSeek Developer Integration

ENGINE-2 conecta un **Developer AI real** al ciclo controlado de ENGINE-1. El
modelo **genera**; PUNTO decide **qué se toca, dónde, cuántas veces y si pasa**.

### Componentes

| Componente | Archivo | Responsabilidad |
| --- | --- | --- |
| `DeepSeekClient` | `src/punto/providers/deepseek.py` | Única salida HTTP del motor. Habla con la API de DeepSeek y devuelve `ModelCompletion`. |
| `DeepSeekDeveloperRunner` | `src/punto/developer/deepseek.py` | Orquesta el ciclo completo: contexto → propuesta → validación → sandbox → reparación → commit. |
| Prompts versionados | `src/punto/developer/prompts.py` | `DEVELOPER_PROMPT_VERSION = "1.0.0"`, sistema, usuario y reparación. |
| Tipos de propuesta | `src/punto/schemas/execution.py` | `DeveloperProposal`, `ProposedFileChange`, `ProposalOperation`, `ModelUsage`. |
| Puerta viva | `tests/integration/test_deepseek_live.py` | Única prueba que exige una llamada real a la API. |

El cliente vive en `punto.providers`, un paquete deliberadamente **sin
re-exportaciones**: importarlo no arrastra `httpx` ni la cadena del runner.

### Flujo de una tarea

```
CAMUS → Policy Engine → DeepSeekDeveloperRunner → DeepSeek API
      → DeveloperProposal → validación atómica → Filesystem Tool
      → ContainerSandboxBackend → pytest / ruff / mypy
           ├── falla  → propuesta de reparación → sandbox (bucle acotado)
           └── pasa   → commit local en rama aislada
      → DeveloperExecutionResult → CAMUS
```

### El modelo propone, PUNTO decide

El modelo **no tiene herramientas**. No puede llamar funciones, ni ejecutar
comandos, ni leer ni escribir archivos, ni hablar con Git. Su única salida es un
objeto JSON con una lista de cambios. Todo lo demás lo hace el motor:

| Decisión | Quién |
| --- | --- |
| Qué archivos puede tocar | `DeveloperTask.allowed_files` (allowlist cerrada por tarea) |
| Qué operaciones existen | `CREATE` y `REPLACE`. **No existe `DELETE`** |
| Cuánto contexto ve | `max_context_bytes` (por defecto 200 000) y `MAX_CONTEXT_FILE_CHARS` |
| Cuántas llamadas al modelo | `ModelLimits.max_model_calls` (por defecto 6) |
| Cuánto puede gastar | `max_input_tokens` (200 000) y `max_output_tokens` (60 000) |
| Dónde se ejecuta | `ContainerSandboxBackend` verificado (nunca el host) |
| Si la tarea pasa | El validador (`pytest`, `ruff`, `mypy`) dentro del contenedor |

Una propuesta se **valida entera o se rechaza entera**: si un solo cambio tiene
traversal (`../`), ruta absoluta, archivo protegido, ruta duplicada, archivo
fuera de la allowlist o un `REPLACE` sin contenido, **no se aplica ninguno**. La
atomicidad está probada por pruebas parametrizadas, no declarada.

### Restricciones estructurales

- **Ningún endpoint HTTP de developer.** La superficie de la API sigue igual que
  en ENGINE-0; el runner se invoca por código, no por red entrante.
- **Sin tool-calling.** El contrato con el modelo es texto entra, JSON sale
  (`response_format={"type": "json_object"}`).
- **Sin modelos heredados.** `deepseek-chat` y `deepseek-reasoner` se rechazan en
  `DeepSeekConfig.__post_init__` con `DeepSeekModelNotSupportedError`.
  Soportados: `deepseek-v4-pro` (por defecto) y `deepseek-v4-flash`.
- **Sin degradación del sandbox.** Si Podman no está disponible, la ejecución se
  **BLOQUEA** con `SANDBOX_REQUIRED`; no hay caída al host.

### Red

El motor habla con **un solo destino externo**: `https://api.deepseek.com`
(configurable con `DEEPSEEK_BASE_URL`). La cabecera es
`Authorization: Bearer <clave>`. No hay proxies, ni telemetría, ni llamadas de
descubrimiento.

El contenedor, en cambio, **no tiene red**: `--network none`. El modelo puede
pedir cualquier cosa; el código que produce se ejecuta sin salida a Internet.

### Bucle de reparación

Cuando el validador falla, la evidencia (salida de `pytest`/`ruff`/`mypy`
recortada a `MAX_EVIDENCE_CHARS`) se devuelve al modelo junto con la propuesta
anterior y se pide una corrección. El bucle está acotado por
`max_model_calls`. Agotado el presupuesto, el workspace se **revierte**
(`git reset --hard <base>` + limpieza de no rastreados) y la tarea se reporta
como fallida: nunca queda un estado a medias.

### Uso de tokens

Cada llamada devuelve `ModelUsage` (`prompt_tokens`, `completion_tokens`,
`total_tokens`, `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens`). El
runner los acumula con `merged()` y **corta** con `MAX_TOKENS_EXCEEDED` si se
pasa del presupuesto. El consumo real queda en el `DeveloperExecutionResult` y
en la auditoría.

### Auditoría

Se registran, sin secretos: `MODEL_REQUEST_STARTED/COMPLETED/FAILED`,
`DEVELOPER_PROPOSAL_RECEIVED/REJECTED`,
`DEVELOPER_ATTEMPT_STARTED/FAILED/REPAIR_REQUESTED/PASSED`. Todo texto que
provenga del modelo o de la API pasa por `redact_secrets()` antes de tocar el
log: la clave nunca aparece, ni siquiera en un mensaje de error de la API.

### El contrato de la propuesta (`DEVELOPER_PROMPT_VERSION`)

El modelo **no adivina** el formato: el prompt de sistema fija los nombres de clave
exactos (`path`, `operation`, `content`, `summary`, `validation_notes`,
`assumptions`), prohíbe los alias (`file_path`, `filename`, `action`) y exige que
`validation_notes` y `assumptions` sean **listas** de strings. El recordatorio
(`PROPOSAL_FORMAT_REMINDER`) se repite **al final** de cada petición, porque es lo
último que el modelo lee antes de responder.

Esto no es decoración: la primera ejecución real de la puerta viva devolvió
`file_path`/`action` y notas como string suelto, y el esquema lo rechazó. El
contrato se cerró con nombres exactos y la desviación quedó cubierta por pruebas.

Cuando una propuesta incumple el contrato **no se aplica nada**: se rechaza entera,
el motivo vuelve al modelo como evidencia y el intento se repite dentro del
presupuesto que fija PUNTO (`attempts_allowed` y `max_model_calls`). Un rechazo
cuesta un intento y una llamada; nunca escribe medio cambio.

### Pruebas

| Comando | Qué cubre | Necesita |
| --- | --- | --- |
| `pytest` | Suite completa, con cliente falso y sandbox real | Podman |
| `pytest tests/test_deepseek_integration.py -q` | Cliente (transporte simulado), contrato del prompt, propuesta, atomicidad, reparación, límites, rollback, auditoría | Podman |
| `pytest tests/integration/test_deepseek_live.py -q` | **Puerta viva**: contrato de producción contra la API real, error estructurado y ciclo completo | `DEEPSEEK_API_KEY` + Podman |

La suite por defecto **ignora** `tests/integration` (`--ignore=tests/integration`)
para que un entorno sin credenciales siga siendo verde. La puerta viva se ejecuta
a propósito y **falla de forma explícita** con
`CREDENTIAL_REQUIRED: DEEPSEEK_API_KEY` cuando la clave no está: no se salta en
silencio, no se marca `xfail`, no se declara PASS sin llamada real.

La puerta viva envía el **prompt de producción**, no uno escrito para la prueba.
Esa distinción importa: una versión anterior usaba un prompt ad-hoc y por eso daba
verde mientras el contrato real fallaba.

### Evidencia de la llamada real

Ejecución con `DEEPSEEK_API_KEY` presente y Podman en marcha (`3 passed`):

| Prueba | Resultado |
| --- | --- |
| Contrato de producción → propuesta válida | `deepseek-v4-pro`, 4675 ms, 1191 tokens, `changes = ["hello.py"]` |
| Credencial inválida | error estructurado, sin filtrar la credencial |
| Misión real completa | 1 llamada, 1 intento, 1596 tokens, `hello.py` escrito, sandbox PASS, commit local |

La misión real siguió el ciclo del mandato de principio a fin: propuesta del modelo
→ validación atómica → escritura en el workspace → contenedor verificado →
`pytest` verde → **commit local** en la rama derivada por PUNTO (`ai/<task_id>-<slug>`),
sin rollback y **sin push**.

### Configuración

La clave se lee del entorno. **No se escribe nunca en el repositorio.**

```bash
# .env (fuera del control de versiones)
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
```

Si la clave no está, el motor no inventa: reporta
`CREDENTIAL_REQUIRED: DEEPSEEK_API_KEY`.

La variable debe estar **en el proceso que ejecuta pytest**. Definirla en una sesión
de PowerShell no la hace visible a procesos ya lanzados desde ella:

```powershell
# Persistente para el usuario: visible a procesos nuevos.
[Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY', $env:DEEPSEEK_API_KEY, 'User')
```

### Limitación declarada

Los identificadores `deepseek-v4-pro` y `deepseek-v4-flash` provienen del mandato.
`deepseek-v4-pro` quedó **confirmado contra la API real** en la puerta viva:
respondió sin `DeepSeekModelNotSupportedError` y devolvió ese mismo identificador en
`completion.model`. `deepseek-v4-flash` sigue sin ejercitarse en vivo.

---

## 21. ENGINE-3 — Architect + Project Planning Layer

ENGINE-3 cambia el propósito del motor: deja de estar diseñado alrededor de un
proyecto concreto y se convierte en un **motor general de creación de software**.

```
Usuario: "Quiero crear una plataforma para administrar clínicas dentales"
        ↓
      CAMUS  →  Architect  →  Planner  →  Developer  →  Sandbox  →  QA …
```

ENGINE-3 construye los dos primeros roles y los conecta a CAMUS. Convierte una
intención humana en estructuras deterministas y validadas:

```
ProjectIntent → ProjectSpec → ArchitecturePlan → Roadmap → TaskGraph
```

**Todavía no ejecuta nada.** El Developer de ENGINE-2 seguirá siendo el ejecutor
cuando CAMUS le entregue trabajo; ENGINE-3 diseña y planifica.

### Separación de roles

La separación está en **interfaces y esquemas**, no solo en los prompts:

| Rol | Responde | No hace |
| --- | --- | --- |
| Architect | qué sistema debemos construir | no escribe archivos, no ejecuta, no decide autoridad |
| Planner | cómo dividirlo en trabajo ejecutable | no elige arquitectura, no escribe código, no ejecuta |
| Developer | cómo implementar una tarea concreta | no decide la arquitectura global |
| CAMUS | qué está permitido y qué ocurre después | no inventa implementaciones |

### Componentes nuevos

| Componente | Archivo | Responsabilidad |
| --- | --- | --- |
| `ArchitectRunner` | `src/punto/architect/base.py` | Interfaz provider-agnostic del diseño |
| `DeepSeekArchitectRunner` | `src/punto/architect/deepseek.py` | Implementación real con repair loop |
| `PlannerRunner` | `src/punto/planner/base.py` | Interfaz provider-agnostic de la planificación |
| `DeepSeekPlannerRunner` | `src/punto/planner/deepseek.py` | Implementación real, con ensamblado determinista |
| Esquemas | `src/punto/schemas/planning.py` | Contratos de datos, con id, versión y timestamp |
| Invariantes | `src/punto/planning/graph.py` | Reglas deterministas del plan |
| Capacidades | `src/punto/planning/capabilities.py` | Qué puede ejecutar PUNTO hoy y qué no |
| Prompts | `src/punto/architect/prompts.py`, `src/punto/planner/prompts.py` | Contratos versionados |

`punto.planning` no importa nada de `punto.architect` ni de `punto.planner`: son los
roles los que dependen de las reglas, nunca al revés. Los `__init__` de los tres
paquetes no reexportan: importar un rol no arrastra `httpx`.

### ProjectIntent

Solo `name` y `description` son obligatorios. Todo lo demás —objetivo de negocio,
usuarios, capacidades, restricciones, stack preferido, despliegue, requisitos no
funcionales, integraciones, presupuesto, notas— es opcional: **no se obliga a la
persona a declarar datos técnicos que el Architect puede inferir**.

### ProjectSpec y preguntas abiertas

El Architect transforma lenguaje humano en especificación estructurada: problema,
objetivos, usuarios, requisitos funcionales y no funcionales (con id trazable y
criterio de aceptación), supuestos, restricciones, fuera de alcance, criterios de
éxito, riesgos y preguntas abiertas **clasificadas**:

| Clasificación | ¿Bloquea la planificación? | ¿Quién decide? |
| --- | --- | --- |
| `TECHNICAL_INFERABLE` | No | El motor |
| `BUSINESS_DECISION` | No | Una persona, antes de ejecutar |
| `LEGAL_DECISION` | No | Una persona, antes de ejecutar |
| `FINANCIAL_DECISION` | No | Una persona, antes de ejecutar |
| `MISSING_CRITICAL_INFORMATION` | **Sí** | Una persona, antes de planificar |

Solo el último tipo detiene el plan. Las decisiones de negocio, legales y financieras
se **registran y se difieren**: no se convierten en Human Gate de planificación.

### ArchitecturePlan

Estilo arquitectónico, componentes con responsabilidad y dependencias, servicios,
módulos, almacenes de datos, integraciones externas, interfaces, fronteras de
seguridad, topología, observabilidad, estrategia de pruebas, elecciones
tecnológicas, alternativas consideradas y riesgos.

Cada decisión tecnológica guarda `decision`, `reason`, `alternatives`, `tradeoffs` y
`confidence`. **Nunca se almacena cadena de pensamiento**: decisiones resumidas y
justificables.

### ProjectCapabilityProfile y huecos

Como el motor es general, **no se asume Python**. El Architect declara lo que el
sistema elegido necesita (lenguajes, frameworks, bases de datos, gestores de
paquetes, validadores, destinos de despliegue y perfiles de ejecución) y PUNTO lo
compara con lo que puede ejecutar de verdad.

| Estado | Significado |
| --- | --- |
| `AVAILABLE` | Demostrado en ENGINE-1.R3: `python312`, `git`, `podman`, `pytest`, `ruff`, `mypy`, `sqlite` |
| `MISSING` | PUNTO sabe que no lo tiene (`node20`, `postgres`, `vercel`, `eslint`, `pip`…) |
| `UNKNOWN` | PUNTO no tiene información: **se trata como no disponible**, jamás como disponible |

Un hueco **no bloquea la planificación** (§17): se registra con las tareas que lo
exigen para poder construir después el perfil de ejecución necesario. Lo que nunca se
hace es improvisar una ejecución en el host como sustituto.

La normalización de nombres es deliberadamente tolerante, porque los modelos escriben
la misma tecnología de muchas formas: `Node.js` → `node20`, `typescript5.4` →
`typescript`, `LANGUAGE: TypeScript 5.x` → `typescript`, `Node.js 20 LTS` → `node20`.
Una capacidad que PUNTO no conoce conserva su nombre legible (`aws ecs/fargate`), no
una versión mutilada.

### Roadmap y TaskGraph

```
Milestone → Epic → Task
```

Cada tarea declara id, título, objetivo, descripción, criterios de aceptación,
dependencias, archivos permitidos y de contexto, checks de validación, capacidades
requeridas, riesgo, autoridad, complejidad, qué produce y su estado.

El Planner propone; **PUNTO cablea las relaciones**: `Epic.task_ids` sale de
`task.epic_id` y `Milestone.epic_ids` de `epic.milestone_id`, de modo que el modelo
no puede declarar un enlace que no exista.

La resolución del grafo es determinista y **no consulta al modelo**:

| Consulta | Significado |
| --- | --- |
| `ready_tasks()` | Pendientes con **todas** sus dependencias en `DONE` |
| `completed_tasks()` | Terminadas |
| `blocked_tasks()` | Fallidas o bloqueadas, y todo lo que depende de ellas |
| `next_tasks(n)` | Las primeras `n` listas, en orden del plan |
| `topological_order()` | Orden determinista; falla si hay ciclo, diciendo cuál |

### Invariantes de planificación

El Planner puede proponer; PUNTO valida. No se acepta automáticamente:

- ciclos de dependencias, identificadores duplicados, dependencias inexistentes,
  tareas que dependen de sí mismas;
- tareas sin criterios de aceptación, o con criterios y objetivos vagos
  («Crear backend»);
- milestones sin trabajo, epics huérfanos, tareas fuera de su epic;
- archivos constitucionalmente protegidos en `allowed_files`;
- capacidades exigidas por una tarea que no estén declaradas en el perfil;
- riesgo alto o crítico (`requires_human_gate`) sin autoridad humana: la **única**
  autoridad admisible es `LEVEL_3_HUMAN`; `LEVEL_1_AUTONOMOUS_REVIEW` es revisión
  posterior y `LEVEL_2_CAMUS` es autoridad del orquestador, así que ninguna de las
  dos representa aprobación humana previa;
- planes desmedidos (más de 120 tareas).

Un plan inválido no se «arregla»: se devuelven **todas** las violaciones al rol
correspondiente para que proponga de nuevo dentro de su presupuesto.

### Bucles de reparación

`architect_max_attempts` y `planner_max_attempts` (3 por defecto) acotan cuántas veces
puede reintentar cada rol. Igual que en ENGINE-2, se separan dos cosas que no son lo
mismo:

- **reintentos del proveedor**: los aplica el cliente HTTP ante fallos transitorios de
  red; no consumen intentos de reparación;
- **intentos de reparación**: los consume el rol cuando su propuesta incumple un
  invariante.

Agotados los intentos, el estado es `BLOCKED` con las violaciones acumuladas.

### Modelo y routing

Se reutiliza el `DeepSeekClient` de ENGINE-2: **no** se duplica cliente HTTP, ni
autenticación, ni reintentos. El modelo es configuración, no código:

```bash
PUNTO_ARCHITECT_MODEL=deepseek-v4-pro
PUNTO_PLANNER_MODEL=deepseek-v4-flash   # cada rol puede usar otro modelo
PUNTO_PLANNING_MAX_TOKENS=65536
```

### Presupuesto de salida: una lección medida

Con `thinking` activo, el razonamiento consume **el mismo** presupuesto que el
documento JSON. La puerta viva lo midió con `deepseek-v4-pro` y
`reasoning_effort=high`:

| `max_tokens` | Resultado real |
| --- | --- |
| 8 192 | `finish_reason='length'`, JSON cortado a mitad de una cadena |
| 16 384 | `stop`, JSON válido, ~11 400 tokens de salida (la mayoría razonamiento) |
| 32 768 | `stop`, JSON válido |
| 65 536 / 131 072 | aceptado por la API sin objeción |

Dos consecuencias de diseño:

1. el motor pide **65 536** tokens de salida para planificar (configurable con
   `PUNTO_PLANNING_MAX_TOKENS`);
2. un truncamiento se detecta y se reporta como tal —`DeepSeekTruncatedResponseError`
   dice `finish_reason='length'` y el `max_tokens`— en lugar de disfrazarse de error
   de sintaxis. Repetir la misma petición con el mismo límite no arregla nada, así que
   **no** se trata como propuesta reparable.

Además el plan está **acotado en el prompt** (máximo 4 milestones, 8 epics y 20
tareas). Sin cota, el modelo produjo un roadmap de 86 656 caracteres. Un plan enorme
no es un plan mejor: es un plan que nadie puede auditar.

### Human Gates

El motor decide por sí mismo lo técnico y reversible: framework, estructura de
carpetas, librería de pruebas, ORM, linter, nombres internos, patrones
arquitectónicos. **No** se convierte cada duda en un Human Gate.

Los Human Gates siguen las reglas constitucionales ya existentes: negocio crítico,
legal, financiero, producción, irreversible, riesgo alto o crítico, límites
excedidos. En ENGINE-3 ninguno de esos casos se dispara todavía: planificar no
ejecuta. Las preguntas diferidas viajan en el `ProjectPlanResult` para que la decisión
se tome cuando toque, no antes.

### Persistencia

La persistencia real queda para una fase posterior, pero **todos** los artefactos
llevan ya id estable (UUID), `created_at` y `schema_version`, de modo que guardarlos
después no obligue a rediseñar los esquemas. Las tareas usan identificadores legibles
estables (`T1`, `T2`…) porque son los que referencian las dependencias.

### Auditoría

Eventos nuevos: `ARCHITECT_REQUEST_STARTED`, `ARCHITECT_PLAN_RECEIVED`,
`ARCHITECT_PLAN_REJECTED`, `ARCHITECT_PLAN_ACCEPTED`, `PLANNER_REQUEST_STARTED`,
`ROADMAP_RECEIVED`, `TASK_GRAPH_REJECTED`, `TASK_GRAPH_ACCEPTED`,
`PROJECT_PLAN_COMPLETED` y `PROJECT_PLAN_BLOCKED`.

Se registran recuentos, motivos y violaciones —nunca el diseño completo ni el
`reasoning_content`— y todo pasa por redacción: la credencial no aparece.

### Generalidad: tres proyectos sintéticos

Las pruebas planifican tres productos de naturaleza distinta con el **mismo código**.
Ninguno se implementa: solo se planifican.

| Fixture | Tecnología | Huecos detectados |
| --- | --- | --- |
| `python-api` (StockFlow) | Python, API REST | `fastapi`, `pip` |
| `nextjs-saas` (ClientPulse) | TypeScript, Next.js, PostgreSQL | `typescript`, `nextjs`, `postgres`, `npm`, `eslint`, `tsc`, `vitest`, `vercel`, `node20` |
| `cli` (NotesCLI) | Python, CLI sin dependencias | `pip` |

Un proyecto Rust también se planifica: registra `rust` y `cargo` como huecos y no
falla. El prompt del Architect no impone ninguna tecnología.

### Pruebas y gates vivos

| Comando | Qué cubre |
| --- | --- |
| `pytest` | Suite completa: esquemas, grafo, invariantes, capacidades, runners, CAMUS, generalidad |
| `pytest tests/integration/test_architect_live.py -q` | **Gate vivo**: idea real → especificación y arquitectura válidas |
| `pytest tests/integration/test_planner_live.py -q` | **Gate vivo**: arquitectura real → roadmap y DAG válidos |
| `pytest tests/integration/test_e2e_planning_live.py -q` | **Gate vivo**: idea → plan completo por CAMUS, sin planificación humana |

Los gates de ENGINE-3 necesitan `DEEPSEEK_API_KEY` pero **no** sandbox: planificar no
ejecuta nada. Sin credencial fallan de forma explícita.

### Evidencia de las llamadas reales

Ejecución completa con `DEEPSEEK_API_KEY` presente: **6 gates, 0 fallos**.

| Gate | Resultado real |
| --- | --- |
| Architect: especificación y arquitectura | `deepseek-v4-pro`, 1 llamada, 11 356 tokens, 4 componentes, 12 requisitos, 15 capacidades, 0 preguntas bloqueantes |
| Architect: clasificación de dudas | 0 preguntas abiertas: no convirtió dudas en bloqueos |
| Planner: roadmap y grafo | 1 intento, 21 063 tokens, 4 milestones, 8 epics, **20 tareas**, 46 dependencias, `ready=[T1]` |
| Planner: cobertura de requisitos | los 6 requisitos `MUST` (R-001…R-006) están enlazados por tareas |
| **End-to-end: idea → plan** | **PASS**: 1 llamada de Architect + 1 de Planner, 31 445 tokens, 20 tareas, **17 huecos de capacidad** registrados, 0 bloqueos, **2 preguntas diferidas** |
| Determinismo del motor | dos planificaciones de la misma idea: dos PASS, 28 601 y 32 770 tokens |

El gate end-to-end es la primera vez que PUNTO convierte una idea («plataforma web
para clínicas dentales») en un plan de software completo **sin planificación humana
manual**: especificación, arquitectura, roadmap y grafo de tareas, todo validado.

Las dos preguntas diferidas de la última ejecución son de negocio y legales: se
registraron y **no** bloquearon la planificación, que es exactamente el
comportamiento pedido.

### API

Sin cambios: no se expone planificación por HTTP. `GET /health` → 200 y la misma
superficie de rutas que en ENGINE-0.

### Limitación declarada

- ENGINE-3 **no ejecuta**: produce un plan validado. Convertir `ready_tasks()` en
  ejecución real del Developer es una decisión de una fase posterior.
- Los huecos de capacidad se registran, no se resuelven: PUNTO sigue sin poder
  ejecutar Node, PostgreSQL ni nada fuera de lo demostrado.
- La persistencia de planes está pendiente; los artefactos ya están preparados para
  ella.
- El modelo elige la tecnología cuando la persona no la declara. Si esa elección no
  gusta, se corrige declarando restricciones en la intención, no editando el plan.

---

## 22. ENGINE-4 — Independent QA Agent

ENGINE-4 añade el primer QA **real e independiente** del motor. Su principio
constitucional cabe en una línea:

```
DEVELOPER ≠ QA
```

El agente que escribe código **no** puede ser la autoridad que decide por sí misma que
ese código cumple los criterios de aceptación. QA no confirma: intenta **refutar**.

### Separación de roles

| Rol | Hace | No hace |
| --- | --- | --- |
| Developer | implementa | no decide si su trabajo cumple el contrato |
| QA | diseña pruebas independientes y las ejecuta | no modifica producción, no corrige el producto, no hace commits, no redefine criterios, **no declara PASS** |
| CAMUS | decide qué ocurre con el resultado | no evalúa por sí mismo |

El resultado del Developer viaja en la tarea como **contexto**, nunca como prueba. En
las pruebas se verifica explícitamente que un `DeveloperExecutionResult` con
`validation.passed=True` **no** convierte la tarea en aprobada.

### Componentes nuevos

| Componente | Archivo | Responsabilidad |
| --- | --- | --- |
| `QARunner` | `src/punto/qa/base.py` | Interfaz provider-agnostic |
| `DeepSeekQARunner` | `src/punto/qa/deepseek.py` | Implementación real |
| Esquemas | `src/punto/schemas/qa.py` | `QATask`, `QAPlan`, `QATestCase`, `AcceptanceCoverage`, `QAReport`, `QAFinding` |
| Guard de rutas | `src/punto/qa/paths.py` | Frontera pruebas / producción |
| Registry de checks | `src/punto/qa/checks.py` | Qué se puede ejecutar y con qué comando |
| Validador | `src/punto/qa/validation.py` | Invariantes del plan |
| Veredicto | `src/punto/qa/report.py` | Clasificación de fallos, cobertura y estado |
| Overlay | `src/punto/qa/overlay.py` | Workspace desechable |
| Prompts | `src/punto/qa/prompts.py` | Contrato versionado (`1.0.0`) |

`punto.qa` no reexporta: importarlo no arrastra `httpx` ni el runner de DeepSeek.

### Plan de QA y trazabilidad

QA produce un `QAPlan` con `summary`, `test_cases`, `test_file_changes`, `checks`,
`coverage_mapping` y `assumptions`. Cada caso declara título, objetivo, tipo
(`UNIT`, `INTEGRATION`, `REGRESSION`, `STATIC`), los criterios que cubre y el
comportamiento observable que espera.

La trazabilidad es **mecánica**, no interpretativa:

- cada criterio recibe un identificador determinista (`AC-1`, `AC-2`…) en el orden en
  que se recibe;
- cada criterio debe aparecer en `coverage_mapping` como `COVERED` (con al menos un
  caso) o `UNTESTABLE` (con un motivo). **Ningún criterio puede desaparecer**;
- cada función de prueba lleva el identificador de su caso en el nombre
  (`test_qu_1_...`). Sin esa convención, PUNTO rechaza el plan: la trazabilidad tiene
  que poder comprobarse, no suponerse;
- el emparejamiento es por **frontera exacta**, no por substring: `test_qu_10_x` no
  pertenece a `QU-1`, y `test_qu_1_x[param]` sí pertenece a `QU-1`. La frontera es
  agnóstica del lenguaje (`def test_qu_1_x`, `it('test_qu_1_x')`), porque PUNTO es un
  motor general;
- dos identificadores que normalizan al mismo token (`QU-1` y `QU_1`) se rechazan: sus
  pruebas serían indistinguibles y un fallo podría atribuirse al caso equivocado;
- un caso sin prueba observada **no** hereda el fallo de otro: se marca como no
  concluyente.

Esa convención permite atribuir cada fallo al caso —y por tanto al criterio— que lo
produjo. Un QA que declara roto todo el contrato porque una sola aserción falló es un
mal QA: aquí solo queda `FAILED` el criterio cuya prueba falló de verdad.

### QA genera pruebas, no producción

La frontera se decide por ruta, antes de escribir un byte, y en caso de duda una ruta es
de producción:

| Clasificación | Ejemplos | ¿QA puede escribir? |
| --- | --- | --- |
| `TEST_ONLY` | `tests/…`, `test/…`, `__tests__/…`, `*.test.ts`, `conftest.py`, raíces declaradas por el proyecto | Sí |
| `PRODUCTION` | `src/…`, `app/…`, `lib/…` | No |
| `PROTECTED` | configuración constitucional | No |
| `INVALID` | rutas absolutas, `..`, `.git`, `node_modules`, `.env`, `id_rsa` | No |

Además, QA **nunca sobrescribe** un archivo existente: solo añade archivos nuevos. Si
pudiera editar la prueba del Developer, podría borrar la evidencia que debe evaluar.

### Registry de checks

QA no devuelve comandos: devuelve **nombres**. Los argumentos los fija PUNTO.

| Disponibles hoy | Registrados pero no disponibles |
| --- | --- |
| `pytest`, `ruff`, `mypy`, `python-import` | `vitest`, `jest`, `eslint`, `tsc`, `npm-test` |

Un nombre fuera del registro invalida el plan. No hay `bash -c`, ni `powershell`, ni
cadenas de shell: el registro es cerrado.

### Workspace efímero

```
candidate workspace → overlay desechable → pruebas QA → sandbox → evidencia → destruir
```

La rama aprobada del Developer no se toca. Las pruebas de QA viven en un directorio
temporal, se ejecutan contra una copia del árbol candidato y desaparecen al terminar:
**no se commitean**. El overlay es siempre una ruta nueva, así que un fallo se descarta
entero; y la escritura es atómica: si un archivo del plan no es escribible, no se escribe
ninguno.

### Ejecución

Todo archivo de prueba generado por QA es código de modelo, así que:

- `ExecutionTrustLevel.UNTRUSTED_MODEL`;
- `ContainerSandboxBackend` **verificado** (los cuatro aislamientos demostrados);
- sin red (`--network none`), sin host, **sin fallback**;
- la credencial vive solo en el proceso del modelo: no entra al overlay ni al sandbox.

### Clasificación de fallos

La distinción que evita el peor error de un QA automático:

| Categoría | Qué significa | Qué ocurre |
| --- | --- | --- |
| `PRODUCT_FAILURE` | la implementación no cumple el criterio | **QA FAIL**. No se repara: se reporta |
| `QA_TEST_FAILURE` | la prueba de QA es inválida (sintaxis, colección, uso) | QA repara **su** prueba, dentro de su presupuesto |
| `INFRASTRUCTURE_FAILURE` | el entorno no permitió ejecutar (timeout, sandbox, binario) | `BLOCKED` |
| `CAPABILITY_GAP` | PUNTO no tiene la capacidad necesaria | `BLOCKED` con la capacidad declarada |

Un `PRODUCT_FAILURE` **nunca** se arregla cambiando la expectativa. La petición de
reparación lo dice explícitamente: «Repara TUS pruebas. Esto NO es un permiso para
cambiar lo que esperas del producto».

### PASS determinista

El modelo **no puede** escribir el veredicto: el contrato rechaza cualquier clave
desconocida, incluida `status`. El estado lo calcula PUNTO:

`PASS` solo si el plan es válido, **todos** los criterios obligatorios quedaron
demostrados por pruebas ejecutadas y superadas, todos los checks se ejecutaron y
pasaron, y no hay ningún fallo del producto, de infraestructura ni hueco de capacidad
que impida comprobar algo.

`FAIL` si existe un defecto del producto. `BLOCKED` en cualquier otro caso: sin
evidencia no hay veredicto, y no se finge PASS.

Los hallazgos (`QAFinding`) llevan gravedad, categoría, criterio afectado, evidencia real
y una pista de reparación. La gravedad es fija por categoría: `CRITICAL` queda reservado
para las fases de Security y Reviewer, que aún no existen.

### CAMUS

`Camus.evaluate_developer_result(task)` —y su alias `qa_task(task)`— delega en el
`QARunner` inyectado y devuelve el `QAReport`. **ENGINE-4 no lanza una reparación
automática** cuando QA falla: el bucle Developer → QA → reparación pertenece a la fase de
workflow/orquestación posterior.

### Auditoría

Eventos: `QA_REQUEST_STARTED`, `QA_PLAN_RECEIVED`, `QA_PLAN_REJECTED`,
`QA_PLAN_ACCEPTED`, `QA_EXECUTION_STARTED`, `QA_CHECK_COMPLETED`, `QA_CHECK_FAILED`,
`QA_FINDING_RECORDED`, `QA_COMPLETED` y `QA_BLOCKED`.

Se registran recuentos, códigos de salida y categorías —nunca la credencial, el código
de las pruebas ni el código de producción—, y el inicio registra si el Developer declaró
su validación superada, precisamente para poder auditar que QA no lo usó como evidencia.

### Generalidad

| Proyecto | Plan de QA | Ejecución |
| --- | --- | --- |
| Python REST/API | válido | **ejecutable** (perfil demostrado) |
| Next.js / TypeScript | válido | `BLOCKED` / `CAPABILITY_REQUIRED` (`node20`, `npm`, `vitest`) |
| CLI en Python | válido | **ejecutable** |

Que PUNTO no pueda ejecutar Node todavía no es un defecto de QA: es un hueco declarado,
y **nunca** se resuelve ejecutando Node en el host como sustituto.

### Pruebas y gate vivo

| Comando | Qué cubre |
| --- | --- |
| `pytest` | Suite completa: guard de rutas, registry, validador, clasificación, overlay, runner con sandbox real, generalidad y CAMUS |
| `pytest tests/integration/test_qa_live.py -q` | **Gate vivo**: defecto real detectado, clasificación del fallo, PASS con el producto corregido e independencia Developer/QA |

El gate vivo usa un `clamp` deliberadamente defectuoso
(`min(value, upper)`, que ignora el límite inferior) con pruebas de Developer que solo
cubren `value > upper`: el Developer declara su validación superada y QA, con sus
propias pruebas, encuentra el defecto que esas pruebas no veían.

### Evidencia de las llamadas reales

Ejecución completa con `DEEPSEEK_API_KEY` presente y Podman en marcha: **5 gates, 0
fallos** (4:24).

| Gate | Resultado real |
| --- | --- |
| Implementación defectuosa | `FAIL` — `deepseek-v4-pro` diseñó **5 casos**, 6 173 tokens, cobertura `AC-1 FAILED` / `AC-2 COVERED` / `AC-3 COVERED` |
| Clasificación del defecto | `PRODUCT_FAILURE` sobre `AC-1` («value < lower devuelve lower») |
| Implementación corregida | `PASS` — 4 651 tokens, los tres criterios `COVERED` |
| Independencia | `developer_claimed_pass=True` en ambos casos → QA **FAIL** con el defectuoso y **PASS** con el corregido |
| Aislamiento | el proyecto queda **byte a byte idéntico** y el overlay se destruye |

### API

Sin cambios: QA no se expone por HTTP. `GET /health` → 200 y la misma superficie de rutas.

### Limitación declarada

- QA **no repara el producto**: detecta y reporta. La reparación automática Developer ↔ QA
  llega con el workflow de orquestación.
- Las pruebas que genera QA son efímeras: una fase futura podrá promoverlas al proyecto.
- Solo se ejecuta lo que tiene perfil demostrado (Python). El resto queda `BLOCKED` por
  capacidad, nunca por atajo.
- `E2E` y pruebas de navegador no existen todavía y **no se prometen**: exigirían
  capacidades que PUNTO no tiene.

---

## 23. ENGINE-5 — Independent Security + Reviewer Agents

ENGINE-5 añade los dos roles que faltaban en la cadena: **Security** y **Reviewer**. La
regla estructural que los separa de todo lo anterior:

```
Developer ≠ QA ≠ Security ≠ Reviewer
```

Pueden usar el mismo proveedor y el mismo modelo, pero son roles, interfaces, prompts y
contratos **separados**. Y ninguno puede anular el veredicto de otro.

```
Developer → Sandbox → QA → Security → Reviewer → CAMUS
```

ENGINE-5 construye los agentes y permite a CAMUS invocarlos **explícitamente**. El
workflow automático de reparación (Developer → QA → reparación → QA → Security → …) llega
en ENGINE-6.

### Separación de roles

| Rol | Responde | No hace |
| --- | --- | --- |
| Developer | ¿cómo lo implemento? | no decide si cumple el contrato |
| QA | ¿funciona? | no modifica producción, no declara PASS |
| Security | ¿es seguro usarlo? | no modifica nada, no declara el estado |
| Reviewer | ¿está listo para aceptarse? | no ejecuta código, no reescribe los informes de otros |

El PASS del Developer y el PASS de QA viajan a Security y al Reviewer como **contexto**,
nunca como prueba: que algo funcione no lo hace seguro, y que sea seguro no lo hace
correcto.

### Security Agent

| Componente | Archivo | Responsabilidad |
| --- | --- | --- |
| `SecurityRunner` | `src/punto/security/base.py` | Interfaz provider-agnostic |
| `DeepSeekSecurityRunner` | `src/punto/security/deepseek.py` | Implementación real |
| Esquemas | `src/punto/schemas/security.py` | `SecurityTask`, `SecurityPlan`, `SecurityFinding`, `SecurityReport` |
| Checks deterministas | `src/punto/security/deterministic.py` | Análisis implementado por PUNTO |
| Registry cerrado | `src/punto/security/checks.py` | Qué se ejecuta y qué no existe |
| Validación | `src/punto/security/validation.py` | Invariantes del plan y de los hallazgos |
| Veredicto | `src/punto/security/report.py` | Deduplicación y estado determinista |

**Plan.** El modelo declara qué archivos revisa (`review_targets`), con qué áreas
(`analysis_areas` de un catálogo de 15: autenticación, autorización, validación de entrada,
inyección, secretos, criptografía, exposición de datos, riesgo de dependencias, red,
sistema de archivos, manejo de errores, registro, privacidad, configuración y cadena de
suministro), qué amenazas busca y qué checks registrados quiere que PUNTO ejecute. No se
exige que todas las áreas apliquen a toda tarea.

**Hallazgos.** Con `severity`, `category`, `file`, `line`, `evidence`, `impact`,
`recommendation`, `confidence` y `sources`. La **evidencia es obligatoria** y el archivo
debe existir dentro del contexto que el agente efectivamente vio: un hallazgo sobre un
archivo que nadie revisó no es un hallazgo, es una invención. Una línea fuera del archivo
se descarta, pero el hallazgo se conserva: perder evidencia de seguridad por un desfase de
una línea sería el peor de los trueques.

**Deduplicación sin perder fuentes.** Si el modelo y un check determinista encuentran el
mismo problema, es **un** hallazgo con **dos** fuentes: la coincidencia es evidencia más
fuerte, no ruido. Al fundir se conserva la gravedad más alta.

### Frontera de contexto del modelo (ENGINE-5.1)

Un hallazgo solo vale si el agente **recibió** el archivo. Eso no puede depender de que el
constructor del prompt y el validador coincidan por casualidad, así que hay una sola fuente
determinista: `src/punto/model_context.py`.

`build_model_review_context(workspace, paths)` devuelve, para un conjunto de rutas:

| Campo | Qué es |
| --- | --- |
| `visible_paths` | Los archivos cuyo contenido se envió al modelo, en orden |
| `content` | El texto exacto que el modelo recibió |
| `omitted_paths` | Lo que **no** se envió, declarado; nunca omitido en silencio |
| `unsafe_paths` | Lo excluido porque su destino real **escapa** del workspace |
| `truncated_paths` | Los archivos enviados con recorte, declarado |
| `line_counts` | Cuántas líneas de cada archivo visible pudo ver el modelo |

Y los validadores usan **ese** conjunto, no la lista declarada por la tarea:

- un hallazgo de `MODEL_REVIEW` sobre un archivo que no está en `visible_paths` se **rechaza**;
- un hallazgo `DETERMINISTIC_CHECK` no lo necesita: su evidencia la produce PUNTO.

**La frontera de ruta es léxica y de destino.** `normalize_relative_path()` bloquea rutas
absolutas y `..`, pero eso no basta: una ruta relativa perfectamente válida puede ser un
enlace que sale del workspace. Antes de leer nada se resuelve el destino real —siguiendo
symlinks y junctions— y se exige que siga dentro de la raíz resuelta:

```python
resolved_candidate.is_relative_to(resolved_workspace)
```

Un enlace **interno** se acepta y se lee; uno que **escape** no se lee nunca, y ni su
contenido ni su destino real llegan al prompt, al informe ni a la evidencia: la ruta declarada
queda en `unsafe_paths` y el archivo se declara omitido. Como un archivo modificado omitido
bloquea la revisión y un objetivo omitido bloquea la auditoría, un enlace que escapa no puede
terminar aprobado por accidente.

La regla vive en un solo sitio (`resolve_within_workspace`) y hay una prueba que exige que
coincida con las dos implementaciones que ya existían en el motor
(`ExecutionContext.resolve_path` del Developer y `SecurityCheckContext.readable` de los checks
deterministas) sobre los mismos fixtures, incluido un enlace que escapa.

Dos reglas que cierran los huecos que tenía la fase anterior:

1. **Allowlist vacía = no autorizar nada**, nunca autorizar todo. Si la tarea no declara
   `changed_files` ni `context_files`, ningún objetivo de revisión es admisible, y ninguna
   auditoría con ese contexto puede terminar en PASS.
2. **Nada se omite en silencio.** Si el plan de seguridad no cabe en el presupuesto del
   modelo (30 archivos), la auditoría es `BLOCKED` / `CONTEXT_LIMIT_EXCEEDED`. Si al Reviewer
   le falta un archivo **modificado**, la revisión es `BLOCKED`: una revisión parcial no se
   aprueba como si fuera completa. Los `context_files` auxiliares que no caben se declaran
   en `omitted_files` y no bloquean, pero tampoco se pueden citar como si se hubieran leído.

Security construye el contexto desde los objetivos **del plan**, no recortando la lista
autorizada: si el plan apunta a un archivo, ese archivo se envía. Y los checks deterministas
siguen inspeccionando todo el contexto autorizado que su límite permita, porque su evidencia
no depende del modelo.

### Checks deterministas

Implementados por PUNTO. Inspeccionan datos, **no ejecutan código del proyecto**, así que
corren en proceso confiable:

| Check | Qué busca |
| --- | --- |
| `secret-pattern-scan` | Claves de proveedor, tokens `Bearer`, bloques de clave privada, contraseñas y URLs con credenciales |
| `dangerous-path-scan` | Rutas del sistema y permisos peligrosos (`chmod 777`, `/etc/shadow`, claves SSH) |
| `python-ast-security` | `eval`, `exec`, `os.system`, `os.popen`, `pickle.loads`, `yaml.load` sin loader seguro y `subprocess` con `shell=True` |
| `dependency-manifest-inspection` | Dependencias sin versión fijada, **sin red** |

El escáner de secretos **evita los falsos positivos obvios**: canarios de prueba,
placeholders de `.env.example` y ejemplos de documentación no son hallazgos. Marcarlos
bloquearía el motor por su propio material didáctico. Y reporta; **nunca elimina** nada.

Los análisis de AST son **evidencia conservadora, no una explotación demostrada**:
`shell=True` y la ejecución dinámica son `HIGH`; el resto, `MEDIUM`. No sustituyen a un
scanner industrial y no se presentan como si lo hicieran.

### Scanners que PUNTO no tiene

`bandit`, `semgrep`, `trivy`, `npm-audit` y `osv-scanner` están **registrados pero no
disponibles**. Pedirlos produce `BLOCKED` / `CAPABILITY_REQUIRED` con el hueco declarado.
**Nunca** se ejecutan en el host como sustituto, y nunca se finge que los checks internos
equivalen a ellos.

### Estado de seguridad

| Situación | Estado |
| --- | --- |
| Algún hallazgo `HIGH` o `CRITICAL` | **FAIL** |
| Solo `MEDIUM`, `LOW` o `INFO` | **PASS**, con los hallazgos informados y conservados |
| Check pedido no disponible, plan irrecuperable o análisis no ejecutable | **BLOCKED** |

El modelo **no** puede escribir el estado: el contrato del plan y de los hallazgos rechaza
cualquier clave desconocida, incluida `status`. Y un producto vulnerable **no se repara**:
se reporta. El bucle de reparación solo existe para un plan o un hallazgo estructuralmente
inválidos.

### Reviewer Agent

| Componente | Archivo | Responsabilidad |
| --- | --- | --- |
| `ReviewerRunner` | `src/punto/reviewer/base.py` | Interfaz provider-agnostic |
| `DeepSeekReviewerRunner` | `src/punto/reviewer/deepseek.py` | Implementación real |
| Esquemas | `src/punto/schemas/review.py` | `ReviewTask`, `ReviewProposal`, `ReviewFinding`, `ReviewReport` |
| **Gates** | `src/punto/reviewer/gates.py` | Las reglas que el modelo no puede anular |

El Reviewer evalúa corrección, mantenibilidad, conformidad arquitectónica, disciplina de
alcance, riesgo de regresión, claridad, consistencia, deuda técnica introducida,
adecuación de las pruebas y disposición de seguridad. **No ejecuta código**: QA y Security
ya lo hicieron.

Sus hallazgos usan categorías propias (`CORRECTNESS`, `ARCHITECTURE`, `MAINTAINABILITY`,
`SCOPE`, `TESTING`, `PERFORMANCE`, `COMPATIBILITY`, `DOCUMENTATION`, `TECHNICAL_DEBT`) y
**no duplican** los de seguridad: los **referencian** por identificador.

### Gates no anulables

Esto es lo que hace que el Reviewer sea un rol y no un opinador. Las reglas viven en código:

| Gate | Condición | Efecto |
| --- | --- | --- |
| QA | `QAStatus != PASS` | no puede aprobar (`CHANGES_REQUESTED`) |
| QA | `QAStatus == BLOCKED` | **BLOCKED** |
| Security | `SecurityStatus == FAIL` | no puede aprobar (`CHANGES_REQUESTED`) |
| Security | `SecurityStatus == BLOCKED` | **BLOCKED** |
| Hallazgos | algún `HIGH`/`CRITICAL` de revisión | **CHANGES_REQUESTED** |
| Falta un informe | sin QA o sin Security | **BLOCKED** |
| Todo en verde | y sin hallazgo bloqueante | **APPROVED** |

Precedencia: bloqueante > no superado > aprobado. El modelo puede aportar hallazgos y
valoraciones; **no** puede escribir el veredicto, y ninguna propuesta suya —por limpia que
sea— convierte un QA fallido o un Security fallido en una aprobación. Hay pruebas
parametrizadas que lo demuestran caso por caso.

### Evaluación completa

`TaskEvaluation` reúne los cuatro resultados de una tarea (`developer_result`, `qa_report`,
`security_report`, `review_report`) y expone sus veredictos. Es **solo** una estructura: no
encadena roles ni dispara nada. Existe para facilitar ENGINE-6 sin diseñar ahora el
workflow.

### CAMUS

| Operación | Qué hace |
| --- | --- |
| `security_task(task)` | Delega la auditoría en el `SecurityRunner` y audita cada hallazgo |
| `review_task(task)` | Delega la revisión, y registra **cada gate** con su motivo |
| `evaluate_task(...)` | Agrupa informes existentes en una `TaskEvaluation`; no ejecuta nada |

Sin el rol inyectado, la operación falla de forma explícita. **No hay reparación
automática**: ENGINE-5 no lanza un Developer nuevo cuando algo falla.

### Auditoría

Security: `SECURITY_REQUEST_STARTED`, `SECURITY_PLAN_RECEIVED`,
`SECURITY_PLAN_REJECTED`, `SECURITY_PLAN_ACCEPTED`, `SECURITY_CHECK_STARTED`,
`SECURITY_CHECK_COMPLETED`, `SECURITY_FINDING_RECORDED`, `SECURITY_COMPLETED` y
`SECURITY_BLOCKED`.

Reviewer: `REVIEW_REQUEST_STARTED`, `REVIEW_PROPOSAL_RECEIVED`,
`REVIEW_PROPOSAL_REJECTED`, `REVIEW_PROPOSAL_ACCEPTED`, `REVIEW_FINDING_RECORDED`,
`REVIEW_COMPLETED` y `REVIEW_BLOCKED`. Cada gate se audita por separado: si alguien
pregunta por qué un cambio no se aprobó, la respuesta está en el registro.

Se registran recuentos, gravedades y motivos —nunca la credencial, el código del proyecto
ni el de los checks—, y el inicio registra el PASS declarado por el Developer y el estado
de QA, precisamente para poder auditar que Security no los usó como prueba.

### Generalidad

Security y Reviewer planifican y evalúan conceptualmente Python, Next.js/TypeScript y CLI
con el mismo código. Un proyecto Node que pida `npm-audit` queda `BLOCKED` por capacidad,
**no** por un fallback al host.

### Configuración

Cada rol independiente apunta a su propio modelo; si la variable no existe, usa
`deepseek-v4-pro`. La credencial (`DEEPSEEK_API_KEY`) sigue viviendo solo en el proceso que
llama al modelo: no entra al sandbox, ni al workspace, ni al código generado, ni al registro
de auditoría.

| Variable | Rol |
| --- | --- |
| `PUNTO_SECURITY_MODEL` | Modelo del Security Agent |
| `PUNTO_REVIEWER_MODEL` | Modelo del Reviewer Agent |

### Pruebas y gate vivo

| Comando | Qué cubre |
| --- | --- |
| `pytest` | Suite completa: checks deterministas, validación, deduplicación, estados, gates, runners, CAMUS y generalidad |
| `pytest tests/test_context_boundary.py -q` | **Frontera de contexto (ENGINE-5.1)**: allowlist vacía, contexto omitido, visibilidad de hallazgos y revisión incompleta |
| `pytest tests/integration/test_engine5_live.py -q` | **Gate vivo**: caso vulnerable rechazado y caso corregido aprobado, con los prompts de producción |

El gate vivo evalúa el **mismo** cambio en dos versiones con DeepSeek real, sandbox real y
la cadena completa QA → Security → Reviewer:

- **Caso A, vulnerable**: `subprocess.check_output(command, shell=True)`. QA demuestra que
  la función *funciona* y declara PASS; Security encuentra la inyección de comandos y
  declara FAIL; el Reviewer **no puede aprobar**;
- **Caso B, corregido**: `shlex` + lista de comandos permitidos + argumentos estructurados
  con `shell=False` + `timeout`. QA PASS, Security PASS, Reviewer aprueba.

Ese contraste es el objetivo entero de la fase.

La primera versión del caso B solo cambiaba `shell=True` por `shell=False`, y el gate vivo la
**rechazó**: Security reportó `CRITICAL` por seguir ejecutando comandos arbitrarios sin lista
de permitidos, más `MEDIUM` por no acotar el tiempo de ejecución. El motor tenía razón: quitar
una vulnerabilidad no vuelve seguro un diseño que sigue aceptando cualquier comando. El fixture
se corrigió cerrando el riesgo, no relajando la puerta.

### API

Sin cambios: Security y Reviewer no se exponen por HTTP. `GET /health` → 200 y la misma
superficie de rutas.

### Limitación declarada

- ENGINE-5 **no** encadena los roles: cada uno se invoca explícitamente. El workflow
  automático es ENGINE-6.
- Los checks deterministas son evidencia acotada, **no** un scanner industrial. Bandit,
  Semgrep, Trivy, `npm audit` y `osv-scanner` no están disponibles y no se simulan.
- No hay análisis dinámico ni de secretos históricos (git history): solo el contenido
  actual del contexto autorizado.
- El Reviewer no puede verificar nada que QA y Security no hayan cubierto: su valor está en
  la coherencia global, no en repetir la ejecución.
- El presupuesto de contexto del modelo es de 30 archivos por revisión y **no** hay batching
  por lotes (ENGINE-5.1 prefirió la opción simple y segura): por encima del límite, Security
  bloquea la auditoría y el Reviewer bloquea la revisión si le faltó un archivo modificado.
  Un cambio que no quepa en una sola pasada necesita ENGINE-6, no una aprobación a medias.
- Los `context_files` auxiliares del Reviewer que no caben en el contexto se declaran en
  `omitted_files` y no bloquean; lo que sí es imposible es citarlos como si se hubieran leído.

---

## 24. ENGINE-5.2 — Multi-Provider + Anthropic/Claude + Cross-Model Audit

ENGINE-5.2 convierte a PUNTO en un motor **multi-proveedor**. Hasta aquí el motor hablaba con
DeepSeek mediante un cliente concreto; ahora hay dos proveedores reales coordinados por CAMUS:

```
CAMUS
├── DeepSeek provider    → ARCHITECT, PLANNER, DEVELOPER, QA, SECURITY, REVIEWER
└── Anthropic provider   → CROSS_AUDITOR (+ VISUAL_ARCHITECT, FRONTEND_SPECIALIST, VISUAL_QA)
```

Claude **no** se integra «dentro de DeepSeek»: son clientes independientes detrás de un
contrato común, y cada rol se enruta de forma explícita.

> **ENGINE-5.2 IMPLEMENTATION = PASS · CLAUDE LIVE = PENDING_API_KEY · FINAL CLOSE = PENDING.**
> *Implementation PASS does not mean Anthropic live connectivity has been verified.* La fase se
> construyó y se probó sin credencial de Anthropic: la suite estándar usa un transporte falso
> fiel al contrato real, y las cinco puertas vivas quedan pendientes hasta que exista
> `ANTHROPIC_API_KEY`.

### Contrato provider-neutral

| Componente | Archivo | Responsabilidad |
| --- | --- | --- |
| `StructuredModelClient` | `src/punto/providers/base.py` | Interfaz mínima: `provider`, `model`, `complete_json`, `redact`, `close` |
| `MultimodalModelClient` | `src/punto/providers/base.py` | Añade `complete_multimodal_json` y `supports_images` |
| `ModelCompletion` | `src/punto/providers/base.py` | Resultado común: contenido, proveedor, modelo, usage, latencia, reintentos, `stop_reason`, `request_id` |
| `ImagePayload` / `ImageLimits` | `src/punto/providers/base.py` | Bytes que controla PUNTO y validación determinista previa |
| `JsonSchema` | `src/punto/providers/base.py` | Esquema opcional que la respuesta debe cumplir, si el proveedor puede aplicarlo |
| `prepare_json_schema` | `src/punto/providers/json_schema.py` | Inlinea `$ref`, quita `$defs` y exige el contrato de PUNTO |

`ModelCompletion` se **extrajo** del cliente de DeepSeek sin romper nada: `punto.providers
.deepseek` la reexporta, así que `from punto.providers.deepseek import ModelCompletion` sigue
funcionando y los seis agentes existentes no cambiaron. Lo que sí se añadió es `provider` y
`request_id` (con default, sin migración) para que el informe pueda decir **quién** respondió.

Nota documentada: `stop_reason` devuelve el valor **nativo** del proveedor. DeepSeek informa
`length` y Anthropic `max_tokens` para el mismo fenómeno; traducirlo exigiría verificar en vivo
la semántica de cada uno, y una tabla inventada sería peor que la asimetría declarada.

### Structured Outputs y dialecto del proveedor (ENGINE-5.2.1 · 5.2.2)

El formato no depende de que el prompt pida JSON por favor. `complete_json` y
`complete_multimodal_json` aceptan `json_schema`, y cada proveedor lo cumple según su
primitiva real:

| Proveedor | Cómo lo aplica |
| --- | --- |
| Anthropic | `output_config.format = {"type": "json_schema", "schema": <provider schema>}` en la petición; sin `output_format` antiguo y sin *assistant prefill* |
| DeepSeek | Conserva `response_format: json_object` y recibe el esquema como contrato **textual** en el prompt de sistema: no se finge una garantía que su API no da |

**Dos esquemas, dos responsabilidades.** El **original** (`Model.model_json_schema()`) es el
contrato de PUNTO y no se toca: `Model.model_validate(...)` lo sigue aplicando entero. El
**provider schema** es la versión compatible que viaja al proveedor, y la produce
`provider_schema_for(Modelo)`:

1. **inlinea** las referencias locales (`$ref` a `$defs`) y elimina `$defs`, para enviar un
   esquema autocontenido en vez de uno que el proveedor tenga que resolver;
2. los **siblings** de un `$ref` solo pueden anotar (`title`, `description`, `default`). Un
   sibling estructural que contradiga o amplíe el destino es un **conflicto** y se rechaza: no
   se elige uno de los dos en silencio;
3. **retira** lo que el dialecto del proveedor no admite y lo cuenta en la `description` del
   campo. La lista es explícita: `minLength`, `maxLength`, `minimum`, `maximum`,
   `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`, `maxItems`, `uniqueItems`,
   `minProperties`, `maxProperties`, `pattern` (PUNTO no puede demostrar que domina el
   subconjunto regex del proveedor, así que no lo envía) y `minItems > 1`. Sí viajan `anyOf`,
   `allOf`, `enum`, `const`, `default`, `required`, `minItems` con 0 o 1, `format` de la
   allowlist documentada (`date`, `date-time`, `duration`, `email`, `hostname`, `ipv4`, `ipv6`,
   `time`, `uri`, `uuid`) y `additionalProperties: false`. Una lista negra improvisada
   destruiría features válidas;
4. **rechaza** ciclos de referencias, referencias irresolubles y profundidades absurdas;
5. valida **todos** los nodos, no solo la raíz: un `minLength` escondido en el `items` de un
   array anidado rompería la petición igual que uno en la raíz. Cada objeto debe estar cerrado
   (`additionalProperties: false`, y **solo** `false`: el dialecto no admite mapas), cada array
   debe declarar `items` **con esquema**, cada unión debe ser una lista no vacía de esquemas,
   `enum`/`const` solo admiten escalares, `type` debe ser uno de los siete tipos básicos y
   ningún keyword desconocido llega al proveedor. Un campo `dict[str, X]` de Pydantic, por
   ejemplo, falla aquí antes de llamar a la API;
6. exige los **límites de complejidad** documentados por el proveedor: como máximo **24
   parámetros opcionales** (propiedades que no están en el `required` de su objeto) y **16
   parámetros de unión** (cada nodo con `anyOf`). El contrato de producción usa 14 y 1; un
   esquema que supere el límite se rechaza en PUNTO, no con un 400 de la API.

Un esquema que no cumple falla en PUNTO con `SchemaValidationError` y **cero** peticiones HTTP.

La normalización de rutas comprueba los **caracteres de control (0x00-0x1F y 0x7F) sobre la
cadena original**, antes de recortar espacios: un `\t` o un `\n` en el borde de una ruta
desaparecería con `strip()` y la ruta pasaría como limpia. El espacio ordinario del borde sí se
recorta, porque no es un carácter de control.

Retirar una restricción del provider schema **no** relaja nada: `Field(min_length=1)` se anota
en la descripción para el modelo, pero `model_validate(...)` sigue rechazando la cadena vacía.
Structured Outputs **reduce** errores de formato; Pydantic decide la validez final, y el bucle
de reparación semántica se conserva para lo que un esquema no puede cubrir (evidencia,
visibilidad de archivos, referencias a hallazgos y los invariantes propios de PUNTO).

### Cliente de Anthropic

`src/punto/providers/anthropic.py` habla la **Messages API nativa** (`POST /v1/messages`,
`x-api-key`, `anthropic-version: 2023-06-01`), no una traducción al dialecto de OpenAI.

| Aspecto | Comportamiento |
| --- | --- |
| Errores | `AnthropicAuthenticationError` (401/403), `AnthropicRateLimitError` (429), `AnthropicServerError` (5xx/529), `AnthropicProviderError` (resto 4xx), `AnthropicTransportError` / `AnthropicTimeoutError`, `AnthropicInvalidResponseError`, `AnthropicTruncatedResponseError`, `AnthropicRefusalError` |
| Reintentos | 401/403 **nunca** (una sola llamada); 429/5xx/529/timeout/red con espera acotada |
| Espera | Un `retry-after` válido manda; ausente o ilegible usa backoff exponencial; por encima de 60 s se falla de forma explícita en vez de bloquear el proceso. Nunca se copian cabeceras a mensajes ni a registros |
| Motivos de parada | `max_tokens` y `model_context_window_exceeded` ⇒ truncamiento (capacidad, nunca «JSON roto»); `refusal` ⇒ `AnthropicRefusalError`, detectado **antes** de interpretar el contenido |
| Redacción | clave exacta, cualquier `sk-ant-...`, `x-api-key: ...` y `Bearer ...`; el detalle HTTP se redacta **antes** de recortarlo, y todo lo que se persiste o se reporta pasa por ella |
| Credencial | solo en el proceso que llama: no entra al sandbox, ni al workspace, ni al prompt, ni al registro de auditoría |

La jerarquía se diseñó para ser **equivalente**, no idéntica, a la de DeepSeek: añade
`AnthropicTimeoutError` (subclase de transporte) y `AnthropicServerError` (subclase de
proveedor) porque distinguir un 500 de un 400 es información útil. Además,
`AnthropicAuthenticationError` hereda de `ProviderAuthenticationError` y
`AnthropicRefusalError` de `ProviderRefusalError`, así que el motor puede tratarlas sin conocer
a Anthropic. Una negativa **no** se reintenta con el mismo prompt ni se sustituye el proveedor:
en la auditoría cruzada produce `BLOCKED` con causa propia, `PROVIDER_REFUSAL`.

### Fundación multimodal

El contrato acepta `texto + imágenes → JSON estructurado`, con los formatos `image/png`,
`image/jpeg` y `image/webp`. PUNTO controla los bytes: el modelo **nunca** recibe una ruta para
leer archivos por su cuenta.

Los límites se validan de forma determinista **antes** de construir cualquier petición (por
defecto: 8 imágenes, 5 MB por imagen, 15 MB en total). Un límite personalizado puede
**estrechar** lo permitido, nunca ampliarlo: si PUNTO declara que soporta tres formatos,
aceptar `text/plain` en la configuración sería contradecir su propio contrato.

Todavía **no** hay screenshots reales: eso es ENGINE-5.3. Lo que está listo y probado es todo
el camino de construcción con transporte falso, para que conectar Chromium sea añadir el
productor de imágenes, no rediseñar el transporte.

### Routing por rol

| Rol | Proveedor por defecto | Modelo por defecto |
| --- | --- | --- |
| ARCHITECT, PLANNER, DEVELOPER, QA, SECURITY, REVIEWER | `deepseek` | `deepseek-v4-pro` |
| CROSS_AUDITOR | `anthropic` | `claude-opus-5` |
| VISUAL_ARCHITECT, FRONTEND_SPECIALIST, VISUAL_QA | `anthropic` | `claude-sonnet-5` (preparados; runner real en ENGINE-5.3) |

`ModelRouter` resuelve la ruta de cada rol desde el entorno (`PUNTO_<ROL>_MODEL` y
`PUNTO_<ROL>_PROVIDER`), valida que haya exactamente una ruta por rol y expone
`is_cross_model(roles)`.

### Sin fallback silencioso

La regla más importante de la fase:

| Situación | Resultado |
| --- | --- |
| `provider=anthropic` y no hay cliente de Anthropic | `PROVIDER_UNAVAILABLE`; **nunca** DeepSeek |
| `provider=deepseek` y no hay cliente de DeepSeek | `PROVIDER_UNAVAILABLE`; **nunca** Anthropic |
| El cliente declarado usa **otro modelo** que la ruta | `PROVIDER_MODEL_MISMATCH`; la ruta y lo que se ejecuta no divergen en silencio |

Un fallback automático convertiría la auditoría cruzada en una auditoría del mismo modelo, que
es exactamente lo que deja de tener valor. Si algún día hace falta un fallback, tendrá que ser
una política explícita de CAMUS, no una conveniencia del cliente.

### Auditoría cruzada

`CrossAuditRunner` (abstracto, provider-agnostic) y `ClaudeCrossModelAuditRunner`
(`src/punto/crossaudit/claude.py`). Su función es auditar **después** de Developer, QA, Security
y Reviewer, con un proveedor distinto. No sustituye al Reviewer, no ejecuta código, no modifica
archivos, no hace commits y no repara el producto.

**Gates que Claude no puede anular** (`src/punto/crossaudit/gates.py`):

| Gate | Condición | Efecto |
| --- | --- | --- |
| QA | sin informe o `BLOCKED` | **BLOCKED** |
| QA | `!= PASS` | `CHANGES_REQUESTED` (nunca PASS) |
| Security | sin informe o `BLOCKED` | **BLOCKED** |
| Security | `!= PASS` | `CHANGES_REQUESTED` (nunca PASS) |
| Reviewer | sin informe o `BLOCKED` | **BLOCKED** |
| Reviewer | `!= APPROVED` | `CHANGES_REQUESTED` |
| Contexto | falta un archivo modificado | **BLOCKED** |
| Proveedor | no disponible o error | **BLOCKED** |
| Hallazgos | alguno `HIGH`/`CRITICAL` | `CHANGES_REQUESTED` |
| Todo verde | contexto completo y propuesta válida | **PASS** |

Precedencia: bloqueante > cambios pedidos > PASS. El veredicto lo calcula PUNTO y
`CrossAuditProposal` **no** admite `status`: si el modelo intenta escribirlo, la propuesta se
rechaza y se le pide de nuevo.

El informe guarda `provider`, `model`, `upstream_providers` y `cross_model`, calculado como un
hecho: si todos los proveedores coinciden, `cross_model` es `False` y el informe lo dice en
lugar de adornarse con la palabra «cruzada».

### Fronteras de contexto

La auditoría cruzada **reutiliza** `build_model_review_context()` y
`resolve_within_workspace()` de ENGINE-5.1/5.1.1: no duplica lectura de archivos. Un hallazgo
solo puede señalar archivos de `model_visible_files`, los enlaces que escapan del workspace no
se leen nunca, y si un archivo modificado no cabe en el contexto la auditoría queda `BLOCKED` /
`CONTEXT_LIMIT_EXCEEDED`. No hay auditoría parcial disfrazada.

Una ruta declarada **inválida** (carácter de control, `..`, absoluta) tampoco desaparece: se
registra en `invalid_paths` y en las omisiones con una representación saneada —nunca con los
caracteres de control crudos— y, si era un archivo obligatorio, cuenta como no cubierto. La
auditoría queda `BLOCKED` **antes** de llamar al modelo.

Un archivo **obligatorio que no existe** tampoco pasa por contexto completo: aparece como
`(no existe)` en el prompt —el modelo debe saberlo— pero se registra en `absent_paths`, cuenta
como omitido y `missing_paths` lo devuelve como no cubierto. La revisión se bloquea sin llamar
al modelo. La única excepción es una **eliminación declarada explícitamente** por la tarea
(`deleted_files`): PUNTO no deduce un borrado de la ausencia de un archivo, porque «no está» y
«se borró a propósito» no son lo mismo.

### Estado live

| Gate vivo | Estado |
| --- | --- |
| A. Autenticación | PENDING_API_KEY |
| B. JSON estructurado | PENDING_API_KEY |
| C. Credencial inválida | PENDING_API_KEY |
| D. Multimodal | PENDING_API_KEY |
| E. Auditoría cruzada | PENDING_API_KEY |

```
pytest tests/integration/test_anthropic_live.py -q -s
```

Sin `ANTHROPIC_API_KEY` esa suite **no se ejecuta** y no se declara nada: no se inventan
resultados, no se simula que Claude respondió y no se pide la credencial por el chat. La suite
estándar no depende de ella (vive en `tests/integration`, que el `addopts` ignora) y mantiene
**0 failed / 0 skipped**.

Los gates vivos están endurecidos para no poder confundir un bloqueo con un éxito:

| Gate | Exigencia |
| --- | --- |
| A. Autenticación | respuesta real con `provider=anthropic`, tokens > 0 |
| B. JSON estructurado | el **esquema de producción** (`provider_schema_for(CrossAuditProposal)`) en `output_config.format` y `json.loads` del contenido **sin** quitar vallas: un dialecto incompatible daría 400 y el gate falla. Un esquema simple complementario también se prueba |
| C. Credencial inválida | `AnthropicAuthenticationError` con **cero** reintentos |
| D. Multimodal | texto + imagen + esquema, y un `image_received is True` que el esquema obliga a responder. Demuestra **transporte y estructura**, no reconocimiento visual ni estética |
| E. Auditoría cruzada | el fixture limpio debe dar **PASS**; `BLOCKED` y `CHANGES_REQUESTED` **no** se aceptan como éxito, y se exige `provider == "anthropic"`, `model == cliente.model`, `cross_model is True` y tokens > 0. El fixture es una función **pura** de normalización de texto: sin subprocess, efectos, red, `eval`/`exec`, import dinámico, shell ni credenciales, para que el veredicto no dependa de una discusión de seguridad ambigua |

### Limitación declarada

- ENGINE-5.2 **no** implementa runtime Node, Next.js, TypeScript, Tailwind, Playwright ni
  Chromium, ni el especialista frontend, ni Visual QA completo: es ENGINE-5.3.
- Los identificadores por defecto (`claude-opus-5` para la auditoría, `claude-sonnet-5` para
  los roles visuales) son los **documentados** por el proveedor: `MODEL_ID_DOCUMENTED` es
  `True`. Lo que sigue sin poder afirmarse es el **acceso de esta cuenta** a ellos
  (`LIVE_ACCOUNT_ACCESS_UNVERIFIED`), porque la fase se construyó sin credencial. No hay lista
  blanca: un identificador equivocado se verá como un 404 explícito del proveedor.
- El provider schema usa `anyOf` para los campos opcionales (`line`, `confidence`), que es lo
  que produce Pydantic para `int | None`, y conserva `default`. Ese detalle del dialecto solo lo
  puede confirmar la API real: lo verifica el gate vivo B con el **esquema de producción**
  completo de `CrossAuditProposal`, no con uno trivial.
- Los límites multimodales se miden en **bytes crudos** de la imagen, mientras el transporte la
  envía en base64 (≈33 % más). No se deben «igualar» a los 10 MB que publica la API pensando que
  son la misma unidad.
- Los tres roles visuales están **preparados** (ruta declarada) pero **sin runner**: sus
  interfaces llegarán en ENGINE-5.3.
- No hay routing autónomo ni bucle de reparación: eso es ENGINE-6.
- Sin fallback entre proveedores, por diseño. Un proveedor caído bloquea; no se sustituye.

---

## 25. ENGINE-5.3 — Web + Visual Execution Foundation

ENGINE-5.3 cierra el eslabón que faltaba entre «el código compila» y «la interfaz se ve y se
comporta»: un **perfil de capacidad web**, un **sandbox con navegador real**, **once
comprobaciones deterministas** y **Visual QA** con Claude, con el veredicto calculado por PUNTO.

Lo que esta fase **no** hace: no ejecuta un pipeline autónomo, no repara lo que encuentra, no
cambia de proveedor cuando uno falla, no despliega y no necesita ninguna credencial para
funcionar. La API HTTP no cambia.

### Perfil de capacidad web

`detect_web_project(workspace)` (`src/punto/web/detection.py`) lee el proyecto del workspace y
devuelve un `WebProjectProfile` con **evidencia** de cada hallazgo: framework, gestor de
paquetes, lockfile, TypeScript, Tailwind, scripts, dependencias y requisito de Node.

| Dato | Regla determinista |
| --- | --- |
| Framework | prioridad `NEXTJS` > `VUE` > `SVELTE` > `ASTRO` > `REACT`; sin dependencia reconocible, `NONE` |
| Gestor de paquetes | `packageManager` del `package.json` manda; si no, prioridad de lockfile: `pnpm-lock.yaml` > `yarn.lock` > `bun.lock` > `bun.lockb` > `package-lock.json`; sin lockfile, `NPM` |
| Tailwind | v4 por `@import "tailwindcss"` en el CSS, o configuración explícita |
| TypeScript | `tsconfig.json` presente |
| Scripts | nombres declarados en `package.json`, acotados por el contrato |

Nada se infiere por olfato: si no hay evidencia, el campo dice que no la hay.

### Política de red

La regla de la fase, y la que más fácil es romper por conveniencia:

| Momento | Red | Por qué |
| --- | --- | --- |
| Sesión de navegador, build y comandos de proyecto | `--network none` | la ejecución del proyecto **nunca** navega a Internet; el *loopback* del contenedor sigue disponible y está verificado |
| Plan de dependencias (`INSTALL_DEPENDENCIES`) | no la pide | un plan de comando no puede ampliar permisos: la red la decide el backend del sandbox, no el plan |
| Resolución de dependencias | fuera de la sesión | resolver dependencias **no** es ejecutar el producto; en esta fase la sesión no tiene red general, así que un proyecto cuyas dependencias no estén ya disponibles no se puede construir. Es una limitación declarada, no un fallback silencioso |

`src/punto/web/commands.py` traduce acciones conceptuales a un `argv` controlado y **sin shell**:
no hay comando libre, no hay `eval`, no hay cadenas interpretadas por un shell. `TYPECHECK` y
`RUN_PLAYWRIGHT` usan `npx --no-install` para que una herramienta ausente sea un error explícito
y no una descarga por sorpresa.

### Sandbox web

La imagen la construye `sandbox/web/Containerfile`:

```
podman build -t localhost/punto-sandbox-web:0.1 sandbox/web/
```

Contiene Node v24.21.0, npm 11.19.0, Python 3.11.2, Playwright 1.63.0 y Chromium
153.0.8010.12 en `/ms-playwright`. La sesión corre con las propiedades endurecidas de ENGINE-1.R3,
todas explícitas: `--rm --network none --read-only --cap-drop ALL --security-opt
no-new-privileges --user 10001:10001`, `--shm-size` acotado, `/tmp` y `/home/punto` como tmpfs
con tamaño máximo, límites de memoria, CPU y PIDs, y **un solo montaje**: el workspace.

El runtime es **podman y solo podman**: es la frontera aprobada, no una preferencia. Pedir otro
runtime es un `WebCommandPolicyError` y no hay lista de candidatos que elija «el primero que
aparezca»; si podman no está, la operación se bloquea con `WEB_SANDBOX_REQUIRED`.
`BROWSER_SESSION_STARTED` deja escrito con qué runtime y con qué imagen se ejecutó de verdad.

Los comandos de proyecto y de preview pasan por una **allowlist de programas**
(`node`, `npm`, `npx`, `pnpm`, `yarn`, `bun`, `python`, `python3`) y se rechaza cualquier `argv[0]`
con ruta: la política de comandos vive en `commands.py`, y esto es la última puerta antes del
contenedor.

Dos hallazgos medidos que están documentados en el propio Containerfile porque costaron tiempo:
`/home/punto` necesita `mode=1777` (sin él, uid 10001 no puede escribir su HOME y el caché de npm
rompe cualquier build) y Chromium necesita `--no-sandbox` **dentro** del contenedor, que ya está
aislado por el runtime.

El navegador **nunca** se ejecuta en el host: si la imagen no está, la operación falla con
`WEB_SANDBOX_REQUIRED` y el comando exacto de construcción. No hay degradación a un modo sin
capturas.

### Once comprobaciones deterministas

`evaluate_web_checks()` (`src/punto/web/checks.py`) trabaja sobre las observaciones del probe y no
necesita navegador para probarse.

| Comprobación | Qué demuestra |
| --- | --- |
| `PAGE_LOAD_ERROR` | la página cargó y respondió con un estado aceptable |
| `CONSOLE_ERROR` | la consola no registró errores (las advertencias se informan, no suspenden) |
| `PAGE_ERROR` | no hubo excepciones no capturadas en la página |
| `FAILED_RESOURCE` | no hay recursos críticos caídos; el código HTTP viaja en la evidencia |
| `HORIZONTAL_OVERFLOW` | el documento no desborda el viewport (tolerancia por defecto: 1 px) |
| `VIEWPORT_CLIPPING` | el contenido no queda recortado por `overflow` |
| `BROKEN_IMAGE` | no hay imágenes que no se puedan mostrar |
| `MISSING_REQUIRED_ELEMENT` | los marcadores exigidos por la especificación están presentes |
| `HYDRATION_ERROR` | no hay señales de fallo de hidratación |
| `RESPONSIVE_CHECK` | cada ruta se midió en cada viewport exigido |
| `ACCESSIBILITY_CHECK` | título, idioma, `alt`, etiquetas, regiones y reglas de `axe` informadas |

Un `PASS` aquí **no** dice que la interfaz sea buena: dice que carga, no rompe y no desborda. La
comprobación sin señal devuelve `ran=False` y no suspende una página por algo que no se midió: se
declara como no ejecutada y se informa. El informe acota todo (50 mensajes de consola, 25 errores
de página, 25 recursos, 2000 caracteres por extracto) y cada hallazgo lleva su ruta, su viewport y
su evidencia.

### Capturas verificadas

Los tres viewports del contrato son `MOBILE` 390x844, `TABLET` 768x1024 y `DESKTOP` 1440x900, y
el máximo es de 8 capturas, alineado con `MAX_IMAGES` del contrato multimodal.

El host **no se cree nada** de lo que declara el probe: lee cada PNG, comprueba firma, dimensiones
(cabecera IHDR), tamaño y `sha256`, y lo compara con el manifiesto y con el viewport pedido. Un
byte de diferencia es un error explícito, y un par artefacto/bytes descuadrado nunca se convierte
en un `ImagePayload`. Un screenshot sin sus observaciones no se puede interpretar y unas
observaciones sin bytes no se pueden auditar, así que viajan juntos.

El nombre lógico de cada captura viene del manifiesto, así que se sanea antes de tocar disco: se
exige un nombre simple terminado en `.png`, sin separadores ni `..`, y además se comprueba que la
ruta resuelta siga dentro de la carpeta de capturas. Una ruta absoluta no puede acabar siendo el
`logical_name` de un artefacto.

Y la evidencia se contrasta con lo que el probe **publicó por stdout**: el probe imprime
`PUNTO_EVIDENCE_SHA256 <sha256>` al cerrar su manifiesto, y el host recalcula ese hash sobre el
archivo que lee. Si alguien reescribió el manifiesto después de que el probe lo cerrara, los dos
valores no coinciden y la sesión se bloquea sin aceptar ningún artefacto.

### Contratos visuales

`VisualSpec` es la respuesta a «no me digas que quede bonito»: rutas, viewports, elementos
obligatorios con marcador verificable, expectativas de responsive, accesibilidad, contenido y
notas. `VisualQATask` lleva además la sesión técnica ya medida, el objetivo, los criterios de
aceptación y el contexto de origen.

`VisualQAProposal` **no** admite `status`: el veredicto no lo escribe el modelo. Si intenta
escribirlo, la propuesta se rechaza y se le pide de nuevo, igual que en la auditoría cruzada.

### Gates y reglas de estado

| Gate | Condición | Efecto |
| --- | --- | --- |
| PROVIDER | el modelo visual no respondió | **BLOCKED** |
| SCREENSHOTS | falta una captura exigida | **BLOCKED** |
| TECHNICAL | la sesión web quedó `BLOCKED` | **BLOCKED** |
| TECHNICAL | hubo fallos deterministas medidos | `CHANGES_REQUESTED` (nunca PASS) |
| FINDINGS | hallazgo visual `HIGH`/`CRITICAL` | `CHANGES_REQUESTED` |
| Todo verde | sesión en verde, capturas completas, propuesta válida y sin hallazgos graves | **PASS** |

Precedencia: bloqueante > cambios pedidos > PASS. El estado técnico se calcula con una regla
explícita (`determine_web_status`): `BLOCKED` solo si no hubo nada que medir, `FAIL` si alguna
comprobación encontró un problema y `PASS` si todo lo que se ejecutó pasó. Un fallo bloqueante
medido en una página que sí se pudo mirar es `FAIL`, no `BLOCKED`: la página existía, y Visual QA
pedirá cambios en consecuencia.

### Runner visual

`ClaudeVisualQARunner` (`src/punto/visualqa/claude.py`) reutiliza **todo** lo aprobado en
ENGINE-5.2: el mismo cliente, el mismo contrato multimodal, el mismo dialecto de esquema y las
mismas reglas de proveedor caído, negativa y error. Lo nuevo es la materia prima: capturas en
lugar de archivos.

| Situación | Resultado |
| --- | --- |
| Más capturas de las que admite el contrato | `VISUAL_IMAGE_BUDGET_EXCEEDED`, **sin** llamar al modelo |
| Credencial inválida | `PROVIDER_UNAVAILABLE` (cero reintentos) |
| Negativa del modelo | `PROVIDER_REFUSAL` |
| Error del servidor o timeout | `PROVIDER_ERROR`, con reintentos acotados |
| Respuesta truncada | se detecta **antes** de interpretar el JSON y no filtra el texto recibido |
| Propuesta inválida | se rechaza, se dice por qué y se pide de nuevo (máximo 3 intentos, 4 llamadas) |

Los cuatro gates se evalúan **siempre**, incluso si el proveedor falla: un Claude caído no
convierte una página rota en un PASS. La credencial se redacta de todo lo que se persiste o se
informa.

### Flujo de extremo a extremo

`tests/integration/test_web_visual_fake_live.py` recorre el ciclo completo con **todo real
excepto la llamada HTTP a Anthropic**: proyecto real en el workspace, contenedor real, Chromium
real, capturas reales en los tres viewports, checks reales, informe real, tarea real y Claude
falso. Es la prueba de que las piezas encajan sin depender de una credencial.

El sandbox con navegador real se prueba en `tests/integration/test_web_browser_live.py`:

```
.\.venv\Scripts\python.exe -m pytest tests/integration/test_web_browser_live.py -q -s
```

Esa suite **no** se salta si falta la imagen: la ausencia del sandbox web es un bloqueo declarado
con el código `WEB_SANDBOX_REQUIRED`.

### Puente manual de Claude

Cuando exista `ANTHROPIC_API_KEY`, el puente es manual y explícito: se ejecuta
`tests/integration/test_anthropic_live.py` (gates A-E de ENGINE-5.2) y la suite visual viva. Sin
credencial no se ejecuta nada de eso y **no se declara nada**: no se inventan resultados, no se
simula que Claude respondió y no se pide la credencial por el chat.

### Estado live

| Gate vivo | Estado |
| --- | --- |
| A. Autenticación | PENDING_API_KEY |
| B. JSON estructurado | PENDING_API_KEY |
| C. Credencial inválida | PENDING_API_KEY |
| D. Multimodal | PENDING_API_KEY |
| E. Auditoría cruzada | PENDING_API_KEY |
| F. Visual QA con capturas reales | PENDING_API_KEY |

La suite estándar mantiene **0 failed / 0 skipped** y no necesita credencial ni Podman para pasar.

### Auditoría

Trece tipos de evento nuevos (WEB_PROFILE_DETECTED, WEB_BUILD_STARTED/COMPLETED,
BROWSER_SESSION_STARTED, BROWSER_CHECK_RECORDED, SCREENSHOT_CAPTURED,
VISUAL_QA_REQUEST_STARTED, VISUAL_QA_PROPOSAL_RECEIVED/REJECTED/ACCEPTED,
VISUAL_QA_FINDING_RECORDED, VISUAL_QA_COMPLETED y VISUAL_QA_BLOCKED) registran **metadatos**: qué
se detectó, qué se ejecutó, cuántas capturas se enviaron y con qué resultado. Nunca se registran
bytes de imagen, ni base64, ni rutas del host, ni la credencial.

Están cableados en las rutas que existen: el perfil detectado, el arranque de la sesión (con su
runtime y su imagen), cada comprobación determinista, cada captura verificada y todo el ciclo
visual. `WEB_BUILD_STARTED` pertenece al llamante que lanza la construcción —es el único que puede
afirmar que empezó— y en esta fase no hay pipeline autónomo que lo lance; queda declarado.

El detalle de un fallo también se sanea: el runtime escribe rutas del host en sus errores
(`Error: statfs /mnt/c/...`), y esas rutas se sustituyen por la que el contenedor conoce antes de
que el texto llegue a un informe.

### Hallazgos de la auditoría adversarial

La capa web se auditó en adversarial antes de cerrar la fase. Lo que se corrigió:

| Hallazgo | Corrección |
| --- | --- |
| El runtime se elegía entre podman y docker, y la sustitución no quedaba registrada | podman-only, con error explícito y runtime e imagen en el evento de sesión |
| El nombre de un screenshot venía del manifiesto sin sanear y se usaba como ruta | patrón de nombre simple más comprobación de contención en la carpeta de capturas |
| Sin ninguna comprobación medida, `determine_web_status` devolvía PASS | sin medición el estado es BLOCKED |
| El manifiesto de evidencia era escribible por el proyecto auditado y su autenticidad no se comprobaba | el digest viaja por stdout y el host lo contrasta con el archivo que lee |
| El `argv[0]` de los comandos solo se validaba en `commands.py` | allowlist de programas en el propio sandbox, sin rutas |
| El detalle de un fallo no estaba acotado y filtraba rutas del host | saneado y acotado antes de informar |
| Una nota de recorte vacía contaba como medición | una nota sin viewport del contrato no es una medición |
| La evidencia del informe copiaba las notas del probe sin límite | acotada nota a nota |
| Una URL con `@` en la contraseña dejaba parte de la credencial a la vista | el saneado borra toda la autoridad hasta el último `@` |

Lo que **no** se corrige en esta fase, y se declara:

- **El canal de evidencia vive en el workspace montado `rw`.** El probe escribe su manifiesto y sus
  PNG en una carpeta del workspace y el proyecto auditado corre en el mismo contenedor mientras
  se captura. El digest por stdout detecta que el manifiesto se reescriba **después** de que el
  probe lo cierre, pero un proyecto hostil que reescriba los PNG durante la captura podría, en
  teoría, fabricar evidencia coherente. Cerrarlo del todo exige un montaje o un proceso aparte que
  esta fase no introduce; queda como límite declarado, no como garantía.
- **Los checks confían en los campos por observación del probe.** El host no puede volver a medir
  sin navegador: un campo que el probe deje vacío se lee como «sin problema». `missing_markers`,
  `broken_images` y compañía llevan la misma forma, y el host no la puede reproducir.
- **La medición corre dentro de la página** (`page.evaluate`): una página que sobrescriba
  `getComputedStyle` o `scrollWidth` puede falsear sus propias medidas. Es inherente a observar con
  JavaScript en la página, y por eso un `PASS` técnico no sustituye a la revisión humana.
- Chromium corre con `--no-sandbox` **dentro** del contenedor: el contenedor es la frontera real,
  pero es defensa en profundidad que se pierde dentro.

### Generalidad, API y portabilidad

La capa web **no** depende conceptualmente de Next.js: el framework se detecta y se declara, pero
el perfil, los comandos, el navegador, los checks y Visual QA valen para cualquier stack web. No
hay endpoints nuevos: la API de ENGINE-0 a ENGINE-5.2 queda intacta. Tampoco hay rutas absolutas
del host en el código, ni dependencias nuevas de Python (Playwright y Chromium viven **dentro** de
la imagen del sandbox).

### Limitación declarada

- Sin credencial de Anthropic, Visual QA contra Claude queda `PENDING_API_KEY`: la fase se cierra
  con `CLAUDE VISUAL LIVE = PENDING_API_KEY` y `ENGINE-5.3 FINAL LIVE CERTIFICATION = PENDING`.
- Un proyecto cuyas dependencias no estén ya disponibles no se puede preparar: la sesión corre sin
  red, por diseño.
- Los identificadores de modelo por defecto siguen siendo los documentados
  (`claude-sonnet-5` para los roles visuales) y el acceso de esta cuenta a ellos continúa
  `LIVE_ACCOUNT_ACCESS_UNVERIFIED`.
- `ACCESSIBILITY_CHECK` es una comprobación acotada, **no** una certificación WCAG.
- No hay bucle de reparación ni pipeline autónomo: eso es ENGINE-6.

---

## 26. ENGINE-5.3.1 — Trusted Web Evidence + Visual Completeness Hardening

Una auditoría independiente ciega y la revisión del programador en jefe dejaron ENGINE-5.3 en
`CHANGES_REQUIRED`. Esta fase cierra los cuatro hallazgos —uno de ellos HIGH con PoC reproducido— y
un endurecimiento de frontera. No cambia ninguna otra capa.

### V53-01 — La frontera de la medición

El hallazgo, reproducido con código real: el probe vivía **dentro del workspace**, así que el
proyecto no confiable podía encontrar `.punto-web-session-*` y reescribir `capture.cjs`
(22 161 bytes → 45) **antes** de que se ejecutara. El digest posterior demostraba que el archivo no
había cambiado *después*, no que la medición la hubiera producido código confiable. Un adversario
que conoce el código de PUNTO no se detiene con un nombre aleatorio ni con un glob más difícil.

| | Antes (ENGINE-5.3) | Ahora (ENGINE-5.3.1) |
| --- | --- | --- |
| Contenedores | uno | **dos**: no confiable (proyecto + preview) y confiable (navegador + evidencia) |
| Red | `--network none` | red **interna** del sandbox (`--internal`), sin ruta a Internet |
| Código de medición | copiado al workspace, escribible | montado desde el host en `/opt/punto/probe` **read-only** |
| Control (payload) | archivo en el workspace | en el mismo montaje read-only, escrito por el host |
| Evidencia | dentro del workspace | directorio del host **fuera** del workspace, montado **solo** en el contenedor confiable |
| Workspace en el contenedor de medición | sí | **no**: mide por HTTP, no necesita tocarlo |
| Integridad | digest del manifiesto | digest del manifiesto **y** digest del código de medición |

El contenedor no confiable ejecuta los comandos del proyecto y sirve la preview en `0.0.0.0`; el
confiable espera a que la URL responda por la red interna, captura con Chromium y escribe la
evidencia. El proyecto **no puede** ver el directorio de evidencia, ni escribir el probe, ni el
control, ni fabricar el stdout del que sale el digest: son sistemas de archivos y procesos
distintos. El endurecimiento no se degrada: los dos contenedores llevan `rootfs` de solo lectura,
`--cap-drop ALL`, `no-new-privileges`, usuario no-root, tmpfs acotados y límites de CPU, memoria y
PIDs.

`tests/integration/test_web_trust_boundary_live.py` lo demuestra **ejecutando** un proyecto
malicioso real que intenta los diez ataques del encargo (glob del directorio del probe, localizar y
sobrescribir `capture.cjs` y `run_web_session.py`, modificar el control, `observations.json`,
`diagnostics.json`, sustituir capturas, mantener un vigilante en segundo plano y falsificar los
marcadores de digest). El propio atacante escribe el informe de lo que consiguió: no consigue
escribir nada, la sesión sigue midiendo bien, y el host acepta únicamente los bytes de la medición
confiable.

### V53-02 — Cobertura visual exigida, no derivada

La cobertura requerida se derivaba de las capturas **producidas** (`task.screenshots`), lo que hacía
el gate tautológico: una sesión que solo midió una ruta de tres parecía completa. Ahora la fuente de
verdad es `VisualSpec`: el producto cartesiano exacto `spec.routes x spec.viewports`, y la identidad
de una captura es el par **(ruta, viewport)**, no su nombre de archivo.

| Situación | Resultado |
| --- | --- |
| Falta una ruta o un viewport exigido | **BLOCKED** con los pares ausentes enumerados |
| Artefacto sin imagen | ese par cuenta como **ausente** |
| Imagen sin artefacto | se ignora: no puede satisfacer cobertura |
| Par repetido | **BLOCKED** (contrato roto) |
| Par extra que nadie pidió | se informa; no tapa un par requerido ausente y no se envía |

El runner comprueba la cobertura **antes** de llamar al modelo: si falta un par, no hay llamada. Y
el informe declara lo que el modelo recibió de verdad: `routes_analyzed`, `viewports_analyzed` y
`screenshots_analyzed` salen de los pares realmente enviados, nunca de la cobertura ideal.

### V53-03 — Gates vivos de Visual QA

`tests/integration/test_anthropic_visual_live.py` define los cuatro gates que faltaban:

| Gate | Qué exige |
| --- | --- |
| A. Autenticación | respuesta real con `PUNTO_CLAUDE_VISUAL_MODEL` (por defecto `claude-sonnet-5`), `provider=anthropic` y tokens > 0 |
| B. Una imagen + esquema de producción | JSON parseable **sin** quitar vallas y `VisualQAProposal.model_validate` |
| C. Varias imágenes + esquema de producción | la ruta multimodal que Visual QA usa de verdad |
| D. Extremo a extremo limpio | `ClaudeVisualQARunner` real → `VisualQAStatus.PASS` calculado por PUNTO, con `model_calls > 0` y tokens > 0 |

Sin `ANTHROPIC_API_KEY` la suite **no se ejecuta** y falla con `CREDENTIAL_REQUIRED` en lugar de
saltarse: `LIVE VISUAL CLAUDE GATES NOT RUN — PENDING_API_KEY`.

### V53-04 — No aplicable no es lo mismo que sin señal

`WebCheckOutcome` distingue ahora tres estados explícitos, y la aplicabilidad la decide el contrato,
no el modelo:

| Estado | Significado | Efecto |
| --- | --- | --- |
| `NOT_APPLICABLE` | la sesión no exigía esa comprobación | no penaliza |
| `PASS` / `FAIL` | aplicable y medida | verde o fallo |
| `NO_SIGNAL` | aplicable y **no** medida | **BLOCKED** |

`determine_web_status` aplica, en este orden: sin comprobaciones o sin ninguna aplicable →
`BLOCKED`; alguna aplicable sin señal → `BLOCKED` (antes que el fallo medido: no se culpa al
producto de una medición que no existe); alguna medida que no pasó → `FAIL`; todo lo aplicable,
medido y verde → `PASS`. La regla de aplicabilidad está escrita por comprobación en
`_is_applicable` (marcadores exigidos, dos o más viewports para el responsive, nota de recorte por
captura, y «se intentó renderizar» para las de carga).

### V53-05 — Enlace artefacto/imagen en la frontera

`ClaudeVisualQARunner` ya no confía en la metadata del llamante: reconstruye el payload **canónico**
desde el artefacto (`artifact.as_image_payload(payload.data)`), lo que revalida tamaño y `sha256`, y
rechaza cualquier contradicción en `logical_name` o `media_type` **antes** de llamar al proveedor.
Mismo tamaño con distinto hash ⇒ bloqueo sin gastar una llamada.

### Evidencia adicional

- El **runtime Node** se ejercita de verdad: la integración de extremo a extremo ejecuta
  `node -e "require('fs').writeFileSync('node-ok.txt','ok')"` y comprueba el archivo, así que la
  evidencia no depende solo de `python3`.
- **Honestidad del gestor de paquetes**: la imagen solo trae npm/npx (y node/python3). Un proyecto
  que declare `pnpm`, `yarn` o `bun` **no** cae en `npm` en silencio: el lanzador comprueba
  `shutil.which(argv[0])`, imprime `PUNTO_PROGRAM_MISSING <programa>` y la sesión falla de forma
  explícita nombrando el programa que falta.

### Limitación declarada

- El contenedor no confiable **puede leer** el código de medición montado (no escribirlo). Se
  acepta: leerlo no permite falsificar la evidencia, y el digest del código se contrasta con el que
  calcula el host.
- La medición sigue corriendo **dentro de la página** (`page.evaluate`): una página que sobrescriba
  `getComputedStyle` o `scrollWidth` puede falsear sus propias medidas. Es inherente a observar con
  JavaScript en la página y por eso un `PASS` técnico no sustituye a la revisión humana.
- Sin red en la sesión, un proyecto con dependencias no disponibles no se puede construir: sigue
  siendo un bloqueo declarado.
- Chromium corre con `--no-sandbox` **dentro** de su contenedor: el contenedor es la frontera real.
- Los gates vivos de Visual QA están **definidos** y sin ejecutar: `CLAUDE VISUAL LIVE =
  PENDING_API_KEY`.

---

## 27. Licencia

Propietario — Punto Inmobiliario HN. `Private :: Do Not Upload`.


