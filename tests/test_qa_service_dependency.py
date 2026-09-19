"""Dependencia de servicio efímera de QA (PILOT-01R.1, alternativa C).

Este archivo prueba la **frontera**, no el detalle: que el servicio PostgreSQL de una sesión de QA
se levante dentro de la red interna (sin egress), con credenciales efímeras que nunca son las del
proyecto, con la imagen de una allowlist fijada por digest, con artefactos SQL validados y
clasificados por PUNTO, y que se destruya sin dejar contenedores, redes ni credenciales.

Ninguna prueba necesita Podman: el ciclo de vida se ejecuta contra un runtime **inyectado** que
graba las órdenes. Lo que se comprueba es lo que PUNTO manda ejecutar y lo que decide con la
respuesta.

| Bloque | Regla |
| --- | --- |
| credenciales | por sesión, efímeras, nunca las del proyecto ni el DSN del proyecto |
| política | la imagen, el puerto y los límites los decide PUNTO; el proyecto no propone nada |
| artefactos | solo ``.sql`` del proyecto, sin enlaces, acotados, y solo DDL aditivo y seed |
| secretos | el workspace de QA no puede traer credenciales locales del proyecto |
| arranque | raíz inmutable, capacidades mínimas, tmpfs, límites y **sin** puertos publicados |
| ciclo | arrancar → readiness → rol sin privilegios → migración y seed → destruir |
| limpieza | no queda contenedor, ni red, ni credencial publicada |
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest

from punto.audit.logger import AuditLogger
from punto.providers.transport import REDACTED
from punto.schemas.audit import AuditEventType
from punto.web.sandbox import WebSandboxBackend
from punto.web.services import (
    ALLOWED_SERVICE_IMAGES,
    ARTIFACT_MOUNT,
    POSTGRES_IMAGE_LABEL,
    POSTGRES_IMAGE_REFERENCE,
    EphemeralPostgres,
    QaPostgresCredentials,
    QaPostgresSpec,
    QaServicePolicyError,
    QaServicePreparationError,
    QaServiceUnavailableError,
    ServiceProcess,
    assert_no_local_secrets,
    authorize_artifacts,
    find_local_secrets,
    generate_credentials,
    service_cleanup_check,
    stage_artifacts,
)

#: Migración mínima admitida: solo DDL aditivo.
MIGRATION_SQL = (
    'CREATE TABLE IF NOT EXISTS "properties" ("id" integer PRIMARY KEY, "slug" text NOT NULL);\n'
    'CREATE INDEX "properties_slug_idx" ON "properties" ("slug");\n'
)

#: Seed mínimo admitido: solo ``INSERT`` idempotente.
SEED_SQL = (
    'INSERT INTO "properties" ("id", "slug") VALUES (1, \'casa-1\') '
    'ON CONFLICT ("id") DO NOTHING;\n'
)

#: Red y contenedor de la sesión de prueba.
TEST_NETWORK = "punto-web-net-sesion"
TEST_ALIAS = "punto-svc-sesion"
TEST_CONTAINER = "punto-qa-svc-sesion"


# ---------------------------------------------------------------------------
# Runtime inyectado
# ---------------------------------------------------------------------------
def _ok(_arguments: list[str]) -> ServiceProcess:
    """Runtime que acepta todo: la sesión feliz."""
    return ServiceProcess(returncode=0, stdout="ok")


def _has(arguments: Sequence[str], token: str) -> bool:
    """True si algún argumento contiene ``token`` (cada sentencia SQL va como un argumento)."""
    return any(token in item for item in arguments)


class ScriptedRuntime:
    """Runtime OCI falso: graba las órdenes y responde con el guion que se le dé."""

    def __init__(self, respond: Callable[[list[str]], ServiceProcess] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._respond = respond or _ok

    def run(self, arguments: Sequence[str], *, timeout: float) -> ServiceProcess:
        """Graba la orden y devuelve la respuesta del guion."""
        del timeout
        call = [str(item) for item in arguments]
        self.calls.append(call)
        return self._respond(call)

    # -- consultas del guion -------------------------------------------------
    def verbs(self) -> list[str]:
        """Primer token de cada orden ejecutada, en orden."""
        return [call[0] for call in self.calls]

    def matching(self, *tokens: str) -> list[list[str]]:
        """Órdenes que contienen todos los tokens dados."""
        return [call for call in self.calls if all(_has(call, token) for token in tokens)]

    def only(self, *tokens: str) -> list[str]:
        """La única orden que contiene esos tokens.

        Raises:
            AssertionError: si no hay exactamente una.
        """
        found = self.matching(*tokens)
        assert len(found) == 1, f"se esperaba una sola orden {tokens}: {found}"
        return found[0]

    def script(self, *, psql_exit: int = 0, ready_after: int = 0) -> ScriptedRuntime:
        """Guion realista: readiness que tarda ``ready_after`` intentos y psql con su código."""
        state = {"ready": 0}

        def respond(arguments: list[str]) -> ServiceProcess:
            if "pg_isready" in arguments:
                state["ready"] += 1
                if state["ready"] <= ready_after:
                    return ServiceProcess(returncode=1, stderr="no response")
                return ServiceProcess(returncode=0, stdout="accepting connections")
            if psql_exit != 0 and _has(arguments, "-f"):
                return ServiceProcess(
                    returncode=psql_exit, stderr="ERROR: relation does not exist"
                )
            return ServiceProcess(returncode=0, stdout="ok")

        self._respond = respond
        return self


# ---------------------------------------------------------------------------
# Utilidades de construcción
# ---------------------------------------------------------------------------
def _workspace(tmp_path: Path, *, relative: str = "app") -> tuple[Path, Path]:
    """Crea un workspace con su proyecto y devuelve las dos rutas."""
    workspace = tmp_path / "ws"
    project = workspace / relative
    project.mkdir(parents=True, exist_ok=True)
    return workspace, project


def _write(path: Path, text: str) -> Path:
    """Escribe un fichero creando sus directorios."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _artifacts_project(tmp_path: Path) -> tuple[Path, Path]:
    """Proyecto con migración y seed válidos."""
    workspace, project = _workspace(tmp_path)
    _write(project / "drizzle" / "0000_inicial.sql", MIGRATION_SQL)
    _write(project / "src" / "db" / "seed.sql", SEED_SQL)
    return workspace, project


