"""VISUAL_QA EFECTIVO con OpenAI/Codex: capacidad real, selección efectiva y evidencia.

Cadena que se cubre: requisito de capacidad → selección de proveedor → capacidad **efectiva** del
transporte → VISUAL_QA → evidencia gobernada → verificación.

La **prueba real** (Codex y una captura real de navegador) vive en
``tests/integration/test_codex_vision_live.py`` (se ejecuta aparte, como los demás live gates):
demuestra si el transporte configurado consume imágenes. Aquí, con dobles de proceso y de
proveedor, se fijan las reglas para que no se puedan romper en silencio:

- una capacidad **declarada** pero no efectiva no hace elegible a nadie;
- Codex solo declara imágenes si el binario instalado anuncia ``--image``;
- el veredicto del modelo se valida con un contrato cerrado y nunca degrada hacia PASS;
- la evidencia queda ligada a la Task, con capturas reales (huella) y proveedor/transporte;
- sin ruta visual efectiva el criterio sigue exigiendo evidencia; Claude BUILDER sigue text-only.

    pytest tests/test_visual_qa_effective.py -q
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto.acceptance import (
    SemanticClaim,
    VisualCapability,
    VisualVerdict,
    verify_claims,
)
from punto.api.console import ConsoleDependencies, register_human_console
from punto.api.dashboard import register_dashboard
from punto.audit.logger import AuditLogger
from punto.orchestrator.dev_cycle import DevelopmentConfig, DevelopmentCycle
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers.base import (
    ImageValidationError,
    ModelCompletion,
    MultimodalModelClient,
)
from punto.providers.contract import (
    ModelUsage,
    ProviderRole,
    ProviderStatus,
    make_request,
)
from punto.providers.effective import visual_capability_for_role
from punto.providers.failover import FailoverCause, FailoverPolicy, SubstituteVerdict
from punto.providers.registry import ProviderRegistry
from punto.providers.router import ProviderRouter
from punto.providers.secrets import SECRETS_FILE_ENV
from punto.providers.settings import LOCAL_CONFIG_ENV
from punto.providers.transport import TransportError, TransportErrorKind, TransportProcess
from punto.providers.transports.claude_code import TEXT_ONLY_ARGV, ClaudeCodeTransport
from punto.providers.transports.codex import CodexTransport
from punto.schemas.audit import AuditEventType
from punto.visualqa.dev_evidence import (
    CapturedShot,
    CaptureError,
    HeadlessBrowserCapture,
    assess_visual_claims,
    is_loopback_url,
    parse_verdicts,
)
from punto.workspace.target import DevelopmentTargetRegistry
from test_human_console import TARGET_ID, _cambio, _plan, _repos, _target

#: PNG mínimo válido (la firma basta: nunca se decodifica en las pruebas).
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
CODEX_HELP_CON_IMAGEN = "Usage: codex exec [OPTIONS]\n  -i, --image <FILE>...  attach images\n"
CODEX_HELP_SIN_IMAGEN = "Usage: codex exec [OPTIONS]\n  -m, --model <MODEL>\n"
CRITERIO = "el mapa se integra visualmente con el diseno actual"


# ================================================================== 1 · transporte de Codex
class _RunnerCodex:
    """Runner de Codex: responde ``--help``, sesión y ejecución; registra lo que recibió."""

    def __init__(self, *, help_text: str = CODEX_HELP_CON_IMAGEN, installed: bool = True) -> None:
        self.help_text = help_text
        self.installed = installed
        self.ejecuciones: list[dict[str, Any]] = []
        self.helps = 0

    def _proceso(
        self, argv: Sequence[str], stdout: str = "", exit_code: int = 0
    ) -> TransportProcess:
        if not self.installed:
            return TransportProcess(
                argv=tuple(argv), exit_code=127, not_installed=True, stderr="no instalado"
            )
        return TransportProcess(argv=tuple(argv), exit_code=exit_code, stdout=stdout)

    def run(
        self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None
    ) -> TransportProcess:
        """``--help``, ``--version`` y estado de sesión."""
        arguments = tuple(argv)
        if "--help" in arguments:
            self.helps += 1
            return self._proceso(arguments, self.help_text)
        if "status" in arguments:
            return self._proceso(arguments, "Logged in using ChatGPT")
        return self._proceso(arguments, "codex-cli 0.155.0")

    def run_with_stdin(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin_text: str,
        env: Mapping[str, str] | None = None,
    ) -> TransportProcess:
        """Ejecución: comprueba que las imágenes existen **durante** la llamada."""
        arguments = tuple(argv)
        existentes: dict[str, bytes] = {}
        if "--image" in arguments:
            inicio = arguments.index("--image") + 1
            for ruta in arguments[inicio:]:
                if ruta.startswith("--"):
                    break
                existentes[ruta] = Path(ruta).read_bytes()
        self.ejecuciones.append({"argv": arguments, "stdin": stdin_text, "imagenes": existentes})
        linea = json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}}
        )
        return self._proceso(arguments, linea)


def _peticion_con_imagenes(*imagenes: Any) -> Any:
    from punto.providers.base import ImagePayload

    return make_request(
        ProviderRole.VISUAL_QA,
        "evalúa la captura",
        context="CRITERIA:\n1. algo",
        attachments=tuple(
            ImagePayload(data=data, media_type="image/png", logical_name=f"c{i}")
            for i, data in enumerate(imagenes or (PNG,), start=1)
        ),
    )


def test_1_codex_declara_imagenes_solo_si_el_binario_las_anuncia() -> None:
    """Capacidad comprobada en el binario instalado, no por catálogo ni por modelo."""
    con = CodexTransport(model="gpt-5.6-sol", runner=_RunnerCodex())
    sin = CodexTransport(model="gpt-5.6-sol", runner=_RunnerCodex(help_text=CODEX_HELP_SIN_IMAGEN))
    ausente = CodexTransport(model="gpt-5.6-sol", runner=_RunnerCodex(installed=False))

    assert con.capabilities().supports_images is True
    assert "--image" in con.capabilities().detail
    assert sin.capabilities().supports_images is False
    assert ausente.capabilities().supports_images is False, "sin binario no se afirma nada"


def test_1b_sin_soporte_en_el_binario_las_imagenes_no_se_descartan_en_silencio() -> None:
    """Un Codex sin ``--image`` rechaza la petición con imágenes (nunca la ejecuta sin ellas)."""
    runner = _RunnerCodex(help_text=CODEX_HELP_SIN_IMAGEN)
    transporte = CodexTransport(model="gpt-5.6-sol", runner=runner)

    with pytest.raises(TransportError) as error:
        transporte.execute(_peticion_con_imagenes())

    assert error.value.kind is TransportErrorKind.UNAVAILABLE
    assert runner.ejecuciones == [], "no se ejecutó nada sin las imágenes"


def test_2_las_imagenes_viajan_como_ficheros_temporales_que_pierden_la_ruta_al_terminar() -> None:
    """``--image`` antes de ``--model``, bytes exactos durante la llamada, borrado después."""
    runner = _RunnerCodex()
    transporte = CodexTransport(model="gpt-5.6-sol", runner=runner)
    otra = PNG + b"\x01"

    resultado = transporte.execute(_peticion_con_imagenes(PNG, otra))

    assert resultado.content == "OK"
    (llamada,) = runner.ejecuciones
    argv = llamada["argv"]
    assert argv.index("--image") < argv.index("--model"), "el flag variádico no se traga nada"
    assert "read-only" in argv and "--sandbox" in argv, "el sandbox no se relaja"
    assert sorted(llamada["imagenes"].values()) == sorted([PNG, otra])
    assert "punto-codex-img-" not in llamada["stdin"], "el prompt no lleva rutas"
    for ruta in llamada["imagenes"]:
        assert not Path(ruta).exists(), "los temporales se borran al terminar"


def test_2b_sin_imagenes_la_invocacion_no_cambia() -> None:
    """Regresión: una petición de texto no lleva ``--image``."""
    runner = _RunnerCodex()

    CodexTransport(model="gpt-5.6-sol", runner=runner).execute(
        make_request(ProviderRole.ARCHITECT, "planifica")
    )

    assert "--image" not in runner.ejecuciones[0]["argv"]


@pytest.mark.parametrize(
    "imagenes",
    [pytest.param((b"",), id="vacia"), pytest.param((PNG,) * 9, id="demasiadas")],
)
def test_2c_las_imagenes_se_validan_antes_de_escribir_nada(imagenes: tuple[bytes, ...]) -> None:
    """Los límites del contrato multimodal mandan: una imagen inválida no llega a Codex."""
    runner = _RunnerCodex()

    with pytest.raises(ImageValidationError):
        CodexTransport(model="gpt-5.6-sol", runner=runner).execute(
            _peticion_con_imagenes(*imagenes)
        )

    assert runner.ejecuciones == []


# ============================================= 2 · selección por capacidad EFECTIVA (registro real)
@pytest.fixture
def registro(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., ProviderRegistry]:
    """Registro real con la política de failover del repositorio y sesiones simuladas."""
    monkeypatch.setenv(SECRETS_FILE_ENV, str(tmp_path / "secrets.json"))
    monkeypatch.setenv(LOCAL_CONFIG_ENV, str(tmp_path / "providers.local.yaml"))

    def crear(
        *, codex_imagenes: bool = True, sesiones: dict[str, str] | None = None
    ) -> ProviderRegistry:
        estados = {"anthropic": "AUTHENTICATED", "openai": "AUTHENTICATED", **(sesiones or {})}
        monkeypatch.setattr(
            ProviderRegistry, "auth_status", lambda self, provider: estados.get(provider, "X")
        )
        monkeypatch.setattr(CodexTransport, "accepts_images", lambda self: codex_imagenes)
        config = tmp_path / "cfg"
        config.mkdir(exist_ok=True)
        (config / "providers.yaml").write_text(
            yaml.safe_dump(
                {
                    "roles": {
                        "ARCHITECT": "openai",
                        "BUILDER": "deepseek",
                        "VISUAL_QA": "anthropic",
                    },
                    "failover": {
                        "roles": {"BUILDER": ["anthropic"], "VISUAL_QA": ["openai"]},
                    },
                }
            ),
            encoding="utf-8",
        )
        return ProviderRegistry(config_dir=config)

    return crear


def test_3_vision_declarada_pero_no_efectiva_no_es_elegible(
    registro: Callable[..., ProviderRegistry],
) -> None:
    """Anthropic declara VISION, pero ``claude --print`` es texto: no es elegible."""
    r = registro()
    assert "VISION" in r.descriptor("anthropic").capabilities

    veredicto = r._judge_substitute(ProviderRole.VISUAL_QA, "anthropic", True)

    assert not veredicto.eligible and veredicto.capability_gap
    assert veredicto.transport == "claude_code" and "VISION" in veredicto.reason


def test_3b_una_capacidad_efectiva_sin_declarar_tampoco_basta(
    registro: Callable[..., ProviderRegistry], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Efectiva ∩ declarada: si la configuración no declara VISION, el transporte no la crea."""
    from punto.providers import registry as registry_module

    monkeypatch.setitem(
        registry_module.DEFAULT_CAPABILITIES, "openai", ("TEXT", "CODING", "STRUCTURED_OUTPUT")
    )

    veredicto = registro()._judge_substitute(ProviderRole.VISUAL_QA, "openai", True)

    assert not veredicto.eligible and veredicto.capability_gap


