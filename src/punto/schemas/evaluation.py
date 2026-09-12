"""Evaluación completa de una tarea (ENGINE-5 §24).

Estructura que reúne los cuatro resultados de una tarea: lo que hizo el Developer, lo que
demostró QA, lo que encontró Security y lo que dictaminó el Reviewer.

Es **solo** una estructura de datos. ENGINE-5 no encadena esos roles
automáticamente: eso es el workflow de ENGINE-6. Aquí sirve para transportar la
evaluación completa entre fases sin volver a diseñar los esquemas.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from punto.common import utc_now
from punto.schemas.execution import DeveloperExecutionResult
from punto.schemas.planning import SCHEMA_VERSION
from punto.schemas.qa import QAReport, QAStatus
from punto.schemas.review import ReviewReport, ReviewStatus
from punto.schemas.security import SecurityReport, SecurityStatus


class TaskEvaluation(BaseModel):
    """Los cuatro informes de una tarea, con el resumen de sus veredictos."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador de la evaluación.")
    created_at: datetime = Field(default_factory=utc_now, description="Momento de creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    task_id: UUID = Field(..., description="Tarea evaluada.")
    project_id: UUID = Field(..., description="Proyecto al que pertenece.")

    developer_result: DeveloperExecutionResult | None = Field(
        default=None, description="Resultado del Developer."
    )
    qa_report: QAReport | None = Field(default=None, description="Informe de QA.")
    security_report: SecurityReport | None = Field(
        default=None, description="Informe de seguridad."
    )
    review_report: ReviewReport | None = Field(default=None, description="Informe de revisión.")

    @property
    def complete(self) -> bool:
        """True si están los cuatro resultados."""
        return all(
            part is not None
            for part in (
                self.developer_result,
                self.qa_report,
                self.security_report,
                self.review_report,
            )
        )

    @property
    def qa_status(self) -> QAStatus | None:
        """Estado de QA, si existe."""
        return None if self.qa_report is None else self.qa_report.status

    @property
    def security_status(self) -> SecurityStatus | None:
        """Estado de Security, si existe."""
        return None if self.security_report is None else self.security_report.status

    @property
    def review_status(self) -> ReviewStatus | None:
        """Veredicto del Reviewer, si existe."""
        return None if self.review_report is None else self.review_report.status

    @property
    def approved(self) -> bool:
        """True si el Reviewer aprobó."""
        return bool(self.review_report is not None and self.review_report.approved)


def build_task_evaluation(
    *,
    task_id: UUID,
    project_id: UUID,
    developer_result: DeveloperExecutionResult | None = None,
    qa_report: QAReport | None = None,
    security_report: SecurityReport | None = None,
    review_report: ReviewReport | None = None,
) -> TaskEvaluation:
    """Reúne los informes disponibles en una :class:`TaskEvaluation`.

    No ejecuta ningún rol: solo agrupa lo que ya existe.
    """
    return TaskEvaluation(
        task_id=task_id,
        project_id=project_id,
        developer_result=developer_result,
        qa_report=qa_report,
        security_report=security_report,
        review_report=review_report,
    )


__all__ = ["TaskEvaluation", "build_task_evaluation"]
