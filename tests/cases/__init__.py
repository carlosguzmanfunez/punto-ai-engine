"""CASE DIRECTORY v0 — casos canónicos reejecutables del motor PUNTO.

Un **caso** declara una situación conocida del motor, lo que entra, lo que se espera y la garantía
que protege. El directorio los carga (fail-closed), los ejecuta por los caminos reales del motor y
compara ``expected`` contra lo observado. No sustituye a pytest: lo usa.

    from cases import load_cases, run_cases

    for result in run_cases(load_cases()):
        print(result.case_id, result.status)
"""

from cases.loader import load_cases
from cases.model import (
    CaseCategory,
    CaseDirectoryError,
    CaseResult,
    CaseStatus,
    EngineCase,
    Observation,
)
from cases.runner import compare, run_case, run_cases

__all__ = [
    "CaseCategory",
    "CaseDirectoryError",
    "CaseResult",
    "CaseStatus",
    "EngineCase",
    "Observation",
    "compare",
    "load_cases",
    "run_case",
    "run_cases",
]
