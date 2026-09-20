"""Parche: import de hashlib y evento de auditoría del handoff causal."""

from __future__ import annotations

import pathlib

CYCLE = pathlib.Path(__file__).resolve().parent.parent / "src/punto/orchestrator/dev_cycle.py"
AUDIT = pathlib.Path(__file__).resolve().parent.parent / "src/punto/schemas/audit.py"
EVENTS = pathlib.Path(__file__).resolve().parent.parent / "src/punto/audit/events.py"

CYCLE_PAIRS = (
    ("import json\n", "import hashlib\nimport json\n"),
)

AUDIT_PAIRS = (
    (
        "    #: SKILL-LAYER-0: se activó una skill para un rol, con su identificador, versión y huella.\n",
        "    #: SKILL-LAYER-0: el handoff causal del plan viajó al BUILDER (presencia, tamaño y huella).\n"
        '    DEV_CAUSAL_HANDOFF = "DEV_CAUSAL_HANDOFF"\n'
        "    #: SKILL-LAYER-0: se activó una skill para un rol, con su identificador, versión y huella.\n",
    ),
)

EVENTS_PAIRS = (
    (
        '    AuditEventType.DEV_SKILL_ACTIVATED: "dev_skill",\n',
        '    AuditEventType.DEV_CAUSAL_HANDOFF: "dev_plan",\n'
        '    AuditEventType.DEV_SKILL_ACTIVATED: "dev_skill",\n',
    ),
)


def _patch(path: pathlib.Path, pairs: tuple[tuple[str, str], ...]) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        if old not in text:
            raise SystemExit(f"no encontrado en {path.name}: {old[:60]!r}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")


def main() -> None:
    """Aplica los tres cambios."""
    _patch(CYCLE, CYCLE_PAIRS)
    _patch(AUDIT, AUDIT_PAIRS)
    _patch(EVENTS, EVENTS_PAIRS)
    print("parcheado")


if __name__ == "__main__":
    main()
