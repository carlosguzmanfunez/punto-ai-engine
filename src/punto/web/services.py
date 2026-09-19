"""Dependencias de servicio aisladas de una sesión de QA (PILOT-01R.1, alternativa C).

Una aplicación real necesita servicios: base de datos, caché, cola. La sesión de QA vive en una red
Podman ``--internal`` **sin salida**, así que el servicio que la aplicación necesita se levanta
**dentro** de esa red: efímero, aislado y gobernado por PUNTO. Nunca se le da Internet al código
bajo prueba y la credencial real del proyecto (Neon) nunca entra al sandbox.

Frontera de autoridad de esta capa
----------------------------------
- **PUNTO decide**: imagen (allowlist con digest fijado), alias interno, puerto, nombre del
  contenedor, usuario/contraseña/base efímeros, rol de aplicación sin privilegios, límites,
  tiempos y ciclo de vida completo.
- **El proyecto no decide nada**: no elige imagen, ni red, ni destino, ni variables de entorno, ni
  credenciales. Solo aporta **artefactos SQL** de su propio árbol (migración y seed), que PUNTO
  valida (ruta relativa, sin enlaces simbólicos, ``.sql`` regular, tamaño acotado), copia a un
  directorio efímero **fuera del workspace** y clasifica con su propio clasificador antes de
  ejecutarlos.
- **Credenciales efímeras**: generadas por sesión con ``secrets``, nunca de Neon, nunca persistidas,
  nunca registradas ni escritas en evidencia; sólo se publica una huella sha256.
- **Sin egress**: red interna de la sesión, sin puertos publicados, sin ``--privileged``, sin
  ``NET_ADMIN``, sin volúmenes persistentes (los datos viven en ``tmpfs``).
- **Sin canal de entorno genérico**: la preview recibe exactamente ``DATABASE_URL``, y su valor lo
  construye PUNTO apuntando al alias interno de esta sesión.

Lo que **no** hace este módulo: decidir si la aplicación funciona. Eso lo decide el QA Consumer
sobre lo que observe el navegador.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import secrets
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import quote

from punto.providers.secrets import redact_secret_text
from punto.tools.database import SqlClassification, classify_sql

# ---------------------------------------------------------------------------
# Configuración confiable (allowlist de PUNTO: no viene del proyecto)
# ---------------------------------------------------------------------------
#: Imagen del servicio PostgreSQL, **fijada por digest** (la etiqueta solo se usa para leerla).
#: Una etiqueta flotante permitiría que el contenido cambiara sin que PUNTO se enterase.
POSTGRES_IMAGE_REFERENCE: Final[str] = (
    "docker.io/library/postgres@sha256:"
    "5d706d5aa3ad311722f373fdc09e6a22b454bec70d34d9786383983fd83c5e46"
)
POSTGRES_IMAGE_LABEL: Final[str] = "postgres:17.2-alpine"

#: Allowlist de imágenes de servicio. El proyecto no puede proponer ninguna otra.
ALLOWED_SERVICE_IMAGES: Final[frozenset[str]] = frozenset({POSTGRES_IMAGE_REFERENCE})

#: Puerto interno del servicio. Nunca se publica al host.
SERVICE_PORT: Final[int] = 5432

#: Prefijos de alias y contenedor, para poder auditar y limpiar por sesión.
SERVICE_ALIAS_PREFIX: Final[str] = "punto-svc-"
SERVICE_CONTAINER_PREFIX: Final[str] = "punto-qa-svc-"

#: Rutas y tamaños de las zonas efímeras del contenedor: los datos **no** sobreviven a la sesión.
DATA_DIR: Final[str] = "/var/lib/postgresql/data"
RUN_DIR: Final[str] = "/var/run/postgresql"
DATA_SIZE: Final[str] = "512m"
RUN_SIZE: Final[str] = "16m"
TMP_SIZE: Final[str] = "64m"

#: Capacidades mínimas que necesita el entrypoint oficial para inicializar el clúster y **soltar**
#: privilegios. No se concede ninguna capacidad de red ni de administración de contenedores: sin
#: ``CHOWN`` el ``initdb`` falla, y con ``NET_ADMIN`` el servicio podría tocar la red de la sesión.
SERVICE_CAPABILITIES: Final[str] = "CHOWN,FOWNER,DAC_OVERRIDE,SETUID,SETGID"

#: Única variable que PUNTO inyecta en la preview. No hay canal genérico de entorno.
#:
#: ``DATABASE_URL`` es el DSN efímero (apunta al alias interno de esta sesión) y
#: ``PUNTO_QA_DATABASE_TRANSPORT`` es la **señal explícita** con la que el proyecto elige su
#: transporte TCP: sin ella, una aplicación que hable Neon por HTTP no puede alcanzar una base
#: PostgreSQL normal, y con ella queda claro que el modo alternativo lo pidió PUNTO, no el proyecto.
ALLOWED_PREVIEW_ENVIRONMENT: Final[tuple[str, ...]] = (
    "DATABASE_URL",
    "PUNTO_QA_DATABASE_TRANSPORT",
)

#: Nombre y valor de la señal de transporte que PUNTO inyecta cuando provee el servicio.
#: El valor pertenece al vocabulario cerrado del proyecto (``neon-http`` / ``postgres-tcp``) y aquí
#: solo se emite el que corresponde a un PostgreSQL efímero por TCP: PUNTO no acepta del proyecto
#: ninguna propuesta de transporte.
QA_TRANSPORT_ENV_VAR: Final[str] = "PUNTO_QA_DATABASE_TRANSPORT"
QA_TRANSPORT_VALUE: Final[str] = "postgres-tcp"

#: Ficheros de configuración local que **no** pueden entrar al sandbox de QA: llevan credenciales
#: reales del proyecto (por ejemplo el DSN de Neon).
LOCAL_SECRET_FILES: Final[tuple[str, ...]] = (".env", ".env.local")
LOCAL_SECRET_EXCEPTIONS: Final[tuple[str, ...]] = (".env.example",)
#: Directorios que no se recorren al buscar credenciales locales (ruido y tamaño).
LOCAL_SECRET_SKIP_DIRS: Final[tuple[str, ...]] = (".git", "node_modules", ".next", ".venv")

#: Tiempos por defecto.
DEFAULT_READINESS_TIMEOUT_SECONDS: Final[float] = 120.0
DEFAULT_PREPARE_TIMEOUT_SECONDS: Final[float] = 240.0

#: Límites por defecto del servicio.
DEFAULT_MEMORY: Final[str] = "512m"
DEFAULT_CPUS: Final[str] = "1"
DEFAULT_PIDS: Final[int] = 128

#: Tamaño máximo de un artefacto SQL autorizado y número máximo de artefactos por sesión.
MAX_ARTIFACT_BYTES: Final[int] = 2_000_000
MAX_ARTIFACTS: Final[int] = 8

#: Clasificaciones admitidas al preparar la base de QA: solo DDL aditivo y seed idempotente.
ALLOWED_PREPARATION_CLASSES: Final[frozenset[SqlClassification]] = frozenset(
    {SqlClassification.SAFE_DDL, SqlClassification.SAFE_SEED}
)

#: Ruta donde el contenedor del servicio ve los artefactos copiados (solo lectura).
ARTIFACT_MOUNT: Final[str] = "/punto/artifacts"

#: Etiquetas de PUNTO para poder limpiar el servicio aunque el proceso muriera.
SERVICE_LABEL: Final[str] = "punto.qa.service=postgresql"


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------
class QaServiceError(RuntimeError):
    """Base de los fallos de una dependencia de servicio de QA."""


class QaServicePolicyError(QaServiceError):
    """La petición de servicio viola la política: imagen, artefacto, secreto o entorno."""


class QaServiceUnavailableError(QaServiceError):
    """El servicio no arrancó o no estuvo listo a tiempo."""


class QaServicePreparationError(QaServiceError):
    """La preparación de la base (rol, migración o seed) falló: la sesión de QA falla."""


# ---------------------------------------------------------------------------
# Credenciales efímeras
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class QaPostgresCredentials:
    """Credenciales de la sesión: generadas por PUNTO, efímeras y nunca persistidas.

    Hay dos roles y la separación importa: el **administrador** del servicio (``admin_user``) solo
    lo usa PUNTO para crear el rol de trabajo y la base; la **aplicación** (``user``) es la que
    recibe la preview y **no** es superusuario, así que no puede usar ``COPY … PROGRAM``,
    ``pg_read_file`` ni el resto de funciones de administración.
    """

    user: str
    password: str
    database: str
    admin_user: str
    admin_password: str

    def dsn(
        self, host: str, *, port: int = SERVICE_PORT, user: str = "", password: str = ""
    ) -> str:
        """DSN hacia el alias **interno** del servicio, con cada componente codificado como URI.

        Codificar cada componente evita que una contraseña con caracteres especiales rompa el DSN o
        permita inyección semántica en la cadena de conexión (``@``, ``/``, ``?``, ``#``).
        """
        chosen_user = quote(user or self.user, safe="")
        chosen_password = quote(password or self.password, safe="")
        database = quote(self.database, safe="")
        return f"postgresql://{chosen_user}:{chosen_password}@{host}:{port}/{database}"

    def fingerprint(self) -> str:
        """Huella de la credencial para la evidencia: permite comparar sesiones sin publicarla."""
        material = f"{self.user}:{self.database}:{self.password}".encode()
        return hashlib.sha256(material).hexdigest()[:16]


def generate_credentials() -> QaPostgresCredentials:
    """Genera credenciales nuevas por sesión con entropía del sistema (``secrets``)."""
    suffix = secrets.token_hex(6)
    return QaPostgresCredentials(
        user=f"qa_app_{suffix}",
        password=secrets.token_urlsafe(24),
        database=f"qa_db_{suffix}",
        admin_user=f"qa_root_{suffix}",
        admin_password=secrets.token_urlsafe(24),
    )


# ---------------------------------------------------------------------------
# Especificación (lo que declara la sesión; todo lo demás lo decide PUNTO)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class QaPostgresSpec:
    """PostgreSQL efímero dentro de la red interna de la sesión.

    ``artifacts`` son rutas **relativas al proyecto** dentro del workspace (migración y seed). Son
    la única aportación del proyecto; se validan, se copian fuera del workspace y se clasifican
    antes de ejecutarse.
    """

    artifacts: tuple[str, ...] = ()
    image: str = POSTGRES_IMAGE_REFERENCE
    port: int = SERVICE_PORT
    readiness_timeout_seconds: float = DEFAULT_READINESS_TIMEOUT_SECONDS
    prepare_timeout_seconds: float = DEFAULT_PREPARE_TIMEOUT_SECONDS
    memory: str = DEFAULT_MEMORY
    cpus: str = DEFAULT_CPUS
    pids: int = DEFAULT_PIDS

    def __post_init__(self) -> None:
        """Valida la petición contra la política de PUNTO. Una petición inválida no se ejecuta.

        Raises:
            QaServicePolicyError: si la imagen no está en la allowlist, el puerto no es el interno,
                o los tiempos, los pids o los artefactos no son utilizables.
        """
        if self.image not in ALLOWED_SERVICE_IMAGES:
            raise QaServicePolicyError(
                f"imagen de servicio no autorizada: {self.image!r}. Autorizadas: "
                f"{', '.join(sorted(ALLOWED_SERVICE_IMAGES))}. La imagen la elige PUNTO, no el "
                "proyecto"
            )
        if self.port != SERVICE_PORT:
            raise QaServicePolicyError(
                f"el servicio solo se expone en su puerto interno ({SERVICE_PORT}); se pidió "
                f"{self.port}"
            )
        if not self.readiness_timeout_seconds > 0 or not self.prepare_timeout_seconds > 0:
            raise QaServicePolicyError("los tiempos de readiness y preparación deben ser positivos")
        if self.pids <= 0:
            raise QaServicePolicyError("pids debe ser positivo")
        if len(self.artifacts) > MAX_ARTIFACTS:
            raise QaServicePolicyError(
                f"demasiados artefactos de preparación: {len(self.artifacts)} > {MAX_ARTIFACTS}"
            )
        for artifact in self.artifacts:
            if not artifact.strip():
                raise QaServicePolicyError("un artefacto de preparación no puede estar vacío")

    def as_public_dict(self) -> dict[str, Any]:
        """Vista publicable de la petición: nunca incluye credenciales."""
        return {
            "type": "postgresql",
            "lifecycle": "ephemeral",
            "image": POSTGRES_IMAGE_LABEL,
            "image_reference": POSTGRES_IMAGE_REFERENCE,
            "port": self.port,
            "artifacts": list(self.artifacts),
            "external_egress": False,
            "memory": self.memory,
            "cpus": self.cpus,
            "pids": self.pids,
        }


# ---------------------------------------------------------------------------
# Guardarraíles del workspace
# ---------------------------------------------------------------------------
def find_local_secrets(workspace: Path) -> tuple[Path, ...]:
    """Ficheros de credenciales locales presentes en el workspace (vacío es lo correcto)."""
    root = Path(workspace)
    found: list[Path] = []
    for candidate in sorted(root.rglob(".env*")):
        if any(part in LOCAL_SECRET_SKIP_DIRS for part in candidate.parts):
            continue
        if not candidate.is_file():
            continue
        if candidate.name in LOCAL_SECRET_EXCEPTIONS:
            continue
        found.append(candidate)
    return tuple(found)


def assert_no_local_secrets(workspace: Path) -> None:
    """Rechaza un workspace de QA que traiga credenciales locales del proyecto.

    El contenedor de la preview recibe un ``DATABASE_URL`` **efímero** construido por PUNTO. Si el
    proyecto trajera su ``.env.local`` (con el DSN real de Neon) al sandbox, el código bajo prueba
    tendría la credencial real: eso es exactamente lo que esta frontera impide.

    Raises:
        QaServicePolicyError: si aparece cualquier fichero ``.env``/``.env.*`` (salvo plantillas).
    """
    found = find_local_secrets(workspace)
    if found:
        names = ", ".join(sorted(str(item.name) for item in found))
        raise QaServicePolicyError(
            f"el workspace de QA contiene ficheros de credenciales locales ({names}): la "
            "credencial real del proyecto nunca entra al sandbox. Prepara un workspace saneado."
        )


# ---------------------------------------------------------------------------
# Artefactos de preparación
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PreparationArtifact:
    """Artefacto SQL autorizado: origen, nombre efímero, huella y número de sentencias."""

    source: Path
    name: str
    sha256: str
    statements: int


def authorize_artifacts(
    workspace: Path, project_relative: Path | str, spec: QaPostgresSpec
) -> tuple[PreparationArtifact, ...]:
    """Valida y clasifica los artefactos de preparación declarados por la sesión.

    Comprobaciones (fail closed): la ruta es relativa y se queda dentro del proyecto; no es un
    enlace simbólico; es un ``.sql`` regular de tamaño acotado dentro del workspace; y **todas**
    sus sentencias son DDL aditivo o seed idempotente según el clasificador de PUNTO. Cualquier
    otra cosa (``COPY … PROGRAM``, ``setval``, sentencias desconocidas o destructivas) rechaza la
    sesión.

    Raises:
        QaServicePolicyError: si un artefacto no es autorizable.
    """
    base = (Path(workspace) / str(project_relative)).resolve()
    artifacts: list[PreparationArtifact] = []
    for declared in spec.artifacts:
        text = declared.strip().replace("\\", "/")
        if text.startswith("/") or re.match(r"^[A-Za-z]:", text) or ".." in Path(text).parts:
            raise QaServicePolicyError(f"artefacto de preparación fuera del proyecto: {declared!r}")
        candidate = base / text
        if candidate.is_symlink():
            raise QaServicePolicyError(
                f"el artefacto de preparación no puede ser un enlace simbólico: {declared!r}"
            )
        resolved = candidate.resolve()
        if not resolved.is_file() or base not in resolved.parents:
            raise QaServicePolicyError(
                f"el artefacto de preparación no existe dentro del proyecto: {declared!r}"
            )
        if resolved.suffix.lower() != ".sql":
            raise QaServicePolicyError(
                f"solo se autorizan artefactos .sql para preparar la base: {declared!r}"
            )
        size = resolved.stat().st_size
        if size <= 0 or size > MAX_ARTIFACT_BYTES:
            raise QaServicePolicyError(
                f"tamaño de artefacto no autorizado ({size} bytes): {declared!r}"
            )
        sql = resolved.read_text(encoding="utf-8")
        plan = classify_sql(sql)
        rejected = [
            item
            for item in plan.statements
            if item.classification not in ALLOWED_PREPARATION_CLASSES
        ]
        if rejected or plan.total == 0:
            detail = (
                ", ".join(
                    f"{item.classification.value} ({item.reason[:120]})" for item in rejected[:5]
                )
                or "sin sentencias"
            )
            raise QaServicePolicyError(
                f"el artefacto {declared!r} no es solo DDL aditivo y seed idempotente: {detail}"
            )
        artifacts.append(
            PreparationArtifact(
                source=resolved,
                name=f"{len(artifacts):02d}-{resolved.name}",
                sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                statements=plan.total,
            )
        )
    return tuple(artifacts)


def stage_artifacts(root: Path, artifacts: Sequence[PreparationArtifact]) -> Path:
    """Copia los artefactos autorizados a un directorio efímero y devuelve ese directorio.

    Al contenedor del servicio se le monta **solo** ese directorio, en solo lectura: nunca el
    workspace completo del proyecto (que puede contener secretos y ficheros que no hacen falta). El
    directorio lo crea y lo elimina quien llama, que es quien conoce su ciclo de vida.
    """
    root.mkdir(parents=True, exist_ok=True)
    for artifact in artifacts:
        shutil.copyfile(artifact.source, root / artifact.name)
    return root


# ---------------------------------------------------------------------------
# Runtime (inyectable: el backend web presta su CLI de OCI)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ServiceProcess:
    """Resultado de una orden del runtime, ya saneado."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class ServiceRuntime(Protocol):
    """Ejecuta la CLI del runtime OCI para el ciclo de vida del servicio."""

    def run(self, arguments: Sequence[str], *, timeout: float) -> ServiceProcess:
        """Ejecuta la orden y devuelve su resultado."""
        ...  # pragma: no cover - protocolo


# ---------------------------------------------------------------------------
# Servicio efímero
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class EphemeralPostgres:
    """PostgreSQL efímero dentro de la red interna de una sesión de QA."""

    runtime: ServiceRuntime
    workspace: Path
    project_relative: Path | str
    network: str
    alias: str
    container: str
    spec: QaPostgresSpec = field(default_factory=QaPostgresSpec)
    credentials: QaPostgresCredentials = field(default_factory=generate_credentials)
    artifacts: tuple[PreparationArtifact, ...] = field(default=(), init=False)
    started: bool = field(default=False, init=False)
    ready: bool = field(default=False, init=False)

    # ------------------------------------------------------------------ estado
    @property
    def preview_dsn(self) -> str:
        """DSN que recibe la preview: apunta **solo** al alias interno de esta sesión."""
        return self.credentials.dsn(self.alias, port=self.spec.port)

    @property
    def admin_dsn(self) -> str:
        """DSN del administrador del servicio (solo lo usa PUNTO, dentro del contenedor)."""
        return self.credentials.dsn(
            self.alias,
            port=self.spec.port,
            user=self.credentials.admin_user,
            password=self.credentials.admin_password,
        )

    def preview_environment(self) -> dict[str, str]:
        """Entorno que PUNTO inyecta en la preview: el DSN efímero y la señal de transporte.

        No hay canal genérico: son exactamente las dos claves de
        :data:`ALLOWED_PREVIEW_ENVIRONMENT`, y la señal la fija PUNTO con el valor de
        :data:`QA_TRANSPORT_VALUE`.
        """
        return {
            "DATABASE_URL": self.preview_dsn,
            QA_TRANSPORT_ENV_VAR: QA_TRANSPORT_VALUE,
        }

    def as_public_dict(self) -> dict[str, Any]:
        """Evidencia del servicio: sin credenciales, con la huella de la sesión."""
        return {
            "type": "postgresql",
            "lifecycle": "ephemeral",
            "container": self.container,
            "alias": self.alias,
            "port": self.spec.port,
            "image": POSTGRES_IMAGE_LABEL,
            "image_reference": self.spec.image,
            "network_isolated": True,
            "external_egress": False,
            "credential_fingerprint": self.credentials.fingerprint(),
            "artifacts": [
                {"name": item.name, "sha256": item.sha256, "statements": item.statements}
                for item in self.artifacts
            ],
            "roles": {
                "preview": self.credentials.user,
                "admin": self.credentials.admin_user,
                "preview_is_superuser": False,
            },
        }

    # ------------------------------------------------------------------ plan
    def plan(self) -> tuple[PreparationArtifact, ...]:
        """Autoriza los artefactos de preparación y los recuerda para la evidencia."""
        self.artifacts = authorize_artifacts(self.workspace, self.project_relative, self.spec)
        return self.artifacts

    # ------------------------------------------------------------------ ciclo
    def start_arguments(self, artifacts_dir: Path) -> list[str]:
        """Argumentos del contenedor del servicio: efímero, aislado y sin puertos publicados.

        El sistema de archivos raíz es inmutable, las capacidades quedan vacías salvo las cinco
        que necesita el entrypoint para inicializar el clúster, los datos y los sockets viven en
        ``tmpfs`` (sin volúmenes persistentes), los límites son explícitos y no hay política de
        reinicio. A los artefactos de preparación se les monta **solo** su directorio efímero, en
        solo lectura.
        """
        return [
            "run",
            "-d",
            "--name",
            self.container,
            "--label",
            SERVICE_LABEL,
            # Red interna de la sesión: el servicio ve a la preview y no tiene ruta a Internet.
            "--network",
            self.network,
            "--network-alias",
            self.alias,
            # Raíz inmutable y capacidades mínimas.
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            SERVICE_CAPABILITIES,
            "--security-opt",
            "no-new-privileges",
            # Nada sobrevive a la sesión: datos, sockets y temporales en tmpfs.
            "--tmpfs",
            f"{DATA_DIR}:rw,size={DATA_SIZE},mode=0700",
            "--tmpfs",
            f"{RUN_DIR}:rw,size={RUN_SIZE},mode=0777",
            "--tmpfs",
            f"/tmp:rw,size={TMP_SIZE},mode=1777",
            # Límites explícitos y sin reintentos.
            "--memory",
            self.spec.memory,
            "--cpus",
            self.spec.cpus,
            "--pids-limit",
            str(self.spec.pids),
            "--restart=no",
            # Credenciales efímeras del **administrador** del servicio (solo viven aquí).
            "--env",
            f"POSTGRES_USER={self.credentials.admin_user}",
            "--env",
            f"POSTGRES_PASSWORD={self.credentials.admin_password}",
            "--env",
            f"POSTGRES_DB={self.credentials.database}",
            # Artefactos de preparación: solo el directorio efímero, en solo lectura.
            "-v",
            f"{artifacts_dir}:{ARTIFACT_MOUNT}:ro,Z",
            # Imagen de la allowlist, fijada por digest.
            self.spec.image,
        ]

    def start(self, artifacts_dir: Path) -> None:
        """Crea y arranca el contenedor del servicio en la red interna de la sesión.

        Raises:
            QaServiceUnavailableError: si el runtime no lo arranca.
        """
        result = self.runtime.run(self.start_arguments(artifacts_dir), timeout=180.0)
        if result.returncode != 0:
            raise QaServiceUnavailableError(
                "el servicio PostgreSQL efímero no arrancó: "
                f"{redact_secret_text(result.stderr or result.stdout) or 'sin salida'}"
            )
        self.started = True

    def wait_ready(self) -> None:
        """Espera a que el servicio acepte conexiones (``pg_isready`` dentro del contenedor).

        Raises:
            QaServiceUnavailableError: si no está listo dentro del tiempo declarado. El contenedor
                se destruye antes de propagar: un servicio a medio arrancar no se deja vivo.
        """
        deadline = time.monotonic() + self.spec.readiness_timeout_seconds
        last = ""
        while time.monotonic() < deadline:
            result = self.runtime.run(
                [
                    "exec",
                    self.container,
                    "pg_isready",
                    "-U",
                    self.credentials.admin_user,
                    "-d",
                    self.credentials.database,
                ],
                timeout=30.0,
            )
            if result.returncode == 0:
                self.ready = True
                return
            last = redact_secret_text(result.stderr or result.stdout)
            time.sleep(1.0)
        self.destroy()
        raise QaServiceUnavailableError(
            f"el servicio PostgreSQL no estuvo listo en "
            f"{self.spec.readiness_timeout_seconds:.0f}s: {last or 'sin salida'}"
        )

    def create_application_role(self) -> None:
        """Crea el rol de aplicación **sin privilegios** y su base, y lo deja como propietario.

        El entrypoint oficial crea un superusuario; ni la migración ni la preview deben usarlo. El
        rol de trabajo no es superusuario, no puede crear bases ni roles, no replica y no salta
        RLS: eso deja fuera de su alcance ``COPY … PROGRAM``, ``pg_read_file`` y el resto de
        funciones de administración.

        Raises:
            QaServicePreparationError: si el rol o la base no se pueden crear.
        """
        password = self.credentials.password.replace("'", "''")
        statements = (
            f"CREATE ROLE \"{self.credentials.user}\" LOGIN PASSWORD '{password}' NOSUPERUSER "
            "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS",
            f'CREATE DATABASE "{self.credentials.database}" OWNER "{self.credentials.user}"',
            f'ALTER DATABASE "{self.credentials.database}" OWNER TO "{self.credentials.user}"',
        )
        for statement in statements:
            result = self._psql_admin(statement)
            if result.returncode != 0:
                detail = redact_secret_text(result.stderr or result.stdout)
                # Idempotencia: si ya existe, el estado buscado ya está.
                if "already exists" in detail:
                    continue
                raise QaServicePreparationError(
                    f"no se pudo preparar el rol de aplicación del servicio: "
                    f"{detail or 'sin salida'}"
                )

    def prepare(self) -> None:
        """Ejecuta los artefactos SQL autorizados con el rol de aplicación.

        Raises:
            QaServicePreparationError: si la migración o el seed fallan. La sesión de QA no
                continúa y el resultado nunca se sustituye por datos simulados.
        """
        for artifact in self.artifacts:
            result = self.runtime.run(
                [
                    "exec",
                    self.container,
                    "psql",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "--no-psqlrc",
                    "-U",
                    self.credentials.user,
                    "-d",
                    self.credentials.database,
                    "-f",
                    f"{ARTIFACT_MOUNT}/{artifact.name}",
                ],
                timeout=self.spec.prepare_timeout_seconds,
            )
            if result.returncode != 0:
                raise QaServicePreparationError(
                    f"la preparación de la base de QA falló con {artifact.name!r}: "
                    f"{redact_secret_text(result.stderr or result.stdout) or 'sin salida'}"
                )

    def destroy(self) -> None:
        """Destruye el servicio y su contenido efímero. Idempotente y sin propagar errores."""
        with contextlib.suppress(Exception):
            self.runtime.run(["rm", "-f", "-t", "0", self.container], timeout=120.0)
        self.started = False
        self.ready = False

    def leftovers(self) -> tuple[str, ...]:
        """Contenedores del servicio que sigan vivos tras la limpieza (debe estar vacío)."""
        table = self.runtime.run(
            ["ps", "-a", "--filter", f"name={self.container}", "--format", "{{.Names}}"],
            timeout=60.0,
        )
        return tuple(
            line.strip()
            for line in (table.stdout or "").splitlines()
            if line.strip() == self.container
        )

    def _psql_admin(self, statement: str) -> ServiceProcess:
        """Ejecuta una sentencia como administrador del servicio, dentro del contenedor."""
        return self.runtime.run(
            [
                "exec",
                self.container,
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                self.credentials.admin_user,
                "-d",
                self.credentials.database,
                "-c",
                statement,
            ],
            timeout=self.spec.prepare_timeout_seconds,
        )


def service_cleanup_check(runtime: ServiceRuntime, container: str, network: str) -> dict[str, Any]:
    """Comprueba que no quedan el contenedor ni la red de la sesión (evidencia de limpieza)."""
    containers = runtime.run(
        ["ps", "-a", "--filter", f"name={container}", "--format", "{{.Names}}"], timeout=60.0
    )
    networks = runtime.run(["network", "ls", "--format", "{{.Name}}"], timeout=60.0)
    container_names = {line.strip() for line in (containers.stdout or "").splitlines()}
    network_names = {line.strip() for line in (networks.stdout or "").splitlines()}
    return {
        "container_removed": container not in container_names,
        "network_removed": network not in network_names,
    }


__all__ = [
    "ALLOWED_PREPARATION_CLASSES",
    "ALLOWED_PREVIEW_ENVIRONMENT",
    "ALLOWED_SERVICE_IMAGES",
    "ARTIFACT_MOUNT",
    "DATA_DIR",
    "DEFAULT_CPUS",
    "DEFAULT_MEMORY",
    "DEFAULT_PIDS",
    "DEFAULT_PREPARE_TIMEOUT_SECONDS",
    "DEFAULT_READINESS_TIMEOUT_SECONDS",
    "LOCAL_SECRET_EXCEPTIONS",
    "LOCAL_SECRET_FILES",
    "MAX_ARTIFACTS",
    "MAX_ARTIFACT_BYTES",
    "POSTGRES_IMAGE_LABEL",
    "POSTGRES_IMAGE_REFERENCE",
    "QA_TRANSPORT_ENV_VAR",
    "QA_TRANSPORT_VALUE",
    "SERVICE_ALIAS_PREFIX",
    "SERVICE_CAPABILITIES",
    "SERVICE_CONTAINER_PREFIX",
    "SERVICE_LABEL",
    "SERVICE_PORT",
    "EphemeralPostgres",
    "PreparationArtifact",
    "QaPostgresCredentials",
    "QaPostgresSpec",
    "QaServiceError",
    "QaServicePolicyError",
    "QaServicePreparationError",
    "QaServiceUnavailableError",
    "ServiceProcess",
    "ServiceRuntime",
    "assert_no_local_secrets",
    "authorize_artifacts",
    "find_local_secrets",
    "generate_credentials",
    "service_cleanup_check",
    "stage_artifacts",
]
