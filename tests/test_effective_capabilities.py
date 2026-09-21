"""Capacidades efectivas, QA y evidencia gobernada (AP000-OBS-03-R1).

El defecto que cierra esta suite: PUNTO podía exigir una verificación que necesita una capacidad
que el **transporte activo** no posee —imágenes por Claude Code, por ejemplo—, completar
construcción, verificaciones y cadena técnica, y terminar en ``EVIDENCE_REQUIRED`` como si fuera
un fallo. Lo que se fija aquí:

1. capacidad **configurada** ∩ capacidad del **transporte** ∩ disponibilidad = capacidad
   **efectiva**;
2. el QA que exige imágenes no se da por ejecutable en una ruta sin entrada de imágenes;
3. un criterio sin evidencia obtenible queda ``EVIDENCE_REQUIRED`` gobernado (nunca un falso
   ``VERIFIED``), con su causa, su capacidad ausente y qué corresponde hacer;
4. si la política prevé persona para esa evidencia, el Human Gate se crea **una** vez, correcto y
   durable, y no se duplica al reintentar;
5. el dashboard distingue lo configurado de lo efectivo.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto.acceptance import (
    ClaimKind,
    VisualCapability,
    capability_requirements,
    claims_result,
    extract_claims,
    verify_claims,
)
from punto.api.console import (
    ConsoleDependencies,
    _capability_evidence,
    register_human_console,
)
from punto.api.console_state import default_console_state_path
from punto.api.dashboard import register_dashboard
from punto.audit.logger import AuditLogger
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers.effective import (
    CAPABILITY_STRUCTURED_OUTPUT,
    CAPABILITY_TEXT,
    CAPABILITY_VISION,
    capability_routes,
    effective_capabilities_table,
    effective_capability,
    transport_capabilities,
    visual_capability_for_role,
)
from punto.providers.transport import TransportCapabilities
from punto.schemas.build import BuildRequest
from punto.schemas.dev import (
    CapabilityEvidence,
    ClaimEvidence,
    DevelopmentResult,
    DevelopmentStatus,
    PlanStatus,
)
from punto.workspace.target import DevelopmentTarget
from test_acceptance import (
    CONTENIDO_EXPLORADOR_REEMPLAZADO,
    EXPLORADOR,
    _cambio_reparacion,
    _ciclo_mapa,
    _plan_mapa,
    _repo_ciclo,
    _solicitud_mapa,
)


class _Cliente:
    """Cliente del contrato con el transporte que se quiera simular."""

    def __init__(self, *, imagenes: bool, transporte: str, detalle: str = "") -> None:
        self._capabilities = TransportCapabilities(
            supports_images=imagenes,
            supports_json_schema=True,
            detail=detalle,
        )
        self.transport = SimpleNamespace(kind=SimpleNamespace(value=transporte))

    def capabilities(self) -> TransportCapabilities:
        """Capacidades declaradas por el transporte simulado."""
        return self._capabilities


def _criterio_visual() -> tuple[str, ...]:
    """Criterio de apariencia: exige mirar el resultado, no solo que exista."""
    return ("el mapa se integra visualmente con el diseno actual",)


def _solicitud(criterios: tuple[str, ...] | None = None) -> BuildRequest:
    """Solicitud del caso con las afirmaciones indicadas."""
    base = _solicitud_mapa()
    return base.model_copy(update={"acceptance_criteria": criterios or _criterio_visual()})


# ------------------------------------------- 1 y 2 · configurada ∩ transporte
def test_capacidad_configurada_con_transporte_con_imagenes_es_efectiva() -> None:
    """1 · si el transporte acepta imágenes, VISION configurada es efectiva."""
    capacidad = effective_capability(
        "anthropic",
        model="claude-x",
        configured=(CAPABILITY_TEXT, CAPABILITY_VISION),
        client=_Cliente(imagenes=True, transporte="api", detalle="API oficial"),
    )

    assert capacidad.has(CAPABILITY_VISION)
    assert capacidad.effective == (CAPABILITY_TEXT, CAPABILITY_VISION)
    assert capacidad.differs is False
    assert capacidad.reasons == ()


def test_capacidad_configurada_con_transporte_sin_imagenes_no_es_efectiva() -> None:
    """2 · Claude Code en modo texto declara VISION pero no la ejecuta: no es efectiva."""
    detalle = "claude --print es texto; para imágenes usa el transporte api"
    capacidad = effective_capability(
        "anthropic",
        model="claude-code",
        configured=(CAPABILITY_TEXT, CAPABILITY_VISION),
        client=_Cliente(imagenes=False, transporte="claude_code", detalle=detalle),
    )

    assert capacidad.has(CAPABILITY_VISION) is False
    assert CAPABILITY_VISION in capacidad.configured
    assert capacidad.differs is True
    assert capacidad.as_dict()["unavailable"] == [CAPABILITY_VISION]
    assert detalle in capacidad.reasons[0]
    assert capacidad.transport == "claude_code"


def test_una_capacidad_no_configurada_no_es_efectiva_aunque_el_transporte_pueda() -> None:
    """3 · reconocer el transporte no inventa capacidad: lo no declarado no es efectivo."""
    capacidad = effective_capability(
        "anthropic",
        configured=(CAPABILITY_TEXT,),
        client=_Cliente(imagenes=True, transporte="api"),
    )

    assert capacidad.has(CAPABILITY_VISION) is False
    assert CAPABILITY_VISION not in capacidad.configured
    assert capacidad.has(CAPABILITY_STRUCTURED_OUTPUT) is False


def test_sin_poder_comprobar_el_transporte_la_capacidad_no_se_afirma() -> None:
    """3 · fail closed: sin transporte comprobable, ninguna capacidad dependiente es efectiva."""
    assert transport_capabilities("guionizado") is None
    assert transport_capabilities("anthropic", client=SimpleNamespace()) is None

    capacidad = effective_capability(
        "guionizado", configured=(CAPABILITY_TEXT, CAPABILITY_VISION)
    )

    assert capacidad.has(CAPABILITY_VISION) is False
    assert capacidad.has(CAPABILITY_TEXT) is True
    assert "no se pudo comprobar el transporte" in capacidad.reasons[0]


def test_una_ruta_sin_capacidad_informa_de_las_que_si_podrian() -> None:
    """B · la información de rutas alternativas no sustituye nada: solo se enumera."""
    rutas = capability_routes(CAPABILITY_VISION)

    assert isinstance(rutas, tuple)
    # Nunca se enumera el doble de prueba como ruta que produce evidencia real.
    assert all("fake" not in item for item in rutas)


# ------------------------------------------- 4 y 5 · asignación y evidencia
def test_el_qa_visual_no_se_asigna_como_ejecutable_sin_entrada_de_imagenes(
    tmp_path: Path,
) -> None:
    """4 · la ruta sin imágenes no queda como QA visual ejecutable: se dice y se pide."""
    root = _repo_ciclo(tmp_path)
    audit = AuditLogger()
    ciclo = _ciclo_mapa(
        root,
        [
            _plan_mapa(superficies=(EXPLORADOR,)),
            _cambio_reparacion(
                EXPLORADOR,
                CONTENIDO_EXPLORADOR_REEMPLAZADO,
                "el mapa se integra visualmente",
            ),
        ],
        audit,
    )
    solicitud = _solicitud()

    resultado = ciclo.run(solicitud)

    assert resultado.capabilities, "la capacidad se comprueba antes de construir"
    requisito = resultado.capabilities[0]
    assert requisito.kind == ClaimKind.VISUAL_APPEARANCE.value
    assert requisito.capability == CAPABILITY_VISION
    assert requisito.available is False
    assert requisito.criterion
    assert requisito.remedy, "se dice qué corresponde hacer"
    # El QA visual no se declara como si pudiera ejecutarse en esta ruta.
    assert resultado.claims[0].capability_available is False
    eventos = {
        evento.event_type.value: dict(evento.metadata)
        for evento in audit.by_resource(solicitud.request_id)
    }
    assert "DEV_CAPABILITY_EVALUATED" in eventos
    assert eventos["DEV_CAPABILITY_EVALUATED"]["missing"] == (CAPABILITY_VISION,)
    visual = dict(eventos["DEV_CAPABILITY_EVALUATED"]["visual"])
    assert visual["available"] is False
    assert visual["provider"] == "guionizado"


def test_un_criterio_visual_cumplido_se_mide_y_se_satisface(tmp_path: Path) -> None:
    """5 · con evidencia obtenible (atestación humana explícita) el criterio se satisface."""
    root = _repo_ciclo(tmp_path)
    audit = AuditLogger()
    ciclo = _ciclo_mapa(
        root,
        [
            _plan_mapa(superficies=(EXPLORADOR,)),
            _cambio_reparacion(
                EXPLORADOR,
                CONTENIDO_EXPLORADOR_REEMPLAZADO,
                "el mapa se integra visualmente",
            ),
        ],
        audit,
    )

    resultado = ciclo.run(
        _solicitud(),
        human_attestation="revisado en local: el mapa se integra con el diseño actual",
    )

    assert resultado.claims_result == "SATISFIED"
    assert resultado.claims[0].result == "SATISFIED"
    assert "atestación humana" in resultado.claims[0].evidence
    assert resultado.status is DevelopmentStatus.COMPLETED
    assert resultado.commit_sha


def test_una_peticion_funcional_no_exige_ninguna_capacidad(tmp_path: Path) -> None:
    """11 · regresión: una petición normal no genera afirmaciones ni requisitos de capacidad."""
    root = _repo_ciclo(tmp_path)
    audit = AuditLogger()
    ciclo = _ciclo_mapa(
        root,
        [
            _plan_mapa(superficies=(EXPLORADOR,)),
            _cambio_reparacion(EXPLORADOR, CONTENIDO_EXPLORADOR_REEMPLAZADO, "el filtro funciona"),
        ],
        audit,
    )
    solicitud = _solicitud_mapa().model_copy(
        update={
            "objective": "Anadir un filtro de precio maximo en el listado",
            "acceptance_criteria": ("el filtro funciona",),
        }
    )

    resultado = ciclo.run(solicitud)

    assert resultado.capabilities == ()
    assert resultado.claims == ()
    assert resultado.claims_result == "NONE"
    assert resultado.status is DevelopmentStatus.COMPLETED


# ------------------------------------------- 6 y 7 · EVIDENCE_REQUIRED gobernado
def test_un_criterio_que_exige_capacidad_inexistente_nunca_es_verified(tmp_path: Path) -> None:
    """6 · sin capacidad efectiva el criterio queda EVIDENCE_REQUIRED, jamás VERIFIED."""
    root = _repo_ciclo(tmp_path)
    audit = AuditLogger()
    ciclo = _ciclo_mapa(
        root,
        [
            _plan_mapa(superficies=(EXPLORADOR,)),
            _cambio_reparacion(
                EXPLORADOR,
                CONTENIDO_EXPLORADOR_REEMPLAZADO,
                "el mapa se integra visualmente",
            ),
        ],
        audit,
    )

    resultado = ciclo.run(_solicitud())

    assert resultado.status is DevelopmentStatus.BLOCKED
    assert resultado.error_kind == "EVIDENCE_REQUIRED"
    assert resultado.claims_result == "EVIDENCE_REQUIRED"
    assert resultado.claims[0].result == "NOT_VERIFIED"
    assert resultado.commit_sha == ""
    eventos = {evento.event_type.value for evento in audit.by_resource(resultado.request_id)}
    assert "GIT_COMMIT_CREATED" not in eventos


def test_evidence_required_conserva_causa_criterio_capacidad_y_evidencia(
    tmp_path: Path,
) -> None:
    """7 · el estado gobernado lleva el criterio, la capacidad ausente y la evidencia exigida."""
    root = _repo_ciclo(tmp_path)
    audit = AuditLogger()
    ciclo = _ciclo_mapa(
        root,
        [
            _plan_mapa(superficies=(EXPLORADOR,)),
            _cambio_reparacion(
                EXPLORADOR,
                CONTENIDO_EXPLORADOR_REEMPLAZADO,
                "el mapa se integra visualmente",
            ),
        ],
        audit,
    )

    resultado = ciclo.run(_solicitud())
    registro = resultado.claims[0]

    assert registro.sentence
    assert registro.kind == ClaimKind.VISUAL_APPEARANCE.value
    assert registro.evidence_required
    assert "atestación humana" in registro.evidence_required
    assert registro.capability == CAPABILITY_VISION
    assert registro.capability_available is False
    assert registro.capability_detail
    assert registro.remedy
    assert "sin verificar" in registro.evidence
    assert resultado.error.startswith("hay un criterio factual/semántico requerido")


def test_la_capacidad_disponible_cambia_el_remedio_no_el_resultado() -> None:
    """Con imágenes disponibles pero sin ninguna aportada, el criterio sigue sin verificarse."""
    claims = extract_claims("el mapa se integra visualmente con el diseno actual")

    registro = verify_claims(
        claims,
        visual=VisualCapability(
            available=True, detail="API oficial con clave", provider="anthropic"
        ),
    )

    assert registro[0].result == "NOT_VERIFIED"
    assert registro[0].capability_available is True
    assert "aporta una imagen" in registro[0].remedy
    assert claims_result(registro) == "EVIDENCE_REQUIRED"


def test_los_requisitos_de_capacidad_se_calculan_antes_de_construir() -> None:
    """4 y 7 · la comprobación previa existe por sí sola y no depende del desenlace."""
    claims = extract_claims("el mapa se integra visualmente con el diseno actual")

    requisitos = capability_requirements(
        claims, visual=VisualCapability(available=False, detail="claude --print es texto")
    )

    assert len(requisitos) == 1
    assert requisitos[0].capability == CAPABILITY_VISION
    assert requisitos[0].available is False
    assert requisitos[0].criterion == claims[0].sentence
    assert "claude --print es texto" in requisitos[0].detail


# ------------------------------------------- 8 y 9 · Human Gate único, durable
class _CicloConEvidencia:
    """Ciclo que devuelve un ``EVIDENCE_REQUIRED`` gobernado, con su evidencia de capacidad."""

    def __init__(self) -> None:
        self.llamadas = 0

    def run(self, request: Any, **kwargs: Any) -> DevelopmentResult:
        """Resultado bloqueado por falta de evidencia, siempre igual en causa y motivo."""
        del kwargs
        self.llamadas += 1
        return DevelopmentResult(
            request_id=request.request_id,
            status=DevelopmentStatus.BLOCKED,
            target_id="punto-inmobiliario-hn",
            plan_status=PlanStatus.REJECTED,
            error_kind="EVIDENCE_REQUIRED",
            error=(
                "hay un criterio factual/semántico requerido que no se puede demostrar con la "
                "evidencia disponible: no hay capacidad de QA visual con imágenes"
            ),
            claims=(
                ClaimEvidence(
                    sentence="el mapa se integra visualmente con el diseno actual",
                    kind="VISUAL_APPEARANCE",
                    result="NOT_VERIFIED",
                    evidence=(
                        "no hay capacidad de QA visual con imágenes en la configuración actual "
                        "(claude --print es texto); el criterio queda sin verificar y exige "
                        "evidencia"
                    ),
                    evidence_required=(
                        "evidencia visual del resultado renderizado (imagen) o atestación humana "
                        "explícita"
                    ),
                    capability="VISION",
                    capability_available=False,
                    capability_detail=(
                        "claude --print es texto; para imágenes usa el transporte api"
                    ),
                    remedy="aporta una atestación humana explícita o usa la ruta con imágenes",
                ),
            ),
            claims_result="EVIDENCE_REQUIRED",
            capabilities=(
                CapabilityEvidence(
                    kind="VISUAL_APPEARANCE",
                    capability="VISION",
                    available=False,
                    criterion="el mapa se integra visualmente con el diseno actual",
                    detail="claude --print es texto; para imágenes usa el transporte api",
                    remedy="aporta una atestación humana explícita o usa la ruta con imágenes",
                ),
            ),
        )


def _estado() -> Path:
    """Fichero de estado durable de la consola de esta prueba (lo fija ``conftest``)."""
    return default_console_state_path()


def _consola() -> tuple[TestClient, AuditLogger, _CicloConEvidencia, ConsoleDependencies]:
    """Consola con el ciclo de evidencia, sobre un destino declarado y sin transporte real."""
    audit = AuditLogger()
    ciclo = _CicloConEvidencia()
    destino = DevelopmentTarget(
        target_id="punto-inmobiliario-hn",
        repository=Path.cwd(),
        baseline_sha="0" * 40,
        scope_roots=("src",),
        display_name="Destino de prueba",
    )
    dependencies = ConsoleDependencies(
        dev_cycle=ciclo,  # type: ignore[arg-type]
        gates=HumanGate(),
        audit=audit,
        policy=PolicyEngine.from_config(),
        targets={"punto-inmobiliario-hn": destino},
        run_inline=True,
        environ={},
    )
    application = FastAPI()
    register_dashboard(application)
    register_human_console(application, dependencies)
    return TestClient(application), audit, ciclo, dependencies


def _crear_tarea(client: TestClient) -> dict[str, Any]:
    """Crea la tarea del caso sobre la consola de prueba."""
    return client.post(
        "/console/tasks",
        json={
            "objective": "el mapa se integra visualmente con el diseno actual",
            "target_id": "punto-inmobiliario-hn",
            "scope_paths": ["src"],
        },
    ).json()


def test_el_gate_de_evidencia_es_unico_correcto_y_durable() -> None:
    """8 y 9 · un solo gate, con el criterio y la capacidad ausente, y sobrevive al reinicio."""
    client, _audit, ciclo, _deps = _consola()
    tarea = _crear_tarea(client)

    assert tarea["stage"] == "WAITING_HUMAN", tarea
    assert len(tarea["gates"]) == 1
    gates = client.get("/console/human-gates").json()
    assert gates["total"] == 1
    assert gates["pending"] == 1
    gate = gates["items"][0]
    assert gate["action"] == "EVIDENCE_REQUIRED"
    assert gate["is_pending"] is True
    assert "no hay capacidad de QA visual" in gate["reason"]

    evidencia = gate["evidence"]
    capacidad = evidencia["capability"]
    assert capacidad["criterion"] == "el mapa se integra visualmente con el diseno actual"
    assert capacidad["required"] == "VISION"
    assert capacidad["available"] is False
    assert capacidad["required_evidence"]
    assert capacidad["remedy"]
    assert "no hay capacidad de QA visual" in evidencia["cause"]["detail"]
    assert "evidencia" in evidencia["authorizes"].lower()
    assert any(
        "no aporta" in item or "sin atestación" in item
        for item in evidencia["does_not_authorize"]
    )

    # Reintento con la misma causa: se reutiliza el gate, no se duplica.
    reintento = client.post(f"/console/tasks/{tarea['task_id']}/run").json()
    assert reintento["runs"] == 2
    assert len(reintento["gates"]) == 1
    assert client.get("/console/human-gates").json()["total"] == 1
    assert ciclo.llamadas == 2

    # Reinicio: el gate sigue ahí, pendiente y con los mismos datos.
    otro, _audit2, _ciclo2, _deps2 = _consola()
    recuperada = otro.get("/console/tasks").json()["items"][0]
    assert recuperada["task_id"] == tarea["task_id"]
    assert recuperada["stage"] == "WAITING_HUMAN"
    gates2 = otro.get("/console/human-gates").json()
    assert gates2["total"] == 1
    assert gates2["items"][0]["approval_id"] == gate["approval_id"]
    assert gates2["items"][0]["evidence"]["capability"] == capacidad


def test_una_atestacion_aprobada_viaja_al_ciclo_como_evidencia() -> None:
    """D · la nota de la persona en el gate aprobado es la atestación del intento siguiente."""
    from punto.api.console import _human_attestation

    client, _audit, _ciclo, dependencies = _consola()
    tarea = _crear_tarea(client)
    approval_id = tarea["gates"][0]
    pendiente = SimpleNamespace(task_id=uuid4())  # placeholder sin uso: la identidad va abajo

    aprobacion = client.post(
        f"/console/human-gates/{approval_id}/approve",
        json={"resolved_by": "carlos", "note": "revisado en local: se integra bien"},
    )
    assert aprobacion.status_code == 200, aprobacion.text

    del pendiente
    tarea_viva = SimpleNamespace(task_id=__import__("uuid").UUID(tarea["task_id"]))
    assert _human_attestation(tarea_viva, dependencies) == "revisado en local: se integra bien"


def test_sin_gate_aprobado_no_hay_atestacion() -> None:
    """D · sin decisión humana no hay atestación: la evidencia no se inventa."""
    from punto.api.console import _human_attestation

    client, _audit, _ciclo, dependencies = _consola()
    tarea = _crear_tarea(client)

    tarea_viva = SimpleNamespace(task_id=__import__("uuid").UUID(tarea["task_id"]))
    assert _human_attestation(tarea_viva, dependencies) == ""


# ------------------------------------------- 10 · dashboard: configurada vs efectiva
def test_el_dashboard_distingue_capacidad_configurada_de_efectiva() -> None:
    """10 · la API expone las dos cosas por separado y la página no ofrece lo no efectivo."""
    application = FastAPI()
    register_dashboard(application)
    client = TestClient(application)

    proveedores = client.get("/providers").json()
    roles = client.get("/roles").json()

    assert proveedores["effective_capabilities"], "hay tabla de capacidad efectiva"
    fila = proveedores["effective_capabilities"][0]
    assert {"provider", "configured", "effective", "unavailable", "reasons", "transport"} <= set(
        fila
    )
    assert set(fila["unavailable"]) == set(fila["configured"]) - set(fila["effective"])

    assert "role_capabilities" in roles
    visual = roles["role_capabilities"]["VISUAL_QA"]
    assert visual["required"] == "VISION"
    assert isinstance(visual["available"], bool)
    if not visual["available"]:
        assert visual["detail"], "cuando no es efectiva, se dice por qué"

    pagina = client.get("/dashboard").text
    assert "declared-only" in pagina
    assert "no efectiva" in pagina
    assert 'data-testid="rol-sin-capacidad-' in pagina
    assert "capability-warning" in pagina


def test_la_tabla_efectiva_no_ofrece_como_utilizable_lo_que_el_transporte_no_ejecuta() -> None:
    """10 · coherencia interna de la tabla: efectivas ⊆ configuradas y motivos por cada hueco."""
    tabla = effective_capabilities_table()

    assert tabla
    for fila in tabla:
        assert set(fila["effective"]) <= set(fila["configured"])
        if fila["unavailable"]:
            assert fila["reasons"], "cada capacidad no efectiva tiene su motivo"
            assert fila["differs"] is True


def test_visual_capability_for_role_devuelve_lo_configurado_y_lo_efectivo() -> None:
    """E · la capacidad visual del rol lleva las dos vistas y el remedio, sin suponer."""
    capacidad = visual_capability_for_role(
        configured=(CAPABILITY_TEXT, CAPABILITY_VISION),
        client=_Cliente(
            imagenes=False, transporte="claude_code", detalle="claude --print es texto"
        ),
    )

    assert capacidad.available is False
    assert capacidad.configured is True
    assert capacidad.transport == "claude_code"
    assert "claude --print es texto" in capacidad.detail
    assert "atestación" in capacidad.remedy
    assert capacidad.as_dict()["configured"] is True


# ------------------------------------------- 9 y 12 · el estado durable no se rompe
def test_el_resultado_con_capacidades_sobrevive_al_estado_durable() -> None:
    """9 · el resultado con capacidades se persiste y se recupera igual (contrato estable)."""
    client, _audit, _ciclo, _deps = _consola()
    _crear_tarea(client)

    documento = json.loads(_estado().read_text(encoding="utf-8"))
    guardada = documento["tasks"][0]
    assert guardada["result"]["error_kind"] == "EVIDENCE_REQUIRED"
    assert guardada["result"]["capabilities"][0]["capability"] == "VISION"
    assert guardada["result"]["capabilities"][0]["available"] is False
    assert guardada["result"]["claims"][0]["capability_available"] is False
    assert guardada["result"]["claims"][0]["evidence_required"]

    otro, _audit2, _ciclo2, _deps2 = _consola()
    recuperada = otro.get(f"/console/tasks/{guardada['task_id']}").json()
    assert recuperada["development"]["error_kind"] == "EVIDENCE_REQUIRED"
    assert recuperada["development"]["claims"][0]["capability"] == "VISION"
    assert recuperada["development"]["claims"][0]["evidence_required"]
    assert recuperada["development"]["capabilities"][0]["capability"] == "VISION"
    assert recuperada["development"]["capabilities"][0]["available"] is False


def test_una_capacidad_sin_nombre_no_se_presenta_como_disponible() -> None:
    """Un resultado sin capacidad nombrada no puede leerse como «capacidad disponible»."""
    sin_capacidad = _resultado_pendiente(ClaimEvidence(
        sentence="el mapa se integra visualmente",
        kind="VISUAL_APPEARANCE",
        required=True,
        result="NOT_VERIFIED",
        evidence="criterio visual sin evidencia",
    ))

    evidencia = _capability_evidence(sin_capacidad)

    assert evidencia["required"] == ""
    assert evidencia["available"] is False

    con_capacidad = _capability_evidence(
        _resultado_pendiente(
            ClaimEvidence(
                sentence="el mapa se integra visualmente",
                kind="VISUAL_APPEARANCE",
                required=True,
                result="NOT_VERIFIED",
                evidence="el transporte puede recibir imágenes pero no se aportó ninguna",
                capability=CAPABILITY_VISION,
                capability_available=True,
                remedy="aporta una imagen renderizada",
            )
        )
    )

    assert con_capacidad["required"] == CAPABILITY_VISION
    assert con_capacidad["available"] is True


def _resultado_pendiente(claim: ClaimEvidence) -> DevelopmentResult:
    """Resultado bloqueado por evidencia con la afirmación dada, sin pasar por el ciclo."""
    return DevelopmentResult(
        request_id=uuid4(),
        status=DevelopmentStatus.BLOCKED,
        target_id="punto-inmobiliario-hn",
        plan_status=PlanStatus.REJECTED,
        error_kind="EVIDENCE_REQUIRED",
        error="hay un criterio factual/semántico requerido que no se puede demostrar",
        claims=(claim,),
        claims_result="EVIDENCE_REQUIRED",
    )
