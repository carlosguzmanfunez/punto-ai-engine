"""SKILL-LAYER-0 — baseline de eficiencia del workflow actual (CONTROL, sin skills).

Ejecuta seis casos representativos (A–F) por los caminos reales del motor y deriva un
``EfficiencyRecord`` por ejecución **sin intervenir en el workflow**: la única instrumentación es un
observador pasivo alrededor de ``ProviderRouter.execute`` que apunta lo que el motor ya produce
(rol, proveedor, modelo, estado, duración, reintentos, consumo que el transporte exponga y tamaño del
prompt/contexto realmente enviados).

Dos modos:

- ``DETERMINISTIC``: proveedores guionizados. Reproducible, sirve para validar la instrumentación y
  medir el coste interno de PUNTO. Los tokens del proveedor guionizado se marcan **UNAVAILABLE**: no
  son consumo real de ningún transporte.
- ``REAL``: proveedores reales (ARCHITECT y BUILDER configurados). Una ejecución válida por caso, sin
  repeticiones: no se gasta suscripción para medir suscripción.

Uso:
    python _punto-skill-layer/run_baseline.py deterministic
    python _punto-skill-layer/run_baseline.py real
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ENGINE = Path(__file__).resolve().parent.parent
EVIDENCE = ENGINE / "_punto-skill-layer"
TARGET_ID = "baseline-fixture"
WORK_BRANCH = "ai/skill-layer-baseline"

FOCUSED = (
    "import pathlib,sys;"
    "texto=pathlib.Path('src/lib/tipos.ts').read_text(encoding='utf-8');"
    "print('TIPOS:', texto.strip()[:80]);"
    "sys.exit(0 if 'Apartamento' in texto else 1)"
)
CHAIN = (
    "import pathlib,sys;"
    "fuente=pathlib.Path('src/lib/tipos.ts').read_text(encoding='utf-8');"
    "consumidores=[pathlib.Path(p) for p in ('src/components/Rejilla.tsx','src/components/Buscador.tsx')];"
    "ok=all('@/lib/tipos' in c.read_text(encoding='utf-8') for c in consumidores);"
    "print('CADENA:', ok);"
    "sys.exit(0 if ok and 'Apartamento' in fuente else 1)"
)

VERIFICATION: dict[str, list[str]] = {
    "focused": ["python", "-c", FOCUSED],
    "chain": ["python", "-c", CHAIN],
}

#: Procedimientos que PUNTO envía en cada invocación: su tamaño se mide, no se estima.
REPEATED_BLOCKS: tuple[tuple[str, str], ...] = (
    ("worker_instructions", "WORKER_INSTRUCTIONS"),
    ("plan_contract", "PLAN_CONTRACT"),
    ("build_contract", "BUILD_CONTRACT"),
)


def _git(root: Path, *args: str) -> str:
    """Git en modo lectura sobre el repositorio del montaje."""
    completed = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    return completed.stdout.strip()


def _repo(root: Path) -> Path:
    """Repositorio fixture mínimo, con su rama de trabajo y un cambio sucio del usuario."""
    repo = root / "destino"
    (repo / "src" / "lib").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "components").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "lib" / "tipos.ts").write_text(
        "export const TIPOS = ['Casa'];\n", encoding="utf-8"
    )
    (repo / "src" / "components" / "Rejilla.tsx").write_text(
        "const tipos = ['Casa'];\nexport function Rejilla() { return tipos.length; }\n",
        encoding="utf-8",
    )
    (repo / "src" / "components" / "Buscador.tsx").write_text(
        "const tipos = ['Casa'];\nexport function Buscador() { return tipos.length; }\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("node_modules\n", encoding="utf-8")
    _git(repo, "init", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=baseline", "-c", "user.email=b@punto.local", "commit", "-m", "base")
    _git(repo, "checkout", "-b", WORK_BRANCH)
    (repo / ".gitignore").write_text("node_modules\n.env.local\n", encoding="utf-8")
    return repo


class _ObservedRouter:
    """Observador **pasivo** del router: delega todo y solo apunta lo que ya ocurre.

    No intercepta ni modifica la petición: mide el prompt que PUNTO construyó y el resultado que el
    proveedor devolvió. Delegar el resto por ``__getattr__`` garantiza que el ciclo hable con el
    mismo router de siempre (asignación de roles incluida).
    """

    def __init__(self, router: Any) -> None:
        self._router = router
        self.calls: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        """Delega cualquier otro atributo al router real."""
        return getattr(self._router, name)

    def execute(self, role: Any, request: Any, **kwargs: Any) -> Any:
        """Ejecuta por el router real y apunta la llamada, sin alterarla."""
        prompt = f"{getattr(request, 'instructions', '')}\n{getattr(request, 'context', '')}"
        context = getattr(request, "context", "") or ""
        started = time.perf_counter()
        result = self._router.execute(role, request, **kwargs)
        duration_ms = int((time.perf_counter() - started) * 1000)
        usage = getattr(result, "usage", None)
        # Los tokens solo se registran como reales si el transporte los expone de verdad: el
        # proveedor guionizado devuelve cifras sintéticas y se marcan NO DISPONIBLES.
        exposed = (
            usage is not None
            and bool(getattr(usage, "total_tokens", 0))
            and getattr(result, "provider", "") != "guionizado"
        )
        self.calls.append(
            {
                "role": getattr(role, "value", str(role)),
                "provider": getattr(result, "provider", ""),
                "model": getattr(result, "model", ""),
                "status": getattr(getattr(result, "status", None), "value", ""),
                "phase": _phase_of(prompt, context),
                "duration_ms": getattr(result, "duration_ms", None) or duration_ms,
                "transport_retries": int(getattr(result, "transport_retries", 0) or 0),
                "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0) if exposed else None,
                "output_tokens": (
                    int(getattr(usage, "completion_tokens", 0) or 0) if exposed else None
                ),
                "total_tokens": int(getattr(usage, "total_tokens", 0) or 0) if exposed else None,
                "prompt_chars": len(prompt),
                "context_chars": len(context),
                "content": getattr(result, "content", "") or "",
            }
        )
        return result


def _phase_of(prompt: str, context: str) -> str:
    """Fase observable de una invocación, derivada del contrato que PUNTO escribió en el prompt."""
    if '"functional_chain"' in prompt or "files_to_modify" in prompt:
        return "planning"
    if "VERIFICATION FAILED" in context or "REJECTED CHANGES" in context:
        return "repair"
    if "VALIDATED PLAN" in prompt:
        return "implementation"
    return "unspecified"


def _scripted(payloads: list[Any]) -> Any:
    """Cliente guionizado mínimo para el baseline determinista."""
    import json as _json

    from punto.providers.base import ModelCompletion
    from punto.providers.contract import ModelUsage

    class _Client:
        def __init__(self, router: Any) -> None:
            self._responses = list(payloads)
            router.register_provider("guionizado", lambda _model: self, model="guionizado-1")

        @property
        def provider(self) -> str:
            """Identificador del proveedor guionizado."""
            return "guionizado"

        @property
        def model(self) -> str:
            """Modelo guionizado."""
            return "guionizado-1"

        def complete_json(self, **kwargs: Any) -> Any:
            """Devuelve la siguiente respuesta del guion."""
            del kwargs
            item = self._responses.pop(0) if self._responses else {"changes": []}
            content = item if isinstance(item, str) else _json.dumps(item)
            return ModelCompletion(
                content=content,
                model="guionizado-1",
                usage=ModelUsage(prompt_tokens=5, completion_tokens=7, total_tokens=12),
                latency_ms=1,
            )

        def redact(self, text: str) -> str:
            """No sanea: el ciclo no puede fiarse de la educación del adaptador."""
            return text

        def close(self) -> None:
            """No hay recursos que liberar."""

    return _Client


def _proposal_summary(content: str) -> dict[str, Any]:
    """Resumen **estructurado** de lo que propuso el BUILDER, sin contenido de ficheros.

    Es lo que permite diagnosticar qué criterio o recurso quedó sin cubrir sin volver a llamar al
    proveedor: rutas, operaciones, criterio de aceptación citado y si declaró causa raíz.
    """
    import json as _json

    try:
        payload = _json.loads(content)
    except (ValueError, TypeError):
        return {"parsed": False, "chars": len(content)}
    if not isinstance(payload, dict):
        return {"parsed": False, "chars": len(content)}
    changes = payload.get("changes")
    resumen = {
        "parsed": True,
        "chars": len(content),
        "changes": [
            {
                "path": str(item.get("path", "")),
                "operation": str(item.get("operation", "")),
                "acceptance_criterion": str(item.get("acceptance_criterion", "")),
                "reason": str(item.get("reason", ""))[:120],
            }
            for item in (changes if isinstance(changes, list) else [])
            if isinstance(item, dict)
        ],
        "root_cause": str(payload.get("root_cause", ""))[:200],
        "scope_expansion": bool(payload.get("scope_expansion")),
        "context_requests": len(payload.get("context_requests") or []),
    }
    return resumen


def _first_attempt(result: Any, calls_detail: list[dict[str, Any]], handoff: str) -> dict[str, Any]:
    """Calidad del **primer** intento del BUILDER, en hechos observables.

    ``first_attempt_pass`` es verdadero solo si el ciclo cerró sin ninguna ronda de reparación: eso
    significa que la primera propuesta, aplicada, pasó la verificación. Cuando no, se dice qué
    criterios del handoff no aparecen citados y qué recursos del plan no se tocaron.
    """
    import json as _json

    proposals = [item for item in calls_detail if item["role"] == "BUILDER"]
    primera = proposals[0]["proposal"] if proposals else {}
    try:
        handoff_payload = _json.loads(handoff) if handoff else {}
    except ValueError:
        handoff_payload = {}
    criterios = [str(item) for item in handoff_payload.get("done", [])]
    recursos = [str(item) for item in handoff_payload.get("resources", [])]
    citados = [str(change.get("acceptance_criterion", "")) for change in primera.get("changes", [])]
    tocados = [str(change.get("path", "")) for change in primera.get("changes", [])]
    return {
        "first_attempt_pass": result.repair_rounds == 0 and result.completed,
        "repair_rounds": result.repair_rounds,
        "builder_calls": len(proposals),
        "missing_acceptance": [item for item in criterios if item and item not in citados],
        "missing_resources": [item for item in recursos if item not in tocados],
        "declared_root_cause": bool(primera.get("root_cause")),
        "failed_verifications": [item.name for item in result.verification if not item.passed],
    }


def _run_case(case: dict[str, Any], *, mode: str, root: Path) -> dict[str, Any]:
    """Ejecuta un caso y devuelve su registro de eficiencia y la evidencia de la corrida."""
    from punto.audit.logger import AuditLogger
    from punto.common import utc_now
    from punto.orchestrator.dev_cycle import DevelopmentConfig, DevelopmentCycle
    from punto.providers.contract import ProviderRole
    from punto.providers.registry import ProviderRegistry
    from punto.providers.router import ProviderRouter
    from punto.schemas.build import BuildRequest
    from punto.schemas.dev import RepositoryOperation
    from punto.telemetry import ProviderCall, RunEvidence, TokenUsage, build_record
    from punto.workspace.target import (
        DevelopmentTarget,
        DevelopmentTargetRegistry,
        VerificationCommand,
    )

    repo = _repo(root)
    if mode == "DETERMINISTIC":
        router = ProviderRouter()
        _scripted(case["responses"])(router)
        for role in ProviderRole:
            router.assign_role(role, "guionizado")
    else:
        registry = ProviderRegistry()
        router = registry.router_instance()

    observed = _ObservedRouter(router)
    target = DevelopmentTarget(
        target_id=TARGET_ID,
        repository=repo,
        baseline_sha=_git(repo, "rev-parse", "HEAD"),
        scope_roots=("src", "tests"),
        allowed_operations=frozenset(
            {
                RepositoryOperation.READ,
                RepositoryOperation.WRITE,
                RepositoryOperation.CREATE,
                RepositoryOperation.DELETE,
                RepositoryOperation.EXECUTE,
                RepositoryOperation.COMMIT,
            }
        ),
        verification=tuple(
            VerificationCommand(name=name, argv=tuple(argv), timeout_seconds=60.0)
            for name, argv in VERIFICATION.items()
        ),
        work_branch=WORK_BRANCH,
        max_repair_rounds=2,
        command_timeout_seconds=60.0,
    )
    audit = AuditLogger()
    skill_reference = os.environ.get("PUNTO_ARCHITECT_SKILL", "").strip()
    builder_skill_reference = os.environ.get("PUNTO_BUILDER_SKILL", "").strip()
    cycle = DevelopmentCycle(
        router=observed,  # type: ignore[arg-type]
        targets=DevelopmentTargetRegistry({TARGET_ID: target}),
        config=DevelopmentConfig(
            max_repair_rounds=2,
            architect_skill=skill_reference,
            builder_skill=builder_skill_reference,
        ),
        audit=audit,
    )
    request = BuildRequest(
        objective=case["objective"],
        target_repository=TARGET_ID,
        requested_role=ProviderRole.BUILDER,
        acceptance_criteria=tuple(case["acceptance"]),
        scope_paths=("src",),
    )
    started_at = utc_now()
    started_clock = time.perf_counter()
    result = cycle.run(request)
    elapsed_ms = int((time.perf_counter() - started_clock) * 1000)
    finished_at = utc_now()
    events = list(audit.by_resource(str(request.request_id)))

    calls = tuple(
        ProviderCall(
            role=item["role"],
            provider=item["provider"],
            model=item["model"],
            transport="scripted" if item["provider"] == "guionizado" else "configured",
            status=item["status"],
            phase=item["phase"],
            duration_ms=item["duration_ms"],
            transport_retries=item["transport_retries"],
            tokens=(
                TokenUsage(
                    input_tokens=item["input_tokens"],
                    output_tokens=item["output_tokens"],
                    total_tokens=item["total_tokens"],
                    source="REAL",
                )
                if item["total_tokens"] is not None
                else TokenUsage()
            ),
            prompt_chars=item["prompt_chars"],
            context_chars=item["context_chars"],
        )
        for item in observed.calls
    )
    discovered = next(
        (dict(e.metadata) for e in events if e.event_type.value == "DEV_REPOSITORY_DISCOVERED"),
        {},
    )
    pell = next(
        (dict(e.metadata) for e in events if e.event_type.value == "DEV_PELL_RETRIEVED"),
        {},
    )
    activations = [
        dict(e.metadata) for e in events if e.event_type.value == "DEV_SKILL_ACTIVATED"
    ]
    activation = next(
        (item for item in activations if item.get("role", "ARCHITECT") == "ARCHITECT"), {}
    )
    builder_activation = next((item for item in activations if item.get("role") == "BUILDER"), {})
    verification_failures = sum(1 for item in result.verification if not item.passed)
    calls_detail = [
        {
            "role": item["role"],
            "phase": item["phase"],
            "status": item["status"],
            "prompt_chars": item["prompt_chars"],
            "total_tokens": item["total_tokens"],
            "duration_ms": item["duration_ms"],
            "proposal": _proposal_summary(item.get("content", "")),
        }
        for item in observed.calls
    ]
    evidence = RunEvidence(
        run_id=str(result.request_id),
        case_id=case["case_id"],
        case_kind=case["kind"],
        mode=mode,  # type: ignore[arg-type]
        started_at=started_at,
        finished_at=finished_at,
        elapsed_ms=elapsed_ms,
        provider_calls=calls,
        tool_calls=sum(1 for e in events if e.event_type.value == "COMMAND_EXECUTED"),
        repair_rounds=result.repair_rounds,
        plan_revisions=max(0, len(result.plan_versions) - 1),
        scope_expansions=len(result.scope_expansions),
        files_discovered=int(discovered.get("candidates", 0) or 0),
        files_read=len(discovered.get("selected", ()) or ()),
        files_changed=len(result.applied),
        verification_count=len(result.verification),
        verification_failures=verification_failures,
        functional_chain_pass=result.functional_chain_result == "VERIFIED",
        pell_retrievals=1 if pell else 0,
        pell_hits=1 if str(pell.get("status", "")).upper() == "HIT" else 0,
        human_gates=1 if result.error_kind == "HUMAN_GATE_REQUIRED" else 0,
        human_gate_required=result.error_kind == "HUMAN_GATE_REQUIRED",
        context_chars=int(discovered.get("selected_chars", 0) or 0),
        final_status=result.status.value,
        success=result.completed,
        rollback_required=result.rolled_back,
        notes=(f"proveedores: {sorted({item['provider'] for item in observed.calls})}",),
    )
    recorded_ms = time.perf_counter()
    record = build_record(
        evidence,
        task_id=case["case_id"],
        primary_role=case["primary_role"],
        primary_provider=calls[0].provider if calls else "",
        primary_model=calls[0].model if calls else "",
        primary_transport=calls[0].transport if calls else "",
        verification_elapsed_ms=sum(item.duration_ms or 0 for item in result.verification),
        context_requests=len(result.context_requests_granted),
        functional_chain_verifications=1 if result.functional_chain_result == "VERIFIED" else 0,
        builder_skill_id=str(builder_activation.get("skill_id", "")),
        builder_skill_version=str(builder_activation.get("skill_version", "")),
        builder_skill_activated=bool(builder_activation.get("activated", False)),
        builder_skill_chars=int(builder_activation.get("chars", 0) or 0),
        causal_handoff_present=bool(
            next(
                (
                    dict(e.metadata).get("present")
                    for e in events
                    if e.event_type.value == "DEV_CAUSAL_HANDOFF"
                ),
                False,
            )
        ),
        causal_handoff_chars=int(
            next(
                (
                    dict(e.metadata).get("chars", 0)
                    for e in events
                    if e.event_type.value == "DEV_CAUSAL_HANDOFF"
                ),
                0,
            )
        ),
        skill_id=str(activation.get("skill_id", "")),
        skill_version=str(activation.get("skill_version", "")),
        skill_activated=bool(activation.get("activated", False)),
    )
    overhead_ms = (time.perf_counter() - recorded_ms) * 1000
    from punto.orchestrator.dev_cycle import causal_handoff

    handoff = causal_handoff(result.plan) if result.plan is not None else ""
    return {
        "record": record,
        "status": result.status.value,
        "plan": result.plan.model_dump(mode="json") if result.plan is not None else None,
        "causal_handoff": handoff,
        "causal_handoff_chars": len(handoff),
        "calls_detail": calls_detail,
        "first_attempt": _first_attempt(result, calls_detail, handoff),
        "audit_events": [
            {
                "event_type": event.event_type.value,
                "result": event.result.value,
                "metadata": dict(event.metadata),
            }
            for event in events
        ],
        "plan_status": result.plan_status.value,
        "error": result.error[:200],
        "change_issues": [issue.code for issue in result.change_issues],
        "overhead_ms": overhead_ms,
        "applied": [item.path for item in result.applied],
        "verification": [(item.name, item.exit_code) for item in result.verification],
        "gates": [item.outcome for item in result.authority_decisions],
        "repeated_blocks": _repeated_blocks(),
        "dirty_preserved": "M .gitignore" in _git(repo, "status", "--porcelain"),
    }


def _repeated_blocks() -> dict[str, int]:
    """Tamaño de los bloques que PUNTO repite en cada invocación (contexto repetido, §16)."""
    from punto.orchestrator import dev_cycle

    sizes: dict[str, int] = {}
    for label, attribute in REPEATED_BLOCKS:
        value = getattr(dev_cycle, attribute, "")
        sizes[label] = len(value) if isinstance(value, str) else 0
    return sizes


def _run_qa_case(case: dict[str, Any], *, root: Path) -> dict[str, Any]:
    """Caso F: verificación de consumidor por el camino real (sandbox web + navegador).

    No llama a ningún proveedor: mide el coste de **verificar**, que es la mitad del trabajo y no
    aparece en las métricas de llamadas. Si el caso no necesitara navegador, aquí habría 0 sesiones.
    """
    from punto.common import utc_now
    from punto.consumer_qa import (
        ConsumerQACase,
        QAExpectation,
        QAExpectationKind,
        QATarget,
        run_consumer_qa,
    )
    from punto.telemetry import RunEvidence, build_record

    workspace = root / "qa"
    shutil.copytree(ENGINE / "fixtures" / "consumer-qa-app", workspace / "app")
    qa_case = ConsumerQACase(
        qa_id="QA-910",
        title="baseline de verificación de consumidor",
        start_url="/",
        expectations=(
            QAExpectation(
                kind=QAExpectationKind.VISIBLE, target="#titulo", expected="#titulo visible"
            ),
        ),
    )
    target = QATarget(
        workspace=workspace,
        project_relative="app",
        preview_argv=(("python3", "-m", "http.server", "4173", "--bind", "0.0.0.0"),),
        timeout_seconds=300.0,
    )
    started_at = utc_now()
    started_clock = time.perf_counter()
    result = run_consumer_qa(qa_case, target, evidence_dir=workspace / "evidencia")
    elapsed_ms = int((time.perf_counter() - started_clock) * 1000)
    finished_at = utc_now()

    evidence = RunEvidence(
        run_id=f"{case['case_id']}-qa",
        case_id=case["case_id"],
        case_kind=case["kind"],
        mode="DETERMINISTIC",
        started_at=started_at,
        finished_at=finished_at,
        elapsed_ms=elapsed_ms,
        verification_count=1,
        verification_failures=0 if result.status.value == "PASS" else 1,
        success=result.status.value == "PASS",
        final_status=result.status.value,
        notes=("verificación de consumidor: 0 llamadas a proveedor",),
    )
    record = build_record(
        evidence,
        task_id=case["case_id"],
        verification_elapsed_ms=elapsed_ms,
        functional_chain_verifications=0,
    )
    return {
        "record": record,
        "status": result.status.value,
        "plan_status": "N/A",
        "error": result.reason[:200],
        "change_issues": [],
        "overhead_ms": 0.0,
        "applied": [],
        "verification": [("consumer_qa", 0 if result.status.value == "PASS" else 1)],
        "gates": [],
        "repeated_blocks": _repeated_blocks(),
        "dirty_preserved": True,
    }


def main() -> int:
    """Ejecuta los casos del modo pedido y escribe la evidencia del baseline."""
    mode = (sys.argv[1] if len(sys.argv) > 1 else "deterministic").upper()
    wanted = (sys.argv[2] if len(sys.argv) > 2 else "").upper()
    skill = os.environ.get("PUNTO_ARCHITECT_SKILL", "").strip()
    cases = CASES if mode == "DETERMINISTIC" else [item for item in CASES if item["real"]]
    if wanted:
        cases = [item for item in cases if item["case_id"] == wanted]
    results: list[dict[str, Any]] = []
    root = Path(tempfile.mkdtemp(prefix=f"punto-baseline-{mode.lower()}-"))
    print(f"modo={mode} casos={[c['case_id'] for c in cases]} skill={skill or '(ninguna)'}")
    for case in cases:
        case_root = root / case["case_id"]
        case_root.mkdir(parents=True, exist_ok=True)
        if case["kind"] == "F":
            outcome = _run_qa_case(case, root=case_root)
        else:
            outcome = _run_case(case, mode=mode, root=case_root)
        results.append({"case": case["case_id"], "kind": case["kind"], **outcome})
        record = outcome["record"]
        print(
            f"[{case['case_id']}] {outcome['status']:24} "
            f"elapsed={record.elapsed_ms:6}ms calls={record.provider_calls} "
            f"repairs={record.repair_rounds} prompt={record.prompt_chars}c "
            f"ctx={record.context_chars}c tokens={record.tokens.source} "
            f"chain={record.functional_chain_pass}"
        )
    EVIDENCE.mkdir(exist_ok=True)
    # El nombre lleva la versión de la skill: sin ella, una corrida nueva pisaría la evidencia
    # de la anterior y se perdería el control (defecto detectado en la ronda 2).
    # El nombre lleva la versión de **cada** skill declarada: mirar solo la del ARCHITECT hizo que
    # una corrida del BUILDER pisara la evidencia del control (defecto detectado en el experimento 02).
    partes: list[str] = []
    if skill:
        partes.append(f"architect-{skill.partition('@')[2] or 'sin-version'}")
    if os.environ.get("PUNTO_BUILDER_SKILL", "").strip():
        builder = os.environ["PUNTO_BUILDER_SKILL"].strip()
        partes.append(f"builder-{builder.partition('@')[2] or 'sin-version'}")
    suffix = f"-skill-{'-'.join(partes)}" if partes else ""
    evidence_name = f"baseline-{mode.lower()}{suffix}.json"
    (EVIDENCE / evidence_name).write_text(
        json.dumps(
            [
                {
                    "case": item["case"],
                    "kind": item["kind"],
                    "status": item["status"],
                    "plan_status": item["plan_status"],
                    "error": item["error"],
                    "change_issues": item["change_issues"],
                    "applied": item["applied"],
                    "verification": item["verification"],
                    "gates": item["gates"],
                    "repeated_blocks": item["repeated_blocks"],
                    "dirty_preserved": item["dirty_preserved"],
                    "overhead_ms": round(item["overhead_ms"], 3),
                    "plan": item.get("plan"),
                    "causal_handoff": item.get("causal_handoff", ""),
                    "causal_handoff_chars": item.get("causal_handoff_chars", 0),
                    "audit_events": item.get("audit_events", []),
                    "calls_detail": item.get("calls_detail", []),
                    "first_attempt": item.get("first_attempt", {}),
                    "record": item["record"].as_dict(),
                }
                for item in results
            ],
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"evidencia: {EVIDENCE / evidence_name}")
    shutil.rmtree(root, ignore_errors=True)
    return 0


# ---------------------------------------------------------------------------
# Los seis casos del baseline
# ---------------------------------------------------------------------------
_PLAN_BASE: dict[str, Any] = {
    "summary": "unificar la lista de tipos en una sola fuente",
    "files_to_read": ["src/lib/tipos.ts", "src/components/Rejilla.tsx"],
    "files_to_modify": [
        "src/lib/tipos.ts",
        "src/components/Rejilla.tsx",
        "src/components/Buscador.tsx",
    ],
    "files_to_create": [],
    "files_to_delete": [],
    "verification_commands": ["focused", "chain"],
    "risks": ["cambiar la interfaz sin querer"],
    "acceptance_mapping": ["una sola fuente de tipos"],
    "functional_chain": [
        {"step": "fuente canónica", "description": "tipos en un solo sitio", "verification": "focused"},
        {"step": "consumidores", "description": "la rejilla usa la fuente", "verification": "chain"},
    ],
}

#: Plan deliberadamente estrecho: deja fuera un consumidor para que la evidencia exija ampliarlo.
_PLAN_ESTRECHO: dict[str, Any] = {
    **_PLAN_BASE,
    "files_to_modify": ["src/lib/tipos.ts", "src/components/Rejilla.tsx"],
}

_CAMBIO_BUENO = {
    "summary": "tipos desde la fuente",
    "changes": [
        {
            "path": "src/lib/tipos.ts",
            "operation": "MODIFY",
            "content": "export const TIPOS = ['Casa', 'Apartamento'];\n",
            "reason": "unificar la fuente",
            "acceptance_criterion": "una sola fuente de tipos",
        },
        {
            "path": "src/components/Rejilla.tsx",
            "operation": "MODIFY",
            "content": "import { TIPOS } from '@/lib/tipos';\nexport function Rejilla() { return TIPOS.length; }\n",
            "reason": "consumir la fuente",
            "acceptance_criterion": "una sola fuente de tipos",
        },
        {
            "path": "src/components/Buscador.tsx",
            "operation": "MODIFY",
            "content": "import { TIPOS } from '@/lib/tipos';\nexport function Buscador() { return TIPOS.length; }\n",
            "reason": "consumir la fuente",
            "acceptance_criterion": "una sola fuente de tipos",
        },
    ],
}

CASES: list[dict[str, Any]] = [
    {
        "case_id": "CASE-A",
        "kind": "A",
        "title": "diagnóstico y causa raíz",
        "objective": "la lista de tipos debe salir de una sola fuente y la verificación debe pasar",
        "acceptance": ["una sola fuente de tipos"],
        "primary_role": "BUILDER",
        "real": False,
        "responses": [
            _PLAN_BASE,
            # Primer intento incompleto: la verificación fallará y habrá que diagnosticar.
            {
                "summary": "cambio incompleto",
                "changes": [
                    {
                        "path": "src/lib/tipos.ts",
                        "operation": "MODIFY",
                        "content": "export const TIPOS = ['Casa'];\n",
                    }
                ],
            },
            # Reparación con causa raíz declarada.
            {
                **_CAMBIO_BUENO,
                "root_cause": "la constante vista por la verificación no incluía el tipo Apartamento",
                "evidence": ["focused exit 1: TIPOS: export const TIPOS = ['Casa']"],
                "expected_effect": "la verificación focalizada encuentra el tipo exigido",
            },
        ],
    },
    {
        "case_id": "CASE-B",
        "kind": "B",
        "title": "arquitectura y planificación",
        "objective": "diseñar la fuente canónica de tipos y su cadena funcional completa",
        "acceptance": ["una sola fuente de tipos"],
        "primary_role": "ARCHITECT",
        "real": True,
        "responses": [_PLAN_BASE, _CAMBIO_BUENO],
    },
    {
        "case_id": "CASE-C",
        "kind": "C",
        "title": "alcance y cadena funcional",
        "objective": "cerrar la cadena funcional de tipos aunque aparezca un consumidor más",
        "acceptance": ["una sola fuente de tipos"],
        "primary_role": "BUILDER",
        "real": False,
        "responses": [
            _PLAN_ESTRECHO,
            {
                **_CAMBIO_BUENO,
                "scope_expansion": {
                    "trigger": "evidencia de la verificación",
                    "evidence": ["la cadena falla: Buscador declara su propia lista"],
                    "root_cause": "fuente de tipos duplicada en un segundo consumidor",
                    "resources": ["src/components/Buscador.tsx"],
                    "operations": ["MODIFY"],
                    "relationship": "consumidor del mismo concepto de dominio",
                },
            },
        ],
    },
    {
        "case_id": "CASE-D",
        "kind": "D",
        "title": "implementación local",
        "objective": "unificar los tipos en la fuente canónica y hacer que los consumidores la usen",
        "acceptance": ["una sola fuente de tipos"],
        "primary_role": "BUILDER",
        "real": True,
        "responses": [_PLAN_BASE, _CAMBIO_BUENO],
    },
    {
        "case_id": "CASE-E",
        "kind": "E",
        "title": "reparación basada en fallo real",
        "objective": "corregir la causa del fallo de la cadena funcional",
        "acceptance": ["una sola fuente de tipos"],
        "primary_role": "BUILDER",
        "real": True,
        "responses": [
            _PLAN_BASE,
            {
                "summary": "intento sin la cadena",
                "changes": [
                    {
                        "path": "src/lib/tipos.ts",
                        "operation": "MODIFY",
                        "content": "export const TIPOS = ['Casa', 'Apartamento'];\n",
                    }
                ],
            },
            {
                **_CAMBIO_BUENO,
                "root_cause": "los consumidores seguían declarando su propia lista de tipos",
                "evidence": ["chain exit 1: CADENA: False"],
                "expected_effect": "la comprobación de cadena pasa",
            },
        ],
    },
    {
        "case_id": "CASE-F",
        "kind": "F",
        "title": "QA y verificación",
        "objective": "verificar el comportamiento público de la aplicación de referencia",
        "acceptance": ["la pantalla principal es visible"],
        "primary_role": "",
        "real": False,
        "responses": [],
    },
]


if __name__ == "__main__":
    sys.exit(main())
