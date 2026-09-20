"""Cadena de publicación a producción: gate humano → push gobernado → verificación real.

Estados (los que el encargo pide, sin inventar los que ya existen):

```
DEVELOPMENT_COMPLETED   (ya existe: DevelopmentStatus.COMPLETED, cambio local verificado)
        ↓
WAITING_PRODUCTION_APPROVAL
        ↓ APPROVE (HumanGate)
PUBLISHING                       → PUBLICATION_FAILED
        ↓
DEPLOYMENT_VERIFICATION          → DEPLOYMENT_NOT_VERIFIED
        ↓
PRODUCTION_VALIDATED
```

Los tres primeros viven en ``DevelopmentStatus``/``TaskStatus`` del motor; los de publicación se
declaran aquí porque **no existían** y son el objeto de esta cadena.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from punto.common import utc_now
from punto.providers.secrets import redact_secret_text

__all__ = [
    "MAX_PUSH_OUTPUT_CHARS",
    "GitPublisher",
    "ProductionEvidence",
    "ProductionProbe",
    "PublicationRecord",
    "PublicationRefused",
    "PublicationService",
    "PublicationStage",
    "PushEvidence",
    "PushPlan",
    "default_fetch",
]

#: Cota de la salida de ``git push`` que se conserva como evidencia.
MAX_PUSH_OUTPUT_CHARS: Final[int] = 1_200

#: Cota de la respuesta de producción que se conserva como evidencia.
MAX_BODY_CHARS: Final[int] = 200_000

_SHA_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
_BRANCH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$")
#: El remoto puede ser un nombre, una ruta (con espacios: las rutas reales los tienen) o una URL.
#: Lo que **no** puede ser es un argumento de Git: eso se comprueba aparte (nada que
#: empiece por ``-`` y nada con caracteres de control), porque el remoto viaja como
#: elemento propio del ``argv``.
_MAX_REMOTE_CHARS: Final[int] = 300

#: Esquema de un remoto de red: lo que **nunca** es una ruta local.
_SCHEME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?:https?|ssh|git|ftps?):", re.IGNORECASE)

#: Opciones de ``git push`` que jamás se usan: reescribir historia, mover todo o borrar remoto.
_FORBIDDEN_PUSH_OPTIONS: Final[tuple[str, ...]] = (
    "--force",
    "--force-with-lease",
    "--force-if-includes",
    "--tags",
    "--all",
    "--mirror",
    "--delete",
    "--prune",
    "--atomic",
    "--set-upstream",
)


class PublicationRefused(RuntimeError):
    """La publicación se rechazó antes de tocar nada, con un motivo gobernado."""

    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}")


#: Motivo con el que se rechaza un expediente persistido que no cumple su contrato.
STATE_INVALID: Final[str] = "PUBLICATION_STATE_INVALID"


def _state_field(
    data: Mapping[str, Any], key: str, kind: type | tuple[type, ...], where: str
) -> Any:
    """Campo obligatorio de un expediente persistido, con su tipo.

    Raises:
        PublicationRefused: si falta o no tiene el tipo esperado. Nunca se rellena un hueco con un
            valor por defecto: un expediente incompleto no se restaura.
    """
    if key not in data:
        raise PublicationRefused(STATE_INVALID, f"{where}: falta el campo {key!r}")
    value = data[key]
    kinds = kind if isinstance(kind, tuple) else (kind,)
    if isinstance(value, bool) and bool not in kinds and int in kinds:
        raise PublicationRefused(STATE_INVALID, f"{where}: {key!r} no puede ser booleano")
    if not isinstance(value, kinds):
        raise PublicationRefused(
            STATE_INVALID, f"{where}: {key!r} no tiene el tipo esperado ({type(value).__name__})"
        )
    return value


def _history_entries(raw: Any, where: str) -> list[dict[str, str]]:
    """Historial persistido de la publicación, validado entrada por entrada."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PublicationRefused(STATE_INVALID, f"{where}: el historial no es una lista")
    entries: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        spot = f"{where}.history[{index}]"
        if not isinstance(item, Mapping):
            raise PublicationRefused(STATE_INVALID, f"{spot}: se esperaba un objeto")
        stage = _state_field(item, "stage", str, spot)
        if stage not in PUBLICATION_STAGES:
            raise PublicationRefused(STATE_INVALID, f"{spot}: etapa desconocida {stage!r}")
        moment = _state_field(item, "at", str, spot)
        try:
            datetime.fromisoformat(moment)
        except ValueError as exc:
            raise PublicationRefused(STATE_INVALID, f"{spot}: momento inválido") from exc
        detail = _state_field(item, "detail", str, spot)
        entries.append({"stage": stage, "at": moment, "detail": detail})
    return entries


