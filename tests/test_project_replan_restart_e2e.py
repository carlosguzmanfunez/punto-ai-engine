"""Reinicio real del intento de replanificación autónoma en cada frontera durable (ENGINE-6.3).

Qué se destruye y qué se reconstruye
------------------------------------
Esta suite no reanuda «desde un objeto que ya estaba en memoria»: en cada frontera **todos** los
objetos del proceso mueren —el ``ProjectExecutionKernel``, el ``WorkflowKernel``, CAMUS, los
adaptadores de rol, el runner real del Developer, el cliente del proveedor, el replanificador de
producción y el doble del ``PlannerRunner``— y el proceso nuevo se construye desde cero leyendo
solo lo que quedó en disco:

- ``FileProjectStore``: el ``ProjectRun`` con su contrato, su generación activa, el disparador, la
  autorización, la propuesta y la decisión del intento en curso;
- ``FileCheckpointStore``: el ``WorkflowRun`` de cada child, con sus pasos, su ``EffectLedger``, su
  plan de reparación y su snapshot;
- ``FileArtifactStore``: el contrato, los grafos congelados de las dos generaciones, el disparador,
  la propuesta, la decisión y los resultados del Developer;
- el árbol de Git: la rama de tarea, los commits y el ``HEAD``.

Las dos formas de caída
-----------------------
1. **Caída de almacén** (``CrashingProjectStore``): envuelve al ``FileProjectStore`` real, escribe
   con su escritura atómica de producción y **después** lanza una excepción propia que hereda de
   ``BaseException``, de modo que nadie la capture dentro del motor. Es la forma de morir justo en
   un hito durable: disparador, reserva, intención de gasto, propuesta, generación publicada,
   generación adoptada o nodo liquidado.
2. **Caída de decisión pura** (fronteras del guard y de la política): esos dos hitos no escriben
   nada —son funciones de decisión—, así que su frontera se materializa envolviendo la función real
   y lanzando la caída **después** de su veredicto y **antes** de que el kernel escriba la decisión.

Las diez fronteras del encargo
------------------------------
tras crear el disparador, tras la reserva, tras la intención de invocación, tras persistir la
propuesta, tras el veredicto del guard, tras la decisión de política, tras publicar el grafo nuevo
(antes de activar la generación), tras aceptar la generación, tras completar ``P`` y tras completar
``B2``.

Lo que se afirma en cada una
----------------------------
Que el proceso nuevo retoma **el mismo intento** —mismos ``generation_id``, misma propuesta (misma
huella), mismos ``child_workflow_id``— y llega al mismo desenlace, sin repetir ninguna llamada al
proveedor ni al replanificador y sin reiniciar el presupuesto acumulado. Las identidades que se
comparan contra la ejecución de referencia son las que **no** dependen de los SHA de Git: los
``workflow_id`` de los children del grafo original, la forma del grafo resultante, el contenido del
árbol final, los asuntos de commit, el consumo y el diario durable de llamadas al modelo.

Dos precisiones que el encargo exige dejar dichas
-------------------------------------------------
- **``final_graph_fingerprint``**: la huella del grafo incluye la identidad de sus nodos, y la
  identidad de un nodo nuevo la asigna el motor a partir de la huella de la propuesta, que incluye
  la identidad —``uuid4``— del disparador. Por eso su **valor** es propio de cada escenario; lo que
  se afirma, y es lo que importa, es que la huella final sea la misma **dentro** del escenario a
  ambos lados de la caída (la de la generación persistida antes de morir), que la generación la
  declare igual, que el grafo resuelto la reproduzca y que el **contenido** del grafo sea el de la
  referencia.
- **La frontera de la invocación en vuelo** (PART O): si el proceso muere **después** de declarar la
  intención de gasto y **antes** de que la propuesta quede durable, el motor no puede distinguir «no
  llegó a llamar» de «llamó y se perdió la respuesta». Falla cerrado: liquida el intento y bloquea
  con ``PROJECT_REPLAN_SPEND_RECONCILIATION_REQUIRED`` **sin** volver a llamar a nadie. Esta suite
  lo afirma como el desenlace correcto de esa frontera, no como una carencia: es la garantía de que
  el reinicio no duplica gasto.

Y una última precisión, la misma que documenta ``test_project_replan_e2e.py``: cuando el motor
adopta una generación nueva **devuelve el árbol a la revisión aceptada** en ese mismo hito (defecto
D-1, cerrado: ``_reconcile_replan_workspace``), así que la conducción de cada proceso no toca el
árbol en ningún momento: la única pieza que lo mueve es el motor. Lo que esta suite mide es que ese
paso —como todos los demás— sea idempotente ante una caída: a ambos lados de la frontera el árbol es
el mismo contenido y ninguna llamada de modelo se repite.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import httpx
import pytest

from punto.audit.logger import AuditLogger
from punto.developer.sandbox import ContainerSandboxBackend, SandboxLimits
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.orchestrator.state_machine import StateMachine
from punto.planner.base import PlannerLimits
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.project.contract import resolve_contract
from punto.project.generations import resolve_active_nodes
from punto.project.graph import graph_fingerprint
from punto.project.handoff import project_run_id_for
from punto.project.kernel import ProjectExecutionKernel
from punto.project.replan import resolve_trigger
from punto.project.replan_guard import ProjectReplanGuard
from punto.project.replanner import (
    PlannerProjectReplanner,
    ProjectReplanProposal,
    ReplanRequest,
    resolve_proposal,
)
from punto.project.store import FileProjectStore, ProjectSnapshot, ProjectStore
from punto.project.workspace import GitWorkspaceLineage
from punto.providers.deepseek import DeepSeekClient, DeepSeekConfig
from punto.schemas.project import (
    ProjectFailureCode,
    ProjectNodeStatus,
    ProjectRequest,
    ProjectRun,
    ProjectState,
)
from punto.schemas.workflow import RoleName
from punto.tasks.manager import TaskManager
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import CheckpointStore, FileCheckpointStore
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from punto.workflow.roles import CamusRoleExecutor
from test_project_replan_e2e import (
    GUION,
    MODEL,
    OBJETIVO_B,
    TARGET,
    AlternativePlanRunner,
    FixedArchitectRunner,
    FixedPlannerRunner,
    NodeAwareQARunner,
    PassingCrossAuditRunner,
    PassingReviewerRunner,
    PassingSecurityRunner,
    WatchingDeepSeekRunner,
    build_workspace,
    chat_response,
    git,
    node_scope,
    publish_plan,
    scenario_request,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

#: Credencial **sintética**: la prueba no usa ni necesita la credencial real.
FAKE_API_KEY: Final[str] = "sk-test-e2e-replan-reinicio"

#: Modelo del cliente real. No hay red: la respuesta la pone el transporte simulado.
CLIENT_MAX_TOKENS: Final[int] = 60_000
RUNNER_MAX_CALLS: Final[int] = 8
RUNNER_MAX_INPUT_TOKENS: Final[int] = 200_000
RUNNER_MAX_OUTPUT_TOKENS: Final[int] = 60_000

#: Clave de proyecto **fija**: con ella, el ``project_run_id`` —y por tanto el ``workflow_id`` de
#: cada child del grafo original— es el mismo en la referencia y en los diez escenarios de caída.
PROJECT_RUN_KEY: Final[str] = "replan-reinicio"

#: Nodos del grafo original: los que existían antes de la replanificación. Sirven para distinguir
#: los nodos nuevos de la generación 1 en los predicados de frontera.
ORIGINAL_NODES: Final[frozenset[str]] = frozenset({"A", "B", "C"})

#: Máximo de procesos por escenario: el que muere y el que retoma. El margen existe para que un
#: escenario que no cierre se declare con un fallo explícito en vez de girar.
MAX_PROCESOS: Final[int] = 4

#: Hitos máximos que un proceso conduce antes de declarar que no progresa.
MAX_STEPS: Final[int] = 48

#: Nombres de los ficheros durables del guion del modelo y del diario del replanificador.
CONTADOR_MODELO: Final[str] = "modelo-contador.txt"
DIARIO_MODELO: Final[str] = "modelo-diario.jsonl"
CONTADOR_REPLAN: Final[str] = "replan-contador.txt"
DIARIO_REPLAN: Final[str] = "replan-diario.jsonl"


class SimulatedCrash(BaseException):
    """Caída de proceso simulada: no es un ``WorkflowError``, así que nadie la captura dentro.

    Hereda de ``BaseException`` a propósito: el kernel captura ``WorkflowError`` para reintentar y
    para traducir fallos, y una caída del proceso no es ninguna de las dos cosas.
    """


class CrashingProjectStore:
    """``ProjectStore`` real que **persiste y después mata** el proceso en la frontera.

    La escritura durable la hace el ``FileProjectStore`` real —con su escritura atómica y su
    marcador de commit—; la caída ocurre justo después, así que el estado que sobrevive es
    exactamente el de la frontera y no uno a medias.
    """

    def __init__(self, root: Path, detiene: Callable[[ProjectRun], bool]) -> None:
        """Envuelve el almacén real con el predicado que decide en qué escritura se muere."""
        self.inner = FileProjectStore(root)
        self.detiene = detiene
        self.escrituras = 0

    def save(self, run: ProjectRun) -> ProjectSnapshot:
        """Persiste el run y, si **es** la frontera, simula la caída del proceso."""
        snapshot = self.inner.save(run)
        self.escrituras += 1
        if self.detiene(run):
            raise SimulatedCrash(
                f"el proceso muere en la frontera tras la escritura {snapshot.sequence} "
                f"del proyecto {run.project_run_id}"
            )
        return snapshot

    def latest(self, project_run_id: UUID) -> ProjectSnapshot | None:
        """Delega en el almacén real: el último snapshot confirmado."""
        return self.inner.latest(project_run_id)

    def load(self, project_run_id: UUID) -> ProjectRun:
        """Delega en el almacén real: el run del último snapshot, validado por completo."""
        return self.inner.load(project_run_id)

    def list_snapshots(self, project_run_id: UUID) -> tuple[ProjectSnapshot, ...]:
        """Delega en el almacén real: todos los snapshots confirmados, en orden."""
        return self.inner.list_snapshots(project_run_id)


class DurableModelScript:
    """Guion del modelo que sobrevive a la caída: contador e índice de llamada en disco.

    El proceso que responde al modelo es siempre otro, así que el guion no puede vivir en memoria:
    antes de responder **incrementa en disco** un contador y devuelve el contenido de esa posición,
    y anota en un diario el índice y el ``app.py`` real que había en el árbol. Con eso, el conjunto
    de procesos de un escenario deja una única secuencia de llamadas: si un reinicio repitiera una
    petición, el diario tendría una entrada de más y su índice rompería la secuencia ``0..n-1``.
    """

    def __init__(
        self,
        *,
        contador: Path,
        diario: Path,
        contents: Sequence[str],
        workspace: Path,
        target: str = TARGET,
        model: str = MODEL,
    ) -> None:
        """Guarda las rutas durables y el guion de respuestas."""
        self._contador = contador
        self._diario = diario
        self._contents = list(contents)
        self._workspace = workspace
        self._target = target
        self._model = model

    def _siguiente_indice(self) -> int:
        """Índice durable de la llamada: se lee, se incrementa y se persiste antes de responder."""
        actual = (
            int(self._contador.read_text(encoding="utf-8")) if self._contador.exists() else 0
        )
        self._contador.write_text(str(actual + 1), encoding="utf-8")
        return actual

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Registra la llamada en el diario durable y devuelve el contenido que le toca."""
        _ = request
        indice = self._siguiente_indice()
        observado = (self._workspace / self._target).read_text(encoding="utf-8")
        with self._diario.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps({"indice": indice, "observado": observado}, sort_keys=True) + "\n"
            )
        contenido = self._contents[min(indice, len(self._contents) - 1)]
        return chat_response(contenido, model=self._model)


