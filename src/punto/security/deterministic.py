"""Checks deterministas de seguridad (ENGINE-5 §8 a §10).

Son análisis **implementados por PUNTO** que inspeccionan datos: leen archivos y los
analizan como texto o como AST. **No ejecutan código del proyecto**, así que corren en
proceso confiable; cualquier ejecución del producto seguiría exigiendo sandbox.

Lo que estos checks **no** son: un scanner industrial. No sustituyen a Bandit, Semgrep ni
Trivy, y no se presentan como si lo hicieran. Aportan evidencia determinista —repetible y
auditable— que el modelo no puede fabricar ni omitir.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from punto.qa.paths import normalize_relative_path
from punto.schemas.enums import FindingSeverity
from punto.schemas.execution import DeveloperExecutionResult
from punto.schemas.planning import Confidence
from punto.schemas.security import (
    SecurityAnalysisArea,
    SecurityFinding,
    SecurityFindingSource,
)

#: Extensiones que se analizan, por tipo de check.
PYTHON_SUFFIXES: Final[frozenset[str]] = frozenset({".py"})
TEXT_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".yaml", ".yml",
        ".toml", ".ini", ".cfg", ".conf", ".env", ".sh", ".ps1", ".sql", ".tf", ".env.example",
        ".txt", ".md", ".rst", ".java", ".go", ".rb", ".php", ".cs",
    }
)
MANIFEST_NAMES: Final[frozenset[str]] = frozenset(
    {"requirements.txt", "pyproject.toml", "package.json", "package-lock.json", "poetry.lock"}
)

#: Tamaño máximo que se inspecciona por archivo, para no leer binarios ni volcar el disco.
MAX_INSPECTED_BYTES: Final[int] = 400_000


@dataclass(frozen=True, slots=True)
class SecurityCheckResult:
    """Resultado de un check determinista."""

    name: str
    findings: tuple[SecurityFinding, ...] = ()
    scanned: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    #: Motivo por el que el check no produjo análisis, si fue el caso.
    skipped_reason: str = ""


@dataclass(slots=True)
class SecurityCheckContext:
    """Contexto de un check: qué archivos puede inspeccionar.

    El contexto es **cerrado**: solo las rutas autorizadas de la tarea. Un check no puede
    recorrer el disco por su cuenta.
    """

    workspace: Path
    paths: tuple[str, ...]
    #: Resultado del Developer, por si un check quiere citar evidencia previa.
    developer_result: DeveloperExecutionResult | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def authorized(self) -> frozenset[str]:
        """Rutas autorizadas, normalizadas. Las inválidas se descartan."""
        normalized: set[str] = set()
        for path in self.paths:
            try:
                normalized.add(normalize_relative_path(path))
            except ValueError:
                continue
        return frozenset(normalized)

    def readable(self, relative: str) -> Path | None:
        """Ruta absoluta de un archivo **autorizado**, si existe y es legible.

        La autorización se comprueba aquí y no en cada check: un check que pidiera un
        archivo fuera del contexto obtiene ``None``. El contexto queda cerrado por
        construcción, no por buena voluntad del código que lo usa.
        """
        try:
            normalized = normalize_relative_path(relative)
        except ValueError:
            return None
        if normalized not in self.authorized:
            return None

        candidate = self.workspace / normalized
        try:
            resolved = candidate.resolve()
            root = self.workspace.resolve()
        except OSError:  # pragma: no cover - depende del sistema de archivos
            return None
        if not resolved.is_relative_to(root):
            return None
        if not resolved.is_file():
            return None
        return resolved

    def read(self, relative: str) -> str | None:
        """Contenido de un archivo autorizado, o ``None`` si no es legible."""
        path = self.readable(relative)
        if path is None:
            return None
        try:
            if path.stat().st_size > MAX_INSPECTED_BYTES:
                return None
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - depende del sistema de archivos
            return None


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _finding(
    *,
    identifier: str,
    severity: FindingSeverity,
    area: SecurityAnalysisArea,
    title: str,
    description: str,
    evidence: str,
    impact: str,
    recommendation: str,
    file: str = "",
    line: int | None = None,
    source: SecurityFindingSource = SecurityFindingSource.DETERMINISTIC_CHECK,
    confidence: Confidence = Confidence.HIGH,
) -> SecurityFinding:
    """Construye un hallazgo determinista."""
    return SecurityFinding(
        id=identifier,
        severity=severity,
        category=area,
        title=title,
        description=description,
        file=file,
        line=line,
        evidence=evidence.strip()[:2_000] or "(sin extracto)",
        impact=impact,
        recommendation=recommendation,
        confidence=confidence,
        sources=(source,),
    )


# ---------------------------------------------------------------------------
# secret-pattern-scan
# ---------------------------------------------------------------------------
#: Marcadores que convierten un valor en un **placeholder**, no en un secreto real.
#:
#: Los tests usan canarios a propósito y la documentación usa ejemplos: marcarlos como
#: hallazgos bloquearía el motor por su propio material didáctico.
PLACEHOLDER_MARKERS: Final[tuple[str, ...]] = (
    "example",
    "placeholder",
    "your-",
    "yourkey",
    "your_key",
    "xxx",
    "changeme",
    "change-me",
    "dummy",
    "fake",
    "canary",
    "redacted",
    "not-a-real",
    "notreal",
    "sample",
    "todo",
    "<",
    "...",
    "${",
    "{{",
    "os.environ",
    "getenv",
    "process.env",
)

#: Patrones de secreto. El orden importa: se reporta el primero que coincide por línea.
SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[str], FindingSeverity], ...]] = (
    (
        "private-key-block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
        FindingSeverity.CRITICAL,
    ),
    (
        "credential-url",
        re.compile(
            r"\b[a-z][a-z0-9+.\-]{2,}://[^\s:/@]{2,}:[^\s:/@]{2,}@[^\s/]+", re.IGNORECASE
        ),
        FindingSeverity.HIGH,
    ),
    (
        "provider-api-key",
        re.compile(r"\b(?:sk|rk|pk|ghp|gho|github_pat|xox[baprs])[-_][A-Za-z0-9_\-]{16,}"),
        FindingSeverity.HIGH,
    ),
    (
        "bearer-token",
        re.compile(r"\bBearer\s+[A-Za-z0-9_\-.]{20,}"),
        FindingSeverity.HIGH,
    ),
    (
        "password-assignment",
        re.compile(
            r"(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
            r"\s*[:=]\s*[\"'][^\"']{8,}[\"']",
            re.IGNORECASE,
        ),
        FindingSeverity.HIGH,
    ),
)


def secret_pattern_scan(context: SecurityCheckContext) -> SecurityCheckResult:
    """Busca secretos embebidos en los archivos autorizados.

    Evita los falsos positivos obvios: canarios de prueba, placeholders de `.env.example`
    y ejemplos de documentación. Reporta el hallazgo; **no** elimina nada.
    """
    findings: list[SecurityFinding] = []
    scanned: list[str] = []
    counter = 0

    for relative in context.paths:
        suffix = Path(relative).suffix.lower()
        name = Path(relative).name.lower()
        if suffix not in TEXT_SUFFIXES and name not in MANIFEST_NAMES and name != ".env.example":
            continue
        content = context.read(relative)
        if content is None:
            continue
        scanned.append(relative)

        for line_number, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("<!--", "```")):
                continue
            for pattern_name, pattern, severity in SECRET_PATTERNS:
                match = pattern.search(line)
                if match is None:
                    continue
                value = match.group(0)
                if _is_placeholder(line, value):
                    continue
                counter += 1
                findings.append(
                    _finding(
                        identifier=f"SEC-D{counter:03d}",
                        severity=severity,
                        area=SecurityAnalysisArea.SECRETS,
                        title=f"Posible secreto embebido ({pattern_name})",
                        description=(
                            "Se encontró un valor con forma de credencial en el código o "
                            "la configuración del proyecto."
                        ),
                        evidence=_redact(value),
                        impact=(
                            "Un secreto en el repositorio queda expuesto a cualquiera con "
                            "acceso al código y sobrevive a rotaciones."
                        ),
                        recommendation=(
                            "Sacar el valor a una variable de entorno o a un gestor de "
                            "secretos y rotarlo."
                        ),
                        file=relative,
                        line=line_number,
                    )
                )
                break

    return SecurityCheckResult(
        name="secret-pattern-scan",
        findings=tuple(findings),
        scanned=tuple(scanned),
        notes=(f"{len(scanned)} archivo(s) inspeccionado(s)",),
    )


def _is_placeholder(line: str, value: str) -> bool:
    """True si el valor es un placeholder, un canario de prueba o una lectura de entorno."""
    lowered_line = line.lower()
    lowered_value = value.lower()
    return any(
        marker in lowered_line or marker in lowered_value for marker in PLACEHOLDER_MARKERS
    )


def _redact(value: str) -> str:
    """Recorta el valor para no copiar el secreto completo a la evidencia."""
    if len(value) <= 12:
        return f"{value[:4]}…[recortado]"
    return f"{value[:8]}…[recortado {len(value)} caracteres]"


# ---------------------------------------------------------------------------
# dangerous-path-scan
# ---------------------------------------------------------------------------
#: Rutas que nunca deberían aparecer en código versionado ni en datos de ejemplo.
DANGEROUS_PATH_PATTERNS: Final[tuple[tuple[str, re.Pattern[str], FindingSeverity], ...]] = (
    (
        "absolute-windows-user-path",
        re.compile(r"[A-Za-z]:\\\\?Users\\\\?[^\\\s\"']+", re.IGNORECASE),
        FindingSeverity.MEDIUM,
    ),
    (
        "system-secret-directory",
        re.compile(r"(?:/etc/shadow|/etc/passwd|\.ssh/id_(?:rsa|ed25519)|\.aws/credentials)"),
        FindingSeverity.HIGH,
    ),
    (
        "world-writable-path",
        re.compile(r"chmod\s+(?:777|a\+rwx)", re.IGNORECASE),
        FindingSeverity.MEDIUM,
    ),
)


def dangerous_path_scan(context: SecurityCheckContext) -> SecurityCheckResult:
    """Busca rutas y permisos peligrosos en archivos y scripts del proyecto."""
    findings: list[SecurityFinding] = []
    scanned: list[str] = []
    counter = 0

    for relative in context.paths:
        content = context.read(relative)
        if content is None:
            continue
        scanned.append(relative)
        for line_number, line in enumerate(content.splitlines(), start=1):
            for pattern_name, pattern, severity in DANGEROUS_PATH_PATTERNS:
                if pattern.search(line) is None:
                    continue
                counter += 1
                findings.append(
                    _finding(
                        identifier=f"SEC-P{counter:03d}",
                        severity=severity,
                        area=SecurityAnalysisArea.FILESYSTEM,
                        title=f"Ruta o permiso peligroso ({pattern_name})",
                        description=(
                            "El proyecto referencia una ruta del sistema o un permiso que "
                            "no debería estar en código versionado."
                        ),
                        evidence=line.strip()[:200],
                        impact=(
                            "Depender de rutas del entorno hace el comportamiento "
                            "impredecible y puede exponer datos del sistema."
                        ),
                        recommendation=(
                            "Parametrizar la ruta y reducir los permisos al mínimo necesario."
                        ),
                        file=relative,
                        line=line_number,
                    )
                )
                break

    return SecurityCheckResult(
        name="dangerous-path-scan",
        findings=tuple(findings),
        scanned=tuple(scanned),
    )


# ---------------------------------------------------------------------------
# python-ast-security
# ---------------------------------------------------------------------------
#: Llamadas peligrosas: (nombre del módulo, nombre de la función o atributo) → regla.
#: Se declaran explícitamente para que la regla sea auditable, no heurística.
DANGEROUS_CALLS: Final[tuple[tuple[str, str, FindingSeverity, str], ...]] = (
    ("", "eval", FindingSeverity.HIGH, "ejecuta código arbitrario desde una cadena"),
    ("", "exec", FindingSeverity.HIGH, "ejecuta código arbitrario desde una cadena"),
    ("os", "system", FindingSeverity.HIGH, "ejecuta un comando en la shell del sistema"),
    ("os", "popen", FindingSeverity.MEDIUM, "abre un proceso con una cadena de shell"),
    ("pickle", "loads", FindingSeverity.HIGH, "deserializa datos no confiables"),
    ("pickle", "load", FindingSeverity.MEDIUM, "deserializa datos no confiables"),
    ("marshal", "loads", FindingSeverity.MEDIUM, "deserializa bytecode no confiable"),
    ("yaml", "load", FindingSeverity.MEDIUM, "carga YAML sin safe_load"),
    ("subprocess", "getoutput", FindingSeverity.HIGH, "ejecuta un comando en la shell"),
)


def python_ast_security(context: SecurityCheckContext) -> SecurityCheckResult:
    """Analiza archivos Python con el AST buscando patrones peligrosos.

    Severidad conservadora: una coincidencia es **evidencia**, no una explotación
    demostrada. ``shell=True`` sube a HIGH porque combina ejecución de shell con
    concatenación de datos; el resto se queda en MEDIUM.

    No se analiza código que no sea Python ni archivos que no compilen: eso lo dirá el
    propio validador del proyecto.
    """
    findings: list[SecurityFinding] = []
    scanned: list[str] = []
    skipped: list[str] = []
    counter = 0

    for relative in context.paths:
        if Path(relative).suffix.lower() not in PYTHON_SUFFIXES:
            continue
        content = context.read(relative)
        if content is None:
            continue
        try:
            tree = ast.parse(content)
        except SyntaxError as exc:
            skipped.append(f"{relative}: no compila ({exc.msg})")
            continue
        scanned.append(relative)
        lines = content.splitlines()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = _call_target(node)
            if target is None:
                continue
            module, attribute = target

            for rule_module, rule_name, severity, reason in DANGEROUS_CALLS:
                if attribute != rule_name:
                    continue
                if rule_module and module != rule_module:
                    continue
                if rule_name == "load" and module != "yaml":
                    continue

                effective = severity
                if rule_name in {"system", "getoutput"} or (
                    module == "subprocess" and _has_shell_true(node)
                ):
                    effective = FindingSeverity.HIGH
                if attribute == "load" and not _is_yaml_unsafe(node):
                    continue

                counter += 1
                line_number = getattr(node, "lineno", 1)
                excerpt = lines[line_number - 1].strip() if line_number <= len(lines) else ""
                findings.append(
                    _finding(
                        identifier=f"SEC-A{counter:03d}",
                        severity=effective,
                        area=SecurityAnalysisArea.INJECTION,
                        title=f"Uso peligroso: {module + '.' if module else ''}{attribute}",
                        description=f"La llamada {reason}.",
                        evidence=excerpt[:200] or "(sin extracto)",
                        impact=(
                            "Si la entrada llega de una fuente no confiable, permite "
                            "ejecución de código o de comandos con los privilegios del proceso."
                        ),
                        recommendation=(
                            "Evitar la ejecución dinámica; si es imprescindible, usar "
                            "argumentos estructurados y validar la entrada."
                        ),
                        file=relative,
                        line=line_number,
                    )
                )
                break

        # subprocess con shell=True, con nombre o atributo.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_subprocess_shell_true(node):
                counter += 1
                line_number = getattr(node, "lineno", 1)
                excerpt = lines[line_number - 1].strip() if line_number <= len(lines) else ""
                findings.append(
                    _finding(
                        identifier=f"SEC-A{counter:03d}",
                        severity=FindingSeverity.HIGH,
                        area=SecurityAnalysisArea.INJECTION,
                        title="subprocess con shell=True",
                        description=(
                            "Se invoca un proceso a través de la shell; la cadena se "
                            "interpreta como comando, no como argumentos."
                        ),
                        evidence=excerpt[:200] or "(sin extracto)",
                        impact=(
                            "Si algún fragmento de la cadena viene de fuera, permite "
                            "inyección de comandos con los privilegios del proceso."
                        ),
                        recommendation=(
                            "Pasar una lista de argumentos con shell=False, o usar una "
                            "API que no invoque la shell."
                        ),
                        file=relative,
                        line=line_number,
                    )
                )

    return SecurityCheckResult(
        name="python-ast-security",
        findings=tuple(findings),
        scanned=tuple(scanned),
        notes=tuple(skipped),
    )


def _call_target(node: ast.Call) -> tuple[str, str] | None:
    """Módulo y nombre llamados, para ``os.system`` o ``eval``."""
    func = node.func
    if isinstance(func, ast.Name):
        return ("", func.id)
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            return (func.value.id, func.attr)
        if isinstance(func.value, ast.Attribute) and isinstance(func.value.value, ast.Name):
            # ``subprocess.run`` dentro de un alias de módulo: se conserva el último nombre.
            return (func.value.attr, func.attr)
    return None


def _has_shell_true(node: ast.Call) -> bool:
    """True si la llamada pasa ``shell=True``."""
    for keyword in node.keywords:
        if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant):
            return keyword.value.value is True
    return False


def _is_yaml_unsafe(node: ast.Call) -> bool:
    """True si ``yaml.load`` se usa sin ``SafeLoader`` ni ``Loader=SafeLoader``."""
    for keyword in node.keywords:
        if keyword.arg == "Loader":
            return not _is_safe_loader(keyword.value)
    if node.args:
        return True
    return True


def _is_safe_loader(value: ast.expr) -> bool:
    """True si el loader indicado es un loader seguro de PyYAML."""
    name = ""
    if isinstance(value, ast.Attribute):
        name = value.attr
    elif isinstance(value, ast.Name):
        name = value.id
    return "safe" in name.lower()


def _is_subprocess_shell_true(node: ast.Call) -> bool:
    """True si la llamada es de ``subprocess`` y usa ``shell=True``."""
    target = _call_target(node)
    if target is None:
        return False
    module, attribute = target
    if module != "subprocess":
        return False
    if attribute in {"Popen", "run", "call", "check_call", "check_output", "getoutput"}:
        return _has_shell_true(node)
    return False


# ---------------------------------------------------------------------------
# dependency-manifest-inspection
# ---------------------------------------------------------------------------
_UNPINNED_REQUIREMENT: Final[re.Pattern[str]] = re.compile(
    r"^\s*([A-Za-z0-9_.\-]+)\s*(?:[<>=!~]|$)"
)


def dependency_manifest_inspection(context: SecurityCheckContext) -> SecurityCheckResult:
    """Inspecciona manifiestos de dependencias **sin red**.

    Comprueba lo que se puede demostrar sin consultar registros externos: dependencias
    sin versión fijada y manifiestos ilegibles. No pretende ser un análisis de
    vulnerabilidades de dependencias: eso exigiría un scanner que PUNTO no tiene.
    """
    findings: list[SecurityFinding] = []
    scanned: list[str] = []
    counter = 0

    for relative in context.paths:
        name = Path(relative).name
        if name not in MANIFEST_NAMES:
            continue
        content = context.read(relative)
        if content is None:
            continue
        scanned.append(relative)
        if name != "requirements.txt":
            continue

        for line_number, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "==" in stripped or "===" in stripped:
                continue
            match = _UNPINNED_REQUIREMENT.match(stripped)
            if match is None:
                continue
            counter += 1
            findings.append(
                _finding(
                    identifier=f"SEC-M{counter:03d}",
                    severity=FindingSeverity.LOW,
                    area=SecurityAnalysisArea.SUPPLY_CHAIN,
                    title=f"Dependencia sin versión fijada: {match.group(1)}",
                    description=(
                        "El manifiesto admite cualquier versión de la dependencia, así que "
                        "dos instalaciones pueden resolver árboles distintos."
                    ),
                    evidence=stripped[:200],
                    impact=(
                        "Una versión futura con un fallo de seguridad entraría sin que nada "
                        "lo detecte, y las compilaciones dejarían de ser reproducibles."
                    ),
                    recommendation="Fijar la versión con `==` y actualizar de forma deliberada.",
                    file=relative,
                    line=line_number,
                    confidence=Confidence.MEDIUM,
                )
            )

    return SecurityCheckResult(
        name="dependency-manifest-inspection",
        findings=tuple(findings),
        scanned=tuple(scanned),
    )


#: Checks deterministas disponibles, en orden de ejecución.
DeterministicCheck = Callable[[SecurityCheckContext], SecurityCheckResult]

DETERMINISTIC_CHECKS: Final[tuple[tuple[str, DeterministicCheck], ...]] = (
    ("secret-pattern-scan", secret_pattern_scan),
    ("dangerous-path-scan", dangerous_path_scan),
    ("python-ast-security", python_ast_security),
    ("dependency-manifest-inspection", dependency_manifest_inspection),
)


__all__ = [
    "DANGEROUS_CALLS",
    "DANGEROUS_PATH_PATTERNS",
    "DETERMINISTIC_CHECKS",
    "MANIFEST_NAMES",
    "MAX_INSPECTED_BYTES",
    "PLACEHOLDER_MARKERS",
    "PYTHON_SUFFIXES",
    "SECRET_PATTERNS",
    "TEXT_SUFFIXES",
    "DeterministicCheck",
    "SecurityCheckContext",
    "SecurityCheckResult",
    "dangerous_path_scan",
    "dependency_manifest_inspection",
    "python_ast_security",
    "secret_pattern_scan",
]