def test_4_codex_con_vision_efectiva_es_la_ruta_de_visual_qa(
    registro: Callable[..., ProviderRegistry],
) -> None:
    """Con imágenes efectivas en Codex, VISUAL_QA se ejecuta por OpenAI/Codex (asignado intacto)."""
    router = registro().router_instance()

    ruta = router.resolve_route(ProviderRole.VISUAL_QA, needs_vision=True)

    assert ruta.available and ruta.via_failover
    assert (ruta.assigned, ruta.provider, ruta.transport) == ("anthropic", "openai", "codex")
    assert router.get_provider_for_role(ProviderRole.VISUAL_QA) == "anthropic"
    capacidad = visual_capability_for_role(router=router)
    assert capacidad.available and capacidad.provider == "openai"
    assert capacidad.transport == "codex" and "anthropic" in capacidad.detail


def test_4b_si_codex_no_ejecuta_imagenes_no_hay_ruta_y_se_falla_cerrado(
    registro: Callable[..., ProviderRegistry],
) -> None:
    """Ningún proveedor visual efectivo: no hay ruta y la capacidad visual sigue en «no»."""
    router = registro(codex_imagenes=False).router_instance()

    ruta = router.resolve_route(ProviderRole.VISUAL_QA, needs_vision=True)

    assert not ruta.available
    assert "anthropic" in ruta.reason and "openai" in ruta.reason
    assert visual_capability_for_role(router=router).available is False


