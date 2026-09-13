"""Contrato de proveedor de modelos, neutral respecto al proveedor (ENGINE-5.2).

El motor hablaba con DeepSeek mediante un cliente concreto. Con dos proveedores reales
—DeepSeek y Anthropic/Claude— el contrato tenía que dejar de vivir dentro de uno de ellos:
si el tipo de retorno y la interfaz pertenecieran a DeepSeek, Claude sería «DeepSeek con otro
nombre», que es justo lo que la fase prohíbe.

Aquí está lo mínimo que un proveedor debe ofrecer:

- ``provider`` y ``model``: quién responde y con qué modelo, declarado, nunca inferido;
- ``complete_json(...)``: una respuesta JSON estructurada con consumo y latencia;
- ``redact(...)``: saneado de credenciales, porque cada proveedor conoce la suya;
- ``ModelCompletion``: el resultado común, con ``provider``, ``stop_reason`` y ``request_id``.

Y lo mínimo que debe ofrecer un proveedor **multimodal**:

- ``ImagePayload``: bytes que controla PUNTO, con su media type y un nombre lógico opcional;
- ``ImageLimits``: validación determinista antes de construir cualquier petición;
- ``complete_multimodal_json(...)``: texto e imágenes, con las imágenes validadas.

Los clientes concretos viven en su propio módulo. DeepSeek reexporta ``ModelCompletion`` desde
aquí para no romper a ningún agente existente.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from punto.schemas.execution import ModelUsage

#: Esquema JSON que el proveedor debe respetar, si su API lo permite.
#:
#: Es una capacidad del **contrato de PUNTO**, no una suposición de que todos los proveedores
#: tengan la misma primitiva: unos la cumplen de forma nativa (Structured Outputs), otros de
#: forma textual. Lo que ninguno puede hacer es ignorarla en silencio.
JsonSchema = Mapping[str, Any]

#: Media types de imagen soportados inicialmente. La lista es corta a propósito: cada
#: formato que se acepta es un formato que hay que saber transportar.
SUPPORTED_IMAGE_MEDIA_TYPES: Final[frozenset[str]] = frozenset(
    {"image/png", "image/jpeg", "image/webp"}
)

#: Máximo de imágenes por petición.
MAX_IMAGES: Final[int] = 8

#: Máximo de bytes por imagen.
MAX_IMAGE_BYTES: Final[int] = 5_000_000

#: Máximo de bytes sumando todas las imágenes.
MAX_TOTAL_IMAGE_BYTES: Final[int] = 15_000_000

#: Proveedores conocidos por el motor. Un proveedor nuevo se declara aquí **y** se
#: implementa; no se acepta un nombre libre.
PROVIDER_DEEPSEEK: Final[str] = "deepseek"
PROVIDER_ANTHROPIC: Final[str] = "anthropic"


class ProviderError(RuntimeError):
    """Base de los errores de un proveedor de modelos."""


class ProviderUnavailableError(ProviderError):
    """El proveedor no está disponible: falta la credencial o el modelo no se puede usar.

    Es un resultado **explícito**. Nunca se traduce en «usar otro proveedor»: si una ruta
    pide Anthropic y Anthropic no está, la operación falla o queda ``BLOCKED``, y jamás se
    responde con DeepSeek como si nada.
    """


class ProviderAuthenticationError(ProviderUnavailableError):
    """La credencial falta o fue rechazada por el proveedor.

    Hereda de :class:`ProviderUnavailableError` a propósito: para el motor, «no hay
    credencial» y «el proveedor no está» son la misma situación operativa —no se puede
    usar— y ninguna de las dos autoriza a responder con otro proveedor.
    """


class ProviderModelUnsupportedError(ProviderError):
    """El modelo solicitado no está soportado por el proveedor."""


class ImageValidationError(ProviderError):
    """Una imagen no cumple los límites declarados."""


@dataclass(frozen=True, slots=True)
class ModelCompletion:
    """Resultado estructurado de una llamada al modelo, sea cual sea el proveedor.

    Los campos comunes son los que PUNTO necesita para auditar: contenido, quién respondió,
    consumo, latencia y reintentos de transporte. ``request_id`` y ``stop_reason`` permiten
    reclamar al proveedor con un identificador real, y ``stop_reason`` es también la señal
    con la que se detecta un truncamiento.
    """

    content: str
    model: str
    usage: ModelUsage
    latency_ms: int
    finish_reason: str = ""
    transport_retries: int = 0
    provider: str = ""
    request_id: str = ""

    @property
    def stop_reason(self) -> str:
        """Motivo de parada con el nombre que usa la API de Anthropic.

        Es el valor **nativo** del proveedor, no una traducción: DeepSeek informa ``length`` y
        Anthropic ``max_tokens`` para el mismo fenómeno. Traducirlos exigiría verificar en vivo
        la semántica de cada uno, y una tabla inventada sería peor que la asimetría declarada.
        Quien necesite saber si hubo truncamiento debe usar el error tipado del proveedor, que
        es donde el motor lo detecta.
        """
        return self.finish_reason


@dataclass(frozen=True, slots=True)
class ImagePayload:
    """Imagen que PUNTO entrega al modelo.

    Los bytes los controla PUNTO. El modelo **nunca** recibe una ruta para leer archivos por
    su cuenta: si algo no está aquí, no existe para el modelo.
    """

    data: bytes
    media_type: str
    logical_name: str = ""

    @property
    def size_bytes(self) -> int:
        """Tamaño real del contenido."""
        return len(self.data)


@dataclass(frozen=True, slots=True)
class ImageLimits:
    """Límites deterministas de una petición multimodal.

    Los valores por defecto son deliberadamente conservadores: una petición multimodal sin
    límites es una petición que nadie ha decidido.
    """

    max_images: int = MAX_IMAGES
    max_image_bytes: int = MAX_IMAGE_BYTES
    max_total_bytes: int = MAX_TOTAL_IMAGE_BYTES
    allowed_media_types: frozenset[str] = SUPPORTED_IMAGE_MEDIA_TYPES

    def __post_init__(self) -> None:
        """Rechaza límites sin sentido al construirlos, no al usarlos."""
        if self.max_images <= 0:
            raise ValueError("max_images debe ser mayor que cero")
        if self.max_image_bytes <= 0:
            raise ValueError("max_image_bytes debe ser mayor que cero")
        if self.max_total_bytes <= 0:
            raise ValueError("max_total_bytes debe ser mayor que cero")
        if not self.allowed_media_types:
            raise ValueError("allowed_media_types no puede estar vacío")
        unsupported = sorted(self.allowed_media_types - SUPPORTED_IMAGE_MEDIA_TYPES)
        if unsupported:
            # Un límite puede **estrechar** lo permitido, nunca ampliarlo: si PUNTO dijera que
            # soporta png/jpeg/webp, aceptar aquí un ``text/plain`` sería contradecirse.
            raise ValueError(
                f"allowed_media_types incluye media types que PUNTO no soporta: "
                f"{', '.join(unsupported)}. Soportados: "
                f"{', '.join(sorted(SUPPORTED_IMAGE_MEDIA_TYPES))}"
            )

    def validate(self, images: Sequence[ImagePayload]) -> tuple[ImagePayload, ...]:
        """Valida la lista completa y la devuelve como tupla inmutable.

        Se comprueba, en este orden: número de imágenes, tamaño de cada una, media type y
        total. El orden importa porque un mensaje de error distinto para el mismo problema
        según el orden de comprobación sería una fuente de confusión.

        Args:
            images: Imágenes entregadas por PUNTO.

        Returns:
            Las mismas imágenes, ya validadas.

        Raises:
            ImageValidationError: si algún límite se incumple.
        """
        if len(images) > self.max_images:
            raise ImageValidationError(
                f"{len(images)} imagen(es) superan el máximo de {self.max_images}"
            )
        total = 0
        for index, image in enumerate(images, start=1):
            size = image.size_bytes
            if size == 0:
                raise ImageValidationError(f"la imagen {index} está vacía")
            if size > self.max_image_bytes:
                raise ImageValidationError(
                    f"la imagen {index} ocupa {size} bytes y el máximo por imagen es "
                    f"{self.max_image_bytes}"
                )
            if image.media_type not in self.allowed_media_types:
                raise ImageValidationError(
                    f"la imagen {index} usa el media type {image.media_type!r}; soportados: "
                    f"{', '.join(sorted(self.allowed_media_types))}"
                )
            total += size
        if total > self.max_total_bytes:
            raise ImageValidationError(
                f"{total} bytes en imágenes superan el máximo total de {self.max_total_bytes}"
            )
        return tuple(images)


class StructuredModelClient(ABC):
    """Proveedor que sabe devolver JSON estructurado.

    El contrato es deliberadamente pequeño: PUNTO no necesita el SDK de nadie, necesita poder
    pedir una respuesta, saber quién respondió, cuánto costó y que se le devuelva saneado.
    """

    @property
    @abstractmethod
    def provider(self) -> str:
        """Identificador del proveedor (``deepseek``, ``anthropic``)."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Modelo configurado para este cliente."""

    @abstractmethod
    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: JsonSchema | None = None,
    ) -> ModelCompletion:
        """Solicita una respuesta JSON estructurada.

        Args:
            system_prompt: Prompt de sistema versionado.
            user_prompt: Petición concreta.
            json_schema: Esquema que la respuesta debe cumplir, si el proveedor puede
                aplicarlo. Un proveedor sin la primitiva nativa puede cumplirlo de forma
                textual; lo que no puede es ignorarlo en silencio.

        Raises:
            ProviderError: cualquier fallo clasificado del proveedor.
        """

    @abstractmethod
    def redact(self, text: str) -> str:
        """Elimina las credenciales que este cliente conoce de un texto."""

    @property
    def supports_images(self) -> bool:
        """True solo si el cliente acepta imágenes.

        Vive en el contrato base —y no solo en el multimodal— para que un consumidor pueda
        preguntarlo sin saber de qué clase concreta se trata.
        """
        return False

    @abstractmethod
    def close(self) -> None:
        """Libera los recursos del cliente.

        Es abstracto a propósito: un cliente que abre una conexión tiene que decir cómo la
        cierra, en vez de heredar un ``pass`` que oculta la fuga.
        """

    def __enter__(self) -> StructuredModelClient:
        """Permite usarlo como gestor de contexto."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Cierra el cliente al salir."""
        self.close()


