"""Puerto y adaptador de producción del replanner del proyecto (ENGINE-6.3).

El ``ProjectExecutionKernel`` no sabe —ni puede saber— que existe DeepSeek: cuando un fallo es
técnico, reversible y cabe dentro del contrato ya autorizado, el kernel necesita **otra
estrategia**, y la pide por un puerto. Este módulo es ese puerto y su adaptador de producción:

- :class:`ProjectReplanner` es el **puerto**: recibe un :class:`ReplanRequest` (contrato inmutable,
  grafo vigente, prefijo completado, candidatos a sustituir y la autorización de **esta**
  invocación) y devuelve una :class:`~punto.schemas.replan.ProjectReplanProposal` tipada.
- :class:`NullProjectReplanner` es el valor por defecto y **falla cerrado**: sin replanner inyectado
  no hay replanificación autónoma, y su ``propose`` lo dice con un motivo explícito en lugar de
  devolver una propuesta vacía que alguien pudiera leer como «no hay cambios».
- :class:`PlannerProjectReplanner` es el adaptador de producción: **no** crea un cliente HTTP,
  **no** conoce proveedores y **no** abre un segundo camino de planificación. Compone un
  ``PlannerRequest`` con el encargo acotado, llama al
  :class:`~punto.planner.base.PlannerRunner` que ya existe —el mismo que usan CAMUS y el rol
  ``PLANNER`` del workflow, con sus prompts, su validación de invariantes y su contabilidad de
  gasto— y **traduce** el ``PlanningOutcome`` a una propuesta tipada.

Por qué el puerto vive separado del adaptador
---------------------------------------------
Porque el kernel tiene que poder decidir sin proveedor. Un proyecto con el replanner nulo no
replanifica nunca y lo declara; una prueba puede inyectar un doble determinista y medir la decisión
del motor sin gastar una llamada; y el día que exista otro camino (otro runner, un planner local) no
hay que tocar ni una línea del kernel. La frontera de gasto es **una**: la autorización de
invocación del replan, que el adaptador convierte en los límites efectivos que viajan al runner.

Por qué el encargo es texto acotado y la respuesta conclusión estructurada
--------------------------------------------------------------------------
El modelo recibe un encargo con el contrato, el grafo vigente y el motivo durable del fallo, acotado
a :data:`MAX_REPLAN_PROMPT_CHARS`; devuelve una conclusión —qué nodos no aceptados sustituye, qué
nodos conserva, qué operaciones propone, qué alcance, riesgo y cobertura **reclama**— y nada más: ni
cadena de razonamiento, ni identidad, ni reloj, ni aprobación, ni presupuesto. Todo eso lo pone el
motor, y por eso :func:`parse_replan_payload` **no lee** ninguna clave de autoridad, aprobación,
presupuesto, identidad o marca de tiempo: el modelo no tiene autoridad sobre ellas, y una clave que
el modelo escriba de más no puede influir en nada. Un payload que no sea una propuesta de PUNTO se
traduce a ``None`` y, en el adaptador, a :class:`ProjectReplannerError`: nunca a una propuesta
inventada.

El puerto no ejecuta nada por su cuenta
---------------------------------------
Aquí no se decide elegibilidad (eso es :mod:`punto.project.replan`), no se asignan identidades
(``assign_node_ids``), no se aplica el guard determinista ni la política, no se escribe ninguna
generación de grafo y no se aprueba nada. Publicar una propuesta es dejarla **por referencia** en el
almacén para que el kernel la juzgue; la decisión no está en este módulo.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import TYPE_CHECKING, Final, Protocol
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, ValidationError

from punto.common import normalize_path, utc_now
from punto.planner.base import PlannerLimits, PlannerRequest
from punto.project.graph import GraphNode
from punto.project.replan import REPLAN_FINGERPRINT_CHARS, operation_touches
from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.planning import (
    ArchitecturePlan,
    ProjectCapabilityProfile,
    ProjectIntent,
    ProjectSpec,
)
from punto.schemas.replan import (
    MAX_REPLAN_OPERATION_NODES,
    MAX_REPLAN_OPERATIONS,
    MAX_REPLAN_SHORT_CHARS,
    MAX_REPLAN_SUPERSEDED,
    MAX_REPLAN_TEXT_CHARS,
    ProjectContract,
    ProjectReplanProposal,
    ProjectReplanTrigger,
    ReplanInvocationAuthorization,
    ReplanNodeSpec,
    ReplanOperation,
    ReplanOperationKind,
)
from punto.schemas.workflow import ArtifactReference, RoleName
from punto.workflow.errors import WorkflowError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from datetime import datetime
    from enum import IntEnum

    from punto.planner.base import PlannerRunner, PlanningOutcome
    from punto.schemas.planning import PlannedTask
    from punto.workflow.artifacts import ArtifactStore

#: Tipo del artefacto durable que guarda la propuesta de replanificación.
PROJECT_REPLAN_PROPOSAL_KIND: Final[str] = "PROJECT_REPLAN_PROPOSAL"

#: Etiqueta legible del artefacto de la propuesta.
REPLAN_PROPOSAL_LABEL: Final[str] = "propuesta de replanificación del proyecto"

#: Caracteres máximos del encargo que viaja al Planner en una replanificación.
#:
#: Es una cota del **encargo**, no del prompt final del proveedor: el runner añade su plantilla y su
#: prompt de sistema, y el tope real de la petición HTTP lo sigue fijando el cliente. Existe porque
#: un grafo con todos sus nodos, criterios y evidencia puede crecer hasta la cota del contrato, y un
#: encargo sin cota convertiría el gasto autorizado en una estimación optimista.
MAX_REPLAN_PROMPT_CHARS: Final[int] = 24_000

#: Espacio de nombres con el que se derivan los identificadores del contexto sintetizado.
#:
#: Es una constante inventada y fijada, como las del handoff: el contexto que este módulo compone
#: para el Planner es **determinista** —mismo encargo, mismos identificadores—, y un ``uuid4`` haría
#: que dos invocaciones del mismo encargo produjeran peticiones distintas sin ningún motivo.
_CONTEXT_NAMESPACE: Final[UUID] = UUID("b1d6f0a4-2c95-4f37-8e60-15a7c3b9d248")

#: Centinela de identidad **sin estampar**.
#:
#: ``parse_replan_payload`` es una función pura del payload: no conoce el encargo y por tanto no
#: puede saber de qué proyecto es la propuesta. Cuando el payload no declara la identidad, deja este
#: centinela —que no es una identidad, es «nadie la ha estampado todavía»— y el adaptador, que sí
#: conoce el encargo, la sustituye por la identidad durable del motor.
_UNSTAMPED_ID: Final[UUID] = UUID(int=0)

#: Claves que la propuesta declara y que el payload del modelo **debe** traer.
#:
#: Ausencia o tipo erróneo de cualquiera de ellas significa «esto no es una propuesta de PUNTO», y
#: el parser devuelve ``None`` en vez de completar huecos con valores por defecto: una propuesta a
#: la que le falta la cobertura no es una propuesta incompleta, es otra cosa.
_REQUIRED_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "acceptance_coverage",
        "expected_outcome",
        "operations",
        "retained_node_ids",
        "risk_claim",
        "scope_claim",
        "superseded_node_ids",
    }
)

#: Claves que el modelo **puede** enviar y que el parser no lee jamás.
#:
#: No es una lista de palabras prohibidas: es la lista de campos sobre los que el modelo no tiene
#: autoridad. La aprobación la da una persona, el presupuesto lo reserva el motor, la autoridad la
#: decide el Policy Engine, la identidad y el reloj los pone PUNTO y la huella la calcula el motor.
#: Un payload que las incluya no se rechaza por incluirlas —el modelo puede intentarlo— pero se
#: ignoran: no llegan a la propuesta ni, por tanto, a ninguna decisión.
_IGNORED_MODEL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "approval",
        "approval_id",
        "approved",
        "authority",
        "authority_claim",
        "authority_level",
        "authorized_model_calls",
        "authorized_total_tokens",
        "budget",
        "created_at",
        "human_approved",
        "max_model_calls",
        "max_total_tokens",
        "policy_decision",
        "policy_decision_id",
        "proposal_fingerprint",
        "proposal_id",
    }
)

#: Nombre con el que se sintetiza el proyecto cuando el contrato no permite derivar uno mejor.
_PROJECT_FALLBACK_NAME: Final[str] = "proyecto en ejecución"

#: Caracteres máximos del nombre sintetizado del proyecto.
_MAX_PROJECT_NAME_CHARS: Final[int] = 120

#: Estilo arquitectónico con el que se describe el proyecto que se está replanificando.
#:
#: No es una invención: describe lo que el encargo **sí** demuestra —el proyecto ya tiene un grafo
#: de tareas validado y en ejecución—. Fingir una arquitectura que este módulo no conoce sería
#: pedirle al Planner que planifique contra una ficción.
_ARCHITECTURE_STYLE: Final[str] = "grafo de tareas de un proyecto en ejecución"


class ProjectReplannerError(RuntimeError):
    """El replanner no pudo producir una propuesta válida."""


class ReplanRequest(BaseModel):
    """Encargo de una replanificación: qué contrato rige, qué grafo hay y qué se autoriza.

    Es inmutable (``frozen``) y no admite campos de más (``extra="forbid"``) por el mismo motivo que
    el resto de contratos del motor: lo que entra al replanner es exactamente lo que el kernel
    decidió, y un campo añadido por descuido sería una vía para colar una decisión que nadie tomó.

    Todo lo que un replanner necesita para proponer sin adivinar viaja aquí: el contrato inmutable
    del proyecto, los nodos **vigentes** del grafo (que son los que puede sustituir), el prefijo ya
    completado (que no puede tocar), los candidatos a sustitución que el motor declaró elegibles, el
    contexto del workspace y la autorización de **esta** invocación, que es la cifra única que
    gobierna el gasto.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_run_id: UUID
    project_id: UUID
    trigger: ProjectReplanTrigger
    contract: ProjectContract
    current_nodes: tuple[GraphNode, ...]
    completed_node_ids: tuple[str, ...]
    superseded_candidates: tuple[str, ...]
    accepted_revision: str = ""
    branch_name: str = ""
    workspace_path: str = ""
    authorization: ReplanInvocationAuthorization


