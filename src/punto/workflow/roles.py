"""Adaptación de los roles existentes al contrato del kernel (ENGINE-6.0).

Por qué existe esta capa
------------------------
El kernel no reescribe los roles: los **invoca**. Esta capa es la frontera que lo hace posible
sin tocar Architect, Planner, Developer, QA, Security, Reviewer, Cross-Audit ni Visual QA, y
tiene tres piezas:

- :class:`RoleExecutor`, el contrato que el kernel conoce: ejecutar una
  :class:`~punto.schemas.workflow.RoleExecutionRequest` y declarar qué capacidad cubre un rol.
- los ``normalize_*``, funciones **puras** que traducen el informe real de cada rol
  (``ArchitectureOutcome``, ``PlanningOutcome``, ``DeveloperExecutionResult``, ``QAReport``,
  ``SecurityReport``, ``ReviewReport``, ``CrossAuditReport``, ``VisualQAReport``) a
  :class:`~punto.schemas.workflow.RoleExecutionResult`. No llaman a ningún modelo, no tocan el
  disco y no deciden nada: leen el estado real y lo copian.
- tres adaptadores: :class:`CallableRoleExecutor` (una función), :class:`UnavailableRoleExecutor`
  (rol sin proveedor disponible) y :class:`CamusRoleExecutor` (CAMUS real).

Reglas de normalización, explícitas porque son el contrato
----------------------------------------------------------
**Estado.** Se lee el estado real del informe; nunca se inventa:

- ``PASS`` / ``APPROVED`` / ``SUCCESS`` → ``RoleStatus.COMPLETED``;
- ``FAIL`` / ``CHANGES_REQUESTED`` / ``FAILED`` / ``TIMEOUT`` → ``RoleStatus.NEEDS_REPAIR``
  (el rol emitió un veredicto negativo: hay algo que rehacer);
- ``BLOCKED`` → ``RoleStatus.BLOCKED``;
- cualquier otro estado, o ninguno → ``RoleStatus.FAILED`` con
  ``WorkflowFailureCode.WORKFLOW_ROLE_FAILED``.

Un estado desconocido **nunca** se traduce en ``COMPLETED``: no entender un veredicto no puede
convertirse en aprobarlo, y el detalle del fallo dice qué estado llegó.

**Hallazgos.** Se copian con su gravedad real (``FindingSeverity``), acotados a
``MAX_WORKFLOW_FINDINGS`` y con los textos recortados a ``MAX_WORKFLOW_TEXT_CHARS``. Una gravedad
que no se pueda leer se traduce a ``MEDIUM``: no se degrada a ``INFO`` porque eso minimizaría el
riesgo en silencio. El Developer no declara hallazgos ni gravedades, así que su resultado no
inventa ninguno: sus hechos viajan en ``artifacts``, ``summary`` y ``recommendation``.

**Valores neutros.** ``summary``, ``provider``, ``model``, ``usage``, ``attempts``,
``started_at`` y ``completed_at`` salen del informe cuando los declara. Cuando no: un resumen
``ROL: ESTADO``, cadena vacía, ``ModelUsage()`` vacío, el intento técnico real de la petición
(``request.attempt``) y la hora de normalización. Son valores declarados, no deducciones.

**Secretos.** Solo se copian campos de una lista cerrada. El resto del informe —credenciales,
notas privadas, volcados— no se lee, de modo que no puede acabar en un checkpoint, en un log ni
en un informe de auditoría.

Nada de fallback silencioso
---------------------------
Un rol sin proveedor no se sustituye por otro: se devuelve un resultado ``PROVIDER_UNAVAILABLE``
con el código ``WORKFLOW_PROVIDER_UNAVAILABLE`` y un detalle que dice **qué rol** y **qué falta**.
Los runners de CAMUS son *opt-in*: si el rol no está inyectado, su ``*NotConfiguredError`` se
traduce a ese mismo resultado en lugar de improvisar una evaluación.

Una etapa, una llamada
----------------------
Cada rol ejecuta **solo** su etapa: el ``ARCHITECT`` llama a ``camus.analyze_project`` y el
``PLANNER`` a ``camus.plan_project_from_architecture``, nunca a ``camus.plan_project`` —que es la
composición de las dos—. El Planner recibe el diseño del Architect reconstruido desde la
referencia durable de la petición; si esa referencia no resuelve a un diseño válido, la etapa
falla con ``WORKFLOW_ROLE_FAILED`` en vez de repetir el diseño por su cuenta.

El handoff durable cubre toda la pipeline (ENGINE-6.0.2 y V603-04)
-----------------------------------------------------------------
El adaptador **real** no necesita ninguna *closure* externa para el handoff (defectos V602-03 y
V603-04): con un :class:`~punto.workflow.artifacts.ArtifactStore` inyectado en ``artifacts``, el
adaptador reconstruye la entrada de **todos** los roles desde las referencias durables del
``RoleExecutionRequest`` y el contenido del almacén. No hay ningún rol del pipeline que exija una
*closure*:

- el ``ARCHITECT`` deriva su ``ProjectIntent`` de la petición y publica su ``ArchitectureOutcome``
  completo con :func:`~punto.workflow.handoff.publish_architecture`, reportando la referencia en
  ``artifact_references``;
- el ``PLANNER`` resuelve el diseño desde las referencias de la petición
  (:func:`~punto.workflow.handoff.resolve_architecture`), planifica sobre él y publica el bundle
  durable del plan con :func:`~punto.workflow.handoff.publish_plan`;
- el ``DEVELOPER`` resuelve el plan durable
  (:func:`~punto.workflow.handoff.resolve_plan`) y construye su pareja
  ``(DeveloperTask, ExecutionContext)`` con el constructor oficial
  :func:`~punto.workflow.handoff.developer_input`; si el paso trae un plan de reparación, añade el
  ``RepairTask`` completo al mismo encargo;
- ``QA``, ``SECURITY``, ``REVIEWER``, ``CROSS_AUDIT`` y ``VISUAL_QA`` reconstruyen su tarea desde el
  plan durable y los informes durables de las etapas anteriores (``qa_input``, ``security_input``,
  ``review_input``, ``cross_audit_input`` y ``visual_qa_input``). Si falta un artefacto del que
  dependen, la etapa se declara con evidencia incompleta (``WORKFLOW_INCOMPLETE_EVIDENCE``) en vez
  de inventar la entrada.

Un ``build_input`` explícito sigue siendo válido y **manda** cuando se inyecta —es el contrato que
ya existía—, pero es solo el atajo: el que usan los dobles de prueba y los llamantes que construyen
su propia entrada. No es un requisito de ningún rol, y sin él el camino de producción es el almacén.

Reparación con el mismo Developer (ENGINE-6.1.1)
------------------------------------------------
Cuando el paso ``DEVELOPER`` trae un plan de reparación entre sus referencias, el adaptador resuelve
del almacén el encargo completo —plan, diagnóstico, defectos y snapshot— y se lo entrega al
**mismo** runner de siempre dentro de la misma tarea (``DeveloperTask.repair``). No hay un segundo
Developer ni un ejecutor de reparación: lo que cambia es el contexto.

Dos reglas duras de esta rama, y las dos son fail-closed:

- **nada a medias**: si hay plan de reparación y falta el diagnóstico, los defectos o el snapshot,
  la etapa se declara incompleta (``WORKFLOW_INCOMPLETE_EVIDENCE``) y el kernel la convierte en
  ``BLOCKED``. PUNTO no repara con medio encargo ni vuelve a diagnosticar por su cuenta.
- **el runner tiene que declararlo**: la reparación se ejecuta por ``camus.execute_repair_task``,
  que exige ``DeveloperRunner.supports_repair_context``. Un runner que no sepa recibir el contexto
  no lo recibe: la etapa falla de forma explícita en vez de ejecutar la reparación como una tarea
  normal, sin reglas duras ni autorización acotada.

Llamadas reales al modelo (V602-04-B)
-------------------------------------
``RoleExecutionResult.model_calls`` sale del informe real de cada rol: del ``ModelExecutionSummary``
del Architect y el Planner, del contador ``model_calls`` del resto de informes y, si el informe no
declara llamadas, de los intentos que sí declara. **Nunca** se deduce de los tokens: un rol puede
llamar tres veces gastando pocos tokens o una sola gastando muchos.

El Developer es la excepción, y está documentada: su informe lleva un contador propio que
**siempre** existe, así que un ``0`` es un hecho —no llamó— y no se suple con los intentos. Suplirlo
convertía el intento de un runner determinista en una llamada de modelo inexistente y la
postcondición del kernel bloqueaba el workflow por un gasto que nunca ocurrió (hallazgo V605-05).

Saldo de modelo autorizado (V602-04-C)
--------------------------------------
El kernel calcula el saldo del intento (``RoleExecutionRequest.budget_allowance``) **antes** de
invocar al rol y no lo invoca si está agotado. El adaptador real comprueba además ese saldo justo
antes de llamar a CAMUS: con cero llamadas o cero tokens autorizados devuelve
``WORKFLOW_BUDGET_EXCEEDED`` sin tocar el proveedor. El gasto ocurre dentro del adaptador, así que
la cota del kernel tiene que estar también en la frontera que gasta.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol, cast, runtime_checkable

from punto.architect.base import ArchitectLimits, ArchitectureOutcome
from punto.common import utc_now
from punto.planner.base import PlannerLimits, PlanningOutcome
from punto.schemas.cross_audit import CrossAuditReport
from punto.schemas.enums import FindingSeverity
from punto.schemas.execution import (
    DeveloperExecutionResult,
    DeveloperInvocationLimits,
    ExecutionTrustLevel,
    ModelUsage,
)
from punto.schemas.planning import ModelExecutionSummary, ProjectIntent
from punto.schemas.qa import QAReport
from punto.schemas.repair import REPAIR_OBJECTIVE, RepairTask
from punto.schemas.review import ReviewReport
from punto.schemas.security import SecurityReport
from punto.schemas.visual import VisualQAReport
from punto.schemas.workflow import (
    MAX_WORKFLOW_ARTIFACTS,
    MAX_WORKFLOW_FINDINGS,
    MAX_WORKFLOW_SUMMARY_CHARS,
    MAX_WORKFLOW_TEXT_CHARS,
    ArtifactReference,
    ModelCallLimits,
    ProviderCapability,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowFailureCode,
    WorkflowFinding,
)
from punto.tools.errors import (
    ArchitectRunnerNotConfiguredError,
    CrossAuditRunnerNotConfiguredError,
    DeveloperExecutionError,
    DeveloperRunnerNotConfiguredError,
    PlannerRunnerNotConfiguredError,
    PlanningValidationError,
    QARunnerNotConfiguredError,
    ReviewerRunnerNotConfiguredError,
    SecurityRunnerNotConfiguredError,
    VisualQARunnerNotConfiguredError,
)
from punto.workflow.artifacts import ArtifactStore
from punto.workflow.errors import (
    WorkflowCheckpointInvalidError,
    WorkflowError,
    WorkflowIncompleteEvidenceError,
    WorkflowProviderUnavailableError,
    WorkflowResumeFailedError,
)
from punto.workflow.handoff import (
    REPAIR_DIAGNOSIS_KIND,
    cross_audit_input,
    developer_input,
    publish_architecture,
    publish_cross_audit,
    publish_developer,
    publish_plan,
    publish_qa,
    publish_review,
    publish_security,
    publish_visual_qa,
    qa_input,
    resolve_architecture,
    resolve_developer,
    resolve_plan,
    resolve_qa,
    resolve_repair_diagnosis,
    resolve_repair_findings,
    resolve_repair_plan,
    resolve_repair_snapshot,
    resolve_review,
    resolve_screenshots,
    resolve_security,
    resolve_visual_evidence,
    review_input,
    security_input,
    visual_qa_input,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from punto.developer.context import ExecutionContext
    from punto.orchestrator.camus import Camus
    from punto.providers.base import ImagePayload
    from punto.schemas.cross_audit import CrossAuditTask
    from punto.schemas.execution import DeveloperTask
    from punto.schemas.qa import QATask
    from punto.schemas.review import ReviewTask
    from punto.schemas.security import SecurityTask
    from punto.schemas.visual import VisualQATask
    from punto.schemas.web import WebSessionReport
    from punto.workflow.providers import ProviderCapabilityRegistry

#: Espejos de los máximos de ``RoleExecutionResult`` que no se exportan como constante propia.
_MAX_PROVIDER_CHARS: Final[int] = 40
_MAX_MODEL_CHARS: Final[int] = 120
#: Espejo del máximo de ``WorkflowFinding.category``.
_MAX_CATEGORY_CHARS: Final[int] = 80
#: Espejo del máximo de ``RoleExecutionResult.model_calls``: el contrato lo acota a ``le=64``.
_MAX_MODEL_CALLS: Final[int] = 64
#: Caracteres por token que supone la estimación **conservadora** de entrada (hallazgo V604-01).
#:
#: Los tokenizadores reales rondan cuatro caracteres por token en texto latino; suponer dos
#: sobreestima la entrada, que es la dirección segura: preferimos rechazar una ejecución que
#: autorizarla con un presupuesto que quizá no alcance.
_CHARS_PER_TOKEN: Final[int] = 2
#: Sobrecarga fija del prompt (instrucciones, formato y ejemplos) que ningún rol declara.
_PROMPT_OVERHEAD_TOKENS: Final[int] = 1_000
#: Cota del nombre de la intención que se deriva del objetivo de la petición.
_MAX_INTENT_NAME_CHARS: Final[int] = 120
#: Nombre de la intención cuando el objetivo no deja ni un carácter utilizable.
_DEFAULT_INTENT_NAME: Final[str] = "workflow"

#: Estados reales que significan «el rol hizo su trabajo y es aceptable».
_COMPLETED_STATES: Final[frozenset[str]] = frozenset({"PASS", "APPROVED", "SUCCESS"})
#: Estados reales que significan «hay algo que rehacer».
_REPAIR_STATES: Final[frozenset[str]] = frozenset(
    {"FAIL", "CHANGES_REQUESTED", "FAILED", "TIMEOUT"}
)
#: Estados reales que significan «no se pudo trabajar».
_BLOCKED_STATES: Final[frozenset[str]] = frozenset({"BLOCKED"})

#: Texto con el que se sustituye un hallazgo que no trae ni título ni descripción.
_NO_DETAIL: Final[str] = "hallazgo sin detalle declarado"

#: Errores tipados de CAMUS que significan «este rol no está configurado».
#:
#: Se listan uno a uno porque no comparten una base común más allá de ``RuntimeError``: cada
#: familia de rol tiene la suya. Traducirlos a ``PROVIDER_UNAVAILABLE`` es lo que impide que un
#: rol ausente se disfrace de otro proveedor.
_NOT_CONFIGURED_ERRORS: Final[tuple[type[Exception], ...]] = (
    ArchitectRunnerNotConfiguredError,
    PlannerRunnerNotConfiguredError,
    DeveloperRunnerNotConfiguredError,
    QARunnerNotConfiguredError,
    SecurityRunnerNotConfiguredError,
    ReviewerRunnerNotConfiguredError,
    CrossAuditRunnerNotConfiguredError,
    VisualQARunnerNotConfiguredError,
)

#: Firma común de los normalizadores puros de rol.
_RoleNormalizer = Callable[[object, RoleExecutionRequest], RoleExecutionResult]


@dataclass(frozen=True, slots=True)
class _EffectiveCap:
    """Cota de gasto autorizada para **esta** ejecución.

    No es un booleano «hay saldo»: es el número de llamadas y de **tokens totales** (entrada más
    salida) que la etapa puede gastar como máximo (hallazgos V603-01 y V604-01). Se traduce a los
    límites reales del rol cuando su contrato los admite por petición, y se usa para rechazar la
    ejecución cuando el gasto declarado del runner no cabe en ella.
    """

    model_calls: int
    total_tokens: int


def estimate_input_tokens(payload: object, request: RoleExecutionRequest) -> int:
    """Estimación **conservadora** de los tokens de entrada que enviará el rol al modelo.

    Orden de preferencia del hallazgo V604-01: cuando el proveedor o el tokenizador permiten conocer
    el conteo exacto, se usa el conteo exacto; cuando no, se usa esta estimación, que es deliberada
    **pesimista** —supone dos caracteres por token, cuando los tokenizadores reales rondan cuatro en
    texto latino— y añade la sobrecarga del prompt de sistema. Nunca optimista: si el presupuesto no
    cabe con la estimación conservadora, no se llama al modelo.

    La estimación se calcula sobre lo que el rol **ve**: el texto de su entrada declarada (objetivo,
    criterios, ficheros, resumen de contexto) más el de la carga útil que PUNTO le entrega. Un
    conteo exacto se inyecta con ``input_estimator`` en el constructor del adaptador.

    Límite declarado (H1 de ENGINE-6.0.6): esto es una **heurística por caracteres**, no una medida
    exacta. No se puede afirmar que sea exacta para cualquier tokenizer ni para cualquier texto
    Unicode —los tokenizadores reales reparten los caracteres no latinos de otra forma—, así que se
    mantiene como estimación conservadora y documentada hasta que el tokenizer exacto del proveedor
    esté disponible; entonces se inyecta y esta función deja de usarse en ese camino.
    """
    parts = (
        request.objective,
        *request.acceptance_criteria,
        *request.changed_files,
        request.context_summary,
        *(reference.label for reference in request.references),
        _text_of(payload),
    )
    characters = sum(len(part) for part in parts if part)
    return _PROMPT_OVERHEAD_TOKENS + -(-characters // _CHARS_PER_TOKEN)


def _text_of(payload: object) -> str:
    """Texto de la carga útil de un rol, para estimar su tamaño de entrada.

    Se prefiere la serialización del contrato (pydantic) y, si no la hay, su representación: lo que
    importa es el orden de magnitud, no el formato, porque la estimación es pesimista a propósito.
    """
    dump = getattr(payload, "model_dump_json", None)
    if callable(dump):
        return str(dump())
    return repr(payload)


def _effective_token_limits(
    *, base_input: int, base_output: int, cap: _EffectiveCap, input_tokens: int
) -> tuple[int, int]:
    """Reparte el saldo total entre entrada y salida sin que la suma lo rebase.

    Regla del hallazgo V604-01: ``entrada_autorizada + salida_autorizada <= total``. La entrada se
    autoriza por lo que el rol **va a enviar** (la estimación, con un mínimo de un token para que
    el contrato del rol siga siendo válido) y la salida se queda con el resto, sin pasar del máximo
    propio del rol. Después se recalcula la entrada con lo que la salida dejó libre, para que la
    suma sea exacta y no quede holgura sin asignar.
    """
    reserved_input = min(base_input, max(input_tokens, 1))
    output = max(1, min(base_output, cap.total_tokens - reserved_input))
    allowed_input = max(1, min(base_input, cap.total_tokens - output))
    return allowed_input, output


def _architect_limits(cap: _EffectiveCap | None, input_tokens: int) -> ArchitectLimits | None:
    """Límites del Architect acotados por el saldo **total** de tokens, o ``None``.

    La cota gobierna los dos lados (hallazgo V604-01): la salida autorizada es lo que queda después
    de reservar la entrada estimada, y ``max_input_tokens`` tampoco se queda en el máximo base del
    rol cuando el workflow tiene menos saldo. ``max_attempts`` se acota igual, porque un intento de
    reparación es otra llamada al modelo.
    """
    if cap is None:
        return None
    base = ArchitectLimits()
    calls = max(1, min(base.max_model_calls, cap.model_calls))
    allowed_input, allowed_output = _effective_token_limits(
        base_input=base.max_input_tokens,
        base_output=base.max_output_tokens,
        cap=cap,
        input_tokens=input_tokens,
    )
    return ArchitectLimits(
        max_attempts=max(1, min(base.max_attempts, calls)),
        max_model_calls=calls,
        max_input_tokens=allowed_input,
        max_output_tokens=allowed_output,
    )


def _planner_limits(cap: _EffectiveCap | None, input_tokens: int) -> PlannerLimits | None:
    """Límites del Planner acotados por el saldo total, con la misma regla que el Architect."""
    if cap is None:
        return None
    base = PlannerLimits()
    calls = max(1, min(base.max_model_calls, cap.model_calls))
    allowed_input, allowed_output = _effective_token_limits(
        base_input=base.max_input_tokens,
        base_output=base.max_output_tokens,
        cap=cap,
        input_tokens=input_tokens,
    )
    return PlannerLimits(
        max_attempts=max(1, min(base.max_attempts, calls)),
        max_model_calls=calls,
        max_input_tokens=allowed_input,
        max_output_tokens=allowed_output,
    )


def _resolve_durable[ResolvedT](
    resolver: Callable[[ArtifactStore, tuple[ArtifactReference, ...]], ResolvedT | None],
    store: ArtifactStore,
    references: tuple[ArtifactReference, ...],
    *,
    detail: str,
) -> ResolvedT | None:
    """Resuelve un artefacto durable traduciendo «no se pudo leer» a evidencia incompleta.

    Un artefacto que falta o que no supera su verificación de integridad es **evidencia
    incompleta**, no un fallo del rol: el adaptador lo convierte en ``BLOCKED`` con
    ``WORKFLOW_INCOMPLETE_EVIDENCE``, que es recuperable, en vez de cerrar el workflow con un
    ``FAILED``. La taxonomía del almacén se conserva intacta; la traducción ocurre aquí, en la
    frontera del rol, que es quien sabe que ese artefacto era su entrada (hallazgo V603-04).
    """
    try:
        return resolver(store, references)
    except (WorkflowResumeFailedError, WorkflowCheckpointInvalidError) as error:
        raise WorkflowIncompleteEvidenceError(f"{detail}: {error.detail or error}") from error


def _screenshots_of(
    store: ArtifactStore,
    references: tuple[ArtifactReference, ...],
    session: WebSessionReport | None,
) -> Mapping[str, ImagePayload]:
    """Capturas **verificadas** que acompañan a la verificación visual, resueltas del almacén.

    El handoff durable guarda la sesión (nombres lógicos, viewport, tamaño y hash de cada captura)
    **y los bytes exactos** en artefactos propios (hallazgo V604-02). Aquí se reconstruyen los
    ``ImagePayload`` desde esa evidencia durable, revalidando con la misma función canónica de
    ENGINE-5.3 que se usó al publicarlos: nombre lógico, media type, tamaño y sha256.

    - si la sesión no declara ninguna captura, el mapa va vacío: no hay nada que analizar y el
      informe dirá cuántas se analizaron (cero), que es un hecho, no una invención;
    - si declara capturas, se resuelven **todas** o la etapa se declara incompleta. Una captura
      faltante, unos bytes modificados con el mismo tamaño, un hash que no cuadra o un nombre lógico
      que no corresponde son huecos de evidencia: nunca se analiza a ciegas ni se entrega un mapa a
      medias.

    Raises:
        WorkflowIncompleteEvidenceError: si alguna captura declarada no se puede reconstruir y
            verificar.
    """
    return resolve_screenshots(store, references, session)


def _required_tokens(declared: ModelCallLimits, input_tokens: int) -> int:
    """Tokens que la ejecución puede gastar: la entrada **estimada** más la salida declarada.

    Es la cantidad que el presupuesto del workflow debe poder cubrir para autorizar la ejecución
    (hallazgos V603-01 y V604-01). Se usa la entrada estimada —lo que el rol va a enviar de verdad—
    y no el techo de entrada que declara el runner: ese techo es su autocomprobación, no una
    expectativa de gasto, y exigir que quepa entero dejaría inutilizable cualquier workflow con el
    presupuesto por defecto.
    """
    return input_tokens + (declared.max_output_tokens or 0)


def _declared_limits(camus: object, role: RoleName) -> ModelCallLimits | None:
    """Cota declarada por el runner del rol, consultando a CAMUS si sabe declararla.

    El ``Camus`` real implementa :meth:`punto.orchestrator.camus.Camus.declared_model_limits`, así
    que en producción la consulta siempre se hace. Un doble de prueba que no la implemente devuelve
    ``None``: significa «no declaro cota», y el adaptador se apoya entonces en el pre-gasto del
    kernel —que ya impide invocar con el saldo agotado— en vez de inventarse un máximo.
    """
    provider = getattr(camus, "declared_model_limits", None)
    if not callable(provider):
        return None
    declared = provider(role)
    return declared if isinstance(declared, ModelCallLimits) else None


@runtime_checkable
class RoleExecutor(Protocol):
    """Contrato mínimo que el kernel conoce de un rol.

    Es deliberadamente estrecho: ejecutar y declarar capacidad. Todo lo demás —prompts,
    proveedores, sandbox— queda por debajo de esta frontera, que es justo lo que permite que el
    kernel no sepa si detrás hay DeepSeek, Anthropic o un doble.
    """

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Ejecuta el rol descrito por la petición y devuelve su resultado normalizado."""
        ...

    def capability(self, role: RoleName) -> ProviderCapability | None:
        """Capacidad declarada para un rol, o ``None`` si este ejecutor no la cubre."""
        ...


