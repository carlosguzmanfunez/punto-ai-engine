"""Registro de capacidades reales de PUNTO y detección de huecos (ENGINE-3 §7 y §17).

El motor es **general**: el Architect puede proponer Next.js, PostgreSQL o Rust. Eso
no obliga a PUNTO a saber ejecutarlo. Este módulo responde una sola pregunta, de
forma determinista y honesta:

    ¿esta capacidad se puede ejecutar hoy con lo que PUNTO tiene instalado?

La respuesta se basa en lo que está **demostrado**, no en lo que sería cómodo
suponer: el perfil ``python312`` y los validadores de la imagen del sandbox
(``localhost/punto-sandbox-python:0.1``). Todo lo demás es ``MISSING`` (si PUNTO
sabe que no lo tiene) o ``UNKNOWN`` (si no tiene información), y en ambos casos se
registra un :class:`CapabilityGap`.

Un hueco **no bloquea la planificación**: se registra para poder construir después
el perfil de ejecución necesario. Lo que nunca se hace es improvisar una ejecución
en el host como sustituto (§17).
"""

from __future__ import annotations

import re
from typing import Final

from punto.schemas.planning import (
    CapabilityGap,
    CapabilityKind,
    CapabilityStatus,
    PlannedTask,
    ProjectCapabilityProfile,
)

#: Perfil de ejecución que PUNTO puede ejecutar hoy, verificado en ENGINE-1.R3.
AVAILABLE_PYTHON_PROFILE: Final[str] = "python312"

#: Capacidades **demostradas**. Todo lo que no esté aquí no se declara disponible.
AVAILABLE_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        AVAILABLE_PYTHON_PROFILE,
        "git",
        "mypy",
        "podman",
        "pytest",
        "pytest-cov",
        "ruff",
        # ``sqlite3`` viene en la biblioteca estándar de Python: no necesita
        # servicio ni instalación adicional dentro del sandbox.
        "sqlite",
        "sqlite3",
    }
)

#: Capacidades que PUNTO sabe que **no** puede ejecutar, con el motivo declarado.
#: La lista no pretende ser exhaustiva: lo que no está aquí ni en
#: :data:`AVAILABLE_CAPABILITIES` queda como ``UNKNOWN``, que tampoco se considera
#: disponible.
KNOWN_UNAVAILABLE_CAPABILITIES: Final[dict[str, str]] = {
    "aws": "sin destino de despliegue AWS configurado",
    "azure": "sin destino de despliegue Azure configurado",
    "bun": "sin perfil de ejecución Bun en PUNTO",
    "cargo": "sin perfil de ejecución Rust en PUNTO",
    "csharp": "sin perfil de ejecución .NET en PUNTO",
    "cypress": "sin navegador ni runner Cypress en la imagen del sandbox",
    "deno": "sin perfil de ejecución Deno en PUNTO",
    "django": "no está instalado en la imagen del sandbox",
    "docker": "el sandbox usa Podman; no hay daemon Docker disponible",
    "docker-compose": "el sandbox usa Podman; no hay Docker Compose disponible",
    "dotnet8": "sin perfil de ejecución .NET en PUNTO",
    "eslint": "sin perfil de ejecución Node en PUNTO",
    "fastapi": "no está instalado en la imagen del sandbox",
    "flask": "no está instalado en la imagen del sandbox",
    "gcp": "sin destino de despliegue GCP configurado",
    "go122": "sin perfil de ejecución Go en PUNTO",
    "gradle": "sin perfil de ejecución Java en PUNTO",
    "java21": "sin perfil de ejecución Java en PUNTO",
    "javascript": "sin perfil de ejecución Node en PUNTO",
    "jest": "sin perfil de ejecución Node en PUNTO",
    "kotlin": "sin perfil de ejecución Kotlin en PUNTO",
    "kubernetes": "sin clúster ni manifiestos verificados en PUNTO",
    "mongodb": "sin servicio de prueba MongoDB en el sandbox",
    "maven": "sin perfil de ejecución Java en PUNTO",
    "mysql": "sin servicio de prueba MySQL en el sandbox",
    "nextjs": "sin perfil de ejecución Node en PUNTO",
    "node20": "sin perfil de ejecución Node en PUNTO",
    "node22": "sin perfil de ejecución Node en PUNTO",
    "npm": "sin perfil de ejecución Node en PUNTO",
    "php83": "sin perfil de ejecución PHP en PUNTO",
    "pip": "sin acceso a red en el sandbox: no se pueden instalar paquetes en caliente",
    "playwright": "sin navegador ni runner Playwright en la imagen del sandbox",
    "pnpm": "sin perfil de ejecución Node en PUNTO",
    "postgresql": "sin servicio de prueba PostgreSQL en el sandbox",
    "prisma": "sin perfil de ejecución Node en PUNTO",
    "react": "sin perfil de ejecución Node en PUNTO",
    "redis": "sin servicio de prueba Redis en el sandbox",
    "ruby33": "sin perfil de ejecución Ruby en PUNTO",
    "rust": "sin perfil de ejecución Rust en PUNTO",
    "svelte": "sin perfil de ejecución Node en PUNTO",
    "swift": "sin perfil de ejecución Swift en PUNTO",
    "tailwind": "sin perfil de ejecución Node en PUNTO",
    "terraform": "sin proveedor ni credenciales de Terraform en PUNTO",
    "tsc": "sin perfil de ejecución Node en PUNTO",
    "typescript": "sin perfil de ejecución Node en PUNTO",
    "vercel": "sin destino de despliegue Vercel configurado",
    "vitest": "sin perfil de ejecución Node en PUNTO",
    "vue": "sin perfil de ejecución Node en PUNTO",
    "yarn": "sin perfil de ejecución Node en PUNTO",
}

