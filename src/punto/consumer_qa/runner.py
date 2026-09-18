"""API del consumidor: ``run_consumer_qa(caso, objetivo)`` y el veredicto.

```
aplicación -> iniciar (preview del sandbox) -> abrir en el navegador -> observar -> interactuar
           -> detectar fallos -> capturar evidencia -> PASS / FAIL
```

El consumidor **evalúa resultados**: no arregla, no autoriza y no cambia nada del motor. Un error de
infraestructura —el navegador no arranca, la aplicación no responde, la evidencia no se puede
evaluar— nunca se convierte en ``PASS``: por defecto es ``FAIL``, y solo es ``SKIP`` si quien llama
declara explícitamente que esa dependencia ambiental está permitida por su contrato.

El ``FAIL`` deja además la información que PELL necesita para aprender del fallo, sin construir otro
bucle de aprendizaje: :func:`failure_as_experience` la describe y :func:`record_failure` la registra
con la interfaz de PELL-0/1.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from punto.consumer_qa.browser import (
    QASessionResult,
    QASessionUnavailable,
    QATarget,
    run_browser_session,
    session_actions,
)
from punto.consumer_qa.model import (
    BROWSER_EXPECTATIONS,
    ConsumerQACase,
    ConsumerQAError,
    QAEvidence,
    QAExpectation,
    QAExpectationKind,
    QAFailure,
    QAResult,
    QAStatus,
)
from punto.memory import ExperienceResult, ExperienceStatus, ExperienceStore
from punto.schemas.web import Viewport
from punto.web.sandbox import WebSandboxBackend

#: Texto acotado de un hecho observado en la evidencia.
MAX_OBSERVED_CHARS = 400
#: Etiquetas de las experiencias que produce un fallo de consumidor.
QA_EXPERIENCE_TAGS: tuple[str, ...] = ("consumer-qa", "ui", "interaccion")


def run_consumer_qa(
    case: ConsumerQACase,
    target: QATarget,
    *,
    evidence_dir: Path | None = None,
    viewport: Viewport | None = None,
    backend: WebSandboxBackend | None = None,
    allow_environmental_skip: bool = False,
) -> QAResult:
    """Ejecuta un caso de consumidor contra una aplicación y devuelve su resultado.

    Args:
        case: Caso canónico (qué se abre, qué se hace, qué se espera).
        target: Aplicación bajo prueba y su comando de arranque.
        evidence_dir: Directorio donde conservar la captura. Si es ``None``, la evidencia declara
            que los bytes no se persistieron en vez de fingir que no hubo captura.
        viewport: Viewport de la sesión; por defecto, el escritorio del contrato web.
        backend: Backend de navegador inyectable (pruebas de infraestructura).
        allow_environmental_skip: Si es ``True``, la ausencia del sandbox de navegador se declara
            ``SKIP`` en lugar de ``FAIL``. Por defecto es ``False``: fail-closed.

    Returns:
        El resultado estructurado: ``qa_id``, ``status``, ``failures`` y ``evidence``.
    """
    planned = session_actions(case.steps, case.expectations)
    try:
        session = run_browser_session(
            start_url=case.start_url,
            planned=planned,
            target=target,
            evidence_dir=evidence_dir,
            artifact_prefix=case.qa_id,
            viewport=viewport,
            backend=backend,
        )
    except QASessionUnavailable as error:
        status = QAStatus.SKIP if allow_environmental_skip else QAStatus.FAIL
        return QAResult(
            qa_id=case.qa_id,
            status=status,
            reason=str(error),
            infrastructure=True,
        )
    except ConsumerQAError as error:
        return QAResult(
            qa_id=case.qa_id,
            status=QAStatus.FAIL,
            reason=str(error),
            infrastructure=True,
        )

    failures = _evaluate(case=case, planned=planned, session=session)
    if failures:
        return QAResult(
            qa_id=case.qa_id,
            status=QAStatus.FAIL,
            failures=failures,
            evidence=session.evidence,
            reason=_failure_reason(failures),
            notes=session.notes,
        )
    return QAResult(
        qa_id=case.qa_id,
        status=QAStatus.PASS,
        evidence=session.evidence,
        reason="la aplicación se abrió, respondió y cumplió lo esperado",
        notes=session.notes,
    )


def _evaluate(
    *,
    case: ConsumerQACase,
    planned: tuple[tuple[str, dict[str, str]], ...],
    session: QASessionResult,
) -> tuple[QAFailure, ...]:
    """Compara lo esperado con lo observado: acciones del navegador y hechos de la sesión.

    La interacción se detiene en la primera acción que no puede ejecutarse, así que ahí termina la
    evaluación: lo que venía después no se midió y el resultado lo dice con el punto exacto.
    """
    failures: list[QAFailure] = []
    evidence = session.evidence
    observed_actions = evidence.actions
    for index, (label, action) in enumerate(planned):
        if index >= len(observed_actions):
            failures.append(
                QAFailure(
                    step=label,
                    expected=f"{action['kind']} se ejecuta en el navegador",
                    observed="la sesión no reportó esta acción",
                    detail="el navegador no dejó registro de un paso que sí se le pidió",
                )
            )
            return tuple(failures)
        record = observed_actions[index]
        if record.ok:
            continue
        is_expectation = label.startswith("expectativa")
        return (
            QAFailure(
                step=label,
                expected=(
                    label if is_expectation else f"{action['kind']} se ejecuta en el navegador"
                ),
                observed=(
                    "la expectativa no se cumplió en el plazo"
                    if is_expectation
                    else "la acción no pudo completarse"
                ),
                detail=_clip(record.detail),
            ),
        )

    for expectation in case.expectations:
        if expectation.kind in BROWSER_EXPECTATIONS:
            continue
        failure = _check_host_expectation(expectation, evidence)
        if failure is not None:
            failures.append(failure)
    return tuple(failures)


def _check_host_expectation(
    expectation: QAExpectation, evidence: QAEvidence
) -> QAFailure | None:
    """Comprueba una expectativa con los hechos que la sesión devolvió (URL, HTTP, consola)."""
    step = f"expectativa ({expectation.label()})"
    if expectation.kind is QAExpectationKind.URL_MATCHES:
        observed = evidence.final_url or evidence.final_route
        if expectation.target in observed:
            return None
        return QAFailure(
            step=step,
            expected=f"la URL final contiene {expectation.target!r}",
            observed=observed or "sin URL final observada",
        )
    if expectation.kind is QAExpectationKind.HTTP_OK:
        status = evidence.http_status
        if status is not None and 200 <= status < 400:
            return None
        return QAFailure(
            step=step,
            expected="una respuesta HTTP correcta (2xx o 3xx)",
            observed="sin respuesta" if status is None else f"HTTP {status}",
            detail=_clip(evidence.load_error),
        )
    if expectation.kind is QAExpectationKind.NO_CONSOLE_ERRORS:
        if not evidence.console_errors:
            return None
        return QAFailure(
            step=step,
            expected="ninguna consola de error",
            observed=f"{len(evidence.console_errors)} error(es) de consola",
            detail=_clip("; ".join(evidence.console_errors)),
        )
    if expectation.kind is QAExpectationKind.NO_JS_EXCEPTIONS:
        if not evidence.page_errors:
            return None
        return QAFailure(
            step=step,
            expected="ninguna excepción de JavaScript sin controlar",
            observed=f"{len(evidence.page_errors)} excepción(es)",
            detail=_clip("; ".join(evidence.page_errors)),
        )
    return QAFailure(
        step=step,
        expected="una expectativa que el consumidor sabe comprobar",
        observed=f"tipo no soportado: {expectation.kind.value}",
        detail="vocabulario cerrado: la expectativa no se puede evaluar",
    )


def _failure_reason(failures: tuple[QAFailure, ...]) -> str:
    """Motivo compacto y determinista del fallo, con el primer punto de ruptura."""
    first = failures[0]
    rest = f" (+{len(failures) - 1} más)" if len(failures) > 1 else ""
    return f"{first.step}: esperado {first.expected}; observado {first.observed}{rest}"


def _clip(text: str) -> str:
    """Acota un texto observado para que la evidencia no crezca sin control."""
    clean = " ".join(str(text).split())
    return clean[:MAX_OBSERVED_CHARS]


@dataclass(frozen=True, slots=True)
class QAFailureExperience:
    """Un ``FAIL`` de consumidor descrito como experiencia PELL (sin escribir nada)."""

    problem: str
    failure_reason: str
    observed: str
    expected: str
    evidence: tuple[str, ...]
    tags: tuple[str, ...]


def failure_as_experience(result: QAResult, *, problem: str = "") -> QAFailureExperience | None:
    """Describe un ``FAIL`` con los campos que PELL-0/1 saben registrar, o ``None`` si no falló."""
    if result.status is not QAStatus.FAIL or not result.failures:
        return None
    first = result.failures[0]
    evidence = result.evidence
    lines: list[str] = []
    if evidence is not None:
        lines.append(f"url: {evidence.final_url or evidence.url}")
        lines.append(f"http: {evidence.http_status}")
        if evidence.screenshot_path:
            lines.append(f"captura: {evidence.screenshot_path}")
        elif evidence.screenshot_note:
            lines.append(f"captura: no disponible ({evidence.screenshot_note})")
        for item in evidence.console_errors[:3]:
            lines.append(f"consola: {_clip(item)}")
    detalle = f"{first.expected} | observado: {first.observed} | {first.detail}"
    return QAFailureExperience(
        problem=problem or f"QA de consumidor: {result.qa_id} falló en {first.step}",
        failure_reason=detalle.strip(" |"),
        observed=first.observed,
        expected=first.expected,
        evidence=tuple(lines),
        tags=QA_EXPERIENCE_TAGS,
    )


def record_failure(
    store: ExperienceStore, result: QAResult, *, problem: str = ""
) -> str | None:
    """Registra el fallo como experiencia ``FAILED`` en la memoria de PELL.

    Reutiliza la interfaz de PELL-0/1 (``ExperienceStore.record``); no crea ningún bucle nuevo.
    Devuelve el identificador de la experiencia, o ``None`` si el resultado no era un fallo.
    """
    described = failure_as_experience(result, problem=problem)
    if described is None:
        return None
    return store.record(
        problem=described.problem,
        failure_reason=described.failure_reason,
        result=ExperienceResult.FAILED,
        status=ExperienceStatus.FAILED,
        tags=described.tags,
    ).id


__all__ = [
    "QA_EXPERIENCE_TAGS",
    "QAFailureExperience",
    "failure_as_experience",
    "record_failure",
    "run_consumer_qa",
]
