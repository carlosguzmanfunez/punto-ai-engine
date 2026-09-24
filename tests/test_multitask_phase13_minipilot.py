"""MULTI-TASK v0 -- FASE 13 -- MINI-PILOTO: proyección operacional sobre el scheduler F11 REAL.

Sin APIs externas. El ``TwoTaskScheduler`` real persiste el estado durable (ledger, workspaces Git y
store en disco); la consola real lo proyecta por ``GET /console/operations`` y la página REAL del
dashboard lo pinta en Node (``dashboard_harness.js``).

    1. Dos Tasks: A RUNNING, B WAITING_RESOURCE por A sobre ``contract:Property``.
    2. Provider: A tiene el slot de openai, B WAITING_PROVIDER; openai sigue CONNECTED.
    3. Integration: I espera a A+B (WAITING_DEPENDENCY) y luego corre bajo el scheduler.
    4. Capacidades: provider conectado con una capacidad configurada no efectiva -> aviso compacto.

    pytest tests/test_multitask_phase13_minipilot.py -q
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto.api.console_state import CONSOLE_STATE_ENV, ConsoleStateStore
from punto.api.dashboard import register_dashboard
from punto.project.integration import integration_task
from punto.providers.transport import TransportCapabilities
from punto.schemas.scheduling import (
    DependencyWaitReason,
    ProviderWaitReason,
    ResourceWaitReason,
    SchedulingState,
)
from test_operational_projection import by_id, edges_of, mount_console
from test_two_task_scheduler import (
    AUTH,
    CATALOG,
    NOW,
    PROPERTY,
    WAIT,
    Harness,
    eventually,
    finished,
    make_harness,
    make_task,
    state,
)

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "punto" / "api" / "static" / "dashboard.html"
HARNESS = Path(__file__).parent / "dashboard_harness.js"
NODE = shutil.which("node")


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    built = make_harness(tmp_path)
    # La consola lee EXACTAMENTE el documento que escribe el scheduler: una sola fuente de verdad.
    monkeypatch.setenv(CONSOLE_STATE_ENV, str(built.store.path))
    yield built
    for gate in built.runner.gates.values():
        gate.set()
    built.scheduler.shutdown(wait=True)


#: Objetivos realmente distintos. La consola, al arrancar, consolida como ``duplicate_objective``
#: Tasks activas con objetivos equivalentes (hallazgo fuera de alcance F13): el piloto modela
#: trabajos distintos, y ``operations`` exige igualmente que montar + consultar no escriba nada.
OBJECTIVES = {
    "A": "Unificar los tipos de propiedad en una fuente canónica",
    "B": "Rediseñar la rejilla del catálogo de inmuebles",
    "I": "Integrar outputs verificados de las Tasks fuente",
}


def task(label: str, **options: Any) -> Any:
    return make_task(label, **options).model_copy(update={"objective": OBJECTIVES[label]})


def operations(harness: Harness) -> tuple[dict[str, Any], bytes]:
    """Consulta la API de una consola recién montada (proceso nuevo) y devuelve el disco intacto."""
    before = harness.store.path.read_bytes()
    body = mount_console().get("/console/operations").json()
    assert harness.store.path.read_bytes() == before, "la proyección escribió estado durable"
    return body, before


def render(tmp_path: Path, scenario: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Ejecuta la página REAL del dashboard en Node con ``payload`` y devuelve lo que imprime."""
    if NODE is None:
        pytest.skip("node no está disponible")
    (tmp_path / "payload.json").write_text(json.dumps(payload), encoding="utf-8")
    script = tmp_path / "render.js"
    script.write_text(
        f"""
        const {{ boot, readHtml }} = require({json.dumps(HARNESS.as_posix())});
        const P = JSON.parse(require("fs").readFileSync(
          {json.dumps((tmp_path / "payload.json").as_posix())}, "utf-8"));
        const pagina = boot({{ html: readHtml({json.dumps(HTML.as_posix())}), routes: {{}} }});
        (async () => {{
        {scenario}
        }})().catch((e) => {{ console.error(e); process.exit(1); }});
        """,
        encoding="utf-8",
    )
    done = subprocess.run(
        [NODE, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    result: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    return result


RENDER_OPERATIONS = """
pagina.page.consoleState.operations = P;
pagina.page.renderOperations();
console.log(JSON.stringify({ summary: pagina.text("operations-summary"),
  list: pagina.html("operations-list"), edges: pagina.html("operations-edges") }));
"""


# ============================================================ 1 · dos Tasks
def test_escenario_1_running_y_waiting_resource(harness: Harness, tmp_path: Path) -> None:
    a = task("A", provider="deepseek", resource=PROPERTY)
    b = task("B", provider="openai", resource=PROPERTY, offset=1)
    harness.runner.hold(a.task_id)
    harness.scheduler.submit(a)
    harness.scheduler.submit(b)
    harness.scheduler.wake()
    harness.runner.wait_started(a.task_id)
    eventually(lambda: state(harness, b) is SchedulingState.WAITING_RESOURCE)
    reason = harness.scheduler.task(b.task_id).scheduling.waiting
    assert isinstance(reason, ResourceWaitReason)

    body, _disk = operations(harness)
    assert body["summary"]["active"] == len(harness.scheduler.active_task_ids()) == 1
    assert body["summary"]["max_active"] == harness.scheduler.limits.max_active_tasks == 2
    view_a, view_b = by_id(body, a), by_id(body, b)
    assert view_a["operational_display_state"] == "RUNNING" and view_a["active"] is True
    assert view_b["operational_display_state"] == "WAITING_RESOURCE"
    assert view_b["blocking_task_ids"] == [str(a.task_id)]
    assert view_b["waiting_detail"]["resource_keys"] == list(reason.resource_keys)
    # Clave canónica real de ResourceClaims (normalizada): la misma que ve el scheduler.
    assert any("property" in key.casefold() for key in reason.resource_keys)
    assert edges_of(body, "resource_block") == {(str(a.task_id), str(b.task_id))}

    # Restart de la consola: misma proyección, reconstruida solo desde el disco.
    again, _ = operations(harness)
    assert again["fingerprint"] == body["fingerprint"]

    page = render(tmp_path, RENDER_OPERATIONS, body)
    assert page["summary"].startswith("Active 1 / 2")
    assert "WAITING_RESOURCE" in page["list"] and f"Blocked by: {OBJECTIVES['A']}" in page["list"]
    assert f"Resource: {reason.resource_keys[0]}" in page["list"]
    assert "Recurso ocupado por otra Task" in page["list"]
    assert "<button" not in page["list"]  # read-only: ninguna acción

    harness.runner.release(a.task_id)
    assert harness.scheduler.wait_idle(WAIT)


# ============================================================ 2 · provider
class _Descriptor(SimpleNamespace):
    pass


class _Catalog:
    """Catálogo mínimo con el estado REAL que el dashboard lee (sin red)."""

    def __init__(self, rows: dict[str, tuple[str, tuple[str, ...]]]) -> None:
        self._rows = rows

    def descriptors(self) -> tuple[_Descriptor, ...]:
        return tuple(
            _Descriptor(provider=name, capabilities=caps, model="")
            for name, (_status, caps) in sorted(self._rows.items())
        )

    def status_table(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {"provider": name, "status": status, "capabilities": list(caps)}
            for name, (status, caps) in sorted(self._rows.items())
        )

    def roles(self) -> dict[str, str]:
        return {}


class _Client:
    def __init__(self, *, images: bool, transport: str) -> None:
        self._caps = TransportCapabilities(
            supports_images=images, supports_json_schema=True, detail="--print es texto"
        )
        self.transport = SimpleNamespace(kind=SimpleNamespace(value=transport))

    def capabilities(self) -> TransportCapabilities:
        return self._caps


def providers_api(monkeypatch: pytest.MonkeyPatch, rows: dict[str, Any], images: set[str]) -> Any:
    import punto.providers.transport_registry as registry

    monkeypatch.setattr(
        registry,
        "transport_client",
        lambda provider, model="": _Client(
            images=provider in images, transport="api" if provider in images else "claude_code"
        ),
    )
    application = FastAPI()
    register_dashboard(application, registry=_Catalog(rows))  # type: ignore[arg-type]
    return TestClient(application).get("/providers").json()


def test_escenario_2_provider_busy_no_es_fallo(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = task("A", provider="openai", resource=AUTH)
    b = task("B", provider="openai", resource=CATALOG, offset=1)
    harness.runner.hold(a.task_id)
    harness.scheduler.submit(a)
    harness.scheduler.submit(b)
    harness.scheduler.wake()
    harness.runner.wait_started(a.task_id)
    eventually(lambda: state(harness, b) is SchedulingState.WAITING_PROVIDER)
    assert isinstance(harness.scheduler.task(b.task_id).scheduling.waiting, ProviderWaitReason)

    body, _disk = operations(harness)
    view_b = by_id(body, b)
    assert view_b["operational_display_state"] == "WAITING_PROVIDER"
    assert view_b["waiting_summary"] == "Provider ocupado (openai)"
    assert view_b["blocking_task_ids"] == [str(a.task_id)]
    assert by_id(body, a)["provider"]["role"] == "current"
    assert not any(item["terminal"] for item in body["tasks"])
    assert "FAILED" not in body["summary"]["by_state"]

    providers = providers_api(
        monkeypatch, {"openai": ("CONNECTED", ("TEXT", "VISION"))}, images={"openai"}
    )
    assert providers["providers"][0]["status"] == "CONNECTED"
    assert providers["capability_limitations"]["count"] == 0

    harness.runner.release(a.task_id)
    assert harness.scheduler.wait_idle(WAIT)
    assert finished(harness, b).result is not None


# ============================================================ 3 · Integration
def test_escenario_3_integration_espera_y_corre_bajo_el_scheduler(
    harness: Harness, tmp_path: Path
) -> None:
    a = task("A", provider="deepseek", resource=AUTH)
    b = task("B", provider="openai", resource=CATALOG, offset=1)
    harness.runner.hold(b.task_id)
    harness.scheduler.submit(a)
    harness.scheduler.submit(b)
    harness.scheduler.wake()
    harness.runner.wait_started(b.task_id)
    eventually(lambda: harness.scheduler.task(a.task_id).finished_at is not None)

    integration = integration_task(
        task_id=__import__("uuid").uuid4(),
        sources=(harness.scheduler.task(a.task_id), harness.scheduler.task(b.task_id)),
        objective=OBJECTIVES["I"],
        target_id="phase11-target",
        created_at=NOW,
    )
    harness.runner.hold(integration.task_id)
    harness.scheduler.submit(integration)
    harness.scheduler.wake()
    eventually(lambda: state(harness, integration) is SchedulingState.WAITING_DEPENDENCY)
    reason = harness.scheduler.task(integration.task_id).scheduling.waiting
    assert isinstance(reason, DependencyWaitReason)

    body, _ = operations(harness)
    view = by_id(body, integration)
    assert view["kind"] == "INTEGRATION"
    assert view["operational_display_state"] == "WAITING_DEPENDENCY"
    assert view["blocking_task_ids"] == [str(b.task_id)]
    target = str(integration.task_id)
    sources = {(str(a.task_id), target), (str(b.task_id), target)}
    assert edges_of(body, "integration_source") == sources
    assert by_id(body, a)["operational_display_state"] == "COMPLETED"

    harness.runner.release(b.task_id)
    harness.runner.wait_started(integration.task_id)
    eventually(lambda: state(harness, integration) is SchedulingState.RUNNING)
    before = {
        str(item.task_id): item.model_dump(mode="json")
        for item in ConsoleStateStore(harness.store.path).load().tasks
    }
    body, _ = operations(harness)
    view = by_id(body, integration)
    assert view["operational_display_state"] == "RUNNING" and view["active"] is True
    assert view["display_label"] == "Integration Task · RUNNING"
    assert edges_of(body, "integration_source") == sources
    for source in (a, b):
        assert by_id(body, source)["terminal"] is True
    after = {
        str(item.task_id): item.model_dump(mode="json")
        for item in ConsoleStateStore(harness.store.path).load().tasks
    }
    assert after == before  # las fuentes (y todo lo demás) intactas

    page = render(tmp_path, RENDER_OPERATIONS, body)
    assert "Integration Task" in page["list"] and "Sources: " in page["list"]
    assert page["edges"].count("integration_source") == 2

    harness.runner.release(integration.task_id)
    assert harness.scheduler.wait_idle(WAIT)


# ============================================================ 4 · capacidades
def test_escenario_4_capacidad_configurada_no_efectiva(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = providers_api(
        monkeypatch,
        {
            "claude": ("CONNECTED", ("TEXT", "VISION")),
            "openai": ("CONNECTED", ("TEXT", "VISION")),
        },
        images={"openai"},
    )
    status = {row["provider"]: row["status"] for row in body["providers"]}
    assert status == {"claude": "CONNECTED", "openai": "CONNECTED"}
    summary = body["capability_limitations"]
    assert summary["count"] == 1 and summary["label"] == "⚠ 1 capacidad limitada"
    item = summary["items"][0]
    assert (item["provider"], item["capability"], item["transport"]) == (
        "claude",
        "VISION",
        "claude_code",
    )
    assert item["configured"] is True and item["effective"] is False
    assert "--print es texto" in item["reason"]

    page = render(
        tmp_path,
        """
        pagina.page.state.limitations = P.capability_limitations;
        pagina.page.renderCapabilitySummary();
        console.log(JSON.stringify({
          resumen: pagina.html("capability-summary"),
          claude: pagina.page.effectiveRow({ provider: "claude" }),
          openai: pagina.page.effectiveRow({ provider: "openai" }) }));
        """,
        body,
    )
    assert page["resumen"].startswith("⚠ 1 capacidad limitada")
    assert "<summary>⚠ 1 capacidad limitada</summary>" in page["claude"]
    assert "VISION: configurada, no efectiva en claude_code" in page["claude"]
    assert page["openai"] == ""
