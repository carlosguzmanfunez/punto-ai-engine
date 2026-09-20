"""PILOT-05 - pruebas del sobre de autoridad adaptativo (casos A-F, I, J, M, P, S, T del encargo).

El sobre decide por **riesgo efectivo**, con reglas nombradas y auditables. Estas pruebas fijan cada
frontera: lo local reversible es autónomo aunque crezca; lo que toca producción, identidad,
secretos, recursos constitucionales o efectos externos exige una persona o se deniega, aunque sea
**un solo archivo**.
"""

from __future__ import annotations

import pytest

from punto.policy.envelope import (
    AUTONOMOUS_MAX_FILES,
    AdaptiveAuthorityEnvelope,
    EnvelopeOperation,
    Environment,
    ExternalEffect,
    OperationRisk,
    Provenance,
    ResourceClass,
    VerificationStrength,
)


@pytest.fixture
def envelope() -> AdaptiveAuthorityEnvelope:
    """Sobre con las rutas constitucionales que declara la constitución del motor."""
    return AdaptiveAuthorityEnvelope(
        constitutional_paths=(
            "config/constitution.yaml",
            "config/permissions.yaml",
            "config/budgets.yaml",
            "config/risk-rules.yaml",
        )
    )


def _write(files: list[str], **overrides: object) -> OperationRisk:
    """Perfil de un cambio local típico."""
    payload: dict[str, object] = {
        "operation": EnvelopeOperation.PLAN_APPLY,
        "resources": tuple(files),
        "environment": Environment.LOCAL,
        "reversible": True,
        "verification_strength": VerificationStrength.STRONG,
        "provenance": Provenance.PUNTO_POLICY,
        "evidence": ("evidencia del plan",),
    }
    payload.update(overrides)
    return OperationRisk(**payload)  # type: ignore[arg-type]


