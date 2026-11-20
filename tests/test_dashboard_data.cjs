"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../web/dashboard-data.js"), "utf8");

const NOW = 1_800_000_000_000;

function endpoints(baseURI, delivery, now = () => NOW) {
  const context = { URL, Date: { now }, document: { baseURI, documentElement: { dataset: { delivery } } } };
  vm.runInNewContext(source, context);
  return context.DashboardData;
}

test("static endpoints stay beneath the GitHub Pages project path", () => {
  for (const page of ["https://sariender.github.io/seps-vaud-dashboard-site/",
    "https://sariender.github.io/seps-vaud-dashboard-site/index.html?screen=1#map"]) {
    const data = endpoints(page, "static");
    for (const name of ["config", "weather", "water-points", "map-temperature"]) {
      assert.equal(data.api(name), `https://sariender.github.io/seps-vaud-dashboard-site/api/${name}.json?v=${Math.floor(NOW / 60_000)}`);
    }
    assert.ok(Object.isFrozen(data));
  }
});

test("static snapshot requests change cache keys each minute without changing endpoints", () => {
  let now = NOW;
  const data = endpoints("https://sariender.github.io/seps-vaud-dashboard-site/", "static", () => now);
  const first = new URL(data.api("weather"));
  now += 60_000;
  const second = new URL(data.api("weather"));
  assert.equal(first.pathname, second.pathname);
  assert.equal(Number(second.searchParams.get("v")), Number(first.searchParams.get("v")) + 1);
  assert.equal(second.origin, first.origin);
});

test("local Python endpoints keep their existing paths", () => {
  for (const page of ["http://127.0.0.1:8080/", "http://127.0.0.1:8080/index.html"]) {
    for (const delivery of [undefined, "dynamic"]) {
      const data = endpoints(page, delivery);
      assert.equal(data.api("weather"), "http://127.0.0.1:8080/api/weather");
      assert.equal(data.api("config"), "http://127.0.0.1:8080/api/config");
    }
  }
});

test("endpoint names cannot select arbitrary files or remote URLs", () => {
  const data = endpoints("https://sariender.github.io/seps-vaud-dashboard-site/", "static");
  for (const name of ["../archive", "/api/weather", "weather?refresh=1", "https://example.com/", "//example.com/", "weather.json", "", null, {}]) {
    assert.throws(() => data.api(name), /Unknown dashboard endpoint/);
  }
  assert.throws(() => endpoints("https://example.com/", "preview"), /Invalid data delivery/);
});

test("HTML and CSS local assets use existing project-relative files", () => {
  const root = path.join(__dirname, "../web");
  const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
  const css = fs.readFileSync(path.join(root, "styles.css"), "utf8");
  const assets = [...html.matchAll(/(?:src|href)="([^"]+)"/g)].map(match => match[1])
    .concat([...css.matchAll(/url\("([^"]+)"\)/g)].map(match => match[1]))
    .filter(value => !/^(?:https?:|data:|#)/.test(value));
  assert.ok(assets.length > 8);
  for (const asset of assets) {
    assert.ok(!asset.startsWith("/"), asset);
    assert.ok(fs.existsSync(path.join(root, asset)), asset);
  }
  assert.ok(html.indexOf('src="dashboard-data.js"') < html.indexOf('src="map-temperature.js"'));
});
