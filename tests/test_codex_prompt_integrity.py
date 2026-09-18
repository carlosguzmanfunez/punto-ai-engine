"""PILOT-01R · R1 — integridad del prompt de Codex (defecto de Windows, medido y corregido).

Evidencia del defecto: en Windows, un cliente oficial instalado por npm es un ``.cmd``; al lanzarlo
Windows interpone ``cmd.exe``, que **reparsea la línea de comandos** y corta cualquier argumento en
el primer salto de línea. Se reproduce aquí de forma determinista con shims ``.cmd`` propios, sin
llamar a ningún modelo, y se prueba la corrección: el prompt viaja por ``stdin``.

    pytest tests/test_codex_prompt_integrity.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from punto.providers.contract import (
    ProviderHealth,
    ProviderHealthStatus,
    ProviderRequest,
    ProviderResult,
)
from punto.providers.transport import (
    RealSubprocessRunner,
    TransportAuthMode,
    TransportAuthStatus,
    TransportKind,
    TransportProcess,
)
from punto.providers.transports.cli import CliTransport
from punto.providers.transports.codex import CodexTransport

#: Payloads que deben llegar íntegros: los seis casos exigidos por el contrato de la fase.
PAYLOADS: dict[str, str] = {
    "single_line": "Responde solo con: PUNTO_R1_OK",
    "multiline": "linea 1: ALFA\nlinea 2: BETA\nlinea 3: GAMMA",
    "multiline_json": '{\n  "objetivo": "PILOT-01R",\n  "lineas": ["a", "b"],\n  "ok": true\n}',
    "multiline_quotes": "dijo 'esto' y luego \"aquello\"\ny cerro con 'otra'",
    "multiline_unicode": (
        "canon, nino, arbol, corazon, ¿que?\nsegunda linea con acentos: ñ á é í ó ú"
    ),
    "shell_metacharacters": (
        "prueba & echo INJECTED > inyectado.txt\n"
        "otra ^| mas %PATH% y $(quien) y `backticks`\n"
        "fin ; rm -rf / --no-preserve-root"
    ),
}


def _shim_argv(directorio: Path) -> Path:
    """Shim ``.cmd`` que escribe lo recibido como argumento (camino con reparseo de cmd.exe)."""
    shim = directorio / "argv_probe.cmd"
    shim.write_text("@echo off\r\necho %*\r\n", encoding="utf-8")
    return shim


def _shim_stdin(directorio: Path) -> Path:
    """Shim ``.cmd`` que devuelve su entrada estándar tal cual (camino sin reparseo)."""
    shim = directorio / "stdin_probe.cmd"
    shim.write_text(
        "@echo off\r\n"
        f'"{sys.executable}" -c "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"\r\n',
        encoding="utf-8",
    )
    return shim


class _ShimTransport(CliTransport):
    """Transporte mínimo sobre un shim: ejercita ``run_prompt`` con el runner real."""

    prompt_via_stdin = True

    def __init__(self, shim: Path) -> None:
        super().__init__(model="modelo-de-prueba", binary=str(shim))

    @property
    def kind(self) -> TransportKind:
        """Transporte de prueba: reutiliza el de Codex."""
        return TransportKind.CODEX

    @property
    def auth_mode(self) -> TransportAuthMode:
        """Modo de prueba."""
        return TransportAuthMode.CHATGPT

    @property
    def provider(self) -> str:
        """Proveedor de prueba."""
        return "openai"

    def prompt_argv(self) -> tuple[str, ...]:
        """``argv`` sin el prompt: el shim lo recibe por ``stdin``."""
        return (self.binary,)

    def _interpret_auth(self, process: TransportProcess) -> TransportAuthStatus:
        """El shim no declara sesión; a efectos de la prueba está autenticado."""
        return TransportAuthStatus.AUTHENTICATED

    def health_check(self) -> ProviderHealth:
        """El shim no tiene sonda: se declara no disponible."""
        return ProviderHealth(
            provider=self.provider, status=ProviderHealthStatus.UNAVAILABLE, model=self.model
        )

    def execute(self, request: ProviderRequest, **kwargs: object) -> ProviderResult:
        """El shim no ejecuta peticiones del contrato."""
        raise NotImplementedError("el transporte de prueba no ejecuta peticiones")


class _RecordingStdinRunner:
    """Runner con entrega por ``stdin`` que registra lo que recibió."""

    def __init__(self) -> None:
        self.llamadas: list[dict[str, object]] = []

    def run(self, argv, *, timeout, env=None):
        """Camino de argumentos: no debería usarse con este transporte."""
        self.llamadas.append({"via": "argv", "argv": tuple(argv)})
        return TransportProcess(argv=tuple(argv), exit_code=0, stdout="")

    def run_with_stdin(self, argv, *, timeout, stdin_text, env=None):
        """Camino de ``stdin``: registra el payload y devuelve un proceso correcto."""
        self.llamadas.append({"via": "stdin", "argv": tuple(argv), "payload": stdin_text})
        return TransportProcess(argv=tuple(argv), exit_code=0, stdout=stdin_text)


class _RecordingArgvRunner:
    """Runner que sólo sabe entregar por argumentos (compatibilidad con dobles existentes)."""

    def __init__(self) -> None:
        self.argv: tuple[str, ...] = ()

    def run(self, argv, *, timeout, env=None):
        """Registra el ``argv`` completo, prompt incluido."""
        self.argv = tuple(argv)
        return TransportProcess(argv=self.argv, exit_code=0, stdout="{}")


# ---------------------------------------------------------------------------
# Causa raíz (mecanismo)
# ---------------------------------------------------------------------------
def test_el_shim_cmd_trunca_un_argumento_multilinea(tmp_path: Path) -> None:
    """Un ``.cmd`` recibido por argumentos pierde todo lo que sigue al primer salto de línea.

    Es la causa raíz medida: no es el quoting de Python ni de PUNTO, es ``cmd.exe`` reparseando la
    línea de comandos del shim de npm. Si alguien volviera a entregar el prompt por ``argv``, esta
    prueba documenta lo que pasaría.
    """
    shim = _shim_argv(tmp_path)
    payload = PAYLOADS["multiline"]

    process = RealSubprocessRunner().run((str(shim), payload), timeout=30.0)

    assert process.exit_code == 0
    assert "ALFA" in process.stdout
    assert "BETA" not in process.stdout and "GAMMA" not in process.stdout, (
        "el shim .cmd sólo debe conservar la primera línea"
    )


# ---------------------------------------------------------------------------
# Corrección: entrega por stdin
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("nombre", sorted(PAYLOADS))
def test_el_prompt_llega_integro_por_stdin(nombre: str, tmp_path: Path) -> None:
    """Los seis payloads llegan byte a byte por ``stdin``, a través del mismo shim ``.cmd``."""
    shim = _shim_stdin(tmp_path)
    payload = PAYLOADS[nombre]

    process = RealSubprocessRunner().run_with_stdin(
        (str(shim),), timeout=60.0, stdin_text=payload
    )

    assert process.exit_code == 0
    assert process.stdout == payload


def test_run_prompt_entrega_por_stdin_y_no_por_argv(tmp_path: Path) -> None:
    """``run_prompt`` de un transporte con ``prompt_via_stdin`` usa ``stdin`` y no el ``argv``."""
    shim = _shim_stdin(tmp_path)
    transporte = _ShimTransport(shim)
    payload = PAYLOADS["multiline_json"]

    process = transporte.run_prompt(payload)

    assert process.exit_code == 0
    assert process.stdout == payload
    assert process.argv == (str(shim),)
    assert payload not in " ".join(process.argv)
    transporte.close()


def test_el_contenido_no_se_ejecuta_como_shell(tmp_path: Path) -> None:
    """Los metacaracteres del payload no se interpretan: ni redirección ni expansión."""
    shim = _shim_stdin(tmp_path)
    transporte = _ShimTransport(shim)
    marcador = tmp_path / "inyectado.txt"

    process = transporte.run_prompt(PAYLOADS["shell_metacharacters"])

    assert process.stdout == PAYLOADS["shell_metacharacters"]
    assert "%PATH%" in process.stdout, "la variable no se expande"
    assert "INJECTED" in process.stdout, "el comando no se ejecuta"
    assert not marcador.exists(), "el payload no puede crear archivos"
    transporte.close()


def test_el_runner_inyectado_decide_la_via() -> None:
    """Con un runner que soporta ``stdin`` se usa esa vía; con uno clásico, el ``argv``."""
    con_stdin = _RecordingStdinRunner()
    transporte = CodexTransport(model="gpt-5.6-sol", runner=con_stdin)

    process = transporte.run_prompt("linea uno\nlinea dos")

    assert con_stdin.llamadas[0]["via"] == "stdin"
    assert con_stdin.llamadas[0]["payload"] == "linea uno\nlinea dos"
    assert process.argv[-1] != "linea uno\nlinea dos", "el prompt no viaja en el argv"
    assert "exec" in process.argv and "--json" in process.argv

    sin_stdin = _RecordingArgvRunner()
    clasico = CodexTransport(model="gpt-5.6-sol", runner=sin_stdin)
    clasico.run_prompt("linea uno\nlinea dos")

    assert sin_stdin.argv[-1] == "linea uno\nlinea dos", (
        "un runner sin entrega por stdin conserva el comportamiento histórico"
    )


def test_codex_declara_la_entrega_por_stdin_y_su_argv_de_inspeccion() -> None:
    """El ``argv`` de inspección documenta el comando; el de entrega omite el prompt."""
    transporte = CodexTransport(model="gpt-5.6-sol")

    inspeccion = transporte.execution_argv("PROMPT")
    entrega = transporte.delivery_argv("PROMPT")

    assert transporte.prompt_via_stdin is True
    assert inspeccion[-1] == "PROMPT"
    assert "PROMPT" not in entrega
    assert entrega == inspeccion[:-1]
    assert entrega[1] == "exec" and "--sandbox" in entrega and "read-only" in entrega
    transporte.close()


def test_claude_code_conserva_la_entrega_por_argumento() -> None:
    """Claude Code no cambia de vía: su prompt sigue viajando como último argumento."""
    from punto.providers.transports.claude_code import ClaudeCodeTransport

    transporte = ClaudeCodeTransport(model="claude-sonnet-5")

    assert transporte.prompt_via_stdin is False
    assert transporte.delivery_argv("PROMPT")[-1] == "PROMPT"
    assert transporte.execution_argv("PROMPT")[-1] == "PROMPT"
    transporte.close()
