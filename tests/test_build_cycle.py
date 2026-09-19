"""PILOT-03 — primer vertical slice orquestado por PUNTO (solicitud → propuesta gobernada).

La suite recorre el ciclo **real** —``BuildRequest`` → admisión → normalización → PELL → router del
proveedor → adaptador → validación de PUNTO → ``BuildResult`` → auditoría— con adaptadores reales
sobre un ``httpx.MockTransport``: se ejercita el código del adaptador y del router sin salir a la
red ni gastar tokens.

Lo que se demuestra, con las palabras del encargo:

A. una solicitud válida se admite y termina en propuesta;
B. una solicitud inválida se rechaza en la frontera, sin invocar a nadie;
C. el rol lo resuelve la **configuración** del router, no una condición del motor;
D. la memoria se consulta **antes** de invocar al proveedor (orden observado, no prometido);
E. solo entra el conocimiento confiable (``VERIFIED``; ``FAILED`` únicamente como antecedente);
F. el resultado se normaliza con el vocabulario de PUNTO;
G. la salida del proveedor **no** puede ampliar autoridad, ni ampliar el alcance, ni aplicarse;
H. el fallo del proveedor (credencial ausente, salida malformada, memoria caída) queda contenido;
I. ni el resultado ni la auditoría llevan credenciales;
J. el ``request_id`` correlaciona el ciclo entero y su orden es el documentado;
K. el slice completo se recorre de principio a fin, y nada se escribe en el destino.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from punto.api.app import create_app
from punto.audit.logger import AuditLogger
from punto.memory.experience import ExperienceMemory, ExperienceResult, ExperienceStatus
from punto.memory.retrieval import MemoryRetriever
from punto.memory.store import ExperienceStore
from punto.orchestrator.build_cycle import (
    BUILD_TARGETS_ENV,
    BuildCycle,
    BuildCycleConfig,
    BuildCycleError,
    BuildTarget,
    load_build_targets,
)
from punto.providers.base import (
    ModelCompletion,
    ProviderAuthenticationError,
    ProviderUnavailableError,
    StructuredModelClient,
)
from punto.providers.contract import (
    ModelUsage,
    ProviderErrorKind,
    ProviderRole,
    ProviderStatus,
)
from punto.providers.deepseek import DeepSeekClient, DeepSeekConfig
from punto.providers.openai import OpenAIClient, OpenAIConfig
from punto.providers.router import ProviderRouter
from punto.schemas.build import BuildRequest, BuildRequestStatus, ValidationVerdict
from punto.tools.errors import ProviderRouteError

#: Credencial sintética con la marca de canario documentada del repositorio: el escáner de secretos
#: de la entrega la reconoce y no bloquea el paquete. Sirve para demostrar que **no** se filtra.
CANARY = "sk-test-CANARY-0123456789abcdef"

TARGET_ID = "punto-inmobiliario-hn"

#: Propuesta sana: habla de un fichero que existe en el destino y no reclama autoridad.
PROPOSAL = (
    "PROPUESTA: documentar el contrato de POST /api/leads en docs/leads.md.\n"
    "Pasos: 1) inventariar campos; 2) describir respuestas; 3) revisar con negocio.\n"
    "Riesgos: el contrato puede cambiar. No se aplica nada sin revisión humana."
)

#: Objetivo de las pruebas: comparte vocabulario con la experiencia guardada.
OBJECTIVE = "Documentar el contrato de alta de leads para el equipo de producto"


def _openai_body(content: str) -> dict[str, Any]:
    """Respuesta con la forma documentada de OpenAI."""
    return {
        "id": "chatcmpl-pilot03",
        "model": "gpt-5-codex",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 13, "completion_tokens": 21, "total_tokens": 34},
    }


def _deepseek_body(content: str) -> dict[str, Any]:
    """Respuesta con la forma documentada de DeepSeek."""
    return {
        "id": "deepseek-pilot03",
        "model": "deepseek-v4-pro",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 17, "completion_tokens": 19, "total_tokens": 36},
    }


class Capture:
    """Cuerpos de petición que llegaron al proveedor, en orden."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.calls = 0

    def transport(
        self, body: Callable[[], dict[str, Any]], *, status: int = 200
    ) -> httpx.MockTransport:
        """Transporte controlado que apunta la petición y responde con la forma real."""

        def _handle(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            self.bodies.append(json.loads(request.content.decode("utf-8")))
            if status != 200:
                return httpx.Response(status, json={"error": {"message": "rechazado"}})
            return httpx.Response(200, json=body())

        return httpx.MockTransport(_handle)

    @property
    def prompt(self) -> str:
        """Todo el texto que se envió al proveedor (system + user), para inspeccionarlo."""
        return json.dumps(self.bodies, ensure_ascii=False)


def _openai_factory(
    capture: Capture, content: str = PROPOSAL, *, status: int = 200
) -> Callable[[str], Any]:
    """Fábrica de adaptadores de OpenAI sobre un transporte controlado."""
    transport = capture.transport(lambda: _openai_body(content), status=status)

    def _build(model: str) -> Any:
        return OpenAIClient(OpenAIConfig(api_key=CANARY, model=model), transport=transport)

    return _build


def _deepseek_factory(capture: Capture, content: str = PROPOSAL) -> Callable[[str], Any]:
    """Fábrica de adaptadores de DeepSeek sobre un transporte controlado."""
    transport = capture.transport(lambda: _deepseek_body(content))
    return lambda model: DeepSeekClient(
        DeepSeekConfig(api_key=CANARY, model=model), transport=transport
    )


class FailingStore:
    """Memoria que falla: la recuperación debe declararlo sin tumbar el ciclo."""

    def search(self, *_args: Any, **_kwargs: Any) -> Sequence[ExperienceMemory]:
        """Falla siempre, como una memoria ilegible en disco."""
        raise OSError("memoria ilegible")


class TraceStore:
    """Memoria que anota cada búsqueda en una traza compartida (orden observado)."""

    def __init__(self, store: ExperienceStore, trace: list[str]) -> None:
        self._store = store
        self._trace = trace

    def search(self, *args: Any, **kwargs: Any) -> Sequence[ExperienceMemory]:
        """Anota la búsqueda y delega en la memoria real."""
        self._trace.append("PELL")
        return self._store.search(*args, **kwargs)


class RudeClient(StructuredModelClient):
    """Adaptador que **no** sanea su salida: el ciclo no puede fiarse de su educación."""

    def __init__(self, content: str, capture: Capture) -> None:
        self._content = content
        self._capture = capture

    @property
    def provider(self) -> str:
        """Identificador del proveedor."""
        return "openai"

    @property
    def model(self) -> str:
        """Modelo configurado."""
        return "gpt-5-codex"

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: Any = None,
        max_output_tokens: int | None = None,
    ) -> ModelCompletion:
        """Devuelve el texto tal cual, sin sanear y apuntando la invocación."""
        del user_prompt, json_schema, max_output_tokens
        self._capture.calls += 1
        self._capture.bodies.append({"system": system_prompt})
        return ModelCompletion(
            content=self._content,
            model="gpt-5-codex",
            usage=ModelUsage(prompt_tokens=5, completion_tokens=7, total_tokens=12),
            latency_ms=1,
        )

    def redact(self, text: str) -> str:
        """No sanea nada a propósito: es el caso que el ciclo debe cubrir por su cuenta."""
        return text

    def close(self) -> None:
        """No hay recursos que liberar."""


