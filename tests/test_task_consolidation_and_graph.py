"""Consolidación general de Tasks + trazabilidad al grafo estructural.

Cubre, con el motor real (nada específico de un proyecto):

- deduplicación segura ante concurrencia por destino + objetivo normalizado + alcance + criterios;
- consolidación (linaje ``SUPERSEDED``) de duplicados históricos, sin perder su historial;
- aislamiento: un fixture nunca puede alterar la identidad canónica de un destino real, y una Task
  cuya identidad ya no es la vigente del destino sale del flujo operativo (``identity_mismatch``);
- reintento (``rerun``) de la misma Task frente a continuación de una Task canónica por una
  solicitud equivalente;
- el tablero operativo excluye lo superado, y el grafo reconstruye
  target → task → attempts → artifact → publication.

    pytest tests/test_task_consolidation_and_graph.py -q
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from punto.api.task_identity import (
    canonical_rank,
    criteria_compatible,
    normalize_tokens,
    objectives_equivalent,
    scopes_compatible,
)
from punto.common import utc_now
from punto.workspace.target import DevelopmentTarget
from test_console_state import _reiniciar
from test_human_console import _app, _cambio, _git, _plan, _repos, _target

TARGET_A = "fixture-target"
TARGET_B = "otro-fixture-target"
OBJETIVO = "unificar la lista de tipos en una sola fuente"
OBJETIVO_EQUIVALENTE = "unificar los tipos en una sola fuente"
OBJETIVO_DISTINTO = "migrar el formulario de contacto a la nueva API de correo"


def _solicitud(target_id: str = TARGET_A, objetivo: str = OBJETIVO, **extra: Any) -> dict[str, Any]:
    return {
        "objective": objetivo,
        "target_id": target_id,
        "acceptance_criteria": ["una sola fuente de tipos"],
        "scope_paths": ["src"],
        **extra,
    }


def _montaje(tmp_path: Path, *, respuestas: list[Any], target_id: str = TARGET_A) -> Any:
    repo, remoto = _repos(tmp_path)
    destino = replace(_target(repo, remoto=remoto), target_id=target_id)
    return _app(target=destino, respuestas=respuestas, targets={target_id: destino})


# ============================== A · concurrencia: dos solicitudes equivalentes ⇒ una canónica
def test_a_dos_solicitudes_equivalentes_simultaneas_producen_una_sola_task(tmp_path: Path) -> None:
    client, _audit, _t, _d = _montaje(tmp_path, respuestas=[_plan(), _cambio()] * 8)

    with ThreadPoolExecutor(max_workers=8) as pool:
        respuestas = list(
            pool.map(lambda _i: client.post("/console/tasks", json=_solicitud(run=False)), range(8))
        )

    identificadores = {item.json()["task_id"] for item in respuestas}
    assert len(identificadores) == 1, "una sola Task canónica para el mismo trabajo"
    assert {item.status_code for item in respuestas} <= {200, 201}
    assert sum(1 for item in respuestas if item.status_code == 201) == 1
    listado = client.get("/console/tasks").json()
    assert listado["total"] == 1 and len(listado["operational"]) == 1


def test_a2_equivalencia_por_texto_parecido_no_por_igualdad_literal() -> None:
    assert objectives_equivalent(OBJETIVO, OBJETIVO_EQUIVALENTE)
    assert not objectives_equivalent(OBJETIVO, OBJETIVO_DISTINTO)
    assert normalize_tokens("Unificar los Tipos") == normalize_tokens("unificar tipo")
    assert scopes_compatible(["src/lib"], ["src/lib/tipos.ts"])
    assert not scopes_compatible(["src/lib"], ["docs/readme.md"])
    assert criteria_compatible([], ["una sola fuente de tipos"]), "sin criterios no bloquea"
    assert not criteria_compatible(["una sola fuente de tipos"], ["borra la tabla de usuarios"])


# ============================== B · duplicado histórico → SUPERSEDED, historial intacto
def _inyectar_task_historica(objetivo: str, *, stage: str = "QUEUED") -> UUID:
    """Escribe directamente un ``TaskRecord`` en el estado durable (como si fuera anterior a la
    deduplicación por creación: dos Tasks activas y equivalentes, sin relación entre ellas)."""
    from punto.api.console_state import ConsoleStateStore, TaskRecord, default_console_state_path

    store = ConsoleStateStore(default_console_state_path())
    existing = store.load().tasks if default_console_state_path().is_file() else ()
    task_id = uuid4()
    now = utc_now()
    record = TaskRecord(
        task_id=task_id,
        objective=objetivo,
        target_id=TARGET_A,
        acceptance_criteria=("una sola fuente de tipos",),
        scope_paths=("src",),
        stage=stage,
        created_at=now,
        updated_at=now,
    )
    store.save(tasks=(*existing, record), gates=())
    return task_id


def test_b_reinicio_consolida_duplicados_historicos_reales(tmp_path: Path) -> None:
    repo, remoto = _repos(tmp_path)
    destino = replace(_target(repo, remoto=remoto), target_id=TARGET_A)
    import os

    os.environ["PUNTO_CONSOLE_STATE_PATH"] = str(tmp_path / "console-state.json")
    primera = _inyectar_task_historica(OBJETIVO)
    segunda = _inyectar_task_historica(OBJETIVO_EQUIVALENTE)
    assert primera != segunda

    reiniciado, _audit2 = _reiniciar(destino)
    primera, segunda = {"task_id": str(primera)}, {"task_id": str(segunda)}

    vista_primera = reiniciado.get(f"/console/tasks/{primera['task_id']}").json()
    vista_segunda = reiniciado.get(f"/console/tasks/{segunda['task_id']}").json()
    canonicas = {vista_primera["lineage"]["status"], vista_segunda["lineage"]["status"]}
    assert canonicas == {"ACTIVE", "SUPERSEDED"}
    superada = (
        vista_primera if vista_primera["lineage"]["status"] == "SUPERSEDED" else vista_segunda
    )
    canonica = vista_segunda if superada is vista_primera else vista_primera
    assert superada["lineage"]["superseded_by"] == canonica["task_id"]
    assert superada["lineage"]["supersession_cause"] == "duplicate_objective"
    assert superada["objective"], "el historial (objetivo, criterios) de la superada persiste"
    assert any(
        rel["kind"] == "duplicate_of" and rel["task_id"] == canonica["task_id"]
        for rel in superada["lineage"]["relations"]
    )
    assert any(
        rel["kind"] == "supersedes" and rel["task_id"] == superada["task_id"]
        for rel in canonica["lineage"]["relations"]
    )
    assert reiniciado.get("/console/tasks").json()["total"] == 2, "nada se borra"
    assert len(reiniciado.get("/console/tasks").json()["operational"]) == 1


# ============================== C · fixture no puede alterar el branch de un target real
def test_c_un_fixture_con_otra_rama_no_contamina_la_identidad_del_destino_real(
    tmp_path: Path,
) -> None:
    repo, remoto = _repos(tmp_path)
    canonico = replace(_target(repo, remoto=remoto), target_id=TARGET_A, work_branch="ai/real")
    client, _audit, _t, deps = _app(
        target=canonico, respuestas=[_plan(), _cambio()], targets={TARGET_A: canonico}
    )
    tarea = client.post("/console/tasks", json=_solicitud()).json()
    assert tarea["stage"] == "DEVELOPMENT_COMPLETED"

    # Alguien reconfigura el destino con la rama de un fixture/scratchpad de pruebas.
    contaminado = replace(canonico, work_branch="ai/console-fixture")
    deps.targets = {TARGET_A: contaminado}
    deps.dev_cycle.targets.targets = {TARGET_A: contaminado}  # type: ignore[attr-defined]

    reintento = client.post(f"/console/tasks/{tarea['task_id']}/run")

    assert reintento.status_code == 409
    assert "identidad" in reintento.json()["detail"]
    vista = client.get(f"/console/tasks/{tarea['task_id']}").json()
    assert vista["stage"] == "DEVELOPMENT_COMPLETED", "no se corrompió el estado ya alcanzado"


def test_c2_la_identidad_no_depende_de_la_ruta_absoluta_sino_del_proyecto(tmp_path: Path) -> None:
    """Mover el checkout (ruta distinta, mismo nombre de carpeta) no rompe la identidad."""
    repo, remoto = _repos(tmp_path)
    original = replace(_target(repo, remoto=remoto), target_id=TARGET_A)
    movido = tmp_path / "otra_ubicacion" / repo.name
    movido.parent.mkdir(parents=True)
    repo.rename(movido)
    reubicado = replace(original, repository=movido)

    assert reubicado.identity.repository == original.identity.repository, "mismo nombre de proyecto"


# ============================== D · restart conserva relaciones
def test_d_el_reinicio_conserva_las_relaciones_supersedes_y_duplicate_of(tmp_path: Path) -> None:
    repo, remoto = _repos(tmp_path)
    destino = replace(_target(repo, remoto=remoto), target_id=TARGET_A)
    client, _audit, _t, _d = _app(
        target=destino, respuestas=[_plan(), _cambio()], targets={TARGET_A: destino}
    )
    primera = client.post("/console/tasks", json=_solicitud(run=False)).json()
    client.post("/console/tasks", json=_solicitud(objetivo=OBJETIVO_EQUIVALENTE, run=False)).json()
    reiniciado_1, _a1 = _reiniciar(destino)
    antes = reiniciado_1.get(f"/console/tasks/{primera['task_id']}").json()["lineage"]

    reiniciado_2, _a2 = _reiniciar(destino)
    despues = reiniciado_2.get(f"/console/tasks/{primera['task_id']}").json()["lineage"]

    assert antes == despues, "un segundo reinicio no cambia ni duplica las relaciones"


# ============================== E · targets diferentes con el mismo objetivo NO se deduplican
def test_e_targets_distintos_con_el_mismo_objetivo_no_se_deduplican(tmp_path: Path) -> None:
    repo_a, remoto_a = _repos(tmp_path / "a")
    repo_b, remoto_b = _repos(tmp_path / "b")
    destino_a = replace(_target(repo_a, remoto=remoto_a), target_id=TARGET_A)
    destino_b = replace(_target(repo_b, remoto=remoto_b), target_id=TARGET_B)
    client, _audit, _t, _d = _app(
        target=destino_a,
        respuestas=[_plan(), _cambio(), _plan(), _cambio()],
        targets={TARGET_A: destino_a, TARGET_B: destino_b},
    )

    de_a = client.post("/console/tasks", json=_solicitud(target_id=TARGET_A)).json()
    de_b = client.post("/console/tasks", json=_solicitud(target_id=TARGET_B)).json()

    assert de_a["task_id"] != de_b["task_id"]
    assert de_a.get("deduplicated") is not True and de_b.get("deduplicated") is not True
    listado = client.get("/console/tasks").json()
    assert len(listado["operational"]) == 2


# ============================== F · rerun continúa la misma Task
def test_f_una_solicitud_equivalente_continua_la_task_canonica_con_rerun(tmp_path: Path) -> None:
    repo, remoto = _repos(tmp_path)
    baseline_real = _git(repo, "rev-parse", "HEAD")
    # El primer intento falla cerrado por baseline discordante (nada que ver con la identidad):
    # DEVELOPMENT_FAILED, sin gate.
    roto = replace(_target(repo, remoto=remoto), target_id=TARGET_A, baseline_sha="0" * 40)
    client, _audit, _t, deps = _app(
        target=roto, respuestas=[_plan(), _cambio()], targets={TARGET_A: roto}
    )
    primera = client.post("/console/tasks", json=_solicitud()).json()
    assert primera["stage"] == "DEVELOPMENT_FAILED"

    # La configuración se corrige (el baseline vigente vuelve a ser el real del repositorio).
    corregido = replace(roto, baseline_sha=baseline_real)
    deps.targets = {TARGET_A: corregido}
    deps.dev_cycle.targets.targets = {TARGET_A: corregido}  # type: ignore[attr-defined]

    continuada = client.post(
        "/console/tasks", json=_solicitud(objetivo=OBJETIVO_EQUIVALENTE)
    ).json()

    assert continuada.get("deduplicated") is True
    assert continuada["task_id"] == primera["task_id"], "misma Task, no una nueva"
    assert continuada.get("continuation_started") is True
    assert continuada["stage"] == "DEVELOPMENT_COMPLETED"
    assert [item["origin"] for item in continuada["attempts"]] == ["initial", "continuation"]
    assert client.get("/console/tasks").json()["total"] == 1, "no se creó una segunda Task"


def test_f2_una_task_completada_no_se_reejecuta_por_una_solicitud_equivalente(
    tmp_path: Path,
) -> None:
    client, _audit, _t, _d = _montaje(tmp_path, respuestas=[_plan(), _cambio()])
    completada = client.post("/console/tasks", json=_solicitud()).json()
    assert completada["stage"] == "DEVELOPMENT_COMPLETED"

    repetida = client.post("/console/tasks", json=_solicitud(objetivo=OBJETIVO_EQUIVALENTE)).json()

    assert repetida["task_id"] == completada["task_id"]
    assert repetida.get("continuation_started") is not True
    assert len(repetida["attempts"]) == 1, "no se repitió el ciclo sobre un trabajo ya completado"


# ============================== G · dashboard operativo sin obsoletos
def test_g_el_tablero_operativo_no_muestra_superadas_ni_gates_obsoletos(tmp_path: Path) -> None:
    repo, remoto = _repos(tmp_path)
    destino = replace(_target(repo, remoto=remoto), target_id=TARGET_A)
    import os

    os.environ["PUNTO_CONSOLE_STATE_PATH"] = str(tmp_path / "console-state.json")
    _inyectar_task_historica(OBJETIVO)
    _inyectar_task_historica(OBJETIVO_EQUIVALENTE)
    reiniciado, _a = _reiniciar(destino)

    listado = reiniciado.get("/console/tasks").json()

    assert len(listado["operational"]) == 1
    assert all(item["lineage"]["status"] == "ACTIVE" for item in listado["operational"])
    assert any(item["lineage"]["status"] == "SUPERSEDED" for item in listado["history"])
    assert listado["total"] == len(listado["operational"]) + len(listado["history"])


# ============================== H · grafo: target -> task -> attempts -> artifact -> publication
def test_h_el_grafo_reconstruye_la_cadena_completa(tmp_path: Path) -> None:
    repo, remoto = _repos(tmp_path)
    destino = replace(
        _target(repo, remoto=remoto),
        target_id=TARGET_A,
        production_branch="main",
        production_url="https://example.local/",
    )
    client, _audit, _t, deps = _app(
        target=destino, respuestas=[_plan(), _cambio()], targets={TARGET_A: destino}
    )
    from test_human_console import _autoridad_completa

    deps.targets = {TARGET_A: replace(destino, authority=_autoridad_completa())}
    deps.dev_cycle.targets.targets = dict(deps.targets)  # type: ignore[attr-defined]
    tarea = client.post("/console/tasks", json=_solicitud()).json()
    assert tarea["stage"] == "PRODUCTION_VALIDATED", tarea

    grafo = client.get(f"/console/graph?task_id={tarea['task_id']}").json()

    from punto.api.task_graph import trace_task

    rastro = trace_task(grafo, tarea["task_id"])
    assert rastro["target"] == TARGET_A
    assert len(rastro["attempts"]) == 1
    assert rastro["artifacts"] and rastro["published_artifacts"]
    kinds = grafo["summary"]["kinds"]
    for esperado in (
        "target",
        "task",
        "attempt",
        "plan",
        "verification",
        "artifact",
        "publication",
        "branch",
    ):
        assert kinds.get(esperado, 0) >= 1, (esperado, kinds)
    relations = {edge["relation"] for edge in grafo["edges"]}
    assert {
        "has_task",
        "has_attempt",
        "produced_artifact",
        "published_as",
        "to_branch",
    } <= relations


def test_h2_el_grafo_incluye_supersedes_y_duplicate_of(tmp_path: Path) -> None:
    repo, remoto = _repos(tmp_path)
    destino = replace(_target(repo, remoto=remoto), target_id=TARGET_A)
    import os

    os.environ["PUNTO_CONSOLE_STATE_PATH"] = str(tmp_path / "console-state.json")
    _inyectar_task_historica(OBJETIVO)
    _inyectar_task_historica(OBJETIVO_EQUIVALENTE)
    reiniciado, _a = _reiniciar(destino)

    grafo = reiniciado.get(f"/console/graph?target_id={TARGET_A}").json()

    relations = {edge["relation"] for edge in grafo["edges"]}
    assert {"supersedes", "superseded_by", "duplicate_of"} <= relations


# ============================== I · mutación que elimine aislamiento/deduplicación falla
def test_i_sin_comprobar_identidad_una_task_de_otra_rama_seguiria_operativa(tmp_path: Path) -> None:
    """Documenta la invariante C con la función pura (para que una regresión la haga fallar)."""
    from punto.api.task_identity import identity_conflict

    repo, remoto = _repos(tmp_path)
    destino = replace(_target(repo, remoto=remoto), target_id=TARGET_A, work_branch="ai/canonica")

    motivo = identity_conflict(
        fingerprint="",
        work_branch="",
        production_branch="",
        result_branch="ai/console-fixture",
        target=destino,
    )

    assert motivo, "una Task producida en otra rama tiene que marcar conflicto de identidad"


def test_i2_sin_deduplicar_dos_solicitudes_equivalentes_crearian_dos_tasks(tmp_path: Path) -> None:
    """Documenta la invariante A con la función pura de equivalencia."""
    from types import SimpleNamespace

    from punto.api.task_identity import find_equivalents

    existente = SimpleNamespace(
        task_id="1",
        target_id=TARGET_A,
        objective=OBJETIVO,
        scope_paths=["src"],
        acceptance_criteria=["una sola fuente de tipos"],
        stage="QUEUED",
        lineage_status="ACTIVE",
    )
    candidata = SimpleNamespace(
        task_id="2",
        target_id=TARGET_A,
        objective=OBJETIVO_EQUIVALENTE,
        scope_paths=["src"],
        acceptance_criteria=["una sola fuente de tipos"],
        stage="QUEUED",
        lineage_status="ACTIVE",
    )

    assert find_equivalents(candidata, [existente, candidata]) == (existente,)


def test_i3_canonical_rank_prefiere_lo_mas_avanzado_y_con_mas_evidencia() -> None:
    from datetime import datetime
    from types import SimpleNamespace

    base = datetime(2026, 1, 1, tzinfo=UTC)
    pobre = SimpleNamespace(
        stage="QUEUED", attempts=[1], result=None, gates=[], created_at=base, updated_at=base
    )
    rico = SimpleNamespace(
        stage="DEVELOPMENT_COMPLETED",
        attempts=[1, 2],
        result=SimpleNamespace(plan=object(), verification=(1,)),
        gates=["g"],
        created_at=base,
        updated_at=base,
    )

    assert canonical_rank(rico) > canonical_rank(pobre)


_ = DevelopmentTarget
