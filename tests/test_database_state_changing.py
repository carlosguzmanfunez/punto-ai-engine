"""PILOT-01R · R3 — funciones que cambian estado y gobierno de secuencias.

Hallazgo del piloto: ``SELECT setval(...)`` / ``SELECT nextval(...)`` se clasificaban como
``SAFE_READ`` aunque **modifican estado**. Esta batería prueba que la semántica de autoridad
ya no se puede burlar empezando una sentencia por ``SELECT``, y que la alineación legítima de una
secuencia tiene una forma gobernada propia.

    pytest tests/test_database_state_changing.py -q
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from punto.audit.logger import AuditLogger
from punto.policy.policy_engine import PolicyEngine
from punto.providers.secrets import SecretStore
from punto.schemas.audit import AuditEventType
from punto.tools.database import (
    DatabaseExecutor,
    DatabaseTarget,
    SqlClassification,
    classify_sql,
    classify_statement,
)

CONFIG_DIR = Path("config")
DSN = "postgresql://punto_canary:canary-not-a-real-secret@ep-dev-1.aws.neon.tech/neondb"


# ---------------------------------------------------------------------------
# Lecturas que siguen siendo lecturas
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    (
        "SELECT 1",
        "SELECT * FROM properties",
        "SELECT p.code FROM properties p JOIN property_types pt ON pt.id = p.type_id",
        "SELECT count(*) FROM properties WHERE status = 'PUBLISHED'",
        "SELECT currval('properties_id_seq')",
        "SHOW search_path",
    ),
)
def test_las_lecturas_siguen_siendo_lecturas(sql: str) -> None:
    """Una lectura real no cambia de clase; ``currval`` es una consulta de sesión."""
    statement = classify_statement(sql)

    assert statement.classification is SqlClassification.SAFE_READ, sql
    assert statement.autonomous is True


# ---------------------------------------------------------------------------
# Escrituras escondidas en un SELECT
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    (
        "SELECT nextval('properties_id_seq')",
        "SELECT setval('properties_id_seq', 33)",
        "SELECT setval('properties_id_seq', (SELECT max(id) FROM properties))",
        "select SETVAL ( 's' , 1 )",
        "SELECT pg_catalog.setval('s', 1)",
        "SELECT pg_catalog.nextval('s')",
        "WITH x AS (SELECT setval('s', 1)) SELECT * FROM x",
        "SELECT * FROM (SELECT nextval('s') AS v) t",
        "SELECT a FROM t WHERE b = nextval('s')",
        "INSERT INTO t (id) VALUES (nextval('s')) ON CONFLICT DO NOTHING",
        "SELECT set_config('search_path', 'public', false)",
        "SELECT pg_terminate_backend(1)",
        "SELECT pg_cancel_backend(1)",
        "SELECT pg_reload_conf()",
        "SELECT pg_create_restore_point('x')",
        "SELECT pg_advisory_lock(1)",
        "SELECT pg_try_advisory_xact_lock(1)",
        "SELECT lo_unlink(1)",
        "SELECT pg_notify('canal', 'mensaje')",
    ),
)
def test_una_funcion_que_cambia_estado_nunca_es_lectura(sql: str) -> None:
    """Ni el ``SELECT`` ni un CTE ni una subconsulta convierten una escritura en ``SAFE_READ``."""
    statement = classify_statement(sql)

    assert statement.classification is not SqlClassification.SAFE_READ, sql
    assert statement.classification is SqlClassification.UNKNOWN, sql
    assert statement.autonomous is False
    assert "cambia estado" in statement.reason


def test_un_cte_que_inserta_con_nextval_no_es_autonomo() -> None:
    """Un CTE que ejecuta DML sigue siendo cambio de datos: ``nextval`` no lo abarata."""
    statement = classify_statement(
        "WITH x AS (SELECT nextval('s') AS v) "
        "INSERT INTO t (id) VALUES (1) ON CONFLICT DO NOTHING"
    )

    assert statement.classification is not SqlClassification.SAFE_READ
    assert statement.autonomous is False
    assert statement.classification is SqlClassification.MASS_DATA_CHANGE


def test_mencion_en_un_literal_tambien_se_deniega() -> None:
    """Un literal que menciona la función se deniega: la frontera no adivina."""
    statement = classify_statement("SELECT 'setval(' AS texto")

    assert statement.classification is SqlClassification.UNKNOWN


def test_la_escritura_de_secuencia_no_escala_a_una_clase_peor() -> None:
    """La clase denegada no se confunde con destructivo ni con cambio masivo de datos."""
    statement = classify_statement("SELECT setval('s', 1)")

    assert statement.classification is SqlClassification.UNKNOWN
    assert statement.classification is not SqlClassification.DESTRUCTIVE
    assert statement.classification is not SqlClassification.MASS_DATA_CHANGE


# ---------------------------------------------------------------------------
# Gobierno de secuencias (§4): la forma admitida es ALTER SEQUENCE ... RESTART
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    (
        "ALTER SEQUENCE properties_id_seq RESTART WITH 33",
        "ALTER SEQUENCE properties_id_seq RESTART 33",
        'ALTER SEQUENCE "public"."properties_id_seq" RESTART WITH 33',
        "alter sequence s restart with 1;",
    ),
)
def test_la_alineacion_gobernada_de_secuencia_es_ddl_seguro(sql: str) -> None:
    """``ALTER SEQUENCE … RESTART [WITH] <entero>`` es aditivo y no puede perder datos."""
    statement = classify_statement(sql)

    assert statement.classification is SqlClassification.SAFE_DDL, sql
    assert statement.autonomous is True
    assert "secuencia" in statement.reason


@pytest.mark.parametrize(
    "sql",
    (
        "ALTER SEQUENCE s RESTART",
        "ALTER SEQUENCE s RESTART WITH -1",
        "ALTER SEQUENCE s RESTART WITH 1.5",
        "ALTER SEQUENCE s RESTART WITH abc",
        "ALTER SEQUENCE s OWNER TO otro",
        "ALTER SEQUENCE s INCREMENT BY 10",
        "ALTER SEQUENCE s RENAME TO otra",
        "ALTER SEQUENCE s RESTART WITH 5, RESTART WITH 6",
        "ALTER SEQUENCE s RESTART WITH 5 NOTHING",
    ),
)
def test_la_forma_no_admitida_de_secuencia_sigue_cerrada(sql: str) -> None:
    """Sin el entero explícito y sin la forma exacta, la sentencia queda denegada."""
    statement = classify_statement(sql)

    assert statement.classification is SqlClassification.UNKNOWN, sql
    assert statement.autonomous is False


def test_la_alineacion_se_aplica_por_la_ruta_gobernada() -> None:
    """La alineación recorre el ejecutor: política de nivel 1, transacción y auditoría."""
    target = DatabaseTarget(
        project_id="punto-inmobiliario-hn",
        scope="punto-inmobiliario-hn",
        environment="development",
        expected_host="ep-dev-1.aws.neon.tech",
        expected_database="neondb",
    )
    secrets = SecretStore(Path(__file__).parent / ".secrets-tmp.json")
    secrets.set_project_secret("punto-inmobiliario-hn", DSN)
    audit = AuditLogger()
    driver = _FakeDriver()
    executor = DatabaseExecutor(
        target=target,
        secrets=secrets,
        policy=PolicyEngine.from_config(CONFIG_DIR, environment="local"),
        audit=audit,
        driver=driver,
    )

    result = executor.apply_migration("ALTER SEQUENCE properties_id_seq RESTART WITH 33")

    assert result.committed is True
    assert any("RESTART WITH 33" in sentencia for sentencia in driver.executed)
    assert AuditEventType.DB_MIGRATION_APPLIED in [event.event_type for event in audit.events()]
    secrets.delete_project_secret("punto-inmobiliario-hn")
    Path(secrets.path).unlink(missing_ok=True)


def test_un_script_que_mezcla_alineacion_y_destructivo_no_es_autonomo() -> None:
    """La alineación no puede arrastrar un destructivo en el mismo plan."""
    plan = classify_sql("ALTER SEQUENCE s RESTART WITH 5; DROP TABLE properties;")

    assert plan.total == 2
    assert plan.statements[0].classification is SqlClassification.SAFE_DDL
    assert plan.statements[1].classification is SqlClassification.DESTRUCTIVE
    assert plan.autonomous is False


class _FakeDriver:
    """Driver mínimo: registra lo ejecutado y no abre ninguna conexión real."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def connect(self, dsn: str, *, timeout: float) -> Any:
        """Devuelve una conexión simulada."""
        del dsn, timeout
        return _FakeConnection(self)


class _FakeConnection:
    """Conexión simulada con commit/rollback contabilizados."""

    def __init__(self, driver: _FakeDriver) -> None:
        self._driver = driver
        self.commits = 0

    def cursor(self) -> Any:
        """Cursor simulado."""
        return _FakeCursor(self._driver)

    def commit(self) -> None:
        """Confirma."""
        self.commits += 1

    def rollback(self) -> None:
        """Revierte."""
        return None

    def close(self) -> None:
        """Cierra."""
        return None


class _FakeCursor:
    """Cursor simulado: registra sentencias y responde a la introspección."""

    def __init__(self, driver: _FakeDriver) -> None:
        self._driver = driver
        self.rowcount = -1

    def execute(self, sql: str) -> None:
        """Registra la sentencia."""
        self._driver.executed.append(sql)

    def fetchone(self) -> tuple[int]:
        """Una fila sintética."""
        return (1,)

    def fetchall(self) -> list[tuple[str, str]]:
        """Esquema sintético."""
        return [("properties", "id")]

    def close(self) -> None:
        """Cierra."""
        return None