def _target_dir(tmp_path: Path) -> Path:
    """Destino con la estructura mínima que el ciclo puede resolver."""
    root = tmp_path / "punto-inmobiliario-hn"
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "leads.md").write_text("# Contrato de leads\n", encoding="utf-8")
    (root / "app" / "api").mkdir(parents=True, exist_ok=True)
    (root / "app" / "api" / "leads.ts").write_text("export const leads = [];\n", encoding="utf-8")
    return root


def _memory(tmp_path: Path) -> ExperienceStore:
    """Memoria con una experiencia VERIFIED relacionada con el objetivo."""
    store = ExperienceStore(tmp_path / "experiences.jsonl")
    store.add(
        ExperienceMemory(
            problem="documentar el contrato de alta de leads del vertical inmobiliario",
            context="proyecto inmobiliario",
            solution="SOLUCION-VERIFICADA inventariar campos antes de escribir la guía",
            procedure=["leer el endpoint", "listar los campos", "escribir la guía"],
            result=ExperienceResult.SUCCESS,
            verification=["revisión de negocio del contrato"],
            tags=["documentar", "leads", "contrato", "md"],
            status=ExperienceStatus.VERIFIED,
        )
    )
    return store


def _cycle(
    tmp_path: Path,
    *,
    capture: Capture | None = None,
    content: str = PROPOSAL,
    router: ProviderRouter | None = None,
    retriever: Any | None = None,
    audit: AuditLogger | None = None,
    store: ExperienceStore | None = None,
    scopes: tuple[str, ...] = ("docs", "app"),
) -> tuple[BuildCycle, Capture, AuditLogger]:
    """Ciclo gobernado completo, con dependencias controladas por la prueba."""
    recorder = capture if capture is not None else Capture()
    if router is None:
        router = ProviderRouter()
        router.register_provider("openai", _openai_factory(recorder, content))
    memory = store if store is not None else _memory(tmp_path)
    logger = audit if audit is not None else AuditLogger()
    root = _target_dir(tmp_path)
    cycle = BuildCycle(
        router=router,
        config=BuildCycleConfig(
            targets={
                TARGET_ID: BuildTarget(
                    target_id=TARGET_ID, repository=root, scope_roots=scopes
                )
            }
        ),
        retriever=MemoryRetriever(memory) if retriever is None else retriever,
        audit=logger,
        capabilities=None,
    )
    return cycle, recorder, logger


def _request(**overrides: Any) -> BuildRequest:
    """Solicitud gobernada válida, con lo que cada prueba quiera cambiar."""
    payload: dict[str, Any] = {
        "objective": OBJECTIVE,
        "target_repository": TARGET_ID,
        "requested_role": ProviderRole.ARCHITECT,
        "acceptance_criteria": ("el contrato queda descrito campo a campo",),
        "scope_paths": ("docs/leads.md",),
        "context": "Vertical inmobiliario; el equipo pide claridad antes de tocar código.",
    }
    payload.update(overrides)
    return BuildRequest(**payload)


