"""Evidencia de aceptación: criterios verificables medidos contra la **superficie solicitada**.

AP000-OBS-02. Una Task no puede darse por satisfecha porque exista una implementación *relacionada*,
porque compile o porque el proveedor diga que terminó. Cuando la solicitud se refiere a un elemento
**existente** («reemplazar el placeholder actual…», «eliminar X», «modificar X en A»,
PUNTO puede localizarlo **antes** de construir y comprobar **después**, de forma determinista:

1. **Grounding (antes del build)**: se extraen de la solicitud las referencias a elementos
   y se localizan en el repositorio (superficie, línea y fragmento). Lo localizado es la
   *precondición*: la evidencia que dice dónde estaba X.
2. **Preflight (antes del build)**: el plan tiene que cubrir esas superficies; si el plan trabaja
   solo en otra parte, se rechaza y el proveedor vuelve a planificar con la superficie delante.
3. **Acceptance (después del build)**: se vuelve a medir: el elemento localizado debe haber cambiado
   (reemplazar/modificar), haber desaparecido (eliminar) o seguir existiendo (conservar). Si no, la
   ronda **no** está superada y entra en la cadena normal de reparación.

Lo que no se puede medir de forma determinista (criterios subjetivos como «integrado con el diseño»)
**no** se convierte en una comprobación falsa: queda como `NOT_MEASURABLE` y sigue su camino hacia
el QA que corresponda.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Final

__all__ = [
    "CAPABILITY_VISION",
    "INTERACTION_MARKERS",
    "AcceptanceRecord",
    "CapabilityRequirement",
    "ClaimKind",
    "ClaimRecord",
    "ElementKind",
    "LocatedSurface",
    "RequestIntent",
    "RequestReference",
    "SemanticClaim",
    "VisualCapability",
    "capability_requirements",
    "claims_result",
    "extract_claims",
    "extract_references",
    "ground_request",
    "is_interaction_claim",
    "measurable",
    "tokens",
    "verify_acceptance",
    "verify_claims",
]

#: Tope de superficies localizadas por referencia (las mejores, en orden determinista).
MAX_SURFACES_PER_REFERENCE: Final[int] = 3

#: Tope de referencias extraídas de una solicitud.
MAX_REFERENCES: Final[int] = 16

#: Ventana de caracteres alrededor de un marcador donde se buscan los términos del tema.
MARKER_WINDOW: Final[int] = 420

#: Fragmento que se guarda como evidencia de lo localizado.
MAX_SNIPPET_CHARS: Final[int] = 240

#: Puntuación mínima (términos del tema distintos) para considerar localizado un elemento.
MIN_TOPIC_SCORE: Final[int] = 3


class RequestIntent(StrEnum):
    """Qué pide la solicitud sobre el elemento referenciado."""

    REPLACE = "REPLACE"
    DELETE = "DELETE"
    MODIFY = "MODIFY"
    CREATE = "CREATE"
    PRESERVE = "PRESERVE"


class ElementKind(StrEnum):
    """Tipo de elemento referenciado, con los marcadores con los que se localiza en el código."""

    PLACEHOLDER = "PLACEHOLDER"
    SECTION = "SECTION"
    BUTTON = "BUTTON"
    FIELD = "FIELD"
    LIST = "LIST"
    COMPONENT = "COMPONENT"
    ROUTE = "ROUTE"
    ENDPOINT = "ENDPOINT"
    FILE = "FILE"
    SCHEMA = "SCHEMA"
    TEXT = "TEXT"
    NONE = "NONE"


#: Verbos/marcas de intención, en español e inglés.
INTENT_MARKERS: Final[dict[str, tuple[str, ...]]] = {
    RequestIntent.REPLACE.value: (
        "reemplazar", "reemplaza", "sustituir", "sustituye", "replace", "swap", "en lugar de",
    ),
    RequestIntent.DELETE.value: (
        "eliminar", "elimina", "borrar", "borra", "quitar", "quita", "retirar", "retira",
        "delete", "remove", "drop",
    ),
    RequestIntent.MODIFY.value: (
        "modificar", "modifica", "actualizar", "actualiza", "cambiar", "cambia", "ajustar",
        "ajusta", "update", "change", "modify",
    ),
    RequestIntent.CREATE.value: (
        "crear", "crea", "añadir", "añade", "agregar", "agrega", "incorporar", "incorpora",
        "implementar", "implementa", "create", "add", "build",
    ),
    RequestIntent.PRESERVE.value: (
        "conservar", "conserva", "mantener", "mantén", "manteniendo", "sin romper", "no romper",
        "no se rompe", "continúa funcionando", "continua funcionando", "sigue funcionando",
        "preserve", "keep", "without breaking",
    ),
}

#: Marcadores con los que cada tipo de elemento aparece en el código.
KIND_MARKERS: Final[dict[str, tuple[str, ...]]] = {
    ElementKind.PLACEHOLDER.value: ("placeholder", "marcador", "coming soon", "proximamente"),
    ElementKind.SECTION.value: ("<section", "classname=\"section", "class=\"section"),
    ElementKind.BUTTON.value: ("<button", "classname=\"button", "class=\"button", "role=\"button"),
    ElementKind.FIELD.value: ("<input", "<select", "<textarea", "<label"),
    ElementKind.LIST.value: ("listado", "grid", "lista", "list"),
    ElementKind.COMPONENT.value: ("export function", "export default function", "component"),
    ElementKind.ROUTE.value: ("page.tsx", "page.ts", "route.ts", "route.js", "@application.get"),
    ElementKind.ENDPOINT.value: ("@application", "@app.", "router.", "route.ts"),
    ElementKind.SCHEMA.value: ("schema", "table(", "create table", "model"),
    ElementKind.TEXT.value: (),
    ElementKind.NONE.value: (),
    ElementKind.FILE.value: (),
}

#: Palabras del tipo de elemento en la solicitud → tipo.
KIND_WORDS: Final[dict[str, tuple[str, ...]]] = {
    ElementKind.PLACEHOLDER.value: ("placeholder", "marcador", "provisional"),
    ElementKind.SECTION.value: ("seccion", "section", "bloque", "apartado"),
    ElementKind.BUTTON.value: ("boton", "button", "cta"),
    ElementKind.FIELD.value: ("campo", "input", "select", "formulario", "field"),
    ElementKind.LIST.value: ("listado", "lista", "grid", "rejilla", "list"),
    ElementKind.COMPONENT.value: ("componente", "component", "widget"),
    ElementKind.ROUTE.value: ("ruta", "pagina", "page", "vista", "homepage", "portada", "route"),
    ElementKind.ENDPOINT.value: ("endpoint", "api", "servicio"),
    ElementKind.FILE.value: ("archivo", "fichero", "file"),
    ElementKind.SCHEMA.value: ("esquema", "schema", "tabla", "table", "modelo"),
}

#: Sinónimos español → formas que aparecen en el código (incluido inglés y nombres técnicos).
TOPIC_SYNONYMS: Final[dict[str, tuple[str, ...]]] = {
    "mapa": ("map", "mapa"),
    "mapas": ("map", "mapa"),
    "cartografico": ("map", "cartograph", "cartografico"),
    "cobertura": ("coverage", "cobertura"),
    "nacional": ("national", "nacional", "country"),
    "departamento": ("department", "departamento"),
    "departamentos": ("department", "departamento"),
    "propiedad": ("property", "propiedad", "propert"),
    "propiedades": ("property", "propiedad", "propert"),
    "interactivo": ("interactive", "interactivo"),
    "listado": ("list", "grid", "listing", "listado"),
    "diseno": ("design", "layout", "diseno"),
    "placeholder": ("placeholder",),
    "homepage": ("page.tsx", "home"),
    "portada": ("page.tsx", "home"),
    "usuario": ("user", "usuario"),
    "busqueda": ("search", "busqueda", "filter"),
    "filtro": ("filter", "filtro"),
    "filtros": ("filter", "filtros"),
    "precio": ("price", "precio"),
    "precios": ("price", "precio"),
    "telefono": ("phone", "telefono"),
    "formulario": ("form", "formulario"),
    "contacto": ("contact", "contacto"),
    "imagen": ("image", "imagen", "photo"),
    "imagenes": ("image", "imagen", "photo"),
    "rendimiento": ("performance", "rendimiento"),
    "movil": ("mobile", "movil", "responsive"),
    "responsive": ("responsive",),
}

#: Palabras vacías que no describen ningún tema.
STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "que", "los", "las", "del", "una", "uno", "unos", "unas", "por", "para", "con", "como",
        "mas", "sin", "sobre", "entre", "desde", "hasta", "este", "esta", "estos", "estas", "ese",
        "esa", "esos", "esas", "actual", "actualmente", "existente", "existentes", "nuevo", "nueva",
        "debe", "deben", "deberia", "debeia", "puede", "puedan", "poder", "hacer", "hacerlo",
        "usar", "usando", "utilizar", "utilizando", "seguir", "sigue", "siguiente", "todo", "toda",
        "todos", "todas", "cada", "donde", "cuando", "tambien", "solo", "sola", "parte", "tipo",
        "and", "for", "from", "with", "the", "add", "new", "fix", "use", "using", "should", "must",
        "when", "where", "this", "that", "these", "those", "into", "your", "their", "its", "all",
        "any", "can", "will", "not", "but", "porque", "pues", "sea", "sean", "ser",
    }
)

#: Marcas de que la frase habla de algo que **ya existe** (y por tanto se puede localizar).
EXISTING_MARKERS: Final[tuple[str, ...]] = (
    "actual",
    "actuales",
    "existente",
    "existentes",
    "en uso",
    "ya existe",
    "hoy",
    "current",
    "existing",
    "placeholder",
)

#: Intenciones que por sí solas implican un elemento existente (reemplazar o eliminar algo).
DESTRUCTIVE_INTENTS: Final[frozenset[str]] = frozenset(
    {RequestIntent.REPLACE.value, RequestIntent.DELETE.value}
)

_QUOTED: Final[re.Pattern[str]] = re.compile(r"[«“\"'`]([^«»“”\"'`\n]{3,120})[»”\"'`]")
_WORD: Final[re.Pattern[str]] = re.compile(r"[a-z0-9_./-]{3,}")
_PATHLIKE: Final[re.Pattern[str]] = re.compile(
    r"[\w./-]+\.(?:tsx?|jsx?|mjs|cjs|py|go|rs|json|ya?ml|sql|css)"
)


@dataclass(frozen=True, slots=True)
class LocatedSurface:
    """Superficie donde se localizó el elemento referenciado, con su fragmento."""

    path: str
    line: int
    snippet: str
    score: int

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable, acotada."""
        return {
            "path": self.path,
            "line": self.line,
            "snippet": self.snippet,
            "score": self.score,
        }


