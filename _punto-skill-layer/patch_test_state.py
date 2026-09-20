"""Reemplazo puntual: el estado del workspace en las pruebas pasa de huellas a contenido."""

from __future__ import annotations

import pathlib

RUTA = pathlib.Path("tests/test_proposal_preflight.py")

REEMPLAZOS: tuple[tuple[str, str], ...] = (
    ('{"src/lib/opciones.ts": _huella("x\\n")}', '{"src/lib/opciones.ts": "x\\n"}'),
    ('{"src/lib/tipos.ts": _huella(TIPOS)}', '{"src/lib/tipos.ts": TIPOS}'),
    (
        '{"src/lib/tipos.ts": _huella(TIPOS), "src/ocupado.ts": _huella("x\\n")}',
        '{"src/lib/tipos.ts": TIPOS, "src/ocupado.ts": "x\\n"}',
    ),
    (
        '{"src/lib/tipos.ts": _huella(TIPOS), "src/components/Rejilla.tsx": _huella("viejo\\n")}',
        '{"src/lib/tipos.ts": TIPOS, "src/components/Rejilla.tsx": "viejo\\n"}',
    ),
    ('{"tests/tipos-chain.test.ts": _huella(TEST_NUEVO)}', '{"tests/tipos-chain.test.ts": TEST_NUEVO}'),
    ('{"src/lib/tipos.ts": _huella(TIPOS)} if existe else {}', '{"src/lib/tipos.ts": TIPOS} if existe else {}'),
)


def main() -> int:
    """Aplica los reemplazos y reporta lo que queda."""
    texto = RUTA.read_text(encoding="utf-8")
    for viejo, nuevo in REEMPLAZOS:
        if viejo not in texto:
            print(f"aviso: no encontrado {viejo!r}")
            continue
        texto = texto.replace(viejo, nuevo)
    RUTA.write_text(texto, encoding="utf-8")
    print("restantes _huella(:", texto.count("_huella("))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
