"""Traducción de acciones web a ``argv`` controlado (ENGINE-5.3).

El modelo **no** propone comandos: propone o pide una de las acciones de
:class:`~punto.schemas.web.WebCommandKind`, y PUNTO decide el ``argv`` exacto. Este módulo es esa
traducción, con tres reglas que no se negocian:

1. **no hay shell**: un plan es una tupla de argumentos, nunca una cadena, así que se ejecuta con
   ``shell=False`` por construcción. No hay metacaracteres, ni tuberías, ni ``&&``, ni sustitución
   de comandos, ni ``eval``, ni ``-c``: nada de lo que venga del proyecto se interpreta como
   código. Un nombre de script hostil sigue siendo **un** argumento.
2. **la política decide, no el modelo**: cada acción exige una capacidad real del perfil
   (TypeScript configurado, script declarado, dependencia declarada). Si no la hay, se lanza
   :class:`WebCommandPolicyError` en lugar de improvisar un comando parecido.
3. **la red no se pide aquí**: ningún plan lleva ``--network`` ni ninguna bandera que amplíe
   permisos. El aislamiento y la red los decide el backend del sandbox; un plan no puede
   autorizarse a sí mismo.

``CAPTURE_SCREENSHOT`` es la excepción deliberada y está documentada en su planificador: **no** es
un comando del proyecto, es una acción interna del probe de PUNTO. Devuelve un plan con
``argv=()`` para que el orquestador lo trate como acción del probe y no lo lance como si fuera un
script del proyecto. Un ``argv`` vacío nunca llega a un proceso.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from punto.model_context import safe_path_label
from punto.schemas.web import PackageManager, WebCommandKind, WebProjectProfile
from punto.web.detection import NPM_LOCKFILE


@dataclass(frozen=True, slots=True)
class WebCommandPlan:
    """Un ``argv`` decidido por PUNTO para una acción web.

    Es inmutable a propósito: una vez decidido el plan, nadie lo reescribe por el camino. ``argv``
    es una tupla, nunca una cadena, así que se ejecuta con ``shell=False`` por construcción. Un
    ``argv`` vacío no es un comando: es una acción que ejecuta el propio probe de PUNTO dentro del
    sandbox (``CAPTURE_SCREENSHOT``).
    """

    kind: WebCommandKind
    argv: tuple[str, ...]
    detail: str = ""


class WebCommandPolicyError(RuntimeError):
    """La acción pedida no aplica a este proyecto, o el perfil no la permite.

    No hereda de :class:`punto.tools.errors.DeveloperExecutionError` a propósito: planificar no es
    ejecutar, y la auditoría debe poder distinguir «PUNTO no va a lanzar esto» de «esto falló al
    ejecutarse». Es la misma separación que ya usan ``PlanningError`` y ``QAError``.
    """


#: Ejecutable de cada gestor de paquetes.
MANAGER_EXECUTABLES: Final[dict[PackageManager, str]] = {
    PackageManager.NPM: "npm",
    PackageManager.PNPM: "pnpm",
    PackageManager.YARN: "yarn",
    PackageManager.BUN: "bun",
}

#: Gestor que PUNTO usa cuando el proyecto no declara ninguno: npm es el que trae Node.
DEFAULT_MANAGER_EXECUTABLE: Final[str] = "npm"

#: Scripts que arrancan la vista previa, en orden de preferencia.
PREVIEW_SCRIPTS: Final[tuple[str, ...]] = ("start", "preview")

#: Dependencia que tiene que estar declarada para poder planificar ``RUN_PLAYWRIGHT``.
PLAYWRIGHT_DEPENDENCY: Final[str] = "@playwright/test"


def plan_command(
    kind: WebCommandKind, profile: WebProjectProfile, *, route: str = ""
) -> WebCommandPlan:
    """Traduce una acción conceptual al ``argv`` exacto que PUNTO ejecutará.

    Args:
        kind: Acción pedida. No se acepta ningún comando en texto libre.
        profile: Perfil detectado del proyecto, del que dependen los scripts y el gestor.
        route: Ruta lógica, solo para las acciones que la necesitan (``CAPTURE_SCREENSHOT``).

    Returns:
        El plan, con el ``argv`` y el motivo de la decisión.

    Raises:
        WebCommandPolicyError: si el perfil no tiene la capacidad que la acción exige, o si la
            acción no existe.
    """
    planner = _PLANNERS.get(kind)
    if planner is None:
        raise WebCommandPolicyError(f"acción web desconocida: {kind!r}")
    return planner(profile, route)


def available_commands(profile: WebProjectProfile) -> tuple[WebCommandKind, ...]:
    """Acciones planificables para ese perfil, en el orden del enum.

    Se calcula intentando planificar cada acción, así que esta lista no puede divergir de
    :func:`plan_command`: es exactamente el conjunto de acciones que no lanzan
    :class:`WebCommandPolicyError`.
    """
    available: list[WebCommandKind] = []
    for kind in WebCommandKind:
        try:
            plan_command(kind, profile)
        except WebCommandPolicyError:
            continue
        available.append(kind)
    return tuple(available)


def _manager_executable(profile: WebProjectProfile) -> str:
    """Ejecutable del gestor detectado, con npm como último recurso **declarado**.

    Si el perfil no pudo identificar el gestor (``UNKNOWN``), el plan usa npm y lo dice en su
    ``detail``: sustituir un gestor por otro sin decirlo convertiría una detección incompleta en
    una instalación distinta de la que el proyecto espera.
    """
    return MANAGER_EXECUTABLES.get(profile.package_manager, DEFAULT_MANAGER_EXECUTABLE)


def _manager_note(profile: WebProjectProfile) -> str:
    """Aviso explícito cuando el gestor no se pudo detectar y se asume npm."""
    if profile.package_manager is PackageManager.UNKNOWN:
        return f"; gestor sin detectar: se asume {DEFAULT_MANAGER_EXECUTABLE}"
    return ""


def _script_plan(
    kind: WebCommandKind, profile: WebProjectProfile, script: str, detail: str
) -> WebCommandPlan:
    """Plan de ``<gestor> run <script>``, con el nombre de script tomado del perfil."""
    return WebCommandPlan(
        kind=kind,
        argv=(_manager_executable(profile), "run", script),
        detail=f"{detail}{_manager_note(profile)}",
    )


def _plan_install(profile: WebProjectProfile, route: str) -> WebCommandPlan:
    """Plan de instalación de dependencias, sin decidir la red.

    npm es el caso con dos formas: ``npm ci`` cuando hay ``package-lock.json`` (instalación
    reproducible) y ``npm install`` cuando no lo hay. Los demás gestores detectados usan su
    instalación con el lockfile congelado.
    """
    manager = profile.package_manager
    if manager is PackageManager.PNPM:
        return WebCommandPlan(
            kind=WebCommandKind.INSTALL_DEPENDENCIES,
            argv=("pnpm", "install", "--frozen-lockfile"),
            detail="instalación reproducible: el lockfile de pnpm se congela",
        )
    if manager is PackageManager.YARN:
        return WebCommandPlan(
            kind=WebCommandKind.INSTALL_DEPENDENCIES,
            argv=("yarn", "install", "--frozen-lockfile"),
            detail="instalación reproducible: el lockfile de yarn se congela",
        )
    if manager is PackageManager.BUN:
        return WebCommandPlan(
            kind=WebCommandKind.INSTALL_DEPENDENCIES,
            argv=("bun", "install", "--frozen-lockfile"),
            detail="instalación reproducible: el lockfile de bun se congela",
        )
    if profile.lockfile == NPM_LOCKFILE:
        return WebCommandPlan(
            kind=WebCommandKind.INSTALL_DEPENDENCIES,
            argv=("npm", "ci"),
            detail=f"instalación reproducible: hay {NPM_LOCKFILE}, así que npm ci",
        )
    return WebCommandPlan(
        kind=WebCommandKind.INSTALL_DEPENDENCIES,
        argv=("npm", "install"),
        detail=f"sin {NPM_LOCKFILE}: npm install resuelve las versiones{_manager_note(profile)}",
    )


def _plan_typecheck(profile: WebProjectProfile, route: str) -> WebCommandPlan:
    """Plan de comprobación de tipos: exige TypeScript declarado por el proyecto.

    Usa ``npx --no-install`` y no el gestor detectado: ``--no-install`` obliga a resolver el
    binario **ya instalado** en el proyecto y falla si no está, así que la comprobación nunca se
    convierte en una descarga de red por sorpresa.
    """
    if not profile.has_typescript:
        raise WebCommandPolicyError(
            "TYPECHECK no aplica: el proyecto no declara typescript en sus dependencias ni "
            "tiene tsconfig.json"
        )
    return WebCommandPlan(
        kind=WebCommandKind.TYPECHECK,
        argv=("npx", "--no-install", "tsc", "--noEmit"),
        detail="comprobación de tipos sin emitir archivos y sin instalar nada",
    )


def _plan_build(profile: WebProjectProfile, route: str) -> WebCommandPlan:
    """Plan de construcción: solo si el proyecto declara el script ``build``."""
    if not profile.has_script("build"):
        raise WebCommandPolicyError(
            "BUILD no aplica: el proyecto no declara el script 'build' en package.json"
        )
    return _script_plan(
        WebCommandKind.BUILD, profile, "build", "construcción declarada por el script 'build'"
    )


def _plan_start_preview(profile: WebProjectProfile, route: str) -> WebCommandPlan:
    """Plan de vista previa: prefiere el script ``start`` y recurre a ``preview``."""
    for script in PREVIEW_SCRIPTS:
        if profile.has_script(script):
            return _script_plan(
                WebCommandKind.START_PREVIEW,
                profile,
                script,
                f"servidor local declarado por el script '{script}'",
            )
    raise WebCommandPolicyError(
        "START_PREVIEW no aplica: el proyecto no declara ninguno de los scripts "
        f"{', '.join(repr(name) for name in PREVIEW_SCRIPTS)} en package.json"
    )


def _plan_run_tests(profile: WebProjectProfile, route: str) -> WebCommandPlan:
    """Plan de pruebas: solo si el proyecto declara el script ``test``."""
    if not profile.has_script("test"):
        raise WebCommandPolicyError(
            "RUN_TESTS no aplica: el proyecto no declara el script 'test' en package.json"
        )
    return _script_plan(
        WebCommandKind.RUN_TESTS, profile, "test", "pruebas declaradas por el script 'test'"
    )


def _plan_run_playwright(profile: WebProjectProfile, route: str) -> WebCommandPlan:
    """Plan de pruebas end-to-end: exige que Playwright esté declarado por el proyecto.

    Igual que ``TYPECHECK``, usa ``npx --no-install`` en lugar del gestor detectado: el binario
    tiene que estar ya instalado en el proyecto, y si no lo está la acción falla en lugar de
    descargar nada.
    """
    if PLAYWRIGHT_DEPENDENCY not in profile.dependency_names:
        raise WebCommandPolicyError(
            f"RUN_PLAYWRIGHT no aplica: el proyecto no declara {PLAYWRIGHT_DEPENDENCY!r}"
        )
    return WebCommandPlan(
        kind=WebCommandKind.RUN_PLAYWRIGHT,
        argv=("npx", "--no-install", "playwright", "test"),
        detail="pruebas end-to-end con el Playwright que declara el propio proyecto",
    )


def _plan_capture_screenshot(profile: WebProjectProfile, route: str) -> WebCommandPlan:
    """Acción de captura: **no** es un comando del proyecto.

    Devuelve ``argv=()`` a propósito, y esa es la decisión de diseño de esta acción: el
    orquestador debe leerlo como «esto lo hace el probe de PUNTO dentro del sandbox», no como un
    proceso que haya que lanzar. La captura necesita un navegador real y el probe que ya vive en
    la imagen del sandbox, y no existe ningún script del proyecto que la equivalga; inventar un
    ``npx playwright screenshot`` aquí sería prometer una ejecución que PUNTO no controla.

    La ruta se sanea con :func:`punto.model_context.safe_path_label` porque el ``detail`` acaba en
    informes y registros: un salto de línea o un NUL en una ruta no debe poder inyectarse ahí.
    """
    detail = (
        "acción interna del probe de PUNTO, no un comando del proyecto: argv vacío a propósito; "
        "el probe captura el PNG dentro del sandbox y PUNTO lo registra como artefacto verificado"
    )
    if route:
        detail = f"{detail}; ruta lógica: {safe_path_label(route)}"
    return WebCommandPlan(kind=WebCommandKind.CAPTURE_SCREENSHOT, argv=(), detail=detail)


#: Planificador de cada acción.
#:
#: Es la única fuente de verdad de qué acciones existen y cómo se traducen, y ``available_commands``
#: la recorre: un plan que se pueda construir y una acción declarada disponible no pueden divergir.
#: Todos los planificadores comparten la firma ``(perfil, ruta)``; solo ``CAPTURE_SCREENSHOT`` usa
#: la ruta, porque es la única acción que no es un comando de proyecto.
_PLANNERS: Final[dict[WebCommandKind, Callable[[WebProjectProfile, str], WebCommandPlan]]] = {
    WebCommandKind.INSTALL_DEPENDENCIES: _plan_install,
    WebCommandKind.TYPECHECK: _plan_typecheck,
    WebCommandKind.BUILD: _plan_build,
    WebCommandKind.START_PREVIEW: _plan_start_preview,
    WebCommandKind.RUN_TESTS: _plan_run_tests,
    WebCommandKind.RUN_PLAYWRIGHT: _plan_run_playwright,
    WebCommandKind.CAPTURE_SCREENSHOT: _plan_capture_screenshot,
}


__all__ = [
    "WebCommandPlan",
    "WebCommandPolicyError",
    "available_commands",
    "plan_command",
]