class DurableReplanJournal:
    """Diario durable del replanificador: cuántas invocaciones salieron y con qué encargo.

    Existe porque el replanificador vive en el proceso que muere: sin una cuenta en disco, un
    reinicio que volviera a pedir la propuesta no dejaría rastro. Cada invocación incrementa el
    contador y anota el disparador del encargo; el conjunto de procesos tiene que dejar **una sola**
    entrada.
    """

    def __init__(self, *, contador: Path, diario: Path) -> None:
        """Guarda las rutas durables del contador y del diario."""
        self._contador = contador
        self._diario = diario

    @property
    def invocaciones(self) -> int:
        """Invocaciones registradas hasta ahora, leídas del contador durable."""
        if not self._contador.exists():
            return 0
        return int(self._contador.read_text(encoding="utf-8"))

    def register(self, request: ReplanRequest) -> None:
        """Anota una invocación del replanificador con el disparador que la motivó."""
        indice = self.invocaciones
        self._contador.write_text(str(indice + 1), encoding="utf-8")
        with self._diario.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "indice": indice,
                        "trigger_id": str(request.trigger.trigger_id),
                        "candidatos": list(request.superseded_candidates),
                    },
                    sort_keys=True,
                )
                + "\n"
            )


class JournaledProjectReplanner:
    """Replanificador de producción con el encargo anotado en un diario **durable**.

    Es un envoltorio que no decide nada: delega el puerto completo en
    ``PlannerProjectReplanner`` —el adaptador real, con su encargo acotado, sus límites efectivos y
    su traducción del ``PlanningOutcome``— y lo único que añade es anotar en disco el encargo antes
    de delegarlo. Esa anotación es lo que permite afirmar, después de un reinicio, que el proceso
    nuevo **no** volvió a pedir la propuesta: el diario tendría una entrada de más.
    """

    def __init__(self, *, journal: DurableReplanJournal, inner: PlannerProjectReplanner) -> None:
        """Guarda el diario durable y el adaptador de producción al que delega todo."""
        self.journal = journal
        self.inner = inner

    @property
    def name(self) -> str:
        """Nombre del replanificador: el del adaptador real, que no se inventa identidad."""
        return self.inner.name

    @property
    def provider(self) -> str:
        """Proveedor que declara el adaptador real."""
        return self.inner.provider

    @property
    def uses_ai(self) -> bool:
        """``True`` si el adaptador consulta un modelo."""
        return self.inner.uses_ai

    @property
    def limits(self) -> PlannerLimits | None:
        """Cota que declara el adaptador real antes de la autorización."""
        return self.inner.limits

    def propose(self, request: ReplanRequest) -> ProjectReplanProposal:
        """Anota el encargo en el diario durable y delega en el adaptador de producción."""
        self.journal.register(request)
        return self.inner.propose(request)