def test_4c_un_codex_sin_sesion_no_es_elegible(
    registro: Callable[..., ProviderRegistry],
) -> None:
    """Conectado se exige de verdad: sin sesión de Codex no hay ruta visual."""
    router = registro(sesiones={"openai": "NOT_AUTHENTICATED"}).router_instance()

    assert not router.resolve_route(ProviderRole.VISUAL_QA, needs_vision=True).available


# ================================================ 3 · el router: sin gastar el primario
class _Multimodal(MultimodalModelClient):
    """Proveedor de prueba con imágenes; registra qué recibió."""

    def __init__(self, provider: str, model: str, respuesta: Callable[[int], str]) -> None:
        self._provider, self._model, self._respuesta = provider, model, respuesta
        self.imagenes: list[tuple[Any, ...]] = []
        self.textos = 0

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    def _completion(self, contenido: str) -> ModelCompletion:
        return ModelCompletion(
            content=contenido,
            model=self._model,
            usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1,
        )

    def complete_json(self, **kwargs: Any) -> ModelCompletion:
        self.textos += 1
        return self._completion(self._respuesta(0))

    def complete_multimodal_json(self, **kwargs: Any) -> ModelCompletion:
        self.imagenes.append(tuple(kwargs["images"]))
        return self._completion(self._respuesta(len(kwargs["images"])))

    def redact(self, text: str) -> str:
        return text

    def close(self) -> None:
        return None


