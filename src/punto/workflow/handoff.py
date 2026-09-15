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

Cobertura de toda la pipeline (ENGINE-6.0.3)
--------------------------------------------
Hasta 6.0.2 el handoff durable solo cubría Architect, Planner y Developer: QA, Security, Reviewer,
la auditoría cruzada y Visual QA seguían exigiendo un ``build_input`` externo —una *closure* que
captura objetos del proceso anterior—, así que un proceso nuevo no podía reconstruir esas etapas
(defecto V603-04). Aquí se cierran con dos piezas por rol: el **códec** de su informe (``publish_*``
y ``resolve_*``) y el **constructor oficial de su entrada** (``qa_input``, ``security_input``,
``review_input``, ``cross_audit_input`` y ``visual_qa_input``), que se apoyan en el plan durable y
en los informes ya publicados en vez de en la memoria del proceso que ejecutó la etapa anterior.
Visual QA añade una pieza más, porque su entrada no la produce ningún rol: la especificación visual
y la sesión técnica medida en un navegador viajan juntas en el sobre ``VISUAL_EVIDENCE``
(:func:`publish_visual_evidence` y :func:`resolve_visual_evidence`), y el adaptador resuelve esa
pareja antes de llamar a :func:`visual_qa_input`.

Bytes de las capturas (ENGINE-6.0.4, V604-02)
---------------------------------------------
La evidencia visual viajaba descrita pero no **medida**: el sobre ``VISUAL_EVIDENCE`` lleva la
especificación y la sesión web —nombre lógico, ruta, viewport, tamaño y sha256 de cada captura—,
nunca los bytes, así que una sesión con capturas verificadas en un navegador real no se podía
reconstruir en un proceso nuevo. Este módulo lo cierra con dos tipos más: cada captura se publica
como su propio artefacto (``SCREENSHOT``) con los bytes exactos, y el manifiesto
(``SCREENSHOT_MANIFEST``) guarda, por captura, su evidencia canónica y la referencia de esos bytes.
El JSON del manifiesto no lleva binarios —solo metadatos y referencias—, y cada captura declarada
por la sesión tiene que estar entera y verificada, o el resolutor falla con
``WORKFLOW_INCOMPLETE_EVIDENCE`` en vez de entregar un mapa a medias.

Identidad de la sesión en el manifiesto (ENGINE-6.0.5, V605-06)
--------------------------------------------------------------
El manifiesto ya guardaba la identidad de la sesión que midió las capturas (``task_id``,
``project_id`` e ``id``), pero el resolutor no la comparaba con el
:class:`~punto.schemas.web.WebSessionReport` que recibe: un manifiesto de **otro replay** —mismo
proyecto, mismas capturas y los mismos nombres lógicos— se resolvía como si fuera el de la sesión
pedida, y Visual QA habría analizado la evidencia de otra ejecución creyendo analizar la suya. Ahora
:func:`resolve_screenshots` compara las tres identidades **antes** de tocar ningún byte y falla con
``WORKFLOW_INCOMPLETE_EVIDENCE`` diciendo qué identidad no cuadra y qué se esperaba. La evidencia de
otro replay es un hueco de evidencia, nunca una aprobación.

Dos reglas gobiernan lo que viaja en esos sobres, y conviene leerlas antes de tocar el códec:

- **nada de secretos**: el almacén es disco y un volcado de error puede traer la cabecera de
  autenticación de un proveedor, así que todo texto del sobre pasa por una redacción de valores con
  forma de credencial antes de escribirse;
- **nada ilimitado**: ningún texto del sobre supera la cota local del payload. El contrato de cada
  informe ya acota muchos campos; los que no acota —salidas de comando, evidencias, valoraciones— se
  recortan con la marca explícita de recorte, de modo que ni un contenido de fichero completo ni un
  volcado de consola engordan un artefacto.

Un informe previo (``qa``, ``security``, ``review``) viaja en la entrada de la etapa siguiente como
**evidencia**, nunca como aprobación: que QA declare PASS significa que el producto funciona, no que
sea seguro, y el veredicto de la etapa actual lo calcula su runner, no el códec. Lo que sí hace el
códec es negarse a construir una entrada a la que le falta un artefacto del que depende: eso es
``WORKFLOW_INCOMPLETE_EVIDENCE``, que el adaptador convierte en ``BLOCKED`` en vez de improvisar.

Reparación (ENGINE-6.1)
-----------------------
El ciclo de reparación autónoma acotado necesita que el Developer reciba algo que antes no existía:
el **encargo** de reparar (plan, diagnóstico, defectos, snapshot y criterios). Ese encargo no es
memoria del proceso que decidió reparar —tiene que sobrevivir a una caída y a una reanudación en
otro proceso—, así que viaja por el mismo camino durable que el resto del handoff, con cuatro
códecs y cuatro tipos de artefacto:

- ``REPAIR_PLAN``: el :class:`~punto.schemas.repair.RepairPlan`, que es la **autorización de
  escritura** del ciclo (archivos objetivo, globs permitidos, archivos prohibidos, cambios
  esperados, criterios y roles que vuelven a verificar);
- ``REPAIR_DIAGNOSIS``: el :class:`~punto.schemas.repair.RepairDiagnosis`, la conclusión técnica
  estructurada sobre el defecto, sin razonamiento privado;
- ``REPAIR_FINDINGS``: los :class:`~punto.schemas.repair.RepairFinding` que se van a reparar, con
  su fingerprint, que es la identidad estable con la que después se comprueba si volvieron;
- ``REPAIR_SNAPSHOT``: el :class:`~punto.schemas.repair.RepairSnapshot`, el estado previo con
  hashes para poder deshacer sin adivinar.

Los cuatro se publican con la petición del paso ``DEVELOPER``, que es la etapa que ejecuta la
reparación, y ninguno lleva bytes binarios: son registros estructurados, redactados y acotados como
el resto de sobres. La regla del ciclo la aplica quien consume: si hay plan de reparación pero falta
el diagnóstico, los defectos o el snapshot, la reparación **no se ejecuta a medias** —el adaptador
la declara ``BLOCKED`` con ``WORKFLOW_INCOMPLETE_EVIDENCE``—, porque un encargo incompleto haría
trabajar al Developer sin saber qué repara o sin poder deshacerlo.

Qué NO hace este módulo
-----------------------
No llama a ningún modelo, no abre red, no ejecuta subprocesos y no decide autoridad. Solo serializa
lo que una etapa ya produjo, lo deja en el almacén y lo reconstruye cuando la etapa siguiente lo
necesita. Tampoco re-ejecuta etapas: si un diseño o un plan no está en las referencias, la etapa que
lo necesitaba falla con un detalle explícito; volver a ejecutar al Architect o al Planner para
rellenar el hueco sería exactamente la duplicación que el kernel elimina. Tampoco publica el
artefacto de un rol cuyo resultado no es aceptable: eso lo decide quien llama, que es quien conoce
la decisión del kernel, no este módulo.

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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from pydantic import BaseModel, ValidationError

from punto.architect.base import ArchitectureOutcome
from punto.developer.context import ExecutionContext
from punto.planner.base import PlanningOutcome
from punto.providers.base import ImagePayload
from punto.schemas.cross_audit import CrossAuditReport, CrossAuditTask
from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.execution import (
    CommandSpec,
    DeveloperExecutionResult,
    DeveloperTask,
    ExecutionTrustLevel,
)
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
from punto.schemas.qa import QAReport, QATask
from punto.schemas.repair import RepairDiagnosis, RepairFinding, RepairPlan, RepairSnapshot
from punto.schemas.review import ReviewReport, ReviewTask
from punto.schemas.security import SecurityReport, SecurityTask
from punto.schemas.visual import VisualQAReport, VisualQATask, VisualSpec
from punto.schemas.web import ScreenshotArtifact, WebSessionReport
from punto.schemas.workflow import ArtifactReference, RoleExecutionRequest, RoleName
from punto.workflow.artifacts import ArtifactStore
from punto.workflow.errors import (
    WorkflowCheckpointInvalidError,
    WorkflowIncompleteEvidenceError,
    WorkflowResumeFailedError,
)

#: Versión del sobre que viaja en cada artefacto del handoff.
#:
#: Cambiarla invalida la lectura de artefactos escritos por otra versión del motor: por eso el
#: resolutor compara la versión declarada y falla en vez de adivinar el formato.
HANDOFF_SCHEMA_VERSION: Final[str] = "1.0.0"
#: Tipo del artefacto que publica la etapa ``ARCHITECT``: el ``ArchitectureOutcome`` completo.
ARCHITECTURE_KIND: Final[str] = "ARCHITECTURE"
#: Tipo del artefacto que publica la etapa ``PLANNER``: el bundle durable del plan.
PLAN_KIND: Final[str] = "PLANNING"
#: Tipo del artefacto que publica la etapa ``DEVELOPER``: el ``DeveloperExecutionResult`` completo.
DEVELOPER_KIND: Final[str] = "DEVELOPER_RESULT"
#: Tipo del artefacto que publica la etapa ``QA``: el ``QAReport`` completo.
QA_KIND: Final[str] = "QA_REPORT"
#: Tipo del artefacto que publica la etapa ``SECURITY``: el ``SecurityReport`` completo.
SECURITY_KIND: Final[str] = "SECURITY_REPORT"
#: Tipo del artefacto que publica la etapa ``REVIEWER``: el ``ReviewReport`` completo.
REVIEW_KIND: Final[str] = "REVIEW_REPORT"
#: Tipo del artefacto que publica la etapa ``CROSS_AUDIT``: el ``CrossAuditReport`` completo.
CROSS_AUDIT_KIND: Final[str] = "CROSS_AUDIT_REPORT"
#: Tipo del artefacto que publica la etapa ``VISUAL_QA``: el ``VisualQAReport`` completo.
VISUAL_QA_KIND: Final[str] = "VISUAL_QA_REPORT"
#: Tipo del artefacto con la evidencia **de entrada** de Visual QA: la especificación visual y el
#: informe técnico de la sesión web, medido por PUNTO en un navegador real.
VISUAL_EVIDENCE_KIND: Final[str] = "VISUAL_EVIDENCE"
#: Tipo del artefacto que guarda los **bytes** de una captura de la sesión web.
#:
#: Es un artefacto propio y no un campo del manifiesto porque unos bytes de imagen dentro de un
#: JSON que se lee, se redacta y se acota como texto dejarían de ser los bytes exactos que el
#: navegador midió.
SCREENSHOT_KIND: Final[str] = "SCREENSHOT"
#: Tipo del artefacto con el **índice** de capturas: la evidencia canónica de cada una más la
#: referencia de sus bytes. Ata cada captura a su contenido sin llevar binarios.
SCREENSHOT_MANIFEST_KIND: Final[str] = "SCREENSHOT_MANIFEST"
#: Tipo del artefacto que publica el paso ``DEVELOPER`` de un ciclo de reparación: el contrato de la
#: reparación (``RepairPlan``), que es su autorización de escritura.
REPAIR_PLAN_KIND: Final[str] = "REPAIR_PLAN"
#: Tipo del artefacto con el diagnóstico estructurado (``RepairDiagnosis``) que motiva la
#: reparación. Es una **propuesta** técnica, nunca la decisión: la decisión la calculó el kernel.
REPAIR_DIAGNOSIS_KIND: Final[str] = "REPAIR_DIAGNOSIS"
#: Tipo del artefacto con los defectos (``RepairFinding``) que la reparación debe corregir.
REPAIR_FINDINGS_KIND: Final[str] = "REPAIR_FINDINGS"
#: Tipo del artefacto con el estado previo a la reparación (``RepairSnapshot``), con hashes para
#: poder deshacerla sin adivinar.
REPAIR_SNAPSHOT_KIND: Final[str] = "REPAIR_SNAPSHOT"

