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
    "punto.tools.validator",
    "punto.developer",
    "punto.developer.base",
    "punto.developer.context",
    "punto.developer.local",
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