def _evaluador_visual(role: ProviderRole, provider: str, needs_vision: bool) -> SubstituteVerdict:
    """Anthropic (texto) sin imágenes; OpenAI (Codex) con imágenes efectivas."""
    if provider == "anthropic" and needs_vision:
        return SubstituteVerdict(
            False, "sin capacidad efectiva VISION", capability_gap=True, transport="claude_code"
        )
    transporte = "codex" if provider == "openai" else "claude_code"
    return SubstituteVerdict(True, transport=transporte)


def _router_visual(
    claude: _Multimodal, gpt: _Multimodal, *, politica: FailoverPolicy | None = None
) -> ProviderRouter:
    router = ProviderRouter()
    for cliente in (claude, gpt):
        router.register_provider(cliente.provider, lambda _m, c=cliente: c, model=cliente.model)
    router.register_provider(
        "deepseek", lambda _m: _Multimodal("deepseek", "ds", lambda n: "{}"), model="ds"
    )
    router.configure_failover(
        politica
        or FailoverPolicy(
            roles={ProviderRole.BUILDER: ("anthropic",), ProviderRole.VISUAL_QA: ("openai",)}
        ),
        _evaluador_visual,
    )
    return router


def _veredicto_json(n: int) -> str:
    return json.dumps({"verdicts": [{"claim": 1, "verdict": "PASS", "observation": "se ve"}]})


def test_5_una_peticion_con_imagenes_va_directa_al_proveedor_visual_efectivo() -> None:
    """Sin gastar el primario de solo texto: el registro dice ``CAPABILITY_MISSING``."""
    claude = _Multimodal("anthropic", "claude-sonnet-5", _veredicto_json)
    gpt = _Multimodal("openai", "gpt-5.6-sol", _veredicto_json)
    router = _router_visual(claude, gpt)
    peticion = _peticion_con_imagenes(PNG)

    resultado = router.execute(ProviderRole.VISUAL_QA, peticion)

    assert resultado.ok and resultado.provider == "openai"
    assert claude.imagenes == [] and claude.textos == 0, "el primario de texto no se ejecutó"
    assert len(gpt.imagenes) == 1 and len(gpt.imagenes[0]) == 1
    (registro,) = resultado.failovers
    assert registro.cause == FailoverCause.CAPABILITY_MISSING.value
    assert registro.primary_error_kind == "CAPABILITY_GAP"
    assert (registro.primary_provider, registro.substitute_provider) == ("anthropic", "openai")


def test_5b_sin_imagenes_el_asignado_responde_como_siempre() -> None:
    """La preselección solo aplica a peticiones con imágenes."""
    claude = _Multimodal("anthropic", "claude-sonnet-5", lambda n: "{}")
    gpt = _Multimodal("openai", "gpt-5.6-sol", _veredicto_json)
    router = _router_visual(claude, gpt)

    resultado = router.execute(ProviderRole.VISUAL_QA, make_request(ProviderRole.VISUAL_QA, "hola"))

    assert resultado.provider == "anthropic" and resultado.failovers == ()
    assert gpt.textos == 0