#: Nombres de los campos del sobre. Son constantes porque son contrato, no texto decorativo.
_SCHEMA_FIELD: Final[str] = "schema_version"
_KIND_FIELD: Final[str] = "kind"
#: Campo del sobre que lleva el informe serializado. El nombre es contrato: lo lee otro proceso.
_CONTENT_FIELD: Final[str] = "content"
#: Campos del sobre de la evidencia visual. Son dos piezas que viajan juntas o no viajan.
_SPEC_FIELD: Final[str] = "spec"
_SESSION_FIELD: Final[str] = "session"
#: Campos del sobre del manifiesto de capturas. Son contrato: los lee otro proceso.
_SCREENSHOTS_FIELD: Final[str] = "screenshots"
#: Campo del sobre de los defectos de una reparación: una lista de ``RepairFinding``.
_FINDINGS_FIELD: Final[str] = "findings"
_REFERENCE_FIELD: Final[str] = "reference"
_LOGICAL_NAME_FIELD: Final[str] = "logical_name"
_TASK_ID_FIELD: Final[str] = "task_id"
_PROJECT_ID_FIELD: Final[str] = "project_id"
_SESSION_ID_FIELD: Final[str] = "session_id"
#: Etiquetas legibles de cada artefacto. Describen el tipo, nunca el contenido.
_ARCHITECTURE_LABEL: Final[str] = "diseño del Architect"
_PLAN_LABEL: Final[str] = "plan durable del Planner"
_DEVELOPER_LABEL: Final[str] = "resultado durable del Developer"
_QA_LABEL: Final[str] = "informe durable de QA"
_SECURITY_LABEL: Final[str] = "informe durable de Security"
_REVIEW_LABEL: Final[str] = "informe durable del Reviewer"
_CROSS_AUDIT_LABEL: Final[str] = "informe durable de la auditoría cruzada"
_VISUAL_QA_LABEL: Final[str] = "informe durable de Visual QA"
_VISUAL_EVIDENCE_LABEL: Final[str] = "evidencia visual durable (especificación y sesión web)"
#: Etiquetas de los artefactos de capturas. Describen el tipo, nunca el contenido de la imagen.
_SCREENSHOT_LABEL: Final[str] = "bytes de una captura de la sesión web"
_SCREENSHOT_MANIFEST_LABEL: Final[str] = "índice durable de capturas de la sesión web"
#: Etiquetas de los artefactos de reparación. Describen el tipo, nunca el contenido del encargo.
_REPAIR_PLAN_LABEL: Final[str] = "contrato durable del plan de reparación"
_REPAIR_DIAGNOSIS_LABEL: Final[str] = "diagnóstico durable de la reparación"
_REPAIR_FINDINGS_LABEL: Final[str] = "defectos durables que la reparación debe corregir"
_REPAIR_SNAPSHOT_LABEL: Final[str] = "estado previo durable a la reparación (snapshot)"
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
#: Cota local de **todo** texto del sobre de un informe.
#:
#: El contrato de cada informe ya acota buena parte de sus campos, pero no todos: una salida de
#: comando, una evidencia o una valoración pueden llegar sin límite. Esta cota es la que impide que
#: un volcado de consola o el contenido completo de un fichero engorden el artefacto.
_MAX_PAYLOAD_TEXT_CHARS: Final[int] = 4_000
#: Cota de los textos de contexto derivados del plan y máximo de elementos que se copian de él.
_MAX_CONTEXT_CHARS: Final[int] = 2_000
_MAX_CONTEXT_ITEMS: Final[int] = 20
#: Marca con la que se sustituye un valor con forma de credencial.
_REDACTION_MARKER: Final[str] = "[credencial omitida]"
#: Formas de credencial que se redactan antes de escribir bytes en el almacén.
#:
#: La lista es corta y conservadora a propósito: solo formas que casi nunca aparecen en texto
#: legítimo. El almacén es disco, y un informe de seguridad que copie la cabecera de autenticación
#: de una petición no puede convertir el artefacto en el sitio donde vive la credencial.
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{6,}"),
    re.compile(r"\bAKIA[0-9A-Z]{10,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{8,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd)\b"
        r"\s*[:=]\s*\S+"
    ),
)
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
# Códecs de los informes posteriores al plan (ENGINE-6.0.3)
# ---------------------------------------------------------------------------
def publish_developer(
    store: ArtifactStore, *, request: RoleExecutionRequest, result: DeveloperExecutionResult
) -> ArtifactReference:
    """Publica el ``DeveloperExecutionResult`` completo y devuelve su referencia durable.

    Se guarda el resultado entero —estado, archivos cambiados, comandos, validación y consumo—
    porque es la evidencia sobre la que QA, Security, Reviewer, auditoría cruzada y Visual QA
    construyen su entrada: sin él, esas etapas solo podrían trabajar sobre una suposición. El
    artefacto se publica cuando el resultado es aceptable (``COMPLETED``), y eso lo decide **quien
    llama**, que es quien conoce la decisión del kernel: el códec solo serializa lo que ya ocurrió.

    Raises:
        ValueError: si la petición no es de la etapa ``DEVELOPER``. Etiquetar un artefacto con el
            rol equivocado corrompería el handoff, así que se rechaza antes de escribir nada.
    """
    return _publish_report(
        store,
        request=request,
        role=RoleName.DEVELOPER,
        kind=DEVELOPER_KIND,
        label=_DEVELOPER_LABEL,
        report=result,
        function="publish_developer",
    )


def resolve_developer(
    store: ArtifactStore, references: tuple[ArtifactReference, ...]
) -> DeveloperExecutionResult | None:
    """Reconstruye el resultado durable del Developer desde la **primera** referencia del tipo.

    ``None`` significa «no hay artefacto de este tipo en las referencias», que la etapa debe
    declarar como hueco. Un artefacto presente pero ilegible, de otro esquema o manipulado **no** se
    degrada a ``None``: falla con ``WORKFLOW_RESUME_FAILED`` o lo detecta el propio almacén, porque
    confundir corrupción con ausencia haría ejecutar la etapa siguiente sobre datos descartados en
    silencio.
    """
    return _resolve_report(store, references, DEVELOPER_KIND, DeveloperExecutionResult)


def publish_qa(
    store: ArtifactStore, *, request: RoleExecutionRequest, report: QAReport
) -> ArtifactReference:
    """Publica el ``QAReport`` completo y devuelve su referencia durable.

    Se guarda el informe entero —estado calculado por PUNTO, cobertura de cada criterio, checks
    ejecutados, hallazgos y capacidades ausentes— porque es lo que Security, Reviewer, auditoría
    cruzada y la reparación necesitan leer en un proceso nuevo. Igual que el resto de publicadores,
    quien llama decide si el resultado es aceptable (``COMPLETED``): este códec no juzga el informe.

    Raises:
        ValueError: si la petición no es de la etapa ``QA``.
    """
    return _publish_report(
        store,
        request=request,
        role=RoleName.QA,
        kind=QA_KIND,
        label=_QA_LABEL,
        report=report,
        function="publish_qa",
    )


def resolve_qa(store: ArtifactStore, references: tuple[ArtifactReference, ...]) -> QAReport | None:
    """Reconstruye el informe de QA desde la **primera** referencia ``QA_REPORT``, o ``None``.

    Solo devuelve ``None`` cuando no hay ninguna referencia de ese tipo. Un informe de QA en estado
    ``FAIL`` sí se resuelve: un veredicto negativo es evidencia de pleno derecho y descartarlo
    borraría el motivo por el que el workflow pidió reparar.
    """
    return _resolve_report(store, references, QA_KIND, QAReport)


def publish_security(
    store: ArtifactStore, *, request: RoleExecutionRequest, report: SecurityReport
) -> ArtifactReference:
    """Publica el ``SecurityReport`` completo y devuelve su referencia durable.

    Viaja el informe entero —estado, hallazgos con su gravedad real, checks ejecutados, archivos
    revisados y omitidos— porque el Reviewer y la auditoría cruzada lo consumen como gate: un gate
    reconstruido a medias no es un gate. Quien llama decide si el resultado es aceptable.

    Raises:
        ValueError: si la petición no es de la etapa ``SECURITY``.
    """
    return _publish_report(
        store,
        request=request,
        role=RoleName.SECURITY,
        kind=SECURITY_KIND,
        label=_SECURITY_LABEL,
        report=report,
        function="publish_security",
    )


def resolve_security(
    store: ArtifactStore, references: tuple[ArtifactReference, ...]
) -> SecurityReport | None:
    """Reconstruye el informe de Security desde la **primera** referencia del tipo, o ``None``.

    Un informe ``FAIL`` se resuelve igual que uno ``PASS``: la gravedad de cada hallazgo viaja tal
    como la calculó PUNTO, y ninguna reconstrucción la rebaja.
    """
    return _resolve_report(store, references, SECURITY_KIND, SecurityReport)


def publish_review(
    store: ArtifactStore, *, request: RoleExecutionRequest, report: ReviewReport
) -> ArtifactReference:
    """Publica el ``ReviewReport`` completo y devuelve su referencia durable.

    Se guarda con sus gates y su veredicto porque la auditoría cruzada los lee como evidencia
    previa: sin ellos, la etapa de auditoría no podría saber si el cambio llegó aprobado o con
    cambios pedidos. Quien llama decide si el resultado es aceptable.

    Raises:
        ValueError: si la petición no es de la etapa ``REVIEWER``.
    """
    return _publish_report(
        store,
        request=request,
        role=RoleName.REVIEWER,
        kind=REVIEW_KIND,
        label=_REVIEW_LABEL,
        report=report,
        function="publish_review",
    )


def resolve_review(
    store: ArtifactStore, references: tuple[ArtifactReference, ...]
) -> ReviewReport | None:
    """Reconstruye el informe del Reviewer desde la **primera** referencia del tipo, o ``None``.

    ``CHANGES_REQUESTED`` y ``BLOCKED`` se resuelven igual que ``APPROVED``: el veredicto es un
    hecho del run y quien lo lee decide qué hacer con él, no este códec.
    """
    return _resolve_report(store, references, REVIEW_KIND, ReviewReport)


