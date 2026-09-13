"""Detección determinista del proyecto web (ENGINE-5.3).

PUNTO no se cree lo que el modelo diga del stack: lee ``package.json``, los lockfiles y los
archivos de configuración que hay en el workspace, y declara la **evidencia** de cada parte del
perfil. Sin evidencia, el perfil no vale: no es una opinión sobre el proyecto, es su trazabilidad.

Reglas de la detección, sin excepciones:

- **solo lectura y sin ejecución**: no se ejecuta ni un comando del proyecto ni una línea de su
  código. Un ``package.json`` con scripts hostiles se lee como texto y nada más.
- **frontera de rutas del motor**: cada ruta candidata se resuelve con
  :func:`punto.model_context.resolve_within_workspace`, la misma frontera que usa el contexto del
  modelo, así que un enlace que salga del workspace no se lee nunca. Aquí no se construye ninguna
  ruta a mano.
- **rutas lógicas**: el perfil no declara ni una ruta absoluta del host. Todo lo que sale de aquí
  es una ruta relativa posix (``src/app/globals.css``), porque el host que detecta no tiene por qué
  ser el host que audita.
- **determinismo**: el mismo workspace produce el mismo perfil, con las tuplas ordenadas, sin
  duplicados y con la evidencia en un orden fijo —``package.json``, framework, configuración de
  Next.js, TypeScript, Tailwind, gestor de paquetes y lockfiles, scripts y dependencias, y
  ``engines``—.
- **un archivo malo no revienta nada**: un ``package.json`` con JSON inválido, que no sea un objeto
  JSON o que supere :data:`MAX_PROFILE_FILE_BYTES` produce un perfil con lo que se pudo leer y la
  evidencia del fallo, nunca una excepción.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from punto.model_context import resolve_within_workspace
from punto.schemas.web import (
    MAX_DEPENDENCY_NAMES,
    MAX_SCRIPT_NAMES,
    PackageManager,
    WebFramework,
    WebProjectProfile,
)

if TYPE_CHECKING:
    from punto.audit.logger import AuditLogger

#: Archivo de manifiesto. Sin él no hay proyecto web reconocible.
PACKAGE_JSON: Final[str] = "package.json"

#: Configuraciones de Next.js, en el orden de prioridad documentado (``js``, ``mjs``, ``ts``,
#: ``cjs``): si un proyecto tuviera varias, gana la primera de esta lista.
NEXT_CONFIG_NAMES: Final[tuple[str, ...]] = (
    "next.config.js",
    "next.config.mjs",
    "next.config.ts",
    "next.config.cjs",
)

#: Configuración de TypeScript.
TSCONFIG_NAME: Final[str] = "tsconfig.json"

#: Configuraciones de Tailwind, en el orden de prioridad documentado. Tailwind v4 puede no tener
#: ninguna: en ese caso su configuración vive dentro del CSS (ver :data:`TAILWIND_CSS_PATTERN`).
TAILWIND_CONFIG_NAMES: Final[tuple[str, ...]] = (
    "tailwind.config.ts",
    "tailwind.config.js",
    "tailwind.config.mjs",
    "tailwind.config.cjs",
)

#: Secciones del ``package.json`` que declaran dependencias.
#:
#: ``optionalDependencies`` queda fuera a propósito: una dependencia opcional no es algo que PUNTO
#: necesite para instalar, comprobar tipos, construir o probar el proyecto.
DEPENDENCY_SECTIONS: Final[tuple[str, ...]] = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
)

#: Lockfile de npm. Decide entre ``npm ci`` y ``npm install`` en la política de comandos.
NPM_LOCKFILE: Final[str] = "package-lock.json"

#: Lockfiles reconocidos, del más explícito al más genérico.
#:
#: Este es el orden de prioridad documentado que usa PUNTO cuando el campo ``packageManager`` no
#: decide: pnpm, yarn y bun solo aparecen si alguien los eligió, mientras que
#: ``package-lock.json`` es el que npm escribe por defecto, así que es el último recurso.
#: ``bun.lock`` (formato actual) precede a ``bun.lockb`` (formato binario heredado).
LOCKFILE_PRIORITY: Final[tuple[tuple[str, PackageManager], ...]] = (
    ("pnpm-lock.yaml", PackageManager.PNPM),
    ("yarn.lock", PackageManager.YARN),
    ("bun.lock", PackageManager.BUN),
    ("bun.lockb", PackageManager.BUN),
    (NPM_LOCKFILE, PackageManager.NPM),
)

#: Prefijos aceptados en el campo ``packageManager``, en orden de comprobación.
PACKAGE_MANAGER_PREFIXES: Final[tuple[tuple[str, PackageManager], ...]] = (
    ("pnpm@", PackageManager.PNPM),
    ("yarn@", PackageManager.YARN),
    ("bun@", PackageManager.BUN),
    ("npm@", PackageManager.NPM),
)

#: Frameworks por marcador de dependencia, en orden de prioridad documentado.
#:
#: ``react`` se comprueba al final a propósito: es dependencia de Next.js y de integraciones como
#: ``@astrojs/react``, así que un proyecto con ``astro`` y ``react`` es un proyecto Astro que usa
#: React, no un proyecto React. Next.js no aparece en esta tabla porque además se detecta por su
#: archivo de configuración, sin exigir que la dependencia esté declarada.
FRAMEWORK_PRIORITY: Final[tuple[tuple[WebFramework, tuple[str, ...]], ...]] = (
    (WebFramework.VUE, ("vue", "nuxt")),
    (WebFramework.SVELTE, ("svelte", "@sveltejs/kit")),
    (WebFramework.ASTRO, ("astro",)),
    (WebFramework.REACT, ("react",)),
)

#: Directorios donde PUNTO busca una hoja de estilo con Tailwind declarado dentro del CSS.
TAILWIND_CSS_ROOTS: Final[tuple[str, ...]] = ("app", "src", "styles")

#: Directivas que declaran Tailwind dentro de una hoja de estilo.
#:
#: ``@import "tailwindcss"`` es la forma de Tailwind v4, y cubre también sus subimports
#: (``tailwindcss/preflight``, ``tailwindcss/utilities``); ``@tailwind`` es la forma de v3.
TAILWIND_CSS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"""@import\s+["']tailwindcss(?:/[^"']*)?["']|@tailwind\b"""
)

#: Nombres de directorio que el recorrido de hojas de estilo nunca abre: son artefactos de
#: construcción o dependencias ya instaladas, no código del proyecto.
IGNORED_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".next",
        ".nuxt",
        ".output",
        ".svelte-kit",
        ".turbo",
        ".venv",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "out",
        "venv",
    }
)

#: Profundidad máxima del recorrido de hojas de estilo.
MAX_CSS_SCAN_DEPTH: Final[int] = 4

#: Máximo de entradas (archivos y directorios) que el recorrido de hojas de estilo visita. Un
#: ``node_modules`` gigante no convierte la detección en una lectura sin límite.
MAX_CSS_SCAN_ENTRIES: Final[int] = 2_000

#: Tamaño máximo de un archivo que PUNTO lee para perfilar el proyecto: un ``package.json`` de
#: 50 MB no es un manifiesto, es un problema, y se declara como tal.
MAX_PROFILE_FILE_BYTES: Final[int] = 1_000_000


@dataclass(frozen=True, slots=True)
class FileProbe:
    """Lo que PUNTO pudo averiguar de un archivo candidato del workspace.

    Separa las tres cosas que la detección necesita distinguir: que el archivo esté, que su ruta
    la haya rechazado la frontera del workspace, y que existiendo no se haya podido leer.
    """

    logical_path: str
    present: bool = False
    unsafe: bool = False
    unreadable: str = ""
    text: str = ""


def detect_web_project(
    workspace: Path | str,
    *,
    audit: AuditLogger | None = None,
    task_id: UUID | None = None,
    project_id: UUID | None = None,
) -> WebProjectProfile:
    """Perfila el proyecto web del workspace, leyendo archivos y sin ejecutar nada.

    Args:
        workspace: Raíz del workspace que se perfila.
        audit: Registro de auditoría; si se pasa junto con los identificadores, se registra el
            perfil detectado por sus metadatos.
        task_id: Tarea a la que pertenece el perfilado, para poder auditarlo.
        project_id: Proyecto al que pertenece, para poder auditarlo.

    Returns:
        El perfil detectado, con la evidencia de cada parte. Nunca lanza por un archivo ausente,
        ilegible o malformado: esos casos se declaran en ``evidence``.
    """
    root = Path(workspace)

    package = _probe(root, PACKAGE_JSON)
    manifest, manifest_notes = _parse_manifest(package)

    declared_scripts = _string_names(manifest.get("scripts"))
    declared_dependencies = _declared_dependencies(manifest)
    script_names = declared_scripts[:MAX_SCRIPT_NAMES]
    dependency_names = declared_dependencies[:MAX_DEPENDENCY_NAMES]
    node_requirement = _node_requirement(manifest)

    package_manager, lockfile, present_lockfiles, declaration = _detect_package_manager(
        manifest, root
    )

    next_config = _first_present(root, NEXT_CONFIG_NAMES)
    typescript_config = _first_present(root, (TSCONFIG_NAME,))
    tailwind_config = _first_present(root, TAILWIND_CONFIG_NAMES)
    tailwind_css, tailwind_directive = _find_tailwind_css(root)

    framework = _detect_framework(
        dependencies=dependency_names,
        next_config=next_config,
        has_scripts=bool(script_names),
        has_manifest=package.present,
    )
    has_typescript = "typescript" in dependency_names or bool(typescript_config)
    has_tailwind = (
        "tailwindcss" in dependency_names or bool(tailwind_config) or bool(tailwind_css)
    )

    # La evidencia se arma en un orden fijo y documentado, para que dos detecciones del mismo
    # workspace produzcan exactamente la misma tupla.
    evidence: list[str] = []
    if package.unsafe:
        evidence.append(f"{PACKAGE_JSON}: ruta rechazada por la frontera del workspace")
    elif package.unreadable:
        evidence.append(f"{PACKAGE_JSON} ilegible: {package.unreadable}")
    elif not package.present:
        evidence.append(f"{PACKAGE_JSON} ausente")
    else:
        evidence.append(f"{PACKAGE_JSON} presente")
    evidence.extend(manifest_notes)

    # Framework: qué lo demuestra, sea una dependencia o una configuración.
    if framework is WebFramework.NEXTJS and "next" in dependency_names:
        evidence.append(f"{PACKAGE_JSON}: dependencia next")
    elif framework is WebFramework.NODE:
        evidence.append(f"{PACKAGE_JSON}: scripts declarados sin framework reconocido")
    elif framework is WebFramework.UNKNOWN:
        evidence.append("sin framework reconocido")
    else:
        for candidate, markers in FRAMEWORK_PRIORITY:
            if candidate is not framework:
                continue
            for marker in markers:
                if marker in dependency_names:
                    evidence.append(f"{PACKAGE_JSON}: dependencia {marker}")
    if next_config:
        evidence.append(f"{next_config} presente")

    # TypeScript.
    if "typescript" in dependency_names:
        evidence.append(f"{PACKAGE_JSON}: dependencia typescript")
    if typescript_config:
        evidence.append(f"{typescript_config} presente")

    # Tailwind: configuración en la raíz, dependencia declarada, o v4 dentro del CSS.
    if "tailwindcss" in dependency_names:
        evidence.append(f"{PACKAGE_JSON}: dependencia tailwindcss")
    if tailwind_config:
        evidence.append(f"{tailwind_config} presente")
    if tailwind_css:
        evidence.append(f"{tailwind_css}: directiva {tailwind_directive}")

    # Gestor de paquetes: la declaración, todos los lockfiles presentes y el veredicto.
    if declaration:
        evidence.append(f"{PACKAGE_JSON}: packageManager {declaration}")
    for name in present_lockfiles:
        evidence.append(f"lockfile: {name}")
    if package_manager is PackageManager.UNKNOWN:
        evidence.append("sin gestor de paquetes declarado")
    elif lockfile:
        evidence.append(f"gestor de paquetes: {package_manager.value} por {lockfile}")
    else:
        evidence.append(f"gestor de paquetes: {package_manager.value} sin lockfile propio")

    # Scripts, dependencias y Node.
    if script_names:
        note = _truncation_note(len(declared_scripts), len(script_names))
        evidence.append(f"{PACKAGE_JSON}: {len(script_names)} script(s) declarado(s){note}")
    if dependency_names:
        note = _truncation_note(len(declared_dependencies), len(dependency_names))
        evidence.append(
            f"{PACKAGE_JSON}: {len(dependency_names)} dependencia(s) declarada(s){note}"
        )
    if node_requirement:
        evidence.append(f"{PACKAGE_JSON}: engines.node {node_requirement}")

    profile = WebProjectProfile(
        framework=framework,
        package_manager=package_manager,
        lockfile=lockfile,
        package_json=PACKAGE_JSON if package.present else "",
        has_typescript=has_typescript,
        typescript_config=typescript_config,
        has_tailwind=has_tailwind,
        tailwind_config=tailwind_config,
        next_config=next_config,
        script_names=script_names,
        dependency_names=dependency_names,
        node_requirement=node_requirement,
        evidence=tuple(evidence),
    )
    if audit is not None and task_id is not None and project_id is not None:
        audit.log_web_profile_detected(
            project_id=project_id,
            task_id=task_id,
            framework=profile.framework.value,
            package_manager=profile.package_manager.value,
            has_typescript=profile.has_typescript,
            has_tailwind=profile.has_tailwind,
            evidence=profile.evidence,
        )
    return profile


def _probe(workspace: Path, logical_path: str) -> FileProbe:
    """Lee un archivo candidato del workspace, sin salir de él y sin lanzar excepciones.

    La ruta pasa por :func:`resolve_within_workspace`, así que un enlace que escape del workspace
    se rechaza en lugar de leerse. Un archivo que supere :data:`MAX_PROFILE_FILE_BYTES` no se lee
    siquiera: se declara ilegible por tamaño en la evidencia.
    """
    resolved = resolve_within_workspace(workspace, logical_path)
    if resolved is None:
        return FileProbe(logical_path=logical_path, unsafe=True)
    try:
        if not resolved.is_file():
            return FileProbe(logical_path=logical_path)
        if resolved.stat().st_size > MAX_PROFILE_FILE_BYTES:
            return FileProbe(
                logical_path=logical_path,
                present=True,
                unreadable=f"supera el límite de {MAX_PROFILE_FILE_BYTES} bytes",
            )
        # ``utf-8-sig`` tolera el BOM que Windows escribe por defecto: un BOM delante de ``{``
        # haría fallar el JSON por un motivo que no es del proyecto.
        text = resolved.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return FileProbe(
            logical_path=logical_path, present=True, unreadable="no se pudo leer"
        )
    return FileProbe(logical_path=logical_path, present=True, text=text)


def _parse_manifest(package: FileProbe) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Manifiesto legible, y las notas de lo que no se pudo leer.

    Un ``package.json`` ilegible no es una excepción: es un perfil sin manifiesto y con el motivo
    escrito en la evidencia.

    Returns:
        ``(manifiesto, notas)``. El manifiesto queda vacío si no se pudo leer o si el JSON no es
        un objeto; ``notas`` lleva el motivo en ese caso y ninguna nota si el archivo no está.
    """
    if not package.present or package.unreadable:
        return {}, ()
    try:
        parsed: object = json.loads(package.text)
    except json.JSONDecodeError:
        return {}, (f"{package.logical_path} ilegible: JSON inválido",)
    if not isinstance(parsed, dict):
        return {}, (f"{package.logical_path} ilegible: no es un objeto JSON",)
    return {str(key): value for key, value in parsed.items()}, ()