def read_journal(path: Path) -> tuple[tuple[int, str], ...]:
    """Diario durable de llamadas al modelo, como pares ``(índice, contenido observado)``.

    Un diario que no existe significa «no salió ninguna llamada», que es un hecho distinto de un
    diario vacío y se representa igual: la tupla vacía.
    """
    if not path.exists():
        return ()
    entries: list[tuple[int, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        entries.append((int(payload["indice"]), str(payload["observado"])))
    return tuple(entries)


def read_replan_journal(path: Path) -> tuple[int, ...]:
    """Índices de las invocaciones del replanificador anotadas en el diario durable."""
    if not path.exists():
        return ()
    lines = path.read_text(encoding="utf-8").splitlines()
    return tuple(int(json.loads(line)["indice"]) for line in lines)


# ---------------------------------------------------------------------------
# Montaje: un proceso completo, reconstruido entero desde disco
# ---------------------------------------------------------------------------
class Process:
    """Un proceso completo del proyecto: kernel de proyecto, de workflow y CAMUS reales, desde cero.

    Cada instancia es un proceso **nuevo**: no comparte ni un objeto con la anterior más allá de los
    almacenes durables que se le inyectan y del sandbox del sistema, que es del entorno y no del
    motor. Es exactamente lo que un reinicio real deja en pie.
    """

    def __init__(
        self,
        *,
        workspace: Path,
        config_dir: Path,
        projects: ProjectStore,
        checkpoints: CheckpointStore,
        artifacts_root: Path,
        script: DurableModelScript,
        journal: DurableReplanJournal,
        sandbox: ContainerSandboxBackend | None,
    ) -> None:
        """Ensambla el motor completo con el replanificador de producción y su diario durable."""
        self.workspace = workspace
        self.client = DeepSeekClient(
            DeepSeekConfig(api_key=FAKE_API_KEY, model=MODEL, max_tokens=CLIENT_MAX_TOKENS),
            transport=httpx.MockTransport(script.handler),
        )
        self.developer = WatchingDeepSeekRunner(client=self.client, backend=sandbox)
        self.artifacts = FileArtifactStore(artifacts_root)
        self.camus = Camus(
            task_manager=TaskManager(state_machine=StateMachine(), audit=AuditLogger()),
            policy_engine=PolicyEngine.from_config(config_dir),
            human_gate=HumanGate(),
            audit=AuditLogger(),
            planner=Planner(),
            architect_runner=FixedArchitectRunner(),
            planner_runner=FixedPlannerRunner(),
            developer_runner=self.developer,
            qa_runner=NodeAwareQARunner(
                workspace=workspace, failing_objectives=(OBJETIVO_B,)
            ),
            security_runner=PassingSecurityRunner(),
            reviewer_runner=PassingReviewerRunner(),
            cross_audit_runner=PassingCrossAuditRunner(),
        )
        self.executors: dict[RoleName, CamusRoleExecutor] = {
            role: CamusRoleExecutor(camus=self.camus, role=role, artifacts=self.artifacts)
            for role in RoleName
        }
        self.workflow = WorkflowKernel(
            executors=self.executors,
            store=checkpoints,
            audit=AuditLogger(),
            policy=WorkflowPolicy(engine=PolicyEngine.from_config(config_dir), gate=HumanGate()),
            artifacts=self.artifacts,
            workspace=workspace,
        )
        self.kernel = ProjectExecutionKernel(
            store=projects,
            workflow=self.workflow,
            artifacts=self.artifacts,
            lineage=GitWorkspaceLineage(workspace),
            audit=AuditLogger(),
            replanner=JournaledProjectReplanner(
                journal=journal,
                inner=PlannerProjectReplanner(runner=AlternativePlanRunner()),
            ),
            policy=WorkflowPolicy(engine=PolicyEngine.from_config(config_dir), gate=HumanGate()),
        )

    def __enter__(self) -> Process:
        """Devuelve el proceso listo para ejecutar."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Cierra el cliente HTTP del transporte simulado."""
        self.client.close()


def build_process(
    *,
    workspace: Path,
    config_dir: Path,
    root: Path,
    script: DurableModelScript,
    journal: DurableReplanJournal,
    sandbox: ContainerSandboxBackend | None,
    frontera: Frontera | None,
) -> Process:
    """Proceso nuevo desde cero; con ``frontera`` el almacén mata el proceso en esa escritura."""
    projects: ProjectStore = (
        FileProjectStore(root / "projects")
        if frontera is None or frontera.proyecto is None
        else CrashingProjectStore(root / "projects", frontera.proyecto)
    )
    return Process(
        workspace=workspace,
        config_dir=config_dir,
        projects=projects,
        checkpoints=FileCheckpointStore(root / "children"),
        artifacts_root=root / "artifacts",
        script=script,
        journal=journal,
        sandbox=sandbox,
    )


# ---------------------------------------------------------------------------
# Fronteras de caída
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Frontera:
    """Una frontera de caída: dónde muere el proceso y qué tiene que haber quedado en disco.

    ``proyecto`` es el predicado que el almacén evalúa **después** de persistir; ``gancho`` es la
    caída de una decisión pura (el guard y la política no escriben nada, así que su frontera se
    materializa envolviendo su función real). ``durable`` describe el estado que sobrevive y
    ``esperado`` es su valor: sin esa comprobación, un predicado que nunca se cumple dejaría el
    escenario sin probar nada y la prueba pasaría por accidente.

    ``estado_final`` declara el desenlace que el motor alcanza al retomar desde esa frontera: todas
    cierran el proyecto salvo la de la invocación en vuelo, que bloquea a propósito (PART O).
    """

    clave: str
    descripcion: str
    esperado: str
    durable: Callable[[ProjectRun], str]
    proyecto: Callable[[ProjectRun], bool] | None = None
    gancho: Callable[[], Iterator[None]] | None = None
    estado_final: ProjectState = ProjectState.COMPLETED
    codigo_final: ProjectFailureCode | None = None


@contextlib.contextmanager
def crash_after_guard() -> Iterator[None]:
    """Caída **después** del veredicto del guard y antes de que el kernel escriba la decisión.

    El guard es una función de decisión pura: no publica artefactos ni escribe en disco, así que su
    frontera no se puede materializar con un almacén que mata. Se envuelve el método real, se evalúa
    de verdad y se muere con el veredicto ya calculado. Lo que sobrevive es la propuesta durable; el
    proceso nuevo volverá a juzgarla —juzgar es gratis y no gasta proveedor— y no volverá a pedirla.
    """
    original = ProjectReplanGuard.evaluate

    def evaluated(*args: Any, **kwargs: Any) -> Any:
        """Evalúa de verdad y muere con el veredicto en la mano."""
        original(*args, **kwargs)
        raise SimulatedCrash("caída justo después del veredicto del guard")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ProjectReplanGuard, "evaluate", evaluated)
        yield


@contextlib.contextmanager
def crash_after_policy() -> Iterator[None]:
    """Caída **después** del veredicto de política y antes de la decisión del motor.

    Misma idea que :func:`crash_after_guard` una comprobación más adelante: la política tampoco
    escribe nada, así que la frontera se construye evaluándola de verdad y muriendo antes de que el
    kernel publique la decisión. El proceso nuevo vuelve a evaluar la acción —la política es
    determinista y no gasta— y sigue.
    """
    original = ProjectExecutionKernel._evaluate_replan_policy

    def evaluated(self: ProjectExecutionKernel, *args: Any, **kwargs: Any) -> Any:
        """Evalúa de verdad y muere con el veredicto de política en la mano."""
        original(self, *args, **kwargs)
        raise SimulatedCrash("caída justo después del veredicto de política")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ProjectExecutionKernel, "_evaluate_replan_policy", evaluated)
        yield