def publish_cross_audit(
    store: ArtifactStore, *, request: RoleExecutionRequest, report: CrossAuditReport
) -> ArtifactReference:
    """Publica el ``CrossAuditReport`` completo y devuelve su referencia durable.

    Se conserva entero —veredicto, gates, hallazgos, proveedores previos y ``cross_model``— porque
    es la prueba de que la auditoría fue de verdad entre proveedores distintos y no una relectura
    del mismo modelo. Quien llama decide si el resultado es aceptable.

    Raises:
        ValueError: si la petición no es de la etapa ``CROSS_AUDIT``.
    """
    return _publish_report(
        store,
        request=request,
        role=RoleName.CROSS_AUDIT,
        kind=CROSS_AUDIT_KIND,
        label=_CROSS_AUDIT_LABEL,
        report=report,
        function="publish_cross_audit",
    )


def resolve_cross_audit(
    store: ArtifactStore, references: tuple[ArtifactReference, ...]
) -> CrossAuditReport | None:
    """Reconstruye la auditoría cruzada desde la **primera** referencia del tipo, o ``None``.

    ``provider``, ``upstream_providers`` y ``cross_model`` viajan en el informe y se reconstruyen
    con él: la reconstrucción no recalcula si la auditoría fue cruzada, porque eso la dejaría en
    manos de una suposición en vez de en las del informe que ya lo declaró.
    """
    return _resolve_report(store, references, CROSS_AUDIT_KIND, CrossAuditReport)


def publish_visual_qa(
    store: ArtifactStore, *, request: RoleExecutionRequest, report: VisualQAReport
) -> ArtifactReference:
    """Publica el ``VisualQAReport`` completo y devuelve su referencia durable.

    Se guarda entero —veredicto, gates, hallazgos, rutas, viewports y capturas analizadas— para que
    el cierre del workflow pueda auditar qué se miró y con qué resultado sin volver a abrir un
    navegador. Los bytes de las imágenes **no** viajan aquí: el contrato del informe nunca los
    lleva. Quien llama decide si el resultado es aceptable.

    Raises:
        ValueError: si la petición no es de la etapa ``VISUAL_QA``.
    """
    return _publish_report(
        store,
        request=request,
        role=RoleName.VISUAL_QA,
        kind=VISUAL_QA_KIND,
        label=_VISUAL_QA_LABEL,
        report=report,
        function="publish_visual_qa",
    )


def resolve_visual_qa(
    store: ArtifactStore, references: tuple[ArtifactReference, ...]
) -> VisualQAReport | None:
    """Reconstruye el informe de Visual QA desde la **primera** referencia del tipo, o ``None``.

    El informe se resuelve sin abrir un navegador y sin las imágenes: lo que viaja es el veredicto
    con su evidencia declarada, que es lo que el cierre del workflow necesita comprobar.
    """
    return _resolve_report(store, references, VISUAL_QA_KIND, VisualQAReport)


def publish_visual_evidence(
    store: ArtifactStore,
    *,
    request: RoleExecutionRequest,
    spec: VisualSpec,
    session: WebSessionReport,
) -> ArtifactReference:
    """Publica la **entrada** de Visual QA —especificación y sesión web— en un solo sobre durable.

    La etapa visual no opina en el vacío: opina contra una especificación y sobre unos hechos
    medidos por PUNTO en un navegador real. Esas dos piezas no las produce ningún rol del plan, así
    que sin publicarlas el proceso nuevo no puede reconstruir la entrada de Visual QA desde el
    checkpoint y los almacenes (defecto V603-04, variante web) y la etapa quedaría siempre
    incompleta. Viajan **juntas** y con un solo digest a propósito: una especificación sin la sesión
    que la mide, o al revés, no permitiría evaluar nada, y un par descuadrado es peor que un hueco.

    No se guardan binarios: el contrato de la sesión declara los screenshots por nombre lógico,
    viewport, tamaño y hash, nunca por sus bytes, y el sobre pasa por la misma redacción y la misma
    cota de texto que el resto de informes.

    Raises:
        ValueError: si la petición no es de la etapa ``VISUAL_QA``, que es la etapa que consume
            esta evidencia y la que identifica el workflow y el paso en el almacén.
    """
    _assert_role(request, RoleName.VISUAL_QA, "publish_visual_evidence")
    payload: dict[str, object] = {
        _KIND_FIELD: VISUAL_EVIDENCE_KIND,
        _SPEC_FIELD: _bounded_json(spec),
        _SESSION_FIELD: _bounded_json(session),
    }
    return store.put(
        workflow_id=request.workflow_id,
        role=RoleName.VISUAL_QA,
        step_index=request.step_index,
        kind=VISUAL_EVIDENCE_KIND,
        label=_VISUAL_EVIDENCE_LABEL,
        data=_encode(payload),
    )


def resolve_visual_evidence(
    store: ArtifactStore, references: tuple[ArtifactReference, ...]
) -> tuple[VisualSpec, WebSessionReport] | None:
    """Reconstruye la pareja ``(especificación, sesión web)`` de la **primera** referencia del tipo.

    ``None`` significa «no hay evidencia visual publicada», que la etapa debe declarar como hueco.
    Un sobre presente pero ilegible, de otro esquema o con una de las dos piezas inválida **no** se
    degrada a ``None``: la pareja no se entrega a medias y el fallo sube como
    ``WORKFLOW_RESUME_FAILED`` con el motivo, porque evaluar con media evidencia es exactamente
    aprobar sin haber mirado.
    """
    for reference in references:
        if reference.kind != VISUAL_EVIDENCE_KIND:
            continue
        payload = _decode(
            store.get(reference), expected_kind=VISUAL_EVIDENCE_KIND, reference=reference
        )
        return (
            _model_field(VisualSpec, payload, _SPEC_FIELD, reference),
            _model_field(WebSessionReport, payload, _SESSION_FIELD, reference),
        )
    return None


# ---------------------------------------------------------------------------
# Códec de los bytes de las capturas de la sesión web (ENGINE-6.0.4, V604-02)
# ---------------------------------------------------------------------------
def publish_screenshots(
    store: ArtifactStore,
    *,
    request: RoleExecutionRequest,
    session: WebSessionReport,
    images: Mapping[str, ImagePayload],
) -> ArtifactReference:
    """Publica los bytes de cada captura y el índice que los ata a su evidencia canónica.

    Es la pieza que cierra el defecto V604-02: hasta 6.0.3 el handoff publicaba la especificación
    visual y la sesión web —nombre lógico, ruta, viewport, tamaño y sha256 de cada captura—, pero
    **no sus bytes**, así que una sesión con capturas verificadas en un navegador real no se podía
    reconstruir en un proceso nuevo. Los bytes viajan en su propio artefacto (``SCREENSHOT``) y el
    índice en otro (``SCREENSHOT_MANIFEST``): meterlos en el JSON del manifiesto obligaría a
    codificarlos, y un binario codificado que se redacta y se acota como texto deja de ser el
    binario que el navegador midió.

    Cada captura declarada se revalida con la función **canónica** de la capa web
    (:meth:`~punto.schemas.web.ScreenshotArtifact.as_image_payload`), que comprueba que los bytes
    miden lo que el artefacto declara y que su sha256 coincide. No se añade una segunda validación
    más débil al lado: la canónica es la única. Su ``ValueError`` se propaga tal cual, y aquí
    significa defecto de quien llama —el adaptador midió esas imágenes y las pasó—, no un estado del
    workflow. Toda la validación ocurre **antes** de escribir el primer byte, de modo que un mapa de
    payloads que no encaja no deja capturas a medias en el almacén.

    El manifiesto lleva la identidad de la sesión (``task_id``, ``project_id`` e ``id``) y la lista
    **completa y ordenada por nombre lógico**, para que el mismo conjunto de capturas produzca
    siempre los mismos bytes y su digest sea reproducible.

    Raises:
        ValueError: si la petición no es de ``VISUAL_QA``; si la sesión no declara ninguna captura
            —el caso «sin capturas» no publica nada de esto y un índice vacío fingiría una
            evidencia que la sesión no midió—; si falta el payload de una captura declarada; si
            llega el payload de una captura que la sesión no declara, porque una de más no puede
            colarse ni ocultar una requerida que falte; si la sesión declara dos veces el mismo
            nombre lógico; o si los bytes de una captura no superan la validación canónica.
    """
    _assert_role(request, RoleName.VISUAL_QA, "publish_screenshots")
    declared = _declared_captures(session)
    payloads = _capture_payloads(declared, images)
    validated: list[tuple[ScreenshotArtifact, ImagePayload]] = [
        (artifact, artifact.as_image_payload(payloads[artifact.logical_name].data))
        for artifact in sorted(declared, key=lambda item: item.logical_name)
    ]
    entries: list[dict[str, object]] = []
    for artifact, payload in validated:
        reference = store.put(
            workflow_id=request.workflow_id,
            role=RoleName.VISUAL_QA,
            step_index=request.step_index,
            kind=SCREENSHOT_KIND,
            label=f"{_SCREENSHOT_LABEL}: {artifact.logical_name}",
            data=payload.data,
        )
        entries.append({**_json_dump(artifact), _REFERENCE_FIELD: _json_dump(reference)})
    manifest: dict[str, object] = {
        _KIND_FIELD: SCREENSHOT_MANIFEST_KIND,
        _TASK_ID_FIELD: str(session.task_id),
        _PROJECT_ID_FIELD: str(session.project_id),
        _SESSION_ID_FIELD: str(session.id),
        _SCREENSHOTS_FIELD: entries,
    }
    return store.put(
        workflow_id=request.workflow_id,
        role=RoleName.VISUAL_QA,
        step_index=request.step_index,
        kind=SCREENSHOT_MANIFEST_KIND,
        label=_SCREENSHOT_MANIFEST_LABEL,
        data=_encode(_bounded_payload(manifest)),
    )


