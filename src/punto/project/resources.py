"""Recursos observables y conjuntos de recursos autorizados (ENGINE-6.3.R1, PARTE C).

La autonomía de una replanificación **no** se decide leyendo el lenguaje natural de la propuesta:
se decide comparando **conjuntos de recursos**. Este módulo define el vocabulario de esos
conjuntos —tokens canónicos ``dimension:nombre``— y los tres orígenes con los que se construyen:

- ``from_contract``: lo que el proyecto **ya tiene autorizado**, derivado del ``ArchitecturePlan``
  durable y del ``ProjectCapabilityProfile`` (nunca del Planner);
- ``from_requests``: lo que una propuesta **pide** en sus campos estructurados ``uses_*`` (es una
  petición, no una autorización);
- ``from_diff``: lo que la implementación **introdujo de verdad**, leído de los manifiestos y de la
  configuración de infraestructura que el child cambió.

La comparación es de **contención**: ``pedido ⊆ autorizado``. Lo que no se pueda leer no se supone
vacío: se declara ``UNRESOLVED``, que es un valor de primera clase y nunca concede autonomía.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from punto.schemas.replan import ProjectContract

#: Máximo de tokens que un conjunto de recursos enumera (cota de serialización y de informe).
MAX_RESOURCE_TOKENS: Final[int] = 200

#: Caracteres máximos de un nombre de recurso.
MAX_RESOURCE_NAME_CHARS: Final[int] = 120


class ResourceDimension(StrEnum):
    """Dimensiones de recurso que el motor distingue."""

    CAPABILITY = "capability"
    DATASTORE = "datastore"
    INTEGRATION = "integration"
    SERVICE = "service"
    COMPONENT = "component"
    INTERFACE = "interface"
    SECURITY = "security"
    DEPLOYMENT = "deployment"
    TECHNOLOGY = "technology"
    PACKAGE = "package"


#: Dimensiones que un manifiesto de dependencias puede introducir.
DEPENDENCY_DIMENSIONS: Final[frozenset[ResourceDimension]] = frozenset(
    {ResourceDimension.PACKAGE, ResourceDimension.TECHNOLOGY}
)


def _bounded(value: str) -> str:
    """Nombre canónico acotado: minúsculas, sin espacios decorativos y sin crecer sin límite."""
    return " ".join(value.split()).casefold()[:MAX_RESOURCE_NAME_CHARS]


def resource_token(dimension: ResourceDimension, name: str) -> str:
    """Token canónico ``dimension:nombre`` de un recurso."""
    return f"{dimension.value}:{_bounded(name)}"


def _canonical(token: str) -> str:
    """Token canónico: dimensión en minúsculas y nombre normalizado.

    Un token sin dimensión reconocible se deja tal cual (solo normalizado), porque quien lo escribió
    a mano no declaró a qué familia pertenece; el envelope del contrato los declara siempre con
    dimensión.
    """
    dimension, separator, name = token.partition(":")
    if not separator:
        return _bounded(token)
    return f"{_bounded(dimension)}:{_bounded(name)}"


def capability_token(capability: str) -> str:
    """Token de una capacidad declarada (``python``, ``node20``, ``pytest``…)."""
    return resource_token(ResourceDimension.CAPABILITY, capability)


@dataclass(frozen=True, slots=True)
class ResourceSet:
    """Conjunto **inmutable** de recursos canónicos, con sus dimensiones.

    Es la unidad con la que se demuestra contención: un ``ResourceSet`` autorizado, otro pedido y
    otro observado. Se compara por inclusión de conjuntos, no por parecido de nombres.
    """

    tokens: tuple[str, ...] = ()

    @staticmethod
    def of(values: Iterable[str]) -> ResourceSet:
        """Conjunto a partir de tokens, canonicalizado, deduplicado y ordenado.

        La canonicalización es la misma que la de :func:`resource_token`: espacios colapsados y
        minúsculas. Sin ella, un token escrito a mano con un espacio de más sería un recurso
        distinto del que el motor deriva —fail-closed, pero frágil y sorprendente—.
        """
        unique = sorted({_canonical(value) for value in values if value.strip()})
        return ResourceSet(tokens=tuple(unique[:MAX_RESOURCE_TOKENS]))

    @classmethod
    def from_contract(cls, contract: ProjectContract) -> ResourceSet:
        """Recursos que el contrato **ya autoriza** (PARTE A → PARTE C).

        Es el único origen que concede algo: se deriva del ``ArchitecturePlan`` durable y del perfil
        de capacidades del plan aceptado, nunca del texto de la propuesta.
        """
        return contract_resources(contract)

    @classmethod
    def from_node_envelope(
        cls, *, resources: Iterable[str] = (), capabilities: Iterable[str] = ()
    ) -> ResourceSet:
        """Envelope que **declara un nodo** del grafo activo.

        Es la referencia con la que se compara la operación que lo sustituye: los nodos nuevos no
        pueden pedir más de lo que el nodo sustituido ya declaraba, más lo que el contrato autoriza
        globalmente.
        """
        return ResourceSet.of((*resources, *(capability_token(item) for item in capabilities)))

    @classmethod
    def from_requests(
        cls,
        *,
        uses_resources: Iterable[str] = (),
        uses_capabilities: Iterable[str] = (),
        target: str = "",
    ) -> ResourceSet:
        """Recursos **pedidos** por una propuesta: una petición, nunca una autorización."""
        return request_resources(
            uses_resources=uses_resources, uses_capabilities=uses_capabilities, target=target
        )

    @classmethod
    def from_diff(
        cls, paths: Sequence[str], read: Callable[[str], str | None]
    ) -> tuple[ResourceSet, tuple[str, ...]]:
        """Recursos **observados** en lo que el child cambió de verdad (PARTE D).

        Returns:
            ``(recursos observados, razones sin resolver)``; lo ilegible no se supone vacío.
        """
        return resources_from_diff(paths, read)

    def union(self, other: ResourceSet) -> ResourceSet:
        """Unión de dos conjuntos."""
        return ResourceSet.of((*self.tokens, *other.tokens))

    def difference(self, other: ResourceSet) -> ResourceSet:
        """Recursos de este conjunto que **no** están en ``other``."""
        return ResourceSet.of(token for token in self.tokens if token not in set(other.tokens))

    def is_subset_of(self, other: ResourceSet) -> bool:
        """``True`` si todos los recursos están contenidos en ``other``."""
        return set(self.tokens) <= set(other.tokens)

    def dimensions(self) -> tuple[str, ...]:
        """Dimensiones presentes, en orden estable."""
        return tuple(sorted({token.split(":", 1)[0] for token in self.tokens if ":" in token}))

    def labels(self, limit: int = 12) -> tuple[str, ...]:
        """Etiquetas legibles y acotadas, para el detalle del veredicto."""
        return self.tokens[:limit]

    @property
    def is_empty(self) -> bool:
        """``True`` si no declara ningún recurso."""
        return not self.tokens

    def __bool__(self) -> bool:
        """Un conjunto vacío es falso, para poder escribir ``if recursos``."""
        return bool(self.tokens)


@dataclass(frozen=True, slots=True)
class ExpansionReport:
    """Resultado de comparar un conjunto pedido u observado con el autorizado.

    ``expanded`` enumera los recursos que **no** están autorizados; ``unresolved`` enumera las
    razones por las que el motor no pudo decidir (un manifiesto que cambió y no tiene parser, un
    fichero ilegible). Las dos cosas bloquean la autonomía, y ninguna se degrada a «conjunto vacío».
    """

    expanded: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        """``True`` si no hay expansión ni incertidumbre."""
        return not self.expanded and not self.unresolved

    @property
    def is_contained(self) -> bool:
        """``True`` si el conjunto estaba contenido y todo se pudo resolver."""
        return self.is_empty

    @property
    def has_unresolved(self) -> bool:
        """``True`` si alguna dimensión quedó sin resolver."""
        return bool(self.unresolved)

    def dimensions(self) -> tuple[str, ...]:
        """Dimensiones expandidas, en orden estable."""
        return tuple(sorted({token.split(":", 1)[0] for token in self.expanded if ":" in token}))

    def detail(self) -> str:
        """Detalle legible y acotado del informe."""
        parts: list[str] = []
        if self.expanded:
            parts.append("recursos no autorizados: " + ", ".join(self.expanded[:12]))
        if self.unresolved:
            parts.append("sin resolver: " + "; ".join(self.unresolved[:6]))
        return " | ".join(parts)


# --------------------------------------------------------------------------- parsers
def _read_text(path: str, read: Callable[[str], str | None]) -> str | None:
    """Contenido de un fichero, o ``None`` si no se puede leer."""
    try:
        return read(path)
    except (OSError, UnicodeDecodeError):  # pragma: no cover - depende del sistema de archivos
        return None


def _normalize_package(name: str) -> str:
    """Nombre de paquete normalizado: sin extras, sin versión y sin prefijos de resolución."""
    clean = name.strip().strip("\"'")
    for separator in ("==", ">=", "<=", "~=", "!=", ">", "<", "@", ";", "[", "("):
        if separator in clean:
            clean = clean.split(separator, 1)[0]
    if "/" in clean:
        clean = clean.rsplit("/", 1)[-1]
    return _bounded(clean.strip())


def _tokens_from_requirements(text: str) -> tuple[str, ...]:
    """Dependencias declaradas en un ``requirements*.txt``."""
    found: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = _normalize_package(line)
        if name:
            found.append(resource_token(ResourceDimension.PACKAGE, name))
    return tuple(found)


def _tokens_from_pyproject(text: str) -> tuple[str, ...]:
    """Dependencias declaradas en ``pyproject.toml`` (PEP 621 y poetry)."""
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise _ManifestUnresolved("pyproject.toml no es TOML válido") from None
    found: list[str] = []
    project = payload.get("project")
    if isinstance(project, dict):
        for item in project.get("dependencies", []) or []:
            if isinstance(item, str):
                name = _normalize_package(item)
                if name:
                    found.append(resource_token(ResourceDimension.PACKAGE, name))
        for group in (project.get("optional-dependencies") or {}).values():
            for item in group or []:
                if isinstance(item, str):
                    name = _normalize_package(item)
                    if name:
                        found.append(resource_token(ResourceDimension.PACKAGE, name))
        requires = project.get("requires-python")
        if isinstance(requires, str) and requires.strip():
            found.append(resource_token(ResourceDimension.TECHNOLOGY, f"python{requires.strip()}"))
    tool = payload.get("tool")
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    if isinstance(poetry, dict):
        for group in ("dependencies", "dev-dependencies"):
            for name in (poetry.get(group) or {}):
                if isinstance(name, str) and name.casefold() != "python":
                    found.append(resource_token(ResourceDimension.PACKAGE, name))
    if not found and not isinstance(project, dict) and poetry is None:
        raise _ManifestUnresolved("pyproject.toml sin tabla de dependencias reconocible")
    return tuple(found)


def _tokens_from_package_json(text: str) -> tuple[str, ...]:
    """Dependencias declaradas en ``package.json``."""
    try:
        payload = json.loads(text)
    except ValueError:
        raise _ManifestUnresolved("package.json no es JSON válido") from None
    if not isinstance(payload, dict):
        raise _ManifestUnresolved("package.json no es un objeto JSON")
    found: list[str] = []
    for group in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        block = payload.get(group)
        if isinstance(block, dict):
            found.extend(
                resource_token(ResourceDimension.PACKAGE, name)
                for name in block
                if isinstance(name, str)
            )
    if isinstance(payload.get("engines"), dict):
        for name, value in payload["engines"].items():
            if isinstance(name, str) and isinstance(value, str):
                found.append(resource_token(ResourceDimension.TECHNOLOGY, f"{name}{value}"))
    return tuple(found)


def _tokens_from_lockfile(text: str) -> tuple[str, ...]:
    """Dependencias de un lockfile textual (``yarn.lock``, ``pnpm-lock.yaml``, ``poetry.lock``)."""
    found: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        match = re.match(r'^"?([@a-z0-9._/-]+)@', line) or re.match(
            r"^\s*([A-Za-z0-9._-]+)\s*=", raw
        )
        if match:
            name = _normalize_package(match.group(1))
            if name:
                found.append(resource_token(ResourceDimension.PACKAGE, name))
    return tuple(found)


def _tokens_from_go_mod(text: str) -> tuple[str, ...]:
    """Módulos requeridos en ``go.mod``."""
    found: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("module", "go ", ")", "//")):
            continue
        parts = line.split()
        if len(parts) >= 2 and "/" in parts[0]:
            found.append(resource_token(ResourceDimension.PACKAGE, parts[0]))
    return tuple(found)


def _tokens_from_cargo(text: str) -> tuple[str, ...]:
    """Dependencias declaradas en ``Cargo.toml``."""
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise _ManifestUnresolved("Cargo.toml no es TOML válido") from None
    found: list[str] = []
    for table in ("dependencies", "dev-dependencies", "build-dependencies"):
        block = payload.get(table)
        if isinstance(block, dict):
            found.extend(
                resource_token(ResourceDimension.PACKAGE, name)
                for name in block
                if isinstance(name, str)
            )
    return tuple(found)


def _tokens_from_dockerfile(text: str) -> tuple[str, ...]:
    """Imágenes base de un ``Dockerfile``."""
    found: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.upper().startswith("FROM "):
            image = line.split()[1]
            found.append(resource_token(ResourceDimension.TECHNOLOGY, f"image:{image}"))
    return tuple(found)


def _tokens_from_compose(text: str) -> tuple[str, ...]:
    """Imágenes y servicios declarados en un fichero de compose."""
    found: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        match = re.match(r"^image:\s*(\S+)", line)
        if match:
            found.append(resource_token(ResourceDimension.TECHNOLOGY, f"image:{match.group(1)}"))
        if re.match(r"^services:\s*$", line):
            found.append(resource_token(ResourceDimension.SERVICE, "compose"))
    if not found:
        raise _ManifestUnresolved("compose sin imágenes ni servicios reconocibles")
    return tuple(found)


def _tokens_from_workflow(text: str) -> tuple[str, ...]:
    """Acciones y servicios declarados en un flujo de CI (``.github/workflows/*.yml``)."""
    found: list[str] = []
    for raw in text.splitlines():
        match = re.match(r"^\s*-?\s*uses:\s*(\S+)", raw)
        if match:
            found.append(resource_token(ResourceDimension.PACKAGE, match.group(1)))
        match = re.match(r"^\s*image:\s*(\S+)", raw)
        if match:
            found.append(resource_token(ResourceDimension.TECHNOLOGY, f"image:{match.group(1)}"))
    return tuple(found)


class _ManifestUnresolved(RuntimeError):
    """El manifiesto cambió y no se pudo interpretar con un parser soportado."""


#: Parsers soportados, por nombre de fichero exacto o por patrón. La lista es **explícita**: un
#: manifiesto que no esté aquí y cambie deja el veredicto en ``UNRESOLVED`` en vez de en «vacío».
_MANIFEST_PARSERS: Final[tuple[tuple[re.Pattern[str], Callable[[str], tuple[str, ...]]], ...]] = (
    (re.compile(r"(^|/)pyproject\.toml$"), _tokens_from_pyproject),
    (re.compile(r"(^|/)requirements[^/]*\.txt$"), _tokens_from_requirements),
    (re.compile(r"(^|/)package\.json$"), _tokens_from_package_json),
    (re.compile(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock)$"),
     _tokens_from_lockfile),
    (re.compile(r"(^|/)go\.mod$"), _tokens_from_go_mod),
    (re.compile(r"(^|/)Cargo\.toml$"), _tokens_from_cargo),
    (re.compile(r"(^|/)Dockerfile[^/]*$"), _tokens_from_dockerfile),
    (re.compile(r"(^|/)(docker-)?compose[^/]*\.ya?ml$"), _tokens_from_compose),
    (re.compile(r"(^|/)\.github/workflows/[^/]+\.ya?ml$"), _tokens_from_workflow),
)

#: Rutas que **declaran** recursos de infraestructura y que el motor todavía no sabe interpretar.
#:
#: Si una de ellas cambia, el veredicto es ``UNRESOLVED``: el motor no supone que no introdujo nada.
_INFRASTRUCTURE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(^|/)(Dockerfile[^/]*|docker-)?compose[^/]*\.ya?ml$"),
    re.compile(r"(^|/)\.github/workflows/"),
    re.compile(r"(^|/)(terraform|pulumi|serverless|vercel|netlify|fly|render)\b"),
    re.compile(r"(^|/)\.env"),
    re.compile(r"(^|/)(requirements[^/]*\.txt|Pipfile|Gemfile|pom\.xml|build\.gradle[^/]*)$"),
    re.compile(r"(^|/)(tsconfig\.json|\.nvmrc|runtime\.txt)$"),
    # Superficies que también pueden cambiar arquitectura y que el motor no interpreta todavía
    # (ENGINE-6.3.R2, AUD-6.3R1-03). La política es la misma: lo que no se sabe leer **no** se
    # supone inocuo. No es una lista de tecnologías: es una lista de **superficies**.
    re.compile(r"\.sql$"),
    re.compile(r"(^|/)(migrations?|alembic|prisma|drizzle|flyway|liquibase)/"),
    re.compile(r"(^|/)(config|conf|settings|deploy|deployment|infra|infrastructure|k8s|kubernetes)/"),
    re.compile(
        r"(^|/)(settings|config|configuration|constants|local_settings)[^/]*\.py$"
    ),
    re.compile(r"\.(?:ini|cfg|conf|properties|tf|tfvars|service|env|toml|ya?ml)$"),
    re.compile(r"(^|/)(Procfile|Caddyfile|nginx\.conf|supervisord\.conf|systemd/)$"),
    re.compile(r"(^|/)(secrets?|credentials?|vault|keyring)"),
)

#: Esquemas de URI que son transporte genérico, no una tecnología de arquitectura.
_GENERIC_URI_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

#: Forma de una URI/DSN con esquema: ``mongodb://``, ``amqp://``, ``redis://``, ``postgres://``…
_URI_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b([a-z][a-z0-9+.\-]{1,31})://")

#: Módulos cuya presencia, por sí sola, **no** puede cambiar la arquitectura autorizada: cálculo,
#: texto, estructuras, serialización y utilidades de proceso.
#:
#: La lista es **cerrada e inmutable**, y esa es la propiedad que importa: lo que no está aquí
#: —cualquier tecnología futura, y también un módulo de la biblioteca estándar que introduce
#: almacenamiento, red, concurrencia o criptografía— queda ``UNRESOLVED``. No es una lista de
#: tecnologías conocidas: es la allowlist de lo que el motor puede **demostrar** inocuo, y su
#: ausencia nunca concede nada (ENGINE-6.3.R3, AUD-R2-02).
_INERT_MODULES: Final[frozenset[str]] = frozenset(
    {
        "__future__",
        "abc",
        "argparse",
        "array",
        "ast",
        "base64",
        "bisect",
        "calendar",
        "collections",
        "contextlib",
        "copy",
        "csv",
        "dataclasses",
        "datetime",
        "decimal",
        "difflib",
        "enum",
        "fnmatch",
        "fractions",
        "functools",
        "glob",
        "hashlib",
        "heapq",
        "html",
        "inspect",
        "io",
        "itertools",
        "json",
        "logging",
        "math",
        "numbers",
        "operator",
        "os",
        "pathlib",
        "pprint",
        "pydoc",
        "random",
        "re",
        "shlex",
        "shutil",
        "statistics",
        "string",
        "struct",
        "sys",
        "tempfile",
        "textwrap",
        "time",
        "traceback",
        "types",
        "typing",
        "unicodedata",
        "unittest",
        "uuid",
        "warnings",
        "zipfile",
    }
)

#: Formas con las que un fichero importa un módulo. Se busca la **raíz** del módulo, que es lo que
#: identifica la tecnología (``pymongo`` en ``pymongo.MongoClient``).
_IMPORT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^\s*import\s+([A-Za-z_][\w.]*)"),
    re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import"),
    re.compile(r"__import__\(\s*['\"]([A-Za-z_][\w.]*)['\"]"),
    re.compile(r"import_module\(\s*['\"]([A-Za-z_][\w.]*)['\"]"),
    re.compile(r"(?:require|load)\(\s*['\"]([A-Za-z_@][\w./@-]*)['\"]"),
)


def module_roots(line: str) -> tuple[str, ...]:
    """Raíces de los módulos que una línea importa, sin repetir."""
    found: list[str] = []
    for pattern in _IMPORT_PATTERNS:
        for match in pattern.finditer(line):
            root = match.group(1).split(".")[0].split("/")[0].lstrip("@")
            if root and root not in found:
                found.append(root)
    return tuple(found)


def unproven_effect(
    *,
    added_lines: Iterable[str],
    authorized: ResourceSet,
    local_roots: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Razones sin resolver por un efecto arquitectónico en el **contenido real** del diff.

    Conocer el camino no demuestra nada sobre el efecto (ENGINE-6.3.R3, AUD-R2-02): un fichero
    autorizado puede cambiar el almacén, el proveedor o el entorno de ejecución del proyecto. Aquí
    se mira lo que la implementación **escribió de verdad** —las líneas añadidas del diff real— y
    se declara sin resolver todo lo que el motor no puede demostrar contenido:

    - un módulo importado que no está en el envelope autorizado, no es un módulo local del
      proyecto y no está en la allowlist cerrada de módulos inertes;
    - una URI/DSN cuyo esquema el envelope no contiene (salvo transporte genérico).

    No hay lista de tecnologías: hay una allowlist de lo demostrablemente inocuo y un defecto
    **fail-closed**. Que el motor no conozca una tecnología es exactamente el caso que debe caer.

    Args:
        added_lines: líneas añadidas por el diff real (todas las rutas cambiadas).
        authorized: envelope autorizado del proyecto (recursos).
        local_roots: raíces de módulos propios del proyecto (ficheros/paquetes del workspace).

    Returns:
        Razones legibles, una por hallazgo.
    """
    authorized_tokens = set(authorized.tokens)
    known_packages = {
        token.split(":", 1)[1].split(":")[0]
        for token in authorized_tokens
        if token.startswith("package:") or token.startswith("technology:")
    }
    inert = _INERT_MODULES | local_roots
    reasons: list[str] = []
    for raw in added_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for root in module_roots(line):
            name = root.casefold().replace("-", "_")
            if name in inert or root in known_packages or name in known_packages:
                continue
            reasons.append(
                f"el diff importa {root!r}: el motor no puede demostrar que esa dependencia quede "
                "dentro de la arquitectura autorizada"
            )
        for match in _URI_PATTERN.finditer(line):
            scheme = match.group(1).casefold()
            if scheme in _GENERIC_URI_SCHEMES:
                continue
            if scheme in authorized_tokens or any(
                token.rsplit(":", 1)[-1] == scheme for token in authorized_tokens
            ):
                continue
            reasons.append(
                f"el diff introduce una URI con esquema {scheme!r} que el envelope autorizado no "
                "contiene; el motor no puede demostrar que el cambio quede dentro del diseño"
            )
    return tuple(dict.fromkeys(reasons))


@dataclass(frozen=True, slots=True)
class AuthorityEnvelope:
    """Qué está autorizado a cambiar un nodo: superficie, recursos y dimensiones (ENGINE-6.3.R3).

    Responde a la pregunta «¿qué se autorizó exactamente?» con hechos del motor —los ficheros que el
    nodo puede escribir, el envelope de recursos del contrato y sus dimensiones—, y viaja con la
    decisión y con el vínculo humano. No es una lista de lo prohibido: es la superficie dentro de la
    cual el efecto real tiene que poder demostrarse.
    """

    files: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    source: str = ""
    #: ``True`` si el motor pudo enunciar la autoridad: superficie concreta, o una operación que
    #: **no introduce trabajo nuevo** (un reordenamiento no autoriza a escribir nada). Un envelope
    #: sin superficie y con trabajo nuevo no es autoridad: es ausencia de información.
    explicit: bool = False

    def detail(self) -> str:
        """Descripción legible y acotada del envelope."""
        files = ", ".join(self.files[:6]) or "sin superficie declarada"
        dimensions = ", ".join(self.dimensions[:6]) or "sin dimensiones autorizadas"
        return f"superficie [{files}] · dimensiones [{dimensions}] · origen {self.source or 'n/d'}"


def parser_for(path: str) -> Callable[[str], tuple[str, ...]] | None:
    """Parser soportado para una ruta, o ``None`` si no hay ninguno."""
    normalized = path.replace("\\", "/")
    for pattern, parser in _MANIFEST_PARSERS:
        if pattern.search(normalized):
            return parser
    return None


def is_resource_relevant(path: str) -> bool:
    """``True`` si la ruta puede declarar recursos de arquitectura."""
    normalized = path.replace("\\", "/")
    if parser_for(normalized) is not None:
        return True
    return any(pattern.search(normalized) for pattern in _INFRASTRUCTURE_PATTERNS)


def unannounced_surfaces(
    paths: Sequence[str],
    read: Callable[[str], str | None],
    authorized: ResourceSet,
) -> tuple[str, ...]:
    """Razones sin resolver por arquitectura detectada en ficheros que no son manifiestos.

    La cobertura post-ejecución no puede depender de conocer el nombre de cada tecnología
    (ENGINE-6.3.R2, AUD-6.3R1-03): lo que se busca es una **forma**, no un producto. Un fichero de
    código o de configuración que introduce una URI/DSN con un esquema que el envelope autorizado no
    contiene está tocando una superficie de arquitectura que el motor no puede demostrar
    contenida, y por eso queda ``UNRESOLVED`` —nunca «no introdujo nada»—. ``http``/``https`` se
    tratan como transporte genérico: su significado arquitectónico está en el destino, que se
    declara en las superficies de configuración (que ya se resuelven o fallan cerrado arriba).

    Args:
        paths: rutas cambiadas.
        read: lector del contenido; ``None`` si no existe.
        authorized: envelope autorizado del proyecto.

    Returns:
        Razones legibles, una por hallazgo.
    """
    authorized_tokens = set(authorized.tokens)
    reasons: list[str] = []
    for path in dict.fromkeys(paths):
        normalized = path.replace("\\", "/")
        if is_resource_relevant(normalized):
            continue
        content = _read_text(path, read)
        if content is None:
            continue
        for match in _URI_PATTERN.finditer(content):
            scheme = match.group(1).casefold()
            if scheme in _GENERIC_URI_SCHEMES:
                continue
            if scheme in authorized_tokens or any(
                token.rsplit(":", 1)[-1] == scheme for token in authorized_tokens
            ):
                continue
            reasons.append(
                f"{path}: introduce una URI con esquema {scheme!r} que el envelope autorizado no "
                "contiene; el motor no puede demostrar que el cambio quede dentro del diseño"
            )
    return tuple(reasons)


def resources_from_diff(
    paths: Sequence[str], read: Callable[[str], str | None]
) -> tuple[ResourceSet, tuple[str, ...]]:
    """Recursos observados en los ficheros que el child cambió, y lo que no se pudo resolver.

    Solo se inspeccionan las rutas que pueden declarar recursos (manifiestos y configuración de
    infraestructura): un cambio en ``app.py`` no introduce ninguna tecnología por sí mismo. Para una
    ruta relevante **sin parser soportado** se devuelve una razón de ``unresolved`` en vez de
    suponer que no introdujo nada.

    Args:
        paths: Rutas relativas cambiadas por el child, en el orden declarado.
        read: Lector del contenido de una ruta relativa; ``None`` si no existe.

    Returns:
        ``(recursos observados, razones sin resolver)``.
    """
    tokens: list[str] = []
    unresolved: list[str] = []
    for path in dict.fromkeys(paths):
        if not is_resource_relevant(path):
            continue
        parser = parser_for(path)
        if parser is None:
            unresolved.append(f"{path}: sin parser soportado para un fichero de infraestructura")
            continue
        content = _read_text(path, read)
        if content is None:
            unresolved.append(f"{path}: el fichero cambió y no se pudo leer su contenido")
            continue
        try:
            tokens.extend(parser(content))
        except _ManifestUnresolved as exc:
            unresolved.append(f"{path}: {exc}")
    return ResourceSet.of(tokens), tuple(unresolved)


def contract_resources(contract: ProjectContract) -> ResourceSet:
    """Recursos que el proyecto **ya tiene autorizados** (``ResourceSet.from_contract``).

    Es el envelope durable que el contrato guarda: se deriva una sola vez, al fijar el contrato, de
    los hechos de arquitectura del plan aceptado y del perfil de capacidades —**nunca** de lo que
    proponga el Planner— y viaja con su huella. Un contrato sin arquitectura resuelta produce un
    conjunto vacío: no se fabrica conocimiento, y con el conjunto vacío toda petición de recurso es
    una expansión.
    """
    return ResourceSet.of(contract.authorized_resources)


#: Dimensión de recurso a la que corresponde cada familia del perfil de capacidades.
_CAPABILITY_DIMENSIONS: Final[dict[str, tuple[ResourceDimension, ...]]] = {
    "LANGUAGE": (ResourceDimension.CAPABILITY, ResourceDimension.PACKAGE),
    "FRAMEWORK": (ResourceDimension.PACKAGE, ResourceDimension.TECHNOLOGY),
    "DATABASE": (ResourceDimension.DATASTORE,),
    "PACKAGE_MANAGER": (ResourceDimension.CAPABILITY,),
    "VALIDATOR": (ResourceDimension.CAPABILITY, ResourceDimension.PACKAGE),
    "DEPLOYMENT_TARGET": (ResourceDimension.DEPLOYMENT,),
    "EXECUTION_PROFILE": (ResourceDimension.CAPABILITY,),
}


def project_resource_envelope(
    architecture: object | None,
    *,
    capability_entries: Iterable[tuple[str, str]] = (),
) -> ResourceSet:
    """Envelope de recursos del proyecto, derivado de la arquitectura **aceptada**.

    Es la única fuente de autorización de recursos del proyecto: el ``ArchitecturePlan`` durable y
    el perfil de capacidades del plan. El Planner no participa en su construcción.

    Args:
        architecture: ``ArchitecturePlan`` durable, o ``None`` si el plan no lo trae.
        capability_entries: pares ``(familia, valor)`` del perfil de capacidades aceptado.

    Returns:
        El conjunto de recursos autorizados del proyecto.
    """
    tokens: list[str] = []
    if architecture is not None:
        style = getattr(architecture, "architecture_style", "")
        if style:
            tokens.append(resource_token(ResourceDimension.TECHNOLOGY, style))
        for store in getattr(architecture, "data_stores", ()):
            engine = getattr(store, "engine", "")
            if engine:
                tokens.append(resource_token(ResourceDimension.DATASTORE, engine))
            name = getattr(store, "name", "")
            if name:
                tokens.append(resource_token(ResourceDimension.DATASTORE, name))
        for integration in getattr(architecture, "external_integrations", ()):
            name = getattr(integration, "name", "")
            if name:
                tokens.append(resource_token(ResourceDimension.INTEGRATION, name))
            protocol = getattr(integration, "protocol", "")
            if protocol:
                tokens.append(resource_token(ResourceDimension.CAPABILITY, protocol))
        for service in getattr(architecture, "services", ()):
            tokens.append(resource_token(ResourceDimension.SERVICE, service))
        for module in getattr(architecture, "modules", ()):
            tokens.append(resource_token(ResourceDimension.COMPONENT, module))
        for component in getattr(architecture, "components", ()):
            identifier = getattr(component, "id", "")
            if identifier:
                tokens.append(resource_token(ResourceDimension.COMPONENT, identifier))
        for interface in getattr(architecture, "interfaces", ()):
            identifier = getattr(interface, "id", "")
            if identifier:
                tokens.append(resource_token(ResourceDimension.INTERFACE, identifier))
        for boundary in getattr(architecture, "security_boundaries", ()):
            identifier = getattr(boundary, "id", "")
            name = getattr(boundary, "name", "")
            tokens.append(resource_token(ResourceDimension.SECURITY, name or identifier))
        topology = getattr(architecture, "deployment_topology", "")
        if topology:
            tokens.append(resource_token(ResourceDimension.DEPLOYMENT, topology))
        for choice in getattr(architecture, "technology_choices", ()):
            topic = getattr(choice, "topic", "")
            value = getattr(choice, "choice", "")
            if topic and value:
                tokens.append(resource_token(ResourceDimension.TECHNOLOGY, f"{topic}:{value}"))
            if value:
                tokens.append(resource_token(ResourceDimension.PACKAGE, value))
        for decision in getattr(architecture, "technology_decisions", ()):
            topic = getattr(decision, "topic", "")
            value = getattr(decision, "decision", "")
            if value:
                tokens.append(resource_token(ResourceDimension.TECHNOLOGY, f"{topic}:{value}"))
    for family, value in capability_entries:
        for dimension in _CAPABILITY_DIMENSIONS.get(family, (ResourceDimension.CAPABILITY,)):
            tokens.append(resource_token(dimension, value))
    return ResourceSet.of(tokens)


def request_resources(
    *, uses_resources: Iterable[str] = (), uses_capabilities: Iterable[str] = (), target: str = ""
) -> ResourceSet:
    """Recursos **pedidos** por una propuesta (``ResourceSet.from_requests``).

    Los campos ``uses_*`` de la propuesta son peticiones del Planner, no autorizaciones: se
    canonicalizan aquí para poder compararlos con el envelope autorizado. Una capacidad sin
    dimensión declarada se interpreta como ``capability:``, que es la lectura conservadora: hay que
    estar autorizado a ella.
    """
    tokens: list[str] = []
    for raw in uses_resources:
        value = raw.strip()
        if not value:
            continue
        dimension, _, name = value.partition(":")
        if name and dimension in {item.value for item in ResourceDimension}:
            tokens.append(resource_token(ResourceDimension(dimension), name))
        else:
            tokens.append(resource_token(ResourceDimension.CAPABILITY, value))
    tokens.extend(capability_token(value) for value in uses_capabilities if value.strip())
    if target.strip():
        tokens.append(resource_token(ResourceDimension.DEPLOYMENT, target))
    return ResourceSet.of(tokens)


def resource_envelope_fingerprint(resources: ResourceSet) -> str:
    """Huella canónica del envelope de recursos autorizados.

    Ata la aprobación humana (y el veredicto estructural) a **este** conjunto: dos contratos con
    envelopes distintos no pueden compartir huella, y una prueba emitida para uno no ampara al otro.
    """
    material = json.dumps(list(resources.tokens), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def expansion_report(requested: ResourceSet, authorized: ResourceSet) -> ExpansionReport:
    """Compara lo pedido con lo autorizado: contención, expansión o incertidumbre."""
    return ExpansionReport(expanded=requested.difference(authorized).tokens)


__all__ = [
    "DEPENDENCY_DIMENSIONS",
    "MAX_RESOURCE_NAME_CHARS",
    "MAX_RESOURCE_TOKENS",
    "AuthorityEnvelope",
    "ExpansionReport",
    "ResourceDimension",
    "ResourceSet",
    "capability_token",
    "contract_resources",
    "expansion_report",
    "is_resource_relevant",
    "module_roots",
    "parser_for",
    "project_resource_envelope",
    "request_resources",
    "resource_envelope_fingerprint",
    "resource_token",
    "resources_from_diff",
    "unannounced_surfaces",
    "unproven_effect",
]
