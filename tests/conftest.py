"""Fixtures compartidas de la suite de pruebas de ENGINE-0.

Las fixtures construyen el motor real (configuración YAML real, código real) sin
dobles de prueba: las pruebas ejercitan el comportamiento determinista de
producción.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from punto.api.app import Engine, create_app
from punto.audit.logger import AuditLogger
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.orchestrator.state_machine import StateMachine
from punto.policy.config_loader import ConfigLoader, find_config_dir
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.decision import ActionRequest
from punto.schemas.enums import RiskLevel
from punto.tasks.manager import TaskManager

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI
    from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def config_dir() -> Path:
    """Directorio ``config/`` real del repositorio."""
    return find_config_dir()


@pytest.fixture(scope="session")
def config_loader(config_dir: Path) -> ConfigLoader:
    """Cargador de configuración apuntando al ``config/`` real."""
    return ConfigLoader(config_dir)


@pytest.fixture
def policy_engine(config_dir: Path) -> PolicyEngine:
    """Policy Engine real construido desde los YAML del repositorio."""
    return PolicyEngine.from_config(config_dir)


@pytest.fixture
def audit_logger() -> AuditLogger:
    """Registro de auditoría en memoria vacío."""
    return AuditLogger()


@pytest.fixture
def state_machine() -> StateMachine:
    """Máquina de estados real."""
    return StateMachine()


@pytest.fixture
def task_manager(state_machine: StateMachine, audit_logger: AuditLogger) -> TaskManager:
    """Gestor de tareas en memoria con auditoría."""
    return TaskManager(state_machine=state_machine, audit=audit_logger)


@pytest.fixture
def human_gate() -> HumanGate:
    """Human Gate en memoria vacío."""
    return HumanGate()


@pytest.fixture
def camus(
    task_manager: TaskManager,
    policy_engine: PolicyEngine,
    human_gate: HumanGate,
    audit_logger: AuditLogger,
    state_machine: StateMachine,
) -> Camus:
    """Orquestador CAMUS ensamblado con dependencias reales."""
    return Camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit_logger,
        state_machine=state_machine,
        planner=Planner(),
    )


@pytest.fixture
def engine() -> Engine:
    """Contenedor del motor (mismo ensamblado que la API)."""
    return Engine(environment="test")


@pytest.fixture
def fastapi_app() -> FastAPI:
    """Aplicación FastAPI con su propio motor aislado."""
    return create_app(environment="test")


@pytest.fixture
def client(fastapi_app: FastAPI) -> Iterator[TestClient]:
    """Cliente de pruebas HTTP."""
    from fastapi.testclient import TestClient as _TestClient

    with _TestClient(fastapi_app) as test_client:
        yield test_client


@pytest.fixture
def low_risk_request() -> ActionRequest:
    """Petición Level 0 técnica, reversible y de riesgo LOW."""
    return ActionRequest(
        action="modify_file",
        technical=True,
        reversible=True,
        risk_level=RiskLevel.LOW,
        estimated_cost=0.1,
        estimated_minutes=2.0,
        files_changed=["src/punto/example.py"],
    )
