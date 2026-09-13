/**
 * capture.cjs — observación de una ruta en un viewport con un navegador REAL.
 *
 * PUNTO AI ENGINE / ENGINE-5.3 (capa de ejecución web).
 *
 * Este script corre **dentro** del contenedor `localhost/punto-sandbox-web:0.1`, con
 * `--network none`, y su única misión es **observar**: navega a una URL local, recoge hechos
 * acotados y escribe (a) un PNG y (b) un JSON con todo lo observado. **No decide** si algo es
 * un fallo: eso lo hacen las comprobaciones deterministas del host
 * (`punto.web.checks.evaluate_web_checks`), que por eso se pueden probar sin abrir un navegador.
 *
 * Uso:
 *   node capture.cjs --url <url-local> --png <ruta.png> --width <px> --height <px>
 *                    --json <ruta.json> [--route <ruta-logica>] [--viewport <NOMBRE>]
 *                    [--marker <spec>]... [--timeout <ms>] [--settle <ms>]
 *
 * `--marker` acepta tres formas, todas deterministas:
 *   `selector:<css>`        el selector debe existir en el documento.
 *   `attr:<nombre>`         al menos un elemento debe llevar ese atributo.
 *   `attr:<nombre>=<valor>` al menos un elemento debe llevar ese atributo con ese valor.
 * Un `spec` que no empiece por `selector:` se interpreta como CSS puro (`attr:` nunca es CSS
 * válido, así que no hay ambigüedad).
 *
 * Garantías de esta capa:
 *   - solo se navega a la URL local recibida: el contenedor no tiene red;
 *   - nada de secretos ni de rutas absolutas del host se escriben en el JSON (las URL se
 *     sanean quitando credenciales);
 *   - todo va acotado a los máximos del contrato (`punto/schemas/web.py`): 50 mensajes de
 *     consola, 25 errores de página, 25 recursos fallidos y 2000 caracteres por texto;
 *   - las claves del JSON son las del contrato (`RouteObservation` / `AccessibilityObservation`)
 *     en snake_case, de modo que el probe de Python pueda validarlas tal cual.
 *
 * El navegador se lanza con `chromiumSandbox: false` y `--no-sandbox` a propósito: dentro de un
 * contenedor rootless con `--cap-drop ALL` y `no-new-privileges` el sandbox setuid de Chromium no
 * puede obtener privilegios. El aislamiento real lo aporta el contenedor, no el navegador.
 */

'use strict';

const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

// --- Límites del contrato (punto/schemas/web.py) -----------------------------
const MAX_CONSOLE_ERRORS = 50;
const MAX_PAGE_ERRORS = 25;
const MAX_FAILED_RESOURCES = 25;
const MAX_TEXT = 2000;

const DEFAULT_TIMEOUT_MS = 20000;
const DEFAULT_SETTLE_MS = 250;

/**
 * Señales de hidratación reconocidas: texto que menciona un fallo de hidratación de un framework.
 *
 * Se declaran aquí como **observación** estructurada; el host decide la gravedad. La lista es
 * deliberadamente corta y literal: una señal amplia produciría falsos positivos, y un check que
 * grita sin motivo deja de ser útil.
 */
const HYDRATION_SIGNALS = [
  'hydration failed',
  'text content does not match',
  'did not match',
  'hydration',
  'minified react error #418',
  'minified react error #423',
  'minified react error #425',
];

/** Mensaje de uso, para fallar con una instrucción en lugar de con un error opaco. */
const USAGE = `uso: node capture.cjs --url <url> --png <ruta> --width <px> --height <px> --json <ruta>`;

/**
 * Recorta un texto al máximo del contrato.
 *
 * @param {unknown} value Valor a convertir y recortar.
 * @returns {string} Texto acotado.
 */
function truncate(value) {
  const text = typeof value === 'string' ? value : String(value === undefined ? '' : value);
  return text.length > MAX_TEXT ? `${text.slice(0, MAX_TEXT)}…` : text;
}

/**
 * Sanea una URL: quita credenciales embebidas y la acota.
 *
 * Un recurso puede declarar `https://usuario:clave@host/...`; ni las credenciales ni una URL
 * kilométrica tienen sitio en un informe.
 *
 * @param {string} value URL observada.
 * @returns {string} URL sin credenciales, acotada.
 */
