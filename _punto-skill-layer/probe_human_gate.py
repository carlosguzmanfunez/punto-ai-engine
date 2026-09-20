"""Sonda determinista: ¿con qué tamaño de ampliación responde el sobre con Human Gate?

No gasta proveedor: construye el ciclo y llama a ``_handle_scope_expansion`` con repositorios
falsos cada vez más grandes, imprimiendo el desenlace y si el plan se revisó. Sirve para saber si la
invariante «sin aprobación no se aplica nada» es demostrable por las dos vías (DENIED y HUMAN_GATE).
"""

from __future__ import annotations

from typing import Any


class _RepoFalso:
    """Repositorio mínimo: todo lo pedido es nuevo, así que la ampliación crea recursos."""

    def __init__(self, existentes: tuple[str, ...] = ()) -> None:
        self._existentes = set(existentes)

    def exists(self, path: str) -> bool:
        """True solo para rutas declaradas existentes."""
        return path in self._existentes


def main() -> int:
    """Prueba varios tamaños de petición y reporta el desenlace."""
    from punto.audit.logger import AuditLogger
    from punto.orchestrator.dev_cycle import DevelopmentConfig, DevelopmentCycle
    from punto.providers.contract import ProviderRole
    from punto.providers.router import ProviderRouter
    from punto.schemas.build import BuildRequest
    from punto.schemas.dev import DevelopmentPlan, FunctionalChainStep
    from punto.workspace.target import DevelopmentTargetRegistry

    plan = DevelopmentPlan(
        summary="unificar la fuente de tipos",
        files_to_modify=("src/components/Rejilla.tsx", "src/components/Buscador.tsx"),
        verification_commands=("focused", "chain"),
        acceptance_mapping=("una sola fuente de tipos",),
        functional_chain=(
            FunctionalChainStep(step="fuente canónica", verification="focused"),
            FunctionalChainStep(step="consumidores", verification="chain"),
        ),
    )
    peticion = BuildRequest(
        objective="unificar la lista de tipos",
        target_repository="sonda",
        requested_role=ProviderRole.BUILDER,
        acceptance_criteria=("una sola fuente de tipos",),
    )
    for tamano in (1, 5, 10, 19, 21, 30, 45, 61, 80):
        ciclo = DevelopmentCycle(
            router=ProviderRouter(),
            targets=DevelopmentTargetRegistry({}),
            config=DevelopmentConfig(),
            audit=AuditLogger(),
        )
        payload: dict[str, Any] = {
            "trigger": "evidencia de la verificación focalizada",
            "evidence": ["focused exit 1: TIPOS: Casa"],
            "root_cause": "la fuente canónica no declara el tipo que la verificación mide",
            "resources": [f"src/lib/nuevo-{i:03d}.ts" for i in range(tamano)],
            "operations": ["MODIFY"],
            "relationship": "misma cadena funcional del objetivo",
        }
        nuevo, estado = ciclo._handle_scope_expansion(
            request=peticion,
            plan=plan,
            repository=_RepoFalso(),  # type: ignore[arg-type]
            payload=payload,
            round_index=1,
        )
        print(
            f"recursos={tamano:3} desenlace={estado:10} "
            f"plan_revisado={nuevo is not plan} "
            f"riesgo={ciclo._last_risk}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
