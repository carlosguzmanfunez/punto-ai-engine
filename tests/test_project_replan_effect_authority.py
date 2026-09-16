"""La autoridad es positiva y el efecto real tiene que caber dentro (ENGINE-6.3.R3).

La auditoría adversarial de R2 reprodujo un bypass end-to-end: la propuesta «Relocate the workload
to another execution environment» no disparaba ninguna señal semántica, T5 no tenía nada que negar
—la propuesta no declaraba recursos— y la implementación cambiaba el almacén (PostgreSQL a SQLite)
**dentro de un fichero autorizado**; el diff real detectaba `app.py`, pero conocer la ruta no dice
nada del efecto, y el proyecto cerraba `COMPLETED`.

R3 cierra las dos mitades:

- la **ruta no es la autoridad**: el motor lee el **contenido añadido** del diff real y exige poder
  demostrar que el efecto cabe en el envelope autorizado; lo que no puede demostrar es
  ``UNRESOLVED`` y el nodo no se acepta (``PROJECT_NODE_ARCHITECTURE_VIOLATION``);
- la **autoridad es explícita**: el veredicto de contención viaja con la superficie y las
  dimensiones autorizadas (``AuthorityEnvelope``) y sin autoridad enunciable no hay autonomía.

La allowlist de módulos inertes es **cerrada**: una tecnología futura —``UnknownTechnology2027``—
cae exactamente donde tiene que caer sin que nadie la conozca. El control positivo sigue avanzando:
la frontera no es un muro.

No hay proveedor real, ni red, ni reloj, ni azar.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_support import ChildOutcome, planned
from punto.audit.logger import AuditLogger
from punto.project.containment import ArchitectureCompatibility
from punto.schemas.audit import AuditEventType
from punto.schemas.project import ProjectFailureCode, ProjectNodeStatus, ProjectState
from test_project_kernel_matrix import Harness
from test_project_replan_escalation_and_binding import (
    ReplanTextReplanner,
    contenido,
    montaje,
    politica,
    propuesta,
)
from test_project_replan_kernel import FakeReplanner, event_types, replan_harness, replan_kernel
from test_project_replan_observed_resources import (
    PYPROJECT_CON_PSYCOPG,
    arquitectura_postgres_con_psycopg,
)

ARCHIVO = "app.py"
CODIGO_LIMPIO = "MENSAJE = 'validacion corregida'\n\n\ndef slug(valor):\n    return valor.strip()\n"

#: Efectos que **no** se pueden demostrar contenidos: tecnología ajena, biblioteca estándar que
#: introduce almacenamiento o concurrencia, import dinámico y tecnologías que no existen.
ATAQUES: tuple[tuple[str, str], ...] = (
    ("postgres->sqlite", "import sqlite3\n\nCONN = sqlite3.connect('app.db')\n"),
    ("import-pymongo", "import pymongo\n"),
    ("import-dinamico", "DRIVER = __import__('pymongo')\n"),
    ("importlib", "import importlib\n\nM = importlib.import_module('pymongo')\n"),
    ("boto3", "import boto3\n\nSQS = boto3.client('sqs')\n"),
    ("cola-en-proceso", "import queue\n\nQ = queue.Queue()\n"),
    ("desconocida-2027", "import UnknownTechnology2027\n"),
    ("desconocida-db", "from UnknownDatabase import Engine\n"),
    ("desconocida-cola", "import UnknownQueueSystem\n"),
    ("uri-ajena", "ENGINE = 'mongodb://cluster0/db'\n"),
    ("mismo-fichero-mixto", CODIGO_LIMPIO + "import sqlite3\n"),
)

#: Cambios corrientes que no tocan arquitectura: deben seguir aceptándose.
CORRIENTES: tuple[tuple[str, str], ...] = (
    ("codigo-limpio", CODIGO_LIMPIO),
    ("stdlib-inerte", "import json\n\nDATA = json.dumps({'a': 1})\n"),
    ("paquete-autorizado", "import psycopg\n\nCURSOR = psycopg.connect\n"),
    ("modulo-local", "from .util import normalizar\n"),
)

TEXTO_BYPASS = "Relocate the workload to another execution environment"
ATAQUES_IDS = [caso[0] for caso in ATAQUES]
CORRIENTES_IDS = [caso[0] for caso in CORRIENTES]


def _escribir(h: Harness, nombre: str, contenido_fichero: str) -> None:
    """Escribe el fichero en el workspace real del proyecto."""
    destino = h.workspace / nombre
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(contenido_fichero, encoding="utf-8")


def _conducir(
    tmp_path: Path,
    contenido_fichero: str,
    *,
    lineas: tuple[str, ...] | None = None,
) -> tuple[AuditLogger, object]:
    """Monta el proyecto, escribe el cambio del nodo A y lo conduce hasta su liquidación.

    El linaje declara el diff real —rutas y contenido añadido— igual que haría Git.
    """
    audit = AuditLogger()
    h = replan_harness(
        tmp_path,
        outcomes={"A": ChildOutcome(files=(ARCHIVO,))},
        default=ChildOutcome(),
        architecture=arquitectura_postgres_con_psycopg(),
        tasks=(planned("A", allowed_files=(ARCHIVO,)),),
    )
    h.lineage.actual_paths = (ARCHIVO,)
    h.lineage.actual_added_lines = (
        tuple(contenido_fichero.splitlines()) if lineas is None else lineas
    )
    _escribir(h, ARCHIVO, contenido_fichero)
    return audit, replan_kernel(h, FakeReplanner(), audit=audit).run_all(h.request)


@pytest.mark.parametrize(("etiqueta", "contenido_fichero"), ATAQUES, ids=ATAQUES_IDS)
def test_el_efecto_real_tiene_que_caber_en_la_autoridad(
    tmp_path: Path, etiqueta: str, contenido_fichero: str
) -> None:
    """Un efecto arquitectónico dentro de un fichero autorizado no se acepta sin prueba.

    Reproducción permanente de AUD-R2-02: el diff real trae la ruta **y** su contenido, y el motor
    no puede demostrar que el efecto quede dentro del envelope, así que el nodo no se acepta.
    """
    _, run = _conducir(tmp_path, contenido_fichero)
    node = run.node("A")

    assert node is not None, etiqueta
    assert node.status is ProjectNodeStatus.BLOCKED, (etiqueta, node.status, node.failure_detail)
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION, (
        etiqueta,
        node.failure_code,
        node.failure_detail,
    )
    assert run.workspace.accepted_revision == run.workspace.initial_revision, (
        "la revisión aceptada no avanza con un efecto no demostrado"
    )
    assert node.handoff_ref is None


@pytest.mark.parametrize(("etiqueta", "contenido_fichero"), CORRIENTES, ids=CORRIENTES_IDS)
def test_el_trabajo_corriente_sigue_avanzando(
    tmp_path: Path, etiqueta: str, contenido_fichero: str
) -> None:
    """Lo que no toca arquitectura —código, stdlib inerte, paquete autorizado, módulo propio— pasa.
    """
    audit, run = _conducir(tmp_path, contenido_fichero)
    node = run.node("A")
    tipos = event_types(audit)

    assert node is not None, etiqueta
    assert node.status is ProjectNodeStatus.COMPLETED, (
        etiqueta,
        node.failure_code,
        node.failure_detail,
    )
    assert run.workspace.accepted_revision != run.workspace.initial_revision
    assert AuditEventType.PROJECT_NODE_ARCHITECTURE_VIOLATION not in tipos
    assert AuditEventType.PROJECT_NODE_UNDECLARED_CHANGE not in tipos


def test_la_ruta_autorizada_no_autoriza_el_efecto(tmp_path: Path) -> None:
    """Declarar ``app.py`` —ruta autorizada— no autoriza a cambiar el almacén dentro de él.

    Es la distinción central de R3: ``authorized file`` no es ``authorized effect``.
    """
    audit, run = _conducir(tmp_path, "import sqlite3\n\nCONN = sqlite3.connect('app.db')\n")
    node = run.node("A")
    violaciones = [
        event
        for event in audit.events()
        if event.event_type is AuditEventType.PROJECT_NODE_ARCHITECTURE_VIOLATION
    ]

    assert node is not None
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
    assert run.status is ProjectState.BLOCKED
    assert violaciones, "la violación queda auditada"
    assert "sqlite3" in str(violaciones[0].metadata)


def test_sin_contenido_del_diff_no_hay_prueba_del_efecto(tmp_path: Path) -> None:
    """El repositorio dice que cambiaron rutas y no hay contenido que juzgar: falla cerrado."""
    _, run = _conducir(tmp_path, CODIGO_LIMPIO, lineas=())
    node = run.node("A")

    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
    assert "contenido" in node.failure_detail


def test_el_bypass_exacto_de_r2_ya_no_cierra_el_proyecto(tmp_path: Path) -> None:
    """La cadena completa de la auditoría de R2, en una sola ejecución del kernel real.

    Texto arquitectónico no reconocido + cambio de almacén dentro del fichero autorizado. La
    replanificación puede adoptarse (la autoridad pre-ejecución es provisional), pero el **efecto**
    no se acepta: el nodo se bloquea con violación de arquitectura y el proyecto no cierra
    ``COMPLETED``.
    """
    carpeta = tmp_path / "cadena"
    carpeta.mkdir()
    replanner = ReplanTextReplanner(objective=TEXTO_BYPASS)
    escenario = montaje(
        carpeta, replanner=replanner, default=ChildOutcome(files=(ARCHIVO,))
    )
    _escribir(escenario.harness, ARCHIVO, "import sqlite3\n\nCONN = sqlite3.connect('app.db')\n")
    escenario.harness.lineage.actual_paths = (ARCHIVO,)
    escenario.harness.lineage.actual_added_lines = (
        "import sqlite3",
        "CONN = sqlite3.connect('app.db')",
    )
    kernel = replan_kernel(
        escenario.harness, replanner, audit=AuditLogger(), policy=politica(escenario.gate)
    )
    run = kernel.run_all(escenario.harness.request)
    nodos = [(node.node_id, node.status.value, node.failure_code) for node in run.nodes]

    assert run.status is ProjectState.BLOCKED, (run.status, run.failure_code)
    assert run.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
    assert any(
        status == ProjectNodeStatus.BLOCKED.value
        and code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
        for _, status, code in nodos
    ), nodos
    assert run.workspace.accepted_revision == run.workspace.initial_revision, (
        "el árbol no acepta el cambio de arquitectura"
    )


def test_la_autoridad_es_explicita_y_viaja_en_el_veredicto(tmp_path: Path) -> None:
    """El veredicto dice qué se autorizó: superficie, recursos y dimensiones (ENGINE-6.3.R3)."""
    escenario = montaje(tmp_path / "envelope", replanner=FakeReplanner())
    veredicto = contenido(
        escenario, propuesta("reintentar el nodo A con la estrategia corregida")
    )

    assert veredicto.allows_autonomous is True
    assert veredicto.authority.explicit is True
    assert veredicto.authority.files == (ARCHIVO,)
    assert veredicto.authority.dimensions, "las dimensiones autorizadas son explícitas"
    assert "superficie" in veredicto.authority.detail()


def test_la_contencion_sigue_siendo_obligatoria(tmp_path: Path) -> None:
    """Una petición fuera del envelope sigue dando ``EXPANDED``: R3 no sustituye a T5."""
    escenario = montaje(tmp_path / "t5", replanner=FakeReplanner())
    veredicto = contenido(
        escenario,
        propuesta(
            "reintentar el nodo A con la estrategia corregida",
            uses_resources=("datastore:mongodb",),
        ),
    )

    assert veredicto.compatibility is ArchitectureCompatibility.EXPANDED
    assert veredicto.allows_autonomous is False
    assert veredicto.requires_human is True


def test_el_envelope_autorizado_del_contrato_se_conserva(tmp_path: Path) -> None:
    """El envelope autorizado sigue viniendo del contrato, no del Planner."""
    escenario = montaje(tmp_path / "contrato", replanner=FakeReplanner())
    veredicto = contenido(escenario, propuesta("reintentar el nodo A con la estrategia corregida"))

    assert "datastore:postgres" in veredicto.authority.resources
    assert PYPROJECT_CON_PSYCOPG.strip(), "el manifiesto autorizado de la suite sigue disponible"