def _spec_with_artifacts() -> QaPostgresSpec:
    """Especificación con los dos artefactos del proyecto."""
    return QaPostgresSpec(artifacts=("drizzle/0000_inicial.sql", "src/db/seed.sql"))


def _service(
    tmp_path: Path,
    runtime: ScriptedRuntime,
    *,
    spec: QaPostgresSpec | None = None,
    credentials: QaPostgresCredentials | None = None,
    relative: str = "app",
) -> EphemeralPostgres:
    """Servicio efímero con rutas y credenciales de prueba, y los artefactos ya escritos."""
    workspace, project = _workspace(tmp_path, relative=relative)
    _write(project / "drizzle" / "0000_inicial.sql", MIGRATION_SQL)
    _write(project / "src" / "db" / "seed.sql", SEED_SQL)
    return EphemeralPostgres(
        runtime=runtime,
        workspace=workspace,
        project_relative=relative,
        network=TEST_NETWORK,
        alias=TEST_ALIAS,
        container=TEST_CONTAINER,
        spec=spec or QaPostgresSpec(),
        credentials=credentials or generate_credentials(),
    )


def _sha256(path: Path) -> str:
    """sha256 del contenido de un fichero, tal y como lo calcula el servicio."""
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Credenciales efímeras
# ---------------------------------------------------------------------------
def _credential_service(runtime: ScriptedRuntime) -> EphemeralPostgres:
    """Servicio sin workspace real: solo para leer su vista pública y su entorno de preview."""
    return EphemeralPostgres(
        runtime=runtime,
        workspace=Path("ws-sin-uso"),
        project_relative="app",
        network=TEST_NETWORK,
        alias=TEST_ALIAS,
        container=TEST_CONTAINER,
    )


def test_each_session_gets_its_own_credentials() -> None:
    """Dos sesiones no comparten credenciales: si una se filtra, no sirve en la otra."""
    first = generate_credentials()
    second = generate_credentials()

    assert first.password != second.password
    assert first.database != second.database
    assert first.user != second.user
    assert first.admin_user != second.admin_user
    assert first.admin_password != second.admin_password


def test_generated_passwords_have_entropy_and_are_not_repeated() -> None:
    """Las contraseñas se generan con ``secrets``: largas y sin repetición observable."""
    passwords = {generate_credentials().password for _ in range(64)}

    assert len(passwords) == 64
    assert all(len(password) >= 24 for password in passwords)


def test_the_preview_dsn_points_only_at_the_internal_alias() -> None:
    """El DSN de la preview nombra el alias interno de la sesión, no un destino externo."""
    credentials = generate_credentials()
    dsn = credentials.dsn(TEST_ALIAS)

    assert dsn.startswith(f"postgresql://{credentials.user}:")
    assert f"@{TEST_ALIAS}:5432/" in dsn
    assert dsn.endswith(credentials.database)
    assert "neon" not in dsn
    assert "aws" not in dsn


def test_the_preview_dsn_never_carries_the_admin_password() -> None:
    """La preview usa el rol de aplicación; la credencial de administración no sale de PUNTO."""
    credentials = generate_credentials()
    dsn = credentials.dsn(TEST_ALIAS)

    assert credentials.admin_password not in dsn
    assert credentials.admin_user not in dsn


def test_the_dsn_encodes_credentials_that_would_break_the_uri() -> None:
    """Una contraseña con ``@``, ``:``, ``/`` o ``?`` no puede reescribir el DSN."""
    credentials = QaPostgresCredentials(
        user="qa_app",
        password="p@ss:w/rd?#1",
        database="qa_db",
        admin_user="qa_root",
        admin_password="otra",
    )

    dsn = credentials.dsn(TEST_ALIAS)

    assert dsn == "postgresql://qa_app:p%40ss%3Aw%2Frd%3F%231@punto-svc-sesion:5432/qa_db"
    assert dsn.count("@") == 1


def test_the_credential_is_published_only_as_a_fingerprint() -> None:
    """La evidencia lleva una huella comparable, nunca la contraseña."""
    credentials = generate_credentials()
    fingerprint = credentials.fingerprint()

    assert len(fingerprint) == 16
    assert credentials.password not in fingerprint
    assert fingerprint == credentials.fingerprint()
    assert fingerprint != generate_credentials().fingerprint()


def test_the_public_view_of_the_service_has_no_credentials() -> None:
    """La vista publicable se puede escribir en un informe: no lleva ninguna credencial."""
    service = _credential_service(ScriptedRuntime())
    public = service.as_public_dict()
    rendered = str(public)

    assert service.credentials.password not in rendered
    assert service.credentials.admin_password not in rendered
    assert public["external_egress"] is False
    assert public["network_isolated"] is True
    assert public["roles"]["preview_is_superuser"] is False
    assert public["credential_fingerprint"] == service.credentials.fingerprint()


def test_the_preview_receives_exactly_one_variable() -> None:
    """No hay canal de entorno genérico: la preview recibe ``DATABASE_URL`` y nada más."""
    service = _credential_service(ScriptedRuntime())
    environment = service.preview_environment()

    assert list(environment) == ["DATABASE_URL"]
    assert environment["DATABASE_URL"] == service.preview_dsn
    assert "PASSWORD" not in environment["DATABASE_URL"]


# ---------------------------------------------------------------------------
# Política: la decide PUNTO
# ---------------------------------------------------------------------------
def test_an_image_outside_the_allowlist_is_rejected() -> None:
    """El proyecto no elige la imagen del servicio; solo vale la allowlist de PUNTO."""
    with pytest.raises(QaServicePolicyError) as caught:
        QaPostgresSpec(image="docker.io/library/redis:7-alpine")

    assert "no autorizada" in str(caught.value)
    assert POSTGRES_IMAGE_REFERENCE in str(caught.value)


