"""Pruebas de la política de comandos web (ENGINE-5.3).

La política se prueba sobre perfiles construidos a mano: es una traducción de acción a ``argv``,
así que no necesita disco, ni red, ni npm. Lo que estas pruebas vigilan es que el ``argv`` sea
exacto, que una capacidad que falta sea un error de política y no un comando improvisado, y que
ningún plan pueda convertirse en una orden de shell.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from punto.schemas.web import (
    PackageManager,
    WebCommandKind,
    WebFramework,
    WebProjectProfile,
)
from punto.web.commands import (
    WebCommandPlan,
    WebCommandPolicyError,
    available_commands,
    plan_command,
)


def build_profile(**overrides: object) -> WebProjectProfile:
    """Perfil sintético con todas las capacidades, para probar la política sin tocar disco."""
    base: dict[str, object] = {
        "framework": WebFramework.NEXTJS,
        "package_manager": PackageManager.NPM,
        "lockfile": "package-lock.json",
        "package_json": "package.json",
        "script_names": ("build", "start", "test"),
        "dependency_names": ("@playwright/test", "next", "typescript"),
        "has_typescript": True,
        "evidence": ("package.json presente",),
    }
    base.update(overrides)
    return WebProjectProfile(**base)


def minimal_profile() -> WebProjectProfile:
    """Proyecto sin scripts, sin TypeScript y sin Playwright: casi nada es planificable."""
    return build_profile(
        framework=WebFramework.NODE,
        package_manager=PackageManager.UNKNOWN,
        lockfile="",
        script_names=(),
        dependency_names=("react",),
        has_typescript=False,
    )


# ---------------------------------------------------------------------------
# INSTALL_DEPENDENCIES
# ---------------------------------------------------------------------------
def test_install_uses_npm_ci_with_the_npm_lockfile() -> None:
    """Con ``package-lock.json`` la instalación es reproducible: ``npm ci``."""
    plan = plan_command(WebCommandKind.INSTALL_DEPENDENCIES, build_profile())

    assert plan.kind is WebCommandKind.INSTALL_DEPENDENCIES
    assert plan.argv == ("npm", "ci")
    assert "package-lock.json" in plan.detail


def test_install_falls_back_to_npm_install_without_the_npm_lockfile() -> None:
    """Sin ``package-lock.json`` no hay instalación congelada que ofrecer."""
    plan = plan_command(
        WebCommandKind.INSTALL_DEPENDENCIES,
        build_profile(package_manager=PackageManager.UNKNOWN, lockfile=""),
    )

    assert plan.argv == ("npm", "install")


@pytest.mark.parametrize(
    ("manager", "executable"),
    [
        (PackageManager.PNPM, "pnpm"),
        (PackageManager.YARN, "yarn"),
        (PackageManager.BUN, "bun"),
    ],
)
def test_install_freezes_the_lockfile_of_every_other_manager(
    manager: PackageManager, executable: str
) -> None:
    """pnpm, yarn y bun instalan con su lockfile congelado."""
    plan = plan_command(
        WebCommandKind.INSTALL_DEPENDENCIES, build_profile(package_manager=manager, lockfile="")
    )

    assert plan.argv == (executable, "install", "--frozen-lockfile")


def test_no_install_plan_asks_for_network() -> None:
    """La red la decide el backend del sandbox: el plan no puede pedirla."""
    for manager in PackageManager:
        argv = plan_command(
            WebCommandKind.INSTALL_DEPENDENCIES,
            build_profile(package_manager=manager, lockfile=""),
        ).argv
        assert not any("network" in item for item in argv)
        assert not any(item.startswith("--net") for item in argv)


# ---------------------------------------------------------------------------
# TYPECHECK
# ---------------------------------------------------------------------------
def test_typecheck_uses_the_installed_tsc_without_installing_anything() -> None:
    """Con TypeScript declarado, el ``argv`` es exacto y no descarga nada."""
    plan = plan_command(WebCommandKind.TYPECHECK, build_profile())

    assert plan.argv == ("npx", "--no-install", "tsc", "--noEmit")


def test_typecheck_without_typescript_is_a_policy_error() -> None:
    """Sin TypeScript configurado, comprobar tipos no aplica."""
    with pytest.raises(WebCommandPolicyError, match="typescript"):
        plan_command(WebCommandKind.TYPECHECK, minimal_profile())


# ---------------------------------------------------------------------------
# BUILD, START_PREVIEW y RUN_TESTS
# ---------------------------------------------------------------------------
def test_npm_plans_every_available_action_with_an_exact_argv() -> None:
    """El ``argv`` exacto de cada acción, para el gestor detectado."""
    current = build_profile()

    assert plan_command(WebCommandKind.BUILD, current).argv == ("npm", "run", "build")
    assert plan_command(WebCommandKind.START_PREVIEW, current).argv == ("npm", "run", "start")
    assert plan_command(WebCommandKind.RUN_TESTS, current).argv == ("npm", "run", "test")
    assert plan_command(WebCommandKind.RUN_PLAYWRIGHT, current).argv == (
        "npx",
        "--no-install",
        "playwright",
        "test",
    )
    assert plan_command(WebCommandKind.CAPTURE_SCREENSHOT, current).argv == ()


@pytest.mark.parametrize(
    ("manager", "executable"),
    [
        (PackageManager.PNPM, "pnpm"),
        (PackageManager.YARN, "yarn"),
        (PackageManager.BUN, "bun"),
    ],
)
def test_every_script_action_uses_the_detected_manager(
    manager: PackageManager, executable: str
) -> None:
    """Los scripts del proyecto se lanzan con el gestor detectado, no siempre con npm."""
    current = build_profile(package_manager=manager, lockfile="")

    assert plan_command(WebCommandKind.BUILD, current).argv == (executable, "run", "build")
    assert plan_command(WebCommandKind.START_PREVIEW, current).argv == (
        executable,
        "run",
        "start",
    )
    assert plan_command(WebCommandKind.RUN_TESTS, current).argv == (executable, "run", "test")


@pytest.mark.parametrize("manager", list(PackageManager))
def test_the_actions_that_do_not_go_through_the_manager_are_the_same_for_all_of_them(
    manager: PackageManager,
) -> None:
    """``npx --no-install`` usa el binario ya instalado, así que no depende del gestor."""
    current = build_profile(package_manager=manager, lockfile="")

    assert plan_command(WebCommandKind.TYPECHECK, current).argv == (
        "npx",
        "--no-install",
        "tsc",
        "--noEmit",
    )
    assert plan_command(WebCommandKind.RUN_PLAYWRIGHT, current).argv == (
        "npx",
        "--no-install",
        "playwright",
        "test",
    )
    assert plan_command(WebCommandKind.CAPTURE_SCREENSHOT, current).argv == ()


def test_build_without_the_build_script_is_a_policy_error() -> None:
    """Sin script ``build`` no hay construcción que planificar."""
    with pytest.raises(WebCommandPolicyError, match="build"):
        plan_command(WebCommandKind.BUILD, build_profile(script_names=("start",)))


def test_run_tests_without_the_test_script_is_a_policy_error() -> None:
    """Sin script ``test`` no hay pruebas que planificar."""
    with pytest.raises(WebCommandPolicyError, match="test"):
        plan_command(WebCommandKind.RUN_TESTS, build_profile(script_names=("build",)))


def test_start_preview_prefers_the_start_script() -> None:
    """Con ``start`` y ``preview``, gana ``start``."""
    current = build_profile(script_names=("build", "preview", "start"))

    assert plan_command(WebCommandKind.START_PREVIEW, current).argv == ("npm", "run", "start")


def test_start_preview_falls_back_to_the_preview_script() -> None:
    """Sin ``start``, ``preview`` sirve para la vista previa."""
    current = build_profile(script_names=("build", "preview"))

    assert plan_command(WebCommandKind.START_PREVIEW, current).argv == ("npm", "run", "preview")


def test_start_preview_without_scripts_is_a_policy_error() -> None:
    """Sin ``start`` ni ``preview`` no hay servidor que arrancar."""
    with pytest.raises(WebCommandPolicyError, match="START_PREVIEW"):
        plan_command(WebCommandKind.START_PREVIEW, minimal_profile())


def test_script_names_are_fixed_by_policy_not_taken_from_the_project() -> None:
    """Aunque el proyecto declare un nombre hostil, el ``argv`` solo lleva nombres fijos."""
    current = build_profile(script_names=("build", "start", "start; rm -rf /"))

    build = plan_command(WebCommandKind.BUILD, current)
    preview = plan_command(WebCommandKind.START_PREVIEW, current)

    assert build.argv == ("npm", "run", "build")
    assert preview.argv == ("npm", "run", "start")
    assert all(";" not in item for item in (*build.argv, *preview.argv))


# ---------------------------------------------------------------------------
# RUN_PLAYWRIGHT
# ---------------------------------------------------------------------------
def test_run_playwright_requires_the_declared_dependency() -> None:
    """Sin ``@playwright/test`` declarado, PUNTO no lanza un Playwright que no existe."""
    with pytest.raises(WebCommandPolicyError, match="@playwright/test"):
        plan_command(WebCommandKind.RUN_PLAYWRIGHT, minimal_profile())


# ---------------------------------------------------------------------------
# CAPTURE_SCREENSHOT
# ---------------------------------------------------------------------------
def test_capture_screenshot_is_not_a_project_command() -> None:
    """La captura es una acción del probe de PUNTO: ``argv`` vacío y motivo explícito."""
    plan = plan_command(
        WebCommandKind.CAPTURE_SCREENSHOT, build_profile(), route="/dashboard"
    )

    assert plan.argv == (), "una captura no se ejecuta como comando del proyecto"
    assert plan.kind is WebCommandKind.CAPTURE_SCREENSHOT
    assert "probe" in plan.detail
    assert "/dashboard" in plan.detail


def test_capture_screenshot_sanitizes_the_route() -> None:
    """La ruta acaba en evidencia: un control no puede inyectarse en el ``detail``."""
    plan = plan_command(
        WebCommandKind.CAPTURE_SCREENSHOT, build_profile(), route="/a\nb\x00"
    )

    assert "\n" not in plan.detail
    assert "\x00" not in plan.detail
    assert "\\x0a" in plan.detail


def test_a_route_is_ignored_by_the_commands_that_are_not_captures() -> None:
    """Solo la captura usa la ruta; el resto de acciones la ignoran."""
    plan = plan_command(WebCommandKind.BUILD, build_profile(), route="/dashboard")

    assert plan.argv == ("npm", "run", "build")
    assert "/dashboard" not in plan.detail


# ---------------------------------------------------------------------------
# Disponibilidad, forma del plan y errores
# ---------------------------------------------------------------------------
def test_available_commands_follows_the_enum_order() -> None:
    """Con todas las capacidades, la lista es el enum completo y en su orden."""
    kinds = available_commands(build_profile())

    assert kinds == tuple(WebCommandKind)


def test_available_commands_for_a_minimal_profile() -> None:
    """Instalar y capturar siempre se pueden planificar; el resto exige capacidad."""
    kinds = available_commands(minimal_profile())

    assert kinds == (WebCommandKind.INSTALL_DEPENDENCIES, WebCommandKind.CAPTURE_SCREENSHOT)


def test_available_commands_are_exactly_the_plannable_ones() -> None:
    """La lista de disponibles no puede divergir de lo que ``plan_command`` acepta."""
    for current in (build_profile(), minimal_profile()):
        kinds = available_commands(current)
        for kind in WebCommandKind:
            if kind in kinds:
                assert plan_command(kind, current).kind is kind
            else:
                with pytest.raises(WebCommandPolicyError):
                    plan_command(kind, current)


def test_no_plan_contains_a_shell_or_a_free_command() -> None:
    """Ningún ``argv`` pide un shell, un ``-c`` ni una ruta absoluta del host."""
    current = build_profile()
    for kind in WebCommandKind:
        try:
            plan = plan_command(kind, current)
        except WebCommandPolicyError:
            continue
        assert isinstance(plan.argv, tuple), "argv es una tupla, nunca una cadena"
        assert all(isinstance(item, str) for item in plan.argv)
        assert not any(item in {"sh", "bash", "cmd", "powershell"} for item in plan.argv)
        assert not any(item == "-c" or item.startswith("--command") for item in plan.argv)
        assert not any(item.startswith("/") for item in plan.argv)
        assert plan.detail, "cada plan explica su decisión"


def test_an_unknown_action_is_a_policy_error() -> None:
    """Una acción que no existe en el contrato se rechaza, no se improvisa."""
    with pytest.raises(WebCommandPolicyError, match="desconocida"):
        plan_command("DEPLOY", build_profile())


def test_the_plan_is_immutable() -> None:
    """Un plan decidido no se reescribe por el camino."""
    plan = plan_command(WebCommandKind.BUILD, build_profile())

    assert isinstance(plan, WebCommandPlan)
    with pytest.raises(FrozenInstanceError):
        plan.detail = "otra cosa"  # type: ignore[misc]