#: Alias normalizados hacia el nombre canónico de la capacidad.
#:
#: Las claves están en forma agresiva (minúsculas, sin espacios ni guiones) porque
#: los modelos escriben la misma tecnología de muchas maneras: ``Node.js``,
#: ``node 20``, ``python3.12runtime``, ``AWS RDS PostgreSQL``.
_CANONICAL_ALIASES: Final[dict[str, str]] = {
    "3.12": AVAILABLE_PYTHON_PROFILE,
    "cpython": AVAILABLE_PYTHON_PROFILE,
    "py": AVAILABLE_PYTHON_PROFILE,
    "python": AVAILABLE_PYTHON_PROFILE,
    "python3": AVAILABLE_PYTHON_PROFILE,
    "python3.12": AVAILABLE_PYTHON_PROFILE,
    "python312": AVAILABLE_PYTHON_PROFILE,
    "python312runtime": AVAILABLE_PYTHON_PROFILE,
    "python3.12runtime": AVAILABLE_PYTHON_PROFILE,
    "golang": "go122",
    "js": "javascript",
    "node": "node20",
    "node.js": "node20",
    "nodejs": "node20",
    "node20runtime": "node20",
    "node.js20runtime": "node20",
    "postgres16": "postgresql",
    "postgresql16": "postgresql",
    "postgres": "postgresql",
    "postgress": "postgresql",
    "psql": "postgresql",
    "pyproject": AVAILABLE_PYTHON_PROFILE,
    "ts": "typescript",
    "dotnet": "dotnet8",
    "java": "java21",
    "rb": "ruby33",
}

#: Versión al final de un nombre de capacidad, para poder reconocer la familia.
#:
#: ``typescript5.4`` es TypeScript, ``redis7`` es Redis y ``postgresql16`` es
#: PostgreSQL: sin esta normalización cada versión sería una capacidad desconocida.
_VERSION_SUFFIX: Final[re.Pattern[str]] = re.compile(r"\d+(?:\.\d+)*(?:\.[a-z])?$")

#: Versión al final del **nombre legible**, separada por espacio o punto.
#:
#: Se aplica cuando la capacidad no está en el vocabulario conocido, para que
#: ``NestJS 10`` y ``NestJS`` sean la misma capacidad. Exige un espacio o un punto
#: decimal delante: ``s3`` no es la versión 3 de una capacidad llamada ``s``.
_VERSION_TAIL: Final[re.Pattern[str]] = re.compile(
    r"(?:\s+\d+(?:\.\d+)*|\d*\.\d+)(?:\.[a-z])?\s*$"
)

