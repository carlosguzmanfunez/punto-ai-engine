"""Pruebas de la detección determinista del proyecto web (ENGINE-5.3).

Los proyectos son sintéticos y se escriben a mano en ``tmp_path``: la detección es lectura de
archivos, así que probarla no necesita red, ni npm, ni ejecutar nada del proyecto. Eso es
justamente lo que estas pruebas vigilan: que perfilar no ejecute, que un archivo malo no reviente
la detección, y que ninguna ruta del host se cuele en el perfil.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from punto.schemas.web import (
    MAX_DEPENDENCY_NAMES,
    MAX_SCRIPT_NAMES,
    PackageManager,
    WebFramework,
    WebProjectProfile,
)
from punto.web.detection import MAX_PROFILE_FILE_BYTES, detect_web_project


def write_manifest(workspace: Path, payload: object) -> None:
    """Escribe un ``package.json`` con el contenido indicado."""
    (workspace / "package.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_file(workspace: Path, relative: str, content: str = "") -> Path:
    """Escribe un archivo del proyecto, creando los directorios que falten."""
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def build_next_project(workspace: Path) -> None:
    """Proyecto Next.js + TypeScript + Tailwind, con lockfile de pnpm."""
    write_manifest(
        workspace,
        {
            "name": "landing",
            "private": True,
            "scripts": {"dev": "next dev", "build": "next build", "start": "next start"},
            "dependencies": {"next": "15.0.0", "react": "19.0.0", "react-dom": "19.0.0"},
            "devDependencies": {"typescript": "5.6.0", "tailwindcss": "3.4.4"},
            "engines": {"node": ">=20"},
        },
    )
    write_file(workspace, "tsconfig.json", "{}\n")
    write_file(workspace, "next.config.mjs", "export default {};\n")
    write_file(workspace, "tailwind.config.ts", "export default {};\n")
    write_file(workspace, "pnpm-lock.yaml", "lockfileVersion: 9\n")


def path_fields(profile: WebProjectProfile) -> tuple[str, ...]:
    """Todo lo que el perfil declara como ruta, más su evidencia."""
    return (
        profile.package_json,
        profile.typescript_config,
        profile.tailwind_config,
        profile.next_config,
        profile.lockfile,
        *profile.evidence,
    )


# ---------------------------------------------------------------------------
# Manifiesto ausente, ilegible o raro
# ---------------------------------------------------------------------------
def test_a_missing_manifest_is_an_unknown_non_web_project(tmp_path: Path) -> None:
    """Sin ``package.json`` no hay proyecto web, y no hay excepción."""
    profile = detect_web_project(tmp_path)

    assert profile.framework is WebFramework.UNKNOWN
    assert profile.is_web_project is False
    assert profile.package_json == ""
    assert profile.script_names == ()
    assert profile.dependency_names == ()
    assert profile.evidence == (
        "package.json ausente",
        "sin framework reconocido",
        "sin gestor de paquetes declarado",
    )


def test_invalid_json_is_declared_in_the_evidence_and_never_raises(tmp_path: Path) -> None:
    """Un JSON inválido se declara: el perfil no vale, pero tampoco revienta."""
    write_file(tmp_path, "package.json", "{ esto no es json")

    profile = detect_web_project(tmp_path)

    assert profile.framework is WebFramework.UNKNOWN
    assert profile.is_web_project is False
    assert profile.package_json == "package.json"
    assert profile.script_names == ()
    assert "package.json ilegible: JSON inválido" in profile.evidence


def test_a_manifest_that_is_not_an_object_is_declared(tmp_path: Path) -> None:
    """Un ``package.json`` que es una lista no es un manifiesto, y se dice."""
    write_file(tmp_path, "package.json", '["next"]')

    profile = detect_web_project(tmp_path)

    assert profile.framework is WebFramework.UNKNOWN
    assert "package.json ilegible: no es un objeto JSON" in profile.evidence


def test_an_oversized_manifest_is_declared_unreadable_by_size(tmp_path: Path) -> None:
    """Un manifiesto de más de 1 MB no se lee: se declara el motivo."""
    padding = "a" * (MAX_PROFILE_FILE_BYTES + 1)
    write_file(tmp_path, "package.json", f'{{"name": "{padding}"}}')

    profile = detect_web_project(tmp_path)

    expected = f"package.json ilegible: supera el límite de {MAX_PROFILE_FILE_BYTES} bytes"
    assert profile.package_json == "package.json"
    assert profile.framework is WebFramework.UNKNOWN
    assert expected in profile.evidence


def test_a_manifest_with_a_bom_is_still_read(tmp_path: Path) -> None:
    """El BOM que Windows escribe por defecto no convierte el manifiesto en ilegible."""
    payload = json.dumps({"scripts": {"dev": "next dev"}}).encode("utf-8")
    (tmp_path / "package.json").write_bytes(b"\xef\xbb\xbf" + payload)

    profile = detect_web_project(tmp_path)

    assert profile.script_names == ("dev",)
    assert profile.framework is WebFramework.NODE


# ---------------------------------------------------------------------------
# Framework
# ---------------------------------------------------------------------------
def test_next_with_typescript_and_tailwind_is_fully_profiled(tmp_path: Path) -> None:
    """Next.js, TypeScript y Tailwind se detectan con su evidencia."""
    build_next_project(tmp_path)

    profile = detect_web_project(tmp_path)

    assert profile.framework is WebFramework.NEXTJS
    assert profile.is_web_project is True
    assert profile.package_json == "package.json"
    assert profile.has_typescript is True
    assert profile.typescript_config == "tsconfig.json"
    assert profile.has_tailwind is True
    assert profile.tailwind_config == "tailwind.config.ts"
    assert profile.next_config == "next.config.mjs"
    assert profile.node_requirement == ">=20"
    assert profile.package_manager is PackageManager.PNPM
    assert profile.lockfile == "pnpm-lock.yaml"
    assert profile.script_names == ("build", "dev", "start")
    assert profile.dependency_names == (
        "next",
        "react",
        "react-dom",
        "tailwindcss",
        "typescript",
    )


def test_every_part_of_the_profile_has_evidence(tmp_path: Path) -> None:
    """Sin evidencia el perfil no vale: cada hallazgo se declara."""
    build_next_project(tmp_path)

    profile = detect_web_project(tmp_path)

    assert "package.json presente" in profile.evidence
    assert "package.json: dependencia next" in profile.evidence
    assert "next.config.mjs presente" in profile.evidence
    assert "tsconfig.json presente" in profile.evidence
    assert "tailwind.config.ts presente" in profile.evidence
    assert "lockfile: pnpm-lock.yaml" in profile.evidence
    assert "gestor de paquetes: PNPM por pnpm-lock.yaml" in profile.evidence
    assert "package.json: engines.node >=20" in profile.evidence


def test_a_next_config_alone_is_enough_for_nextjs(tmp_path: Path) -> None:
    """``next.config.*`` sin declarar la dependencia también es Next.js."""
    write_manifest(tmp_path, {"scripts": {"build": "next build"}})
    write_file(tmp_path, "next.config.ts", "export default {};\n")

    profile = detect_web_project(tmp_path)

    assert profile.framework is WebFramework.NEXTJS
    assert profile.next_config == "next.config.ts"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("next.config.js", "next.config.js"),
        ("next.config.mjs", "next.config.mjs"),
        ("next.config.ts", "next.config.ts"),
        ("next.config.cjs", "next.config.cjs"),
    ],
)
def test_every_next_config_variant_is_detected(
    tmp_path: Path, filename: str, expected: str
) -> None:
    """Las cuatro variantes documentadas de configuración de Next.js cuentan."""
    write_manifest(tmp_path, {"scripts": {"dev": "next dev"}})
    write_file(tmp_path, filename, "export default {};\n")

    assert detect_web_project(tmp_path).next_config == expected


def test_react_without_next_is_react(tmp_path: Path) -> None:
    """React sin Next.js es un proyecto React."""
    write_manifest(
        tmp_path,
        {
            "scripts": {"dev": "vite", "build": "vite build"},
            "dependencies": {"react": "19.0.0", "react-dom": "19.0.0"},
        },
    )

    profile = detect_web_project(tmp_path)

    assert profile.framework is WebFramework.REACT
    assert profile.is_web_project is True
    assert "package.json: dependencia react" in profile.evidence


@pytest.mark.parametrize(
    ("dependency", "expected"),
    [
        ("vue", WebFramework.VUE),
        ("nuxt", WebFramework.VUE),
        ("svelte", WebFramework.SVELTE),
        ("@sveltejs/kit", WebFramework.SVELTE),
        ("astro", WebFramework.ASTRO),
    ],
)
def test_framework_markers(
    tmp_path: Path, dependency: str, expected: WebFramework
) -> None:
    """Cada framework se reconoce por su marcador, sin necesitar scripts."""
    write_manifest(tmp_path, {"dependencies": {dependency: "1.0.0"}})

    profile = detect_web_project(tmp_path)

    assert profile.framework is expected
    assert f"package.json: dependencia {dependency}" in profile.evidence


def test_astro_with_a_react_integration_is_still_astro(tmp_path: Path) -> None:
    """Astro que usa React no deja de ser Astro: React se comprueba al final."""
    write_manifest(
        tmp_path,
        {"dependencies": {"astro": "5.0.0", "react": "19.0.0"}},
    )

    assert detect_web_project(tmp_path).framework is WebFramework.ASTRO


def test_scripts_without_a_known_framework_are_a_node_project(tmp_path: Path) -> None:
    """Un manifiesto con scripts y sin framework conocido es un proyecto Node."""
    write_manifest(tmp_path, {"name": "herramienta", "scripts": {"build": "tsc"}})

    profile = detect_web_project(tmp_path)

    assert profile.framework is WebFramework.NODE
    assert profile.is_web_project is True
    assert profile.has_typescript is False


def test_a_manifest_without_scripts_or_framework_is_still_unknown(tmp_path: Path) -> None:
    """Un manifiesto sin scripts y sin framework no declara nada que PUNTO sepa ejecutar."""
    write_manifest(tmp_path, {"name": "vacío", "private": True})

    profile = detect_web_project(tmp_path)

    assert profile.framework is WebFramework.UNKNOWN
    assert profile.is_web_project is False


# ---------------------------------------------------------------------------
# Gestor de paquetes y lockfiles
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("lockfile", "expected"),
    [
        ("package-lock.json", PackageManager.NPM),
        ("pnpm-lock.yaml", PackageManager.PNPM),
        ("yarn.lock", PackageManager.YARN),
        ("bun.lockb", PackageManager.BUN),
        ("bun.lock", PackageManager.BUN),
    ],
)
def test_lockfile_detection(
    tmp_path: Path, lockfile: str, expected: PackageManager
) -> None:
    """Cada lockfile reconocido declara su gestor."""
    write_manifest(tmp_path, {"dependencies": {"react": "19.0.0"}})
    write_file(tmp_path, lockfile, "")

    profile = detect_web_project(tmp_path)

    assert profile.package_manager is expected
    assert profile.lockfile == lockfile
    assert f"lockfile: {lockfile}" in profile.evidence


def test_without_a_lockfile_the_manager_is_unknown(tmp_path: Path) -> None:
    """Sin lockfile y sin declaración, PUNTO no inventa un gestor."""
    write_manifest(tmp_path, {"dependencies": {"react": "19.0.0"}})

    profile = detect_web_project(tmp_path)

    assert profile.package_manager is PackageManager.UNKNOWN
    assert profile.lockfile == ""
    assert "sin gestor de paquetes declarado" in profile.evidence


@pytest.mark.parametrize(
    ("declaration", "lockfile", "expected"),
    [
        ("npm@10.8.0", "package-lock.json", PackageManager.NPM),
        ("pnpm@9.1.0", "pnpm-lock.yaml", PackageManager.PNPM),
        ("yarn@4.5.0", "yarn.lock", PackageManager.YARN),
        ("bun@1.1.0", "bun.lock", PackageManager.BUN),
    ],
)
def test_the_package_manager_field_decides_the_manager(
    tmp_path: Path, declaration: str, lockfile: str, expected: PackageManager
) -> None:
    """El campo ``packageManager`` declara el gestor y su lockfile lo respalda."""
    write_manifest(
        tmp_path,
        {"packageManager": declaration, "dependencies": {"next": "15.0.0"}},
    )
    write_file(tmp_path, lockfile, "")

    profile = detect_web_project(tmp_path)

    assert profile.package_manager is expected
    assert profile.lockfile == lockfile
    assert f"package.json: packageManager {declaration}" in profile.evidence


def test_the_package_manager_field_beats_a_lockfile_of_another_manager(
    tmp_path: Path,
) -> None:
    """Con ``packageManager`` y un lockfile de otro gestor, manda la declaración."""
    write_manifest(
        tmp_path,
        {"packageManager": "pnpm@9.1.0", "dependencies": {"next": "15.0.0"}},
    )
    write_file(tmp_path, "package-lock.json", "")

    profile = detect_web_project(tmp_path)

    assert profile.package_manager is PackageManager.PNPM
    assert profile.lockfile == "", "el lockfile declarado no existe: no hay lockfile propio"
    assert "lockfile: package-lock.json" in profile.evidence, "el lockfile suelto no se omite"


def test_a_declared_manager_with_its_own_lockfile_uses_it(tmp_path: Path) -> None:
    """Si el gestor declarado tiene su lockfile, se usa ese y no el de mayor prioridad."""
    write_manifest(
        tmp_path,
        {"packageManager": "yarn@4.5.0", "dependencies": {"next": "15.0.0"}},
    )
    write_file(tmp_path, "pnpm-lock.yaml", "")
    write_file(tmp_path, "yarn.lock", "")

    profile = detect_web_project(tmp_path)

    assert profile.package_manager is PackageManager.YARN
    assert profile.lockfile == "yarn.lock"


@pytest.mark.parametrize(
    ("lockfiles", "expected"),
    [
        (("pnpm-lock.yaml", "package-lock.json"), PackageManager.PNPM),
        (("yarn.lock", "package-lock.json"), PackageManager.YARN),
        (("bun.lock", "package-lock.json"), PackageManager.BUN),
        (("bun.lockb", "bun.lock"), PackageManager.BUN),
    ],
)
def test_several_lockfiles_without_a_declaration_follow_the_documented_priority(
    tmp_path: Path, lockfiles: tuple[str, ...], expected: PackageManager
) -> None:
    """Sin ``packageManager`` gana el lockfile de mayor prioridad, no el primero del disco."""
    write_manifest(tmp_path, {"dependencies": {"next": "15.0.0"}})
    for lockfile in lockfiles:
        write_file(tmp_path, lockfile, "")

    profile = detect_web_project(tmp_path)

    assert profile.package_manager is expected
    assert profile.lockfile in lockfiles


def test_a_lockfile_outside_the_root_is_not_detected(tmp_path: Path) -> None:
    """El gestor se declara por el lockfile de la raíz, no por uno de un subdirectorio."""
    write_manifest(tmp_path, {"dependencies": {"next": "15.0.0"}})
    write_file(tmp_path, "paquetes/package-lock.json", "")

    profile = detect_web_project(tmp_path)

    assert profile.package_manager is PackageManager.UNKNOWN
    assert profile.lockfile == ""


# ---------------------------------------------------------------------------
# TypeScript, Tailwind y Node
# ---------------------------------------------------------------------------
def test_typescript_declared_without_a_config_is_still_typescript(tmp_path: Path) -> None:
    """La dependencia declarada basta; la ruta de la configuración queda vacía."""
    write_manifest(
        tmp_path,
        {"scripts": {"build": "tsc"}, "devDependencies": {"typescript": "5.6.0"}},
    )

    profile = detect_web_project(tmp_path)

    assert profile.has_typescript is True
    assert profile.typescript_config == ""
    assert "package.json: dependencia typescript" in profile.evidence


def test_a_tsconfig_without_the_dependency_is_still_typescript(tmp_path: Path) -> None:
    """El archivo de configuración también demuestra TypeScript."""
    write_manifest(tmp_path, {"scripts": {"build": "node ."}})
    write_file(tmp_path, "tsconfig.json", "{}\n")

    profile = detect_web_project(tmp_path)

    assert profile.has_typescript is True
    assert profile.typescript_config == "tsconfig.json"


@pytest.mark.parametrize(
    "filename",
    [
        "tailwind.config.js",
        "tailwind.config.cjs",
        "tailwind.config.mjs",
        "tailwind.config.ts",
    ],
)
def test_every_tailwind_config_variant_is_detected(tmp_path: Path, filename: str) -> None:
    """Las cuatro variantes documentadas de configuración de Tailwind cuentan."""
    write_manifest(tmp_path, {"scripts": {"dev": "next dev"}})
    write_file(tmp_path, filename, "export default {};\n")

    profile = detect_web_project(tmp_path)

    assert profile.has_tailwind is True
    assert profile.tailwind_config == filename


@pytest.mark.parametrize("directory", ["app", "src", "src/app", "styles"])
@pytest.mark.parametrize(
    ("content", "directive"),
    [
        ('@import "tailwindcss";\n', '@import "tailwindcss"'),
        ("@tailwind base;\n", "@tailwind"),
        ("@import 'tailwindcss/preflight';\n", "@import 'tailwindcss/preflight'"),
    ],
)
def test_tailwind_v4_declared_in_css_is_detected(
    tmp_path: Path, directory: str, content: str, directive: str
) -> None:
    """Tailwind v4 se declara en el CSS y no tiene por qué tener archivo de configuración."""
    write_manifest(
        tmp_path,
        {"scripts": {"dev": "next dev"}, "dependencies": {"next": "15.0.0"}},
    )
    write_file(tmp_path, f"{directory}/globals.css", content)

    profile = detect_web_project(tmp_path)

    assert profile.has_tailwind is True
    assert profile.tailwind_config == ""
    assert f"{directory}/globals.css: directiva {directive}" in profile.evidence


def test_css_without_tailwind_does_not_declare_tailwind(tmp_path: Path) -> None:
    """Una hoja de estilo normal no convierte el proyecto en un proyecto Tailwind."""
    write_manifest(tmp_path, {"scripts": {"dev": "next dev"}})
    write_file(tmp_path, "src/app/globals.css", "body { margin: 0; }\n")

    profile = detect_web_project(tmp_path)

    assert profile.has_tailwind is False
    assert profile.tailwind_config == ""


def test_css_outside_the_scanned_roots_is_not_tailwind(tmp_path: Path) -> None:
    """La búsqueda de Tailwind v4 está acotada a ``app/``, ``src/`` y ``styles/``."""
    write_manifest(tmp_path, {"scripts": {"dev": "next dev"}})
    write_file(tmp_path, "componentes/boton.css", '@import "tailwindcss";\n')

    assert detect_web_project(tmp_path).has_tailwind is False


def test_installed_dependencies_are_not_scanned_for_tailwind(tmp_path: Path) -> None:
    """``node_modules`` no se recorre: es dependencia instalada, no código del proyecto."""
    write_manifest(tmp_path, {"scripts": {"dev": "next dev"}})
    write_file(tmp_path, "src/node_modules/paquete/hoja.css", '@import "tailwindcss";\n')

    assert detect_web_project(tmp_path).has_tailwind is False


def test_the_node_requirement_is_empty_when_it_is_not_declared(tmp_path: Path) -> None:
    """Sin ``engines.node`` el perfil no inventa un rango."""
    write_manifest(tmp_path, {"scripts": {"dev": "next dev"}})

    assert detect_web_project(tmp_path).node_requirement == ""


def test_engines_that_are_not_a_string_are_ignored(tmp_path: Path) -> None:
    """Un ``engines.node`` que no es texto no se propaga como si lo fuera."""
    write_manifest(tmp_path, {"scripts": {"dev": "next dev"}, "engines": {"node": 20}})

    assert detect_web_project(tmp_path).node_requirement == ""


# ---------------------------------------------------------------------------
# Orden, duplicados y recorte de nombres
# ---------------------------------------------------------------------------
def test_declared_names_are_sorted_deduplicated_and_capped(tmp_path: Path) -> None:
    """Los nombres salen ordenados, sin duplicados y recortados al máximo del contrato."""
    scripts = {
        f"tarea-{index:03d}": "node ." for index in reversed(range(MAX_SCRIPT_NAMES + 5))
    }
    dependencies = {
        f"paquete-{index:03d}": "1.0.0" for index in range(MAX_DEPENDENCY_NAMES + 5)
    }
    dependencies["compartida"] = "1.0.0"
    dev_dependencies = {"compartida": "^2.0.0"}
    write_manifest(
        tmp_path,
        {
            "scripts": scripts,
            "dependencies": dependencies,
            "devDependencies": dev_dependencies,
        },
    )

    profile = detect_web_project(tmp_path)

    assert len(profile.script_names) == MAX_SCRIPT_NAMES
    assert profile.script_names == tuple(sorted(profile.script_names))
    assert profile.script_names[0] == "tarea-000"
    assert len(profile.dependency_names) == MAX_DEPENDENCY_NAMES
    assert profile.dependency_names == tuple(sorted(profile.dependency_names))
    assert profile.dependency_names.count("compartida") == 1
    evidence = " ".join(profile.evidence)
    assert f"(recortado de {len(scripts)})" in evidence
    assert f"(recortado de {len(set(dependencies) | set(dev_dependencies))})" in evidence


def test_install_and_build_scripts_are_read_from_the_manifest(tmp_path: Path) -> None:
    """Un proyecto sin dependencias de framework pero con scripts es un proyecto Node."""
    write_manifest(
        tmp_path,
        {
            "scripts": {"build": "node build.js", "test": "node --test"},
            "dependencies": {"astro": "5.0.0"},
        },
    )

    profile = detect_web_project(tmp_path)

    assert profile.script_names == ("build", "test")
    assert profile.has_script("build") is True
    assert profile.has_script("deploy") is False
    assert profile.framework is WebFramework.ASTRO


# ---------------------------------------------------------------------------
# Rutas lógicas y determinismo
# ---------------------------------------------------------------------------
def test_no_field_contains_a_host_path(tmp_path: Path) -> None:
    """Ningún campo del perfil declara una ruta absoluta del host."""
    build_next_project(tmp_path)

    profile = detect_web_project(tmp_path)

    for value in path_fields(profile):
        assert str(tmp_path) not in value
        assert str(tmp_path.resolve()) not in value
        assert Path(value).is_absolute() is False
        assert "\\" not in value, "las rutas lógicas son posix"
    for relative in (
        profile.package_json,
        profile.typescript_config,
        profile.next_config,
        profile.tailwind_config,
    ):
        assert (tmp_path / relative).is_file()


def test_two_detections_of_the_same_workspace_are_equal(tmp_path: Path) -> None:
    """Mismo workspace, mismo perfil, con los campos en el mismo orden."""
    build_next_project(tmp_path)

    first = detect_web_project(tmp_path)
    second = detect_web_project(str(tmp_path))

    assert first == second
    assert first.model_dump() == second.model_dump()
    assert tuple(first.model_dump()) == tuple(second.model_dump())
