"""AUD-T-01 — `go.mod` no puede producir una prueba falsa de contención.

Go declara dependencias de dos formas válidas:

```go
require go.mongodb.org/mongo-driver v1.13.0      // una línea
```

```go
require (
	go.mongodb.org/mongo-driver v1.13.0          // bloque
)
```

La auditoría terminal independiente demostró que la forma de una línea producía ``tokens=[]`` y
``unresolved=()``: el parser tomaba ``"require"`` como candidato de módulo y, al no contener
``/``, no extraía nada. La segunda capa tampoco veía los imports de Go, así que el motor afirmaba
``CONTAINED`` sobre un diff que añadía un almacén de datos no autorizado: un nodo Go terminaba
``COMPLETED``.

Aquí se fija el arreglo: **ambas formas producen el mismo hecho**, un ``require`` que el motor no
sabe leer declara *sin resolver*, y el import de Go (suelto, en bloque o con alias) está cubierto
por la segunda capa. La detección textual sigue siendo defensa en profundidad: la autoridad es la
comprobación estructural post-hoc sobre el diff real.

No hay proveedor real, ni red, ni reloj, ni azar.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_support import ChildOutcome, planned
from punto.audit.logger import AuditLogger
from punto.project.resources import (
    ResourceSet,
    resources_from_diff,
    unproven_effect,
)
from punto.schemas.planning import ArchitecturePlan, DataStore, TechnologyChoice
from punto.schemas.project import ProjectFailureCode, ProjectNodeStatus, ProjectState
from test_project_kernel_matrix import Harness
from test_project_replan_kernel import FakeReplanner, replan_harness, replan_kernel
from test_project_replan_observed_resources import arquitectura_postgres_con_psycopg

LINEA = "module x\nrequire go.mongodb.org/mongo-driver v1.13.0\n"
BLOQUE = "module x\nrequire (\n\tgo.mongodb.org/mongo-driver v1.13.0\n)\n"
MODULO = "go.mongodb.org/mongo-driver"


def _diff(texto: str) -> tuple[ResourceSet, tuple[str, ...]]:
    """Recursos y razones sin resolver que el motor deriva de un ``go.mod`` que cambió."""
    contenido = {"go.mod": texto}
    return resources_from_diff(list(contenido), contenido.get)


# ---------------------------------------------------------------------------
# 1. El test exacto del hallazgo
# ---------------------------------------------------------------------------
def test_go_mod_require_de_una_linea_no_produce_contencion_falsa() -> None:
    """Un manifiesto relevante que añade una dependencia no puede quedar vacío **y** resuelto."""
    recursos, unresolved = _diff(LINEA)

    assert recursos.tokens or unresolved, (
        "un manifiesto relevante que anade una dependencia no puede producir el conjunto vacio "
        "sin declararse sin resolver"
    )
    assert f"package:{MODULO}" in recursos.tokens


@pytest.mark.parametrize("texto", (LINEA, BLOQUE), ids=("una-linea", "bloque"))
def test_ambas_formas_de_require_se_comportan_igual(texto: str) -> None:
    """Las dos formas válidas de Go tienen que producir el mismo hecho, no una sí y otra no."""
    recursos, unresolved = _diff(texto)

    assert any(MODULO in token for token in recursos.tokens) or unresolved
    assert not (recursos.is_empty and not unresolved), (
        "la dependencia añadida no puede quedar en tokens vacios y sin resolver a la vez"
    )


def test_las_dos_formas_producen_el_mismo_conjunto() -> None:
    """Equivalencia exacta entre la forma de una línea y la de bloque."""
    assert _diff(LINEA) == _diff(BLOQUE)


# ---------------------------------------------------------------------------
# 2. Sintaxis de go.mod que el motor debe leer o declarar sin resolver
# ---------------------------------------------------------------------------
VARIANTES: tuple[tuple[str, str], ...] = (
    ("bloque-multiple", "module x\nrequire (\n\tgo.mongodb.org/mongo-driver v1.13.0\n"
                        "\tgithub.com/redis/go-redis/v9 v9.0.0\n)\n"),
    ("indirecto", "module x\nrequire go.mongodb.org/mongo-driver v1.13.0 // indirect\n"),
    ("sin-version", "module x\nrequire go.mongodb.org/mongo-driver\n"),
    ("replace", "module x\nreplace example.com/viejo => go.mongodb.org/mongo-driver v1.13.0\n"),
)

IDS_VARIANTES = [caso[0] for caso in VARIANTES]


@pytest.mark.parametrize(("etiqueta", "texto"), VARIANTES, ids=IDS_VARIANTES)
def test_las_variantes_de_go_mod_no_quedan_en_vacio(etiqueta: str, texto: str) -> None:
    """Bloque múltiple, ``// indirect``, sin versión y ``replace``: detectan o fallan cerrado."""
    recursos, unresolved = _diff(texto)

    assert recursos.tokens or unresolved, etiqueta
    assert not (recursos.is_empty and not unresolved), etiqueta


def test_un_require_ilegible_declara_sin_resolver() -> None:
    """Un ``require`` ilegible no se interpreta como «sin dependencias»: queda sin resolver."""
    recursos, unresolved = _diff("module x\nrequire (\n)\n")

    assert recursos.is_empty is True
    assert unresolved, "un require vacío queda sin resolver, nunca en conjunto vacío resuelto"


def test_un_go_mod_sin_dependencias_no_bloquea() -> None:
    """Sin ``require`` no hay dependencia que negar: el conjunto vacío es correcto y resuelto."""
    recursos, unresolved = _diff("module x\n\ngo 1.21\n\ntoolchain go1.22.0\n")

    assert recursos.is_empty is True
    assert unresolved == ()


