"""Frontera de confianza de la replanificación: la evidencia no se sustituye con otro plan.

Cierra la verificación del hallazgo F631-01. Antes de la corrección, ``CHILD_TECHNICAL_CODES``
incluía ``WORKFLOW_REPAIR_EVIDENCE_INCOMPLETE`` y ``WORKFLOW_REPAIR_SNAPSHOT_INVALID``: un child
``BLOCKED`` con esos códigos se clasificaba ``AUTONOMOUS_REPLAN_ALLOWED``, el replanner proponía un
plan nuevo y el proyecto cerraba ``COMPLETED``. Es incorrecto, y estas pruebas lo fijan delante del
estado durable:

1. la **allowlist técnica es corta y cerrada** (``CHILD_TECHNICAL_CODES``): solo un rol fallido, un
   rol bloqueado y un bucle de reparación sin progreso autorizan a replanificar en autonomía;
2. cualquier **frontera de confianza** —evidencia incompleta, instantánea o checkpoint inválidos,
   efecto desconocido, gasto desconocido, conflicto de idempotencia, brecha de presupuesto,
   política, prueba humana inválida— se declara con su elegibilidad de parada y **falla cerrado**;
3. un código que no esté en la allowlist **nunca** es autónomo: se recorre el catálogo entero de
   ``WorkflowFailureCode`` y de ``ProjectFailureCode`` para que un código nuevo del workflow no
   herede una autorización que nadie le concedió.

Todo es determinista y no toca ningún proveedor: usa el doble de child y el replanner doble de
``test_project_replan_kernel``.
"""

from __future__ import annotations

from pathlib import Path

from project_support import ChildOutcome
from punto.audit.logger import AuditLogger
from punto.project.replan import (
    CHILD_STOP_CODES,
    CHILD_TECHNICAL_CODES,
    NODE_STOP_CODES,
    NODE_TECHNICAL_CODES,
    ReplanClassification,
    classify_failure,
    classify_node,
)
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import TaskStatus
from punto.schemas.project import (
    ProjectFailureCode,
    ProjectNodeStatus,
    ProjectState,
)
from punto.schemas.replan import ReplanEligibility
from punto.schemas.workflow import WorkflowFailureCode
from test_project_replan_kernel import (
    FakeReplanner,
    blocked_child,
    event_types,
    replan_harness,
    replan_kernel,
)

#: Fronteras de confianza del **child**: el estado o la evidencia no son de fiar.
#:
#: Ninguna se arregla con otro plan —la replanificación no reconcilia ni sustituye corrupción de
#: evidencia, instantáneas inválidas, checkpoints inválidos, efectos desconocidos ni gasto
#: desconocido—, así que todas tienen que declararse con una elegibilidad de parada.
FRONTERAS_DE_CONFIANZA_DEL_CHILD: tuple[WorkflowFailureCode, ...] = (
    WorkflowFailureCode.WORKFLOW_REPAIR_EVIDENCE_INCOMPLETE,
    WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE,
    WorkflowFailureCode.WORKFLOW_REPAIR_SNAPSHOT_INVALID,
    WorkflowFailureCode.WORKFLOW_CHECKPOINT_INVALID,
    WorkflowFailureCode.WORKFLOW_RESUME_FAILED,
    WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED,
    WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED,
    WorkflowFailureCode.WORKFLOW_MODEL_SPEND_RECONCILIATION_REQUIRED,
    WorkflowFailureCode.WORKFLOW_IDEMPOTENCY_CONFLICT,
    WorkflowFailureCode.WORKFLOW_BUDGET_RECONCILIATION_REQUIRED,
    WorkflowFailureCode.WORKFLOW_BUDGET_RECONCILIATION_DENIED,
    WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED,
    WorkflowFailureCode.WORKFLOW_REPAIR_BUDGET_EXHAUSTED,
    WorkflowFailureCode.WORKFLOW_POLICY_REJECTED,
    WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID,
    WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
)

