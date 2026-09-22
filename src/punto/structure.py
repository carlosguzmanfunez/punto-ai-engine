"""Consistencia ESTRUCTURAL: una fuente canónica única, usada por sus consumidores, sin
definiciones paralelas duplicadas en el alcance.

EVIDENCE MODALITY ROUTING. Un criterio como «unificar los tipos de propiedad en una sola fuente
de verdad... entre filtros, formularios, validaciones y visualizaciones» no es una afirmación
sobre el ASPECTO de la interfaz (una captura no puede demostrar que existe una única fuente, que
los consumidores la importan o que no hay catálogos duplicados): es una afirmación ESTRUCTURAL,
verificable de forma estática y determinista, igual que ``cartography.py`` demuestra una
afirmación cartográfica con un dataset real en vez de mirar un mapa dibujado a mano.

Este módulo es puro y no depende de ``acceptance.py`` (misma frontera que ``cartography.py``):
analiza texto y ficheros, nunca invoca un modelo ni conoce Tasks, Human Gates ni proyectos.
``acceptance.py`` consume su resultado (duck-typed, como ya hace con los datasets cartográficos)
para producir el ``ClaimRecord`` del criterio.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = ["StructuralEvidence", "analyze_structural_consistency"]

_CODE_SUFFIXES: Final[tuple[str, ...]] = (".ts", ".tsx", ".js", ".jsx", ".mjs")
_TEST_MARKERS: Final[tuple[str, ...]] = ("/test/", "/tests/", ".test.", ".spec.", "/__tests__/")

#: Nombre exportado que declara una definición «canónica»: una constante, un tipo o un esquema.
_EXPORT_NAME: Final[re.Pattern[str]] = re.compile(
    r"export\s+(?:const|type|interface|enum)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
_IMPORT_SPECIFIER: Final[re.Pattern[str]] = re.compile(
    r"""from\s+['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]\s*\)"""
)
_RESOLVE_SUFFIXES: Final[tuple[str, ...]] = (
    "",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    "/index.ts",
    "/index.tsx",
    "/index.js",
)

#: La cláusula que nombra qué se unifica: «unificar X en una sola fuente...».
_TOPIC_CLAUSE: Final[re.Pattern[str]] = re.compile(
    r"(?:unificar|unify|consolidar|consolidate)\s+(.+?)\s+en\s+una",
    re.IGNORECASE,
)
#: La cláusula que nombra los consumidores: «...entre A, B y C».
_CONSUMERS_CLAUSE: Final[re.Pattern[str]] = re.compile(
    r"(?:entre|among|across)\s+(.+?)(?:[.;]|$)", re.IGNORECASE
)
_SPLIT_CONSUMERS: Final[re.Pattern[str]] = re.compile(r",|\by\b|\band\b", re.IGNORECASE)

#: Palabras vacías (mínimo, solo para no tratarlas como tema/consumidor).
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {"el", "la", "los", "las", "de", "del", "un", "una", "unos", "unas", "en", "para", "the", "of"}
)


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", str(text))
    plain = "".join(char for char in folded if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", plain.casefold()).strip()


def _tokens(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for raw in re.findall(r"[a-z0-9_]{3,}", _normalize(text)):
        if raw in _STOPWORDS or raw in found:
            continue
        found.append(raw)
    return tuple(found)


def _stem_match(a: str, b: str) -> bool:
    """Coincidencia tolerante a plural/derivación (``formulario``/``formularios``)."""
    if a == b:
        return True
    return len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a))


def _any_stem_match(tokens_a: Sequence[str], tokens_b: Sequence[str]) -> bool:
    return any(_stem_match(a, b) for a in tokens_a for b in tokens_b)


def _identifier_tokens(name: str) -> tuple[str, ...]:
    """Palabras de un identificador (``TIPOS_PROPIEDAD``/``PropertyType`` -> tipos, propiedad)."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ")
    found: list[str] = []
    for word in re.findall(r"[A-Za-z0-9]+", spaced):
        token = word.casefold()
        if len(token) >= 3 and token not in found:
            found.append(token)
    return tuple(found)


def _is_test_path(path: str) -> bool:
    lowered = "/" + path.replace("\\", "/").casefold().lstrip("/")
    return any(marker in lowered for marker in _TEST_MARKERS)


@dataclass(frozen=True, slots=True)
class StructuralEvidence:
    """Lo que el análisis estático encontró para un criterio de consistencia estructural."""

    #: Tokens del tema que se pide unificar (derivados de la frase, nunca de un proyecto).
    topic: tuple[str, ...]
    #: Ficheros con una definición exportada que declara el tema: la fuente canónica candidata.
    canonical: tuple[str, ...]
    #: Dominios consumidores nombrados en la frase («filtros», «formularios»...), tal cual.
    consumer_domains: tuple[str, ...]
    #: De ``consumer_domains``, los que sí importan alguna de las fuentes canónicas.
    consumers_confirmed: tuple[str, ...]
    #: De ``consumer_domains``, los que no se encontró que importaran ninguna fuente canónica.
    consumers_missing: tuple[str, ...]

    @property
    def has_single_canonical(self) -> bool:
        """True solo si hay exactamente una fuente canónica (ni cero ni duplicada)."""
        return len(self.canonical) == 1

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable (auditoría/grafo)."""
        return {
            "topic": list(self.topic),
            "canonical": list(self.canonical),
            "consumer_domains": list(self.consumer_domains),
            "consumers_confirmed": list(self.consumers_confirmed),
            "consumers_missing": list(self.consumers_missing),
        }


