"""Inspecciona la evidencia de una corrida del arnés: resolución, mapeo y progreso causal.

Uso:
    python _punto-skill-layer/inspect_resolution.py <evidencia.json> [CASE-ID]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _dump(titulo: str, valor: Any) -> None:
    print(f"--- {titulo} ---")
    print(json.dumps(valor, indent=2, ensure_ascii=False, default=str))


def main() -> int:
    """Imprime la evidencia de resolución de un caso."""
    ruta = Path(sys.argv[1])
    quiero = (sys.argv[2] if len(sys.argv) > 2 else "").upper()
    casos = json.loads(ruta.read_text(encoding="utf-8"))
    for item in casos:
        if quiero and item["case"] != quiero:
            continue
        print(f"===== {item['case']} ({item['kind']}) {item['status']} =====")
        _dump("first_repair", item.get("first_repair", {}))
        _dump("resolution_skill", item.get("resolution_skill", {}))
        _dump("first_attempt", item.get("first_attempt", {}))
        _dump(
            "calls",
            [
                {
                    "role": call["role"],
                    "phase": call["phase"],
                    "prompt_chars": call["prompt_chars"],
                    "tokens": call["total_tokens"],
                    "changes": [c["path"] for c in call["proposal"].get("changes", [])],
                    "unchanged": call["proposal"].get("unchanged_resources", []),
                    "root_cause": call["proposal"].get("root_cause", ""),
                }
                for call in item.get("calls_detail", [])
            ],
        )
        _dump("plan", item.get("plan"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
