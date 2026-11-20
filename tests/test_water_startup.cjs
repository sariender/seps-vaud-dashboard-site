"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const root = path.join(__dirname, "../web");
const source = fs.readFileSync(path.join(root, "illustrated-map.js"), "utf8");
const helper = fs.readFileSync(path.join(root, "dashboard-data.js"), "utf8");
const saved = JSON.parse(fs.readFileSync(path.join(root, "assets/maps/category-scenes.json"), "utf8"));

class Element {
  constructor() {
    this.attributes = new Map();
    this.children = [];
    this.dataset = {};
    this.style = { setProperty() {} };
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  append(...children) { for (const child of children) { child.remove(); child.parent = this; this.children.push(child); } }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this);
    this.parent = null;
  }
  replaceWith(replacement) {
    const parent = this.parent;
    const index = parent.children.indexOf(this);
    replacement.remove();
    parent.children[index] = replacement;
    replacement.parent = parent;
    this.parent = null;
  }
  addEventListener() {}
  getBoundingClientRect() { return { width: 0, height: 0 }; }
}

async function start({ config, water, delivery = "static", now = Date.now() }) {
  const elements = new Map();
  const requests = [];
  const timeouts = [];
  const intervals = [];
  const documentListeners = new Map();
  const windowListeners = new Map();
  const clock = { now };
  const baseURI = "https://sariender.github.io/seps-vaud-dashboard-site/";
  const document = {
    baseURI, documentElement: { dataset: { delivery } }, hidden: false,
    addEventListener: (name, callback) => documentListeners.set(name, callback),
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element());
      return elements.get(id);
    },
    createElement: () => new Element(),
    createElementNS: () => new Element(),
  };
  const context = vm.createContext({
    document, window: { addEventListener: (name, callback) => windowListeners.set(name, callback) }, URL,
    Date: class extends Date { static now() { return clock.now; } },
    Image: class { set src(value) { this.onload(); } },
    ResizeObserver: class { observe() {} },
    AbortSignal: { timeout() {} },
    setTimeout: (callback, ms) => { const timer = { callback, ms }; timeouts.push(timer); return timer; },
    clearTimeout: timer => { timer.cancelled = true; },
    setInterval: (callback, ms) => { const timer = { callback, ms }; intervals.push(timer); return timer; },
    clearInterval: timer => { timer.cancelled = true; },
    fetch: async url => {
      const pathname = new URL(url, baseURI).pathname;
      requests.push(pathname);
      if (pathname.endsWith("category-scenes.json")) return { ok: true, json: async () => structuredClone(saved) };
      if (/\/config(?:\.json)?$/.test(pathname)) {
        if (config instanceof Error) throw config;
        return { ok: true, json: async () => config };
      }
      if (/\/water-points(?:\.json)?$/.test(pathname)) return { ok: true, json: async () => typeof water === "function" ? water() : water };
      throw new Error("Unexpected request: " + pathname);
    },
  });
  vm.runInContext(helper, context);
  vm.runInContext(source, context);
  await new Promise(setImmediate);
  return { state: () => context.window.IllustratedMap.getState(), requests, timeouts, intervals,
    select: id => context.window.IllustratedMap.setScene(id),
    advance: ms => { clock.now += ms; },
    focus: () => windowListeners.get("focus")(),
    visibility: hidden => { document.hidden = hidden; documentListeners.get("visibilitychange")(); } };
}

test("a Pages configuration failure never displays saved water markers", async () => {
  assert.ok(saved.scenes[0].points.length > 0, "regression fixture must contain saved markers");
  const page = await start({ config: new Error("offline") });
  assert.equal(page.state().loadStatus, "ready");
  assert.equal(page.state().sceneId, "points_eau");
  assert.equal(page.state().pointCount, 0);
  assert.equal(page.state().water.mode, "live");
  assert.ok(page.timeouts.some(timer => timer.ms === 60_000));
  assert.ok(!page.requests.some(url => url.endsWith("water-points.json")));
});

test("only confirmed local design mode may show saved water markers", async () => {
  const preview = await start({ config: { mode: "design", water_updates: "snapshot" }, delivery: "dynamic" });
  assert.equal(preview.state().loadStatus, "ready");
  assert.equal(preview.state().pointCount, saved.scenes[0].points.length);
  assert.equal(preview.state().water.mode, "snapshot");
  const live = await start({ config: { mode: "live", water_updates: "snapshot" } });
  assert.equal(live.state().pointCount, 0);
});

