"""La autonomía exige prueba positiva de tacticidad (ENGINE-6.3.R2, AUD-6.3R1-01).

La auditoría de cierre reprodujo cuatro propuestas que **nombraban** una dimensión de arquitectura y
obtenían autonomía porque no usaban un verbo de sustitución del vocabulario:

    Use NATS as the message bus
    Point the application at another database engine
    Improve persistence layer for scalability
    Routine cleanup of the data layer

Terminaban en ``NO_SEMANTIC_SUSPICION`` → ``CONTAINED`` → ``COMPLETED``. La carga de la prueba
estaba al revés: bastaba con que el motor **no** reconociera una señal para conceder.

Desde ENGINE-6.3.R2 nombrar una dimensión obliga a **demostrar** que la estrategia sigue siendo un
trabajo acotado sobre lo existente (reparación o reintento acotado, o preservación explícita del
diseño autorizado). Lo que no se puede demostrar es ``UNKNOWN_OR_AMBIGUOUS`` y va al Human Gate; lo
genuinamente táctico sigue siendo autónomo, porque el objetivo es conservar autonomía segura, no
convertir el motor en un muro.

No hay proveedor real, ni red, ni reloj, ni azar.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from punto.project.replan_change import ReplanChangeClass, classify_replan_change, tactical_proof
from punto.schemas.project import ProjectState
from test_project_replan_escalation_and_binding import (
    ReplanTextReplanner,
    contenido,
    montaje,
)

#: Las cuatro reproducciones del hallazgo, literales, más la variante de proveedor cloud.
SIN_PRUEBA: tuple[tuple[str, str], ...] = (
    ("NATS", "Use NATS as the message bus"),
    ("otra-db", "Point the application at another database engine"),
    ("persistencia", "Improve persistence layer for scalability"),
    ("capa-de-datos", "Routine cleanup of the data layer"),
    ("cloud", "Ship the service on a new cloud provider"),
)

#: Formulaciones que **no** están en ningún vocabulario del motor: la frontera no depende de conocer
#: el nombre de la tecnología futura.
NUEVAS: tuple[tuple[str, str], ...] = (
    ("cache-distribuida", "Swap the in-memory cache for a distributed one"),
    ("backend-persistencia", "Introduce a new persistence backend for the orders module"),
    ("proveedor-auth", "Use a new authentication provider for the service"),
    ("cola-nueva", "Introduce a new queue infrastructure for the background jobs"),
    ("almacenamiento", "Replace the storage subsystem with a managed one"),
    ("workflow-temporal", "Adopt Temporal for workflow orchestration"),
    ("datastore-planetscale", "Move the primary datastore to PlanetScale"),
    ("datastore-cockroach", "Replace the current relational engine with CockroachDB"),
    ("deploy-fly", "Move the service from Vercel to Fly.io"),
    ("identidad-clerk", "Replace the current identity service with Clerk"),
)

#: Disfraces: el adjetivo no convierte un cambio de diseño en táctico.
DISFRAZ: tuple[tuple[str, str], ...] = (
    ("cleanup-auth", "Small configuration cleanup: migrate authentication to a new provider."),
    ("minor-db", "Minor maintenance: replace the production database."),
    ("tiny-deploy", "Tiny refactor: move deployment from the current platform to Fly.io."),
    ("cleanup-capa", "Routine cleanup of the data layer"),
)

#: Controles positivos: trabajo acotado sobre lo existente, con o sin dimensión nombrada.
TACTICOS: tuple[tuple[str, str], ...] = (
    ("algoritmo", "cambiar el algoritmo interno de ordenación del nodo A"),
    ("consulta", "reparar la consulta existente del nodo A contra postgres"),
    ("callback-auth", "reparar el callback de autenticación existente del nodo A"),
    ("reintento", "reintentar el nodo B con la estrategia tecnica corregida"),
    ("prerrequisito", "preparar el prerrequisito tecnico que el nodo B necesita"),
    (
        "preservacion",
        "retry node B with an alternative implementation strategy while preserving its contract, "
        "architecture, scope and acceptance criteria",
    ),
)

SIN_PRUEBA_IDS = [caso[0] for caso in SIN_PRUEBA + NUEVAS + DISFRAZ]
TACTICOS_IDS = [caso[0] for caso in TACTICOS]


@pytest.mark.parametrize(("etiqueta", "texto"), SIN_PRUEBA + NUEVAS + DISFRAZ, ids=SIN_PRUEBA_IDS)
def test_lo_que_no_se_demuestra_no_obtiene_autonomia(
    tmp_path: Path, etiqueta: str, texto: str
) -> None:
    """Sin prueba positiva, la propuesta no es táctica y el proyecto espera a una persona.

    Se mide con el kernel real: clasificación, contención y disposición final. La comprobación clave
    no es «escala» sino que **no** exista el estado que concedía autonomía por defecto
    (``NO_SEMANTIC_SUSPICION`` con ``allows_autonomous``).
    """
    escenario = tmp_path / etiqueta
    escenario.mkdir()
    replanner = ReplanTextReplanner(objective=texto)
    montado = montaje(escenario, replanner=replanner)
    run = montado.kernel.run_all(montado.harness.request)
    propuesta = replanner.proposals[0]
    clasificacion = classify_replan_change(
        propuesta, contract=montado.contract, action="modify_file"
    )
    veredicto = contenido(montado, propuesta)
    diagnostico = (
        f"{etiqueta}: clase={clasificacion.change_class.value} "
        f"dimensiones={clasificacion.dimensions} detalle={clasificacion.detail}"
    )

    assert not (not clasificacion.escalates and veredicto.allows_autonomous), diagnostico
    assert clasificacion.escalates is True, diagnostico
    assert run.status is ProjectState.HUMAN_APPROVAL, (diagnostico, run.status, run.failure_code)
    assert run.active_generation is not None, "no se adopta ninguna generación"
    assert run.active_generation.generation_index == 0
    assert run.active_replan_approval is not None
    assert run.active_replan_approval.authorized is False
    assert len(replanner.calls) == 1, "una sola invocación al replanner"


@pytest.mark.parametrize(("etiqueta", "texto"), TACTICOS, ids=TACTICOS_IDS)
def test_lo_demostrado_sigue_siendo_autonomo(tmp_path: Path, etiqueta: str, texto: str) -> None:
    """El trabajo acotado sobre lo existente conserva la autonomía: la barrera no es un muro.

    El nodo nuevo escribe el mismo archivo, conserva el mismo criterio, el mismo riesgo y la misma
    autoridad, y no pide ningún recurso nuevo: T1-T7 se demuestran y el proyecto adopta y cierra.
    """
    escenario = tmp_path / etiqueta
    escenario.mkdir()
    replanner = ReplanTextReplanner(objective=texto)
    montado = montaje(escenario, replanner=replanner)
    run = montado.kernel.run_all(montado.harness.request)
    clasificacion = classify_replan_change(
        replanner.proposals[0], contract=montado.contract, action="modify_file"
    )

    assert clasificacion.change_class is ReplanChangeClass.NO_SEMANTIC_SUSPICION, (
        clasificacion.detail
    )
    assert clasificacion.escalates is False
    assert run.status is ProjectState.COMPLETED, (run.status, run.failure_code)
    assert run.active_generation is not None
    assert run.active_generation.generation_index == 1
    assert run.usage.replans_accepted == 1


def test_la_prueba_positiva_es_explicita_y_vacia_por_defecto() -> None:
    """La prueba positiva se enumera; un texto sin ella devuelve la tupla vacía, no un «sí»."""
    assert tactical_proof("reparar el callback de autenticación existente") != ()
    assert tactical_proof("preserving the architecture of the project") != ()
    assert tactical_proof("Use NATS as the message bus") == ()
    assert tactical_proof("Improve persistence layer for scalability") == ()
