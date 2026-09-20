"""Depuración del preflight: qué prompt recibió cada invocación y qué eventos salieron.

Uso:
    python _punto-skill-layer/debug_preflight.py d7 | noop
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parent.parent


def _modulo() -> Any:
    """Carga el módulo de pruebas del preflight para reutilizar su montaje."""
    ruta = RAIZ / "tests" / "test_proposal_preflight.py"
    especificacion = importlib.util.spec_from_file_location("tpp", ruta)
    assert especificacion is not None and especificacion.loader is not None
    modulo = importlib.util.module_from_spec(especificacion)
    especificacion.loader.exec_module(modulo)
    return modulo


def _resumen(modulo: Any, responses: list[Any], etiqueta: str) -> None:
    """Ejecuta el escenario y describe prompts y eventos."""
    destino = Path(tempfile.mkdtemp(prefix=f"debug-{etiqueta}-"))
    result, detalle, client = modulo._run_e2e(destino, responses)
    print(f"===== {etiqueta} =====")
    print(f"status={result.status.value} repairs={result.repair_rounds} "
          f"structural={result.structural_corrections} chain={result.functional_chain_result}")
    for indice, prompt in enumerate(client.prompts):
        marcas = {
            "plan": '"functional_chain"' in prompt,
            "resolucion": "RESOLUTION INPUT" in prompt,
            "preflight": "PROPOSAL PREFLIGHT FAILED" in prompt,
            "rechazadas": "REJECTED CHANGES" in prompt,
        }
        print(f"  prompt[{indice}] chars={len(prompt)} {marcas}")
    for evento in detalle:
        if evento["event"] in (
            "DEV_PROPOSAL_PREFLIGHT_FAILED",
            "DEV_CHANGE_REJECTED",
            "DEV_CHANGE_VALIDATED",
            "DEV_SCOPE_EXPANSION_APPROVED",
            "DEV_CAUSAL_PROGRESS",
            "DEV_REPAIR_EXHAUSTED",
        ):
            print(f"  {evento['event']}: {str(dict(evento))[:220]}")


def main() -> int:
    """Ejecuta los dos escenarios de interés."""
    modulo = _modulo()
    _resumen(
        modulo,
        [modulo._plan(), modulo._impl_inicial(), modulo._propuesta_d7(), modulo._propuesta_corregida()],
        "d7",
    )
    _resumen(
        modulo,
        [
            modulo._plan(),
            modulo._impl_inicial(),
            {
                "summary": "lo mismo otra vez",
                "changes": [
                    {
                        "path": "src/components/Rejilla.tsx",
                        "operation": "MODIFY",
                        "content": modulo.CONSUMIDOR_FUENTE.replace(
                            "function X", "function Rejilla"
                        ),
                        "reason": "repetir",
                        "acceptance_criterion": "una sola fuente de tipos",
                    },
                    {
                        "path": "src/components/Buscador.tsx",
                        "operation": "MODIFY",
                        "content": modulo.CONSUMIDOR_FUENTE.replace(
                            "function X", "function Buscador"
                        ),
                        "reason": "repetir",
                        "acceptance_criterion": "una sola fuente de tipos",
                    },
                ],
                "root_cause": "los consumidores no consumen la fuente",
                "evidence": ["chain exit 1"],
                "expected_effect": "consumir la fuente",
            },
            {
                "summary": "la fuente",
                "changes": [
                    {
                        "path": "src/lib/tipos.ts",
                        "operation": "MODIFY",
                        "content": "export const TIPOS = ['Casa', 'Apartamento'];\n",
                        "reason": "la verificación mide este fichero",
                        "acceptance_criterion": "una sola fuente de tipos",
                    }
                ],
                "root_cause": "la fuente no declara el tipo",
                "evidence": ["focused exit 1"],
                "expected_effect": "focused pasa",
                "scope_expansion": {
                    "trigger": "evidencia",
                    "evidence": ["focused mide src/lib/tipos.ts"],
                    "root_cause": "fuente canónica",
                    "resources": ["src/lib/tipos.ts"],
                    "operations": ["MODIFY"],
                    "relationship": "misma cadena funcional",
                },
            },
        ],
        "noop",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
