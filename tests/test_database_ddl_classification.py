"""PILOT-01R · R2 — DDL de migración gobernado (claves ajenas aditivas).

Evidencia del piloto: la migración inicial de Drizzle producía 17 ``SAFE_DDL`` + **8 ``UNKNOWN``**,
todas ellas ``ALTER TABLE … ADD CONSTRAINT … FOREIGN KEY …``, así que el ejecutor gobernado no podía
aplicarla completa y hubo que recurrir a ``drizzle-kit migrate``.

Aquí se prueba que la clasificación de esa forma es **estructural y conservadora**: sólo una clave
ajena completa y bien formada es ``SAFE_DDL``; todo lo demás sigue denegado o destructivo.

    pytest tests/test_database_ddl_classification.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from punto.tools.database import (
    SqlClassification,
    classify_sql,
    classify_statement,
)

#: Migración real generada durante PILOT-01 (proyecto congelado en 980f929). Si no está, la prueba
#: que la clasifica entera se salta: el resto de la batería es autónoma.
MIGRACION_PILOT01 = (
    Path(r"C:\Users\Carlos Funez\Desktop\FLIPPEAK FINAL PROYECT\punto-inmobiliario-hn")
    / "drizzle"
    / "0000_magenta_silverclaw.sql"
)

#: Las ocho claves ajenas exactas de esa migración.
CLAJES_AJENAS_REALES = (
    'ALTER TABLE "leads" ADD CONSTRAINT "leads_property_id_properties_id_fk" FOREIGN KEY '
    '("property_id") REFERENCES "public"."properties"("id") '
    "ON DELETE no action ON UPDATE no action",
    'ALTER TABLE "leads" ADD CONSTRAINT "leads_agent_id_agents_id_fk" FOREIGN KEY ("agent_id") '
    'REFERENCES "public"."agents"("id") ON DELETE no action ON UPDATE no action',
    'ALTER TABLE "municipalities" ADD CONSTRAINT "municipalities_department_id_departments_id_fk" '
    'FOREIGN KEY ("department_id") REFERENCES "public"."departments"("id") ON DELETE no action '
    "ON UPDATE no action",
    'ALTER TABLE "properties" ADD CONSTRAINT "properties_property_type_id_property_types_id_fk" '
    'FOREIGN KEY ("property_type_id") REFERENCES "public"."property_types"("id") '
    "ON DELETE no action ON UPDATE no action",
    'ALTER TABLE "properties" ADD CONSTRAINT "properties_department_id_departments_id_fk" '
    'FOREIGN KEY ("department_id") REFERENCES "public"."departments"("id") ON DELETE no action '
    "ON UPDATE no action",
    'ALTER TABLE "properties" ADD CONSTRAINT "properties_municipality_id_municipalities_id_fk" '
    'FOREIGN KEY ("municipality_id") REFERENCES "public"."municipalities"("id") '
    "ON DELETE no action ON UPDATE no action",
    'ALTER TABLE "properties" ADD CONSTRAINT "properties_agent_id_agents_id_fk" FOREIGN KEY '
    '("agent_id") REFERENCES "public"."agents"("id") ON DELETE no action ON UPDATE no action',
    'ALTER TABLE "property_media" ADD CONSTRAINT "property_media_property_id_properties_id_fk" '
    'FOREIGN KEY ("property_id") REFERENCES "public"."properties"("id") ON DELETE cascade '
    "ON UPDATE no action",
)


@pytest.mark.parametrize("sql", CLAJES_AJENAS_REALES)
def test_las_claves_ajenas_reales_son_ddl_seguro(sql: str) -> None:
    """Las ocho claves ajenas de la migración real dejan de ser ``UNKNOWN``."""
    statement = classify_statement(sql)

    assert statement.classification is SqlClassification.SAFE_DDL
    assert statement.autonomous is True
    assert "ajena" in statement.reason


@pytest.mark.parametrize(
    "sql",
    (
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (b)",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a,b) REFERENCES u (x,y)",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES s.u (b) ON DELETE CASCADE",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (b) ON UPDATE SET NULL",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (b) ON DELETE RESTRICT "
        "ON UPDATE SET DEFAULT",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (b) ON UPDATE no action "
        "ON DELETE no action",
        'ALTER TABLE "t" ADD CONSTRAINT "c" FOREIGN KEY ("a") REFERENCES "u" ("b")',
        "alter table t add constraint c foreign key (a) references u (b);",
    ),
)
def test_formas_aditivas_de_clave_ajena(sql: str) -> None:
    """La clave ajena completa es DDL aditivo; el orden de las cláusulas no importa."""
    assert classify_statement(sql).classification is SqlClassification.SAFE_DDL


@pytest.mark.parametrize(
    "sql",
    (
        "ALTER TABLE t ADD CONSTRAINT c CHECK (a > 0)",
        "ALTER TABLE t ADD CONSTRAINT c UNIQUE (a)",
        "ALTER TABLE t ADD CONSTRAINT c PRIMARY KEY (a)",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u ()",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY () REFERENCES u (b)",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a REFERENCES u (b)",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (b) DEFERRABLE",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (b) NOT VALID",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (b) MATCH FULL",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (b) ON DELETE TELEPORT",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (b) ON DELETE CASCADE "
        "ON DELETE CASCADE",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (b), "
        "ADD CONSTRAINT d FOREIGN KEY (e) REFERENCES v (f)",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (b) NOTHING",
        "ALTER TABLE t ADD FOREIGN KEY (a) REFERENCES u (b)",
    ),
)
def test_constraints_no_demostradas_siguen_cerradas(sql: str) -> None:
    """Sin la forma exacta y completa, la sentencia es ``UNKNOWN``: no se adivina."""
    statement = classify_statement(sql)

    assert statement.classification is SqlClassification.UNKNOWN, sql
    assert statement.autonomous is False


@pytest.mark.parametrize(
    "sql",
    (
        "ALTER TABLE t DROP CONSTRAINT c",
        "ALTER TABLE t DROP COLUMN c",
        "DROP TABLE t",
        "TRUNCATE t",
    ),
)
def test_lo_destructivo_sigue_siendo_destructivo(sql: str) -> None:
    """Ni ``DROP CONSTRAINT`` ni ningún otro destructivo se vuelven DDL seguro."""
    statement = classify_statement(sql)

    assert statement.classification is SqlClassification.DESTRUCTIVE, sql
    assert statement.autonomous is False


@pytest.mark.parametrize(
    "sql",
    (
        "ALTER TABLE t RENAME COLUMN a TO b",
        "ALTER TABLE t ALTER COLUMN a TYPE text",
        "ALTER TABLE t ADD CONSTRAINT c CHECK (a > 0)",
    ),
)
def test_los_cambios_de_esquema_no_autorizados_se_rechazan(sql: str) -> None:
    """Renombrar, cambiar tipos o añadir CHECK no están admitidos: se rechazan, no se adivinan."""
    statement = classify_statement(sql)

    assert statement.classification is SqlClassification.UNKNOWN, sql
    assert statement.autonomous is False


def test_multi_sentencia_camuflada_no_es_autonoma() -> None:
    """Una clave ajena seguida de un destructivo no puede colarse como un único plan seguro."""
    plan = classify_sql(
        'ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (b); DROP TABLE u;'
    )

    assert plan.total == 2
    assert plan.statements[0].classification is SqlClassification.SAFE_DDL
    assert plan.statements[1].classification is SqlClassification.DESTRUCTIVE
    assert plan.autonomous is False


def test_los_comentarios_no_esconden_ni_inventan_claves_ajenas() -> None:
    """Los comentarios se eliminan antes de analizar: no cambian lo que la sentencia es."""
    con_comentarios = classify_sql(
        "/* nota */ ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (b) /* fin */"
    )
    comentario_en_medio = classify_sql(
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (b) -- nota final"
    )
    destructiva_escondida = classify_sql("ALTER TABLE t DROP/* oculto */ CONSTRAINT c")
    palabra_partida = classify_sql("ALT/* x */ER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) "
                                   "REFERENCES u (b)")

    assert con_comentarios.statements[0].classification is SqlClassification.SAFE_DDL
    assert comentario_en_medio.statements[0].classification is SqlClassification.SAFE_DDL
    assert destructiva_escondida.statements[0].classification is SqlClassification.DESTRUCTIVE, (
        "un comentario no puede ocultar un DROP"
    )
    assert palabra_partida.statements[0].classification is SqlClassification.UNKNOWN, (
        "partir una palabra clave con un comentario no crea una sentencia válida"
    )


def test_identificadores_inyectados_no_amplian_la_forma() -> None:
    """Una sentencia destructiva añadida después no viaja dentro del mismo plan seguro."""
    for sql in (
        "ALTER TABLE t; DROP TABLE u; ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (b)",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (b); DROP TABLE u;",
    ):
        plan = classify_sql(sql)
        assert plan.autonomous is False, sql


@pytest.mark.skipif(
    not MIGRACION_PILOT01.is_file(), reason="la migración real de PILOT-01 no está disponible"
)
def test_la_migracion_real_de_pilot01_se_clasifica_entera() -> None:
    """Aceptación de R2: la migración completa deja de tener ``UNKNOWN`` injustificados."""
    plan = classify_sql(MIGRACION_PILOT01.read_text(encoding="utf-8"))

    assert plan.total == 25
    assert plan.count(SqlClassification.UNKNOWN) == 0
    assert plan.classifications == {"SAFE_DDL": 25}
    assert plan.autonomous is True