@dataclass(frozen=True, slots=True)
class _RoleView:
    """Vista neutra de un informe real, antes de convertirse en ``RoleExecutionResult``."""

    status: RoleStatus
    summary: str = ""
    recommendation: str = ""
    provider: str = ""
    model: str = ""
    artifacts: tuple[str, ...] = ()
    findings: tuple[WorkflowFinding, ...] = ()
    usage: ModelUsage | None = None
    #: Llamadas reales al modelo declaradas por el informe. ``0`` significa «no las declara».
    model_calls: int = 0
    #: Intentos declarados por el informe. ``0`` significa «no los declara».
    attempts: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: WorkflowFailureCode | None = None
    error_detail: str = ""


class _InvalidRoleInputError(Exception):
    """La entrada que devolvió ``build_input`` no tiene la forma que el rol exige.

    Es interna: el adaptador la convierte en un resultado ``FAILED`` en vez de propagarla, para
    que un error de cableado del llamante no rompa el bucle del kernel con una excepción no
    tipada.
    """


class CallableRoleExecutor:
    """Adaptador genérico: envuelve una función que ya devuelve un resultado normalizado.

    Sirve para roles deterministas, dobles de prueba y cualquier implementación que no necesite
    conocer CAMUS. La capacidad es **declarada**, nunca deducida de la función: si no se declara,
    :meth:`capability` devuelve ``None`` y el kernel lo trata como un hueco, no como un proveedor.
    """

    def __init__(
        self,
        role: RoleName,
        run: Callable[[RoleExecutionRequest], RoleExecutionResult],
        capability_info: ProviderCapability | None = None,
    ) -> None:
        self._role = role
        self._run = run
        self._capability = capability_info

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Devuelve exactamente lo que produce la función envuelta."""
        return self._run(request)

    def capability(self, role: RoleName) -> ProviderCapability | None:
        """Capacidad declarada, solo para el rol que este ejecutor cubre."""
        if role is not self._role:
            return None
        return self._capability


class UnavailableRoleExecutor:
    """Rol sin proveedor disponible: lo declara y no improvisa nada.

    No hay aquí ningún camino que llame a un rol, porque no hay rol que llamar. Existe para que
    el kernel pueda cablear los ocho roles sin mentir sobre los que no tienen proveedor: el
    resultado dice **qué rol** falta, con el código del contrato y sin sustituirlo por otro.
    """

    def __init__(
        self,
        role: RoleName,
        detail: str,
        code: WorkflowFailureCode = WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
    ) -> None:
        self._role = role
        self._detail = detail
        self._code = code

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Devuelve ``PROVIDER_UNAVAILABLE`` sin ejecutar ni sustituir ningún rol."""
        return _failure(
            self._role,
            request,
            status=RoleStatus.PROVIDER_UNAVAILABLE,
            code=self._code,
            detail=self._detail,
        )

    def capability(self, role: RoleName) -> ProviderCapability | None:
        """``None``: no hay proveedor que declarar, y declararlo sería inventarlo."""
        return None