def pending_graph(run: ProjectRun) -> bool:
    """``True`` si la generación nueva está persistida y todavía no activada."""
    if len(run.generations) < 2 or run.active_generation is None:
        return False
    return run.active_generation.generation_index == 0


def adopted_generation(run: ProjectRun) -> bool:
    """``True`` si la generación nueva está activa y el proyecto ya volvió a ``RUNNING``."""
    if run.active_generation is None or run.active_generation.generation_index < 1:
        return False
    return run.status is ProjectState.RUNNING and run.active_node_id == ""


def replan_phase(run: ProjectRun) -> str:
    """Nombre del hito durable del intento en el que está el run, en orden de avance."""
    if adopted_generation(run):
        return "adoptada"
    if pending_graph(run):
        return "generacion-pendiente"
    if run.active_replan_decision_ref is not None:
        return "decision"
    if run.active_replan_proposal_ref is not None:
        return "propuesta"
    authorization = run.active_replan_authorization
    if authorization is not None and authorization.invocation_started:
        return "invocacion-iniciada"
    if authorization is not None:
        return "reserva"
    if run.active_replan_trigger is not None:
        return "disparador"
    return "sin-intento"


def new_completed(run: ProjectRun) -> tuple[str, ...]:
    """Identificadores de los nodos **nuevos** de la generación 1 ya aceptados por el parent."""
    return tuple(
        node.node_id
        for node in run.nodes
        if node.node_id not in ORIGINAL_NODES
        and node.status is ProjectNodeStatus.COMPLETED
    )


