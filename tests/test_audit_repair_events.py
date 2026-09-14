"""Pruebas de la auditoría del bucle de reparación autónoma (ENGINE-6.1).

La auditoría de reparación es el único rastro de por qué el motor decidió volver a intentar,
desde qué etapa reinició, qué instantánea guardó y en qué momento se detuvo. Estas pruebas fijan
ese contrato evento a evento: que cada método registre su tipo, su acción y su ``resource_id``
(el ``workflow_id``), y que los valores que recibe lleguen al ``metadata`` sin pérdidas.

Son deterministas: no hay proveedores, ni reloj, ni sistema de ficheros; solo un
``AuditLogger()`` en memoria y UUID generados al vuelo.
"""

from __future__ import annotations

from uuid import uuid4

from punto.audit.logger import AuditLogger
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import AuditResult

#: Claves que jamás deben aparecer en los metadatos de un evento de reparación.
CLAVES_PROHIBIDAS = frozenset({"prompt", "chain_of_thought", "api_key", "secret"})


def _cuatro_ids() -> tuple:
    """Cuatro identificadores frescos (proyecto, tarea, workflow y reparación)."""
    return uuid4(), uuid4(), uuid4(), uuid4()


# ---------------------------------------------------------------------------
# Un caso por método del logger
# ---------------------------------------------------------------------------
def test_repair_decided_registra_tipo_accion_recurso_y_metadata() -> None:
    """La decisión de reparar se registra en PENDING con su veredicto y sus hallazgos."""
    audit = AuditLogger()
    project_id, task_id, workflow_id, repair_id = _cuatro_ids()

    event = audit.log_workflow_repair_decided(
        project_id=project_id,
        task_id=task_id,
        workflow_id=workflow_id,
        repair_id=repair_id,
        cycle=2,
        repairability="AUTO",
        finding_ids=["finding-1", "finding-2"],
        requires_human=False,
        policy_decision_id="policy-1",
        reason="fallo reproducible",
        actor="reparador",
    )

    (registrado,) = audit.by_type(AuditEventType.WORKFLOW_REPAIR_DECIDED)
    assert registrado is event
    assert AuditEventType.WORKFLOW_REPAIR_DECIDED in audit.types_present()
    assert event.action == "workflow_repair_decided"
    assert event.resource_id == str(workflow_id)
    assert event.result is AuditResult.PENDING
    assert event.actor == "reparador"
    metadata = event.metadata_dict
    assert metadata["project_id"] == str(project_id)
    assert metadata["task_id"] == str(task_id)
    assert metadata["workflow_id"] == str(workflow_id)
    assert metadata["repair_id"] == str(repair_id)
    assert metadata["cycle"] == 2
    assert metadata["repairability"] == "AUTO"
    assert metadata["finding_ids"] == ("finding-1", "finding-2")
    assert metadata["requires_human"] is False
    assert metadata["policy_decision_id"] == "policy-1"
    assert metadata["reason"] == "fallo reproducible"


def test_repair_started_registra_tipo_accion_recurso_y_metadata() -> None:
    """El arranque del ciclo deja constancia de la etapa de origen y la de reinicio."""
    audit = AuditLogger()
    project_id, task_id, workflow_id, repair_id = _cuatro_ids()

    event = audit.log_workflow_repair_started(
        project_id=project_id,
        task_id=task_id,
        workflow_id=workflow_id,
        repair_id=repair_id,
        cycle=1,
        origin_stage="QA",
        restart_stage="DEVELOPER",
        plan_fingerprint="fp-plan-1",
        actor="reparador",
    )

    (registrado,) = audit.by_type(AuditEventType.WORKFLOW_REPAIR_STARTED)
    assert registrado is event
    assert AuditEventType.WORKFLOW_REPAIR_STARTED in audit.types_present()
    assert event.action == "workflow_repair_started"
    assert event.resource_id == str(workflow_id)
    assert event.result is AuditResult.SUCCESS
    metadata = event.metadata_dict
    assert metadata["project_id"] == str(project_id)
    assert metadata["task_id"] == str(task_id)
    assert metadata["workflow_id"] == str(workflow_id)
    assert metadata["repair_id"] == str(repair_id)
    assert metadata["cycle"] == 1
    assert metadata["origin_stage"] == "QA"
    assert metadata["restart_stage"] == "DEVELOPER"
    assert metadata["plan_fingerprint"] == "fp-plan-1"


