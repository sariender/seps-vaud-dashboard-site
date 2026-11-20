/* Hourly numeric model temperatures; category rotation stays independent. */
(() => {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const POLL_MS = 60_000;
  const TINT_OPACITY = 0.5;
  const LAND_COLOUR = [197, 234, 190];
  const byId = id => document.getElementById(id);
  const numberFormat = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 1 });
  let context = null;
  let alignment = null;
  let tint = null;
  let image = null;
  let lastGood = null;
  let renderedSignature = null;
  let result = null;
  let state = { status: "disabled", opacity: TINT_OPACITY, error: null };
  let resizeTimer = null;
  let polling = false;

  function element(name, attributes = {}) {
    const node = document.createElementNS(NS, name);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    return node;
  }

  async function read(url, type = "json") {
    const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(20_000) });
    if (!response.ok) throw new Error("Map temperature data could not be loaded.");
    return response[type]();
  }

  // Use the original land shapes. Ink, roads, water and drawings are above the tint.
  function landClip(root, source) {
    const greens = new Set([
      "rgb(77.255249%, 91.764832%, 74.510193%)",
      "rgb(43.528748%, 79.608154%, 36.077881%)",
      "rgb(24.705505%, 72.941589%, 14.509583%)",
    ]);
    const clip = element("clipPath", { id: "temperature-land", clipPathUnits: "userSpaceOnUse" });
    function visit(node, inheritedTransform = "", inheritedClip = null) {
      const transform = [inheritedTransform, node.getAttribute("transform")].filter(Boolean).join(" ");
      const clipPath = node.getAttribute("clip-path") || inheritedClip;
      if (node.localName === "path" && greens.has(node.getAttribute("fill"))) {
        const shape = node.cloneNode(true);
        if (transform) shape.setAttribute("transform", transform);
        if (clipPath) shape.setAttribute("clip-path", clipPath);
        shape.setAttribute("clip-rule", node.getAttribute("fill-rule") || "nonzero");
        clip.append(shape);
      }
      for (const child of node.children) visit(child, transform, clipPath);
    }
    [...source.children].slice(0, 108).concat(source.children[130]).forEach(node => visit(node));
    if (clip.children.length !== 108) throw new Error("The illustrated land mask has changed.");
    root.querySelector("defs").append(clip);
  }

  // Inline the complete artwork without colliding with the gauge's SVG definitions.
  function prefixReferences(root) {
    const nodes = [root, ...root.querySelectorAll("*")];
    for (const node of nodes) {
      if (node.id) node.id = `temperature-art-${node.id}`;
      for (const attribute of [...node.attributes]) {
        if (attribute.localName === "id") continue;
        let value = attribute.value.replace(/url\(#([^\)]+)\)/g, "url(#temperature-art-$1)");
        if (attribute.localName === "href" && value.startsWith("#")) value = `#temperature-art-${value.slice(1)}`;
        if (value !== attribute.value) node.setAttributeNS(attribute.namespaceURI, attribute.name, value);
      }
    }
  }

  async function prepareArtwork() {
    if (tint) return;
    const [text, geometry] = await Promise.all([
      read(`assets/maps/${context.manifest.background}`, "text"),
      read("assets/maps/water-alignment.json"),
    ]);
    const parsed = new DOMParser().parseFromString(text, "image/svg+xml");
    if (parsed.querySelector("parsererror")) throw new Error("The illustrated artwork is invalid.");
    const native = document.importNode(parsed.documentElement, true);
    const source = native.querySelector('[id="source-8"]');
    if (!source || source.children[108]?.getAttribute("clip-path") !== "url(#clip-20)"
        || source.children[115]?.getAttribute("fill") !== "rgb(63.529968%, 85.490417%, 97.254944%)") {
      throw new Error("The illustrated layer order has changed.");
    }
    landClip(native, source);
    const layer = element("g", { id: "temperature-tint", opacity: String(TINT_OPACITY), "clip-path": "url(#temperature-land)", display: "none" });
    const texture = element("image", { x: "0", y: "0", width: "1190.574219", height: "1246.480469", preserveAspectRatio: "none" });
    layer.append(texture);
    source.insertBefore(layer, source.children[108]);
    prefixReferences(native);
    const [x, y, width, height] = context.manifest.viewBox;
    for (const [key, value] of Object.entries({ id: "illustrated-background", x, y, width, height,
      preserveAspectRatio: "xMidYMid meet", "aria-hidden": "true", focusable: "false" })) native.setAttribute(key, value);
    context.background.replaceWith(native);
    context.background = native;
    tint = layer;
    image = texture;
    alignment = geometry;
  }

  function hideTemperature(message, status = "unavailable") {
    if (tint) tint.setAttribute("display", "none");
    byId("map-temperature-credit").hidden = true;
    byId("map-temperature-attribution").hidden = true;
    byId("map-temperature-scale").hidden = true;
    byId("map-temperature-notice").textContent = message;
    byId("map-temperature-notice").hidden = !message;
    byId("map-temperature-legend").hidden = status === "disabled";
    state = { ...state, status };
    positionLegend();
  }

  function positionLegend() {
    if (!context) return;
    const legend = byId("map-temperature-legend");
    if (legend.hidden) return;
    const frame = byId("illustrated-map-frame").getBoundingClientRect();
    const [left, top, width, height] = context.getViewBox();
    const bounds = legend.getBoundingClientRect();
    // Follow Pully on the illustration, allowing the compact key to fit small screens.
    const nativeRight = frame.width < 500 ? 1004 : 1005;
    const preferredLeft = (nativeRight - left) / width * frame.width - bounds.width;
    const preferredTop = (940 - top) / height * frame.height;
    const attribution = byId("map-temperature-attribution");
    const bottomSpace = attribution.hidden ? 6 : attribution.getBoundingClientRect().height + 10;
    legend.style.left = `${Math.max(6, Math.min(preferredLeft, frame.width - bounds.width - 6))}px`;
    legend.style.top = `${Math.max(6, Math.min(preferredTop, frame.height - bounds.height - bottomSpace))}px`;
  }

  function isCurrent(payload) {
    const now = Date.now();
    return payload && Date.parse(payload.valid_at) <= now
      && Date.parse(payload.fetched_at) <= now + 1000 && now <= Date.parse(payload.valid_until);
  }

  function expire() {
    if (lastGood && !isCurrent(lastGood)) hideTemperature("Temperature update delayed", "stale");
  }

  function displayKey(scale) {
    // Pad the key to evenly spaced whole degrees without changing map colours.
    const step = Math.max(1, Math.ceil((Math.ceil(scale.max_c) - Math.floor(scale.min_c)) / 4));
    const low = Math.floor(scale.min_c / step) * step;
    const high = Math.ceil(scale.max_c / step) * step;
    const blend = colour => "#" + [1, 3, 5].map((offset, channel) => {
      const value = parseInt(colour.slice(offset, offset + 2), 16);
      return Math.round(TINT_OPACITY * value + (1 - TINT_OPACITY) * LAND_COLOUR[channel])
        .toString(16).padStart(2, "0");
    }).join("");
    // Outside the original bounds the palette clamps to its endpoint colours,
    // exactly as the map's colour function does. Original stop values stay fixed.
    const stops = [
      { value_c: low, color: scale.stops[0].color },
      ...scale.stops,
      { value_c: high, color: scale.stops.at(-1).color },
    ].map(stop => ({ ...stop, color: blend(stop.color) }));
    const values = [];
    for (let value = high; value >= low; value -= step) values.push(value || 0);
    return { min_c: low, max_c: high, step_c: step, stops, values };
  }

  function render() {
    if (!lastGood || !tint || !alignment) return;
    if (!isCurrent(lastGood)) return expire();
    const view = context.getViewBox();
    const signature = JSON.stringify([lastGood.points, view]);
    if (signature !== renderedSignature) {
      const next = window.MapTemperatureField.buildTexture(lastGood, alignment, view);
      image.setAttribute("href", next.dataUrl);
      result = next;
      renderedSignature = signature;
    }
    tint.setAttribute("display", "inline");
    byId("map-temperature-legend").hidden = false;
    byId("map-temperature-credit").hidden = false;
    byId("map-temperature-attribution").hidden = false;
    byId("map-temperature-scale").hidden = false;
    byId("map-temperature-notice").hidden = true;
    const key = displayKey(result.scale);
    const { min_c: low, max_c: high, stops, values } = key;
    byId("map-temperature-key-colours").replaceChildren(...stops.map(stop => element("stop", {
      offset: `${100 * (stop.value_c - low) / (high - low)}%`, "stop-color": stop.color,
    })));
    const ticks = document.querySelector(".map-temperature-ticks");
    ticks.replaceChildren(...values.map(value => {
      const tick = document.createElement("span");
      tick.textContent = numberFormat.format(value || 0);
      tick.dataset.temperatureC = String(value || 0);
      tick.style.top = `${100 * (high - value) / (high - low)}%`;
      return tick;
    }));
    const labelLength = Math.max(...values.map(value => numberFormat.format(value).length));
    byId("map-temperature-legend").style.setProperty("--key-label-width", `${labelLength + .2}ch`);
    byId("map-temperature-legend").setAttribute("aria-description", `Modelled air temperature at 2 m, valid ${new Date(lastGood.valid_at).toLocaleString("en-GB", { timeZone: "Europe/Zurich" })}. MeteoSwiss ICON CH1 via Open-Meteo, approximately 1 km resolution. Updated hourly; colours use a local Celsius scale. Smooth interpolation adds no street-level detail. Illustrated alignment is approximate.`);
    byId("map-temperature-gradient").setAttribute("aria-label", `Modelled air temperature: ${numberFormat.format(low)} to ${numberFormat.format(high)} degrees Celsius`);
    state = { ...state, valid_at: lastGood.valid_at, fetched_at: lastGood.fetched_at,
      valid_until: lastGood.valid_until, sampleCount: lastGood.points.length,
      scale: result.scale, legendScale: key, supportedPixels: result.supportedPixels,
      featheredPixels: result.featheredPixels, edgeFeather: result.edgeFeather, viewBox: [...view] };
    positionLegend();
  }

  function validate(payload) {
    const now = Date.now();
    const valid = Date.parse(payload.valid_at);
    const fetched = Date.parse(payload.fetched_at);
    const until = Date.parse(payload.valid_until);
    if (payload.unit !== "°C" || payload.variable !== "temperature_2m"
        || ![valid, fetched, until].every(Number.isFinite)
        || valid > now || fetched > now + 1000 || fetched < valid || fetched - valid > 70 * 60_000
        || until <= fetched || until - fetched > 3680_000
        || !Array.isArray(payload.points) || payload.points.length < 3
        || payload.points.some(point => ![point.longitude, point.latitude, point.temperature_c].every(Number.isFinite)
          || Math.abs(point.longitude) > 180 || Math.abs(point.latitude) > 90 || point.temperature_c < -80 || point.temperature_c > 65)) {
      throw new Error("The numeric map temperatures are invalid.");
    }
    return payload;
  }

  function renderSafely() {
    try { render(); }
    catch (error) {
      state.error = error.message;
      hideTemperature("Map temperature unavailable");
    }
  }

  async function poll() {
    if (polling) return;
    polling = true;
    let loading = false;
    let responseReceived = false;
    try {
      const payload = await read(DashboardData.api("map-temperature"));
      responseReceived = true;
      window.WeatherCondition?.apply(payload);
      if (payload.status === "disabled") {
        lastGood = null;
        renderedSignature = null;
        result = null;
        state = { status: "disabled", opacity: TINT_OPACITY, error: null };
        hideTemperature("", "disabled");
      } else if (payload.status === "live") {
        validate(payload);
        if (!isCurrent(payload)) throw new Error("The numeric map temperatures have expired.");
        await prepareArtwork();
        lastGood = payload;
        state = { ...state, status: payload.message ? "delayed" : "live", error: payload.message || null };
        renderSafely();
      } else if (payload.status === "loading") {
        loading = true;
        if (!isCurrent(lastGood)) hideTemperature("Loading map temperatures…", "loading");
      } else {
        throw new Error("Waiting for the next map temperature update.");
      }
    } catch (error) {
      if (!responseReceived) window.WeatherCondition?.delayed();
      state = { ...state, status: "delayed", error: error.message };
      if (isCurrent(lastGood)) renderSafely();
      else hideTemperature("Map temperature unavailable");
    } finally {
      polling = false;
      expire();
      setTimeout(poll, loading ? 2000 : POLL_MS);
    }
  }

  function initialize(options) {
    if (context) return;
    context = options;
    byId("map-temperature-legend").hidden = false;
    state.status = "loading";
    poll();
    new ResizeObserver(() => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(renderSafely, 80);
    }).observe(byId("illustrated-map-frame"));
    setInterval(expire, 15_000);
    document.addEventListener("visibilitychange", expire);
  }

  window.MapTemperature = Object.freeze({ initialize, getState: () => ({ ...state }) });
})();