class CamusRoleExecutor:
    """Adaptador real sobre CAMUS: llama al método público del rol, normaliza y publica el handoff.

    Forma de la entrada por rol, porque cada método de CAMUS recibe cosas distintas:

    - ``ARCHITECT``: un ``ProjectIntent``. Se ejecuta ``camus.analyze_project``, que hace
      **solo** el diseño y devuelve un ``ArchitectureOutcome``;
    - ``PLANNER``: la pareja ``(ProjectIntent, ArchitectureOutcome)``. Se ejecuta
      ``camus.plan_project_from_architecture``, que hace **solo** la planificación sobre el
      diseño del Architect. El diseño se reconstruye desde la referencia durable que viaja en
      ``RoleExecutionRequest.references`` (el ``reference`` apunta a un artefacto del almacén del
      motor): el adaptador no lo adivina ni vuelve a ejecutar al Architect para rellenar el hueco;
    - ``DEVELOPER``: la pareja ``(DeveloperTask, ExecutionContext)``;
    - ``QA``: un ``QATask``; ``SECURITY``: un ``SecurityTask``; ``REVIEWER``: un ``ReviewTask``;
      ``CROSS_AUDIT``: un ``CrossAuditTask``;
    - ``VISUAL_QA``: la pareja ``(VisualQATask, Mapping[str, ImagePayload])``.

    Dos vías para construir esa entrada, y el **defecto** es la durable:

    - ``build_input`` inyectado: la *closure* explícita del llamante. Se mantiene por
      compatibilidad y para los dobles; si se da, manda;
    - sin ``build_input`` y con ``artifacts``: el adaptador construye la entrada él mismo. El
      ``ARCHITECT`` deriva la intención de la petición, el ``PLANNER`` resuelve el diseño del
      almacén, el ``DEVELOPER`` resuelve el plan durable (y el contexto de reparación cuando el
      ciclo lo pide) y ``QA``, ``SECURITY``, ``REVIEWER``, ``CROSS_AUDIT`` y ``VISUAL_QA``
      reconstruyen su entrada del plan y de los informes durables de las etapas anteriores. Es el
      camino de producción (ENGINE-6.0.2): el handoff no depende de variables del proceso anterior
      (hallazgo F611-11: la versión anterior de este docstring decía que esos cinco roles exigían
      ``build_input``, y no era cierto).

    ``build_input`` es, por tanto, un **atajo** para los dobles de prueba, no un requisito de
    ningún rol: un rol sin constructor inyectado y con almacén se construye solo, y si falta el
    artefacto del que depende la etapa se declara incompleta (``WORKFLOW_INCOMPLETE_EVIDENCE``) en
    vez de improvisar la entrada.

    Publicación automática: con ``artifacts`` inyectado, el ``ARCHITECT`` publica su
    ``ArchitectureOutcome`` completo y el ``PLANNER`` el bundle durable del plan, y ambos reportan
    la referencia en ``artifact_references``. Nadie tiene que guardar nada a mano.

    Saldo de modelo: antes de invocar a CAMUS se comprueba el ``budget_allowance`` de la petición.
    Si el kernel autorizó cero llamadas o cero tokens, la etapa falla con
    ``WORKFLOW_BUDGET_EXCEEDED`` y **no se toca el proveedor**: el saldo es la autorización de gasto
    del intento, y gastarlo sin permiso es exactamente lo que el presupuesto del kernel existe para
    impedir. Un saldo positivo **no** se propaga a CAMUS porque ningún método público del motor lo
    admite como argumento: la cota real la aplica el kernel paso a paso, y el adaptador no puede
    gastar más de lo autorizado porque el kernel no lo invoca cuando el saldo se agota y, si lo
    invocara, esta comprobación lo detiene.

    Ningún rol de planificación usa ``camus.plan_project``: ese método compone las dos etapas, y
    llamarlo desde los dos adaptadores ejecutaría Architect + Planner dos veces.

    Si se da ``registry``, la capacidad se consulta con ``require(role, provider)`` **antes** de
    llamar a CAMUS: un rol sin proveedor utilizable no llega a ejecutarse. Los fallos tipados de
    configuración, de proveedor, de validación del diseño y del handoff se traducen a resultados del
    contrato; cualquier otra excepción se propaga tal cual, porque ocultarla convertiría un defecto
    real en un resultado inventado.
    """

    def __init__(
        self,
        *,
        camus: Camus,
        role: RoleName,
        build_input: Callable[[RoleExecutionRequest], object] | None = None,
        artifacts: ArtifactStore | None = None,
        registry: ProviderCapabilityRegistry | None = None,
        provider: str | None = None,
        input_estimator: Callable[[object, RoleExecutionRequest], int] | None = None,
    ) -> None:
        """Construye el adaptador.

        ``input_estimator`` es el punto donde entra un **conteo exacto** de tokens de entrada cuando
        el proveedor o el tokenizador lo permiten (hallazgo V604-01). Sin él se usa
        :func:`estimate_input_tokens`, que es conservador y está documentado.
        """
        self._camus = camus
        self._role = role
        self._build_input = build_input
        self._artifacts = artifacts
        self._registry = registry
        self._provider = provider
        self._input_estimator = input_estimator
        self._handlers: Mapping[RoleName, Callable[..., object]] = MappingProxyType(
            {
                RoleName.ARCHITECT: self._call_analyze_project,
                RoleName.PLANNER: self._call_plan_from_architecture,
                RoleName.DEVELOPER: self._call_developer,
                RoleName.QA: self._call_qa,
                RoleName.SECURITY: self._call_security,
                RoleName.REVIEWER: self._call_review,
                RoleName.CROSS_AUDIT: self._call_cross_audit,
                RoleName.VISUAL_QA: self._call_visual_qa,
            }
        )

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Comprueba la capacidad, delega en CAMUS, normaliza el informe y publica su artefacto."""
        if request.role is not self._role:
            return _failure(
                self._role,
                request,
                status=RoleStatus.FAILED,
                code=WorkflowFailureCode.WORKFLOW_ROLE_FAILED,
                detail=(
                    f"el adaptador de {self._role.value} recibió una petición para "
                    f"{request.role.value}: el ejecutor no ejecuta otro rol"
                ),
            )

        blocked = self._registry_failure(request)
        if blocked is not None:
            return blocked

        exhausted = self._allowance_failure(request)
        if exhausted is not None:
            return exhausted

        cap = self._effective_cap(request)

        try:
            payload = self._request_input(request)
        except _InvalidRoleInputError as error:
            return _failure(
                self._role,
                request,
                status=RoleStatus.FAILED,
                code=WorkflowFailureCode.WORKFLOW_ROLE_FAILED,
                detail=f"entrada inválida para {self._role.value}: {error}",
            )
        except WorkflowIncompleteEvidenceError as error:
            # Evidencia incompleta, no fallo del rol: el kernel lo convierte en BLOCKED. Va antes
            # que el manejador genérico de ``WorkflowError`` porque es su subclase.
            return _failure(
                self._role,
                request,
                status=RoleStatus.BLOCKED,
                code=error.code,
                detail=error.detail or str(error),
            )
        except WorkflowError as error:
            return _failure(
                self._role,
                request,
                status=RoleStatus.FAILED,
                code=error.code,
                detail=error.detail or str(error),
            )
        except DeveloperExecutionError as error:
            return _failure(
                self._role,
                request,
                status=RoleStatus.FAILED,
                code=WorkflowFailureCode.WORKFLOW_ROLE_FAILED,
                detail=(
                    f"la entrada del rol {self._role.value} no se pudo construir con el workspace "
                    f"declarado por la petición: {error}"
                ),
            )

        # La cota de tokens se decide **después** de construir la entrada, porque necesita saber
        # cuántos tokens de entrada va a enviar el rol (hallazgo V604-01).
        input_tokens = self._input_tokens(payload, request)
        if cap is not None:
            refused = self._cap_failure_if_budget_exceeds(request, cap, input_tokens)
            if refused is not None:
                return refused
        try:
            produced = self._invoke(payload, cap, input_tokens)
        except _NOT_CONFIGURED_ERRORS as error:
            return _unavailable(
                self._role,
                request,
                (
                    f"el rol {self._role.value} no tiene runner configurado en CAMUS "
                    f"({type(error).__name__}): falta inyectarlo. PUNTO no lo sustituye por otro "
                    "proveedor"
                ),
            )
        except WorkflowProviderUnavailableError as error:
            return _unavailable(
                self._role,
                request,
                f"el rol {self._role.value} no tiene proveedor disponible: {error}",
            )
        except PlanningValidationError as error:
            return _failure(
                self._role,
                request,
                status=RoleStatus.FAILED,
                code=WorkflowFailureCode.WORKFLOW_ROLE_FAILED,
                detail=(
                    f"la entrada del rol {self._role.value} no es un diseño válido de PUNTO: "
                    f"{error}. Se rechaza en vez de volver a ejecutar al Architect"
                ),
            )
        except _InvalidRoleInputError as error:
            return _failure(
                self._role,
                request,
                status=RoleStatus.FAILED,
                code=WorkflowFailureCode.WORKFLOW_ROLE_FAILED,
                detail=f"entrada inválida para {self._role.value}: {error}",
            )
        result = _NORMALIZERS[self._role](produced, request)
        return self._publish(request, payload, produced, result)

    def capability(self, role: RoleName) -> ProviderCapability | None:
        """Capacidad declarada por el registro, o ``None`` si el rol no se puede cubrir hoy."""
        if role is not self._role or self._registry is None:
            return None
        try:
            return self._registry.require(self._role, self._provider)
        except WorkflowProviderUnavailableError:
            return None

    def model_limits(self, role: RoleName) -> ModelCallLimits | None:
        """Cota de modelo declarada por el runner del rol, para la reserva pre-gasto del kernel.

        Es la parte del puerto ``RoleExecutor`` que permite al kernel saber si el rol puede usar
        modelo (``uses_ai``) y cuánto declara poder gastar antes de invocarlo (hallazgo V604-01). Un
        ejecutor que no la implemente deja al kernel reservando de forma conservadora.
        """
        if role is not self._role:
            return None
        return _declared_limits(self._camus, self._role)

    # ------------------------------------------------------------------ interno
    def _registry_failure(self, request: RoleExecutionRequest) -> RoleExecutionResult | None:
        """Consulta la capacidad declarada **antes** de llamar a CAMUS.

        Un rol sin proveedor utilizable no debe llegar a ejecutarse: si llegara, el fallo se
        confundiría con un fallo del rol, que es justo lo que el encargo prohíbe.
        """
        if self._registry is None:
            return None
        try:
            capability = self._registry.require(self._role, self._provider)
        except WorkflowProviderUnavailableError as error:
            return _unavailable(self._role, request, str(error))
        if not capability.available or not capability.supports(self._role):
            return _unavailable(
                self._role,
                request,
                (
                    f"el rol {self._role.value} no tiene proveedor utilizable: "
                    f"proveedor={capability.provider!r}, available={capability.available}, "
                    f"credential_state={capability.credential_state.value}. No hay fallback"
                ),
            )
        return None

    def _allowance_failure(self, request: RoleExecutionRequest) -> RoleExecutionResult | None:
        """Frena la etapa si el kernel **no** autorizó saldo de modelo para este intento.

        El kernel calcula el saldo antes de invocar al rol y no lo invoca si está agotado; esto es
        la segunda línea de defensa del adaptador real, y existe porque el gasto ocurre aquí dentro:
        sin ella, un ejecutor construido a mano —o un llamante que reutilice la petición— podría
        tocar el proveedor con saldo cero.

        Se comprueban las dos cotas que el saldo declara: llamadas de modelo y tokens. Cualquiera de
        las dos agotada detiene la etapa, porque ninguna de las dos se puede recuperar gastando la
        otra. Un saldo ``None`` significa «el kernel no declaró saldo»: el adaptador no se inventa
        uno, y la cota que aplica entonces es la del propio contrato del rol.
        """
        allowance = request.budget_allowance
        if allowance is None:
            return None
        if allowance.model_calls_remaining > 0 and allowance.tokens_remaining > 0:
            return None
        return _failure(
            self._role,
            request,
            status=RoleStatus.FAILED,
            code=WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED,
            detail=(
                "el saldo de modelo autorizado por el kernel está agotado: no se invoca al "
                f"proveedor (model_calls_remaining={allowance.model_calls_remaining}, "
                f"tokens_remaining={allowance.tokens_remaining}). Cero llamadas reales"
            ),
        )

    def _effective_cap(self, request: RoleExecutionRequest) -> _EffectiveCap | None:
        """Cota efectiva autorizada para esta ejecución, o ``None`` si no hay saldo declarado.

        Hallazgos V603-01 y V604-01: el saldo positivo no bastaba como permiso, tenía que ser una
        **cota**, y esa cota es de **tokens totales** (entrada más salida), no solo de salida. El
        adaptador la convierte en los límites reales de cada rol cuando su contrato admite límites
        por petición y, cuando no, exige que el gasto declarado del runner quepa entero en el saldo.
        """
        allowance = request.budget_allowance
        if allowance is None:
            return None
        return _EffectiveCap(
            model_calls=allowance.model_calls_remaining,
            total_tokens=allowance.tokens_remaining,
        )

    def _input_tokens(self, payload: object, request: RoleExecutionRequest) -> int:
        """Tokens de entrada del rol: conteo exacto si se inyectó, si no estimación conservadora."""
        estimator = self._input_estimator or estimate_input_tokens
        return max(0, estimator(payload, request))

    def _cap_failure_if_budget_exceeds(
        self, request: RoleExecutionRequest, cap: _EffectiveCap, input_tokens: int
    ) -> RoleExecutionResult | None:
        """Comprueba, antes de invocar, que el gasto del rol cabe en el saldo **total** de tokens.

        Tres reglas, todas del hallazgo V604-01:

        - **Architect y Planner**: la cota se inyecta en sus límites (entrada y salida); si la
          entrada estimada ya consume o excede el saldo, no se llama al proveedor.
        - **Runner con IA y cota declarada**: se exige que sus máximos de llamadas y de tokens
          (entrada más salida) quepan en el saldo; si no, no se ejecuta.
        - **Runner determinista** (``uses_ai=False``): no reserva ni gasta presupuesto de modelo,
          así que la cota de tokens no lo frena.
        """
        if self._role in (RoleName.ARCHITECT, RoleName.PLANNER, RoleName.DEVELOPER):
            if input_tokens >= cap.total_tokens:
                return self._cap_failure(request, cap, declared=None, input_tokens=input_tokens)
            return None
        declared = _declared_limits(self._camus, self._role)
        if declared is None or not declared.uses_ai:
            return None
        if not declared.known:
            return self._cap_failure(request, cap, declared=declared, input_tokens=input_tokens)
        need = _required_tokens(declared, input_tokens)
        assert declared.max_model_calls is not None  # ``known`` lo garantiza
        if declared.max_model_calls > cap.model_calls or need > cap.total_tokens:
            return self._cap_failure(request, cap, declared=declared, input_tokens=input_tokens)
        return None

    def _cap_failure(
        self,
        request: RoleExecutionRequest,
        cap: _EffectiveCap,
        *,
        declared: ModelCallLimits | None,
        input_tokens: int,
    ) -> RoleExecutionResult:
        """Rechaza la ejecución de un rol cuyo gasto no cabe en el saldo autorizado.

        Política conservadora y documentada (hallazgos V603-01 y V604-01): si el contrato del rol
        no permite inyectar una cota por ejecución, PUNTO no invoca al runner con un saldo menor
        que su máximo declarado; y si el runner usa IA sin declarar cota, la cota es
        **desconocida**, que no es lo mismo que «sin límite»: no hay gasto autónomo. Nunca se llama
        al proveedor.
        """
        if declared is None:
            detail = (
                f"la entrada estimada de {self._role.value} ({input_tokens} token(s)) ya consume o "
                f"excede el saldo total de tokens ({cap.total_tokens}): no se invoca al proveedor"
            )
        elif not declared.known:
            detail = (
                f"el runner de {self._role.value} usa IA y no declara cota de llamadas ni de "
                "tokens: sin cota conocida no hay gasto autónomo, así que no se invoca al proveedor"
            )
        else:
            detail = (
                f"el saldo autorizado no cubre el gasto declarado por el runner de "
                f"{self._role.value} (saldo: {cap.model_calls} llamada(s) y {cap.total_tokens} "
                f"token(s); declarado: {declared.max_model_calls} llamada(s) y "
                f"{_required_tokens(declared, input_tokens)} token(s) de entrada más salida): no "
                "se invoca al proveedor"
            )
        return _failure(
            self._role,
            request,
            status=RoleStatus.BLOCKED,
            code=WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED,
            detail=detail,
        )

    def _request_input(self, request: RoleExecutionRequest) -> object:
        """Entrada del rol: la inyectada si la hay y, si no, la que PUNTO deriva de la petición.

        El ``build_input`` explícito manda porque es el contrato que ya existía (dobles de prueba y
        llamantes que construyen su propia entrada). Sin él, el defecto es el handoff durable: para
        un rol sin constructor oficial se falla explícitamente en vez de improvisar.
        """
        if self._build_input is not None:
            return self._build_input(request)
        return self._durable_input(request)

    def _durable_input(self, request: RoleExecutionRequest) -> object:
        """Entrada reconstruida desde el almacén de artefactos, sin ninguna *closure* externa.

        Cubre **toda** la pipeline (defecto V603-04): el Architect deriva su intención de la
        petición, el Planner resuelve el diseño del almacén, el Developer reconstruye su tarea del
        plan, y QA, Security, Reviewer, CrossAudit y VisualQA reconstruyen la suya desde el plan y
        los informes durables de las etapas anteriores.

        Nada de esto usa memoria de otro proceso: las referencias vienen del checkpoint
        (``RoleExecutionRequest.references``) y el contenido, del almacén estable.

        Raises:
            WorkflowIncompleteEvidenceError: si falta un artefacto del que depende la entrada. El
                adaptador lo convierte en ``BLOCKED`` con ``WORKFLOW_INCOMPLETE_EVIDENCE``: no se
                inventa la entrada ni se vuelve a ejecutar una etapa anterior en silencio.
            _InvalidRoleInputError: si no hay almacén inyectado y el rol no puede construir su
                entrada. El detalle viaja al resultado.
        """
        if self._role is RoleName.ARCHITECT:
            return _intent_from_request(request)
        if self._role is RoleName.PLANNER:
            return (_intent_from_request(request), self._resolve_design(request))
        if self._role is RoleName.DEVELOPER:
            return self._developer_input(request)
        store = self._require_store()
        plan = _resolve_durable(
            resolve_plan, store, request.references, detail="falta el plan durable"
        )
        developer = _resolve_durable(
            resolve_developer,
            store,
            request.references,
            detail="falta el resultado durable del Developer",
        )
        if self._role is RoleName.QA:
            return qa_input(plan, developer, request)
        if self._role is RoleName.SECURITY:
            qa = _resolve_durable(
                resolve_qa, store, request.references, detail="falta el informe durable de QA"
            )
            return security_input(plan, developer, qa, request)
        if self._role is RoleName.REVIEWER:
            qa = _resolve_durable(
                resolve_qa, store, request.references, detail="falta el informe durable de QA"
            )
            security = _resolve_durable(
                resolve_security,
                store,
                request.references,
                detail="falta el informe durable de Security",
            )
            return review_input(plan, developer, qa, security, request)
        if self._role is RoleName.CROSS_AUDIT:
            qa = _resolve_durable(
                resolve_qa, store, request.references, detail="falta el informe durable de QA"
            )
            security = _resolve_durable(
                resolve_security,
                store,
                request.references,
                detail="falta el informe durable de Security",
            )
            review = _resolve_durable(
                resolve_review,
                store,
                request.references,
                detail="falta el informe durable del Reviewer",
            )
            return cross_audit_input(plan, developer, qa, security, review, request)
        evidence = _resolve_durable(
            resolve_visual_evidence,
            store,
            request.references,
            detail="falta la evidencia visual durable",
        )
        spec, session = evidence if evidence is not None else (None, None)
        task = visual_qa_input(plan, developer, request, spec=spec, session=session)
        return (task, _screenshots_of(store, request.references, session))

    def _require_store(self) -> ArtifactStore:
        """Almacén de artefactos inyectado, o un error de cableado explícito.

        Raises:
            _InvalidRoleInputError: si el adaptador no tiene almacén. Sin él no hay handoff durable
                que reconstruir, y PUNTO no improvisa la entrada de una etapa.
        """
        if self._artifacts is None:
            msg = (
                f"el rol {self._role.value} necesita artefactos durables y no hay almacén "
                "inyectado: sin 'artifacts' ni 'build_input', el handoff no se puede reconstruir"
            )
            raise _InvalidRoleInputError(msg)
        return self._artifacts

    def _resolve_design(self, request: RoleExecutionRequest) -> ArchitectureOutcome:
        """Diseño del Architect resuelto desde las referencias durables de la petición.

        Raises:
            _InvalidRoleInputError: si no hay almacén inyectado (defecto de cableado).
            WorkflowIncompleteEvidenceError: si no hay referencia ``ARCHITECTURE`` resoluble. Es
                evidencia incompleta, no un fallo del rol: el kernel lo convierte en ``BLOCKED``. Un
                artefacto corrupto sube con su propio código, que el kernel también interpreta.
        """
        if self._artifacts is None:
            msg = (
                "el rol PLANNER necesita el diseño del Architect y no hay almacén de artefactos "
                "inyectado: sin 'artifacts' ni 'build_input', el handoff no se puede reconstruir"
            )
            raise _InvalidRoleInputError(msg)
        design = _resolve_durable(
            resolve_architecture,
            self._artifacts,
            request.references,
            detail="falta el diseño del Architect",
        )
        if design is None:
            raise WorkflowIncompleteEvidenceError(
                "falta el diseño del Architect en las referencias durables de la petición "
                "(RoleExecutionRequest.references): el Planner no planifica sin él y PUNTO no "
                "vuelve a ejecutar al Architect para suplirlo"
            )
        return design

    def _developer_trust_level(self) -> ExecutionTrustLevel:
        """Nivel de confianza que exige el runner del Developer, según la frontera que representa.

        Hallazgo F614-01: el contexto durable se construía con el valor por defecto
        (``TRUSTED_LOCAL``), así que el camino oficial —kernel → adaptador → almacén →
        ``developer_input`` → CAMUS → runner— no podía ejecutar un Developer que genera código con
        IA: su frontera exige ``UNTRUSTED_MODEL`` y bloqueaba antes de llamar al modelo. El nivel se
        deriva **determinísticamente** de ``DeveloperRunner.trust_level_required``, que es la
        autoridad de PUNTO sobre la frontera elegida; nunca del modelo, del plan, del almacén ni de
        un texto.

        Solo se **eleva** el aislamiento: si el runner exige ``UNTRUSTED_MODEL``, el contexto
        generado por PUNTO se construye así (sin red). Un runner determinista conserva
        ``TRUSTED_LOCAL`` y no cambia de comportamiento. La elevación vale para el contexto que
        PUNTO construye en el handoff durable: un ``build_input`` explícito sigue fallando cerrado
        si entrega un contexto incompatible, porque ese contexto no lo ha construido el motor.
        """
        runner = getattr(self._camus, "developer_runner", None)
        required = getattr(runner, "trust_level_required", None)
        if isinstance(required, ExecutionTrustLevel):
            return required
        return ExecutionTrustLevel.TRUSTED_LOCAL

    def _developer_input(
        self, request: RoleExecutionRequest
    ) -> tuple[DeveloperTask, ExecutionContext]:
        """Pareja ``(DeveloperTask, ExecutionContext)`` construida desde el plan durable.

        Si el paso trae además un plan de reparación, la tarea se completa con el contexto de
        reparación **completo** (``RepairTask``): es la misma tarea del Developer y el mismo runner,
        no un segundo Developer. El plan durable de planificación sigue siendo obligatorio: una
        reparación también necesita saber sobre qué tarea planificada se está reparando.

        Raises:
            _InvalidRoleInputError: si no hay almacén inyectado (defecto de cableado).
            WorkflowIncompleteEvidenceError: si la petición no trae una referencia ``PLANNING``
                resoluble, o si trae un plan de reparación y le falta el diagnóstico, los defectos o
                el snapshot. Es evidencia incompleta y el kernel la convierte en ``BLOCKED``: PUNTO
                no vuelve a ejecutar al Planner para rellenar el hueco ni repara con medio encargo.
        """
        if self._artifacts is None:
            msg = (
                "el rol DEVELOPER necesita el plan durable y no hay almacén de artefactos "
                "inyectado: sin 'artifacts' ni 'build_input', el handoff no se puede reconstruir"
            )
            raise _InvalidRoleInputError(msg)
        plan = resolve_plan(self._artifacts, request.references)
        if plan is None:
            raise WorkflowIncompleteEvidenceError(
                "falta el plan durable en las referencias de la petición "
                "(RoleExecutionRequest.references): el Developer no trabaja sin plan y PUNTO no "
                "vuelve a ejecutar al Planner para suplirlo"
            )
        task, context = developer_input(
            plan, request, trust_level=self._developer_trust_level()
        )
        repair = self._repair_task(request)
        if repair is None:
            return task, context
        return _with_repair(task, repair), context

    def _repair_task(self, request: RoleExecutionRequest) -> RepairTask | None:
        """Contexto de reparación resuelto del almacén, o ``None`` si este paso no repara.

        Devuelve ``None`` cuando las referencias no traen plan de reparación: es el camino normal
        del Developer. Cuando sí lo traen, el encargo se reconstruye **entero** y, si falta lo que
        el propio plan declara como parte del encargo, falla como evidencia incompleta. Un
        ``RepairTask`` a medias haría reparar al Developer sin saber qué defectos corregir o sin
        poder deshacer un cambio: es exactamente el contexto inventado que esta capa existe para
        impedir.

        Qué se exige, y por qué:

        - el **plan** de reparación, que es la autorización de escritura: sin él no hay reparación;
        - los **defectos** (``REPAIR_FINDINGS``), porque sin ellos no se sabe qué corregir;
        - el **snapshot** (``REPAIR_SNAPSHOT``), porque sin estado previo no hay rollback posible y
          el contrato del ciclo dice que nada se repara sin él;
        - el **diagnóstico** (``REPAIR_DIAGNOSIS``) solo cuando el plan lo declara
          (``RepairPlan.diagnosis_id``): si el plan se apoya en un diagnóstico, ese diagnóstico
          tiene que estar y ser el que el plan nombra, con su mismo identificador. Un diagnóstico
          ausente o de otro ciclo es un encargo incompleto; no declararlo no lo es, porque el
          contrato de ``RepairTask`` admite reparaciones sin diagnóstico de modelo.

        Raises:
            _InvalidRoleInputError: si no hay almacén inyectado (defecto de cableado).
            WorkflowIncompleteEvidenceError: si hay plan de reparación y falta el contexto exigido.
                El adaptador lo convierte en ``BLOCKED`` con ``WORKFLOW_INCOMPLETE_EVIDENCE``.
        """
        store = self._require_store()
        plan = _resolve_durable(
            resolve_repair_plan,
            store,
            request.references,
            detail="falta el plan durable de reparación",
        )
        if plan is None:
            return None
        diagnosis = _resolve_durable(
            resolve_repair_diagnosis,
            store,
            request.references,
            detail="falta el diagnóstico durable de reparación",
        )
        findings = _resolve_durable(
            resolve_repair_findings,
            store,
            request.references,
            detail="faltan los defectos durables de reparación",
        )
        snapshot = _resolve_durable(
            resolve_repair_snapshot,
            store,
            request.references,
            detail="falta el snapshot durable de reparación",
        )
        if findings is None or snapshot is None:
            raise WorkflowIncompleteEvidenceError(
                f"el paso trae el plan de reparación {plan.repair_id} y le falta parte del "
                f"contexto: defectos={'sí' if findings is not None else 'no'}, "
                f"snapshot={'sí' if snapshot is not None else 'no'}. PUNTO no repara con un "
                "encargo a medias: sin los defectos no sabe qué corregir y sin snapshot no puede "
                "deshacer el cambio"
            )
        if plan.diagnosis_id is not None:
            if diagnosis is None:
                raise WorkflowIncompleteEvidenceError(
                    f"el plan de reparación {plan.repair_id} declara el diagnóstico "
                    f"{plan.diagnosis_id} y el paso no trae ese artefacto "
                    f"({REPAIR_DIAGNOSIS_KIND}): PUNTO no repara apoyándose en un diagnóstico que "
                    "no puede leer"
                )
            if diagnosis.diagnosis_id != plan.diagnosis_id:
                raise WorkflowIncompleteEvidenceError(
                    f"el plan de reparación {plan.repair_id} declara el diagnóstico "
                    f"{plan.diagnosis_id} y el artefacto resuelto es el diagnóstico "
                    f"{diagnosis.diagnosis_id}: no es el diagnóstico de este ciclo, y reparar con "
                    "él sería reparar el defecto de otro ciclo"
                )
        return RepairTask(
            repair_id=plan.repair_id,
            workflow_id=request.workflow_id,
            task_id=request.task_id,
            cycle=plan.cycle,
            project_id=request.project_id,
            objective=REPAIR_OBJECTIVE,
            plan=plan,
            diagnosis=diagnosis,
            findings=findings,
            target_files=plan.target_files,
            allowed_file_globs=plan.allowed_file_globs,
            forbidden_files=plan.forbidden_files,
            snapshot_id=snapshot.snapshot_id,
            acceptance_criteria=plan.acceptance_criteria,
            verification_roles=plan.verification_roles,
            idempotency_key=plan.idempotency_key,
            workspace_path=request.workspace_path,
        )

    def _publish(
        self,
        request: RoleExecutionRequest,
        payload: object,
        produced: object,
        result: RoleExecutionResult,
    ) -> RoleExecutionResult:
        """Publica el artefacto durable de la etapa y lo reporta, cuando la etapa lo produce.

        Solo se publica un resultado ``COMPLETED``: un informe fallido no deja un artefacto que la
        etapa siguiente pudiera confundir con un diseño o un plan válidos. Sin almacén inyectado no
        se publica nada, y el adaptador se comporta como antes de ENGINE-6.0.2.
        """
        store = self._artifacts
        if store is None or result.status is not RoleStatus.COMPLETED:
            return result
        reference = self._durable_reference(request, store, payload, produced)
        if reference is None:
            return result
        return result.model_copy(update={"artifact_references": (reference,)})

    def _durable_reference(
        self,
        request: RoleExecutionRequest,
        store: ArtifactStore,
        payload: object,
        produced: object,
    ) -> ArtifactReference | None:
        """Referencia del artefacto durable de esta etapa, o ``None`` si la etapa no publica.

        Publica **toda** la pipeline (defecto V603-04): el Architect su diseño, el Planner el bundle
        del plan, el Developer su resultado de ejecución, y QA, Security, Reviewer, CrossAudit y
        VisualQA sus informes. Lo que se publica es el informe estructurado del contrato, nunca
        contenido de ficheros ni cadenas de razonamiento, y solo cuando el resultado de la etapa es
        aceptable —eso lo decide :meth:`_publish` antes de llamar aquí—.
        """
        if self._role is RoleName.ARCHITECT and isinstance(produced, ArchitectureOutcome):
            if produced.proposal is None:
                return None
            return publish_architecture(store, request=request, outcome=produced)
        if self._role is RoleName.PLANNER and isinstance(produced, PlanningOutcome):
            if produced.roadmap is None or produced.task_graph is None:
                return None
            return publish_plan(
                store,
                request=request,
                outcome=produced,
                architecture=_design_of(payload),
            )
        if self._role is RoleName.DEVELOPER and isinstance(produced, DeveloperExecutionResult):
            return publish_developer(store, request=request, result=produced)
        if self._role is RoleName.QA and isinstance(produced, QAReport):
            return publish_qa(store, request=request, report=produced)
        if self._role is RoleName.SECURITY and isinstance(produced, SecurityReport):
            return publish_security(store, request=request, report=produced)
        if self._role is RoleName.REVIEWER and isinstance(produced, ReviewReport):
            return publish_review(store, request=request, report=produced)
        if self._role is RoleName.CROSS_AUDIT and isinstance(produced, CrossAuditReport):
            return publish_cross_audit(store, request=request, report=produced)
        if self._role is RoleName.VISUAL_QA and isinstance(produced, VisualQAReport):
            return publish_visual_qa(store, request=request, report=produced)
        return None

    def _invoke(
        self, payload: object, cap: _EffectiveCap | None, input_tokens: int
    ) -> object:
        """Despacha al método público de CAMUS del rol, con la cota de gasto si la hay."""
        handler = self._handlers.get(self._role)
        if handler is None:
            msg = f"no hay método de CAMUS declarado para el rol {self._role.value}"
            raise _InvalidRoleInputError(msg)
        if self._role is RoleName.ARCHITECT:
            return self._call_analyze_project(payload, cap, input_tokens)
        if self._role is RoleName.PLANNER:
            return self._call_plan_from_architecture(payload, cap, input_tokens)
        if self._role is RoleName.DEVELOPER:
            return self._call_developer(payload, cap)
        return handler(payload)

    def _call_analyze_project(
        self, payload: object, cap: _EffectiveCap | None, input_tokens: int
    ) -> object:
        """``analyze_project`` ejecuta **solo** al Architect y devuelve su informe.

        La cota efectiva entra en el ``ArchitectRequest`` como límites del rol, gobernando **entrada
        y salida**: la salida autorizada es lo que queda del saldo después de la entrada estimada, y
        el tope de entrada tampoco se queda en el máximo base del rol (hallazgo V604-01). Es lo que
        hace que el saldo del workflow sea una restricción real sobre el bucle que hace cada llamada
        al modelo y no una comprobación posterior.
        """
        return self._camus.analyze_project(
            cast("ProjectIntent", payload), limits=_architect_limits(cap, input_tokens)
        )

    def _call_plan_from_architecture(
        self, payload: object, cap: _EffectiveCap | None, input_tokens: int
    ) -> object:
        """``plan_project_from_architecture`` ejecuta **solo** al Planner sobre el diseño.

        ``build_input`` debe devolver la pareja ``(ProjectIntent, ArchitectureOutcome)``, con el
        diseño del Architect reconstruido desde la referencia durable de la petición. Si el
        diseño no llega, se falla aquí: volver a ejecutar al Architect para rellenar el hueco
        sería exactamente la duplicación que este adaptador debe impedir. Los límites efectivos
        viajan en el ``PlannerRequest``, con la misma regla de entrada y salida que en el Architect.
        """
        intent, architecture = _pair(
            payload, RoleName.PLANNER, "ProjectIntent y ArchitectureOutcome"
        )
        if getattr(architecture, "proposal", None) is None:
            msg = (
                "falta el diseño del Architect: build_input debe reconstruirlo desde las "
                "referencias durables de la petición (RoleExecutionRequest.references). "
                "PUNTO no vuelve a ejecutar al Architect para suplirlo"
            )
            raise _InvalidRoleInputError(msg)
        return self._camus.plan_project_from_architecture(
            cast("ProjectIntent", intent),
            cast("ArchitectureOutcome", architecture),
            limits=_planner_limits(cap, input_tokens),
        )

    def _with_invocation_limits(
        self,
        context: ExecutionContext,
        *,
        cap: _EffectiveCap | None,
        repair: RepairTask | None,
    ) -> ExecutionContext:
        """Contexto del Developer con la autorización de modelo de **esta** invocación (F613-01).

        Los techos se toman por **mínimo**, nunca por suma: la cota del workflow es lo que el
        kernel reservó, y el presupuesto del ``RepairPlan`` puede ser más estrecho que el saldo
        global, pero nunca más ancho. Ninguno de los dos amplía al otro, y el runner aplicará
        después el mínimo con su propia configuración.

        Sin cota declarada —una ejecución fuera del workflow— el contexto se devuelve intacto y
        manda la configuración del runner, que es el comportamiento de siempre.
        """
        if cap is None:
            return context
        calls = cap.model_calls
        total_tokens: int | None = cap.total_tokens
        if repair is not None:
            plan = repair.plan
            if plan.budget_model_calls > 0:
                calls = min(calls, plan.budget_model_calls)
            if plan.budget_total_tokens > 0:
                total_tokens = (
                    plan.budget_total_tokens
                    if total_tokens is None
                    else min(total_tokens, plan.budget_total_tokens)
                )
        return replace(
            context,
            model_limits=DeveloperInvocationLimits(
                max_model_calls=max(0, calls),
                max_total_tokens=total_tokens if total_tokens is None else max(1, total_tokens),
                source="repair_plan" if repair is not None else "workflow",
            ),
        )

    def _call_developer(self, payload: object, cap: _EffectiveCap | None) -> object:
        """``execute_developer_task`` recibe la tarea de desarrollo y su contexto de ejecución.

        Si la tarea trae contexto de reparación, la ejecuta el **mismo** runner a través de
        ``camus.execute_repair_task``, y solo si el runner declara que sabe recibirlo
        (``supports_repair_context``). Un runner que no lo declare no recibe la reparación: la etapa
        falla de forma explícita en vez de ejecutar una reparación como una tarea normal, sin las
        reglas duras ni la autorización acotada del plan. PUNTO no crea un segundo Developer para
        esto: la diferencia es el contexto, no el rol ni el runner.

        La **autorización de modelo de esta invocación** entra por el contexto (hallazgo F613-01):
        es la cota que el kernel reservó, acotada además por el presupuesto del ``RepairPlan`` si el
        paso repara. Sin esto, el presupuesto del workflow se quedaba en la reserva del kernel y el
        bucle real de llamadas del Developer ejecutaba la configuración del runner.
        """
        task, context = _pair(payload, RoleName.DEVELOPER, "DeveloperTask y ExecutionContext")
        developer_task = cast("DeveloperTask", task)
        execution_context = self._with_invocation_limits(
            cast("ExecutionContext", context),
            cap=cap,
            repair=getattr(developer_task, "repair", None),
        )
        # ``getattr`` y no acceso directo: ``build_input`` puede devolver el doble de prueba que los
        # llamantes usan desde antes de la reparación (una pareja de cadenas), y sin contexto de
        # reparación el camino es exactamente el de siempre.
        if getattr(developer_task, "repair", None) is None:
            return self._camus.execute_developer_task(developer_task, execution_context)
        if not getattr(self._camus, "developer_repair_supported", False):
            msg = (
                "el runner del Developer no declara soporte de contexto de reparación "
                "(supports_repair_context=False): CAMUS no le entrega una reparación como si fuera "
                "una tarea normal y PUNTO no crea un segundo Developer incompatible"
            )
            raise _InvalidRoleInputError(msg)
        return self._camus.execute_repair_task(developer_task, execution_context)

    def _call_qa(self, payload: object) -> object:
        """``qa_task`` delega en el ``QARunner`` inyectado."""
        return self._camus.qa_task(cast("QATask", payload))

    def _call_security(self, payload: object) -> object:
        """``security_task`` delega en el ``SecurityRunner`` inyectado."""
        return self._camus.security_task(cast("SecurityTask", payload))

    def _call_review(self, payload: object) -> object:
        """``review_task`` delega en el ``ReviewerRunner`` inyectado."""
        return self._camus.review_task(cast("ReviewTask", payload))

    def _call_cross_audit(self, payload: object) -> object:
        """``cross_audit`` delega en el ``CrossAuditRunner`` inyectado."""
        return self._camus.cross_audit(cast("CrossAuditTask", payload))

    def _call_visual_qa(self, payload: object) -> object:
        """``visual_qa`` recibe la tarea visual y las capturas ya verificadas por PUNTO."""
        task, screenshots = _pair(
            payload, RoleName.VISUAL_QA, "VisualQATask y las capturas verificadas"
        )
        return self._camus.visual_qa(
            cast("VisualQATask", task), cast("Mapping[str, ImagePayload]", screenshots)
        )


# ---------------------------------------------------------------------------
# Normalizadores puros
# ---------------------------------------------------------------------------
def normalize_architecture(outcome: object, request: RoleExecutionRequest) -> RoleExecutionResult:
    """Normaliza el resultado real del Architect.

    La entrada real es el ``ArchitectureOutcome`` que devuelve ``camus.analyze_project``. También
    se tolera —sin usarlo ya el adaptador— el ``ProjectPlanResult`` de ``camus.plan_project``, que
    lleva el mismo informe en ``architect``: leerlo no cuesta nada y evita que un llamante antiguo
    se quede sin normalizador.
    """
    status, error_code, status_detail = _map_status(outcome)
    summary = _model_summary(outcome, RoleName.ARCHITECT)
    return _result(
        RoleName.ARCHITECT,
        request,
        _RoleView(
            status=status,
            summary=_coalesce(_text(outcome, "summary"), _text(outcome, "error")),
            recommendation=_join(*_texts(outcome, "violations")),
            provider=_text(summary, "provider"),
            model=_text(summary, "model"),
            artifacts=_component_ids(outcome),
            usage=_as_usage(getattr(summary, "usage", None))
            or _as_usage(getattr(outcome, "model_usage", None)),
            model_calls=_model_calls(outcome, summary),
            attempts=_int(summary, "attempts_used"),
            started_at=_moment(outcome, "started_at"),
            completed_at=_moment(outcome, "completed_at"),
            error_code=error_code,
            error_detail=_coalesce(_text(outcome, "error"), status_detail),
        ),
    )


def normalize_planning(outcome: object, request: RoleExecutionRequest) -> RoleExecutionResult:
    """Normaliza el resultado real del Planner.

    La entrada real es el ``PlanningOutcome`` que devuelve
    ``camus.plan_project_from_architecture``. Igual que el Architect, también tolera el
    ``ProjectPlanResult`` de ``camus.plan_project``; en ese caso el resumen está en ``planner``.
    """
    status, error_code, status_detail = _map_status(outcome)
    summary = _model_summary(outcome, RoleName.PLANNER)
    return _result(
        RoleName.PLANNER,
        request,
        _RoleView(
            status=status,
            summary=_coalesce(_text(outcome, "summary"), _text(outcome, "error")),
            recommendation=_join(*_texts(outcome, "violations")),
            provider=_text(summary, "provider"),
            model=_text(summary, "model"),
            artifacts=_planned_task_ids(outcome),
            usage=_as_usage(getattr(summary, "usage", None))
            or _as_usage(getattr(outcome, "model_usage", None)),
            model_calls=_model_calls(outcome, summary),
            attempts=_int(summary, "attempts_used"),
            started_at=_moment(outcome, "started_at"),
            completed_at=_moment(outcome, "completed_at"),
            error_code=error_code,
            error_detail=_coalesce(_text(outcome, "error"), status_detail),
        ),
    )


def normalize_developer(result: object, request: RoleExecutionRequest) -> RoleExecutionResult:
    """Normaliza el ``DeveloperExecutionResult`` real.

    El Developer no declara hallazgos ni gravedades, así que este normalizador **no** fabrica
    ninguno: copia el estado, los archivos cambiados, los checks fallidos y el consumo real.

    Las llamadas al modelo se copian del contador **propio** del informe, que siempre existe y cuyo
    ``0`` es un hecho (no llamó), no un silencio: sin esa distinción un runner determinista
    reportaba una llamada que nunca hizo y el kernel lo bloqueaba por un gasto inexistente (hallazgo
    V605-05, ver :func:`_declared_model_calls`).
    """
    status, error_code, status_detail = _map_status(result)
    validation = _nested(result, "validation")
    return _result(
        RoleName.DEVELOPER,
        request,
        _RoleView(
            status=status,
            summary=_text_or_error(result),
            recommendation=_join(*_texts(validation, "failed_checks")),
            provider=_text(result, "provider"),
            model=_text(result, "model"),
            artifacts=_attribute_texts(result, "files_changed", "path"),
            usage=_as_usage(getattr(result, "usage", None)),
            model_calls=_declared_model_calls(result),
            attempts=_int(result, "attempts_used"),
            started_at=_moment(result, "started_at"),
            completed_at=_moment(result, "completed_at"),
            error_code=error_code,
            error_detail=_coalesce(_text(result, "error"), status_detail),
        ),
    )


def normalize_qa(report: object, request: RoleExecutionRequest) -> RoleExecutionResult:
    """Normaliza el ``QAReport`` real."""
    status, error_code, status_detail = _map_status(report)
    return _result(
        RoleName.QA,
        request,
        _RoleView(
            status=status,
            summary=_text_or_error(report),
            recommendation=_join(
                *(_text(item, "repair_hint") for item in _sequence(report, "findings"))
            ),
            provider=_text(report, "provider"),
            model=_text(report, "model"),
            artifacts=(
                *_attribute_texts(report, "executed_checks", "name"),
                *_attribute_texts(report, "test_cases", "id"),
            ),
            findings=_findings(
                report, RoleName.QA, ("file", "acceptance_criterion", "repair_hint")
            ),
            usage=_as_usage(getattr(report, "model_usage", None)),
            model_calls=_model_calls(report, None),
            attempts=_int(report, "attempts"),
            started_at=_moment(report, "started_at"),
            completed_at=_moment(report, "completed_at"),
            error_code=error_code,
            error_detail=_coalesce(_text(report, "error"), status_detail),
        ),
    )


def normalize_security(report: object, request: RoleExecutionRequest) -> RoleExecutionResult:
    """Normaliza el ``SecurityReport`` real.

    Conserva la gravedad de cada hallazgo tal como la calculó PUNTO: un ``HIGH`` o ``CRITICAL``
    llega al kernel como hallazgo bloqueante y ninguna normalización lo rebaja.
    """
    status, error_code, status_detail = _map_status(report)
    return _result(
        RoleName.SECURITY,
        request,
        _RoleView(
            status=status,
            summary=_text_or_error(report),
            recommendation=_join(
                *(_text(item, "recommendation") for item in _sequence(report, "findings"))
            ),
            provider=_text(report, "provider"),
            model=_text(report, "model"),
            artifacts=_texts(report, "reviewed_files"),
            findings=_findings(
                report, RoleName.SECURITY, ("impact", "file", "acceptance_criterion")
            ),
            usage=_as_usage(getattr(report, "model_usage", None)),
            model_calls=_model_calls(report, None),
            attempts=_int(report, "attempts"),
            started_at=_moment(report, "started_at"),
            completed_at=_moment(report, "completed_at"),
            error_code=error_code,
            error_detail=_coalesce(_text(report, "error"), status_detail),
        ),
    )


def normalize_review(report: object, request: RoleExecutionRequest) -> RoleExecutionResult:
    """Normaliza el ``ReviewReport`` real (veredicto y gates calculados por PUNTO)."""
    status, error_code, status_detail = _map_status(report)
    return _result(
        RoleName.REVIEWER,
        request,
        _RoleView(
            status=status,
            summary=_text_or_error(report),
            recommendation=_text(report, "recommendation_notes"),
            provider=_text(report, "provider"),
            model=_text(report, "model"),
            artifacts=_texts(report, "model_visible_files"),
            findings=_findings(report, RoleName.REVIEWER, ("file", "recommendation")),
            usage=_as_usage(getattr(report, "model_usage", None)),
            model_calls=_model_calls(report, None),
            attempts=_int(report, "attempts"),
            started_at=_moment(report, "started_at"),
            completed_at=_moment(report, "completed_at"),
            error_code=error_code,
            error_detail=_coalesce(_text(report, "error"), status_detail),
        ),
    )


def normalize_cross_audit(report: object, request: RoleExecutionRequest) -> RoleExecutionResult:
    """Normaliza el ``CrossAuditReport`` real de la auditoría cruzada."""
    status, error_code, status_detail = _map_status(report)
    return _result(
        RoleName.CROSS_AUDIT,
        request,
        _RoleView(
            status=status,
            summary=_text_or_error(report),
            recommendation=_text(report, "recommendation_notes"),
            provider=_text(report, "provider"),
            model=_text(report, "model"),
            artifacts=_texts(report, "model_visible_files"),
            findings=_findings(report, RoleName.CROSS_AUDIT, ("file", "recommendation")),
            usage=_as_usage(getattr(report, "model_usage", None)),
            model_calls=_model_calls(report, None),
            attempts=_int(report, "attempts"),
            started_at=_moment(report, "started_at"),
            completed_at=_moment(report, "completed_at"),
            error_code=error_code,
            error_detail=_coalesce(_text(report, "error"), status_detail),
        ),
    )


def normalize_visual_qa(report: object, request: RoleExecutionRequest) -> RoleExecutionResult:
    """Normaliza el ``VisualQAReport`` real."""
    status, error_code, status_detail = _map_status(report)
    return _result(
        RoleName.VISUAL_QA,
        request,
        _RoleView(
            status=status,
            summary=_text_or_error(report),
            recommendation=_text(report, "recommendation_notes"),
            provider=_text(report, "provider"),
            model=_text(report, "model"),
            artifacts=(
                *_texts(report, "routes_analyzed"),
                *_texts(report, "viewports_analyzed"),
                *_texts(report, "screenshots_analyzed"),
            ),
            findings=_findings(report, RoleName.VISUAL_QA, ("route", "viewport", "recommendation")),
            usage=_as_usage(getattr(report, "model_usage", None)),
            model_calls=_model_calls(report, None),
            attempts=_int(report, "attempts"),
            started_at=_moment(report, "started_at"),
            completed_at=_moment(report, "completed_at"),
            error_code=error_code,
            error_detail=_coalesce(_text(report, "error"), status_detail),
        ),
    )


#: Normalizador puro de cada rol. Es la tabla que usa :class:`CamusRoleExecutor`.
_NORMALIZERS: Final[Mapping[RoleName, _RoleNormalizer]] = MappingProxyType(
    {
        RoleName.ARCHITECT: normalize_architecture,
        RoleName.PLANNER: normalize_planning,
        RoleName.DEVELOPER: normalize_developer,
        RoleName.QA: normalize_qa,
        RoleName.SECURITY: normalize_security,
        RoleName.REVIEWER: normalize_review,
        RoleName.CROSS_AUDIT: normalize_cross_audit,
        RoleName.VISUAL_QA: normalize_visual_qa,
    }
)


# ---------------------------------------------------------------------------
# Construcción de resultados
# ---------------------------------------------------------------------------
def _result(role: RoleName, request: RoleExecutionRequest, view: _RoleView) -> RoleExecutionResult:
    """Construye el resultado normalizado, aplicando acotado y valores neutros documentados."""
    started_at = view.started_at or utc_now()
    return RoleExecutionResult(
        role=role,
        status=view.status,
        summary=_excerpt(
            view.summary or f"{role.value}: {view.status.value}", MAX_WORKFLOW_SUMMARY_CHARS
        ),
        artifacts=_artifacts(view.artifacts),
        findings=view.findings[:MAX_WORKFLOW_FINDINGS],
        recommendation=_excerpt(view.recommendation, MAX_WORKFLOW_TEXT_CHARS),
        usage=view.usage if view.usage is not None else ModelUsage(),
        model_calls=_bounded_model_calls(view.model_calls),
        provider=_excerpt(view.provider, _MAX_PROVIDER_CHARS),
        model=_excerpt(view.model, _MAX_MODEL_CHARS),
        attempts=view.attempts if view.attempts >= 1 else request.attempt,
        started_at=started_at,
        completed_at=view.completed_at or started_at,
        error_code=view.error_code,
        error_detail=_excerpt(view.error_detail, MAX_WORKFLOW_TEXT_CHARS),
    )


def _failure(
    role: RoleName,
    request: RoleExecutionRequest,
    *,
    status: RoleStatus,
    code: WorkflowFailureCode,
    detail: str,
) -> RoleExecutionResult:
    """Resultado de fallo sin trabajo producido: el rol no llegó a ejecutarse."""
    return _result(role, request, _RoleView(status=status, error_code=code, error_detail=detail))


def _unavailable(
    role: RoleName, request: RoleExecutionRequest, detail: str
) -> RoleExecutionResult:
    """Resultado de proveedor no disponible, con el rol y lo que falta en el detalle."""
    return _failure(
        role,
        request,
        status=RoleStatus.PROVIDER_UNAVAILABLE,
        code=WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
        detail=detail,
    )


def _pair(payload: object, role: RoleName, expectation: str) -> tuple[object, object]:
    """Descompone la pareja que ``build_input`` debe devolver para los roles de dos argumentos."""
    if isinstance(payload, tuple) and len(payload) == 2:
        return payload[0], payload[1]
    msg = (
        f"build_input debe devolver {expectation} para el rol {role.value}; "
        f"se recibió {type(payload).__name__}"
    )
    raise _InvalidRoleInputError(msg)


def _with_repair(task: DeveloperTask, repair: RepairTask) -> DeveloperTask:
    """Devuelve la tarea del Developer con el contexto de reparación, sin ampliar su autorización.

    Es la misma tarea y el mismo contrato: lo que se añade es ``repair``. La precedencia de los
    campos que la reparación puede ajustar es explícita, y ninguna la amplía:

    - ``allowed_files`` y ``context_files``: los ``target_files`` del plan de reparación, que son su
      autorización de escritura. Si el plan de reparación no declara ninguno, se conserva la
      autorización que ya traía la tarea planificada, y **nunca** se sustituye por el alcance de la
      petición: una reparación no puede ampliar lo que el plan autorizó.
    - ``acceptance_criteria``: los del plan de reparación y, si no declara, los de la tarea.
    - ``objective``, ``slug``, ``commit_message`` y ``validations``: los de la tarea planificada. El
      encargo de reparación viaja en ``repair`` —con su propio objetivo y sus reglas duras—, y
      sobrescribir el objetivo perdería la tarea original que se está reparando.
    """
    update: dict[str, object] = {"repair": repair}
    if repair.target_files:
        update["allowed_files"] = repair.target_files
        update["context_files"] = repair.target_files
    if repair.acceptance_criteria:
        update["acceptance_criteria"] = repair.acceptance_criteria
    return task.model_copy(update=update)


def _intent_from_request(request: RoleExecutionRequest) -> ProjectIntent:
    """Intención que PUNTO deriva de la petición, sin inventar datos del producto.

    El kernel declara el objetivo humano, los criterios y el workspace; **no** declara nombre de
    producto ni dominio. Así que la intención se construye solo con lo declarado: el objetivo como
    descripción y su primera línea acotada como nombre. Nada de rellenar campos que la petición no
    trae: un dato inventado en la intención acabaría en el diseño del Architect como si alguien lo
    hubiera pedido.

    Es determinista: la misma petición produce siempre la misma intención, sin reloj ni azar.
    """
    name = _one_line(request.objective)[:_MAX_INTENT_NAME_CHARS].strip() or _DEFAULT_INTENT_NAME
    return ProjectIntent(name=name, description=request.objective)


def _one_line(text: str) -> str:
    """Primera línea del texto, sin espacios sobrantes, o cadena vacía si no hay texto."""
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped.splitlines()[0].strip()


def _design_of(payload: object) -> ArchitectureOutcome | None:
    """Diseño del Architect que viajó en la entrada del Planner, si es un diseño de verdad.

    La entrada del Planner es la pareja ``(ProjectIntent, ArchitectureOutcome)``. Se comprueba el
    tipo en vez de confiar en la posición: si lo que llegó no es un diseño, el bundle del plan se
    publica sin las piezas del diseño (que es un hueco declarado) en lugar de con un objeto ajeno
    serializado como si fuera una arquitectura.
    """
    if isinstance(payload, tuple) and len(payload) == 2:
        candidate = payload[1]
        if isinstance(candidate, ArchitectureOutcome):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Lectura defensiva del informe real
# ---------------------------------------------------------------------------
def _state_value(report: object) -> str:
    """Estado real del informe en mayúsculas, o cadena vacía si no lo declara."""
    raw = getattr(report, "status", None)
    value = getattr(raw, "value", raw)
    return value.strip().upper() if isinstance(value, str) else ""


def _map_status(report: object) -> tuple[RoleStatus, WorkflowFailureCode | None, str]:
    """Traduce el estado real del informe a estado normalizado, código y detalle.

    El detalle solo se rellena cuando el estado **no** es reconocible: en ese caso es la
    explicación de por qué el kernel no puede tratarlo como éxito.
    """
    state = _state_value(report)
    if state in _COMPLETED_STATES:
        return RoleStatus.COMPLETED, None, ""
    if state in _REPAIR_STATES:
        return RoleStatus.NEEDS_REPAIR, None, ""
    if state in _BLOCKED_STATES:
        return RoleStatus.BLOCKED, None, ""
    detail = (
        f"estado {state!r} no reconocido por el kernel: no se trata como éxito"
        if state
        else "el informe no declara estado: no se trata como éxito"
    )
    return RoleStatus.FAILED, WorkflowFailureCode.WORKFLOW_ROLE_FAILED, detail


def _text(report: object, field: str) -> str:
    """Campo de texto del informe, o cadena vacía si falta o no es texto."""
    raw = getattr(report, field, None)
    return raw if isinstance(raw, str) else ""


def _text_or_error(report: object) -> str:
    """Resumen textual del informe: el ``summary`` real o, si no lo hay, su error."""
    return _coalesce(_text(report, "summary"), _text(report, "error"))


def _int(report: object, field: str) -> int:
    """Campo entero del informe, o ``0`` si falta (``0`` significa «no lo declara»)."""
    raw = getattr(report, field, None)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    return 0


def _moment(report: object, field: str) -> datetime | None:
    """Marca temporal del informe, o ``None`` si no la declara."""
    raw = getattr(report, field, None)
    return raw if isinstance(raw, datetime) else None


def _as_usage(value: object) -> ModelUsage | None:
    """Consumo de tokens, solo si el valor es realmente un ``ModelUsage``."""
    return value if isinstance(value, ModelUsage) else None


def _sequence(report: object, field: str) -> tuple[object, ...]:
    """Elementos de una colección del informe, o tupla vacía si falta."""
    raw = getattr(report, field, None)
    if isinstance(raw, (list, tuple)):
        return tuple(raw)
    return ()


def _texts(report: object, field: str) -> tuple[str, ...]:
    """Textos de una colección de cadenas del informe, en orden."""
    return tuple(item for item in _sequence(report, field) if isinstance(item, str) and item)


def _attribute_texts(report: object | None, field: str, attribute: str) -> tuple[str, ...]:
    """Atributo de texto de cada elemento de una colección del informe, en orden."""
    return tuple(
        value
        for item in _sequence(report, field)
        if isinstance(value := getattr(item, attribute, None), str) and value
    )


def _nested(report: object, *fields: str) -> object | None:
    """Atributo anidado del informe, o ``None`` en cuanto falta un eslabón."""
    current: object | None = report
    for field in fields:
        if current is None:
            return None
        current = getattr(current, field, None)
    return current


def _model_summary(report: object, role: RoleName) -> ModelExecutionSummary | None:
    """Resumen de ejecución del rol, si el informe lo declara.

    ``ArchitectureOutcome`` y ``PlanningOutcome`` —lo que devuelven ``analyze_project`` y
    ``plan_project_from_architecture``— lo llevan en ``summary``; el ``ProjectPlanResult`` de
    ``camus.plan_project`` lo separa en ``architect`` y ``planner``. Se aceptan los dos.
    """
    direct = getattr(report, "summary", None)
    if isinstance(direct, ModelExecutionSummary):
        return direct
    field = "architect" if role is RoleName.ARCHITECT else "planner"
    nested = getattr(report, field, None)
    if isinstance(nested, ModelExecutionSummary):
        return nested
    return None


def _model_calls(report: object, summary: ModelExecutionSummary | None) -> int:
    """Llamadas reales al modelo que declara el informe (V602-04-B), con precedencia explícita.

    Orden, de la fuente más específica a la más general:

    1. ``ModelExecutionSummary.model_calls`` del informe: es el contador propio del resumen de
       ejecución del Architect y del Planner, y el único que declara llamadas de reparación;
    2. el ``model_calls`` del propio informe: lo declaran el Developer, QA, Security, el Reviewer,
       la auditoría cruzada y la QA visual;
    3. los intentos que el informe sí declara —``attempts_used`` y, en los informes que lo llaman
       así, ``attempts``—: un intento de reparación es una llamada al modelo, y es lo único que
       queda cuando el informe no cuenta llamadas.

    Si no hay ninguno, ``0``: el informe no declara llamadas y ninguna se inventa. Los **tokens no
    se usan nunca** como fuente: un rol puede llamar tres veces gastando pocos tokens o una sola
    gastando muchos, así que los tokens no son un contador de llamadas.
    """
    candidates = (
        None if summary is None else summary.model_calls,
        _int(report, "model_calls"),
        None if summary is None else summary.attempts_used,
        _int(report, "attempts_used"),
        _int(report, "attempts"),
    )
    for declared in candidates:
        if declared is not None and declared > 0:
            return declared
    return 0


def _bounded_model_calls(value: int) -> int:
    """Acota las llamadas al rango del contrato (``ge=0, le=64``) sin cambiar su significado."""
    return min(max(value, 0), _MAX_MODEL_CALLS)


def _declared_model_calls(report: object) -> int:
    """Llamadas al modelo que el informe **declara** con su propio contador (V605-05).

    Es la lectura que usa el Developer, y es distinta de :func:`_model_calls` en un punto que
    importa: ``DeveloperExecutionResult.model_calls`` existe siempre y un ``0`` ahí es un hecho
    declarado —el runner no llamó al modelo—, no un silencio que haya que suplir con los intentos.
    Tratar ese ``0`` como un silencio convertía el intento técnico de un runner **determinista** en
    una llamada de modelo que nunca ocurrió, y la postcondición del kernel (un rol con
    ``uses_ai=False`` tiene autorización cero, así que cualquier gasto reportado es una brecha)
    bloqueaba el workflow por un gasto inexistente: exactamente lo que el hallazgo V605-05 prohíbe.

    Un informe que no exponga el contador no tiene nada que declarar: ahí se mantiene la política de
    :func:`_model_calls`, que es la de los roles cuyo informe no cuenta llamadas.
    """
    declared = getattr(report, "model_calls", None)
    if isinstance(declared, int) and not isinstance(declared, bool):
        return _bounded_model_calls(declared)
    return _model_calls(report, None)


def _component_ids(outcome: object) -> tuple[str, ...]:
    """Componentes del plan de arquitectura, como referencias de artefacto."""
    architecture = _nested(outcome, "proposal", "architecture")
    if architecture is None:
        architecture = _nested(outcome, "architecture")
    return _attribute_texts(architecture, "components", "id")


def _planned_task_ids(outcome: object) -> tuple[str, ...]:
    """Tareas del roadmap, como referencias de artefacto."""
    return _attribute_texts(_nested(outcome, "roadmap"), "tasks", "id")


def _enum_text(value: object) -> str:
    """Texto de un valor de enumeración (su ``value``) o de una cadena ya lista."""
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else ""


def _severity(value: object) -> FindingSeverity:
    """Gravedad real del hallazgo; ``MEDIUM`` si el informe no declara una reconocible.

    No se degrada a ``INFO``: una gravedad ilegible no es una gravedad benigna, y tratar una
    como la otra minimizaría el riesgo sin decirlo.
    """
    raw = getattr(value, "value", value)
    if isinstance(raw, str):
        try:
            return FindingSeverity(raw.strip().upper())
        except ValueError:
            return FindingSeverity.MEDIUM
    return FindingSeverity.MEDIUM


def _findings(
    report: object, role: RoleName, extras: tuple[str, ...] = ()
) -> tuple[WorkflowFinding, ...]:
    """Copia los hallazgos del informe con su gravedad real, acotados y recortados.

    ``extras`` son campos propios del rol (impacto, archivo, ruta visual…) que se anexan a la
    evidencia para no perder contexto al normalizar, siempre acotados.
    """
    collected: list[WorkflowFinding] = []
    for item in _sequence(report, "findings")[:MAX_WORKFLOW_FINDINGS]:
        reference = _join(*(_text(item, field) for field in extras))
        collected.append(
            WorkflowFinding(
                role=role,
                severity=_severity(getattr(item, "severity", None)),
                category=_excerpt(
                    _enum_text(getattr(item, "category", None)), _MAX_CATEGORY_CHARS
                ),
                message=_excerpt(
                    _coalesce(
                        _join(_text(item, "title"), _text(item, "description")), _NO_DETAIL
                    ),
                    MAX_WORKFLOW_TEXT_CHARS,
                ),
                evidence=_excerpt(
                    _join(_text(item, "evidence"), reference), MAX_WORKFLOW_TEXT_CHARS
                ),
            )
        )
    return tuple(collected)


# ---------------------------------------------------------------------------
# Utilidades puras
# ---------------------------------------------------------------------------
def _excerpt(value: object, limit: int) -> str:
    """Recorta un texto al límite indicado, sin inventar contenido.

    Un valor que no sea texto se traduce a cadena vacía: preferimos un hueco declarado a un
    ``str()`` de un objeto que nadie quiso convertir en prosa.
    """
    if limit < 1 or not isinstance(value, str):
        return ""
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _coalesce(*values: str) -> str:
    """Primer valor no vacío, o cadena vacía si no hay ninguno."""
    for value in values:
        if value:
            return value
    return ""


def _join(*parts: str) -> str:
    """Une los textos no vacíos con ``"; "``, en orden y sin duplicar separadores."""
    return "; ".join(part for part in parts if part)


def _artifacts(values: tuple[str, ...]) -> tuple[str, ...]:
    """Acota y deduplica las referencias de artefacto, conservando el orden."""
    bounded: list[str] = []
    for value in values:
        text = _excerpt(value, MAX_WORKFLOW_TEXT_CHARS)
        if text and text not in bounded:
            bounded.append(text)
        if len(bounded) == MAX_WORKFLOW_ARTIFACTS:
            break
    return tuple(bounded)


__all__ = [
    "CallableRoleExecutor",
    "CamusRoleExecutor",
    "RoleExecutor",
    "UnavailableRoleExecutor",
    "normalize_architecture",
    "normalize_cross_audit",
    "normalize_developer",
    "normalize_planning",
    "normalize_qa",
    "normalize_review",
    "normalize_security",
    "normalize_visual_qa",
]
