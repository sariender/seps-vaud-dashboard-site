"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const root = path.join(__dirname, "../web");
const scripts = ["dashboard-data.js", "weather-condition.js", "map-temperature.js"]
  .map(name => [name, fs.readFileSync(path.join(root, name), "utf8")]);
const FROZEN_AT = "2026-11-20T15:20:00Z";
const CAPTURED = Date.parse(FROZEN_AT);
const frozenConfig = { mode: "frozen", frozen_at: FROZEN_AT, water_updates: "frozen", heat_updates: "frozen" };

class Element {
  constructor(localName = "g") {
    this.localName = localName;
    this.children = [];
    this.attributes = [];
    this.dataset = {};
    this.style = { setProperty() {} };
    this.hidden = false;
  }
  get id() { return this.getAttribute("id"); }
  set id(value) { this.setAttribute("id", value); }
  getAttribute(name) { return this.attributes.find(attribute => attribute.name === name)?.value ?? null; }
  setAttribute(name, value) {
    let attribute = this.attributes.find(attribute => attribute.name === name);
    if (!attribute) {
      attribute = { name, localName: name, namespaceURI: null };
      this.attributes.push(attribute);
    }
    attribute.value = String(value);
  }
  setAttributeNS(_namespace, name, value) { this.setAttribute(name, value); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  insertBefore(node, next) { this.children.splice(this.children.indexOf(next), 0, node); }
  replaceWith() {}
  cloneNode() {
    const result = new Element(this.localName);
    this.attributes.forEach(({ name, value }) => result.setAttribute(name, value));
    return result;
  }
  getBoundingClientRect() { return { width: 700, height: 600 }; }
  querySelectorAll() { return this.children.flatMap(child => [child, ...child.querySelectorAll("*")]); }
}

// The renderer depends on the original artwork's exact land/lake layer order.
// This small DOM fixture models that contract while leaving pixel interpolation
// to test_temperature_field.cjs and browser visual verification.
function artwork() {
  const svg = new Element("svg");
  const definitions = new Element("defs");
  const source = new Element("g");
  source.id = "source-8";
  for (let index = 0; index <= 130; index++) {
    const shape = new Element("path");
    if (index < 108) shape.setAttribute("fill", "rgb(77.255249%, 91.764832%, 74.510193%)");
    if (index === 108) shape.setAttribute("clip-path", "url(#clip-20)");
    if (index === 115) shape.setAttribute("fill", "rgb(63.529968%, 85.490417%, 97.254944%)");
    source.append(shape);
  }
  svg.append(definitions, source);
  svg.querySelector = selector => selector === "defs" ? definitions : selector === '[id="source-8"]' ? source : null;
  return svg;
}

function payload() {
  return { mode: "live", status: "live", frozen_at: FROZEN_AT,
    valid_at: "2026-11-20T15:00:00Z", fetched_at: "2026-11-20T15:19:00Z", valid_until: "2026-11-20T16:20:20Z",
    unit: "°C", variable: "temperature_2m",
    points: [{ longitude: 6.60, latitude: 46.51, temperature_c: 8 },
      { longitude: 6.63, latitude: 46.52, temperature_c: 9 },
      { longitude: 6.61, latitude: 46.54, temperature_c: 7 }],
    conditions: { weather_code: 63, is_day: true, valid_at: "2026-11-20T15:00:00Z" } };
}

async function start({ config = frozenConfig, data = payload(), frozenAt = FROZEN_AT,
  now = CAPTURED, delivery = "static" } = {}) {
  const elements = new Map();
  const byId = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const requests = [], timeouts = [], intervals = [], listeners = [], resizeCallbacks = [];
  const clock = { now };
  const context = {
    URL, Date: class extends Date { static now() { return clock.now; } },
    document: { baseURI: "https://example.com/dashboard/", documentElement: { dataset: { delivery, frozenAt } },
      getElementById: byId, createElement: name => new Element(name), createElementNS: (_ns, name) => new Element(name),
      querySelector: selector => byId(selector), importNode: node => node,
      addEventListener: (...args) => listeners.push(args) },
    DOMParser: class { parseFromString() { return { documentElement: artwork(), querySelector: () => null }; } },
    AbortSignal: { timeout() {} },
    ResizeObserver: class { constructor(callback) { resizeCallbacks.push(callback); } observe() {} },
    setTimeout: (callback, ms) => { const timer = { callback, ms }; timeouts.push(timer); return timer; },
    clearTimeout: timer => { if (timer) timer.cancelled = true; },
    setInterval: (callback, ms) => { const timer = { callback, ms }; intervals.push(timer); return timer; },
    fetch: async url => {
      const pathname = new URL(url, "https://example.com/dashboard/").pathname;
      requests.push(pathname);
      const value = pathname.endsWith("config.json") ? config
        : pathname.endsWith("map-temperature.json") ? data : {};
      return { ok: true, json: async () => value, text: async () => "<svg />" };
    },
    MapTemperatureField: { buildTexture: () => ({ dataUrl: "data:image/png;base64,fixture", supportedPixels: 100,
      scale: { min_c: 7, max_c: 9, stops: [{ value_c: 7, color: "#00aaaa" }, { value_c: 9, color: "#ffaa00" }] } }) },
  };
  context.window = context;
  const sandbox = vm.createContext(context);
  for (const [name, source] of scripts) vm.runInContext(source, sandbox, { filename: name });
  context.MapTemperature.initialize({ manifest: { background: "map.svg", viewBox: [0, 0, 1190, 1246] },
    background: byId("illustrated-background"), getViewBox: () => [0, 0, 1190, 1246] });
  await new Promise(setImmediate);
  return { context, byId, requests, timeouts, intervals, listeners,
    state: () => context.MapTemperature.getState(), condition: () => context.WeatherCondition.getState(),
    advance: ms => { clock.now += ms; }, resize: () => resizeCallbacks[0]() };
}

test("frozen temperature and condition remain visible on next-day reload and resize without polls", async () => {
  for (const now of [CAPTURED, CAPTURED + 86_400_000]) {
    const page = await start({ now });
    assert.equal(page.state().status, "frozen", JSON.stringify(page.state()));
    assert.equal(page.state().sampleCount, 3);
    assert.equal(page.state().valid_at, payload().valid_at);
    assert.equal(page.condition().status, "frozen");
    assert.equal(page.byId("condition-icon").textContent, "🌧️");
    assert.equal(page.byId("map-temperature-scale").hidden, false);
    assert.match(page.byId("map-temperature-legend").getAttribute("aria-description"), /Snapshot captured 2026-11-20T15:20:00Z/);
    assert.doesNotMatch(page.byId("weather-condition").getAttribute("aria-label"), /checked hourly/);
    assert.equal(page.timeouts.length + page.intervals.length + page.listeners.length, 0);
    page.advance(86_400_000);
    page.resize();
    assert.equal(page.timeouts.length, 1, "only the resize debounce may schedule work");
    assert.equal(page.timeouts[0].ms, 80);
    page.timeouts[0].callback();
    assert.equal(page.state().status, "frozen");
    assert.equal(page.byId("map-temperature-scale").hidden, false);
    assert.equal(page.requests.filter(url => url.endsWith("map-temperature.json")).length, 1);
  }
});

test("mismatched frozen map configuration or payload fails closed and never retries", async () => {
  for (const mismatch of ["config", "payload"]) {
    const page = await start({ config: { ...frozenConfig, ...(mismatch === "config" ? { frozen_at: "2026-11-20T14:20:00Z" } : {}) },
      data: { ...payload(), ...(mismatch === "payload" ? { frozen_at: "2026-11-20T14:20:00Z" } : {}) } });
    assert.equal(page.state().status, "unavailable", mismatch);
    assert.equal(page.byId("map-temperature-scale").hidden, true, mismatch);
    assert.equal(page.byId("condition-icon").textContent, "—", mismatch);
    assert.equal(page.timeouts.length + page.intervals.length + page.listeners.length, 0, mismatch);
    assert.equal(page.requests.filter(url => url.endsWith("map-temperature.json")).length, mismatch === "config" ? 0 : 1);
  }
});

test("a frozen stamp cannot keep data valid if it was already expired at capture", async () => {
  const page = await start({ data: { ...payload(), valid_until: "2026-11-20T15:19:30Z" } });
  assert.equal(page.state().status, "unavailable");
  assert.equal(page.byId("map-temperature-scale").hidden, true);
  assert.equal(page.condition().condition, null);
  assert.equal(page.timeouts.length + page.intervals.length, 0);
});
