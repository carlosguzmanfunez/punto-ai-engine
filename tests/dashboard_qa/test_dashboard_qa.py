"""DASHBOARD QA — el dashboard de proveedores, conducido por QA Consumer con navegador real.

Cada caso arranca **el backend real** (la misma aplicación FastAPI que sirve la página) dentro del
sandbox, con su propia copia del árbol `src/`, y deja que Chromium la abra. El estado que se observa
es el estado real del motor dentro de ese contenedor: sin Codex instalado y sin sesión de Claude
Code, así que los estados esperados son los honestos, no un PASS inventado.

    pytest tests/dashboard_qa -q
"""

from __future__ import annotations

import os
import shutil
import textwrap
from pathlib import Path

import pytest

from punto.consumer_qa import (
    ConsumerQACase,
    QAExpectation,
    QAExpectationKind,
    QAStatus,
    QAStep,
    QAStepKind,
    QATarget,
    run_consumer_qa,
)

#: Imagen del contenedor que ejecuta el backend del dashboard.
DASHBOARD_IMAGE = "localhost/punto-dashboard:0.1"

#: Repositorio, para copiar el código que se ejecuta dentro del contenedor.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Credencial sintética con la marca de canario documentado del repositorio.
TEST_KEY = "sk-test-CANARY-0123456789abcdef"

#: Arranque del backend dentro del contenedor: uvicorn con la fábrica real de la aplicación.
LAUNCH = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, '/workspace/app/src')
    os.environ.setdefault('PUNTO_SECRETS_FILE', '/workspace/app/.qa-secrets.json')
    os.environ.setdefault(
        'PUNTO_PROVIDERS_LOCAL_FILE', '/workspace/app/config/providers.local.yaml'
    )
    import uvicorn
    from punto.api.app import create_app
    uvicorn.run(create_app(environment='test'), host='0.0.0.0', port=4173, log_level='warning')
    """
).strip()


#: Título de cada escenario del encargo.
DASH_TITLES = {
    "DASH-QA-001": "la página de proveedores carga",
    "DASH-QA-002": "las tarjetas de OpenAI, DeepSeek y Anthropic aparecen",
    "DASH-QA-003": "cambiar el transporte actualiza el estado",
    "DASH-QA-004": "cambiar el mapeo de roles persiste",
    "DASH-QA-005": "test connection muestra el resultado real",
    "DASH-QA-006": "un proveedor nuevo se abre y se guarda",
    "DASH-QA-007": "la clave guardada no vuelve al navegador",
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Workspace temporal con el código que ejecutará el contenedor.

    Solo el código: el sandbox **prohíbe** montar un workspace que contenga ficheros
    constitucionales (`config/constitution.yaml`), y hace bien. El dashboard no los necesita: su
    configuración llega por las variables del arranque y por el catálogo por defecto.
    """
    root = tmp_path / "workspace"
    application = root / "app"
    shutil.copytree(REPO_ROOT / "src", application / "src")
    (application / "config").mkdir(parents=True)
    return root


@pytest.fixture
def target(workspace: Path) -> QATarget:
    """Objetivo: el backend del dashboard servido dentro del contenedor de la aplicación."""
    return QATarget(
        workspace=workspace,
        project_relative="app",
        preview_argv=(("python3", "-c", LAUNCH),),
        preview_image=DASHBOARD_IMAGE,
        timeout_seconds=300.0,
    )


def _case(
    dash_id: str, steps: tuple[QAStep, ...], expectations: tuple[QAExpectation, ...]
) -> ConsumerQACase:
    """Caso de consumidor del dashboard.

    El contrato de consumidor numera sus casos ``QA-NNN``; el escenario de esta fase se llama
    ``DASH-QA-00N`` y viaja en el título, que es el identificador con el que se ejecuta la suite.
    """
    number = int(dash_id.rsplit("-", 1)[-1])
    return ConsumerQACase(
        qa_id=f"QA-{100 + number:03d}",
        title=f"{dash_id} · {DASH_TITLES[dash_id]}",
        start_url="/dashboard",
        steps=steps,
        expectations=expectations,
        description="caso de QA del dashboard de configuración de proveedores",
    )


def _visible(selector: str) -> QAExpectation:
    """Expectativa de visibilidad."""
    return QAExpectation(
        kind=QAExpectationKind.VISIBLE, target=selector, expected=f"{selector} visible"
    )


def _text(selector: str, value: str) -> QAExpectation:
    """Expectativa de texto contenido."""
    return QAExpectation(
        kind=QAExpectationKind.TEXT_CONTAINS,
        target=selector,
        value=value,
        expected=f"{selector} contiene {value!r}",
    )


