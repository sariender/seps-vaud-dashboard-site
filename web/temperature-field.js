/* Numeric model temperatures on the existing schematic illustration.
 * Interpolation smooths model cells for display; it adds no weather resolution.
 */
(function (root) {
  "use strict";

  const EXTENT = Object.freeze([0, 0, 1190.574219, 1246.480469]);
  const PALETTE = Object.freeze(["#44adcc", "#69c586", "#e7d850", "#ec9440", "#df622e"]);
  const RGB = PALETTE.map(hex => [1, 3, 5].map(offset => parseInt(hex.slice(offset, offset + 2), 16)));
  const EPSILON = 1e-9;
  const FEATHER_NATIVE_UNITS = 80;
  const FEATHER_MODEL_METRES = 200;

  function finite(value) { return typeof value === "number" && Number.isFinite(value); }

  function triangle(vertices, values = null) {
    const [[ax, ay], [bx, by], [cx, cy]] = vertices;
    const denominator = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy);
    if (!finite(denominator) || Math.abs(denominator) < 1e-12) return null;
    return {
      vertices, values, denominator,
      bounds: [Math.min(ax, bx, cx), Math.min(ay, by, cy), Math.max(ax, bx, cx), Math.max(ay, by, cy)],
    };
  }

  function weightsAt(item, x, y) {
    const [left, top, right, bottom] = item.bounds;
    if (x < left - EPSILON || x > right + EPSILON || y < top - EPSILON || y > bottom + EPSILON) return null;
    const [[ax, ay], [bx, by], [cx, cy]] = item.vertices;
    const a = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / item.denominator;
    const b = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / item.denominator;
    const c = 1 - a - b;
    return Math.min(a, b, c) >= -EPSILON ? [a, b, c] : null;
  }

  function boundaryEdges(triangles) {
    const edges = new Map();
    for (const item of triangles) {
      for (let index = 0; index < 3; index++) {
        const start = item.vertices[index], end = item.vertices[(index + 1) % 3];
        const key = [start.join(":"), end.join(":")].sort().join("/");
        const previous = edges.get(key);
        if (previous) previous.count++;
        else edges.set(key, { start, end, count: 1 });
      }
    }
    return [...edges.values()].filter(edge => edge.count === 1);
  }

  function distanceToBoundary(edges, x, y) {
    let minimum = Infinity;
    for (const { start: [ax, ay], end: [bx, by] } of edges) {
      const dx = bx - ax, dy = by - ay;
      const fraction = Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)));
      minimum = Math.min(minimum, (x - ax - fraction * dx) ** 2 + (y - ay - fraction * dy) ** 2);
    }
    return Math.sqrt(minimum);
  }

  function smoothCoverage(distance, width) {
    if (!width) return 1;
    const fraction = Math.max(0, Math.min(1, distance / width));
    return fraction * fraction * (3 - 2 * fraction);
  }

  function validateAlignment(alignment) {
    const origin = alignment?.origin;
    if (!origin || ![origin.longitude, origin.latitude, origin.east_scale, origin.north_scale].every(finite)
        || origin.east_scale <= 0 || origin.north_scale <= 0 || !Array.isArray(alignment.triangles)
        || !alignment.triangles.length) throw new Error("Invalid illustrated-map alignment.");
    const vertices = new Map();
    alignment.triangles.forEach(item => {
      if (![item.artwork, item.ground].every(vertices => Array.isArray(vertices) && vertices.length === 3
          && vertices.every(vertex => Array.isArray(vertex) && vertex.length === 2 && vertex.every(finite)))) {
        throw new Error("Invalid illustrated-map alignment triangle.");
      }
      if (!triangle(item.artwork, item.ground)) throw new Error("Degenerate illustrated-map alignment triangle.");
      item.artwork.forEach(([x, y], index) => {
        const key = `${x}:${y}`, previous = vertices.get(key), ground = item.ground[index];
        if (previous && Math.hypot(previous.ground[0] - ground[0], previous.ground[1] - ground[1]) > 1e-6) {
          throw new Error("Conflicting coordinates for one illustrated-map control.");
        }
        vertices.set(key, { artwork: [x, y], ground });
      });
    });
    // The water updater triangulates in geographic space. Its schematic output
    // can overlap after warping, so the inverse must triangulate in artwork
    // space. Reuse exactly the same reference vertices, without new anchors.
    const points = [...vertices.values()];
    const triangles = delaunay(points.map(point => point.artwork))
      .map(indices => triangle(indices.map(index => points[index].artwork), indices.map(index => points[index].ground)))
      .filter(Boolean);
    if (!triangles.length) throw new Error("Illustrated-map controls do not span an area.");
    return { origin, triangles };
  }

  function groundAt(compiled, x, y) {
    for (const item of compiled.triangles) {
      const weights = weightsAt(item, x, y);
      if (weights) return [0, 1].map(axis => weights.reduce((sum, weight, index) => sum + weight * item.values[index][axis], 0));
    }
    return null;
  }

  function inverseArtwork(alignment, x, y) {
    if (![x, y].every(finite)) return null;
    const compiled = validateAlignment(alignment);
    const ground = groundAt(compiled, x, y);
    return ground ? {
      longitude: compiled.origin.longitude + ground[0] / compiled.origin.east_scale,
      latitude: compiled.origin.latitude + ground[1] / compiled.origin.north_scale,
      ground,
    } : null;
  }

  // Bowyer-Watson Delaunay triangulation. Normalising coordinates keeps circle
  // predicates stable for both metre coordinates and small synthetic tests.
  function delaunay(vertices) {
    const xs = vertices.map(point => point[0]), ys = vertices.map(point => point[1]);
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
    const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
    if (!finite(span) || span <= 0) return [];
    const points = vertices.map(([x, y]) => [(x - cx) / span, (y - cy) / span]);
    const count = points.length;
    points.push([-20, -10], [0, 20], [20, -10]);

    function circle(indices) {
      const [[ax, ay], [bx, by], [cx, cy]] = indices.map(index => points[index]);
      const d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by));
      if (Math.abs(d) < 1e-14) return null;
      const aa = ax * ax + ay * ay, bb = bx * bx + by * by, cc = cx * cx + cy * cy;
      const x = (aa * (by - cy) + bb * (cy - ay) + cc * (ay - by)) / d;
      const y = (aa * (cx - bx) + bb * (ax - cx) + cc * (bx - ax)) / d;
      return { indices, x, y, radius2: (x - ax) ** 2 + (y - ay) ** 2 };
    }

    let triangles = [circle([count, count + 1, count + 2])];
    for (let index = 0; index < count; index++) {
      const [x, y] = points[index];
      const bad = new Set(triangles.filter(item => (x - item.x) ** 2 + (y - item.y) ** 2 <= item.radius2 + 1e-12));
      const edges = new Map();
      for (const item of bad) {
        for (let edge = 0; edge < 3; edge++) {
          const a = item.indices[edge], b = item.indices[(edge + 1) % 3];
          const key = a < b ? `${a}:${b}` : `${b}:${a}`;
          const entry = edges.get(key);
          if (entry) entry.count++;
          else edges.set(key, { a, b, count: 1 });
        }
      }
      triangles = triangles.filter(item => !bad.has(item));
      for (const edge of edges.values()) {
        if (edge.count !== 1) continue;
        const item = circle([edge.a, edge.b, index]);
        if (item) triangles.push(item);
      }
    }
    return triangles.filter(item => item.indices.every(index => index < count)).map(item => item.indices);
  }

  function createField(payload, alignment) {
    const compiled = validateAlignment(alignment);
    if (!Array.isArray(payload?.points) || payload.points.length < 3 || payload.points.length > 500) {
      throw new Error("At least three model cells are needed for a temperature field.");
    }
    const unique = new Map();
    for (const point of payload.points) {
      if (![point.longitude, point.latitude, point.temperature_c].every(finite)
          || Math.abs(point.longitude) > 180 || Math.abs(point.latitude) > 90
          || point.temperature_c < -90 || point.temperature_c > 65) throw new Error("Invalid model temperature cell.");
      const key = `${point.longitude}:${point.latitude}`;
      if (unique.has(key) && Math.abs(unique.get(key).temperature_c - point.temperature_c) > 1e-8) {
        throw new Error("Conflicting temperatures for one model cell.");
      }
      unique.set(key, point);
    }
    const points = [...unique.values()];
    const ground = points.map(point => [
      (point.longitude - compiled.origin.longitude) * compiled.origin.east_scale,
      (point.latitude - compiled.origin.latitude) * compiled.origin.north_scale,
    ]);
    const triangles = delaunay(ground).map(indices => triangle(indices.map(index => ground[index]), indices.map(index => points[index].temperature_c))).filter(Boolean);
    if (!triangles.length) throw new Error("Model cells do not span a temperature field.");
    const nativeBoundary = boundaryEdges(compiled.triangles);
    const modelBoundary = boundaryEdges(triangles);
    function sampleGround(x, y) {
      for (const item of triangles) {
        const weights = weightsAt(item, x, y);
        if (!weights) continue;
        const result = weights.reduce((sum, weight, index) => sum + weight * item.values[index], 0);
        return Math.max(Math.min(...item.values), Math.min(Math.max(...item.values), result));
      }
      return null;
    }
    return {
      sampleCount: points.length, triangleCount: triangles.length,
      sampleGeographic(longitude, latitude) {
        if (![longitude, latitude].every(finite)) return null;
        return sampleGround((longitude - compiled.origin.longitude) * compiled.origin.east_scale,
          (latitude - compiled.origin.latitude) * compiled.origin.north_scale);
      },
      sampleNative(x, y) {
        if (![x, y].every(finite)) return null;
        const ground = groundAt(compiled, x, y);
        return ground ? sampleGround(...ground) : null;
      },
      sampleNativeWithCoverage(x, y, nativeWidth = FEATHER_NATIVE_UNITS, modelWidth = FEATHER_MODEL_METRES) {
        if (![x, y].every(finite)) return null;
        const ground = groundAt(compiled, x, y);
        if (!ground) return null;
        const temperature = sampleGround(...ground);
        if (temperature === null) return null;
        // This is a coverage fade, not a colder temperature band. Keep every
        // numeric value unchanged and soften opacity only inside the two hulls.
        const coverage = Math.min(smoothCoverage(distanceToBoundary(nativeBoundary, x, y), nativeWidth),
          smoothCoverage(distanceToBoundary(modelBoundary, ...ground), modelWidth));
        return { temperature_c: temperature, coverage };
      },
    };
  }

  function temperatureScale(low, high) {
    if (![low, high].every(finite) || high < low) throw new Error("Invalid temperature scale.");
    let min = Math.floor(low * 2) / 2, max = Math.ceil(high * 2) / 2;
    if (max - min < 1) {
      min = Math.round(low + high) / 2 - .5;
      max = min + 1;
    }
    return { min_c: min, max_c: max,
      stops: PALETTE.map((color, index) => ({ value_c: min + index * (max - min) / (PALETTE.length - 1), color })) };
  }

  function colourAt(value, scale) {
    const position = Math.max(0, Math.min(1, (value - scale.min_c) / (scale.max_c - scale.min_c))) * (RGB.length - 1);
    const index = Math.min(RGB.length - 2, Math.floor(position));
    const amount = position - index;
    return RGB[index].map((channel, axis) => Math.round(channel + amount * (RGB[index + 1][axis] - channel)));
  }

  function buildRaster(payload, alignment, viewBox, options = {}) {
    if (!Array.isArray(viewBox) || viewBox.length !== 4 || !viewBox.every(finite) || viewBox[2] <= 0 || viewBox[3] <= 0) {
      throw new Error("A valid illustrated-map viewport is required.");
    }
    const width = options.width ?? 600;
    if (!Number.isInteger(width) || width < 2 || width > 1200) throw new Error("Invalid temperature texture width.");
    const nativeWidth = options.featherNativeUnits ?? FEATHER_NATIVE_UNITS;
    const modelWidth = options.featherModelMetres ?? FEATHER_MODEL_METRES;
    if (![nativeWidth, modelWidth].every(value => finite(value) && value >= 0)) throw new Error("Invalid temperature coverage fade width.");
    const height = Math.ceil(width * EXTENT[3] / EXTENT[2]);
    const field = createField(payload, alignment);
    const values = new Float64Array(width * height).fill(NaN);
    const alpha = new Uint8ClampedArray(width * height);
    let low = Infinity, high = -Infinity, supportedPixels = 0, visiblePixels = 0, featheredPixels = 0;
    for (let y = 0; y < height; y++) {
      const nativeY = (y + .5) * EXTENT[3] / height;
      for (let x = 0; x < width; x++) {
        const nativeX = (x + .5) * EXTENT[2] / width;
        const sample = field.sampleNativeWithCoverage(nativeX, nativeY, nativeWidth, modelWidth);
        if (sample === null) continue;
        const value = sample.temperature_c;
        const index = y * width + x;
        values[index] = value;
        alpha[index] = Math.round(255 * sample.coverage);
        if (alpha[index] < 255) featheredPixels++;
        supportedPixels++;
        if (nativeX >= viewBox[0] && nativeX <= viewBox[0] + viewBox[2]
            && nativeY >= viewBox[1] && nativeY <= viewBox[1] + viewBox[3]) {
          low = Math.min(low, value); high = Math.max(high, value); visiblePixels++;
        }
      }
    }
    if (!visiblePixels) throw new Error("No model temperatures cover the calibrated map view.");
    const scale = temperatureScale(low, high);
    const data = new Uint8ClampedArray(width * height * 4);
    values.forEach((value, index) => {
      if (!finite(value)) return;
      data.set([...colourAt(value, scale), alpha[index]], index * 4);
    });
    return { width, height, data, scale, sampleCount: field.sampleCount, triangleCount: field.triangleCount,
      supportedPixels, visiblePixels, visibleMinC: low, visibleMaxC: high, extent: [...EXTENT],
      featheredPixels, edgeFeather: { native_units: nativeWidth, model_metres: modelWidth } };
  }

  function buildTexture(payload, alignment, viewBox) {
    const raster = buildRaster(payload, alignment, viewBox);
    const canvas = root.document.createElement("canvas");
    canvas.width = raster.width; canvas.height = raster.height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Temperature map rendering is unavailable.");
    const image = context.createImageData(raster.width, raster.height);
    image.data.set(raster.data);
    context.putImageData(image, 0, 0);
    const { data, ...metadata } = raster;
    return { ...metadata, dataUrl: canvas.toDataURL("image/png") };
  }

  const api = Object.freeze({ buildTexture, buildRaster, createField, inverseArtwork, temperatureScale, colourAt, delaunay, EXTENT, PALETTE });
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.MapTemperatureField = api;
})(typeof window !== "undefined" ? window : globalThis);
