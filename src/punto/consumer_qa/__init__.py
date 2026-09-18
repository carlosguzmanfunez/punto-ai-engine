"""QA CONSUMER v0 — verificar una aplicación como la vería un usuario real.

PUNTO ya comprueba código, tests, tipos, lint, autoridad, regresiones y casos contractuales. Esta
capa comprueba otra cosa: **si la aplicación de verdad abre, funciona y se puede utilizar**.

```
APP -> INICIAR -> ABRIR EN NAVEGADOR -> OBSERVAR -> INTERACTUAR -> DETECTAR FALLOS
    -> CAPTURAR EVIDENCIA -> PASS / FAIL
```

Reutiliza la capa web que ya existe (sandbox endurecido + Playwright + Chromium + capturas
verificadas) y añade solo lo que faltaba: interacción de usuario y un veredicto de consumidor.

El consumidor **evalúa resultados**: no tiene autoridad sobre el motor y no modifica
capacidades, ``ResourceSet``, Human Gate ni políticas. Un error de infraestructura nunca se
convierte en ``PASS``.

    from punto.consumer_qa import CANONICAL_CASES, QATarget, run_consumer_qa

    argv = (("python3", "-m", "http.server", "4173"),)
    target = QATarget(workspace=app.parent, preview_argv=argv)
    result = run_consumer_qa(CANONICAL_CASES[0], target)
    print(result.status, result.failures, result.evidence)
"""

from punto.consumer_qa.browser import (
    DEFAULT_CONSUMER_VIEWPORT,
    QASessionResult,
    QASessionUnavailable,
    QATarget,
    run_browser_session,
    session_actions,
)
from punto.consumer_qa.cases import CANONICAL_CASES, case_by_id
from punto.consumer_qa.model import (
    ConsumerQACase,
    ConsumerQAError,
    QAActionRecord,
    QAEvidence,
    QAExpectation,
    QAExpectationKind,
    QAFailure,
    QAResult,
    QAStatus,
    QAStep,
    QAStepKind,
)
from punto.consumer_qa.runner import (
    QA_EXPERIENCE_TAGS,
    QAFailureExperience,
    failure_as_experience,
    record_failure,
    run_consumer_qa,
)

__all__ = [
    "CANONICAL_CASES",
    "DEFAULT_CONSUMER_VIEWPORT",
    "QA_EXPERIENCE_TAGS",
    "ConsumerQACase",
    "ConsumerQAError",
    "QAActionRecord",
    "QAEvidence",
    "QAExpectation",
    "QAExpectationKind",
    "QAFailure",
    "QAFailureExperience",
    "QAResult",
    "QASessionResult",
    "QASessionUnavailable",
    "QAStatus",
    "QAStep",
    "QAStepKind",
    "QATarget",
    "case_by_id",
    "failure_as_experience",
    "record_failure",
    "run_browser_session",
    "run_consumer_qa",
    "session_actions",
]