class MultimodalModelClient(StructuredModelClient):
    """Proveedor que además acepta imágenes junto al texto.

    La validación de límites es **previa** a cualquier construcción de petición: si una
    imagen no cumple, no se llega a preparar nada.
    """

    @property
    def supports_images(self) -> bool:
        """Siempre ``True``: es lo que define a este contrato."""
        return True
    @abstractmethod
    def complete_multimodal_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        images: Sequence[ImagePayload],
        limits: ImageLimits | None = None,
        json_schema: JsonSchema | None = None,
    ) -> ModelCompletion:
        """Solicita una respuesta JSON estructurada a partir de texto e imágenes.

        Args:
            system_prompt: Prompt de sistema versionado.
            user_prompt: Petición concreta en texto.
            images: Imágenes cuyos bytes controla PUNTO.
            limits: Límites a aplicar. Si es ``None`` se usan los seguros por defecto.
            json_schema: Esquema que la respuesta debe cumplir, si el proveedor puede
                aplicarlo.

        Raises:
            ImageValidationError: si una imagen incumple los límites.
            ProviderError: cualquier fallo clasificado del proveedor.
        """


__all__ = [
    "MAX_IMAGES",
    "MAX_IMAGE_BYTES",
    "MAX_TOTAL_IMAGE_BYTES",
    "PROVIDER_ANTHROPIC",
    "PROVIDER_DEEPSEEK",
    "SUPPORTED_IMAGE_MEDIA_TYPES",
    "ImageLimits",
    "ImagePayload",
    "ImageValidationError",
    "JsonSchema",
    "ModelCompletion",
    "MultimodalModelClient",
    "ProviderAuthenticationError",
    "ProviderError",
    "ProviderModelUnsupportedError",
    "ProviderUnavailableError",
    "StructuredModelClient",
]
