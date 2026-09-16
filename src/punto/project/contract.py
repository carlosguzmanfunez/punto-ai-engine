"""Contrato durable e inmutable del proyecto (ENGINE-6.3).

Por qué existe este módulo
--------------------------
La replanificación autónoma acotada necesita un límite que el modelo **no** pueda renegociar. Ese
límite es el contrato del proyecto: el objetivo original, los criterios de aceptación globales con
identidad estable, el alcance autorizado, las rutas protegidas y los techos de riesgo y autoridad.
Sin él, «replanificar» sería reescribir el encargo, y el motor no tendría contra qué comprobar que
la estrategia nueva sigue haciendo el mismo trabajo que se autorizó: se declararía
``PROJECT_REPLAN_CONTRACT_CHANGE_REQUIRED`` y pararía, o pediría una persona.

Qué se deriva y de dónde
------------------------
Nada se inventa aquí. La derivación reutiliza lo que el motor ya tiene:

- el **objetivo** es el de la ``ProjectRequest`` (el enunciado del encargo, tal cual: no se
  reescribe);
- los **criterios de aceptación globales** y el **alcance autorizado** salen de lo que la petición
  declare y, cuando no declara nada —el ``ProjectRequest`` de ENGINE-6.2/6.3 no tiene campos
  ``acceptance_criteria`` ni ``changed_files``—, del **plan durable**: los criterios de sus tareas,
  sin repetidos y en orden declarado, y la unión de sus ``allowed_files``. Es el plan que el
  proyecto ejecuta de verdad, así que es el plan el que dice qué trabajo se autorizó;
- las **rutas protegidas** se derivan con :func:`punto.policy.permissions.is_protected_path` sobre
  ese alcance declarado **más** el piso constitucional, que se consulta y no se inventa;
- los **techos** de riesgo y autoridad son los que la petición declara. No se relajan ni se
  elevan: una propuesta que no quepa en ellos no cabe en el contrato;
- la **revisión inicial** llega como argumento, no de ``request.initial_revision``, porque el kernel
  es el único que resuelve la revisión real del workspace (una petición con revisión vacía significa
  «la que tenga el workspace al iniciar»).

Por qué la huella excluye la identidad y el sello
-------------------------------------------------
``contract_fingerprint`` se calcula solo sobre los **términos** del contrato. ``contract_id`` y
``created_at`` quedan fuera a propósito: un proyecto reanudado en otro proceso vuelve a derivar su
contrato y obtiene un ``contract_id`` y un sello nuevos, y si esos campos entraran en la huella, la
reanudación se leería como un cambio de contrato y bloquearía el proyecto. La huella es lo que
sobrevive al proceso; la identidad del artefacto, no.

Dos reglas que se repiten en todo el módulo:

- **determinismo total**: ni reloj, ni azar, ni entorno. Los mismos argumentos producen el mismo
  contrato (salvo identidad y sello) y siempre la misma huella;
- **una sola normalización**: las rutas pasan por :func:`punto.common.normalize_path`, el
  normalizador canónico del motor. Una segunda normalización más débil al lado de la canónica sería
  la vía clásica para colarse fuera de la autorización.

Este módulo no decide nada más: no valida grafos, no ejecuta, no aprueba propuestas y no publica
generaciones. Deriva, publica, resuelve y compara; quien decide es el kernel.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from pydantic import ValidationError

from punto.common import normalize_path
from punto.policy.permissions import CONSTITUTIONAL_PROTECTED_PATHS, is_protected_path
from punto.project.graph import FINGERPRINT_CHARS
from punto.project.handoff import project_run_id_for
from punto.project.resources import (
    project_resource_envelope,
    resource_envelope_fingerprint,
)
from punto.schemas.planning import ArchitecturePlan
from punto.schemas.project import ProjectRequest
from punto.schemas.replan import (
    MAX_REPLAN_COVERAGE,
    MAX_REPLAN_SHORT_CHARS,
    MAX_REPLAN_TEXT_CHARS,
    ProjectContract,
)
from punto.schemas.workflow import ArtifactReference, RoleName
from punto.workflow.artifacts import ArtifactStore
from punto.workflow.errors import WorkflowError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from punto.workflow.handoff import DurablePlan

#: Tipo del artefacto que guarda el contrato durable e inmutable del proyecto.
PROJECT_CONTRACT_KIND: Final[str] = "PROJECT_CONTRACT"

#: Etiqueta con la que el proyecto nombra su contrato en el almacén y en su auditoría.
CONTRACT_LABEL: Final[str] = "contrato durable e inmutable del proyecto"

#: Prefijo de la identidad estable de un criterio de aceptación global.
#:
#: Es el mismo ``AC-`` que usa el contrato de QA (``punto.schemas.qa``): un criterio global y un
#: criterio de tarea se nombran igual porque son la misma clase de objeto, y un identificador
#: distinto para lo mismo obligaría a traducir entre dos vocabularios que dicen lo mismo.
CRITERION_ID_PREFIX: Final[str] = "AC-"

#: Paso con el que se registran los artefactos del **proyecto** en el almacén.
#:
#: El proyecto no ejecuta pasos de rol —eso es el child workflow—, así que su contrato se publica
#: en el paso 0 bajo la identidad del run del proyecto, igual que el grafo congelado y los handoffs
#: de sus nodos. La referencia que se devuelve lleva la ruta exacta, de modo que resolverla nunca
#: depende de este ordinal.
_CONTRACT_STEP_INDEX: Final[int] = 0

#: Caracteres máximos de la revisión inicial, que es la cota de todos los SHA del proyecto.
_MAX_REVISION_CHARS: Final[int] = 64

#: Términos del contrato: lo que una propuesta **no** puede cambiar.
#:
#: El orden es fijo y es el de la tupla que devuelve :func:`assert_contract_unchanged`: quien lee el
#: detalle de un rechazo ve siempre los campos en el mismo orden, sea cual sea el orden en el que el
#: candidato los tocó. ``contract_id``, ``created_at``, ``contract_fingerprint`` y las identidades
#: del proyecto quedan fuera: no son términos del encargo, son identidad y procedencia.
_CONTRACT_TERM_FIELDS: Final[tuple[str, ...]] = (
    "original_goal",
    "acceptance_criteria",
    "acceptance_criterion_ids",
    "authorized_scope",
    "protected_paths",
    "risk_ceiling",
    "authority_ceiling",
    "initial_revision",
    # Baseline de arquitectura (ENGINE-6.3.2): forma parte de los términos porque una propuesta que
    # cambie el diseño autorizado tiene que verse como un cambio de contrato, no como una táctica.
    "architecture_fingerprint",
    "architecture_style",
    "architecture_components",
    "architecture_services",
    "architecture_data_stores",
    "architecture_integrations",
    "architecture_interfaces",
    "architecture_security",
    "architecture_deployment",
    "architecture_technology",
    # Envelope de recursos (ENGINE-6.3.R1): la autorización estructural del proyecto forma parte de
    # los términos; una propuesta que pida recursos fuera de él no es una táctica.
    "authorized_resources",
    "resource_envelope_fingerprint",
)


class ProjectContractError(RuntimeError):
    """El contrato durable del proyecto no se puede construir o resolver.

    Es el equivalente de :class:`punto.project.handoff.ProjectHandoffError` en esta capa, y es un
    ``RuntimeError`` por el mismo motivo: describe un defecto de la ejecución del proyecto, no una
    entrada mal formada. Quien lo captura —el ``ProjectExecutionKernel``— lo traduce a un código
    estable de :class:`~punto.schemas.project.ProjectFailureCode`, igual que hace con los errores
    del handoff: ``PROJECT_REPLAN_CONTRACT_CHANGE_REQUIRED`` cuando el contrato no encaja con lo
    autorizado y el código que corresponda cuando falta el artefacto que lo respalda.

    Hay una diferencia deliberada con el handoff: aquí **no** se distingue «no es de este tipo» de
    «está roto» con dos excepciones. Lo primero se resuelve devolviendo ``None`` —es el caso normal
    cuando la lista de referencias del run trae artefactos de otros tipos— y lo segundo es este
    error. Un contrato que existe pero no valida no se degrada nunca: sin contrato no hay contra qué
    comparar una propuesta, y seguir sin él sería autorizar a ciegas.
    """


def criterion_ids(count: int) -> tuple[str, ...]:
    """Identidades de criterio ``("AC-1", "AC-2", …)`` para ``count`` criterios.

    La identidad es **posicional**: no se deriva del texto del criterio. Un criterio reescrito —una
    coma, una palabra— seguiría siendo el mismo criterio, y una identidad derivada del texto
    cambiaría con él y rompería la cobertura declarada por todas las propuestas anteriores. Es la
    misma decisión que ya toma el contrato de QA.

    Raises:
        ValueError: si ``count`` es negativo. No hay «menos uno criterios»: pedirlo es un defecto de
            quien llama y se dice antes de devolver una tupla vacía que se leería como «ninguno».
    """
    if count < 0:
        raise ValueError(f"no se pueden numerar {count} criterios: la cuenta no puede ser negativa")
    return tuple(f"{CRITERION_ID_PREFIX}{index}" for index in range(1, count + 1))


def contract_fingerprint(contract: ProjectContract) -> str:
    """Huella canónica de los **términos** del contrato (sha256 truncado a 32 caracteres).

    Entran el objetivo, los criterios con sus identidades, el alcance, las rutas protegidas, los
    techos y la revisión inicial. No entran ``contract_id``, ``created_at`` ni la propia huella: un
    proyecto reanudado deriva un contrato nuevo con identidad y sello nuevos, y la huella tiene que
    seguir siendo la misma o la reanudación se leería como un cambio de contrato (ver el docstring
    del módulo).

    La forma canónica es JSON con claves ordenadas, sin espacios decorativos y sin escapar UTF-8,
    así que dos contratos con los mismos términos producen **los mismos bytes** y, por tanto, la
    misma huella, los calcule el proceso que los calcule. La longitud es la de la huella del grafo
    (:data:`punto.project.graph.FINGERPRINT_CHARS`): dos huellas del proyecto con la misma forma se
    comparan y se registran igual.
    """
    material = json.dumps(
        {
            "acceptance_criteria": list(contract.acceptance_criteria),
            "acceptance_criterion_ids": list(contract.acceptance_criterion_ids),
            "architecture_components": list(contract.architecture_components),
            "architecture_data_stores": list(contract.architecture_data_stores),
            "architecture_deployment": contract.architecture_deployment,
            "architecture_fingerprint": contract.architecture_fingerprint,
            "architecture_integrations": list(contract.architecture_integrations),
            "architecture_interfaces": list(contract.architecture_interfaces),
            "architecture_security": list(contract.architecture_security),
            "architecture_services": list(contract.architecture_services),
            "architecture_style": contract.architecture_style,
            "architecture_technology": list(contract.architecture_technology),
            "authorized_resources": list(contract.authorized_resources),
            "authority_ceiling": int(contract.authority_ceiling),
            "authorized_scope": list(contract.authorized_scope),
            "initial_revision": contract.initial_revision,
            "original_goal": contract.original_goal,
            "protected_paths": list(contract.protected_paths),
            "resource_envelope_fingerprint": contract.resource_envelope_fingerprint,
            "risk_ceiling": int(contract.risk_ceiling),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


def architecture_baseline(architecture: ArchitecturePlan | None) -> dict[str, Any]:
    """Hechos de arquitectura que la replanificación autónoma **no** puede cambiar (F632-01).

    Se derivan del ``ArchitecturePlan`` durable que viaja con el plan aceptado —no del Planner, y no
    de una síntesis vacía— y se acotan como el resto de colecciones del contrato: identificadores,
    nombres y motores, sin copiar objetos ilimitados.

    Si la arquitectura no se puede resolver, se devuelve un baseline **vacío y sin huella**: el
    motor no fabrica conocimiento de arquitectura, y el clasificador tratará toda propuesta con
    semántica de diseño como ambigua y exigirá una persona.

    Args:
        architecture: Arquitectura original del plan durable, o ``None`` si el plan no la trae.

    Returns:
        Diccionario con los campos del contrato que forman el baseline y su huella.
    """
    if architecture is None:
        return {
            "architecture_fingerprint": "",
            "architecture_style": "",
            "architecture_components": (),
            "architecture_services": (),
            "architecture_data_stores": (),
            "architecture_integrations": (),
            "architecture_interfaces": (),
            "architecture_security": (),
            "architecture_deployment": "",
            "architecture_technology": (),
        }
    components = tuple(
        _bounded(f"{component.id}:{component.name}", MAX_REPLAN_SHORT_CHARS)
        for component in architecture.components
    )[:MAX_REPLAN_COVERAGE]
    data_stores = tuple(
        _bounded(f"{store.id}:{store.name}:{store.engine}", MAX_REPLAN_SHORT_CHARS)
        for store in architecture.data_stores
    )[:MAX_REPLAN_COVERAGE]
    integrations = tuple(
        _bounded(
            f"{item.id}:{item.name}:{item.protocol}:{item.auth}", MAX_REPLAN_SHORT_CHARS
        )
        for item in architecture.external_integrations
    )[:MAX_REPLAN_COVERAGE]
    interfaces = tuple(
        _bounded(f"{item.id}:{item.name}:{item.kind.value}", MAX_REPLAN_SHORT_CHARS)
        for item in architecture.interfaces
    )[:MAX_REPLAN_COVERAGE]
    security = tuple(
        _bounded(f"{item.id}:{item.name}:{item.description}", MAX_REPLAN_SHORT_CHARS)
        for item in architecture.security_boundaries
    )[:MAX_REPLAN_COVERAGE]
    technology = tuple(
        _bounded(f"{choice.topic}:{choice.choice}", MAX_REPLAN_SHORT_CHARS)
        for choice in architecture.technology_choices
    )[:MAX_REPLAN_COVERAGE]
    baseline: dict[str, Any] = {
        "architecture_fingerprint": architecture_fingerprint(architecture),
        "architecture_style": _bounded(
            architecture.architecture_style, MAX_REPLAN_SHORT_CHARS
        ),
        "architecture_components": components,
        "architecture_services": tuple(
            _bounded(item, MAX_REPLAN_SHORT_CHARS) for item in architecture.services
        )[:MAX_REPLAN_COVERAGE],
        "architecture_data_stores": data_stores,
        "architecture_integrations": integrations,
        "architecture_interfaces": interfaces,
        "architecture_security": security,
        "architecture_deployment": _bounded(
            architecture.deployment_topology, MAX_REPLAN_TEXT_CHARS
        ),
        "architecture_technology": technology,
    }
    return baseline


def architecture_fingerprint(architecture: ArchitecturePlan) -> str:
    """Huella canónica de la arquitectura autorizada, para atarla al contrato.

    Incluye las dimensiones que una replanificación autónoma no puede cambiar —estilo, componentes,
    servicios, almacenes con su motor, integraciones con su autenticación, interfaces, fronteras de
    seguridad, topología de despliegue y elecciones tecnológicas—, de modo que dos arquitecturas
    distintas no puedan compartir baseline por parecerse en el nombre.
    """
    material = json.dumps(
        {
            "architecture_style": architecture.architecture_style,
            "components": sorted(
                f"{item.id}:{item.name}:{item.kind.value}" for item in architecture.components
            ),
            "data_stores": sorted(
                f"{item.id}:{item.name}:{item.engine}:{item.managed}"
                for item in architecture.data_stores
            ),
            "deployment_topology": architecture.deployment_topology,
            "external_integrations": sorted(
                f"{item.id}:{item.name}:{item.protocol}:{item.auth}"
                for item in architecture.external_integrations
            ),
            "interfaces": sorted(
                f"{item.id}:{item.name}:{item.kind.value}" for item in architecture.interfaces
            ),
            "modules": sorted(architecture.modules),
            "security_boundaries": sorted(
                f"{item.id}:{item.name}:{item.description}"
                for item in architecture.security_boundaries
            ),
            "services": sorted(architecture.services),
            "technology_choices": sorted(
                f"{item.topic}:{item.choice}" for item in architecture.technology_choices
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


def derive_contract(
    request: ProjectRequest,
    plan: DurablePlan | None,
    *,
    project_run_id: UUID,
    initial_revision: str,
) -> ProjectContract:
    """Deriva el contrato inmutable del proyecto y le fija su huella.

    Es una función pura de sus argumentos: mismos argumentos, mismo contrato salvo ``contract_id`` y
    ``created_at`` —que son identidad y sello del artefacto, no términos— y siempre la misma huella.
    Esa pureza no es estética: el kernel deriva el contrato al iniciar y vuelve a derivarlo tras una
    caída en otro proceso, y las dos derivaciones tienen que declarar la misma huella o el proyecto
    se bloquearía solo.

    De dónde sale cada término:

    - ``original_goal``: el objetivo de la petición, recortado a ``MAX_REPLAN_TEXT_CHARS``. No se
      reescribe ni se resume: es el enunciado del encargo;
    - ``acceptance_criteria``: lo que la petición declare en ``acceptance_criteria`` si su contrato
      lo declara; si no, los criterios de las tareas del plan durable, sin vacíos, sin repetidos y
      en orden declarado. El ``ProjectRequest`` de esta fase **no** declara ese campo, así que la
      fuente real es el plan: es el plan el que dice qué trabajo se autorizó;
    - ``acceptance_criterion_ids``: :func:`criterion_ids` sobre el número de criterios, para que la
      identidad sea posicional y estable;
    - ``authorized_scope``: lo que la petición declare en ``changed_files`` si su contrato lo
      declara; si no, la unión de los ``allowed_files`` de las tareas del plan, normalizada con
      :func:`punto.common.normalize_path`, sin vacíos y sin repetidos. Un plan sin tareas deja el
      alcance vacío: no se inventa una ruta;
    - ``protected_paths``: :func:`_protected_paths` sobre ese alcance;
    - ``risk_ceiling`` y ``authority_ceiling``: los de la petición, tal cual. Elevarlos aquí sería
      ampliar la autorización sin que nadie la aprobara;
    - ``initial_revision``: el argumento, no ``request.initial_revision``: el kernel resuelve la
      revisión real del workspace y una petición con revisión vacía significa «la que tenga».

    Raises:
        ProjectContractError: si la petición no declara objetivo. Sin objetivo no hay contrato que
            comparar —una propuesta no podría demostrar que sigue haciendo el mismo trabajo— y el
            fallo se dice aquí en vez de dejar que reviente la validación del modelo.
    """
    goal = _bounded(request.objective, MAX_REPLAN_TEXT_CHARS)
    if not goal.strip():
        raise ProjectContractError(
            "la petición no declara objetivo: un contrato sin objetivo no permite comprobar que "
            "una propuesta siga haciendo el trabajo autorizado, y el contrato se deriva antes de "
            "abrir ninguna replanificación"
        )
    criteria = _criterion_texts(
        _declared(request, "acceptance_criteria", plan, "acceptance_criteria")
    )
    scope = _scope_paths(_declared(request, "changed_files", plan, "allowed_files"))
    baseline = architecture_baseline(None if plan is None else plan.architecture)
    envelope = project_resource_envelope(
        None if plan is None else plan.architecture,
        capability_entries=_capability_entries(plan),
    )
    contract = ProjectContract(
        project_run_id=project_run_id,
        project_id=request.project_id,
        original_goal=goal,
        acceptance_criteria=criteria,
        acceptance_criterion_ids=criterion_ids(len(criteria)),
        authorized_scope=scope,
        protected_paths=_protected_paths(scope),
        risk_ceiling=request.risk,
        authority_ceiling=request.authority,
        initial_revision=_bounded(initial_revision, _MAX_REVISION_CHARS),
        authorized_resources=envelope.tokens,
        resource_envelope_fingerprint=resource_envelope_fingerprint(envelope),
        **baseline,
    )
    return contract.model_copy(update={"contract_fingerprint": contract_fingerprint(contract)})


def _capability_entries(plan: DurablePlan | None) -> tuple[tuple[str, str], ...]:
    """Pares ``(familia, valor)`` del perfil de capacidades aceptado, para el envelope.

    El perfil lo produce la etapa de arquitectura y viaja en el plan durable; si el plan no lo trae,
    el envelope se deriva solo de la arquitectura. Nunca se toma del Planner de la replanificación.
    """
    profile = None if plan is None else plan.capability_profile
    if profile is None:
        return ()
    return tuple((kind.value, value) for kind, value in profile.entries())


def publish_contract(
    store: ArtifactStore, *, request: ProjectRequest, contract: ProjectContract
) -> ArtifactReference:
    """Publica el contrato como artefacto JSON y devuelve su referencia durable.

    Imita el patrón del handoff del proyecto (``punto.project.handoff._put``): se escribe en el
    espacio del **run del proyecto** —``project_run_id_for(request)``, no el de ningún child—, con
    rol ``PLANNER`` y paso 0. El contrato es un artefacto del proyecto y tiene que seguir ahí cuando
    el child que lo motivó ya terminó; una reanudación lo resuelve desde su referencia.

    El JSON se serializa con las claves ordenadas y sin espacios decorativos, así que el mismo
    contrato produce siempre los mismos bytes: el digest es reproducible y republicar el mismo
    contrato repite el artefacto en vez de crear una segunda verdad.

    La huella se publica **como llegó**, no se recalcula: el publicador no decide el contrato, solo
    lo deja escrito. Recalcular aquí crearía una segunda fuente de verdad y haría que un contrato
    derivado por otra ruta —por ejemplo con una revisión inicial distinta— se publicara con una
    huella que no es la suya, justo lo contrario de lo que hace falta para auditar.
    """
    return store.put(
        workflow_id=project_run_id_for(request),
        role=RoleName.PLANNER,
        step_index=_CONTRACT_STEP_INDEX,
        kind=PROJECT_CONTRACT_KIND,
        label=CONTRACT_LABEL,
        data=_encode(contract.model_dump(mode="json")),
    )


def resolve_contract(store: ArtifactStore, reference: ArtifactReference) -> ProjectContract | None:
    """Contrato de una referencia de tipo :data:`PROJECT_CONTRACT_KIND`, o ``None``.

    ``None`` significa «esta referencia no es un contrato», que es el caso normal cuando la lista de
    referencias del run trae artefactos de otros tipos: distinguir «no es de este tipo» de «está
    roto» importa, porque lo primero se resuelve mirando la siguiente referencia y lo segundo tiene
    que fallar ruidosamente.

    Un contrato presente pero que no se puede recuperar del almacén, no es UTF-8, no es JSON o no
    valida contra :class:`~punto.schemas.replan.ProjectContract` **no** se degrada a ``None``: es
    corrupción y se reporta como :class:`ProjectContractError` encadenando el error original para no
    perder el motivo real. Devolver ``None`` ahí haría que el proyecto siguiera sin contrato y que
    una propuesta se evaluara contra la nada.
    """
    if reference.kind != PROJECT_CONTRACT_KIND:
        return None
    raw = _read_payload(store, reference)
    try:
        return ProjectContract.model_validate(raw)
    except ValidationError as error:
        raise ProjectContractError(
            f"el contrato {reference.reference!r} no valida contra ProjectContract: {error}"
        ) from error


def assert_contract_unchanged(
    current: ProjectContract, candidate: ProjectContract
) -> tuple[str, ...]:
    """Términos que ``candidate`` cambiaría respecto de ``current``, en orden fijo.

    Una tupla vacía significa «el contrato está intacto» y es la única respuesta que permite seguir
    adelante con una replanificación autónoma. Cualquier otra se declara como
    ``PROJECT_REPLAN_CONTRACT_CHANGE_REQUIRED``: el contrato no se renegocia desde dentro, así que
    la propuesta se rechaza (o se para y se pide una persona).

    Se comparan los ocho términos de :data:`_CONTRACT_TERM_FIELDS` —objetivo, criterios con sus
    identidades, alcance, rutas protegidas, techos y revisión inicial— y **no** la identidad del
    artefacto, su sello ni el proyecto: un contrato derivado en otro proceso tiene otro
    ``contract_id`` y otro ``created_at``, y tratarlos como un cambio haría que ninguna reanudación
    pudiera continuar. La huella tampoco se compara campo a campo: es derivada de estos términos, y
    comparar el derivado en lugar de los términos ocultaría **qué** cambió.
    """
    return tuple(
        name for name in _CONTRACT_TERM_FIELDS if getattr(current, name) != getattr(candidate, name)
    )


def scope_violations(contract: ProjectContract, paths: Sequence[str]) -> tuple[str, ...]:
    """Rutas que se salen del alcance autorizado o que caen en una ruta protegida.

    Una tupla vacía significa «todo dentro del contrato». Una ruta es violación por **cualquiera**
    de los dos motivos, y el segundo se comprueba a propósito aunque la ruta esté en el alcance: que
    un plan autorice una ruta protegida no la desprotege —el piso constitucional no depende de lo
    que el plan declare—, y esa combinación es exactamente la que el contrato existe para detectar.

    La comparación usa el **normalizador canónico** (:func:`punto.common.normalize_path`), el mismo
    que usan el Policy Engine y el guardián de reparaciones, así que ``src\\\\a.py``, ``./src/a.py``
    y ``SRC/A.PY`` son la misma ruta. La contención es por **igualdad exacta o prefijo de
    directorio** (``alcance`` contiene ``alcance/…``), que es la regla del resto del motor: una
    entrada del alcance declara un archivo o un árbol, y no hay tercer caso. Los globs **no** se
    interpretan aquí: el alcance del contrato es una lista de rutas, y una entrada con ``*`` sería
    un defecto del plan que conviene que no autorice nada en vez de autorizar de más.

    Las violaciones se devuelven **normalizadas**, en el orden en que llegaron y sin repetir
    ninguna: la lista es el detalle de un rechazo y un detalle repetido no añade nada. Las rutas
    vacías se ignoran: no son rutas, y marcarlas como violación convertiría un dato ausente en un
    defecto de quien llama.
    """
    scope = _normalized_scope(contract.authorized_scope)
    protected = _normalized_scope(contract.protected_paths)
    violations: list[str] = []
    seen: set[str] = set()
    for path in paths:
        normalized = normalize_path(path)
        if not normalized or normalized in seen:
            continue
        if _contains(scope, normalized) and not _contains(protected, normalized):
            continue
        seen.add(normalized)
        violations.append(normalized)
    return tuple(violations)


def criterion_coverage(contract: ProjectContract, covered: Sequence[str]) -> tuple[str, ...]:
    """Identidades de criterio del contrato que ``covered`` **no** menciona.

    Una tupla vacía significa «cobertura completa»: cada criterio global del contrato queda
    demostrado por algún trabajo declarado. Cualquier otra es cobertura incompleta, y la cobertura
    no se acepta a medias porque el contrato es el encargo entero: replanificar y perder un criterio
    por el camino sería cerrar el proyecto habiendo hecho otro trabajo.

    La comparación es por **identidad** (``AC-2``), no por texto: el enunciado de un criterio se
    lee, la identidad se cita, y aceptar el texto como cobertura haría que una paráfrasis contara
    como prueba. Los identificadores llegan con espacios sobrantes —los escribe un modelo— y se
    recortan; el resto se compara literal. El orden de la respuesta es el de declaración del
    contrato, sin repetir ninguno.
    """
    present = {item.strip() for item in covered if item.strip()}
    return tuple(
        identifier
        for identifier in dict.fromkeys(contract.acceptance_criterion_ids)
        if identifier not in present
    )


# ---------------------------------------------------------------------------
# Interno
# ---------------------------------------------------------------------------
def _declared(
    request: ProjectRequest, request_field: str, plan: DurablePlan | None, plan_field: str
) -> tuple[str, ...]:
    """Valores declarados para un término: primero los de la petición, después los del plan.

    La precedencia es deliberada. La petición es la autorización explícita de quien encarga el
    trabajo; el plan es la traducción de esa autorización en tareas. Si la petición declara el
    término, manda ella; si no lo declara —y el ``ProjectRequest`` de esta fase no declara ni
    criterios ni alcance—, se usa el plan, que es lo que el proyecto ejecuta de verdad.

    La lectura es defensiva a propósito: ``getattr`` con respaldo vacío permite que un
    ``ProjectRequest`` al que se le añada el campo más adelante —o que llegue construido con él por
    una migración— alimente el contrato sin cambiar una línea de este módulo. Si no hay ninguna de
    las dos fuentes, se devuelve vacío: no se inventa ni un criterio ni una ruta.
    """
    from_request = _as_texts(getattr(request, request_field, ()))
    if from_request:
        return from_request
    if plan is None:
        return ()
    collected: list[str] = []
    for task in plan.task_graph.tasks:
        collected.extend(_as_texts(getattr(task, plan_field, ())))
    return tuple(collected)


def _as_texts(values: object) -> tuple[str, ...]:
    """Vista textual de una colección declarada, o vacío si lo recibido no es una colección."""
    if not isinstance(values, list | tuple):
        return ()
    return tuple(item for item in values if isinstance(item, str))


def _criterion_texts(values: Sequence[str]) -> tuple[str, ...]:
    """Criterios globales: sin vacíos, sin repetidos, en orden declarado y acotados al contrato.

    El vacío se descarta porque un criterio sin texto no es un criterio: darle una identidad
    ``AC-n`` prometería una comprobación que nadie puede leer. El repetido también, porque dos
    identidades para el mismo enunciado inflarían la cobertura declarada sin añadir trabajo. El
    recorte es por la cola —el orden declarado es información— y a ``MAX_REPLAN_COVERAGE``, que es
    la cota del modelo: no se construye un contrato que el contrato no admita.
    """
    return _unique(values, normalize=lambda text: text.strip())


def _scope_paths(values: Sequence[str]) -> tuple[str, ...]:
    """Alcance autorizado: rutas canónicas, sin vacías, sin repetidas y acotado al contrato.

    La normalización es la canónica del motor, no una propia: ``tests\\\\test_a.py`` y
    ``./tests/test_a.py`` son la misma ruta que ``tests/test_a.py``, y comparar contra una forma sin
    normalizar dejaría pasar la misma ruta escrita de otra manera.
    """
    return _unique(values, normalize=normalize_path)


def _unique(values: Sequence[str], *, normalize: Callable[[str], str]) -> tuple[str, ...]:
    """Valores normalizados, sin vacíos ni repetidos, en orden declarado y acotados.

    Recibe el normalizador como argumento para que criterios y rutas compartan la misma regla de
    deduplicación —la primera aparición manda y el orden declarado se conserva— sin compartir la
    transformación, que es lo único que los distingue.
    """
    kept: list[str] = []
    seen: set[str] = set()
    for value in values:
        candidate = normalize(value)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        kept.append(candidate)
        if len(kept) == MAX_REPLAN_COVERAGE:
            break
    return tuple(kept)


def _protected_paths(scope: Sequence[str]) -> tuple[str, ...]:
    """Rutas protegidas del contrato: el piso constitucional más las protegidas del alcance.

    Se incluye **siempre** el piso constitucional
    (:data:`punto.policy.permissions.CONSTITUTIONAL_PROTECTED_PATHS`), esté o no en el alcance: la
    protección no depende de lo que un plan declare, y un contrato que la omitiera dejaría que una
    propuesta reclamara como propio un archivo que gobierna la autoridad del motor. Además se
    pregunta a :func:`~punto.policy.permissions.is_protected_path` por cada ruta del alcance, porque
    el piso reconoce la ruta declarada con cualquier prefijo (``src/config/permissions.yaml``
    también es el archivo protegido).

    Las rutas que no se puedan consultar no se inventan: la única fuente es el alcance declarado y
    el piso del código, y si el alcance viene vacío, el contrato se queda con el piso. El resultado
    se ordena para que la huella no dependa del orden en que el plan declaró las rutas.
    """
    found: set[str] = set()
    for raw in CONSTITUTIONAL_PROTECTED_PATHS:
        normalized = normalize_path(raw)
        if normalized:
            found.add(normalized)
    for raw in scope:
        if is_protected_path(raw):
            normalized = normalize_path(raw)
            if normalized:
                found.add(normalized)
    return tuple(sorted(found))[:MAX_REPLAN_COVERAGE]


def _normalized_scope(entries: Sequence[str]) -> tuple[str, ...]:
    """Rutas de una colección del contrato, normalizadas y sin vacíos, conservando el orden."""
    return tuple(entry for entry in (normalize_path(item) for item in entries) if entry)


def _contains(entries: Sequence[str], path: str) -> bool:
    """``True`` si ``path`` (ya normalizado) es una entrada o cuelga de ella.

    La regla es la del resto del motor: igualdad exacta o prefijo de **directorio**, sin interpretar
    globs. La barra se añade al comparar para que ``src/modulo`` no contenga ``src/modulo_otro.py``,
    que es el error clásico de una comparación por prefijo de cadena.
    """
    return any(path == entry or path.startswith(f"{entry.rstrip('/')}/") for entry in entries)


def _encode(payload: dict[str, object]) -> bytes:
    """Serializa un payload a JSON canónico en UTF-8, sin escapes innecesarios."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _read_payload(store: ArtifactStore, reference: ArtifactReference) -> object:
    """Lee y decodifica el JSON de un artefacto del proyecto, o falla con el motivo real.

    Traduce a :class:`ProjectContractError` los tres fallos que impiden resolver el artefacto —no
    poder recuperarlo del almacén, no ser UTF-8, no ser JSON— encadenando el error original: en esta
    capa «el contrato durable del proyecto no se resuelve» es **una** condición, y decirlo con el
    motivo real es lo que permite distinguir un artefacto ausente de uno manipulado.
    """
    try:
        data = store.get(reference)
    except WorkflowError as error:
        raise ProjectContractError(
            f"no se pudo recuperar el contrato {reference.reference!r} "
            f"(kind={reference.kind!r}) del almacén {reference.store!r}: {error}"
        ) from error
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProjectContractError(
            f"el contrato {reference.reference!r} no es UTF-8: {error}"
        ) from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ProjectContractError(
            f"el contrato {reference.reference!r} no lleva JSON válido: {error}"
        ) from error


def _bounded(text: str, max_chars: int) -> str:
    """Recorta ``text`` a ``max_chars`` sin adornos, para campos que el contrato ya acota.

    No se añade marca de recorte porque el texto va a un campo del contrato que no la admite y
    porque el recorte está documentado en la función que lo aplica. Nunca devuelve más caracteres de
    los pedidos, ni siquiera cuando el límite es cero.
    """
    return text if len(text) <= max_chars else text[:max_chars]


__all__ = [
    "CONTRACT_LABEL",
    "CRITERION_ID_PREFIX",
    "PROJECT_CONTRACT_KIND",
    "ProjectContractError",
    "architecture_baseline",
    "architecture_fingerprint",
    "assert_contract_unchanged",
    "contract_fingerprint",
    "criterion_coverage",
    "criterion_ids",
    "derive_contract",
    "publish_contract",
    "resolve_contract",
    "scope_violations",
]
