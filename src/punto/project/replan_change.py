"""Escalado semántico de un solo sentido en la replanificación (6.3.2 → 6.3.R1, F632-01).

ENGINE-6.3 es replanificación **táctica**: un fallo técnico admite otra estrategia de implementación
dentro del mismo diseño, pero **no** admite que el motor cambie, por su cuenta, la arquitectura del
proyecto. La primera versión de esta barrera (ENGINE-6.3.1) razonaba al revés: buscaba marcas
conocidas de alto impacto y, cuando no encontraba ninguna, **autorizaba**. Ese razonamiento es
inválido —«no lo reconozco como alto impacto» no es «he demostrado que es táctico»— y la auditoría
independiente lo reprodujo con tecnologías que las listas no contenían:

    «Replace the current relational engine with CockroachDB»
    «Move the service from Vercel to Fly.io»
    «Replace the current identity service with Clerk»

Las tres se adoptaban en autonomía porque los nombres no estaban en ninguna lista. La lección no es
añadir tres marcas más —el siguiente nombre desconocido volvería a pasar— sino invertir la carga de
la prueba. Desde **ENGINE-6.3.R1** esa carga ya no la lleva un texto: el clasificador **solo puede
escalar** —manda el caso a una persona o no añade sospecha, nunca concede— y la autonomía la
demuestra la contención estructural (T1-T7) contra el envelope autorizado del contrato.

Este módulo implementa el escalado, y tiene cuatro propiedades deliberadas:

1. **Derivada del motor.** El veredicto sale de hechos que PUNTO comprueba: la estructura de la
   propuesta contra el contrato congelado (alcance, criterios, riesgo, autoridad, rutas protegidas)
   y los hechos de arquitectura del **baseline** —estilo, almacenes, integraciones, fronteras de
   seguridad, topología de despliegue, tecnología—. Una estructura que no cabe en el contrato deja
   el caso en ``UNKNOWN_OR_AMBIGUOUS``, que exige una persona; una estructura que sí cabe y ningún
   indicio de diseño deja el caso en ``NO_SEMANTIC_SUSPICION``, que **no** concede nada por sí.
2. **Escalado de un solo sentido.** Una sospecha semántica —``HIGH_IMPACT`` (cambio de datos,
   identidad, despliegue, proveedor, arquitectura o reglas de negocio) o ``UNKNOWN_OR_AMBIGUOUS``—
   manda el caso a una persona. Nunca ocurre lo contrario: **ninguna clase de este módulo concede
   autonomía**, y ``NO_SEMANTIC_SUSPICION`` solo significa «el texto no aporta ninguna razón para
   escalar». La autonomía la demuestra la contención estructural (T1-T7) en
   :mod:`punto.project.containment`.
3. **Sin dependencia de listas de productos.** Las marcas conocidas existen como **aceleradores**
   que
   afinan el nombre de la clase, no como frontera: una tecnología nueva se detecta por su **forma**
   (nombre propio, dígitos, dominio, jerga de sustitución) y por el **contrato del vocabulario
   genérico** —motor, almacén, servicio de identidad, alojamiento, plataforma, proveedor, marco de
   trabajo—, de modo que «ExampleDB9000» y «CockroachDB» caen por el mismo sitio.
4. **Conservadora y auditable.** Ante la duda, persona. El veredicto viaja con las marcas que lo
   demuestran, las dimensiones tocadas, las tecnologías no reconocidas y la **prueba** de tacticidad
   (los hechos verificados), de modo que un revisor pueda discutir el veredicto y no solo sufrirlo.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from punto.workflow.policy import action_impact

if TYPE_CHECKING:
    from collections.abc import Sequence

    from punto.schemas.replan import ProjectContract, ProjectReplanProposal, ReplanNodeSpec

#: Máximo de marcas, dimensiones y tecnologías no reconocidas que el veredicto enumera.
MAX_CHANGE_MATCHES: Final[int] = 12

#: Caracteres máximos de cada marca citada en el detalle.
CHANGE_MATCH_CHARS: Final[int] = 80

#: Longitud mínima de una palabra para considerarla señal léxica.
MIN_TOKEN_CHARS: Final[int] = 3


class ReplanChangeClass(StrEnum):
    """Sospecha semántica que el motor deriva de una propuesta (ENGINE-6.3.R1).

    Desde ENGINE-6.3.R1 este vocabulario **no concede autonomía**: la autonomía la demuestra la
    contención estructural (:mod:`punto.project.containment`). Lo que este módulo aporta es
    **escalado de un solo sentido**: si el texto de la propuesta suena a cambio de diseño, se manda
    a una persona; si no suena a nada, no autoriza nada — simplemente no añade una sospecha.

    ``NO_SEMANTIC_SUSPICION`` no significa «táctico demostrado»: significa «el texto no aporta
    ninguna razón para escalar». La demostración vive en T1-T7.
    """

    NO_SEMANTIC_SUSPICION = "NO_SEMANTIC_SUSPICION"
    DATASTORE_CHANGE = "DATASTORE_CHANGE"
    AUTH_MODEL_CHANGE = "AUTH_MODEL_CHANGE"
    DEPLOYMENT_CHANGE = "DEPLOYMENT_CHANGE"
    PROVIDER_CHANGE = "PROVIDER_CHANGE"
    BUSINESS_RULE_CHANGE = "BUSINESS_RULE_CHANGE"
    ARCHITECTURE_CHANGE = "ARCHITECTURE_CHANGE"
    UNKNOWN_HIGH_IMPACT = "UNKNOWN_HIGH_IMPACT"
    UNKNOWN_OR_AMBIGUOUS = "UNKNOWN_OR_AMBIGUOUS"

    @property
    def escalates(self) -> bool:
        """``True`` si la sospecha obliga a que decida una persona.

        Es la **única** autoridad de este módulo, y es de un solo sentido: puede restringir, nunca
        conceder. Un falso positivo cuesta una revisión humana; un falso negativo no concede nada,
        porque sin contención estructural demostrada no hay adopción autónoma.
        """
        return self is not ReplanChangeClass.NO_SEMANTIC_SUSPICION

    @property
    def requires_human(self) -> bool:
        """Alias explícito de :attr:`escalates`, para el código que ya preguntaba por él."""
        return self.escalates

    @property
    def is_high_impact(self) -> bool:
        """``True`` si la clase nombra un cambio de diseño concreto."""
        return self in _HIGH_IMPACT_CLASSES

    @property
    def is_ambiguous(self) -> bool:
        """``True`` si el motor no pudo demostrar ni el cambio ni la ausencia de sospecha."""
        return self is ReplanChangeClass.UNKNOWN_OR_AMBIGUOUS


#: Clases que nombran un cambio de diseño concreto (frente a la ambigüedad).
_HIGH_IMPACT_CLASSES: Final[frozenset[ReplanChangeClass]] = frozenset(
    {
        ReplanChangeClass.DATASTORE_CHANGE,
        ReplanChangeClass.AUTH_MODEL_CHANGE,
        ReplanChangeClass.DEPLOYMENT_CHANGE,
        ReplanChangeClass.PROVIDER_CHANGE,
        ReplanChangeClass.BUSINESS_RULE_CHANGE,
        ReplanChangeClass.ARCHITECTURE_CHANGE,
        ReplanChangeClass.UNKNOWN_HIGH_IMPACT,
    }
)


@dataclass(frozen=True, slots=True)
class ReplanChangeClassification:
    """Sospecha determinista: clase, evidencia de auditoría y hechos que la sostienen.

    ``proof`` es la lista de hechos que el motor **verificó** mientras miraba el texto; desde
    ENGINE-6.3.R1 es evidencia de auditoría, no una autorización: la autonomía sale de la contención
    estructural (T1-T7), no de esta lista.
    """

    change_class: ReplanChangeClass
    detail: str
    matches: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    unknown_tokens: tuple[str, ...] = ()
    proof: tuple[str, ...] = ()

    @property
    def escalates(self) -> bool:
        """``True`` si la sospecha semántica exige que decida una persona."""
        return self.change_class.escalates

    @property
    def requires_human(self) -> bool:
        """Alias de :attr:`escalates`: la sospecha nunca concede, solo restringe."""
        return self.escalates

    @property
    def reason_code(self) -> str:
        """Código estable del veredicto, para la auditoría y el vínculo del Human Gate."""
        return f"REPLAN_CHANGE_{self.change_class.value}"


#: Patrones genéricos de **intención de sustitución**. No son marcas de producto: describen que la
#: propuesta propone cambiar algo por otra cosa, y por eso solo son peligrosos cuando el objeto del
#: cambio pertenece a una dimensión de arquitectura.
_CHANGE_INTENT: Final[str] = (
    r"\b(?:replace|replacing|replacement|swap\w*|exchange|migrat\w*|move|moving|"
    r"port|ports|ported|porting|"
    r"switch\w*|convert\w*|transition\w*|adopt\w*|introduc\w*|instead of|in place of|"
    r"rather than|change|changes|changed|changing|modif\w*|updat\w*|revis\w*|"
    r"alter|alters|altered|altering|"
    r"rework\w*|redefin\w*|restructur\w*|reorganiz\w*|re-?architect\w*|rewrite|rewriting|"
    r"reimplement\w*|redesign\w*|upgrade to|drop|"
    r"sustitu\w*|reempla\w*|cambia\w*|migra\w*|mover|mueve|adopta\w*|introduc\w*|"
    r"en lugar de|en vez de|reescrib\w*|redise\w*|rehacer|anadir|añadir|incorpora\w*)\b"
)

#: Conceptos genéricos por dimensión. Es vocabulario de **concepto**, no de producto: «motor»,
#: «almacén», «identidad», «alojamiento»… Un nombre de marca nuevo no necesita estar aquí para que
#: la frontera funcione; lo que hace que la frontera funcione es que la sustitución de un concepto
#: de arquitectura exige prueba de compatibilidad con el baseline.
_DIMENSIONS: Final[tuple[tuple[str, ReplanChangeClass, tuple[str, ...]], ...]] = (
    (
        "datastore",
        ReplanChangeClass.DATASTORE_CHANGE,
        (
            r"\b(?:database|databases|datastore|data store|db engine|database engine|"
            r"relational engine|storage engine|storage backend|persistence backend|"
            r"persistence layer|persistence|sql|nosql|no sql|schema|records|tables|"
            r"almacen de datos|base de datos|motor relacional|motor de datos|"
            r"capa de persistencia|persistencia|esquema|registros|tablas|cache)\b",
        ),
    ),
    (
        "identity",
        ReplanChangeClass.AUTH_MODEL_CHANGE,
        (
            r"\b(?:auth|authentication|authorization|identity|identity service|identity system|"
            r"identity provider|login|session|sessions|oauth|oidc|sso|jwt|saml|rbac|permissions|"
            r"autenticacion|autorizacion|identidad|servicio de identidad|proveedor de identidad|"
            r"sesion|sesiones|permisos)\b",
        ),
    ),
    (
        "deployment",
        ReplanChangeClass.DEPLOYMENT_CHANGE,
        (
            r"\b(?:deploy|deploys|deployment|hosting|host|platform|platforms|runtime platform|"
            r"infrastructure|cloud|kubernetes|k8s|containers|orchestration|serverless|region|"
            r"topology|ci/cd|server|servers|cluster|despliegue|desplegar|alojamiento|plataforma|"
            r"infraestructura|nube|contenedores|orquestacion|servidor|servidores|topologia)\b",
        ),
    ),
    (
        "integration",
        ReplanChangeClass.PROVIDER_CHANGE,
        (
            r"\b(?:integration|integrations|external service|provider|providers|vendor|"
            r"third party|third-party|gateway|api gateway|sdk|broker|queue|messaging|webhook|"
            r"message bus|model provider|integracion|integraciones|servicio externo|proveedor|"
            r"proveedores|terceros|pasarela|cola|mensajeria)\b",
        ),
    ),
    (
        "architecture",
        ReplanChangeClass.ARCHITECTURE_CHANGE,
        (
            r"\b(?:architecture|architectural|component|components|module|modules|service|"
            r"services|layer|layers|boundary|boundaries|monolith|microservice|microservices|"
            r"stack|framework|frameworks|pattern|patterns|arquitectura|arquitectonico|"
            r"componente|componentes|modulo|modulos|servicio|servicios|capa|capas|frontera|"
            r"monolito|microservicio|microservicios|patron|patrones)\b",
        ),
    ),
    (
        "business",
        ReplanChangeClass.BUSINESS_RULE_CHANGE,
        (
            r"\b(?:business rules?|business rule|business model|business logic|domain model|"
            r"pricing|billing|invoicing|monetization|reglas? de negocio|modelo de negocio|"
            r"logica de negocio|modelo de dominio|facturacion|monetizacion|tarifas|precios)\b",
        ),
    ),
)

#: Aceleradores: marcas conocidas que **afinan el nombre** de la clase. Nunca son la frontera: una
#: marca que no esté aquí cae igual —por dimensión o por forma del nombre— y una que esté aquí no
#: autoriza nada por sí sola.
_KNOWN_BRANDS: Final[tuple[tuple[str, str], ...]] = (
    ("postgres", "datastore"),
    ("postgresql", "datastore"),
    ("mysql", "datastore"),
    ("mariadb", "datastore"),
    ("sqlite", "datastore"),
    ("oracle", "datastore"),
    ("sql server", "datastore"),
    ("mongodb", "datastore"),
    ("mongo", "datastore"),
    ("dynamodb", "datastore"),
    ("cassandra", "datastore"),
    ("redis", "datastore"),
    ("neo4j", "datastore"),
    ("elasticsearch", "datastore"),
    ("cockroachdb", "datastore"),
    ("surrealdb", "datastore"),
    ("planetscale", "datastore"),
    ("supabase", "datastore"),
    ("firebase", "datastore"),
    ("turso", "datastore"),
    ("neon", "datastore"),
    ("auth0", "identity"),
    ("okta", "identity"),
    ("keycloak", "identity"),
    ("cognito", "identity"),
    ("clerk", "identity"),
    ("vercel", "deployment"),
    ("netlify", "deployment"),
    ("heroku", "deployment"),
    ("fly.io", "deployment"),
    ("railway", "deployment"),
    ("render", "deployment"),
    ("deepseek", "integration"),
    ("anthropic", "integration"),
    ("claude", "integration"),
    ("openai", "integration"),
    ("gemini", "integration"),
    ("mistral", "integration"),
    ("bedrock", "integration"),
    ("vertex", "integration"),
)

#: Palabras que **no** son señal de tecnología aunque se escriban con mayúscula inicial o contengan
#: dígitos: es un filtro de ruido para que el detector de nombres propios no marque cada sustantivo
#: en inglés del encargo. No es la frontera —lo que no se reconoce cae igual en ambiguo si aparece
#: junto a una dimensión o a una intención de sustitución—, sino el cepillo que evita falsos
#: positivos groseros sobre texto corriente.
_GENERIC_WORDS: Final[frozenset[str]] = frozenset(
    {
        "add",
        "alternative",
        "anadir",
        "approach",
        "archivo",
        "alcance",
        "build",
        "change",
        "check",
        "clase",
        "code",
        "config",
        "conservar",
        "contract",
        "contrato",
        "criterio",
        "criteria",
        "criterion",
        "current",
        "deterministic",
        "documentation",
        "ejecutar",
        "es",
        "este",
        "existing",
        "fichero",
        "file",
        "files",
        "fix",
        "funcion",
        "function",
        "helper",
        "implementation",
        "issue",
        "javascript",
        "mantener",
        "nodo",
        "nodos",
        "node",
        "nodes",
        "objective",
        "objetivo",
        "paso",
        "pasos",
        "plan",
        "preparar",
        "prerrequisito",
        "proceso",
        "project",
        "proyecto",
        "prueba",
        "python",
        "readme",
        "reintentar",
        "reparacion",
        "resultado",
        "retry",
        "revision",
        "riesgo",
        "risk",
        "run",
        "same",
        "scope",
        "software",
        "hardware",
        "step",
        "steps",
        "strategy",
        "estrategia",
        "tarea",
        "tareas",
        "task",
        "tasks",
        "test",
        "tests",
        "the",
        "utility",
        "validacion",
        "verificacion",
        "verificar",
        "verify",
        "workflow",
    }
)

#: Sufijos y formas que delatan un nombre de tecnología sin conocer la marca.
#:
#: La **forma** es la parte que no depende de ninguna lista: un nombre con mayúscula interna
#: (``CockroachDB``, ``SurrealDB``), con dígitos (``ExampleDB9000``), con dominio (``Fly.io``) o con
#: jerga tecnológica (``…db``, ``…sql``, ``…cloud``) es un candidato a tecnología aunque nadie lo
#: haya catalogado. Una palabra capitalizada suelta (``Clerk``, ``Vercel``) también lo es: es la
#: forma más común de un nombre de producto, y detectarla es lo que evita que «Use Clerk» pase por
#: táctico solo porque no aparece ninguna palabra de dimensión.
_PRODUCT_FORMS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"[a-z][A-Z]"),
    re.compile(r"\d"),
    re.compile(r"\.(?:io|ai|com|dev|cloud|co|net|org|sh|xyz)\b"),
    re.compile(r"(?:db|sql|cloud|hub|stack|api|sdk|ops|kit|box|flow|ware)$"),
    re.compile(r"^[A-Z][a-z]+$"),
)

#: Identificadores del proyecto (``AC-1``, ``R-2``, ``N4``): no son tecnologías, son nombres de
#: criterio o de paso.
_IDENTIFIER_FORM: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{1,4}-?\d+$")

#: Extensiones de archivo y marcas de ruta que **no** son nombres de tecnología.
#:
#: Un criterio de aceptación que habla de ``app.py`` no introduce ninguna tecnología: es un archivo
#: del proyecto. Sin esta exclusión, cada ruta con extensión de dos o tres letras se leería como un
#: nombre propio desconocido y cualquier propuesta táctica quedaría ambigua.
_FILE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        "bat",
        "cfg",
        "cpp",
        "cs",
        "css",
        "csv",
        "env",
        "go",
        "h",
        "html",
        "ini",
        "java",
        "js",
        "json",
        "jsx",
        "lock",
        "md",
        "ps1",
        "py",
        "rb",
        "rs",
        "sh",
        "sql",
        "toml",
        "ts",
        "tsx",
        "txt",
        "xml",
        "yaml",
        "yml",
    }
)

_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.+@-]+")


def _normalize(text: str) -> str:
    """Texto comparable: minúsculas, sin acentos y con espacios colapsados."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", plain).strip()