def test_the_authorized_image_is_pinned_by_digest() -> None:
    """La allowlist contiene la referencia por digest; la etiqueta flotante queda fuera."""
    assert "@sha256:" in POSTGRES_IMAGE_REFERENCE
    assert POSTGRES_IMAGE_LABEL == "postgres:17.2-alpine"
    assert frozenset({POSTGRES_IMAGE_REFERENCE}) == ALLOWED_SERVICE_IMAGES

    with pytest.raises(QaServicePolicyError):
        QaPostgresSpec(image=POSTGRES_IMAGE_LABEL)


def test_the_service_only_listens_on_its_internal_port() -> None:
    """El puerto no es negociable: pedir otro se rechaza en lugar de publicarse."""
    with pytest.raises(QaServicePolicyError) as caught:
        QaPostgresSpec(port=15432)

    assert "5432" in str(caught.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"readiness_timeout_seconds": 0},
        {"prepare_timeout_seconds": -1},
        {"pids": 0},
        {"artifacts": ("",)},
    ],
)
def test_invalid_limits_are_rejected(overrides: dict[str, object]) -> None:
    """Un límite inválido invalida la petición: no se arranca nada para descubrirlo después."""
    with pytest.raises(QaServicePolicyError):
        QaPostgresSpec(**overrides)  # type: ignore[arg-type]


def test_too_many_artifacts_are_rejected() -> None:
    """El número de artefactos está acotado: preparar la base no es una vía de carga arbitraria."""
    with pytest.raises(QaServicePolicyError):
        QaPostgresSpec(artifacts=tuple(f"sql/{index}.sql" for index in range(50)))


# ---------------------------------------------------------------------------
# Artefactos de preparación
# ---------------------------------------------------------------------------
def test_valid_artifacts_are_authorized_with_their_hash_and_statement_count(
    tmp_path: Path,
) -> None:
    """Un artefacto válido se acepta con su huella y su número de sentencias, no por confianza."""
    workspace, project = _artifacts_project(tmp_path)

    artifacts = authorize_artifacts(workspace, "app", _spec_with_artifacts())

    assert [item.name for item in artifacts] == ["00-0000_inicial.sql", "01-seed.sql"]
    assert artifacts[0].statements == 2
    assert artifacts[1].statements == 1
    assert artifacts[0].sha256 == _sha256(project / "drizzle" / "0000_inicial.sql")
    assert all(item.source.is_absolute() for item in artifacts)


@pytest.mark.parametrize(
    ("declared", "reason"),
    [
        ("/etc/passwd", "fuera del proyecto"),
        ("C:/Windows/system32/x.sql", "fuera del proyecto"),
        ("../fuera.sql", "fuera del proyecto"),
        ("missing.sql", "no existe dentro del proyecto"),
        ("drizzle/0000_inicial.txt", "solo se autorizan artefactos .sql"),
    ],
)
def test_unauthorized_artifact_paths_are_rejected(
    tmp_path: Path, declared: str, reason: str
) -> None:
    """Rutas absolutas, con ``..``, inexistentes o que no son ``.sql`` rechazan la sesión."""
    workspace, project = _artifacts_project(tmp_path)
    _write(project / "drizzle" / "0000_inicial.txt", MIGRATION_SQL)

    with pytest.raises(QaServicePolicyError) as caught:
        authorize_artifacts(workspace, "app", QaPostgresSpec(artifacts=(declared,)))

    assert reason in str(caught.value)


def test_a_directory_that_looks_like_an_artifact_is_rejected(tmp_path: Path) -> None:
    """Un directorio con extensión ``.sql`` no es un artefacto: no se ejecuta nada."""
    workspace, project = _artifacts_project(tmp_path)
    (project / "carpeta.sql").mkdir()

    with pytest.raises(QaServicePolicyError) as caught:
        authorize_artifacts(workspace, "app", QaPostgresSpec(artifacts=("carpeta.sql",)))

    assert "no existe dentro del proyecto" in str(caught.value)


def test_an_uppercase_sql_extension_is_accepted(tmp_path: Path) -> None:
    """La extensión se compara sin distinguir mayúsculas: ``.SQL`` es un artefacto SQL."""
    workspace, project = _artifacts_project(tmp_path)
    _write(project / "MAYUSCULAS.SQL", MIGRATION_SQL)

    artifacts = authorize_artifacts(workspace, "app", QaPostgresSpec(artifacts=("MAYUSCULAS.SQL",)))

    assert len(artifacts) == 1
    assert artifacts[0].statements == 2


def test_a_symlinked_artifact_is_rejected(tmp_path: Path) -> None:
    """Un enlace simbólico puede apuntar fuera del proyecto: se rechaza sin seguirlo."""
    workspace, project = _artifacts_project(tmp_path)
    link = project / "atajo.sql"
    try:
        link.symlink_to(project / "drizzle" / "0000_inicial.sql")
    except (OSError, NotImplementedError):  # pragma: no cover - Windows sin privilegio
        pytest.skip("el sistema no permite crear enlaces simbólicos")

    with pytest.raises(QaServicePolicyError) as caught:
        authorize_artifacts(workspace, "app", QaPostgresSpec(artifacts=("atajo.sql",)))

    assert "enlace simbólico" in str(caught.value)


def test_an_empty_artifact_is_rejected(tmp_path: Path) -> None:
    """Un artefacto sin sentencias no prepara nada: es un error, no un no-op silencioso."""
    workspace, project = _artifacts_project(tmp_path)
    _write(project / "vacio.sql", "   \n")

    with pytest.raises(QaServicePolicyError):
        authorize_artifacts(workspace, "app", QaPostgresSpec(artifacts=("vacio.sql",)))


def test_an_oversized_artifact_is_rejected(tmp_path: Path) -> None:
    """El tamaño está acotado: preparar la base no es una vía para mover datos arbitrarios."""
    workspace, project = _artifacts_project(tmp_path)
    _write(project / "grande.sql", "-- relleno\n" * 200_000)

    with pytest.raises(QaServicePolicyError) as caught:
        authorize_artifacts(workspace, "app", QaPostgresSpec(artifacts=("grande.sql",)))

    assert "tamaño" in str(caught.value)