def _as_object(value: object) -> dict[str, Any]:
    """El valor como objeto JSON con claves de texto, o un objeto vacío si no lo es."""
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _string_names(value: object) -> tuple[str, ...]:
    """Nombres de las claves de un objeto JSON, ordenados y sin duplicados."""
    return tuple(sorted(_as_object(value)))


def _declared_dependencies(manifest: dict[str, Any]) -> tuple[str, ...]:
    """Nombres de las dependencias declaradas, sin versiones, ordenados y sin duplicados.

    Se unen las secciones de :data:`DEPENDENCY_SECTIONS`: una dependencia que aparezca en dos de
    ellas se declara **una vez**. Solo nombres: la versión no forma parte del perfil.
    """
    names: set[str] = set()
    for section in DEPENDENCY_SECTIONS:
        names.update(_as_object(manifest.get(section)))
    return tuple(sorted(names))


def _node_requirement(manifest: dict[str, Any]) -> str:
    """Rango de Node declarado en ``engines.node``, o ``""`` si no se declara."""
    engines = _as_object(manifest.get("engines"))
    node = engines.get("node")
    return node if isinstance(node, str) else ""


def _manager_from_declaration(declaration: str) -> PackageManager:
    """Gestor declarado por el campo ``packageManager``, o ``UNKNOWN`` si no lo declara."""
    lowered = declaration.lower()
    for prefix, manager in PACKAGE_MANAGER_PREFIXES:
        if lowered.startswith(prefix):
            return manager
    return PackageManager.UNKNOWN


