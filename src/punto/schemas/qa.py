"""Esquemas del QA independiente (ENGINE-4).

Contratos de datos del rol QA: la tarea a evaluar (:class:`QATask`), el plan de
pruebas que propone el modelo (:class:`QAPlan`), la trazabilidad de cada criterio de
aceptación (:class:`AcceptanceCoverage`) y la evidencia final (:class:`QAReport`).

Principio que gobierna este módulo: **Developer ≠ QA**. El resultado del Developer es
contexto, nunca prueba. El estado final del reporte lo calcula PUNTO de forma
determinista a partir de la ejecución real; el modelo no puede escribirlo: el
contrato del plan rechaza cualquier clave desconocida, incluida ``status``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from punto.common import utc_now
from punto.schemas.execution import (
    DeveloperExecutionResult,
    ModelUsage,
    ValidationCheck,
)
from punto.schemas.planning import SCHEMA_VERSION, CapabilityGap

#: Longitud máxima del extracto de salida que se guarda como evidencia.
MAX_EVIDENCE_CHARS: Final[int] = 4_000

#: Longitud máxima del contenido de un archivo de prueba generado por QA.
MAX_TEST_FILE_CHARS: Final[int] = 120_000

#: Máximo de archivos de prueba que QA puede proponer en un plan.
MAX_TEST_FILES: Final[int] = 20


class QATestType(StrEnum):
    """Naturaleza de un caso de prueba diseñado por QA.

    ``E2E`` y las pruebas de navegador **no** existen todavía: exigirían capacidades
    que PUNTO no tiene, y prometerlas sería fingir cobertura.
    """

    UNIT = "UNIT"
    INTEGRATION = "INTEGRATION"
    REGRESSION = "REGRESSION"
    STATIC = "STATIC"


class AcceptanceCoverageStatus(StrEnum):
    """Estado de un criterio de aceptación en el plan y en el reporte.

    El **plan** solo admite ``COVERED`` y ``UNTESTABLE``: son las dos únicas cosas que
    el modelo puede afirmar. ``FAILED`` y ``NOT_EXECUTED`` son consecuencia de la
    ejecución y usarlas en un plan es una violación, no una opción.
    """

    COVERED = "COVERED"
    UNTESTABLE = "UNTESTABLE"
    FAILED = "FAILED"
    NOT_EXECUTED = "NOT_EXECUTED"

    @property
    def is_complete(self) -> bool:
        """True solo si el criterio quedó demostrado por pruebas ejecutadas."""
        return self is AcceptanceCoverageStatus.COVERED

    @property
    def is_plannable(self) -> bool:
        """True si el modelo puede declarar este estado en un plan."""
        return self in _PLANNABLE_COVERAGE_STATUSES


#: Estados que un plan puede declarar. El resto los produce la ejecución.
_PLANNABLE_COVERAGE_STATUSES: Final[frozenset[AcceptanceCoverageStatus]] = frozenset(
    {AcceptanceCoverageStatus.COVERED, AcceptanceCoverageStatus.UNTESTABLE}
)


class QAFailureCategory(StrEnum):
    """Por qué falló algo durante la evaluación de QA.

    Es la distinción que impide el peor error posible de un QA automático: confundir
    «el producto está mal» con «mi prueba está mal» y arreglar la prueba.
    """

    #: La implementación no cumple el criterio de aceptación.
    PRODUCT_FAILURE = "PRODUCT_FAILURE"
    #: La prueba que generó QA es inválida (sintaxis, colección, uso incorrecto).
    QA_TEST_FAILURE = "QA_TEST_FAILURE"
    #: El entorno no permitió ejecutar (sandbox caído, timeout, binario ausente).
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
    #: PUNTO no tiene la capacidad necesaria (perfil de ejecución, servicio, validador).
    CAPABILITY_GAP = "CAPABILITY_GAP"

    @property
    def is_product_defect(self) -> bool:
        """True si la causa es el producto y no la prueba ni la infraestructura."""
        return self is QAFailureCategory.PRODUCT_FAILURE


class QASeverity(StrEnum):
    """Gravedad de un hallazgo de QA."""

    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class QAStatus(StrEnum):
    """Estado final de una evaluación de QA, calculado por PUNTO."""

    #: Todos los criterios obligatorios quedaron demostrados por pruebas ejecutadas.
    PASS = "PASS"
    #: Existe al menos un fallo del producto.
    FAIL = "FAIL"
    #: No se pudo evaluar: plan irrecuperable, capacidad ausente o infraestructura.
    BLOCKED = "BLOCKED"

    @property
    def succeeded(self) -> bool:
        """True solo si QA declaró PASS."""
        return self is QAStatus.PASS


# ---------------------------------------------------------------------------
# Tarea de QA
# ---------------------------------------------------------------------------
class QATask(BaseModel):
    """Trabajo del Developer que QA debe evaluar de forma independiente.

    El resultado del Developer viaja como **contexto**: sirve para saber qué se hizo y
    dónde, no para concluir nada. ``developer_claimed_pass`` existe para poder
    demostrar en pruebas que QA **no** lo usa como evidencia.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador de la evaluación.")
    created_at: datetime = Field(default_factory=utc_now, description="Momento de creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    task_id: UUID = Field(..., description="Tarea evaluada.")
    project_id: UUID = Field(..., description="Proyecto al que pertenece.")
    objective: str = Field(..., min_length=1, description="Objetivo de la tarea.")

    acceptance_criteria: tuple[str, ...] = Field(
        default=(),
        description=(
            "Contrato de la tarea. Cada criterio recibe un identificador determinista "
            "``AC-1``, ``AC-2``… en el orden recibido."
        ),
    )
    changed_files: tuple[str, ...] = Field(
        default=(), description="Archivos que el Developer modificó."
    )
    context_files: tuple[str, ...] = Field(
        default=(), description="Archivos que QA puede leer como contexto."
    )
    validation_checks: tuple[str, ...] = Field(
        default=(), description="Checks que el Developer declaró (contexto, no prueba)."
    )
    required_capabilities: tuple[str, ...] = Field(
        default=(), description="Capacidades que la tarea exige."
    )
    capability_profile: tuple[str, ...] = Field(
        default=(),
        description="Vocabulario de capacidades del proyecto, tal como lo declaró el Architect.",
    )
    test_only_paths: tuple[str, ...] = Field(
        default=(),
        description=(
            "Rutas que el proyecto declara explícitamente como solo-de-pruebas. "
            "Amplían la allowlist determinista de QA."
        ),
    )
    workspace_path: str = Field(
        ..., min_length=1, description="Workspace candidato del Developer (no se modifica)."
    )
    architecture_context: str = Field(
        default="", description="Resumen de la arquitectura relevante para la tarea."
    )
    project_spec_context: str = Field(
        default="", description="Resumen de la especificación relevante para la tarea."
    )
    developer_result: DeveloperExecutionResult | None = Field(
        default=None,
        description=(
            "Evidencia del Developer. **Contexto**, nunca prueba: el PASS del Developer "
            "no es el PASS de QA."
        ),
    )

    @property
    def criterion_ids(self) -> tuple[str, ...]:
        """Identificadores deterministas de los criterios, en orden."""
        return tuple(f"AC-{index}" for index in range(1, len(self.acceptance_criteria) + 1))

    @property
    def criteria_by_id(self) -> dict[str, str]:
        """Mapa ``AC-n -> enunciado`` del contrato."""
        return dict(zip(self.criterion_ids, self.acceptance_criteria, strict=False))

    @property
    def developer_claimed_pass(self) -> bool:
        """True si el Developer declaró que su validación pasó.

        Se expone **solo** para que sea verificable que QA no lo usa como evidencia.
        """
        result = self.developer_result
        if result is None or result.validation is None:
            return False
        return result.validation.passed


# ---------------------------------------------------------------------------
# Plan de QA
# ---------------------------------------------------------------------------
class QATestCase(BaseModel):
    """Caso de prueba diseñado por QA, trazable a los criterios que cubre."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1, description="Identificador único, por ejemplo QU-1.")
    title: str = Field(..., min_length=1, description="Título corto.")
    objective: str = Field(..., min_length=1, description="Qué intenta demostrar.")
    type: QATestType = Field(default=QATestType.UNIT, description="Tipo de prueba.")
    acceptance_criteria_refs: tuple[str, ...] = Field(
        default=(), description="Criterios que cubre, por ejemplo AC-1."
    )
    expected_behavior: str = Field(
        ..., min_length=1, description="Comportamiento observable que se espera."
    )
    required_capabilities: tuple[str, ...] = Field(
        default=(), description="Capacidades necesarias para ejecutar esta prueba."
    )


class QATestFile(BaseModel):
    """Archivo de prueba que QA propone **añadir** al proyecto.

    QA nunca sobrescribe un archivo existente: hacerlo destruiría la evidencia del
    Developer y permitiría «arreglar» una prueba en lugar del producto.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1, description="Identificador único del archivo.")
    path: str = Field(..., min_length=1, description="Ruta relativa, dentro de la zona de pruebas.")
    content: str = Field(
        ..., min_length=1, max_length=MAX_TEST_FILE_CHARS, description="Contenido completo."
    )
    test_case_ids: tuple[str, ...] = Field(
        default=(), description="Casos que implementa este archivo."
    )


