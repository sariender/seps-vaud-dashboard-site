"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const field = require("../web/temperature-field.js");
const [,, WIDTH, HEIGHT] = field.EXTENT;
const alignment = {
  origin: { longitude: 6, latitude: 46, east_scale: 1000, north_scale: 1000 },
  triangles: [
    { artwork: [[0, 0], [WIDTH, 0], [WIDTH, HEIGHT]], ground: [[0, 0], [1000, 0], [1000, 1000]] },
    { artwork: [[0, 0], [WIDTH, HEIGHT], [0, HEIGHT]], ground: [[0, 0], [1000, 1000], [0, 1000]] },
  ],
};
const linear = (longitude, latitude) => 8 + 2 * (longitude - 6) + 3 * (latitude - 46);
const payload = (reading = linear) => ({ points: [
  [6, 46], [6.5, 46], [7, 46], [6, 46.5], [6.5, 46.5], [7, 46.5], [6, 47], [6.5, 47], [7, 47],
].map(([longitude, latitude]) => ({ longitude, latitude, temperature_c: reading(longitude, latitude) })) });
const almostEqual = (actual, expected, tolerance = 1e-8) => assert.ok(Math.abs(actual - expected) < tolerance, `${actual} != ${expected}`);

test("model interpolation preserves every cell value and linear spatial fields", () => {
  const source = payload();
  const model = field.createField(source, alignment);
  assert.equal(model.sampleCount, 9);
  assert.equal(model.triangleCount, 8);
  for (const point of source.points) almostEqual(model.sampleGeographic(point.longitude, point.latitude), point.temperature_c);
  for (const longitude of [6.01, 6.19, 6.51, 6.99]) {
    for (const latitude of [46.01, 46.31, 46.8, 46.99]) {
      almostEqual(model.sampleGeographic(longitude, latitude), linear(longitude, latitude));
      almostEqual(model.sampleNative((longitude - 6) * WIDTH, (latitude - 46) * HEIGHT), linear(longitude, latitude));
    }
  }
});

test("constant model temperatures stay uniform with no invented local variation", () => {
  const model = field.createField(payload(() => 11.4), alignment);
  for (const [x, y] of [[0, 0], [200, 400], [WIDTH, HEIGHT], [WIDTH / 2, HEIGHT / 2]]) almostEqual(model.sampleNative(x, y), 11.4);
  const raster = field.buildRaster(payload(() => 11.4), alignment, field.EXTENT, { width: 18 });
  assert.ok(raster.scale.max_c - raster.scale.min_c >= 1);
  assert.ok(raster.scale.min_c <= 11.4 && raster.scale.max_c >= 11.4);
  const expected = field.colourAt(11.4, raster.scale);
  for (let index = 0; index < raster.data.length; index += 4) assert.deepEqual([...raster.data.slice(index, index + 3)], expected);
  assert.ok(raster.featheredPixels > 0);
  const centre = (Math.floor(raster.height / 2) * raster.width + Math.floor(raster.width / 2)) * 4;
  assert.equal(raster.data[centre + 3], 255);
});

test("existing control vertices invert to their exact geographic references", () => {
  const actual = JSON.parse(fs.readFileSync(path.join(__dirname, "../web/assets/maps/water-alignment.json"), "utf8"));
  for (const item of actual.triangles) item.artwork.forEach(([x, y], index) => {
    const inverted = field.inverseArtwork(actual, x, y);
    almostEqual(inverted.longitude, actual.origin.longitude + item.ground[index][0] / actual.origin.east_scale);
    almostEqual(inverted.latitude, actual.origin.latitude + item.ground[index][1] / actual.origin.north_scale);
  });
});

test("neither illustration nor model hull is extrapolated", () => {
  const model = field.createField(payload(), alignment);
  assert.equal(model.sampleNative(-1, 10), null);
  assert.equal(model.sampleNative(WIDTH + 1, HEIGHT), null);
  assert.equal(model.sampleGeographic(6, 47.01), null);
  const small = { points: [
    { longitude: 6, latitude: 46, temperature_c: 10 },
    { longitude: 6.5, latitude: 46, temperature_c: 10 },
    { longitude: 6, latitude: 46.5, temperature_c: 10 },
  ] };
  const raster = field.buildRaster(small, alignment, field.EXTENT, { width: 20 });
  assert.ok(raster.supportedPixels > 0 && raster.supportedPixels < raster.width * raster.height);
  const last = raster.data.slice(-4);
  assert.deepEqual([...last], [0, 0, 0, 0]);
  const clipped = { ...alignment, triangles: alignment.triangles.slice(0, 1) };
  const clippedRaster = field.buildRaster(payload(), clipped, field.EXTENT, { width: 20 });
  assert.ok(clippedRaster.supportedPixels < clippedRaster.width * clippedRaster.height);
});