def test_repair_snapshot_created_registra_tipo_accion_recurso_y_metadata() -> None:
    """La instantánea previa al parche queda auditada con su número de ficheros."""
    audit = AuditLogger()
    project_id, task_id, workflow_id, repair_id = _cuatro_ids()
    snapshot_id = uuid4()

    event = audit.log_workflow_repair_snapshot_created(
        project_id=project_id,
        task_id=task_id,
        workflow_id=workflow_id,
        repair_id=repair_id,
        cycle=3,
        snapshot_id=snapshot_id,
        files=7,
        workspace_fingerprint="fp-workspace-1",
        actor="reparador",
    )

    (registrado,) = audit.by_type(AuditEventType.WORKFLOW_REPAIR_SNAPSHOT_CREATED)
    assert registrado is event
    assert AuditEventType.WORKFLOW_REPAIR_SNAPSHOT_CREATED in audit.types_present()
    assert event.action == "workflow_repair_snapshot_created"
    assert event.resource_id == str(workflow_id)
    assert event.result is AuditResult.SUCCESS
    metadata = event.metadata_dict
    assert metadata["project_id"] == str(project_id)
    assert metadata["task_id"] == str(task_id)
    assert metadata["workflow_id"] == str(workflow_id)
    assert metadata["repair_id"] == str(repair_id)
    assert metadata["cycle"] == 3
    assert metadata["snapshot_id"] == str(snapshot_id)
    assert metadata["files"] == 7
    assert metadata["workspace_fingerprint"] == "fp-workspace-1"


def test_repair_applied_registra_tipo_accion_recurso_y_metadata() -> None:
    """La aplicación del parche registra cuántos ficheros cambió y con qué estado."""
    audit = AuditLogger()
    project_id, task_id, workflow_id, repair_id = _cuatro_ids()

    event = audit.log_workflow_repair_applied(
        project_id=project_id,
        task_id=task_id,
        workflow_id=workflow_id,
        repair_id=repair_id,
        cycle=1,
        changed_files=3,
        status="APPLIED",
        detail="parche aplicado sin conflictos",
        actor="reparador",
    )

    (registrado,) = audit.by_type(AuditEventType.WORKFLOW_REPAIR_APPLIED)
    assert registrado is event
    assert AuditEventType.WORKFLOW_REPAIR_APPLIED in audit.types_present()
    assert event.action == "workflow_repair_applied"
    assert event.resource_id == str(workflow_id)
    assert event.result is AuditResult.SUCCESS
    metadata = event.metadata_dict
    assert metadata["project_id"] == str(project_id)
    assert metadata["task_id"] == str(task_id)
    assert metadata["workflow_id"] == str(workflow_id)
    assert metadata["repair_id"] == str(repair_id)
    assert metadata["cycle"] == 1
    assert metadata["changed_files"] == 3
    assert metadata["status"] == "APPLIED"
    assert metadata["detail"] == "parche aplicado sin conflictos"


def test_repair_verification_started_registra_tipo_accion_recurso_y_metadata() -> None:
    """La verificación posterior se registra en PENDING con los roles que la ejecutan."""
    audit = AuditLogger()
    project_id, task_id, workflow_id, repair_id = _cuatro_ids()

    event = audit.log_workflow_repair_verification_started(
        project_id=project_id,
        task_id=task_id,
        workflow_id=workflow_id,
        repair_id=repair_id,
        cycle=1,
        roles=["QA", "SECURITY"],
        actor="reparador",
    )

    (registrado,) = audit.by_type(AuditEventType.WORKFLOW_REPAIR_VERIFICATION_STARTED)
    assert registrado is event
    assert AuditEventType.WORKFLOW_REPAIR_VERIFICATION_STARTED in audit.types_present()
    assert event.action == "workflow_repair_verification_started"
    assert event.resource_id == str(workflow_id)
    assert event.result is AuditResult.PENDING
    metadata = event.metadata_dict
    assert metadata["project_id"] == str(project_id)
    assert metadata["task_id"] == str(task_id)
    assert metadata["workflow_id"] == str(workflow_id)
    assert metadata["repair_id"] == str(repair_id)
    assert metadata["cycle"] == 1
    assert metadata["roles"] == ("QA", "SECURITY")


