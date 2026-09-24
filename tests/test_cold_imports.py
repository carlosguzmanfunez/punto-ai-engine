"""Pruebas de importación en frío (ENGINE-0.R3).

Cada import se ejecuta en un **subproceso Python nuevo**. Probarlos todos dentro
del mismo intérprete no serviría de nada: los módulos ya importados quedan en
``sys.modules`` y el orden previo ocultaría el ciclo que estas pruebas vigilan.

Motivo del ciclo que se corrigió en R3: ``punto.orchestrator.__init__``
reexportaba ``camus`` de forma eager, de modo que importar cualquier submódulo
del orquestador —incluido ``state_machine``— cargaba ``camus`` y, con él,
``punto.tasks.manager``, que a su vez estaba a medio inicializar.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import punto

#: Raíz ``src`` del árbol bajo prueba.
#:
#: Se propaga en ``PYTHONPATH`` para que el subproceso importe exactamente este
#: código, con independencia de cómo esté instalado el paquete en el entorno. No
#: altera la prueba: el intérprete sigue siendo nuevo y arranca con
#: ``sys.modules`` limpio, que es lo que permiten verificar estos tests.
SRC_DIR: Path = Path(punto.__file__).resolve().parents[1]

#: Directorio de trabajo neutro para los subprocesos (raíz del repositorio).
_REPO_ROOT: Path = SRC_DIR.parent

#: Módulos que deben poder importarse desde un proceso limpio, en cualquier orden.
COLD_IMPORT_MODULES: tuple[str, ...] = (
    "punto.tasks",
    "punto.tasks.manager",
    "punto.tasks.transitions",
    "punto.orchestrator",
    "punto.orchestrator.camus",
    "punto.orchestrator.state_machine",
    "punto.policy",
    "punto.policy.human_gate",
    "punto.policy.policy_engine",
    "punto.api.app",
    # --- ENGINE-1: capa de ejecución controlada ------------------------------
    "punto.schemas.execution",
    "punto.tools",
    "punto.tools.errors",
    "punto.tools.filesystem",
    "punto.tools.git",
    "punto.tools.shell",
    "punto.tools.shell_policy",
    "punto.tools.validator",
    "punto.developer",
    "punto.developer.backend",
    "punto.developer.base",
    "punto.developer.context",
    "punto.developer.local",
    "punto.developer.sandbox",
    # --- ENGINE-3: capa de planificación -------------------------------------
    "punto.schemas.planning",
    "punto.planning",
    "punto.planning.capabilities",
    "punto.planning.graph",
    "punto.architect",
    "punto.architect.base",
    "punto.architect.prompts",
    "punto.planner",
    "punto.planner.base",
    "punto.planner.prompts",
    # --- ENGINE-4: QA independiente -------------------------------------------
    "punto.schemas.qa",
    "punto.qa",
    "punto.qa.base",
    "punto.qa.capabilities",
    "punto.qa.checks",
    "punto.qa.overlay",
    "punto.qa.paths",
    "punto.qa.prompts",
    "punto.qa.report",
    "punto.qa.validation",
    # --- ENGINE-5: Security y Reviewer ----------------------------------------
    "punto.model_context",
    "punto.schemas.security",
    "punto.schemas.review",
    "punto.schemas.evaluation",
    "punto.security",
    "punto.security.base",
    "punto.security.checks",
    "punto.security.deterministic",
    "punto.security.prompts",
    "punto.security.report",
    "punto.security.validation",
    "punto.reviewer",
    "punto.reviewer.base",
    "punto.reviewer.gates",
    "punto.reviewer.prompts",
    "punto.reviewer.validation",
    # --- ENGINE-5.2: multi-proveedor y auditoría cruzada -----------------------
    "punto.providers.base",
    "punto.providers.anthropic",
    "punto.providers.routing",
    "punto.schemas.cross_audit",
    "punto.crossaudit",
    "punto.crossaudit.base",
    "punto.crossaudit.prompts",
    "punto.crossaudit.validation",
    "punto.crossaudit.gates",
    "punto.crossaudit.claude",
    # --- ENGINE-5.3: ejecución web y Visual QA --------------------------------
    "punto.schemas.web",
    "punto.schemas.visual",
    "punto.web",
    "punto.web.detection",
    "punto.web.commands",
    "punto.web.checks",
    "punto.web.sandbox",
    "punto.web.report",
    "punto.visualqa",
    "punto.visualqa.base",
    "punto.visualqa.gates",
    "punto.visualqa.prompts",
    "punto.visualqa.validation",
    "punto.visualqa.claude",
    # --- ENGINE-6.0 / 6.0.1: kernel de workflow autónomo -----------------------
    "punto.workflow",
    "punto.workflow.artifacts",
    "punto.workflow.budgets",
    "punto.workflow.checkpoints",
    "punto.workflow.decisions",
    "punto.workflow.effects",
    "punto.workflow.errors",
    "punto.workflow.handoff",
    "punto.workflow.kernel",
    "punto.workflow.pipeline",
    "punto.workflow.policy",
    "punto.workflow.providers",
    "punto.workflow.roles",
    "punto.workflow.state_machine",
    # --- MULTI-TASK FASE 7: arbitraje ProviderLease / WAITING_PROVIDER --------
    "punto.scheduling.provider_waits",
    # --- SKILL-LAYER-0: telemetría pasiva de eficiencia ------------------------
    "punto.telemetry",
    "punto.telemetry.efficiency",
)


def run_in_fresh_interpreter(code: str) -> subprocess.CompletedProcess[str]:
    """Ejecuta ``code`` en un intérprete Python nuevo y devuelve el resultado."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing}" if existing else str(SRC_DIR)

    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_REPO_ROOT),
        check=False,
    )


