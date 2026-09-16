"""Matriz adversarial de alto impacto contra el escalado semántico (6.3.2 → 6.3.R1).

El hallazgo F632-01 demostró que la barrera anterior —buscar marcas conocidas de alto impacto y
autorizar cuando no encontraba ninguna— era inválida: «no lo reconozco como alto impacto» no es
«he demostrado que es táctico». La auditoría independiente la reprodujo con tecnologías que las
listas no contenían («Replace the current relational engine with CockroachDB», «Move the service
from Vercel to Fly.io», «Replace the current identity service with Clerk»), y la corrección
inviertió la carga de la prueba: ``classify_replan_change`` solo devuelve
``NO_SEMANTIC_SUSPICION`` cuando no encuentra ninguna sospecha, y desde ENGINE-6.3.R1 esa ausencia
**no concede autonomía**: la demuestra la contención estructural (T1-T7) en
``punto.project.containment``. Lo que esta suite fija es la mitad semántica: ninguna propuesta de
alto impacto puede quedar sin escalar.

Esta suite somete esa prueba a una **matriz adversarial acotada y parametrizada**, y lo hace con dos
asertos que se necesitan mutuamente:

1. **La matriz de alto impacto** (``MATRIX``): 33 propuestas que deben quedar NO tácticas, en inglés
   y en español, con marcas conocidas y con marcas **inventadas** (``ExampleDB9000``,
   ``QuasarStore42``, ``NimbusGridX``, ``AlphaAuthNine``, ``OrbitDeploy``, ``ZenithQueue``,
   ``FusionStack8``), cubriendo las seis categorías del encargo: ``datastore``, ``identity``,
   ``deployment``, ``integration``/proveedor, ``architecture``/stack y ``business``. La detección es
   legítima por dos vías —la dimensión esperada está entre las dimensiones tocadas **o** el motor
   enumera tecnologías que el baseline no contiene—, y el veredicto tiene que ser no táctico en
   **ambas** variantes del contrato: con baseline de arquitectura resuelto y sin él.
2. **El control positivo** (``CONTROL_TACTICO``): seis textos tácticos legítimos —cambiar la
   estrategia de implementación de un nodo, reintentar un paso, insertar un prerrequisito técnico
   interno— que **no** deben escalar, también con y sin baseline. Sin este control, la matriz se
   aprobaría sola si alguien convirtiera el clasificador en un muro que exige una persona para
   todo: la ausencia de sospecha dejaría de distinguir y la regresión no se vería.

No hay proveedor real, ni red, ni reloj, ni azar: el contrato, la propuesta y los textos son
deterministas, y las marcas inventadas no existen en ninguna lista de productos del motor —ese es
justamente el punto de la matriz.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from punto.project.replan_change import (
    ReplanChangeClass,
    classify_replan_change,
    replan_change_facts,
)
from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.replan import (
    ProjectContract,
    ProjectReplanProposal,
    ReplanNodeSpec,
    ReplanOperation,
    ReplanOperationKind,
)

#: Identidades fijas de la propuesta unitaria: la clasificación no depende de ellas.
PROJECT_RUN_ID = UUID("f6320000-0000-4000-8000-000000000001")
PROJECT_ID = UUID("f6320000-0000-4000-8000-000000000002")
GENERATION_ID = UUID("f6320000-0000-4000-8000-000000000003")
TRIGGER_ID = UUID("f6320000-0000-4000-8000-000000000004")

#: Categorías que la matriz tiene que cubrir: ninguna puede quedarse sin representación.
CATEGORIAS: tuple[str, ...] = (
    "datastore",
    "identity",
    "deployment",
    "integration",
    "architecture",
    "business",
)

#: Matriz adversarial: ``(categoria, texto, dimension_esperada)``.
#:
#: Cada texto es una propuesta de alto impacto que la barrera de ENGINE-6.3.2 **no** puede adoptar
#: en autonomía. Los casos obligatorios del encargo están aquí literalmente; las marcas
#: inventadas se añaden para demostrar que el veredicto no sale de una lista de productos, sino de
#: la forma del nombre y del contrato del vocabulario genérico.
MATRIX: tuple[tuple[str, str, str], ...] = (
    # --- datastore: cambiar el motor o el almacén de datos --------------------------------
    ("datastore", "Replace the current relational engine with CockroachDB", "datastore"),
    ("datastore", "Move relational records into SurrealDB", "datastore"),
    ("datastore", "Adopt a different distributed SQL backend", "datastore"),
    ("datastore", "Replace the current database engine with ExampleDB9000", "datastore"),
    ("datastore", "Sustituir el motor de base de datos actual por QuasarStore42", "datastore"),
    ("datastore", "Migrar los registros a un almacén de datos no relacional", "datastore"),
    ("datastore", "Adoptar un motor de persistencia diferente para los registros", "datastore"),
    # --- identity: cambiar el modelo de identidad, sesión o autorización -------------------
    ("identity", "Replace the current identity service with Clerk", "identity"),
    ("identity", "Move authentication to a managed identity provider", "identity"),
    ("identity", "Sustituir el servicio de identidad actual por AlphaAuthNine", "identity"),
    ("identity", "Cambiar el modelo de sesiones y permisos por RBAC", "identity"),
    ("identity", "Adopt OAuth for session handling", "identity"),
    ("identity", "Reemplazar el inicio de sesión por un SSO gestionado", "identity"),
    # --- deployment: cambiar dónde y cómo se despliega el proyecto -------------------------
    ("deployment", "Move the service from Vercel to Fly.io", "deployment"),
    ("deployment", "Switch the hosting platform to self-managed Kubernetes", "deployment"),
    ("deployment", "Migrar el despliegue a otra plataforma de nube", "deployment"),
    ("deployment", "Cambiar el alojamiento del servicio a un clúster propio", "deployment"),
    ("deployment", "Migrate the runtime platform to OrbitDeploy", "deployment"),
    ("deployment", "Move the hosting of the service to NimbusGridX", "deployment"),
    # --- integration/proveedor: cambiar servicios externos, colas o pasarelas --------------
    ("integration", "Replace the model provider with ZenithQueue", "integration"),
    ("integration", "Switch the external service integration to another vendor", "integration"),
    (
        "integration",
        "Sustituir la integración del proveedor de modelos por otro servicio externo",
        "integration",
    ),
    ("integration", "Cambiar la cola de mensajería por un broker gestionado", "integration"),
    ("integration", "Adopt a different message bus for background jobs", "integration"),
    # --- architecture/stack: reescribir el diseño del proyecto -----------------------------
    ("architecture", "Rearchitect the monolith into microservices", "architecture"),
    ("architecture", "Rewrite the modular monolith stack with another framework", "architecture"),
    ("architecture", "Reorganizar los módulos del monolito en microservicios", "architecture"),
    ("architecture", "Adopt a FusionStack8 architecture for the service layer", "architecture"),
    # --- business: reescribir lo que el producto decide, no cómo se ejecuta ----------------
    ("business", "Change the business rules for billing", "business"),
    ("business", "Redefine the pricing model of the service", "business"),
    ("business", "Cambiar las reglas de negocio de la facturación", "business"),
    ("business", "Modificar el modelo de negocio y la monetización", "business"),
    ("business", "Rewrite the domain model to support another invoicing policy", "business"),
)

#: Casos obligatorios del encargo, citados literalmente: si alguno desapareciera de la matriz, la
#: cobertura del hallazgo F632-01 se perdería sin que ninguna otra prueba lo notara.
TEXTOS_OBLIGATORIOS: tuple[str, ...] = (
    "Replace the current relational engine with CockroachDB",
    "Move the service from Vercel to Fly.io",
    "Replace the current identity service with Clerk",
    "Move relational records into SurrealDB",
    "Adopt a different distributed SQL backend",
    "Replace the current database engine with ExampleDB9000",
)

#: ``(marca_inventada, texto, dimension_esperada)``: marcas que no existen en ninguna lista del
#: motor. Son la prueba de que la frontera no depende del catálogo de productos: si el veredicto
#: saliera de las listas, estas siete propuestas se adoptarían en autonomía.
MARCAS_INVENTADAS: tuple[tuple[str, str, str], ...] = (
    (
        "ExampleDB9000",
        "Replace the current database engine with ExampleDB9000",
        "datastore",
    ),
    (
        "QuasarStore42",
        "Sustituir el motor de base de datos actual por QuasarStore42",
        "datastore",
    ),
    (
        "AlphaAuthNine",
        "Sustituir el servicio de identidad actual por AlphaAuthNine",
        "identity",
    ),
    ("OrbitDeploy", "Migrate the runtime platform to OrbitDeploy", "deployment"),
    (
        "NimbusGridX",
        "Move the hosting of the service to NimbusGridX",
        "deployment",
    ),
    ("ZenithQueue", "Replace the model provider with ZenithQueue", "integration"),
    (
        "FusionStack8",
        "Adopt a FusionStack8 architecture for the service layer",
        "architecture",
    ),
)

#: Control positivo: textos tácticos legítimos que **sí** se adoptan en autonomía. Cambian la
#: estrategia de implementación de un nodo, reintentan un paso o insertan un prerrequisito técnico
#: interno sin tocar ninguna dimensión de arquitectura ni introducir tecnología ajena. Es lo que
#: impide que la matriz se apruebe por haber convertido todo en «exige humano».
CONTROL_TACTICO: tuple[str, ...] = (
    "Cambiar la estrategia de implementación del nodo R",
    "Reintentar el paso fallido con otra estrategia",
    "Insertar un prerrequisito técnico interno antes del nodo R",
    "Añadir un paso interno de verificación previa",
    "Use another implementation strategy for the same node",
    "Retry the failed step with a corrected strategy inside the same scope",
)

#: Variantes del contrato: con baseline de arquitectura resuelto y sin él. El veredicto tiene que
#: ser no táctico en las dos, porque un contrato sin baseline no autoriza nada: exige una persona.
BASELINE: tuple[bool, ...] = (True, False)
IDS_BASELINE: tuple[str, ...] = ("con-baseline", "sin-baseline")

IDS_MATRIZ: tuple[str, ...] = tuple(
    f"{categoria}-{indice:02d}" for indice, (categoria, _, _) in enumerate(MATRIX)
)
IDS_MARCAS: tuple[str, ...] = tuple(marca for marca, _, _ in MARCAS_INVENTADAS)
IDS_CONTROL: tuple[str, ...] = tuple(
    f"tactico-{indice:02d}" for indice in range(len(CONTROL_TACTICO))
)


def contrato(*, con_baseline: bool) -> ProjectContract:
    """Contrato inmutable mínimo, con o sin baseline de arquitectura resuelto.

    El baseline es lo que permite **demostrar** que la propuesta no cambia el diseño: sin él
    (``architecture_fingerprint`` vacío y sin hechos de arquitectura) el motor no puede comprobar
    que una tecnología esté dentro del diseño aceptado, así que la única respuesta legítima sigue
    siendo exigir una persona. Por eso la matriz se ejecuta contra las dos variantes: si un texto
    solo quedara frenado por tener baseline, el contrato sin baseline delataría el hueco.
    """
    base: dict[str, object] = {
        "project_run_id": PROJECT_RUN_ID,
        "project_id": PROJECT_ID,
        "original_goal": "entregar el servicio con su suite de pruebas",
        "acceptance_criteria": ("AC-1 el paso es verificable",),
        "acceptance_criterion_ids": ("AC-1",),
        "authorized_scope": ("app.py",),
    }
    if con_baseline:
        base |= {
            "architecture_fingerprint": "b" * 32,
            "architecture_style": "monolito modular",
            "architecture_data_stores": ("db:principal:postgres",),
            "architecture_deployment": "un servidor con contenedor",
            "architecture_technology": ("lenguaje:python", "web:fastapi"),
        }
    return ProjectContract(**base)


def propuesta(texto: str) -> ProjectReplanProposal:
    """Propuesta unitaria con el texto a clasificar en el campo acotado del nodo nuevo.

    Sustituye un nodo no aceptado escribiendo el archivo que el contrato autoriza y declarando
    ``LOW``/``LEVEL_0_AUTONOMOUS``: es exactamente la declaración con la que el hallazgo F632-01 se
    colaba, así que lo único que puede frenar la adopción es el veredicto del motor sobre el texto.
    """
    spec = ReplanNodeSpec(
        label="R",
        objective=texto,
        allowed_files=("app.py",),
        risk=RiskLevel.LOW,
        authority=AuthorityLevel.LEVEL_0_AUTONOMOUS,
        supersedes_node_id="N2",
    )
    operation = ReplanOperation(
        index=0,
        kind=ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
        target_node_id="N2",
        nodes=(spec,),
        reason="sustituye el nodo no aceptado por otro equivalente",
    )
    return ProjectReplanProposal(
        project_run_id=PROJECT_RUN_ID,
        source_generation_id=GENERATION_ID,
        trigger_id=TRIGGER_ID,
        superseded_node_ids=("N2",),
        retained_node_ids=("N1",),
        operations=(operation,),
        acceptance_coverage=(("AC-1", ("R",)),),
    )


@pytest.mark.parametrize("con_baseline", BASELINE, ids=IDS_BASELINE)
@pytest.mark.parametrize("categoria, texto, dimension", MATRIX, ids=IDS_MATRIZ)
def test_la_matriz_de_alto_impacto_no_es_tactica(
    categoria: str, texto: str, dimension: str, *, con_baseline: bool
) -> None:
    """Ningún texto de la matriz es táctico ni adopta en autonomía, con baseline y sin él.

    El aserto no se rebaja: la matriz solo admite propuestas de alto impacto, así que una
    ``NO_SEMANTIC_SUSPICION`` aquí sería un agujero de la barrera, no un caso a reclasificar. La
    detección se acepta por las dos vías legítimas del motor: la dimensión esperada entre las
    dimensiones tocadas, o tecnologías que el baseline no contiene (``unknown_tokens``), que es lo
    que hace caer
    a las marcas que ninguna lista conoce.
    """
    classification = classify_replan_change(
        propuesta(texto), contract=contrato(con_baseline=con_baseline), action="modify_file"
    )

    assert classification.escalates is True, (categoria, texto)
    assert classification.requires_human is True, (categoria, texto)
    assert classification.change_class is not ReplanChangeClass.NO_SEMANTIC_SUSPICION, (
        categoria,
        texto,
    )
    assert classification.change_class.is_high_impact or classification.change_class.is_ambiguous
    detectado = dimension in classification.dimensions or bool(classification.unknown_tokens)
    assert detectado, (
        f"{categoria}: {texto!r} no toca la dimensión {dimension!r} ni introduce tecnología "
        f"desconocida (clase={classification.change_class.value}, "
        f"dimensiones={classification.dimensions}, desconocidas={classification.unknown_tokens})"
    )


@pytest.mark.parametrize("con_baseline", BASELINE, ids=IDS_BASELINE)
@pytest.mark.parametrize("marca, texto, dimension", MARCAS_INVENTADAS, ids=IDS_MARCAS)
def test_las_marcas_inventadas_no_dependen_de_listas_de_productos(
    marca: str, texto: str, dimension: str, *, con_baseline: bool
) -> None:
    """Una marca que no existe en ninguna lista cae igual: es el punto del hallazgo F632-01.

    El motor no reconoce ``ExampleDB9000`` ni ``NimbusGridX`` —no están en el catálogo de
    productos— y aun así el veredicto no puede ser táctico. La prueba fija las dos mitades: los
    hechos del motor enumeran la marca como tecnología no contenida en el baseline
    (``unknown_tokens`` no está vacío y cita la marca exacta), y la clase derivada nunca es
    ``NO_SEMANTIC_SUSPICION``. Si mañana alguien ampliara el catálogo con estas siete marcas, la
    prueba seguiría pasando por la vía de la dimensión: la frontera no es la lista.
    """
    facts = replan_change_facts(propuesta(texto), contract=contrato(con_baseline=con_baseline))
    classification = classify_replan_change(
        propuesta(texto), contract=contrato(con_baseline=con_baseline), action="modify_file"
    )

    assert facts.unknown_tokens, f"{marca} debería ser una tecnología no reconocida"
    assert marca in facts.unknown_tokens, (marca, facts.unknown_tokens)
    assert classification.change_class is not ReplanChangeClass.NO_SEMANTIC_SUSPICION, (
        marca,
        texto,
    )
    assert classification.requires_human is True, (marca, texto)
    assert dimension in facts.dimensions or bool(facts.unknown_tokens), (marca, texto)


@pytest.mark.parametrize("con_baseline", BASELINE, ids=IDS_BASELINE)
@pytest.mark.parametrize("texto", CONTROL_TACTICO, ids=IDS_CONTROL)
def test_el_control_positivo_tactico_sigue_siendo_tactico(
    texto: str, *, con_baseline: bool
) -> None:
    """La barrera no es un muro: el trabajo táctico legítimo no añade ninguna sospecha.

    Sin este control, la matriz de alto impacto se aprobaría sola el día que alguien hiciera que el
    clasificador exigiera una persona para todo. Estos seis textos —otra estrategia de
    implementación del mismo nodo, un reintento, un prerrequisito técnico interno— no tocan ninguna
    dimensión de arquitectura, no introducen tecnología ajena y su estructura cabe en el contrato,
    así que **no escalan**. La autonomía de estos nodos la demuestra la contención estructural, no
    esta ausencia de sospecha.
    """
    classification = classify_replan_change(
        propuesta(texto), contract=contrato(con_baseline=con_baseline), action="modify_file"
    )

    assert classification.change_class is ReplanChangeClass.NO_SEMANTIC_SUSPICION, texto
    assert classification.escalates is False, texto
    assert classification.requires_human is False, texto
    assert classification.proof, "los hechos verificados quedan como evidencia de auditoría"
    assert classification.matches == (), texto
    assert classification.unknown_tokens == (), texto


def test_la_matriz_cubre_todas_las_categorias() -> None:
    """Las seis categorías del encargo están representadas: la matriz no puede quedarse coja.

    Una matriz que perdiera una categoría entera —por ejemplo, todas las propuestas de identidad—
    seguiría pasando sus casos y nadie notaría el hueco. Este recorrido lo impide, y exige además
    varios casos por categoría para que ninguna quede representada por un único texto afortunado.
    """
    presentes = {categoria for categoria, _, _ in MATRIX}

    assert presentes == set(CATEGORIAS), (presentes, set(CATEGORIAS))
    for categoria in CATEGORIAS:
        casos = [texto for fila, texto, _ in MATRIX if fila == categoria]
        assert len(casos) >= 3, f"la categoría {categoria!r} está infrarrepresentada: {casos}"


def test_la_matriz_suma_al_menos_veinticuatro_casos() -> None:
    """La matriz es acotada pero suficiente: 24 casos de alto impacto como mínimo, y sus anexos.

    El tamaño no es decorativo: 24 propuestas obligan a cubrir las seis categorías con variantes,
    incluidas las marcas inventadas y los casos obligatorios del encargo, sin que la cobertura se
    sostenga en un par de textos.
    """
    assert len(MATRIX) >= 24, f"la matriz solo tiene {len(MATRIX)} casos"
    assert len(MARCAS_INVENTADAS) >= 6, "hacen falta al menos seis marcas inventadas"
    assert len(CONTROL_TACTICO) >= 4, "hacen falta al menos cuatro textos del control positivo"


def test_los_casos_obligatorios_del_encargo_estan_en_la_matriz() -> None:
    """Los seis textos obligatorios del encargo, citados literalmente, siguen en la matriz.

    La matriz y los anexos (marcas inventadas, casos obligatorios) pueden divergir sin que ninguna
    otra prueba lo note: aquí se fija que cada texto obligatorio y cada caso con marca inventada
    viaja dentro de ``MATRIX``, que es lo que la parametrización ejecuta de verdad.
    """
    textos = {texto for _, texto, _ in MATRIX}

    for texto in TEXTOS_OBLIGATORIOS:
        assert texto in textos, f"falta el caso obligatorio {texto!r}"
    for marca, texto, _ in MARCAS_INVENTADAS:
        assert texto in textos, f"el caso con marca inventada {marca!r} no está en la matriz"