class AcceptanceCoverage(BaseModel):
    """Trazabilidad de un criterio de aceptación.

    En el **plan** refleja lo que el modelo afirma (COVERED o UNTESTABLE). En el
    **reporte** refleja lo que PUNTO verificó tras ejecutar. Ningún criterio puede
    desaparecer sin dejar constancia.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_id: str = Field(..., min_length=1, description="Identificador AC-n.")
    criterion: str = Field(default="", description="Enunciado del criterio.")
    status: AcceptanceCoverageStatus = Field(..., description="Estado del criterio.")
    test_case_ids: tuple[str, ...] = Field(
        default=(), description="Casos que lo cubren, si los hay."
    )
    reason: str = Field(
        default="", description="Motivo obligatorio cuando el criterio es UNTESTABLE."
    )


class QAPlanProposal(BaseModel):
    """Salida JSON completa del modelo QA, antes de la validación de PUNTO.

    No admite ``status`` ni ninguna otra clave: el veredicto de QA **no** lo escribe
    el modelo. Una propuesta que intente declararlo se rechaza entera.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(..., min_length=1, description="Resumen del plan de pruebas.")
    test_cases: tuple[QATestCase, ...] = Field(
        default=(), description="Casos de prueba diseñados."
    )
    test_file_changes: tuple[QATestFile, ...] = Field(
        default=(), description="Archivos de prueba a añadir."
    )
    checks: tuple[str, ...] = Field(
        default=(), description="Nombres de checks registrados que hay que ejecutar."
    )
    coverage_mapping: tuple[AcceptanceCoverage, ...] = Field(
        default=(), description="Cobertura declarada de cada criterio."
    )
    assumptions: tuple[str, ...] = Field(default=(), description="Supuestos del plan.")