class ProjectReplanner(Protocol):
    """Puerto: recibe el encargo y devuelve una propuesta tipada.

    Las tres propiedades de identidad (``name``, ``provider``, ``uses_ai``) existen para la
    auditoría y para que el kernel sepa si esta replanificación puede gastar modelo; ``limits``
    declara la cota que el replanner conoce **antes** de que exista una autorización, y ``None``
    significa *desconocida*, nunca «sin límite».
    """

    @property
    def name(self) -> str:
        """Nombre del replanner, para auditoría y evidencia."""
        ...

    @property
    def provider(self) -> str:
        """Proveedor del modelo. Cadena vacía si el replanner es determinista."""
        ...

    @property
    def uses_ai(self) -> bool:
        """``True`` si el replanner consulta un modelo externo."""
        ...

    @property
    def limits(self) -> PlannerLimits | None:
        """Cota declarada por el replanner, o ``None`` si no declara ninguna."""
        ...

    def propose(self, request: ReplanRequest) -> ProjectReplanProposal:
        """Propone un plan alternativo para el encargo, o lanza si no puede."""
        ...


class NullProjectReplanner:
    """Replanner determinista que NO llama a ningún proveedor: falla cerrado.

    Es el valor por defecto del kernel: sin replanner inyectado no hay replanificación autónoma. Su
    `propose` lanza `ProjectReplannerError` con un motivo explícito.
    """

    @property
    def name(self) -> str:
        """Nombre del replanner nulo."""
        return "NullProjectReplanner"

    @property
    def provider(self) -> str:
        """Cadena vacía: este replanner no consulta ningún proveedor."""
        return ""

    @property
    def uses_ai(self) -> bool:
        """``False``: no hay modelo detrás de este replanner."""
        return False

    @property
    def limits(self) -> PlannerLimits | None:
        """``None``: no declara cota porque no gasta nada."""
        return None

    def propose(self, request: ReplanRequest) -> ProjectReplanProposal:
        """Falla cerrado, con el motivo explícito, en vez de devolver una propuesta vacía.

        Devolver una propuesta sin operaciones sería peor que fallar: el kernel tendría que decidir
        entre leerla como «no hay cambios» —y cerrar el proyecto por no-progreso sin decir por qué—
        o detectar el vacío por su cuenta. Un replanner nulo no propone: lo dice.
        """
        _ = request
        raise ProjectReplannerError(
            "no hay replanner de proyecto inyectado: el ProjectExecutionKernel no replanifica por "
            "su cuenta ni conoce proveedores, y una replanificación autónoma exige un "
            "ProjectReplanner explícito (NullProjectReplanner falla cerrado a propósito)"
        )