def test_5c_sin_proveedor_visual_efectivo_falla_cerrado() -> None:
    """Nadie con imágenes efectivas: fallo cerrado, ningún proveedor recibe la imagen."""
    claude = _Multimodal("anthropic", "claude-sonnet-5", _veredicto_json)
    gpt = _Multimodal("openai", "gpt-5.6-sol", _veredicto_json)
    router = _router_visual(claude, gpt)
    router.configure_failover(
        FailoverPolicy(roles={ProviderRole.VISUAL_QA: ("openai",)}),
        lambda role, provider, vision: SubstituteVerdict(
            False, "sin capacidad efectiva VISION", capability_gap=True
        ),
    )

    resultado = router.execute(ProviderRole.VISUAL_QA, _peticion_con_imagenes(PNG))

    assert not resultado.ok and resultado.status is ProviderStatus.UNAVAILABLE
    assert claude.imagenes == [] and gpt.imagenes == []
    assert "ningún sustituto compatible" in resultado.error


def test_6_claude_builder_sigue_text_only_y_el_failover_visual_no_le_da_nada() -> None:
    """La ruta visual es de VISUAL_QA: Claude como BUILDER sigue sin herramientas ni imágenes."""
    argv = ClaudeCodeTransport(model="claude-sonnet-5").prompt_argv()
    assert argv[argv.index("--tools") : argv.index("--tools") + 3] == TEXT_ONLY_ARGV
    assert ClaudeCodeTransport(model="x").capabilities().supports_images is False

    claude = _Multimodal("anthropic", "claude-sonnet-5", _veredicto_json)
    router = _router_visual(claude, _Multimodal("openai", "gpt", _veredicto_json))
    ruta_builder = router.resolve_route(ProviderRole.BUILDER, needs_vision=False)
    assert ruta_builder.provider == "deepseek", "la asignación de BUILDER no cambia"
    # Una política de VISUAL_QA no cubre otros roles (ni amplía nada para ellos).
    assert not router.failover_policy().covers(ProviderRole.ARCHITECT)  # type: ignore[union-attr]


# ======================================================== 4 · el veredicto: contrato cerrado
def test_7_el_veredicto_valido_se_interpreta_por_criterio() -> None:
    """Un veredicto por criterio, acotado, con su observación."""
    contenido = json.dumps(
        {
            "verdicts": [
                {"claim": 1, "verdict": "pass", "observation": "se ve " + "x" * 900},
                {"claim": 2, "verdict": "FAIL", "observation": "no aparece"},
            ]
        }
    )

    uno, dos = parse_verdicts(contenido, claims=2)

    assert (uno.verdict, dos.verdict) == ("PASS", "FAIL")
    assert len(uno.observation) <= 300


@pytest.mark.parametrize(
    "contenido",
    [
        pytest.param("no es json", id="ilegible"),
        pytest.param("", id="vacio"),
        pytest.param('{"verdicts": "PASS"}', id="forma-rota"),
        pytest.param(
            '{"verdicts": [{"claim": 1, "verdict": "APROBADO"}]}', id="veredicto-desconocido"
        ),
        pytest.param('{"verdicts": [{"claim": 9, "verdict": "PASS"}]}', id="fuera-de-rango"),
        pytest.param(
            '{"verdicts": [{"claim": true, "verdict": "PASS"}]}', id="booleano-no-es-numero"
        ),
        pytest.param(
            '{"verdicts": [{"claim": 1, "verdict": "PASS"}, {"claim": 1, "verdict": "FAIL"}]}',
            id="duplicado",
        ),
    ],
)
def test_7b_nada_dudoso_llega_como_pass(contenido: str) -> None:
    """Salida ilegible, criterio sin veredicto, desconocido, duplicado o fuera de rango: UNCLEAR."""
    (unico,) = parse_verdicts(contenido, claims=1)

    assert unico.verdict == "UNCLEAR"


def test_7c_un_criterio_sin_entrada_queda_unclear_aunque_otro_pase() -> None:
    """Un PASS no arrastra a los criterios que el modelo no juzgó."""
    uno, dos = parse_verdicts('{"verdicts": [{"claim": 1, "verdict": "PASS"}]}', claims=2)

    assert (uno.verdict, dos.verdict) == ("PASS", "UNCLEAR")


# ============================================================ 5 · captura: solo bucle local
def test_8_solo_se_capturan_urls_de_bucle_local() -> None:
    """El ciclo no navega a Internet."""
    assert is_loopback_url("http://localhost:3000/propiedades")
    assert is_loopback_url("http://127.0.0.1:8000/")
    assert not is_loopback_url("https://ejemplo.com/")
    assert not is_loopback_url("http://localhost.evil.com/")
    assert not is_loopback_url("file:///etc/passwd")
    with pytest.raises(CaptureError, match="bucle local"):
        HeadlessBrowserCapture(browser=__file__).capture(("https://ejemplo.com/",), (800, 600))


