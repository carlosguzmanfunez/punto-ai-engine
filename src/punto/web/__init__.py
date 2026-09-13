"""Capa de ejecución web (ENGINE-5.3).

PUNTO puede perfilar un proyecto web del workspace, planificar sus acciones —instalar
dependencias, comprobar tipos, construir, arrancar la vista previa, probar— y capturar
screenshots desde un navegador real que corre dentro del sandbox. Este paquete contiene esa capa;
el contrato de sus artefactos vive en :mod:`punto.schemas.web`.

Los módulos:

- ``detection``: qué proyecto es, leído del workspace y con evidencia de cada hallazgo;
- ``commands``: la traducción de acciones conceptuales a un ``argv`` controlado, sin shell;
- ``sandbox``: la sesión real dentro del contenedor, con el probe y la verificación del host;
- ``checks``: las once comprobaciones deterministas sobre lo observado;
- ``report``: el informe técnico y su estado, calculado por PUNTO.

La capa web **no** depende conceptualmente de Next.js: el framework se detecta y se declara, pero
el perfil, los comandos, el navegador y los checks valen para cualquier stack web.

Igual que el resto de paquetes internos del motor, este ``__init__`` no reexporta nada: importar
``punto.web`` no arrastra la detección ni la política de comandos.
"""

__all__: list[str] = []