def test_repair_resolved_registra_tipo_accion_recurso_y_metadata() -> None:
    """El cierre del ciclo separa los hallazgos resueltos de los que siguen abiertos."""
    audit = AuditLogger()
    project_id, task_id, workflow_id, repair_id = _cuatro_ids()

    event = audit.log_workflow_repair_resolved(
        project_id=project_id,
        task_id=task_id,
        workflow_id=workflow_id,
        repair_id=repair_id,
        cycle=2,
        resolved_findings=["finding-1"],
        unresolved_findings=["finding-3", "finding-4"],
        actor="reparador",
    )

    (registrado,) = audit.by_type(AuditEventType.WORKFLOW_REPAIR_RESOLVED)
    assert registrado is event
    assert AuditEventType.WORKFLOW_REPAIR_RESOLVED in audit.types_present()
    assert event.action == "workflow_repair_resolved"
    assert event.resource_id == str(workflow_id)
    assert event.result is AuditResult.SUCCESS
    metadata = event.metadata_dict
    assert metadata["project_id"] == str(project_id)
    assert metadata["task_id"] == str(task_id)
    assert metadata["workflow_id"] == str(workflow_id)
    assert metadata["repair_id"] == str(repair_id)
    assert metadata["cycle"] == 2
    assert metadata["resolved_findings"] == ("finding-1",)
    assert metadata["unresolved_findings"] == ("finding-3", "finding-4")


def test_repair_failed_registra_tipo_accion_recurso_y_metadata() -> None:
    """El fallo del ciclo se registra como FAILURE con su código estable."""
    audit = AuditLogger()
    project_id, task_id, workflow_id, repair_id = _cuatro_ids()

    event = audit.log_workflow_repair_failed(
        project_id=project_id,
        task_id=task_id,
        workflow_id=workflow_id,
        repair_id=repair_id,
        cycle=1,
        code="REPAIR_PATCH_REJECTED",
        detail="el parche no compila",
        actor="reparador",
    )

    (registrado,) = audit.by_type(AuditEventType.WORKFLOW_REPAIR_FAILED)
    assert registrado is event
    assert AuditEventType.WORKFLOW_REPAIR_FAILED in audit.types_present()
    assert event.action == "workflow_repair_failed"
    assert event.resource_id == str(workflow_id)
    assert event.result is AuditResult.FAILURE
    metadata = event.metadata_dict
    assert metadata["project_id"] == str(project_id)
    assert metadata["task_id"] == str(task_id)
    assert metadata["workflow_id"] == str(workflow_id)
    assert metadata["repair_id"] == str(repair_id)
    assert metadata["cycle"] == 1
    assert metadata["code"] == "REPAIR_PATCH_REJECTED"
    assert metadata["detail"] == "el parche no compila"


def test_repair_no_progress_registra_tipo_accion_recurso_y_metadata() -> None:
    """La falta de progreso se deniega dejando la huella repetida y su número de repeticiones."""
    audit = AuditLogger()
    project_id, task_id, workflow_id, repair_id = _cuatro_ids()

    event = audit.log_workflow_repair_no_progress(
        project_id=project_id,
        task_id=task_id,
        workflow_id=workflow_id,
        repair_id=repair_id,
        cycle=4,
        fingerprint="fp-sin-cambios",
        repeats=2,
        detail="misma huella que el ciclo anterior",
        actor="reparador",
    )

    (registrado,) = audit.by_type(AuditEventType.WORKFLOW_REPAIR_NO_PROGRESS)
    assert registrado is event
    assert AuditEventType.WORKFLOW_REPAIR_NO_PROGRESS in audit.types_present()
    assert event.action == "workflow_repair_no_progress"
    assert event.resource_id == str(workflow_id)
    assert event.result is AuditResult.DENIED
    metadata = event.metadata_dict
    assert metadata["project_id"] == str(project_id)
    assert metadata["task_id"] == str(task_id)
    assert metadata["workflow_id"] == str(workflow_id)
    assert metadata["repair_id"] == str(repair_id)
    assert metadata["cycle"] == 4
    assert metadata["fingerprint"] == "fp-sin-cambios"
    assert metadata["repeats"] == 2
    assert metadata["detail"] == "misma huella que el ciclo anterior"