def _fields(proposal: ProjectReplanProposal) -> tuple[tuple[str, str], ...]:
    """Campos acotados que el motor inspecciona, con su nombre para el motivo."""
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


def _dimensions_in(text: str) -> tuple[str, ...]:
    """Dimensiones de arquitectura que el texto menciona, en el orden declarado."""
    return tuple(
        name
        for name, _, concepts in _DIMENSIONS
        if any(re.search(concept, text) for concept in concepts)
    )


def _has_change_intent(text: str) -> bool:
    """``True`` si el texto propone sustituir, migrar, adoptar o introducir algo."""
    return bool(re.search(_CHANGE_INTENT, text))


def _brands_in(text: str) -> tuple[tuple[str, str], ...]:
    """Marcas conocidas presentes en el texto, con su dimensión."""
    return tuple(
        (brand, dimension)
        for brand, dimension in _KNOWN_BRANDS
        if re.search(rf"\b{re.escape(brand)}\b", text)
    )


def _looks_like_product(token: str) -> bool:
    """``True`` si el token tiene **forma** de nombre de tecnología, sin conocer la marca."""
    if len(token) < MIN_TOKEN_CHARS or token.casefold() in _GENERIC_WORDS:
        return False
    if "/" in token or "\\" in token:
        return False
    if _IDENTIFIER_FORM.match(token):
        return False
    if token.rsplit(".", 1)[-1].casefold() in _FILE_SUFFIXES:
        return False
    return any(form.search(token) for form in _PRODUCT_FORMS)