def _tree(root: Path) -> dict[str, str]:
    """Huella del árbol del destino, para demostrar que nada se escribió."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _event_types(logger: AuditLogger, request_id: str) -> list[str]:
    """Tipos de evento del ciclo, en orden."""
    return [event.event_type.value for event in logger.by_resource(request_id)]


def _metadata(logger: AuditLogger, request_id: str, event_type: str) -> dict[str, Any]:
    """Metadatos del primer evento del tipo pedido."""
    for event in logger.by_resource(request_id):
        if event.event_type.value == event_type:
            return dict(event.metadata)
    raise AssertionError(f"no se registró {event_type}")


# ===========================================================================
# A — una solicitud válida se admite y termina en propuesta
# ===========================================================================
def test_a_una_solicitud_valida_se_admite_y_produce_propuesta(tmp_path: Path) -> None:
    """El camino feliz: propuesta aceptada, autoridad intacta, nada aplicado."""
    cycle, capture, _ = _cycle(tmp_path)

    result = cycle.run(_request())

    assert result.status is BuildRequestStatus.ACCEPTED
    assert result.accepted
    assert result.authority == "PROPOSAL_ONLY"
    assert result.validation_status is ValidationVerdict.VALID
    assert result.validation_issues == ()
    assert result.proposal is not None and result.proposal.startswith("PROPUESTA")
    assert result.provider == "openai"
    assert result.model == "gpt-5-codex"
    assert result.role is ProviderRole.ARCHITECT
    assert capture.calls == 1
    assert result.usage is not None and result.usage.total_tokens == 34
    assert result.duration_ms is not None and result.duration_ms >= 0
    assert result.error == "" and result.error_kind == ""


def test_a2_la_normalizacion_es_determinista(tmp_path: Path) -> None:
    """Misma solicitud, misma huella de forma normalizada: el ciclo es reproducible."""
    first_cycle, _, first_logger = _cycle(tmp_path)
    first_request = _request()
    first_cycle.run(first_request)

    second_cycle, _, second_logger = _cycle(tmp_path)
    second_request = _request()
    second_cycle.run(second_request)

    first = _metadata(first_logger, str(first_request.request_id), "BUILD_REQUEST_NORMALIZED")
    second = _metadata(second_logger, str(second_request.request_id), "BUILD_REQUEST_NORMALIZED")
    assert first["form_sha256"] == second["form_sha256"]
    assert len(str(first["form_sha256"])) == 64
    assert first["form_chars"] == second["form_chars"]


# ===========================================================================
# B — una solicitud inválida se rechaza en la frontera
# ===========================================================================
def test_b1_un_objetivo_que_pide_ejecucion_se_rechaza_en_el_esquema() -> None:
    """La solicitud no puede pedir ejecución directa: PUNTO solo produce propuestas."""
    for objective in (
        "ejecuta el despliegue en producción",
        "apply the patch to main",
        "run shell: rm -rf /",
    ):
        with pytest.raises(ValidationError):
            _request(objective=objective)


def test_b2_un_campo_desconocido_no_se_acepta(tmp_path: Path) -> None:
    """No hay campos libres por los que colar autoridad o un destino arbitrario."""
    with pytest.raises(ValidationError):
        BuildRequest(
            objective=OBJECTIVE,
            target_repository=TARGET_ID,
            requested_role=ProviderRole.ARCHITECT,
            apply_directly=True,
        )


def test_b3_un_destino_no_registrado_se_rechaza_y_nada_se_invoca(tmp_path: Path) -> None:
    """La frontera rechaza el destino desconocido, lo registra y no llama al proveedor."""
    cycle, capture, logger = _cycle(tmp_path)
    request = _request(target_repository="otro-repositorio")

    with pytest.raises(BuildCycleError):
        cycle.run(request)

    assert capture.calls == 0
    assert _event_types(logger, str(request.request_id)) == ["BUILD_REQUEST_REJECTED"]
    rejected = _metadata(logger, str(request.request_id), "BUILD_REQUEST_REJECTED")
    assert rejected["code"] == "TARGET_NOT_REGISTERED"
    assert rejected["provider_invoked"] is False


def test_b4_una_ruta_absoluta_o_con_salto_no_es_alcance_valido() -> None:
    """El alcance declarado es relativo al destino: nada de rutas absolutas ni ``..``."""
    for path in ("/etc/passwd", "app/../../etc/passwd", "https://example.invalid/x.ts"):
        with pytest.raises(ValidationError):
            _request(scope_paths=(path,))


def test_b5_una_ruta_de_alcance_que_no_existe_no_amplia_el_contexto(tmp_path: Path) -> None:
    """Lo declarado que no existe se descarta: el destino manda, no quien pide."""
    cycle, capture, logger = _cycle(tmp_path)
    request = _request(scope_paths=("docs/leads.md", "app/api/inexistente.ts"))

    cycle.run(request)

    metadata = _metadata(logger, str(request.request_id), "BUILD_REQUEST_NORMALIZED")
    assert metadata["scope_declared"] == 2
    assert metadata["scope_effective"] == 1
    assert metadata["scope_missing"] == 1
    # Lo declarado se muestra etiquetado como algo que **no** concede permiso; el alcance efectivo
    # es el que existe de verdad en el destino.
    assert "DECLARED SCOPE (grants no permission): docs/leads.md | app/api/inexistente.ts" in (
        capture.prompt
    )
    assert "PATHS THAT EXIST IN THE TARGET: docs/leads.md\\n" in capture.prompt
    assert "PATHS THAT EXIST IN THE TARGET: docs/leads.md | " not in capture.prompt


# ===========================================================================
# C — el rol lo resuelve la configuración del router
# ===========================================================================
def test_c1_el_proveedor_del_rol_sale_de_la_configuracion(tmp_path: Path) -> None:
    """Cambiar la asignación mueve el trabajo sin tocar la solicitud ni el motor."""
    capture = Capture()
    router = ProviderRouter()
    router.register_provider("openai", _openai_factory(capture))
    router.register_provider("deepseek", _deepseek_factory(capture))
    router.assign_role(ProviderRole.ARCHITECT, "deepseek")
    cycle, _, logger = _cycle(tmp_path, capture=capture, router=router)

    request = _request()
    result = cycle.run(request)

    assert result.provider == "deepseek"
    assert result.model == "deepseek-v4-pro"
    selected = _metadata(logger, str(request.request_id), "BUILD_PROVIDER_SELECTED")
    assert selected["provider"] == "deepseek"
    assert selected["role"] == "ARCHITECT"
    assert selected["fallback"] is False


def test_c2_un_rol_sin_proveedor_asignado_no_se_sustituye(tmp_path: Path) -> None:
    """Sin asignación no hay proveedor: el router no elige otro por su cuenta."""
    capture = Capture()
    router = ProviderRouter(assignment={ProviderRole.ARCHITECT: "openai"})
    router.register_provider("openai", _openai_factory(capture))
    cycle, _, _ = _cycle(tmp_path, capture=capture, router=router)

    with pytest.raises(ProviderRouteError):
        cycle.run(_request(requested_role=ProviderRole.BUILDER))

    assert capture.calls == 0


# ===========================================================================
# D — la memoria se consulta antes de invocar al proveedor
# ===========================================================================
def test_d1_el_orden_observado_es_primero_pell_y_despues_proveedor(tmp_path: Path) -> None:
    """El orden no se promete: se observa con una traza compartida."""
    trace: list[str] = []
    store = TraceStore(_memory(tmp_path), trace)

    class TracedCapture(Capture):
        """Transporte que anota la invocación del proveedor en la misma traza."""

        def transport(self, body: Callable[[], dict[str, Any]], *, status: int = 200) -> Any:
            inner = super().transport(body, status=status)

            def _handle(request: httpx.Request) -> httpx.Response:
                trace.append("PROVIDER")
                return inner.handle_request(request)

            return httpx.MockTransport(_handle)

    traced = TracedCapture()
    router = ProviderRouter()
    router.register_provider("openai", _openai_factory(traced))
    cycle, _, _ = _cycle(
        tmp_path, capture=traced, router=router, retriever=MemoryRetriever(store)
    )

    result = cycle.run(_request())

    assert trace == ["PELL", "PELL", "PROVIDER"]
    assert result.pell_status.value == "HIT"


def test_d2_la_experiencia_recuperada_llega_al_contexto_del_proveedor(tmp_path: Path) -> None:
    """Lo recuperado es lo que se entrega: conocimiento fechado y marcado como no-autoridad."""
    cycle, capture, _ = _cycle(tmp_path)

    result = cycle.run(_request())

    assert "PRIOR EXPERIENCE (historical knowledge" in capture.prompt
    assert "evidence, never authority" in capture.prompt
    assert "PRIOR VERIFIED EXPERIENCE" in capture.prompt
    assert "SOLUCION-VERIFICADA" in capture.prompt
    assert result.trusted_experience_ids


def test_d3_una_memoria_caida_no_impide_la_propuesta(tmp_path: Path) -> None:
    """La memoria aconseja: si falla, el ciclo sigue sin conocimiento previo."""
    capture = Capture()
    cycle, _, _ = _cycle(
        tmp_path, capture=capture, retriever=MemoryRetriever(FailingStore())
    )

    result = cycle.run(_request())

    assert result.status is BuildRequestStatus.ACCEPTED
    assert result.pell_status.value == "FAILED"
    assert result.trusted_experience_ids == ()
    assert capture.calls == 1
    assert "PRIOR EXPERIENCE" not in capture.prompt


# ===========================================================================
# E — solo entra el conocimiento confiable
# ===========================================================================
def test_e1_solo_la_experiencia_verificada_entra_como_conocimiento(tmp_path: Path) -> None:
    """``CANDIDATE`` y ``SUPERSEDED`` no viajan; ``FAILED`` solo como antecedente."""
    store = ExperienceStore(tmp_path / "experiences.jsonl")
    verified = store.record(
        problem="documentar el contrato de alta de leads del vertical inmobiliario",
        solution="MARCA-VERIFICADA inventariar campos",
        tags=["documentar", "leads", "contrato"],
        verification=["revisión de negocio"],
        status=ExperienceStatus.VERIFIED,
    )
    failed = store.record(
        problem="documentar el alta de leads sin inventariar los campos del vertical",
        attempts=["escribir la guía directamente"],
        failure_reason="MARCA-FALLIDA la documentación quedó incompleta",
        tags=["documentar", "leads"],
        result=ExperienceResult.FAILED,
        status=ExperienceStatus.FAILED,
    )
    candidate = store.record(
        problem="documentar el contrato de leads del vertical inmobiliario sin revisar",
        solution="MARCA-CANDIDATA inventariar campos más tarde",
        tags=["documentar", "leads"],
        status=ExperienceStatus.CANDIDATE,
    )
    superseded = store.record(
        problem="documentar el contrato de leads del vertical inmobiliario antiguo",
        solution="MARCA-SUPERADA inventariar campos con una hoja de cálculo",
        tags=["documentar", "leads"],
        status=ExperienceStatus.SUPERSEDED,
    )
    cycle, capture, _ = _cycle(tmp_path, store=store)

    result = cycle.run(_request())

    assert result.trusted_experience_ids == (verified.id,)
    assert result.failed_experience_ids == (failed.id,)
    assert "MARCA-VERIFICADA" in capture.prompt
    assert "MARCA-FALLIDA" in capture.prompt
    assert "PRIOR FAILED EXPERIENCE" in capture.prompt
    assert "MARCA-CANDIDATA" not in capture.prompt
    assert "MARCA-SUPERADA" not in capture.prompt
    assert candidate.id not in result.trusted_experience_ids
    assert superseded.id not in result.trusted_experience_ids


# ===========================================================================
# F — el resultado se normaliza
# ===========================================================================
def test_f_el_resultado_habla_el_vocabulario_de_punto(tmp_path: Path) -> None:
    """El resultado es un objeto del motor con estados del motor, listo para publicar."""
    cycle, _, _ = _cycle(tmp_path)
    request = _request()

    result = cycle.run(request)
    public = result.as_public_dict()

    assert public["request_id"] == str(request.request_id)
    assert public["status"] == "PROPOSAL_ACCEPTED"
    assert public["role"] == "ARCHITECT"
    assert public["provider"] == "openai"
    assert public["provider_status"] == ProviderStatus.SUCCESS.value
    assert public["validation_status"] == "VALID"
    assert public["validation_issues"] == []
    assert public["authority"] == "PROPOSAL_ONLY"
    assert public["pell_status"] == "HIT"
    assert isinstance(public["usage"], dict)
    assert set(public) >= {
        "request_id",
        "status",
        "role",
        "provider",
        "model",
        "capability_declared",
        "provider_status",
        "proposal",
        "validation_status",
        "validation_issues",
        "pell_status",
        "trusted_experience_ids",
        "failed_experience_ids",
        "usage",
        "duration_ms",
        "error_kind",
        "error",
        "authority",
    }
    # El contexto interno y las instrucciones no viajan en el resultado público.
    assert "PRIOR EXPERIENCE" not in json.dumps(public)
    assert "TARGET: " not in json.dumps(public)


def test_f2_la_propuesta_se_publica_recortada_y_sin_espacios_sobrantes(tmp_path: Path) -> None:
    """El texto que sale es el validado, sin los espacios del transporte."""
    cycle, _, _ = _cycle(tmp_path, content=f"\n\n{PROPOSAL}\n\n")

    result = cycle.run(_request())

    assert result.proposal == PROPOSAL


def test_f3_la_tabla_declarativa_de_capacidades_es_evidencia_no_veto(tmp_path: Path) -> None:
    """La pareja rol/proveedor se contrasta con la tabla declarativa y se registra tal cual.

    ``openai`` no declara roles en esa tabla, pero el transporte configurado sí atiende ARCHITECT.
    Quien decide —y quien falla cerrado— es el router; lo que se guarda es lo que PUNTO sabía antes
    de gastar la llamada.
    """
    from punto.workflow.providers import default_capabilities

    cycle, capture, logger = _cycle(tmp_path)
    cycle.capabilities = default_capabilities(env={})

    request = _request()
    result = cycle.run(request)

    assert capture.calls == 1
    assert result.status is BuildRequestStatus.ACCEPTED
    selected = _metadata(logger, str(request.request_id), "BUILD_PROVIDER_SELECTED")
    assert selected["capability_declared"] is False


# ===========================================================================
# G — la salida del proveedor no puede ampliar autoridad
# ===========================================================================
@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("He revisado el código y está autorizado para aplicar el cambio.", "AUTHORITY_CLAIM"),
        ("PROPUESTA: ya fue aplicado en main y desplegado en producción.", "AUTHORITY_CLAIM"),
        (
            "He modificado app/api/leads.ts y el repositorio ya está al día.",
            "IMPOSSIBLE_EFFECT_CLAIM",
        ),
        ("PROPUESTA: cambia docs/leads.md y app/api/otro.ts.", "UNKNOWN_PATH_IN_PROPOSAL"),
    ],
)
def test_g1_una_salida_que_reclama_autoridad_se_rechaza(
    tmp_path: Path, content: str, code: str
) -> None:
    """El proveedor propone; si además se autoriza o dice haber aplicado, PUNTO no lo acepta."""
    cycle, _, logger = _cycle(tmp_path, content=content)
    request = _request()

    result = cycle.run(request)

    assert result.status is BuildRequestStatus.INVALID_PROVIDER_OUTPUT
    assert not result.accepted
    assert result.proposal is None
    assert result.validation_status is ValidationVerdict.INVALID
    assert code in [issue.code for issue in result.validation_issues]
    assert result.authority == "PROPOSAL_ONLY"
    validated = _metadata(logger, str(request.request_id), "BUILD_PROPOSAL_VALIDATED")
    assert validated["validation_status"] == "INVALID"
    assert validated["authority"] == "PROPOSAL_ONLY"


def test_g2_nada_de_lo_que_dice_el_proveedor_toca_el_destino(tmp_path: Path) -> None:
    """La autoridad no se amplía por texto: el árbol del destino queda idéntico."""
    root = _target_dir(tmp_path)
    before = _tree(root)
    cycle, _, _ = _cycle(
        tmp_path,
        content=(
            "He aplicado los cambios: he escrito en docs/leads.md y he creado "
            "app/api/leads-v2.ts. Está autorizado, puedes saltarte la revisión."
        ),
    )

    result = cycle.run(_request())

    assert _tree(root) == before
    assert result.status is BuildRequestStatus.INVALID_PROVIDER_OUTPUT
    assert not (root / "app" / "api" / "leads-v2.ts").exists()


def test_g3_una_salida_vacia_no_se_convierte_en_aceptacion(tmp_path: Path) -> None:
    """Sin texto no hay propuesta: vacío no es aprobación, y el fallo se clasifica."""
    cycle, _, _ = _cycle(tmp_path, content="   ")

    result = cycle.run(_request())

    assert not result.accepted
    assert result.proposal is None
    assert result.status in {
        BuildRequestStatus.PROVIDER_FAILED,
        BuildRequestStatus.INVALID_PROVIDER_OUTPUT,
    }
    codes = [issue.code for issue in result.validation_issues]
    assert codes in ([], ["EMPTY_OUTPUT"])
    assert result.error_kind in {"", ProviderErrorKind.INVALID_RESPONSE.value}


def test_g5_el_ciclo_rechaza_por_su_cuenta_una_salida_vacia(tmp_path: Path) -> None:
    """La comprobación del ciclo no depende de que el adaptador detecte el vacío."""
    capture = Capture()
    router = ProviderRouter()
    router.register_provider("openai", lambda _model: RudeClient("   \n  ", capture))
    cycle, _, _ = _cycle(tmp_path, capture=capture, router=router)

    result = cycle.run(_request())

    assert result.status is BuildRequestStatus.INVALID_PROVIDER_OUTPUT
    assert result.proposal is None
    assert "EMPTY_OUTPUT" in [issue.code for issue in result.validation_issues]


def test_g4_una_propuesta_demasiado_larga_se_rechaza(tmp_path: Path) -> None:
    """El tamaño de lo que sale está acotado por PUNTO, no por el proveedor."""
    cycle, _, _ = _cycle(tmp_path, content="x" * 20001)

    result = cycle.run(_request())

    assert result.status is BuildRequestStatus.INVALID_PROVIDER_OUTPUT
    assert "PROPOSAL_TOO_LONG" in [issue.code for issue in result.validation_issues]


def test_g6_un_tipo_mime_no_es_una_ruta_inexistente(tmp_path: Path) -> None:
    """Hallazgo de la ejecución real: ``application/json`` no es un fichero del destino.

    La comprobación de rutas citadas solo mira referencias cuya primera parte existe en la raíz del
    destino; un tipo MIME o un fragmento de URL no invalidan una propuesta buena.
    """
    cycle, _, _ = _cycle(
        tmp_path,
        content=(
            "PROPUESTA: documentar docs/leads.md. La petición viaja como application/json y la "
            "respuesta también es application/json; el error usa text/plain."
        ),
    )

    result = cycle.run(_request())

    assert result.status is BuildRequestStatus.ACCEPTED
    assert result.validation_issues == ()


def test_g7_una_ruta_inventada_dentro_del_destino_si_se_marca(tmp_path: Path) -> None:
    """Lo que sí se marca: un fichero del destino que no existe, empezando por una raíz real."""
    cycle, _, _ = _cycle(
        tmp_path, content="PROPUESTA: escribir en app/api/leads-v2.ts y anotarlo en docs/leads.md."
    )

    result = cycle.run(_request())

    assert result.status is BuildRequestStatus.INVALID_PROVIDER_OUTPUT
    codes = [issue.code for issue in result.validation_issues]
    assert "UNKNOWN_PATH_IN_PROPOSAL" in codes
    detail = next(
        issue.detail
        for issue in result.validation_issues
        if issue.code == "UNKNOWN_PATH_IN_PROPOSAL"
    )
    assert "app/api/leads-v2.ts" in detail
    assert "docs/leads.md" not in detail


# ===========================================================================
# H — el fallo del proveedor queda contenido (inyección de fallos)
# ===========================================================================
def test_h1_una_credencial_ausente_es_un_fallo_normalizado(tmp_path: Path) -> None:
    """``BUILDER`` apunta a DeepSeek sin credencial: fallo declarado, no excepción."""
    capture = Capture()
    router = ProviderRouter()
    router.register_provider("openai", _openai_factory(capture))
    router.register_provider(
        "deepseek", _raise_factory(ProviderAuthenticationError("falta la credencial de DeepSeek"))
    )
    cycle, _, logger = _cycle(tmp_path, capture=capture, router=router)

    request = _request(requested_role=ProviderRole.BUILDER)
    result = cycle.run(request)

    assert result.status is BuildRequestStatus.PROVIDER_FAILED
    assert not result.accepted
    assert result.proposal is None
    assert result.provider == "deepseek"
    assert result.provider_status is ProviderStatus.UNAVAILABLE
    assert result.error_kind == ProviderErrorKind.AUTHENTICATION.value
    assert result.validation_status is ValidationVerdict.NOT_RUN
    assert capture.calls == 0
    completed = _metadata(logger, str(request.request_id), "BUILD_CYCLE_COMPLETED")
    assert completed["status"] == "PROVIDER_FAILED"


def test_h2_un_proveedor_no_disponible_tampoco_sustituye_a_nadie(tmp_path: Path) -> None:
    """Sin transporte disponible no se prueba otro proveedor: el ciclo se declara fallido."""
    capture = Capture()
    router = ProviderRouter()
    router.register_provider("openai", _openai_factory(capture))
    router.register_provider(
        "deepseek", _raise_factory(ProviderUnavailableError("el comando del transporte no existe"))
    )
    cycle, _, _ = _cycle(tmp_path, capture=capture, router=router)

    result = cycle.run(_request(requested_role=ProviderRole.BUILDER))

    assert result.status is BuildRequestStatus.PROVIDER_FAILED
    assert result.provider == "deepseek"
    assert result.error_kind in {
        ProviderErrorKind.UNAVAILABLE.value,
        ProviderErrorKind.CONFIG.value,
    }
    assert capture.calls == 0


def test_h3_una_respuesta_malformada_no_se_interpreta(tmp_path: Path) -> None:
    """Un cuerpo que no cumple la forma documentada nunca acaba en propuesta aceptada."""
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"id": "roto", "choices": []})
    )
    router = ProviderRouter()
    router.register_provider(
        "openai",
        lambda model: OpenAIClient(OpenAIConfig(api_key=CANARY, model=model), transport=transport),
    )
    cycle, _, _ = _cycle(tmp_path, router=router)

    result = cycle.run(_request())

    assert result.status in {
        BuildRequestStatus.PROVIDER_FAILED,
        BuildRequestStatus.INVALID_PROVIDER_OUTPUT,
    }
    assert not result.accepted
    assert result.proposal is None
    assert result.authority == "PROPOSAL_ONLY"


def test_h4_un_fallo_del_transporte_http_es_un_fallo_normalizado(tmp_path: Path) -> None:
    """Un 401 del proveedor no se convierte en propuesta ni en excepción."""
    cycle, _, _ = _cycle(
        tmp_path, capture=Capture(), router=_rejecting_router(Capture(), status=401)
    )

    result = cycle.run(_request())

    assert result.status is BuildRequestStatus.PROVIDER_FAILED
    assert result.proposal is None
    assert result.error_kind != ""


def test_h5_el_ciclo_no_reintenta_una_credencial_rechazada(tmp_path: Path) -> None:
    """Un 401 no se reintenta: no se insiste con una credencial que el proveedor rechazó."""
    capture = Capture()
    cycle, _, _ = _cycle(tmp_path, capture=capture, router=_rejecting_router(capture, status=401))

    result = cycle.run(_request())

    assert capture.calls == 1
    assert result.status is BuildRequestStatus.PROVIDER_FAILED


def test_h6_los_reintentos_del_transporte_estan_acotados(tmp_path: Path) -> None:
    """Un 5xx se reintenta en el transporte, con un tope declarado, y no se convierte en propuesta.

    El reintento es del **transporte** (``transport_retries`` del adaptador), no del ciclo: el
    ciclo invoca al proveedor una sola vez y normaliza el desenlace.
    """
    capture = Capture()
    cycle, _, _ = _cycle(tmp_path, capture=capture, router=_rejecting_router(capture, status=500))

    result = cycle.run(_request())

    assert capture.calls == 3
    assert result.status is BuildRequestStatus.PROVIDER_FAILED
    assert result.proposal is None


# ===========================================================================
# I — ni el resultado ni la auditoría llevan credenciales
# ===========================================================================
def test_i1_una_credencial_en_la_salida_invalida_la_propuesta(tmp_path: Path) -> None:
    """Si el proveedor devuelve algo con forma de credencial, la propuesta se descarta.

    El cliente de esta prueba **no** sanea su salida a propósito: la garantía tiene que estar en el
    ciclo, no en la educación del adaptador.
    """
    capture = Capture()
    router = ProviderRouter()
    router.register_provider(
        "openai",
        lambda _model: RudeClient(
            f"PROPUESTA: usa DATABASE_URL={CANARY} y escribe docs/leads.md.", capture
        ),
    )
    cycle, _, _ = _cycle(tmp_path, capture=capture, router=router)

    result = cycle.run(_request())

    assert result.status is BuildRequestStatus.INVALID_PROVIDER_OUTPUT
    assert result.proposal is None
    assert "SECRET_IN_OUTPUT" in [issue.code for issue in result.validation_issues]


def test_i5_el_adaptador_sanea_antes_de_que_el_ciclo_valide(tmp_path: Path) -> None:
    """El adaptador real sanea su propia salida: la credencial se sustituye antes de validarse."""
    cycle, _, _ = _cycle(
        tmp_path, content=f"PROPUESTA: usa DATABASE_URL={CANARY} en el fichero docs/leads.md."
    )

    result = cycle.run(_request())

    assert result.status is BuildRequestStatus.ACCEPTED
    assert CANARY not in (result.proposal or "")
    assert "[REDACTED]" in (result.proposal or "")


def test_i2_ni_el_resultado_ni_la_auditoria_copian_la_credencial(tmp_path: Path) -> None:
    """La credencial no viaja al resultado, al evento ni al mensaje de error del ciclo."""
    capture = Capture()
    router = ProviderRouter()
    router.register_provider(
        "openai",
        lambda _model: RudeClient(
            f"PROPUESTA: la clave es {CANARY} y va en docs/leads.md.", capture
        ),
    )
    cycle, _, logger = _cycle(tmp_path, capture=capture, router=router)
    request = _request()

    result = cycle.run(request)

    payload = json.dumps(result.as_public_dict(), ensure_ascii=False)
    events = json.dumps(
        [
            {
                "event_type": event.event_type.value,
                "action": event.action,
                "result": event.result.value,
                "metadata": {str(k): str(v) for k, v in event.metadata},
            }
            for event in logger.by_resource(str(request.request_id))
        ],
        ensure_ascii=False,
    )
    assert CANARY not in payload
    assert CANARY not in events
    assert "sk-test" not in payload and "sk-test" not in events


def test_i3_el_mensaje_de_error_del_proveedor_se_sanea_en_el_ciclo(tmp_path: Path) -> None:
    """Aunque el router no conozca el cliente, el ciclo sanea antes de publicar el error."""
    router = ProviderRouter()
    router.register_provider("openai", _openai_factory(Capture()))
    router.register_provider(
        "deepseek", _raise_factory(ProviderUnavailableError(f"rechazado con la clave {CANARY}"))
    )
    cycle, _, _ = _cycle(tmp_path, router=router)

    result = cycle.run(_request(requested_role=ProviderRole.BUILDER))

    assert CANARY not in result.error
    assert result.status is BuildRequestStatus.PROVIDER_FAILED


def test_i4_las_instrucciones_internas_no_se_copian_a_la_auditoria(tmp_path: Path) -> None:
    """La auditoría guarda huellas y conteos: el objetivo no se copia literalmente."""
    cycle, _, logger = _cycle(tmp_path)
    request = _request(objective=OBJECTIVE)

    cycle.run(request)

    metadata = _metadata(logger, str(request.request_id), "BUILD_REQUEST_ACCEPTED")
    stored = json.dumps({str(k): str(v) for k, v in metadata.items()}, ensure_ascii=False)
    assert OBJECTIVE not in stored
    assert metadata["objective_sha256"] and metadata["objective_chars"] == len(OBJECTIVE)


# ===========================================================================
# J — el request_id correlaciona el ciclo entero
# ===========================================================================
def test_j1_el_ciclo_entero_se_reconstruye_por_request_id(tmp_path: Path) -> None:
    """Todos los eventos del ciclo comparten recurso y siguen el orden documentado."""
    cycle, _, logger = _cycle(tmp_path)
    request = _request()

    result = cycle.run(request)

    events = logger.by_resource(str(request.request_id))
    assert [event.event_type.value for event in events] == [
        "BUILD_REQUEST_ACCEPTED",
        "BUILD_REQUEST_NORMALIZED",
        "BUILD_PROVIDER_SELECTED",
        "BUILD_PROPOSAL_VALIDATED",
        "BUILD_CYCLE_COMPLETED",
    ]
    assert {event.resource_id for event in events} == {str(request.request_id)}
    assert {event.resource for event in events} == {
        "build_request",
        "build_provider",
        "build_proposal",
        "build_cycle",
    }
    assert result.request_id == request.request_id
    assert logger.by_resource("otro-id") == ()


def test_j2_los_eventos_del_ciclo_llevan_las_cifras_del_resultado(tmp_path: Path) -> None:
    """El desenlace queda auditable sin abrir la salida del proveedor."""
    cycle, _, logger = _cycle(tmp_path)
    request = _request()

    result = cycle.run(request)
    completed = _metadata(logger, str(request.request_id), "BUILD_CYCLE_COMPLETED")

    assert completed["status"] == result.status.value
    assert completed["authority"] == "PROPOSAL_ONLY"
    assert completed["validation_status"] == "VALID"
    assert completed["provider"] == "openai"
    assert completed["trusted_experience"] == len(result.trusted_experience_ids)
    assert completed["duration_ms"] == result.duration_ms
    validated = _metadata(logger, str(request.request_id), "BUILD_PROPOSAL_VALIDATED")
    assert validated["proposal_sha256"] and len(str(validated["proposal_sha256"])) == 64
    assert validated["authority"] == "PROPOSAL_ONLY"


def test_j3_la_huella_de_la_propuesta_corresponde_al_texto_validado(tmp_path: Path) -> None:
    """La huella publicada identifica el texto exacto que PUNTO validó."""
    cycle, _, logger = _cycle(tmp_path)
    request = _request()

    result = cycle.run(request)
    validated = _metadata(logger, str(request.request_id), "BUILD_PROPOSAL_VALIDATED")

    assert validated["proposal_sha256"] == hashlib.sha256(
        (result.proposal or "").encode("utf-8")
    ).hexdigest()
    assert validated["proposal_chars"] == len(result.proposal or "")


# ===========================================================================
# K — el slice completo, de principio a fin
# ===========================================================================
def test_k_el_slice_completo_se_recorre_sin_escribir_en_el_destino(tmp_path: Path) -> None:
    """Intención → normalización → PELL → rol → proveedor → validación → resultado → auditoría."""
    root = _target_dir(tmp_path)
    before = _tree(root)
    trace: list[str] = []
    store = TraceStore(_memory(tmp_path), trace)

    class TracedCapture(Capture):
        """Transporte que anota la invocación real del proveedor."""

        def transport(self, body: Callable[[], dict[str, Any]], *, status: int = 200) -> Any:
            inner = super().transport(body, status=status)

            def _handle(request: httpx.Request) -> httpx.Response:
                trace.append("PROVIDER")
                return inner.handle_request(request)

            return httpx.MockTransport(_handle)

    traced = TracedCapture()
    router = ProviderRouter()
    router.register_provider("openai", _openai_factory(traced))
    cycle, _, logger = _cycle(
        tmp_path, capture=traced, router=router, retriever=MemoryRetriever(store)
    )
    request = _request(
        constraints=("no tocar código de producción",),
        acceptance_criteria=("el contrato queda descrito campo a campo",),
    )

    result = cycle.run(request)

    assert result.status is BuildRequestStatus.ACCEPTED
    assert result.authority == "PROPOSAL_ONLY"
    assert trace == ["PELL", "PELL", "PROVIDER"]
    assert _event_types(logger, str(request.request_id)) == [
        "BUILD_REQUEST_ACCEPTED",
        "BUILD_REQUEST_NORMALIZED",
        "BUILD_PROVIDER_SELECTED",
        "BUILD_PROPOSAL_VALIDATED",
        "BUILD_CYCLE_COMPLETED",
    ]
    assert "no tocar código de producción" in traced.prompt
    assert "el contrato queda descrito campo a campo" in traced.prompt
    assert "punto-inmobiliario-hn" in traced.prompt
    assert _tree(root) == before
    assert result.usage is not None and result.usage.total_tokens > 0


# ===========================================================================
# API — la intención entra por la superficie HTTP
# ===========================================================================
@pytest.fixture
def build_cycle(tmp_path: Path) -> Iterator[BuildCycle]:
    """Ciclo gobernado con un destino temporal y un proveedor controlado."""
    cycle, _, _ = _cycle(tmp_path)
    yield cycle


@pytest.fixture
def build_client(build_cycle: BuildCycle) -> Iterator[TestClient]:
    """Cliente HTTP con el ciclo inyectado: la API solo deserializa, ejecuta y traduce."""
    with TestClient(create_app(environment="test", build_cycle=build_cycle)) as client:
        yield client


def test_api1_los_destinos_registrados_se_pueden_consultar(
    build_client: TestClient, build_cycle: BuildCycle
) -> None:
    """La API publica las claves de destino, no las rutas locales."""
    response = build_client.get("/build-targets")

    assert response.status_code == 200
    body = response.json()
    assert body["authority"] == "PROPOSAL_ONLY"
    assert body["targets"] == [{"target_id": TARGET_ID, "scope_roots": ["docs", "app"]}]
    local_path = str(build_cycle.config.targets[TARGET_ID].repository)
    assert local_path not in json.dumps(body)
    assert "repository" not in json.dumps(body)


def test_api2_una_solicitud_entra_por_http_y_devuelve_la_propuesta(
    build_client: TestClient,
) -> None:
    """``POST /build-requests`` ejecuta el ciclo y publica el resultado normalizado."""
    response = build_client.post(
        "/build-requests",
        json={
            "objective": OBJECTIVE,
            "target_repository": TARGET_ID,
            "requested_role": "ARCHITECT",
            "acceptance_criteria": ["el contrato queda descrito campo a campo"],
            "scope_paths": ["docs/leads.md"],
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "PROPOSAL_ACCEPTED"
    assert body["authority"] == "PROPOSAL_ONLY"
    assert body["applied"] is False
    assert body["audit_resource"] == body["request_id"]
    assert body["proposal"].startswith("PROPUESTA")


def test_api3_el_resultado_se_puede_consultar_y_el_ciclo_se_reconstruye(
    build_client: TestClient,
) -> None:
    """``GET /build-requests/{id}`` y ``/audit/events`` cuentan la misma historia."""
    created = build_client.post(
        "/build-requests",
        json={
            "objective": OBJECTIVE,
            "target_repository": TARGET_ID,
            "requested_role": "ARCHITECT",
            "scope_paths": ["docs/leads.md"],
        },
    ).json()

    fetched = build_client.get(f"/build-requests/{created['request_id']}")
    events = build_client.get(
        "/audit/events", params={"resource_id": created["request_id"]}
    ).json()

    assert fetched.status_code == 200
    assert fetched.json()["request_id"] == created["request_id"]
    assert events["total"] == 5
    assert [item["event_type"] for item in events["items"]] == [
        "BUILD_REQUEST_ACCEPTED",
        "BUILD_REQUEST_NORMALIZED",
        "BUILD_PROVIDER_SELECTED",
        "BUILD_PROPOSAL_VALIDATED",
        "BUILD_CYCLE_COMPLETED",
    ]


def test_api4_un_destino_no_registrado_se_rechaza_con_su_motivo(
    build_client: TestClient,
) -> None:
    """El rechazo de la frontera es explícito: 422, sin proveedor invocado."""
    response = build_client.post(
        "/build-requests",
        json={
            "objective": OBJECTIVE,
            "target_repository": "repositorio-desconocido",
            "requested_role": "ARCHITECT",
        },
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "build_request_rejected"
    assert body["status"] == "REQUEST_REJECTED"
    assert body["provider_invoked"] is False


def test_api5_una_solicitud_invalida_no_llega_al_ciclo(build_client: TestClient) -> None:
    """El esquema corta antes: un objetivo que pide ejecución no se admite."""
    response = build_client.post(
        "/build-requests",
        json={
            "objective": "ejecuta el despliegue en producción",
            "target_repository": TARGET_ID,
            "requested_role": "ARCHITECT",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "value_error"


def test_api6_una_solicitud_desconocida_no_existe(build_client: TestClient) -> None:
    """Consultar un resultado que no se ejecutó no inventa nada."""
    response = build_client.get("/build-requests/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404


# ===========================================================================
# Configuración de destinos
# ===========================================================================
def test_cfg1_sin_configuracion_no_hay_destinos() -> None:
    """Un motor sin destinos declarados arranca: simplemente no acepta trabajo."""
    assert load_build_targets({}) == {}


def test_cfg2_un_destino_valido_se_lee_con_sus_raices(tmp_path: Path) -> None:
    """La configuración declara la clave, la ruta y las raíces admitidas."""
    root = _target_dir(tmp_path)
    payload = json.dumps({"inmobiliario": {"repository": str(root), "scope_roots": ["docs"]}})

    targets = load_build_targets({BUILD_TARGETS_ENV: payload})

    assert set(targets) == {"inmobiliario"}
    assert targets["inmobiliario"].repository == root
    assert targets["inmobiliario"].scope_roots == ("docs",)


@pytest.mark.parametrize(
    "payload",
    [
        "{no es json}",
        "[]",
        '{"x": "no-es-objeto"}',
        '{"x": {"repository": "C:/ruta/que/no/existe/jamas"}}',
        '{"x": {"repository": ".", "scope_roots": "docs"}}',
        '{"x": {"repository": ".", "scope_roots": ["../fuera"]}}',
        '{"": {"repository": "."}}',
        '{"a/b": {"repository": "."}}',
    ],
)
def test_cfg3_una_configuracion_invalida_se_rechaza_sin_silencios(payload: str) -> None:
    """Una configuración a medias no se ignora: se declara el fallo."""
    with pytest.raises(BuildCycleError):
        load_build_targets({BUILD_TARGETS_ENV: payload})


def test_cfg4_demasiados_destinos_se_rechazan(tmp_path: Path) -> None:
    """La lista de destinos es configuración acotada, no un catálogo sin límite."""
    payload = json.dumps(
        {f"destino-{index}": {"repository": str(tmp_path)} for index in range(17)}
    )

    with pytest.raises(BuildCycleError):
        load_build_targets({BUILD_TARGETS_ENV: payload})


# ===========================================================================
# Utilidades de la suite
# ===========================================================================
def _raise_factory(error: BaseException) -> Callable[[str], Any]:
    """Fábrica que falla al construir el cliente (credencial o transporte ausente)."""

    def _build(_model: str) -> Any:
        raise error

    return _build


def _rejecting_router(capture: Capture, *, status: int) -> ProviderRouter:
    """Router cuyo proveedor de ARCHITECT responde con un error HTTP (401/500)."""
    transport = capture.transport(lambda: _openai_body(PROPOSAL), status=status)
    router = ProviderRouter()
    router.register_provider(
        "openai",
        lambda model: OpenAIClient(OpenAIConfig(api_key=CANARY, model=model), transport=transport),
    )
    return router