def resolve_screenshots(
    store: ArtifactStore,
    references: tuple[ArtifactReference, ...],
    session: WebSessionReport | None,
) -> Mapping[str, ImagePayload]:
    """Reconstruye los bytes **verificados** de cada captura que declara la sesión web.

    Es la otra mitad de V604-02: un proceso nuevo resuelve el manifiesto (``SCREENSHOT_MANIFEST``)
    de la **primera** referencia de ese tipo, localiza por nombre lógico la entrada de cada captura
    declarada, recupera sus bytes del almacén con la referencia del propio manifiesto y los revalida
    con la misma función canónica con la que se publicaron. Así la verificación visual no depende de
    ninguna variable del proceso que midió la sesión.

    Antes de reconstruir ningún byte se comprueba que el manifiesto es el de la **sesión pedida**:
    su ``task_id``, su ``project_id`` y su ``session_id`` tienen que ser los que declara ``session``
    (V605-06). Un manifiesto de otro replay —mismo proyecto, mismas capturas y los mismos nombres
    lógicos— se rechaza con ``WORKFLOW_INCOMPLETE_EVIDENCE`` diciendo qué identidad no cuadra y qué
    se esperaba. Analizar la evidencia de otra ejecución no es una aprobación: es un hueco de
    evidencia, y quien lo recibe declara ``BLOCKED`` en vez de dar por verificado lo que no se miró.

    Nada se entrega a medias: o están **todas** las capturas declaradas y verificadas, o hay
    ``WORKFLOW_INCOMPLETE_EVIDENCE``. Un manifiesto de otro replay, una captura que falte, unos
    bytes ausentes, unos bytes del tamaño declarado cuyo hash no cuadra, un sha256 que no
    corresponde, una entrada bajo otro nombre lógico, una entrada de más en el manifiesto o una
    referencia de bytes que el almacén no puede verificar son, todos, huecos de evidencia
    recuperables: el adaptador los convierte en ``BLOCKED`` en vez de cerrar el workflow.
    Traducirlos aquí es deliberado, porque en esta función «la evidencia durable no es la de esta
    sesión o no es de fiar» significa exactamente eso.

    Un ``session`` ausente o sin capturas devuelve un mapa vacío y no toca el almacén: es el caso
    «sin capturas» del adaptador, donde no hay nada que analizar y el informe dirá cero, que es un
    hecho y no una invención.

    Returns:
        Mapa ``logical_name -> ImagePayload`` con los bytes exactos y verificados de cada captura
        declarada, en el orden en que la sesión las declara.

    Raises:
        WorkflowIncompleteEvidenceError: si la sesión declara capturas y falta el manifiesto, si el
            manifiesto no es el de esa sesión (otra tarea, otro proyecto u otro replay) o si
            cualquier comprobación de integridad de las capturas falla.
    """
    if session is None or not session.screenshots:
        return {}
    declared = tuple(session.screenshots)
    manifest = _first_reference(references, SCREENSHOT_MANIFEST_KIND)
    if manifest is None:
        raise WorkflowIncompleteEvidenceError(
            f"la sesión web declara {len(declared)} captura(s) y las referencias del paso no traen "
            f"ningún manifiesto {SCREENSHOT_MANIFEST_KIND}: sin él los bytes no se pueden "
            "reconstruir en un proceso nuevo y la verificación visual se haría a ciegas"
        )
    index = _read_capture_manifest(store, manifest)
    _assert_same_session(index, session, manifest)
    entries = index.entries
    images: dict[str, ImagePayload] = {}
    for artifact in declared:
        name = _bounded_text(artifact.logical_name)
        entry = _capture_entry(entries, artifact, name, manifest)
        stored, reference = _manifest_capture(entry, manifest, name)
        _assert_same_capture(artifact, stored, name)
        images[name] = _verified_payload(store, reference, stored, name)
    extra = sorted(set(entries) - {_bounded_text(item.logical_name) for item in declared})
    if extra:
        raise WorkflowIncompleteEvidenceError(
            f"el manifiesto {manifest.reference!r} lleva {len(extra)} entrada(s) que la sesión no "
            f"declara ({extra}): el índice y la sesión no son el mismo conjunto de capturas y no "
            "se puede saber qué evidencia corresponde a qué imagen"
        )
    return images


# ---------------------------------------------------------------------------
# Códecs de la reparación (ENGINE-6.1)
# ---------------------------------------------------------------------------
def publish_repair_plan(
    store: ArtifactStore, *, request: RoleExecutionRequest, plan: RepairPlan
) -> ArtifactReference:
    """Publica el contrato de la reparación y devuelve su referencia durable.

    El ``RepairPlan`` es la **autorización de escritura** del ciclo: qué archivos se pueden tocar,
    cuáles no, qué cambios se esperan y qué roles vuelven a verificar. Se guarda entero porque la
    reparación se ejecuta en un proceso que puede no ser el que decidió reparar, y sin el plan ese
    proceso no sabría qué está autorizado a cambiar.

    Se publica con la petición del paso ``DEVELOPER``: es la etapa que ejecuta la reparación, y el
    artefacto queda registrado en su paso para que la traza diga de dónde salió el encargo.

    Raises:
        ValueError: si la petición no es de la etapa ``DEVELOPER``. Etiquetar el encargo con el rol
            equivocado corrompería el handoff, así que se rechaza antes de escribir nada.
    """
    return _publish_repair(
        store,
        request=request,
        kind=REPAIR_PLAN_KIND,
        label=_REPAIR_PLAN_LABEL,
        content=plan,
        function="publish_repair_plan",
    )


def resolve_repair_plan(
    store: ArtifactStore, references: tuple[ArtifactReference, ...]
) -> RepairPlan | None:
    """Reconstruye el contrato de la reparación desde la **primera** referencia de su tipo.

    ``None`` significa «este paso no repara»: sin plan de reparación el Developer ejecuta su tarea
    normal. Un artefacto presente pero ilegible, de otro esquema o manipulado **no** se degrada a
    ``None``: falla con ``WORKFLOW_RESUME_FAILED`` o lo detecta el almacén, porque confundir
    corrupción con ausencia ejecutaría la reparación sin saber qué está autorizado a tocar.
    """
    return _resolve_report(store, references, REPAIR_PLAN_KIND, RepairPlan)


def publish_repair_diagnosis(
    store: ArtifactStore, *, request: RoleExecutionRequest, diagnosis: RepairDiagnosis
) -> ArtifactReference:
    """Publica el diagnóstico estructurado de la reparación y devuelve su referencia durable.

    Viaja entero —causa raíz, evidencia, archivos sospechosos, restricciones, estrategia,
    confianza y lo que sigue sin saberse— porque es lo que permite a un proceso nuevo entender
    **por qué** se repara sin volver a diagnosticar. Es una propuesta: la decisión de reparar la
    calculó el kernel, y este códec no la reinterpreta.

    Raises:
        ValueError: si la petición no es de la etapa ``DEVELOPER``.
    """
    return _publish_repair(
        store,
        request=request,
        kind=REPAIR_DIAGNOSIS_KIND,
        label=_REPAIR_DIAGNOSIS_LABEL,
        content=diagnosis,
        function="publish_repair_diagnosis",
    )


def resolve_repair_diagnosis(
    store: ArtifactStore, references: tuple[ArtifactReference, ...]
) -> RepairDiagnosis | None:
    """Reconstruye el diagnóstico desde la **primera** referencia de su tipo, o ``None``.

    Un diagnóstico de confianza ``LOW`` se resuelve igual que uno ``HIGH``: la categoría es un hecho
    declarado y quien lo lee decide qué hacer con él, no este códec.
    """
    return _resolve_report(store, references, REPAIR_DIAGNOSIS_KIND, RepairDiagnosis)


def publish_repair_findings(
    store: ArtifactStore,
    *,
    request: RoleExecutionRequest,
    findings: Sequence[RepairFinding],
) -> ArtifactReference:
    """Publica los defectos que la reparación debe corregir y devuelve su referencia durable.

    El sobre lleva la lista de ``RepairFinding`` —con su fingerprint, que es la identidad estable
    con la que después se comprueba si el mismo defecto volvió— y nada más. Se guarda la lista
    **completa**: un defecto que no viaja es un defecto que la reparación no arregla y que la
    verificación posterior volvería a encontrar sin que nadie sepa por qué.

    Raises:
        ValueError: si la petición no es de la etapa ``DEVELOPER``.
    """
    _assert_role(request, RoleName.DEVELOPER, "publish_repair_findings")
    payload: dict[str, object] = {
        _KIND_FIELD: REPAIR_FINDINGS_KIND,
        _FINDINGS_FIELD: [_bounded_json(finding) for finding in findings],
    }
    return store.put(
        workflow_id=request.workflow_id,
        role=RoleName.DEVELOPER,
        step_index=request.step_index,
        kind=REPAIR_FINDINGS_KIND,
        label=_REPAIR_FINDINGS_LABEL,
        data=_encode(payload),
    )


def resolve_repair_findings(
    store: ArtifactStore, references: tuple[ArtifactReference, ...]
) -> tuple[RepairFinding, ...]:
    """Reconstruye los defectos desde la **primera** referencia de su tipo.

    Devuelve la tupla vacía si el paso no trae ninguna referencia de este tipo, que es el caso «este
    paso no repara» y no un hueco: quien exige el artefacto es la etapa, que lo declara incompleto.
    Un sobre presente pero sin lista de defectos, con un elemento que no valida contra
    ``RepairFinding`` o de otro esquema **no** se interpreta «lo mejor posible»: falla con
    ``WORKFLOW_RESUME_FAILED``, porque una lista a medias haría reparar unos defectos y olvidar
    otros en silencio.
    """
    for reference in references:
        if reference.kind != REPAIR_FINDINGS_KIND:
            continue
        payload = _decode(
            store.get(reference), expected_kind=REPAIR_FINDINGS_KIND, reference=reference
        )
        raw = payload.get(_FINDINGS_FIELD)
        if not isinstance(raw, list):
            raise WorkflowResumeFailedError(
                f"el artefacto {reference.reference!r} se referencia como "
                f"{REPAIR_FINDINGS_KIND!r} y no lleva una lista en {_FINDINGS_FIELD!r} sino "
                f"{type(raw).__name__}: no hay defectos que reconstruir"
            )
        return tuple(
            _validate(RepairFinding, item, f"{_FINDINGS_FIELD}[{index}]", reference)
            for index, item in enumerate(raw)
        )
    return ()


def publish_repair_snapshot(
    store: ArtifactStore, *, request: RoleExecutionRequest, snapshot: RepairSnapshot
) -> ArtifactReference:
    """Publica el estado previo a la reparación y devuelve su referencia durable.

    El snapshot lleva, por archivo, su hash y su tamaño —o su ausencia declarada—, y no el contenido
    de los ficheros: lo que permite **deshacer** una reparación local y reversible es comparar
    hashes, no guardar una copia del proyecto dentro del almacén. Se publica con la petición del
    paso ``DEVELOPER`` para que el ciclo y su rollback queden en la misma traza.

    Raises:
        ValueError: si la petición no es de la etapa ``DEVELOPER``.
    """
    return _publish_repair(
        store,
        request=request,
        kind=REPAIR_SNAPSHOT_KIND,
        label=_REPAIR_SNAPSHOT_LABEL,
        content=snapshot,
        function="publish_repair_snapshot",
    )


def resolve_repair_snapshot(
    store: ArtifactStore, references: tuple[ArtifactReference, ...]
) -> RepairSnapshot | None:
    """Reconstruye el estado previo desde la **primera** referencia de su tipo, o ``None``.

    Un snapshot ilegible o manipulado no se degrada a ``None``: sin él no se puede deshacer la
    reparación con fundamento, y confundir «no hay snapshot» con «el snapshot está roto» haría
    intentar un rollback a ciegas.
    """
    return _resolve_report(store, references, REPAIR_SNAPSHOT_KIND, RepairSnapshot)