@pytest.mark.parametrize(
    "sql",
    [
        'DROP TABLE "properties";\n',
        'TRUNCATE "properties";\n',
        'DELETE FROM "properties";\n',
        'UPDATE "properties" SET "slug" = \'x\';\n',
        'ALTER TABLE "properties" DROP COLUMN "slug";\n',
        "COPY \"properties\" FROM PROGRAM 'curl http://ejemplo/x';\n",
        "SELECT pg_read_file('/etc/passwd');\n",
        "SELECT setval('seq', 5);\n",
        'INSERT INTO "propiedades" ("id") SELECT "id" FROM "otras";\n',
        "VACUUM FULL;\n",
    ],
)
def test_artifacts_that_are_not_additive_ddl_and_idempotent_seed_are_rejected(
    tmp_path: Path, sql: str
) -> None:
    """Solo DDL aditivo y seed idempotente: lo demás rechaza la preparación de la base de QA."""
    workspace, project = _artifacts_project(tmp_path)
    _write(project / "peligro.sql", sql)

    with pytest.raises(QaServicePolicyError) as caught:
        authorize_artifacts(workspace, "app", QaPostgresSpec(artifacts=("peligro.sql",)))

    assert "peligro.sql" in str(caught.value)


def test_a_file_of_the_workspace_outside_the_project_is_rejected(tmp_path: Path) -> None:
    """El artefacto tiene que estar **dentro** del proyecto declarado, no solo en el workspace."""
    workspace, _ = _artifacts_project(tmp_path)
    _write(workspace / "otro" / "suelto.sql", MIGRATION_SQL)

    with pytest.raises(QaServicePolicyError) as caught:
        authorize_artifacts(workspace, "app", QaPostgresSpec(artifacts=("otro/suelto.sql",)))

    assert "no existe dentro del proyecto" in str(caught.value)


def test_a_nested_artifact_inside_the_project_is_accepted(tmp_path: Path) -> None:
    """Dentro del proyecto la profundidad es libre: el artefacto solo tiene que quedarse dentro."""
    workspace, project = _artifacts_project(tmp_path)
    _write(project / "db" / "migraciones" / "0001.sql", MIGRATION_SQL)

    artifacts = authorize_artifacts(
        workspace, "app", QaPostgresSpec(artifacts=("db/migraciones/0001.sql",))
    )

    assert len(artifacts) == 1
    assert artifacts[0].statements == 2


def test_artifacts_are_staged_outside_the_project(tmp_path: Path) -> None:
    """Los artefactos se copian a un directorio efímero: se monta eso, no el árbol del proyecto."""
    workspace, project = _artifacts_project(tmp_path)
    artifacts = authorize_artifacts(workspace, "app", _spec_with_artifacts())
    staging = tmp_path / "stage"

    stage_artifacts(staging, artifacts)

    assert sorted(item.name for item in staging.iterdir()) == [
        "00-0000_inicial.sql",
        "01-seed.sql",
    ]
    assert (staging / "00-0000_inicial.sql").read_text(encoding="utf-8") == MIGRATION_SQL
    assert staging != project


# ---------------------------------------------------------------------------
# Guardarraíl de credenciales locales
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name", [".env", ".env.local", ".env.production", "sub/.env.local", "app/.env"]
)
def test_a_workspace_with_local_credentials_is_rejected(tmp_path: Path, name: str) -> None:
    """La credencial real del proyecto no puede estar donde la preview la vaya a leer."""
    workspace = tmp_path / "ws"
    target = workspace / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("DATABASE_URL=postgresql://real@ep-real.aws.neon.tech/neondb\n", "utf-8")

    with pytest.raises(QaServicePolicyError) as caught:
        assert_no_local_secrets(workspace)

    assert "credenciales locales" in str(caught.value)


def test_a_template_file_is_not_a_credential_file(tmp_path: Path) -> None:
    """``.env.example`` no lleva credenciales reales: se permite explícitamente."""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    (workspace / ".env.example").write_text("DATABASE_URL=\n", encoding="utf-8")

    assert_no_local_secrets(workspace)
    assert find_local_secrets(workspace) == ()


def test_dependency_folders_are_not_scanned_for_credentials(tmp_path: Path) -> None:
    """``node_modules`` y ``.git`` no se recorren: son ruido y no son del proyecto."""
    workspace = tmp_path / "ws"
    (workspace / "node_modules" / "paquete").mkdir(parents=True)
    (workspace / "node_modules" / "paquete" / ".env").write_text("X=1", encoding="utf-8")
    (workspace / ".git").mkdir(parents=True)
    (workspace / ".git" / ".env").write_text("X=1", encoding="utf-8")

    assert_no_local_secrets(workspace)


def test_the_guardrail_names_the_offending_files(tmp_path: Path) -> None:
    """El error dice qué fichero sobra: un bloqueo sin causa accionable no vale."""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    (workspace / ".env.local").write_text("X=1", encoding="utf-8")

    found = find_local_secrets(workspace)

    assert [item.name for item in found] == [".env.local"]


# ---------------------------------------------------------------------------
# Arranque: las propiedades de aislamiento
# ---------------------------------------------------------------------------
def _start_arguments(tmp_path: Path) -> tuple[list[str], EphemeralPostgres]:
    """Argumentos de arranque del servicio, con la instancia para poder contrastarlos."""
    service = _service(tmp_path, ScriptedRuntime())
    staging = tmp_path / "stage"
    staging.mkdir(exist_ok=True)
    return service.start_arguments(staging), service


def test_the_service_runs_the_digest_pinned_image(tmp_path: Path) -> None:
    """La imagen que se ejecuta es la referencia por digest de la allowlist, no una etiqueta."""
    arguments, _ = _start_arguments(tmp_path)

    assert arguments[-1] == POSTGRES_IMAGE_REFERENCE
    assert POSTGRES_IMAGE_LABEL not in arguments


