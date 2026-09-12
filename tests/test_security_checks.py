"""Pruebas de los checks deterministas de seguridad (ENGINE-5 §8 a §10).

Son análisis implementados por PUNTO: inspeccionan datos y no ejecutan código del
proyecto. Estas pruebas fijan tres cosas: qué detectan, qué **no** deben detectar (los
falsos positivos obvios) y que su resultado sea determinista.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine5_support import (
    CANARY_FILE,
    CORRECTED_RUNNER,
    LEAKY_FILE,
    VULNERABLE_RUNNER,
    build_security_project,
)
from punto.security.checks import (
    DEFAULT_SECURITY_REGISTRY,
    RegisteredSecurityCheck,
    SecurityCheckKind,
    SecurityCheckRegistry,
)
from punto.security.deterministic import (
    SecurityCheckContext,
    dangerous_path_scan,
    dependency_manifest_inspection,
    python_ast_security,
    secret_pattern_scan,
)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def context(tmp_path: Path, files: dict[str, str], paths: tuple[str, ...] | None = None):
    """Contexto de check sobre un proyecto sintético."""
    workspace = build_security_project(tmp_path, files)
    return SecurityCheckContext(workspace=workspace, paths=paths or tuple(files))


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------
def test_registry_declares_deterministic_and_scanner_checks() -> None:
    """El registro separa lo que PUNTO implementa de lo que no tiene."""
    assert DEFAULT_SECURITY_REGISTRY.deterministic_names() == (
        "secret-pattern-scan",
        "dangerous-path-scan",
        "python-ast-security",
        "dependency-manifest-inspection",
    )
    assert "bandit" in DEFAULT_SECURITY_REGISTRY.unavailable_names()
    assert "semgrep" in DEFAULT_SECURITY_REGISTRY.unavailable_names()


def test_scanner_checks_are_registered_but_not_available() -> None:
    """Ningún scanner industrial se declara disponible por suposición."""
    for name in ("bandit", "semgrep", "trivy", "npm-audit", "osv-scanner"):
        check = DEFAULT_SECURITY_REGISTRY.require(name)
        assert check.kind is SecurityCheckKind.SCANNER
        assert check.available is False
        assert check.requires


def test_unavailable_check_cannot_be_run() -> None:
    """Ejecutar un check no disponible es un error, no una simulación."""
    ctx = SecurityCheckContext(workspace=Path("."), paths=())

    with pytest.raises(ValueError, match="no está disponible"):
        DEFAULT_SECURITY_REGISTRY.run("bandit", ctx)


def test_unknown_check_does_not_exist() -> None:
    """No hay forma de colar un comando: el registro es cerrado."""
    assert not DEFAULT_SECURITY_REGISTRY.exists("bash -c 'rm -rf /'")
    assert not DEFAULT_SECURITY_REGISTRY.exists("curl")


def test_deterministic_check_flag() -> None:
    """Un check determinista se identifica como tal."""
    check: RegisteredSecurityCheck = DEFAULT_SECURITY_REGISTRY.require("python-ast-security")

    assert check.deterministic is True
    assert check.runner is not None


def test_registry_is_injectable() -> None:
    """El registro se puede sustituir, lo que permite probar escenarios acotados."""
    empty = SecurityCheckRegistry(checks=())

    assert empty.names() == ()
    assert empty.available_names() == ()


# ---------------------------------------------------------------------------
# secret-pattern-scan
# ---------------------------------------------------------------------------
def test_embedded_api_key_is_detected(tmp_path: Path) -> None:
    """Una clave con forma real se detecta y no se copia entera a la evidencia."""
    result = secret_pattern_scan(context(tmp_path, {"config.py": LEAKY_FILE}))

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.severity.value == "HIGH"
    assert finding.category.value == "SECRETS"
    assert finding.file == "config.py"
    assert "sk-live-9f8e7d6c5b4a3210fedcba9876543210" not in finding.evidence
    assert "recortado" in finding.evidence


def test_private_key_block_is_critical(tmp_path: Path) -> None:
    """Un bloque de clave privada es CRITICAL."""
    content = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----\n"

    result = secret_pattern_scan(context(tmp_path, {"key.pem.txt": content}))

    assert result.findings[0].severity.value == "CRITICAL"


def test_credential_url_is_detected(tmp_path: Path) -> None:
    """Una URL con usuario y contraseña se detecta."""
    content = 'DATABASE_URL = "postgres://admin:s3cretpass@db.internal:5432/app"\n'

    result = secret_pattern_scan(context(tmp_path, {"settings.py": content}))

    assert result.findings
    assert result.findings[0].category.value == "SECRETS"


def test_test_canary_is_not_a_finding(tmp_path: Path) -> None:
    """§9: un canario de prueba explícito no es un hallazgo.

    Marcarlo bloquearía el motor por su propio material de pruebas.
    """
    result = secret_pattern_scan(context(tmp_path, {"tests/test_x.py": CANARY_FILE}))

    assert result.findings == ()
    assert result.scanned == ("tests/test_x.py",)


def test_env_example_placeholder_is_not_a_finding(tmp_path: Path) -> None:
    """§9: un placeholder de `.env.example` no es un hallazgo."""
    content = "# DEEPSEEK_API_KEY=\nDEEPSEEK_API_KEY=your-key-here\nAPI_KEY=<pon-tu-clave>\n"

    result = secret_pattern_scan(context(tmp_path, {".env.example": content}))

    assert result.findings == ()


def test_documentation_example_is_not_a_finding(tmp_path: Path) -> None:
    """§9: un ejemplo de documentación con marcador tampoco."""
    content = "Ejemplo: export API_KEY=example-key-for-docs-only-1234\n"

    result = secret_pattern_scan(context(tmp_path, {"README.md": content}))

    assert result.findings == ()


def test_environment_lookup_is_not_a_finding(tmp_path: Path) -> None:
    """Leer la credencial del entorno es la práctica correcta, no un hallazgo."""
    content = 'API_KEY = os.environ["API_KEY"]\n'

    result = secret_pattern_scan(context(tmp_path, {"config.py": content}))

    assert result.findings == ()


def test_secret_scan_does_not_delete_anything(tmp_path: Path) -> None:
    """§9: reporta el hallazgo; no elimina ni modifica el archivo."""
    workspace = build_security_project(tmp_path, {"config.py": LEAKY_FILE})
    ctx = SecurityCheckContext(workspace=workspace, paths=("config.py",))

    secret_pattern_scan(ctx)

    assert (workspace / "config.py").read_text(encoding="utf-8") == LEAKY_FILE


def test_secret_scan_is_deterministic(tmp_path: Path) -> None:
    """Mismo contenido, mismos hallazgos, en el mismo orden."""
    ctx = context(tmp_path, {"config.py": LEAKY_FILE, "settings.py": LEAKY_FILE})

    first = secret_pattern_scan(ctx)
    second = secret_pattern_scan(ctx)

    assert first == second


# ---------------------------------------------------------------------------
# python-ast-security
# ---------------------------------------------------------------------------
def test_shell_true_is_detected_as_high(tmp_path: Path) -> None:
    """§15: ``shell=True`` con entrada de usuario es HIGH."""
    result = python_ast_security(context(tmp_path, {"runner.py": VULNERABLE_RUNNER}))

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.severity.value == "HIGH"
    assert finding.category.value == "INJECTION"
    assert finding.file == "runner.py"
    assert finding.line == 5
    assert "shell" in finding.evidence.lower()


def test_corrected_subprocess_has_no_finding(tmp_path: Path) -> None:
    """La versión con argumentos estructurados y shell=False no genera hallazgo."""
    result = python_ast_security(context(tmp_path, {"runner.py": CORRECTED_RUNNER}))

    assert result.findings == ()
    assert result.scanned == ("runner.py",)


@pytest.mark.parametrize(
    ("snippet", "expected_severity"),
    [
        ("eval(user_input)\n", "HIGH"),
        ("exec(payload)\n", "HIGH"),
        ("os.system(cmd)\n", "HIGH"),
        ("pickle.loads(blob)\n", "HIGH"),
        ("os.popen(cmd)\n", "MEDIUM"),
    ],
)
def test_dangerous_calls_are_detected(
    tmp_path: Path, snippet: str, expected_severity: str
) -> None:
    """Las llamadas declaradas se detectan con su gravedad conservadora."""
    imports = "import os\nimport pickle\n\n\n"
    result = python_ast_security(context(tmp_path, {"app.py": imports + snippet}))

    assert result.findings
    assert result.findings[0].severity.value == expected_severity


def test_yaml_unsafe_load_is_detected(tmp_path: Path) -> None:
    """``yaml.load`` sin loader seguro es un hallazgo MEDIUM."""
    content = "import yaml\n\n\ndef parse(text: str) -> object:\n    return yaml.load(text)\n"

    result = python_ast_security(context(tmp_path, {"app.py": content}))

    assert result.findings
    assert result.findings[0].severity.value == "MEDIUM"


def test_yaml_safe_load_is_not_a_finding(tmp_path: Path) -> None:
    """Con ``SafeLoader`` no hay hallazgo."""
    content = (
        "import yaml\n\n\n"
        "def parse(text: str) -> object:\n"
        "    return yaml.load(text, Loader=yaml.SafeLoader)\n"
    )

    result = python_ast_security(context(tmp_path, {"app.py": content}))

    assert result.findings == ()


def test_non_python_files_are_skipped(tmp_path: Path) -> None:
    """El análisis sintáctico de Python no opina sobre otros lenguajes."""
    result = python_ast_security(context(tmp_path, {"app.js": "eval(userInput);\n"}))

    assert result.findings == ()
    assert result.scanned == ()


def test_file_that_does_not_compile_is_skipped_with_a_note(tmp_path: Path) -> None:
    """Un archivo que no compila no se analiza, y se deja constancia."""
    result = python_ast_security(context(tmp_path, {"broken.py": "def f(:\n    pass\n"}))

    assert result.findings == ()
    assert result.notes
    assert "no compila" in result.notes[0]


def test_ast_check_is_deterministic(tmp_path: Path) -> None:
    """Mismo archivo, mismo hallazgo."""
    ctx = context(tmp_path, {"runner.py": VULNERABLE_RUNNER})

    assert python_ast_security(ctx) == python_ast_security(ctx)


# ---------------------------------------------------------------------------
# dangerous-path-scan
# ---------------------------------------------------------------------------
def test_system_secret_path_is_detected(tmp_path: Path) -> None:
    """Referenciar ``/etc/shadow`` es un hallazgo HIGH."""
    content = 'SOURCE = "/etc/shadow"\n'

    result = dangerous_path_scan(context(tmp_path, {"config.py": content}))

    assert result.findings
    assert result.findings[0].severity.value == "HIGH"
    assert result.findings[0].category.value == "FILESYSTEM"


def test_world_writable_permission_is_detected(tmp_path: Path) -> None:
    """``chmod 777`` es un hallazgo MEDIUM."""
    result = dangerous_path_scan(context(tmp_path, {"deploy.sh": "chmod 777 /var/app\n"}))

    assert result.findings
    assert result.findings[0].severity.value == "MEDIUM"


def test_clean_file_has_no_path_findings(tmp_path: Path) -> None:
    """Un archivo sin rutas del sistema no genera hallazgos."""
    result = dangerous_path_scan(context(tmp_path, {"app.py": "PATH = 'data/input.csv'\n"}))

    assert result.findings == ()


# ---------------------------------------------------------------------------
# dependency-manifest-inspection
# ---------------------------------------------------------------------------
def test_unpinned_dependency_is_detected(tmp_path: Path) -> None:
    """Una dependencia sin versión fijada es un hallazgo LOW de cadena de suministro."""
    content = "requests\nflask==3.0.0\n# comentario\n"

    result = dependency_manifest_inspection(context(tmp_path, {"requirements.txt": content}))

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.severity.value == "LOW"
    assert finding.category.value == "SUPPLY_CHAIN"
    assert "requests" in finding.title


def test_pinned_dependencies_have_no_findings(tmp_path: Path) -> None:
    """Con todas las versiones fijadas no hay hallazgo."""
    content = "requests==2.32.3\nflask==3.0.0\n"

    result = dependency_manifest_inspection(context(tmp_path, {"requirements.txt": content}))

    assert result.findings == ()


def test_manifest_inspection_does_not_use_the_network(tmp_path: Path) -> None:
    """El check inspecciona datos: no consulta registros externos.

    Es la razón por la que puede correr en proceso confiable y por la que **no** se
    presenta como un análisis de vulnerabilidades de dependencias.
    """
    content = "flask==3.0.0\n"
    result = dependency_manifest_inspection(
        context(
            tmp_path, {"requirements.txt": content, "package.json": '{"dependencies": {}}'}
        )
    )

    # Inspecciona los manifiestos presentes sin producir hallazgos: solo lee del disco,
    # porque no hay red ni scanner externo que consultar.
    assert result.scanned == ("requirements.txt", "package.json")
    assert result.findings == ()


# ---------------------------------------------------------------------------
# Contexto cerrado
# ---------------------------------------------------------------------------
def test_context_only_reads_authorized_paths(tmp_path: Path) -> None:
    """Un check no puede leer archivos fuera del contexto autorizado."""
    workspace = build_security_project(
        tmp_path, {"allowed.py": "x = 1\n", "secret/other.py": LEAKY_FILE}
    )
    ctx = SecurityCheckContext(workspace=workspace, paths=("allowed.py",))

    result = secret_pattern_scan(ctx)

    assert result.findings == ()
    assert result.scanned == ("allowed.py",)
    assert ctx.read("secret/other.py") is None


def test_context_rejects_traversal(tmp_path: Path) -> None:
    """Una ruta con traversal no se resuelve, aunque exista."""
    workspace = build_security_project(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    ctx = SecurityCheckContext(workspace=workspace, paths=("../fuera.py",))

    assert ctx.read("../fuera.py") is None
