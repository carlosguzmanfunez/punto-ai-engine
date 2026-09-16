"""Barrera determinista contra las replanificaciones de alto impacto (ENGINE-6.3.1, F631-02).

ENGINE-6.3 es replanificación **táctica**. Un fallo técnico admite otra estrategia dentro del mismo
diseño —dividir un nodo, insertar un prerrequisito, reordenar lo pendiente, reemplazar un nodo no
aceptado—, pero **no** admite que el motor cambie, por su cuenta, la arquitectura del proyecto: el
stack, el motor de base de datos, el modelo de autenticación, la plataforma de despliegue, el
proveedor de modelo o las reglas de negocio.

El hallazgo F631-02 demostró que la frontera anterior no lo garantizaba: ``pure_technical`` se
derivaba solo de riesgo, autoridad, alcance y rutas protegidas, así que una propuesta cuyo objetivo
era «Replace PostgreSQL with MongoDB and redesign authentication architecture», declarada ``LOW`` y
``LEVEL_0_AUTONOMOUS`` y escribiendo en el mismo archivo, se adoptaba como si fuera táctica. La
lección no es añadir otro campo a la declaración del Planner —el Planner tiene autoridad **cero**
sobre el juicio—, sino que **el motor derive la clase de cambio** con sus propios medios.

Este módulo es esa derivación, y tiene tres propiedades deliberadas:

1. **Es del motor.** No lee ninguna declaración de impacto del modelo: la clase sale de la acción
   declarada del proyecto (tabla del catálogo, que es del motor) y de los campos acotados de la
   propuesta (título, objetivo, criterios y motivo de la operación), interpretados por patrones
   estables de este módulo. El modelo no clasifica: es clasificado.
2. **Es conservadora.** Ante la duda, alto impacto: un falso positivo cuesta una aprobación humana,
   y un falso negativo cuesta una reescritura de arquitectura adoptada en autonomía. La asimetría es
   el motivo de que los patrones miren cambios —dos motores de datos distintos, un verbo de cambio
   junto a una frontera— y no menciones sueltas.
3. **Es determinista y auditable.** La misma propuesta produce siempre la misma clase, el mismo
   motivo y las mismas marcas; la clase viaja a la política y al Human Gate, de modo que una persona
   aprueba sabiendo **qué** clase de cambio está aprobando.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from punto.schemas.replan import MAX_REPLAN_SHORT_CHARS
from punto.workflow.policy import action_impact

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from punto.schemas.replan import ProjectContract, ProjectReplanProposal, ReplanNodeSpec

#: Máximo de marcas y de campos inspeccionados que el veredicto enumera.
#:
#: El tope protege el informe y la auditoría: una propuesta hostil (ocho nodos, treinta y dos
#: criterios, textos de dos mil caracteres) no puede hacer crecer el detalle sin límite. No afecta
#: al veredicto: la clase ya está decidida con la primera marca.
MAX_CHANGE_MATCHES: Final[int] = 12

#: Caracteres máximos de cada marca citada en el detalle.
CHANGE_MATCH_CHARS: Final[int] = 80


class ReplanChangeClass(StrEnum):
    """Clase de cambio que el motor deriva de una propuesta.

    ``TACTICAL_ALLOWED`` es la única que se puede adoptar en autonomía; cualquier otra exige una
    persona, porque cambia algo que el contrato del proyecto no autorizó a reescribir.
    """

    TACTICAL_ALLOWED = "TACTICAL_ALLOWED"
    ARCHITECTURE_CHANGE = "ARCHITECTURE_CHANGE"
    AUTH_MODEL_CHANGE = "AUTH_MODEL_CHANGE"
    DATASTORE_CHANGE = "DATASTORE_CHANGE"
    DEPLOYMENT_CHANGE = "DEPLOYMENT_CHANGE"
    BUSINESS_RULE_CHANGE = "BUSINESS_RULE_CHANGE"
    PROVIDER_CHANGE = "PROVIDER_CHANGE"
    UNKNOWN_HIGH_IMPACT = "UNKNOWN_HIGH_IMPACT"

    @property
    def is_tactical(self) -> bool:
        """``True`` solo para la clase que el motor puede adoptar sin persona."""
        return self is ReplanChangeClass.TACTICAL_ALLOWED

    @property
    def requires_human(self) -> bool:
        """``True`` si la clase está por encima de lo táctico y exige autorización humana."""
        return not self.is_tactical


@dataclass(frozen=True, slots=True)
class ReplanChangeClassification:
    """Veredicto determinista: la clase de cambio, sus marcas y el motivo legible."""

    change_class: ReplanChangeClass
    detail: str
    matches: tuple[str, ...] = ()

    @property
    def is_tactical(self) -> bool:
        """``True`` si el cambio es táctico y puede seguir el camino autónomo."""
        return self.change_class.is_tactical

    @property
    def requires_human(self) -> bool:
        """``True`` si el cambio exige una persona antes de adoptarse."""
        return self.change_class.requires_human

    @property
    def reason_code(self) -> str:
        """Código estable del veredicto, para la auditoría y el vínculo del Human Gate."""
        return f"REPLAN_CHANGE_{self.change_class.value}"


#: Verbos de cambio: lo que convierte una mención en una **sustitución**.
_CHANGE_VERB: Final[str] = (
    r"(?:replace|replacing|replacement|swap|swapping|migrat\w*|move|moving|port|porting|rewrite|"
    r"rewriting|redesign\w*|re-?architect\w*|change|changing|switch\w*|convert\w*|drop|rework\w*|"
    r"sustitu\w*|reempla\w*|cambia\w*|migra\w*|mover|reescrib\w*|redise\w*|rehacer|reconver\w*|"
    r"rehacer)"
)

#: Motores de datos conocidos: dos distintos en el mismo campo es un cambio de motor.
_DATASTORE_ENGINES: Final[tuple[str, ...]] = (
    "postgres",
    "postgresql",
    "mysql",
    "mariadb",
    "sqlite",
    "oracle",
    "sql server",
    "mongodb",
    "mongo",
    "dynamodb",
    "cassandra",
    "redis",
    "neo4j",
    "elasticsearch",
    "nosql",
)

#: Proveedores de modelo conocidos: dos distintos, o uno con verbo de cambio, es un cambio de
#: proveedor (lo que la fase prohíbe expresamente).
_PROVIDERS: Final[tuple[str, ...]] = (
    "deepseek",
    "anthropic",
    "claude",
    "openai",
    "gpt",
    "gemini",
    "mistral",
    "llama",
    "bedrock",
    "vertex",
    "azure openai",
)

#: Patrones por clase, en el orden en que se evalúan (el más específico primero).
#:
#: Cada patrón se aplica sobre el texto **normalizado** (minúsculas y sin acentos) de un campo
#: acotado. El orden fija la clase que gana cuando varias coinciden: «Replace deployment
#: architecture» es un cambio de despliegue antes que uno de arquitectura, y así el informe dice lo
#: más específico que el motor pudo demostrar.
_HIGH_IMPACT_PATTERNS: Final[tuple[tuple[ReplanChangeClass, tuple[str, ...]], ...]] = (
    (
        ReplanChangeClass.DATASTORE_CHANGE,
        (
            r"\b(?:datastore|data store|database engine|database architecture|persistence strategy|"
            r"motor de base de datos|arquitectura de base de datos|estrategia de persistencia|"
            r"almacen de datos)\b",
            r"\b(?:sql|relacional|relational)\b[^.]{0,40}\b(?:to|a|por|hacia)\b[^.]{0,40}"
            r"\b(?:nosql|no sql|documental|document)\b",
            rf"{_CHANGE_VERB}\b[^.]{{0,60}}\b(?:database|base de datos|datastore|persistencia)\b",
        ),
    ),
    (
        ReplanChangeClass.AUTH_MODEL_CHANGE,
        (
            r"\b(?:auth|authentication|authorization|autenticacion|autorizacion)\b[^.]{0,40}"
            r"\b(?:architecture|architectural|model|scheme|strategy|provider|flow|tenant|"
            r"arquitectura|modelo|esquema|estrategia|proveedor|flujo)\b",
            r"\b(?:architecture|architectural|model|scheme|strategy|arquitectura|modelo|esquema|"
            r"estrategia)\b[^.]{0,40}"
            r"\b(?:auth|authentication|authorization|autenticacion|autorizacion|oauth|sso|jwt|saml)\b",
            r"\b(?:oauth|sso|saml|jwt|identity provider|proveedor de identidad)\b",
            rf"{_CHANGE_VERB}\b[^.]{{0,40}}"
            r"\b(?:auth|authentication|authorization|autenticacion|autorizacion|login|sesion)\b",
        ),
    ),
    (
        ReplanChangeClass.DEPLOYMENT_CHANGE,
        (
            r"\b(?:deploy\w*|despliegue|desplegar|kubernetes|k8s|helm|terraform|docker[- ]compose|"
            r"serverless|infrastructure as code|infraestructura como codigo)\b[^.]{0,40}"
            r"\b(?:architecture|architectural|platform|strategy|target|topology|arquitectura|"
            r"plataforma|estrategia|destino|topologia)\b",
            r"\b(?:architecture|architectural|platform|strategy|arquitectura|plataforma|estrategia)"
            r"\b[^.]{0,40}\b(?:deploy\w*|despliegue|kubernetes|k8s|terraform|serverless)\b",
            rf"{_CHANGE_VERB}\b[^.]{{0,40}}\b(?:deploy\w*|despliegue|hosting|alojamiento)\b",
        ),
    ),
    (
        ReplanChangeClass.PROVIDER_CHANGE,
        (
            r"\b(?:provider|proveedor|model provider|proveedor de modelo|proveedor de ia)\b"
            r"[^.]{0,40}\b(?:replace\w*|swap\w*|migrat\w*|change|switch\w*|sustitu\w*|reempla\w*|"
            r"cambia\w*)\b",
            r"\b(?:replace\w*|swap\w*|migrat\w*|change|switch\w*|sustitu\w*|reempla\w*|cambia\w*)"
            r"\b[^.]{0,40}\b(?:provider|proveedor|model provider|proveedor de modelo)\b",
        ),
    ),
    (
        ReplanChangeClass.BUSINESS_RULE_CHANGE,
        (
            r"\b(?:business rules?|reglas? de negocio|business model|modelo de negocio|"
            r"business logic|logica de negocio|domain model|modelo de dominio|pricing|"
            r"billing|facturacion|monetization|monetizacion|contrato comercial)\b",
        ),
    ),
    (
        ReplanChangeClass.ARCHITECTURE_CHANGE,
        (
            r"\b(?:architecture|architectural|arquitectura|arquitectonico)\b",
            r"\b(?:tech stack|technology stack|stack tecnologico|monolith|monolito|microservice\w*|"
            r"microservicio\w*|re-?architect\w*)\b",
            rf"{_CHANGE_VERB}\b[^.]{{0,40}}\b(?:stack|framework|monolito|monolith)\b",
        ),
    ),
    (
        ReplanChangeClass.UNKNOWN_HIGH_IMPACT,
        (
            r"\b(?:security architecture|arquitectura de seguridad|persistence|persistencia|"
            r"storage strategy|estrategia de almacenamiento|data model|modelo de datos|"
            r"schema migration|migracion de esquema|topology|topologia|platform migration|"
            r"migracion de plataforma)\b",
            rf"{_CHANGE_VERB}\b[^.]{{0,40}}\b(?:infrastructure|infraestructura|persistence|"
            r"persistencia|security|seguridad)\b",
        ),
    ),
)


#: Prioridad determinista de las clases: el índice del patrón decide cuál manda cuando varias
#: coinciden. Lo más específico primero (un cambio de motor de datos es un cambio de datos antes que
#: de arquitectura), y lo indeterminado al final.
_CLASS_PRIORITY: Final[dict[ReplanChangeClass, int]] = {
    change_class: index for index, (change_class, _) in enumerate(_HIGH_IMPACT_PATTERNS)
}


def _normalize(text: str) -> str:
    """Texto comparable: minúsculas, sin acentos y con espacios colapsados.

    La normalización existe para que los patrones sean estables en español y en inglés sin duplicar
    cada variante acentuada, y para que un texto con saltos de línea no rompa una ventana de
    proximidad.
    """
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", plain).strip()


def _fields(proposal: ProjectReplanProposal) -> tuple[tuple[str, str], ...]:
    """Campos acotados que el motor inspecciona, con su nombre para el motivo.

    Es la superficie entera de la propuesta que puede describir un cambio: el título, el objetivo
    y los criterios de cada nodo nuevo, y el motivo de cada operación, más el resultado esperado. No
    se inspecciona ningún texto libre adicional —no existe en el contrato de la propuesta— ni nada
    que el modelo pueda hacer crecer por encima de las cotas del esquema.
    """
    items: list[tuple[str, str]] = []
    for operation in proposal.operations:
        if operation.reason:
            items.append((f"operacion[{operation.index}].motivo", operation.reason))
        for spec in operation.nodes:
            items.extend(_spec_fields(spec))
    if proposal.expected_outcome:
        items.append(("resultado_esperado", proposal.expected_outcome))
    return tuple(items)


def _spec_fields(spec: ReplanNodeSpec) -> tuple[tuple[str, str], ...]:
    """Campos de un nodo propuesto: etiqueta, título, objetivo y criterios."""
    items: list[tuple[str, str]] = [
        (f"nodo[{spec.label}].titulo", spec.title),
        (f"nodo[{spec.label}].objetivo", spec.objective),
    ]
    items.extend(
        (f"nodo[{spec.label}].criterio[{index}]", text)
        for index, text in enumerate(spec.acceptance_criteria)
    )
    return tuple(items)


def _distinct_mentions(text: str, needles: Sequence[str]) -> tuple[str, ...]:
    """Marcas distintas de la lista que aparecen en el texto, en el orden de la lista."""
    return tuple(needle for needle in needles if re.search(rf"\b{re.escape(needle)}\b", text))


def _engine_swap(text: str, needles: Sequence[str]) -> tuple[str, ...]:
    """Marcas de un cambio de motor: dos distintos en el campo, o uno con verbo de cambio.

    La distinción es deliberada: mencionar un motor no es cambiarlo —una tarea táctica puede tocar
    una consulta—, pero proponer **dos** motores en el mismo campo, o uno junto a un verbo de
    sustitución, es un cambio de motor.
    """
    found = _distinct_mentions(text, needles)
    if len(found) >= 2:
        return found
    if found and re.search(rf"{_CHANGE_VERB}\b", text):
        return found
    return ()


def _semantic_matches(text: str) -> tuple[ReplanChangeClass, tuple[str, ...]] | None:
    """Primera clase de alto impacto que el texto demuestra, con sus marcas."""
    for change_class, patterns in _HIGH_IMPACT_PATTERNS:
        for pattern in patterns:
            match = re.search(pattern, text)
            if match is not None:
                return change_class, (match.group(0)[:CHANGE_MATCH_CHARS],)
    return None


def classify_replan_change(
    proposal: ProjectReplanProposal,
    *,
    contract: ProjectContract,
    action: str = "",
) -> ReplanChangeClassification:
    """Deriva, **en el motor**, la clase de cambio de una propuesta de replanificación.

    El orden de las evidencias es el de la fuerza:

    1. **Evidencia estructurada del motor**: el impacto de la acción declarada del proyecto, que
       sale de la tabla del catálogo —no de la propuesta—; una acción con impacto en producción,
       legal o de negocio no es un cambio táctico, venga como venga el texto;
    2. **Evidencia estructurada de la propuesta**: dos motores de datos distintos, o dos proveedores
       distintos, propuestos en el mismo campo;
    3. **Guardia semántica conservadora** sobre los campos acotados de la propuesta (título,
       objetivo, criterios, motivo de operación y resultado esperado), con los patrones estables de
       este módulo.

    Un texto que no demuestra ninguna de las clases de alto impacto se declara ``TACTICAL_ALLOWED``:
    la ausencia de marcas no es una autorización —el guard, el contrato y la política siguen
    juzgando—, sino la constatación de que el motor no encontró un cambio de diseño en lo que se le
    propone.

    Args:
        proposal: Propuesta tipada del Planner, con sus textos acotados.
        contract: Contrato inmutable del proyecto, para el motivo legible.
        action: Acción canónica del proyecto, si se conoce; su impacto se consulta al catálogo.

    Returns:
        La clase de cambio con su motivo y las marcas que la demuestran.
    """
    impact = action_impact(action) if action else None
    if impact is not None and (impact.production or impact.legal or impact.business):
        return ReplanChangeClassification(
            ReplanChangeClass.UNKNOWN_HIGH_IMPACT,
            (
                f"la acción del proyecto {action!r} declara impacto en producción, legal o de "
                "negocio: no es un cambio táctico, y el motor no lo adopta en autonomía"
            ),
            (f"accion={action}",),
        )
    matches: list[str] = []
    found: dict[ReplanChangeClass, list[str]] = {}
    for name, text in _fields(proposal):
        if not text.strip():
            continue
        normalized = _normalize(text)
        swap = _engine_swap(normalized, _DATASTORE_ENGINES)
        if len(swap) >= 2:
            candidate, marks = ReplanChangeClass.DATASTORE_CHANGE, swap
        else:
            providers = _engine_swap(normalized, _PROVIDERS)
            if len(providers) >= 2:
                candidate, marks = ReplanChangeClass.PROVIDER_CHANGE, providers
            else:
                semantic = _semantic_matches(normalized)
                if semantic is None:
                    continue
                candidate, marks = semantic
        found.setdefault(candidate, []).extend(f"{name}: {mark}" for mark in marks)
        if sum(len(items) for items in found.values()) >= MAX_CHANGE_MATCHES:
            break
    if not found:
        return ReplanChangeClassification(
            ReplanChangeClass.TACTICAL_ALLOWED,
            (
                f"la propuesta no cambia el diseño del proyecto {contract.original_goal[:80]!r}: "
                "ninguna marca de cambio de arquitectura, datos, autenticación, despliegue, "
                "proveedor o reglas de negocio"
            ),
        )
    # La clase que manda es la más específica según el orden fijo de patrones, **no** la que
    # aparezca en el primer campo: así el veredicto no depende de si el modelo escribió la
    # sustitución en el título o en el objetivo.
    ordered = sorted(found, key=lambda item: _CLASS_PRIORITY[item])
    found_class = ordered[0]
    for candidate in ordered:
        matches.extend(found[candidate])
    bounded = tuple(matches[:MAX_CHANGE_MATCHES])
    return ReplanChangeClassification(
        found_class,
        (
            f"la propuesta declara un cambio de {found_class.value} fuera de lo táctico "
            f"({len(bounded)} marca(s)): el contrato del proyecto no autoriza reescribir el diseño "
            "en autonomía"
        ),
        bounded,
    )


def change_classes_of(proposal: ProjectReplanProposal) -> tuple[str, ...]:
    """Clases de alto impacto que la propuesta demuestra, sin decidir cuál gana.

    Es la vista para la auditoría: el veredicto dice la clase que manda, y esto dice todo lo que el
    motor vio. Se calcula con la misma guardia semántica, así que no puede discrepar del veredicto.
    """
    found: list[str] = []
    for _, text in _fields(proposal):
        if not text.strip():
            continue
        normalized = _normalize(text)
        if len(_engine_swap(normalized, _DATASTORE_ENGINES)) >= 2:
            found.append(ReplanChangeClass.DATASTORE_CHANGE.value)
            continue
        if len(_engine_swap(normalized, _PROVIDERS)) >= 2:
            found.append(ReplanChangeClass.PROVIDER_CHANGE.value)
            continue
        semantic = _semantic_matches(normalized)
        if semantic is not None:
            found.append(semantic[0].value)
    return tuple(dict.fromkeys(found))


def high_impact_labels(classes: Iterable[str]) -> str:
    """Texto legible y acotado de una lista de clases, para el detalle de la decisión."""
    joined = ", ".join(sorted(dict.fromkeys(classes)))
    return joined[:MAX_REPLAN_SHORT_CHARS]


__all__ = [
    "CHANGE_MATCH_CHARS",
    "MAX_CHANGE_MATCHES",
    "ReplanChangeClass",
    "ReplanChangeClassification",
    "change_classes_of",
    "classify_replan_change",
    "high_impact_labels",
]