def test_the_service_joins_the_session_internal_network(tmp_path: Path) -> None:
    """El servicio vive en la red interna de la sesión: no se le da otra red ni la del host."""
    arguments, _ = _start_arguments(tmp_path)

    assert arguments[arguments.index("--network") + 1] == TEST_NETWORK
    assert arguments[arguments.index("--network-alias") + 1] == TEST_ALIAS
    assert arguments.count("--network") == 1
    assert "host" not in arguments


def test_the_service_publishes_no_ports(tmp_path: Path) -> None:
    """El servicio no abre ningún puerto al host: se alcanza solo desde la red de la sesión."""
    arguments, _ = _start_arguments(tmp_path)

    assert "-p" not in arguments
    assert "--publish" not in arguments
    assert not any(item.startswith("5432:") for item in arguments)
    assert not any(item.startswith("127.0.0.1:") for item in arguments)


def test_the_service_is_hardened(tmp_path: Path) -> None:
    """Raíz inmutable, capacidades mínimas, sin escalada, sin privilegios y con límites."""
    arguments, _ = _start_arguments(tmp_path)
    joined = " ".join(arguments)

    assert "--read-only" in arguments
    assert arguments[arguments.index("--cap-drop") + 1] == "ALL"
    assert arguments[arguments.index("--cap-add") + 1] == "CHOWN,FOWNER,DAC_OVERRIDE,SETUID,SETGID"
    assert arguments[arguments.index("--security-opt") + 1] == "no-new-privileges"
    assert "--privileged" not in arguments
    assert "--user" not in arguments
    assert "NET_ADMIN" not in joined
    assert "SYS_ADMIN" not in joined
    assert arguments[arguments.index("--memory") + 1] == "512m"
    assert arguments[arguments.index("--cpus") + 1] == "1"
    assert arguments[arguments.index("--pids-limit") + 1] == "128"
    assert "--restart=no" in arguments
    assert "--rm" not in arguments


def test_the_service_keeps_its_data_in_tmpfs(tmp_path: Path) -> None:
    """Sin volúmenes persistentes: los datos viven en memoria y no sobreviven a la sesión."""
    arguments, _ = _start_arguments(tmp_path)
    tmpfs = [arguments[index + 1] for index, item in enumerate(arguments) if item == "--tmpfs"]
    volumes = [arguments[index + 1] for index, item in enumerate(arguments) if item == "-v"]

    assert any(item.startswith("/var/lib/postgresql/data:rw,size=512m") for item in tmpfs)
    assert any(item.startswith("/var/run/postgresql:rw") for item in tmpfs)
    assert any(item.startswith("/tmp:rw") for item in tmpfs)
    assert volumes == [f"{tmp_path / 'stage'}:{ARTIFACT_MOUNT}:ro,Z"]


def test_only_the_staged_artifacts_are_mounted_into_the_service(tmp_path: Path) -> None:
    """Al servicio no se le monta el workspace del proyecto: solo su directorio de artefactos."""
    arguments, _ = _start_arguments(tmp_path)
    volumes = [arguments[index + 1] for index, item in enumerate(arguments) if item == "-v"]

    assert all(item.endswith(f":{ARTIFACT_MOUNT}:ro,Z") for item in volumes)
    assert all("/workspace" not in item for item in volumes)


def test_the_application_dsn_never_travels_in_the_runtime_arguments(tmp_path: Path) -> None:
    """El DSN de la aplicación viaja por el payload de la preview, no por el argv del contenedor."""
    arguments, service = _start_arguments(tmp_path)
    joined = " ".join(arguments)

    assert service.preview_dsn not in joined
    assert service.credentials.password not in joined
    assert "DATABASE_URL" not in joined


def test_the_admin_credential_of_the_service_is_its_own(tmp_path: Path) -> None:
    """El contenedor del servicio se inicializa con su credencial efímera, no con otra."""
    arguments, service = _start_arguments(tmp_path)
    environment = [arguments[index + 1] for index, item in enumerate(arguments) if item == "--env"]

    assert f"POSTGRES_USER={service.credentials.admin_user}" in environment
    assert f"POSTGRES_DB={service.credentials.database}" in environment
    assert f"POSTGRES_PASSWORD={service.credentials.admin_password}" in environment
    assert service.credentials.password not in " ".join(arguments)
    assert service.preview_dsn not in " ".join(arguments)


# ---------------------------------------------------------------------------
# Ciclo de vida
# ---------------------------------------------------------------------------
def test_the_service_lifecycle_follows_the_declared_order(tmp_path: Path) -> None:
    """Arrancar → readiness → rol sin privilegios → migración y seed, en ese orden."""
    runtime = ScriptedRuntime().script()
    service = _service(tmp_path, runtime, spec=_spec_with_artifacts())
    artifacts = service.plan()
    staging = tmp_path / "stage"
    stage_artifacts(staging, artifacts)

    service.start(staging)
    service.wait_ready()
    service.create_application_role()
    service.prepare()
    service.destroy()

    assert runtime.verbs() == ["run", "exec", "exec", "exec", "exec", "exec", "exec", "rm"]
    role_call = runtime.only("CREATE ROLE")
    assert service.credentials.user in " ".join(role_call)
    assert service.credentials.admin_user in role_call
    for artifact in artifacts:
        call = runtime.only(artifact.name)
        assert call[0] == "exec"
        assert "-f" in call
        assert f"{ARTIFACT_MOUNT}/{artifact.name}" in call


def test_readiness_retries_before_succeeding(tmp_path: Path) -> None:
    """El servicio se espera: ``pg_isready`` se reintenta hasta que acepta conexiones."""
    runtime = ScriptedRuntime().script(ready_after=3)
    service = _service(tmp_path, runtime)

    service.wait_ready()

    assert len(runtime.matching("pg_isready")) == 4
    assert service.ready is True