class QAPlan(BaseModel):
    """Plan de QA validado por PUNTO, listo para aplicarse sobre el overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador estable del plan.")
    created_at: datetime = Field(default_factory=utc_now, description="Momento de creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    task_id: UUID = Field(..., description="Tarea que evalúa.")
    project_id: UUID = Field(..., description="Proyecto al que pertenece.")
    attempt: int = Field(default=1, ge=1, description="Intento en el que se aceptó el plan.")
    prompt_version: str = Field(default="", description="Versión del prompt que lo produjo.")

    summary: str = Field(..., min_length=1, description="Resumen del plan.")
    test_cases: tuple[QATestCase, ...] = Field(default=(), description="Casos de prueba.")
    test_file_changes: tuple[QATestFile, ...] = Field(
        default=(), description="Archivos de prueba a añadir."
    )
    checks: tuple[str, ...] = Field(default=(), description="Checks a ejecutar.")
    coverage_mapping: tuple[AcceptanceCoverage, ...] = Field(
        default=(), description="Cobertura declarada y validada."
    )
    assumptions: tuple[str, ...] = Field(default=(), description="Supuestos del plan.")

    @property
    def test_case_ids(self) -> tuple[str, ...]:
        """Identificadores de los casos, en orden."""
        return tuple(case.id for case in self.test_cases)

    def case_by_id(self, case_id: str) -> QATestCase | None:
        """Caso por identificador, o ``None`` si no existe."""
        for case in self.test_cases:
            if case.id == case_id:
                return case
        return None


# ---------------------------------------------------------------------------
# Ejecución y reporte
# ---------------------------------------------------------------------------
class QAExecutedCheck(BaseModel):
    """Resultado de un check del registry ejecutado por PUNTO en el sandbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1, description="Nombre del check registrado.")
    command: str = Field(default="", description="Comando declarado por PUNTO, no por el modelo.")
    exit_code: int = Field(default=0, description="Código de salida observado.")
    passed: bool = Field(default=False, description="True si el check pasó.")
    timed_out: bool = Field(default=False, description="True si agotó el timeout.")
    blocked: bool = Field(default=False, description="True si la política lo bloqueó.")
    duration_ms: int = Field(default=0, ge=0, description="Duración observada.")
    output_excerpt: str = Field(
        default="", description=f"Extracto de salida (máximo {MAX_EVIDENCE_CHARS} caracteres)."
    )
    failure: QAFailureCategory | None = Field(
        default=None, description="Clasificación del fallo, si lo hubo."
    )

    @classmethod
    def from_validation_check(
        cls,
        check: ValidationCheck,
        *,
        failure: QAFailureCategory | None,
    ) -> QAExecutedCheck:
        """Construye la evidencia a partir de un ``ValidationCheck`` real."""
        output = "\n".join(part for part in (check.stdout, check.stderr) if part).strip()
        return cls(
            name=check.name,
            command=check.command,
            exit_code=check.exit_code,
            passed=check.passed,
            timed_out=check.timed_out,
            blocked=check.blocked,
            duration_ms=check.duration_ms,
            output_excerpt=output[:MAX_EVIDENCE_CHARS],
            failure=failure,
        )


