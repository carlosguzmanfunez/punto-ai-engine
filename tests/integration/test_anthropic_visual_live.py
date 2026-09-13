"""Gates vivos de Visual QA contra Anthropic/Claude (ENGINE-5.3.1, hallazgo V53-03).

Cuatro puertas contra la API real, con el **esquema de producción** y capturas reales:

A. **autenticación del modelo visual**: la credencial funciona y el modelo resuelto por
   ``PUNTO_CLAUDE_VISUAL_MODEL`` responde a una petición estructurada trivial;
B. **una imagen + esquema de producción**: un PNG real viaja junto a
   ``provider_schema_for(VisualQAProposal)`` y la respuesta es JSON parseable tal cual y válida
   contra el contrato de PUNTO;
C. **varias imágenes + esquema de producción**: la petición multimodal con dos o más capturas
   sigue siendo aceptada, estructurada y válida;
D. **Visual QA de extremo a extremo**: el runner real ``ClaudeVisualQARunner`` evalúa una tarea
   inequívocamente limpia y los gates deterministas de PUNTO calculan ``PASS``.

**Sin credencial no se ejecuta nada.** Si ``ANTHROPIC_API_KEY`` no existe, **cada** prueba falla
con el mensaje ``CREDENTIAL_REQUIRED: ANTHROPIC_API_KEY``: no hay ``skip``, no se simula que
Claude respondió y no se declara ningún PASS. El estado correcto de la fase mientras falte la
credencial es exactamente:

    LIVE VISUAL CLAUDE GATES NOT RUN — PENDING_API_KEY

Estas pruebas viven en ``tests/integration``, que el ``addopts`` del proyecto ignora, así que la
suite estándar nunca las ejecuta. Se invocan a propósito y con red:

    .\\.venv\\Scripts\\python.exe -m pytest tests/integration/test_anthropic_visual_live.py -q -s

Por qué cada gate existe: el esquema de producción es el mismo que envía el runner, de modo que
un esquema de prueba trivial podría quedar verde mientras la ruta real responde 400; y el gate D
existe porque un informe visual solo vale si el proveedor contesta **y** los gates deterministas
de PUNTO llegan a PASS sin ayuda del modelo.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Final

import pytest

from punto.providers.anthropic import (
    API_KEY_ENV,
    DEFAULT_VISUAL_MODEL,
    LIVE_ACCOUNT_ACCESS_UNVERIFIED,
    MODEL_ID_DOCUMENTED,
    VISUAL_MODEL_ENV,
    AnthropicClient,
    config_from_environment,
)
from punto.providers.base import MAX_IMAGES, ImageLimits, ImagePayload
from punto.providers.json_schema import provider_schema_for
from punto.schemas.visual import (
    VisualQAProposal,
    VisualQAStatus,
    VisualQATask,
)
from punto.schemas.web import (
    DEFAULT_VIEWPORTS,
    Viewport,
    WebTechnicalStatus,
)
from punto.visualqa.claude import ClaudeVisualQARunner
from punto.visualqa.coverage import evaluate_visual_coverage
from punto.visualqa.prompts import VISUAL_SYSTEM_PROMPT
from visual_support import make_visual_task, png_bytes, visual_images

pytestmark = pytest.mark.integration

#: Mensaje exacto exigido cuando falta la credencial.
CREDENTIAL_REQUIRED: Final[str] = f"CREDENTIAL_REQUIRED: {API_KEY_ENV}"

#: Estado honesto de estos gates mientras no exista credencial: no se ha ejecutado nada.
PENDING_STATE: Final[str] = "LIVE VISUAL CLAUDE GATES NOT RUN — PENDING_API_KEY"

#: Presupuesto multimodal de PUNTO: como máximo ocho capturas por petición.
IMAGE_LIMITS: Final[ImageLimits] = ImageLimits()

#: Contrato trivial del gate A: structured outputs, no una instrucción textual.
AUTH_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}

#: Viewports del gate C: dos capturas reales con el mismo esquema de producción.
MULTI_IMAGE_VIEWPORTS: Final[tuple[Viewport, ...]] = DEFAULT_VIEWPORTS[:2]

#: Petición del gate B: una sola captura, sin contexto que el modelo pueda confundir.
ONE_IMAGE_PROMPT: Final[str] = (
    "Se adjunta 1 captura real de la ruta / en el viewport MOBILE (390x844) de una página "
    "correcta. Devuelve una propuesta visual que cumpla el esquema entregado: un `summary`, "
    "`findings` (vacío si no ves defectos), las cuatro valoraciones y `recommendation_notes`. "
    "Cada valoración debe ser una frase completa. No incluyas `status`."
)

#: Petición del gate C: varias capturas, que es el caso real de una evaluación por viewports.
MULTI_IMAGE_PROMPT: Final[str] = (
    "Se adjuntan 2 capturas reales de la ruta / en los viewports MOBILE (390x844) y "
    "TABLET (768x1024), en ese orden, de una página correcta. Devuelve una propuesta visual que "
    "cumpla el esquema entregado: un `summary`, `findings` (vacío si no ves defectos), las "
    "cuatro valoraciones y `recommendation_notes`, cada una como una frase completa. No incluyas "
    "`status`."
)


def require_credential() -> None:
    """Falla con el mensaje exigido si no hay credencial.

    No se salta la prueba: sin credencial no se ejecuta ninguna llamada y el estado correcto es
    ``PENDING_STATE``. Un ``skip`` escondería que los gates vivos siguen sin correr y permitiría
    leer el silencio como si fuera un resultado.
    """
    if not os.environ.get(API_KEY_ENV, "").strip():
        pytest.fail(
            f"{CREDENTIAL_REQUIRED}: no se pueden ejecutar los live gates de Visual QA sin la "
            "credencial. No se ejecuta ninguna llamada, no se simula ninguna respuesta del "
            f"modelo y esta suite no declara ningún PASS. Estado correcto: {PENDING_STATE}."
        )


@pytest.fixture(scope="module", autouse=True)
def visual_gate() -> None:
    """Sin credencial la suite falla de forma explícita, no se salta en silencio."""
    require_credential()


@pytest.fixture(scope="module")
def client() -> Iterator[AnthropicClient]:
    """Cliente real, con el modelo visual resuelto desde el entorno.

    La credencial se vuelve a exigir aquí para que el fallo tenga el mensaje exigido aunque el
    orden de instanciación de fixtures cambiara: construir el cliente sin clave lanzaría el error
    del proveedor, que no es el mensaje contractual de esta fase.
    """
    require_credential()
    instance = AnthropicClient(
        config_from_environment(
            model_env=VISUAL_MODEL_ENV, default_model=DEFAULT_VISUAL_MODEL
        )
    )
    try:
        yield instance
    finally:
        instance.close()


def clean_visual_case(
    viewports: tuple[Viewport, ...] = DEFAULT_VIEWPORTS,
) -> tuple[VisualQATask, dict[str, ImagePayload]]:
    """Tarea limpia y verificada, con la cobertura completa que exige su especificación.

    ``make_visual_task`` construye la sesión técnica con PNG reales y los checks deterministas en
    verde; ``visual_images`` produce los payloads **canónicos** reconstruidos desde el artefacto,
    que es exactamente lo que recibe el runner en producción. La cobertura se comprueba aquí con
    el evaluador real de PUNTO porque el runner **no llama al modelo** si falta un par
    ruta x viewport: una tarea incompleta nunca probaría el camino de extremo a extremo.
    """
    task, raw = make_visual_task(viewports=viewports)
    images = visual_images(task, raw)
    coverage = evaluate_visual_coverage(task.spec, task.screenshots, images)

    assert coverage.complete, coverage.detail()
    # El presupuesto multimodal es de ocho capturas: una tarea que lo superara no se podría
    # enviar, así que el fixture tiene que caber en el contrato que dice respetar.
    assert len(coverage.expected) <= MAX_IMAGES, (
        f"{len(coverage.expected)} pares ruta x viewport superan el presupuesto multimodal de "
        f"{MAX_IMAGES}"
    )
    assert task.session.status is WebTechnicalStatus.PASS
    return task, images


def test_live_visual_a_model_authentication_works(client: AnthropicClient) -> None:
    """Gate A: la credencial autentica y el modelo visual configurado responde.

    Es la puerta mínima: sin ella todo lo demás sería una suposición. El contrato es trivial a
    propósito —aquí se prueba la autenticación, no la materia visual— pero viaja igualmente como
    structured output, porque el camino que usa el motor es siempre el mismo.
    """
    completion = client.complete_json(
        system_prompt="Respondes solo con un objeto JSON que cumple el esquema entregado.",
        user_prompt='Devuelve exactamente {"ok": true} y nada más.',
        json_schema=AUTH_SCHEMA,
    )

    assert completion.provider == "anthropic"
    # El modelo del informe es el del cliente real. Se compara en igualdad estricta: si el
    # proveedor resolviera otro identificador, el informe estaría atribuyendo la evaluación
    # visual a un modelo que nadie configuró. La corrección de una diferencia es fijar
    # PUNTO_CLAUDE_VISUAL_MODEL al identificador que la cuenta resuelve, nunca relajar esto.
    assert completion.model == client.model
    assert completion.usage.total_tokens > 0
    assert json.loads(completion.content) == {"ok": True}
    print(
        f"\nA. autenticación visual: model={completion.model} "
        f"tokens={completion.usage.total_tokens} stop={completion.stop_reason!r} "
        f"request_id={completion.request_id or '(sin id)'} "
        f"model_id_documented={MODEL_ID_DOCUMENTED} "
        f"live_account_access_unverified={LIVE_ACCOUNT_ACCESS_UNVERIFIED}"
    )


def test_live_visual_b_one_real_png_with_the_production_schema(
    client: AnthropicClient,
) -> None:
    """Gate B: un PNG real más el esquema de producción produce JSON válido tal cual.

    El esquema es ``provider_schema_for(VisualQAProposal)``, el mismo que envía
    ``ClaudeVisualQARunner``: uno trivial podría quedar verde mientras la ruta real de producción
    responde 400, así que aquí no se sustituye por nada. La respuesta se parsea **sin** quitar
    vallas de código, porque limpiarlas escondería precisamente que structured outputs no
    funcionó.
    """
    image = ImagePayload(
        data=png_bytes(390, 844), media_type="image/png", logical_name="home-mobile.png"
    )
    completion = client.complete_multimodal_json(
        system_prompt=VISUAL_SYSTEM_PROMPT,
        user_prompt=ONE_IMAGE_PROMPT,
        images=(image,),
        limits=IMAGE_LIMITS,
        json_schema=provider_schema_for(VisualQAProposal),
    )

    payload = json.loads(completion.content)
    proposal = VisualQAProposal.model_validate(payload)

    assert completion.provider == "anthropic"
    assert completion.usage.total_tokens > 0
    assert proposal.summary.strip()
    print(
        f"\nB. una imagen + esquema de producción: claves={sorted(payload)} "
        f"bytes={image.size_bytes} tokens={completion.usage.total_tokens} "
        f"hallazgos={len(proposal.findings)}"
    )


def test_live_visual_c_multiple_real_images_with_the_production_schema(
    client: AnthropicClient,
) -> None:
    """Gate C: dos capturas reales con el mismo esquema de producción siguen siendo válidas.

    Una sola imagen no demuestra que el contrato multimodal aguante varias: el orden de los
    bloques, la codificación en base64 y la validación del esquema tienen que seguir funcionando
    con más de una captura, que es lo que ocurre en una evaluación real de varios viewports.
    """
    task, images = clean_visual_case(MULTI_IMAGE_VIEWPORTS)
    payloads = tuple(images.values())
    assert len(payloads) >= 2, sorted(images)

    completion = client.complete_multimodal_json(
        system_prompt=VISUAL_SYSTEM_PROMPT,
        user_prompt=MULTI_IMAGE_PROMPT,
        images=payloads,
        limits=IMAGE_LIMITS,
        json_schema=provider_schema_for(VisualQAProposal),
    )

    payload = json.loads(completion.content)
    proposal = VisualQAProposal.model_validate(payload)

    assert completion.provider == "anthropic"
    assert completion.usage.total_tokens > 0
    assert proposal.summary.strip()
    print(
        f"\nC. {len(payloads)} imágenes + esquema de producción: claves={sorted(payload)} "
        f"capturas={sorted(images)} rutas={list(task.spec.routes)} "
        f"tokens={completion.usage.total_tokens} hallazgos={len(proposal.findings)}"
    )


def test_live_visual_d_clean_end_to_end_reaches_pass(client: AnthropicClient) -> None:
    """Gate D: el runner real sobre una tarea limpia llega a PASS calculado por PUNTO.

    El fixture es inequívocamente limpio: sesión técnica en PASS, todos los checks deterministas
    en verde, cobertura completa de la especificación y PNG reales. ``CHANGES_REQUESTED`` y
    ``BLOCKED`` **no** cuentan como éxito: si ocurren, la prueba falla mostrando ``report.error``
    y el detalle de los gates, porque entonces el problema está en el fixture o en la ruta real,
    nunca en la exigencia.
    """
    task, images = clean_visual_case()
    runner = ClaudeVisualQARunner(client=client, image_limits=IMAGE_LIMITS)
    report = runner.evaluate(task, images)

    detail = (
        f"status={report.status.value} error={report.error!r} "
        f"gates={[(gate.name.value, gate.passed, gate.detail) for gate in report.gates]} "
        "hallazgos="
        f"{[(item.id, item.severity.value, item.category.value) for item in report.findings]}"
    )
    assert report.status is VisualQAStatus.PASS, detail

    assert report.provider == "anthropic"
    assert report.model == client.model
    assert report.model_calls > 0
    assert report.model_usage.total_tokens > 0
    assert report.screenshots_analyzed == tuple(images)
    print(
        f"\nD. Visual QA limpio: status={report.status.value} model={report.model} "
        f"llamadas={report.model_calls} intentos={report.attempts} "
        f"tokens={report.model_usage.total_tokens} "
        f"capturas={list(report.screenshots_analyzed)} "
        f"gates={[(gate.name.value, gate.passed) for gate in report.gates]}"
    )