#: Fronteras de confianza del **nodo** del proyecto que no admiten otro plan.
FRONTERAS_DE_CONFIANZA_DEL_NODO: tuple[ProjectFailureCode, ...] = (
    ProjectFailureCode.PROJECT_BUDGET_BREACH,
    ProjectFailureCode.PROJECT_BUDGET_EXCEEDED,
    ProjectFailureCode.PROJECT_NODE_SCOPE_VIOLATION,
    ProjectFailureCode.PROJECT_WORKSPACE_REVISION_MISMATCH,
    ProjectFailureCode.PROJECT_GRAPH_INVALID,
    ProjectFailureCode.PROJECT_GRAPH_CHANGED,
    ProjectFailureCode.PROJECT_DEPENDENCY_EVIDENCE_INCOMPLETE,
    ProjectFailureCode.PROJECT_EFFECT_UNRECONCILED,
    ProjectFailureCode.PROJECT_APPROVAL_PROOF_INVALID,
    ProjectFailureCode.PROJECT_REPLAN_PROOF_INVALID,
    ProjectFailureCode.PROJECT_REPLAN_SPEND_RECONCILIATION_REQUIRED,
)


def _clasificar_child(code: WorkflowFailureCode) -> ReplanClassification:
    """Clasifica el fallo de un child con ese código y un nodo ``PROJECT_CHILD_BLOCKED``."""
    return classify_failure(
        node_failure_code=ProjectFailureCode.PROJECT_CHILD_BLOCKED,
        child_failure_code=code.value,
    )


def _clasificar_nodo(code: ProjectFailureCode) -> ReplanClassification:
    """Clasifica ese código de nodo con un child **técnico**: el peor caso para la frontera."""
    return classify_failure(
        node_failure_code=code,
        child_failure_code=WorkflowFailureCode.WORKFLOW_ROLE_FAILED.value,
    )


def _nodo_bloqueado_durable(tmp_path: Path, code: WorkflowFailureCode) -> ReplanClassification:
    """Clasifica el estado durable real que el kernel deja con ese child ``BLOCKED``.

    No se inventa el nodo: se ejecuta el proyecto con el guion del child y se lee del ``ProjectRun``
    el estado que un checkpoint deja en disco —nodo ``BLOCKED`` con ``PROJECT_CHILD_BLOCKED``—, que
    es exactamente el que la puerta del motor consulta al liquidar un nodo.
    """
    h = replan_harness(
        tmp_path,
        outcomes={
            "A": ChildOutcome(
                status=TaskStatus.BLOCKED,
                failure_code=code,
                failure_detail=f"el child cerró BLOCKED con {code.value}",
                model_calls=1,
                total_tokens=150,
            )
        },
    )
    kernel = replan_kernel(h, FakeReplanner())
    run = kernel.run_all(h.request)
    node = run.node("A")
    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_CHILD_BLOCKED
    return classify_node(run, node, child_failure_code=code.value)


# ---------------------------------------------------------------------------
# A/B - el estado durable del nodo: evidencia y instantánea no se replanifican
# ---------------------------------------------------------------------------
def test_evidencia_incompleta_no_se_replanifica_por_classify_node(tmp_path: Path) -> None:
    """La evidencia incompleta del bucle de reparación es una frontera de confianza, no estrategia.

    Es el caso del hallazgo F631-01: si el nodo está ``BLOCKED`` con ``PROJECT_CHILD_BLOCKED`` y el
    child cerró con ``WORKFLOW_REPAIR_EVIDENCE_INCOMPLETE``, no hay estrategia técnica agotada que
    sustituir —la evidencia no permite ni diagnosticar— y otro plan la taparía en vez de
    reconciliarla. La elegibilidad tiene que ser ``EVIDENCE_BLOCKED`` y **nunca** autónoma.
    """
    classification = _nodo_bloqueado_durable(
        tmp_path, WorkflowFailureCode.WORKFLOW_REPAIR_EVIDENCE_INCOMPLETE
    )

    assert classification.eligibility is ReplanEligibility.EVIDENCE_BLOCKED
    assert classification.eligibility is not ReplanEligibility.AUTONOMOUS_REPLAN_ALLOWED
    assert classification.allows_autonomous is False


