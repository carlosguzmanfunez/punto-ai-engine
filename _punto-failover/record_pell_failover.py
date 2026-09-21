"""Registra en PELL los aprendizajes reutilizables de PROVIDER FAILOVER v0.

Reglas que se respetan (mismas que ``_punto-skill-layer/record_pell_preflight.py``):

- solo se registra lo que es **propiedad del motor demostrada con pruebas deterministas**
  (``VERIFIED`` exige evidencia declarada); nada de esto es una observación empírica de una corrida
  real, así que no hay ``CANDIDATE``;
- solo lo **reutilizable**: no se guarda «cambié el fichero X» ni el caso de un proyecto concreto;
- no se guardan logs, prompts ni secretos: causa, resolución, procedimiento y evidencia.

Escribe ``_punto-failover/pell-provider-failover.jsonl`` (no toca la memoria por defecto del motor).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

EVIDENCE = Path(__file__).resolve().parent
MEMORIA = EVIDENCE / "pell-provider-failover.jsonl"

APRENDIZAJES: tuple[dict[str, Any], ...] = (
    {
        "clave": "F1",
        "problem": (
            "un error de proveedor que no hereda de la jerarquía normalizada se degrada a UNKNOWN y "
            "pierde la causa operativa (sin saldo, sin sesión, sin servicio)"
        ),
        "context": (
            "Los errores de DeepSeek son RuntimeError: atraviesan el transporte de API sin "
            "traducirse y el router los forzaba a UNKNOWN, así que un 402 era indistinguible de "
            "cualquier fallo y el motor no podía decidir nada sobre la disponibilidad."
        ),
        "solution": (
            "clasificar en la frontera del router por la clase que declara el adaptador (nunca por "
            "el texto del mensaje) y dejar UNKNOWN solo para lo que no se reconoce; añadir "
            "QUOTA_EXHAUSTED al vocabulario normalizado"
        ),
        "procedure": (
            "no forzar UNKNOWN en el except genérico del router",
            "mapear Balance/Quota/Credit → QUOTA_EXHAUSTED, RateLimit → RATE_LIMIT, AuthError → "
            "AUTHENTICATION, ServerError → UNAVAILABLE por nombre de clase",
        ),
        "verification": (
            "tests/test_provider_failover.py::test_2b_el_402_de_deepseek_ya_no_es_un_fallo_desconocido",
            "tests/test_provider_failover.py::test_2_primario_sin_creditos_o_cuota_continua_con_el_sustituto",
        ),
        "tags": (
            "type:error-classification",
            "trigger:provider-error",
            "component:providers/router",
            "provenance:deterministic-test",
            "related_to:F2",
        ),
    },
    {
        "clave": "F2",
        "problem": (
            "un rol se detiene cuando su proveedor primario no está operativo aunque otro proveedor "
            "conectado pueda hacer el trabajo"
        ),
        "context": (
            "El motor prohibía cambiar de proveedor (auditoría cruzada y cargos), sin ninguna "
            "excepción gobernada. Un failover permisivo sería un fallback silencioso; ninguno deja "
            "la Task parada por una cuenta sin créditos."
        ),
        "solution": (
            "failover explícito por rol: política declarada en configuración, disparada solo por "
            "indisponibilidad operativa demostrable del primario, hacia un candidato CONNECTED con "
            "la capacidad del rol efectiva en su transporte; sin ampliar autoridad y con fallo "
            "cerrado si nadie es compatible"
        ),
        "procedure": (
            "decidir por causa normalizada (cuota, tasa, no disponible, sin sesión); nunca por una "
            "respuesta incorrecta, un timeout o un error del proyecto",
            "juzgar al candidato con la capacidad efectiva (configurada ∩ transporte ∩ "
            "disponibilidad), no con la declarada; exigir opt-in para sustitutos de pago por uso",
            "un intento por proveedor y tope de sustitutos; empezar siempre por el primario",
            "registrar primario, causa, sustituto y desenlace en la Task (misma identidad)",
        ),
        "verification": (
            "tests/test_provider_failover.py::test_a_aceptacion_builder_sin_creditos_continua_con_claude_en_la_misma_task",
            "tests/test_provider_failover.py::test_7_una_respuesta_incorrecta_o_un_fallo_no_operativo_no_cambia_de_proveedor",
            "tests/test_provider_failover.py::test_d_el_sustituto_no_hereda_autoridad_un_borrado_sigue_exigiendo_persona",
            "tests/test_provider_failover.py::test_11_cuando_el_primario_se_recupera_vuelve_a_responder_el_primario",
            "audit:PROVIDER_FAILOVER",
        ),
        "tags": (
            "type:provider-failover",
            "trigger:primary-provider-unavailable",
            "component:providers/failover",
            "provenance:deterministic-test",
            "related_to:F1",
        ),
    },
)


def main() -> int:
    """Registra los aprendizajes y reporta el estado resultante."""
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORIA)
    for aprendizaje in APRENDIZAJES:
        guardado = store.record(
            problem=aprendizaje["problem"],
            context=aprendizaje["context"],
            attempts=(),
            failure_reason="",
            solution=aprendizaje["solution"],
            procedure=aprendizaje["procedure"],
            result=ExperienceResult.SUCCESS,
            verification=aprendizaje["verification"],
            tags=aprendizaje["tags"],
            status=ExperienceStatus.VERIFIED,
        )
        print(f"[V] {aprendizaje['clave']} {guardado.id} {guardado.status.value}: "
              f"{guardado.problem[:70]}")
    print(f"\nmemoria: {MEMORIA} ({len(store.list())} experiencias)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
