"""Soportes de prueba de la capa de planificación (ENGINE-3).

Contiene dos cosas que comparten varias pruebas:

1. **Fixtures sintéticos de generalidad** (§21): tres proyectos de naturaleza
   distinta —API REST en Python, SaaS en Next.js/TypeScript y una CLI pequeña— con
   la forma exacta que el Architect y el Planner deben producir. No se implementa
   ninguno de los tres productos: solo se planifican, para demostrar que el motor no
   está escrito para PUNTO Inmobiliario ni para Python.
2. Un **cliente de modelo falso** que devuelve respuestas preparadas, para ejercitar
   el ciclo completo sin red.

Nada de este módulo se usa en producción.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from punto.providers.deepseek import ModelCompletion, redact_secrets
from punto.schemas.execution import ModelUsage


# ---------------------------------------------------------------------------
# Cliente de modelo falso
# ---------------------------------------------------------------------------
@dataclass
class FakePlanningClient:
    """Cliente de modelo que devuelve las respuestas preparadas, en orden.

    Si se le piden más respuestas de las preparadas, repite la última: es lo que
    permite comprobar que un plan inválido agota intentos en lugar de colgarse.

    ``redact`` se comporta como el cliente real (redacta la clave configurada y
    enmascara ``Bearer``), para que las pruebas de no-filtrado sean fieles.
    """

    responses: list[str]
    model: str = "deepseek-v4-pro"
    api_key: str = ""
    calls: int = 0
    prompts: list[str] = field(default_factory=list)
    system_prompts: list[str] = field(default_factory=list)
    usage: ModelUsage = field(
        default_factory=lambda: ModelUsage(
            prompt_tokens=100, completion_tokens=50, total_tokens=150
        )
    )

    def redact(self, text: str) -> str:
        """Redacción equivalente a la del cliente real."""
        return redact_secrets(text, api_key=self.api_key)

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> ModelCompletion:
        """Devuelve la siguiente respuesta preparada."""
        self.system_prompts.append(system_prompt)
        self.prompts.append(user_prompt)
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ModelCompletion(
            content=self.responses[index],
            model=self.model,
            usage=self.usage,
            latency_ms=7,
        )


def payload(response: dict[str, Any]) -> str:
    """Serializa una respuesta del modelo como JSON."""
    return json.dumps(response)


# ---------------------------------------------------------------------------
# Utilidades de construcción de fixtures
# ---------------------------------------------------------------------------
def requirement(
    identifier: str, statement: str, *, priority: str = "MUST", acceptance: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Requisito con forma de salida del Architect."""
    return {
        "id": identifier,
        "statement": statement,
        "priority": priority,
        "acceptance": list(acceptance) or [f"{identifier} es observable en producción"],
    }


