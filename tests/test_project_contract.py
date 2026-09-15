"""Contrato durable e inmutable del proyecto (ENGINE-6.3).

Qué demuestra esta prueba y por qué está separada
-------------------------------------------------
``punto.project.contract`` es lo que la replanificación autónoma **no** puede renegociar: el
objetivo, los criterios globales con identidad estable, el alcance autorizado, las rutas protegidas
y los techos. Esta prueba lo ejercita sin kernel, sin child workflows y sin red, contra un
:class:`~punto.workflow.artifacts.FileArtifactStore` real en ``tmp_path``: lo que se comprueba es la
derivación, la huella, la publicación durable y las tres comparaciones que el motor usará para
decidir (contrato intacto, alcance respetado, cobertura de criterios completa).

Lo que se comprueba, en el orden de la prueba:

1. las identidades de criterio son posicionales (``AC-1``, ``AC-2``…) y coinciden con los textos que
   el contrato declara;
2. ``derive_contract`` deriva lo declarado —objetivo, criterios, alcance, protegidas, techos y
   revisión— y es determinista: dos derivaciones dan la **misma huella** aunque cambien
   ``contract_id`` y ``created_at``;
3. la huella cambia si cambia cualquiera de los términos, uno por uno;
4. sin plan no se inventa ni un criterio ni una ruta: queda el piso constitucional, y si la petición
   sí declara los términos, mandan los de la petición;
5. el contrato publicado se resuelve **entero**, y republicarlo repite bytes y digest;
6. ``assert_contract_unchanged`` nombra exactamente el término cambiado y calla si solo cambian la
   identidad o el sello;
7. ``scope_violations`` detecta lo que se sale del alcance y lo que cae en una ruta protegida,
   incluso si el plan la autorizó, normalizando los separadores de Windows;
8. ``criterion_coverage`` detecta el criterio que ninguna cobertura menciona;
9. un contrato ausente, ilegible, no-UTF-8 o que no valida falla con ``ProjectContractError`` en
   vez de degradarse a ``None``; una referencia de otro tipo sí devuelve ``None``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import pytest

from punto.policy.permissions import CONSTITUTIONAL_PROTECTED_PATHS
from punto.project.contract import (
    CONTRACT_LABEL,
    CRITERION_ID_PREFIX,
    PROJECT_CONTRACT_KIND,
    ProjectContractError,
    assert_contract_unchanged,
    contract_fingerprint,
    criterion_coverage,
    criterion_ids,
    derive_contract,
    publish_contract,
    resolve_contract,
    scope_violations,
)
from punto.project.handoff import project_run_id_for
from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.planning import PlannedTask, Roadmap, TaskGraph
from punto.schemas.project import ProjectRequest
from punto.schemas.replan import MAX_REPLAN_COVERAGE, ProjectContract
from punto.schemas.workflow import ArtifactReference, RoleName
from punto.workflow.artifacts import STORE_NAME, FileArtifactStore
from punto.workflow.handoff import PLAN_KIND, DurablePlan

#: Identidad fija del proyecto de prueba: la misma petición produce siempre el mismo run.
PROJECT_ID: Final[UUID] = UUID("11111111-2222-3333-4444-555555555555")
#: Identidad del run, fijada por la prueba (en producción la deriva ``project_run_id_for``).
RUN_ID: Final[UUID] = UUID("aaaaaaaa-1111-2222-3333-444444444444")
#: Identidad del plan durable de prueba.
PLAN_ID: Final[UUID] = UUID("bbbbbbbb-1111-2222-3333-444444444444")
#: Instante congelado: el plan de la prueba no depende del reloj.
_FIXED_TIME: Final[datetime] = datetime(2026, 1, 1, tzinfo=UTC)
#: Revisión inicial declarada por la prueba.
INITIAL_REVISION: Final[str] = "sha-inicial"
#: Criterios y alcance que declara el plan base, en el orden en que los declara.
BASE_CRITERIA: Final[tuple[str, ...]] = ("criterio uno", "criterio dos", "criterio tres")
BASE_SCOPE: Final[tuple[str, ...]] = ("src/a.py", "tests/test_a.py", "src/modulo")
#: Ruta del piso constitucional que se usa en las pruebas de protección.
CONSTITUTIONAL_PATH: Final[str] = "config/permissions.yaml"


# ---------------------------------------------------------------------------
# Constructores de la prueba
# ---------------------------------------------------------------------------
def _reference(
    *,
    kind: str = PLAN_KIND,
    label: str = "artefacto de prueba",
    reference: str = "plan/1.bin",
) -> ArtifactReference:
    """Referencia durable de prueba apuntando al almacén local."""
    return ArtifactReference(
        kind=kind, label=label, store=STORE_NAME, reference=reference, digest="0" * 64
    )


def _request(
    *,
    objective: str = "construir el proyecto de prueba",
    risk: RiskLevel = RiskLevel.MEDIUM,
    authority: AuthorityLevel = AuthorityLevel.LEVEL_1_AUTONOMOUS_REVIEW,
) -> ProjectRequest:
    """Petición de proyecto fija: sin campos variables, para que dos derivaciones coincidan."""
    return ProjectRequest(
        project_id=PROJECT_ID,
        objective=objective,
        action="modify_file",
        plan_ref=_reference(label="plan del proyecto"),
        idempotency_key="clave-del-proyecto",
        risk=risk,
        authority=authority,
        created_at=_FIXED_TIME,
    )


def _task(
    task_id: str,
    *,
    criteria: tuple[str, ...],
    allowed_files: tuple[str, ...],
) -> PlannedTask:
    """Tarea del plan durable con los dos campos de los que el contrato deriva sus términos."""
    return PlannedTask(
        id=task_id,
        title=f"Tarea {task_id}",
        objective=f"hacer el trabajo de {task_id}",
        epic_id="epic-de-prueba",
        acceptance_criteria=criteria,
        allowed_files=allowed_files,
    )


def _plan(*tasks: PlannedTask) -> DurablePlan:
    """Plan durable con las tareas dadas, en el mismo orden en el roadmap y en el grafo."""
    return DurablePlan(
        roadmap=Roadmap(
            id=PLAN_ID, created_at=_FIXED_TIME, project_name="proyecto de prueba", tasks=tasks
        ),
        task_graph=TaskGraph(
            id=PLAN_ID, created_at=_FIXED_TIME, project_name="proyecto de prueba", tasks=tasks
        ),
    )


def _base_plan() -> DurablePlan:
    """Plan de referencia: tres criterios (uno de ellos repetido) y tres entradas de alcance.

    El criterio repetido está a propósito: el contrato global no puede declarar dos identidades para
    el mismo enunciado.
    """
    return _plan(
        _task(
            "A",
            criteria=("criterio uno", "criterio dos"),
            allowed_files=("src/a.py", "tests/test_a.py"),
        ),
        _task("B", criteria=("criterio tres", "criterio uno"), allowed_files=("src/modulo",)),
    )


def _derive(
    *,
    request: ProjectRequest | None = None,
    plan: DurablePlan | None = None,
    initial_revision: str = INITIAL_REVISION,
) -> ProjectContract:
    """Contrato derivado con la petición y el plan base, salvo lo que la prueba sustituya."""
    return derive_contract(
        request if request is not None else _request(),
        plan if plan is not None else _base_plan(),
        project_run_id=RUN_ID,
        initial_revision=initial_revision,
    )


# ---------------------------------------------------------------------------
# 1. Identidad de los criterios
# ---------------------------------------------------------------------------
def test_criterion_ids_son_estables_por_posicion_y_coinciden_con_los_textos() -> None:
    """Las identidades de criterio se derivan de la posición, no del texto del criterio."""
    assert criterion_ids(0) == ()
    assert criterion_ids(1) == (f"{CRITERION_ID_PREFIX}1",)
    assert criterion_ids(3) == ("AC-1", "AC-2", "AC-3")
    assert criterion_ids(2) != criterion_ids(3)
    assert criterion_ids(5)[-1] == "AC-5"
    with pytest.raises(ValueError, match="negativa"):
        criterion_ids(-1)

    contract = _derive()

    assert contract.acceptance_criteria == BASE_CRITERIA
    assert contract.acceptance_criterion_ids == criterion_ids(len(BASE_CRITERIA))
    # La identidad y el enunciado quedan alineados: ``criterion_text`` devuelve el texto por id.
    assert tuple(
        contract.criterion_text(identifier) for identifier in contract.acceptance_criterion_ids
    ) == BASE_CRITERIA
    assert contract.has_criterion("AC-2")
    assert not contract.has_criterion("AC-9")
    assert contract.criterion_text("AC-9") == ""


# ---------------------------------------------------------------------------
# 2. Derivación determinista
# ---------------------------------------------------------------------------
def test_derive_contract_deriva_lo_declarado_y_es_determinista() -> None:
    """El contrato sale de la petición y del plan, y dos derivaciones comparten huella.

    La identidad del artefacto y su sello **sí** cambian entre derivaciones —son ``uuid4`` y reloj—,
    y por eso la huella los excluye: si entraran, cada reanudación en otro proceso se leería como un
    cambio de contrato.
    """
    request = _request()
    first = _derive(request=request)
    second = _derive(request=request)

    assert first.original_goal == request.objective
    assert first.project_id == request.project_id
    assert first.project_run_id == RUN_ID
    assert first.acceptance_criteria == BASE_CRITERIA
    assert first.acceptance_criterion_ids == criterion_ids(len(BASE_CRITERIA))
    # El alcance es la unión de los ``allowed_files`` del plan, en orden declarado y sin repetidos.
    assert first.authorized_scope == BASE_SCOPE
    # El piso constitucional entra siempre, aunque el plan no lo declare.
    assert set(first.protected_paths) == set(CONSTITUTIONAL_PROTECTED_PATHS)
    assert tuple(sorted(first.protected_paths)) == first.protected_paths
    assert first.risk_ceiling is request.risk
    assert first.authority_ceiling is request.authority
    assert first.initial_revision == INITIAL_REVISION

    # La huella se guarda calculada y se puede recalcular: es una función de los términos.
    assert first.contract_fingerprint == contract_fingerprint(first)

    # Determinismo: mismos términos, misma huella, aunque la identidad y el sello sean otros.
    assert first.contract_id != second.contract_id
    assert first.contract_fingerprint == second.contract_fingerprint
    replica = first.model_copy(update={"contract_id": uuid4(), "created_at": _FIXED_TIME})
    assert replica != first
    assert contract_fingerprint(replica) == first.contract_fingerprint

    # Y las cotas del contrato se respetan: un plan más grande que la cota se recorta, no se copia.
    enorme = _plan(
        _task(
            "A",
            criteria=tuple(f"criterio {index}" for index in range(40)),
            allowed_files=tuple(f"src/f{index}.py" for index in range(40)),
        )
    )
    acotado = _derive(plan=enorme)
    assert len(acotado.acceptance_criteria) == MAX_REPLAN_COVERAGE
    assert len(acotado.acceptance_criterion_ids) == MAX_REPLAN_COVERAGE
    assert len(acotado.authorized_scope) == MAX_REPLAN_COVERAGE


# ---------------------------------------------------------------------------
# 3. La huella cambia con cada término
# ---------------------------------------------------------------------------
_FINGERPRINT_CHANGES: Final[tuple[tuple[str, dict[str, object]], ...]] = (
    ("objetivo", {"original_goal": "otro objetivo distinto"}),
    (
        "texto de criterio",
        {"acceptance_criteria": ("criterio uno cambiado", "criterio dos", "criterio tres")},
    ),
    ("identidad de criterio", {"acceptance_criterion_ids": ("AC-1", "AC-9", "AC-3")}),
    (
        "alcance",
        {"authorized_scope": ("src/a.py", "tests/test_a.py", "src/modulo", "otro/x.py")},
    ),
    ("ruta protegida", {"protected_paths": (*CONSTITUTIONAL_PROTECTED_PATHS, "config/otra.yaml")}),
    ("techo de riesgo", {"risk_ceiling": RiskLevel.CRITICAL}),
    ("techo de autoridad", {"authority_ceiling": AuthorityLevel.LEVEL_3_HUMAN}),
    ("revision inicial", {"initial_revision": "sha-distinta"}),
)


@pytest.mark.parametrize(
    ("termino", "changes"),
    _FINGERPRINT_CHANGES,
    ids=[termino for termino, _ in _FINGERPRINT_CHANGES],
)
def test_la_huella_cambia_si_cambia_cualquier_termino(
    termino: str, changes: dict[str, object]
) -> None:
    """Cada término del contrato participa en la huella: cambiarlo cambia la identidad durable."""
    base = _derive()
    candidate = base.model_copy(update=changes)

    assert contract_fingerprint(candidate) != base.contract_fingerprint, termino
    assert assert_contract_unchanged(base, candidate), termino


# ---------------------------------------------------------------------------
# 4. Sin plan no se inventa nada
# ---------------------------------------------------------------------------
def test_sin_plan_no_se_inventan_criterios_ni_alcance() -> None:
    """Sin plan el contrato queda con lo que la petición declara, y con el piso constitucional.

    ``ProjectRequest`` no declara criterios de aceptación ni alcance en esta fase, así que sin
    plan durable no hay fuente: el contrato queda vacío en esos dos términos en vez de inventar una
    ruta o un criterio, y el piso constitucional sigue estando porque no depende de una declaración.
    """
    vacio = derive_contract(_request(), None, project_run_id=RUN_ID, initial_revision="")

    assert vacio.acceptance_criteria == ()
    assert vacio.acceptance_criterion_ids == ()
    assert vacio.authorized_scope == ()
    assert set(vacio.protected_paths) == set(CONSTITUTIONAL_PROTECTED_PATHS)
    assert vacio.original_goal == _request().objective
    assert vacio.initial_revision == ""

    # Si la petición **sí** declara esos términos, mandan los suyos sobre los del plan.
    declarada = _request().model_copy(
        update={
            "acceptance_criteria": ("criterio de la petición",),
            # El separador de Windows es el caso a probar: la normalización es la canónica.
            "changed_files": ("SRC\\declarado.py",),
        }
    )
    con_peticion = _derive(request=declarada)

    assert con_peticion.acceptance_criteria == ("criterio de la petición",)
    assert con_peticion.authorized_scope == ("src/declarado.py",)

    # Una petición sin objetivo no produce contrato: se dice con el error de esta capa.
    sin_objetivo = _request().model_copy(update={"objective": "   "})
    with pytest.raises(ProjectContractError, match="objetivo"):
        _derive(request=sin_objetivo)


# ---------------------------------------------------------------------------
# 5. Publicación y resolución
# ---------------------------------------------------------------------------
def test_publicar_y_resolver_conserva_el_contrato_entero(tmp_path: Path) -> None:
    """El contrato va al almacén como ``PROJECT_CONTRACT`` y vuelve idéntico."""
    store = FileArtifactStore(tmp_path)
    request = _request()
    contract = _derive(request=request)

    reference = publish_contract(store, request=request, contract=contract)

    assert reference.kind == PROJECT_CONTRACT_KIND
    assert reference.label == CONTRACT_LABEL
    resolved = resolve_contract(store, reference)
    assert resolved is not None
    assert resolved == contract
    assert resolved.model_dump() == contract.model_dump()
    assert resolved.model_dump()["risk_ceiling"] == int(request.risk)
    assert resolved.model_dump()["authority_ceiling"] == int(request.authority)

    # El contrato vive en el espacio del run del proyecto, no en el de ningún child.
    assert reference.reference.startswith(f"{project_run_id_for(request)}/")
    # Republicar el mismo contrato repite bytes y digest: no hay una segunda verdad.
    repeated = publish_contract(store, request=request, contract=contract)
    assert repeated.digest == reference.digest
    assert repeated.bytes_written == reference.bytes_written

    # Una referencia de otro tipo no es un contrato: es un hueco declarado, no un error.
    assert resolve_contract(store, _reference(kind=PLAN_KIND)) is None


# ---------------------------------------------------------------------------
# 6. Comparación de contratos
# ---------------------------------------------------------------------------
_IDENTITY_ONLY_CHANGES: Final[dict[str, object]] = {
    "contract_id": UUID("cccccccc-1111-2222-3333-444444444444"),
    "created_at": _FIXED_TIME,
    "contract_fingerprint": "huella-distinta-de-la-declarada",
}


@pytest.mark.parametrize(
    ("termino", "changes"),
    _FINGERPRINT_CHANGES,
    ids=[termino for termino, _ in _FINGERPRINT_CHANGES],
)
def test_assert_contract_unchanged_nombra_el_termino_cambiado(
    termino: str, changes: dict[str, object]
) -> None:
    """Cada término cambiado se nombra una sola vez, con el nombre del campo del contrato."""
    base = _derive()
    candidate = base.model_copy(update=changes)

    changed = assert_contract_unchanged(base, candidate)

    assert len(changed) == 1, termino
    assert changed == tuple(changes), termino


def test_assert_contract_unchanged_ignora_identidad_y_sello() -> None:
    """Cambiar la identidad del artefacto o su sello no es cambiar el contrato.

    Es lo que permite que un proyecto reanudado en otro proceso vuelva a derivar su contrato sin
    declararse a sí mismo un cambio de encargo.
    """
    base = _derive()
    replica = base.model_copy(update=_IDENTITY_ONLY_CHANGES)

    assert assert_contract_unchanged(base, base) == ()
    assert assert_contract_unchanged(base, replica) == ()
    assert replica != base

    cambiado = base.model_copy(update={**_IDENTITY_ONLY_CHANGES, "initial_revision": "sha-otra"})
    assert assert_contract_unchanged(base, cambiado) == ("initial_revision",)


# ---------------------------------------------------------------------------
# 7. Alcance
# ---------------------------------------------------------------------------
def test_scope_violations_detecta_fuera_de_alcance_protegida_y_normaliza() -> None:
    """Fuera del alcance o dentro de una ruta protegida: violación, con la ruta normalizada."""
    contract = _derive()
    assert contract.authorized_scope == BASE_SCOPE

    assert scope_violations(contract, ()) == ()
    # Igualdad exacta, prefijo de directorio y normalización canónica (separadores ``\`` incluidos).
    assert scope_violations(contract, ("src/a.py", "SRC\\A.PY", "  ./src/a.py  ")) == ()
    assert scope_violations(contract, ("src/modulo/hoja.py", "src\\modulo\\otra\\hoja.py")) == ()
    # Fuera de alcance: se devuelve normalizado, en orden declarado y sin repetir.
    assert scope_violations(contract, ("otro\\paquete\\modulo.py",)) == ("otro/paquete/modulo.py",)
    assert scope_violations(
        contract, ("otro/x.py", "OTRO\\X.PY", "", "   ")
    ) == ("otro/x.py",)
    # El prefijo se compara por directorio: ``src/modulo`` no contiene ``src/modulo_otro.py``.
    assert scope_violations(contract, ("src/modulo_otro.py",)) == ("src/modulo_otro.py",)

    # Una ruta protegida es violación **aunque el plan la haya autorizado**.
    expuesto = _derive(
        plan=_plan(
            _task("A", criteria=("criterio",), allowed_files=(CONSTITUTIONAL_PATH, "src/a.py"))
        )
    )
    assert CONSTITUTIONAL_PATH in expuesto.authorized_scope
    assert CONSTITUTIONAL_PATH in expuesto.protected_paths
    assert scope_violations(expuesto, (CONSTITUTIONAL_PATH, "config\\permissions.yaml")) == (
        CONSTITUTIONAL_PATH,
    )
    # El piso reconoce la ruta declarada con cualquier prefijo.
    assert scope_violations(expuesto, ("src/config/permissions.yaml",)) == (
        "src/config/permissions.yaml",
    )


# ---------------------------------------------------------------------------
# 8. Cobertura de criterios
# ---------------------------------------------------------------------------
def test_criterion_coverage_detecta_el_criterio_perdido() -> None:
    """La cobertura se mide por identidad de criterio, y el que falta se nombra."""
    contract = _derive()
    todos = contract.acceptance_criterion_ids

    assert criterion_coverage(contract, todos) == ()
    assert criterion_coverage(contract, ()) == todos
    assert criterion_coverage(contract, ("AC-1", "AC-3")) == ("AC-2",)
    assert criterion_coverage(contract, (" AC-2 ",)) == ("AC-1", "AC-3")
    # El enunciado no es la identidad: citar el texto no cubre el criterio.
    assert criterion_coverage(contract, ("criterio uno",)) == todos


# ---------------------------------------------------------------------------
# 9. Contrato irresoluble
# ---------------------------------------------------------------------------
def test_resolve_contract_falla_si_el_contrato_falta_o_no_valida(tmp_path: Path) -> None:
    """Corrupción y ausencia no se degradan a ``None``: fallan con ``ProjectContractError``."""
    store = FileArtifactStore(tmp_path)
    run_id = project_run_id_for(_request())

    ausente = _reference(
        kind=PROJECT_CONTRACT_KIND,
        reference=f"{run_id}/PLANNER-0-{PROJECT_CONTRACT_KIND}-9.bin",
    )
    with pytest.raises(ProjectContractError, match="no se pudo recuperar"):
        resolve_contract(store, ausente)

    ilegible = store.put(
        workflow_id=run_id,
        role=RoleName.PLANNER,
        step_index=0,
        kind=PROJECT_CONTRACT_KIND,
        label="contrato ilegible",
        data=b"{esto no es json",
    )
    with pytest.raises(ProjectContractError, match="JSON"):
        resolve_contract(store, ilegible)

    no_utf8 = store.put(
        workflow_id=run_id,
        role=RoleName.PLANNER,
        step_index=0,
        kind=PROJECT_CONTRACT_KIND,
        label="contrato no utf-8",
        data=b"\xff\xfe{",
    )
    with pytest.raises(ProjectContractError, match="UTF-8"):
        resolve_contract(store, no_utf8)

    invalido = store.put(
        workflow_id=run_id,
        role=RoleName.PLANNER,
        step_index=0,
        kind=PROJECT_CONTRACT_KIND,
        label="contrato inválido",
        data=b'{"original_goal": 7}',
    )
    with pytest.raises(ProjectContractError, match="no valida"):
        resolve_contract(store, invalido)

    # Un artefacto manipulado (digest que no cuadra) tampoco se devuelve como si fuera el contrato.
    bueno = publish_contract(store, request=_request(), contract=_derive())
    manipulado = bueno.model_copy(update={"digest": "f" * 64})
    with pytest.raises(ProjectContractError):
        resolve_contract(store, manipulado)