@pytest.mark.parametrize("module", COLD_IMPORT_MODULES)
def test_module_imports_in_a_fresh_interpreter(module: str) -> None:
    """Cada módulo principal se importa en un subproceso limpio (exit code 0)."""
    result = run_in_fresh_interpreter(f"import {module}")

    assert result.returncode == 0, (
        f"'{module}' no se pudo importar en un intérprete limpio.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_tasks_then_camus_in_a_fresh_interpreter() -> None:
    """Orden ``tasks.manager`` -> ``orchestrator.camus`` en un proceso nuevo."""
    result = run_in_fresh_interpreter(
        "import punto.tasks.manager\nimport punto.orchestrator.camus"
    )

    assert result.returncode == 0, result.stderr


def test_camus_then_tasks_in_a_fresh_interpreter() -> None:
    """Orden inverso ``orchestrator.camus`` -> ``tasks.manager`` en un proceso nuevo."""
    result = run_in_fresh_interpreter(
        "import punto.orchestrator.camus\nimport punto.tasks.manager"
    )

    assert result.returncode == 0, result.stderr


def test_every_module_imports_in_reverse_declaration_order() -> None:
    """Todos los módulos, en un proceso nuevo y en orden inverso, sin fallar."""
    code = "\n".join(f"import {module}" for module in reversed(COLD_IMPORT_MODULES))

    result = run_in_fresh_interpreter(code)

    assert result.returncode == 0, result.stderr


def test_planning_packages_do_not_eagerly_load_their_runners() -> None:
    """``punto.architect`` y ``punto.planner`` no arrastran el cliente HTTP.

    Misma regla que en ENGINE-1.R3: los ``__init__`` internos no reexportan. Importar
    el paquete de un rol no debe cargar ``httpx`` ni la implementación de DeepSeek.
    """
    result = run_in_fresh_interpreter(
        "import sys\n"
        "import punto.architect\n"
        "import punto.planner\n"
        "import punto.planning\n"
        "assert 'httpx' not in sys.modules, 'la planificacion arrastro httpx'\n"
        "assert 'punto.architect.deepseek' not in sys.modules, (\n"
        "    'punto.architect cargo su runner de DeepSeek de forma eager'\n"
        ")\n"
        "assert 'punto.planner.deepseek' not in sys.modules, (\n"
        "    'punto.planner cargo su runner de DeepSeek de forma eager'\n"
        ")"
    )

    assert result.returncode == 0, result.stderr


def test_qa_package_does_not_eagerly_load_its_runner() -> None:
    """``punto.qa`` no arrastra el cliente HTTP ni su runner de DeepSeek.

    Misma regla que en ENGINE-1.R3 y ENGINE-3: los ``__init__`` internos no reexportan.
    """
    result = run_in_fresh_interpreter(
        "import sys\n"
        "import punto.qa\n"
        "import punto.qa.checks\n"
        "import punto.qa.paths\n"
        "assert 'httpx' not in sys.modules, 'el QA arrastro httpx'\n"
        "assert 'punto.qa.deepseek' not in sys.modules, (\n"
        "    'punto.qa cargo su runner de DeepSeek de forma eager'\n"
        ")"
    )

    assert result.returncode == 0, result.stderr


def test_orchestrator_package_does_not_eagerly_load_camus() -> None:
    """``punto.orchestrator`` no debe arrastrar ``camus`` al importarse.

    Es la regla de diseño que elimina el ciclo: los ``__init__`` de paquetes
    internos no provocan cargas eager de módulos de alto nivel que vuelven a
    depender de ellos.
    """
    result = run_in_fresh_interpreter(
        "import sys\n"
        "import punto.orchestrator\n"
        "loaded = 'punto.orchestrator.camus' in sys.modules\n"
        "assert not loaded, 'punto.orchestrator cargo camus de forma eager'\n"
        "assert 'punto.tasks.manager' not in sys.modules, (\n"
        "    'punto.orchestrator arrastro punto.tasks.manager'\n"
        ")"
    )

    assert result.returncode == 0, result.stderr
