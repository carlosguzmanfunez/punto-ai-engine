// Arnés determinista para ejecutar la página REAL del dashboard (dashboard.html) en Node, sin
// navegador. El DOM es un registro construido a partir de los ``id`` que el marcado DECLARA: pedir
// un id que la página no declara devuelve ``null``, exactamente como en el navegador. Es lo que
// permite reproducir el defecto «Cannot set properties of null (setting 'textContent')».
//
//   const { boot } = require("./dashboard_harness.js");
//   const page = boot({ html, routes, dropIds: ["tarea-note"] });
"use strict";

const fs = require("fs");
const vm = require("vm");

function fakeElement(id) {
  const listeners = {};
  return {
    id,
    innerHTML: "",
    textContent: "",
    className: "",
    value: "",
    hidden: false,
    disabled: false,
    dataset: {},
    style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    querySelector: () => null,
    querySelectorAll: () => [],
    closest: () => null,
    setAttribute() {},
    _listeners: listeners,
  };
}

/**
 * @param {object} options
 * @param {string} options.html            contenido de dashboard.html (ya con el marcado a probar)
 * @param {object} options.routes          "METHOD /ruta" -> {status, body} | función async (calls)
 * @param {string[]} [options.dropIds]     ids del marcado que se eliminan (elemento opcional ausente)
 */
function boot({ html, routes, dropIds = [] }) {
  const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
  for (const id of dropIds) ids.delete(id);
  const registry = new Map([...ids].map((id) => [id, fakeElement(id)]));
  const calls = [];
  const notes = [];

  const document = {
    getElementById: (id) => registry.get(id) || null,
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener() {},
    createElement: () => fakeElement("(creado)"),
  };

  async function fetchStub(path, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const key = `${method} ${path}`;
    calls.push(key);
    let handler = routes[key];
    if (handler === undefined) handler = { status: 200, body: {} };
    if (typeof handler === "function") handler = await handler(calls);
    if (handler && handler.throws) throw new TypeError(handler.throws);
    const status = handler.status || 200;
    const text = JSON.stringify(handler.body === undefined ? {} : handler.body);
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: String(status),
      text: async () => text,
    };
  }

  const sandbox = {
    document,
    fetch: fetchStub,
    console: { log() {}, error() {}, warn() {} },
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    clearInterval() {},
    Date,
    Promise,
    JSON,
    Set,
    Map,
    Math,
    String,
    Number,
    Array,
    Object,
    Error,
    TypeError,
    encodeURIComponent,
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);

  const start = html.indexOf("<script>") + "<script>".length;
  const end = html.lastIndexOf("</script>");
  const source = html.slice(start, end);
  // Se registra cada nota de la consola y se exponen los símbolos de la página bajo prueba.
  const wrapped = `${source}
;globalThis.__page = { releaseTask, releaseRequest, releaseOutcome, renderTasks, loadTasks,
  loadGates, consoleState, setText, setHtml, consoleNote, taskCard, loadOperations,
  renderOperations, state, effectiveRow, renderCapabilitySummary, limitedLabel };`;
  process.on("unhandledRejection", () => {});
  vm.runInContext(wrapped, sandbox);

  return {
    page: sandbox.__page,
    element: (id) => registry.get(id) || null,
    calls,
    notes,
    text: (id) => (registry.get(id) ? registry.get(id).textContent : null),
    html: (id) => (registry.get(id) ? registry.get(id).innerHTML : null),
  };
}

function readHtml(path) {
  return fs.readFileSync(path, "utf-8");
}

module.exports = { boot, readHtml };