#: Las diez fronteras del encargo, cada una con el estado durable que tiene que dejar escrito.
FRONTERAS: Final[tuple[Frontera, ...]] = (
    Frontera(
        clave="tras-crear-el-trigger",
        descripcion="el disparador está publicado y el intento no ha reservado nada",
        esperado="REPLANNING:disparador",
        durable=lambda run: f"{run.status.value}:{replan_phase(run)}",
        proyecto=lambda run: (
            run.status is ProjectState.REPLANNING
            and run.active_replan_trigger is not None
            and run.active_replan_authorization is None
        ),
    ),
    Frontera(
        clave="tras-la-reserva",
        descripcion="la invocación está reservada (llamadas, tokens y replan) y no ha salido",
        esperado="REPLANNING:reserva",
        durable=lambda run: f"{run.status.value}:{replan_phase(run)}",
        proyecto=lambda run: (
            run.status is ProjectState.REPLANNING
            and run.active_replan_authorization is not None
            and not run.active_replan_authorization.invocation_started
        ),
    ),
    Frontera(
        clave="tras-el-intent-de-invocacion",
        descripcion="la intención de gasto está escrita y la propuesta todavía no existe",
        esperado="REPLANNING:invocacion-iniciada",
        durable=lambda run: f"{run.status.value}:{replan_phase(run)}",
        proyecto=lambda run: (
            run.status is ProjectState.REPLANNING
            and run.active_replan_authorization is not None
            and run.active_replan_authorization.invocation_started
            and run.active_replan_proposal_ref is None
        ),
        # PART O: con el gasto en outcome desconocido, el motor no reintenta a ciegas. Retomar esta
        # frontera **no** cierra el proyecto: lo bloquea declarando que hace falta reconciliar el
        # gasto, que es exactamente lo que impide duplicar la llamada al proveedor.
        estado_final=ProjectState.BLOCKED,
        codigo_final=ProjectFailureCode.PROJECT_REPLAN_SPEND_RECONCILIATION_REQUIRED,
    ),
    Frontera(
        clave="tras-persistir-la-propuesta",
        descripcion="la propuesta está publicada y el motor todavía no la ha juzgado",
        esperado="REPLANNING:propuesta",
        durable=lambda run: f"{run.status.value}:{replan_phase(run)}",
        proyecto=lambda run: (
            run.active_replan_proposal_ref is not None
            and run.active_replan_decision_ref is None
        ),
    ),
    Frontera(
        clave="tras-el-veredicto-del-guard",
        descripcion="el guard ya dictó veredicto y la política todavía no se ha evaluado",
        esperado="REPLANNING:propuesta",
        durable=lambda run: f"{run.status.value}:{replan_phase(run)}",
        gancho=crash_after_guard,
    ),
    Frontera(
        clave="tras-la-decision-de-politica",
        descripcion="la política ya dictó veredicto y la decisión del motor no está escrita",
        esperado="REPLANNING:propuesta",
        durable=lambda run: f"{run.status.value}:{replan_phase(run)}",
        gancho=crash_after_policy,
    ),
    Frontera(
        clave="tras-publicar-el-grafo-nuevo",
        descripcion="el bundle del grafo nuevo está publicado y la generación, pendiente",
        esperado="REPLANNING:generacion-pendiente",
        durable=lambda run: f"{run.status.value}:{replan_phase(run)}",
        proyecto=pending_graph,
    ),
    Frontera(
        clave="tras-aceptar-la-generacion",
        descripcion=(
            "la generación 1 manda, sus nodos existen y el proyecto va a arrancar el primero"
        ),
        esperado="RUNNING:adoptada",
        durable=lambda run: f"{run.status.value}:{replan_phase(run)}",
        proyecto=adopted_generation,
    ),
    Frontera(
        clave="tras-completar-P",
        descripcion="el prerrequisito P está aceptado y el sustituto B2 todavía no ha arrancado",
        esperado="RUNNING:1-nodo-nuevo",
        durable=lambda run: f"{run.status.value}:{len(new_completed(run))}-nodo-nuevo",
        proyecto=lambda run: (
            run.status is ProjectState.RUNNING and len(new_completed(run)) == 1
        ),
    ),
    Frontera(
        clave="tras-completar-B2",
        descripcion="el sustituto B2 está aceptado y solo queda C",
        esperado="RUNNING:2-nodo-nuevo",
        durable=lambda run: f"{run.status.value}:{len(new_completed(run))}-nodo-nuevo",
        proyecto=lambda run: (
            run.status is ProjectState.RUNNING and len(new_completed(run)) == 2
        ),
    ),
)


# ---------------------------------------------------------------------------
# Medición: lo que dos ejecuciones tienen que reproducir igual
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Anclas:
    """Identidades durables del intento: lo que un reinicio **no** puede volver a inventar.

    ``trigger_id``, la huella de la propuesta, los ``generation_id`` y los ``child_workflow_id``
    son la huella dactilar del intento: si el proceso nuevo creara un disparador, una propuesta, una
    generación o un child distintos, estos valores cambiarían y la prueba lo vería.
    """

    trigger_id: UUID | None
    proposal_fingerprint: str
    generation_ids: tuple[UUID, ...]
    child_ids: tuple[tuple[str, UUID | None], ...]


@dataclass(frozen=True, slots=True)
class Ejecucion:
    """Desenlace medible de un escenario: estado, identidades, linaje, consumo y llamadas."""

    run: ProjectRun
    durable: str
    anclas_caida: Anclas
    anclas_final: Anclas
    child_ids: tuple[UUID | None, ...]
    estados: tuple[str, ...]
    forma_grafo: tuple[tuple[str, str, tuple[str, ...], int], ...]
    huella_final: str
    revision_final: str
    arbol: str
    asuntos: tuple[str, ...]
    commits: int
    main: str
    commits_en_main: int
    contenido: str
    llamadas: tuple[tuple[int, str], ...]
    llamadas_replan: tuple[int, ...]
    cifras: tuple[int, ...]


def anchors_of(run: ProjectRun, artifacts: FileArtifactStore) -> Anclas:
    """Identidades durables del intento tal como están escritas en el run y en el almacén."""
    trigger_id = None if run.active_replan_trigger is None else run.active_replan_trigger.trigger_id
    reference = run.active_replan_proposal_ref
    for generation in reversed(run.generations):
        if reference is None and generation.replan_proposal_ref is not None:
            reference = generation.replan_proposal_ref
        if trigger_id is None and generation.replan_trigger_ref is not None:
            trigger = resolve_trigger(artifacts, generation.replan_trigger_ref)
            if trigger is not None:
                trigger_id = trigger.trigger_id
    fingerprint = ""
    if reference is not None:
        proposal = resolve_proposal(artifacts, reference)
        if proposal is not None:
            fingerprint = proposal.proposal_fingerprint
    return Anclas(
        trigger_id=trigger_id,
        proposal_fingerprint=fingerprint,
        generation_ids=tuple(generation.generation_id for generation in run.generations),
        child_ids=tuple((node.node_id, node.child_workflow_id) for node in run.nodes),
    )


