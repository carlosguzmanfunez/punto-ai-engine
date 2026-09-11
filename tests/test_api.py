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


def test_invalid_transition_returns_409(client: TestClient) -> None:
    """Una transición imposible devuelve 409 con el detalle del conflicto."""
    created = client.post(
        "/tasks", json={"objective": "Transicionar", "action": "create_file"}
    ).json()

    response = client.post(
        f"/tasks/{created['id']}/transitions",
        json={"target": "ANALYZING"},
    )

    assert response.status_code == 409
    payload = response.json()
    assert payload["current"] == "COMPLETED"
    assert payload["target"] == "ANALYZING"


def test_valid_transition_is_applied(client: TestClient) -> None:
    """Una transición válida se aplica a través de la API."""
    created = client.post(
        "/tasks",
        json={"objective": "Pendiente", "action": "deploy_production"},
    ).json()

    response = client.post(
        f"/tasks/{created['id']}/transitions",
        json={"target": "CANCELLED", "reason": "cancelada por prueba"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


# ---------------------------------------------------------------------------
# Human Gate por API
# ---------------------------------------------------------------------------
def test_human_gate_can_be_approved_through_api(client: TestClient) -> None:
    """Un Human Gate aprobado por API reanuda y completa la tarea."""
    created = client.post(
        "/tasks", json={"objective": "Desplegar", "action": "deploy_production"}
    ).json()
    approval_id = created["human_approval_id"]
    assert approval_id is not None

    pending = client.get("/human-gate", params={"pending_only": True}).json()
    assert pending["total"] == 1
    assert pending["items"][0]["id"] == approval_id

    response = client.post(
        f"/human-gate/{approval_id}/resolve",
        json={"approved": True, "resolved_by": "carlos"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["outcome"] == "COMPLETED"
    assert payload["status"] == "COMPLETED"

    detail = client.get(f"/tasks/{created['id']}").json()
    assert detail["status"] == "COMPLETED"


def test_human_gate_rejection_cancels_the_task(client: TestClient) -> None:
    """Un Human Gate rechazado por API cancela la tarea."""
    created = client.post(
        "/tasks",
        json={"objective": "Eliminar base de producción", "action": "production_database_delete"},
    ).json()

    response = client.post(
        f"/human-gate/{created['human_approval_id']}/resolve",
        json={"approved": False, "resolved_by": "carlos", "note": "no autorizado"},
    )

    assert response.status_code == 200
    assert response.json()["outcome"] == "REJECTED"
    assert response.json()["status"] == "CANCELLED"


def test_human_gate_double_resolution_returns_409(client: TestClient) -> None:
    """Resolver dos veces el mismo gate devuelve 409."""
    created = client.post(
        "/tasks", json={"objective": "Desplegar", "action": "deploy_production"}
    ).json()
    approval_id = created["human_approval_id"]
    client.post(f"/human-gate/{approval_id}/resolve", json={"approved": True})

    response = client.post(f"/human-gate/{approval_id}/resolve", json={"approved": True})

    assert response.status_code == 409


def test_unknown_human_gate_returns_404(client: TestClient) -> None:
    """Un gate inexistente devuelve 404."""
    assert client.get(f"/human-gate/{uuid4()}").status_code == 404
    assert client.post(f"/human-gate/{uuid4()}/resolve", json={"approved": True}).status_code == 404


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
    """El esquema OpenAPI se genera correctamente."""
    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/health" in paths
    assert "/tasks" in paths
    assert "/tasks/{task_id}" in paths


def test_state_is_isolated_between_apps(fastapi_app: FastAPI) -> None:
    """Dos aplicaciones no comparten estado en memoria."""
    with TestClient(fastapi_app) as first_client:
        first_client.post("/tasks", json={"objective": "Aislada", "action": "create_file"})
        assert first_client.get("/tasks").json()["total"] == 1

    with TestClient(create_app(environment="test")) as second_client:
        assert second_client.get("/tasks").json()["total"] == 0
