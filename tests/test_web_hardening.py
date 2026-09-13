"""Endurecimiento de la capa web tras la auditoría adversarial de ENGINE-5.3.

Cada prueba de este archivo corresponde a un hallazgo concreto del auditor, y comprueba la
**regla**, no el detalle de implementación:

| Hallazgo | Regla |
| --- | --- |
| B1 | el sandbox web solo se ejecuta con el runtime aprobado; no hay sustitución silenciosa |
| B2 | el nombre de un screenshot viene del manifiesto del probe: se sanea antes de tocar disco |
| B3 | sin ninguna comprobación medida no se declara PASS |
| M1 | el manifiesto de evidencia se contrasta con el digest que el probe publicó por stdout |
| M2 | el ``argv[0]`` de los comandos de proyecto sale de una allowlist, sin rutas |
| M3 | el detalle de un fallo se acota y no filtra rutas del host |
| M4 | una nota de recorte sin viewport del contrato no cuenta como medición |
| M6 | la evidencia del informe está acotada, también la que viene del probe |

Ninguna necesita Podman: son reglas del host, y se prueban sin navegador.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from punto.schemas.web import (
    MAX_EXCERPT_CHARS,
    WebCheckKind,
    WebCheckOutcome,
    WebTechnicalStatus,
)
from punto.tools.errors import WebCommandPolicyError
from punto.web.checks import parse_clipping_notes
from punto.web.report import determine_web_status
from punto.web.sandbox import (
    ALLOWED_COMMAND_PROGRAMS,
    EVIDENCE_DIGEST_MARKER,
    WEB_SANDBOX_RUNTIME,
    WebSandboxBackend,
    WebSandboxEvidenceError,
    _normalize_argv_list,
    _safe_relative,
    _sanitize,
    _session_failure_detail,
    _verify_evidence_digest,
)


# ---------------------------------------------------------------------------
# B1: runtime aprobado, sin sustitución silenciosa
# ---------------------------------------------------------------------------
def test_the_web_sandbox_only_runs_with_the_approved_runtime() -> None:
    """El runtime es parte de la frontera: pedir otro no es una preferencia, es un error."""
    assert WEB_SANDBOX_RUNTIME == "podman"

    with pytest.raises(WebCommandPolicyError) as caught:
        WebSandboxBackend(runtime="docker")

    assert "docker" in str(caught.value)
    assert "podman" in str(caught.value)


def test_the_default_runtime_is_the_approved_one() -> None:
    """Sin decir nada, el backend usa el runtime aprobado; nunca «el primero que aparezca»."""
    backend = WebSandboxBackend()

    assert backend.runtime == WEB_SANDBOX_RUNTIME
    assert backend.image.startswith("localhost/punto-sandbox-web")


def test_the_session_event_declares_the_runtime_used() -> None:
    """La auditoría de la sesión deja escrito con qué runtime y con qué imagen se ejecutó."""
    from uuid import uuid4

    from punto.audit.logger import AuditLogger
    from punto.schemas.audit import AuditEventType
    from punto.schemas.web import DEFAULT_VIEWPORTS

    audit = AuditLogger()
    backend = WebSandboxBackend(audit=audit)
    backend._audit_session_started(
        task_id=uuid4(),
        project_id=uuid4(),
        route="/",
        viewports=DEFAULT_VIEWPORTS,
    )

    events = audit.by_type(AuditEventType.BROWSER_SESSION_STARTED)
    assert len(events) == 1
    metadata = dict(events[0].metadata)
    assert metadata["sandbox_image"].startswith(f"{WEB_SANDBOX_RUNTIME}:")
    assert metadata["viewports"] == ("MOBILE", "TABLET", "DESKTOP")


def test_without_task_and_project_the_session_is_not_audited() -> None:
    """Sin identificadores no se inventa un evento: se omite, que es lo honesto."""
    from punto.audit.logger import AuditLogger
    from punto.schemas.audit import AuditEventType
    from punto.schemas.web import DEFAULT_VIEWPORTS

    audit = AuditLogger()
    backend = WebSandboxBackend(audit=audit)
    backend._audit_session_started(
        task_id=None, project_id=None, route="/", viewports=DEFAULT_VIEWPORTS
    )

    assert audit.by_type(AuditEventType.BROWSER_SESSION_STARTED) == ()


# ---------------------------------------------------------------------------
# B2: nombres de screenshot saneados
# ---------------------------------------------------------------------------
def test_a_screenshot_name_cannot_escape_the_capture_folder(tmp_path: Path) -> None:
    """El nombre viene del manifiesto: se exige forma de archivo simple, no una ruta."""
    from punto.web.sandbox import SCREENSHOT_NAME_PATTERN

    for bad in (
        "../../../Windows/win.ini",
        "C:/Windows/win.ini",
        "/etc/passwd",
        "sub/dir/ok.png",
        "sub\\dir\\ok.png",
        ".oculto.png",
        "sin-extension",
        "vacio.png.exe",
    ):
        assert not SCREENSHOT_NAME_PATTERN.match(bad), bad

    for good in ("home-mobile.png", "index-desktop.png", "a_b-c.1.png", "captura.PNG.png"):
        assert SCREENSHOT_NAME_PATTERN.match(good), good


# ---------------------------------------------------------------------------
# B3: sin medición no hay PASS
# ---------------------------------------------------------------------------
def test_no_measured_check_is_blocked_not_pass() -> None:
    """Con las once comprobaciones sin señal, el estado es BLOCKED: no se afirma lo no medido."""
    checks = tuple(
        WebCheckOutcome(kind=kind, ran=False, passed=True, detail="sin señal")
        for kind in WebCheckKind
    )

    status, reasons = determine_web_status(checks)

    assert status is WebTechnicalStatus.BLOCKED
    assert "ninguna" in reasons[0]
    assert "PAGE_LOAD_ERROR" in reasons[0]


def test_one_measured_check_is_enough_to_have_a_verdict() -> None:
    """Basta con que una comprobación se haya medido para poder dar veredicto."""
    checks = (
        WebCheckOutcome(kind=WebCheckKind.PAGE_LOAD_ERROR, ran=True, passed=True),
        WebCheckOutcome(kind=WebCheckKind.CONSOLE_ERROR, ran=False, passed=True),
    )

    status, reasons = determine_web_status(checks)

    assert status is WebTechnicalStatus.PASS
    assert any("CONSOLE_ERROR" in reason for reason in reasons)


def test_an_empty_observations_contract_cannot_be_a_pass() -> None:
    """El extremo real del hallazgo: contrato vacío no puede acabar en PASS."""
    from punto.schemas.web import WebObservations
    from punto.web.checks import evaluate_web_checks

    checks, findings = evaluate_web_checks(WebObservations())

    status, _ = determine_web_status(checks)

    assert findings == ()
    assert not any(check.ran for check in checks)
    assert status is WebTechnicalStatus.BLOCKED


# ---------------------------------------------------------------------------
# M1: digest de la evidencia
# ---------------------------------------------------------------------------
def test_the_evidence_digest_is_verified_against_the_published_one(tmp_path: Path) -> None:
    """El manifiesto que el host lee tiene que ser el que el probe cerró."""
    manifest = tmp_path / "diagnostics.json"
    manifest.write_text('{"probe": "run_web_session"}\n', encoding="utf-8")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    stdout = f"runtime node=v24\n{EVIDENCE_DIGEST_MARKER} {digest}\n"

    _verify_evidence_digest(stdout, manifest)  # no lanza


def test_a_rewritten_manifest_is_detected(tmp_path: Path) -> None:
    """Reescribir el manifiesto después del probe se detecta y bloquea la sesión."""
    manifest = tmp_path / "diagnostics.json"
    manifest.write_text('{"probe": "run_web_session"}\n', encoding="utf-8")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    manifest.write_text('{"probe": "otro", "screenshots": []}\n', encoding="utf-8")

    with pytest.raises(WebSandboxEvidenceError) as caught:
        _verify_evidence_digest(f"{EVIDENCE_DIGEST_MARKER} {digest}\n", manifest)

    assert "cambió" in str(caught.value)


def test_a_probe_that_publishes_no_digest_blocks(tmp_path: Path) -> None:
    """Sin digest publicado no se puede comprobar la autenticidad: se bloquea, no se confía."""
    manifest = tmp_path / "diagnostics.json"
    manifest.write_text("{}\n", encoding="utf-8")

    with pytest.raises(WebSandboxEvidenceError) as caught:
        _verify_evidence_digest("probe: sin digest\n", manifest)

    assert EVIDENCE_DIGEST_MARKER in str(caught.value)


# ---------------------------------------------------------------------------
# M2: allowlist de programas
# ---------------------------------------------------------------------------
def test_a_program_outside_the_allowlist_is_rejected() -> None:
    """Un binario arbitrario no entra por la puerta del sandbox, aunque venga en una lista."""
    with pytest.raises(WebCommandPolicyError) as caught:
        _normalize_argv_list([["curl", "http://ejemplo"]], field="commands")

    assert "curl" in str(caught.value)
    assert "allowlist" in str(caught.value)


def test_a_program_with_a_path_is_rejected() -> None:
    """Tampoco se acepta un programa por ruta: la allowlist es por nombre."""
    for argv in (("/usr/bin/node", "-v"), ("..\\node.exe", "-v"), ("./node", "-v")):
        with pytest.raises(WebCommandPolicyError):
            _normalize_argv_list([argv], field="preview_argv")


def test_the_allowlist_covers_the_commands_the_plans_produce() -> None:
    """Todo lo que un plan de ENGINE-5.3 puede producir está dentro de la allowlist."""
    allowed = {
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "bun",
        "node",
        "python3",
    }

    assert allowed <= ALLOWED_COMMAND_PROGRAMS


def test_a_valid_command_list_still_passes() -> None:
    """La allowlist no rompe el camino bueno."""
    commands = _normalize_argv_list(
        (("npm", "run", "build"), ("python3", "-m", "http.server", "4173")), field="commands"
    )

    assert commands == (
        ("npm", "run", "build"),
        ("python3", "-m", "http.server", "4173"),
    )
    assert _normalize_argv_list((), field="commands") == ()


# ---------------------------------------------------------------------------
# M3: rutas del host y textos acotados
# ---------------------------------------------------------------------------
def test_the_failure_detail_never_leaks_the_host_workspace(tmp_path: Path) -> None:
    """El error del runtime trae rutas del host: se sustituyen antes de informar."""
    completed = subprocess.CompletedProcess(
        args=["podman"], returncode=125, stdout="", stderr=""
    )
    diagnostics = {"error": f"statfs {tmp_path}\\site: no such file or directory"}

    detail = _session_failure_detail(completed, diagnostics, workspace=tmp_path)

    assert str(tmp_path) not in detail
    assert "/workspace" in detail
    assert "exit=125" in detail


def test_the_failure_detail_is_bounded() -> None:
    """Un diagnóstico enorme no viaja entero al informe (y 'exit' no cuenta como relleno)."""
    completed = subprocess.CompletedProcess(args=["podman"], returncode=1, stdout="", stderr="")
    diagnostics = {"error": "z" * 100_000}

    detail = _session_failure_detail(completed, diagnostics)

    assert len(detail) < MAX_EXCERPT_CHARS + 500
    assert detail.count("z") <= MAX_EXCERPT_CHARS
    assert "z" * (MAX_EXCERPT_CHARS + 1) not in detail


def test_sanitize_bounds_even_without_a_workspace() -> None:
    """El acotado no depende de conocer el workspace."""
    assert _sanitize("y" * 100_000).count("y") <= MAX_EXCERPT_CHARS


# ---------------------------------------------------------------------------
# M4: una nota de recorte sin viewport no es una medición
# ---------------------------------------------------------------------------
def test_an_empty_clipping_note_is_not_a_measurement() -> None:
    """Un objeto vacío no mide nada: aceptarlo haría afirmar una medición inexistente."""
    assert parse_clipping_notes(["viewport_clipping {}"]) == ()
    assert parse_clipping_notes(['viewport_clipping {"viewport": "NO-EXISTE"}']) == ()
    assert parse_clipping_notes(['viewport_clipping {"elements": ["div"]}']) == ()


def test_a_real_clipping_note_is_a_measurement() -> None:
    """La nota que emite el probe sí mide, incluso cuando no encuentra recorte."""
    clean = parse_clipping_notes(['viewport_clipping {"route": "/", "viewport": "MOBILE"}'])
    dirty = parse_clipping_notes(
        ['viewport_clipping {"route": "/", "viewport": "TABLET", "elements": ["div.x"]}']
    )

    assert len(clean) == 1 and clean[0].elements == ()
    assert len(dirty) == 1 and dirty[0].elements == ("div.x",)
    assert dirty[0].viewport is not None


# ---------------------------------------------------------------------------
# _safe_relative: rutas absolutas de cualquier plataforma
# ---------------------------------------------------------------------------
def test_absolute_paths_are_rejected_on_every_platform() -> None:
    """En Windows ``/etc`` no es «absoluta» para pathlib: se rechaza explícitamente."""
    for bad in ("/etc", "C:/Windows", "c:\\Windows", "..", "../fuera", "site/../../fuera"):
        with pytest.raises(ValueError):
            _safe_relative(bad, field="project_relative")

    assert _safe_relative("site", field="project_relative") == Path("site")
    assert _safe_relative("apps/web", field="project_relative") == Path("apps/web")


# ---------------------------------------------------------------------------
# Auditoría del perfil detectado
# ---------------------------------------------------------------------------
def test_the_detected_profile_is_audited(tmp_path: Path) -> None:
    """El perfil detectado deja evento, con su evidencia como metadato."""
    import json
    from uuid import uuid4

    from punto.audit.logger import AuditLogger
    from punto.schemas.audit import AuditEventType
    from punto.web.detection import detect_web_project

    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"next": "16.0.0", "react": "19.0.0"}}), encoding="utf-8"
    )
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    audit = AuditLogger()

    profile = detect_web_project(
        tmp_path, audit=audit, task_id=uuid4(), project_id=uuid4()
    )

    assert profile.framework.value == "NEXTJS"
    events = audit.by_type(AuditEventType.WEB_PROFILE_DETECTED)
    assert len(events) == 1
    metadata = dict(events[0].metadata)
    assert metadata["framework"] == "NEXTJS"
    assert metadata["package_manager"] == "NPM"
    assert metadata["evidence"]


def test_without_identifiers_the_profile_is_not_audited(tmp_path: Path) -> None:
    """Sin tarea ni proyecto no se inventa un evento de auditoría."""
    from punto.audit.logger import AuditLogger
    from punto.schemas.audit import AuditEventType
    from punto.web.detection import detect_web_project

    audit = AuditLogger()

    detect_web_project(tmp_path, audit=audit)

    assert audit.by_type(AuditEventType.WEB_PROFILE_DETECTED) == ()
