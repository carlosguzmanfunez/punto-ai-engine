"""Ejecutor de base de datos con autoridad (DB AUTHORITY EXECUTOR v0).

PUNTO puede operar **una base PostgreSQL/Neon exclusivamente de desarrollo** después de una
autorización humana inicial, sin pedir permiso en cada operación rutinaria. Este módulo es el
controlador de esa capacidad y nada más: no es un ORM, no implementa Drizzle ni un cliente de
Neon, y no conoce ningún proveedor de modelo. **PUNTO ejecuta SQL**; el proyecto sigue usando su
propio ORM.

Frontera de autoridad:

- el **BUILDER** genera esquema, migraciones y seeds como artefactos; nunca recibe el DSN, nunca
  ejecuta SQL contra la base y nunca administra recursos externos;
- el **controlador** (este módulo) custodia la referencia al secreto, valida el destino, conecta,
  introspecciona, **clasifica cada sentencia**, consulta el Policy Engine, ejecuta sólo lo
  autorizado dentro de una transacción, verifica y audita.

Invariantes que este módulo hace cumplir en código:

- ``UNKNOWN = DENY``: lo que el clasificador no reconoce no se ejecuta nunca;
- ``PRODUCTION = NO AUTONOMOUS EXECUTION``: sólo se admiten entornos de desarrollo y el destino
  declarado tiene que coincidir con la credencial presentada;
- el DSN **nunca** sale de aquí: ni a un prompt, ni a un evento de auditoría, ni a un error, ni a
  una respuesta; sólo viaja al driver del controlador;
- toda migración va en **una transacción**: si algo falla, ``ROLLBACK`` y se para; no se continúa
  con una alternativa más agresiva.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Protocol, cast
from urllib.parse import parse_qs, urlsplit

from punto.audit.logger import AuditLogger
from punto.policy.policy_engine import PolicyEngine
from punto.providers.secrets import SecretStore, redact_secret_text
from punto.schemas.decision import ActionRequest
from punto.schemas.enums import RiskLevel

# ---------------------------------------------------------------------------
# Entornos y presupuestos
# ---------------------------------------------------------------------------
#: Entorno canónico de la autonomía de datos.
DEVELOPMENT_ENVIRONMENT: Final[str] = "development"

#: Entornos en los que el motor puede operar la base sin aprobación humana por operación.
#: ``staging`` y ``production`` **no** están aquí: la ejecución autónoma es sólo de desarrollo.
DEVELOPMENT_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"development", "local", "test"})

#: Marcas que delatan un destino de producción. Es una barrera barata y conservadora: si el host o
#: la base las contienen, se bloquea aunque el entorno declarado sea de desarrollo.
PRODUCTION_MARKERS: Final[tuple[str, ...]] = ("prod", "production", "live", "master", "main")

#: Máximo de sentencias por operación cuando el destino no declara otro.
DEFAULT_MAX_STATEMENTS: Final[int] = 50

#: Máximo de filas afectadas acumuladas cuando el destino no declara otro.
DEFAULT_MAX_ROWS_AFFECTED: Final[int] = 5_000

#: Tiempo máximo de conexión y de sentencia, en segundos.
DEFAULT_CONNECT_TIMEOUT_SECONDS: Final[float] = 15.0

#: Esquemas de DSN admitidos. Cualquier otro se rechaza.
POSTGRES_SCHEMES: Final[frozenset[str]] = frozenset({"postgres", "postgresql"})


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------
class DatabaseError(RuntimeError):
    """Base de los fallos del ejecutor de base de datos."""


class DatabaseTargetError(DatabaseError):
    """El destino no está autorizado: entorno no de desarrollo, secreto ausente o DSN distinto."""


class DatabaseDeniedError(DatabaseError):
    """La política denegó la operación: acción no catalogada, SQL desconocido o destructivo."""


class DatabaseApprovalRequiredError(DatabaseError):
    """La operación exige aprobación humana y no hay una aprobación válida."""


class DatabaseBudgetError(DatabaseError):
    """Se excedió el presupuesto declarado de la operación (sentencias o filas)."""


class DatabaseUnavailableError(DatabaseError):
    """No hay driver disponible o la conexión no se pudo establecer."""


class DatabaseExecutionError(DatabaseError):
    """El driver falló al aplicar la operación. La transacción ya se revirtió."""


class SqlParseError(DatabaseError):
    """El SQL no se pudo analizar de forma conservadora. Se trata como ``UNKNOWN``."""


# ---------------------------------------------------------------------------
# Clasificación de sentencias
# ---------------------------------------------------------------------------
class SqlClassification(StrEnum):
    """Vocabulario cerrado de clasificación. Lo que no encaja es ``UNKNOWN``."""

    SAFE_READ = "SAFE_READ"
    SAFE_DDL = "SAFE_DDL"
    SAFE_SEED = "SAFE_SEED"
    DESTRUCTIVE = "DESTRUCTIVE"
    MASS_DATA_CHANGE = "MASS_DATA_CHANGE"
    UNKNOWN = "UNKNOWN"


#: Clasificaciones que el motor puede aplicar por sí mismo en desarrollo.
AUTONOMOUS_CLASSIFICATIONS: Final[frozenset[SqlClassification]] = frozenset(
    {
        SqlClassification.SAFE_READ,
        SqlClassification.SAFE_DDL,
        SqlClassification.SAFE_SEED,
    }
)

#: Acción del catálogo de autoridad que corresponde a cada clasificación.
CLASSIFICATION_ACTIONS: Final[Mapping[SqlClassification, str]] = {
    SqlClassification.SAFE_READ: "db_safe_read",
    SqlClassification.SAFE_DDL: "db_migration_apply",
    SqlClassification.SAFE_SEED: "db_seed",
    SqlClassification.DESTRUCTIVE: "db_destructive_apply",
    SqlClassification.MASS_DATA_CHANGE: "db_mass_data_change",
}

#: Palabras que, dentro de un ``SELECT``, convierten la sentencia en escritura o en efecto no
#: demostrable. Conservador a propósito.
SELECT_FORBIDDEN: Final[tuple[str, ...]] = (
    " into ",
    " insert ",
    " update ",
    " delete ",
    " merge ",
    " for update",
    " for share",
    " pg_read_file",
    " pg_read_binary_file",
    " pg_ls_dir",
    " lo_import",
    " lo_export",
    " dblink",
    " copy ",
)

#: Funciones y formas que nunca se aceptan dentro de una sentencia "segura".
UNSAFE_TOKENS: Final[tuple[str, ...]] = (
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "lo_import",
    "lo_export",
    "dblink",
    "pg_sleep",
)

#: Sentencias que abren o cierran transacción: las controla el ejecutor, no el script.
TRANSACTION_KEYWORDS: Final[tuple[str, ...]] = (
    "BEGIN",
    "START TRANSACTION",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT",
    "RELEASE SAVEPOINT",
    "END",
    "ABORT",
)

#: Identificador PostgreSQL: simple o citado, y opcionalmente cualificado por esquema.
_PG_IDENTIFIER: Final[str] = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
_PG_QUALIFIED: Final[str] = rf"{_PG_IDENTIFIER}(?:\s*\.\s*{_PG_IDENTIFIER})?"

#: Forma **estrecha** de creación de un tipo enumerado: ``CREATE TYPE <id> AS ENUM ('v', …)``.
#: Es la única variante de ``CREATE TYPE`` que el motor admite en autonomía, porque es aditiva y no
#: puede perder datos. Cualquier otra forma queda como ``UNKNOWN`` (denegada).
_ENUM_CREATE: Final[re.Pattern[str]] = re.compile(
    rf"^CREATE\s+TYPE\s+{_PG_QUALIFIED}\s+AS\s+ENUM\s*\((?P<values>.*)\)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)

#: Lista de columnas de una restricción: identificadores separados por comas.
_IDENT_LIST: Final[str] = rf"{_PG_IDENTIFIER}(?:\s*,\s*{_PG_IDENTIFIER})*"

#: Acciones referenciales admitidas por PostgreSQL en una clave ajena.
_REFERENTIAL_ACTION: Final[str] = r"(?:NO\s+ACTION|RESTRICT|CASCADE|SET\s+NULL|SET\s+DEFAULT)"

#: Cabecera de una clave ajena añadida con ``ALTER TABLE``: la única forma de ``ADD CONSTRAINT``
#: que el motor admite, porque es **aditiva** (no puede perder datos).
_FK_HEAD: Final[re.Pattern[str]] = re.compile(
    rf"^ALTER\s+TABLE\s+{_PG_QUALIFIED}\s+ADD\s+CONSTRAINT\s+{_PG_IDENTIFIER}\s+"
    rf"FOREIGN\s+KEY\s*\({_IDENT_LIST}\)\s+REFERENCES\s+{_PG_QUALIFIED}\s*\({_IDENT_LIST}\)",
    re.IGNORECASE,
)

#: Cláusulas que pueden seguir a una clave ajena: ``ON DELETE`` y ``ON UPDATE``, una vez cada una.
_FK_TAIL: Final[re.Pattern[str]] = re.compile(
    rf"^(?:\s+ON\s+(?P<kind>DELETE|UPDATE)\s+(?P<action>{_REFERENTIAL_ACTION}))", re.IGNORECASE
)

#: Texto que puede quedar al final de una sentencia ya completa.
_TRAILING: Final[re.Pattern[str]] = re.compile(r"^\s*;?\s*$")

def _classify_add_foreign_key(normalized: str) -> tuple[SqlClassification, str]:
    """Clasifica ``ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY ...``.

    Se analiza de forma estructural: cabecera exacta (tabla, nombre, columnas locales, tabla y
    columnas referenciadas) y, después, sólo las cláusulas ``ON DELETE``/``ON UPDATE`` con acciones
    admitidas y **una vez cada una**. Cualquier otra cosa (``CHECK``, ``UNIQUE``, ``PRIMARY KEY``,
    ``NOT VALID``, ``DEFERRABLE``, una segunda restricción en la misma sentencia, texto sobrante) no
    encaja y la sentencia queda como ``UNKNOWN``.
    """
    cabecera = _FK_HEAD.match(normalized)
    if cabecera is None:
        return (
            SqlClassification.UNKNOWN,
            "ADD CONSTRAINT no admitido: sólo FOREIGN KEY con REFERENCES e identificadores válidos",
        )
    resto = normalized[cabecera.end() :]
    vistas: set[str] = set()
    while True:
        clausula = _FK_TAIL.match(resto)
        if clausula is None:
            break
        tipo = clausula.group("kind").upper()
        if tipo in vistas:
            return SqlClassification.UNKNOWN, f"cláusula ON {tipo} repetida en la clave ajena"
        vistas.add(tipo)
        resto = resto[clausula.end() :]
    if not _TRAILING.match(resto):
        return (
            SqlClassification.UNKNOWN,
            "texto no admitido tras la clave ajena (opciones no soportadas)",
        )
    acciones = f" con {' y '.join(sorted(f'ON {tipo}' for tipo in vistas))}" if vistas else ""
    return SqlClassification.SAFE_DDL, f"clave ajena aditiva{acciones}"


def _parse_enum_values(values: str) -> tuple[str, ...] | None:
    """Interpreta la lista de valores de un ``ENUM``.

    Sólo se admite una lista **no vacía** de literales de cadena separados por comas, sin coma
    final, con los escapes de PostgreSQL (``''`` y ``\\'``). Cualquier otra cosa devuelve ``None``:
    no se interpreta la semántica de los valores, sólo se demuestra que la forma es la segura.
    """
    index = 0
    length = len(values)
    found: list[str] = []
    while index < length:
        while index < length and values[index].isspace():
            index += 1
        if index >= length or values[index] != "'":
            return None
        index += 1
        closed = False
        while index < length:
            char = values[index]
            if char == "\\" and index + 1 < length:
                index += 2
                continue
            if char == "'":
                if index + 1 < length and values[index + 1] == "'":
                    index += 2
                    continue
                index += 1
                closed = True
                break
            index += 1
        if not closed:
            return None
        found.append("value")
        while index < length and values[index].isspace():
            index += 1
        if index >= length:
            break
        if values[index] != ",":
            return None
        index += 1
        probe = index
        while probe < length and values[probe].isspace():
            probe += 1
        if probe >= length:
            return None
    return tuple(found) if found else None


def _classify_enum_type(normalized: str) -> tuple[SqlClassification, str]:
    """Clasifica un ``CREATE TYPE``: sólo ``AS ENUM (...)`` bien formado es ``SAFE_DDL``."""
    match = _ENUM_CREATE.match(normalized)
    if match is None:
        return (
            SqlClassification.UNKNOWN,
            "CREATE TYPE no admitido: sólo la forma CREATE TYPE <id> AS ENUM ('v', ...)",
        )
    values = _parse_enum_values(match.group("values"))
    if not values:
        return (
            SqlClassification.UNKNOWN,
            "CREATE TYPE ... AS ENUM mal formado o con valores que no son literales de cadena",
        )
    return SqlClassification.SAFE_DDL, f"tipo enumerado con {len(values)} valor(es)"


def statement_sha256(statement: str) -> str:
    """SHA-256 del texto de una sentencia, para evidencia sin exponer su contenido."""
    return hashlib.sha256(statement.strip().encode("utf-8")).hexdigest()


def script_sha256(sql: str) -> str:
    """SHA-256 del script completo, para evidencia."""
    return hashlib.sha256(sql.strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SqlStatement:
    """Una sentencia clasificada: qué es, por qué y su huella."""

    text: str
    classification: SqlClassification
    reason: str

    @property
    def sha256(self) -> str:
        """Huella de la sentencia."""
        return statement_sha256(self.text)

    @property
    def autonomous(self) -> bool:
        """True sólo si el motor puede aplicarla sin aprobación humana."""
        return self.classification in AUTONOMOUS_CLASSIFICATIONS


@dataclass(frozen=True, slots=True)
class SqlPlan:
    """Plan de ejecución: todas las sentencias clasificadas, con sus recuentos."""

    statements: tuple[SqlStatement, ...] = ()
    source_sha256: str = ""
    note: str = ""

    @property
    def total(self) -> int:
        """Número de sentencias del plan."""
        return len(self.statements)

    def count(self, classification: SqlClassification) -> int:
        """Cuántas sentencias hay de una clasificación."""
        return sum(1 for item in self.statements if item.classification is classification)

    @property
    def classifications(self) -> dict[str, int]:
        """Recuento por clasificación, sólo de las presentes, en orden del vocabulario."""
        return {
            item.value: self.count(item)
            for item in SqlClassification
            if self.count(item) > 0
        }

    @property
    def blocking(self) -> tuple[SqlStatement, ...]:
        """Sentencias que impiden la ejecución autónoma (desconocidas o destructivas)."""
        return tuple(item for item in self.statements if not item.autonomous)

    @property
    def autonomous(self) -> bool:
        """True sólo si **todas** las sentencias son aplicables en autonomía."""
        return bool(self.statements) and all(item.autonomous for item in self.statements)

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable para auditoría y evidencia (sin SQL)."""
        return {
            "statements": self.total,
            "classifications": self.classifications,
            "source_sha256": self.source_sha256,
            "autonomous": self.autonomous,
            "statement_sha256": [item.sha256 for item in self.statements],
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Análisis conservador del SQL
# ---------------------------------------------------------------------------
_DOLLAR_TAG: Final[re.Pattern[str]] = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def _strip_comments(sql: str) -> str:
    """Quita comentarios ``--`` y ``/* */`` (anidados) respetando cadenas y citas.

    Raises:
        SqlParseError: si una cadena, una cita o un comentario queda abierto. Un SQL que no se
            entiende no se interpreta: se trata como ``UNKNOWN``.
    """
    out: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "-" and sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = length if newline == -1 else newline
            out.append(" ")
            continue
        if char == "/" and sql.startswith("/*", index):
            depth = 1
            index += 2
            while index < length and depth:
                if sql.startswith("/*", index):
                    depth += 1
                    index += 2
                elif sql.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise SqlParseError("comentario de bloque sin cerrar")
            out.append(" ")
            continue
        if char == "'":
            out.append(char)
            index += 1
            while index < length:
                if sql[index] == "\\" and index + 1 < length:
                    out.append(sql[index : index + 2])
                    index += 2
                    continue
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        out.append("''")
                        index += 2
                        continue
                    out.append("'")
                    index += 1
                    break
                out.append(sql[index])
                index += 1
            else:
                raise SqlParseError("cadena sin cerrar")
            continue
        if char == '"':
            out.append(char)
            index += 1
            while index < length and sql[index] != '"':
                out.append(sql[index])
                index += 1
            if index >= length:
                raise SqlParseError("identificador entre comillas sin cerrar")
            out.append('"')
            index += 1
            continue
        if char == "$":
            match = _DOLLAR_TAG.match(sql, index)
            if match:
                tag = match.group(0)
                end = sql.find(tag, match.end())
                if end == -1:
                    raise SqlParseError("bloque entre dólares sin cerrar")
                out.append(sql[match.start() : end + len(tag)])
                index = end + len(tag)
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _split_statements(clean_sql: str) -> tuple[str, ...]:
    """Parte el SQL en sentencias por ``;`` de primer nivel, respetando cadenas y citas."""
    statements: list[str] = []
    current: list[str] = []
    index = 0
    length = len(clean_sql)
    while index < length:
        char = clean_sql[index]
        if char == "'":
            current.append(char)
            index += 1
            while index < length:
                if clean_sql[index] == "\\" and index + 1 < length:
                    current.append(clean_sql[index : index + 2])
                    index += 2
                    continue
                if clean_sql[index] == "'":
                    if index + 1 < length and clean_sql[index + 1] == "'":
                        current.append("''")
                        index += 2
                        continue
                    current.append("'")
                    index += 1
                    break
                current.append(clean_sql[index])
                index += 1
            else:
                raise SqlParseError("cadena sin cerrar")
            continue
        if char == '"':
            current.append(char)
            index += 1
            while index < length and clean_sql[index] != '"':
                current.append(clean_sql[index])
                index += 1
            if index >= length:
                raise SqlParseError("identificador entre comillas sin cerrar")
            current.append('"')
            index += 1
            continue
        if char == "$":
            match = _DOLLAR_TAG.match(clean_sql, index)
            if match:
                tag = match.group(0)
                end = clean_sql.find(tag, match.end())
                if end == -1:
                    raise SqlParseError("bloque entre dólares sin cerrar")
                current.append(clean_sql[match.start() : end + len(tag)])
                index = end + len(tag)
                continue
        if char == ";":
            text = "".join(current).strip()
            if text:
                statements.append(text)
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return tuple(statements)


def _first_words(normalized: str, count: int = 4) -> tuple[str, ...]:
    """Primeras palabras de una sentencia normalizada."""
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", normalized.upper())
    return tuple(words[:count])


def _padded(normalized: str) -> str:
    """Texto en minúsculas con espacios alrededor, para buscar palabras completas."""
    return f" {' '.join(normalized.lower().split())} "


def _classify_normalized(normalized: str) -> tuple[SqlClassification, str]:
    """Clasifica una sentencia ya normalizada (sin comentarios). Conservador."""
    upper = normalized.upper()
    padded = _padded(normalized)
    words = _first_words(normalized)

    if not words:
        return SqlClassification.UNKNOWN, "sentencia vacía"

    if upper in TRANSACTION_KEYWORDS:
        return (
            SqlClassification.UNKNOWN,
            "el control de transacción lo ejerce el ejecutor, no el script",
        )

    # 1. Destructivo explícito, siempre primero.
    if words[0] in {"DROP", "TRUNCATE"}:
        return SqlClassification.DESTRUCTIVE, f"{words[0]} elimina estructura o datos"
    if words[0] == "ALTER" and " drop " in padded:
        return SqlClassification.DESTRUCTIVE, "ALTER TABLE ... DROP elimina estructura"

    # 2. Cambio masivo de datos.
    if words[0] in {"UPDATE", "DELETE", "MERGE"}:
        return (
            SqlClassification.MASS_DATA_CHANGE,
            f"{words[0]} altera filas: exige aprobación humana en v0",
        )
    if words[0] == "WITH" and any(
        f" {keyword} " in padded for keyword in ("insert", "update", "delete", "merge")
    ):
        return SqlClassification.MASS_DATA_CHANGE, "WITH que ejecuta DML: cambio de datos"

    # 3. Efectos no demostrables dentro de una sentencia que parecía segura.
    for token in UNSAFE_TOKENS:
        if token in padded:
            return SqlClassification.UNKNOWN, f"función no admitida: {token}"

    # 4. Lectura.
    if words[0] in {"SELECT", "SHOW", "VALUES"}:
        for forbidden in SELECT_FORBIDDEN:
            if forbidden in padded:
                return SqlClassification.UNKNOWN, f"SELECT con efecto no demostrable: {forbidden}"
        return SqlClassification.SAFE_READ, "lectura sin efecto"
    if words[0] == "TABLE":
        return SqlClassification.SAFE_READ, "lectura de tabla completa"
    if words[0] == "EXPLAIN":
        if words[1:2] and words[1] in {"SELECT", "VALUES"}:
            return SqlClassification.SAFE_READ, "EXPLAIN de una lectura"
        return SqlClassification.UNKNOWN, "EXPLAIN de una sentencia no reconocida"

    # 5. DDL aditivo.
    if words[0] == "CREATE":
        second = words[1] if len(words) > 1 else ""
        if second == "TABLE":
            if " as select" in padded or " as  select" in padded:
                return SqlClassification.UNKNOWN, "CREATE TABLE ... AS SELECT no está admitido"
            return SqlClassification.SAFE_DDL, "creación de tabla"
        if second in {"INDEX", "UNIQUE"}:
            return SqlClassification.SAFE_DDL, "creación de índice"
        if second == "TYPE":
            # Única variante admitida: CREATE TYPE <id> AS ENUM ('v', ...). Todo lo demás
            # (ALTER TYPE, DROP TYPE, composite, RANGE, DOMAIN, EXTENSION, FUNCTION…) queda
            # denegado por no estar en la lista admitida.
            return _classify_enum_type(normalized)
        return SqlClassification.UNKNOWN, f"CREATE {second or '?'} no está en la lista admitida"

    # 6. ALTER TABLE aditivo y compatible.
    if words[0] == "ALTER":
        if words[1:2] != ("TABLE",):
            return SqlClassification.UNKNOWN, "sólo se admite ALTER TABLE aditivo"
        if " add constraint" in padded or " add  constraint" in padded:
            # Única forma admitida de ADD CONSTRAINT: una clave ajena completa y bien formada.
            # CHECK, UNIQUE, PRIMARY KEY y las opciones no soportadas quedan denegadas.
            return _classify_add_foreign_key(normalized)
        if " add column" not in padded and " add  column" not in padded:
            return (
                SqlClassification.UNKNOWN,
                "sólo se admite ADD COLUMN o ADD CONSTRAINT FOREIGN KEY",
            )
        for forbidden in (" rename", " alter column", " add constraint", " set ", " owner"):
            if forbidden in padded:
                return SqlClassification.UNKNOWN, f"ALTER TABLE con '{forbidden.strip()}'"
        if " not null" in padded and " default" not in padded:
            return (
                SqlClassification.UNKNOWN,
                "ADD COLUMN NOT NULL sin DEFAULT no es compatible con filas existentes",
            )
        return SqlClassification.SAFE_DDL, "ADD COLUMN compatible"

    # 7. Seed idempotente.
    if words[0] == "INSERT":
        if " on conflict" not in padded:
            return SqlClassification.UNKNOWN, "INSERT sin ON CONFLICT no es idempotente"
        if " select " in padded:
            return SqlClassification.UNKNOWN, "INSERT ... SELECT no está admitido como seed"
        return SqlClassification.SAFE_SEED, "INSERT idempotente (ON CONFLICT)"

    return SqlClassification.UNKNOWN, f"sentencia no reconocida: {' '.join(words[:2])}"


def classify_statement(statement: str) -> SqlStatement:
    """Clasifica una sentencia suelta. Cualquier duda es ``UNKNOWN``."""
    text = statement.strip()
    try:
        clean = _strip_comments(text).strip()
    except SqlParseError as error:
        return SqlStatement(text, SqlClassification.UNKNOWN, f"no analizable: {error}")
    normalized = " ".join(clean.split())
    if not normalized:
        return SqlStatement(text, SqlClassification.UNKNOWN, "sentencia vacía")
    classification, reason = _classify_normalized(normalized)
    return SqlStatement(text, classification, reason)


def classify_sql(sql: str) -> SqlPlan:
    """Clasifica un script completo: parte las sentencias y las clasifica todas.

    Un script vacío o sin sentencias produce un plan vacío, que **no** es autónomo: sin sentencias
    no hay nada que aplicar y el ejecutor lo rechaza.
    """
    source = script_sha256(sql)
    try:
        clean = _strip_comments(sql)
        statements = _split_statements(clean)
    except SqlParseError as error:
        fallback = SqlStatement(sql.strip(), SqlClassification.UNKNOWN, f"no analizable: {error}")
        return SqlPlan(statements=(fallback,), source_sha256=source, note=str(error))
    classified = tuple(classify_statement(item) for item in statements)
    note = (
        "todas las sentencias son aplicables en desarrollo"
        if classified and all(item.autonomous for item in classified)
        else "hay sentencias que requieren aprobación humana o están denegadas"
    )
    return SqlPlan(statements=classified, source_sha256=source, note=note)


# ---------------------------------------------------------------------------
# Destino y credencial
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DsnParts:
    """Partes de un DSN que sí se pueden afirmar sin exponer la credencial."""

    scheme: str
    host: str
    port: int
    database: str
    user: str

    def as_public_dict(self) -> dict[str, Any]:
        """Vista publicable: nunca incluye contraseña."""
        return {
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "user": self.user,
        }


def parse_dsn(dsn: str) -> DsnParts:
    """Parte un DSN PostgreSQL. No valida destino; sólo forma.

    Raises:
        DatabaseTargetError: si el DSN no tiene forma utilizable.
    """
    raw = dsn.strip()
    if not raw:
        raise DatabaseTargetError("la credencial de base de datos está vacía")
    try:
        parts = urlsplit(raw)
    except ValueError as error:  # pragma: no cover - defensivo
        raise DatabaseTargetError(f"DSN no analizable ({type(error).__name__})") from error
    scheme = parts.scheme.lower()
    if scheme not in POSTGRES_SCHEMES:
        raise DatabaseTargetError(
            f"el DSN no es PostgreSQL: esquema {scheme!r}. Admitidos: postgres, postgresql"
        )
    host = (parts.hostname or "").strip()
    if not host:
        raise DatabaseTargetError("el DSN no declara host")
    database = (parts.path or "").lstrip("/").strip()
    if not database:
        raise DatabaseTargetError("el DSN no declara base de datos")
    try:
        port = parts.port or 5432
    except ValueError as error:
        raise DatabaseTargetError("el DSN declara un puerto inválido") from error
    user = (parts.username or "").strip()
    return DsnParts(scheme=scheme, host=host, port=port, database=database, user=user)


def redact_dsn(text: str) -> str:
    """Borra cualquier DSN PostgreSQL de un texto, conservando el resto del mensaje.

    Se aplica antes de que un texto salga hacia un log, un error, un evento de auditoría o una
    respuesta HTTP. No necesita conocer el valor: reconoce la forma.
    """
    return redact_secret_text(text)


@dataclass(frozen=True, slots=True)
class DatabaseTarget:
    """Autorización ligada a un proyecto, un entorno, un secreto y un destino concretos."""

    project_id: str
    scope: str
    environment: str = DEVELOPMENT_ENVIRONMENT
    expected_host: str = ""
    expected_database: str = ""
    expected_port: int = 5432
    max_statements: int = DEFAULT_MAX_STATEMENTS
    max_rows_affected: int = DEFAULT_MAX_ROWS_AFFECTED
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        """Valida la autorización. Un destino mal declarado no se admite."""
        if not self.project_id.strip():
            raise DatabaseTargetError("el proyecto no puede estar vacío")
        if not self.scope.strip():
            raise DatabaseTargetError("el alcance del secreto no puede estar vacío")
        environment = self.environment.strip().lower()
        if environment not in DEVELOPMENT_ENVIRONMENTS:
            raise DatabaseTargetError(
                f"entorno {self.environment!r} no autorizado: la autonomía de datos es sólo de "
                f"desarrollo ({', '.join(sorted(DEVELOPMENT_ENVIRONMENTS))})"
            )
        if not self.expected_host.strip() or not self.expected_database.strip():
            raise DatabaseTargetError("el destino esperado (host y base) tiene que estar declarado")
        for mark in PRODUCTION_MARKERS:
            if mark in self.expected_host.lower() or mark in self.expected_database.lower():
                raise DatabaseTargetError(
                    f"el destino declarado parece de producción ({mark!r}): no se autoriza"
                )
        if self.expected_port <= 0 or self.expected_port > 65535:
            raise DatabaseTargetError("el puerto esperado no es válido")
        if self.max_statements <= 0:
            raise DatabaseTargetError("max_statements tiene que ser mayor que cero")
        if self.max_rows_affected < 0:
            raise DatabaseTargetError("max_rows_affected no puede ser negativo")
        if self.connect_timeout_seconds <= 0:
            raise DatabaseTargetError("connect_timeout_seconds tiene que ser mayor que cero")

    def as_public_dict(self) -> dict[str, Any]:
        """Vista publicable: nunca incluye el DSN."""
        return {
            "project_id": self.project_id,
            "scope": self.scope,
            "environment": self.environment,
            "expected_host": self.expected_host,
            "expected_database": self.expected_database,
            "expected_port": self.expected_port,
            "max_statements": self.max_statements,
            "max_rows_affected": self.max_rows_affected,
        }


def assert_destination(dsn: str, target: DatabaseTarget) -> DsnParts:
    """Comprueba que la credencial presentada corresponde al destino autorizado.

    Raises:
        DatabaseTargetError: si el host, el puerto, la base o el esquema no coinciden, o si el
            destino huele a producción. Un mismatch es **BLOCK**, nunca una operación silenciosa.
    """
    parts = parse_dsn(dsn)
    mismatches: list[str] = []
    if parts.host.lower() != target.expected_host.strip().lower():
        mismatches.append("host")
    if parts.port != target.expected_port:
        mismatches.append("puerto")
    if parts.database.lower() != target.expected_database.strip().lower():
        mismatches.append("base de datos")
    if mismatches:
        raise DatabaseTargetError(
            "la credencial presentada no corresponde al destino autorizado "
            f"(difiere en {', '.join(mismatches)}); operación BLOQUEADA"
        )
    for mark in PRODUCTION_MARKERS:
        if mark in parts.host.lower() or mark in parts.database.lower():
            raise DatabaseTargetError(
                f"el destino de la credencial parece de producción ({mark!r}): no se autoriza"
            )
    return parts


def resolve_dsn(target: DatabaseTarget, *, secrets: SecretStore) -> tuple[str, str]:
    """Recupera el DSN del almacén y lo ata al destino.

    Returns:
        ``(dsn, huella)``: el DSN para el driver y su huella para la evidencia. La huella no
        permite recuperar el secreto.

    Raises:
        DatabaseTargetError: si el secreto no está configurado o el destino no coincide.
    """
    dsn = secrets.project_secret(target.scope)
    if not dsn:
        raise DatabaseTargetError(
            f"el secreto de proyecto {target.scope!r} no está configurado; no hay credencial"
        )
    assert_destination(dsn, target)
    fingerprint = hashlib.sha256(dsn.encode("utf-8")).hexdigest()[:16]
    return dsn, fingerprint


# ---------------------------------------------------------------------------
# Driver (protocolo) y driver real
# ---------------------------------------------------------------------------
class DatabaseCursor(Protocol):
    """Cursor mínimo que el ejecutor necesita."""

    @property
    def rowcount(self) -> int:
        """Filas afectadas por la última sentencia (``-1`` si no es medible)."""
        ...

    def execute(self, sql: str) -> Any:
        """Ejecuta una sentencia."""
        ...

    def fetchone(self) -> Sequence[Any] | None:
        """Primera fila del resultado, o ``None``."""
        ...

    def fetchall(self) -> Sequence[Sequence[Any]]:
        """Todas las filas del resultado."""
        ...

    def close(self) -> None:
        """Cierra el cursor."""
        ...


class DatabaseConnection(Protocol):
    """Conexión mínima que el ejecutor necesita."""

    def cursor(self) -> DatabaseCursor:
        """Abre un cursor."""
        ...

    def commit(self) -> None:
        """Confirma la transacción."""
        ...

    def rollback(self) -> None:
        """Revierte la transacción."""
        ...

    def close(self) -> None:
        """Cierra la conexión."""
        ...


class DatabaseDriver(Protocol):
    """Driver inyectable: el real usa psycopg; las pruebas usan un doble controlado."""

    def connect(self, dsn: str, *, timeout: float) -> DatabaseConnection:
        """Abre una conexión."""
        ...


class PsycopgDriver:
    """Driver real: psycopg 3 desde el **controlador**, nunca desde un sandbox."""

    def connect(self, dsn: str, *, timeout: float) -> DatabaseConnection:
        """Abre una conexión psycopg con ``autocommit`` desactivado.

        Raises:
            DatabaseUnavailableError: si psycopg no está instalado o la conexión falla.
        """
        try:
            import psycopg
        except ImportError as error:  # pragma: no cover - depende del entorno
            raise DatabaseUnavailableError(
                "psycopg no está instalado en el controlador: no hay driver de base de datos"
            ) from error
        try:
            connection = psycopg.connect(dsn, connect_timeout=int(timeout), autocommit=False)
        except Exception as error:
            raise DatabaseUnavailableError(
                f"no se pudo conectar a la base de datos: {redact_dsn(str(error))}"
            ) from error
        return cast(DatabaseConnection, connection)


# ---------------------------------------------------------------------------
# Resultados
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ConnectionStatus:
    """Resultado de la comprobación de conectividad, sin credencial."""

    ok: bool
    host: str
    database: str
    environment: str
    project_id: str
    latency_ms: int
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable."""
        return {
            "ok": self.ok,
            "host": self.host,
            "database": self.database,
            "environment": self.environment,
            "project_id": self.project_id,
            "latency_ms": self.latency_ms,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class SchemaSnapshot:
    """Introspección de esquema: tablas con sus columnas y la huella del conjunto."""

    tables: Mapping[str, tuple[str, ...]]
    fingerprint: str
    detail: str = ""

    @property
    def table_count(self) -> int:
        """Número de tablas observadas."""
        return len(self.tables)

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable (sin datos de negocio)."""
        return {
            "tables": {name: list(columns) for name, columns in sorted(self.tables.items())},
            "table_count": self.table_count,
            "fingerprint": self.fingerprint,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Resultado de aplicar un plan: qué se ejecutó, cuánto y con qué huella."""

    kind: str
    statements: int
    rows_affected: int
    classifications: Mapping[str, int]
    source_sha256: str
    schema_before: str
    schema_after: str
    duration_ms: int
    committed: bool = True
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable y sin secretos."""
        return {
            "kind": self.kind,
            "statements": self.statements,
            "rows_affected": self.rows_affected,
            "classifications": dict(self.classifications),
            "source_sha256": self.source_sha256,
            "schema_before": self.schema_before,
            "schema_after": self.schema_after,
            "duration_ms": self.duration_ms,
            "committed": self.committed,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Resultado de una consulta de verificación (sólo lectura)."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    duration_ms: int

    @property
    def row_count(self) -> int:
        """Número de filas devueltas."""
        return len(self.rows)

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable."""
        return {
            "columns": list(self.columns),
            "row_count": self.row_count,
            "rows": [list(row) for row in self.rows],
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# Ejecutor
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class DatabaseExecutor:
    """Controlador de operaciones de base de datos con autoridad.

    El ejecutor no decide por su cuenta: consulta el Policy Engine existente para cada acción y
    exige una aprobación humana válida cuando la política la pide. No hay ninguna vía para que el
    propio ejecutor (ni un modelo) apruebe su gate.
    """

    target: DatabaseTarget
    secrets: SecretStore
    policy: PolicyEngine
    audit: AuditLogger | None = None
    driver: DatabaseDriver | None = None
    human_gate: Any | None = None
    _dsn_fingerprint: str = field(default="", init=False, repr=False)

    # ------------------------------------------------------------------ interno
    def _driver(self) -> DatabaseDriver:
        """Driver en uso: el inyectado o el real de psycopg."""
        return self.driver if self.driver is not None else PsycopgDriver()

    def _connect(self) -> tuple[DatabaseConnection, DsnParts]:
        """Resuelve la credencial, ata el destino y conecta.

        Cualquier fallo del driver se vuelve a emitir **redactado**: un mensaje de error del driver
        puede incluir la cadena de conexión completa y ésta no sale de aquí.
        """
        dsn, fingerprint = resolve_dsn(self.target, secrets=self.secrets)
        self._dsn_fingerprint = fingerprint
        parts = parse_dsn(dsn)
        try:
            connection = self._driver().connect(dsn, timeout=self.target.connect_timeout_seconds)
        except Exception as error:  # el fallo del driver se normaliza y se redacta
            raise DatabaseUnavailableError(redact_dsn(str(error))) from error
        return connection, parts

    def _decide(self, action: str, *, description: str, reversible: bool = True,
                risk: RiskLevel = RiskLevel.LOW) -> Any:
        """Consulta la política para una acción y devuelve la decisión."""
        return self.policy.evaluate(
            ActionRequest(
                action=action,
                description=description,
                reversible=reversible,
                risk_level=risk,
                production_impact=False,
            )
        )

    def _authorize(
        self,
        action: str,
        *,
        description: str,
        reversible: bool = True,
        approval_id: Any | None = None,
    ) -> Any:
        """Autoriza o deniega una acción con el Policy Engine y el Human Gate.

        Raises:
            DatabaseDeniedError: la política denegó (acción no catalogada, presupuesto, etc.).
            DatabaseApprovalRequiredError: la política exige aprobación humana y no hay una válida.
        """
        decision = self._decide(action, description=description, reversible=reversible)
        outcome = decision.outcome.value
        if outcome == "REJECT":
            raise DatabaseDeniedError(
                f"política DENEGÓ {action}: {decision.reason}. Operación no ejecutada."
            )
        if decision.requires_human:
            if approval_id is None or self.human_gate is None:
                raise DatabaseApprovalRequiredError(
                    f"{action} exige aprobación humana y no hay una aprobación válida: "
                    f"{decision.reason}"
                )
            self.human_gate.assert_executable(approval_id)
        return decision

    def _audit_context(self) -> dict[str, Any]:
        """Metadatos comunes de auditoría: identidad del proyecto y huella, **sin** destino.

        Se registran el proyecto, el entorno y la huella del secreto; nunca el DSN, ni su usuario,
        ni su contraseña, ni siquiera el host: el evento no tiene por qué publicar infraestructura.
        """
        return {
            "project_id": self.target.project_id,
            "environment": self.target.environment,
            "dsn_fingerprint": self._dsn_fingerprint,
        }

    # ------------------------------------------------------------------ lectura
    def connect_check(self) -> ConnectionStatus:
        """Comprueba conectividad real con la credencial del almacén.

        Raises:
            DatabaseTargetError: destino no autorizado o secreto ausente.
            DatabaseDeniedError, DatabaseApprovalRequiredError: la política no lo permite.
            DatabaseUnavailableError: no se pudo conectar.
        """
        self._authorize("db_connect_check", description="Comprobar conectividad de base de datos")
        started = time.perf_counter()
        connection, parts = self._connect()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            finally:
                cursor.close()
        except Exception as error:
            connection.close()
            raise DatabaseUnavailableError(
                f"la comprobación de conectividad falló: {redact_dsn(str(error))}"
            ) from error
        finally:
            connection.close()
        latency = int((time.perf_counter() - started) * 1000)
        status = ConnectionStatus(
            ok=True,
            host=parts.host,
            database=parts.database,
            environment=self.target.environment,
            project_id=self.target.project_id,
            latency_ms=latency,
            detail="conexión establecida con el destino autorizado",
        )
        if self.audit is not None:
            self.audit.log_db_connect_checked(
                resource_id=self.target.project_id,
                metadata={**self._audit_context(), "latency_ms": latency, "ok": True},
            )
        return status

    def introspect(self) -> SchemaSnapshot:
        """Lee el esquema real (tablas y columnas) y calcula su huella."""
        self._authorize(
            "db_introspect", description="Introspección del esquema de la base de datos"
        )
        connection, _ = self._connect()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' ORDER BY table_name, ordinal_position"
                )
                rows = cursor.fetchall()
            finally:
                cursor.close()
        except Exception as error:
            connection.close()
            raise DatabaseUnavailableError(
                f"la introspección falló: {redact_dsn(str(error))}"
            ) from error
        finally:
            connection.close()
        tables: dict[str, list[str]] = {}
        for row in rows:
            name = str(row[0])
            tables.setdefault(name, []).append(str(row[1]))
        frozen = {name: tuple(columns) for name, columns in tables.items()}
        fingerprint = schema_fingerprint(frozen)
        snapshot = SchemaSnapshot(
            tables=frozen, fingerprint=fingerprint, detail="esquema leído por el controlador"
        )
        if self.audit is not None:
            self.audit.log_db_schema_introspected(
                resource_id=self.target.project_id,
                metadata={
                    **self._audit_context(),
                    "table_count": snapshot.table_count,
                    "schema_fingerprint": fingerprint,
                },
            )
        return snapshot

    def verify(self, query: str) -> QueryResult:
        """Ejecuta una consulta de verificación. Debe ser ``SAFE_READ``."""
        statement = classify_statement(query)
        self._record_classification(statement)
        if statement.classification is not SqlClassification.SAFE_READ:
            self._reject(statement, action="db_safe_read")
            raise DatabaseDeniedError(
                f"la verificación no es una lectura segura ({statement.classification.value}): "
                f"{statement.reason}"
            )
        self._authorize("db_safe_read", description="Consulta de verificación de sólo lectura")
        connection, _ = self._connect()
        started = time.perf_counter()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(statement.text)
                rows = tuple(tuple(row) for row in cursor.fetchall())
                columns = tuple(
                    str(item[0]) for item in (getattr(cursor, "description", None) or ())
                )
            finally:
                cursor.close()
        except Exception as error:
            connection.close()
            raise DatabaseExecutionError(
                f"la consulta de verificación falló: {redact_dsn(str(error))}"
            ) from error
        finally:
            connection.close()
        duration = int((time.perf_counter() - started) * 1000)
        result = QueryResult(columns=columns, rows=rows, duration_ms=duration)
        if self.audit is not None:
            self.audit.log_db_query_verified(
                resource_id=self.target.project_id,
                metadata={
                    **self._audit_context(),
                    "row_count": result.row_count,
                    "statement_sha256": statement.sha256,
                    "duration_ms": duration,
                },
            )
        return result

    # ------------------------------------------------------------------ escritura
    def apply_migration(
        self, sql: str, *, approval_id: Any | None = None, verify_query: str = ""
    ) -> ExecutionResult:
        """Clasifica y aplica una migración en **una** transacción.

        Raises:
            DatabaseDeniedError: hay sentencias denegadas o desconocidas.
            DatabaseApprovalRequiredError: hay sentencias que exigen aprobación y no la hay.
            DatabaseBudgetError: se excedió el presupuesto de sentencias o de filas.
            DatabaseExecutionError: el driver falló; la transacción se revirtió.
        """
        return self._apply(
            sql,
            kind="migration",
            audit_applied=True,
            approval_id=approval_id,
            verify_query=verify_query,
        )

    def run_seed(self, sql: str, *, approval_id: Any | None = None) -> ExecutionResult:
        """Aplica un seed idempotente: sólo sentencias ``SAFE_SEED``."""
        return self._apply(sql, kind="seed", audit_applied=False, approval_id=approval_id)

    def plan(self, sql: str) -> SqlPlan:
        """Clasifica un script sin ejecutarlo, dejando constancia en auditoría."""
        plan = classify_sql(sql)
        for statement in plan.statements:
            self._record_classification(statement)
        return plan

    def _apply(
        self,
        sql: str,
        *,
        kind: str,
        audit_applied: bool,
        approval_id: Any | None,
        verify_query: str = "",
    ) -> ExecutionResult:
        """Aplica un plan con presupuesto, transacción y auditoría."""
        plan = self.plan(sql)
        if plan.total == 0:
            raise DatabaseDeniedError("el script no contiene ninguna sentencia aplicable")
        if plan.total > self.target.max_statements:
            raise DatabaseBudgetError(
                f"el script trae {plan.total} sentencias y el presupuesto es "
                f"{self.target.max_statements}: operación BLOQUEADA"
            )
        if kind == "seed" and any(
            item.classification is not SqlClassification.SAFE_SEED for item in plan.statements
        ):
            raise DatabaseDeniedError("un seed sólo admite sentencias INSERT idempotentes")

        # Autorización por clasificación: las seguras por su acción; las demás exigen aprobación.
        for statement in plan.statements:
            if statement.classification is SqlClassification.UNKNOWN:
                self._reject(statement, action="db_unknown_statement")
                raise DatabaseDeniedError(
                    "UNKNOWN = DENY: hay una sentencia que el clasificador no reconoce y no se "
                    f"ejecuta nada. Motivo: {statement.reason}"
                )
            action = CLASSIFICATION_ACTIONS[statement.classification]
            self._authorize(
                action,
                description=f"Aplicar sentencia {statement.classification.value} en desarrollo",
                reversible=statement.autonomous,
                approval_id=approval_id,
            )

        connection, _ = self._connect()
        started = time.perf_counter()
        rows_total = 0
        schema_before = ""
        try:
            schema_before = self._schema_fingerprint(connection)
            cursor = connection.cursor()
            try:
                for statement in plan.statements:
                    cursor.execute(statement.text)
                    rows = self._rowcount(cursor)
                    rows_total += rows
                    if (
                        self.target.max_rows_affected
                        and rows_total > self.target.max_rows_affected
                    ):
                        raise DatabaseBudgetError(
                            "la operación supera el presupuesto de "
                            f"{self.target.max_rows_affected} filas afectadas "
                            f"({rows_total}): ROLLBACK"
                        )
            finally:
                cursor.close()
            if verify_query:
                verify_cursor = connection.cursor()
                try:
                    verify_cursor.execute(verify_query)
                    verify_cursor.fetchall()
                finally:
                    verify_cursor.close()
            schema_after = self._schema_fingerprint(connection)
            connection.commit()
        except DatabaseBudgetError:
            connection.rollback()
            self._reject_plan(plan, reason="presupuesto excedido")
            raise
        except DatabaseDeniedError:
            connection.rollback()
            self._reject_plan(plan, reason="denegado por política")
            raise
        except Exception as error:
            connection.rollback()
            self._reject_plan(plan, reason=f"fallo del driver: {type(error).__name__}")
            raise DatabaseExecutionError(
                f"la operación falló y se revirtió: {redact_dsn(str(error))}"
            ) from error
        finally:
            connection.close()
        duration = int((time.perf_counter() - started) * 1000)
        result = ExecutionResult(
            kind=kind,
            statements=plan.total,
            rows_affected=rows_total,
            classifications=plan.classifications,
            source_sha256=plan.source_sha256,
            schema_before=schema_before,
            schema_after=schema_after,
            duration_ms=duration,
            committed=True,
            detail="aplicado en una transacción sobre el destino autorizado",
        )
        if self.audit is not None:
            metadata = {
                **self._audit_context(),
                "statements": result.statements,
                "rows_affected": result.rows_affected,
                "classifications": dict(result.classifications),
                "source_sha256": result.source_sha256,
                "schema_before": result.schema_before,
                "schema_after": result.schema_after,
                "duration_ms": duration,
            }
            if audit_applied:
                self.audit.log_db_migration_applied(
                    resource_id=self.target.project_id, metadata=metadata
                )
            else:
                self.audit.log_db_seed_applied(
                    resource_id=self.target.project_id, metadata=metadata
                )
        return result

    def _schema_fingerprint(self, connection: DatabaseConnection) -> str:
        """Huella del esquema observado en la conexión actual (o vacío si no se puede leer)."""
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' ORDER BY table_name, ordinal_position"
                )
                rows = cursor.fetchall()
            finally:
                cursor.close()
        except Exception:
            return ""
        tables: dict[str, list[str]] = {}
        for row in rows:
            tables.setdefault(str(row[0]), []).append(str(row[1]))
        return schema_fingerprint({name: tuple(cols) for name, cols in tables.items()})

    @staticmethod
    def _rowcount(cursor: DatabaseCursor) -> int:
        """Filas afectadas por la última sentencia, si el driver las declara."""
        try:
            value = int(cursor.rowcount)
        except (TypeError, ValueError):  # pragma: no cover - drivers que no lo declaran
            return 0
        return value if value > 0 else 0

    # ------------------------------------------------------------------ auditoría
    def _record_classification(self, statement: SqlStatement) -> None:
        """Registra la clasificación de una sentencia (sin su texto)."""
        if self.audit is None:
            return
        self.audit.log_db_statement_classified(
            resource_id=self.target.project_id,
            metadata={
                **self._audit_context(),
                "classification": statement.classification.value,
                "statement_sha256": statement.sha256,
                "reason": statement.reason,
                "autonomous": statement.autonomous,
            },
        )

    def _reject(self, statement: SqlStatement, *, action: str) -> None:
        """Registra el rechazo de una sentencia."""
        if self.audit is None:
            return
        self.audit.log_db_migration_rejected(
            resource_id=self.target.project_id,
            metadata={
                **self._audit_context(),
                "action": action,
                "classification": statement.classification.value,
                "statement_sha256": statement.sha256,
                "reason": statement.reason,
            },
        )

    def _reject_plan(self, plan: SqlPlan, *, reason: str) -> None:
        """Registra el rechazo de un plan completo."""
        if self.audit is None:
            return
        self.audit.log_db_migration_rejected(
            resource_id=self.target.project_id,
            metadata={
                **self._audit_context(),
                "statements": plan.total,
                "classifications": dict(plan.classifications),
                "source_sha256": plan.source_sha256,
                "reason": reason,
            },
        )

    # ------------------------------------------------------------------ frontera
    def provider_context(self, plan: SqlPlan) -> str:
        """Resumen seguro para el BUILDER: qué se clasificó, **sin** credencial ni destino.

        Es el único texto de esta capa que puede llegar a un proveedor: lleva identidad de
        proyecto, entorno, clasificaciones, recuentos y huellas. Nunca el DSN, ni el host, ni la
        base, ni el SQL.
        """
        lines = [
            f"proyecto: {self.target.project_id}",
            f"entorno: {self.target.environment}",
            f"sentencias: {plan.total}",
            f"clasificaciones: {plan.classifications}",
            f"script_sha256: {plan.source_sha256}",
            f"autónomo: {plan.autonomous}",
        ]
        text = "\n".join(lines)
        return redact_dsn(text)


def schema_fingerprint(tables: Mapping[str, Sequence[str]]) -> str:
    """Huella estable del esquema observado (tablas y columnas), sin datos de negocio."""
    canonical = "\n".join(
        f"{name}({','.join(columns)})" for name, columns in sorted(tables.items())
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def dsn_query_options(dsn: str) -> dict[str, list[str]]:
    """Opciones de query del DSN (por ejemplo ``sslmode``). Útil para diagnóstico sin secreto."""
    return parse_qs(urlsplit(dsn.strip()).query)


__all__ = [
    "AUTONOMOUS_CLASSIFICATIONS",
    "CLASSIFICATION_ACTIONS",
    "DEFAULT_MAX_ROWS_AFFECTED",
    "DEFAULT_MAX_STATEMENTS",
    "DEVELOPMENT_ENVIRONMENT",
    "DEVELOPMENT_ENVIRONMENTS",
    "ConnectionStatus",
    "DatabaseApprovalRequiredError",
    "DatabaseBudgetError",
    "DatabaseConnection",
    "DatabaseCursor",
    "DatabaseDeniedError",
    "DatabaseDriver",
    "DatabaseError",
    "DatabaseExecutionError",
    "DatabaseExecutor",
    "DatabaseTarget",
    "DatabaseTargetError",
    "DatabaseUnavailableError",
    "DsnParts",
    "ExecutionResult",
    "PsycopgDriver",
    "QueryResult",
    "SchemaSnapshot",
    "SqlClassification",
    "SqlParseError",
    "SqlPlan",
    "SqlStatement",
    "assert_destination",
    "classify_sql",
    "classify_statement",
    "dsn_query_options",
    "parse_dsn",
    "redact_dsn",
    "resolve_dsn",
    "schema_fingerprint",
    "script_sha256",
    "statement_sha256",
]