def _select(selector: str, value: str) -> QAStep:
    """Paso de selección en un desplegable."""
    return QAStep(kind=QAStepKind.SELECT, target=selector, value=value)


DASH_QA_001 = _case(
    "DASH-QA-001",
    (),
    (
        _visible('[data-testid="providers"]'),
        _text("body", "Configuración"),
        QAExpectation(
            kind=QAExpectationKind.HTTP_OK, target="/dashboard", expected="la página responde 200"
        ),
        QAExpectation(
            kind=QAExpectationKind.NO_CONSOLE_ERRORS,
            target="consola",
            expected="sin errores de consola",
        ),
    ),
)

DASH_QA_002 = _case(
    "DASH-QA-002",
    (),
    (
        _visible('[data-provider="openai"]'),
        _visible('[data-provider="deepseek"]'),
        _visible('[data-provider="anthropic"]'),
        _text('[data-provider="openai"]', "OpenAI / Codex"),
        _text('[data-provider="openai"]', "ARCHITECT"),
        _text('[data-provider="anthropic"]', "VISUAL_QA"),
    ),
)

DASH_QA_003 = _case(
    "DASH-QA-003",
    (_select("#transport-anthropic", "api"),),
    (
        _text("#auth-anthropic", "api_key"),
        _text("#note-anthropic", "transporte cambiado"),
    ),
)

DASH_QA_004 = _case(
    "DASH-QA-004",
    (_select("#role-BUILDER", "anthropic"),),
    (_text("#roles-note", "BUILDER"),),
)

DASH_QA_005 = _case(
    "DASH-QA-005",
    (QAStep(kind=QAStepKind.CLICK, target="#test-openai"),),
    (_text("#note-openai", "NOT_INSTALLED"),),
)

DASH_QA_006 = _case(
    "DASH-QA-006",
    (
        QAStep(kind=QAStepKind.FILL, target="#custom-provider-id", value="qwen_local"),
        QAStep(kind=QAStepKind.FILL, target="#custom-display-name", value="Qwen local"),
        QAStep(
            kind=QAStepKind.FILL,
            target="#custom-base-url",
            value="https://api.ejemplo.com/v1",
        ),
        QAStep(kind=QAStepKind.FILL, target="#custom-model", value="qwen2.5-14b-instruct"),
        QAStep(kind=QAStepKind.CLICK, target="#add-provider"),
    ),
    (
        _text("#custom-note", "qwen_local"),
        _visible('[data-provider="qwen_local"]'),
    ),
)

DASH_QA_007 = _case(
    "DASH-QA-007",
    (
        _select("#transport-openai", "api"),
        QAStep(kind=QAStepKind.FILL, target="#api-key-openai", value=TEST_KEY),
        QAStep(kind=QAStepKind.CLICK, target="#save-key-openai"),
    ),
    (
        _text("#note-openai", "api_key_configured=true"),
        _text("#api-key-state-openai", "clave configurada"),
    ),
)

CASES = (DASH_QA_001, DASH_QA_002, DASH_QA_003, DASH_QA_004, DASH_QA_005, DASH_QA_006, DASH_QA_007)

IDS = [case.title.split(" ·")[0] for case in CASES]


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_el_dashboard_pasa_su_qa_de_consumidor(
    case: ConsumerQACase, target: QATarget, tmp_path: Path
) -> None:
    """Cada caso atraviesa navegador real → dashboard → backend → respuesta → DOM."""
    result = run_consumer_qa(case, target, evidence_dir=tmp_path / "evidencia")

    if result.status is QAStatus.SKIP:
        pytest.skip(result.reason)
    assert result.status is QAStatus.PASS, result.reason
    assert result.evidence is not None
    assert result.evidence.has_screenshot


def test_la_clave_guardada_no_vuelve_al_navegador(
    target: QATarget, tmp_path: Path
) -> None:
    """Tras guardar una clave, el backend no la publica: solo dice que está configurada.

    El caso DASH-QA-007 demuestra que la UI muestra el estado en vez del valor; aquí se comprueba
    además dónde vive la clave: en el almacén del workspace temporal, nunca en el repositorio.
    """
    result = run_consumer_qa(DASH_QA_007, target, evidence_dir=tmp_path / "evidencia")
    assert result.status is QAStatus.PASS, result.reason

    secrets_file = target.workspace / "app" / ".qa-secrets.json"
    assert secrets_file.is_file(), "la clave se guarda en el almacén, no en el repositorio"
    assert TEST_KEY in secrets_file.read_text(encoding="utf-8")
    assert not (REPO_ROOT / ".qa-secrets.json").exists()
    assert not (REPO_ROOT / "config" / "providers.local.yaml").exists()
    assert os.sep.join(("punto-ai-engine", "config", "providers.local.yaml")) not in str(
        secrets_file
    )