function sanitizeUrl(value) {
  let url = truncate(value);
  // `[^/]*@` (greedy) borra TODO lo que hay entre el esquema y el último `@` de la autoridad:
  // con `[^/@]*@` una contraseña que contuviera `@` dejaba la cola visible en el informe.
  url = url.replace(/^([a-zA-Z][a-zA-Z0-9+.-]*:\/\/)[^/]*@/, '$1');
  return url;
}

/**
 * Interpreta los argumentos de línea de comandos.
 *
 * @param {string[]} argv Argumentos sin `node` ni el nombre del script.
 * @returns {object} Opciones normalizadas.
 */
function parseArguments(argv) {
  const options = {
    url: '',
    png: '',
    json: '',
    width: 0,
    height: 0,
    route: '/',
    viewport: '',
    markers: [],
    timeoutMs: DEFAULT_TIMEOUT_MS,
    settleMs: DEFAULT_SETTLE_MS,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index];
    const next = () => {
      index += 1;
      if (index >= argv.length) {
        throw new Error(`falta el valor de ${flag}`);
      }
      return argv[index];
    };
    switch (flag) {
      case '--url':
        options.url = next();
        break;
      case '--png':
        options.png = next();
        break;
      case '--json':
        options.json = next();
        break;
      case '--route':
        options.route = next();
        break;
      case '--viewport':
        options.viewport = next();
        break;
      case '--marker':
        options.markers.push(next());
        break;
      case '--timeout': {
        const value = Number.parseInt(next(), 10);
        if (Number.isFinite(value) && value > 0) {
          options.timeoutMs = value;
        }
        break;
      }
      case '--settle': {
        const value = Number.parseInt(next(), 10);
        if (Number.isFinite(value) && value >= 0) {
          options.settleMs = value;
        }
        break;
      }
      case '--width': {
        const value = Number.parseInt(next(), 10);
        if (Number.isFinite(value) && value > 0) {
          options.width = value;
        }
        break;
      }
      case '--height': {
        const value = Number.parseInt(next(), 10);
        if (Number.isFinite(value) && value > 0) {
          options.height = value;
        }
        break;
      }
      default:
        throw new Error(`argumento no reconocido: ${flag}`);
    }
  }

  if (!options.url || !options.png || !options.json || !options.width || !options.height) {
    throw new Error(USAGE);
  }
  return options;
}

/**
 * Escribe el JSON de observaciones, creando el directorio si hace falta.
 *
 * @param {string} target Ruta de destino.
 * @param {object} payload Observaciones.
 */