test("fresh water snapshots show markers and missing checks remain empty", async () => {
  const checked_at = new Date().toISOString();
  const config = { mode: "live", water_updates: "live", water_refresh_seconds: 3600, water_max_age_seconds: 3680 };
  const fresh = await start({ config, water: { mode: "live", status: "current", checked_at,
    points: [saved.scenes[0].points[0]], refresh_seconds: 3600, max_age_seconds: 3680 } });
  assert.equal(fresh.state().loadStatus, "ready");
  assert.equal(fresh.state().pointCount, 1);
  assert.equal(fresh.state().water.status, "current");
  assert.ok(fresh.requests.some(url => url.endsWith("/api/water-points.json")));
  const missing = await start({ config, water: { mode: "live", status: "unavailable", checked_at: null, points: [] } });
  assert.equal(missing.state().pointCount, 0);
  assert.equal(missing.state().water.status, "unavailable");
});

test("live water expires on its own 15-second check while a network response is missing", async () => {
  const now = Date.now();
  const payload = { mode: "live", status: "current", checked_at: new Date(now).toISOString(),
    points: [saved.scenes[0].points[0]], max_age_seconds: 3680 };
  let replies = 0;
  const page = await start({ now, config: { mode: "live", water_updates: "live" },
    water: () => ++replies === 1 ? payload : new Promise(() => {}) });
  assert.equal(page.state().pointCount, 1);
  page.focus();
  page.focus();
  await new Promise(setImmediate);
  assert.equal(replies, 2, "focus must not start concurrent polls");
  const timersBeforeExpiry = page.intervals.length;
  page.advance(3_680_001);
  const expiry = page.intervals.find(timer => timer.callback.name === "expireLiveWater");
  assert.equal(expiry.ms, 15_000);
  expiry.callback();
  assert.equal(page.state().pointCount, 0);
  assert.equal(page.state().water.status, "unavailable");
  assert.equal(page.state().sceneId, "points_eau");
  assert.equal(page.intervals.length, timersBeforeExpiry, "expiry must not restart category rotation");
});

test("focus, visibility and manual selection immediately hide expired live markers", async t => {
  for (const event of ["focus", "visibility", "selection"]) await t.test(event, async () => {
    const now = Date.now();
    const page = await start({ now, config: { mode: "live", water_updates: "live" },
      water: { mode: "live", status: "current", checked_at: new Date(now).toISOString(),
        points: [saved.scenes[0].points[0]], max_age_seconds: 3680 } });
    page.advance(3_680_001);
    if (event === "focus") page.focus();
    else if (event === "visibility") page.visibility(false);
    else page.select("points_eau");
    assert.equal(page.state().pointCount, 0);
    assert.equal(page.state().water.status, "unavailable");
    await new Promise(setImmediate);
    assert.equal(page.state().pointCount, 0, "an old cached reply cannot renew validity");
  });
});

test("water expiry preserves another selected category and explicit design snapshots", async () => {
  const now = Date.now();
  const live = await start({ now, config: { mode: "live", water_updates: "live" },
    water: { mode: "live", status: "current", checked_at: new Date(now).toISOString(),
      points: [saved.scenes[0].points[0]], max_age_seconds: 3680 } });
  live.select("exterieurs");
  const rotation = live.intervals.at(-1);
  live.advance(3_680_001);
  live.intervals.find(timer => timer.callback.name === "expireLiveWater").callback();
  assert.equal(live.state().sceneId, "exterieurs");
  assert.equal(rotation.cancelled, undefined);
  assert.equal(live.state().water.status, "unavailable");
  live.select("points_eau");
  assert.equal(live.state().pointCount, 0);

  const preview = await start({ now, delivery: "dynamic", config: { mode: "design", water_updates: "snapshot" } });
  preview.advance(86_400_000);
  preview.intervals.find(timer => timer.callback.name === "expireLiveWater").callback();
  preview.focus();
  preview.visibility(false);
  assert.equal(preview.state().water.mode, "snapshot");
  assert.equal(preview.state().pointCount, saved.scenes[0].points.length);
  assert.ok(!preview.requests.some(url => /\/water-points/.test(url)));
});