# ---------------------------------------------------------------------------
# Entrada oficial del rol DEVELOPER
# ---------------------------------------------------------------------------
def developer_input(
    plan: DurablePlan,
    request: RoleExecutionRequest,
    *,
    trust_level: ExecutionTrustLevel = ExecutionTrustLevel.TRUSTED_LOCAL,
) -> tuple[DeveloperTask, ExecutionContext]:
    """Construye la pareja ``(DeveloperTask, ExecutionContext)`` desde el plan durable.

    Es el constructor **oficial** de la entrada del Developer: el adaptador real lo usa sin ninguna
    *closure* externa, así que la entrada se puede reconstruir en un proceso nuevo a partir de las
    referencias del checkpoint y del almacén.

    Regla de precedencia, explícita porque es el contrato de esta función: **el plan manda cuando
    declara el dato y la petición es el respaldo declarado**, nunca una invención. Lo que la
    petición declara y el plan no lleva (identidad, workspace, acción) sale de la petición.

    El **nivel de confianza del contexto** no se decide aquí (hallazgo F614-01): lo declara quien
    conoce la frontera de ejecución elegida —el adaptador, a partir de
    ``DeveloperRunner.trust_level_required``— y esta función solo lo aplica. ``handoff`` no adivina
    qué proveedor genera el código, ni lo deduce del plan, del almacén o de un texto: si lo hiciera,
    el aislamiento dependería de un dato que el modelo o un artefacto podrían influir. El valor por
    defecto es ``TRUSTED_LOCAL`` porque es el contexto de las ejecuciones deterministas que
    construían esta entrada antes de que existiera el parámetro.

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
    - ``trust_level``: el que declara el llamante, que es el único que conoce la frontera de
      ejecución elegida; por defecto ``TRUSTED_LOCAL``, el de las ejecuciones deterministas. El
      handoff **no** infiere el nivel: lo aplica. Quien construye el paso durable lo deriva de
      ``DeveloperRunner.trust_level_required`` y solo **eleva** el aislamiento —nunca lo degrada—,
      de modo que un runner que genera código con IA recibe el ``UNTRUSTED_MODEL`` que su frontera
      exige y un runner determinista sigue trabajando en local. Un ``build_input`` explícito que
      entregue un contexto incompatible no pasa por aquí y sigue fallando en cerrado, que es lo
      correcto: ese contexto no lo construyó el motor.

    Raises:
        WorkspaceViolationError: si el workspace declarado por la petición no existe. Es el error
            tipado de la capa de ejecución y se propaga tal cual: el handoff no lo disfraza.
    """
    task = _next_task(plan)
    objective = _objective(task, request)
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
        trust_level=trust_level,
    )
    return developer_task, context


# ---------------------------------------------------------------------------
# Entradas oficiales de los roles posteriores al Developer (ENGINE-6.0.3)
# ---------------------------------------------------------------------------
def qa_input(
    plan: DurablePlan | None,
    developer: DeveloperExecutionResult | None,
    request: RoleExecutionRequest,
) -> QATask:
    """Construye la entrada oficial del rol ``QA`` desde artefactos durables.

    Es el constructor **oficial** de la entrada de QA: el adaptador real lo usa sin ninguna
    *closure*, así que la evaluación se puede reconstruir en un proceso nuevo a partir de las
    referencias del checkpoint y del almacén (defecto V603-04).

    Precedencia, explícita porque es el contrato de esta función:

    - el **plan** manda en el contrato de la tarea: objetivo y criterios de aceptación de la primera
      tarea lista, capacidades que exige, vocabulario de capacidades del Architect y contexto de la
      especificación y la arquitectura;
    - el **resultado del Developer** manda en los hechos: qué archivos cambió y qué checks declaró,
      que es lo que QA audita de verdad. Viaja como **evidencia, nunca como aprobación**: un informe
      del Developer dice qué se hizo, no que esté bien, y ``QATask.developer_claimed_pass`` existe
      justo para poder demostrar que QA no lo usa como prueba;
    - la **petición** es el respaldo declarado cuando el plan o el resultado no traen el dato
      (objetivo, criterios, archivos cambiados) y la única fuente de la identidad y del workspace.

    Lo que ningún artefacto durable declara viaja **vacío** en lugar de inventado:
    ``test_only_paths`` no lleva elementos porque ni el plan ni la petición declaran rutas
    solo-de-pruebas, y la allowlist determinista de QA decide después con lo que sí sabe.

    Raises:
        WorkflowIncompleteEvidenceError: si falta el plan durable o el resultado del Developer. Sin
            ellos la entrada se construiría sobre datos inventados, y PUNTO no ejecuta una etapa
            sobre una suposición: es el hueco que el adaptador convierte en ``BLOCKED``.
    """
    durable = _require_plan(plan, "QA")
    result = _require_developer(developer, "QA")
    task = _next_task(durable)
    return QATask(
        task_id=request.task_id,
        project_id=request.project_id,
        objective=_objective(task, request),
        acceptance_criteria=_acceptance_criteria(task, request),
        changed_files=_changed_files(result, request),
        context_files=_context_files(task, request),
        validation_checks=_validation_checks(result, task),
        required_capabilities=_declared(task, "required_capabilities"),
        capability_profile=_capability_profile(durable),
        test_only_paths=(),
        workspace_path=str(_workspace(request)),
        architecture_context=_architecture_context(durable),
        project_spec_context=_project_spec_context(durable),
        developer_result=result,
    )


def security_input(
    plan: DurablePlan | None,
    developer: DeveloperExecutionResult | None,
    qa: QAReport | None,
    request: RoleExecutionRequest,
) -> SecurityTask:
    """Construye la entrada oficial del rol ``SECURITY`` desde artefactos durables.

    Prioridad de las fuentes, la misma que en :func:`qa_input`: el **plan** aporta el contrato de la
    tarea y el vocabulario de capacidades, el **resultado del Developer** aporta los hechos —qué se
    cambió— y la **petición** aporta identidad y workspace.

    El informe de QA es **opcional a propósito**: la seguridad del cambio no depende de que QA haya
    pasado, y un QA que no publicó informe (porque su resultado no fue aceptable) no puede impedir
    una auditoría de seguridad que sí tiene sentido. Cuando viaja, lo hace como **evidencia, nunca
    como aprobación**: que QA declare PASS significa que el producto funciona, no que sea seguro, y
    ``SecurityTask.qa_claimed_pass`` lo expone solo para poder demostrarlo.

    Raises:
        WorkflowIncompleteEvidenceError: si falta el plan durable o el resultado del Developer.
    """
    durable = _require_plan(plan, "SECURITY")
    result = _require_developer(developer, "SECURITY")
    task = _next_task(durable)
    return SecurityTask(
        task_id=request.task_id,
        project_id=request.project_id,
        objective=_objective(task, request),
        acceptance_criteria=_acceptance_criteria(task, request),
        changed_files=_changed_files(result, request),
        context_files=_context_files(task, request),
        workspace_path=str(_workspace(request)),
        architecture_context=_architecture_context(durable),
        project_spec_context=_project_spec_context(durable),
        capability_profile=_capability_profile(durable),
        required_capabilities=_declared(task, "required_capabilities"),
        developer_result=result,
        qa_report=qa,
    )


def review_input(
    plan: DurablePlan | None,
    developer: DeveloperExecutionResult | None,
    qa: QAReport | None,
    security: SecurityReport | None,
    request: RoleExecutionRequest,
) -> ReviewTask:
    """Construye la entrada oficial del rol ``REVIEWER`` desde artefactos durables.

    El plan aporta el contrato de la tarea, las restricciones de la especificación y el riesgo y la
    autoridad **declarados** para la tarea lista; el resultado del Developer aporta los hechos y un
    resumen controlado del cambio; la petición aporta identidad y workspace. El riesgo y la
    autoridad que viajan no se rebajan ni se elevan aquí: el handoff copia lo que el plan declaró y
    la autoridad efectiva la calcula el Policy Engine fuera de esta capa.

    Los informes de QA y de Security son **gates preceptivos** de esta etapa: se exigen los dos
    porque sin ellos no hay aprobación posible y el Reviewer no puede evaluar nada. Viajan como
    **evidencia, nunca como aprobación**: un gate en ``FAIL`` o ``CHANGES_REQUESTED`` se resuelve
    igual que uno en verde y el veredicto lo calcula el runner, no este constructor.
    ``deleted_files`` viaja vacío porque ni el plan durable ni el resultado declaran eliminaciones
    explícitas y PUNTO no deduce una eliminación de la ausencia de un archivo.

    Raises:
        WorkflowIncompleteEvidenceError: si falta el plan, el resultado del Developer, el informe de
            QA o el de Security. Faltar un gate no se suple con una aprobación inventada.
    """
    durable = _require_plan(plan, "REVIEWER")
    result = _require_developer(developer, "REVIEWER")
    qa_report = _require_report(qa, QA_KIND, "REVIEWER", "de QA")
    security_report = _require_report(security, SECURITY_KIND, "REVIEWER", "de Security")
    task = _next_task(durable)
    return ReviewTask(
        task_id=request.task_id,
        project_id=request.project_id,
        objective=_objective(task, request),
        acceptance_criteria=_acceptance_criteria(task, request),
        changed_files=_changed_files(result, request),
        context_files=_context_files(task, request),
        deleted_files=(),
        workspace_path=str(_workspace(request)),
        architecture_constraints=_architecture_constraints(durable),
        risk_level=RiskLevel.LOW if task is None else task.risk_level,
        authority_level=(
            AuthorityLevel.LEVEL_0_AUTONOMOUS if task is None else task.authority_level
        ),
        project_spec_context=_project_spec_context(durable),
        architecture_context=_architecture_context(durable),
        diff_summary=_diff_summary(result),
        developer_result=result,
        qa_report=qa_report,
        security_report=security_report,
    )


def cross_audit_input(
    plan: DurablePlan | None,
    developer: DeveloperExecutionResult | None,
    qa: QAReport | None,
    security: SecurityReport | None,
    review: ReviewReport | None,
    request: RoleExecutionRequest,
) -> CrossAuditTask:
    """Construye la entrada oficial de la auditoría cruzada desde artefactos durables.

    Misma precedencia que en :func:`review_input`: el plan aporta el contrato y el riesgo y la
    autoridad declarados, el resultado del Developer los hechos y el resumen del cambio, y la
    petición la identidad y el workspace.

    Los tres informes previos —QA, Security y Reviewer— son **gates preceptivos** y se exigen los
    tres: la auditoría cruzada existe para mirar con otros ojos lo que ya se verificó, así que sin
    ellos no hay nada que auditar. Viajan como **evidencia, nunca como aprobación**: sus estados se
    copian tal cual y la decisión de la auditoría la toma su runner, que además deriva de ellos los
    proveedores previos y si la auditoría es de verdad cruzada.

    Raises:
        WorkflowIncompleteEvidenceError: si falta el plan, el resultado del Developer o cualquiera
            de los tres informes previos.
    """
    durable = _require_plan(plan, "CROSS_AUDIT")
    result = _require_developer(developer, "CROSS_AUDIT")
    qa_report = _require_report(qa, QA_KIND, "CROSS_AUDIT", "de QA")
    security_report = _require_report(security, SECURITY_KIND, "CROSS_AUDIT", "de Security")
    review_report = _require_report(review, REVIEW_KIND, "CROSS_AUDIT", "del Reviewer")
    task = _next_task(durable)
    return CrossAuditTask(
        task_id=request.task_id,
        project_id=request.project_id,
        objective=_objective(task, request),
        acceptance_criteria=_acceptance_criteria(task, request),
        changed_files=_changed_files(result, request),
        context_files=_context_files(task, request),
        deleted_files=(),
        workspace_path=str(_workspace(request)),
        project_spec_context=_project_spec_context(durable),
        architecture_context=_architecture_context(durable),
        diff_summary=_diff_summary(result),
        risk_level=RiskLevel.LOW if task is None else task.risk_level,
        authority_level=(
            AuthorityLevel.LEVEL_0_AUTONOMOUS if task is None else task.authority_level
        ),
        developer_result=result,
        qa_report=qa_report,
        security_report=security_report,
        review_report=review_report,
    )