class PublicationStage(StrEnum):
    """Etapas de la publicación a producción (las que no existían en el motor)."""

    BLOCKED_NOT_PUBLISHABLE = "BLOCKED_NOT_PUBLISHABLE"
    WAITING_PRODUCTION_APPROVAL = "WAITING_PRODUCTION_APPROVAL"
    PUBLISHING = "PUBLISHING"
    PUBLICATION_FAILED = "PUBLICATION_FAILED"
    DEPLOYMENT_VERIFICATION = "DEPLOYMENT_VERIFICATION"
    DEPLOYMENT_NOT_VERIFIED = "DEPLOYMENT_NOT_VERIFIED"
    PRODUCTION_VALIDATED = "PRODUCTION_VALIDATED"


#: Etapas válidas de publicación: una etapa fuera de aquí delata un expediente manipulado.
PUBLICATION_STAGES: Final[frozenset[str]] = frozenset(stage.value for stage in PublicationStage)


@dataclass(frozen=True, slots=True)
class PushEvidence:
    """Resultado del push gobernado: qué se empujó, a dónde y con qué salida."""

    remote: str
    ref: str
    sha: str
    argv: tuple[str, ...]
    exit_code: int
    output: str
    pushed: bool

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable, con la salida redactada."""
        return {
            "remote": self.remote,
            "ref": self.ref,
            "sha": self.sha,
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "output": redact_secret_text(self.output)[:MAX_PUSH_OUTPUT_CHARS],
            "pushed": self.pushed,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PushEvidence:
        """Reconstruye la evidencia del push desde su vista serializable.

        Se usa al recuperar un expediente durable tras un reinicio. Falla cerrado: un campo ausente
        o con otra forma levanta ``PublicationRefused`` en vez de rellenarse con un valor por
        defecto, porque una evidencia inventada es peor que ninguna evidencia.
        """
        if not isinstance(data, Mapping):
            raise PublicationRefused(STATE_INVALID, "push: se esperaba un objeto")
        argv = _state_field(data, "argv", list, "push")
        return cls(
            remote=_state_field(data, "remote", str, "push"),
            ref=_state_field(data, "ref", str, "push"),
            sha=_state_field(data, "sha", str, "push"),
            argv=tuple(str(item) for item in argv),
            exit_code=_state_field(data, "exit_code", int, "push"),
            output=_state_field(data, "output", str, "push"),
            pushed=_state_field(data, "pushed", bool, "push"),
        )


@dataclass(frozen=True, slots=True)
class ProductionEvidence:
    """Resultado de la comprobación de producción: qué respondió el destino real."""

    url: str
    marker: str
    attempts: int
    status_code: int
    marker_found: bool
    detail: str

    @property
    def validated(self) -> bool:
        """True solo si producción respondió bien **y** mostró lo que se esperaba."""
        return self.status_code == 200 and (not self.marker or self.marker_found)

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable."""
        return {
            "url": self.url,
            "marker": self.marker,
            "attempts": self.attempts,
            "status_code": self.status_code,
            "marker_found": self.marker_found,
            "validated": self.validated,
            "detail": self.detail[:300],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProductionEvidence:
        """Reconstruye la evidencia de producción desde su vista serializable.

        La validación declarada tiene que coincidir con la que sale de la propia evidencia: si el
        documento dice ``validated`` y la respuesta real no lo sostiene, el expediente se rechaza.
        """
        if not isinstance(data, Mapping):
            raise PublicationRefused(STATE_INVALID, "production: se esperaba un objeto")
        evidence = cls(
            url=_state_field(data, "url", str, "production"),
            marker=_state_field(data, "marker", str, "production"),
            attempts=_state_field(data, "attempts", int, "production"),
            status_code=_state_field(data, "status_code", int, "production"),
            marker_found=_state_field(data, "marker_found", bool, "production"),
            detail=_state_field(data, "detail", str, "production"),
        )
        declared = _state_field(data, "validated", bool, "production")
        if declared is not evidence.validated:
            raise PublicationRefused(
                STATE_INVALID,
                "production: la validación declarada no coincide con la evidencia comprobada",
            )
        return evidence


@dataclass(slots=True)
class PublicationRecord:
    """Expediente de la publicación de una tarea: etapa, evidencia y motivo."""

    task_id: str
    request_id: str
    target_id: str
    commit_sha: str
    approval_id: str = ""
    stage: PublicationStage = PublicationStage.WAITING_PRODUCTION_APPROVAL
    error_kind: str = ""
    error: str = ""
    push: PushEvidence | None = None
    production: ProductionEvidence | None = None
    history: list[dict[str, str]] = field(default_factory=list)
    #: Decisión de autoridad que amparó la publicación (AP000-R01), si no fue un Human Gate.
    authority: dict[str, Any] | None = None

    def advance(self, stage: PublicationStage, detail: str = "") -> PublicationStage:
        """Cambia de etapa dejando constancia del momento y del motivo."""
        self.stage = stage
        self.history.append(
            {"stage": stage.value, "at": utc_now().isoformat(), "detail": detail[:300]}
        )
        return stage

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable, sin secretos."""
        return {
            "task_id": self.task_id,
            "request_id": self.request_id,
            "target_id": self.target_id,
            "commit_sha": self.commit_sha,
            "approval_id": self.approval_id,
            "stage": self.stage.value,
            "error_kind": self.error_kind,
            "error": self.error[:300],
            "push": self.push.as_dict() if self.push is not None else None,
            "production": self.production.as_dict() if self.production is not None else None,
            "history": list(self.history),
            "authority": self.authority,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PublicationRecord:
        """Reconstruye el expediente completo desde su vista serializable (estado durable).

        Es la operación simétrica de :meth:`as_dict`: lo que se guarda se puede volver a leer con el
        mismo contrato. Falla cerrado (``PublicationRefused`` con ``PUBLICATION_STATE_INVALID``) si
        falta un campo, la etapa no es una etapa real o la evidencia no sostiene lo que declara, de
        modo que un expediente manipulado **no** se convierte en una publicación validada.
        """
        if not isinstance(data, Mapping):
            raise PublicationRefused(STATE_INVALID, "publication: se esperaba un objeto")
        where = "publication"
        stage_raw = _state_field(data, "stage", str, where)
        if stage_raw not in PUBLICATION_STAGES:
            raise PublicationRefused(STATE_INVALID, f"{where}: etapa desconocida {stage_raw!r}")
        push = data.get("push")
        production = data.get("production")
        authority = data.get("authority")
        if push is not None and not isinstance(push, Mapping):
            raise PublicationRefused(STATE_INVALID, f"{where}: el push no es un objeto")
        if production is not None and not isinstance(production, Mapping):
            raise PublicationRefused(STATE_INVALID, f"{where}: la producción no es un objeto")
        if authority is not None and not isinstance(authority, Mapping):
            raise PublicationRefused(STATE_INVALID, f"{where}: la autoridad no es un objeto")
        return cls(
            task_id=_state_field(data, "task_id", str, where),
            request_id=_state_field(data, "request_id", str, where),
            target_id=_state_field(data, "target_id", str, where),
            commit_sha=_state_field(data, "commit_sha", str, where),
            approval_id=_state_field(data, "approval_id", str, where),
            stage=PublicationStage(stage_raw),
            error_kind=_state_field(data, "error_kind", str, where),
            error=_state_field(data, "error", str, where),
            push=PushEvidence.from_dict(push) if push is not None else None,
            production=(
                ProductionEvidence.from_dict(production) if production is not None else None
            ),
            history=_history_entries(data.get("history"), where),
            authority=dict(authority) if authority is not None else None,
        )


@dataclass(frozen=True, slots=True)
class PushPlan:
    """Plan mínimo del push: un remoto, una rama y un commit concreto."""

    remote: str
    branch: str
    sha: str

    @property
    def ref(self) -> str:
        """Referencia de destino completa."""
        return f"refs/heads/{self.branch}"

    @property
    def refspec(self) -> str:
        """Refspec explícito: el sha aprobado a la rama de producción, y nada más."""
        return f"{self.sha}:{self.ref}"

    def validate(self) -> None:
        """Comprueba la forma del plan antes de construir ningún comando.

        La lista blanca es la que impide que un remoto o una rama se conviertan en un argumento de
        Git: aquí no hay ``--force``, ni tags, ni espejo, ni borrados, ni comodines. El remoto sí
        admite espacios, porque una ruta de repositorio real los tiene: viaja como un
        elemento propio del ``argv``, sin shell de por medio.

        Raises:
            PublicationRefused: si el remoto, la rama o el sha no tienen la forma esperada.
        """
        if not self._remote_valido():
            raise PublicationRefused("PUSH_PLAN_INVALID", f"remoto inválido: {self.remote!r}")
        if not _BRANCH_PATTERN.match(self.branch) or self.branch.endswith("/"):
            raise PublicationRefused("PUSH_PLAN_INVALID", f"rama inválida: {self.branch!r}")
        if ".." in self.branch or "*" in self.branch:
            raise PublicationRefused("PUSH_PLAN_INVALID", "la rama no admite comodines")
        if not _SHA_PATTERN.match(self.sha):
            raise PublicationRefused("PUSH_PLAN_INVALID", "el commit a publicar debe ser un sha")
        for option in _FORBIDDEN_PUSH_OPTIONS:
            if self.remote == option or self.branch == option:
                raise PublicationRefused(
                    "PUSH_PLAN_INVALID", "el plan contiene una opción prohibida"
                )

    def _remote_valido(self) -> bool:
        """True si el remoto es una ruta, un nombre o una URL y no un argumento de Git."""
        remote = self.remote
        if not remote or len(remote) > _MAX_REMOTE_CHARS:
            return False
        if remote.startswith("-"):
            return False
        return not any(character in remote for character in "\r\n\t\x00")

    def argv(self) -> tuple[str, ...]:
        """``argv`` exacto del push: sin opciones, sin comodines, sin ambigüedad."""
        self.validate()
        return ("git", "push", "--porcelain", self.remote, self.refspec)


def _is_local_remote(remote: str) -> bool:
    """True si el remoto es local (ruta, ``file://``) o apunta a la propia máquina.

    Un remoto con esquema (``https:``, ``ssh:``, ``git:``) **nunca** es local, aunque llegue con una
    sola barra o con barras invertidas: confundirlo con una ruta autorizaría un push remoto sin
    autorización del operador. Solo ``localhost``/``127.0.0.1``/``[::1]`` cuentan como locales.
    """
    normalized = remote.replace("\\", "/").strip()
    if normalized.lower().startswith("file://"):
        return True
    if _SCHEME_PATTERN.match(normalized):
        host = normalized.split(":", 1)[1].lstrip("/").split("/", 1)[0].split("@")[-1]
        return host.startswith(("localhost", "127.0.0.1", "[::1]"))
    return True


class GitPublisher:
    """Push gobernado de **un** commit a **una** rama de **un** remoto.

    No es el shell del motor: es una capacidad de primera parte con ``argv`` construido por lista
    blanca, la misma idea que ``GovernedRepository`` para las operaciones locales. La política de
    shell sigue prohibiendo ``git push`` para los flujos no confiables; esto no la toca.
    """

    def __init__(
        self,
        root: Path,
        *,
        timeout_seconds: float = 180.0,
        runner: Callable[[tuple[str, ...], Path, float], tuple[int, str]] | None = None,
    ) -> None:
        self._root = Path(root)
        self._timeout = timeout_seconds
        self._runner = runner or _default_git_runner

    @property
    def root(self) -> Path:
        """Repositorio sobre el que se publica."""
        return self._root

    def has_commit(self, sha: str) -> bool:
        """True si el commit existe en el repositorio local."""
        code, _ = self._runner(
            ("git", "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"),
            self._root,
            self._timeout,
        )
        return code == 0

    def push(self, plan: PushPlan, *, allow_remote: bool = False) -> PushEvidence:
        """Ejecuta el push y devuelve la evidencia, sin declarar nada sobre producción.

        Raises:
            PublicationRefused: si el plan no es válido, si el commit no existe o si el remoto no es
                local y el operador no ha autorizado explícitamente el push remoto.
        """
        argv = plan.argv()
        if not _is_local_remote(plan.remote) and not allow_remote:
            raise PublicationRefused(
                "REMOTE_PUSH_NOT_AUTHORIZED",
                "el remoto no es local y el push remoto no está autorizado por el operador "
                "(PUNTO_PRODUCTION_PUSH=1)",
            )
        if not self.has_commit(plan.sha):
            raise PublicationRefused(
                "COMMIT_NOT_FOUND", f"el commit {plan.sha[:12]}… no existe en el repositorio local"
            )
        code, output = self._runner(argv, self._root, self._timeout)
        return PushEvidence(
            remote=plan.remote,
            ref=plan.ref,
            sha=plan.sha,
            argv=argv,
            exit_code=code,
            output=output,
            pushed=code == 0,
        )


def _default_git_runner(
    argv: tuple[str, ...], root: Path, timeout: float
) -> tuple[int, str]:
    """Ejecuta Git en el repositorio con el entorno **saneado** del motor.

    El proceso hijo **no hereda** el entorno del host: se construye con
    ``build_sanitized_environment`` (lista blanca de variables de sistema, ``PATH`` reconstruido y
    ``TEMP``/``TMP`` redirigidos), la misma frontera que usa el resto de PUNTO. Así el push no
    recibe credenciales del puesto de trabajo, ni un token de Vercel, ni la cadena de conexión de
    la base de datos. El prompt interactivo se desactiva para que un remoto sin credenciales falle
    rápido en vez de quedarse esperando.
    """
    from punto.tools.shell_policy import build_controlled_path, build_sanitized_environment

    git_dir: Path | None = None
    found = shutil.which("git")
    if found:
        git_dir = Path(found).parent
    temporary = Path(tempfile.mkdtemp(prefix="punto-push-"))
    try:
        env = build_sanitized_environment(controlled_temp=temporary, executable_dir=git_dir)
        env["PATH"] = build_controlled_path(git_dir)
        env["GIT_TERMINAL_PROMPT"] = "0"
        completed = subprocess.run(
            list(argv),
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # git ausente o sin respuesta
        return 1, f"no se pudo ejecutar git: {exc}"
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return completed.returncode, f"{completed.stdout}\n{completed.stderr}".strip()


def default_fetch(url: str, timeout: float) -> tuple[int, str]:
    """GET acotado con la biblioteca estándar: sin dependencias nuevas.

    Returns:
        ``(status_code, cuerpo)``; ``(0, motivo)`` si no se pudo medir.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "punto-production-probe/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_BODY_CHARS).decode("utf-8", errors="replace")
            return int(response.status), body
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_BODY_CHARS).decode("utf-8", errors="replace")
        except Exception:  # la respuesta no se pudo leer: el código sigue siendo evidencia
            body = ""
        return int(exc.code), body
    except Exception as exc:
        return 0, f"sin respuesta: {exc}"


class ProductionProbe:
    """Comprueba que producción sirve lo que se esperaba, con reintentos acotados.

    Un despliegue tarda: por eso hay varios intentos y una espera entre ellos. Lo que **no** hay es
    optimismo: si la respuesta no es 200 o el marcador esperado no aparece, producción no está
    validada.
    """

    def __init__(
        self,
        *,
        url: str,
        marker: str = "",
        attempts: int = 6,
        delay_seconds: float = 5.0,
        timeout_seconds: float = 15.0,
        fetch: Callable[[str, float], tuple[int, str]] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._url = url
        self._marker = marker
        self._attempts = max(1, attempts)
        self._delay = delay_seconds
        self._timeout = timeout_seconds
        self._fetch = fetch or default_fetch
        self._sleep = sleeper or time.sleep

    def check(self) -> ProductionEvidence:
        """Comprueba producción y devuelve la evidencia de lo observado."""
        status = 0
        marker_found = False
        detail = ""
        attempts = 0
        for index in range(self._attempts):
            attempts = index + 1
            status, body = self._fetch(self._url, self._timeout)
            marker_found = bool(self._marker) and self._marker in body
            detail = (body or "")[:300]
            if status == 200 and (not self._marker or marker_found):
                break
            if attempts < self._attempts:
                self._sleep(self._delay)
        return ProductionEvidence(
            url=self._url,
            marker=self._marker,
            attempts=attempts,
            status_code=status,
            marker_found=marker_found,
            detail=detail,
        )


class PublicationService:
    """Ejecuta la publicación con autoridad demostrada: Human Gate o sobre persistente del destino.

    AP000-R01: hay **dos** formas legítimas de autorizar la publicación y ambas son explícitas:

    - un ``HumanGate`` aprobado para **esa** operación (la vía de siempre, ``assert_executable``);
    - una decisión ``AUTO`` de :mod:`punto.policy.target_authority`, que exige autoridad persistente
      declarada por el destino y todas las condiciones verificables demostradas.

    Sin una de las dos, ``publish`` no sigue. No hay tercera vía.
    """

    def __init__(
        self,
        *,
        target_id: str,
        repository: Path,
        branch: str,
        url: str,
        remote: str = "origin",
        marker: str = "",
        publisher: GitPublisher | None = None,
        probe: ProductionProbe | None = None,
        allow_remote_push: bool = False,
        audit: Any | None = None,
        actor: str = "punto-console",
    ) -> None:
        self._target_id = target_id
        self._repository = Path(repository)
        self._branch = branch
        self._url = url
        self._remote_name = remote or "origin"
        self._marker = marker
        self._publisher = publisher or GitPublisher(self._repository)
        self._probe = probe or ProductionProbe(url=url, marker=marker)
        self._allow_remote_push = allow_remote_push
        self._audit = audit
        self._actor = actor

    @property
    def publishable(self) -> bool:
        """True si el destino declara rama y URL de producción."""
        return bool(self._branch and self._url)

    def publish(
        self,
        *,
        task_id: UUID | str,
        request_id: str,
        commit_sha: str,
        approval_id: UUID | None = None,
        gate: Any = None,
        record: PublicationRecord | None = None,
        authority: Any = None,
    ) -> PublicationRecord:
        """Publica el commit autorizado y comprueba producción.

        Args:
            task_id: Tarea humana cuyo resultado se publica.
            request_id: Solicitud de desarrollo que produjo el commit.
            commit_sha: Commit local ya validado que se integra en la rama de producción.
            approval_id: Human Gate de publicación (vía humana).
            gate: ``HumanGate`` real (el único emisor de aprobaciones humanas).
            record: Expediente ya abierto para la tarea (conserva su historial de etapas).
            authority: Decisión ``AUTO`` del sobre persistente del destino (AP000-R01). Si no hay
                aprobación humana, esta es la **única** otra autorización admitida.

        Raises:
            HumanGateNotApprovedError: si no hay aprobación humana válida para **esa** operación y
                tampoco una decisión de autoridad persistente en estado ``AUTO``. Es el mismo error
                del motor, para que no exista una segunda noción de autorización humana.
            PublicationRefused: si el destino no es publicable o el plan de push no es válido.
        """
        record = record or PublicationRecord(
            task_id=str(task_id),
            request_id=request_id,
            target_id=self._target_id,
            commit_sha=commit_sha,
            approval_id="" if approval_id is None else str(approval_id),
        )
        autonomous = authority is not None and bool(getattr(authority, "autonomous", False))
        if not autonomous:
            # La autorización humana se comprueba con el punto único de parada del motor: sin gate
            # aprobado y sin sobre persistente que lo sustituya, esta función no sigue.
            if gate is None:
                record.error_kind = "HUMAN_GATE_REQUIRED"
                record.error = (
                    "sin Human Gate aprobado y sin autoridad persistente del destino: no se publica"
                )
                record.advance(PublicationStage.PUBLICATION_FAILED, record.error)
                return record
            gate.assert_executable(approval_id)
        record.authority = authority.as_dict() if authority is not None else None
        if not self.publishable:
            record.error_kind = "TARGET_NOT_PUBLISHABLE"
            record.error = (
                "el destino no declara rama y URL de producción: PUNTO no adivina dónde vive "
                "producción"
            )
            record.advance(
                PublicationStage.BLOCKED_NOT_PUBLISHABLE, record.error
            )
            return record

        plan = PushPlan(remote=self._remote_name, branch=self._branch, sha=commit_sha)
        record.advance(PublicationStage.PUBLISHING, f"push a {plan.ref}")
        if autonomous:
            # Evidencia de la decisión que ampara la publicación: quién la autorizó (el sobre
            # persistente del destino) y con qué condiciones demostradas.
            self._log(
                "release_authorized",
                record,
                {
                    "target_id": self._target_id,
                    "disposition": authority.disposition,
                    "conditions": [
                        item["name"]
                        for item in authority.as_dict().get("conditions", [])
                        if item.get("state") == "SATISFIED"
                    ],
                    "policy_decision_id": authority.policy_decision_id,
                },
            )
        self._log(
            "publication_started",
            record,
            {
                "ref": plan.ref,
                "sha": commit_sha[:12],
                "authorized_by": "target-authority-envelope" if autonomous else "human-gate",
            },
        )
        try:
            evidence = self._publisher.push(plan, allow_remote=self._allow_remote_push)
        except PublicationRefused as refused:
            record.error_kind = refused.kind
            record.error = refused.detail
            record.advance(PublicationStage.PUBLICATION_FAILED, refused.detail)
            self._log("publication_failed", record, {"kind": refused.kind}, failed=True)
            return record
        record.push = evidence
        if not evidence.pushed:
            record.error_kind = "PUSH_FAILED"
            record.error = "el push no terminó bien: la rama de producción no cambió"
            record.advance(PublicationStage.PUBLICATION_FAILED, evidence.output[:300])
            self._log("publication_failed", record, {"kind": "PUSH_FAILED"}, failed=True)
            return record
        self._log("publication_pushed", record, {"ref": plan.ref, "sha": commit_sha[:12]})

        record.advance(PublicationStage.DEPLOYMENT_VERIFICATION, f"comprobando {self._url}")
        production = self._probe.check()
        record.production = production
        if not production.validated:
            record.error_kind = "DEPLOYMENT_NOT_VERIFIED"
            record.error = (
                f"producción respondió {production.status_code} y el marcador esperado "
                f"{'no apareció' if self._marker else 'no se comprobó'}"
            )
            record.advance(PublicationStage.DEPLOYMENT_NOT_VERIFIED, record.error)
            self._log(
                "production_not_verified", record, production.as_dict(), failed=True
            )
            return record
        record.advance(PublicationStage.PRODUCTION_VALIDATED, "producción comprobada")
        self._log("production_verified", record, production.as_dict())
        return record

    def _log(
        self,
        action: str,
        record: PublicationRecord,
        metadata: dict[str, Any],
        *,
        failed: bool = False,
    ) -> None:
        """Registra la transición en la auditoría del motor, si hay una."""
        if self._audit is None:
            return
        from punto.schemas.audit import AuditEventType
        from punto.schemas.enums import AuditResult

        event = {
            "publication_started": AuditEventType.PUBLICATION_REQUESTED,
            "publication_pushed": AuditEventType.PUBLICATION_PUSHED,
            "publication_failed": AuditEventType.PUBLICATION_FAILED,
            "production_verified": AuditEventType.PRODUCTION_VERIFIED,
            "production_not_verified": AuditEventType.PRODUCTION_NOT_VERIFIED,
            "release_authorized": AuditEventType.AUTONOMOUS_RELEASE_AUTHORIZED,
        }[action]
        self._audit.log_dev_event(
            event,
            action,
            request_id=record.request_id,
            metadata={
                "task_id": record.task_id,
                "target_id": record.target_id,
                "approval_id": record.approval_id,
                "stage": record.stage.value,
                "payload": json.dumps(metadata, ensure_ascii=False)[:600],
            },
            result=AuditResult.FAILURE if failed else AuditResult.SUCCESS,
            actor=self._actor,
        )
