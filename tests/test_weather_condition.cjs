"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const condition = require("../web/weather-condition.js");
const now = Date.parse("2026-11-19T21:20:00Z");

function payload() {
  return { mode: "live", status: "live", valid_at: "2026-11-19T21:00:00Z",
    fetched_at: "2026-11-19T21:15:00Z", valid_until: "2026-11-19T22:16:20Z",
    conditions: { weather_code: 0, is_day: false, valid_at: "2026-11-19T21:00:00Z" } };
}

test("clear conditions use a moon at night and a sun only in daylight", () => {
  assert.deepEqual(condition.describe(0, true), { label: "Sunny", icon: "☀️" });
  assert.deepEqual(condition.describe(0, false), { label: "Clear", icon: "🌙" });
  assert.equal(condition.describe(1, false).icon, "🌙");
  assert.equal(condition.describe(53, false).icon, "🌧️");
  assert.equal(condition.describe(80, false).icon, "🌧️");
});

test("rain, cloud, snow, fog and storms have distinct meaningful descriptions", () => {
  assert.equal(condition.describe(3, true).label, "Cloudy");
  assert.equal(condition.describe(63, false).label, "Rain");
  assert.equal(condition.describe(73, false).label, "Snow");
  assert.equal(condition.describe(45, true).label, "Fog");
  assert.equal(condition.describe(97, false).label, "Heavy storms");
  assert.notEqual(condition.describe(63, false).icon, condition.describe(73, false).icon);
});

test("unknown or malformed codes cannot invent sunny conditions", () => {
  for (const code of [null, undefined, NaN, Infinity, "0", true, -1, 4, 100]) {
    assert.equal(condition.describe(code, true), null);
  }
  assert.equal(condition.describe(0, 1), null);
});

test("selection requires a fresh shared model hour and preserves that hour", () => {
  const result = condition.select(payload(), now);
  assert.equal(result.label, "Clear");
  assert.equal(result.is_day, false);
  assert.equal(result.valid_at, payload().valid_at);
  const deadline = Date.parse(payload().valid_until);
  assert.ok(condition.select(payload(), deadline));
  assert.equal(condition.select(payload(), deadline + 1), null);
});

test("future clocks, source-hour mismatches and invalid deadlines hide conditions", () => {
  const cases = [
    data => { data.conditions.valid_at = "2026-11-19T20:00:00Z"; },
    data => { data.valid_until = data.fetched_at; },
    data => { data.valid_until = "2026-11-21T22:16:20Z"; },
    data => { data.fetched_at = "2026-11-19T21:25:00Z"; },
    data => { data.fetched_at = "2026-11-19T20:59:00Z"; },
    data => { data.valid_at = "invalid"; },
    data => { data.conditions.weather_code = 200; },
    data => { data.conditions.is_day = null; },
  ];
  for (const change of cases) {
    const data = payload(); change(data);
    assert.equal(condition.select(data, now), null);
  }
  assert.equal(condition.select(payload(), Date.parse("2026-11-19T20:00:00Z")), null);
});

test("missing, paused and expired data never show a current weather condition", () => {
  assert.equal(condition.select({ ...payload(), conditions: null }, now), null);
  for (const status of ["disabled", "loading", "stale", "unavailable"]) {
    assert.equal(condition.select({ ...payload(), status }, now), null);
  }
  assert.equal(condition.select({ ...payload(), mode: "disabled" }, now), null);
});