def _manager_for_lockfile(name: str) -> PackageManager:
    """Gestor al que corresponde un lockfile, o ``UNKNOWN`` si no es uno reconocido."""
    for lockfile_name, manager in LOCKFILE_PRIORITY:
        if lockfile_name == name:
            return manager
    return PackageManager.UNKNOWN


def _detect_package_manager(
    manifest: dict[str, Any], workspace: Path
) -> tuple[PackageManager, str, tuple[str, ...], str]:
    """Gestor efectivo, lockfile efectivo, lockfiles presentes y declaración explícita.

    Reglas, en este orden:

    1. el campo ``packageManager`` del ``package.json`` **manda** sobre los lockfiles: declara la
       intención del proyecto, y un lockfile suelto de otro gestor no la cambia;
    2. sin ``packageManager``, gana el lockfile de mayor prioridad de :data:`LOCKFILE_PRIORITY`;
    3. sin ninguna de las dos cosas, el gestor es ``UNKNOWN``.

    ``lockfile`` es el que respalda al gestor efectivo: si el gestor viene del campo
    ``packageManager`` y no hay lockfile suyo, queda vacío aunque existan lockfiles de otros
    gestores. Esos otros no se omiten: aparecen uno a uno en la evidencia, así que la discrepancia
    entre lo declarado y lo que hay en disco queda a la vista.

    Returns:
        ``(gestor, lockfile efectivo, lockfiles presentes en orden de prioridad, declaración)``.
    """
    present = tuple(name for name, _ in LOCKFILE_PRIORITY if _probe(workspace, name).present)
    raw_declaration = manifest.get("packageManager")
    declaration = raw_declaration.strip() if isinstance(raw_declaration, str) else ""
    declared = _manager_from_declaration(declaration)
    if declared is not PackageManager.UNKNOWN:
        matching = [name for name in present if _manager_for_lockfile(name) is declared]
        return declared, (matching[0] if matching else ""), present, declaration
    if present:
        return _manager_for_lockfile(present[0]), present[0], present, declaration
    return PackageManager.UNKNOWN, "", present, declaration