def test_a_service_that_never_becomes_ready_is_destroyed(tmp_path: Path) -> None:
    """Sin readiness no hay sesión, y el contenedor no se queda a medio arrancar."""
    runtime = ScriptedRuntime(lambda arguments: ServiceProcess(returncode=1, stderr="no response"))
    service = _service(tmp_path, runtime, spec=QaPostgresSpec(readiness_timeout_seconds=0.2))

    with pytest.raises(QaServiceUnavailableError) as caught:
        service.wait_ready()

    assert "no estuvo listo" in str(caught.value)
    assert runtime.matching("rm", service.container)
    assert service.ready is False


def test_a_failed_start_is_explicit_and_not_marked_as_started(tmp_path: Path) -> None:
    """Si el runtime no arranca el contenedor, la sesión se bloquea con el detalle saneado."""
    runtime = ScriptedRuntime(
        lambda arguments: ServiceProcess(returncode=125, stderr="no such image")
    )
    service = _service(tmp_path, runtime)
    staging = tmp_path / "stage"
    staging.mkdir()

    with pytest.raises(QaServiceUnavailableError) as caught:
        service.start(staging)

    assert "no arrancó" in str(caught.value)
    assert service.started is False


def test_the_application_role_has_no_privileges(tmp_path: Path) -> None:
    """El rol de trabajo no es superusuario y no puede crear bases ni roles: es el rol de la app."""
    runtime = ScriptedRuntime().script()
    service = _service(tmp_path, runtime)

    service.create_application_role()

    statement = runtime.only("CREATE ROLE")[-1]
    assert "NOSUPERUSER" in statement
    assert "NOCREATEDB" in statement
    assert "NOCREATEROLE" in statement
    assert "NOREPLICATION" in statement
    assert "NOBYPASSRLS" in statement
    assert service.credentials.password in statement
    database = runtime.only("CREATE DATABASE")[-1]
    assert f'OWNER "{service.credentials.user}"' in database
    assert runtime.only("ALTER DATABASE")[-1].endswith(
        f'OWNER TO "{service.credentials.user}"'
    )


def test_preparing_the_role_is_idempotent(tmp_path: Path) -> None:
    """Si el rol ya existe, el estado buscado ya está: no se convierte en un fallo."""
    runtime = ScriptedRuntime(
        lambda arguments: ServiceProcess(
            returncode=1, stderr='ERROR: role "qa_app_x" already exists'
        )
        if _has(arguments, "CREATE ROLE")
        else ServiceProcess(returncode=0)
    )
    service = _service(tmp_path, runtime)

    service.create_application_role()

    assert len(runtime.matching("CREATE ROLE")) == 1


def test_a_role_failure_is_a_preparation_failure(tmp_path: Path) -> None:
    """Un fallo al crear el rol bloquea la sesión: no se sigue con una base a medias."""
    runtime = ScriptedRuntime(
        lambda arguments: ServiceProcess(returncode=1, stderr="ERROR: permission denied")
        if _has(arguments, "CREATE ROLE")
        else ServiceProcess(returncode=0)
    )
    service = _service(tmp_path, runtime)

    with pytest.raises(QaServicePreparationError) as caught:
        service.create_application_role()

    assert "rol de aplicación" in str(caught.value)


def test_preparation_uses_the_application_role_and_stops_on_error(tmp_path: Path) -> None:
    """La migración y el seed los aplica el rol de la app, con ``ON_ERROR_STOP=1``."""
    runtime = ScriptedRuntime().script()
    service = _service(tmp_path, runtime, spec=_spec_with_artifacts())
    service.plan()

    service.prepare()

    for artifact in service.artifacts:
        call = runtime.only(artifact.name)
        assert "ON_ERROR_STOP=1" in call
        assert "--no-psqlrc" in call
        assert call[call.index("-U") + 1] == service.credentials.user
        assert service.credentials.admin_user not in call


def test_a_failed_preparation_names_the_artifact_and_blocks(tmp_path: Path) -> None:
    """Si la migración o el seed fallan, la sesión se bloquea: no hay datos simulados de reserva."""
    runtime = ScriptedRuntime().script(psql_exit=1)
    service = _service(tmp_path, runtime, spec=_spec_with_artifacts())
    service.plan()

    with pytest.raises(QaServicePreparationError) as caught:
        service.prepare()

    assert "00-0000_inicial.sql" in str(caught.value)
    assert "la preparación de la base de QA falló" in str(caught.value)


def test_destroying_is_idempotent_and_never_propagates(tmp_path: Path) -> None:
    """La limpieza no puede fallar la salida de la sesión: se intenta y no se propaga."""
    runtime = ScriptedRuntime(
        lambda arguments: ServiceProcess(returncode=1, stderr="no such container")
    )
    service = _service(tmp_path, runtime)

    service.destroy()
    service.destroy()

    assert len(runtime.matching("rm")) == 2
    assert service.started is False


def test_no_leftovers_after_destroying(tmp_path: Path) -> None:
    """Tras destruir, ningún contenedor del servicio sigue vivo."""
    service = _service(tmp_path, ScriptedRuntime())

    service.destroy()

    assert service.leftovers() == ()


def test_a_leftover_container_is_reported(tmp_path: Path) -> None:
    """Si el runtime siguiera listando el contenedor, la comprobación lo dice."""
    runtime = ScriptedRuntime(
        lambda arguments: ServiceProcess(returncode=0, stdout=f"{TEST_CONTAINER}\n")
        if arguments[0] == "ps"
        else ServiceProcess(returncode=0)
    )
    service = _service(tmp_path, runtime)

    assert service.leftovers() == (TEST_CONTAINER,)


def test_the_cleanup_check_reports_containers_and_networks(tmp_path: Path) -> None:
    """La evidencia de limpieza se mide contra el runtime, no contra la intención."""
    runtime = ScriptedRuntime()
    service = _service(tmp_path, runtime)

    check = service_cleanup_check(runtime, service.container, TEST_NETWORK)

    assert check == {"container_removed": True, "network_removed": True}


def test_the_cleanup_check_detects_what_is_still_there() -> None:
    """Un contenedor o una red que sobreviven aparecen como no eliminados."""
    runtime = ScriptedRuntime(
        lambda arguments: ServiceProcess(
            returncode=0,
            stdout=f"{TEST_CONTAINER}\n" if arguments[0] == "ps" else f"{TEST_NETWORK}\n",
        )
    )

    check = service_cleanup_check(runtime, TEST_CONTAINER, TEST_NETWORK)

    assert check == {"container_removed": False, "network_removed": False}


