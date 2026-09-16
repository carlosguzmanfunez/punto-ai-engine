"""Matriz terminal A-P de ENGINE-6.3 (auditoría de cierre sobre el candidato final).

Cada puerta del encargo terminal queda con una prueba permanente. Las puertas A-D (bypass original,
mismo fichero, tecnología desconocida, import dinámico) viven en la suite de autoridad del efecto;
la M (Human Gate) en la suite de aprobación y en el grupo G de la de escalado; aquí están E, F, G,
H, I, J, K, L, N y P.

Reglas que se demuestran: la existencia de ``http://`` **no** es segura ni peligrosa por sí misma
(se juzga el destino contra la autoridad concedida); una llamada de shell **no** es segura ni
arquitectónica por sí misma (se juzga el comando contra la frontera de ejecución del motor); el
conjunto de ficheros sale del repositorio; y ninguna incertidumbre ni ningún fallo de un control de
autoridad concede nada.

No hay proveedor real, ni red, ni reloj, ni azar.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_support import ChildOutcome, planned
from punto.audit.logger import AuditLogger
from punto.project import kernel as kernel_module
from punto.schemas.audit import AuditEventType
from punto.schemas.planning import ArchitecturePlan, DataStore, ExternalIntegration
from punto.schemas.project import ProjectFailureCode, ProjectNodeStatus, ProjectState
from test_project_kernel_matrix import Harness
from test_project_replan_escalation_and_binding import contenido, montaje, propuesta
from test_project_replan_kernel import FakeReplanner, event_types, replan_harness, replan_kernel
from test_project_replan_observed_resources import arquitectura_postgres_con_psycopg

ARCHIVO = "app.py"
CODIGO_LIMPIO = "MENSAJE = 'validacion corregida'\n"

#: Arquitectura con la integración ``oauth`` autorizada: es la que hace legítimo un destino que la
#: nombra, y la que deja fuera a cualquier otro destino.
ARQUITECTURA_CON_OAUTH = ArchitecturePlan(
    architecture_style="monolito modular",
    data_stores=(DataStore(id="db", name="principal", engine="postgres"),),
    external_integrations=(
        ExternalIntegration(id="auth", name="oauth", protocol="https", auth="oauth2"),
    ),
)


def _escribir(h: Harness, nombre: str, contenido_fichero: str) -> None:
    """Escribe el fichero en el workspace real del proyecto."""
    destino = h.workspace / nombre
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(contenido_fichero, encoding="utf-8")


def _conducir(
    tmp_path: Path,
    contenido_fichero: str,
    *,
    fichero: str = ARCHIVO,
    rutas: tuple[str, ...] | None = None,
    lineas: tuple[str, ...] | None = None,
    architecture: ArchitecturePlan | None = None,
) -> tuple[AuditLogger, object]:
    """Monta el proyecto, escribe el cambio del nodo A y lo conduce hasta su liquidación.

    El linaje declara el diff real —rutas y contenido añadido— como lo haría Git.
    """
    audit = AuditLogger()
    h = replan_harness(
        tmp_path,
        outcomes={"A": ChildOutcome(files=(fichero,))},
        default=ChildOutcome(),
        architecture=(
            architecture if architecture is not None else arquitectura_postgres_con_psycopg()
        ),
        tasks=(planned("A", allowed_files=(fichero,)),),
    )
    h.lineage.actual_paths = (fichero,) if rutas is None else rutas
    h.lineage.actual_added_lines = (
        tuple(contenido_fichero.splitlines()) if lineas is None else lineas
    )
    _escribir(h, fichero, contenido_fichero)
    return audit, replan_kernel(h, FakeReplanner(), audit=audit).run_all(h.request)


def _fallo(run: object) -> ProjectFailureCode | None:
    """Código de fallo del nodo A, si lo hay."""
    node = run.node("A")  # type: ignore[attr-defined]
    assert node is not None
    return node.failure_code


# ---------------------------------------------------------------------------
# E — HTTP/HTTPS: se juzga el destino contra la autoridad, no el esquema
# ---------------------------------------------------------------------------
def test_e1_http_tactico_dentro_de_la_autoridad_sigue_adelante(tmp_path: Path) -> None:
    """Un destino que el envelope autorizó (la integración ``oauth``) no bloquea el trabajo."""
    contenido_fichero = 'TOKEN_URL = "https://oauth.example.com/token"\n'
    audit, run = _conducir(
        tmp_path, contenido_fichero, architecture=ARQUITECTURA_CON_OAUTH
    )
    node = run.node("A")  # type: ignore[attr-defined]

    assert node is not None
    assert node.status is ProjectNodeStatus.COMPLETED, (node.failure_code, node.failure_detail)
    assert run.workspace.accepted_revision != run.workspace.initial_revision  # type: ignore[attr-defined]
    assert AuditEventType.PROJECT_NODE_ARCHITECTURE_VIOLATION not in event_types(audit)


def test_e2_http_para_una_integracion_no_autorizada_falla_cerrado(tmp_path: Path) -> None:
    """Una integración externa nueva no la autoriza el hecho de usar ``https``."""
    contenido_fichero = 'ENDPOINT = "https://unknown-external-provider.example/api"\n'
    audit, run = _conducir(tmp_path, contenido_fichero)
    node = run.node("A")  # type: ignore[attr-defined]

    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
    assert "unknown-external-provider.example" in node.failure_detail
    assert AuditEventType.PROJECT_NODE_ARCHITECTURE_VIOLATION in event_types(audit)


def test_e2b_peticion_http_a_proveedor_desconocido(tmp_path: Path) -> None:
    """``requests.post`` a un destino no autorizado: el efecto externo no se puede demostrar."""
    contenido_fichero = (
        "import requests\n\n"
        'RESP = requests.post("https://otro-proveedor.example/api", json={})\n'
    )
    _, run = _conducir(tmp_path, contenido_fichero)
    node = run.node("A")  # type: ignore[attr-defined]

    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION


def test_e3_un_host_local_no_es_una_integracion_externa(tmp_path: Path) -> None:
    """``localhost`` no introduce una integración nueva: sigue siendo trabajo corriente."""
    contenido_fichero = 'BASE = "http://localhost:8000/api"\n'
    _, run = _conducir(tmp_path, contenido_fichero)
    node = run.node("A")  # type: ignore[attr-defined]

    assert node is not None
    assert node.status is ProjectNodeStatus.COMPLETED, (node.failure_code, node.failure_detail)


# ---------------------------------------------------------------------------
# F — shell / subprocess: el comando se juzga contra la frontera del motor
# ---------------------------------------------------------------------------
def test_f1_una_llamada_de_shell_acotada_sigue_adelante(tmp_path: Path) -> None:
    """``pytest`` es un ejecutable que el motor ya considera acotado: no bloquea."""
    contenido_fichero = 'import subprocess\n\nsubprocess.run(["pytest", "-q"])\n'
    _, run = _conducir(tmp_path, contenido_fichero)
    node = run.node("A")  # type: ignore[attr-defined]

    assert node is not None
    assert node.status is ProjectNodeStatus.COMPLETED, (node.failure_code, node.failure_detail)


@pytest.mark.parametrize(
    "linea",
    (
        'subprocess.run(["curl", "https://otro.example"])\n',
        'subprocess.run(["docker", "compose", "up"])\n',
        'os.system("systemctl restart app")\n',
        "subprocess.run(cmd)\n",
    ),
    ids=("curl", "docker", "systemctl", "dinamico"),
)
def test_f2_una_llamada_de_shell_no_acotada_falla_cerrado(tmp_path: Path, linea: str) -> None:
    """El efecto de un comando no se demuestra leyendo el fichero: sin comando acotado, no pasa."""
    _, run = _conducir(tmp_path, linea)
    node = run.node("A")  # type: ignore[attr-defined]

    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED, (linea, node.status, node.failure_detail)
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION


# ---------------------------------------------------------------------------
# G — generación indirecta de ficheros: el conjunto sale del repositorio
# ---------------------------------------------------------------------------
def test_g1_un_fichero_generado_con_arquitectura_no_se_acepta(tmp_path: Path) -> None:
    """El nodo declara ``app.py`` y genera además una migración: el diff real trae las dos."""
    audit, run = _conducir(
        tmp_path,
        CODIGO_LIMPIO,
        rutas=(ARCHIVO, "migrations/0004_generada.sql"),
        lineas=("MENSAJE = 'validacion corregida'", "ALTER TABLE ordenes ADD COLUMN total INT;"),
    )
    node = run.node("A")  # type: ignore[attr-defined]

    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
    assert AuditEventType.PROJECT_NODE_UNDECLARED_CHANGE in event_types(audit)


def test_g2_un_fichero_relevante_borrado_no_se_acepta(tmp_path: Path) -> None:
    """Borrar un manifiesto relevante es un efecto que no se puede leer: falla cerrado."""
    _, run = _conducir(
        tmp_path,
        CODIGO_LIMPIO,
        rutas=(ARCHIVO, "Dockerfile"),
        lineas=("MENSAJE = 'validacion corregida'",),
    )
    node = run.node("A")  # type: ignore[attr-defined]

    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION


def test_g3_un_fichero_nuevo_corriente_no_bloquea(tmp_path: Path) -> None:
    """Un fichero nuevo sin arquitectura se inspecciona y no bloquea."""
    _, run = _conducir(
        tmp_path,
        CODIGO_LIMPIO,
        rutas=(ARCHIVO, "src/util.py"),
        lineas=("MENSAJE = 'validacion corregida'", "def normalizar(v):\n    return v.strip()"),
    )
    node = run.node("A")  # type: ignore[attr-defined]

    assert node is not None
    assert node.status is ProjectNodeStatus.COMPLETED, (node.failure_code, node.failure_detail)


def test_g4_un_renombrado_hacia_una_superficie_relevante_no_se_acepta(tmp_path: Path) -> None:
    """Git representa el renombrado por su ruta nueva, y esa ruta es una superficie relevante."""
    _, run = _conducir(
        tmp_path,
        CODIGO_LIMPIO,
        rutas=(ARCHIVO, "config/settings.yaml"),
        lineas=("MENSAJE = 'validacion corregida'",),
    )
    node = run.node("A")  # type: ignore[attr-defined]

    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION


# ---------------------------------------------------------------------------
# H / I / J — dependencias, SQL y configuración sensible
# ---------------------------------------------------------------------------
DEPENDENCIAS: tuple[tuple[str, str], ...] = (
    ("manifiesto", 'pyproject.toml', '[project]\nname = "s"\ndependencies = ["pymongo"]\n'),
    ("lockfile", "poetry.lock", '[[package]]\nname = "pymongo"\nversion = "4.6"\n'),
)

SQL_Y_CONFIG: tuple[tuple[str, str], ...] = (
    ("migracion", "migrations/0005_cambio.sql", "ALTER SYSTEM SET engine = 'otro';\n"),
    ("sql", "db/schema.sql", "DROP TABLE ordenes;\n"),
    ("persistencia", "config/persistence.py", "ENGINE = 'otro'\n"),
    ("despliegue", "deploy/k8s.yaml", "kind: Deployment\n"),
    ("autenticacion", "config/auth.py", "PROVIDER = 'otro'\n"),
    ("infraestructura", "infra/main.tf", 'resource "aws_db_instance" "x" {}\n'),
    ("seguridad", "secrets/prod.env", "TOKEN=abc\n"),
)

IDS_DEPENDENCIAS = [caso[0] for caso in DEPENDENCIAS]
IDS_SQL_CONFIG = [caso[0] for caso in SQL_Y_CONFIG]


@pytest.mark.parametrize(
    ("etiqueta", "fichero", "contenido_fichero"), DEPENDENCIAS, ids=IDS_DEPENDENCIAS
)
def test_h_dependencia_no_autorizada_falla_cerrado(
    tmp_path: Path, etiqueta: str, fichero: str, contenido_fichero: str
) -> None:
    """Manifiesto o *lockfile* con una dependencia que el envelope no autoriza."""
    _, run = _conducir(tmp_path, contenido_fichero, fichero=fichero)
    node = run.node("A")  # type: ignore[attr-defined]

    assert node is not None, etiqueta
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION


@pytest.mark.parametrize(
    ("etiqueta", "fichero", "contenido_fichero"), SQL_Y_CONFIG, ids=IDS_SQL_CONFIG
)
def test_i_j_sql_y_configuracion_sensible_fallan_cerrado(
    tmp_path: Path, etiqueta: str, fichero: str, contenido_fichero: str
) -> None:
    """SQL, migraciones, persistencia, despliegue, autenticación, infraestructura y secretos."""
    _, run = _conducir(tmp_path, contenido_fichero, fichero=fichero)
    node = run.node("A")  # type: ignore[attr-defined]

    assert node is not None, etiqueta
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION


# ---------------------------------------------------------------------------
# K — información ausente: nunca aumenta la autoridad
# ---------------------------------------------------------------------------
def test_k1_sin_contenido_de_diff_falla_cerrado(tmp_path: Path) -> None:
    """Rutas cambiadas y cero contenido legible: no hay prueba del efecto."""
    _, run = _conducir(tmp_path, CODIGO_LIMPIO, lineas=())
    node = run.node("A")  # type: ignore[attr-defined]

    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION


def test_k2_un_linaje_que_no_responde_falla_cerrado(tmp_path: Path) -> None:
    """El linaje no puede dar el contenido del diff: autoridad irresoluble."""
    from punto.project.workspace import ProjectRevisionMismatchError

    audit = AuditLogger()
    h = replan_harness(
        tmp_path,
        outcomes={"A": ChildOutcome(files=(ARCHIVO,))},
        default=ChildOutcome(),
        architecture=arquitectura_postgres_con_psycopg(),
        tasks=(planned("A", allowed_files=(ARCHIVO,)),),
    )

    def falla(_base: str, _head: str) -> None:
        raise ProjectRevisionMismatchError("git no responde")

    h.lineage._on_changed = falla
    h.lineage.actual_paths = (ARCHIVO,)
    _escribir(h, ARCHIVO, CODIGO_LIMPIO)
    run = replan_kernel(h, FakeReplanner(), audit=audit).run_all(h.request)
    node = run.node("A")

    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION


def test_k3_sin_superficie_autorizada_no_hay_autonomia(tmp_path: Path) -> None:
    """Un nodo que introduce trabajo y no declara superficie no tiene autoridad que conceder."""
    from punto.schemas.replan import ReplanNodeSpec

    escenario = montaje(tmp_path / "k3", replanner=FakeReplanner())
    base = propuesta("reintentar el nodo A con la estrategia corregida")
    operation = base.operations[0]
    spec = operation.nodes[0].model_copy(update={"allowed_files": ()})
    sin_superficie = base.model_copy(
        update={"operations": (operation.model_copy(update={"nodes": (spec,)}),)}
    )
    veredicto = contenido(escenario, sin_superficie)

    assert isinstance(spec, ReplanNodeSpec)
    assert veredicto.authority.files == ()
    assert veredicto.authority.explicit is False, "sin superficie y con trabajo, no hay autoridad"
    assert veredicto.allows_autonomous is False
    assert veredicto.requires_human is True


# ---------------------------------------------------------------------------
# L — el agente miente sobre files_changed
# ---------------------------------------------------------------------------
def test_l_el_conjunto_real_manda_sobre_lo_declarado(tmp_path: Path) -> None:
    """Declara ``app.py`` y cambia además manifiesto y migración: se inspeccionan las tres."""
    audit, run = _conducir(
        tmp_path,
        CODIGO_LIMPIO,
        rutas=(ARCHIVO, "pyproject.toml", "migrations/0006.sql"),
        lineas=("MENSAJE = 'validacion corregida'", 'dependencies = ["pymongo"]'),
    )
    node = run.node("A")  # type: ignore[attr-defined]

    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
    assert AuditEventType.PROJECT_NODE_UNDECLARED_CHANGE in event_types(audit)
    declarado = next(
        event
        for event in audit.events()
        if event.event_type is AuditEventType.PROJECT_NODE_UNDECLARED_CHANGE
    )
    reales = set(dict(declarado.metadata)["actual_paths"])
    assert {"pyproject.toml", "migrations/0006.sql"} <= reales


# ---------------------------------------------------------------------------
# N — la autonomía segura sigue funcionando
# ---------------------------------------------------------------------------
POSITIVOS: tuple[tuple[str, str, str], ...] = (
    ("edicion-de-codigo", ARCHIVO, CODIGO_LIMPIO),
    (
        "edicion-de-test",
        "tests/test_app.py",
        "import pytest\n\n\ndef test_slug():\n    assert True\n",
    ),
    ("documentacion", "docs/guia.md", "# Guia\n\nTexto de la guia.\n"),
    ("utilidad-local", "src/util.py", "def normalizar(v):\n    return v.strip()\n"),
)

IDS_POSITIVOS = [caso[0] for caso in POSITIVOS]


@pytest.mark.parametrize(("etiqueta", "fichero", "contenido_fichero"), POSITIVOS, ids=IDS_POSITIVOS)
def test_n_la_autonomia_segura_sigue_funcionando(
    tmp_path: Path, etiqueta: str, fichero: str, contenido_fichero: str
) -> None:
    """Ediciones acotadas de código, test, documentación y utilidades: se aceptan y avanzan."""
    audit, run = _conducir(tmp_path, contenido_fichero, fichero=fichero)
    node = run.node("A")  # type: ignore[attr-defined]

    assert node is not None, etiqueta
    assert node.status is ProjectNodeStatus.COMPLETED, (
        etiqueta,
        node.failure_code,
        node.failure_detail,
    )
    assert run.workspace.accepted_revision != run.workspace.initial_revision  # type: ignore[attr-defined]
    assert AuditEventType.PROJECT_NODE_ARCHITECTURE_VIOLATION not in event_types(audit)


# ---------------------------------------------------------------------------
# P — caminos de fallo: un control que falla no autoriza
# ---------------------------------------------------------------------------
def test_p1_una_excepcion_del_clasificador_no_adopta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si el clasificador revienta, el intento no continúa: no hay adopción ni gasto aceptado."""

    def revienta(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("clasificador roto")

    escenario = montaje(tmp_path / "p1", replanner=FakeReplanner())
    kernel = replan_kernel(escenario.harness, FakeReplanner(), audit=AuditLogger())
    monkeypatch.setattr(kernel_module, "classify_replan_change", revienta)
    run = kernel.create(escenario.harness.request)
    with pytest.raises(RuntimeError):
        for _ in range(8):
            run = kernel.step(run)

    assert run.active_generation is not None
    assert run.active_generation.generation_index == 0, "no se adopta ninguna generación"
    assert run.usage.replans_accepted == 0


def test_p2_una_excepcion_de_la_inspeccion_falla_cerrado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si la inspección del efecto no puede ejecutarse, el nodo no se acepta."""

    def revienta(**_kwargs: object) -> None:
        raise RuntimeError("inspeccion rota")

    monkeypatch.setattr(kernel_module, "unproven_effect", revienta)
    _, run = _conducir(tmp_path, CODIGO_LIMPIO)
    node = run.node("A")  # type: ignore[attr-defined]

    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
    assert "inspección del efecto real falló" in node.failure_detail


def test_p3_un_resultado_ilegible_no_se_interpreta_como_vacio(tmp_path: Path) -> None:
    """Un manifiesto que cambia y no se puede leer deja el veredicto sin resolver."""
    _, run = _conducir(
        tmp_path,
        CODIGO_LIMPIO,
        rutas=(ARCHIVO, "pyproject.toml"),
        lineas=("MENSAJE = 'validacion corregida'",),
    )
    node = run.node("A")  # type: ignore[attr-defined]

    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
    assert run.status is ProjectState.BLOCKED