def graph_shape(
    artifacts: FileArtifactStore, run: ProjectRun
) -> tuple[tuple[str, str, tuple[str, ...], int], ...]:
    """Contenido del grafo activo, sin identidades: lo que dos escenarios tienen que compartir.

    La identidad de los nodos nuevos es del motor y depende de la identidad —aleatoria— del
    disparador, así que no se compara entre escenarios; lo que sí es el mismo plan es el título, el
    objetivo, el alcance y el número de dependencias de cada nodo, y eso es lo que se mide.
    """
    return tuple(
        (node.title, node.objective, tuple(node.allowed_files), len(node.dependencies))
        for node in resolve_active_nodes(artifacts, run)
    )


def usage_cifras(run: ProjectRun) -> tuple[int, ...]:
    """Cifras deterministas del consumo, para comparar dos ejecuciones.

    El tiempo de pared se excluye a propósito: es el único valor del consumo que mide un reloj real.
    """
    usage = run.usage
    return (
        usage.nodes_started,
        usage.nodes_completed,
        usage.child_workflows,
        usage.child_workflows_reserved,
        usage.model_calls,
        usage.model_calls_reserved,
        usage.total_tokens,
        usage.tokens_reserved,
        usage.repairs,
        usage.replans_attempted,
        usage.replans_accepted,
        usage.replans_reserved,
        usage.graph_generations,
    )


def describe(run: ProjectRun) -> str:
    """Traza legible del proyecto: estado, código, detalle y estado de cada nodo."""
    code = run.failure_code.value if run.failure_code else "-"
    nodes = ", ".join(f"{node.node_id}:{node.status.value}" for node in run.nodes)
    return f"{run.status.value} ({code}) {run.failure_detail} | nodos: {nodes}"


@contextlib.contextmanager
def crash_hook(frontera: Frontera | None) -> Iterator[None]:
    """Instala el gancho de caída de la frontera, o no instala nada si la frontera no lo tiene."""
    if frontera is None or frontera.gancho is None:
        yield
        return
    with frontera.gancho():
        yield


def drive_process(process: Process, request: ProjectRequest, workspace: Path) -> ProjectRun:
    """Conduce un proceso entero, hito a hito, sin tocar el árbol por fuera del motor.

    Es el bucle de ``run_all``: cada hito lo decide el motor, **incluida** la vuelta del árbol a la
    revisión aceptada cuando adopta una generación nueva (defecto D-1, cerrado en
    ``project/kernel.py``). ``workspace`` se recibe —y no se usa— para dejar explícito que esta
    función no escribe en el árbol: la única pieza que lo mueve es el motor.
    """
    del workspace
    run = process.kernel.create(request)
    for _ in range(MAX_STEPS):
        if run.is_terminal or run.is_paused:
            return run
        run = process.kernel.step(run)
    raise AssertionError(f"el proceso no cerró el proyecto en {MAX_STEPS} hitos: {describe(run)}")


def measure(
    run: ProjectRun,
    *,
    root: Path,
    durable: str,
    anclas_caida: Anclas,
    anclas_final: Anclas,
) -> Ejecucion:
    """Mide el desenlace de un escenario sobre el disco: identidades, linaje, consumo y llamadas.

    Un proyecto bloqueado no tiene resultado —el resultado solo lo escribe el cierre—, así que la
    revisión final se mide del linaje aceptado, que es el dato que existe en los dos desenlaces.
    """
    workspace = root / "proyecto"
    artifacts = FileArtifactStore(root / "artifacts")
    revision = run.workspace.accepted_revision if run.result is None else run.result.final_revision
    return Ejecucion(
        run=run,
        durable=durable,
        anclas_caida=anclas_caida,
        anclas_final=anclas_final,
        child_ids=tuple(node.child_workflow_id for node in run.nodes),
        estados=tuple(node.status.value for node in run.nodes),
        forma_grafo=graph_shape(artifacts, run),
        huella_final=run.graph_fingerprint,
        revision_final=revision,
        arbol=git(workspace, "rev-parse", "HEAD^{tree}"),
        asuntos=tuple(git(workspace, "log", "--format=%s", "HEAD").splitlines()),
        commits=int(git(workspace, "rev-list", "--count", "HEAD")),
        main=git(workspace, "rev-parse", "main"),
        commits_en_main=int(git(workspace, "rev-list", "--count", "main")),
        contenido=(workspace / TARGET).read_text(encoding="utf-8"),
        llamadas=read_journal(root / DIARIO_MODELO),
        llamadas_replan=read_replan_journal(root / DIARIO_REPLAN),
        cifras=usage_cifras(run),
    )