def visual_qa_input(
    plan: DurablePlan | None,
    developer: DeveloperExecutionResult | None,
    request: RoleExecutionRequest,
    *,
    spec: VisualSpec | None = None,
    session: WebSessionReport | None = None,
) -> VisualQATask:
    """Construye la entrada oficial de ``VISUAL_QA`` desde artefactos durables y la capa web.

    El plan aporta el contrato de la tarea y el contexto de la arquitectura; el resultado del
    Developer aporta los hechos —qué archivos cambiaron—; la petición aporta identidad y workspace.

    ``spec`` y ``session`` se piden **explícitamente** porque las mide un navegador real, no este
    módulo: el adaptador las obtiene de :func:`resolve_visual_evidence`, que devuelve la pareja
    publicada con :func:`publish_visual_evidence` desde el almacén del motor (``VISUAL_EVIDENCE``),
    así que la entrada se reconstruye en un proceso nuevo y no desde una variable del anterior.
    Cuando esa pareja no está, el resolutor devuelve ``None`` y esta función se niega a inventar una
    especificación sin rutas o una sesión con estado técnico ``PASS``: eso sería fabricar la
    evidencia contra la que se opina. ``source_context`` viaja vacío por el mismo motivo: el plan
    durable no contiene contexto de código, y rellenarlo con una lista de rutas sería disfrazar otra
    cosa de contexto.

    Raises:
        WorkflowIncompleteEvidenceError: si falta el plan, el resultado del Developer, la
            especificación visual o el informe de la sesión web.
    """
    durable = _require_plan(plan, "VISUAL_QA")
    result = _require_developer(developer, "VISUAL_QA")
    visual_spec = _require_web_evidence(spec, "la especificación visual (VisualSpec)")
    web_session = _require_web_evidence(session, "el informe técnico de la sesión web")
    task = _next_task(durable)
    return VisualQATask(
        task_id=request.task_id,
        project_id=request.project_id,
        objective=_objective(task, request),
        acceptance_criteria=_acceptance_criteria(task, request),
        spec=visual_spec,
        session=web_session,
        changed_files=_changed_files(result, request),
        context_files=_context_files(task, request),
        source_context="",
        architecture_context=_architecture_context(durable),
    )


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


def _publish_repair(
    store: ArtifactStore,
    *,
    request: RoleExecutionRequest,
    kind: str,
    label: str,
    content: BaseModel,
    function: str,
) -> ArtifactReference:
    """Publica uno de los tres sobres de reparación que son un modelo, con el rol comprobado.

    Plan, diagnóstico y snapshot comparten mecánica —comprobar que la petición es del paso que
    repara, envolver el modelo con su ``kind`` y escribir el JSON canónico—, así que comparten
    helper. Los defectos no pasan por aquí porque su sobre lleva una **lista**, no un modelo.

    Raises:
        ValueError: si la petición no es de la etapa ``DEVELOPER``.
    """
    return _publish_report(
        store,
        request=request,
        role=RoleName.DEVELOPER,
        kind=kind,
        label=label,
        report=content,
        function=function,
    )


def _publish_report(
    store: ArtifactStore,
    *,
    request: RoleExecutionRequest,
    role: RoleName,
    kind: str,
    label: str,
    report: BaseModel,
    function: str,
) -> ArtifactReference:
    """Publica el sobre de un informe de rol con el rol comprobado y el contenido saneado.

    Lo comparten los publicadores de informes —y los de reparación— porque la mecánica es la misma
    y solo cambia qué se serializa: comprobar que la petición es de la etapa que publica, envolver
    el informe con su ``kind`` y escribirlo con la versión del esquema y el JSON canónico. La
    comprobación va **antes** de tocar el disco: un artefacto etiquetado con el rol equivocado
    corrompería el handoff.

    Raises:
        ValueError: si la petición no es de ``role``.
    """
    _assert_role(request, role, function)
    payload: dict[str, object] = {_KIND_FIELD: kind, _CONTENT_FIELD: _bounded_json(report)}
    return store.put(
        workflow_id=request.workflow_id,
        role=role,
        step_index=request.step_index,
        kind=kind,
        label=label,
        data=_encode(payload),
    )


def _resolve_report[ModelT: BaseModel](
    store: ArtifactStore,
    references: tuple[ArtifactReference, ...],
    kind: str,
    model_type: type[ModelT],
) -> ModelT | None:
    """Reconstruye el informe de la **primera** referencia de ese tipo, o ``None`` si no hay.

    Se usa la primera en orden cronológico, que es la que el run declaró antes. Un sobre presente
    pero ilegible, de otro esquema o de otro ``kind`` no se degrada a ``None``: ``_decode`` y
    ``_validate`` lo convierten en ``WORKFLOW_RESUME_FAILED``, porque confundir corrupción con
    ausencia haría que la etapa siguiente trabajara sobre datos descartados en silencio.
    """
    for reference in references:
        if reference.kind != kind:
            continue
        payload = _decode(store.get(reference), expected_kind=kind, reference=reference)
        return _model_field(model_type, payload, _CONTENT_FIELD, reference)
    return None


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


def _bounded_json(model: BaseModel) -> dict[str, object]:
    """Serializa un informe listo para el almacén: sin credenciales y con todos sus textos acotados.

    Es la puerta por la que pasa **todo** informe de rol antes de convertirse en bytes. Se recorre
    el JSON ya serializado, así que la regla vale también para los textos anidados —la salida de un
    comando, la evidencia de un hallazgo, la valoración de una propuesta— que ningún contrato acota.
    Recorrer el JSON y no el modelo es deliberado: el modelo no se muta, y lo que se acota es
    exactamente lo que se va a escribir.
    """
    return cast("dict[str, object]", _bound_value(_json_dump(model)))


def _bound_value(value: object) -> object:
    """Recorre un valor JSON saneando y acotando todos los textos que contenga."""
    if isinstance(value, str):
        return _bounded_text(value)
    if isinstance(value, list):
        return [_bound_value(item) for item in cast("list[object]", value)]
    if isinstance(value, dict):
        mapping = cast("dict[str, object]", value)
        return {key: _bound_value(item) for key, item in mapping.items()}
    return value


def _bounded_text(value: str) -> str:
    """Redacta credenciales y recorta a la cota local, dejando marca explícita de lo hecho.

    El orden importa: primero se redacta y después se recorta, de modo que la marca de recorte nunca
    pueda partir una credencial por la mitad y dejar un fragmento reconocible en el artefacto.
    """
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_REDACTION_MARKER, redacted)
    return _excerpt(redacted, _MAX_PAYLOAD_TEXT_CHARS)


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


def _objective(task: PlannedTask | None, request: RoleExecutionRequest) -> str:
    """Objetivo de la etapa: el de la tarea lista del plan y, si no lo declara, el de la petición.

    La tarea lista se elige por orden del plan (``ready_tasks`` conserva ese orden), así que el
    mismo plan produce siempre el mismo objetivo. El valor por defecto solo aparece si ni el plan ni
    la petición dejan un carácter utilizable, porque los contratos de las tareas exigen un objetivo
    no vacío.
    """
    declared = _one_line(_task_text(task, "objective"))
    return _coalesce(declared, _one_line(request.objective), _DEFAULT_SLUG)


def _changed_files(
    developer: DeveloperExecutionResult, request: RoleExecutionRequest
) -> tuple[str, ...]:
    """Archivos que el Developer cambió **de verdad** y, si no declaró ninguno, los de la petición.

    El resultado durable del Developer es el registro real del cambio; los ``changed_files`` de la
    petición son lo que el llamante declaró antes de ejecutar. Cuando hay hechos, mandan los hechos.
    """
    declared = tuple(change.path for change in developer.files_changed if change.path)
    if declared:
        return declared
    return tuple(request.changed_files)


def _validation_checks(
    developer: DeveloperExecutionResult, task: PlannedTask | None
) -> tuple[str, ...]:
    """Checks que el Developer declaró en su resultado y, si no declaró ninguno, los del plan.

    El contrato de ``QATask`` dice que estos checks son **contexto, no prueba**: QA los recibe para
    saber qué se ejecutó, y su veredicto lo calcula PUNTO a partir de su propia ejecución.
    """
    validation = developer.validation
    if validation is not None:
        names = tuple(check.name for check in validation.checks if check.name)
        if names:
            return names
    return _declared(task, "validation_checks")


def _capability_profile(plan: DurablePlan) -> tuple[str, ...]:
    """Vocabulario de capacidades declarado por el Architect, en el orden del perfil y sin repetir.

    Se copian los **nombres** —``python312``, ``pytest``, ``node20``— porque es el vocabulario con
    el que el plan escribe ``required_capabilities``; añadir la familia convertiría cada nombre en
    algo que ningún contrato de capacidades reconoce.
    """
    profile = plan.capability_profile
    if profile is None:
        return ()
    return tuple(dict.fromkeys(name for _kind, name in profile.entries() if name))


def _architecture_context(plan: DurablePlan) -> str:
    """Resumen acotado de la arquitectura del plan, o cadena vacía si el bundle no la trae.

    Se compone con datos del contrato —estilo y componentes— y nunca con contenido de ficheros: el
    bundle durable no lleva código, y fabricarlo aquí sería inventar contexto.
    """
    architecture = plan.architecture
    if architecture is None:
        return ""
    components = ", ".join(component.id for component in architecture.components)
    summary = f"estilo: {architecture.architecture_style}"
    if components:
        summary = f"{summary}; componentes: {components}"
    return _excerpt(_one_line(summary), _MAX_CONTEXT_CHARS)


def _project_spec_context(plan: DurablePlan) -> str:
    """Resumen acotado de la especificación del plan, o cadena vacía si el bundle no la trae."""
    spec = plan.project_spec
    if spec is None:
        return ""
    return _excerpt(_one_line(f"{spec.project_name}: {spec.problem_statement}"), _MAX_CONTEXT_CHARS)


def _architecture_constraints(plan: DurablePlan) -> tuple[str, ...]:
    """Restricciones que la especificación del plan declara, acotadas y sin repetir.

    Son las únicas restricciones durables que existen: la arquitectura declara estilo, componentes y
    fronteras, no restricciones verificables de un cambio. Inventar restricciones a partir de la
    arquitectura haría que el Reviewer exigiera cosas que nadie escribió.
    """
    spec = plan.project_spec
    if spec is None:
        return ()
    bounded = (_excerpt(_one_line(item), _MAX_CONTEXT_CHARS) for item in spec.constraints)
    return tuple(dict.fromkeys(item for item in bounded if item))[:_MAX_CONTEXT_ITEMS]