#: Prefijo de familia que algunos modelos añaden al declarar una capacidad.
#:
#: ``LANGUAGE: TypeScript 5.x`` es TypeScript. El prefijo aparece porque el prompt del
#: Planner presenta el vocabulario agrupado por familia; aceptarlo reconoce una
#: variación real sin relajar la regla: la capacidad sigue teniendo que estar
#: declarada en el perfil.
_KIND_PREFIX: Final[re.Pattern[str]] = re.compile(
    r"^(?:LANGUAGE|FRAMEWORK|DATABASE|PACKAGE_MANAGER|VALIDATOR|EXECUTION_PROFILE"
    r"|DEPLOYMENT_TARGET)\s*:\s*",
    re.IGNORECASE,
)


def capability_key(name: str) -> str:
    """Clave de búsqueda agresiva: minúsculas, sin espacios, guiones ni subrayados.

    Se descartan además el prefijo de familia y el sufijo ``LTS``, que los modelos
    añaden con frecuencia (``Node.js 20 LTS``).
    """
    lowered = _KIND_PREFIX.sub("", name.strip()).lower()
    for token in (" ", "\t", "_", "-", "/"):
        lowered = lowered.replace(token, "")
    if lowered.endswith("lts"):
        lowered = lowered[: -len("lts")]
    return lowered


def _authored_canonical_by_key() -> dict[str, str]:
    """Mapa ``clave agresiva -> nombre canónico tal como está escrito en el módulo``."""
    mapping: dict[str, str] = {}
    for capability in AVAILABLE_CAPABILITIES:
        mapping.setdefault(capability_key(capability), capability)
    for capability in KNOWN_UNAVAILABLE_CAPABILITIES:
        mapping.setdefault(capability_key(capability), capability)
    mapping.update(_CANONICAL_ALIASES)
    return mapping


#: Índice de resolución construido una sola vez.
_CANONICAL_BY_KEY: Final[dict[str, str]] = _authored_canonical_by_key()


def canonical_capability(name: str) -> str:
    """Nombre canónico **legible** de una capacidad declarada por el modelo.

    Resuelve en cuatro pasos, de más preciso a más general:

    1. se descarta un prefijo de familia (``LANGUAGE: TypeScript 5.x``);
    2. el alias exacto (``Node.js`` → ``node20``);
    3. la familia sin versión (``typescript5.4`` → ``typescript``);
    4. el nombre tal como lo escribió el modelo, normalizado.

    El paso 4 importa: una capacidad que PUNTO no conoce **no** se mutila. Antes
    devolvía ``awsecs/fargate``; ahora devuelve ``aws ecs/fargate``, que es lo que el
    humano necesita leer en el informe.
    """
    cleaned = _KIND_PREFIX.sub("", name.strip())
    key = capability_key(name)
    known = _CANONICAL_BY_KEY.get(key)
    if known is not None:
        return known
    stripped = _VERSION_SUFFIX.sub("", key)
    family = _CANONICAL_BY_KEY.get(stripped)
    if family is not None:
        return family
    display = " ".join(cleaned.lower().split())
    without_version = _VERSION_TAIL.sub("", display).strip()
    return without_version or display


def capability_status(name: str) -> tuple[CapabilityStatus, str]:
    """Estado real de una capacidad, con su motivo.

    Returns:
        El estado y una explicación legible. Nunca lanza: una capacidad
        desconocida es ``UNKNOWN``, jamás ``AVAILABLE``.
    """
    canonical = canonical_capability(name)
    if canonical in AVAILABLE_CAPABILITIES:
        return CapabilityStatus.AVAILABLE, f"disponible en PUNTO ({canonical})"
    reason = KNOWN_UNAVAILABLE_CAPABILITIES.get(canonical)
    if reason is not None:
        return CapabilityStatus.MISSING, reason
    return (
        CapabilityStatus.UNKNOWN,
        f"PUNTO no tiene información sobre {canonical!r}: se trata como no disponible",
    )