class PlannerProjectReplanner:
    """Adaptador de producción: reutiliza el `PlannerRunner` (y CAMUS) que ya existe.

    No crea un segundo cliente ni conoce proveedores: compone un `PlannerRequest` desde el
    `ReplanRequest`, invoca `runner.plan(...)` con los límites efectivos de la autorización
    (`PlannerLimits(max_attempts=1, max_model_calls=authorization.authorized_model_calls,
    max_input_tokens=..., max_output_tokens=authorization.max_output_tokens)`) y **traduce** el
    `PlanningOutcome` a una `ProjectReplanProposal` tipada. Un modelo que devuelva algo que no es
    una propuesta de PUNTO se traduce a `ProjectReplannerError` (nunca a una propuesta inventada).

    Tres decisiones que conviene leer antes de tocar el adaptador:

    - **Un solo intento** (``max_attempts=1``): reintentar dentro de la misma invocación
      multiplicaría el gasto de una autorización que se reservó para una llamada. Si el Planner no
      acierta, el kernel decide con la evidencia si abre otra replanificación —con su propia
      autorización— o para.
    - **La entrada autorizada es el saldo total de tokens**: el adaptador no conoce el tamaño real
      del prompt que compondrá el runner, así que no puede repartir el saldo entre entrada y salida;
      el reparto fino lo hace el runner, que sí conoce su plantilla y aplica el mínimo con sus
      propios techos. El tope de salida sí viaja explícito porque la autorización lo declara.
    - **El presupuesto no se decide aquí**: el tope de salida llega hasta la petición del proveedor
      por el camino que ya existe —el cliente real decide con ``accepts_output_budget`` si acepta el
      parámetro— y la postcondición de gasto la reconcilia el motor contra la autorización. Este
      adaptador ni crea clientes ni inspecciona sus firmas.

    Los límites que viajan al runner son la **cota efectiva** de la autorización, no los del runner
    a secas: el runner aplicará después el mínimo con su configuración, y ninguno de los dos amplía
    al otro.
    """

    def __init__(
        self, *, runner: PlannerRunner, clock: Callable[[], datetime] | None = None
    ) -> None:
        """Guarda el runner inyectado y el reloj con el que se marca lo que esta invocación produce.

        El reloj es inyectable porque todas las marcas durables del motor lo son: una prueba puede
        fijarlo y comprobar la propuesta entera, y un proceso que reanuda no depende del reloj de la
        máquina para saber cuándo se propuso algo.
        """
        self._runner = runner
        self._clock = clock if clock is not None else utc_now

    @property
    def name(self) -> str:
        """Nombre del runner inyectado: el adaptador no inventa una identidad propia."""
        return self._runner.name

    @property
    def provider(self) -> str:
        """Proveedor que declara el runner, o cadena vacía si no declara ninguno."""
        return self._runner.provider

    @property
    def uses_ai(self) -> bool:
        """``True`` si el runner consulta un modelo externo."""
        return self._runner.uses_ai

    @property
    def limits(self) -> PlannerLimits | None:
        """Cota que declara el runner, o ``None`` si no declara ninguna.

        Es la cota **pre-gasto** que el kernel necesita para reservar antes de que exista una
        autorización. El adaptador no inventa una: si el runner no declara límites, el kernel trata
        el caso con su política conservadora en vez de creerse un techo que nadie ha declarado. La
        cota que gobierna de verdad cada invocación es la autorización del replan, que llega en el
        encargo.
        """
        declared = getattr(self._runner, "limits", None)
        return declared if isinstance(declared, PlannerLimits) else None

    def propose(self, request: ReplanRequest) -> ProjectReplanProposal:
        """Compone el encargo, invoca al Planner y traduce su salida a una propuesta tipada.

        El camino es corto y cada paso falla cerrado:

        1. los límites efectivos se derivan de la autorización, y una autorización sin llamada o sin
           tokens positivos se rechaza **antes** de tocar al runner (no se construyen límites
           imposibles);
        2. el encargo acotado se sintetiza de forma determinista desde el contrato y el grafo;
        3. el runner decide (el adaptador no conoce proveedores); una excepción del runner o un
           ``PlanningOutcome`` que no sea ``PASS`` con roadmap y grafo se traducen a
           :class:`ProjectReplannerError`;
        4. el roadmap se traduce al payload de conclusión y pasa por el **mismo** parser que valida
           el JSON del modelo: si el resultado no es una propuesta de PUNTO —o no propone ninguna
           operación— se lanza en vez de devolver algo a medias;
        5. la identidad, el reloj y la huella los estampa el motor, que es el único que los conoce.

        Raises:
            ProjectReplannerError: si la autorización no permite gastar, si el Planner falla o
                lanza, o si su salida no se traduce a una propuesta con al menos una operación.
        """
        stamp = self._clock()
        limits = _effective_limits(request)
        outcome = self._plan(_planner_request(request, limits=limits, stamp=stamp))
        if not outcome.succeeded:
            raise ProjectReplannerError(
                "el Planner no produjo un roadmap válido para el replan "
                f"({outcome.status.value}): {_failure_detail(outcome)}"
            )
        try:
            payload = _payload_from_outcome(request=request, outcome=outcome)
        except ValidationError as error:
            raise ProjectReplannerError(
                "el roadmap del Planner no cabe en el contrato de la propuesta: "
                f"{_bounded(str(error), MAX_REPLAN_TEXT_CHARS)}"
            ) from error
        proposal = parse_replan_payload(payload)
        if proposal is None:
            raise ProjectReplannerError(
                "el roadmap del Planner no se traduce a una propuesta válida de PUNTO: las cotas "
                f"del contrato son {MAX_REPLAN_OPERATIONS} operaciones, "
                f"{MAX_REPLAN_OPERATION_NODES} nodos por operación, {MAX_REPLAN_SUPERSEDED} nodos "
                f"superseded y {MAX_REPLAN_TEXT_CHARS} caracteres por texto, y una salida que no "
                "cabe o no tiene la forma esperada no se adopta a medias"
            )
        if not proposal.operations:
            raise ProjectReplannerError(
                "el roadmap del Planner no propone ninguna operación: una replanificación sin "
                "cambio de plan no es una propuesta, y el motor no la puede confundir con «no hay "
                "cambios»"
            )
        stamped = proposal.model_copy(
            update={
                "project_run_id": request.project_run_id,
                "source_generation_id": request.trigger.generation_id,
                "trigger_id": request.trigger.trigger_id,
                "authority_claim": _authority_claim(proposal),
                "created_at": stamp,
            }
        )
        return stamped.model_copy(update={"proposal_fingerprint": proposal_fingerprint(stamped)})

    def _plan(self, request: PlannerRequest) -> PlanningOutcome:
        """Invoca al runner traduciendo cualquier excepción a :class:`ProjectReplannerError`.

        El contrato del ``PlannerRunner`` dice que no lanza por un fallo del trabajo —los fallos se
        expresan como ``status``—, pero un defecto del runner (o un fallo de transporte que no sepa
        traducir) sí puede lanzar. Aquí no se silencia ni se convierte en una propuesta vacía: se
        convierte en el error del puerto, encadenando el original para no perder el motivo.
        """
        try:
            return self._runner.plan(request)
        except Exception as error:
            raise ProjectReplannerError(
                f"el Planner {self._runner.name!r} lanzó {type(error).__name__} al replanificar: "
                "un fallo del runner no se convierte en una propuesta"
            ) from error