def _diff_summary(developer: DeveloperExecutionResult) -> str:
    """Resumen controlado del cambio, compuesto solo con los hechos del resultado durable.

    Se declara qué archivo cambió, con qué operación y cuántos bytes: es lo que el contrato del
    resultado registra. No se copia contenido y no se inventa ningún diff, porque el handoff no lee
    el workspace ni tiene el contenido anterior.
    """
    lines = [
        f"- {change.path}: {change.operation.value.lower()}, {change.bytes_written} bytes"
        for change in developer.files_changed[:_MAX_CONTEXT_ITEMS]
        if change.path
    ]
    return _excerpt("\n".join(lines), _MAX_CONTEXT_CHARS)


def _require_plan(plan: DurablePlan | None, stage: str) -> DurablePlan:
    """Plan durable de la etapa, o ``WORKFLOW_INCOMPLETE_EVIDENCE`` diciendo qué falta y por qué.

    Raises:
        WorkflowIncompleteEvidenceError: si el resolutor no encontró plan durable. No se sustituye
            por el objetivo de la petición disfrazado de plan: el contrato de la tarea —criterios,
            capacidades, contexto— saldría inventado y la etapa evaluaría algo que nadie planificó.
    """
    if plan is None:
        raise WorkflowIncompleteEvidenceError(
            f"falta el plan durable ({PLAN_KIND}) del que {stage} toma el contrato de su tarea: "
            "sin él, el objetivo, los criterios y las capacidades se inventarían, y PUNTO no "
            "ejecuta una etapa sobre una suposición"
        )
    return plan


def _require_developer(
    developer: DeveloperExecutionResult | None, stage: str
) -> DeveloperExecutionResult:
    """Resultado durable del Developer, o ``WORKFLOW_INCOMPLETE_EVIDENCE`` diciendo qué falta.

    Raises:
        WorkflowIncompleteEvidenceError: si no hay resultado durable. Sin el trabajo real no hay
            nada que verificar, y fabricar una evidencia vacía haría que la etapa aprobara un cambio
            que quizá nunca ocurrió.
    """
    if developer is None:
        raise WorkflowIncompleteEvidenceError(
            f"falta el resultado durable del Developer ({DEVELOPER_KIND}) que {stage} debe "
            "evaluar: sin el trabajo real no hay nada que verificar y PUNTO no fabrica su evidencia"
        )
    return developer


def _require_report[ReportT: BaseModel](
    report: ReportT | None, kind: str, stage: str, requirement: str
) -> ReportT:
    """Informe previo del que la etapa depende, o ``WORKFLOW_INCOMPLETE_EVIDENCE``.

    Un informe previo es un **gate preceptivo**: falta o no, y no hay término medio. Rellenarlo con
    un informe inventado en estado favorable sería exactamente la aprobación falsa que los gates
    existen para impedir.

    Raises:
        WorkflowIncompleteEvidenceError: si el informe no está.
    """
    if report is None:
        raise WorkflowIncompleteEvidenceError(
            f"falta el informe durable {requirement} ({kind}) que {stage} necesita: es un gate "
            "preceptivo y PUNTO no lo sustituye por una aprobación inventada"
        )
    return report


def _require_web_evidence[EvidenceT: BaseModel](
    evidence: EvidenceT | None, description: str
) -> EvidenceT:
    """Evidencia de la capa web que la etapa visual exige, o ``WORKFLOW_INCOMPLETE_EVIDENCE``.

    Raises:
        WorkflowIncompleteEvidenceError: si falta. La especificación visual y la sesión técnica las
            mide PUNTO en un navegador real; no las produce ningún códec durable de esta fase, así
            que la única alternativa a faltar sería inventarlas.
    """
    if evidence is None:
        raise WorkflowIncompleteEvidenceError(
            f"falta {description} de la etapa VISUAL_QA: no tiene códec durable en esta fase "
            "—la produce la capa web— y PUNTO no inventa la evidencia contra la que se opina"
        )
    return evidence


def _declared_captures(session: WebSessionReport) -> tuple[ScreenshotArtifact, ...]:
    """Capturas declaradas por la sesión, o ``ValueError`` si no hay ninguna o se repiten.

    Una sesión sin capturas no publica nada de este códec: el caso «sin capturas» sigue siendo el
    que era —lo mantiene el adaptador— y un manifiesto vacío fingiría una evidencia que la sesión
    nunca midió. Dos capturas con el mismo nombre lógico tampoco se aceptan: el manifiesto se
    indexa por ese nombre y una de las dos quedaría sin entrada, que es justo el hueco silencioso
    que este códec existe para impedir.
    """
    declared = tuple(session.screenshots)
    if not declared:
        raise ValueError(
            "no se publican capturas de una sesión que no declara ninguna: el caso «sin capturas» "
            "no deja bytes ni manifiesto, y un índice vacío fingiría una evidencia que la sesión "
            "no midió"
        )
    names = [artifact.logical_name for artifact in declared]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(
            f"la sesión declara más de una captura con el mismo nombre lógico {duplicates}: dos "
            "imágenes distintas no se pueden atar a la misma entrada del manifiesto"
        )
    return declared


def _capture_payloads(
    declared: tuple[ScreenshotArtifact, ...], images: Mapping[str, ImagePayload]
) -> Mapping[str, ImagePayload]:
    """Payloads indexados por nombre lógico declarado, o ``ValueError`` si el mapa no encaja.

    Se exige el conjunto **exacto**: que falte una declarada y que sobre una que la sesión no
    declara son los dos rechazos, y ambos ocurren antes de escribir un solo byte. Una captura de más
    no puede colarse en el manifiesto ni, sobre todo, ocultar una requerida que falte.
    """
    names = {artifact.logical_name for artifact in declared}
    missing = sorted(names - set(images))
    if missing:
        raise ValueError(
            f"la sesión declara {len(names)} captura(s) y no llegó el payload de {missing}: el "
            "manifiesto ataría la evidencia a una imagen que no existe"
        )
    extra = sorted(set(images) - names)
    if extra:
        raise ValueError(
            f"llegaron payloads de capturas que la sesión no declara ({extra}): una captura de más "
            "no puede colarse en el manifiesto ni ocultar una requerida que falte"
        )
    return {name: images[name] for name in names}


def _first_reference(
    references: tuple[ArtifactReference, ...], kind: str
) -> ArtifactReference | None:
    """Primera referencia del tipo indicado, en el orden en que el run las declaró."""
    for reference in references:
        if reference.kind == kind:
            return reference
    return None


@dataclass(frozen=True, slots=True)
class _CaptureIndex:
    """Manifiesto de capturas ya leído: la identidad de sesión que declara y su índice por nombre.

    La identidad viaja con el índice y no por separado porque son **el mismo artefacto**: si el
    manifiesto es de otra sesión, sus capturas tampoco son las de esta, así que leerlas por separado
    invitaría a resolver unas con la identidad de otras. Es privado a propósito: la identidad del
    manifiesto es un detalle del códec, no parte del contrato de ``resolve_screenshots``, que
    devuelve solo bytes verificados.
    """

    task_id: str
    project_id: str
    session_id: str
    entries: Mapping[str, dict[str, object]]