def _detect_framework(
    *,
    dependencies: tuple[str, ...],
    next_config: str,
    has_scripts: bool,
    has_manifest: bool,
) -> WebFramework:
    """Framework detectado, por dependencias, configuración y scripts.

    Sin ``package.json`` el perfil es ``UNKNOWN`` aunque exista un ``next.config.js`` suelto: un
    archivo de configuración huérfano no es un proyecto. Con manifiesto y scripts pero sin ningún
    framework conocido, el proyecto es ``NODE``.
    """
    if not has_manifest:
        return WebFramework.UNKNOWN
    if "next" in dependencies or next_config:
        return WebFramework.NEXTJS
    for framework, markers in FRAMEWORK_PRIORITY:
        if any(marker in dependencies for marker in markers):
            return framework
    if has_scripts:
        return WebFramework.NODE
    return WebFramework.UNKNOWN


def _first_present(workspace: Path, candidates: tuple[str, ...]) -> str:
    """Primera ruta lógica de la lista que existe como archivo del workspace, o ``""``."""
    for candidate in candidates:
        if _probe(workspace, candidate).present:
            return candidate
    return ""


def _find_tailwind_css(workspace: Path) -> tuple[str, str]:
    """Primera hoja de estilo que declara Tailwind, y la directiva que lo demuestra.

    Tailwind v4 se declara dentro del CSS (``@import "tailwindcss"``) y puede no tener ningún
    archivo de configuración en la raíz, así que sin esta búsqueda un proyecto v4 se perfilaría
    como si no usara Tailwind.

    Returns:
        ``(ruta lógica, directiva)``, o ``("", "")`` si ninguna hoja de estilo lo declara.
    """
    for logical_path in _iter_css_paths(workspace):
        probe = _probe(workspace, logical_path)
        if not probe.present or probe.unreadable:
            continue
        match = TAILWIND_CSS_PATTERN.search(probe.text)
        if match is not None:
            return logical_path, match.group(0)
    return "", ""