def test_instantanea_invalida_no_se_replanifica_por_classify_node(tmp_path: Path) -> None:
    """Una instantánea que no permite deshacer la reparación no se rodea con otro plan.

    El estado durable es el mismo que en el caso anterior, pero el código es
    ``WORKFLOW_REPAIR_SNAPSHOT_INVALID``. Replanificar aquí adoptaría un grafo nuevo sobre un estado
    cuya reversibilidad nadie puede demostrar, que es exactamente lo que el hallazgo F631-01
    encontró: el proyecto podía cerrar ``COMPLETED`` sobre una frontera de confianza rota.
    """
    classification = _nodo_bloqueado_durable(
        tmp_path, WorkflowFailureCode.WORKFLOW_REPAIR_SNAPSHOT_INVALID
    )

    assert classification.eligibility is ReplanEligibility.EVIDENCE_BLOCKED
    assert classification.eligibility is not ReplanEligibility.AUTONOMOUS_REPLAN_ALLOWED
    assert classification.allows_autonomous is False


# ---------------------------------------------------------------------------
# C/D - el proyecto entero: parada con el código del nodo y cero llamadas
# ---------------------------------------------------------------------------
def test_evidencia_incompleta_detiene_el_proyecto_sin_llamar_al_replanner(tmp_path: Path) -> None:
    """El kernel completo para el proyecto con el código del nodo y sin gastar en el replanner.

    Es la garantía que el hallazgo F631-01 exigía: aunque el proyecto tenga ``max_replans`` ≥ 1 y un
    replanner inyectado, un child ``BLOCKED`` por evidencia incompleta **no** entra en
    ``REPLANNING``. El nodo no se acepta, el motor se detiene con ``PROJECT_CHILD_BLOCKED``, no se
    publica ningún disparador y el gasto del replan sigue en cero.
    """
    audit = AuditLogger()
    replanner = FakeReplanner()
    h = replan_harness(
        tmp_path,
        outcomes={
            "A": ChildOutcome(
                status=TaskStatus.BLOCKED,
                failure_code=WorkflowFailureCode.WORKFLOW_REPAIR_EVIDENCE_INCOMPLETE,
                failure_detail="la reparación no tiene evidencia suficiente para diagnosticar",
                model_calls=1,
                total_tokens=150,
            )
        },
    )
    kernel = replan_kernel(h, replanner, audit=audit)

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_CHILD_BLOCKED
    assert replanner.calls == [], "una frontera de evidencia no autoriza ninguna llamada"
    assert run.usage.replans_attempted == 0
    assert len(run.generations) == 1
    assert run.active_generation is not None
    assert run.active_generation.generation_index == 0
    types = event_types(audit)
    assert AuditEventType.PROJECT_REPLAN_ELIGIBILITY_EVALUATED in types, "la puerta sí se consultó"
    assert AuditEventType.PROJECT_REPLAN_TRIGGER_CREATED not in types


def test_instantanea_invalida_detiene_el_proyecto_sin_llamar_al_replanner(tmp_path: Path) -> None:
    """El mismo cierre para la instantánea inválida: parada, cero llamadas y generación 0.

    Se fija aparte del caso de evidencia porque el hallazgo F631-01 nombró **dos** códigos mal
    clasificados: la corrección tiene que cubrir los dos, no uno. El estado final es idéntico —nodo
    no aceptado, ``PROJECT_CHILD_BLOCKED``, sin disparador durable y sin generación nueva— porque la
    replanificación no es el camino en ninguno de los dos.
    """
    audit = AuditLogger()
    replanner = FakeReplanner()
    h = replan_harness(
        tmp_path,
        outcomes={
            "A": ChildOutcome(
                status=TaskStatus.BLOCKED,
                failure_code=WorkflowFailureCode.WORKFLOW_REPAIR_SNAPSHOT_INVALID,
                failure_detail="el snapshot previo a la reparación no permite deshacerla",
                model_calls=1,
                total_tokens=150,
            )
        },
    )
    kernel = replan_kernel(h, replanner, audit=audit)

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_CHILD_BLOCKED
    assert replanner.calls == [], "una instantánea inválida no autoriza ninguna llamada"
    assert run.usage.replans_attempted == 0
    assert len(run.generations) == 1
    assert run.active_generation is not None
    assert run.active_generation.generation_index == 0
    types = event_types(audit)
    assert AuditEventType.PROJECT_REPLAN_ELIGIBILITY_EVALUATED in types, "la puerta sí se consultó"
    assert AuditEventType.PROJECT_REPLAN_TRIGGER_CREATED not in types