# ---------------------------------------------------------------------------
# Huella canónica de la propuesta
# ---------------------------------------------------------------------------
def proposal_fingerprint(proposal: ProjectReplanProposal) -> str:
    """Huella canónica de la propuesta: qué plan se propone, sin identidad ni marcas de tiempo.

    Entra **todo** lo que la propuesta declara y puede cambiar el plan: la generación de origen, el
    trigger, los nodos superseded y retenidos (ordenados, porque el orden en que se enumeran no
    cambia el grafo resultante), las operaciones con el material de cada nodo propuesto —etiqueta,
    título, objetivo, criterios, identificadores de criterio, alcance, checks, dependencias
    ordenadas, riesgo, autoridad, tecnicidad y nodo al que sustituye—, la cobertura declarada, el
    alcance y el riesgo reclamados.

    No entra nada que el motor posea o que no sea un cambio de plan: el ``proposal_id`` y el
    ``created_at`` (no son deterministas y no describen el plan), el ``project_run_id`` (ya está
    implícito en la generación y el trigger que sí entran), la ``authority_claim`` del resumen —que
    el motor deriva de la autoridad de cada nodo, que sí entra—, las referencias de evidencia y el
    ``expected_outcome``, que es texto libre: dos propuestas que solo difieran en la redacción del
    desenlace esperado son la **misma** propuesta, y si la huella cambiara, el guard de no-progreso
    aceptaría como nueva una replanificación que no cambia nada.

    Se incluyen además dos materiales que el encargo no enumera y que sí cambian el plan: las
    dependencias de una operación de reordenamiento y la declaración de tecnicidad de cada nodo. Una
    huella que los confundiera haría que el guard de no-progreso rechazara un cambio real.

    Es la huella que hace idempotente la adopción tras una caída y la que detecta el no-progreso: el
    mismo plan produce siempre la misma cadena, en cualquier proceso y en cualquier momento.
    """
    material = {
        "source_generation_id": str(proposal.source_generation_id),
        "trigger_id": str(proposal.trigger_id),
        "superseded_node_ids": sorted(proposal.superseded_node_ids),
        "retained_node_ids": sorted(proposal.retained_node_ids),
        "operations": [_operation_material(operation) for operation in proposal.operations],
        "acceptance_coverage": [
            [criterion_id, sorted(labels)] for criterion_id, labels in proposal.acceptance_coverage
        ],
        "scope_claim": sorted(proposal.scope_claim),
        "risk_claim": int(proposal.risk_claim),
    }
    encoded = json.dumps(
        material, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return sha256(encoded).hexdigest()[:REPLAN_FINGERPRINT_CHARS]


# ---------------------------------------------------------------------------
# Publicación y resolución de la propuesta
# ---------------------------------------------------------------------------
def publish_proposal(
    store: ArtifactStore, *, request: ReplanRequest, proposal: ProjectReplanProposal
) -> ArtifactReference:
    """Publica la propuesta como artefacto durable del proyecto y devuelve su referencia.

    La propuesta vive en el espacio del **run del proyecto** y con el ordinal del intento de
    replanificación, no en el de ningún child: es un artefacto del proyecto, tiene que seguir ahí
    cuando el child ya terminó y su ordinal solo sirve para que dos intentos distintos no se
    confundan al mirar el directorio. La referencia guarda la ruta exacta, así que resolverla nunca
    depende de ese ordinal.

    Se serializa en JSON canónico (claves ordenadas, sin espacios decorativos) por la misma razón
    que el handoff de 6.2: el contenido y el digest tienen que ser función del contenido y de nada
    más. La propuesta lleva dentro su ``proposal_id`` y su ``created_at``, de modo que publicar dos
    veces la misma propuesta produce dos artefactos con el mismo significado y digests distintos; lo
    que identifica durablemente una propuesta no son sus bytes, es :func:`proposal_fingerprint`, y
    el kernel persiste su referencia **antes** de adoptarla.
    """
    payload = proposal.model_dump(mode="json")
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return store.put(
        workflow_id=request.project_run_id,
        role=RoleName.PLANNER,
        step_index=max(0, request.authorization.replan_attempt - 1),
        kind=PROJECT_REPLAN_PROPOSAL_KIND,
        label=REPLAN_PROPOSAL_LABEL,
        data=data,
    )


def resolve_proposal(
    store: ArtifactStore, reference: ArtifactReference
) -> ProjectReplanProposal | None:
    """Reconstruye la propuesta de su referencia, o ``None`` si no es de ese tipo.

    ``None`` significa «esta referencia no es una propuesta» —el caso normal cuando la lista de
    referencias del encargo trae artefactos de otros tipos—: distinguir «no es de este tipo» de
    «está roto» importa, porque lo primero se resuelve mirando la referencia siguiente y lo segundo
    tiene que fallar ruidosamente.

    Un artefacto que existe pero no se puede leer, no es UTF-8, no es JSON o no valida contra el
    contrato **no** se degrada a ``None``: es corrupción, y se reporta como
    :class:`ProjectReplannerError` encadenando el motivo original. Degradarla a ``None`` haría que
    el kernel creyera que ese intento de replanificación nunca propuso nada.
    """
    if reference.kind != PROJECT_REPLAN_PROPOSAL_KIND:
        return None
    try:
        data = store.get(reference)
    except WorkflowError as error:
        raise ProjectReplannerError(
            f"no se pudo recuperar la propuesta {reference.reference!r} del almacén "
            f"{reference.store!r}: {error}"
        ) from error
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProjectReplannerError(
            f"la propuesta {reference.reference!r} no es UTF-8: {error}"
        ) from error
    try:
        payload = json.loads(text)
    except ValueError as error:
        raise ProjectReplannerError(
            f"la propuesta {reference.reference!r} no lleva JSON válido: {error}"
        ) from error
    try:
        return ProjectReplanProposal.model_validate(payload)
    except ValidationError as error:
        raise ProjectReplannerError(
            f"la propuesta {reference.reference!r} no valida contra ProjectReplanProposal: {error}"
        ) from error


# ---------------------------------------------------------------------------
# Payload del modelo
# ---------------------------------------------------------------------------
def parse_replan_payload(payload: object) -> ProjectReplanProposal | None:
    """Traduce el JSON del modelo a la propuesta tipada de PUNTO, o ``None`` si no tiene esa forma.

    El payload describe una **conclusión**, y solo eso::

        {"superseded_node_ids": ["B"], "retained_node_ids": ["A"],
         "operations": [{"kind": "SPLIT_NODE", "target_node_id": "B", "reason": "...",
                         "nodes": [{"label": "B1", "objective": "...",
                                    "acceptance_criterion_ids": ["AC-2"],
                                    "allowed_files": ["app.py"], "validation_checks": [...],
                                    "dependencies": [], "risk": "LOW",
                                    "authority": "LEVEL_0_AUTONOMOUS",
                                    "supersedes_node_id": "B", "pure_technical": true}]}],
         "acceptance_coverage": [{"criterion_id": "AC-2", "covered_by": ["B1", "B2"]}],
         "scope_claim": ["app.py"], "risk_claim": "LOW", "expected_outcome": "..."}

    Se devuelve ``None`` si el payload no es un objeto, si le falta alguna de las claves de la
    conclusión, si alguna tiene un tipo que no corresponde (incluida una operación cuyo ``kind`` no
    pertenece a :class:`~punto.schemas.replan.ReplanOperationKind`) o si, aun teniendo la forma, no
    valida contra el contrato —por ejemplo, porque excede las cotas de nodos u operaciones—. No se
    completa ningún hueco con valores por defecto: una conclusión incompleta no es una conclusión
    conservadora, es otra cosa, y el adaptador la convierte en un fallo explícito.

    Dos detalles de autoridad que el parser hace cumplir leyendo **menos** de lo que recibe:

    - las claves de autoridad, aprobación, presupuesto, identidad, huella y reloj se ignoran siempre
      (:data:`_IGNORED_MODEL_KEYS`). El modelo no aprueba nada, no autoriza gasto, no decide
      autoridad y no pone el reloj ni la identidad: si las envía, no llegan a la propuesta y por
      tanto no pueden influir en ninguna decisión;
    - la identidad del encargo (``project_run_id``, ``source_generation_id``, ``trigger_id``) sí se
      lee cuando el payload la trae, porque PUNTO se la incluye como esqueleto; si falta, queda
      **sin estampar** (:data:`_UNSTAMPED_ID`) y el adaptador la sustituye por la identidad durable
      que él conoce, que es la única verdad.

    La forma declarativa del modelo se traduce a la del contrato sin reinterpretarla: la cobertura
    llega como objetos ``criterion_id``/``covered_by`` y se guarda como pares, porque es el mismo
    dato escrito de la forma que un modelo produce de manera fiable. Nunca se copia una cadena de
    razonamiento: lo que no esté en el contrato no existe para PUNTO.
    """
    if not isinstance(payload, dict):
        return None
    declared = {key: value for key, value in payload.items() if key not in _IGNORED_MODEL_KEYS}
    if _REQUIRED_PAYLOAD_KEYS.difference(declared):
        return None
    try:
        return _proposal_from_payload(declared)
    except (_InvalidPayload, ValidationError):
        return None


# ---------------------------------------------------------------------------
# Encargo: límites efectivos y contexto sintetizado
# ---------------------------------------------------------------------------
def _effective_limits(request: ReplanRequest) -> PlannerLimits:
    """Límites de la invocación, derivados **solo** de la autorización del encargo.

    La autorización de invocación es la cifra única que gobierna el gasto de esta llamada: un
    intento, tantas llamadas de modelo como autorice, el saldo total de tokens como tope de entrada
    y el tope de salida que declare. El adaptador no los amplía ni los negocia; el runner aplicará
    después el mínimo con su propia configuración, así que el gasto real nunca supera lo autorizado.

    Una autorización que no autorice ni una llamada, ni un token de entrada, ni un token de salida
    se rechaza aquí: ``PlannerLimits`` exige valores positivos, y construir unos límites imposibles
    para descubrir el problema dentro del runner sería gastar el turno en un fallo que se sabe antes
    de empezar.

    Raises:
        ProjectReplannerError: si la autorización no permite una llamada con presupuesto positivo.
    """
    authorization = request.authorization
    if (
        authorization.authorized_model_calls < 1
        or authorization.authorized_total_tokens < 1
        or authorization.max_output_tokens < 1
    ):
        raise ProjectReplannerError(
            "la autorización de esta invocación no permite una llamada con presupuesto positivo "
            f"(llamadas={authorization.authorized_model_calls}, "
            f"tokens={authorization.authorized_total_tokens}, "
            f"salida={authorization.max_output_tokens}): no se construyen límites imposibles ni "
            "se toca al Planner"
        )
    return PlannerLimits(
        max_attempts=1,
        max_model_calls=authorization.authorized_model_calls,
        max_input_tokens=authorization.authorized_total_tokens,
        max_output_tokens=authorization.max_output_tokens,
    )


def _planner_request(
    request: ReplanRequest, *, limits: PlannerLimits, stamp: datetime
) -> PlannerRequest:
    """Compone la petición que recibe el ``PlannerRunner``, sin inventar ni un dato.

    El contrato del Planner habla de intención, especificación, arquitectura y capacidades, y una
    replanificación no tiene nada de eso guardado: lo que tiene es un contrato inmutable, un grafo
    vigente y un motivo. Así que el contexto se **sintetiza** de forma determinista desde esos datos
    —el encargo acotado en la descripción y el problema, el objetivo original como meta, los
    criterios globales como criterios de éxito, el alcance autorizado como restricción y las rutas
    protegidas como fuera de alcance— y el perfil de capacidades queda vacío por una razón concreta:
    el encargo no lo transporta, y declarar capacidades que nadie ha verificado convertiría el
    perfil en una autorización falsa. El encargo se lo dice al modelo en texto: no declare
    capacidades requeridas.

    Los identificadores sintetizados se derivan del run y del trigger, así que el mismo encargo
    produce la misma petición —y la misma huella de auditoría— en cualquier proceso; la marca de
    tiempo es la del reloj inyectado.
    """
    intent, spec, architecture, profile = _context_for(request, stamp=stamp)
    return PlannerRequest(
        project_id=request.project_id,
        intent=intent,
        project_spec=spec,
        architecture=architecture,
        capability_profile=profile,
        limits=limits,
    )


def _context_for(
    request: ReplanRequest, *, stamp: datetime
) -> tuple[ProjectIntent, ProjectSpec, ArchitecturePlan, ProjectCapabilityProfile]:
    """Contexto determinista del Planner: intención, especificación, arquitectura y perfil."""
    contract = request.contract
    brief = _replan_brief(request)
    name = _project_name(request)
    return (
        ProjectIntent(
            id=_context_id(request, "intent"),
            created_at=stamp,
            name=name,
            description=brief,
            business_goal=contract.original_goal,
            constraints=tuple(contract.authorized_scope),
        ),
        ProjectSpec(
            id=_context_id(request, "spec"),
            created_at=stamp,
            project_name=name,
            problem_statement=brief,
            product_goals=(contract.original_goal,),
            constraints=tuple(contract.authorized_scope),
            out_of_scope=tuple(contract.protected_paths),
            success_criteria=tuple(contract.acceptance_criteria),
        ),
        ArchitecturePlan(
            id=_context_id(request, "architecture"),
            created_at=stamp,
            architecture_style=_ARCHITECTURE_STYLE,
        ),
        ProjectCapabilityProfile(),
    )


def _context_id(request: ReplanRequest, kind: str) -> UUID:
    """Identificador determinista del contexto sintetizado, derivado del run y del trigger."""
    material = f"{request.project_run_id}:{request.trigger.trigger_id}:{kind}"
    return uuid5(_CONTEXT_NAMESPACE, material)


def _project_name(request: ReplanRequest) -> str:
    """Nombre del proyecto para el contexto, derivado del objetivo original del contrato."""
    declared = " ".join(request.contract.original_goal.split())
    return _bounded(declared or _PROJECT_FALLBACK_NAME, _MAX_PROJECT_NAME_CHARS)


def _replan_brief(request: ReplanRequest) -> str:
    """Encargo que viaja al Planner: contrato, grafo vigente, motivo y reglas, acotado.

    Es la única entrada en prosa que el modelo recibe de este módulo, y por eso está hecha de datos
    durables y de nada más: contrato, trigger, nodos y candidatos. Nada de lo que el modelo devolvió
    antes entra aquí, así que un modelo no puede reescribir su propio encargo. El recorte final a
    :data:`MAX_REPLAN_PROMPT_CHARS` es explícito y va al final del texto, donde están las reglas de
    forma: un encargo recortado sigue siendo un encargo con contrato, y una petición sin cota
    convertiría el gasto autorizado en una estimación.
    """
    contract = request.contract
    trigger = request.trigger
    completed = set(request.completed_node_ids)
    candidates = set(request.superseded_candidates)
    lines: list[str] = [
        "REPLANIFICACIÓN ACOTADA DE UN PROYECTO YA EN EJECUCIÓN",
        f"Proyecto: {request.project_id} (run {request.project_run_id})",
        f"Generación del grafo: {trigger.generation_id}",
        f"Revisión aceptada: {request.accepted_revision or '(sin declarar)'}",
        f"Rama: {request.branch_name or '(sin declarar)'}",
        f"Workspace: {request.workspace_path or '(sin declarar)'}",
        f"Motivo durable: {trigger.category} / {trigger.failure_code or '(sin código)'}",
        f"Detalle del fallo: {trigger.detail or '(sin detalle)'}",
        (
            f"Nodo que falló: {trigger.source_node_id} "
            f"(intentos {trigger.attempts_on_node}, reparaciones {trigger.repairs_on_node})"
        ),
        "",
        "CONTRATO INMUTABLE (no se puede cambiar en una replanificación autónoma):",
        f"- Objetivo original: {contract.original_goal}",
    ]
    lines.extend(
        f"- Criterio {criterion_id}: {text}"
        for criterion_id, text in zip(
            contract.acceptance_criterion_ids, contract.acceptance_criteria, strict=False
        )
    )
    if contract.authorized_scope:
        lines.append(f"- Alcance autorizado: {', '.join(contract.authorized_scope)}")
    if contract.protected_paths:
        lines.append(f"- Rutas protegidas (prohibidas): {', '.join(contract.protected_paths)}")
    lines.append(
        f"- Techos: riesgo <= {contract.risk_ceiling.name}, "
        f"autoridad <= {contract.authority_ceiling.name}"
    )
    lines.extend(("", "GRAFO VIGENTE:"))
    for node in request.current_nodes:
        status = "COMPLETED" if node.node_id in completed else "PENDIENTE"
        candidate = " candidato-a-sustituir" if node.node_id in candidates else ""
        dependencies = ", ".join(node.dependencies) or "(ninguna)"
        files = ", ".join(node.allowed_files) or "(ninguno)"
        lines.append(
            f"- {node.node_id} [{status}{candidate}] riesgo={node.risk.name} "
            f"autoridad={node.authority.name} deps=[{dependencies}] archivos=[{files}]: "
            f"{node.objective}"
        )
    lines.extend(
        (
            "",
            "QUÉ SE PIDE:",
            (
                f"- Proponga como máximo {MAX_REPLAN_OPERATIONS} operaciones con "
                f"{MAX_REPLAN_OPERATION_NODES} nodos cada una y como máximo "
                f"{MAX_REPLAN_SUPERSEDED} nodos sustituidos."
            ),
            "- Use etiquetas lógicas: PUNTO asigna los identificadores durables.",
            "- No cambie el objetivo, los criterios, el alcance ni los techos.",
            "- No sustituya nodos COMPLETED: el prefijo completado está congelado.",
            (
                "- El perfil de capacidades está vacío: no declare capacidades requeridas que no "
                "pueda justificar con lo ya disponible."
            ),
            (
                f"- Candidatos a sustituir: "
                f"{', '.join(request.superseded_candidates) or '(ninguno declarado)'}"
            ),
            (
                f"- Devuelva solo la conclusión estructurada, con textos de "
                f"{MAX_REPLAN_TEXT_CHARS} caracteres como máximo por campo."
            ),
        )
    )
    return _bounded("\n".join(lines), MAX_REPLAN_PROMPT_CHARS)


# ---------------------------------------------------------------------------
# Traducción del PlanningOutcome al payload de conclusión
# ---------------------------------------------------------------------------
def _payload_from_outcome(
    *, request: ReplanRequest, outcome: PlanningOutcome
) -> dict[str, object]:
    """Traduce el roadmap del Planner al payload de conclusión, sin reinterpretar el plan.

    La traducción es una **derivación determinista**, y cada regla existe porque el Planner no dice
    explícitamente a qué nodo no aceptado sustituye cada nodo nuevo:

    - una tarea del roadmap cuyo identificador ya existe en el grafo **conserva** ese nodo: la
      identidad es del motor, así que un nodo solo se sustituye si el motor lo declaró candidato;
    - los nodos candidatos que el roadmap ya no contiene —y que no están completados— se sustituyen.
      Solo se declara superseded lo que una operación toca de verdad: los nodos superseded se
      derivan de las operaciones con :func:`punto.project.replan.operation_touches`, de modo que la
      propuesta no puede declarar «esto ya no está» sin decir qué lo reemplaza;
    - los nodos nuevos se reparten entre los nodos sustituidos **en el orden declarado y de la forma
      más uniforme posible**: es el reparto por defecto cuando el modelo no dice cuál es cuál, es
      total (no se pierde ningún nodo nuevo ni ningún nodo sustituido) y el guard determinista puede
      rechazarlo, pero no puede pasar inadvertido;
    - una tarea que conserva un nodo y cambia sus dependencias genera una operación de
      reordenamiento. Las dependencias se comparan como **conjunto**, no en el orden en que el
      modelo las escribió: reordenar la lista de una dependencia no cambia el grafo, y tratarlo como
      un cambio haría que el guard de no-progreso aceptara una replanificación vacía;
    - si el roadmap vuelve a declarar un nodo del prefijo completado, no cambia nada: el trabajo
      aceptado está congelado y la identidad de sus nodos no se reutiliza para trabajo nuevo.

    Los nodos retenidos son «todos los vigentes que no se sustituyen», incluidos los que el roadmap
    no menciona: el silencio del modelo no borra trabajo que el motor no declaró sustituible.

    Devuelve el payload en la forma en que lo devolvería un modelo (objetos declarativos para las
    operaciones, los nodos y la cobertura), de modo que la salida del adaptador pasa por el mismo
    parser que valida el JSON de un modelo y las cotas del contrato se aplican en un solo sitio.
    """
    roadmap = outcome.roadmap
    tasks = () if roadmap is None else tuple(roadmap.tasks)
    current = {node.node_id: node for node in request.current_nodes}
    completed = set(request.completed_node_ids)
    candidates = set(request.superseded_candidates)
    kept = tuple(task.id for task in tasks if task.id in current)
    new_tasks = tuple(task for task in tasks if task.id not in current)
    targets = tuple(
        node.node_id
        for node in request.current_nodes
        if node.node_id in candidates
        and node.node_id not in completed
        and node.node_id not in kept
    )
    operations: list[ReplanOperation] = []
    if new_tasks and targets:
        operations.extend(
            _substitutions(request=request, targets=targets, tasks=new_tasks, offset=0)
        )
    elif new_tasks:
        operations.append(_insertion(request=request, tasks=new_tasks, index=len(operations)))
    operations.extend(
        _reorders(
            request=request,
            tasks=tasks,
            current=current,
            completed=completed,
            offset=len(operations),
        )
    )
    superseded = _dedupe(
        touched for operation in operations for touched in operation_touches(operation)
    )
    superseded_set = set(superseded)
    specs = tuple(node for operation in operations for node in operation.nodes)
    return {
        "project_run_id": str(request.project_run_id),
        "source_generation_id": str(request.trigger.generation_id),
        "trigger_id": str(request.trigger.trigger_id),
        "superseded_node_ids": list(superseded),
        "retained_node_ids": [
            node.node_id for node in request.current_nodes if node.node_id not in superseded_set
        ],
        "operations": [_operation_payload(operation) for operation in operations],
        "acceptance_coverage": _coverage_payload(contract=request.contract, specs=specs),
        "scope_claim": list(_scope_claim(specs)),
        "risk_claim": _risk_claim(request=request, specs=specs, superseded=superseded).name,
        "expected_outcome": _expected_outcome(
            request=request, superseded=superseded, proposed=len(new_tasks)
        ),
    }


def _substitutions(
    *, request: ReplanRequest, targets: Sequence[str], tasks: Sequence[PlannedTask], offset: int
) -> list[ReplanOperation]:
    """Operaciones que sustituyen los nodos no aceptados, repartiendo las tareas nuevas por orden.

    El reparto es uniforme y contiguo (``divmod``), así que es determinista y total: cada nodo
    sustituido recibe su parte en el orden declarado y cada nodo nuevo cae en exactamente una
    operación. Una parte de un solo nodo es un reemplazo; una parte de varios es una división. Si
    hay más nodos que sustituir que nodos nuevos, los últimos quedan con una operación sin nodos:
    el contrato no tiene una operación de borrado, y una sustitución sin sustituto es eso.
    """
    total = len(tasks)
    count = len(targets)
    base, extra = divmod(total, count)
    operations: list[ReplanOperation] = []
    cursor = 0
    for position, target in enumerate(targets):
        size = base + (1 if position < extra else 0)
        chunk = tuple(tasks[cursor : cursor + size])
        cursor += size
        operations.append(
            ReplanOperation(
                index=offset + len(operations),
                kind=(
                    ReplanOperationKind.SPLIT_NODE
                    if size > 1
                    else ReplanOperationKind.REPLACE_UNACCEPTED_NODE
                ),
                target_node_id=target,
                nodes=tuple(
                    _node_spec(task, contract=request.contract, supersedes=target)
                    for task in chunk
                ),
                reason=_bounded(
                    f"sustituye {target} por {size} nodo(s) para superar "
                    f"{request.trigger.category}",
                    MAX_REPLAN_SHORT_CHARS,
                ),
            )
        )
    return operations


def _insertion(
    *, request: ReplanRequest, tasks: Sequence[PlannedTask], index: int
) -> ReplanOperation:
    """Operación que inserta trabajo nuevo cuando el roadmap no sustituye ningún nodo vigente.

    ``INSERT_PREREQUISITE`` con objetivo vacío significa «esto entra antes de lo pendiente sin
    reemplazar a nadie»: el modelo propuso trabajo que el grafo no tenía, y el motor decidirá dónde
    encajarlo. Un roadmap que no sustituya nada y no añada nada no llega aquí: se rechaza por no
    proponer ninguna operación.
    """
    return ReplanOperation(
        index=index,
        kind=ReplanOperationKind.INSERT_PREREQUISITE,
        target_node_id="",
        nodes=tuple(_node_spec(task, contract=request.contract, supersedes="") for task in tasks),
        reason=_bounded(
            f"inserta {len(tasks)} nodo(s) nuevos para superar {request.trigger.category}",
            MAX_REPLAN_SHORT_CHARS,
        ),
    )


def _reorders(
    *,
    request: ReplanRequest,
    tasks: Sequence[PlannedTask],
    current: Mapping[str, GraphNode],
    completed: set[str],
    offset: int,
) -> list[ReplanOperation]:
    """Operaciones que reordenan las dependencias de nodos retenidos y no completados.

    Solo entran los nodos que el roadmap **conserva** (mismo identificador) y que no están
    completados: el prefijo aceptado no se reordena, porque su orden ya produjo el trabajo que
    existe. La comparación es de conjunto: reescribir la misma dependencia en otro orden no es un
    cambio de plan.
    """
    operations: list[ReplanOperation] = []
    for task in tasks:
        node = current.get(task.id)
        if node is None or node.node_id in completed:
            continue
        if tuple(sorted(task.dependencies)) == tuple(sorted(node.dependencies)):
            continue
        operations.append(
            ReplanOperation(
                index=offset + len(operations),
                kind=ReplanOperationKind.REORDER_PENDING_DEPENDENCIES,
                target_node_id=node.node_id,
                dependencies=((node.node_id, tuple(task.dependencies)),),
                reason=_bounded(
                    f"reordena las dependencias de {node.node_id} según el plan propuesto",
                    MAX_REPLAN_SHORT_CHARS,
                ),
            )
        )
    return operations


def _node_spec(
    task: PlannedTask, *, contract: ProjectContract, supersedes: str
) -> ReplanNodeSpec:
    """Nodo propuesto a partir de una tarea del roadmap, con lo que el contrato permite derivar.

    Los identificadores de criterio de aceptación **no** vienen en la tarea: se derivan del contrato
    buscando qué criterios globales menciona el texto del nodo (por identificador o por enunciado),
    que es lo que el guard necesita para comprobar cobertura sin creerse una lista que el modelo
    habría tenido que copiar a mano.

    ``pure_technical`` se **deriva**, no se copia: el contrato del Planner no lleva esa
    declaración, y copiar una que nadie hizo sería inventarla. Se marca ``True`` solo cuando el nodo
    cabe entero en el contrato —riesgo y autoridad dentro de los techos, alcance dentro del
    autorizado y fuera de las rutas protegidas—; en cualquier otro caso se marca ``False`` y el
    guard decide. El adaptador no rechaza aquí lo que no cabe: lo declara, porque quien juzga es el
    motor, no el adaptador.
    """
    risk: RiskLevel = task.risk_level
    authority: AuthorityLevel = task.authority_level
    criteria = tuple(task.acceptance_criteria)
    return ReplanNodeSpec(
        label=task.id,
        title=_bounded(task.title, MAX_REPLAN_SHORT_CHARS),
        objective=task.objective,
        acceptance_criteria=criteria,
        acceptance_criterion_ids=_mentioned_ids(criteria, contract),
        allowed_files=tuple(task.allowed_files),
        context_files=tuple(task.context_files),
        validation_checks=tuple(task.validation_checks),
        dependencies=tuple(task.dependencies),
        risk=risk,
        authority=authority,
        supersedes_node_id=supersedes,
        pure_technical=_within_contract(task, contract),
    )


def _within_contract(task: PlannedTask, contract: ProjectContract) -> bool:
    """``True`` si la tarea cabe entera en los techos y el alcance del contrato.

    Un alcance autorizado vacío significa «el contrato no restringe rutas», no «no se autoriza
    ninguna»: es la misma lectura que hace el resto del motor con las colecciones declaradas, y
    suponer lo contrario marcaría como no técnica cualquier tarea de un contrato sin alcance.
    """
    if task.risk_level > contract.risk_ceiling or task.authority_level > contract.authority_ceiling:
        return False
    protected = {normalize_path(path) for path in contract.protected_paths}
    scope = {normalize_path(path) for path in contract.authorized_scope}
    for path in task.allowed_files:
        normalized = normalize_path(path)
        if normalized in protected:
            return False
        if scope and normalized not in scope:
            return False
    return True


def _mentioned_ids(criteria: Sequence[str], contract: ProjectContract) -> tuple[str, ...]:
    """Identificadores de criterio global que mencionan esos textos, en el orden del contrato."""
    return tuple(
        criterion_id
        for criterion_id, text in zip(
            contract.acceptance_criterion_ids, contract.acceptance_criteria, strict=False
        )
        if _mentions(criteria, criterion_id, text)
    )


def _mentions(criteria: Sequence[str], criterion_id: str, criterion_text: str) -> bool:
    """``True`` si alguno de los textos menciona el identificador o el enunciado del criterio.

    La comparación es normalizada (espacios, mayúsculas) y por contención: un nodo que declare
    ``"AC-2: el paso es verificable"`` cubre ``AC-2`` sin tener que repetir el enunciado global, y
    un nodo que copie el enunciado también.
    """
    needle_id = criterion_id.strip().casefold()
    needle_text = criterion_text.strip().casefold()
    for criterion in criteria:
        normalized = criterion.strip().casefold()
        if needle_id and needle_id in normalized:
            return True
        if needle_text and needle_text in normalized:
            return True
    return False


def _coverage_payload(
    *, contract: ProjectContract, specs: Sequence[ReplanNodeSpec]
) -> list[dict[str, object]]:
    """Cobertura declarada de los criterios globales por los nodos propuestos.

    Solo se declaran los criterios que algún nodo nuevo **reclama**: la ausencia de un criterio es
    información (la propuesta no lo cubre) y el guard la calcula por su cuenta sobre el grafo
    resultante, que es donde la cobertura significa algo. Rellenar la lista con entradas vacías
    daría a entender que la propuesta declara lo que precisamente no declara.
    """
    entries: list[dict[str, object]] = []
    for criterion_id, text in zip(
        contract.acceptance_criterion_ids, contract.acceptance_criteria, strict=False
    ):
        covered = [
            spec.label
            for spec in specs
            if criterion_id in spec.acceptance_criterion_ids
            or _mentions(spec.acceptance_criteria, criterion_id, text)
        ]
        if covered:
            entries.append({"criterion_id": criterion_id, "covered_by": covered})
    return entries


def _scope_claim(specs: Sequence[ReplanNodeSpec]) -> tuple[str, ...]:
    """Alcance reclamado: la unión ordenada de los archivos que los nodos proponen tocar."""
    return _dedupe(path for spec in specs for path in spec.allowed_files)


def _risk_claim(
    *, request: ReplanRequest, specs: Sequence[ReplanNodeSpec], superseded: Sequence[str]
) -> RiskLevel:
    """Riesgo reclamado: el mayor de todo lo que la propuesta toca, nunca a la baja.

    Incluye los nodos sustituidos además de los propuestos: una operación que reemplaza un nodo de
    riesgo alto por otro de riesgo bajo sigue tocando trabajo de riesgo alto (el que ya falló), y
    reclamar menos que eso sería una estimación optimista sobre la que el guard tendría que decidir
    a ciegas.
    """
    current = {node.node_id: node for node in request.current_nodes}
    touched = [current[node_id].risk for node_id in superseded if node_id in current]
    proposed = [spec.risk for spec in specs]
    return max((*proposed, *touched), default=RiskLevel.LOW)


def _authority_claim(proposal: ProjectReplanProposal) -> AuthorityLevel:
    """Autoridad reclamada por la propuesta: la mayor de los nodos que propone.

    Es un derivado determinista de la propuesta, no una declaración del modelo: el parser no lee
    ninguna clave de autoridad, y el resumen lo estampa el motor a partir de la autoridad de cada
    nodo —que sí es material y viaja en cada nodo—. La autoridad **efectiva** de la replanificación
    la decide el Policy Engine con el techo del contrato; esto describe lo que la propuesta pide.
    """
    levels = [node.authority for operation in proposal.operations for node in operation.nodes]
    return max(levels, default=AuthorityLevel.LEVEL_0_AUTONOMOUS)


def _expected_outcome(
    *, request: ReplanRequest, superseded: Sequence[str], proposed: int
) -> str:
    """Desenlace esperado, resumido de forma determinista desde la derivación.

    No es texto del modelo: es la descripción de lo que la propuesta hace —cuántos nodos sustituye y
    cuántos añade— para el motivo durable del trigger. Un resumen inventado por el modelo no podría
    auditarse contra el plan; este se puede contar.
    """
    return _bounded(
        f"replanificación {request.trigger.category} sobre el nodo "
        f"{request.trigger.source_node_id}: {len(superseded)} nodo(s) sustituidos y "
        f"{proposed} nodo(s) propuestos",
        MAX_REPLAN_TEXT_CHARS,
    )


def _operation_payload(operation: ReplanOperation) -> dict[str, object]:
    """Operación en la forma declarativa del payload, tal como la devolvería un modelo."""
    return {
        "kind": operation.kind.value,
        "target_node_id": operation.target_node_id,
        "reason": operation.reason,
        "dependencies": [
            [node_id, list(dependencies)] for node_id, dependencies in operation.dependencies
        ],
        "nodes": [
            {
                "label": node.label,
                "title": node.title,
                "objective": node.objective,
                "acceptance_criteria": list(node.acceptance_criteria),
                "acceptance_criterion_ids": list(node.acceptance_criterion_ids),
                "allowed_files": list(node.allowed_files),
                "context_files": list(node.context_files),
                "validation_checks": list(node.validation_checks),
                "dependencies": list(node.dependencies),
                "risk": node.risk.name,
                "authority": node.authority.name,
                "supersedes_node_id": node.supersedes_node_id,
                "pure_technical": node.pure_technical,
            }
            for node in operation.nodes
        ],
    }


def _operation_material(operation: ReplanOperation) -> dict[str, object]:
    """Material de una operación para la huella: sin ordinal y con las dependencias ordenadas."""
    return {
        "kind": operation.kind.value,
        "target_node_id": operation.target_node_id,
        "dependencies": [
            [node_id, sorted(dependencies)] for node_id, dependencies in operation.dependencies
        ],
        "nodes": [
            {
                "label": node.label,
                "title": node.title,
                "objective": node.objective,
                "acceptance_criteria": list(node.acceptance_criteria),
                "acceptance_criterion_ids": list(node.acceptance_criterion_ids),
                "allowed_files": list(node.allowed_files),
                "validation_checks": list(node.validation_checks),
                "dependencies": sorted(node.dependencies),
                "risk": int(node.risk),
                "authority": int(node.authority),
                "supersedes_node_id": node.supersedes_node_id,
                "pure_technical": node.pure_technical,
            }
            for node in operation.nodes
        ],
    }


# ---------------------------------------------------------------------------
# Parser del payload (forma del contrato)
# ---------------------------------------------------------------------------
class _InvalidPayload(Exception):
    """El payload no tiene la forma del contrato: se traduce a ``None``, nunca a una propuesta."""


def _proposal_from_payload(payload: Mapping[str, object]) -> ProjectReplanProposal:
    """Construye la propuesta desde un payload ya filtrado, o falla con la forma del defecto."""
    return ProjectReplanProposal(
        project_run_id=_identity(payload.get("project_run_id")),
        source_generation_id=_identity(payload.get("source_generation_id")),
        trigger_id=_identity(payload.get("trigger_id")),
        superseded_node_ids=_texts(payload["superseded_node_ids"], field="superseded_node_ids"),
        retained_node_ids=_texts(payload["retained_node_ids"], field="retained_node_ids"),
        operations=_operations(payload["operations"]),
        acceptance_coverage=_coverage(payload["acceptance_coverage"]),
        scope_claim=_texts(payload["scope_claim"], field="scope_claim"),
        risk_claim=_risk_level(payload["risk_claim"]),
        expected_outcome=_text(payload["expected_outcome"], field="expected_outcome"),
    )


def _operations(value: object) -> tuple[ReplanOperation, ...]:
    """Operaciones del payload, con el ordinal tomado de la **posición**, no del modelo.

    El ordinal de una operación es un dato del motor: describe en qué orden se declararon las
    operaciones, y un número elegido por el modelo no puede reordenarlas ni colisionar con otra.
    """
    if not isinstance(value, list | tuple):
        raise _InvalidPayload("operations")
    return tuple(_operation(entry, index=index) for index, entry in enumerate(value))


def _operation(value: object, *, index: int) -> ReplanOperation:
    """Una operación del payload, o ``_InvalidPayload`` si su forma no es la del contrato."""
    if not isinstance(value, dict):
        raise _InvalidPayload("operations[]")
    kind = value.get("kind")
    if not isinstance(kind, str):
        raise _InvalidPayload("operations[].kind")
    try:
        parsed_kind = ReplanOperationKind(kind)
    except ValueError as error:
        raise _InvalidPayload(f"operations[].kind desconocido: {kind!r}") from error
    return ReplanOperation(
        index=index,
        kind=parsed_kind,
        target_node_id=_text(value.get("target_node_id", ""), field="operations[].target"),
        nodes=_nodes(value.get("nodes", [])),
        dependencies=_dependencies(value.get("dependencies", [])),
        reason=_text(value.get("reason", ""), field="operations[].reason"),
    )


def _nodes(value: object) -> tuple[ReplanNodeSpec, ...]:
    """Nodos propuestos por una operación."""
    if not isinstance(value, list | tuple):
        raise _InvalidPayload("operations[].nodes")
    return tuple(_node(entry) for entry in value)


def _node(value: object) -> ReplanNodeSpec:
    """Un nodo propuesto, con los campos de la conclusión que el contrato admite."""
    if not isinstance(value, dict):
        raise _InvalidPayload("operations[].nodes[]")
    return ReplanNodeSpec(
        label=_text(value.get("label"), field="nodes[].label"),
        title=_text(value.get("title", ""), field="nodes[].title"),
        objective=_text(value.get("objective"), field="nodes[].objective"),
        acceptance_criteria=_texts(value.get("acceptance_criteria", []), field="criteria"),
        acceptance_criterion_ids=_texts(
            value.get("acceptance_criterion_ids", []), field="criterion_ids"
        ),
        allowed_files=_texts(value.get("allowed_files", []), field="allowed_files"),
        context_files=_texts(value.get("context_files", []), field="context_files"),
        validation_checks=_texts(value.get("validation_checks", []), field="validation_checks"),
        dependencies=_texts(value.get("dependencies", []), field="dependencies"),
        risk=_risk_level(value.get("risk", RiskLevel.LOW.name)),
        authority=_authority_level(
            value.get("authority", AuthorityLevel.LEVEL_0_AUTONOMOUS.name)
        ),
        supersedes_node_id=_text(
            value.get("supersedes_node_id", ""), field="nodes[].supersedes_node_id"
        ),
        pure_technical=_boolean(value.get("pure_technical", True), field="pure_technical"),
    )


def _dependencies(value: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Dependencias nuevas de una operación de reordenamiento, como pares ``(nodo, deps)``."""
    if not isinstance(value, list | tuple):
        raise _InvalidPayload("operations[].dependencies")
    entries: list[tuple[str, tuple[str, ...]]] = []
    for entry in value:
        if not isinstance(entry, list | tuple) or len(entry) != 2:
            raise _InvalidPayload("operations[].dependencies[]")
        entries.append(
            (
                _text(entry[0], field="dependencies[].node"),
                _texts(entry[1], field="dependencies[].values"),
            )
        )
    return tuple(entries)


def _coverage(value: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Cobertura declarada, aceptando la forma del modelo y la del contrato.

    El modelo envía objetos (``{"criterion_id": ..., "covered_by": [...]}``) porque es la forma que
    produce de manera fiable; el contrato guarda pares porque es la forma que no se puede
    malinterpretar al persistir. Son el mismo dato, así que se aceptan las dos y se normaliza a
    pares.
    """
    if not isinstance(value, list | tuple):
        raise _InvalidPayload("acceptance_coverage")
    entries: list[tuple[str, tuple[str, ...]]] = []
    for entry in value:
        if isinstance(entry, dict):
            if "criterion_id" not in entry or "covered_by" not in entry:
                raise _InvalidPayload("acceptance_coverage[]")
            entries.append(
                (
                    _text(entry["criterion_id"], field="criterion_id"),
                    _texts(entry["covered_by"], field="covered_by"),
                )
            )
            continue
        if isinstance(entry, list | tuple) and len(entry) == 2:
            entries.append(
                (_text(entry[0], field="criterion_id"), _texts(entry[1], field="covered_by"))
            )
            continue
        raise _InvalidPayload("acceptance_coverage[]")
    return tuple(entries)


def _identity(value: object) -> UUID:
    """Identidad declarada en el payload, o el centinela de «sin estampar» si no viene.

    El centinela no es una identidad inventada: es la marca de que el parser no la conoce porque el
    payload no la trae, y el adaptador —que sí conoce el encargo— la sustituye antes de devolver la
    propuesta. Un valor que no sea un UUID se considera forma errónea.
    """
    if value is None:
        return _UNSTAMPED_ID
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as error:
            raise _InvalidPayload(f"identidad no es UUID: {value!r}") from error
    raise _InvalidPayload("identidad con tipo erróneo")


def _text(value: object, *, field: str) -> str:
    """Campo de texto obligatorio del payload."""
    if value is None or not isinstance(value, str):
        raise _InvalidPayload(field)
    return value


def _texts(value: object, *, field: str) -> tuple[str, ...]:
    """Colección de textos del payload."""
    if not isinstance(value, list | tuple):
        raise _InvalidPayload(field)
    return tuple(_text(entry, field=field) for entry in value)


def _boolean(value: object, *, field: str) -> bool:
    """Campo booleano del payload, sin admitir sustitutos."""
    if not isinstance(value, bool):
        raise _InvalidPayload(field)
    return value


def _risk_level(value: object) -> RiskLevel:
    """Nivel de riesgo declarado, por nombre o por número, o forma errónea."""
    return RiskLevel(_level_number(value, field="risk", level=RiskLevel))


def _authority_level(value: object) -> AuthorityLevel:
    """Nivel de autoridad declarado, por nombre o por número, o forma errónea."""
    return AuthorityLevel(_level_number(value, field="authority", level=AuthorityLevel))


def _level_number(value: object, *, field: str, level: type[IntEnum]) -> int:
    """Número del nivel declarado, admitiendo el nombre declarativo y el número serializado.

    Se admiten los dos porque el JSON del modelo usa nombres (``"LOW"``) y la serialización del
    contrato usa números (un ``IntEnum`` se escribe como su valor): el mismo dato, dos formas. Un
    booleano se rechaza explícitamente porque en Python es un ``int`` y ``True`` no es un nivel.
    """
    if isinstance(value, bool):
        raise _InvalidPayload(field)
    if isinstance(value, int):
        try:
            return level(value).value
        except ValueError as error:
            raise _InvalidPayload(f"{field} fuera de rango: {value!r}") from error
    if isinstance(value, str):
        try:
            return level[value.strip().upper()].value
        except KeyError as error:
            raise _InvalidPayload(f"{field} desconocido: {value!r}") from error
    raise _InvalidPayload(field)


# ---------------------------------------------------------------------------
# Interno
# ---------------------------------------------------------------------------
def _failure_detail(outcome: PlanningOutcome) -> str:
    """Motivo acotado del fallo del Planner: sus violaciones o su error."""
    detail = "; ".join(outcome.violations) if outcome.violations else outcome.error
    return _bounded(detail or "sin detalle", MAX_REPLAN_SHORT_CHARS)


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    """Copia sin vacíos y sin repetidos, conservando el orden de primera aparición.

    Se descartan los vacíos porque una cadena vacía no es un identificador ni una ruta: dejarla
    pasar convertiría un dato ausente en un elemento del plan.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return tuple(ordered)


def _bounded(text: str, max_chars: int) -> str:
    """Recorta ``text`` a ``max_chars`` sin adornos, para campos que el contrato ya acota.

    No se añade marca de recorte porque estos textos van a campos de contrato que no la admiten y
    porque el recorte está documentado en cada función que lo aplica. Nunca devuelve más caracteres
    de los pedidos, ni siquiera cuando el límite es cero.
    """
    return text if len(text) <= max_chars else text[:max_chars]


__all__ = [
    "MAX_REPLAN_PROMPT_CHARS",
    "PROJECT_REPLAN_PROPOSAL_KIND",
    "REPLAN_PROPOSAL_LABEL",
    "NullProjectReplanner",
    "PlannerProjectReplanner",
    "ProjectReplanner",
    "ProjectReplannerError",
    "ReplanRequest",
    "parse_replan_payload",
    "proposal_fingerprint",
    "publish_proposal",
    "resolve_proposal",
]