def run_scenario(
    *,
    root: Path,
    config_dir: Path,
    sandbox: ContainerSandboxBackend | None,
    frontera: Frontera | None,
) -> Ejecucion:
    """Ejecuta el escenario: procesos que mueren en la frontera y procesos nuevos que retoman.

    El primer proceso se construye entero —kernel, CAMUS, adaptadores, runner real y replanificador—
    y muere donde diga la frontera; los siguientes se construyen **desde cero**, sin compartir un
    solo objeto con el anterior, y solo pueden leer el disco. Si la frontera no llega a dispararse,
    el escenario no prueba nada y se declara fallido en vez de dar por buena una caída que no
    ocurrió.
    """
    root.mkdir(parents=True, exist_ok=True)
    workspace = build_workspace(root / "proyecto")
    plan_ref = publish_plan(root=root)
    request = scenario_request(plan_ref=plan_ref, workspace=workspace, key=PROJECT_RUN_KEY)
    project_run_id = project_run_id_for(request)
    script = DurableModelScript(
        contador=root / CONTADOR_MODELO,
        diario=root / DIARIO_MODELO,
        contents=GUION,
        workspace=workspace,
    )
    journal = DurableReplanJournal(
        contador=root / CONTADOR_REPLAN, diario=root / DIARIO_REPLAN
    )
    artifacts = FileArtifactStore(root / "artifacts")

    durable = ""
    run: ProjectRun | None = None
    anclas_caida: Anclas | None = None
    for intento in range(MAX_PROCESOS):
        activa = frontera if intento == 0 else None
        with crash_hook(activa), build_process(
            workspace=workspace,
            config_dir=config_dir,
            root=root,
            script=script,
            journal=journal,
            sandbox=sandbox,
            frontera=activa,
        ) as process, contextlib.suppress(SimulatedCrash):
            drive_process(process, request, workspace)
        run = FileProjectStore(root / "projects").load(project_run_id)
        if intento == 0 and frontera is not None:
            # El estado durable es la única prueba de que la caída ocurrió donde se declara.
            durable = frontera.durable(run)
            if durable != frontera.esperado:
                raise AssertionError(
                    f"la frontera {frontera.clave!r} no se alcanzó: el estado durable es "
                    f"{durable!r} y se esperaba {frontera.esperado!r}"
                )
            anclas_caida = anchors_of(run, artifacts)
        if run.is_terminal or run.is_paused:
            break
    else:
        raise AssertionError(
            f"el proyecto no cerró tras {MAX_PROCESOS} procesos: {describe(run)}"
        )
    finales = anchors_of(run, artifacts)
    if anclas_caida is None:
        anclas_caida = finales
    return measure(
        run,
        root=root,
        durable=durable,
        anclas_caida=anclas_caida,
        anclas_final=finales,
    )


@pytest.fixture(scope="module")
def sandbox(podman_gate: None) -> Iterator[ContainerSandboxBackend]:
    """Sandbox de prueba **verificado**: backend real de contenedor, con capacidades acreditadas.

    Se comparte entre todos los escenarios del módulo: es del entorno, no del motor, y construirlo
    uno por escenario solo añadiría minutos sin cambiar nada de lo que se mide.
    """
    backend = ContainerSandboxBackend(limits=SandboxLimits(timeout_seconds=180.0))
    backend.prepare()
    backend.verify_capabilities()
    yield backend
    backend.destroy()