# ---------------------------------------------------------------------------
# E - regresión: el fallo técnico sigue siendo replanificable
# ---------------------------------------------------------------------------
def test_no_progress_sigue_siendo_replanificable(tmp_path: Path) -> None:
    """Cerrar la frontera de evidencia no puede apagar la replanificación legítima.

    Es la mitad que impide que la corrección se pase de frenada: ``blocked_child()`` cierra el child
    ``BLOCKED`` con ``WORKFLOW_ROLE_FAILED`` —el código técnico replanificable por excelencia, junto
    a ``WORKFLOW_ROLE_BLOCKED`` y ``WORKFLOW_REPAIR_NO_PROGRESS``—, y ese caso sigue autorizando
    otra estrategia. Con ``max_replans`` = 1 el proyecto replanifica una vez, adopta la generación 1
    y cierra ``COMPLETED``.
    """
    replanner = FakeReplanner()
    h = replan_harness(tmp_path, outcomes={"A": blocked_child()}, default=ChildOutcome())
    kernel = replan_kernel(h, replanner)

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.COMPLETED, (run.status, run.failure_code)
    assert len(replanner.calls) == 1, "el fallo técnico sí llama al replanner, una sola vez"
    assert run.usage.replans_accepted == 1
    assert run.active_generation is not None
    assert run.active_generation.generation_index == 1
    assert len(run.generations) == 2, "la generación original no se borra"


# ---------------------------------------------------------------------------
# F - la allowlist es una decisión consciente, no una inferencia
# ---------------------------------------------------------------------------
def test_la_allowlist_tecnica_es_corta_y_cerrada() -> None:
    """La lista de códigos técnicos se fija literalmente: añadir uno exige tocar esta prueba.

    Es lo contrario de una heurística: si ``CHILD_TECHNICAL_CODES`` se ampliara —o si alguien
    reintrodujera en ella un código de evidencia o de instantánea—, esta igualdad falla y obliga a
    justificar por qué el problema es técnico, reversible y no amplía el contrato. Sin esta prueba,
    la allowlist podría crecer en silencio y reabrir el hallazgo F631-01.
    """
    allowlist_esperada = frozenset(
        {
            "WORKFLOW_ROLE_FAILED",
            "WORKFLOW_ROLE_BLOCKED",
            "WORKFLOW_REPAIR_NO_PROGRESS",
        }
    )

    assert allowlist_esperada == CHILD_TECHNICAL_CODES


# ---------------------------------------------------------------------------
# G - exhaustiva: ningún código de workflow de frontera es autónomo
# ---------------------------------------------------------------------------
def test_ningun_codigo_de_frontera_de_confianza_es_replanificable_autonomo() -> None:
    """Recorre **todo** el catálogo del workflow: autónomo si y solo si está en la allowlist.

    La prueba no enumera casos: enumera ``WorkflowFailureCode`` entero, de modo que un código nuevo
    del workflow queda cubierto el día que se añada, sin que nadie tenga que acordarse de esta
    suite. Además fija explícitamente las fronteras de confianza que el hallazgo F631-01 nombró
    —evidencia, instantánea, checkpoint, efecto desconocido, gasto desconocido, idempotencia,
    presupuesto, política y prueba humana inválida— con una elegibilidad de parada.

    Nota: el Human Gate no es ``is_stop`` sino ``HUMAN_REPLAN_REQUIRED`` (la decisión es de una
    persona, no del motor), así que ahí se afirma lo que sí es cierto: no es autónomo y exige
    humano.
    """
    for code in WorkflowFailureCode:
        classification = _clasificar_child(code)
        assert classification.allows_autonomous is (code.value in CHILD_TECHNICAL_CODES), (
            f"{code.value} quedó en {classification.eligibility.value}: la autorización autónoma "
            "solo la concede la allowlist técnica"
        )

    for code in FRONTERAS_DE_CONFIANZA_DEL_CHILD:
        classification = _clasificar_child(code)
        assert classification.eligibility.is_stop is True, (
            f"{code.value} debe ser una parada declarada y quedó en "
            f"{classification.eligibility.value}"
        )
        assert classification.allows_autonomous is False, f"{code.value} no puede ser autónomo"

    human_gate = _clasificar_child(WorkflowFailureCode.WORKFLOW_HUMAN_APPROVAL_REQUIRED)
    assert human_gate.eligibility is ReplanEligibility.HUMAN_REPLAN_REQUIRED
    assert human_gate.eligibility.requires_human is True
    assert human_gate.allows_autonomous is False

    for signal in (
        classify_failure(
            node_failure_code=ProjectFailureCode.PROJECT_CHILD_BLOCKED,
            child_failure_code=WorkflowFailureCode.WORKFLOW_ROLE_FAILED.value,
            credentials_missing=True,
        ),
        classify_failure(
            node_failure_code=ProjectFailureCode.PROJECT_CHILD_BLOCKED,
            child_failure_code=WorkflowFailureCode.WORKFLOW_ROLE_FAILED.value,
            security_stop=True,
        ),
        classify_failure(
            node_failure_code=ProjectFailureCode.PROJECT_CHILD_BLOCKED,
            child_failure_code=WorkflowFailureCode.WORKFLOW_ROLE_FAILED.value,
            policy_rejected=True,
        ),
    ):
        assert signal.eligibility.is_stop is True, signal.eligibility.value
        assert signal.allows_autonomous is False, signal.eligibility.value

    pending = classify_failure(
        node_failure_code=ProjectFailureCode.PROJECT_CHILD_BLOCKED,
        child_failure_code=WorkflowFailureCode.WORKFLOW_ROLE_FAILED.value,
        human_gate_pending=True,
    )
    assert pending.eligibility is ReplanEligibility.HUMAN_REPLAN_REQUIRED
    assert pending.allows_autonomous is False