test("calibration coverage fades gradually without changing the model temperatures", () => {
  const model = field.createField(payload(), alignment);
  assert.equal(model.sampleNativeWithCoverage(-1, HEIGHT / 2), null);
  const distances = [0, 20, 40, 60, 80, 100];
  const expectedCoverage = [0, .15625, .5, .84375, 1, 1];
  distances.forEach((x, index) => {
    const sample = model.sampleNativeWithCoverage(x, HEIGHT / 2, 80, 0);
    almostEqual(sample.coverage, expectedCoverage[index]);
    almostEqual(sample.temperature_c, model.sampleNative(x, HEIGHT / 2));
  });
  // Internal interpolation edges retain full coverage: only the exterior fades.
  assert.equal(model.sampleNativeWithCoverage(WIDTH / 2, HEIGHT / 2).coverage, 1);
  const soft = field.buildRaster(payload(), alignment, field.EXTENT, { width: 60 });
  const hard = field.buildRaster(payload(), alignment, field.EXTENT,
    { width: 60, featherNativeUnits: 0, featherModelMetres: 0 });
  assert.deepEqual(soft.scale, hard.scale);
  assert.equal(soft.supportedPixels, hard.supportedPixels);
  assert.ok(soft.featheredPixels > 0);
  assert.deepEqual(soft.edgeFeather, { native_units: 80, model_metres: 200 });
  for (let index = 0; index < soft.data.length; index += 4) {
    assert.deepEqual([...soft.data.slice(index, index + 3)], [...hard.data.slice(index, index + 3)]);
    assert.ok(soft.data[index + 3] <= hard.data[index + 3]);
  }
});

test("a model coverage boundary fades inside its hull and stays transparent beyond it", () => {
  const narrow = { points: [[6, 46], [6.5, 46], [6.5, 47], [6, 47]]
    .map(([longitude, latitude]) => ({ longitude, latitude, temperature_c: 11 })) };
  const model = field.createField(narrow, alignment);
  assert.equal(model.sampleNativeWithCoverage(WIDTH * .501, HEIGHT / 2), null);
  [0, 50, 100, 150, 200].forEach((metres, index) => {
    const sample = model.sampleNativeWithCoverage(metres / 1000 * WIDTH, HEIGHT / 2, 0, 200);
    almostEqual(sample.coverage, [0, .15625, .5, .84375, 1][index]);
    almostEqual(sample.temperature_c, 11);
  });
});

test("legend uses outward half-degree bounds and exactly the texture colour function", () => {
  const scale = field.temperatureScale(10.22, 12.63);
  assert.equal(scale.min_c, 10);
  assert.equal(scale.max_c, 13);
  assert.equal(scale.stops.length, 5);
  scale.stops.forEach((stop, index) => {
    assert.equal(stop.color, field.PALETTE[index]);
    const rgb = field.colourAt(stop.value_c, scale);
    assert.equal("#" + rgb.map(value => value.toString(16).padStart(2, "0")).join(""), stop.color);
  });
  const raster = field.buildRaster(payload(), alignment, [WIDTH / 2, HEIGHT / 2, WIDTH / 2, HEIGHT / 2], { width: 40 });
  assert.ok(raster.visibleMinC >= 10.5 && raster.visibleMaxC <= 13);
  assert.ok(raster.scale.min_c <= raster.visibleMinC && raster.scale.max_c >= raster.visibleMaxC);
  assert.ok(raster.visiblePixels < raster.supportedPixels);
  assert.deepEqual(field.colourAt(100, scale), field.colourAt(scale.max_c, scale));
  assert.deepEqual(field.colourAt(-100, scale), field.colourAt(scale.min_c, scale));
});

test("bad or degenerate data cannot generate a misleading coloured map", () => {
  assert.throws(() => field.createField({ points: [] }, alignment));
  assert.throws(() => field.createField(payload(() => NaN), alignment));
  assert.throws(() => field.createField(payload(() => Infinity), alignment));
  assert.throws(() => field.createField(payload(() => 100), alignment));
  assert.throws(() => field.createField({ points: [{ longitude: 6, latitude: 46, temperature_c: 10 },
    { longitude: 6.1, latitude: 46, temperature_c: 11 }, { longitude: 6.2, latitude: 46, temperature_c: 12 }] }, alignment));
  assert.throws(() => field.createField({ points: [...payload().points, { longitude: 6, latitude: 46, temperature_c: 22 }] }, alignment));
  assert.throws(() => field.buildRaster(payload(), alignment, [WIDTH * 2, HEIGHT * 2, WIDTH, HEIGHT]));
  assert.throws(() => field.temperatureScale(12, 10));
  assert.throws(() => field.createField(payload(), { ...alignment, origin: { ...alignment.origin, east_scale: 0 } }));
  assert.throws(() => field.buildRaster(payload(), alignment, field.EXTENT, { featherNativeUnits: -1 }));
  assert.throws(() => field.buildRaster(payload(), alignment, field.EXTENT, { featherModelMetres: NaN }));
});

test("duplicate identical cells are harmless and each irregular cell remains represented", () => {
  const source = { points: [...payload().points, payload().points[0],
    { longitude: 6.12, latitude: 46.27, temperature_c: linear(6.12, 46.27) },
    { longitude: 6.76, latitude: 46.63, temperature_c: linear(6.76, 46.63) }] };
  const model = field.createField(source, alignment);
  assert.equal(model.sampleCount, 11);
  for (const point of source.points) almostEqual(model.sampleGeographic(point.longitude, point.latitude), point.temperature_c);
});