def _technology_tokens(text: str) -> tuple[str, ...]:
    """Tokens con forma de tecnología, sin repetir y conservando el orden.

    El primer token de cada campo se descarta como candidato: en una frase en inglés o en español la
    primera palabra va en mayúscula por ortografía, no por ser un nombre propio, y marcarla sería
    ruido constante.
    """
    tokens = _TOKEN_PATTERN.findall(text)
    found: list[str] = []
    for index, token in enumerate(tokens):
        if index == 0:
            continue
        if _looks_like_product(token) and token not in found:
            found.append(token)
    return tuple(found)


def _baseline_tokens(contract: ProjectContract) -> frozenset[str]:
    """Vocabulario del baseline de arquitectura, normalizado, para comparar propuestas.

    Incluye los tokens del texto de cada campo y las palabras del vocabulario genérico que el
    baseline contiene: un almacén ``postgres`` autoriza a hablar de «postgres», y un estilo
    ``monolito modular`` autoriza a hablar de «monolito».
    """
    material = " ".join(
        [
            contract.architecture_fingerprint,
            contract.architecture_style,
            contract.architecture_deployment,
            *contract.architecture_components,
            *contract.architecture_services,
            *contract.architecture_data_stores,
            *contract.architecture_integrations,
            *contract.architecture_interfaces,
            *contract.architecture_security,
            *contract.architecture_technology,
        ]
    )
    normalized = _normalize(material)
    tokens = {
        token
        for token in _TOKEN_PATTERN.findall(normalized)
        if len(token) >= MIN_TOKEN_CHARS
    }
    return frozenset(tokens)