def test_repair_budget_exhausted_registra_tipo_accion_recurso_y_metadata() -> None:
    """El agotamiento del presupuesto de reparaciones se deniega nombrando ambas cifras."""
    audit = AuditLogger()
    workflow_id = uuid4()
    project_id = uuid4()
    task_id = uuid4()

    event = audit.log_workflow_repair_budget_exhausted(
        project_id=project_id,
        task_id=task_id,
        workflow_id=workflow_id,
        repairs_used=3,
        max_repairs=3,
        detail="tope de reparaciones alcanzado",
        actor="reparador",
    )

    (registrado,) = audit.by_type(AuditEventType.WORKFLOW_REPAIR_BUDGET_EXHAUSTED)
    assert registrado is event
    assert AuditEventType.WORKFLOW_REPAIR_BUDGET_EXHAUSTED in audit.types_present()
    assert event.action == "workflow_repair_budget_exhausted"
    assert event.resource_id == str(workflow_id)
    assert event.result is AuditResult.DENIED
    metadata = event.metadata_dict
    assert metadata["project_id"] == str(project_id)
    assert metadata["task_id"] == str(task_id)
    assert metadata["workflow_id"] == str(workflow_id)
    assert metadata["repairs_used"] == 3
    assert metadata["max_repairs"] == 3
    assert metadata["detail"] == "tope de reparaciones alcanzado"


def test_repair_rolled_back_registra_tipo_accion_recurso_y_metadata() -> None:
    """El rollback queda auditado con la instantánea usada y los ficheros restaurados."""
    audit = AuditLogger()
    project_id, task_id, workflow_id, repair_id = _cuatro_ids()
    snapshot_id = uuid4()

    event = audit.log_workflow_repair_rolled_back(
        project_id=project_id,
        task_id=task_id,
        workflow_id=workflow_id,
        repair_id=repair_id,
        cycle=1,
        snapshot_id=snapshot_id,
        restored_files=5,
        detail="workspace restaurado",
        actor="reparador",
    )

    (registrado,) = audit.by_type(AuditEventType.WORKFLOW_REPAIR_ROLLED_BACK)
    assert registrado is event
    assert AuditEventType.WORKFLOW_REPAIR_ROLLED_BACK in audit.types_present()
    assert event.action == "workflow_repair_rolled_back"
    assert event.resource_id == str(workflow_id)
    assert event.result is AuditResult.SUCCESS
    metadata = event.metadata_dict
    assert metadata["project_id"] == str(project_id)
    assert metadata["task_id"] == str(task_id)
    assert metadata["workflow_id"] == str(workflow_id)
    assert metadata["repair_id"] == str(repair_id)
    assert metadata["cycle"] == 1
    assert metadata["snapshot_id"] == str(snapshot_id)
    assert metadata["restored_files"] == 5
    assert metadata["detail"] == "workspace restaurado"


def test_budget_reconciliation_authorized_registra_tipo_accion_recurso_y_metadata() -> None:
    """La autorización de reconciliación apunta a la prueba y a la brecha que la motivan."""
    audit = AuditLogger()
    project_id, task_id, workflow_id = uuid4(), uuid4(), uuid4()
    proof_id = uuid4()
    breach_id = uuid4()

    event = audit.log_workflow_budget_reconciliation_authorized(
        project_id=project_id,
        task_id=task_id,
        workflow_id=workflow_id,
        role="QA",
        step_index=4,
        proof_id=proof_id,
        breach_id=breach_id,
        policy_decision_id="policy-9",
        known_overrun_model_calls=2,
        known_overrun_tokens=1800,
        actor="camus",
    )

    (registrado,) = audit.by_type(AuditEventType.WORKFLOW_BUDGET_RECONCILIATION_AUTHORIZED)
    assert registrado is event
    assert AuditEventType.WORKFLOW_BUDGET_RECONCILIATION_AUTHORIZED in audit.types_present()
    assert event.action == "workflow_budget_reconciliation_authorized"
    assert event.resource_id == str(workflow_id)
    assert event.result is AuditResult.SUCCESS
    metadata = event.metadata_dict
    assert metadata["project_id"] == str(project_id)
    assert metadata["task_id"] == str(task_id)
    assert metadata["workflow_id"] == str(workflow_id)
    assert metadata["role"] == "QA"
    assert metadata["step_index"] == 4
    assert metadata["proof_id"] == str(proof_id)
    assert metadata["breach_id"] == str(breach_id)
    assert metadata["policy_decision_id"] == "policy-9"
    assert metadata["known_overrun_model_calls"] == 2
    assert metadata["known_overrun_tokens"] == 1800