@dataclass(frozen=True, slots=True)
class RequestReference:
    """Referencia de la solicitud a un elemento, con lo localizado (si se pudo localizar)."""

    sentence: str
    intent: str
    kind: str
    literal: str = ""
    topics: tuple[str, ...] = ()
    surfaces: tuple[LocatedSurface, ...] = ()

    @property
    def measurable(self) -> bool:
        """True si PUNTO puede medirla de forma determinista."""
        if self.literal:
            return True
        return self.kind != ElementKind.NONE.value and bool(self.surfaces)

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable."""
        return {
            "sentence": self.sentence,
            "intent": self.intent,
            "kind": self.kind,
            "literal": self.literal,
            "topics": list(self.topics),
            "surfaces": [item.as_dict() for item in self.surfaces],
            "measurable": self.measurable,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceRecord:
    """Evidencia de aceptación de una referencia: precondición, postcondición y resultado."""

    sentence: str
    intent: str
    kind: str
    surface: str
    precondition: str
    postcondition: str
    result: str

    @property
    def satisfied(self) -> bool:
        """True solo si el criterio quedó demostrado."""
        return self.result == "SATISFIED"

    @property
    def failed(self) -> bool:
        """True si el criterio se midió y **no** se cumplió."""
        return self.result == "UNSATISFIED"

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable."""
        return {
            "sentence": self.sentence,
            "intent": self.intent,
            "kind": self.kind,
            "surface": self.surface,
            "precondition": self.precondition,
            "postcondition": self.postcondition,
            "result": self.result,
        }