def test_8b_sin_navegador_o_sin_rutas_no_se_inventa_una_captura(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallo explícito, nunca una captura fabricada."""
    monkeypatch.setattr("punto.visualqa.dev_evidence._BROWSER_PATHS", ())
    monkeypatch.setattr("punto.visualqa.dev_evidence._BROWSER_NAMES", ())
    monkeypatch.delenv("PUNTO_BROWSER", raising=False)

    with pytest.raises(CaptureError, match="navegador"):
        HeadlessBrowserCapture().capture(("http://localhost:3000/",), (800, 600))
    with pytest.raises(CaptureError, match="rutas visuales"):
        HeadlessBrowserCapture(browser=__file__).capture((), (800, 600))


# ================================================ 6 · la verificación de criterios (acceptance)
def _reclamo() -> SemanticClaim:
    return SemanticClaim(
        sentence=CRITERIO, kind="VISUAL_APPEARANCE", evidence_required="imagen", capability="VISION"
    )


def _verdict(valor: str, obs: str = "se ve") -> VisualVerdict:
    return VisualVerdict(
        verdict=valor,
        observation=obs,
        provider="openai",
        model="gpt-5.6-sol",
        transport="codex",
        screenshots=("http://localhost:3000/ 0744bcffbe0e",),
    )


@pytest.mark.parametrize(
    ("valor", "resultado"),
    [("PASS", "SATISFIED"), ("FAIL", "UNSATISFIED"), ("UNCLEAR", "NOT_VERIFIED")],
)
def test_9_el_veredicto_determina_el_resultado_del_criterio(valor: str, resultado: str) -> None:
    """PASS satisface, FAIL es reparable y UNCLEAR sigue exigiendo evidencia."""
    capacidad = VisualCapability(available=True, detail="ruta efectiva", provider="openai")

    (registro,) = verify_claims(
        [_reclamo()], visual=capacidad, visual_verdicts={CRITERIO: _verdict(valor)}
    )

    assert registro.result == resultado
    assert "openai/gpt-5.6-sol (codex)" in registro.evidence, "queda quién y por dónde"


def test_9b_sin_veredicto_o_sin_capacidad_el_criterio_sigue_sin_verificar() -> None:
    """Ni capacidad sin imagen ni veredicto sin capacidad efectiva satisfacen nada."""
    con = VisualCapability(available=True, detail="ok")
    sin = VisualCapability(available=False, detail="claude --print es texto")

    (a,) = verify_claims([_reclamo()], visual=con)
    (b,) = verify_claims([_reclamo()], visual=sin, visual_verdicts={CRITERIO: _verdict("PASS")})

    assert a.result == "NOT_VERIFIED" and "no se aportó ninguna" in a.evidence
    assert b.result == "NOT_VERIFIED", "un veredicto sin capacidad efectiva no cuenta"


# =============================================================== 7 · evaluación (sin ruta efectiva)
class _RouterSinRuta:
    """Router de prueba sin ruta visual efectiva: nada debe ejecutarse."""

    def __init__(self) -> None:
        self.ejecuciones = 0

    def resolve_route(self, role: ProviderRole, *, needs_vision: bool = False) -> Any:
        from punto.providers.failover import RouteChoice

        return RouteChoice(assigned="anthropic", reason="anthropic: sin capacidad efectiva VISION")

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.ejecuciones += 1
        raise AssertionError("no debe ejecutarse sin ruta efectiva")


def test_10_sin_ruta_visual_efectiva_no_se_llama_a_nadie() -> None:
    """La evaluación no se simula: sin ruta, el resultado lo dice y el criterio sigue exigiendo."""
    router = _RouterSinRuta()
    shot = CapturedShot(url="http://localhost:3000/", viewport=(800, 600), data=PNG)

    evaluacion = assess_visual_claims(router, [CRITERIO], [shot], request_id="t")

    assert not evaluacion.performed and "sin ruta visual efectiva" in evaluacion.error
    assert router.ejecuciones == 0


# ============================================ 8 · ciclo + consola: evidencia ligada a la Task
class _CapturaFalsa:
    """Captor de prueba: devuelve una captura fija (o falla) y registra sus llamadas."""

    def __init__(self, *, falla: bool = False) -> None:
        self.falla = falla
        self.llamadas: list[tuple[tuple[str, ...], tuple[int, int]]] = []
        self.shot = CapturedShot(url="http://localhost:3000/mapa", viewport=(1280, 800), data=PNG)

    def capture(self, urls: Sequence[str], viewport: tuple[int, int]) -> tuple[CapturedShot, ...]:
        self.llamadas.append((tuple(urls), viewport))
        if self.falla:
            raise CaptureError("el navegador no produjo una captura válida")
        return (self.shot,)


def _consola_visual(
    tmp_path: Path,
    *,
    veredicto: str,
    captura: _CapturaFalsa | None,
    visual_efectiva: bool = True,
    con_rutas: bool = True,
) -> tuple[TestClient, AuditLogger, _Multimodal, _CapturaFalsa | None]:
    """Consola real con ciclo real, repositorio Git real y VISUAL_QA por OpenAI/Codex (doble)."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    if con_rutas:
        target = replace(target, visual_routes=("http://localhost:3000/mapa",))
    audit = AuditLogger()

    def gpt_responde(imagenes: int) -> str:
        if imagenes:
            return json.dumps(
                {"verdicts": [{"claim": 1, "verdict": veredicto, "observation": "así se ve"}]}
            )
        return json.dumps(_plan())

    gpt = _Multimodal("openai", "gpt-5.6-sol", gpt_responde)
    claude = _Multimodal("anthropic", "claude-sonnet-5", lambda n: "{}")
    deepseek = _Multimodal("deepseek", "deepseek-v4-pro", lambda n: json.dumps(_cambio()))
    router = ProviderRouter()
    for cliente in (gpt, claude, deepseek):
        router.register_provider(cliente.provider, lambda _m, c=cliente: c, model=cliente.model)
    router.configure_failover(
        FailoverPolicy(
            roles={ProviderRole.BUILDER: ("anthropic",), ProviderRole.VISUAL_QA: ("openai",)}
        ),
        _evaluador_visual
        if visual_efectiva
        else lambda role, provider, vision: SubstituteVerdict(
            provider != "openai" or not vision,
            "sin capacidad efectiva VISION",
            capability_gap=vision,
        ),
    )
    ciclo = DevelopmentCycle(
        router=router,
        targets=DevelopmentTargetRegistry({TARGET_ID: target}),
        config=DevelopmentConfig(max_repair_rounds=2, max_structural_corrections=2),
        audit=audit,
        policy_engine=PolicyEngine.from_config(),
        visual_capture=captura,
    )
    dependencies = ConsoleDependencies(
        dev_cycle=ciclo,
        gates=HumanGate(),
        audit=audit,
        policy=PolicyEngine.from_config(),
        targets={TARGET_ID: target},
        run_inline=True,
        environ={},
    )
    application = FastAPI()
    register_dashboard(application)
    register_human_console(application, dependencies)
    return TestClient(application), audit, gpt, captura


SOLICITUD = {
    "objective": "unificar la lista de tipos en una sola fuente",
    "target_id": TARGET_ID,
    "acceptance_criteria": [CRITERIO],
    "scope_paths": ["src"],
}


def test_11_un_pass_de_visual_qa_satisface_el_criterio_y_deja_evidencia_ligada_a_la_task(
    tmp_path: Path,
) -> None:
    """Captura real → Codex (efectivo) → PASS → criterio satisfecho → desarrollo completado."""
    client, audit, gpt, captura = _consola_visual(
        tmp_path, veredicto="PASS", captura=_CapturaFalsa()
    )

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea["development"]
    desarrollo = tarea["development"]
    assert desarrollo["claims_result"] == "SATISFIED"
    (evidencia,) = desarrollo["visual_evidence"]
    assert evidencia["request_id"] == tarea["task_id"], "ligada a la identidad de la Task"
    assert (evidencia["provider"], evidencia["transport"]) == ("openai", "codex")
    assert evidencia["model"] == "gpt-5.6-sol" and evidencia["via_failover"] is True
    assert evidencia["verdict"] == "PASS" and evidencia["applied_digest"]
    (foto,) = evidencia["screenshots"]
    assert foto["sha256"] == hashlib.sha256(PNG).hexdigest() and foto["size_bytes"] == len(PNG)
    assert foto["url"] == "http://localhost:3000/mapa" and foto["viewport"] == "1280x800"
    # El intento de la Task lo refleja, y la auditoría también.
    assert tarea["attempts"][0]["visual"] == "openai/codex: PASS=1"
    assert audit.by_type(AuditEventType.DEV_VISUAL_CAPTURED)
    (evaluado,) = audit.by_type(AuditEventType.DEV_VISUAL_ASSESSED)
    assert dict(evaluado.metadata)["provider"] == "openai"
    # La imagen que vio el proveedor es exactamente la capturada.
    assert gpt.imagenes and gpt.imagenes[0][0].data == PNG
    assert captura is not None and captura.llamadas == [
        (("http://localhost:3000/mapa",), (1280, 800))
    ]


def test_11b_la_evidencia_sobrevive_al_reinicio_con_la_task(tmp_path: Path) -> None:
    """La evidencia visual es parte del resultado durable, no un adorno de la sesión."""
    client, _audit, _gpt, _c = _consola_visual(tmp_path, veredicto="PASS", captura=_CapturaFalsa())
    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    otro, _a, _g, _c2 = _consola_visual(tmp_path / "b", veredicto="PASS", captura=None)
    recuperada = otro.get(f"/console/tasks/{tarea['task_id']}").json()

    assert recuperada["recovered"] is True
    assert recuperada["development"]["visual_evidence"][0]["verdict"] == "PASS"
    assert recuperada["attempts"][0]["visual"] == "openai/codex: PASS=1"


def test_12_unclear_deja_el_criterio_exigiendo_evidencia(tmp_path: Path) -> None:
    """Un revisor que no puede decidir (p. ej. interacción) no satisface el criterio."""
    client, _audit, _gpt, _c = _consola_visual(
        tmp_path, veredicto="UNCLEAR", captura=_CapturaFalsa()
    )

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    desarrollo = tarea["development"]
    assert desarrollo["error_kind"] == "EVIDENCE_REQUIRED"
    assert desarrollo["claims_result"] == "EVIDENCE_REQUIRED"
    assert desarrollo["visual_evidence"][0]["verdict"] == "UNCLEAR"
    assert tarea["stage"] == "WAITING_HUMAN" and len(tarea["gates"]) == 1


def test_12b_un_fail_es_un_criterio_no_satisfecho_no_un_pass(tmp_path: Path) -> None:
    """FAIL nunca completa el desarrollo."""
    client, _a, _g, _c = _consola_visual(tmp_path, veredicto="FAIL", captura=_CapturaFalsa())

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["stage"] != "DEVELOPMENT_COMPLETED"
    assert tarea["development"]["visual_evidence"][0]["verdict"] == "FAIL"


def test_13_sin_proveedor_visual_efectivo_sigue_siendo_evidence_required(tmp_path: Path) -> None:
    """Ni captura ni evaluación: el proveedor visual no recibe nada; el criterio exige evidencia."""
    captura = _CapturaFalsa()
    client, _a, gpt, _c = _consola_visual(
        tmp_path, veredicto="PASS", captura=captura, visual_efectiva=False
    )

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["development"]["error_kind"] == "EVIDENCE_REQUIRED"
    assert tarea["development"]["visual_evidence"] == []
    assert captura.llamadas == [] and gpt.imagenes == []
    assert tarea["stage"] == "WAITING_HUMAN"


@pytest.mark.parametrize(
    "escenario",
    [
        pytest.param({"captura": None}, id="sin-captor"),
        pytest.param({"captura": _CapturaFalsa(falla=True)}, id="captura-falla"),
        pytest.param({"captura": _CapturaFalsa(), "con_rutas": False}, id="sin-rutas-declaradas"),
    ],
)
def test_13b_sin_captura_real_no_hay_evidencia_ni_pass(
    tmp_path: Path, escenario: dict[str, Any]
) -> None:
    """Sin captura real (o si falla) no se inventa evidencia: el criterio exige evidencia."""
    client, _a, gpt, _c = _consola_visual(tmp_path, veredicto="PASS", **escenario)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["development"]["error_kind"] == "EVIDENCE_REQUIRED"
    assert tarea["development"]["visual_evidence"] == []
    assert gpt.imagenes == [], "sin captura no se le entrega nada al proveedor"


def test_14_sin_ampliacion_de_autoridad_la_evidencia_no_aprueba_gates_ni_publica(
    tmp_path: Path,
) -> None:
    """Un PASS visual completa el desarrollo local; no resuelve gates ni publica nada."""
    client, _a, _g, _c = _consola_visual(tmp_path, veredicto="PASS", captura=_CapturaFalsa())

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["development"]["published"] is False
    assert tarea["development"]["commit_sha"], "solo commit local del ciclo"
    assert tarea["gates"] == [] and not tarea.get("publication_stage")
    assert client.get("/console/human-gates").json()["pending"] == 0
