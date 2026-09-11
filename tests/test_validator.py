"""Validator: PASS solo si todo se ejecutó de verdad (ENGINE-1).

Mandato §19 y casos §25.21-22.
"""

from __future__ import annotations

import pytest

from punto.developer.context import ExecutionContext
from punto.schemas.execution import CommandSpec
from punto.tools.shell import ShellRunner
from punto.tools.validator import Validator


@pytest.fixture
def validator(ai_context: ExecutionContext) -> Validator:
    """Validador sobre el workspace temporal."""
    return Validator(ai_context, ShellRunner(ai_context))


PYTEST_CHECK = CommandSpec(
    name="pytest", executable="python", args=("-m", "pytest", "-q")
)


# ---------------------------------------------------------------------------
# §25.22 - PASS solo si todas las checks pasan
# ---------------------------------------------------------------------------
def test_validator_passes_when_every_check_passes(validator: Validator) -> None:
    """Con todas las checks en verde, el agregado es PASS."""
    result = validator.validate((PYTEST_CHECK,))

    assert result.passed is True
    assert result.failed_checks == ()
    assert result.total == 1
    assert result.checks[0].passed is True
    assert result.checks[0].exit_code == 0
    assert result.duration_ms >= 0


def test_validator_reports_all_checks(validator: Validator) -> None:
    """La evidencia incluye cada check con su comando y su salida."""
    checks = (
        CommandSpec(name="pytest", executable="python", args=("-m", "pytest", "-q")),
        CommandSpec(name="version", executable="python", args=("--version",)),
    )

    result = validator.validate(checks)

    assert result.total == 2
    assert [check.name for check in result.checks] == ["pytest", "version"]
    assert all(check.passed for check in result.checks)


# ---------------------------------------------------------------------------
# §25.21 - FAIL si una check falla
# ---------------------------------------------------------------------------
def test_validator_fails_when_a_check_fails(
    validator: Validator, ai_context: ExecutionContext
) -> None:
    """§25.21: si ``pytest`` falla, el resultado no es PASS."""
    (ai_context.workspace_root / "app.py").write_text(
        'def greet(name: str) -> str:\n    return "roto"\n', encoding="utf-8"
    )

    result = validator.validate((PYTEST_CHECK,))

    assert result.passed is False
    assert result.failed_checks == ("pytest",)
    assert result.checks[0].exit_code != 0
    assert result.checks[0].passed is False


def test_validator_fails_if_any_check_fails(validator: Validator) -> None:
    """Basta una check en rojo para que el agregado no sea PASS."""
    checks = (
        CommandSpec(name="ok", executable="python", args=("--version",)),
        CommandSpec(
            name="ko", executable="python", args=("-c", "import sys; sys.exit(4)")
        ),
    )

    result = validator.validate(checks)

    assert result.passed is False
    assert result.failed_checks == ("ko",)
    assert [check.passed for check in result.checks] == [True, False]


def test_validator_fails_on_timeout(validator: Validator) -> None:
    """Una check que agota el timeout no puede contar como PASS."""
    checks = (
        CommandSpec(
            name="lenta",
            executable="python",
            args=("-c", "import time; time.sleep(10)"),
            timeout_seconds=0.5,
        ),
    )

    result = validator.validate(checks)

    assert result.passed is False
    assert result.checks[0].timed_out is True
    assert result.checks[0].passed is False
    assert result.failed_checks == ("lenta",)


def test_validator_without_checks_is_not_pass(validator: Validator) -> None:
    """Sin checks declarados no se ha validado nada: no es PASS."""
    result = validator.validate(())

    assert result.passed is False
    assert result.total == 0
    assert result.failed_checks == ()


def test_validator_records_blocked_check_without_crashing(validator: Validator) -> None:
    """Una check no permitida queda como fallo bloqueado, con su motivo."""
    checks = (CommandSpec(name="prohibida", executable="curl", args=("http://x",)),)

    result = validator.validate(checks)

    assert result.passed is False
    assert result.checks[0].blocked is True
    assert result.checks[0].passed is False
    assert "curl" in result.checks[0].stderr


def test_validator_exposes_command_results(
    validator: Validator, ai_context: ExecutionContext
) -> None:
    """Los comandos ejecutados quedan disponibles como evidencia."""
    validator.validate((PYTEST_CHECK,))

    results = validator.command_results

    assert len(results) == 1
    assert results[0].name == "pytest"
    assert results[0].cwd == str(ai_context.workspace_root)


def test_validator_respects_remaining_time_budget(validator: Validator) -> None:
    """Un techo de tiempo más estricto provoca timeout aunque la check sea rápida."""
    checks = (
        CommandSpec(
            name="corta",
            executable="python",
            args=("-c", "import time; time.sleep(2)"),
            timeout_seconds=30.0,
        ),
    )

    result = validator.validate(checks, max_timeout_seconds=0.4)

    assert result.checks[0].timed_out is True
    assert result.passed is False
