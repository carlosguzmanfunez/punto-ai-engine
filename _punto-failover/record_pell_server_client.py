"""Registra en PELL dos aprendizajes reutilizables (VERIFIED): frontera server/client y hover real.

Mismas reglas que ``record_pell_failover.py``. Escribe ``pell-server-client-boundary.jsonl``.
"""

from __future__ import annotations

from pathlib import Path

MEMORIA = Path(__file__).resolve().parent / "pell-server-client-boundary.jsonl"


def main() -> int:
    """Registra los aprendizajes y reporta el resultado."""
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORIA)
    a = store.record(
        problem=(
            "un módulo server-only (node:fs) entra al bundle del cliente por un import transitivo "
            "y el framework responde 500 en todas las páginas que lo renderizan"
        ),
        context=(
            "Next/Turbopack: src/lib/honduras.ts lee un GeoJSON con node:fs y era inocuo mientras "
            "solo lo importaban Server Components. Al añadir hover, el mapa pasó a 'use client' y "
            "siguió importando valores de ese módulo: page.tsx -> mapa (use client) -> honduras.ts "
            "-> node:fs, y / y /propiedades dieron 500 (TurbopackInternalError: external modules)."
        ),
        attempts=("deshabilitar Turbopack o un workaround global de bundling",),
        failure_reason="el error señala el síntoma (node:fs), no el cliente que arrastró el módulo",
        solution=(
            "cortar en la frontera: un Server Component lee los datos server-only y los entrega por "
            "props a una vista cliente que solo importa tipos (import type se borra al compilar); "
            "las páginas conservan su llamada"
        ),
        procedure=(
            "trazar el grafo desde la página: server -> client -> módulo -> node:*",
            "al volver cliente un componente, revisar qué importa (valores vs tipos)",
            "fijarlo con una prueba de grafo: ningún 'use client' alcanza node:* transitivamente",
            "comprobar la prueba con una mutación que reintroduzca el import",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "punto-inmobiliario-hn: tests/honduras-map.test.mjs::ningún Client Component arrastra node:fs",
            "GET / y /propiedades -> 200 con el servidor real y sin node:fs en el HTML",
        ),
        tags=(
            "type:server-client-boundary",
            "trigger:turbopack-node-fs-500",
            "component:nextjs/use-client",
            "provenance:live-and-deterministic-test",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    b = store.record(
        problem=(
            "el hover real falla en sitios con scroll-behavior: smooth porque el punto se calcula "
            "con coordenadas de un scroll aún en animación"
        ),
        context=(
            "Al ejecutar la interacción sobre la app real, el elemento se localizaba pero el "
            "navegador no reportaba :hover: globals.css declara html{scroll-behavior:smooth}, "
            "scrollIntoView animaba y las coordenadas quedaban obsoletas cuando se movía el ratón."
        ),
        attempts=("calcular el punto una sola vez justo tras scrollIntoView",),
        failure_reason="las coordenadas se usaron antes de que el layout se asentara",
        solution=(
            "scrollIntoView instantáneo y recalcular el punto de hover tras el asentamiento, "
            "volviendo a exigir que el elemento reciba el cursor (elementFromPoint)"
        ),
        procedure=(
            "no reutilizar coordenadas calculadas antes de esperar al asentamiento",
            "forzar behavior 'instant' en el scroll de la automatización",
            "reproducir con una página real de scroll suave y el elemento bajo el pliegue",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_visual_interaction.py::test_3d_el_hover_funciona_con_scroll_suave_y_el_elemento_bajo_el_pliegue",
            "hover real sobre Cortés en la app real: usable, pixels_changed, etiqueta correcta",
        ),
        tags=(
            "type:interaction-evidence",
            "trigger:hover-not-applied-smooth-scroll",
            "component:visualqa/interaction",
            "provenance:live-and-deterministic-test",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {a.id} / {b.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
