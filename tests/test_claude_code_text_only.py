"""Claude Code como proveedor de PUNTO: solo texto, sin herramientas, prompt íntegro.

Cierra el riesgo detectado al habilitar el failover de BUILDER hacia Claude (PROVIDER FAILOVER v0).
Medido con la CLI real (Claude Code 2.1.263, Windows), con los flags anteriores (``--print``):

- el cliente cargaba **37 herramientas** (``Bash``, ``Write``, ``Edit``, ``Read``... y 8 MCP de
  escritura del conector de la cuenta); ``Write`` se intentaba y solo lo frenaba el permiso por
  defecto, no PUNTO; ``Read`` funcionaba **sin pedir permiso** sobre el cwd del motor;
- con ``--tools ""`` solo, seguían cargados los 8 MCP; con ``--tools "" --strict-mcp-config`` el
  evento ``init`` declara **0** herramientas;
- ``--tools`` es variádico: si el prompt va como último argumento se lo traga (``Input must be
  provided``); y en Windows el shim ``claude.CMD`` corta el prompt en el primer salto de línea, así
  que el cliente solo veía su primera línea. Por ``stdin`` llega íntegro.

Estas pruebas fijan la forma de la invocación con un runner de prueba (no llaman a nadie). La
verificación contra la CLI real se hizo a mano y consta en el informe de cierre.

    pytest tests/test_claude_code_text_only.py -q
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import pytest

from punto.providers.contract import ProviderRole, make_request
from punto.providers.transport import TransportProcess
from punto.providers.transports.claude_code import TEXT_ONLY_ARGV, ClaudeCodeTransport

#: Prompt realista: multilínea, con comillas, ``%VARIABLE%`` y caracteres que un shell reparsearía.
PROMPT_CONTEXT = (
    'TARGET: destino\nOBJECTIVE: "unificar tipos" %PATH% & echo hola\nDELIVERABLE: JSON'
)

#: Flags que darían al cliente capacidad propia: ninguno puede aparecer jamás en la invocación.
FLAGS_PROHIBIDOS = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--allowedTools",
    "--allowed-tools",
    "--mcp-config",
    "--add-dir",
    "--permission-mode",
    "--chrome",
)

_RESULTADO = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "{}"})
_SESION = json.dumps({"loggedIn": True, "authMethod": "oauth"})


class _RunnerStdin:
    """Runner que sabe entregar por ``stdin`` (como el real) y registra cada llamada."""

    def __init__(self) -> None:
        self.llamadas: list[dict[str, object]] = []

    def run(
        self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None
    ) -> TransportProcess:
        """Versión y sesión; una ejecución por este camino se registra como ``argv``."""
        arguments = tuple(argv)
        if "--version" in arguments:
            return TransportProcess(argv=arguments, exit_code=0, stdout="2.1.263 (Claude Code)")
        if "status" in arguments:
            return TransportProcess(argv=arguments, exit_code=0, stdout=_SESION)
        self.llamadas.append({"via": "argv", "argv": arguments})
        return TransportProcess(argv=arguments, exit_code=0, stdout=_RESULTADO)

    def run_with_stdin(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin_text: str,
        env: Mapping[str, str] | None = None,
    ) -> TransportProcess:
        """Ejecución con el prompt por ``stdin``."""
        arguments = tuple(argv)
        self.llamadas.append({"via": "stdin", "argv": arguments, "payload": stdin_text})
        return TransportProcess(argv=arguments, exit_code=0, stdout=_RESULTADO)


class _RunnerSoloArgv:
    """Runner sin ``stdin`` (dobles antiguos): el prompt cae al último argumento."""

    def __init__(self) -> None:
        self.ejecuciones: list[tuple[str, ...]] = []

    def run(
        self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None
    ) -> TransportProcess:
        """Versión, sesión y ejecución."""
        arguments = tuple(argv)
        if "--version" in arguments:
            return TransportProcess(argv=arguments, exit_code=0, stdout="2.1.263 (Claude Code)")
        if "status" in arguments:
            return TransportProcess(argv=arguments, exit_code=0, stdout=_SESION)
        self.ejecuciones.append(arguments)
        return TransportProcess(argv=arguments, exit_code=0, stdout=_RESULTADO)


def _pedir(role: ProviderRole = ProviderRole.BUILDER):  # type: ignore[no-untyped-def]
    """Petición normalizada con instrucciones y contexto multilínea."""
    return make_request(
        role, "Eres el BUILDER.\nDevuelve solo JSON.", context=PROMPT_CONTEXT, request_id="r-1"
    )


# ------------------------------------------------------------------ 1 · sin herramientas
def test_1_toda_ejecucion_desactiva_las_herramientas_integradas_y_las_mcp() -> None:
    """``--tools ""`` + ``--strict-mcp-config``: con ambos el cliente declara cero herramientas."""
    argv = ClaudeCodeTransport(model="claude-sonnet-5", runner=_RunnerStdin()).prompt_argv()

    assert TEXT_ONLY_ARGV == ("--tools", "", "--strict-mcp-config")
    inicio = argv.index("--tools")
    assert argv[inicio : inicio + len(TEXT_ONLY_ARGV)] == TEXT_ONLY_ARGV
    assert argv[inicio + 1] == "", "lista vacía = ninguna herramienta integrada"
    assert "--mcp-config" not in argv, "sin --mcp-config, strict deja cero servidores MCP"


def test_2_ninguna_ejecucion_concede_capacidad_propia_al_cliente() -> None:
    """La invocación no lleva ningún flag que amplíe permisos, directorios o integraciones."""
    runner = _RunnerStdin()
    transport = ClaudeCodeTransport(model="claude-sonnet-5", runner=runner)

    transport.execute(_pedir())
    transport.execute(_pedir(ProviderRole.VISUAL_QA))

    ejecuciones = [call["argv"] for call in runner.llamadas]
    assert len(ejecuciones) == 2
    for argv in ejecuciones:
        assert isinstance(argv, tuple)
        for prohibido in FLAGS_PROHIBIDOS:
            assert prohibido not in argv, prohibido
        assert argv[argv.index("--tools") + 1] == ""


# ------------------------------------------------------------------- 2 · prompt íntegro
def test_3_el_prompt_viaja_integro_por_stdin_y_no_por_argv() -> None:
    """Multilínea, comillas y ``%VAR%`` llegan tal cual; el ``argv`` no lleva el prompt."""
    runner = _RunnerStdin()
    transport = ClaudeCodeTransport(model="claude-sonnet-5", runner=runner)

    transport.execute(_pedir())

    (llamada,) = runner.llamadas
    assert llamada["via"] == "stdin"
    payload = str(llamada["payload"])
    assert payload == "Eres el BUILDER.\nDevuelve solo JSON.\n\n" + PROMPT_CONTEXT
    assert "\n" in payload and "%PATH%" in payload
    argv = llamada["argv"]
    assert isinstance(argv, tuple)
    assert all("BUILDER" not in item and "DELIVERABLE" not in item for item in argv)


def test_4_sin_stdin_el_prompt_cae_al_ultimo_argumento_sin_que_tools_se_lo_trague() -> None:
    """``--tools`` es variádico: ``--strict-mcp-config`` lo cierra y el prompt sigue intacto."""
    runner = _RunnerSoloArgv()
    transport = ClaudeCodeTransport(model="claude-sonnet-5", runner=runner)

    transport.execute(_pedir())

    (argv,) = runner.ejecuciones
    assert argv[-1].endswith(PROMPT_CONTEXT), "el prompt es el último argumento posicional"
    assert argv.index("--strict-mcp-config") == argv.index("--tools") + 2
    assert argv.index("--model") > argv.index("--strict-mcp-config")


def test_5_el_transporte_declara_stdin_como_via_del_prompt() -> None:
    """Misma solución y mismo motivo que Codex: el shim ``.cmd`` de Windows corta en el ``\\n``."""
    assert ClaudeCodeTransport.prompt_via_stdin is True


# --------------------------------------------------------------- 3 · el resto no se degrada
@pytest.mark.parametrize("flag", ["--print", "--output-format", "--model"])
def test_6_se_conservan_los_flags_de_la_ejecucion_no_interactiva(flag: str) -> None:
    """El cierre es aditivo: la ejecución no interactiva, el JSON y el modelo siguen igual."""
    argv = ClaudeCodeTransport(model="claude-sonnet-5", runner=_RunnerStdin()).prompt_argv()

    assert flag in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