# ===========================================================================
# A-C · capacidad local amplia: el número de archivos no es la autoridad
# ===========================================================================
def test_a_un_archivo_local_reversible_es_autonomo(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """A · un cambio local reversible no necesita a nadie."""
    decision = envelope.assess(_write(["src/lib/opciones.ts"]))

    assert decision.outcome.value == "ALLOW"
    assert decision.authority_class.value == "AUTONOMOUS_LOCAL"
    assert decision.risk.name == "LOW"
    assert decision.autonomous and not decision.requires_human


def test_b_ocho_archivos_relacionados_con_tests_siguen_siendo_autonomos(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """B · ocho ficheros locales con tests: más trabajo, el mismo riesgo."""
    files = [f"src/components/panel-{index}.tsx" for index in range(1, 7)]
    files += ["tests/property-types.test.mjs", "src/lib/property-types.ts"]

    decision = envelope.assess(_write(files))

    assert len(files) == 8
    assert decision.outcome.value == "ALLOW"
    assert decision.risk.name == "MEDIUM"
    assert decision.autonomous


def test_c_quince_archivos_de_refactor_mecanico_se_evaluan_por_riesgo(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """C · 15 ficheros reversibles no se rechazan por número: el riesgo sigue siendo MEDIUM."""
    files = [f"src/components/legacy-{index:02d}.tsx" for index in range(15)]

    decision = envelope.assess(_write(files, verification_strength=VerificationStrength.MODERATE))

    assert decision.risk.name == "MEDIUM"
    assert decision.outcome.value == "ALLOW"
    assert decision.autonomous
    assert "runaway-blast-radius" not in decision.rule_names


def test_el_techo_anti_runaway_no_es_la_frontera_pero_existe(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """Más allá del presupuesto anti-runaway, un cambio local pide autorización humana."""
    files = [f"src/components/bulk-{index:03d}.tsx" for index in range(AUTONOMOUS_MAX_FILES + 5)]

    decision = envelope.assess(_write(files))

    assert "runaway-blast-radius" in decision.rule_names
    assert decision.requires_human
    assert decision.risk.name == "HIGH"


# ===========================================================================
# D-F · fronteras que siguen exigiendo una persona
# ===========================================================================
def test_d_un_archivo_de_produccion_exige_human_gate(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """D · un solo archivo, pero en producción: Human Gate."""
    decision = envelope.assess(
        _write(["src/app/panel/page.tsx"], environment=Environment.PRODUCTION)
    )

    assert decision.requires_human
    assert decision.authority_class.value == "HUMAN_GATE_REQUIRED"
    assert "production-environment" in decision.rule_names
    assert decision.required_evidence


def test_e_un_cambio_de_permisos_de_autenticacion_exige_human_gate(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """E · identidad/autorización: Human Gate aunque sea un fichero pequeño."""
    decision = envelope.assess(_write(["src/lib/auth/session.ts"]))

    assert decision.requires_human
    assert "identity-auth" in decision.rule_names
    assert ResourceClass.IDENTITY_AUTH in decision.resource_classes


def test_f_una_migracion_destructiva_de_produccion_exige_human_gate(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """F · mutar datos de producción es irreversible para el ciclo."""
    decision = envelope.assess(
        _write(
            ["drizzle/0007_drop.sql"],
            operation=EnvelopeOperation.PRODUCTION_DATABASE,
            destructive=True,
            environment=Environment.PRODUCTION,
        )
    )

    assert decision.requires_human
    assert decision.risk.name == "CRITICAL"
    assert "operation-production_database" in decision.rule_names


# ===========================================================================
# I, J · ni el proveedor ni PELL conceden autoridad
# ===========================================================================
def test_i_una_afirmacion_del_proveedor_sin_evidencia_se_deniega(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """I · el proveedor propone; sin evidencia no obtiene nada."""
    decision = envelope.assess(
        _write(["src/lib/opciones.ts"], provenance=Provenance.PROVIDER_PROPOSAL, evidence=())
    )

    assert decision.prohibited
    assert "provider-claim-without-evidence" in decision.rule_names
    # Una operación prohibida es el caso más grave del modelo: riesgo máximo.
    assert decision.risk.name == "CRITICAL"


@pytest.mark.parametrize(
    "operation",
    [EnvelopeOperation.PUBLISH, EnvelopeOperation.FORCE_PUSH, EnvelopeOperation.AUTHORITY_CHANGE],
)
def test_j_una_experiencia_de_pell_no_concede_autoridad_protegida(
    envelope: AdaptiveAuthorityEnvelope, operation: EnvelopeOperation
) -> None:
    """J · PELL es experiencia, no permiso: no puede amparar una operación protegida."""
    decision = envelope.assess(
        _write(
            ["src/lib/opciones.ts"],
            operation=operation,
            provenance=Provenance.PELL_MEMORY,
            evidence=("una experiencia lo sugiere",),
        )
    )

    assert decision.outcome.value == "REJECT"
    assert "pell-authority-claim" in decision.rule_names


# ===========================================================================
# M · autoelevación: modificar las reglas de la propia autoridad
# ===========================================================================
@pytest.mark.parametrize(
    "target",
    [
        "config/budgets.yaml",
        "config/constitution.yaml",
        "config/risk-rules.yaml",
        "config/permissions.yaml",
        "src/punto/policy/policy_engine.py",
        "src/punto/policy/human_gate.py",
        "src/punto/policy/envelope.py",
        "src/punto/security/deterministic.py",
    ],
)
def test_m_la_autoelevacion_de_autoridad_esta_denegada(
    envelope: AdaptiveAuthorityEnvelope, target: str
) -> None:
    """M · cambiar lo que decide cuánto puede cambiar el ciclo es autoelevación."""
    decision = envelope.assess(_write([target]))

    assert decision.prohibited
    assert "constitutional-resource" in decision.rule_names
    assert "autoelevación" in " ".join(decision.reasons)


def test_m_el_ciclo_no_puede_hacer_pasar_la_autoelevacion_por_una_expansion(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """M · pedirla como expansión de alcance tampoco la concede."""
    expansion = envelope.expansion(
        _write(["src/lib/opciones.ts"]),
        _write(["src/lib/opciones.ts", "config/budgets.yaml"]),
        evidence=("quiero subir el techo",),
        relationship="me conviene",
    )

    assert expansion.outcome.value == "REJECT"
    assert not expansion.approved


# ===========================================================================
# P-T · fronteras clásicas que no se relajan
# ===========================================================================
def test_p_un_fichero_de_secretos_sigue_denegado(envelope: AdaptiveAuthorityEnvelope) -> None:
    """P · la frontera de secretos no se mueve."""
    decision = envelope.assess(_write(["src/.env.local"]))

    assert decision.prohibited
    assert "secret-store" in decision.rule_names


def test_s_push_y_reescritura_de_historial_denegados(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """S · publicar el trabajo no es una operación del ciclo local."""
    for operation in (EnvelopeOperation.FORCE_PUSH, EnvelopeOperation.HISTORY_REWRITE):
        decision = envelope.assess(_write(["src/lib/opciones.ts"], operation=operation))
        assert decision.prohibited, operation
    # Publicar sí tiene camino humano: es una decisión de release, no una prohibición técnica.
    publish = envelope.assess(
        _write(["src/lib/opciones.ts"], operation=EnvelopeOperation.PUBLISH)
    )
    assert publish.requires_human and not publish.autonomous


def test_t_un_despliegue_a_produccion_exige_human_gate(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """T · desplegar es producción, no desarrollo."""
    decision = envelope.assess(
        _write(["vercel.json"], operation=EnvelopeOperation.DEPLOY, cost_impact=True)
    )

    assert decision.requires_human
    assert decision.risk.name in {"HIGH", "CRITICAL"}
    assert "operation-deploy" in decision.rule_names


# ===========================================================================
# Escritura sin verificación, reversibilidad y efectos externos
# ===========================================================================
def test_escribir_sin_ninguna_verificacion_no_es_autonomo(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """Sin forma de comprobar el resultado, la escritura necesita a una persona."""
    decision = envelope.assess(
        _write(["src/lib/a.ts"], verification_strength=VerificationStrength.NONE)
    )

    assert decision.requires_human
    assert "no-verification" in decision.rule_names


def test_una_verificacion_debil_aplica_con_evidencia_obligatoria(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """Con verificación débil el cambio se aplica, pero exige evidencia antes de confirmar."""
    decision = envelope.assess(
        _write(["src/lib/a.ts"], verification_strength=VerificationStrength.WEAK)
    )

    assert decision.outcome.value == "ALLOW_WITH_REVIEW"
    assert decision.authority_class.value == "AUTONOMOUS_VERIFIED"
    assert decision.required_evidence


def test_un_efecto_externo_de_coste_exige_human_gate(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """Crear coste no es una decisión técnica."""
    decision = envelope.assess(_write(["infra/main.tf"], external_effect=ExternalEffect.COST))

    assert decision.requires_human
    assert "external-cost" in decision.rule_names


def test_borrar_lo_que_el_propio_ciclo_creo_es_revertir(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """Borrar un recurso propio no es destruir: es deshacer."""
    decision = envelope.assess(
        _write(["src/lib/nuevo.ts"], operation=EnvelopeOperation.DELETE, destructive=True,
               created_by_cycle=True)
    )

    assert decision.autonomous
    assert "revert-of-own-change" in decision.rule_names


def test_borrar_algo_preexistente_exige_human_gate(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """Borrar algo que ya estaba no es una operación autónoma del ciclo."""
    decision = envelope.assess(
        _write(["src/db/seed.sql"], operation=EnvelopeOperation.DELETE, destructive=True)
    )

    assert decision.requires_human
    assert "destructive-local-data" in decision.rule_names


def test_una_clase_de_recurso_desconocida_falla_cerrado(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """Lo que no se sabe clasificar no se toca."""
    decision = envelope.assess(_write(["src/lib/datos.binario"]))

    assert decision.requires_human
    assert "unknown-resource" in decision.rule_names


# ===========================================================================
# G, H, K, L, N · expansión de alcance y revisión de plan
# ===========================================================================
def test_g_una_expansion_causal_dentro_de_la_misma_clase_es_autonoma(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """G · el sexto consumidor del mismo concepto entra sin pedir permiso."""
    expansion = envelope.expansion(
        _write(["src/components/CategoryGrid.tsx", "src/lib/property-types.ts"]),
        _write(
            [
                "src/components/CategoryGrid.tsx",
                "src/lib/property-types.ts",
                "src/app/propiedades/page.tsx",
            ]
        ),
        evidence=("la prueba de la cadena funcional falla: page.tsx declara su propia lista",),
        relationship="consumidor del mismo concepto de dominio (tipos de propiedad)",
        trigger="evidencia de la regresión",
        root_cause="fuente de tipos duplicada",
    )

    assert expansion.approved
    assert expansion.outcome.value == "ALLOW"
    assert expansion.delta.previous_risk is not None
    assert expansion.record["new_resources"] == ["src/app/propiedades/page.tsx"]
    assert expansion.record["authority_decision"] == "ALLOW"


def test_h_una_expansion_sin_causa_se_deniega(envelope: AdaptiveAuthorityEnvelope) -> None:
    """H · sin evidencia ni relación con el objetivo, no hay expansión."""
    for evidence, relationship in (((), "da igual"), (("porque sí",), "")):
        expansion = envelope.expansion(
            _write(["src/lib/opciones.ts"]),
            _write(["src/lib/opciones.ts", "src/components/otro.tsx"]),
            evidence=evidence,
            relationship=relationship,
        )
        assert expansion.outcome.value == "REJECT"
        assert not expansion.approved


def test_l_una_revision_que_cruza_una_frontera_critica_exige_human_gate(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """L · el plan puede crecer; lo que no puede es cruzar a una clase protegida."""
    expansion = envelope.expansion(
        _write(["src/lib/opciones.ts"]),
        _write(["src/lib/opciones.ts", "src/lib/auth/permissions.ts"]),
        evidence=("el test de permisos falla",),
        relationship="mismo flujo de la aplicación",
    )

    assert expansion.outcome.value == "REQUIRE_HUMAN"
    assert not expansion.approved
    assert any("identidad" in item for item in expansion.reasons)


def test_la_fragmentacion_en_revisiones_pequenas_no_es_autonoma() -> None:
    """Fragmentar una escalada grande en revisiones pequeñas sigue siendo una escalada.

    Cada revisión, mirada sola, cabe en el techo anti-runaway; lo que no cabe es la **sesión**. Por
    eso el sobre compara el alcance acumulado, no solo lo añadido.
    """
    envelope = AdaptiveAuthorityEnvelope(max_files=20, session_ceiling=25)
    cumulative = [f"src/components/piece-{index:02d}.tsx" for index in range(20)]
    requested = [
        *cumulative[:14],
        *(f"src/components/more-{index}.tsx" for index in range(6)),
    ]

    expansion = envelope.expansion(
        _write(cumulative),
        _write(requested),
        evidence=("evidencia real",),
        relationship="misma cadena funcional",
        cumulative_resources=cumulative,
    )

    assert expansion.outcome.value == "REQUIRE_HUMAN"
    assert expansion.record.get("fragmentation_rule") == "session-fragmentation"
    assert not expansion.approved


def test_una_expansion_hacia_produccion_exige_human_gate(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """Cruzar a producción no se aprueba solo porque la evidencia sea buena."""
    expansion = envelope.expansion(
        _write(["src/lib/opciones.ts"]),
        _write(["src/lib/opciones.ts", "infra/production.tf"]),
        evidence=("el despliegue falla",),
        relationship="misma cadena",
    )

    assert expansion.outcome.value == "REQUIRE_HUMAN"


# ===========================================================================
# Auditabilidad del modelo de riesgo (no hay puntuación opaca)
# ===========================================================================
def test_la_decision_es_explicable_regla_a_regla(envelope: AdaptiveAuthorityEnvelope) -> None:
    """Cada veredicto lleva sus reglas, sus motivos y la evidencia exigida."""
    decision = envelope.assess(_write(["src/lib/auth/login.ts"]))
    payload = decision.as_dict()

    assert payload["outcome"] == "REQUIRE_HUMAN"
    assert payload["rules"], "una decisión sin reglas no es auditable"
    assert all({"rule", "verdict", "reason"} == set(item) for item in payload["rules"])
    assert payload["required_evidence"]
    assert payload["profile"]["operation"] == "plan_apply"
    assert "risk_score" not in payload


def test_atributos_vacios_no_degradan_la_decision(envelope: AdaptiveAuthorityEnvelope) -> None:
    """Un perfil sin recursos ni evidencia no se convierte en un permiso por omisión."""
    decision = envelope.assess(
        OperationRisk(
            operation=EnvelopeOperation.WRITE,
            provenance=Provenance.PROVIDER_PROPOSAL,
            evidence=(),
        )
    )

    assert decision.prohibited
    assert decision.risk.name == "CRITICAL"


def test_el_mismo_perfil_da_siempre_la_misma_decision(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """Determinismo: dos evaluaciones idénticas no pueden diferir."""
    profile = _write(["src/components/CategoryGrid.tsx", "src/lib/property-types.ts"])

    first = envelope.assess(profile).as_dict()
    second = envelope.assess(profile).as_dict()

    assert first == second
