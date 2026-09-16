"""Recursos **observados** en el diff: la mitad post-hoc de la contención (ENGINE-6.3.R1, PARTE D).

La contención pre-hoc mide lo que una propuesta **pide**; esta suite mide lo que la implementación
**introdujo de verdad**. Al liquidar el child, el parent lee los manifiestos y la configuración de
infraestructura que el child declaró haber cambiado —desde ``run.request.workspace_path``— y los
compara con el envelope autorizado (los recursos del nodo **más** los del contrato). Si aparece un
recurso fuera del envelope, el nodo **no** se acepta: no hay handoff, la revisión aceptada no
avanza, el gasto se contabiliza una sola vez y el proyecto se bloquea con
``PROJECT_NODE_ARCHITECTURE_VIOLATION`` sin pasar por la replanificación. Si un fichero relevante
cambió y no se puede interpretar —manifiesto sin parser soportado o ilegible— el veredicto queda
``UNRESOLVED``, que **también** rechaza: nunca se degrada a «no introdujo nada».

El grupo H fija la frontera como **regresión de F621**: una violación de arquitectura no la borra
una reanudación genérica, no dispara una replanificación autónoma y no reabre las fronteras
hermanas (presupuesto, alcance, revisión, evidencia, gasto desconocido).

El doble de child de ``project_support`` no escribe ficheros: solo declara qué rutas cambió. Por eso
el contenido de cada manifiesto lo escribe la prueba en ``h.workspace`` **antes** de conducir el
kernel, que es de donde el parent lo lee en el momento de la liquidación.

Nota de interfaz, medida en esta fase: el Planner **no** puede declarar ``uses_resources`` ni
``pure_technical`` en el plan. Esos campos solo existen en ``ReplanNodeSpec``, es decir, en una
propuesta de replanificación, y B1 no llega a proponer ninguna (el rechazo es del contrato, no del
replanner). El escenario se monta, por tanto, contra el **único** origen de autorización que existe
en el plan —el envelope derivado del ``ArchitecturePlan``—, que es exactamente la tesis de la fase:
ninguna declaración autoriza nada y el diff manda.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_support import ChildOutcome, planned
from punto.audit.logger import AuditLogger
from punto.project.kernel import (
    RECONCILIATION_REQUIRED_CODES,
    ProjectReconciliationRequiredError,
)
from punto.project.replan import ReplanCategory, classify_failure, classify_node
from punto.project.resources import (
    is_resource_relevant,
    parser_for,
    project_resource_envelope,
    resources_from_diff,
)
from punto.schemas.audit import AuditEventType
from punto.schemas.planning import ArchitecturePlan, DataStore, TechnologyChoice
from punto.schemas.project import (
    ProjectFailureCode,
    ProjectNodeStatus,
    ProjectRun,
    ProjectState,
)
from punto.schemas.replan import ReplanEligibility
from test_project_kernel_matrix import Harness
from test_project_replan_kernel import (
    FakeReplanner,
    event_types,
    replan_harness,
    replan_kernel,
)

#: ``pyproject.toml`` que declara el driver autorizado **y** el que nadie autorizó.
PYPROJECT_CON_PSYCOPG_Y_PYMONGO = """
[project]
name = "servicio"
version = "0.1.0"
dependencies = ["psycopg[binary]==3.1", "pymongo==4.6"]
"""

#: ``pyproject.toml`` que solo reutiliza lo que la arquitectura ya autorizó.
PYPROJECT_CON_FASTAPI = """
[project]
name = "servicio"
version = "0.1.0"
dependencies = ["fastapi==0.110"]
"""

#: ``pyproject.toml`` con el driver que la arquitectura autorizó como elección tecnológica.
PYPROJECT_CON_PSYCOPG = """
[project]
name = "servicio"
version = "0.1.0"
dependencies = ["psycopg==3.1"]
"""

#: ``package.json`` que introduce un SDK que la arquitectura no declaró.
PACKAGE_JSON_CON_ANTHROPIC = '{"dependencies": {"@anthropic-ai/sdk": "1.0.0"}}'

#: ``Pipfile``: declara dependencias y el motor **no tiene parser** para él.
PIPFILE_DECLARATIVO = """
[[source]]
url = "https://pypi.org/simple"
verify_ssl = true
name = "pypi"