class QAFinding(BaseModel):
    """Hallazgo de QA, con la evidencia que lo sostiene."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1, description="Identificador del hallazgo.")
    severity: QASeverity = Field(..., description="Gravedad.")
    category: QAFailureCategory = Field(..., description="Causa del hallazgo.")
    title: str = Field(..., min_length=1, description="Título corto.")
    description: str = Field(default="", description="Explicación del hallazgo.")
    acceptance_criterion: str = Field(
        default="", description="Criterio afectado (AC-n), si aplica."
    )
    file: str = Field(default="", description="Archivo implicado, si aplica.")
    evidence: str = Field(default="", description="Evidencia observada, nunca inventada.")
    repair_hint: str = Field(
        default="", description="Qué debería cambiar el Developer. QA no lo implementa."
    )


class QAReport(BaseModel):
    """Resultado completo de una evaluación de QA.

    ``status`` lo calcula PUNTO de forma determinista a partir de la evidencia
    ejecutada: el modelo no interviene.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador del reporte.")
    created_at: datetime = Field(default_factory=utc_now, description="Momento de creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    task_id: UUID = Field(..., description="Tarea evaluada.")
    project_id: UUID = Field(..., description="Proyecto al que pertenece.")
    status: QAStatus = Field(..., description="Estado final calculado por PUNTO.")
    summary: str = Field(default="", description="Resumen del resultado.")

    plan: QAPlan | None = Field(default=None, description="Plan validado, si lo hubo.")
    coverage: tuple[AcceptanceCoverage, ...] = Field(
        default=(), description="Trazabilidad verificada de cada criterio."
    )
    test_cases: tuple[QATestCase, ...] = Field(
        default=(), description="Casos de prueba del plan aceptado."
    )
    executed_checks: tuple[QAExecutedCheck, ...] = Field(
        default=(), description="Checks ejecutados y su resultado."
    )
    findings: tuple[QAFinding, ...] = Field(default=(), description="Hallazgos.")
    evidence: tuple[str, ...] = Field(
        default=(), description="Notas de evidencia del ciclo completo."
    )
    capability_gaps: tuple[CapabilityGap, ...] = Field(
        default=(), description="Capacidades ausentes que impidieron comprobar algo."
    )

    provider: str = Field(default="", description="Proveedor del modelo.")
    model: str = Field(default="", description="Modelo usado.")
    prompt_version: str = Field(default="", description="Versión del prompt de QA.")
    model_calls: int = Field(default=0, ge=0, description="Llamadas al modelo.")
    attempts: int = Field(default=0, ge=0, description="Intentos de plan usados.")
    test_repairs: int = Field(
        default=0, ge=0, description="Reparaciones de pruebas de QA (no del producto)."
    )
    model_usage: ModelUsage = Field(default_factory=ModelUsage, description="Consumo de tokens.")
    workspace: str = Field(default="", description="Overlay desechable usado (se destruye).")

    started_at: datetime = Field(default_factory=utc_now, description="Inicio de la evaluación.")
    completed_at: datetime | None = Field(default=None, description="Fin de la evaluación.")
    error: str = Field(default="", description="Motivo del bloqueo, si lo hubo.")

    @property
    def succeeded(self) -> bool:
        """True solo si el estado final es PASS."""
        return self.status is QAStatus.PASS

    @property
    def product_failures(self) -> tuple[QAFinding, ...]:
        """Hallazgos que señalan un defecto del producto."""
        return tuple(
            finding
            for finding in self.findings
            if finding.category is QAFailureCategory.PRODUCT_FAILURE
        )

    @property
    def uncovered_criteria(self) -> tuple[AcceptanceCoverage, ...]:
        """Criterios que no quedaron demostrados por pruebas ejecutadas."""
        return tuple(item for item in self.coverage if not item.status.is_complete)


__all__ = [
    "MAX_EVIDENCE_CHARS",
    "MAX_TEST_FILES",
    "MAX_TEST_FILE_CHARS",
    "AcceptanceCoverage",
    "AcceptanceCoverageStatus",
    "QAExecutedCheck",
    "QAFailureCategory",
    "QAFinding",
    "QAPlan",
    "QAPlanProposal",
    "QAReport",
    "QASeverity",
    "QAStatus",
    "QATask",
    "QATestCase",
    "QATestFile",
    "QATestType",
]
