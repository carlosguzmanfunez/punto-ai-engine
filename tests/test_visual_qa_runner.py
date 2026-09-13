"""Runner de Visual QA contra el transporte falso de Anthropic (ENGINE-5.3 §27 a §31).

Aquí no hay credencial ni navegador: se ejercita el **cliente real** de Anthropic contra un
``httpx.MockTransport`` guionado, con capturas PNG reales producidas por los soportes. Lo que se
comprueba es lo que importa: el contrato multimodal, el esquema enviado, los gates, la redacción y
el veredicto calculado por PUNTO.

El flujo de extremo a extremo con navegador real vive en
``tests/integration/test_web_visual_fake_live.py``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from punto.audit.logger import AuditLogger
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers.base import ImagePayload
from punto.providers.json_schema import provider_schema_for
from punto.schemas.audit import AuditEventType
from punto.schemas.visual import VisualQAGateName, VisualQAProposal, VisualQAStatus
from punto.schemas.web import DEFAULT_VIEWPORTS, ViewportName, WebTechnicalStatus
from punto.tasks.manager import TaskManager
from punto.tools.errors import VisualQARunnerNotConfiguredError
from punto.visualqa.claude import (
    BLOCKED_VISUAL_COVERAGE,
    BLOCKED_VISUAL_IMAGES,
    BLOCKED_VISUAL_PROVIDER_ERROR,
    BLOCKED_VISUAL_PROVIDER_REFUSAL,
    BLOCKED_VISUAL_PROVIDER_UNAVAILABLE,
    ClaudeVisualQARunner,
)
from punto.visualqa.prompts import VISUAL_SYSTEM_PROMPT
from punto.web.routes import screenshot_logical_name
from test_anthropic_client import (
    FAKE_KEY,
    FakeAnthropicAPI,
    error_response,
    make_client,
    message_response,
    timing_out,
)
from visual_support import (
    make_visual_task,
    visual_finding_payload,
    visual_images,
    visual_payload,
)


def proposal_response(**overrides: Any) -> Any:
    """Respuesta 200 con una propuesta visual serializada."""
    return message_response(text=json.dumps(visual_payload(**overrides)))


def make_runner(
    script: list[Any] | None = None,
    *,
    audit: AuditLogger | None = None,
) -> tuple[ClaudeVisualQARunner, FakeAnthropicAPI]:
    """Runner visual contra el transporte falso, sin esperas reales."""
    api = FakeAnthropicAPI(script)
    return ClaudeVisualQARunner(client=make_client(api), audit=audit), api


def refusing_response() -> Any:
    """Respuesta 200 con una negativa explícita del modelo."""
    return message_response(text="No puedo ayudar con esta petición.", stop_reason="refusal")


def test_clean_proposal_over_green_session_passes() -> None:
    """Sesión técnica en verde y propuesta limpia: PASS, con todos los gates en verde."""
    task, raw = make_visual_task(viewports=DEFAULT_VIEWPORTS[:1])
    runner, api = make_runner([proposal_response()])

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.PASS
    assert report.passed is True
    assert report.findings == ()
    assert all(gate.passed for gate in report.gates), [
        (gate.name.value, gate.detail) for gate in report.gates
    ]
    assert report.provider == "anthropic"
    assert report.model.startswith("claude")
    assert report.screenshots_analyzed == (
        screenshot_logical_name("/", ViewportName.MOBILE),
    )
    assert report.routes_analyzed == ("/",)
    assert report.viewports_analyzed == ("MOBILE",)
    assert report.model_usage.total_tokens > 0
    assert report.attempts == 1
    assert report.model_calls == 1
    assert api.calls == 1


def test_request_carries_images_in_order_with_the_production_schema() -> None:
    """La petición lleva los bloques en orden, con su media type y el esquema real."""
    task, raw = make_visual_task(routes=("/", "/precios"))
    images = visual_images(task, raw)
    runner, api = make_runner([proposal_response()])

    runner.evaluate(task, images)

    body = api.last_body
    content = body["messages"][0]["content"]
    texts = [block for block in content if block["type"] == "text"]
    sent_images = [block for block in content if block["type"] == "image"]
    assert len(texts) == 1
    assert len(sent_images) == len(task.screenshots)
    assert all(block["source"]["media_type"] == "image/png" for block in sent_images)
    assert body["system"] == VISUAL_SYSTEM_PROMPT
    assert body["output_config"]["format"]["type"] == "json_schema"
    schema = body["output_config"]["format"]["schema"]
    assert schema == provider_schema_for(VisualQAProposal)
    assert "status" not in schema["properties"]
    for artifact in task.screenshots:
        assert f"- {artifact.logical_name} (" in texts[0]["text"]


def test_prompt_states_the_measured_technical_facts() -> None:
    """El prompt entrega a Claude lo que PUNTO midió, para que no lo contradiga por ignorancia."""
    task, raw = make_visual_task()
    runner, api = make_runner([proposal_response()])

    runner.evaluate(task, visual_images(task, raw))

    prompt = api.last_body["messages"][0]["content"][0]["text"]
    assert "no se negocian" in prompt
    assert "Estado técnico: PASS" in prompt
    assert "MOBILE" in prompt


def test_high_finding_requests_changes() -> None:
    """Gates verdes con un hallazgo HIGH: CHANGES_REQUESTED, no PASS."""
    task, raw = make_visual_task()
    runner, _ = make_runner(
        [proposal_response(findings=(visual_finding_payload(severity="HIGH", category="LAYOUT"),))]
    )

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.CHANGES_REQUESTED
    assert report.passed is False
    assert report.blocking_findings
    gate = report.gate(VisualQAGateName.FINDINGS)
    assert gate is not None and gate.passed is False and gate.blocking is False


def test_medium_finding_is_reported_but_keeps_pass() -> None:
    """Un hallazgo MEDIUM se informa y no impide el PASS."""
    task, raw = make_visual_task()
    runner, _ = make_runner([proposal_response(findings=(visual_finding_payload(),))])

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.PASS
    assert len(report.findings) == 1
    assert report.blocking_findings == ()


# ---------------------------------------------------------------------------
# §30: nada anula un hecho determinista
# ---------------------------------------------------------------------------
def test_a_blocked_session_blocks_even_with_a_clean_proposal() -> None:
    """Si el navegador no pudo cargar la página, no hay PASS posible."""
    task, raw = make_visual_task(
        status=WebTechnicalStatus.BLOCKED, error="el proyecto no arrancó"
    )
    runner, _ = make_runner([proposal_response()])

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.BLOCKED
    gate = report.gate(VisualQAGateName.TECHNICAL)
    assert gate is not None and gate.passed is False and gate.blocking is True


def test_a_missing_capture_blocks() -> None:
    """Evaluar sin todas las capturas exigidas: BLOCKED, aunque el modelo conteste."""
    task, raw = make_visual_task(routes=("/", "/precios"))
    images = visual_images(task, raw)
    del images[screenshot_logical_name("/precios", ViewportName.MOBILE)]
    runner, api = make_runner([proposal_response()])

    report = runner.evaluate(task, images)

    assert report.status is VisualQAStatus.BLOCKED
    gate = report.gate(VisualQAGateName.SCREENSHOTS)
    assert gate is not None and gate.passed is False and gate.blocking is True
    assert "precios @ MOBILE" in gate.detail
    # No se envió nada: informar de las cinco capturas que sí había sería declarar como analizado
    # lo que el modelo nunca vio.
    assert report.screenshots_analyzed == ()
    assert report.routes_analyzed == ()
    assert BLOCKED_VISUAL_COVERAGE in report.error
    assert api.calls == 0, "no se evalúa una parte haciéndola pasar por el todo"


def test_the_image_budget_blocks_before_any_call() -> None:
    """Más capturas de las que admite el contrato multimodal: BLOCKED sin llamar al modelo."""
    task, raw = make_visual_task(routes=("/", "/precios", "/contacto"))
    assert len(task.screenshots) == 9
    runner, api = make_runner([proposal_response()])

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.BLOCKED
    assert BLOCKED_VISUAL_IMAGES in report.error
    assert report.screenshots_analyzed == ()
    assert api.calls == 0


def test_a_mismatched_image_is_rejected_before_the_call() -> None:
    """Una imagen que no enlaza con el artefacto no viaja, y el motivo queda en el informe."""
    task, raw = make_visual_task()
    artifact = task.screenshots[0]
    images = visual_images(task, raw)
    images[artifact.logical_name] = ImagePayload(
        data=b"\x89PNG\r\n\x1a\n" + b"x" * 10,
        media_type="image/png",
        logical_name=artifact.logical_name,
    )
    runner, api = make_runner([proposal_response()])

    report = runner.evaluate(task, images)

    assert report.status is VisualQAStatus.BLOCKED
    assert BLOCKED_VISUAL_COVERAGE in report.error
    assert artifact.logical_name in report.error
    assert api.calls == 0


def test_same_size_with_a_different_hash_is_rejected_before_the_call() -> None:
    """V53-05: mismo tamaño y distinto sha256 no llega al proveedor.

    Es el ataque que la frontera del runner tiene que parar por sí sola: un llamante que construya
    el ``ImagePayload`` a mano puede conservar el tamaño declarado y cambiar los bytes. La
    revalidación contra el artefacto lo detecta y se bloquea sin gastar una llamada.
    """
    task, raw = make_visual_task()
    artifact = task.screenshots[0]
    original = raw[artifact.logical_name]
    tampered = original[:-1] + bytes([original[-1] ^ 0xFF])
    assert len(tampered) == len(original)
    images = visual_images(task, raw)
    images[artifact.logical_name] = ImagePayload(
        data=tampered, media_type="image/png", logical_name=artifact.logical_name
    )
    runner, api = make_runner([proposal_response()])

    report = runner.evaluate(task, images)

    assert report.status is VisualQAStatus.BLOCKED
    assert BLOCKED_VISUAL_COVERAGE in report.error
    assert api.calls == 0


def test_a_wrong_media_type_is_rejected_before_the_call() -> None:
    """Una media type que contradice al artefacto se rechaza: no se canonicaliza en silencio."""
    task, raw = make_visual_task()
    artifact = task.screenshots[0]
    images = visual_images(task, raw)
    images[artifact.logical_name] = ImagePayload(
        data=raw[artifact.logical_name],
        media_type="image/jpeg",
        logical_name=artifact.logical_name,
    )
    runner, api = make_runner([proposal_response()])

    report = runner.evaluate(task, images)

    assert report.status is VisualQAStatus.BLOCKED
    assert "image/jpeg" in report.error
    assert api.calls == 0


# ---------------------------------------------------------------------------
# Proveedor: negativa, credencial, error y truncamiento
# ---------------------------------------------------------------------------
def test_refusal_blocks_with_its_own_cause() -> None:
    """Una negativa es BLOCKED con causa propia, no un error genérico."""
    task, raw = make_visual_task()
    runner, api = make_runner([refusing_response()])

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.BLOCKED
    assert BLOCKED_VISUAL_PROVIDER_REFUSAL in report.error
    assert BLOCKED_VISUAL_PROVIDER_ERROR not in report.error
    assert api.calls == 1


def test_authentication_error_blocks_as_provider_unavailable() -> None:
    """Sin credencial válida: PROVIDER_UNAVAILABLE. Ningún otro proveedor entra a sustituirlo."""
    task, raw = make_visual_task()
    runner, api = make_runner([error_response(401, "invalid x-api-key")])

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.BLOCKED
    assert BLOCKED_VISUAL_PROVIDER_UNAVAILABLE in report.error
    assert api.calls == 1


def test_server_error_blocks_after_bounded_retries() -> None:
    """Un 500 se reintenta de forma acotada y termina en BLOCKED."""
    task, raw = make_visual_task()
    runner, api = make_runner([error_response(500, "error interno")])

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.BLOCKED
    assert BLOCKED_VISUAL_PROVIDER_ERROR in report.error
    assert api.calls == 3


def test_timeout_blocks_after_bounded_retries() -> None:
    """Un timeout agotado también bloquea."""
    task, raw = make_visual_task()
    runner, api = make_runner([timing_out])

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.BLOCKED
    assert api.calls == 3


def test_truncated_response_blocks_without_leaking_the_partial_text() -> None:
    """El truncamiento se detecta antes de interpretar el JSON y no filtra el texto recibido."""
    task, raw = make_visual_task()
    partial = '{"summary": "cortado", "findings": ['
    runner, api = make_runner([message_response(text=partial, stop_reason="max_tokens")])

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.BLOCKED
    assert BLOCKED_VISUAL_PROVIDER_ERROR in report.error
    assert "max_tokens" in report.error
    assert partial not in report.error
    assert api.calls == 1


# ---------------------------------------------------------------------------
# Contrato y reparación
# ---------------------------------------------------------------------------
def test_a_status_field_is_rejected_and_repaired() -> None:
    """Claude no escribe el veredicto: si lo intenta, se rechaza y se le pide corregir."""
    task, raw = make_visual_task()
    bad = json.dumps(visual_payload(extra={"status": "PASS"}))
    audit = AuditLogger()
    runner, api = make_runner([message_response(text=bad), proposal_response()], audit=audit)

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.PASS
    assert report.attempts == 2
    assert api.calls == 2
    rejections = audit.by_type(AuditEventType.VISUAL_QA_PROPOSAL_REJECTED)
    assert len(rejections) == 1
    assert any("status" in str(item) for item in dict(rejections[0].metadata)["violations"])
    assert "status" in api.bodies[1]["messages"][0]["content"][0]["text"]


def test_an_invented_route_is_repaired() -> None:
    """Una ruta que nadie declaró se rechaza y el modelo tiene que corregirla."""
    task, raw = make_visual_task()
    bad = proposal_response(findings=(visual_finding_payload(route="/inventada"),))
    runner, api = make_runner([bad, proposal_response()])

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.PASS
    assert api.calls == 2
    assert "inventada" in api.bodies[1]["messages"][0]["content"][0]["text"]


def test_json_without_the_object_shape_is_repaired() -> None:
    """Con el esquema activo el JSON roto es la excepción: se repara, no se adivina."""
    task, raw = make_visual_task()
    runner, api = make_runner(
        [message_response(text='{"summary": "incompleto"'), proposal_response()]
    )

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.PASS
    assert api.calls == 2
    assert all("output_config" in body for body in api.bodies)


def test_validation_errors_are_redacted() -> None:
    """La credencial no aparece en el informe aunque Pydantic cite el valor rechazado."""
    task, raw = make_visual_task()
    broken = visual_payload()
    broken["findings"] = [{**visual_finding_payload(), "severity": FAKE_KEY}]
    runner, _ = make_runner([message_response(text=json.dumps(broken))])

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.BLOCKED
    assert FAKE_KEY not in report.error
    assert FAKE_KEY not in report.model_dump_json()


# ---------------------------------------------------------------------------
# Auditoría y CAMUS
# ---------------------------------------------------------------------------
def test_audit_records_the_cycle_without_image_bytes() -> None:
    """§36: metadatos del ciclo, nunca bytes de imagen."""
    task, raw = make_visual_task()
    audit = AuditLogger()
    runner, _ = make_runner(
        [proposal_response(findings=(visual_finding_payload(severity="LOW"),))], audit=audit
    )

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.PASS
    for event_type in (
        AuditEventType.VISUAL_QA_REQUEST_STARTED,
        AuditEventType.VISUAL_QA_PROPOSAL_RECEIVED,
        AuditEventType.VISUAL_QA_PROPOSAL_ACCEPTED,
        AuditEventType.VISUAL_QA_FINDING_RECORDED,
        AuditEventType.VISUAL_QA_COMPLETED,
    ):
        assert audit.by_type(event_type), event_type

    dumped = json.dumps([dict(event.metadata) for event in audit.events()], default=str)
    assert "iVBOR" not in dumped, "un base64 de imagen no puede acabar en el registro"
    assert "\\x89PNG" not in dumped
    assert '"screenshots": 3' in dumped, "el recuento de capturas sí es evidencia"
    assert '"images": 3' in dumped, "las imágenes enviadas al modelo también se cuentan"


def test_a_blocked_evaluation_is_recorded() -> None:
    """Un bloqueo también queda registrado, con su motivo."""
    task, raw = make_visual_task(status=WebTechnicalStatus.BLOCKED, error="sin build")
    audit = AuditLogger()
    runner, _ = make_runner([proposal_response()], audit=audit)

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.BLOCKED
    blocked = audit.by_type(AuditEventType.VISUAL_QA_BLOCKED)
    assert blocked
    assert any("sin build" in str(dict(event.metadata)) for event in blocked)


def test_camus_delegates_visual_qa(
    task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """CAMUS delega en el runner inyectado y registra los hallazgos que devuelve."""
    task, raw = make_visual_task()
    audit = AuditLogger()
    runner, _ = make_runner(
        [proposal_response(findings=(visual_finding_payload(severity="LOW"),))], audit=audit
    )
    camus = Camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        planner=Planner(),
        visual_qa_runner=runner,
    )

    report = camus.visual_qa(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.PASS
    assert camus.visual_qa_runner is runner
    assert audit.by_type(AuditEventType.VISUAL_QA_FINDING_RECORDED)


def test_camus_without_visual_qa_fails_explicitly(
    task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """Sin rol inyectado, CAMUS falla: no improvisa un veredicto visual."""
    task, raw = make_visual_task()
    camus = Camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=AuditLogger(),
        planner=Planner(),
    )

    with pytest.raises(VisualQARunnerNotConfiguredError):
        camus.visual_qa(task, visual_images(task, raw))


def test_evaluation_is_deterministic() -> None:
    """El mismo input produce el mismo veredicto y las mismas capturas analizadas."""
    task, raw = make_visual_task()
    images: Mapping[str, ImagePayload] = visual_images(task, raw)

    def evaluate_once() -> tuple[str, tuple[str, ...]]:
        runner, _ = make_runner([proposal_response()])
        report = runner.evaluate(task, images)
        return report.status.value, report.screenshots_analyzed

    assert evaluate_once() == evaluate_once()


def test_runner_declares_its_identity() -> None:
    """El runner declara quién es, con qué prompt trabaja y con qué presupuesto."""
    runner, _ = make_runner([proposal_response()])

    assert runner.name == "ClaudeVisualQARunner"
    assert runner.provider == "anthropic"
    assert runner.model.startswith("claude")
    assert runner.uses_ai is True
    assert runner.prompt_version
    assert runner.limits.max_attempts >= 1
    assert runner.image_limits.max_images == 8
