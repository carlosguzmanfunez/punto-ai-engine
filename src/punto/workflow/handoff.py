"""Handoff estructurado y durable entre etapas, en el camino real del motor (ENGINE-6.0.2).

El problema que resuelve
------------------------
``punto.workflow.artifacts`` (ENGINE-6.0.1) dejó la **infraestructura**: un puerto de almacén
estable, una implementación en disco, el registro de etapas en el run y una vista de contexto. Lo
que faltaba era el **camino real**: los adaptadores de rol no publicaban nada, así que el handoff
durable solo se demostraba con ejecutores de prueba que guardaban el diseño a mano. Este módulo es
la capa de producción que cierra ese hueco (defecto V602-03), y tiene tres piezas:

- un **códec** versionado de los dos artefactos que el workflow se pasa de una etapa a otra: el
  :class:`~punto.architect.base.ArchitectureOutcome` del Architect y el **bundle** del plan que
  produce el Planner;
- su **publicador y resolutor** contra un :class:`ArtifactStore`, que es el único punto por el que
  entran y salen bytes;
- :func:`developer_input`, el constructor **oficial y determinista** de la entrada del rol
  ``DEVELOPER`` a partir del plan durable, para que el handoff no dependa de una *closure* que solo
  existe en el proceso que ejecutó la etapa anterior.

Por qué un sobre versionado y no el objeto serializado a pelo
------------------------------------------------------------
El contenido del almacén es **contrato**: lo escribe un proceso y lo lee otro que puede ser de otra
versión del motor. Por eso cada artefacto es un objeto JSON con ``schema_version`` y ``kind``
explícitos, serializado con pydantic (``model_dump(mode="json")``) y **nunca** con ``pickle``: un
payload que declare otro esquema o que se referencie con el tipo equivocado se rechaza con
:class:`~punto.workflow.errors.WorkflowResumeFailedError` en vez de interpretarse a la buena de
Dios. El JSON se escribe con claves ordenadas y sin espacios decorativos, de modo que el mismo
contenido produce siempre los mismos bytes y el digest del almacén es reproducible.

Qué NO hace este módulo
-----------------------
No llama a ningún modelo, no abre red, no ejecuta subprocesos y no decide autoridad. Solo serializa
lo que una etapa ya produjo, lo deja en el almacén y lo reconstruye cuando la etapa siguiente lo
necesita. Tampoco re-ejecuta etapas: si un diseño o un plan no está en las referencias, la etapa que
lo necesitaba falla con un detalle explícito; volver a ejecutar al Architect o al Planner para
rellenar el hueco sería exactamente la duplicación que el kernel elimina.

Integridad
----------
Un artefacto manipulado (digest o tamaño que no cuadran) lo detecta
:meth:`punto.workflow.artifacts.FileArtifactStore.get`, que lanza su
``WorkflowCheckpointInvalidError``: este módulo **no** lo captura ni lo disfraza. Lo que sí traduce
es un payload ausente, ilegible o de otro esquema, porque eso es un fallo de reanudación y tiene su
propio código estable.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from pydantic import BaseModel, ValidationError

from punto.architect.base import ArchitectureOutcome
from punto.developer.context import ExecutionContext
from punto.planner.base import PlanningOutcome
from punto.schemas.enums import RiskLevel
from punto.schemas.execution import CommandSpec, DeveloperTask
from punto.schemas.planning import (
    ArchitecturePlan,
    ArchitectureProposal,
    ModelExecutionSummary,
    PlannedTask,
    ProjectCapabilityProfile,
    ProjectPlanStatus,
    ProjectSpec,
    Roadmap,
    TaskGraph,
)
from punto.schemas.workflow import ArtifactReference, RoleExecutionRequest, RoleName
from punto.workflow.artifacts import ArtifactStore
from punto.workflow.errors import WorkflowResumeFailedError

#: Versión del sobre que viaja en cada artefacto del handoff.
#:
#: Cambiarla invalida la lectura de artefactos escritos por otra versión del motor: por eso el
#: resolutor compara la versión declarada y falla en vez de adivinar el formato.
HANDOFF_SCHEMA_VERSION: Final[str] = "1.0.0"
#: Tipo del artefacto que publica la etapa ``ARCHITECT``: el ``ArchitectureOutcome`` completo.
ARCHITECTURE_KIND: Final[str] = "ARCHITECTURE"
#: Tipo del artefacto que publica la etapa ``PLANNER``: el bundle durable del plan.
PLAN_KIND: Final[str] = "PLANNING"

#: Nombres de los campos del sobre. Son constantes porque son contrato, no texto decorativo.
_SCHEMA_FIELD: Final[str] = "schema_version"
_KIND_FIELD: Final[str] = "kind"
#: Etiquetas legibles de cada artefacto. Describen el tipo, nunca el contenido.
_ARCHITECTURE_LABEL: Final[str] = "diseño del Architect"
_PLAN_LABEL: Final[str] = "plan durable del Planner"
#: Acción con la que se declara el trabajo del Developer.
#:
#: Ni ``RoleExecutionRequest`` ni ``PlannedTask`` declaran una acción, así que inventarla a partir
#: de las palabras del objetivo sería deducir autoridad de texto libre. Se declara la acción L0 que
#: el propio contrato de :class:`~punto.schemas.execution.DeveloperTask` trae por defecto para
#: modificar archivos, que es el trabajo que este rol hace.
_DEVELOPER_ACTION: Final[str] = "modify_file"
#: Prefijo obligatorio de las ramas de tarea del motor (espejo de ``punto.developer.context``).
_BRANCH_PREFIX: Final[str] = "ai/"
#: Slug con el que se nombra el trabajo cuando el objetivo no deja ni un carácter utilizable.
_DEFAULT_SLUG: Final[str] = "task"
#: Cotas locales de los textos derivados. No sustituyen a las del contrato: las respetan.
_MAX_SLUG_CHARS: Final[int] = 60
_MAX_COMMIT_SUBJECT_CHARS: Final[int] = 120
_MAX_ID_CHARS: Final[int] = 8
#: Marca de recorte explícita: un texto recortado en silencio se leería como completo.
_TRUNCATION_MARKER: Final[str] = "…"
#: Todo lo que no sea ``[a-z0-9]`` separa palabras en un slug.
_SLUG_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class DurablePlan:
    """Plan reconstruido desde el almacén: lo que la etapa ``DEVELOPER`` necesita para trabajar.

    ``roadmap`` y ``task_graph`` son obligatorios porque sin ellos no hay plan que resolver: un
    artefacto que no los traiga no es un plan a medias, es un plan inválido, y se rechaza. El resto
    de piezas vienen del diseño del Architect y son opcionales para que un bundle antiguo o parcial
    no impida leer lo esencial.
    """

    roadmap: Roadmap
    task_graph: TaskGraph
    project_spec: ProjectSpec | None = None
    architecture: ArchitecturePlan | None = None
    capability_profile: ProjectCapabilityProfile | None = None
    summary: ModelExecutionSummary | None = None


# ---------------------------------------------------------------------------
# Publicación
# ---------------------------------------------------------------------------
def publish_architecture(
    store: ArtifactStore, *, request: RoleExecutionRequest, outcome: ArchitectureOutcome
) -> ArtifactReference:
    """Publica el ``ArchitectureOutcome`` **completo** y devuelve su referencia durable.

    Se guarda el outcome entero —estado, propuesta, resumen de ejecución, violaciones y error— y no
    solo la propuesta: la etapa siguiente necesita saber si el diseño fue aceptado y por qué no lo
    fue cuando no lo fue. Un artefacto que guardara solo la propuesta perdería esa decisión y
    obligaría a reinterpretarla.

    Raises:
        ValueError: si la petición no es de la etapa ``ARCHITECT``. Etiquetar un artefacto con el
            rol equivocado corrompería el handoff, así que se rechaza antes de escribir nada.
    """
    _assert_role(request, RoleName.ARCHITECT, "publish_architecture")
    payload: dict[str, object] = {
        _KIND_FIELD: ARCHITECTURE_KIND,
        "status": outcome.status.value,
        "proposal": _json_or_none(outcome.proposal),
        "summary": _json_dump(outcome.summary),
        "violations": list(outcome.violations),
        "error": outcome.error,
    }
    return store.put(
        workflow_id=request.workflow_id,
        role=RoleName.ARCHITECT,
        step_index=request.step_index,
        kind=ARCHITECTURE_KIND,
        label=_ARCHITECTURE_LABEL,
        data=_encode(payload),
    )


def resolve_architecture(
    store: ArtifactStore, references: tuple[ArtifactReference, ...]
) -> ArchitectureOutcome | None:
    """Reconstruye el diseño a partir de la **primera** referencia ``ARCHITECTURE``.

    Devuelve ``None`` si la petición no trae ninguna: distinguir «no hay diseño» de «el diseño está
    roto» importa, porque lo primero es un hueco que la etapa debe declarar y lo segundo es
    corrupción que debe fallar ruidosamente. Si hay varias referencias del mismo tipo se usa la
    primera en orden cronológico, que es la que el run declaró antes.
    """
    for reference in references:
        if reference.kind != ARCHITECTURE_KIND:
            continue
        return _decode_architecture(store.get(reference), reference)
    return None


def publish_plan(
    store: ArtifactStore,
    *,
    request: RoleExecutionRequest,
    outcome: PlanningOutcome,
    architecture: ArchitectureOutcome | None = None,
) -> ArtifactReference:
    """Publica el **bundle durable** del plan y devuelve su referencia.

    El bundle lleva todo lo que el ``Developer`` necesita para reconstruir su entrada sin volver a
    planificar: estado y error del Planner, violaciones, roadmap, grafo de tareas y resumen de
    ejecución. ``PlanningOutcome`` no lleva la especificación, la arquitectura ni el perfil de
    capacidades —los produce el Architect—, así que se reciben en ``architecture`` y se publican
    con el plan. Sin ese diseño el bundle seguiría siendo resoluble, solo que más pobre: el plan
    siempre se resuelve, el diseño se añade cuando la etapa lo tiene a mano.

    Raises:
        ValueError: si la petición no es de la etapa ``PLANNER`` o si el outcome no trae roadmap y
            grafo de tareas. Sin esas dos piezas no hay plan durable que publicar, y guardar un
            bundle vacío daría por resoluble algo que no lo es.
    """
    _assert_role(request, RoleName.PLANNER, "publish_plan")
    if outcome.roadmap is None or outcome.task_graph is None:
        raise ValueError(
            "no se puede publicar un plan durable sin roadmap ni grafo de tareas: el outcome llegó "
            f"en {outcome.status.value} y sin esas dos piezas no hay plan que resolver"
        )
    design = None if architecture is None else architecture.proposal
    payload: dict[str, object] = {
        _KIND_FIELD: PLAN_KIND,
        "status": outcome.status.value,
        "error": outcome.error,
        "violations": list(outcome.violations),
        "roadmap": _json_dump(outcome.roadmap),
        "task_graph": _json_dump(outcome.task_graph),
        "summary": _json_dump(outcome.summary),
        "project_spec": _plan_part(outcome, design, "project_spec"),
        "architecture": _plan_part(outcome, design, "architecture"),
        "capability_profile": _plan_part(outcome, design, "capability_profile"),
    }
    return store.put(
        workflow_id=request.workflow_id,
        role=RoleName.PLANNER,
        step_index=request.step_index,
        kind=PLAN_KIND,
        label=_PLAN_LABEL,
        data=_encode(payload),
    )


def resolve_plan(
    store: ArtifactStore, references: tuple[ArtifactReference, ...]
) -> DurablePlan | None:
    """Reconstruye el plan durable a partir de la **primera** referencia ``PLANNING``.

    Devuelve ``None`` si no hay ninguna. Un bundle presente pero sin roadmap o sin grafo no se
    degrada a un plan parcial: se rechaza con ``WORKFLOW_RESUME_FAILED``, porque un plan a medias
    haría trabajar al Developer sobre una ficción.
    """
    for reference in references:
        if reference.kind != PLAN_KIND:
            continue
        payload = _decode(store.get(reference), expected_kind=PLAN_KIND, reference=reference)
        return DurablePlan(
            roadmap=_model_field(Roadmap, payload, "roadmap", reference),
            task_graph=_model_field(TaskGraph, payload, "task_graph", reference),
            project_spec=_optional_model_field(ProjectSpec, payload, "project_spec", reference),
            architecture=_optional_model_field(
                ArchitecturePlan, payload, "architecture", reference
            ),
            capability_profile=_optional_model_field(
                ProjectCapabilityProfile, payload, "capability_profile", reference
            ),
            summary=_optional_model_field(ModelExecutionSummary, payload, "summary", reference),
        )
    return None


# ---------------------------------------------------------------------------
# Entrada oficial del rol DEVELOPER
# ---------------------------------------------------------------------------
def developer_input(
    plan: DurablePlan, request: RoleExecutionRequest
) -> tuple[DeveloperTask, ExecutionContext]:
    """Construye la pareja ``(DeveloperTask, ExecutionContext)`` desde el plan durable.

    Es el constructor **oficial** de la entrada del Developer: el adaptador real lo usa sin ninguna
    *closure* externa, así que la entrada se puede reconstruir en un proceso nuevo a partir de las
    referencias del checkpoint y del almacén.

    Regla de precedencia, explícita porque es el contrato de esta función: **el plan manda cuando
    declara el dato y la petición es el respaldo declarado**, nunca una invención. Lo que la
    petición declara y el plan no lleva (identidad, workspace, acción) sale de la petición.

    Decisiones, una a una, y por qué son deterministas:

    - ``task_id``: el de la petición. Es la tarea que el motor asoció al paso; el plan no la lleva.
    - ``objective``: el de la primera tarea **lista** del grafo, que es la unidad de trabajo real de
      esta etapa. Si el plan no declara ninguna tarea lista se usa el objetivo de la petición. La
      tarea lista se elige por orden del plan (``ready_tasks`` conserva ese orden), así que el mismo
      plan produce siempre el mismo objetivo.
    - ``slug`` y ``commit_message``: derivados por función pura del objetivo (minúsculas, sin
      acentos, separadores colapsados). No dependen del reloj, del azar ni del proceso.
    - ``acceptance_criteria``: las de la tarea planificada; si no declara ninguna, las de la
      petición. Un criterio inventado haría verificar algo que nadie pidió.
    - ``context_files``: los de la tarea planificada. Si no declara, los ``changed_files`` de la
      petición, que es el alcance que el llamante ya declaró. Nunca el repositorio entero.
    - ``allowed_files``: los de la tarea planificada y, si no declara, el mismo conjunto que
      ``context_files`` (es la derivación que el propio contrato de ``DeveloperTask`` documenta).
    - ``risk_level``: el de la tarea planificada, calculado por PUNTO al planificar. Sin tarea lista
      se usa el valor por defecto del contrato.
    - ``validations``: los ``validation_checks`` declarados por el plan, partidos por espacios (el
      primer token es el ejecutable y el resto sus argumentos, que es la forma que exige
      ``CommandSpec``). No se añade ningún check: si el plan no declara ninguno, la lista va vacía.
    - ``files``, ``replacements`` y ``assertions``: vacíos. El plan durable no contiene contenido de
      archivos ni recetas: eso lo propone el runner del Developer, y fabricarlo aquí sería inventar
      código en la capa que solo transporta el plan.
    - ``workspace_path`` y ``task_id`` del contexto: los de la petición. El workspace declarado por
      el llamante es el único que el motor puede autorizar.
    - ``branch_name``: ``ai/<slug>-<8 primeros del task_id>``. Determinista y único por tarea: dos
      tareas distintas no comparten rama y el mismo caso produce siempre la misma.
    - ``allowed_commands``, ``max_files_changed``, ``max_execution_minutes``, ``max_cost_usd``,
      ``default_timeout_seconds`` y ``attempts_allowed``: los valores por defecto del contrato. El
      plan no declara presupuesto de ejecución por tarea y derivarlo de un contador inventado sería
      conceder límites que nadie aprobó.
    - ``trust_level``: el valor por defecto del contrato. La frontera de confianza la impone el
      runner que ejecute (``DeveloperRunner.resolve_backend``) y **no** la decide el handoff: quien
      ejecuta es quien conoce si el trabajo lo generó un modelo. Si el runner exige
      ``UNTRUSTED_MODEL`` y el contexto declara confianza local, el motor falla en cerrado en vez de
      ejecutar en el host; lo contrario —declarar desconfianza aquí— rompería los runners
      deterministas sin ganar ninguna garantía.

    Raises:
        WorkspaceViolationError: si el workspace declarado por la petición no existe. Es el error
            tipado de la capa de ejecución y se propaga tal cual: el handoff no lo disfraza.
    """
    task = _next_task(plan)
    objective = _coalesce(_one_line(_task_text(task, "objective")), _one_line(request.objective))
    objective = objective or _DEFAULT_SLUG
    slug = _slug(objective)
    context_files = _context_files(task, request)
    developer_task = DeveloperTask(
        task_id=request.task_id,
        objective=objective,
        slug=slug,
        action=_DEVELOPER_ACTION,
        risk_level=RiskLevel.LOW if task is None else task.risk_level,
        validations=_validations(task),
        commit_message=f"{slug}: {_excerpt(objective, _MAX_COMMIT_SUBJECT_CHARS)}",
        acceptance_criteria=_acceptance_criteria(task, request),
        context_files=context_files,
        allowed_files=_declared(task, "allowed_files") or context_files,
    )
    context = ExecutionContext(
        task_id=request.task_id,
        workspace_path=_workspace(request),
        branch_name=_branch_name(request, slug),
    )
    return developer_task, context


# ---------------------------------------------------------------------------
# Códec
# ---------------------------------------------------------------------------
def _encode(payload: Mapping[str, object]) -> bytes:
    """Serializa el sobre a JSON canónico en UTF-8, con la versión del esquema delante.

    ``sort_keys`` no es cosmético: fija el orden de las claves para que el mismo contenido produzca
    siempre los mismos bytes, que es lo que hace reproducible el digest del almacén.
    ``ensure_ascii`` se desactiva para que los acentos viajen como UTF-8 y no como escapes.
    """
    canonical = {_SCHEMA_FIELD: HANDOFF_SCHEMA_VERSION, **payload}
    return json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _decode(
    data: bytes, *, expected_kind: str, reference: ArtifactReference
) -> Mapping[str, object]:
    """Lee el sobre de un artefacto, o falla con ``WORKFLOW_RESUME_FAILED`` diciendo por qué.

    Se comprueban tres cosas antes de mirar el contenido: que los bytes sean UTF-8, que sean un
    objeto JSON y que la versión del esquema y el tipo declarado sean los que esta capa entiende.
    Un payload de otro esquema no se interpreta «lo mejor posible»: se rechaza, porque adivinar el
    formato de otra versión es la forma clásica de leer datos que significan otra cosa.
    """
    where = f"el artefacto {reference.reference!r} (kind={reference.kind!r})"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkflowResumeFailedError(f"{where} no es UTF-8: {error}") from error
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise WorkflowResumeFailedError(f"{where} no es JSON válido: {error}") from error
    if not isinstance(payload, dict):
        raise WorkflowResumeFailedError(
            f"{where} no lleva un objeto JSON sino {type(payload).__name__}: no hay sobre legible"
        )
    version = payload.get(_SCHEMA_FIELD)
    if version != HANDOFF_SCHEMA_VERSION:
        raise WorkflowResumeFailedError(
            f"{where} declara esquema {version!r} y esta capa entiende "
            f"{HANDOFF_SCHEMA_VERSION!r}: no se interpreta un sobre de otra versión"
        )
    declared_kind = payload.get(_KIND_FIELD)
    if declared_kind != expected_kind:
        raise WorkflowResumeFailedError(
            f"{where} se referencia como {expected_kind!r} y su sobre declara "
            f"{declared_kind!r}: la referencia y el contenido no coinciden"
        )
    return cast("Mapping[str, object]", payload)


def _decode_architecture(data: bytes, reference: ArtifactReference) -> ArchitectureOutcome:
    """Reconstruye el ``ArchitectureOutcome`` completo desde su sobre."""
    payload = _decode(data, expected_kind=ARCHITECTURE_KIND, reference=reference)
    return ArchitectureOutcome(
        status=_status_field(payload, reference),
        proposal=_optional_model_field(ArchitectureProposal, payload, "proposal", reference),
        summary=_optional_model_field(ModelExecutionSummary, payload, "summary", reference)
        or ModelExecutionSummary(),
        violations=_texts_field(payload, "violations", reference),
        error=_text_field(payload, "error", reference),
    )


def _status_field(payload: Mapping[str, object], reference: ArtifactReference) -> ProjectPlanStatus:
    """Estado declarado por el sobre, o ``WORKFLOW_RESUME_FAILED`` si no es un estado del motor."""
    raw = payload.get("status")
    if not isinstance(raw, str):
        raise WorkflowResumeFailedError(
            f"el artefacto {reference.reference!r} declara el estado {raw!r}, que no es el texto "
            "de ningún estado de planificación de PUNTO"
        )
    try:
        return ProjectPlanStatus(raw)
    except ValueError as error:
        raise WorkflowResumeFailedError(
            f"el artefacto {reference.reference!r} declara el estado {raw!r}, que no es un estado "
            f"de planificación de PUNTO: {error}"
        ) from error


def _text_field(payload: Mapping[str, object], field: str, reference: ArtifactReference) -> str:
    """Campo de texto del sobre, o ``WORKFLOW_RESUME_FAILED`` si no es texto."""
    raw = payload.get(field)
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise WorkflowResumeFailedError(
            f"el campo {field!r} de {reference.reference!r} no es texto sino {type(raw).__name__}"
        )
    return raw


def _texts_field(
    payload: Mapping[str, object], field: str, reference: ArtifactReference
) -> tuple[str, ...]:
    """Colección de textos del sobre, o ``WORKFLOW_RESUME_FAILED`` si algún elemento no es texto."""
    raw = payload.get(field)
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise WorkflowResumeFailedError(
            f"el campo {field!r} de {reference.reference!r} no es una lista sino "
            f"{type(raw).__name__}"
        )
    texts: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise WorkflowResumeFailedError(
                f"el campo {field!r} de {reference.reference!r} lleva un elemento que no es texto "
                f"sino {type(item).__name__}"
            )
        texts.append(item)
    return tuple(texts)


def _model_field[ModelT: BaseModel](
    model_type: type[ModelT],
    payload: Mapping[str, object],
    field: str,
    reference: ArtifactReference,
) -> ModelT:
    """Valida un campo obligatorio del sobre contra su modelo, o falla con el motivo real."""
    return _validate(model_type, payload.get(field), field, reference)


def _optional_model_field[ModelT: BaseModel](
    model_type: type[ModelT],
    payload: Mapping[str, object],
    field: str,
    reference: ArtifactReference,
) -> ModelT | None:
    """Valida un campo opcional del sobre, o ``None`` si el sobre no lo trae."""
    if payload.get(field) is None:
        return None
    return _validate(model_type, payload.get(field), field, reference)


def _validate[ModelT: BaseModel](
    model_type: type[ModelT], raw: object, field: str, reference: ArtifactReference
) -> ModelT:
    """Valida ``raw`` contra ``model_type`` traduciendo el fallo de pydantic a su código estable."""
    try:
        return model_type.model_validate(raw)
    except ValidationError as error:
        raise WorkflowResumeFailedError(
            f"el campo {field!r} de {reference.reference!r} no valida contra "
            f"{model_type.__name__}: {error}"
        ) from error


# ---------------------------------------------------------------------------
# Serialización de modelos
# ---------------------------------------------------------------------------
def _json_dump(model: BaseModel) -> dict[str, object]:
    """Serializa un modelo pydantic a un objeto JSON de tipos simples."""
    return cast("dict[str, object]", model.model_dump(mode="json"))


def _json_or_none(model: BaseModel | None) -> dict[str, object] | None:
    """Serializa un modelo pydantic, o ``None`` si no hay modelo que serializar."""
    return None if model is None else _json_dump(model)


def _plan_part(
    outcome: PlanningOutcome, design: ArchitectureProposal | None, name: str
) -> dict[str, object] | None:
    """Parte del diseño dentro del bundle del plan.

    Precedencia: lo que declare el propio outcome —si una versión futura lo lleva— y, si no, la
    parte correspondiente del diseño del Architect. Sin diseño y sin declaración, el campo viaja a
    ``None``: un hueco declarado es mejor que una pieza inventada.
    """
    declared = getattr(outcome, name, None)
    if isinstance(declared, BaseModel):
        return _json_dump(declared)
    if design is None:
        return None
    return _json_or_none(getattr(design, name, None))


# ---------------------------------------------------------------------------
# Derivaciones deterministas
# ---------------------------------------------------------------------------
def _next_task(plan: DurablePlan) -> PlannedTask | None:
    """Primera tarea lista del grafo, en el orden del plan, o ``None`` si no hay ninguna.

    ``ready_tasks`` conserva el orden en que el Planner declaró las tareas, así que el mismo plan
    elige siempre la misma tarea: la decisión no depende del reloj ni del proceso que la lee.
    """
    ready = plan.task_graph.ready_tasks()
    return ready[0] if ready else None


def _task_text(task: PlannedTask | None, field: str) -> str:
    """Campo de texto de la tarea planificada, o cadena vacía si no hay tarea o no es texto."""
    value = getattr(task, field, None)
    return value if isinstance(value, str) else ""


def _declared(task: PlannedTask | None, field: str) -> tuple[str, ...]:
    """Colección de textos declarada por la tarea planificada, o tupla vacía."""
    value = getattr(task, field, None)
    if isinstance(value, (list, tuple)):
        return tuple(item for item in value if isinstance(item, str) and item)
    return ()


def _context_files(task: PlannedTask | None, request: RoleExecutionRequest) -> tuple[str, ...]:
    """Ficheros de contexto: los del plan y, si no declara, los ``changed_files`` de la petición."""
    declared = _declared(task, "context_files")
    if declared:
        return declared
    return tuple(request.changed_files)


def _acceptance_criteria(
    task: PlannedTask | None, request: RoleExecutionRequest
) -> tuple[str, ...]:
    """Criterios de aceptación: los del plan y, si no declara, los de la petición."""
    declared = _declared(task, "acceptance_criteria")
    if declared:
        return declared
    return tuple(request.acceptance_criteria)


def _validations(task: PlannedTask | None) -> tuple[CommandSpec, ...]:
    """Checks de validación del plan como ``CommandSpec``, sin inventar ninguno.

    PUNTO declara los checks como texto (``pytest``, ``mypy --strict``). ``CommandSpec`` exige un
    ejecutable y sus argumentos, así que el texto se parte por espacios: el primer token es el
    ejecutable y el resto los argumentos. La allowlist del contexto decide después si ese ejecutable
    puede correr; aquí no se filtra ni se sustituye nada.
    """
    specs: list[CommandSpec] = []
    for check in _declared(task, "validation_checks"):
        parts = check.split()
        if not parts:
            continue
        specs.append(CommandSpec(name=check, executable=parts[0], args=tuple(parts[1:])))
    return tuple(specs)


def _workspace(request: RoleExecutionRequest) -> Path:
    """Workspace declarado por la petición, o el directorio actual si la petición no lo declara."""
    return Path(request.workspace_path) if request.workspace_path.strip() else Path()


def _branch_name(request: RoleExecutionRequest, slug: str) -> str:
    """Rama de tarea determinista: ``ai/<slug>-<8 primeros del task_id>``.

    El prefijo es el que la capa de ejecución exige para permitir escrituras y el sufijo sale del
    ``task_id`` para que dos tareas con el mismo objetivo no compartan rama. Nada de reloj ni de
    contador de proceso: el mismo caso produce siempre la misma rama.
    """
    short = str(request.task_id).replace("-", "")[:_MAX_ID_CHARS]
    return f"{_BRANCH_PREFIX}{slug}-{short}"


def _slug(objective: str) -> str:
    """Slug determinista del objetivo: minúsculas, sin acentos y con separadores colapsados.

    La normalización es una función pura del texto: ``unicodedata.normalize`` descompone los
    acentos, se descarta lo que no sea ASCII, todo lo que no sea ``[a-z0-9]`` se convierte en un
    único ``-`` y se recorta a la cota. Sin objetivo utilizable se devuelve el slug por defecto.
    """
    decomposed = unicodedata.normalize("NFKD", objective)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    collapsed = _SLUG_SEPARATOR_RE.sub("-", ascii_only.lower()).strip("-")
    return collapsed[:_MAX_SLUG_CHARS].strip("-") or _DEFAULT_SLUG


def _one_line(text: str) -> str:
    """Primera línea del texto, sin espacios sobrantes, o cadena vacía si no hay texto.

    Se toma la primera línea porque un objetivo de varias líneas no cabe en un asunto de commit, y
    recortar por la mitad dejaría un texto que parece completo sin serlo.
    """
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped.splitlines()[0].strip()


def _excerpt(value: str, limit: int) -> str:
    """Recorta un texto al límite dejando una marca explícita de recorte."""
    if len(value) <= limit:
        return value
    if limit <= len(_TRUNCATION_MARKER):
        return value[:limit]
    return value[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def _coalesce(*values: str) -> str:
    """Primer valor no vacío, o cadena vacía si no hay ninguno."""
    for value in values:
        if value:
            return value
    return ""


def _assert_role(request: RoleExecutionRequest, expected: RoleName, function: str) -> None:
    """Exige que la petición sea de la etapa que publica el artefacto.

    Raises:
        ValueError: si el rol no coincide. Un artefacto publicado por (o para) otro rol quedaría
            etiquetado con una etapa que no lo produjo, y el handoff dejaría de ser trazable.
    """
    if request.role is not expected:
        raise ValueError(
            f"{function} publica el artefacto de {expected.value} y la petición es de "
            f"{request.role.value}: el handoff no puede atribuirse a otro rol"
        )


__all__ = [
    "ARCHITECTURE_KIND",
    "HANDOFF_SCHEMA_VERSION",
    "PLAN_KIND",
    "DurablePlan",
    "developer_input",
    "publish_architecture",
    "publish_plan",
    "resolve_architecture",
    "resolve_plan",
]
