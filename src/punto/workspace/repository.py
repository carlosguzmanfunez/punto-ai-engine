"""Frontera de recursos gobernada sobre un repositorio destino (PILOT-04).

Este módulo es la **única** puerta por la que el ciclo de desarrollo lee, escribe, ejecuta y
confirma sobre un repositorio real. No duplica piezas que ya existen: compone las que el motor ya
tenía y añade exactamente lo que faltaba.

Lo que **reutiliza**, sin reimplementarlo:

- containment de rutas: :func:`~punto.qa.paths.normalize_relative_path` (rechaza byte nulo,
  caracteres de control, rutas absolutas, unidad Windows, UNC y ``..``) y
  :meth:`~punto.developer.context.ExecutionContext.resolve_path`, que resuelve enlaces y exige que
  el resultado siga dentro del workspace — es lo que detiene un escape por symlink o junction;
- escritura verificada: :class:`~punto.tools.filesystem.FilesystemTool` (relee lo escrito y exige
  rama de tarea, workspace y rutas no protegidas);
- ejecución: :class:`~punto.tools.shell.ShellRunner` sobre ``TrustedLocalBackend`` (allowlist,
  default deny, entorno saneado, ``shell=False``, timeout) y
  :class:`~punto.tools.validator.Validator` (un check solo pasa si se ejecutó, no expiró y salió 0);
- Git local sin remoto: :class:`~punto.tools.git.GitWorkspace` (``push``, ``fetch``, ``pull`` y
  ``clone`` están prohibidos por política y no existen en su API) y
  :class:`~punto.project.workspace.GitWorkspaceLineage` (baseline SHA + diff real);
- autoridad: :class:`~punto.policy.policy_engine.PolicyEngine` con el catálogo revisado;
- frontera de secretos en el contenido: :func:`~punto.memory.experience.assert_no_secrets`.

Lo que **añade**: las operaciones autorizables una a una (READ/WRITE/CREATE/DELETE/EXECUTE/COMMIT),
el inventario de cambios preexistentes del usuario, la precondición de huella al escribir (para no
escribir encima de algo distinto de lo que se leyó), la denegación de ficheros que parecen
almacenes de secretos, y la auditoría de cada operación.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from uuid import UUID

from punto.audit.logger import AuditLogger
from punto.developer.context import ExecutionContext
from punto.memory.experience import ExperienceSecretError, assert_no_secrets
from punto.policy.policy_engine import PolicyEngine
from punto.qa.paths import normalize_relative_path
from punto.schemas.decision import ActionRequest
from punto.schemas.dev import ChangeOperation, RepositoryOperation
from punto.schemas.enums import RiskLevel
from punto.schemas.execution import (
    CommandRequest,
    CommandResult,
    ExecutionTrustLevel,
    FileChange,
)
from punto.tools.filesystem import FilesystemTool
from punto.tools.git import GitWorkspace
from punto.tools.shell import ShellRunner
from punto.tools.validator import Validator

#: Nombres que **nunca** se leen ni se escriben: un fichero así es un almacén de credenciales.
#: Se compara por nombre base, en minúsculas, y por prefijo para los ``.env.*``.
SECRET_FILE_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".env",
        ".npmrc",
        ".netrc",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        ".pypirc",
        ".htpasswd",
    }
)

#: Sufijos que delatan material criptográfico.
SECRET_FILE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".pem", ".key", ".p12", ".pfx", ".jks", ".kdbx", ".ppk", ".keystore"}
)

#: Directorios cuyo contenido no forma parte del trabajo: metadatos de herramientas y del propio
#: motor (el contenedor de snapshots vive fuera del repositorio, pero se protege igual).
FORBIDDEN_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {".git", ".vercel", ".next", "node_modules", ".punto-repair-snapshots"}
)

#: Acción del catálogo de autoridad que ampara cada operación.
POLICY_ACTION: Final[Mapping[RepositoryOperation, str]] = {
    RepositoryOperation.WRITE: "modify_file",
    RepositoryOperation.CREATE: "create_file",
    RepositoryOperation.DELETE: "delete_file",
    RepositoryOperation.EXECUTE: "run_tests",
    RepositoryOperation.COMMIT: "create_commit",
}


class RepositoryDenied(RuntimeError):
    """La frontera de recursos denegó una operación."""

    code: str = "REPOSITORY_DENIED"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class OperationNotAuthorizedError(RepositoryDenied):
    """La operación no está autorizada para este destino."""

    code = "OPERATION_NOT_AUTHORIZED"


class ScopeViolation(RepositoryDenied):
    """La ruta queda fuera del alcance autorizado o del repositorio."""

    code = "SCOPE_VIOLATION"


class SecretBoundaryViolation(RepositoryDenied):
    """La ruta o el contenido tocan material que no cruza esta frontera."""

    code = "SECRET_BOUNDARY_VIOLATION"


class StaleWriteError(RepositoryDenied):
    """El fichero no está en el estado que el cambio declaró haber leído."""

    code = "STALE_WRITE"


class PolicyDeniedError(RepositoryDenied):
    """El catálogo de autoridad no permite la operación."""

    code = "POLICY_DENIED"


@dataclass(frozen=True, slots=True)
class RepositoryPolicy:
    """Qué puede hacer el ciclo sobre un destino concreto, declarado por PUNTO."""

    allowed_operations: frozenset[RepositoryOperation]
    allowed_commands: frozenset[str]
    allowed_command_lines: tuple[tuple[str, ...], ...]
    scope_roots: tuple[str, ...] = ()
    max_files_changed: int = 12
    max_read_bytes: int = 200_000
    command_timeout_seconds: float = 300.0
    is_task_branch_required: bool = True

    def authorize(self, operation: RepositoryOperation) -> None:
        """Comprueba que la operación esté autorizada.

        Raises:
            OperationNotAuthorizedError: si el destino no la declara.
        """
        if operation not in self.allowed_operations:
            allowed = ", ".join(sorted(item.value for item in self.allowed_operations))
            raise OperationNotAuthorizedError(
                f"la operación {operation.value} no está autorizada en este destino "
                f"(autorizadas: {allowed})"
            )

    def authorize_argv(self, argv: Sequence[str]) -> None:
        """Comprueba que un ``argv`` sea exactamente una línea autorizada.

        Se compara por **prefijo exacto**: el proveedor elige un nombre del catálogo, no construye
        el comando, así que no puede colar argumentos nuevos.

        Raises:
            OperationNotAuthorizedError: si el ``argv`` no es prefijo de ninguna línea autorizada.
        """
        if not argv:
            raise OperationNotAuthorizedError("argv vacío")
        candidate = tuple(argv)
        for allowed in self.allowed_command_lines:
            if candidate[: len(allowed)] == allowed:
                return
        rendered = " ".join(candidate)
        raise OperationNotAuthorizedError(f"el comando {rendered!r} no está autorizado")


@dataclass(slots=True)
class GovernedRepository:
    """Acceso gobernado a un repositorio destino, con alcance, secretos y autoridad."""

    root: Path
    task_id: UUID
    policy: RepositoryPolicy
    branch: str = ""
    audit: AuditLogger | None = None
    policy_engine: PolicyEngine | None = None
    actor: str = "punto-dev-cycle"
    context: ExecutionContext = field(init=False)
    snapshot_root: Path | None = None
    _filesystem: FilesystemTool = field(init=False, repr=False)
    _shell: ShellRunner = field(init=False, repr=False)
    _git: GitWorkspace = field(init=False, repr=False)
    _validator: Validator = field(init=False, repr=False)
    _baseline_sha: str = field(init=False, default="", repr=False)
    _preexisting: tuple[str, ...] | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        """Resuelve el workspace, construye las herramientas y captura el baseline.

        El estado que **no** hace falta para decidir nada —rama actual y cambios preexistentes— se
        lee la primera vez que se pide: construir la frontera no debe costar procesos de más.
        """
        self.root = Path(self.root).resolve()
        self.context = ExecutionContext(
            task_id=self.task_id,
            workspace_path=self.root,
            branch_name=self.branch,
            allowed_commands=self.policy.allowed_commands,
            max_files_changed=self.policy.max_files_changed,
            default_timeout_seconds=self.policy.command_timeout_seconds,
            trust_level=ExecutionTrustLevel.TRUSTED_LOCAL,
            network_access=False,
        )
        self._filesystem = FilesystemTool(self.context)
        self._shell = ShellRunner(self.context)
        self._git = GitWorkspace(self.context, self._shell)
        self._validator = Validator(self.context, self._shell)
        self._baseline_sha = self._git.head_sha()

    # ------------------------------------------------------------------ estado
    @property
    def baseline_sha(self) -> str:
        """SHA del commit sobre el que empezó el ciclo."""
        return self._baseline_sha

    @property
    def preexisting_changes(self) -> tuple[str, ...]:
        """Líneas de ``git status --porcelain`` que ya existían antes del ciclo."""
        if self._preexisting is None:
            self._preexisting = self._git.status_lines()
        return self._preexisting

    def preexisting_paths(self) -> tuple[str, ...]:
        """Rutas modificadas antes del ciclo, tal como las declara Git."""
        paths: list[str] = []
        for line in self.preexisting_changes:
            candidate = line[3:].strip().strip('"')
            if candidate:
                paths.append(candidate.replace("\\", "/"))
        return tuple(paths)

    def current_branch(self) -> str:
        """Rama real del repositorio ahora mismo (una lectura de Git)."""
        return self._git.current_branch()

    def verify_work_branch(self) -> str:
        """Comprueba que la rama real sea la declarada y la devuelve.

        Raises:
            RepositoryDenied: si la rama real no coincide con la declarada.
        """
        real = self.current_branch()
        self.branch = real
        if real != self.context.branch_name:
            raise RepositoryDenied(
                f"la rama real {real!r} no es la rama de trabajo declarada "
                f"{self.context.branch_name!r}"
            )
        return real

    def status_lines(self) -> tuple[str, ...]:
        """Estado actual del árbol."""
        return self._git.status_lines()

    def head_sha(self) -> str:
        """SHA del commit actual."""
        return self._git.head_sha()

    def changed_paths(self) -> tuple[str, ...]:
        """Rutas que difieren del baseline: las del árbol de trabajo y las ya confirmadas."""
        working = [
            line[3:].strip().strip('"').replace("\\", "/")
            for line in self.status_lines()
            if line[3:].strip()
        ]
        try:
            committed = list(self._git.diff_names(self._baseline_sha, "HEAD"))
        except Exception:  # pragma: no cover - repositorio sin commits posteriores
            committed = []
        return tuple(dict.fromkeys([*working, *committed]))

    def ensure_work_branch(self, slug: str) -> str:
        """Deja el repositorio en su rama de tarea ``ai/<task>-<slug>`` (la crea si hace falta)."""
        return self._git.ensure_task_branch(self.task_id, slug)

    def switch_to_work_branch(self, name: str) -> str:
        """Deja el repositorio en la rama de trabajo **declarada**, con ese nombre exacto.

        Si la rama existe se activa; si no, se crea desde el estado actual. Nunca se escribe código
        sobre ``main``/``master``: eso lo sigue imponiendo :class:`ExecutionContext`.

        Raises:
            RepositoryDenied: si el nombre no es una rama de tarea válida.
        """
        declared = name.strip()
        if not declared.startswith("ai/"):
            raise RepositoryDenied(
                f"la rama de trabajo {declared!r} no es una rama de tarea (debe empezar por 'ai/')"
            )
        if self._git.branch_exists(declared):
            return self._git.checkout(declared)
        return self._git.create_branch(declared)

    # ------------------------------------------------------------------ rutas
    def resolve(self, path: str) -> Path:
        """Resuelve una ruta y comprueba alcance, protección y directorios prohibidos.

        Raises:
            ScopeViolation: si la ruta no es declarable, sale del repositorio o del alcance.
        """
        try:
            relative = normalize_relative_path(path)
        except ValueError as exc:
            raise ScopeViolation(f"ruta no declarable ({exc})") from exc
        head = relative.split("/", maxsplit=1)[0]
        if head in FORBIDDEN_DIRECTORIES:
            raise ScopeViolation(f"{head!r} no forma parte del trabajo del ciclo")
        if self.policy.scope_roots and not any(
            relative == root or relative.startswith(f"{root}/") for root in self.policy.scope_roots
        ):
            raise ScopeViolation(
                f"{relative!r} queda fuera del alcance autorizado "
                f"({' ,'.join(self.policy.scope_roots)})"
            )
        try:
            resolved = self.context.resolve_path(relative)
        except Exception as exc:
            raise ScopeViolation(
                f"{relative!r} no resuelve dentro del repositorio ({exc})"
            ) from exc
        return resolved

    @staticmethod
    def _is_secret_name(relative: str) -> bool:
        """True si el nombre del fichero delata un almacén de credenciales."""
        name = relative.rsplit("/", maxsplit=1)[-1].lower()
        if name in SECRET_FILE_NAMES or name.startswith(".env"):
            return True
        return Path(name).suffix in SECRET_FILE_SUFFIXES

    def assert_not_secret(self, relative: str) -> None:
        """Deniega rutas que parecen almacenes de secretos.

        Raises:
            SecretBoundaryViolation: si el nombre corresponde a material sensible.
        """
        if self._is_secret_name(relative):
            raise SecretBoundaryViolation(
                f"{relative!r} parece un almacén de credenciales: no cruza esta frontera"
            )
        if relative.split("/", maxsplit=1)[0] in FORBIDDEN_DIRECTORIES:
            raise SecretBoundaryViolation(f"{relative!r} no es contenido del trabajo")

    # ------------------------------------------------------------------ lectura
    def read_text(self, path: str) -> str:
        """Lee un fichero de texto dentro del alcance y sin secretos.

        Raises:
            ScopeViolation: si la ruta sale del alcance.
            SecretBoundaryViolation: si el fichero es un almacén de credenciales o su contenido
                tiene forma de credencial (se deniega; no se sanea en silencio).
        """
        resolved = self.resolve(path)
        relative = self.context.relative_path(resolved)
        self.assert_not_secret(relative)
        if not resolved.is_file():
            raise ScopeViolation(f"{relative!r} no existe en el repositorio")
        size = resolved.stat().st_size
        if size > self.policy.max_read_bytes:
            raise ScopeViolation(
                f"{relative!r} tiene {size} bytes y el tope de lectura es "
                f"{self.policy.max_read_bytes}"
            )
        text = resolved.read_text(encoding="utf-8", errors="replace")
        self.assert_no_secrets_in_text(relative, text)
        return text

    @staticmethod
    def assert_no_secrets_in_text(where: str, text: str) -> None:
        """Deniega contenido con forma de credencial.

        Raises:
            SecretBoundaryViolation: si el texto contiene una credencial o cabecera de autorización.
        """
        try:
            assert_no_secrets(where, text)
        except ExperienceSecretError as exc:
            raise SecretBoundaryViolation(
                f"{where}: el contenido tiene forma de credencial y no cruza esta frontera"
            ) from exc

    def sha256(self, path: str) -> str:
        """Huella del fichero, o cadena vacía si no existe."""
        try:
            resolved = self.resolve(path)
        except ScopeViolation:
            return ""
        if not resolved.is_file():
            return ""
        return hashlib.sha256(resolved.read_bytes()).hexdigest()

    def exists(self, path: str) -> bool:
        """True si la ruta existe dentro del alcance."""
        try:
            return self.resolve(path).exists()
        except ScopeViolation:
            return False

    # ---------------------------------------------------------------- autoridad
    def authorize(
        self,
        operation: RepositoryOperation,
        *,
        paths: Sequence[str],
        description: str = "",
    ) -> None:
        """Comprueba autorización declarada y del catálogo de política.

        Raises:
            OperationNotAuthorizedError: si el destino no declara la operación.
            PolicyDeniedError: si el catálogo de autoridad no la permite.
        """
        self.policy.authorize(operation)
        action = POLICY_ACTION.get(operation)
        if action is None or self.policy_engine is None:
            return
        decision = self.policy_engine.evaluate(
            ActionRequest(
                action=action,
                technical=True,
                reversible=operation is not RepositoryOperation.DELETE,
                risk_level=RiskLevel.LOW,
                production_impact=False,
                legal_impact=False,
                business_impact=False,
                files_changed=list(paths),
                description=description or f"{operation.value} en el ciclo de desarrollo",
            )
        )
        if not decision.allowed:
            raise PolicyDeniedError(
                f"la política no permite {operation.value} sobre "
                f"{', '.join(paths) or '(sin rutas)'}: acción {action!r} "
                f"({decision.authority_level.name}, human={decision.requires_human})"
            )

    # ---------------------------------------------------------------- escritura
    def write_text(
        self,
        path: str,
        content: str,
        *,
        operation: ChangeOperation,
        expected_sha256: str | None = None,
    ) -> FileChange:
        """Escribe un cambio validado, con precondición de huella y verificación por relectura.

        Raises:
            StaleWriteError: si la huella actual no coincide con la declarada por el cambio.
            SecretBoundaryViolation: si el destino o el contenido tocan secretos.
            PolicyDeniedError: si la política no permite la operación.
            ScopeViolation: si la ruta sale del alcance.
        """
        resolved = self.resolve(path)
        relative = self.context.relative_path(resolved)
        self.assert_not_secret(relative)
        self.assert_no_secrets_in_text(relative, content)

        current = hashlib.sha256(resolved.read_bytes()).hexdigest() if resolved.is_file() else ""
        if expected_sha256 is not None and expected_sha256 != current:
            raise StaleWriteError(
                f"{relative}: el cambio declaró la huella {expected_sha256[:12]}… y el fichero "
                f"está en {current[:12] or '(no existe)'}…"
            )
        if operation is ChangeOperation.CREATE and resolved.exists():
            raise StaleWriteError(f"{relative}: se pidió crear y el fichero ya existe")

        repository_operation = (
            RepositoryOperation.CREATE
            if operation is ChangeOperation.CREATE
            else RepositoryOperation.WRITE
        )
        self.authorize(repository_operation, paths=(relative,))
        change = self._filesystem.write_text(relative, content)
        self._log_file_changed(change)
        return change

    def delete_file(self, path: str, *, expected_sha256: str | None = None) -> FileChange:
        """Elimina un fichero del alcance (solo si el destino autoriza DELETE).

        Raises:
            OperationNotAuthorizedError: si DELETE no está autorizada.
            PolicyDeniedError: si el catálogo no la permite.
            StaleWriteError: si la huella no coincide con la declarada.
        """
        resolved = self.resolve(path)
        relative = self.context.relative_path(resolved)
        self.assert_not_secret(relative)
        current = hashlib.sha256(resolved.read_bytes()).hexdigest() if resolved.is_file() else ""
        if expected_sha256 is not None and expected_sha256 != current:
            raise StaleWriteError(f"{relative}: huella distinta de la declarada")
        self.authorize(RepositoryOperation.DELETE, paths=(relative,))
        change = self._filesystem.delete_file(relative)
        self._log_file_changed(change)
        return change

    # ---------------------------------------------------------------- ejecución
    def run(
        self, argv: Sequence[str], *, name: str, timeout_seconds: float | None = None
    ) -> CommandResult:
        """Ejecuta un comando autorizado, con allowlist, entorno saneado y sin shell.

        Raises:
            OperationNotAuthorizedError: si la operación o el ``argv`` no están autorizados.
            PolicyDeniedError: si el catálogo de autoridad no permite ejecutar.
        """
        self.policy.authorize_argv(tuple(argv))
        self.authorize(RepositoryOperation.EXECUTE, paths=(), description=f"verificación {name}")
        request = CommandRequest(
            executable=argv[0],
            args=tuple(argv[1:]),
            timeout_seconds=timeout_seconds or self.policy.command_timeout_seconds,
        )
        result = self._shell.run(request, name=name)
        if self.audit is not None:
            self.audit.log_command_executed(
                task_id=self.task_id, result=result, actor=self.actor
            )
        return result

    # ------------------------------------------------------------------- commit
    def commit_local(self, paths: Sequence[str], message: str) -> str:
        """Crea un commit local **solo** con las rutas del ciclo.

        Nunca usa ``git add -A``: los cambios que ya existían antes del ciclo (por ejemplo el
        ``.gitignore`` modificado del usuario) no entran en el commit.

        Raises:
            OperationNotAuthorizedError: si COMMIT no está autorizada.
            PolicyDeniedError: si la política no permite confirmar, o si alguna ruta es un cambio
                preexistente del usuario.
        """
        self.authorize(RepositoryOperation.COMMIT, paths=tuple(paths), description=message)
        preexisting = set(self.preexisting_paths())
        intruders = sorted(set(paths) & preexisting)
        if intruders:
            raise PolicyDeniedError(
                "el commit no puede incluir cambios preexistentes del usuario: "
                + ", ".join(intruders)
            )
        self._git.add(tuple(paths))
        sha = self._git.commit(message)
        if self.audit is not None:
            self.audit.log_git_commit_created(
                task_id=self.task_id,
                branch=self.branch,
                commit_sha=sha,
                message=message,
                actor=self.actor,
            )
        return sha

    # ------------------------------------------------------------------ auditoría
    def _log_file_changed(self, change: FileChange) -> None:
        """Registra un cambio de fichero sin copiar su contenido."""
        if self.audit is None:
            return
        self.audit.log_file_changed(task_id=self.task_id, change=change, actor=self.actor)

    # ------------------------------------------------------------------ consulta
    def file_hashes(self, paths: Sequence[str]) -> dict[str, str]:
        """Huellas actuales de un conjunto de rutas (``""`` si no existe)."""
        return {path: self.sha256(path) for path in paths}

    def validate_checks(self, names: Sequence[str], catalog: Mapping[str, tuple[str, ...]]) -> None:
        """Comprueba que los nombres de verificación existan en el catálogo del destino.

        Raises:
            OperationNotAuthorizedError: si algún nombre no está en el catálogo.
        """
        unknown = [name for name in names if name not in catalog]
        if unknown:
            known = ", ".join(sorted(catalog)) or "ninguno"
            raise OperationNotAuthorizedError(
                f"verificación desconocida: {', '.join(unknown)} (conocidas: {known})"
            )


__all__ = [
    "FORBIDDEN_DIRECTORIES",
    "POLICY_ACTION",
    "SECRET_FILE_NAMES",
    "SECRET_FILE_SUFFIXES",
    "GovernedRepository",
    "OperationNotAuthorizedError",
    "PolicyDeniedError",
    "RepositoryDenied",
    "RepositoryPolicy",
    "ScopeViolation",
    "SecretBoundaryViolation",
    "StaleWriteError",
]