[packages]
requests = "*"
"""

#: ``go.mod`` con un módulo requerido en la forma canónica de bloque.
GO_MOD_CON_GIN = """
module ejemplo

go 1.22

require (
\tgithub.com/gin-gonic/gin v1.9.0
)
"""

#: ``Cargo.toml`` con una dependencia declarada.
CARGO_TOML_CON_SERDE = """
[package]
name = "ejemplo"
version = "0.1.0"

[dependencies]
serde = "1.0"
"""


def arquitectura_postgres_con_psycopg() -> ArchitecturePlan:
    """Arquitectura durable que autoriza el motor relacional y su driver.

    Es el envelope contra el que se mide el diff: ``datastore:postgres`` (motor),
    ``datastore:principal`` (nombre), ``technology:driver:psycopg`` y ``package:psycopg``. Mongo no
    está en esa lista y ninguna declaración del Planner puede añadirlo.
    """
    return ArchitecturePlan(
        architecture_style="monolito modular",
        data_stores=(DataStore(id="db", name="principal", engine="postgres"),),
        technology_choices=(TechnologyChoice(topic="driver", choice="psycopg"),),
    )


def arquitectura_sin_integracion_de_ia() -> ArchitecturePlan:
    """Arquitectura sin integración de IA: todo SDK de modelo es una expansión."""
    return ArchitecturePlan(
        architecture_style="monolito modular",
        technology_choices=(TechnologyChoice(topic="lenguaje", choice="python"),),
    )


def escribir(h: Harness, nombre: str, contenido: str) -> None:
    """Escribe el manifiesto en el workspace real, que es de donde el parent lo lee al liquidar."""
    (h.workspace / nombre).write_text(contenido, encoding="utf-8")


def escenario_del_diff_con_mongo(
    tmp_path: Path,
) -> tuple[Harness, FakeReplanner, AuditLogger, ProjectRun]:
    """Monta y conduce el caso B1: el contrato autoriza postgres y el diff introduce Mongo.

    Devuelve las piezas con las que cada prueba explica el veredicto: el montaje —para releer el
    estado durable y para conocer el alcance autorizado del nodo—, el replanner doble —para afirmar
    que **no** se le llamó—, la auditoría y el run ya bloqueado.
    """
    audit = AuditLogger()
    replanner = FakeReplanner()
    h = replan_harness(
        tmp_path,
        outcomes={"A": ChildOutcome(files=("app.py", "pyproject.toml"))},
        default=ChildOutcome(),
        architecture=arquitectura_postgres_con_psycopg(),
        tasks=(planned("A", allowed_files=("app.py", "pyproject.toml")),),
    )
    escribir(h, "pyproject.toml", PYPROJECT_CON_PSYCOPG_Y_PYMONGO)
    kernel = replan_kernel(h, replanner, audit=audit)
    return h, replanner, audit, kernel.run_all(h.request)


def test_b1_el_planner_declara_postgres_y_el_diff_añade_mongo(tmp_path: Path) -> None:
    """El nodo que declara postgres y añade un driver de Mongo **no** se acepta: manda el diff.

    El caso es el interesante justo porque la declaración cae dentro del envelope autorizado
    (``datastore:postgres``): ninguna declaración del Planner salva a la implementación. Lo que
    decide es lo que el nodo **introdujo**, y el ``pyproject.toml`` del workspace añade ``pymongo``,
    que el contrato no autoriza. El parent rechaza el nodo con
    ``PROJECT_NODE_ARCHITECTURE_VIOLATION``: sin handoff, sin avanzar la revisión aceptada, con el
    gasto contabilizado una sola vez y sin abrir ninguna replanificación —otra estrategia no
    convierte en autorizado lo que ya se introdujo—.
    """
    h, replanner, audit, run = escenario_del_diff_con_mongo(tmp_path)
    node = run.node("A")
    assert node is not None

    envelope = project_resource_envelope(arquitectura_postgres_con_psycopg())
    assert "datastore:postgres" in envelope.tokens, "la arquitectura sí autoriza postgres"
    assert "package:pymongo" not in envelope.tokens, "y no autoriza el driver de Mongo"

    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
    assert node.handoff_ref is None, "un nodo rechazado no publica handoff"
    assert node.accepted_revision_after == node.accepted_revision_before
    assert run.workspace.accepted_revision == run.workspace.initial_revision, (
        "la revisión aceptada no avanza: el trabajo del nodo no se acepta"
    )
    assert run.workspace.last_completed_node_id == ""
    assert run.status is ProjectState.BLOCKED
    # El código del proyecto es el del veredicto del parent, no un `PROJECT_CHILD_BLOCKED`: el child
    # cerró `COMPLETED` y lo que falló fue la postcondición que el parent comprobó sobre el diff.
    # Es el mismo comportamiento que la violación de alcance (que bloquea con su propio código).
    assert run.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
    assert node.model_calls == 1, "el gasto del nodo se contabiliza una sola vez"
    assert run.usage.model_calls == 1, "y no hay ninguna otra llamada contabilizada"
    assert replanner.calls == [], "una violación de arquitectura no se replanifica"
    assert run.usage.replans_attempted == 0
    types = event_types(audit)
    assert AuditEventType.PROJECT_NODE_ARCHITECTURE_VIOLATION in types
    assert "package:pymongo" in node.failure_detail
    violation = audit.by_type(AuditEventType.PROJECT_NODE_ARCHITECTURE_VIOLATION)
    assert len(violation) == 1
    assert "package:pymongo" in violation[0].metadata_dict["expanded_resources"]
    assert h.reload().failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION


def test_b2_el_diff_añade_un_proveedor_no_declarado(tmp_path: Path) -> None:
    """Un SDK de modelo que la arquitectura no declaró es una expansión, no una dependencia más.

    La arquitectura del caso no declara ninguna integración de IA, así que
    ``@anthropic-ai/sdk`` en el ``package.json`` del workspace no tiene nada que lo ampare: el
    parent lo mide como recurso observado fuera del envelope y rechaza el nodo por el mismo código
    que el caso B1.
    """
    replanner = FakeReplanner()
    h = replan_harness(
        tmp_path,
        outcomes={"A": ChildOutcome(files=("app.py", "package.json"))},
        architecture=arquitectura_sin_integracion_de_ia(),
        tasks=(planned("A", allowed_files=("app.py", "package.json")),),
    )
    escribir(h, "package.json", PACKAGE_JSON_CON_ANTHROPIC)
    kernel = replan_kernel(h, replanner)

    run = kernel.run_all(h.request)

    node = run.node("A")
    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
    assert node.handoff_ref is None
    assert run.status is ProjectState.BLOCKED
    assert "package:@anthropic-ai/sdk" in node.failure_detail
    assert replanner.calls == []
    assert run.usage.replans_attempted == 0


def test_b3_manifiesto_relevante_sin_parser_es_unresolved(tmp_path: Path) -> None:
    """Un manifiesto relevante sin parser soportado **no** se lee como «no introdujo nada».

    ``Pipfile`` declara dependencias y el motor no tiene parser para él. El nodo lo permite en su
    alcance, así que no hay violación de alcance que lo salve: lo que hay es incertidumbre, y
    ``UNRESOLVED`` es un valor de primera clase que también rechaza. El detalle tiene que decir que
    la evidencia no se pudo resolver y citar el fichero, para que una persona sepa qué mirar.
    """
    replanner = FakeReplanner()
    h = replan_harness(
        tmp_path,
        outcomes={"A": ChildOutcome(files=("app.py", "Pipfile"))},
        tasks=(planned("A", allowed_files=("app.py", "Pipfile")),),
    )
    escribir(h, "Pipfile", PIPFILE_DECLARATIVO)
    kernel = replan_kernel(h, replanner)

    run = kernel.run_all(h.request)

    node = run.node("A")
    assert node is not None
    assert is_resource_relevant("Pipfile") is True, "el fichero sí declara recursos"
    assert parser_for("Pipfile") is None, "y el motor no lo sabe interpretar"
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
    assert node.failure_code is not ProjectFailureCode.PROJECT_NODE_SCOPE_VIOLATION
    assert "Pipfile" in node.failure_detail
    assert "no se pudo resolver" in node.failure_detail
    assert replanner.calls == []
    assert run.status is ProjectState.BLOCKED


def test_b4_un_fichero_irrelevante_no_declara_recursos(tmp_path: Path) -> None:
    """Cambiar solo código no declara ningún recurso: el nodo se acepta con normalidad.

    ``app.py`` no es un manifiesto ni configuración de infraestructura, así que el diff no aporta
    recursos observados ni incertidumbre. La contención post-hoc no estorba al camino feliz: el nodo
    se acepta, publica su handoff, la revisión aceptada avanza y el proyecto cierra ``COMPLETED``.
    """
    replanner = FakeReplanner()
    h = replan_harness(tmp_path, outcomes={"A": ChildOutcome(files=("app.py",))})
    kernel = replan_kernel(h, replanner)

    run = kernel.run_all(h.request)

    node = run.node("A")
    assert node is not None
    assert run.status is ProjectState.COMPLETED, run.failure_detail
    assert node.status is ProjectNodeStatus.COMPLETED
    assert node.failure_code is None
    assert node.handoff_ref is not None, "el nodo aceptado publica su handoff"
    assert run.workspace.accepted_revision != run.workspace.initial_revision
    assert run.workspace.last_completed_node_id == "A"
    assert run.result is not None
    assert replanner.calls == []


def test_c1_un_manifiesto_que_reutiliza_lo_autorizado_no_rechaza(tmp_path: Path) -> None:
    """Una dependencia que la arquitectura ya eligió no es una expansión: el nodo se acepta.

    Es la cara positiva de la contención post-hoc: ``fastapi`` está en las elecciones tecnológicas
    del plan, así que el ``package:fastapi`` que el diff introduce ya estaba autorizado. Sin esta
    mitad, la frontera sería un rechazo indiscriminado en vez de una comprobación de contención.
    """
    replanner = FakeReplanner()
    architecture = ArchitecturePlan(
        architecture_style="monolito modular",
        technology_choices=(TechnologyChoice(topic="web", choice="fastapi"),),
    )
    h = replan_harness(
        tmp_path,
        outcomes={"A": ChildOutcome(files=("app.py", "pyproject.toml"))},
        architecture=architecture,
        tasks=(planned("A", allowed_files=("app.py", "pyproject.toml")),),
    )
    escribir(h, "pyproject.toml", PYPROJECT_CON_FASTAPI)
    kernel = replan_kernel(h, replanner)

    run = kernel.run_all(h.request)

    node = run.node("A")
    assert node is not None
    assert run.status is ProjectState.COMPLETED, run.failure_detail
    assert node.failure_code is None
    assert node.handoff_ref is not None
    assert run.workspace.accepted_revision != run.workspace.initial_revision, (
        "la revisión aceptada avanza porque el recurso estaba autorizado"
    )
    assert replanner.calls == []


def test_c2_un_manifiesto_nuevo_dentro_del_envelope_no_rechaza(tmp_path: Path) -> None:
    """Un driver nuevo pero autorizado por la arquitectura tampoco rechaza.

    El caso cambia el manifiesto de verdad —el ``pyproject.toml`` introduce ``psycopg``— y aun así
    se acepta, porque la elección tecnológica ``driver: psycopg`` ya forma parte del envelope: la
    contención se mide contra lo autorizado, no contra lo que ya existía en el árbol.
    """
    replanner = FakeReplanner()
    h = replan_harness(
        tmp_path,
        outcomes={"A": ChildOutcome(files=("app.py", "pyproject.toml"))},
        architecture=arquitectura_postgres_con_psycopg(),
        tasks=(planned("A", allowed_files=("app.py", "pyproject.toml")),),
    )
    escribir(h, "pyproject.toml", PYPROJECT_CON_PSYCOPG)
    kernel = replan_kernel(h, replanner)

    run = kernel.run_all(h.request)

    node = run.node("A")
    assert node is not None
    assert run.status is ProjectState.COMPLETED, run.failure_detail
    assert node.failure_code is None
    assert node.handoff_ref is not None
    assert run.workspace.accepted_revision != run.workspace.initial_revision
    assert replanner.calls == []


def test_c3_el_parser_lee_lo_que_el_motor_soporta() -> None:
    """El vocabulario declarado en ``src/punto/project/resources.py`` es el que el motor lee.

    La lista de parsers es **explícita**, así que la prueba la mide: ``package.json``, ``go.mod``,
    ``Cargo.toml``, ``Dockerfile`` y ``requirements.txt`` producen tokens observables, y ninguno
    queda sin resolver. Si un parser se rompiera, el veredicto post-hoc pasaría a ``UNRESOLVED``
    silenciosamente en todos los proyectos que usen ese manifiesto.
    """
    contenidos = {
        "package.json": '{"dependencies": {"left-pad": "1.0.0"}}',
        "go.mod": GO_MOD_CON_GIN,
        "Cargo.toml": CARGO_TOML_CON_SERDE,
        "Dockerfile": "FROM python:3.12-slim\n",
        "requirements.txt": "fastapi==0.110\n",
    }

    observed, unresolved = resources_from_diff(tuple(contenidos), contenidos.get)

    assert unresolved == (), "todos los manifiestos soportados se resuelven"
    assert "package:left-pad" in observed.tokens
    assert "package:github.com/gin-gonic/gin" in observed.tokens
    assert "package:serde" in observed.tokens
    assert "technology:image:python:3.12-slim" in observed.tokens
    assert "package:fastapi" in observed.tokens
    assert all(parser_for(path) is not None for path in contenidos)
    assert all(is_resource_relevant(path) is True for path in contenidos)


def test_c4_la_evidencia_ilegible_no_se_interpreta_como_vacia() -> None:
    """Un fichero que cambió y no se puede leer deja el veredicto sin resolver, no vacío.

    Es la frontera que impide que «no pude leerlo» se degrade a «no introdujo nada»: con un lector
    que devuelve ``None`` el conjunto observado queda vacío, y por eso la prueba exige que la razón
    sin resolver venga declarada y cite la ruta. Sin ella, un manifiesto ilegible sería una vía de
    escape del envelope.
    """
    observed, unresolved = resources_from_diff(["pyproject.toml"], lambda _: None)

    assert observed.is_empty is True
    assert observed.tokens == ()
    assert unresolved, "la evidencia ilegible tiene que declararse sin resolver"
    assert "pyproject.toml" in unresolved[0]
    assert "no se pudo leer" in unresolved[0]


def test_h1_la_violacion_no_la_borra_una_reanudacion_generica(tmp_path: Path) -> None:
    """Una violación de arquitectura es una frontera: una reanudación genérica no la cierra.

    Es la regresión de F621 aplicada al hermano nuevo. Tras el rechazo de B1, reanudar el proyecto
    —en un kernel nuevo, sobre el estado durable— lanza ``ProjectReconciliationRequiredError`` en
    vez de borrar el código y volver a conducir: el nodo rechazado sigue rechazado y el motivo sigue
    siendo cierto hasta que una reconciliación explícita lo cierre.
    """
    h, _replanner, _audit, run = escenario_del_diff_con_mongo(tmp_path)
    kernel = replan_kernel(h, FakeReplanner())

    with pytest.raises(ProjectReconciliationRequiredError) as error:
        kernel.resume(run.project_run_id)

    assert "PROJECT_NODE_ARCHITECTURE_VIOLATION" in str(error.value)
    durable = h.reload()
    assert durable.status is ProjectState.BLOCKED
    assert durable.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
    node = durable.node("A")
    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
    assert ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION in RECONCILIATION_REQUIRED_CODES


def test_h2_la_violacion_no_dispara_replanificacion_autonoma(tmp_path: Path) -> None:
    """Con replan autorizado e inyectado, la violación **no** abre ninguna replanificación.

    El caso lleva ``max_replans`` = 1 y un replanner doble a mano: aun así no recibe ninguna llamada
    y el contador de intentos queda a cero. La clasificación es del motor y dice lo que hay que
    decir: ``ARCHITECTURE_VIOLATION`` con elegibilidad de parada, porque otra estrategia no
    convierte en autorizado lo que ya se introdujo.
    """
    h, replanner, _audit, run = escenario_del_diff_con_mongo(tmp_path)
    node = run.node("A")
    assert node is not None

    classification = classify_node(run, node)

    assert run.budget.max_replans == 1, "el presupuesto autorizaba una replanificación"
    assert replanner.calls == []
    assert run.usage.replans_attempted == 0
    assert run.usage.replans_reserved == 0
    assert classification.category is ReplanCategory.ARCHITECTURE_VIOLATION
    assert classification.eligibility is ReplanEligibility.NON_REPLANNABLE
    assert classification.allows_autonomous is False
    assert h.reload().failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION


def test_h3_las_fronteras_hermanas_siguen_cerradas() -> None:
    """Las postcondiciones del parent siguen siendo parada, no materia de otro plan.

    Es la otra mitad de la regresión: cerrar la violación de arquitectura no puede haber aflojado a
    sus hermanas. Brecha de presupuesto, violación de alcance, revisión que el árbol no demuestra y
    evidencia incompleta conservan su categoría, su elegibilidad de parada y la ausencia de
    autonomía.
    """
    casos = (
        (
            ProjectFailureCode.PROJECT_BUDGET_BREACH,
            ReplanCategory.BUDGET_BREACH,
            ReplanEligibility.BUDGET_STOP,
        ),
        (
            ProjectFailureCode.PROJECT_NODE_SCOPE_VIOLATION,
            ReplanCategory.SCOPE_VIOLATION,
            ReplanEligibility.NON_REPLANNABLE,
        ),
        (
            ProjectFailureCode.PROJECT_WORKSPACE_REVISION_MISMATCH,
            ReplanCategory.REVISION_MISMATCH,
            ReplanEligibility.NON_REPLANNABLE,
        ),
        (
            ProjectFailureCode.PROJECT_DEPENDENCY_EVIDENCE_INCOMPLETE,
            ReplanCategory.EVIDENCE_INCOMPLETE,
            ReplanEligibility.EVIDENCE_BLOCKED,
        ),
        (
            ProjectFailureCode.PROJECT_EFFECT_UNRECONCILED,
            ReplanCategory.EVIDENCE_INCOMPLETE,
            ReplanEligibility.EVIDENCE_BLOCKED,
        ),
    )

    for code, category, eligibility in casos:
        classification = classify_failure(node_failure_code=code)
        assert classification.category is category, code.value
        assert classification.eligibility is eligibility, code.value
        assert classification.eligibility.is_stop is True, code.value
        assert classification.allows_autonomous is False, code.value


def test_h4_el_gasto_desconocido_sigue_exigiendo_reconciliacion() -> None:
    """El gasto sin reconciliar no es autónomo: su código exige una decisión explícita.

    Una invocación que salió y no dejó propuesta durable deja un gasto desconocido; reintentarlo
    sería gastar a ciegas. La elegibilidad de parada y la pertenencia del código a
    ``RECONCILIATION_REQUIRED_CODES`` son la frontera que lo impide, y la violación de arquitectura
    viaja en la misma lista.
    """
    classification = classify_failure(
        node_failure_code=ProjectFailureCode.PROJECT_REPLAN_SPEND_RECONCILIATION_REQUIRED
    )

    assert classification.eligibility is ReplanEligibility.EVIDENCE_BLOCKED
    assert classification.eligibility.is_stop is True
    assert classification.allows_autonomous is False
    assert (
        ProjectFailureCode.PROJECT_REPLAN_SPEND_RECONCILIATION_REQUIRED
        in RECONCILIATION_REQUIRED_CODES
    )
    assert (
        ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION in RECONCILIATION_REQUIRED_CODES
    )
