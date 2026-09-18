"""Suite de QA CONSUMER v0: navegador real, aplicación real y veredicto de consumidor.

El comando único es ``pytest tests/consumer_qa``: arranca la aplicación de referencia en el sandbox
web, la abre con Chromium, interactúa y compara lo observado con lo esperado, conservando la captura
como evidencia.
"""

from consumer_qa.support import FIXTURE_APP, PREVIEW_ARGV, SESSION_TIMEOUT_SECONDS, caso, visible

__all__ = ["FIXTURE_APP", "PREVIEW_ARGV", "SESSION_TIMEOUT_SECONDS", "caso", "visible"]