def detect_capability_gaps(
    profile: ProjectCapabilityProfile,
    tasks: tuple[PlannedTask, ...] = (),
) -> tuple[CapabilityGap, ...]:
    """Detecta las capacidades exigidas por el plan que PUNTO no puede ejecutar.

    El orden es determinista: primero las capacidades del perfil en su orden de
    declaración y después las que solo aparecen en las tareas, en orden de tarea.

    Una misma capacidad se reporta **una sola vez**: si el perfil la declara dos veces
    con distinta ortografía (``PostgreSQL 16`` y ``postgresql``) y además hay tareas
    que la exigen, los huecos se funden en una entrada con todas las tareas que la
    requieren. Duplicar el informe no aporta nada y hace ilegible la evidencia.

    Args:
        profile: Perfil de capacidades declarado por el Architect.
        tasks: Tareas del roadmap, para saber qué tarea exige cada hueco.

    Returns:
        Los huecos encontrados. Vacío significa que PUNTO puede ejecutar todo lo
        que el plan exige.
    """
    gaps: list[CapabilityGap] = []
    #: Posición de cada capacidad ya vista en ``gaps``; ``-1`` si no es un hueco.
    position_by_capability: dict[str, int] = {}

    def register(
        canonical: str,
        kind: CapabilityKind,
        status: CapabilityStatus,
        detail: str,
        task_id: str = "",
    ) -> None:
        """Añade el hueco o amplía las tareas que lo exigen."""
        position = position_by_capability.get(canonical)
        if position is not None:
            if task_id and position >= 0:
                existing = gaps[position]
                if task_id not in existing.required_by:
                    gaps[position] = existing.model_copy(
                        update={"required_by": (*existing.required_by, task_id)}
                    )
            return
        if not status.is_gap:
            position_by_capability[canonical] = -1
            return
        position_by_capability[canonical] = len(gaps)
        gaps.append(
            CapabilityGap(
                capability=canonical,
                kind=kind,
                status=status,
                required_by=(task_id,) if task_id else _tasks_requiring(tasks, canonical),
                detail=detail,
            )
        )

    for kind, name in profile.entries():
        status, detail = capability_status(name)
        register(canonical_capability(name), kind, status, detail)

    for task in tasks:
        for name in task.required_capabilities:
            canonical = canonical_capability(name)
            status, detail = capability_status(name)
            register(canonical, _infer_kind(canonical), status, detail, task.id)

    return tuple(gaps)


def _tasks_requiring(tasks: tuple[PlannedTask, ...], capability: str) -> tuple[str, ...]:
    """Identificadores de las tareas que exigen una capacidad, en orden."""
    return tuple(
        task.id
        for task in tasks
        if any(canonical_capability(name) == capability for name in task.required_capabilities)
    )


#: Familias deducibles de forma determinista a partir del nombre canónico.
_KIND_HINTS: Final[tuple[tuple[CapabilityKind, frozenset[str]], ...]] = (
    (
        CapabilityKind.VALIDATOR,
        frozenset({"eslint", "jest", "mypy", "pytest", "pytest-cov", "ruff", "tsc", "vitest"}),
    ),
    (
        CapabilityKind.DATABASE,
        frozenset({"mongodb", "mysql", "postgres", "postgresql", "redis"}),
    ),
    (
        CapabilityKind.PACKAGE_MANAGER,
        frozenset({"bun", "cargo", "gradle", "maven", "npm", "pnpm", "yarn"}),
    ),
    (
        CapabilityKind.DEPLOYMENT_TARGET,
        frozenset({"aws", "azure", "docker", "docker-compose", "gcp", "kubernetes",
                   "netlify", "vercel"}),
    ),
)


def _infer_kind(capability: str) -> CapabilityKind:
    """Familia probable de una capacidad que solo aparece en las tareas."""
    for kind, names in _KIND_HINTS:
        if capability in names:
            return kind
    return CapabilityKind.EXECUTION_PROFILE


__all__ = [
    "AVAILABLE_CAPABILITIES",
    "AVAILABLE_PYTHON_PROFILE",
    "KNOWN_UNAVAILABLE_CAPABILITIES",
    "canonical_capability",
    "capability_status",
    "detect_capability_gaps",
]
