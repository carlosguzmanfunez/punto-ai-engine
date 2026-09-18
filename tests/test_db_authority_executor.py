"""DB AUTHORITY EXECUTOR v0 — clasificador, destino, política, transacción y frontera de secretos.

**Ninguna prueba toca una base real**: el driver es un doble controlado que registra lo ejecutado,
simula fallos y filas afectadas. Lo que se prueba es la frontera de autoridad del controlador: qué
SQL se clasifica como seguro, qué se deniega, qué exige aprobación humana, qué se revierte y qué
nunca sale del proceso (el DSN).

    pytest tests/test_db_authority_executor.py -q
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from punto.audit.logger import AuditLogger
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers.contract import ProviderRole, make_request
from punto.providers.secrets import SecretStore, SecretStoreError, redact_secret_text
from punto.providers.transport import build_environment as build_transport_environment
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import RiskLevel
from punto.tools.database import (
    AUTONOMOUS_CLASSIFICATIONS,
    CLASSIFICATION_ACTIONS,
    DatabaseApprovalRequiredError,
    DatabaseBudgetError,
    DatabaseDeniedError,
    DatabaseExecutionError,
    DatabaseExecutor,
    DatabaseTarget,
    DatabaseTargetError,
    DatabaseUnavailableError,
    PsycopgDriver,
    SqlClassification,
    assert_destination,
    classify_sql,
    classify_statement,
    parse_dsn,
    redact_dsn,
    resolve_dsn,
    schema_fingerprint,
)
from punto.tools.shell_policy import build_sanitized_environment

#: Configuración real del motor (catálogo de autoridad incluido).
CONFIG_DIR = Path("config")

#: Alcance del secreto de proyecto del piloto.
SCOPE = "punto-inmobiliario-hn"

#: Host y base de desarrollo sintéticos (no existen; nunca se conecta).
DEV_HOST = "ep-dev-123456.us-east-2.aws.neon.tech"
DEV_DATABASE = "neondb"

#: Contraseña sintética del canario: no es una credencial real.
DEV_PASSWORD = "canary-not-a-real-secret"

#: DSN sintético de desarrollo. No es una credencial real: es un canario documentado de prueba.
DEV_DSN = (
    f"postgresql://punto_canary:{DEV_PASSWORD}@{DEV_HOST}/{DEV_DATABASE}?sslmode=require"
)

#: Credencial sintética de proveedor, para la compatibilidad del almacén.
PROVIDER_KEY = "sk-test-CANARY-0123456789abcdef"


# ---------------------------------------------------------------------------
# Doble de driver controlado
# ---------------------------------------------------------------------------
class FakeCursor:
    """Cursor simulado: registra lo ejecutado y devuelve filas guionizadas."""

    def __init__(self, driver: FakeDriver) -> None:
        self._driver = driver
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = -1

    def execute(self, sql: str) -> None:
        """Registra la sentencia y simula el resultado o el fallo guionizado."""
        self._driver.executed.append(sql)
        failure = self._driver.failure_for(sql)
        if failure is not None:
            raise RuntimeError(failure)
        if "information_schema.columns" in sql:
            self._rows = list(self._driver.schema_rows)
            self.rowcount = -1
            return
        self._rows = [(1,)]
        self.rowcount = self._driver.rowcount_for(sql)

    def fetchone(self) -> tuple[Any, ...] | None:
        """Primera fila."""
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        """Todas las filas."""
        return list(self._rows)

    def close(self) -> None:
        """Cierra el cursor."""
        return None


class FakeConnection:
    """Conexión simulada con contabilidad de commit y rollback."""

    def __init__(self, driver: FakeDriver) -> None:
        self._driver = driver
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        """Abre un cursor simulado."""
        return FakeCursor(self._driver)

    def commit(self) -> None:
        """Confirma."""
        self.commits += 1

    def rollback(self) -> None:
        """Revierte."""
        self.rollbacks += 1

    def close(self) -> None:
        """Cierra."""
        self.closed = True


class FakeDriver:
    """Driver inyectable: sin red, sin base, con fallos y filas guionizados."""

    def __init__(
        self,
        *,
        schema_rows: tuple[tuple[str, str], ...] = (
            ("properties", "id"),
            ("properties", "title"),
        ),
        rowcounts: dict[str, int] | None = None,
        failures: dict[str, str] | None = None,
        connect_error: str = "",
    ) -> None:
        self.schema_rows = schema_rows
        self.rowcounts = rowcounts or {}
        self.failures = failures or {}
        self.connect_error = connect_error
        self.executed: list[str] = []
        self.connections: list[FakeConnection] = []
        self.dsns: list[str] = []

    def connect(self, dsn: str, *, timeout: float) -> FakeConnection:
        """Abre una conexión simulada, o falla si se pidió."""
        del timeout
        self.dsns.append(dsn)
        if self.connect_error:
            raise DatabaseUnavailableError(self.connect_error)
        connection = FakeConnection(self)
        self.connections.append(connection)
        return connection

    def failure_for(self, sql: str) -> str | None:
        """Fallo guionizado para una sentencia, si lo hay."""
        for fragment, message in self.failures.items():
            if fragment.lower() in sql.lower():
                return message
        return None

    def rowcount_for(self, sql: str) -> int:
        """Filas afectadas guionizadas para una sentencia."""
        for fragment, count in self.rowcounts.items():
            if fragment.lower() in sql.lower():
                return count
        return 1

    @property
    def last_connection(self) -> FakeConnection:
        """Última conexión abierta."""
        assert self.connections, "no se abrió ninguna conexión"
        return self.connections[-1]


@pytest.fixture
def policy() -> PolicyEngine:
    """Policy Engine real, con el catálogo de autoridad del repositorio."""
    return PolicyEngine.from_config(CONFIG_DIR, environment="local")


@pytest.fixture
def secrets(tmp_path: Path) -> SecretStore:
    """Almacén de secretos en un temporal, con el DSN de desarrollo ya guardado."""
    store = SecretStore(tmp_path / "secrets.json")
    store.set_project_secret(SCOPE, DEV_DSN)
    return store


@pytest.fixture
def target() -> DatabaseTarget:
    """Autorización de desarrollo ligada al destino sintético."""
    return DatabaseTarget(
        project_id="punto-inmobiliario-hn",
        scope=SCOPE,
        environment="development",
        expected_host=DEV_HOST,
        expected_database=DEV_DATABASE,
    )


def make_executor(
    target: DatabaseTarget,
    secrets: SecretStore,
    policy: PolicyEngine,
    *,
    driver: FakeDriver | None = None,
    audit: AuditLogger | None = None,
    human_gate: HumanGate | None = None,
) -> tuple[DatabaseExecutor, FakeDriver]:
    """Ejecutor con driver controlado y auditoría opcional."""
    fake = driver if driver is not None else FakeDriver()
    executor = DatabaseExecutor(
        target=target,
        secrets=secrets,
        policy=policy,
        audit=audit,
        driver=fake,
        human_gate=human_gate,
    )
    return executor, fake


# ---------------------------------------------------------------------------
# 1. Clasificador
# ---------------------------------------------------------------------------
def test_clasifica_lectura_y_ddl_seguro() -> None:
    """Lectura, DDL aditivo y seed idempotente caen en las clases seguras."""
    casos = {
        "SELECT id, title FROM properties": SqlClassification.SAFE_READ,
        "select 1": SqlClassification.SAFE_READ,
        "SHOW search_path": SqlClassification.SAFE_READ,
        "CREATE TABLE IF NOT EXISTS leads (id serial primary key)": SqlClassification.SAFE_DDL,
        "CREATE INDEX properties_status_idx ON properties (status)": SqlClassification.SAFE_DDL,
        "CREATE UNIQUE INDEX code_uq ON properties (code)": SqlClassification.SAFE_DDL,
        "ALTER TABLE properties ADD COLUMN featured boolean": SqlClassification.SAFE_DDL,
        "ALTER TABLE properties ADD COLUMN views integer DEFAULT 0 NOT NULL": (
            SqlClassification.SAFE_DDL
        ),
        "INSERT INTO departments (name) VALUES ('Cortés') ON CONFLICT DO NOTHING": (
            SqlClassification.SAFE_SEED
        ),
    }

    for sql, expected in casos.items():
        assert classify_statement(sql).classification is expected, sql
        assert classify_statement(sql).autonomous is True, sql


def test_add_column_not_null_sin_default_es_unknown() -> None:
    """Un ``ADD COLUMN NOT NULL`` sin default no es compatible con filas existentes."""
    statement = classify_statement("ALTER TABLE properties ADD COLUMN code varchar NOT NULL")

    assert statement.classification is SqlClassification.UNKNOWN
    assert "compatible" in statement.reason or "NOT NULL" in statement.reason


def test_multiples_sentencias_se_clasifican_todas() -> None:
    """Un script con varias sentencias produce una entrada por sentencia."""
    plan = classify_sql(
        "CREATE TABLE a (id int); CREATE INDEX a_idx ON a (id); "
        "INSERT INTO a VALUES (1) ON CONFLICT DO NOTHING;"
    )

    assert plan.total == 3
    assert plan.classifications == {"SAFE_DDL": 2, "SAFE_SEED": 1}
    assert plan.autonomous is True


def test_comentarios_y_espacios_no_permiten_bypass() -> None:
    """Los comentarios no esconden una sentencia destructiva ni la inventan."""
    con_comentario = classify_sql("/* DROP TABLE properties */ SELECT 1")
    linea = classify_sql("SELECT 1; -- DROP TABLE properties")
    destructiva = classify_sql("/* nota */ DROP TABLE properties")
    anidado = classify_sql("/* a /* b */ c */ TRUNCATE properties")

    assert con_comentario.total == 1
    assert con_comentario.statements[0].classification is SqlClassification.SAFE_READ
    assert linea.total == 1
    assert [item.classification for item in destructiva.statements] == [
        SqlClassification.DESTRUCTIVE
    ]
    assert [item.classification for item in anidado.statements] == [
        SqlClassification.DESTRUCTIVE
    ]


@pytest.mark.parametrize(
    "sql",
    (
        "CREATE TYPE estado AS ENUM ('a', 'b')",
        "CREATE EXTENSION IF NOT EXISTS pgcrypto",
        "GRANT ALL ON properties TO public",
        "REVOKE SELECT ON properties FROM public",
        "DO $$ BEGIN NULL; END $$",
        "COPY properties FROM '/etc/passwd'",
        "CREATE FUNCTION f() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql",
        "ANALYZE properties",
        "VACUUM FULL properties",
        "SET search_path TO public",
        "BEGIN",
        "COMMIT",
        "ALTER TABLE properties RENAME COLUMN title TO name",
        "ALTER TABLE properties ALTER COLUMN price TYPE text",
        "ALTER TABLE properties ADD CONSTRAINT c CHECK (price > 0)",
    ),
)
def test_sentencia_no_reconocida_falla_cerrado(sql: str) -> None:
    """Todo lo que no está en la lista admitida es ``UNKNOWN`` y por tanto DENY."""
    statement = classify_statement(sql)

    assert statement.classification is SqlClassification.UNKNOWN, sql
    assert statement.autonomous is False


def test_drop_truncate_y_drop_column_son_destructive() -> None:
    """DROP, TRUNCATE y DROP COLUMN nunca son autónomos."""
    for sql in (
        "DROP TABLE properties",
        "DROP INDEX properties_status_idx",
        "DROP DATABASE neondb",
        "TRUNCATE TABLE properties",
        "ALTER TABLE properties DROP COLUMN featured",
    ):
        statement = classify_statement(sql)
        assert statement.classification is SqlClassification.DESTRUCTIVE, sql
        assert statement.autonomous is False


def test_delete_y_update_masivo_es_mass_data_change() -> None:
    """Cualquier UPDATE/DELETE es cambio masivo en v0: una condición no es una prueba."""
    for sql in (
        "DELETE FROM properties",
        "DELETE FROM properties WHERE id > 0",
        "UPDATE properties SET price = 0",
        "UPDATE properties SET price = 0 WHERE id = 1",
        "MERGE INTO properties USING staging ON properties.id = staging.id",
    ):
        assert classify_statement(sql).classification is SqlClassification.MASS_DATA_CHANGE, sql


def test_insert_sin_on_conflict_es_unknown() -> None:
    """Un INSERT que no es idempotente no se admite como seed."""
    statement = classify_statement("INSERT INTO departments (name) VALUES ('Cortés')")

    assert statement.classification is SqlClassification.UNKNOWN
    assert "idempotente" in statement.reason


def test_select_con_efecto_no_demostrable_es_unknown() -> None:
    """Un SELECT que escribe o lee del sistema de archivos no es una lectura segura."""
    for sql in (
        "SELECT * INTO tabla_nueva FROM properties",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT * FROM dblink('host=otro', 'select 1')",
    ):
        assert classify_statement(sql).classification is SqlClassification.UNKNOWN, sql


def test_sql_no_analizable_es_unknown() -> None:
    """Una cadena o un comentario sin cerrar no se interpretan: ``UNKNOWN``."""
    for sql in (
        "SELECT 'sin cerrar",
        "SELECT 1 /* sin cerrar",
        "SELECT $$ sin cerrar",
    ):
        plan = classify_sql(sql)
        assert plan.total >= 1
        assert all(
            item.classification is SqlClassification.UNKNOWN for item in plan.statements
        ), sql
        assert plan.autonomous is False


def test_plan_vacio_no_es_autonomo() -> None:
    """Sin sentencias no hay nada que aplicar."""
    plan = classify_sql("   -- sólo un comentario\n")

    assert plan.total == 0
    assert plan.autonomous is False


def test_control_de_transaccion_del_script_es_unknown() -> None:
    """La transacción la abre el ejecutor: el script no puede manipularla."""
    statement = classify_statement("BEGIN")

    assert statement.classification is SqlClassification.UNKNOWN
    assert "transacción" in statement.reason


# ---------------------------------------------------------------------------
# 2. Destino y credencial
# ---------------------------------------------------------------------------
def test_entorno_de_produccion_no_se_autoriza() -> None:
    """Un destino declarado como producción no se puede construir."""
    with pytest.raises(DatabaseTargetError) as error:
        DatabaseTarget(
            project_id="p",
            scope=SCOPE,
            environment="production",
            expected_host=DEV_HOST,
            expected_database=DEV_DATABASE,
        )

    assert "producción" in str(error.value) or "desarrollo" in str(error.value)


def test_destino_declarado_de_produccion_no_se_autoriza() -> None:
    """Un host o una base con marca de producción se rechazan aunque el entorno sea desarrollo."""
    with pytest.raises(DatabaseTargetError):
        DatabaseTarget(
            project_id="p",
            scope=SCOPE,
            environment="development",
            expected_host="ep-prod-1.aws.neon.tech",
            expected_database=DEV_DATABASE,
        )


def test_destination_mismatch_bloquea(target: DatabaseTarget) -> None:
    """Una credencial de otro host no coincide con el destino autorizado: BLOCK."""
    otro = "postgresql://user:pass@otro-host.example.com/neondb"

    with pytest.raises(DatabaseTargetError) as error:
        assert_destination(otro, target)

    assert "BLOQUEADA" in str(error.value) or "no corresponde" in str(error.value)


def test_destination_mismatch_no_ejecuta_nada(
    target: DatabaseTarget, policy: PolicyEngine, tmp_path: Path
) -> None:
    """El mismatch se detecta antes de tocar el driver."""
    store = SecretStore(tmp_path / "secrets.json")
    store.set_project_secret(SCOPE, "postgresql://user:pass@otro-host.example.com/neondb")
    executor, driver = make_executor(target, store, policy)

    with pytest.raises(DatabaseTargetError):
        executor.connect_check()

    assert driver.executed == []
    assert driver.dsns == []


def test_dsn_con_esquema_no_postgres_se_rechaza() -> None:
    """Sólo se admiten DSN de PostgreSQL."""
    with pytest.raises(DatabaseTargetError):
        parse_dsn("mysql://user:pass@host/db")


def test_secreto_ausente_bloquea(target: DatabaseTarget, tmp_path: Path) -> None:
    """Sin secreto configurado no hay credencial: no se intenta conectar."""
    store = SecretStore(tmp_path / "secrets.json")

    with pytest.raises(DatabaseTargetError) as error:
        resolve_dsn(target, secrets=store)

    assert "no está configurado" in str(error.value)


def test_parse_dsn_no_expone_contrasena() -> None:
    """La vista pública del DSN nunca incluye la contraseña."""
    parts = parse_dsn(DEV_DSN)
    publico = parts.as_public_dict()

    assert parts.host == DEV_HOST
    assert parts.database == DEV_DATABASE
    assert DEV_PASSWORD not in json.dumps(publico)
    assert publico["user"] == "punto_canary"


# ---------------------------------------------------------------------------
# 3. SecretStore: secreto de proyecto y compatibilidad con proveedores
# ---------------------------------------------------------------------------
def test_secreto_de_proyecto_se_guarda_y_no_se_publica(tmp_path: Path) -> None:
    """El DSN vive en el almacén, fuera del repositorio, y nunca se publica."""
    store = SecretStore(tmp_path / "secrets.json")

    store.set_project_secret(SCOPE, DEV_DSN)

    assert store.has_project_secret(SCOPE) is True
    assert store.project_secret(SCOPE) == DEV_DSN
    assert store.configured_project_scopes() == (SCOPE,)
    # La costura de proveedores no lo devuelve ni lo lista.
    assert store.api_key(SCOPE) == ""
    assert store.has_api_key(SCOPE) is False
    assert SCOPE not in store.configured_providers()
    assert store.as_public_dict() == {}
    # El fichero está fuera del repositorio y no es legible por la API pública.
    assert (tmp_path / "secrets.json").is_file()


def test_secreto_de_proyecto_exige_dsn(tmp_path: Path) -> None:
    """Un secreto de proyecto que no es un DSN PostgreSQL se rechaza."""
    store = SecretStore(tmp_path / "secrets.json")

    with pytest.raises(SecretStoreError):
        store.set_project_secret(SCOPE, "no-es-un-dsn")
    with pytest.raises(SecretStoreError):
        store.set_project_secret("", DEV_DSN)


def test_compatibilidad_con_api_keys_de_proveedor(tmp_path: Path) -> None:
    """Las claves de proveedor siguen funcionando igual, conviviendo con el secreto de proyecto."""
    store = SecretStore(tmp_path / "secrets.json")

    store.set_api_key("deepseek", PROVIDER_KEY)
    store.set_project_secret(SCOPE, DEV_DSN)

    assert store.has_api_key("deepseek") is True
    assert store.api_key("deepseek") == PROVIDER_KEY
    assert store.configured_providers() == ("deepseek",)
    assert store.as_public_dict() == {"deepseek": True}
    assert store.configured_project_scopes() == (SCOPE,)
    assert store.delete_api_key("deepseek") is True
    assert store.has_project_secret(SCOPE) is True
    assert store.delete_project_secret(SCOPE) is True
    assert store.has_project_secret(SCOPE) is False


def test_redaccion_de_dsn() -> None:
    """El DSN se borra entero de cualquier texto, sin necesidad de conocerlo."""
    texto = f"fallo al conectar con {DEV_DSN} y también password=abc123"

    limpio = redact_secret_text(texto)

    assert DEV_PASSWORD not in limpio
    assert DEV_HOST not in limpio
    assert "postgresql://" not in limpio
    assert "abc123" not in limpio
    assert "postgresql://" not in redact_dsn(DEV_DSN)


# ---------------------------------------------------------------------------
# 4. Catálogo de autoridad
# ---------------------------------------------------------------------------
def test_las_acciones_de_base_de_datos_estan_catalogadas(policy: PolicyEngine) -> None:
    """Las acciones existen con el nivel que fija el modelo de autoridad."""
    esperado = {
        "db_connect_check": "LEVEL_0_AUTONOMOUS",
        "db_introspect": "LEVEL_0_AUTONOMOUS",
        "db_safe_read": "LEVEL_0_AUTONOMOUS",
        "db_migration_apply": "LEVEL_1_AUTONOMOUS_REVIEW",
        "db_seed": "LEVEL_1_AUTONOMOUS_REVIEW",
        "db_destructive_apply": "LEVEL_3_HUMAN",
        "db_mass_data_change": "LEVEL_3_HUMAN",
        "external_resource_create": "LEVEL_3_HUMAN",
    }

    for action, level in esperado.items():
        rule = policy.catalog.get(action)
        assert rule is not None, action
        assert rule.level.name == level, action


def test_destructive_y_masivo_estan_en_never_autonomous(policy: PolicyEngine) -> None:
    """Lo destructivo, lo masivo y la creación de recursos externos nunca son autónomos."""
    for action in (
        "db_destructive_apply",
        "db_mass_data_change",
        "external_resource_create",
    ):
        assert policy.catalog.is_never_autonomous(action) is True, action


def test_la_clasificacion_mapea_a_acciones_existentes() -> None:
    """Cada clasificación autónoma tiene su acción; ``UNKNOWN`` no tiene ninguna."""
    for classification in AUTONOMOUS_CLASSIFICATIONS:
        assert classification in CLASSIFICATION_ACTIONS
    assert SqlClassification.UNKNOWN not in CLASSIFICATION_ACTIONS


def test_policy_nivel_0_permite_y_nivel_1_revisa(policy: PolicyEngine) -> None:
    """Nivel 0 autoriza sin más; nivel 1 autoriza con revisión posterior obligatoria."""
    from punto.schemas.decision import ActionRequest

    lectura = policy.evaluate(ActionRequest(action="db_safe_read"))
    migracion = policy.evaluate(ActionRequest(action="db_migration_apply"))

    assert lectura.outcome.value == "ALLOW"
    assert lectura.allowed is True and lectura.requires_human is False
    assert migracion.outcome.value == "ALLOW_WITH_REVIEW"
    assert migracion.allowed is True and migracion.requires_review is True


def test_policy_nivel_3_exige_humano(policy: PolicyEngine) -> None:
    """Destructivo y masivo quedan en Human Gate, nunca en autonomía."""
    from punto.schemas.decision import ActionRequest

    for action in ("db_destructive_apply", "db_mass_data_change"):
        decision = policy.evaluate(ActionRequest(action=action, reversible=False))
        assert decision.outcome.value == "REQUIRE_HUMAN", action
        assert decision.requires_human is True
        assert decision.allowed is False


def test_una_accion_desconocida_sigue_siendo_deny(policy: PolicyEngine) -> None:
    """DEFAULT DENY intacto: una acción inventada se rechaza."""
    from punto.schemas.decision import ActionRequest

    decision = policy.evaluate(ActionRequest(action="db_borrar_todo"))

    assert decision.outcome.value == "REJECT"
    assert "DEFAULT DENY" in decision.reason


# ---------------------------------------------------------------------------
# 5. Ejecución con driver controlado
# ---------------------------------------------------------------------------
def test_connect_check_nivel_0(
    target: DatabaseTarget, secrets: SecretStore, policy: PolicyEngine
) -> None:
    """La comprobación de conectividad informa del destino, nunca del DSN."""
    audit = AuditLogger()
    executor, driver = make_executor(target, secrets, policy, audit=audit)

    status = executor.connect_check()

    assert status.ok is True
    assert status.host == DEV_HOST
    assert status.database == DEV_DATABASE
    assert status.environment == "development"
    assert driver.executed == ["SELECT 1"]
    assert DEV_DSN not in json.dumps(status.as_dict())
    assert [event.event_type for event in audit.events()] == [
        AuditEventType.DB_CONNECT_CHECKED
    ]


def test_introspect_devuelve_huella(
    target: DatabaseTarget, secrets: SecretStore, policy: PolicyEngine
) -> None:
    """La introspección lee el esquema real y calcula su huella."""
    audit = AuditLogger()
    executor, _ = make_executor(target, secrets, policy, audit=audit)

    snapshot = executor.introspect()

    assert snapshot.table_count == 1
    assert snapshot.tables["properties"] == ("id", "title")
    assert snapshot.fingerprint == schema_fingerprint({"properties": ("id", "title")})
    tipos = [event.event_type for event in audit.events()]
    assert AuditEventType.DB_SCHEMA_INTROSPECTED in tipos


def test_apply_migration_segura_nivel_1(
    target: DatabaseTarget, secrets: SecretStore, policy: PolicyEngine
) -> None:
    """Una migración no destructiva se aplica, se confirma y se audita."""
    audit = AuditLogger()
    executor, driver = make_executor(target, secrets, policy, audit=audit)
    sql = (
        "CREATE TABLE IF NOT EXISTS departments (id serial primary key, name text);\n"
        "CREATE INDEX departments_name_idx ON departments (name);"
    )

    result = executor.apply_migration(sql)

    assert result.committed is True
    assert result.statements == 2
    assert result.classifications == {"SAFE_DDL": 2}
    assert driver.last_connection.commits == 1
    assert driver.last_connection.rollbacks == 0
    assert DEV_DSN not in json.dumps(result.as_dict())
    tipos = [event.event_type for event in audit.events()]
    assert AuditEventType.DB_STATEMENT_CLASSIFIED in tipos
    assert AuditEventType.DB_MIGRATION_APPLIED in tipos


def test_run_seed_solo_admite_insert_idempotente(
    target: DatabaseTarget, secrets: SecretStore, policy: PolicyEngine
) -> None:
    """El seed aplica UPSERTs idempotentes y rechaza cualquier otra cosa."""
    audit = AuditLogger()
    executor, driver = make_executor(target, secrets, policy, audit=audit)

    result = executor.run_seed(
        "INSERT INTO departments (id, name) VALUES (1, 'Cortés') ON CONFLICT DO NOTHING"
    )

    assert result.kind == "seed"
    assert result.committed is True
    assert AuditEventType.DB_SEED_APPLIED in [e.event_type for e in audit.events()]

    with pytest.raises(DatabaseDeniedError):
        executor.run_seed("CREATE TABLE otra (id int)")
    assert driver.last_connection.commits == 1


def test_verify_solo_lectura(
    target: DatabaseTarget, secrets: SecretStore, policy: PolicyEngine
) -> None:
    """La verificación acepta SELECT y rechaza cualquier escritura."""
    audit = AuditLogger()
    executor, _ = make_executor(target, secrets, policy, audit=audit)

    result = executor.verify("SELECT count(*) FROM departments")

    assert result.row_count == 1
    assert AuditEventType.DB_QUERY_VERIFIED in [e.event_type for e in audit.events()]

    with pytest.raises(DatabaseDeniedError):
        executor.verify("UPDATE departments SET name = 'x'")


def test_drop_no_se_ejecuta_sin_aprobacion(
    target: DatabaseTarget, secrets: SecretStore, policy: PolicyEngine
) -> None:
    """SQL destructivo sin aprobación humana: se pide el gate y no se ejecuta nada."""
    audit = AuditLogger()
    gate = HumanGate()
    executor, driver = make_executor(
        target, secrets, policy, audit=audit, human_gate=gate
    )

    with pytest.raises(DatabaseApprovalRequiredError):
        executor.apply_migration("DROP TABLE properties")

    assert driver.executed == []
    assert driver.dsns == []
    tipos = [event.event_type for event in audit.events()]
    assert AuditEventType.DB_STATEMENT_CLASSIFIED in tipos
    assert AuditEventType.DB_MIGRATION_APPLIED not in tipos


def test_cambio_masivo_no_se_ejecuta_sin_aprobacion(
    target: DatabaseTarget, secrets: SecretStore, policy: PolicyEngine
) -> None:
    """UPDATE/DELETE exigen aprobación humana y no se ejecutan solos."""
    executor, driver = make_executor(target, secrets, policy, human_gate=HumanGate())

    for sql in ("DELETE FROM properties", "UPDATE properties SET price = 1"):
        with pytest.raises(DatabaseApprovalRequiredError):
            executor.apply_migration(sql)

    assert driver.executed == []


def test_destructive_con_aprobacion_humana_se_ejecuta(
    target: DatabaseTarget, secrets: SecretStore, policy: PolicyEngine
) -> None:
    """Con una aprobación humana válida del Human Gate existente, la operación procede."""
    gate = HumanGate()
    approval = gate.request(
        task_id=uuid4(),
        action="db_destructive_apply",
        risk=RiskLevel.CRITICAL,
        reason="prueba de integración con el Human Gate",
    )
    gate.approve(approval.id, resolved_by="humano-de-prueba", note="autorizado en desarrollo")
    executor, driver = make_executor(target, secrets, policy, human_gate=gate)

    result = executor.apply_migration("DROP TABLE properties", approval_id=approval.id)

    assert result.committed is True
    assert "DROP TABLE properties" in driver.executed
    assert driver.last_connection.commits == 1


def test_una_aprobacion_rechazada_no_autoriza(
    target: DatabaseTarget, secrets: SecretStore, policy: PolicyEngine
) -> None:
    """Una aprobación rechazada no habilita nada."""
    gate = HumanGate()
    approval = gate.request(
        task_id=uuid4(), action="db_destructive_apply", risk=RiskLevel.CRITICAL, reason="x"
    )
    gate.reject(approval.id, note="no autorizado")
    executor, driver = make_executor(target, secrets, policy, human_gate=gate)

    with pytest.raises(Exception) as error:
        executor.apply_migration("TRUNCATE properties", approval_id=approval.id)

    assert type(error.value).__name__ == "HumanGateNotApprovedError"
    assert driver.executed == []


def test_unknown_no_se_ejecuta(
    target: DatabaseTarget, secrets: SecretStore, policy: PolicyEngine
) -> None:
    """UNKNOWN = DENY: no se ejecuta nada, aunque el resto del script sea inocuo."""
    audit = AuditLogger()
    executor, driver = make_executor(target, secrets, policy, audit=audit)

    with pytest.raises(DatabaseDeniedError) as error:
        executor.apply_migration("CREATE TABLE a (id int); GRANT ALL ON a TO public;")

    assert "UNKNOWN = DENY" in str(error.value)
    assert driver.executed == []
    assert AuditEventType.DB_MIGRATION_REJECTED in [e.event_type for e in audit.events()]


def test_limite_de_statements_bloquea(
    target: DatabaseTarget, secrets: SecretStore, policy: PolicyEngine
) -> None:
    """El presupuesto de sentencias se comprueba antes de conectar."""
    ajustado = DatabaseTarget(
        project_id=target.project_id,
        scope=target.scope,
        environment="development",
        expected_host=DEV_HOST,
        expected_database=DEV_DATABASE,
        max_statements=1,
    )
    executor, driver = make_executor(ajustado, secrets, policy)

    with pytest.raises(DatabaseBudgetError) as error:
        executor.apply_migration("CREATE TABLE a (id int); CREATE TABLE b (id int);")

    assert "BLOQUEADA" in str(error.value)
    assert driver.dsns == []


def test_limite_de_filas_revierte(
    target: DatabaseTarget, secrets: SecretStore, policy: PolicyEngine
) -> None:
    """Superar el presupuesto de filas afectadas revierte la transacción."""
    ajustado = DatabaseTarget(
        project_id=target.project_id,
        scope=target.scope,
        environment="development",
        expected_host=DEV_HOST,
        expected_database=DEV_DATABASE,
        max_rows_affected=2,
    )
    driver = FakeDriver(
        rowcounts={"create table uno": 5},
    )
    audit = AuditLogger()
    executor, _ = make_executor(ajustado, secrets, policy, driver=driver, audit=audit)

    with pytest.raises(DatabaseBudgetError):
        executor.apply_migration("CREATE TABLE uno (id int);")

    assert driver.last_connection.rollbacks == 1
    assert driver.last_connection.commits == 0
    assert AuditEventType.DB_MIGRATION_REJECTED in [e.event_type for e in audit.events()]


def test_fallo_del_driver_revierte(
    target: DatabaseTarget, secrets: SecretStore, policy: PolicyEngine
) -> None:
    """Un fallo del driver revierte la transacción y se reporta sin DSN."""
    driver = FakeDriver(
        failures={"create index": f"duplicate key en {DEV_DSN}"},
    )
    audit = AuditLogger()
    executor, _ = make_executor(target, secrets, policy, driver=driver, audit=audit)

    with pytest.raises(DatabaseExecutionError) as error:
        executor.apply_migration("CREATE TABLE a (id int); CREATE INDEX a_idx ON a (id);")

    mensaje = str(error.value)
    assert "se revirtió" in mensaje
    assert DEV_DSN not in mensaje
    assert DEV_HOST not in mensaje
    assert driver.last_connection.rollbacks == 1
    assert driver.last_connection.commits == 0
    assert AuditEventType.DB_MIGRATION_REJECTED in [e.event_type for e in audit.events()]


def test_la_transaccion_se_abre_en_el_destino_autorizado(
    target: DatabaseTarget, secrets: SecretStore, policy: PolicyEngine
) -> None:
    """El DSN llega al driver del controlador (y sólo a él)."""
    executor, driver = make_executor(target, secrets, policy)

    executor.apply_migration("CREATE TABLE a (id int)")

    assert driver.dsns == [DEV_DSN]
    assert driver.last_connection.closed is True


# ---------------------------------------------------------------------------
# 6. Frontera de secretos
# ---------------------------------------------------------------------------
def test_dsn_ausente_de_errores_de_conexion(
    target: DatabaseTarget, secrets: SecretStore, policy: PolicyEngine
) -> None:
    """Un fallo de conexión que incluya el DSN se publica redactado."""
    driver = FakeDriver(connect_error=f"no route to {DEV_DSN}")
    executor, _ = make_executor(target, secrets, policy, driver=driver)

    with pytest.raises(DatabaseUnavailableError) as error:
        executor.connect_check()

    assert DEV_PASSWORD not in str(error.value)
    assert DEV_HOST not in str(error.value)


def test_dsn_ausente_de_auditoria(
    target: DatabaseTarget, secrets: SecretStore, policy: PolicyEngine
) -> None:
    """Ningún evento de auditoría contiene el DSN, ni siquiera si se pasa por error."""
    audit = AuditLogger()
    executor, _ = make_executor(target, secrets, policy, audit=audit)

    executor.connect_check()
    executor.introspect()
    executor.apply_migration("CREATE TABLE a (id int)")
    executor.run_seed("INSERT INTO a (id) VALUES (1) ON CONFLICT DO NOTHING")
    executor.verify("SELECT 1")

    # Segunda barrera: aunque quien llama pase el DSN como metadato, no llega al evento.
    event = audit.log_db_connect_checked(
        resource_id="prueba", metadata={"detalle": DEV_DSN, "anidado": {"dsn": DEV_DSN}}
    )

    for elemento in (*audit.events(), event):
        serializado = json.dumps(elemento.model_dump(mode="json"), default=str)
        assert DEV_PASSWORD not in serializado, elemento.event_type
        assert DEV_HOST not in serializado, elemento.event_type
    assert "postgresql://" not in json.dumps(
        [dict(evento.metadata_dict) for evento in audit.events()], default=str
    )


def test_dsn_ausente_de_provider_request(
    target: DatabaseTarget, secrets: SecretStore, policy: PolicyEngine
) -> None:
    """El contexto que puede llegar a un proveedor no lleva el DSN."""
    executor, _ = make_executor(target, secrets, policy)
    plan = executor.plan("CREATE TABLE a (id int)")

    contexto = executor.provider_context(plan)
    request = make_request(ProviderRole.BUILDER, contexto)
    serializado = json.dumps(asdict(request), default=str)

    assert DEV_PASSWORD not in contexto
    assert DEV_DSN not in contexto
    assert DEV_PASSWORD not in serializado
    assert DEV_HOST not in serializado
    assert "CREATE TABLE" not in contexto


def test_dsn_no_entra_al_entorno_de_comandos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ni el shell genérico ni un transporte de proveedor reciben la credencial."""
    monkeypatch.setenv("DATABASE_URL", DEV_DSN)

    entorno_shell = build_sanitized_environment(controlled_temp=tmp_path)
    entorno_transporte = build_transport_environment()

    assert "DATABASE_URL" not in entorno_shell
    assert "DATABASE_URL" not in entorno_transporte
    assert DEV_DSN not in json.dumps(entorno_shell)
    assert DEV_DSN not in json.dumps(entorno_transporte)


def test_el_driver_real_esta_disponible_sin_abrir_conexiones() -> None:
    """El driver del controlador está instalado y se construye sin abrir ninguna conexión.

    Ninguna prueba de esta suite abre una conexión real: psycopg se comprueba como dependencia
    disponible y el resto del camino se ejercita con el doble controlado.
    """
    import psycopg

    assert PsycopgDriver() is not None
    assert psycopg.__version__
