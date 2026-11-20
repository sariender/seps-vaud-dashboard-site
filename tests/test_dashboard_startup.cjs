"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const appSource = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");
const dataSource = fs.readFileSync(path.join(__dirname, "../web/dashboard-data.js"), "utf8");
const savedPreview = JSON.parse(fs.readFileSync(path.join(__dirname, "../web/design-snapshot.json"), "utf8"));
const response = data => ({ ok: true, json: async () => data });

async function startDashboard(respond, { baseURI = "https://dashboard.test/", delivery, frozenAt, now = Date.now() } = {}) {
  const elements = new Map();
  const requests = [];
  const timeouts = [];
  const intervals = [];
  const listeners = [];
  const gaugeValues = [];
  const clock = { now };
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      textContent: "", dataset: {}, attributes: {}, style: { setProperty() {} },
      setAttribute(name, value) { this.attributes[name] = value; },
      removeAttribute(name) { delete this.attributes[name]; },
    });
    return elements.get(id);
  };
  const context = {
    document: { getElementById: element, addEventListener: (...args) => listeners.push(args), hidden: false,
      baseURI, documentElement: { dataset: { delivery, frozenAt } } },
    window: { addEventListener: (...args) => listeners.push(args) },
    URL,
    Date: class extends Date { static now() { return clock.now; } },
    DialGauge: class { setValue(value) { gaugeValues.push(value); } },
    fetch: async url => {
      const pathname = new URL(url, baseURI).pathname;
      requests.push(pathname);
      return respond(pathname, requests);
    },
    AbortSignal: { timeout: () => ({}) },
    setTimeout: (callback, ms) => timeouts.push({ callback, ms }),
    setInterval: (callback, ms) => intervals.push({ callback, ms }),
  };
  const sandbox = vm.createContext(context);
  vm.runInContext(dataSource, sandbox, { filename: "dashboard-data.js" });
  await vm.runInContext(appSource, sandbox, { filename: "app.js" });
  await new Promise(setImmediate);
  return { element, requests, timeouts, intervals, listeners, gaugeValues, advance: ms => { clock.now += ms; } };
}

test("failed or malformed configuration never loads saved observations", async t => {
  const failures = [
    ["network failure", () => { throw new Error("offline"); }],
    ["HTTP failure", () => ({ ok: false })],
    ["invalid JSON", () => ({ ok: true, json: async () => { throw new Error("bad JSON"); } })],
    ["unrecognized mode", () => response({ mode: "preview" })],
    ["missing mode", () => response({})],
  ];
  for (const [name, fail] of failures) await t.test(name, async () => {
    const page = await startDashboard(url => url === "/api/config" ? fail() : response(savedPreview));
    assert.deepEqual(page.requests, ["/api/config"]);
    assert.equal(page.element("board").dataset.mode, "unknown");
    assert.equal(page.element("board").dataset.state, "unavailable");
    for (const id of ["temperature-value", "humidity-value", "wind-value"]) {
      assert.equal(page.element(id).textContent, "—");
    }
    assert.deepEqual(page.gaugeValues, [null]);
    assert.equal(page.element("risk-label").textContent, "No fresh data");
    assert.equal(page.element("recommendation").textContent, "Current activity guidance needs a fresh observation.");
    assert.equal(page.timeouts.length, 1);
    assert.equal(page.timeouts[0].ms, 60_000);
    assert.equal(page.intervals.length, 0);
    assert.equal(page.listeners.length, 0);
  });
});

test("a frozen weather export loads once and preserves capture time on a next-day reload", async () => {
  const frozenAt = "2026-11-20T15:20:00Z";
  const captured = Date.parse(frozenAt);
  const weather = { ...savedPreview, mode: "live", status: "live", frozen_at: frozenAt,
    fetched_at: frozenAt, valid_until: new Date(captured + 3680_000).toISOString(),
    expires_at: new Date(captured + 7_200_000).toISOString(),
    observation: { ...savedPreview.observation, observed_at: "2026-11-20T15:10:00Z" } };
  const config = { mode: "frozen", frozen_at: frozenAt, water_updates: "frozen", heat_updates: "frozen" };
  for (const now of [captured, captured + 86_400_000]) {
    const page = await startDashboard(url => response(url.endsWith("config.json") ? config : weather),
      { delivery: "static", frozenAt, now });
    assert.deepEqual(page.requests, ["/api/config.json", "/api/weather.json"]);
    assert.equal(page.element("board").dataset.mode, "frozen");
    assert.equal(page.element("board").dataset.state, "live");
    assert.equal(page.element("temperature-value").textContent, weather.observation.air_temperature_c.toFixed(1));
    assert.equal(page.element("clock").textContent, "16:20");
    assert.equal(page.element("status-text").textContent, "Snapshot");
    page.advance(86_400_000);
    assert.equal(page.element("clock").textContent, "16:20");
    assert.deepEqual(page.intervals, []);
    assert.deepEqual(page.timeouts, []);
    assert.deepEqual(page.listeners, []);
  }
});