# ---------------------------------------------------------------------------
# 3. Segunda capa: imports de Go en unproven_effect
# ---------------------------------------------------------------------------
IMPORTS: tuple[tuple[str, bool], ...] = (
    ('import "go.mongodb.org/mongo-driver/mongo"', True),
    ('import (\n\t"go.mongodb.org/mongo-driver/mongo"\n)', True),
    ('import mongo "go.mongodb.org/mongo-driver/mongo"', True),
    ('mongo "go.mongodb.org/mongo-driver/mongo"', True),
    ('import "fmt"', False),
    ('import (\n\t"fmt"\n\t"net/http"\n)', False),
    ('import "github.com/redis/go-redis/v9"', True),
)

IDS_IMPORTS = [caso[0].replace("\n", " ")[:34] for caso in IMPORTS]


@pytest.mark.parametrize(("texto", "espera_razon"), IMPORTS, ids=IDS_IMPORTS)
def test_unproven_effect_detecta_imports_de_go(texto: str, espera_razon: bool) -> None:
    """Un import de Go con dominio no autorizado escala; la biblioteca estándar no."""
    razones = unproven_effect(added_lines=tuple(texto.splitlines()), authorized=ResourceSet())

    assert bool(razones) is espera_razon, (texto, razones)


def test_un_import_de_go_autorizado_no_escala() -> None:
    """Si el contrato autorizó ese módulo, la segunda capa no lo bloquea."""
    autorizado = ResourceSet.of([f"package:{MODULO}"])
    razones = unproven_effect(
        added_lines=('import "go.mongodb.org/mongo-driver/mongo"',), authorized=autorizado
    )

    assert razones == ()


# ---------------------------------------------------------------------------
# 4. Regresión de los parsers que no debían cambiar
# ---------------------------------------------------------------------------
PARSERS: tuple[tuple[str, str, str], ...] = (
    ("requirements", "requirements.txt", "fastapi==0.110\n"),
    ("pyproject", "pyproject.toml", '[project]\nname = "s"\ndependencies = ["fastapi==0.1"]\n'),
    ("package-json", "package.json", '{"dependencies": {"express": "4.0.0"}}'),
    ("cargo", "Cargo.toml", '[dependencies]\nserde = "1.0"\n'),
    ("dockerfile", "Dockerfile", "FROM python:3.12-slim\n"),
    ("compose", "compose.yaml", "services:\n  app:\n    image: redis:7\n"),
)

IDS_PARSERS = [caso[0] for caso in PARSERS]


@pytest.mark.parametrize(("etiqueta", "fichero", "texto"), PARSERS, ids=IDS_PARSERS)
def test_los_demas_parsers_siguen_detectando(etiqueta: str, fichero: str, texto: str) -> None:
    """El parche de ``go.mod`` no altera el resto de parsers soportados."""
    contenido = {fichero: texto}
    recursos, unresolved = resources_from_diff(list(contenido), contenido.get)

    assert recursos.tokens, etiqueta
    assert unresolved == (), etiqueta


# ---------------------------------------------------------------------------
# 5. Extremo a extremo: el nodo Go no puede adoptarse en autonomía
# ---------------------------------------------------------------------------
def _conducir(
    tmp_path: Path, texto: str, *, architecture: ArchitecturePlan | None = None
) -> object:
    """Conduce un nodo cuyo diff real añade el ``go.mod`` con esa dependencia."""
    audit = AuditLogger()
    h: Harness = replan_harness(
        tmp_path,
        outcomes={"A": ChildOutcome(files=("go.mod",))},
        default=ChildOutcome(),
        architecture=(
            architecture if architecture is not None else arquitectura_postgres_con_psycopg()
        ),
        tasks=(planned("A", allowed_files=("go.mod",)),),
    )
    h.lineage.actual_paths = ("go.mod",)
    h.lineage.actual_added_lines = tuple(texto.splitlines())
    (h.workspace / "go.mod").write_text(texto, encoding="utf-8")
    return replan_kernel(h, FakeReplanner(), audit=audit).run_all(h.request)


@pytest.mark.parametrize("texto", (LINEA, BLOQUE), ids=("una-linea", "bloque"))
def test_e2e_una_dependencia_go_no_autorizada_no_se_adopta(tmp_path: Path, texto: str) -> None:
    """El nodo que añade la dependencia por cualquiera de las dos formas queda bloqueado.

    Resultado prohibido: adopción autónoma del cambio como ``CONTAINED``.
    """
    run = _conducir(tmp_path, texto)
    node = run.node("A")

    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED, (node.status, node.failure_detail)
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION
    assert run.status is ProjectState.BLOCKED
    assert run.workspace.accepted_revision == run.workspace.initial_revision, (
        "la revisión aceptada no avanza con una expansión no autorizada"
    )


def test_e2e_una_dependencia_go_autorizada_sigue_adelante(tmp_path: Path) -> None:
    """Si el contrato autorizó ese módulo, el nodo no se bloquea: no se sobre-restringe."""
    arquitectura = ArchitecturePlan(
        architecture_style="monolito modular",
        data_stores=(DataStore(id="db", name="principal", engine="postgres"),),
        technology_choices=(TechnologyChoice(topic="persistencia", choice=MODULO),),
    )
    run = _conducir(tmp_path, LINEA, architecture=arquitectura)
    node = run.node("A")

    assert node is not None
    assert node.status is ProjectNodeStatus.COMPLETED, (node.failure_code, node.failure_detail)
    assert run.workspace.accepted_revision != run.workspace.initial_revision