# ---------------------------------------------------------------------------
# Saneado de lo que sale del ciclo de vida
# ---------------------------------------------------------------------------
def test_a_dsn_in_a_runtime_error_is_redacted(tmp_path: Path) -> None:
    """Un error del runtime con el DSN dentro no puede publicarlo: sale redactado."""
    credentials = generate_credentials()
    leaked = credentials.dsn(TEST_ALIAS)
    runtime = ScriptedRuntime(
        lambda arguments: ServiceProcess(returncode=1, stderr=f"connection refused: {leaked}")
    )
    service = _service(tmp_path, runtime, credentials=credentials)
    staging = tmp_path / "stage"
    staging.mkdir()

    with pytest.raises(QaServiceUnavailableError) as caught:
        service.start(staging)

    message = str(caught.value)
    assert credentials.password not in message
    assert leaked not in message
    assert REDACTED in message


def test_a_password_in_a_preparation_error_is_redacted(tmp_path: Path) -> None:
    """Lo mismo al preparar: el fallo se cuenta sin la credencial de la sesión."""
    credentials = generate_credentials()
    runtime = ScriptedRuntime(
        lambda arguments: ServiceProcess(
            returncode=1, stderr=f"FATAL: password={credentials.password}"
        )
    )
    service = _service(tmp_path, runtime, credentials=credentials, spec=_spec_with_artifacts())
    service.plan()

    with pytest.raises(QaServicePreparationError) as caught:
        service.prepare()

    assert credentials.password not in str(caught.value)
    assert REDACTED in str(caught.value)


# ---------------------------------------------------------------------------
# Costura con el sandbox: payload y auditoría
# ---------------------------------------------------------------------------
def test_the_preview_arguments_carry_no_environment(tmp_path: Path) -> None:
    """El contenedor de la preview no recibe ``--env``: el DSN viaja por el payload del probe."""
    backend = WebSandboxBackend()

    arguments = backend._preview_arguments(
        workspace=tmp_path,
        probe_root=tmp_path,
        network=TEST_NETWORK,
        alias="punto-preview-sesion",
        container="punto-preview-sesion",
    )

    assert "--env" not in arguments
    assert "DATABASE_URL" not in " ".join(arguments)
    assert arguments[arguments.index("--network") + 1] == TEST_NETWORK