# ---------------------------------------------------------------------------
# H - exhaustiva: ningún código del proyecto de frontera es autónomo
# ---------------------------------------------------------------------------
def test_ningun_codigo_de_proyecto_de_confianza_es_replanificable_autonomo() -> None:
    """Recorre **todo** ``ProjectFailureCode`` con un child técnico: solo decide el nodo.

    El child es ``WORKFLOW_ROLE_FAILED`` —replanificable— a propósito: así lo único que puede
    negar la autorización es el código del nodo, y la prueba mide que un código de proyecto nuevo
    no hereda la autorización técnica del child. Brecha de presupuesto, alcance, revisión, grafo,
    evidencia de dependencia, prueba humana inválida, gasto de replan sin reconciliar y efecto sin
    reconciliar son paradas declaradas.
    """
    for code in ProjectFailureCode:
        classification = _clasificar_nodo(code)
        assert classification.allows_autonomous is (code in NODE_TECHNICAL_CODES), (
            f"{code.value} quedó en {classification.eligibility.value}: la autorización autónoma "
            "solo la concede la allowlist técnica del nodo"
        )

    for code in FRONTERAS_DE_CONFIANZA_DEL_NODO:
        classification = _clasificar_nodo(code)
        assert classification.eligibility.is_stop is True, (
            f"{code.value} debe ser una parada declarada y quedó en "
            f"{classification.eligibility.value}"
        )
        assert classification.allows_autonomous is False, f"{code.value} no puede ser autónomo"

    human_gate = _clasificar_nodo(ProjectFailureCode.PROJECT_HUMAN_APPROVAL_REQUIRED)
    assert human_gate.eligibility is ReplanEligibility.HUMAN_REPLAN_REQUIRED
    assert human_gate.eligibility.requires_human is True
    assert human_gate.allows_autonomous is False


# ---------------------------------------------------------------------------
# I - las tablas de parada declaradas no autorizan nada
# ---------------------------------------------------------------------------
def test_los_codigos_de_parada_declarados_no_son_autonomos() -> None:
    """Ni un código de las tablas de parada puede salir autónomo, ni declarado ni derivado.

    Se comprueba dos veces: la elegibilidad que la tabla declara y la que ``classify_failure``
    devuelve para ese mismo código. Un error de copia entre las dos —el modo en que el hallazgo
    F631-01 se coló— no pasaría esta prueba ni aunque la tabla pareciera correcta.
    """
    for code, (_category, eligibility) in CHILD_STOP_CODES.items():
        assert eligibility.allows_autonomous_replan is False, code
        derived = classify_failure(node_failure_code=None, child_failure_code=code)
        assert derived.allows_autonomous is False, code
        assert derived.eligibility is eligibility, code

    for code, (_category, eligibility) in NODE_STOP_CODES.items():
        assert eligibility.allows_autonomous_replan is False, code.value
        derived = _clasificar_nodo(code)
        assert derived.allows_autonomous is False, code.value
        assert derived.eligibility is eligibility, code.value