def _structural_proof(
    proposal: ProjectReplanProposal, contract: ProjectContract
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Hechos estructurales verificados y los que **no** se pudieron verificar.

    Devuelve ``(probados, fallos)``. Cada hecho es una frase legible: el veredicto tiene que poder
    leerse y discutirse, no ser un booleano opaco.
    """
    proven: list[str] = []
    failures: list[str] = []
    scope = set(contract.authorized_scope)
    criterion_ids = set(contract.acceptance_criterion_ids)
    for operation in proposal.operations:
        proven.append(f"operacion {operation.index} declarada ({operation.kind.value})")
    for operation in proposal.operations:
        for spec in operation.nodes:
            outside = tuple(path for path in spec.allowed_files if scope and path not in scope)
            if outside:
                failures.append(
                    f"el nodo {spec.label!r} escribe fuera del alcance autorizado del contrato "
                    f"({', '.join(outside)})"
                )
            else:
                proven.append(f"alcance del nodo {spec.label!r} dentro del contrato")
            invented = tuple(
                item for item in spec.acceptance_criterion_ids if item not in criterion_ids
            )
            if invented:
                failures.append(
                    f"el nodo {spec.label!r} declara criterios que el contrato no tiene "
                    f"({', '.join(invented)})"
                )
            else:
                proven.append(f"criterios del nodo {spec.label!r} son del contrato")
            if spec.risk > contract.risk_ceiling:
                failures.append(
                    f"el nodo {spec.label!r} sube el riesgo por encima del techo del contrato"
                )
            else:
                proven.append(f"riesgo del nodo {spec.label!r} dentro del techo")
            if spec.authority > contract.authority_ceiling:
                failures.append(
                    f"el nodo {spec.label!r} sube la autoridad por encima del techo del contrato"
                )
            else:
                proven.append(f"autoridad del nodo {spec.label!r} dentro del techo")
    return tuple(proven), tuple(failures)


def classify_replan_change(
    proposal: ProjectReplanProposal,
    *,
    contract: ProjectContract,
    action: str = "",
) -> ReplanChangeClassification:
    """Deriva, **en el motor**, si una propuesta está **demostrada** táctica (F632-01).

    El orden de las comprobaciones es el de la fuerza, y el resultado por defecto es exigir una
    persona:

    1. **Reglas de negocio**: el contrato del proyecto *es* el contrato de negocio; una propuesta
       que
       hable de reglas, modelo o lógica de negocio no es táctica, la mencione como la mencione;
    2. **Dimensión + intención de cambio**: sustituir, migrar, mover, adoptar o introducir un
       concepto de arquitectura —motor, almacén, identidad, alojamiento, plataforma, integración,
       componente, marco de trabajo— es un cambio de diseño, aunque el nombre del producto sea
       desconocido y aunque el Planner lo declare ``LOW``;
    3. **Marca conocida**: si además aparece una marca del catálogo, la clase se nombra con su
       dimensión (acelerador, nunca frontera);
    4. **Tecnología no reconocida**: un nombre con forma de tecnología que el baseline no contiene
       —o cualquiera, si el baseline no se pudo resolver— deja el caso en ``UNKNOWN_OR_AMBIGUOUS``:
       el motor no puede demostrar que esté dentro del diseño;
    5. **Hechos estructurales**: alcance, criterios, riesgo y autoridad de cada nodo nuevo tienen
       que caber en el contrato congelado; si no, tampoco hay ausencia de sospecha;
    6. **Sin sospecha**: sin cambio de dimensión, sin tecnología ajena y con la estructura
       comprobada, la propuesta queda en ``NO_SEMANTIC_SUSPICION`` — que **no** es autonomía, solo
       ausencia de una razón para escalar.

    Args:
        proposal: Propuesta tipada del Planner, con sus textos acotados.
        contract: Contrato inmutable del proyecto, con su baseline de arquitectura.
        action: Acción canónica del proyecto; su impacto se consulta al catálogo del motor.

    Returns:
        La clase de cambio con su motivo, sus marcas, las dimensiones tocadas, las tecnologías no
        reconocidas y los hechos estructurales verificados.
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
    dimensions: list[str] = []
    unknown: list[str] = []
    high_impact: dict[ReplanChangeClass, list[str]] = {}
    known_brands: list[tuple[str, str]] = []
    baseline = _baseline_tokens(contract)
    for name, text in _fields(proposal):
        if not text.strip():
            continue
        normalized = _normalize(text)
        intent = _has_change_intent(normalized)
        touched = _dimensions_in(normalized)
        for dimension in touched:
            if dimension not in dimensions:
                dimensions.append(dimension)
        known_brands.extend(_brands_in(normalized) if intent else ())
        if intent:
            for _, dimension in _brands_in(normalized):
                if dimension not in dimensions:
                    dimensions.append(dimension)
        for token in _technology_tokens(text):
            normalized_token = _normalize(token)
            if normalized_token not in baseline and token not in unknown:
                unknown.append(token)
        for dimension in touched:
            if not intent:
                continue
            target = _class_of_dimension(dimension)
            entry = high_impact.setdefault(target, [])
            if len(entry) < MAX_CHANGE_MATCHES:
                entry.append(f"{name}: {dimension}")
        if "business" in touched:
            entry = high_impact.setdefault(ReplanChangeClass.BUSINESS_RULE_CHANGE, [])
            if not entry:
                entry.append(f"{name}: reglas de negocio")
    for brand, dimension in known_brands:
        target = _class_of_dimension(dimension)
        entry = high_impact.setdefault(target, [])
        if len(entry) < MAX_CHANGE_MATCHES:
            entry.append(f"marca conocida: {brand}")
    if high_impact:
        ordered = sorted(high_impact, key=lambda item: _CLASS_PRIORITY[item])
        chosen = ordered[0]
        for candidate in ordered:
            matches.extend(high_impact[candidate])
        bounded = tuple(matches[:MAX_CHANGE_MATCHES])
        return ReplanChangeClassification(
            chosen,
            (
                f"la propuesta cambia {chosen.value} fuera de lo táctico ({len(bounded)} "
                "marca(s)): el contrato del proyecto no autoriza reescribir el diseño en autonomía"
            ),
            bounded,
            tuple(dimensions),
            tuple(unknown[:MAX_CHANGE_MATCHES]),
        )
    if unknown:
        return ReplanChangeClassification(
            ReplanChangeClass.UNKNOWN_OR_AMBIGUOUS,
            (
                f"la propuesta introduce {len(unknown)} tecnología(s) que el baseline de "
                "arquitectura del proyecto no contiene"
                if contract.has_architecture
                else (
                    "el proyecto no tiene baseline de arquitectura resuelto y la propuesta "
                    "introduce tecnologías: el motor no puede demostrar que estén dentro del diseño"
                )
            ),
            tuple(f"tecnologia no reconocida: {token}" for token in unknown[:MAX_CHANGE_MATCHES]),
            tuple(dimensions),
            tuple(unknown[:MAX_CHANGE_MATCHES]),
        )
    proven, failures = _structural_proof(proposal, contract)
    if failures:
        return ReplanChangeClassification(
            ReplanChangeClass.UNKNOWN_OR_AMBIGUOUS,
            "la tacticidad no se puede demostrar: " + "; ".join(failures),
            tuple(failures),
            tuple(dimensions),
            (),
            proven,
        )
    return ReplanChangeClassification(
        ReplanChangeClass.NO_SEMANTIC_SUSPICION,
        (
            "sin sospecha semántica: el texto no menciona ningún cambio de dimensión de "
            "arquitectura ni tecnología ajena al baseline. Esto **no** concede autonomía: la "
            "demuestra la contención estructural (T1-T7)"
        ),
        (),
        (),
        (),
        proven,
    )


def _class_of_dimension(dimension: str) -> ReplanChangeClass:
    """Clase de alto impacto que corresponde a una dimensión."""
    for name, change_class, _ in _DIMENSIONS:
        if name == dimension:
            return change_class
    return ReplanChangeClass.UNKNOWN_HIGH_IMPACT


#: Prioridad determinista entre dimensiones: lo más específico primero, lo indeterminado al final.
_CLASS_PRIORITY: Final[dict[ReplanChangeClass, int]] = {
    ReplanChangeClass.DATASTORE_CHANGE: 0,
    ReplanChangeClass.AUTH_MODEL_CHANGE: 1,
    ReplanChangeClass.DEPLOYMENT_CHANGE: 2,
    ReplanChangeClass.PROVIDER_CHANGE: 3,
    ReplanChangeClass.BUSINESS_RULE_CHANGE: 4,
    ReplanChangeClass.ARCHITECTURE_CHANGE: 5,
    ReplanChangeClass.UNKNOWN_HIGH_IMPACT: 6,
    ReplanChangeClass.UNKNOWN_OR_AMBIGUOUS: 7,
    ReplanChangeClass.NO_SEMANTIC_SUSPICION: 8,
}


@dataclass(frozen=True, slots=True)
class _Facts:
    """Hechos acotados que el motor extrae de una propuesta, para la vista de auditoría."""

    dimensions: tuple[str, ...] = ()
    unknown_tokens: tuple[str, ...] = ()
    brands: tuple[str, ...] = ()
    change_intent: bool = False
    fields: tuple[str, ...] = field(default=())


def replan_change_facts(
    proposal: ProjectReplanProposal, *, contract: ProjectContract | None = None
) -> _Facts:
    """Hechos acotados de la propuesta, para auditoría y pruebas.

    Es la vista que permite explicar un veredicto sin repetir la lógica: qué dimensiones menciona,
    qué tecnologías no reconoce el baseline, qué marcas aceleradoras aparecen y si propone sustituir
    algo.
    """
    dimensions: list[str] = []
    unknown: list[str] = []
    brands: list[str] = []
    intent = False
    baseline = _baseline_tokens(contract) if contract is not None else frozenset()
    for _, text in _fields(proposal):
        if not text.strip():
            continue
        normalized = _normalize(text)
        for dimension in _dimensions_in(normalized):
            if dimension not in dimensions:
                dimensions.append(dimension)
        if _has_change_intent(normalized):
            intent = True
        for brand, dimension in _brands_in(normalized):
            if brand not in brands:
                brands.append(brand)
            if intent and dimension not in dimensions:
                dimensions.append(dimension)
        for token in _technology_tokens(text):
            if contract is not None and _normalize(token) in baseline:
                continue
            if token not in unknown:
                unknown.append(token)
    return _Facts(
        dimensions=tuple(dimensions),
        unknown_tokens=tuple(unknown[:MAX_CHANGE_MATCHES]),
        brands=tuple(brands),
        change_intent=intent,
        fields=tuple(name for name, _ in _fields(proposal)),
    )


def change_classes_of(
    proposal: ProjectReplanProposal, *, contract: ProjectContract | None = None
) -> tuple[str, ...]:
    """Clases de alto impacto que la propuesta demuestra, para la vista de auditoría.

    El veredicto elige **una** clase (la de mayor prioridad); esto enumera todo lo que el motor vio,
    que es lo que un auditor necesita para discutir el caso. Se calcula con los mismos hechos que el
    veredicto, así que no puede discrepar de él.
    """
    facts = replan_change_facts(proposal, contract=contract)
    classes = [_class_of_dimension(dimension).value for dimension in facts.dimensions]
    if facts.unknown_tokens:
        classes.append(ReplanChangeClass.UNKNOWN_OR_AMBIGUOUS.value)
    return tuple(dict.fromkeys(classes))


def high_impact_labels(classes: Sequence[str]) -> str:
    """Texto legible y acotado de una lista de clases, para el detalle de la decisión."""
    joined = ", ".join(sorted(dict.fromkeys(classes)))
    return joined[:CHANGE_MATCH_CHARS * 4]


__all__ = [
    "CHANGE_MATCH_CHARS",
    "MAX_CHANGE_MATCHES",
    "MIN_TOKEN_CHARS",
    "ReplanChangeClass",
    "ReplanChangeClassification",
    "change_classes_of",
    "classify_replan_change",
    "high_impact_labels",
    "replan_change_facts",
]