def _sandbox_backend_with_fake_runtime(
    handler: Callable[[list[str]], ServiceProcess],
) -> tuple[WebSandboxBackend, list[list[str]], AuditLogger]:
    """Backend web con el runtime OCI sustituido por un guion grabado."""
    runtime = ScriptedRuntime(handler)
    audit = AuditLogger()
    backend = WebSandboxBackend(audit=audit)

    def fake_run_runtime(
        arguments: list[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        result = runtime.run(arguments, timeout=1.0)
        return subprocess.CompletedProcess(
            args=list(arguments),
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    backend._run_runtime = fake_run_runtime  # type: ignore[method-assign]
    return backend, runtime.calls, audit


def test_the_sandbox_prepares_the_service_and_audits_the_lifecycle(tmp_path: Path) -> None:
    """El backend levanta el servicio, lo prepara y deja eventos con huella, nunca con el DSN."""
    backend, calls, audit = _sandbox_backend_with_fake_runtime(_ok)
    workspace, _ = _artifacts_project(tmp_path)
    staging = tmp_path / "stage"
    task_id, project_id = uuid4(), uuid4()

    service = backend._prepare_service(
        spec=_spec_with_artifacts(),
        workspace=workspace,
        project_relative=Path("app"),
        network=TEST_NETWORK,
        suffix="abc123",
        staging=staging,
        task_id=task_id,
        project_id=project_id,
    )

    assert service.started is True
    assert [call[0] for call in calls].count("run") == 1
    started = audit.by_type(AuditEventType.QA_SERVICE_STARTED)
    prepared = audit.by_type(AuditEventType.QA_SERVICE_PREPARED)
    assert len(started) == 1
    assert len(prepared) == 1
    metadata = dict(started[0].metadata)
    assert metadata["image_reference"] == POSTGRES_IMAGE_REFERENCE
    assert metadata["external_egress"] is False
    assert metadata["project_id"] == str(project_id)
    rendered = str(metadata)
    assert service.credentials.password not in rendered
    assert service.credentials.admin_password not in rendered
    assert metadata["credential_fingerprint"] == service.credentials.fingerprint()


def test_a_failed_preparation_destroys_the_service_and_blocks(tmp_path: Path) -> None:
    """Si la preparación falla, la sesión se bloquea y el contenedor se destruye en el acto."""
    backend, calls, audit = _sandbox_backend_with_fake_runtime(
        lambda arguments: ServiceProcess(returncode=1, stderr="ERROR: syntax error")
        if _has(arguments, "-f")
        else _ok(arguments)
    )
    workspace, _ = _artifacts_project(tmp_path)

    with pytest.raises(QaServicePreparationError):
        backend._prepare_service(
            spec=_spec_with_artifacts(),
            workspace=workspace,
            project_relative=Path("app"),
            network=TEST_NETWORK,
            suffix="abc123",
            staging=tmp_path / "stage",
            task_id=uuid4(),
            project_id=uuid4(),
        )

    assert any(call[0] == "rm" for call in calls), calls
    assert len(audit.by_type(AuditEventType.QA_SERVICE_STARTED)) == 1
    assert audit.by_type(AuditEventType.QA_SERVICE_PREPARED) == ()


def test_a_failed_readiness_destroys_the_service(tmp_path: Path) -> None:
    """Un servicio que no llega a estar listo no se queda vivo esperando a nadie."""
    backend, calls, _ = _sandbox_backend_with_fake_runtime(
        lambda arguments: ServiceProcess(returncode=1, stderr="no response")
    )
    workspace, _ = _artifacts_project(tmp_path)

    with pytest.raises(QaServiceUnavailableError):
        backend._prepare_service(
            spec=QaPostgresSpec(
                artifacts=("drizzle/0000_inicial.sql",), readiness_timeout_seconds=0.2
            ),
            workspace=workspace,
            project_relative=Path("app"),
            network=TEST_NETWORK,
            suffix="abc123",
            staging=tmp_path / "stage",
            task_id=uuid4(),
            project_id=uuid4(),
        )

    assert any(call[0] == "rm" for call in calls), calls


def test_the_destroyed_event_records_the_cleanup_check(tmp_path: Path) -> None:
    """El evento de destrucción dice lo que el runtime responde, no lo que se pretendía."""
    backend, _, audit = _sandbox_backend_with_fake_runtime(_ok)
    service = _service(tmp_path, ScriptedRuntime())

    backend._audit_service_destroyed(
        task_id=uuid4(),
        project_id=uuid4(),
        service=service,
        network=TEST_NETWORK,
    )

    events = audit.by_type(AuditEventType.QA_SERVICE_DESTROYED)
    assert len(events) == 1
    metadata = dict(events[0].metadata)
    assert dict(metadata["cleanup"]) == {"container_removed": True, "network_removed": True}
    assert service.credentials.password not in str(metadata)


def test_the_service_events_are_not_recorded_without_identifiers(tmp_path: Path) -> None:
    """Sin identificadores no se inventa un evento de servicio: se omite."""
    audit = AuditLogger()
    backend = WebSandboxBackend(audit=audit)
    service = _service(tmp_path, ScriptedRuntime())

    backend._audit_service_started(task_id=None, project_id=None, service=service)

    assert audit.events() == ()


def test_the_backend_finds_orphan_service_containers() -> None:
    """La vía de recuperación encuentra el servicio por su etiqueta, aunque su nombre sea otro."""
    backend, _, _ = _sandbox_backend_with_fake_runtime(
        lambda arguments: ServiceProcess(returncode=0, stdout=f"{TEST_CONTAINER}\n")
        if arguments[0] == "ps"
        else ServiceProcess(returncode=0)
    )

    assert backend.list_service_containers() == (TEST_CONTAINER,)


def test_destroy_removes_an_orphan_service_container() -> None:
    """Un servicio huérfano (proceso muerto a mitad de sesión) lo limpia ``destroy``."""
    backend, calls, _ = _sandbox_backend_with_fake_runtime(
        lambda arguments: ServiceProcess(returncode=0, stdout=f"{TEST_CONTAINER}\n")
        if arguments[0] == "ps"
        else ServiceProcess(returncode=0)
    )

    backend.destroy()

    assert any(call[0] == "rm" and TEST_CONTAINER in call for call in calls), calls


def test_abandoned_host_dirs_from_a_killed_process_are_swept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un directorio temporal de una sesión muerta de golpe se barre; uno vivo, no.

    El payload de la preview lleva el DSN efímero de su sesión, así que un directorio abandonado no
    puede quedarse ahí: el ``atexit`` no cubre una muerte dura, pero el barrido por antigüedad sí.
    """
    from punto.web.sandbox import STALE_HOST_DIR_SECONDS, _sweep_stale_host_dirs

    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    abandoned = tmp_path / "punto-web-probe-muerta"
    alive = tmp_path / "punto-web-probe-viva"
    for directory in (abandoned, alive):
        directory.mkdir()
        (directory / "preview-payload.json").write_text("{}", encoding="utf-8")
    stale = time.time() - STALE_HOST_DIR_SECONDS - 60
    os.utime(abandoned, (stale, stale))

    removed = _sweep_stale_host_dirs()

    assert removed == ("punto-web-probe-muerta",)
    assert not abandoned.exists()
    assert alive.exists()


# ---------------------------------------------------------------------------
# El probe: allowlist de entorno del payload
# ---------------------------------------------------------------------------
def _load_preview_probe() -> ModuleType:
    """Carga ``run_preview.py`` por ruta: vive fuera de ``src`` y no es un paquete.

    El módulo se registra en ``sys.modules`` antes de ejecutarlo: el probe define un
    ``dataclass``, y ``dataclasses`` resuelve las anotaciones por ``sys.modules[cls.__module__]``.
    """
    import sys

    path = Path(__file__).resolve().parents[1] / "sandbox" / "web" / "probes" / "run_preview.py"
    spec = importlib.util.spec_from_file_location("punto_web_probe_preview", path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_the_probe_accepts_only_the_allowlisted_environment_variable() -> None:
    """El payload no es un canal de entorno: la única clave admitida es ``DATABASE_URL``."""
    probe = _load_preview_probe()
    dsn = "postgresql://qa_app_x:secreto@punto-svc-sesion:5432/qa_db_x"

    assert probe._environment({"DATABASE_URL": dsn}) == {"DATABASE_URL": dsn}
    assert probe._environment(None) == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"PATH": "/usr/bin"},
        {"NODE_OPTIONS": "--require /workspace/x.js"},
        {"LD_PRELOAD": "/workspace/x.so"},
        {"DATABASE_URL": "postgresql://a@b/d", "PGPASSWORD": "x"},
        {"DATABASE_URL": ""},
        {"DATABASE_URL": 5},
        "DATABASE_URL=x",
        {"a": "1", "b": "2", "c": "3", "d": "4", "e": "5"},
    ],
)
def test_the_probe_rejects_any_other_environment_shape(payload: object) -> None:
    """Cualquier clave no autorizada invalida el payload, aunque el host fuera quien la mandara."""
    probe = _load_preview_probe()

    with pytest.raises(ValueError):
        probe._environment(payload)


def test_the_probe_injects_the_variable_into_the_child_environment() -> None:
    """La variable validada llega al hijo, y el resto del entorno del contenedor se conserva."""
    probe = _load_preview_probe()
    dsn = "postgresql://qa_app_x:secreto@punto-svc-sesion:5432/qa_db_x"

    merged = probe._child_environment({"DATABASE_URL": dsn})

    assert merged["DATABASE_URL"] == dsn
    assert "PATH" in merged
