"""Pruebas de la API FastAPI.

Casos obligatorios cubiertos aquí: 11 y 12, más los errores HTTP.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto._version import ENGINE_NAME, ENGINE_VERSION
from punto.api.app import create_app


def is_valid_uuid(value: str) -> bool:
    """True si la cadena es un UUID válido."""
    try:
        UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


# ---------------------------------------------------------------------------
# Caso 11: GET /health
# ---------------------------------------------------------------------------
def test_case_11_health(client: TestClient) -> None:
    """GET /health devuelve exactamente el contrato exigido."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "engine": ENGINE_NAME,
        "version": ENGINE_VERSION,
    }


def test_health_version_is_0_1_0(client: TestClient) -> None:
    """La versión reportada por la API es la de la Base Constitucional V0.1."""
    payload = client.get("/health").json()

    assert payload["version"] == "0.1.0"
    assert payload["engine"] == "PUNTO AI ENGINE"


# ---------------------------------------------------------------------------
# Caso 12: POST /tasks
# ---------------------------------------------------------------------------
def test_case_12_create_task(client: TestClient) -> None:
    """POST /tasks crea la tarea, la procesa con CAMUS y devuelve 201."""
    response = client.post(
        "/tasks",
        json={
            "objective": "Crear el módulo de reportes",
            "action": "create_file",
            "description": "Implementar el módulo base",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "COMPLETED"
    assert payload["outcome"] == "COMPLETED"
    assert payload["allowed"] is True
    assert payload["authority_level"] == "LEVEL_0_AUTONOMOUS"
    assert payload["effective_risk"] == "LOW"
    assert payload["task"]["title"] == "Crear el módulo de reportes"
    assert is_valid_uuid(payload["id"])


def test_create_task_requiring_human_approval(client: TestClient) -> None:
    """Una acción Level 3 deja la tarea en HUMAN_APPROVAL con su gate."""
    response = client.post(
        "/tasks",
        json={"objective": "Desplegar a producción", "action": "deploy_production"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "HUMAN_APPROVAL"
    assert payload["outcome"] == "HUMAN_APPROVAL_REQUIRED"
    assert payload["requires_human"] is True
    assert payload["allowed"] is False
    assert payload["human_approval_id"] is not None


def test_create_task_with_unknown_action_is_blocked(client: TestClient) -> None:
    """Una acción desconocida se rechaza por DEFAULT DENY y bloquea la tarea."""
    response = client.post(
        "/tasks",
        json={"objective": "Acción inexistente", "action": "accion_inexistente"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "BLOCKED"
    assert payload["outcome"] == "REJECTED"
    assert payload["blocked_reason"] is not None
    assert "DEFAULT DENY" in payload["reason"]


def test_create_task_over_budget_is_blocked(client: TestClient) -> None:
    """Exceder el presupuesto bloquea la tarea a través de la API."""
    response = client.post(
        "/tasks",
        json={
            "objective": "Cambio carísimo",
            "action": "modify_file",
            "estimated_cost": 500.0,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "BLOCKED"
    assert payload["blocked_reason"] == "MAX_COST_EXCEEDED"


def test_create_task_modifying_constitution_is_rejected(client: TestClient) -> None:
    """La protección constitucional se aplica también en la API."""
    response = client.post(
        "/tasks",
        json={
            "objective": "Editar la constitución",
            "action": "modify_file",
            "files_changed": ["config/constitution.yaml"],
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "BLOCKED"
    assert payload["outcome"] == "REJECTED"
    assert "config/constitution.yaml" in payload["reason"] or "protegido" in payload["reason"]


def test_create_task_invalid_payload_returns_422(client: TestClient) -> None:
    """Un cuerpo inválido devuelve 422."""
    assert client.post("/tasks", json={"action": "create_file"}).status_code == 422
    assert client.post("/tasks", json={"objective": "x"}).status_code == 422
    assert client.post("/tasks", json={"objective": "x", "action": ""}).status_code == 422


def test_create_task_rejects_unknown_fields(client: TestClient) -> None:
    """Los campos desconocidos se rechazan (extra=forbid)."""
    response = client.post(
        "/tasks",
        json={"objective": "x", "action": "create_file", "campo_inexistente": 1},
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /tasks y GET /tasks/{task_id}
# ---------------------------------------------------------------------------
def test_list_tasks(client: TestClient) -> None:
    """GET /tasks lista las tareas creadas en memoria."""
    client.post("/tasks", json={"objective": "Primera", "action": "create_file"})
    client.post("/tasks", json={"objective": "Segunda", "action": "create_documentation"})

    response = client.get("/tasks")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["returned"] == 2
    assert [item["title"] for item in payload["items"]] == ["Primera", "Segunda"]


def test_list_tasks_can_filter_by_status(client: TestClient) -> None:
    """GET /tasks acepta el filtro por estado."""
    client.post("/tasks", json={"objective": "Completada", "action": "create_file"})
    client.post("/tasks", json={"objective": "Bloqueada", "action": "accion_inexistente"})

    blocked = client.get("/tasks", params={"status": "BLOCKED"}).json()
    completed = client.get("/tasks", params={"status": "COMPLETED"}).json()

    assert blocked["total"] == 1
    assert blocked["items"][0]["title"] == "Bloqueada"
    assert completed["total"] == 1
    assert completed["items"][0]["title"] == "Completada"


def test_get_task_by_id(client: TestClient) -> None:
    """GET /tasks/{task_id} devuelve el detalle de la tarea."""
    created = client.post("/tasks", json={"objective": "Detallar", "action": "create_file"}).json()

    response = client.get(f"/tasks/{created['id']}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == created["id"]
    assert payload["title"] == "Detallar"
    assert payload["status"] == "COMPLETED"
    assert payload["task"]["id"] == created["id"]
    assert "COMPLETED" not in payload["allowed_transitions"]


def test_get_unknown_task_returns_404(client: TestClient) -> None:
    """Un identificador inexistente devuelve 404."""
    response = client.get(f"/tasks/{uuid4()}")

    assert response.status_code == 404
    assert "no encontrada" in response.json()["detail"].lower()


def test_get_task_with_invalid_uuid_returns_422(client: TestClient) -> None:
    """Un identificador con formato inválido devuelve 422."""
    assert client.get("/tasks/no-es-un-uuid").status_code == 422


# ---------------------------------------------------------------------------
# R1.1 - La API no expone ninguna ruta capaz de transicionar una tarea
# ---------------------------------------------------------------------------
def test_transitions_endpoint_does_not_exist(client: TestClient) -> None:
    """El endpoint genérico de transiciones fue eliminado de la API pública.

    Era la vía por la que un consumidor podía forzar ``HUMAN_APPROVAL ->
    APPROVED`` saltándose el Human Gate. La ruta ya no existe en absoluto
    (Starlette responde 404; 405 si el método no coincide con una ruta viva), y
    en ningún caso el estado de la tarea cambia.
    """
    created = client.post(
        "/tasks", json={"objective": "Desplegar", "action": "deploy_production"}
    ).json()
    assert created["status"] == "HUMAN_APPROVAL"

    response = client.post(
        f"/tasks/{created['id']}/transitions",
        json={"target": "APPROVED", "reason": "bypass"},
    )

    assert response.status_code in {404, 405}

    detail = client.get(f"/tasks/{created['id']}").json()
    assert detail["status"] == "HUMAN_APPROVAL"


def test_no_http_route_can_move_a_task_out_of_human_approval(client: TestClient) -> None:
    """Ninguna ruta HTTP permite sacar una tarea de ``HUMAN_APPROVAL``."""
    created = client.post(
        "/tasks", json={"objective": "Desplegar", "action": "deploy_production"}
    ).json()
    task_id = created["id"]

    attempts = {
        "transitions": client.post(f"/tasks/{task_id}/transitions", json={"target": "APPROVED"}),
        "approve": client.post(f"/tasks/{task_id}/approve"),
        "status": client.post(f"/tasks/{task_id}/status", json={"status": "APPROVED"}),
        "patch": client.patch(f"/tasks/{task_id}", json={"status": "APPROVED"}),
        "put": client.put(f"/tasks/{task_id}", json={"status": "APPROVED"}),
    }

    unsafe = {
        name: response.status_code
        for name, response in attempts.items()
        if response.status_code not in {404, 405}
    }
    assert unsafe == {}

    assert client.get(f"/tasks/{task_id}").json()["status"] == "HUMAN_APPROVAL"


# ---------------------------------------------------------------------------
# R1.1 - El Human Gate no se resuelve por HTTP en ENGINE-0
# ---------------------------------------------------------------------------
def test_human_gate_resolve_endpoint_does_not_exist(client: TestClient) -> None:
    """La resolución del Human Gate por HTTP fue eliminada de ENGINE-0.

    La especificación original ya indicaba que el gate no necesitaba interfaz
    externa. Sin endpoint de resolución, la única vía es ``Camus.resume()``, que
    exige una solicitud ``APPROVED`` y valida la decisión vinculada.
    """
    created = client.post(
        "/tasks", json={"objective": "Desplegar", "action": "deploy_production"}
    ).json()
    approval_id = created["human_approval_id"]
    assert approval_id is not None

    resolve = client.post(f"/human-gate/{approval_id}/resolve", json={"approved": True})
    assert resolve.status_code == 404

    # Tampoco se puede mutar la solicitud con otros métodos sobre la ruta GET.
    patch = client.patch(f"/human-gate/{approval_id}", json={"status": "APPROVED"})
    assert patch.status_code == 405

    # El gate sigue pendiente y la tarea sigue esperando decisión humana.
    assert client.get(f"/human-gate/{approval_id}").json()["status"] == "PENDING"
    assert client.get(f"/tasks/{created['id']}").json()["status"] == "HUMAN_APPROVAL"


def test_human_gate_introspection_is_read_only(client: TestClient) -> None:
    """La introspección GET expone el vínculo del gate con su PolicyDecision."""
    created = client.post(
        "/tasks", json={"objective": "Desplegar", "action": "deploy_production"}
    ).json()

    pending = client.get("/human-gate", params={"pending_only": True}).json()
    assert pending["total"] == 1
    item = pending["items"][0]

    assert item["id"] == created["human_approval_id"]
    assert item["task_id"] == created["id"]
    assert item["status"] == "PENDING"
    assert item["is_pending"] is True
    # Vínculo inequívoco con la decisión que originó el gate (R1.2).
    assert item["policy_decision_id"] is not None

    detail = client.get(f"/human-gate/{item['id']}").json()
    assert detail["policy_decision_id"] == item["policy_decision_id"]
    assert detail["resolved_at"] is None


def test_unknown_human_gate_returns_404(client: TestClient) -> None:
    """Un gate inexistente devuelve 404."""
    assert client.get(f"/human-gate/{uuid4()}").status_code == 404


# ---------------------------------------------------------------------------
# Política, auditoría y metadatos
# ---------------------------------------------------------------------------
def test_policy_evaluate_endpoint(client: TestClient) -> None:
    """POST /policy/evaluate evalúa sin crear tarea."""
    response = client.post(
        "/policy/evaluate",
        json={"action": "modify_file", "files_changed": ["config/permissions.yaml"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is False
    assert payload["outcome"] == "REJECT"
    assert payload["protected_files"] == ["config/permissions.yaml"]
    assert client.get("/tasks").json()["total"] == 0


def test_authority_catalog_endpoint(client: TestClient) -> None:
    """GET /policy/authority expone el catálogo por nivel."""
    payload = client.get("/policy/authority").json()

    assert payload["default_deny"] is True
    assert "create_file" in payload["levels"]["0"]
    assert "install_dependency" in payload["levels"]["1"]
    assert "deploy_production" in payload["levels"]["3"]
    assert "config/constitution.yaml" in payload["protected_files"]


def test_audit_events_endpoint(client: TestClient) -> None:
    """GET /audit/events expone los eventos generados en memoria."""
    created = client.post("/tasks", json={"objective": "Auditar", "action": "create_file"}).json()

    payload = client.get("/audit/events").json()

    assert payload["total"] > 0
    types = {item["event_type"] for item in payload["items"]}
    assert "TASK_CREATED" in types
    assert "TASK_TRANSITION" in types
    assert "POLICY_DECISION" in types

    filtered = client.get("/audit/events", params={"resource_id": created["id"]}).json()
    assert filtered["total"] > 0
    assert all(item["resource_id"] == created["id"] for item in filtered["items"])


def test_engine_endpoint_reports_no_llm(client: TestClient) -> None:
    """GET /engine confirma que no hay IA ni integraciones externas activas."""
    payload = client.get("/engine").json()

    assert payload["engine"] == ENGINE_NAME
    assert payload["version"] == ENGINE_VERSION
    assert payload["phase"] == "ENGINE-0"
    assert payload["integrity"]["llm_enabled"] is False
    assert payload["integrity"]["external_integrations"] == []
    assert payload["integrity"]["default_deny"] is True


def test_openapi_schema_is_available(client: TestClient) -> None:
    """El esquema OpenAPI se genera y no expone rutas de mutación de estado."""
    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/health" in paths
    assert "/engine" in paths
    assert "/tasks" in paths
    assert "/tasks/{task_id}" in paths

    # Rutas eliminadas en ENGINE-0.R1: ninguna permite manipular el estado.
    assert "/tasks/{task_id}/transitions" not in paths
    assert "/human-gate/{approval_id}/resolve" not in paths

    # Sobre una tarea individual solo se admite lectura.
    assert set(paths["/tasks/{task_id}"]) == {"get"}
    assert set(paths["/human-gate/{approval_id}"]) == {"get"}


def test_state_is_isolated_between_apps(fastapi_app: FastAPI) -> None:
    """Dos aplicaciones no comparten estado en memoria."""
    with TestClient(fastapi_app) as first_client:
        first_client.post("/tasks", json={"objective": "Aislada", "action": "create_file"})
        assert first_client.get("/tasks").json()["total"] == 1

    with TestClient(create_app(environment="test")) as second_client:
        assert second_client.get("/tasks").json()["total"] == 0
