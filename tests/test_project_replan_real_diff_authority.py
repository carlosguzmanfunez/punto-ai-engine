"""El diff real manda: lo que el Developer declara no autoriza nada (ENGINE-6.3.R2, AUD-6.3R1-02).

La auditoría de cierre reprodujo un bypass: un nodo declaraba haber cambiado solo ``app.py``
mientras el árbol contenía además un ``pyproject.toml`` con un driver no autorizado, y el proyecto
cerraba ``COMPLETED``. La verificación post-ejecución miraba únicamente las rutas **declaradas**.

Desde ENGINE-6.3.R2 las rutas que se inspeccionan salen del **repositorio** (el linaje responde qué
cambió entre la revisión de arranque y la que el child dejó publicada) y lo declarado solo sirve
para diagnóstico. Una discrepancia no se ignora: se audita con las dos listas. Si el motor **no
puede** derivar el diff real, el nodo no se acepta —autoridad irresoluble, falla cerrado—.

No hay proveedor real, ni red, ni reloj, ni azar: el linaje, el contrato y los guiones son dobles
deterministas.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from project_support import ChildOutcome, FollowProjectLineage, planned
from punto.audit.logger import AuditLogger
from punto.project.workspace import ProjectRevisionMismatchError
from punto.schemas.audit import AuditEventType
from punto.schemas.project import ProjectFailureCode, ProjectNodeStatus, ProjectState
from test_project_kernel_matrix import Harness
from test_project_replan_kernel import FakeReplanner, event_types, replan_harness, replan_kernel
from test_project_replan_observed_resources import (
    PYPROJECT_CON_PSYCOPG,
    PYPROJECT_CON_PSYCOPG_Y_PYMONGO,
    arquitectura_postgres_con_psycopg,
)

ARCHIVO = "app.py"
MANIFIESTO = "pyproject.toml"


def escribir(h: Harness, nombre: str, contenido: str) -> None:
    """Escribe un fichero en el workspace real, creando los directorios que falten."""
    destino = h.workspace / nombre
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(contenido, encoding="utf-8")


def escenario(
    tmp_path: Path,
    *,
    declarado: tuple[str, ...],
    real: tuple[str, ...],
    manifiesto: str = "",
    on_changed: Callable[[str, str], None] | None = None,
) -> tuple[Harness, AuditLogger, object]:
    """Monta el proyecto con un diff real distinto del declarado y conduce el nodo A.

    ``real`` es lo que el repositorio demuestra que cambió; ``declarado`` lo que el resultado del
    Developer dice. El manifiesto se escribe en el workspace porque es de ahí de donde el parent lee
    el contenido al liquidar.
    """
    audit = AuditLogger()
    lineage = FollowProjectLineage(on_changed=on_changed)
    lineage.actual_paths = real
    h = replan_harness(
        tmp_path,
        outcomes={"A": ChildOutcome(files=declarado)},
        default=ChildOutcome(),
        architecture=arquitectura_postgres_con_psycopg(),
        tasks=(planned("A", allowed_files=(ARCHIVO, MANIFIESTO)),),
        lineage=lineage,
    )
    if manifiesto:
        escribir(h, MANIFIESTO, manifiesto)
    escribir(h, ARCHIVO, "def slug(valor):\n    return valor.strip()\n")
    kernel = replan_kernel(h, FakeReplanner(), audit=audit)
    return h, audit, kernel.run_all(h.request)


def test_el_diff_real_se_inspecciona_aunque_el_developer_no_lo_declare(tmp_path: Path) -> None:
    """Declarar ``app.py`` mientras el repositorio cambia ``pyproject.toml`` no oculta el
    manifiesto.

    Es la reproducción literal del hallazgo: el diff real introduce ``pymongo`` y el nodo **no** se
    acepta, aunque el resultado del Developer no lo haya mencionado. El evento de discrepancia queda
    registrado con las dos listas, además de la violación de arquitectura.
    """
    h, audit, run = escenario(
        tmp_path,
        declarado=(ARCHIVO,),
        real=(ARCHIVO, MANIFIESTO),
        manifiesto=PYPROJECT_CON_PSYCOPG_Y_PYMONGO,
    )
    node = run.node("A")

    assert node is not None
    assert h.lineage.changed_reads, "el motor pidió el diff real al linaje"
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
    assert run.status is ProjectState.BLOCKED
    assert run.workspace.accepted_revision == run.workspace.initial_revision, (
        "la revisión aceptada no avanza: el trabajo del nodo no se acepta"
    )
    assert node.handoff_ref is None, "un nodo rechazado no publica handoff"
    types = event_types(audit)
    assert AuditEventType.PROJECT_NODE_UNDECLARED_CHANGE in types, (
        "la discrepancia entre lo declarado y el diff real no puede pasar en silencio"
    )
    assert AuditEventType.PROJECT_NODE_ARCHITECTURE_VIOLATION in types


def test_declarado_igual_al_real_es_el_comportamiento_normal(tmp_path: Path) -> None:
    """Cuando el diff real y lo declarado coinciden y todo está autorizado, el nodo se acepta."""
    _, _, run = escenario(
        tmp_path,
        declarado=(ARCHIVO, MANIFIESTO),
        real=(ARCHIVO, MANIFIESTO),
        manifiesto=PYPROJECT_CON_PSYCOPG,
    )
    node = run.node("A")

    assert node is not None
    assert node.status is ProjectNodeStatus.COMPLETED
    assert run.workspace.accepted_revision != run.workspace.initial_revision


def test_lo_no_declarado_pero_contenido_no_es_violacion_y_queda_auditado(tmp_path: Path) -> None:
    """Un fichero no declarado cuyo efecto **cabe** en la autoridad no bloquea, pero se audita.

    La discrepancia no es delito por sí misma: lo que decide es la inspección del diff real. Lo que
    no se permite es que ocurra sin dejar rastro.
    """
    _, audit, run = escenario(
        tmp_path,
        declarado=(ARCHIVO,),
        real=(ARCHIVO, MANIFIESTO),
        manifiesto=PYPROJECT_CON_PSYCOPG,
    )
    node = run.node("A")

    assert node is not None
    assert node.status is ProjectNodeStatus.COMPLETED, (
        f"el diff real cabe en el envelope autorizado: {node.failure_code}"
    )
    assert run.workspace.accepted_revision != run.workspace.initial_revision
    assert AuditEventType.PROJECT_NODE_UNDECLARED_CHANGE in event_types(audit)


def test_sin_poder_derivar_el_diff_real_el_nodo_no_se_acepta(tmp_path: Path) -> None:
    """Un linaje que no puede responder al diff real no autoriza nada: falla cerrado.

    «No lo sé» no es «no cambió nada». El nodo se rechaza con la misma frontera de arquitectura y
    con la razón sin resolver en el detalle.
    """

    def falla(_base: str, _head: str) -> None:
        raise ProjectRevisionMismatchError("git no responde en este workspace")

    _, _, run = escenario(
        tmp_path,
        declarado=(ARCHIVO,),
        real=(ARCHIVO,),
        on_changed=falla,
    )
    node = run.node("A")

    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
    assert "diff real" in node.failure_detail
    assert run.workspace.accepted_revision == run.workspace.initial_revision