function writeJson(target, payload) {
  fs.mkdirSync(path.dirname(path.resolve(target)), { recursive: true });
  fs.writeFileSync(target, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
}

/**
 * Observa el documento: métricas, marcadores requeridos, accesibilidad y recorte.
 *
 * Se ejecuta **dentro** de la página. Devuelve hechos, nunca veredictos.
 *
 * @param {string[]} markers Especificaciones de marcadores requeridos.
 * @returns {object} Observaciones del documento.
 */
function collectDocumentFacts(markers) {
  // Los límites se repiten dentro de la función a propósito: `page.evaluate` serializa esta
  // función y la ejecuta en el contexto de la página, donde las constantes del módulo no existen.
  const maxElements = 25;
  const clippingDetails = [];

  /** Identificador lógico y estable de un elemento (nunca revela datos del host). */
  const describe = (element) => {
    const tag = element.tagName ? element.tagName.toLowerCase() : 'desconocido';
    const id = element.id ? `#${element.id}` : '';
    let classes = '';
    if (typeof element.className === 'string' && element.className.trim()) {
      classes = `.${element.className.trim().split(/\s+/).slice(0, 2).join('.')}`;
    }
    return `${tag}${id}${classes}`;
  };

  /** Nombre accesible aproximado de un control, con las señales más gruesas. */
  const accessibleName = (element) => {
    const ariaLabel = element.getAttribute('aria-label');
    if (ariaLabel && ariaLabel.trim()) {
      return ariaLabel.trim();
    }
    const labelledBy = element.getAttribute('aria-labelledby');
    if (labelledBy) {
      const text = labelledBy
        .split(/\s+/)
        .map((id) => {
          const target = document.getElementById(id);
          return target && target.textContent ? target.textContent : '';
        })
        .join(' ')
        .trim();
      if (text) {
        return text;
      }
    }
    const text = (element.textContent || '').trim();
    if (text) {
      return text;
    }
    const title = element.getAttribute('title');
    if (title && title.trim()) {
      return title.trim();
    }
    for (const attribute of ['value', 'alt']) {
      const value = element.getAttribute(attribute);
      if (value && value.trim()) {
        return value.trim();
      }
    }
    return '';
  };

  // --- Imágenes rotas -------------------------------------------------------
  const brokenImages = [];
  for (const image of Array.from(document.images)) {
    const source = image.getAttribute('src') || image.currentSrc || '';
    if (source && image.naturalWidth === 0) {
      brokenImages.push(describe(image));
    }
  }

  // --- Contenido recortado por el viewport ---------------------------------
  // Solo se declara «recorte» cuando el contenido es **inalcanzable**: un contenedor con
  // overflow oculto cuyo contenido desborda. Un desbordamiento visible y desplazable es
  // HORIZONTAL_OVERFLOW, no recorte.
  for (const element of Array.from(document.querySelectorAll('body *'))) {
    if (clippingDetails.length >= maxElements) {
      break;
    }
    const rectangle = element.getBoundingClientRect();
    if (rectangle.width === 0 || rectangle.height === 0) {
      continue;
    }
    const style = window.getComputedStyle(element);
    const hiddenX = style.overflowX === 'hidden' || style.overflowX === 'clip';
    const hiddenY = style.overflowY === 'hidden' || style.overflowY === 'clip';
    if (hiddenX && element.scrollWidth > element.clientWidth + 1) {
      clippingDetails.push(
        `${describe(element)} recorta ${element.scrollWidth - element.clientWidth}px en horizontal`,
      );
      continue;
    }
    if (hiddenY && element.scrollHeight > element.clientHeight + 1) {
      clippingDetails.push(
        `${describe(element)} recorta ${element.scrollHeight - element.clientHeight}px en vertical`,
      );
    }
  }

  // --- Marcadores requeridos -----------------------------------------------
  const missingMarkers = [];
  const presentMarkers = [];
  for (const spec of markers) {
    let found = false;
    try {
      if (spec.startsWith('selector:')) {
        found = document.querySelector(spec.slice('selector:'.length)) !== null;
      } else if (spec.startsWith('attr:')) {
        const body = spec.slice('attr:'.length);
        const separator = body.indexOf('=');
        if (separator === -1) {
          found = document.querySelector(`[${body}]`) !== null;
        } else {
          const name = body.slice(0, separator);
          const expected = body.slice(separator + 1);
          const escaped =
            typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(expected) : expected;
          found = document.querySelector(`[${name}="${escaped}"]`) !== null;
        }
      } else {
        found = document.querySelector(spec) !== null;
      }
    } catch (error) {
      // Un selector inválido no puede fingir que el marcador existe.
      found = false;
    }
    if (found) {
      presentMarkers.push(spec);
    } else {
      missingMarkers.push(spec);
    }
  }

  // --- Accesibilidad -------------------------------------------------------
  const imagesWithoutAlt = [];
  for (const image of Array.from(document.images)) {
    const hasAlt = image.hasAttribute('alt');
    const alt = (image.getAttribute('alt') || '').trim();
    const decorative =
      image.getAttribute('role') === 'presentation' ||
      image.getAttribute('role') === 'none' ||
      image.getAttribute('aria-hidden') === 'true';
    if (!hasAlt || (alt === '' && !decorative)) {
      imagesWithoutAlt.push(describe(image));
    }
  }

  const buttonsWithoutName = [];
  const controls = document.querySelectorAll(
    'button, [role="button"], input[type="submit"], input[type="button"], input[type="image"]',
  );
  for (const control of Array.from(controls)) {
    if (!accessibleName(control)) {
      buttonsWithoutName.push(describe(control));
    }
  }

  const inputsWithoutLabel = [];
  const fields = document.querySelectorAll(
    'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]):not([type="image"]), select, textarea',
  );
  for (const field of Array.from(fields)) {
    const hasLabelElement = field.labels && field.labels.length > 0;
    const hasAriaLabel = Boolean(
      (field.getAttribute('aria-label') || '').trim() ||
        (field.getAttribute('aria-labelledby') || '').trim(),
    );
    const hasTitle = Boolean((field.getAttribute('title') || '').trim());
    if (!hasLabelElement && !hasAriaLabel && !hasTitle) {
      inputsWithoutLabel.push(describe(field));
    }
  }

  const landmarkNames = [
    'main',
    'nav',
    'header',
    'footer',
    'aside',
    'form',
    'section[aria-label]',
    '[role="main"]',
    '[role="navigation"]',
    '[role="banner"]',
    '[role="contentinfo"]',
    '[role="complementary"]',
    '[role="search"]',
    '[role="form"]',
  ];
  const landmarks = [];
  for (const name of landmarkNames) {
    if (document.querySelector(name) !== null) {
      landmarks.push(name);
    }
  }

  const headingLevels = [];
  for (const heading of Array.from(document.querySelectorAll('h1, h2, h3, h4, h5, h6'))) {
    headingLevels.push(Number.parseInt(heading.tagName.slice(1), 10));
  }
  let headingOrderOk = true;
  let previous = 0;
  for (const level of headingLevels) {
    if (previous === 0 ? level > 1 : level > previous + 1) {
      headingOrderOk = false;
    }
    previous = level;
  }

  return {
    brokenImages,
    clippingDetails,
    missingMarkers,
    presentMarkers,
    accessibility: {
      document_title: (document.title || '').trim(),
      html_lang: (document.documentElement.getAttribute('lang') || '').trim(),
      images_without_alt: imagesWithoutAlt,
      buttons_without_name: buttonsWithoutName,
      inputs_without_label: inputsWithoutLabel,
      landmarks,
      heading_order_ok: headingOrderOk,
      // axe-core no está instalado en la imagen (solo Playwright y Chromium): la lista va
      // vacía y el probe lo declara en `notes`. Nunca se inventan violaciones.
      axe_violations: [],
    },
    headingSequence: headingLevels,
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  };
}

/**
 * Ejecuta una captura completa y devuelve el objeto de observaciones.
 *
 * @param {object} options Opciones ya validadas.
 * @returns {Promise<object>} Observaciones de la ruta y el viewport.
 */
async function capture(options) {
  const started = Date.now();
  const consoleErrors = [];
  const pageErrors = [];
  const hydrationSignals = [];
  const failedResources = [];
  const failedUrls = new Set();
  let consoleWarningCount = 0;
  let httpStatus = null;
  let loadError = '';
  let timedOut = false;

  const noteHydration = (text) => {
    const lowered = text.toLowerCase();
    if (HYDRATION_SIGNALS.some((signal) => lowered.includes(signal))) {
      if (!hydrationSignals.includes(truncate(text))) {
        hydrationSignals.push(truncate(text));
      }
    }
  };

  const registerFailedResource = (url, reason, statusCode, resourceType) => {
    const sanitized = sanitizeUrl(url);
    if (failedUrls.has(sanitized) || failedResources.length >= MAX_FAILED_RESOURCES) {
      return;
    }
    failedUrls.add(sanitized);
    failedResources.push({
      url: sanitized,
      reason: truncate(reason),
      status_code: statusCode,
      resource_type: truncate(resourceType),
    });
  };

  const browser = await chromium.launch({
    chromiumSandbox: false,
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
  });

  const result = {
    ok: false,
    route: options.route,
    viewport: options.viewport,
    local_url: sanitizeUrl(options.url),
    http_status: null,
    load_error: '',
    timed_out: false,
    console_errors: consoleErrors,
    console_warning_count: 0,
    page_errors: pageErrors,
    failed_resources: failedResources,
    broken_images: [],
    scroll_width: 0,
    client_width: 0,
    missing_markers: [],
    present_markers: [],
    hydration_signals: hydrationSignals,
    viewport_clipping: [],
    accessibility: null,
    heading_sequence: [],
    browser: `chromium ${browser.version()}`,
    user_agent: '',
    duration_ms: 0,
    error: '',
  };

  let context = null;
  try {
    context = await browser.newContext({
      viewport: { width: options.width, height: options.height },
      deviceScaleFactor: 1,
    });
    const page = await context.newPage();

    page.on('console', (message) => {
      const type = message.type();
      if (type === 'error') {
        if (consoleErrors.length < MAX_CONSOLE_ERRORS) {
          const text = truncate(message.text());
          consoleErrors.push(text);
          noteHydration(text);
        }
      } else if (type === 'warning') {
        consoleWarningCount += 1;
      }
    });

    page.on('pageerror', (error) => {
      const text = truncate(error && error.message ? error.message : String(error));
      if (pageErrors.length < MAX_PAGE_ERRORS) {
        pageErrors.push(text);
        noteHydration(text);
      }
    });

    page.on('requestfailed', (request) => {
      const failure = request.failure();
      registerFailedResource(
        request.url(),
        failure && failure.errorText ? failure.errorText : 'request failed',
        null,
        request.resourceType(),
      );
    });

    page.on('response', (response) => {
      if (response.status() >= 400) {
        registerFailedResource(
          response.url(),
          `HTTP ${response.status()}`,
          response.status(),
          response.request().resourceType(),
        );
      }
    });

    result.user_agent = truncate(await page.evaluate(() => navigator.userAgent));

    try {
      const response = await page.goto(options.url, {
        waitUntil: 'networkidle',
        timeout: options.timeoutMs,
      });
      httpStatus = response ? response.status() : null;
    } catch (error) {
      const message = truncate(error && error.message ? error.message : String(error));
      if (/timeout/i.test(message)) {
        timedOut = true;
      } else {
        loadError = message;
      }
    }

    if (options.settleMs > 0) {
      // Espera corta adicional: `networkidle` no garantiza que el último repintado haya ocurrido.
      await page.waitForTimeout(options.settleMs);
    }

    const facts = await page.evaluate(collectDocumentFacts, options.markers);

    result.http_status = httpStatus;
    result.load_error = loadError;
    result.timed_out = timedOut;
    result.console_warning_count = consoleWarningCount;
    result.broken_images = facts.brokenImages;
    result.scroll_width = facts.scrollWidth;
    result.client_width = facts.clientWidth;
    result.missing_markers = facts.missingMarkers;
    result.present_markers = facts.presentMarkers;
    result.viewport_clipping = facts.clippingDetails;
    result.accessibility = facts.accessibility;
    result.heading_sequence = facts.headingSequence;

    fs.mkdirSync(path.dirname(path.resolve(options.png)), { recursive: true });
    await page.screenshot({ path: options.png, fullPage: false });
    result.ok = true;
  } catch (error) {
    result.error = truncate(error && error.message ? error.message : String(error));
    result.console_warning_count = consoleWarningCount;
  } finally {
    if (context !== null) {
      await context.close().catch(() => {});
    }
    await browser.close().catch(() => {});
  }

  result.duration_ms = Date.now() - started;
  return result;
}

/**
 * Punto de entrada: captura, imprime versiones y escribe el JSON.
 */
async function main() {
  let options = null;
  try {
    options = parseArguments(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exit(2);
  }

  let observations = null;
  try {
    observations = await capture(options);
  } catch (error) {
    observations = {
      ok: false,
      route: options.route,
      viewport: options.viewport,
      local_url: sanitizeUrl(options.url),
      http_status: null,
      load_error: '',
      timed_out: false,
      console_errors: [],
      console_warning_count: 0,
      page_errors: [],
      failed_resources: [],
      broken_images: [],
      scroll_width: 0,
      client_width: 0,
      missing_markers: [],
      present_markers: [],
      hydration_signals: [],
      viewport_clipping: [],
      accessibility: null,
      heading_sequence: [],
      browser: '',
      user_agent: '',
      duration_ms: 0,
      error: truncate(error && error.message ? error.message : String(error)),
    };
  }

  writeJson(options.json, observations);

  process.stdout.write(`capture navegador: ${observations.browser || 'desconocido'}\n`);
  process.stdout.write(`capture user-agent: ${observations.user_agent || 'desconocido'}\n`);
  process.stdout.write(
    `capture resultado: ruta=${observations.route} viewport=${
      observations.viewport || 'sin-nombre'
    } estado=${observations.http_status === null ? 'sin-respuesta' : observations.http_status} ` +
      `overflow=${observations.scroll_width - observations.client_width}px ok=${observations.ok}\n`,
  );

  if (!observations.ok) {
    process.stderr.write(`capture falló: ${observations.error || 'motivo desconocido'}\n`);
    process.exit(1);
  }
  process.exit(0);
}

main().catch((error) => {
  process.stderr.write(`capture falló de forma inesperada: ${error && error.message}\n`);
  process.exit(1);
});