# ---------------------------------------------------------------------------
# Propiedades transversales de la familia completa
# ---------------------------------------------------------------------------
def _registrar_los_once_eventos() -> AuditLogger:
    """Registra los once eventos de la familia de reparación y devuelve el log."""
    audit = AuditLogger()
    project_id, task_id, workflow_id, repair_id = _cuatro_ids()
    snapshot_id = uuid4()
    comun = {
        "project_id": project_id,
        "task_id": task_id,
        "workflow_id": workflow_id,
        "repair_id": repair_id,
        "cycle": 1,
    }
    audit.log_workflow_repair_decided(
        **comun,
        repairability="AUTO",
        finding_ids=["finding-1"],
        requires_human=False,
    )
    audit.log_workflow_repair_started(**comun, origin_stage="QA", restart_stage="DEVELOPER")
    audit.log_workflow_repair_snapshot_created(**comun, snapshot_id=snapshot_id, files=1)
    audit.log_workflow_repair_applied(**comun, changed_files=1, status="APPLIED")
    audit.log_workflow_repair_verification_started(**comun, roles=["QA"])
    audit.log_workflow_repair_resolved(
        **comun,
        resolved_findings=["finding-1"],
        unresolved_findings=[],
    )
    audit.log_workflow_repair_failed(**comun, code="REPAIR_FAILED")
    audit.log_workflow_repair_no_progress(**comun, fingerprint="fp", repeats=2)
    audit.log_workflow_repair_budget_exhausted(
        project_id=project_id,
        task_id=task_id,
        workflow_id=workflow_id,
        repairs_used=3,
        max_repairs=3,
    )
    audit.log_workflow_repair_rolled_back(**comun, snapshot_id=snapshot_id, restored_files=1)
    audit.log_workflow_budget_reconciliation_authorized(
        project_id=project_id,
        task_id=task_id,
        workflow_id=workflow_id,
        role="QA",
        step_index=1,
        proof_id=uuid4(),
        breach_id=uuid4(),
    )
    return audit


def test_ningun_evento_de_reparacion_registra_prompt_ni_secretos() -> None:
    """Ningún metadata de la familia lleva prompts, razonamiento interno, claves ni secretos."""
    audit = _registrar_los_once_eventos()

    assert audit.count() == 11
    for event in audit.events():
        assert not CLAVES_PROHIBIDAS.intersection(event.metadata_dict)


def test_detail_largo_se_recorta_a_trescientos_caracteres() -> None:
    """Un detalle de 500 caracteres se recorta a 300: el metadato nunca crece sin límite.

    Se comprueba en los cinco métodos que aceptan ``detail``, porque el recorte es una regla
    del módulo y no una casualidad de uno solo.
    """
    audit = AuditLogger()
    project_id, task_id, workflow_id, repair_id = _cuatro_ids()
    snapshot_id = uuid4()
    detalle_largo = "d" * 500
    eventos = (
        audit.log_workflow_repair_applied(
            project_id=project_id,
            task_id=task_id,
            workflow_id=workflow_id,
            repair_id=repair_id,
            cycle=1,
            changed_files=1,
            status="APPLIED",
            detail=detalle_largo,
        ),
        audit.log_workflow_repair_failed(
            project_id=project_id,
            task_id=task_id,
            workflow_id=workflow_id,
            repair_id=repair_id,
            cycle=1,
            code="REPAIR_FAILED",
            detail=detalle_largo,
        ),
        audit.log_workflow_repair_no_progress(
            project_id=project_id,
            task_id=task_id,
            workflow_id=workflow_id,
            repair_id=repair_id,
            cycle=1,
            fingerprint="fp",
            repeats=2,
            detail=detalle_largo,
        ),
        audit.log_workflow_repair_budget_exhausted(
            project_id=project_id,
            task_id=task_id,
            workflow_id=workflow_id,
            repairs_used=3,
            max_repairs=3,
            detail=detalle_largo,
        ),
        audit.log_workflow_repair_rolled_back(
            project_id=project_id,
            task_id=task_id,
            workflow_id=workflow_id,
            repair_id=repair_id,
            cycle=1,
            snapshot_id=snapshot_id,
            restored_files=1,
            detail=detalle_largo,
        ),
    )

    assert len(eventos) == 5
    for event in eventos:
        registrado = event.metadata_dict["detail"]
        assert isinstance(registrado, str)
        assert len(registrado) == 300
        assert registrado == detalle_largo[:300]