def _read_capture_manifest(store: ArtifactStore, reference: ArtifactReference) -> _CaptureIndex:
    """Lee el manifiesto de capturas, con su identidad de sesión y su índice por nombre lógico.

    Un manifiesto ausente, ilegible, de otro esquema, sin identidad de sesión, sin lista de
    capturas, con una entrada que no es un objeto o con dos entradas para el mismo nombre lógico no
    se interpreta «lo mejor posible»: es evidencia incompleta, porque el índice que ata cada captura
    a sus bytes no es de fiar y sin él no se puede saber qué imagen se midió ni en qué replay.

    Leer la identidad aquí —y no en una segunda pasada del almacén— es deliberado: el manifiesto se
    verifica por digest una sola vez y la identidad que se compara es la del **mismo** contenido que
    aporta las capturas.

    Raises:
        WorkflowIncompleteEvidenceError: si el manifiesto no se puede leer, no tiene la forma que
            este códec escribió o no declara la identidad de su sesión web.
    """
    try:
        data = store.get(reference)
    except (WorkflowResumeFailedError, WorkflowCheckpointInvalidError) as error:
        raise WorkflowIncompleteEvidenceError(
            f"no se pudo leer el manifiesto durable de capturas ({reference.reference!r}): "
            f"{error.detail or error}"
        ) from error
    try:
        payload = _decode(data, expected_kind=SCREENSHOT_MANIFEST_KIND, reference=reference)
    except WorkflowResumeFailedError as error:
        raise WorkflowIncompleteEvidenceError(
            f"el manifiesto durable de capturas ({reference.reference!r}) no es un sobre legible "
            f"de esta versión: {error.detail or error}"
        ) from error
    raw = payload.get(_SCREENSHOTS_FIELD)
    if not isinstance(raw, list):
        raise WorkflowIncompleteEvidenceError(
            f"el manifiesto {reference.reference!r} no lleva una lista en "
            f"{_SCREENSHOTS_FIELD!r} sino {type(raw).__name__}: no hay índice que resolver"
        )
    entries: dict[str, dict[str, object]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise WorkflowIncompleteEvidenceError(
                f"el manifiesto {reference.reference!r} lleva una entrada que no es un objeto JSON "
                f"sino {type(item).__name__}"
            )
        entry = cast("dict[str, object]", item)
        name = entry.get(_LOGICAL_NAME_FIELD)
        if not isinstance(name, str) or not name:
            raise WorkflowIncompleteEvidenceError(
                f"una entrada del manifiesto {reference.reference!r} no declara un nombre lógico "
                "utilizable: sin él la captura no se puede atar a su evidencia"
            )
        if name in entries:
            raise WorkflowIncompleteEvidenceError(
                f"el manifiesto {reference.reference!r} lleva dos entradas para el nombre lógico "
                f"{name!r}: no se puede saber cuál de las dos es la captura medida"
            )
        entries[name] = entry
    return _CaptureIndex(
        task_id=_identity_field(payload, _TASK_ID_FIELD, "tarea", reference),
        project_id=_identity_field(payload, _PROJECT_ID_FIELD, "proyecto", reference),
        session_id=_identity_field(payload, _SESSION_ID_FIELD, "sesión", reference),
        entries=entries,
    )


def _identity_field(
    payload: Mapping[str, object], field: str, label: str, reference: ArtifactReference
) -> str:
    """Campo de identidad del manifiesto, o evidencia incompleta si no es un texto utilizable.

    Un manifiesto que no declara con qué sesión se midió no se interpreta «lo mejor posible»:
    aceptarlo sería resolver evidencia que no se puede atribuir a ningún replay. El fallo se dice
    con el nombre de la identidad que falta —tarea, proyecto o sesión— porque es lo que permite
    distinguir un índice de otro replay de un índice incompleto.

    Raises:
        WorkflowIncompleteEvidenceError: si el campo no está o no es un texto no vacío.
    """
    raw = payload.get(field)
    if not isinstance(raw, str) or not raw:
        raise WorkflowIncompleteEvidenceError(
            f"el manifiesto {reference.reference!r} no declara la identidad de {label} de la "
            f"sesión web ({field}={raw!r}): sin ella la evidencia no se puede atribuir al replay "
            "que se quiere analizar y analizarla sería aprobar sin haber mirado"
        )
    return raw


def _assert_same_session(
    index: _CaptureIndex, session: WebSessionReport, reference: ArtifactReference
) -> None:
    """Comprueba que el manifiesto es el de la sesión que se quiere resolver (V605-06).

    El manifiesto ya guardaba la identidad de la sesión, pero el resolutor no la comparaba: un
    manifiesto de otro replay —mismo proyecto, mismas capturas y los mismos nombres lógicos— se
    resolvía como el de la sesión pedida y Visual QA habría analizado la evidencia de otra
    ejecución. Se comparan las **tres** identidades y se falla con la primera que no cuadra,
    diciendo cuál es y qué se esperaba; el orden es el de la cadena de atribución —tarea,
    proyecto, sesión—, de modo que el detalle nombra el hecho más general que falla antes que el
    más específico.

    La comprobación ocurre antes de leer ningún byte de captura, así que un manifiesto de otro
    replay no consume el almacén ni entrega un mapa a medias.

    Raises:
        WorkflowIncompleteEvidenceError: si la tarea, el proyecto o la sesión que declara el
            manifiesto no son los de ``session``. La evidencia de otro replay es un hueco de
            evidencia, nunca una aprobación.
    """
    identities: tuple[tuple[str, str, str, str], ...] = (
        ("tarea", _TASK_ID_FIELD, index.task_id, str(session.task_id)),
        ("proyecto", _PROJECT_ID_FIELD, index.project_id, str(session.project_id)),
        ("sesión", _SESSION_ID_FIELD, index.session_id, str(session.id)),
    )
    for label, field, stored, expected in identities:
        if stored != expected:
            raise WorkflowIncompleteEvidenceError(
                f"el manifiesto {reference.reference!r} es de otra sesión web: declara la "
                f"identidad de {label} {stored!r} ({field}) y la sesión que se quiere resolver "
                f"declara {expected!r}. La evidencia es de otro replay: entregarla sería analizar "
                "la ejecución equivocada y aprobar sin haber mirado la pedida"
            )


def _capture_entry(
    entries: Mapping[str, dict[str, object]],
    declared: ScreenshotArtifact,
    name: str,
    manifest: ArtifactReference,
) -> dict[str, object]:
    """Entrada del manifiesto de una captura declarada, o ``WORKFLOW_INCOMPLETE_EVIDENCE``.

    Distingue dos hechos que no son lo mismo: que la captura **no esté** en el índice —un hueco de
    evidencia— y que sus bytes estén bajo **otro** nombre lógico, que es una manipulación del
    emparejamiento entre la captura y su contenido y se dice como tal.

    Raises:
        WorkflowIncompleteEvidenceError: si el manifiesto no ata esa captura a ningún contenido.
    """
    entry = entries.get(name)
    if entry is not None:
        return entry
    impostors = sorted(
        other for other, candidate in entries.items() if candidate.get("sha256") == declared.sha256
    )
    if impostors:
        raise WorkflowIncompleteEvidenceError(
            f"la sesión declara la captura {name!r} y el manifiesto {manifest.reference!r} ata "
            f"esos mismos bytes al nombre lógico {impostors!r}: el emparejamiento entre la captura "
            "y su evidencia fue manipulado"
        )
    raise WorkflowIncompleteEvidenceError(
        f"la sesión declara la captura {name!r} y el manifiesto {manifest.reference!r} no lleva "
        "ninguna entrada para ella: sin entrada no hay bytes que verificar"
    )


def _manifest_capture(
    entry: Mapping[str, object], manifest: ArtifactReference, name: str
) -> tuple[ScreenshotArtifact, ArtifactReference]:
    """Reconstruye la evidencia canónica y la referencia de bytes de una entrada del manifiesto.

    La evidencia se reconstruye contra el contrato de :class:`ScreenshotArtifact` y la referencia
    contra :class:`ArtifactReference`: una entrada que no valida contra ellos es un índice
    manipulado, no una captura a la que le falte un campo.

    Raises:
        WorkflowIncompleteEvidenceError: si la entrada o su referencia no validan contra el
            contrato.
    """
    raw = {key: value for key, value in entry.items() if key != _REFERENCE_FIELD}
    try:
        artifact = ScreenshotArtifact.model_validate(raw)
    except ValidationError as error:
        raise WorkflowIncompleteEvidenceError(
            f"la entrada {name!r} del manifiesto {manifest.reference!r} no valida contra "
            f"ScreenshotArtifact: {error}"
        ) from error
    try:
        reference = ArtifactReference.model_validate(entry.get(_REFERENCE_FIELD))
    except ValidationError as error:
        raise WorkflowIncompleteEvidenceError(
            f"la entrada {name!r} del manifiesto {manifest.reference!r} no lleva una referencia de "
            f"bytes válida: {error}"
        ) from error
    return artifact, reference


def _assert_same_capture(
    declared: ScreenshotArtifact, stored: ScreenshotArtifact, name: str
) -> None:
    """Comprueba que la evidencia del manifiesto es la que la sesión declara.

    Se comparan los campos que atan la captura a su contenido y a su contexto —nombre lógico, ruta,
    viewport, media type, tamaño y sha256— y se falla con el detalle del primero que no cuadra. Los
    textos se comparan en su forma acotada, que es la que el manifiesto guarda: la misma que ya
    viajó en el sobre de la sesión.

    Raises:
        WorkflowIncompleteEvidenceError: si algún campo no coincide. Dos evidencias distintas para
            la misma captura significan que el índice durable no es de fiar.
    """
    if stored.logical_name != _bounded_text(declared.logical_name):
        raise WorkflowIncompleteEvidenceError(
            f"la entrada del manifiesto ata la captura {name!r} al nombre lógico "
            f"{stored.logical_name!r}: el índice no corresponde a la captura que la sesión declara"
        )
    if stored.route != _bounded_text(declared.route):
        raise WorkflowIncompleteEvidenceError(
            f"la ruta canónica de la captura {name!r} es {stored.route!r} y la sesión declara "
            f"{declared.route!r}: no es la misma captura"
        )
    if stored.viewport != declared.viewport:
        raise WorkflowIncompleteEvidenceError(
            f"el viewport canónico de la captura {name!r} es {stored.viewport.value!r} y la sesión "
            f"declara {declared.viewport.value!r}: no es la misma captura"
        )
    if stored.media_type != _bounded_text(declared.media_type):
        raise WorkflowIncompleteEvidenceError(
            f"el media type canónico de la captura {name!r} es {stored.media_type!r} y la sesión "
            f"declara {declared.media_type!r}: no es la misma captura"
        )
    if stored.bytes != declared.bytes:
        raise WorkflowIncompleteEvidenceError(
            f"la evidencia canónica de la captura {name!r} declara {stored.bytes} bytes y la "
            f"sesión declara {declared.bytes}: el índice y la sesión no miden lo mismo"
        )
    if stored.sha256 != declared.sha256:
        raise WorkflowIncompleteEvidenceError(
            f"el sha256 canónico de la captura {name!r} no coincide con el que declara la sesión: "
            "el índice y la sesión no atan la misma imagen"
        )


def _verified_payload(
    store: ArtifactStore,
    reference: ArtifactReference,
    stored: ScreenshotArtifact,
    name: str,
) -> ImagePayload:
    """Bytes del almacén revalidados contra la evidencia canónica del manifiesto.

    Traduce **todos** los fallos de integridad a ``WORKFLOW_INCOMPLETE_EVIDENCE`` con un detalle
    distinto por caso: unos bytes que no están, una referencia que el almacén no puede verificar,
    unos bytes de otro tamaño y unos bytes del tamaño declarado cuyo hash no cuadra. En esta función
    cualquiera de los cuatro significa «la captura durable no es de fiar», que es un hueco de
    evidencia recuperable y no una corrupción del workflow.

    Raises:
        WorkflowIncompleteEvidenceError: si los bytes no están, no se pueden leer, no superan la
            verificación del almacén o no superan la validación canónica.
    """
    try:
        data = store.get(reference)
    except WorkflowResumeFailedError as error:
        raise WorkflowIncompleteEvidenceError(
            f"los bytes durables de la captura {name!r} no están en el almacén "
            f"({reference.reference!r}): {error.detail or error}"
        ) from error
    except WorkflowCheckpointInvalidError as error:
        raise WorkflowIncompleteEvidenceError(
            f"la referencia de bytes de la captura {name!r} no supera la verificación de "
            f"integridad del almacén ({reference.reference!r}): {error.detail or error}"
        ) from error
    if len(data) != stored.bytes:
        raise WorkflowIncompleteEvidenceError(
            f"los bytes durables de la captura {name!r} ocupan {len(data)} y su evidencia canónica "
            f"declara {stored.bytes}: la imagen no es la que se midió"
        )
    try:
        return stored.as_image_payload(data)
    except ValueError as error:
        raise WorkflowIncompleteEvidenceError(
            f"los bytes durables de la captura {name!r} tienen el tamaño declarado pero su hash no "
            f"cuadra con el sha256 canónico: {error}"
        ) from error


def _bounded_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Sanea y acota un sobre construido a mano, campo a campo, antes de serializarlo.

    Es la variante de :func:`_bounded_json` para sobres que no son un modelo pydantic —el manifiesto
    de capturas lo es— y aplica la misma redacción de credenciales y la misma cota de texto al JSON
    que se va a escribir. Nunca toca los bytes: los binarios no viven en este sobre.
    """
    return cast("dict[str, object]", _bound_value(dict(payload)))


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
    "CROSS_AUDIT_KIND",
    "DEVELOPER_KIND",
    "HANDOFF_SCHEMA_VERSION",
    "PLAN_KIND",
    "QA_KIND",
    "REPAIR_DIAGNOSIS_KIND",
    "REPAIR_FINDINGS_KIND",
    "REPAIR_PLAN_KIND",
    "REPAIR_SNAPSHOT_KIND",
    "REVIEW_KIND",
    "SCREENSHOT_KIND",
    "SCREENSHOT_MANIFEST_KIND",
    "SECURITY_KIND",
    "VISUAL_EVIDENCE_KIND",
    "VISUAL_QA_KIND",
    "DurablePlan",
    "cross_audit_input",
    "developer_input",
    "publish_architecture",
    "publish_cross_audit",
    "publish_developer",
    "publish_plan",
    "publish_qa",
    "publish_repair_diagnosis",
    "publish_repair_findings",
    "publish_repair_plan",
    "publish_repair_snapshot",
    "publish_review",
    "publish_screenshots",
    "publish_security",
    "publish_visual_evidence",
    "publish_visual_qa",
    "qa_input",
    "resolve_architecture",
    "resolve_cross_audit",
    "resolve_developer",
    "resolve_plan",
    "resolve_qa",
    "resolve_repair_diagnosis",
    "resolve_repair_findings",
    "resolve_repair_plan",
    "resolve_repair_snapshot",
    "resolve_review",
    "resolve_screenshots",
    "resolve_security",
    "resolve_visual_evidence",
    "resolve_visual_qa",
    "review_input",
    "security_input",
    "visual_qa_input",
]