def component(
    identifier: str,
    name: str,
    kind: str,
    responsibility: str,
    depends_on: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Componente con forma de salida del Architect."""
    return {
        "id": identifier,
        "name": name,
        "kind": kind,
        "responsibility": responsibility,
        "depends_on": list(depends_on),
    }


def task(
    identifier: str,
    title: str,
    objective: str,
    epic_id: str,
    *,
    acceptance: tuple[str, ...],
    dependencies: tuple[str, ...] = (),
    allowed_files: tuple[str, ...] = (),
    context_files: tuple[str, ...] = (),
    checks: tuple[str, ...] = (),
    capabilities: tuple[str, ...] = (),
    risk: str = "LOW",
    authority: str = "LEVEL_0_AUTONOMOUS",
    complexity: str = "MEDIUM",
    produces: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Tarea con forma de salida del Planner."""
    return {
        "id": identifier,
        "title": title,
        "objective": objective,
        "description": objective,
        "epic_id": epic_id,
        "acceptance_criteria": list(acceptance),
        "dependencies": list(dependencies),
        "allowed_files": list(allowed_files),
        "context_files": list(context_files),
        "validation_checks": list(checks),
        "required_capabilities": list(capabilities),
        "risk_level": risk,
        "authority_level": authority,
        "estimated_complexity": complexity,
        "produces": list(produces),
    }


def milestone(identifier: str, title: str, objective: str, exit_criteria: tuple[str, ...]) -> dict:
    """Milestone con forma de salida del Planner."""
    return {
        "id": identifier,
        "title": title,
        "objective": objective,
        "exit_criteria": list(exit_criteria),
    }


def epic(identifier: str, title: str, objective: str, milestone_id: str) -> dict[str, Any]:
    """Epic con forma de salida del Planner."""
    return {"id": identifier, "title": title, "objective": objective, "milestone_id": milestone_id}


# ---------------------------------------------------------------------------
# Fixture A: API REST en Python (tecnología que PUNTO sí puede ejecutar)
# ---------------------------------------------------------------------------
PYTHON_API_INTENT: dict[str, Any] = {
    "name": "StockFlow",
    "description": "API REST para administrar inventario de almacenes y movimientos.",
    "business_goal": "Reducir las roturas de stock en almacenes medianos.",
    "target_users": ["Encargado de almacén", "Analista de operaciones"],
    "core_capabilities": ["Registrar movimientos", "Consultar existencias"],
    "preferred_stack": ["Python"],
}

PYTHON_API_ARCHITECT: dict[str, Any] = {
    "project_spec": {
        "project_name": "StockFlow",
        "problem_statement": "El inventario se lleva en hojas de cálculo y no hay trazabilidad.",
        "product_goals": [
            "Trazar cada movimiento de stock",
            "Consultar existencias en tiempo real",
        ],
        "target_users": ["Encargado de almacén", "Analista de operaciones"],
        "functional_requirements": [
            requirement(
                "R-001",
                "Registrar entradas y salidas de stock",
                acceptance=("POST /movements responde 201",),
            ),
            requirement(
                "R-002",
                "Consultar existencias por almacén",
                acceptance=("GET /stock responde el saldo actual",),
            ),
        ],
        "non_functional_requirements": [
            requirement(
                "NFR-001",
                "Responder en menos de 300 ms en el percentil 95",
                priority="SHOULD",
                acceptance=("la métrica p95 se mantiene bajo 300 ms",),
            )
        ],
        "assumptions": ["Un solo almacén por instalación en la primera versión"],
        "constraints": ["El equipo conoce Python"],
        "out_of_scope": ["Aplicación móvil nativa"],
        "success_criteria": ["El 100% de los movimientos queda auditado"],
        "risk_notes": ["Duplicar movimientos si el cliente reintenta"],
        "open_questions": [],
    },
    "architecture": {
        "architecture_style": "API REST en capas",
        "components": [
            component("C1", "HTTP API", "SERVICE", "Expone los endpoints REST"),
            component("C2", "Dominio", "MODULE", "Reglas de stock", ("C1",)),
            component("C3", "Persistencia", "MODULE", "Acceso a datos", ("C2",)),
        ],
        "services": ["stockflow-api"],
        "modules": ["stock", "movements"],
        "data_stores": [
            {
                "id": "DS1",
                "name": "StockDB",
                "engine": "sqlite",
                "purpose": "Persistir movimientos y saldos",
                "managed": False,
            }
        ],
        "external_integrations": [],
        "interfaces": [
            {
                "id": "IF1",
                "name": "REST v1",
                "kind": "HTTP_API",
                "description": "Endpoints /movements y /stock",
                "consumers": ["Frontend interno"],
            }
        ],
        "security_boundaries": [
            {
                "id": "SB1",
                "name": "Borde HTTP",
                "description": "Separa clientes de la lógica de dominio",
                "controls": ["validación de payload"],
            }
        ],
        "deployment_topology": "Un contenedor con la API y un volumen de datos",
        "observability": ["logs estructurados"],
        "testing_strategy": ["pruebas unitarias de dominio", "pruebas de API"],
        "technology_choices": [
            {"topic": "lenguaje", "choice": "python"},
            {"topic": "framework", "choice": "fastapi"},
            {"topic": "base de datos", "choice": "sqlite"},
        ],
        "technology_decisions": [
            {
                "id": "D1",
                "topic": "lenguaje",
                "decision": "python",
                "reason": "El equipo ya lo conoce y el motor tiene perfil python312",
                "alternatives": ["node20"],
                "tradeoffs": "Menor rendimiento bruto que alternativas compiladas",
                "confidence": "HIGH",
            }
        ],
        "alternatives_considered": ["Un servicio en Node con el mismo alcance"],
        "risks": ["Crecimiento del volumen de movimientos"],
    },
    "capability_profile": {
        "languages": ["python"],
        "frameworks": ["fastapi"],
        "databases": ["sqlite"],
        "package_managers": ["pip"],
        "validators": ["pytest", "ruff", "mypy"],
        "deployment_targets": [],
        "execution_profiles_required": ["python312"],
    },
    "notes": ["Diseño en capas para poder probar el dominio sin HTTP"],
}

PYTHON_API_PLANNER: dict[str, Any] = {
    "project_name": "StockFlow",
    "milestones": [
        milestone(
            "M1",
            "Inventario operativo",
            "Registrar y consultar stock",
            ("Se registran movimientos",),
        ),
        milestone(
            "M2", "Endurecimiento", "Dejar el servicio listo para producción", ("p95 bajo 300 ms",)
        ),
    ],
    "epics": [
        epic("E1", "Modelo de stock", "Definir el dominio de existencias", "M1"),
        epic("E2", "API de movimientos", "Exponer los endpoints REST", "M1"),
        epic("E3", "Rendimiento y auditoría", "Medir y trazar", "M2"),
    ],
    "tasks": [
        task(
            "T1",
            "Crear el modelo Movement",
            "Crear el modelo Movement con campos sku, quantity y direction",
            "E1",
            acceptance=("el modelo valida quantity distinta de cero",),
            allowed_files=("src/stock/models.py",),
            checks=("pytest",),
            capabilities=("python312",),
            produces=("R-001",),
        ),
        task(
            "T2",
            "Implementar el cálculo de saldo",
            "Implementar el cálculo de saldo por almacén a partir de los movimientos",
            "E1",
            acceptance=("el saldo suma entradas y resta salidas",),
            dependencies=("T1",),
            allowed_files=("src/stock/domain.py",),
            checks=("pytest", "mypy"),
            capabilities=("python312",),
            produces=("R-002",),
        ),
        task(
            "T3",
            "Implementar POST /movements",
            "Implementar el endpoint POST /movements con validación del payload",
            "E2",
            acceptance=("POST /movements responde 201 con el movimiento creado",),
            dependencies=("T2",),
            allowed_files=("src/stock/api.py",),
            checks=("pytest", "ruff"),
            capabilities=("python312",),
            produces=("R-001",),
        ),
        task(
            "T4",
            "Implementar GET /stock",
            "Implementar el endpoint GET /stock que devuelve el saldo por almacén",
            "E2",
            acceptance=("GET /stock responde el saldo de cada almacén",),
            dependencies=("T2",),
            allowed_files=("src/stock/api.py",),
            checks=("pytest",),
            capabilities=("python312",),
            produces=("R-002",),
        ),
        task(
            "T5",
            "Medir la latencia del percentil 95",
            "Añadir una prueba de latencia que mida el percentil 95 de GET /stock",
            "E3",
            acceptance=("la prueba falla si p95 supera 300 ms",),
            dependencies=("T4",),
            allowed_files=("tests/test_latency.py",),
            checks=("pytest",),
            capabilities=("python312",),
            produces=("NFR-001",),
        ),
        task(
            "T6",
            "Añadir traza de auditoría de movimientos",
            "Registrar en un log estructurado cada movimiento aplicado",
            "E3",
            acceptance=("todo movimiento aplicado deja una línea de log",),
            dependencies=("T3",),
            allowed_files=("src/stock/audit.py",),
            checks=("pytest", "ruff"),
            capabilities=("python312",),
            produces=("R-001",),
        ),
    ],
    "notes": ["Se empieza por el dominio para poder probarlo sin HTTP"],
}

# ---------------------------------------------------------------------------
# Fixture B: SaaS en Next.js / TypeScript (tecnología que PUNTO NO puede ejecutar)
# ---------------------------------------------------------------------------
NEXTJS_INTENT: dict[str, Any] = {
    "name": "ClientPulse",
    "description": "SaaS para gestionar la relación con clientes de agencias pequeñas.",
    "business_goal": "Centralizar clientes, propuestas y seguimiento.",
    "target_users": ["Director de agencia", "Ejecutivo de cuentas"],
}

NEXTJS_ARCHITECT: dict[str, Any] = {
    "project_spec": {
        "project_name": "ClientPulse",
        "problem_statement": "Las agencias siguen sus cuentas en herramientas dispersas.",
        "product_goals": [
            "Centralizar la cartera de clientes",
            "Acelerar el seguimiento comercial",
        ],
        "target_users": ["Director de agencia", "Ejecutivo de cuentas"],
        "functional_requirements": [
            requirement(
                "R-001",
                "Gestionar la ficha de cliente",
                acceptance=("la ficha guarda contacto y estado",),
            ),
            requirement(
                "R-002",
                "Registrar interacciones con el cliente",
                acceptance=("cada interacción queda con fecha y autor",),
            ),
        ],
        "non_functional_requirements": [
            requirement(
                "NFR-001",
                "Cargar el panel en menos de 2 s",
                priority="SHOULD",
                acceptance=("el panel carga bajo 2 s con 500 clientes",),
            )
        ],
        "assumptions": ["Un espacio de trabajo por agencia"],
        "constraints": ["El equipo trabaja en TypeScript"],
        "out_of_scope": ["Facturación electrónica"],
        "success_criteria": ["El 90% de las cuentas tiene actividad registrada"],
        "risk_notes": ["Crecimiento del costo de consultas"],
        "open_questions": [
            {
                "id": "Q-001",
                "question": "¿Se debe cumplir alguna norma sectorial de retención de datos?",
                "kind": "LEGAL_DECISION",
                "context": "Afecta al tiempo de retención, no a la arquitectura",
            }
        ],
    },
    "architecture": {
        "architecture_style": "Aplicación web full-stack con render en servidor",
        "components": [
            component("C1", "Web", "UI", "Interfaz de usuario"),
            component("C2", "API de aplicación", "SERVICE", "Lógica de negocio", ("C1",)),
            component("C3", "Acceso a datos", "MODULE", "Consultas y escrituras", ("C2",)),
        ],
        "services": ["clientpulse-web"],
        "modules": ["clients", "interactions"],
        "data_stores": [
            {
                "id": "DS1",
                "name": "ClientDB",
                "engine": "postgres",
                "purpose": "Persistir clientes e interacciones",
                "managed": True,
            }
        ],
        "external_integrations": [
            {
                "id": "I1",
                "name": "Correo transaccional",
                "purpose": "Enviar recordatorios",
                "protocol": "HTTPS",
                "auth": "API key",
            }
        ],
        "interfaces": [
            {
                "id": "IF1",
                "name": "Panel web",
                "kind": "UI",
                "description": "Listado y ficha de cliente",
                "consumers": ["Ejecutivo de cuentas"],
            }
        ],
        "security_boundaries": [
            {
                "id": "SB1",
                "name": "Sesión de usuario",
                "description": "Separa espacios de trabajo por agencia",
                "controls": ["sesión firmada", "aislamiento por tenant"],
            }
        ],
        "deployment_topology": "Plataforma gestionada con base de datos administrada",
        "observability": ["traza de peticiones", "alertas de error"],
        "testing_strategy": ["pruebas de componente", "pruebas de extremo a extremo"],
        "technology_choices": [
            {"topic": "lenguaje", "choice": "typescript"},
            {"topic": "framework", "choice": "nextjs"},
            {"topic": "base de datos", "choice": "postgres"},
            {"topic": "gestor de paquetes", "choice": "npm"},
            {"topic": "despliegue", "choice": "vercel"},
        ],
        "technology_decisions": [
            {
                "id": "D1",
                "topic": "framework",
                "decision": "nextjs",
                "reason": "Render en servidor y despliegue gestionado con poco mantenimiento",
                "alternatives": ["remix", "astro"],
                "tradeoffs": "Acoplamiento al proveedor de despliegue",
                "confidence": "MEDIUM",
            }
        ],
        "alternatives_considered": ["Aplicación separada en Python con frontend propio"],
        "risks": ["Costo variable de la base de datos administrada"],
    },
    "capability_profile": {
        "languages": ["typescript"],
        "frameworks": ["nextjs"],
        "databases": ["postgres"],
        "package_managers": ["npm"],
        "validators": ["eslint", "tsc", "vitest"],
        "deployment_targets": ["vercel"],
        "execution_profiles_required": ["node20"],
    },
    "notes": ["La pregunta legal no impide planificar; se difiere a la ejecución"],
}

NEXTJS_PLANNER: dict[str, Any] = {
    "project_name": "ClientPulse",
    "milestones": [
        milestone("M1", "Cartera operativa", "Gestionar clientes", ("Se crean y listan clientes",)),
        milestone(
            "M2", "Seguimiento", "Registrar interacciones", ("Hay interacciones registradas",)
        ),
    ],
    "epics": [
        epic("E1", "Modelo de clientes", "Definir la ficha de cliente", "M1"),
        epic("E2", "Interfaz de cartera", "Listar y editar clientes", "M1"),
        epic("E3", "Interacciones", "Registrar el seguimiento", "M2"),
    ],
    "tasks": [
        task(
            "T1",
            "Crear el esquema de Client",
            "Crear el esquema de base de datos de la tabla client con contacto y estado",
            "E1",
            acceptance=("la migración crea la tabla client con sus columnas",),
            allowed_files=("db/migrations/001_client.sql",),
            checks=("npm run lint",),
            capabilities=("node20", "postgres", "npm"),
            produces=("R-001",),
        ),
        task(
            "T2",
            "Implementar el repositorio de clientes",
            "Implementar el repositorio de clientes con listado y búsqueda por nombre",
            "E1",
            acceptance=("el repositorio devuelve los clientes del espacio de trabajo",),
            dependencies=("T1",),
            allowed_files=("src/server/clients.ts",),
            checks=("vitest",),
            capabilities=("node20", "npm"),
            produces=("R-001",),
        ),
        task(
            "T3",
            "Construir el listado de clientes",
            "Construir la pantalla de listado de clientes con paginación",
            "E2",
            acceptance=("el listado muestra 20 clientes por página",),
            dependencies=("T2",),
            allowed_files=("src/app/clients/page.tsx",),
            checks=("vitest",),
            capabilities=("node20", "npm"),
            produces=("R-001",),
        ),
        task(
            "T4",
            "Crear el esquema de Interaction",
            "Crear el esquema de base de datos de la tabla interaction con fecha y autor",
            "E3",
            acceptance=("la migración crea la tabla interaction",),
            dependencies=("T1",),
            allowed_files=("db/migrations/002_interaction.sql",),
            checks=("npm run lint",),
            capabilities=("node20", "postgres", "npm"),
            produces=("R-002",),
        ),
        task(
            "T5",
            "Implementar el registro de interacciones",
            "Implementar la acción de servidor que registra una interacción del cliente",
            "E3",
            acceptance=("la interacción queda guardada con autor y fecha",),
            dependencies=("T4",),
            allowed_files=("src/server/interactions.ts",),
            checks=("vitest",),
            capabilities=("node20", "npm"),
            produces=("R-002",),
        ),
        task(
            "T6",
            "Medir la carga del panel",
            "Añadir una medición de tiempo de carga del panel con 500 clientes",
            "E3",
            acceptance=("la medición falla si el panel tarda más de 2 s",),
            dependencies=("T3",),
            allowed_files=("tests/panel-loading.test.ts",),
            checks=("vitest",),
            capabilities=("node20", "npm"),
            produces=("NFR-001",),
        ),
    ],
    "notes": ["El despliegue no puede validarse hasta existir un perfil Node en PUNTO"],
}

# ---------------------------------------------------------------------------
# Fixture C: CLI pequeña (sin servicios externos)
# ---------------------------------------------------------------------------
CLI_INTENT: dict[str, Any] = {
    "name": "NotesCLI",
    "description": "Herramienta de línea de comandos para tomar notas rápidas en texto plano.",
    "target_users": ["Desarrollador"],
}

CLI_ARCHITECT: dict[str, Any] = {
    "project_spec": {
        "project_name": "NotesCLI",
        "problem_statement": "Tomar una nota rápida requiere abrir una aplicación pesada.",
        "product_goals": ["Capturar una nota en un solo comando"],
        "target_users": ["Desarrollador"],
        "functional_requirements": [
            requirement(
                "R-001",
                "Añadir una nota desde la terminal",
                acceptance=("el comando add guarda la nota con marca de tiempo",),
            ),
            requirement(
                "R-002",
                "Listar las notas guardadas",
                acceptance=("el comando list muestra las notas en orden",),
            ),
        ],
        "non_functional_requirements": [
            requirement(
                "NFR-001",
                "Arrancar en menos de 200 ms",
                priority="COULD",
                acceptance=("el tiempo de arranque medido baja de 200 ms",),
            )
        ],
        "assumptions": ["Un único usuario por equipo"],
        "constraints": ["Sin dependencias externas"],
        "out_of_scope": ["Sincronización en la nube"],
        "success_criteria": ["Añadir una nota requiere un solo comando"],
        "risk_notes": ["Corrupción del archivo de notas si el proceso se interrumpe"],
        "open_questions": [],
    },
    "architecture": {
        "architecture_style": "Herramienta de línea de comandos con almacenamiento en archivo",
        "components": [
            component("C1", "CLI", "CLI", "Interpreta los subcomandos"),
            component("C2", "Almacén de notas", "MODULE", "Lee y escribe el archivo", ("C1",)),
        ],
        "services": [],
        "modules": ["notes"],
        "data_stores": [
            {
                "id": "DS1",
                "name": "Archivo de notas",
                "engine": "texto plano",
                "purpose": "Guardar las notas del usuario",
                "managed": False,
            }
        ],
        "external_integrations": [],
        "interfaces": [
            {
                "id": "IF1",
                "name": "Subcomandos add/list",
                "kind": "CLI",
                "description": "notes add <texto> y notes list",
                "consumers": ["Usuario de terminal"],
            }
        ],
        "security_boundaries": [],
        "deployment_topology": "Ejecutable local instalado con el gestor de paquetes del lenguaje",
        "observability": ["mensajes de error en stderr"],
        "testing_strategy": ["pruebas de la CLI con entrada y salida simuladas"],
        "technology_choices": [
            {"topic": "lenguaje", "choice": "python"},
            {"topic": "empaquetado", "choice": "pip"},
        ],
        "technology_decisions": [
            {
                "id": "D1",
                "topic": "lenguaje",
                "decision": "python",
                "reason": "Sin dependencias externas y disponible en el motor",
                "alternatives": ["bash"],
                "tradeoffs": "Requiere un intérprete instalado",
                "confidence": "HIGH",
            }
        ],
        "alternatives_considered": ["Un script de shell"],
        "risks": ["El archivo crece sin límite"],
    },
    "capability_profile": {
        "languages": ["python"],
        "frameworks": [],
        "databases": [],
        "package_managers": ["pip"],
        "validators": ["pytest", "ruff"],
        "deployment_targets": [],
        "execution_profiles_required": ["python312"],
    },
    "notes": ["Proyecto pequeño: una sola fase"],
}

CLI_PLANNER: dict[str, Any] = {
    "project_name": "NotesCLI",
    "milestones": [
        milestone("M1", "CLI usable", "Añadir y listar notas", ("Los dos subcomandos funcionan",)),
    ],
    "epics": [
        epic("E1", "Almacenamiento", "Guardar y leer notas", "M1"),
        epic("E2", "Interfaz de comandos", "Exponer los subcomandos", "M1"),
    ],
    "tasks": [
        task(
            "T1",
            "Crear el almacén de notas",
            "Implementar la lectura y escritura del archivo de notas con marca de tiempo",
            "E1",
            acceptance=("el archivo conserva las notas entre ejecuciones",),
            allowed_files=("notes/store.py",),
            checks=("pytest",),
            capabilities=("python312",),
            produces=("R-001",),
        ),
        task(
            "T2",
            "Implementar el subcomando add",
            "Implementar el subcomando add que guarda el texto recibido por argumento",
            "E2",
            acceptance=("add guarda la nota y no imprime nada en stderr",),
            dependencies=("T1",),
            allowed_files=("notes/cli.py",),
            checks=("pytest", "ruff"),
            capabilities=("python312",),
            produces=("R-001",),
        ),
        task(
            "T3",
            "Implementar el subcomando list",
            "Implementar el subcomando list que imprime las notas en orden cronológico",
            "E2",
            acceptance=("list imprime las notas de la más antigua a la más reciente",),
            dependencies=("T1",),
            allowed_files=("notes/cli.py",),
            checks=("pytest",),
            capabilities=("python312",),
            produces=("R-002",),
        ),
        task(
            "T4",
            "Medir el tiempo de arranque",
            "Añadir una prueba que mida el tiempo de arranque de la CLI",
            "E2",
            acceptance=("la prueba falla si el arranque supera 200 ms",),
            dependencies=("T2",),
            allowed_files=("tests/test_startup.py",),
            checks=("pytest",),
            capabilities=("python312",),
            produces=("NFR-001",),
        ),
    ],
    "notes": ["Sin dependencias externas por restricción declarada"],
}


#: Los tres fixtures, con su nombre y una etiqueta legible para la evidencia.
PLANNING_FIXTURES: tuple[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]], ...] = (
    ("python-api", PYTHON_API_INTENT, PYTHON_API_ARCHITECT, PYTHON_API_PLANNER),
    ("nextjs-saas", NEXTJS_INTENT, NEXTJS_ARCHITECT, NEXTJS_PLANNER),
    ("cli", CLI_INTENT, CLI_ARCHITECT, CLI_PLANNER),
)


__all__ = [
    "CLI_ARCHITECT",
    "CLI_INTENT",
    "CLI_PLANNER",
    "NEXTJS_ARCHITECT",
    "NEXTJS_INTENT",
    "NEXTJS_PLANNER",
    "PLANNING_FIXTURES",
    "PYTHON_API_ARCHITECT",
    "PYTHON_API_INTENT",
    "PYTHON_API_PLANNER",
    "FakePlanningClient",
    "component",
    "epic",
    "milestone",
    "payload",
    "requirement",
    "task",
]
