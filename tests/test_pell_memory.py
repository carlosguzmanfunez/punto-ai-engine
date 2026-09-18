"""PELL-0 — memoria práctica de experiencia: fracasos, éxitos, recuperación y consolidación.

La demostración completa es la cadena que pide la fase:

```
problema -> intento -> fallo -> causa -> corrección -> éxito verificado -> procedimiento guardado
   -> memoria persistente -> problema similar futuro -> consulta -> conocimiento recuperado
```

Se fija con la memoria real (JSONL en un directorio temporal), con las experiencias reales de esta
fase —AUD-T-01 en `go.mod` y el cierre de F633— para que el conocimiento que se prueba sea el que de
verdad se quiere recordar.

No hay proveedor real, ni red, ni reloj, ni azar.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from punto.memory import (
    EXPERIENCE_SCHEMA_VERSION,
    ExperienceMemory,
    ExperienceResult,
    ExperienceSchemaError,
    ExperienceSecretError,
    ExperienceStatus,
    ExperienceStore,
    problem_fingerprint,
)

GO_PROBLEM = "go.mod con require de una línea evade ResourceSet"
GO_SOLUTION = "parsear require <module> <version> y añadir defensa para imports Go"
GO_PROCEDURE = (
    "identificar el manifiesto afectado",
    "reproducir ambas variantes sintácticas",
    "comprobar extracción estructural",
    "comprobar fail-closed",
    "ejecutar regresión E2E",
    "ejecutar control positivo",
)
GO_TAGS = ("go", "go.mod", "resources", "containment", "parser")


def _store(tmp_path: Path, nombre: str = "memoria.jsonl") -> ExperienceStore:
    """Memoria local en un fichero temporal (sobrevive a re-instanciar el store)."""
    return ExperienceStore(tmp_path / nombre)


def _guardar_go(store: ExperienceStore, *, verified: bool = True) -> ExperienceMemory:
    """Registra la experiencia de AUD-T-01: primero como candidata y luego verificada."""
    candidata = store.record(
        problem=GO_PROBLEM,
        context="contención de recursos en proyectos Go",
        attempts=(
            "la auditoría inicial no detectó la variante de una línea",
            "el parser funcionaba para require en bloque pero no inline",
        ),
        failure_reason=(
            "el parser esperaba que parts[0] fuese el módulo; en sintaxis inline parts[0] "
            "es require"
        ),
        solution=GO_SOLUTION,
        procedure=GO_PROCEDURE,
        tags=GO_TAGS,
        status=ExperienceStatus.CANDIDATE,
    )
    if not verified:
        return candidata
    return store.verify(
        candidata.id,
        evidence=(
            "test de la forma de una línea",
            "test de la forma de bloque",
            "E2E: dependencia no autorizada termina en architecture violation",
            "control positivo: dependencia autorizada continúa",
        ),
    )


# ---------------------------------------------------------------------------
# 1-2. Guardar fracaso y guardar éxito
# ---------------------------------------------------------------------------
def test_guarda_un_fracaso_con_su_causa(tmp_path: Path) -> None:
    """Un fracaso se conserva con el motivo por el que no funcionó."""
    store = _store(tmp_path)
    fallida = store.record(
        problem="cerrar F633 usando clasificación semántica amplia",
        context="clasificador de cambios de replanificación",
        attempts=("ampliar el vocabulario de verbos", "añadir marcas de producto conocidas"),
        failure_reason="las paráfrasis arquitectónicas seguían clasificándose como tácticas",
        result=ExperienceResult.FAILED,
        status=ExperienceStatus.FAILED,
        tags=("f633", "clasificador", "contención"),
    )

    assert fallida.status is ExperienceStatus.FAILED
    assert fallida.failure_reason
    assert store.get(fallida.id) == fallida


def test_verificar_exige_evidencia(tmp_path: Path) -> None:
    """Una experiencia no pasa a VERIFIED porque alguien diga que funcionó."""
    store = _store(tmp_path)
    candidata = store.record(problem=GO_PROBLEM, tags=GO_TAGS)
    assert candidata.status is ExperienceStatus.CANDIDATE

    with pytest.raises(ExperienceSchemaError):
        store.verify(candidata.id, evidence=())
    with pytest.raises(ValueError):
        ExperienceMemory(problem="x", status=ExperienceStatus.VERIFIED)

    verificada = store.verify(candidata.id, evidence=("test inline",))
    assert verificada.status is ExperienceStatus.VERIFIED
    assert verificada.status.reusable is True
    assert store.get(candidata.id).status is ExperienceStatus.VERIFIED  # type: ignore[union-attr]


def test_marcar_fracaso_exige_causa(tmp_path: Path) -> None:
    """Un fracaso sin motivo no enseña nada: la API lo rechaza."""
    store = _store(tmp_path)
    candidata = store.record(problem="otro problema", tags=("x",))

    with pytest.raises(ExperienceSchemaError):
        store.mark_failed(candidata.id, reason="   ")

    fallida = store.mark_failed(candidata.id, reason="el enfoque no cerraba la fuga")
    assert fallida.status is ExperienceStatus.FAILED
    assert fallida.result is ExperienceResult.FAILED


# ---------------------------------------------------------------------------
# 3. Persistencia tras reiniciar el store
# ---------------------------------------------------------------------------
def test_la_memoria_sobrevive_al_reinicio(tmp_path: Path) -> None:
    """El conocimiento sigue ahí cuando se vuelve a abrir la memoria."""
    primero = _store(tmp_path)
    verificada = _guardar_go(primero)

    segundo = ExperienceStore(primero.path)
    recuperada = segundo.get(verificada.id)

    assert recuperada is not None
    assert recuperada.status is ExperienceStatus.VERIFIED
    assert recuperada.procedure == GO_PROCEDURE
    assert recuperada.verification


# ---------------------------------------------------------------------------
# 4-6. Búsqueda: relevancia, VERIFIED primero, FAILED recuperable
# ---------------------------------------------------------------------------
def test_la_busqueda_recupera_conocimiento_relevante(tmp_path: Path) -> None:
    """Una consulta nueva sobre dependencias Go encuentra la experiencia de `go.mod`."""
    store = _store(tmp_path)
    _guardar_go(store)

    resultados = store.search("dependencia Go no detectada")

    assert resultados, "la consulta tiene que recuperar la experiencia de go.mod"
    assert resultados[0].problem == GO_PROBLEM
    assert resultados[0].status is ExperienceStatus.VERIFIED


def test_verified_va_antes_que_failed_y_failed_se_recupera(tmp_path: Path) -> None:
    """El conocimiento demostrado va primero; el fracaso sigue apareciendo para no repetirlo."""
    store = _store(tmp_path)
    store.record(
        problem=GO_PROBLEM,
        failure_reason="se intentó ignorar la forma de una línea",
        result=ExperienceResult.FAILED,
        status=ExperienceStatus.FAILED,
        tags=GO_TAGS,
    )
    _guardar_go(store)

    resultados = store.search("containment de dependencias en go.mod", limit=5)

    estados = [experiencia.status for experiencia in resultados]
    assert ExperienceStatus.VERIFIED in estados
    assert ExperienceStatus.FAILED in estados
    assert estados.index(ExperienceStatus.VERIFIED) < estados.index(ExperienceStatus.FAILED)


def test_una_consulta_ajena_no_inventa_relaciones(tmp_path: Path) -> None:
    """Sin coincidencia real no se devuelve nada: la memoria no adivina."""
    store = _store(tmp_path)
    _guardar_go(store)

    assert store.search("color del botón del panel de administración") == ()


def test_el_filtro_por_estado_limita_la_recuperacion(tmp_path: Path) -> None:
    """Se puede pedir solo conocimiento demostrado."""
    store = _store(tmp_path)
    store.record(
        problem=GO_PROBLEM,
        failure_reason="camino descartado",
        result=ExperienceResult.FAILED,
        status=ExperienceStatus.FAILED,
        tags=GO_TAGS,
    )
    _guardar_go(store)

    solo_verified = store.search("go.mod", statuses=(ExperienceStatus.VERIFIED,))

    assert solo_verified
    assert all(item.status is ExperienceStatus.VERIFIED for item in solo_verified)


# ---------------------------------------------------------------------------
# 7. Consolidación
# ---------------------------------------------------------------------------
def test_la_consolidacion_evita_duplicados_y_conserva_evidencia(tmp_path: Path) -> None:
    """Registrar dos veces lo mismo fusiona y conserva la evidencia nueva; no duplica."""
    store = _store(tmp_path)
    primera = store.record(
        problem=GO_PROBLEM,
        solution=GO_SOLUTION,
        tags=GO_TAGS,
        status=ExperienceStatus.CANDIDATE,
    )
    segunda = store.record(
        problem="go.mod   con require de una linea evade ResourceSet",
        procedure=("comprobar fail-closed", "ejecutar regresión E2E"),
        verification=("test de bloque",),
        tags=(*GO_TAGS, "fail-closed"),
        status=ExperienceStatus.CANDIDATE,
    )

    assert segunda.id == primera.id, "la misma huella consolida en el mismo registro"
    assert len(store.list()) == 1
    assert segunda.procedure == ("comprobar fail-closed", "ejecutar regresión E2E")
    assert segunda.tags[-1] == "fail-closed"


def test_exito_y_fracaso_del_mismo_problema_conviven(tmp_path: Path) -> None:
    """«Esto falló» y «esto funcionó» son dos hechos distintos y los dos se conservan."""
    store = _store(tmp_path)
    store.record(
        problem=GO_PROBLEM,
        failure_reason="ignorar la variante de una línea",
        result=ExperienceResult.FAILED,
        status=ExperienceStatus.FAILED,
        tags=GO_TAGS,
    )
    _guardar_go(store)

    assert len(store.list()) == 2
    assert {experiencia.status for experiencia in store.list()} == {
        ExperienceStatus.FAILED,
        ExperienceStatus.VERIFIED,
    }
    assert problem_fingerprint(GO_PROBLEM) == store.list()[0].fingerprint


# ---------------------------------------------------------------------------
# 8-9. Procedimiento y verificación se conservan
# ---------------------------------------------------------------------------
def test_el_procedimiento_reutilizable_se_conserva(tmp_path: Path) -> None:
    """El procedimiento es lo que permite no reconstruir la solución desde cero."""
    store = _store(tmp_path)
    verificada = _guardar_go(store)
    recuperada = ExperienceStore(store.path).get(verificada.id)

    assert recuperada is not None
    assert recuperada.procedure == GO_PROCEDURE
    assert recuperada.procedure[0] == "identificar el manifiesto afectado"


def test_la_verificacion_se_conserva(tmp_path: Path) -> None:
    """La evidencia con la que se demostró la solución viaja con la experiencia."""
    store = _store(tmp_path)
    verificada = _guardar_go(store)

    assert any("E2E" in item for item in verificada.verification)
    assert verificada.result is ExperienceResult.SUCCESS


# ---------------------------------------------------------------------------
# 10. Secretos
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "texto",
    (
        "usar la clave sk-ant-api03-TEST-CANARY-0123456789abcdef",
        "configurar DATABASE_URL=postgres://admin:s3cretpass@db.internal/core",
        "Authorization: Bearer abcdef0123456789abcdef0123456789",
        "token=9f3a7c1b5d2e8f4a6c0b9d3e7f1a5c8b",
    ),
    ids=("api-key", "dsn", "bearer", "token"),
)
def test_los_secretos_no_se_almacenan(tmp_path: Path, texto: str) -> None:
    """Ni en el problema, ni en el contexto, ni en la solución, ni en las listas."""
    store = _store(tmp_path)

    with pytest.raises(ExperienceSecretError):
        store.record(problem="probar credenciales", solution=texto)
    with pytest.raises(ExperienceSecretError):
        store.record(problem="probar credenciales", procedure=(texto,))
    with pytest.raises(ExperienceSecretError):
        store.record(problem=texto)

    assert store.list() == ()
    assert not store.path.exists() or store.path.read_text(encoding="utf-8").strip() == ""


def test_el_texto_se_acota_y_se_resume(tmp_path: Path) -> None:
    """La memoria guarda resúmenes: no es un vertedero de stdout/stderr."""
    store = _store(tmp_path)
    larga = "linea de log repetida " * 500
    guardada = store.record(problem=larga, tags=("log",))

    assert len(guardada.problem) <= 2000


# ---------------------------------------------------------------------------
# 11. Esquema desconocido
# ---------------------------------------------------------------------------
def test_un_esquema_desconocido_falla_claramente(tmp_path: Path) -> None:
    """Un registro de otra versión no se interpreta ni se ignora en silencio."""
    store = _store(tmp_path)
    store.record(problem="experiencia válida", tags=("x",))
    linea = json.loads(store.path.read_text(encoding="utf-8").splitlines()[0])
    linea["schema_version"] = EXPERIENCE_SCHEMA_VERSION + 7
    store.path.write_text(f"{json.dumps(linea)}\n", encoding="utf-8")

    with pytest.raises(ExperienceSchemaError) as error:
        store.list()
    assert "esquema de experiencia desconocido" in str(error.value)


def test_una_linea_ilegible_falla_claramente(tmp_path: Path) -> None:
    """Una memoria corrupta se declara, no se interpreta como vacía."""
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{no es json}\n", encoding="utf-8")

    with pytest.raises(ExperienceSchemaError):
        store.list()


# ---------------------------------------------------------------------------
# 12. La memoria no toca la autoridad del motor
# ---------------------------------------------------------------------------
def test_la_memoria_no_modifica_la_autoridad_del_motor(tmp_path: Path) -> None:
    """Usar la memoria no cambia el contrato durable ni el envelope de recursos del proyecto.

    Se lee el contrato **resuelto del artefacto** antes y después de un ciclo completo de memoria
    (registrar fracaso, registrar éxito verificado, buscar): si la memoria tuviera alguna influencia
    sobre la autoridad, la huella o los recursos autorizados cambiarían. No cambian.
    """
    from punto.memory import ExperienceStatus
    from punto.project.contract import resolve_contract
    from test_project_replan_escalation_and_binding import montaje
    from test_project_replan_kernel import FakeReplanner

    escenario = montaje(tmp_path / "autoridad", replanner=FakeReplanner())
    assert escenario.run.contract_ref is not None
    antes = resolve_contract(escenario.harness.artifacts, escenario.run.contract_ref)
    assert antes is not None
    huella_antes = antes.contract_fingerprint
    recursos_antes = tuple(antes.authorized_resources)
    estado_antes = escenario.run.status

    store = _store(tmp_path)
    verificada = _guardar_go(store)
    assert store.search("go.mod")[0].id == verificada.id
    store.record(
        problem="intentar ampliar capabilities desde la memoria",
        failure_reason="la memoria no tiene autoridad: no puede ampliar nada",
        result=ExperienceResult.FAILED,
        status=ExperienceStatus.FAILED,
        tags=("autoridad",),
    )
    store.mark_failed(verificada.id, reason="prueba de que marcar fracaso no toca la autoridad")

    despues = resolve_contract(escenario.harness.artifacts, escenario.run.contract_ref)
    assert despues is not None
    assert despues.contract_fingerprint == huella_antes
    assert tuple(despues.authorized_resources) == recursos_antes
    assert escenario.run.status is estado_antes


def test_la_memoria_no_importa_la_autoridad_del_motor() -> None:
    """El paquete de memoria no depende de ninguna pieza de autoridad del motor."""
    package = Path(__file__).resolve().parent.parent / "src" / "punto" / "memory"
    prohibidos = (
        "punto.policy",
        "punto.project.kernel",
        "punto.project.containment",
        "punto.project.replan_guard",
        "punto.project.replan_change",
        "punto.workflow",
    )
    for fichero in sorted(package.glob("*.py")):
        texto = fichero.read_text(encoding="utf-8")
        for modulo in prohibidos:
            assert f"import {modulo}" not in texto, (fichero.name, modulo)


def test_la_api_de_la_memoria_no_ejecuta_nada() -> None:
    """La superficie pública de PELL-0 son verbos de memoria, no de ejecución ni de autoridad."""
    from punto import memory

    publica = set(memory.__all__)
    prohibidos = {
        "execute", "run", "apply", "authorize", "approve", "grant", "escalate", "reconcile",
    }

    assert not (publica & prohibidos)
    for nombre in ("record", "search", "verify", "mark_failed", "update_status", "get", "list"):
        assert hasattr(ExperienceStore, nombre)


# ---------------------------------------------------------------------------
# 13. La cadena completa de la fase (casos A-D)
# ---------------------------------------------------------------------------
def test_cadena_completa_problema_fallo_correccion_recuperacion(tmp_path: Path) -> None:
    """A) fracaso · B) éxito verificado · C) consulta nueva · D) reinicio del store."""
    store = _store(tmp_path)

    # A) intento que no funcionó, con su causa
    store.record(
        problem="cerrar F633 ampliando el vocabulario del clasificador",
        attempts=("añadir verbos de sustitución", "añadir marcas de producto conocidas"),
        failure_reason="las paráfrasis arquitectónicas seguían pasando como tácticas",
        result=ExperienceResult.FAILED,
        status=ExperienceStatus.FAILED,
        tags=("f633", "clasificador", "paráfrasis"),
    )

    # B) corrección que sí funcionó, con procedimiento y evidencia
    exito = store.record(
        problem="cerrar F633 con prueba positiva y comprobación estructural",
        solution="enumeración positiva de tacticidad + verificación post-hoc del efecto real",
        procedure=(
            "exigir prueba positiva de tacticidad",
            "juzgar el contenido añadido del diff real",
            "fallar cerrado si no se puede demostrar",
        ),
        verification=("3154 pruebas verdes", "E2E de replanificación", "auditoría adversarial"),
        tags=("f633", "autoridad", "contención", "clasificador"),
        status=ExperienceStatus.VERIFIED,
        result=ExperienceResult.SUCCESS,
    )

    # C) problema nuevo relacionado: se recuperan el éxito, su procedimiento y el fracaso
    resultados = store.search("paráfrasis que evade el clasificador de contención")
    problemas = [item.problem for item in resultados]
    assert exito.problem in problemas
    assert any(item.status is ExperienceStatus.FAILED for item in resultados)
    recuperado = next(item for item in resultados if item.status is ExperienceStatus.VERIFIED)
    assert recuperado.procedure[0] == "exigir prueba positiva de tacticidad"

    # D) reinicio: la misma consulta sigue recuperando el conocimiento
    reiniciada = ExperienceStore(store.path)
    assert [item.id for item in reiniciada.search("paráfrasis que evade el clasificador")] == [
        item.id for item in resultados
    ]