def _topic_and_consumers(sentence: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Deriva el tema y los consumidores de la frase — genérico, no depende de un dominio."""
    topic_match = _TOPIC_CLAUSE.search(sentence)
    topic = _tokens(topic_match.group(1) if topic_match else sentence)
    consumers_match = _CONSUMERS_CLAUSE.search(sentence)
    domains: tuple[str, ...] = ()
    if consumers_match:
        raw = _SPLIT_CONSUMERS.split(consumers_match.group(1))
        domains = tuple(dict.fromkeys(item.strip() for item in raw if item.strip()))
    return topic, domains


def _resolve_specifier(spec: str, base: str) -> str:
    """Resuelve un especificador de import a una ruta del repositorio (alias ``@/`` o relativa)."""
    if spec.startswith("@/"):
        return "src/" + spec[2:]
    if not spec.startswith("."):
        return ""
    parts: list[str] = []
    for chunk in f"{base}/{spec}".split("/"):
        if chunk in {"", "."}:
            continue
        if chunk == "..":
            if parts:
                parts.pop()
            continue
        parts.append(chunk)
    return "/".join(parts)


def _imported_targets(content: str, path: str) -> tuple[str, ...]:
    base = path.rsplit("/", maxsplit=1)[0] if "/" in path else ""
    targets: list[str] = []
    for match in _IMPORT_SPECIFIER.finditer(content):
        spec = match.group(1) or match.group(2)
        target = _resolve_specifier(spec, base)
        if target:
            targets.append(target)
    return tuple(targets)


def analyze_structural_consistency(
    sentence: str,
    *,
    files: Sequence[str],
    read_text: Callable[[str], str],
    exists: Callable[[str], bool],
) -> StructuralEvidence:
    """Analiza el repositorio para un criterio «unificar X en una sola fuente... entre A, B, C».

    Determinista y sin capacidad de modelo: busca definiciones EXPORTADAS cuyo nombre coincide con
    el tema (candidatas a fuente canónica) y, para cada consumidor nombrado en la frase, si algún
    fichero de su dominio importa una de esas candidatas. Ni el tema ni los consumidores están
    prefijados: se derivan de la propia frase.
    """
    del exists  # la resolución de import ya se verifica leyendo el fichero candidato
    topic, domains = _topic_and_consumers(sentence)
    code_files = tuple(
        path.replace("\\", "/")
        for path in files
        if path.casefold().endswith(_CODE_SUFFIXES) and not _is_test_path(path)
    )
    contents: dict[str, str] = {}
    canonical: list[str] = []
    for path in code_files:
        try:
            content = read_text(path)
        except Exception:
            continue
        contents[path] = content
        if not topic:
            continue
        for match in _EXPORT_NAME.finditer(content):
            if _any_stem_match(topic, _identifier_tokens(match.group(1))):
                canonical.append(path)
                break
    canonical_tuple = tuple(dict.fromkeys(canonical))
    canonical_set = set(canonical_tuple)

    def _resolves_to_canonical(target: str) -> bool:
        return any(target + suffix in canonical_set for suffix in _RESOLVE_SUFFIXES)

    confirmed: list[str] = []
    missing: list[str] = []
    for domain in domains:
        domain_tokens = _tokens(domain)
        found = False
        for path, content in contents.items():
            if path in canonical_set:
                continue
            if domain_tokens and not _any_stem_match(domain_tokens, _tokens(path)):
                continue
            if any(_resolves_to_canonical(target) for target in _imported_targets(content, path)):
                confirmed.append(domain)
                found = True
                break
        if not found:
            missing.append(domain)

    return StructuralEvidence(
        topic=topic,
        canonical=canonical_tuple,
        consumer_domains=domains,
        consumers_confirmed=tuple(confirmed),
        consumers_missing=tuple(missing),
    )