test("frozen configuration and payload mismatches fail closed without retries", async () => {
  const frozenAt = "2026-11-20T15:20:00Z";
  const config = { mode: "frozen", frozen_at: frozenAt, water_updates: "frozen", heat_updates: "frozen" };
  for (const change of ["config stamp", "config mode", "payload stamp"]) {
    const badConfig = { ...config, ...(change === "config stamp" ? { frozen_at: "2026-11-20T14:20:00Z" }
      : change === "config mode" ? { mode: "live" } : {}) };
    const weather = { ...savedPreview, mode: "live", status: "live", frozen_at: "2026-11-20T14:20:00Z",
      fetched_at: frozenAt, valid_until: "2026-11-20T16:21:20Z", expires_at: "2026-11-20T17:20:00Z",
      observation: { ...savedPreview.observation, observed_at: "2026-11-20T15:10:00Z" } };
    const page = await startDashboard(url => response(url.endsWith("config.json") ? badConfig : weather),
      { delivery: "static", frozenAt, now: Date.parse(frozenAt) });
    assert.equal(page.element("temperature-value").textContent, "—", change);
    assert.equal(page.gaugeValues.at(-1), null, change);
    assert.equal(page.intervals.length + page.timeouts.length + page.listeners.length, 0, change);
    assert.equal(page.requests.length, change === "payload stamp" ? 2 : 1, change);
  }
});

test("the actual saved preview remains available after confirmed design configuration", async () => {
  const page = await startDashboard(url => response(url === "/api/config" ? { mode: "design" } : savedPreview));
  assert.deepEqual(page.requests, ["/api/config", "/design-snapshot.json"]);
  assert.equal(page.element("board").dataset.mode, "design");
  assert.equal(page.element("board").dataset.state, "preview");
  assert.equal(page.element("temperature-value").textContent, savedPreview.observation.air_temperature_c.toFixed(1));
  assert.deepEqual(page.gaugeValues, [savedPreview.observation.wbgt_c]);
  assert.equal(page.intervals.length, 0);
  assert.equal(page.timeouts.length, 0);
});

test("a failed configuration retries and starts live polling once after recovery", async () => {
  let configurationCalls = 0;
  const now = Date.now();
  const weather = {
    mode: "live", status: "live", risk: { id: "low" },
    fetched_at: new Date(now).toISOString(),
    valid_until: new Date(now + 3_600_000).toISOString(),
    expires_at: new Date(now + 7_200_000).toISOString(),
    observation: { observed_at: new Date(now).toISOString(), wbgt_c: 6.4,
      air_temperature_c: 8.3, relative_humidity_percent: 80, wind_speed_m_s: 2 },
  };
  const page = await startDashboard(url => {
    if (url === "/api/config") {
      configurationCalls++;
      if (configurationCalls === 1) throw new Error("temporary outage");
      return response({ mode: "live", weather_poll_seconds: 60 });
    }
    if (url === "/api/weather") return response(weather);
    return response(savedPreview);
  });
  await page.timeouts[0].callback();
  await new Promise(setImmediate);
  assert.deepEqual(page.requests, ["/api/config", "/api/config", "/api/weather"]);
  assert.equal(page.element("board").dataset.mode, "live");
  assert.equal(page.element("board").dataset.state, "live");
  assert.equal(page.element("temperature-value").textContent, "8.3");
  assert.deepEqual(page.gaugeValues, [null, 6.4]);
  assert.deepEqual(page.intervals.map(timer => timer.ms), [1_000, 60_000, 15_000]);
  assert.equal(page.listeners.length, 4);
});

test("a Pages project uses snapshot endpoints but still rejects expired weather", async () => {
  const base = "/seps-vaud-dashboard-site/api/";
  const now = Date.now();
  const expired = {
    ...savedPreview, mode: "live", status: "live",
    fetched_at: new Date(now - 7_200_000).toISOString(),
    valid_until: new Date(now - 3_600_000).toISOString(),
    expires_at: new Date(now - 1_000).toISOString(),
    observation: { ...savedPreview.observation, observed_at: new Date(now - 7_800_000).toISOString() },
  };
  const page = await startDashboard(url => response(url === base + "config.json"
    ? { mode: "live", weather_poll_seconds: 60 } : expired),
  { baseURI: "https://sariender.github.io/seps-vaud-dashboard-site/", delivery: "static" });
  assert.deepEqual(page.requests, [base + "config.json", base + "weather.json"]);
  assert.equal(page.element("board").dataset.mode, "live");
  assert.equal(page.element("temperature-value").textContent, "—");
  assert.deepEqual(page.gaugeValues, [null]);
  assert.equal(page.element("recommendation").textContent, "Current activity guidance needs a fresh observation.");
});