@pytest.fixture(scope="module")
def referencia(
    tmp_path_factory: pytest.TempPathFactory,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> Ejecucion:
    """Ejecución **sin caídas**: la referencia contra la que se compara cada reinicio.

    Se ejecuta una sola vez por módulo y no se toca después: es de solo lectura, así que los diez
    escenarios de caída pueden compararse con ella sin que ninguno la altere.
    """
    root = tmp_path_factory.mktemp("referencia-replan")
    ejecucion = run_scenario(root=root, config_dir=config_dir, sandbox=sandbox, frontera=None)
    assert ejecucion.run.status is ProjectState.COMPLETED, describe(ejecucion.run)
    assert ejecucion.llamadas_replan == (0,), "la referencia invoca al replanificador una sola vez"
    return ejecucion


# ---------------------------------------------------------------------------
# El reinicio, frontera a frontera
# ---------------------------------------------------------------------------
def assert_mismo_intento(caida: Anclas, final: Anclas) -> None:
    """El proceso nuevo retoma **el mismo** intento: nadie inventa otra identidad.

    Se comparan las cuatro anclas del intento: disparador, huella de la propuesta, historia de
    generaciones y children ya identificados. Todas tienen que sobrevivir al reinicio; si el proceso
    nuevo creara un disparador, una propuesta, una generación o un child distintos, la comparación
    fallaría.
    """
    if caida.trigger_id is not None:
        assert final.trigger_id == caida.trigger_id, "el reinicio creó otro disparador"
    if caida.proposal_fingerprint:
        assert final.proposal_fingerprint == caida.proposal_fingerprint, (
            "el reinicio propuso otra cosa"
        )
    assert final.generation_ids[: len(caida.generation_ids)] == caida.generation_ids, (
        "el reinicio no conservó la historia de generaciones"
    )
    finales = dict(final.child_ids)
    for node_id, child_id in caida.child_ids:
        if child_id is not None:
            assert finales.get(node_id) == child_id, (
                f"el reinicio cambió el child del nodo {node_id!r}"
            )


def assert_sin_repeticion(escenario: Ejecucion, referencia: Ejecucion) -> None:
    """Ninguna llamada pagada se repite: los diarios durables son prefijos de la referencia.

    El diario del modelo y el del replanificador son **durables**: si un proceso nuevo repitiera una
    petición —una etapa ya ejecutada, una propuesta ya pagada—, la secuencia tendría una entrada de
    más o un índice repetido. Se comprueba que los índices sean ``0..n-1`` sin huecos y que la
    secuencia observada sea exactamente la de la ejecución sin caídas.
    """
    assert [indice for indice, _ in escenario.llamadas] == list(range(len(escenario.llamadas)))
    assert escenario.llamadas == referencia.llamadas[: len(escenario.llamadas)], (
        "el reinicio repitió o cambió alguna llamada al modelo"
    )
    assert list(escenario.llamadas_replan) == list(range(len(escenario.llamadas_replan)))
    assert escenario.llamadas_replan == referencia.llamadas_replan[
        : len(escenario.llamadas_replan)
    ], "el reinicio volvió a invocar al replanificador"


def assert_part_o(escenario: Ejecucion, frontera: Frontera) -> None:
    """El desenlace de la frontera de la invocación en vuelo: falla cerrado y no reintenta.

    Es la garantía que impide duplicar gasto: con la intención de invocación escrita y sin propuesta
    durable, el motor no puede saber si el proveedor llegó a responder, así que liquida el intento
    —liberando la reserva y contabilizando lo autorizado— y exige reconciliación explícita. El
    replanificador **no** se vuelve a invocar y el proyecto no adopta ninguna generación.
    """
    run = escenario.run
    assert run.status is frontera.estado_final, describe(run)
    assert run.failure_code is frontera.codigo_final, describe(run)
    assert run.usage.replans_reserved == 0, "el intento se liquidó antes de bloquear"
    assert run.usage.replans_attempted == 1
    assert run.usage.model_calls_reserved == 0 and run.usage.tokens_reserved == 0
    assert run.active_generation is not None
    assert run.active_generation.generation_index == 0, "no se adoptó ninguna generación"
    assert escenario.llamadas_replan == (), "la invocación en vuelo no se repite"
    assert len(escenario.llamadas) < len(GUION), (
        "el proyecto se detuvo antes de ejecutar los nodos de la generación 1"
    )
    authorization = run.active_replan_authorization
    assert authorization is not None
    node_a = run.node("A")
    node_b = run.node("B")
    assert node_a is not None and node_b is not None
    assert run.usage.model_calls == (
        node_a.model_calls + node_b.model_calls + authorization.authorized_model_calls
    ), "el gasto autorizado del intento quedó contabilizado y no se reinició nada"


@pytest.mark.parametrize("frontera", FRONTERAS, ids=[frontera.clave for frontera in FRONTERAS])
def test_el_proceso_nuevo_retoma_el_intento_de_replan_y_llega_al_mismo_desenlace(
    tmp_path: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
    referencia: Ejecucion,
    frontera: Frontera,
) -> None:
    """Con todos los objetos destruidos, el proceso nuevo retoma el intento y no gasta dos veces.

    Lo que se afirma, para cada frontera: el estado durable de la caída es el declarado; el intento
    es **el mismo** a los dos lados de la caída (mismo disparador, misma propuesta, mismos
    ``generation_id``, mismos ``child_workflow_id``); el desenlace es el de la referencia (mismos
    ``workflow_id`` de los children del grafo original, misma forma de grafo, mismo árbol final,
    mismo consumo); y no hay ni una llamada de modelo ni una invocación del replanificador de más.

    Nueve de las diez fronteras cierran el proyecto. La de la invocación en vuelo **no**: el motor
    no puede saber si el proveedor llegó a responder, así que falla cerrado y exige reconciliar el
    gasto antes de volver a llamar (PART O). Que esa frontera no cierre es el resultado correcto, y
    la prueba lo afirma explícitamente.
    """
    raiz = tmp_path / "escenario"
    escenario = run_scenario(root=raiz, config_dir=config_dir, sandbox=sandbox, frontera=frontera)

    assert escenario.durable == frontera.esperado, (
        f"la frontera {frontera.clave!r} no dejó el estado declarado: {frontera.descripcion}"
    )
    assert_sin_repeticion(escenario, referencia)
    assert_mismo_intento(escenario.anclas_caida, escenario.anclas_final)

    run = escenario.run
    if frontera.estado_final is not ProjectState.COMPLETED:
        assert_part_o(escenario, frontera)
        return

    assert escenario.anclas_final.child_ids[:3] == referencia.anclas_final.child_ids[:3], (
        "los child del grafo original son los mismos que en la ejecución sin caídas"
    )
    assert run.status is ProjectState.COMPLETED, describe(run)
    assert run.failure_code is None
    assert run.result is not None
    assert run.result.status is ProjectState.COMPLETED
    assert run.result.graph_generations_count == 2
    assert run.result.replans_attempted == 1
    assert run.result.replans_accepted == 1
    assert run.result.superseded_nodes_count == 1

    # El mismo intento, con las anclas completas del intento aceptado.
    anclas = escenario.anclas_final
    assert anclas.trigger_id is not None
    assert anclas.proposal_fingerprint
    assert len(anclas.generation_ids) == 2
    assert run.active_generation is not None
    assert run.active_generation.generation_id == anclas.generation_ids[1]
    assert run.graph_fingerprint == run.active_generation.graph_fingerprint
    assert run.result.final_graph_fingerprint == run.graph_fingerprint
    artifacts = FileArtifactStore(raiz / "artifacts")
    assert escenario.huella_final == graph_fingerprint(resolve_active_nodes(artifacts, run)), (
        "el grafo activo no es el que la generación declara"
    )

    # El mismo grafo (en contenido), el mismo árbol final, el mismo consumo.
    assert escenario.forma_grafo == referencia.forma_grafo
    assert escenario.estados == referencia.estados
    assert escenario.estados == (
        ProjectNodeStatus.COMPLETED.value,
        ProjectNodeStatus.SUPERSEDED.value,
        ProjectNodeStatus.COMPLETED.value,
        ProjectNodeStatus.COMPLETED.value,
        ProjectNodeStatus.COMPLETED.value,
    )
    assert len(escenario.child_ids) == 5
    assert len(set(escenario.child_ids)) == 5, "cada nodo tiene un child único"
    assert escenario.arbol == referencia.arbol, "el árbol final es el mismo contenido"
    assert escenario.asuntos == referencia.asuntos
    assert escenario.commits == referencia.commits
    assert escenario.contenido == referencia.contenido
    assert escenario.cifras == referencia.cifras, "el reinicio cambió el presupuesto acumulado"

    # La misma revisión final: la del resultado, la del linaje y la del ``HEAD`` real del árbol.
    workspace = raiz / "proyecto"
    node_c = run.node("C")
    assert node_c is not None
    assert run.result.final_revision == run.workspace.accepted_revision
    assert run.result.final_revision == node_c.accepted_revision_after
    assert escenario.revision_final == git(workspace, "rev-parse", "HEAD")

    # Sin reservas vivas, sin reinicio del presupuesto y sin ampliación de alcance.
    assert run.usage.model_calls_reserved == 0 and run.usage.tokens_reserved == 0
    assert run.usage.replans_reserved == 0
    assert run.usage.repairs == 3, "la reparación de A y las dos de B siguen contadas"
    assert run.contract_ref is not None
    contract = resolve_contract(artifacts, run.contract_ref)
    assert contract is not None
    active_nodes = resolve_active_nodes(artifacts, run)
    for node in run.nodes:
        if node.node_id not in ORIGINAL_NODES:
            assert set(node_scope(active_nodes, node.node_id)) <= set(contract.authorized_scope)

    # ``main`` intacto, y ningún commit del proyecto en él.
    assert escenario.main == git(workspace, "rev-parse", "main")
    assert escenario.commits_en_main == 1
    assert escenario.main != escenario.revision_final