def _iter_css_paths(workspace: Path) -> tuple[str, ...]:
    """Rutas lógicas de las hojas ``*.css`` candidatas, en orden determinista.

    Recorre cada raíz de :data:`TAILWIND_CSS_ROOTS` en anchura, con las entradas ordenadas por
    nombre, sin abrir los directorios de :data:`IGNORED_DIRECTORY_NAMES` y sin descender por un
    enlace que salga del workspace. El recorrido está acotado por :data:`MAX_CSS_SCAN_DEPTH` y
    :data:`MAX_CSS_SCAN_ENTRIES`.
    """
    found: list[str] = []
    pending: deque[tuple[str, int]] = deque((root, 0) for root in TAILWIND_CSS_ROOTS)
    visited = 0
    while pending and visited < MAX_CSS_SCAN_ENTRIES:
        logical_dir, depth = pending.popleft()
        directory = resolve_within_workspace(workspace, logical_dir)
        if directory is None or not directory.is_dir():
            continue
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            if visited >= MAX_CSS_SCAN_ENTRIES:
                break
            visited += 1
            child = f"{logical_dir}/{entry.name}"
            if entry.is_dir():
                if depth < MAX_CSS_SCAN_DEPTH and entry.name not in IGNORED_DIRECTORY_NAMES:
                    pending.append((child, depth + 1))
                continue
            if entry.name.endswith(".css") and entry.is_file():
                found.append(child)
    return tuple(found)


def _truncation_note(declared: int, kept: int) -> str:
    """Aviso de recorte para la evidencia, o cadena vacía si no se recortó nada."""
    if declared <= kept:
        return ""
    return f" (recortado de {declared})"


__all__ = [
    "LOCKFILE_PRIORITY",
    "MAX_PROFILE_FILE_BYTES",
    "NPM_LOCKFILE",
    "detect_web_project",
]