def normalize(text: str) -> str:
    """Texto comparable: sin acentos, en minúsculas y con espacios colapsados."""
    folded = unicodedata.normalize("NFKD", str(text))
    plain = "".join(char for char in folded if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", plain.casefold()).strip()


def tokens(text: str) -> tuple[str, ...]:
    """Tokens significativos de un texto libre, sin acentos ni palabras vacías."""
    found: list[str] = []
    for raw in _WORD.findall(normalize(text)):
        if raw in STOPWORDS or raw in found:
            continue
        found.append(raw)
    return tuple(found)


def _sentences(text: str) -> tuple[str, ...]:
    """Divide una solicitud en frases: la intención se lee por frase, no por documento."""
    parts = re.split(r"(?<=[.;:!?\n])\s+", str(text))
    return tuple(item.strip() for item in parts if item.strip())


def _intent_of(sentence: str) -> str:
    """Intención dominante de una frase (el verbo de intención que aparece antes)."""
    plain = normalize(sentence)
    best_position = len(plain) + 1
    best_intent = RequestIntent.MODIFY.value
    for intent, markers in INTENT_MARKERS.items():
        for marker in markers:
            position = plain.find(marker)
            if 0 <= position < best_position:
                best_position = position
                best_intent = intent
    return best_intent


def _kind_of(sentence: str) -> str:
    """Tipo de elemento referenciado, por su palabra en la solicitud."""
    plain = normalize(sentence)
    for kind, words in KIND_WORDS.items():
        if any(re.search(rf"\b{re.escape(word)}", plain) for word in words):
            return kind
    return ElementKind.NONE.value


def _topics_of(sentence: str) -> tuple[str, ...]:
    """Términos del tema, expandidos a las formas que aparecen en el código."""
    expanded: list[str] = []
    for token in tokens(sentence):
        forms = TOPIC_SYNONYMS.get(token, (token,))
        for form in forms:
            if form not in expanded:
                expanded.append(form)
    return tuple(expanded)


def _is_groundable(
    sentence: str, *, literal: str, paths: Sequence[str], intent: str, kind: str
) -> bool:
    """True solo si la frase se refiere a algo existente que PUNTO pueda localizar.

    Es la frontera de precisión del grounding: una petición que **no** habla de un elemento
    existente («unificar la lista de tipos», «añadir un filtro») no genera obligaciones de
    aceptación —no se inventa una comprobación— y un criterio subjetivo sigue su QA.
    """
    if literal or paths:
        return True
    if kind == ElementKind.NONE.value:
        return False
    if intent in DESTRUCTIVE_INTENTS:
        return True
    plain = normalize(sentence)
    return any(marker in plain for marker in EXISTING_MARKERS)


def extract_references(
    objective: str, criteria: Iterable[str] = ()
) -> tuple[RequestReference, ...]:
    """Extrae de la solicitud las referencias a elementos, con su intención y su tema.

    No toca el repositorio: es la parte pura del grounding (lo que se puede probar sin ficheros).
    """
    references: list[RequestReference] = []
    for text in (objective, *criteria):
        for sentence in _sentences(text):
            literal = ""
            quoted = _QUOTED.search(sentence)
            if quoted:
                literal = quoted.group(1).strip()
            kind = ElementKind.TEXT.value if literal else _kind_of(sentence)
            # Una ruta explícita en la frase también es una referencia a una superficie concreta.
            paths = tuple(match.group(0) for match in _PATHLIKE.finditer(sentence))
            if paths and kind == ElementKind.NONE.value:
                kind = ElementKind.FILE.value
            topics = _topics_of(sentence)
            intent = _intent_of(sentence)
            # Sin tipo, sin literal y sin ruta no hay nada medible de forma determinista: no se
            # inventa una comprobación y el criterio sigue su camino hacia el QA que corresponda.
            if not _is_groundable(
                sentence, literal=literal, paths=paths, intent=intent, kind=kind
            ):
                continue
            if kind == ElementKind.NONE.value and not literal and len(topics) < MIN_TOPIC_SCORE:
                continue
            references.append(
                RequestReference(
                    sentence=sentence[:300],
                    intent=intent,
                    kind=kind,
                    literal=literal[:200],
                    topics=topics,
                )
            )
            if len(references) >= MAX_REFERENCES:
                return tuple(references)
    return tuple(references)


def _marker_hits(content: str, markers: Sequence[str]) -> tuple[int, str]:
    """Primer marcador presente en el contenido y la posición donde aparece.

    El marcador ``placeholder`` de un **elemento** se distingue del atributo ``placeholder=`` de un
    campo de formulario: el atributo no es «el placeholder» que una persona pide reemplazar, así que
    no se acepta como superficie del elemento.
    """
    plain = content.casefold()
    for marker in markers:
        position = plain.find(marker)
        while position >= 0:
            if marker != "placeholder" or not _is_attribute(plain, position, marker):
                return position, marker
            position = plain.find(marker, position + 1)
    return -1, ""


def _is_attribute(plain: str, position: int, marker: str) -> bool:
    """True si la coincidencia es una asignación de atributo (``placeholder="..."``)."""
    after = plain[position + len(marker) : position + len(marker) + 3].lstrip()
    return after.startswith("=")


def _score_window(content: str, position: int, topics: Sequence[str]) -> int:
    """Términos distintos del tema presentes alrededor del marcador."""
    start = max(position - MARKER_WINDOW, 0)
    window = normalize(content[start : position + MARKER_WINDOW])
    return sum(1 for topic in topics if normalize(topic) in window and len(topic) >= 3)


def locate_reference(
    reference: RequestReference,
    *,
    files: Sequence[str],
    read_text: Callable[[str], str],
    max_surfaces: int = MAX_SURFACES_PER_REFERENCE,
    min_score: int = MIN_TOPIC_SCORE,
) -> RequestReference:
    """Localiza la referencia en el repositorio: superficie, línea y fragmento.

    Un elemento se considera localizado cuando el fichero contiene el marcador de su tipo **y** al
    menos ``min_score`` términos distintos del tema a su alrededor. Una ruta explícita en la frase
    localiza esa superficie directamente, y un literal entrecomillado se localiza por coincidencia
    exacta. Nada de esto usa el modelo: es búsqueda determinista.
    """
    explicit = _explicit_paths(reference, files, read_text=read_text)
    if reference.literal:
        surfaces = _locate_literal(reference.literal, files=files, read_text=read_text)
        return replace(reference, surfaces=surfaces)
    if explicit:
        surfaces = tuple(
            LocatedSurface(path=path, line=1, snippet=path, score=len(reference.topics) + 1)
            for path in explicit[:max_surfaces]
        )
        return replace(reference, surfaces=surfaces)
    markers = KIND_MARKERS.get(reference.kind, ())
    if not markers or not reference.topics:
        return reference
    candidates: list[LocatedSurface] = []
    for path in files:
        if _is_stylesheet(path):
            # Una hoja de estilos **estiliza** el elemento; no es la superficie donde vive.
            continue
        try:
            content = read_text(path)
        except Exception:  # fichero ilegible, fuera de alcance o con secretos: no es superficie
            continue
        position, marker = _marker_hits(content, markers)
        if position < 0:
            continue
        score = _score_window(content, position, reference.topics)
        if score < min_score:
            continue
        line = content.count("\n", 0, position) + 1
        snippet = _snippet(content, position, marker)
        candidates.append(LocatedSurface(path=path, line=line, snippet=snippet, score=score))
    candidates.sort(key=lambda item: (-item.score, item.path))
    return replace(reference, surfaces=tuple(candidates[:max_surfaces]))


def _is_stylesheet(path: str) -> bool:
    """True si el fichero es una hoja de estilos (estiliza elementos, no los contiene)."""
    return path.casefold().endswith((".css", ".scss", ".sass", ".less"))


def _explicit_paths(
    reference: RequestReference, files: Sequence[str], *, read_text: Callable[[str], str]
) -> tuple[str, ...]:
    """Rutas nombradas en la frase que existen de verdad y **contienen** el elemento.

    Una ruta mencionada no basta: si la frase habla de un placeholder, el fichero tiene que tener
    ese marcador. Así «reutilizando ``src/lib/honduras.ts``» no convierte ese fichero en una
    superficie que deba cambiar.
    """
    known = {path.replace("\\", "/"): path for path in files}
    markers = KIND_MARKERS.get(reference.kind, ())
    found: list[str] = []
    for match in _PATHLIKE.finditer(reference.sentence):
        candidate = match.group(0).replace("\\", "/").lstrip("./")
        for path, original in known.items():
            if not path.endswith(candidate) or original in found:
                continue
            if not markers:
                found.append(original)
                continue
            # El marcador puede estar en la propia ruta (``page.tsx``) o en el contenido.
            if any(marker in path for marker in markers):
                found.append(original)
                continue
            try:
                content = read_text(original)
            except Exception:
                continue
            position, _marker = _marker_hits(content, markers)
            if position >= 0:
                found.append(original)
    return tuple(found)


def _locate_literal(
    literal: str, *, files: Sequence[str], read_text: Callable[[str], str]
) -> tuple[LocatedSurface, ...]:
    """Localiza un texto entrecomillado de la solicitud, tal cual, en el repositorio."""
    found: list[LocatedSurface] = []
    needle = normalize(literal)
    for path in files:
        try:
            content = read_text(path)
        except Exception:
            continue
        plain = normalize(content)
        position = plain.find(needle)
        if position < 0:
            continue
        line = plain.count("\n", 0, position) + 1
        found.append(
            LocatedSurface(
                path=path,
                line=line,
                snippet=literal[:MAX_SNIPPET_CHARS],
                score=len(tokens(literal)),
            )
        )
    return tuple(found[:MAX_SURFACES_PER_REFERENCE])


def _snippet(content: str, position: int, marker: str) -> str:
    """Fragmento acotado alrededor del marcador, con la línea del elemento."""
    start = content.rfind("\n", 0, position) + 1
    end = content.find("\n", position)
    if end < 0:
        end = len(content)
    return content[start:end].strip()[:MAX_SNIPPET_CHARS] or marker


def ground_request(
    *,
    objective: str,
    criteria: Iterable[str] = (),
    files: Sequence[str],
    read_text: Callable[[str], str],
) -> tuple[RequestReference, ...]:
    """Grounding completo: extrae referencias y las localiza en el repositorio."""
    references = extract_references(objective, criteria)
    return tuple(
        locate_reference(reference, files=files, read_text=read_text) for reference in references
    )


def measurable(references: Sequence[RequestReference]) -> tuple[RequestReference, ...]:
    """Referencias que PUNTO puede medir de forma determinista."""
    return tuple(item for item in references if item.measurable)


def verify_acceptance(
    references: Sequence[RequestReference],
    *,
    read_text: Callable[[str], str],
    changed_paths: Sequence[str],
    exists: Callable[[str], bool],
) -> tuple[AcceptanceRecord, ...]:
    """Mide las postcondiciones contra las superficies reales, después del build.

    Cada referencia medible produce un registro con su precondición (dónde estaba X), su
    postcondición (qué se midió después) y el resultado. Las no medibles quedan como
    ``NOT_MEASURABLE``: no se inventa una comprobación estática para un criterio subjetivo.
    """
    changed = {path.replace("\\", "/") for path in changed_paths}
    records: list[AcceptanceRecord] = []
    for reference in references:
        if reference.literal:
            records.append(_verify_literal(reference, read_text=read_text, changed=changed))
            continue
        if not reference.surfaces:
            records.append(
                AcceptanceRecord(
                    sentence=reference.sentence,
                    intent=reference.intent,
                    kind=reference.kind,
                    surface="",
                    precondition="no se localizó ningún elemento existente para esta referencia",
                    postcondition="criterio no medible de forma determinista: sigue su QA",
                    result="NOT_MEASURABLE",
                )
            )
            continue
        for surface in reference.surfaces:
            records.append(
                _verify_surface(
                    reference, surface, read_text=read_text, changed=changed, exists=exists
                )
            )
    return tuple(records)


def _verify_literal(
    reference: RequestReference, *, read_text: Callable[[str], str], changed: set[str]
) -> AcceptanceRecord:
    """Un texto entrecomillado: si se pide reemplazar/eliminar, no puede seguir estando."""
    del changed
    surfaces = reference.surfaces
    if not surfaces:
        return AcceptanceRecord(
            sentence=reference.sentence,
            intent=reference.intent,
            kind=reference.kind,
            surface="",
            precondition=f"el texto {reference.literal!r} no se localizó en el repositorio",
            postcondition="no se puede medir sin localizar el texto: sigue su QA",
            result="NOT_MEASURABLE",
        )
    if reference.intent == RequestIntent.PRESERVE.value:
        present = all(_contains(read_text, item.path, reference.literal) for item in surfaces)
        return AcceptanceRecord(
            sentence=reference.sentence,
            intent=reference.intent,
            kind=reference.kind,
            surface=surfaces[0].path if surfaces else "",
            precondition=f"el texto {reference.literal!r} estaba en "
            f"{', '.join(item.path for item in surfaces) or 'ninguna superficie'}",
            postcondition="el texto sigue presente" if present else "el texto desapareció",
            result="SATISFIED" if present else "UNSATISFIED",
        )
    remaining = [
        item.path for item in surfaces if _contains(read_text, item.path, reference.literal)
    ]
    return AcceptanceRecord(
        sentence=reference.sentence,
        intent=reference.intent,
        kind=reference.kind,
        surface=", ".join(item.path for item in surfaces),
        precondition=f"el texto {reference.literal!r} estaba en "
        f"{', '.join(item.path for item in surfaces) or 'ninguna superficie'}",
        postcondition=(
            f"sigue presente en {', '.join(remaining)}" if remaining else "el texto ya no está"
        ),
        result="UNSATISFIED" if remaining else "SATISFIED",
    )


def _verify_surface(
    reference: RequestReference,
    surface: LocatedSurface,
    *,
    read_text: Callable[[str], str],
    changed: set[str],
    exists: Callable[[str], bool],
) -> AcceptanceRecord:
    """Postcondición sobre un elemento localizado en una superficie concreta."""
    path = surface.path
    present = exists(path)
    content = read_text(path) if present else ""
    still_there = bool(content) and _still_present(content, surface, reference)
    touched = path.replace("\\", "/") in changed
    intent = reference.intent
    if intent == RequestIntent.PRESERVE.value:
        result = "SATISFIED" if still_there else "UNSATISFIED"
        postcondition = (
            "el elemento que debía conservarse sigue ahí"
            if still_there
            else "el elemento que debía conservarse desapareció"
        )
    elif intent == RequestIntent.CREATE.value:
        result = "SATISFIED" if touched else "UNSATISFIED"
        postcondition = (
            "la superficie solicitada se modificó"
            if touched
            else "la superficie solicitada no cambió"
        )
    else:
        if not touched:
            result = "UNSATISFIED"
            postcondition = "la superficie donde estaba el elemento no se modificó"
        elif still_there:
            result = "UNSATISFIED"
            postcondition = "el elemento localizado sigue igual tras el cambio"
        else:
            result = "SATISFIED"
            postcondition = "el elemento localizado se reemplazó o desapareció"
    return AcceptanceRecord(
        sentence=reference.sentence,
        intent=intent,
        kind=reference.kind,
        surface=f"{path}:{surface.line}",
        precondition=f"el elemento estaba en {path}:{surface.line} ({surface.snippet[:120]})",
        postcondition=postcondition,
        result=result,
    )


def _still_present(content: str, surface: LocatedSurface, reference: RequestReference) -> bool:
    """True si el elemento localizado sigue presente tal cual (o su marcador sigue con el tema)."""
    if surface.snippet and surface.snippet in content:
        return True
    markers = KIND_MARKERS.get(reference.kind, ())
    position, marker = _marker_hits(content, markers)
    if position < 0:
        return False
    return _score_window(content, position, reference.topics) >= MIN_TOPIC_SCORE and bool(marker)


def _contains(read_text: Callable[[str], str], path: str, needle: str) -> bool:
    """True si el texto sigue en el fichero (comparación sin acentos ni mayúsculas)."""
    try:
        return normalize(needle) in normalize(read_text(path))
    except Exception:
        return False


# ===========================================================================
# AP000-OBS-03: afirmaciones factuales/semánticas y su evidencia
# ===========================================================================
class ClaimKind(StrEnum):
    """Propiedad que la solicitud afirma y que no se demuestra con la presencia de algo."""

    CARTOGRAPHIC_CORRECTNESS = "CARTOGRAPHIC_CORRECTNESS"
    VISUAL_APPEARANCE = "VISUAL_APPEARANCE"


#: Marcas de corrección (frente a mera presencia) en una frase.
CORRECTNESS_MARKERS: Final[tuple[str, ...]] = (
    "real",
    "reales",
    "correctamente",
    "correcta",
    "correcto",
    "exacta",
    "exacto",
    "fiel",
    "representa",
    "representan",
    "representados correctamente",
    "corresponde",
    "coincide",
    "verdadera",
)

#: Sujetos geográficos/cartográficos: la frase habla del territorio representado.
GEO_SUBJECTS: Final[tuple[str, ...]] = (
    "mapa",
    "map",
    "cartograf",
    "geometr",
    "departamento",
    "poligono",
    "territorio",
)

#: Sujetos visuales: la frase habla del aspecto, no de los datos.
VISUAL_SUBJECTS: Final[tuple[str, ...]] = (
    "integra",
    "visual",
    "diseno",
    "aspecto",
    "estilo",
    "responsive",
)

#: Marcas de que la frase juzga el **aspecto** (no basta con mencionar el diseño).
VISUAL_MARKERS: Final[tuple[str, ...]] = (
    "visual",
    "visualmente",
    "integra",
    "integrad",
    "estetic",
    "aspecto",
)

#: Capacidad efectiva que exige mirar el resultado renderizado. Se toma del vocabulario del
#: catálogo de proveedores (``punto.providers.registry``) para no inventar una taxonomía paralela.
CAPABILITY_VISION: Final[str] = "VISION"


@dataclass(frozen=True, slots=True)
class SemanticClaim:
    """Afirmación factual/semántica de la solicitud, con la evidencia que exige.

    ``capability`` es la capacidad **efectiva** que hace falta para producir esa evidencia: vacía
    cuando la demostración no depende de ninguna capacidad del modelo (un dataset, por ejemplo) y
    ``VISION`` cuando exige mirar el resultado renderizado.
    """

    sentence: str
    kind: str
    evidence_required: str
    required: bool = True
    capability: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable."""
        return {
            "sentence": self.sentence,
            "kind": self.kind,
            "evidence_required": self.evidence_required,
            "required": self.required,
            "capability": self.capability,
        }


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    """Evidencia de una afirmación: resultado, por qué y con qué capacidad."""

    sentence: str
    kind: str
    result: str
    evidence: str
    required: bool = True
    #: Evidencia que exige la afirmación, tal como la declaró la extracción.
    evidence_required: str = ""
    #: Capacidad efectiva que exigía la demostración (vacía si no exigía ninguna).
    capability: str = ""
    #: True si esa capacidad estaba disponible en la ruta efectiva (o no hacía falta).
    capability_available: bool = True
    #: Detalle real de la capacidad cuando no está disponible (transporte, motivo).
    capability_detail: str = ""
    #: Qué corresponde hacer para obtener la evidencia que falta.
    remedy: str = ""

    @property
    def satisfied(self) -> bool:
        """True solo si la afirmación quedó demostrada."""
        return self.result == "SATISFIED"

    @property
    def unsatisfied(self) -> bool:
        """True si se midió y no se cumple: es reparable."""
        return self.result == "UNSATISFIED"

    @property
    def not_verified(self) -> bool:
        """True si no hay evidencia disponible: no se inventa un PASS."""
        return self.result == "NOT_VERIFIED"

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable."""
        return {
            "sentence": self.sentence,
            "kind": self.kind,
            "result": self.result,
            "evidence": self.evidence,
            "required": self.required,
            "evidence_required": self.evidence_required,
            "capability": self.capability,
            "capability_available": self.capability_available,
            "capability_detail": self.capability_detail,
            "remedy": self.remedy,
        }


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    """Lo que una afirmación exige de la ruta efectiva, evaluado **antes** de construir.

    Es la comprobación que evita exigir una verificación imposible: si la ruta activa no puede
    producir la evidencia, el ciclo lo sabe desde el principio y el desenlace es el gobernado
    (``EVIDENCE_REQUIRED``), nunca un ``VERIFIED`` inventado.
    """

    kind: str
    capability: str
    available: bool
    criterion: str = ""
    detail: str = ""
    remedy: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable."""
        return {
            "kind": self.kind,
            "capability": self.capability,
            "available": self.available,
            "criterion": self.criterion,
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass(frozen=True, slots=True)
class VisualCapability:
    """Capacidad de evidencia visual de la ruta asignada: lo configurado y lo **efectivo**.

    ``available`` es la capacidad **efectiva** (el transporte activo puede recibir imágenes), no lo
    que el modelo soporte teóricamente: si el transporte no las acepta, no hay evidencia visual por
    esa ruta (AP000-OBS-03-R1). ``configured`` conserva lo declarado para poder decir en la interfaz
    que difieren, y ``remedy`` qué corresponde hacer.
    """

    available: bool
    detail: str
    provider: str = ""
    configured: bool = False
    transport: str = ""
    remedy: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable."""
        return {
            "available": self.available,
            "configured": self.configured,
            "detail": self.detail,
            "provider": self.provider,
            "transport": self.transport,
            "remedy": self.remedy,
        }


#: Marcas de un criterio de **interacción** (transición): una captura estática no lo demuestra.
INTERACTION_MARKERS: Final[tuple[str, ...]] = (
    "cursor",
    "hover",
    "al pasar",
    "pasar el",
    "pasa el",
    "clic",
    "click",
    "tooltip",
    "resalt",
)


def is_interaction_claim(sentence: str) -> bool:
    """True si el criterio describe una interacción (pasar el cursor, hacer clic...)."""
    plain = normalize(sentence)
    return any(marker in plain for marker in INTERACTION_MARKERS)


@dataclass(frozen=True, slots=True)
class VisualVerdict:
    """Veredicto de VISUAL_QA sobre un criterio, con quién lo emitió y sobre qué capturas.

    Es evidencia, no autoridad: ``PASS`` satisface el criterio solo porque una ruta con capacidad
    **efectiva** de imágenes lo demostró sobre capturas reales; ``FAIL`` es reparable y ``UNCLEAR``
    deja el criterio sin verificar.
    """

    verdict: str
    observation: str = ""
    provider: str = ""
    model: str = ""
    transport: str = ""
    screenshots: tuple[str, ...] = ()


def extract_claims(
    objective: str, criteria: Iterable[str] = ()
) -> tuple[SemanticClaim, ...]:
    """Extrae las afirmaciones factuales/semánticas de la solicitud.

    Solo se consideran afirmaciones que **hablan de corrección** sobre un sujeto geográfico o
    visual: «el mapa real», «los 18 departamentos representados correctamente», «integrado
    visualmente». Una petición funcional no genera ninguna: no se inventan afirmaciones que
    PUNTO no pueda demostrar.
    """
    claims: list[SemanticClaim] = []
    for text in (objective, *criteria):
        for sentence in _sentences(text):
            plain = normalize(sentence)
            aspecto = any(subject in plain for subject in VISUAL_SUBJECTS) and any(
                marker in plain for marker in VISUAL_MARKERS
            )
            if not aspecto and not any(marker in plain for marker in CORRECTNESS_MARKERS):
                continue
            # El aspecto manda sobre el sujeto: «el mapa se integra visualmente» es una afirmación
            # de apariencia, no de datos, aunque la frase mencione el mapa.
            if aspecto:
                claims.append(
                    SemanticClaim(
                        sentence=sentence[:300],
                        kind=ClaimKind.VISUAL_APPEARANCE.value,
                        evidence_required=(
                            "evidencia visual del resultado renderizado (imagen) o atestación "
                            "humana explícita"
                        ),
                        capability=CAPABILITY_VISION,
                    )
                )
                continue
            if any(subject in plain for subject in GEO_SUBJECTS):
                claims.append(
                    SemanticClaim(
                        sentence=sentence[:300],
                        kind=ClaimKind.CARTOGRAPHIC_CORRECTNESS.value,
                        evidence_required=(
                            "dataset administrativo real de Honduras con los 18 departamentos, "
                            "mapeado a la taxonomía del proyecto y usado por la interfaz"
                        ),
                    )
                )
            if len(claims) >= MAX_REFERENCES:
                return tuple(claims)
    return tuple(claims)


def verify_claims(
    claims: Sequence[SemanticClaim],
    *,
    datasets: Sequence[Any] = (),
    rendered: tuple[bool, str] = (False, "no se comprobó el uso del dataset"),
    visual: VisualCapability | None = None,
    attestation: str = "",
    visual_verdicts: Mapping[str, VisualVerdict] | None = None,
) -> tuple[ClaimRecord, ...]:
    """Mide cada afirmación con la evidencia disponible, sin inventar PASS.

    Args:
        claims: Afirmaciones extraídas de la solicitud.
        datasets: Informes de integridad de los datasets encontrados en el repositorio.
        rendered: Si el código modificado usa el dataset real, con su evidencia.
        visual: Capacidad real del transporte de VISUAL_QA (imágenes).
        attestation: Atestación humana explícita de la apariencia visual, si existe.
        visual_verdicts: Veredictos de VISUAL_QA por frase del criterio, si se evaluaron capturas.

    Returns:
        Un registro por afirmación con ``SATISFIED``, ``UNSATISFIED`` (reparable) o
        ``NOT_VERIFIED`` (no hay evidencia disponible).
    """
    records: list[ClaimRecord] = []
    validos = [item for item in datasets if getattr(item, "valid", False)]
    for claim in claims:
        if claim.kind == ClaimKind.CARTOGRAPHIC_CORRECTNESS.value:
            records.append(_cartographic_record(claim, validos, rendered))
            continue
        verdict = (visual_verdicts or {}).get(claim.sentence)
        records.append(_visual_record(claim, visual, attestation, verdict))
    return tuple(records)


def _cartographic_record(
    claim: SemanticClaim, validos: Sequence[Any], rendered: tuple[bool, str]
) -> ClaimRecord:
    """La corrección cartográfica se demuestra con un dataset válido **usado** por el producto."""
    if not validos:
        return ClaimRecord(
            sentence=claim.sentence,
            kind=claim.kind,
            result="UNSATISFIED",
            evidence=(
                "no hay ningún dataset administrativo válido de Honduras con los 18 departamentos "
                "en el repositorio: la geometría propia no es evidencia cartográfica"
            ),
            evidence_required=claim.evidence_required,
        )
    dataset = validos[0]
    if not rendered[0]:
        return ClaimRecord(
            sentence=claim.sentence,
            kind=claim.kind,
            result="UNSATISFIED",
            evidence=(
                f"existe un dataset válido ({dataset.path}) pero el cambio no lo usa: {rendered[1]}"
            ),
            evidence_required=claim.evidence_required,
        )
    return ClaimRecord(
        sentence=claim.sentence,
        kind=claim.kind,
        result="SATISFIED",
        evidence=(
            f"dataset {dataset.path} válido: {len(dataset.units)} departamentos, fuente "
            f"{dataset.source or 'declarada'}, licencia {dataset.license or 'declarada'}; "
            f"{rendered[1]}"
        ),
        evidence_required=claim.evidence_required,
    )


def _visual_record(
    claim: SemanticClaim,
    visual: VisualCapability | None,
    attestation: str,
    verdict: VisualVerdict | None = None,
) -> ClaimRecord:
    """La apariencia se demuestra con imagen evaluada o con atestación humana, no por suposición.

    Si la ruta no tiene capacidad efectiva de imágenes, el registro lo dice con su causa, la
    capacidad que faltaba y qué corresponde hacer; el resultado sigue siendo ``NOT_VERIFIED`` (nunca
    un PASS inventado).
    """
    capability = visual or VisualCapability(available=False, detail="capacidad no comprobada")
    if attestation.strip():
        return ClaimRecord(
            sentence=claim.sentence,
            kind=claim.kind,
            result="SATISFIED",
            evidence=f"atestación humana explícita: {attestation.strip()[:200]}",
            evidence_required=claim.evidence_required,
            capability=CAPABILITY_VISION,
            capability_available=capability.available,
            capability_detail=capability.detail,
        )
    if verdict is not None and not verdict.provider:
        # Sin revisor: la interacción no se pudo demostrar (elemento no localizado, hover no
        # confirmado...). El motivo es un hecho determinista, no un juicio del modelo.
        return ClaimRecord(
            sentence=claim.sentence,
            kind=claim.kind,
            result="NOT_VERIFIED",
            evidence=f"evidencia visual de interacción no disponible: {verdict.observation}"[:600],
            evidence_required=claim.evidence_required,
            capability=CAPABILITY_VISION,
            capability_available=capability.available,
            capability_detail=capability.detail,
            remedy=(
                "declara la interacción del destino (visual.interactions) con un selector que "
                "localice el elemento, o aporta una atestación humana explícita"
            ),
        )
    if verdict is not None and capability.available:
        via = (
            f"QA visual {verdict.provider}/{verdict.model} ({verdict.transport}) sobre "
            f"{len(verdict.screenshots)} captura(s) [{', '.join(verdict.screenshots)}]"
        )
        if verdict.verdict == "PASS":
            return ClaimRecord(
                sentence=claim.sentence,
                kind=claim.kind,
                result="SATISFIED",
                evidence=f"{via}: {verdict.observation}"[:600],
                evidence_required=claim.evidence_required,
                capability=CAPABILITY_VISION,
                capability_available=True,
                capability_detail=capability.detail,
            )
        if verdict.verdict == "FAIL":
            return ClaimRecord(
                sentence=claim.sentence,
                kind=claim.kind,
                result="UNSATISFIED",
                evidence=f"{via} no lo demuestra: {verdict.observation}"[:600],
                evidence_required=claim.evidence_required,
                capability=CAPABILITY_VISION,
                capability_available=True,
                capability_detail=capability.detail,
            )
        return ClaimRecord(
            sentence=claim.sentence,
            kind=claim.kind,
            result="NOT_VERIFIED",
            evidence=f"{via} no pudo decidir: {verdict.observation}"[:600],
            evidence_required=claim.evidence_required,
            capability=CAPABILITY_VISION,
            capability_available=True,
            capability_detail=capability.detail,
            remedy=(
                "el criterio no es demostrable con una captura estática: aporta evidencia de la "
                "interacción o una atestación humana explícita"
            ),
        )
    if not capability.available:
        return ClaimRecord(
            sentence=claim.sentence,
            kind=claim.kind,
            result="NOT_VERIFIED",
            evidence=(
                "no hay capacidad de QA visual con imágenes en la configuración actual "
                f"({capability.detail}); el criterio queda sin verificar y exige evidencia"
            ),
            evidence_required=claim.evidence_required,
            capability=CAPABILITY_VISION,
            capability_available=False,
            capability_detail=capability.detail,
            remedy=capability.remedy
            or "aporta una atestación humana explícita o habilita una ruta con imágenes",
        )
    return ClaimRecord(
        sentence=claim.sentence,
        kind=claim.kind,
        result="NOT_VERIFIED",
        evidence=(
            "el transporte puede recibir imágenes pero no se aportó ninguna para este resultado"
        ),
        evidence_required=claim.evidence_required,
        capability=CAPABILITY_VISION,
        capability_available=True,
        capability_detail=capability.detail,
        remedy="aporta una imagen renderizada del cambio para que el QA visual la evalúe",
    )


def capability_requirements(
    claims: Sequence[SemanticClaim], *, visual: VisualCapability | None = None
) -> tuple[CapabilityRequirement, ...]:
    """Capacidades que exigen las afirmaciones, evaluadas contra la ruta **efectiva**.

    Se calcula antes de construir: así el ciclo sabe desde el principio qué criterio podrá demostrar
    por sí mismo y cuál necesitará una persona, en vez de descubrirlo después de completar todo el
    trabajo. Una afirmación que no depende de ninguna capacidad del modelo (cartográfica: se
    demuestra con el dataset) aparece con ``capability`` vacía y ``available=True``.
    """
    requisitos: list[CapabilityRequirement] = []
    for claim in claims:
        if not claim.capability:
            requisitos.append(
                CapabilityRequirement(
                    kind=claim.kind,
                    capability="",
                    available=True,
                    criterion=claim.sentence,
                    detail="la evidencia no depende de una capacidad del modelo",
                )
            )
            continue
        capability = visual or VisualCapability(available=False, detail="capacidad no comprobada")
        requisitos.append(
            CapabilityRequirement(
                kind=claim.kind,
                capability=claim.capability,
                available=capability.available,
                criterion=claim.sentence,
                detail=capability.detail,
                remedy=capability.remedy
                or "aporta la evidencia que falta o habilita una ruta que pueda producirla",
            )
        )
    return tuple(requisitos)



def claims_result(records: Sequence[ClaimRecord]) -> str:
    """Resultado global de las afirmaciones: FAILED, EVIDENCE_REQUIRED, SATISFIED o NONE."""
    if not records:
        return "NONE"
    if any(item.unsatisfied for item in records if item.required):
        return "FAILED"
    if any(item.not_verified for item in records if item.required):
        return "EVIDENCE_REQUIRED"
    return "SATISFIED"
