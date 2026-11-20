"use strict";

// All positions share the supplied SVG coordinate system. The optional water
// updater reads the local API; the server checks the City register separately.
(() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const ROTATION_MS = 15_000;
  const WATER_POLL_MS = 60_000;
  const WATER_EXPIRY_MS = 15_000;
  let waterRefreshSeconds = 3600;
  let waterMaxAgeMs = (3600 + 80) * 1000;
  const ASSET_ROOT = "assets/maps/";
  const ORDER = ["points_eau", "exterieurs", "exterieurs_jeux", "swimming", "interieurs"];
  const svg = document.getElementById("illustrated-map");
  const frame = document.getElementById("illustrated-map-frame");
  const status = document.getElementById("illustrated-map-status");
  const heading = document.getElementById("illustrated-scene-title");
  const headingIcons = document.getElementById("illustrated-scene-icon");
  const controls = document.getElementById("map-scene-buttons");
  let manifest = null;
  let sceneIndex = -1;
  let timer = null;
  let loadStatus = "loading";
  let errorMessage = null;
  let content = null;
  let currentView = null;
  let viewClip = null;
  let waterState = { mode: "snapshot", status: "snapshot", checked_at: null };
  let waterSignature = null;
  let savedWaterPoints = [];
  let waterUpdatesStarted = false;
  let waterPolling = false;
  let waterPollTimer = null;
  const groups = [];
  const buttons = [];
  const icons = [];

  function svgElement(name, attributes = {}, text = null) {
    const element = document.createElementNS(SVG_NS, name);
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
    if (text !== null) element.textContent = text;
    return element;
  }

  function assetUrl(filename) {
    if (typeof filename !== "string" || !/^[a-zA-Z0-9][a-zA-Z0-9_.-]*\.(svg|png)$/.test(filename)) {
      throw new Error("Invalid local map asset name.");
    }
    return ASSET_ROOT + filename;
  }

  function iconFile(scene) {
    return scene.icon || manifest.icons?.[scene.icon_id];
  }

  function markerIconFile(scene, point) {
    return point.icon || iconFile(scene);
  }

  function displayViewBox(data = manifest) {
    return data === manifest && currentView ? currentView : (data.displayViewBox || data.viewBox);
  }

  // Keep the closer area fixed across categories and fill the smaller panel.
  // Its vertical alignment retains the fountains and swimming sites on narrow boards.
  function fittedViewBox() {
    const [left, top, width, height] = manifest.displayViewBox || manifest.viewBox;
    const bounds = frame.getBoundingClientRect();
    if (!bounds.width || !bounds.height) return [left, top, width, height];
    const scale = Math.max(bounds.width / width, bounds.height / height);
    const fittedWidth = bounds.width / scale;
    const fittedHeight = bounds.height / scale;
    let fittedLeft = left + (width - fittedWidth) / 2;
    let fittedTop = top + height - fittedHeight;
    const margin = (manifest.icon_size || 36) / 2 + 3;
    const water = manifest.reference_scenes.find(scene => scene.id === "points_eau").points;
    const minX = Math.min(...water.map(point => Math.min(point.x, point.icon_x ?? point.x))) - margin;
    const maxX = Math.max(...water.map(point => Math.max(point.x, point.icon_x ?? point.x))) + margin;
    const minY = Math.min(...water.map(point => Math.min(point.y, point.icon_y ?? point.y))) - margin;
    const maxY = Math.max(...water.map(point => Math.max(point.y, point.icon_y ?? point.y))) + margin;
    if (fittedWidth >= maxX - minX) fittedLeft = Math.max(left, Math.min(fittedLeft, minX));
    if (fittedHeight >= maxY - minY) fittedTop = Math.max(top, Math.min(fittedTop, minY));
    return [fittedLeft, fittedTop, fittedWidth, fittedHeight];
  }

  function updateViewport() {
    const view = fittedViewBox();
    if (currentView && view.every((value, index) => Math.abs(value - currentView[index]) < 0.01)) return;
    currentView = view;
    svg.setAttribute("viewBox", view.join(" "));
    ["x", "y", "width", "height"].forEach((name, index) => viewClip.setAttribute(name, view[index]));
    groups.forEach(group => group.remove());
    groups.length = 0;
    manifest.scenes.forEach(addMarkers);
    if (sceneIndex >= 0) showScene(sceneIndex);
  }

  function visiblePoints(scene) {
    const [left, top, width, height] = displayViewBox();
    return scene.points.filter((point) => point.x >= left && point.x <= left + width
      && point.y >= top && point.y <= top + height);
  }

  function validate(data) {
    if (!Array.isArray(data.viewBox) || data.viewBox.length !== 4 ||
        !data.viewBox.every(Number.isFinite) || data.viewBox[2] <= 0 || data.viewBox[3] <= 0) {
      throw new Error("The illustrated map needs a valid SVG viewBox.");
    }
    assetUrl(data.background);
    if (!Array.isArray(data.scenes) || data.scenes.length !== ORDER.length ||
        data.scenes.some((scene, index) => scene.id !== ORDER[index])) {
      throw new Error("The illustrated map needs the five display categories in order.");
    }
    const [left, top, width, height] = data.viewBox;
    const view = displayViewBox(data);
    if (!Array.isArray(view) || view.length !== 4 || !view.every(Number.isFinite)
        || view[2] <= 0 || view[3] <= 0 || view[0] < left || view[1] < top
        || view[0] + view[2] > left + width || view[1] + view[3] > top + height) {
      throw new Error("The selected map view must stay inside the original artwork.");
    }
    data.scenes.forEach((scene) => {
      if (typeof scene.title !== "string" || !scene.title || !Array.isArray(scene.points) ||
          !/^#[0-9a-fA-F]{6}$/.test(scene.color)) {
        throw new Error("Invalid illustrated category metadata.");
      }
      assetUrl(scene.icon || data.icons?.[scene.icon_id]);
      const identifiers = new Set();
      scene.points.forEach((point) => {
        if (!point.id || identifiers.has(point.id) || !Number.isFinite(point.x) || !Number.isFinite(point.y) ||
            point.x < left || point.x > left + width || point.y < top || point.y > top + height) {
          throw new Error("Invalid or repeated illustrated map anchor.");
        }
        identifiers.add(point.id);
        if (point.icon !== undefined) assetUrl(point.icon);
        ["icon_x", "icon_y", "badge_x", "badge_y"].forEach((key) => {
          if (point[key] !== undefined && !Number.isFinite(point[key])) {
            throw new Error("Invalid illustrated marker position.");
          }
        });
      });
    });
    return data;
  }

  function loadImage(url) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error(`Local map image could not be loaded: ${url}`));
      image.src = url;
    });
  }

  function checkedLabel(value, full = false) {
    const date = new Date(value);
    if (!value || !Number.isFinite(date.getTime())) return "unknown time";
    return new Intl.DateTimeFormat("en-GB", full
      ? { timeZone: "Europe/Zurich", dateStyle: "medium", timeStyle: "short" }
      : { timeZone: "Europe/Zurich", hour: "2-digit", minute: "2-digit" }).format(date);
  }

  function updateMapCredit() {
    const credit = document.getElementById("map-credits");
    if (manifest.scenes[sceneIndex]?.id !== "points_eau") {
      credit.textContent = "Cool spaces map 2026 · City of Lausanne";
      credit.removeAttribute("title");
      credit.removeAttribute("aria-label");
      return;
    }
    const time = checkedLabel(waterState.checked_at);
    const labels = {
      current: `Water checked ${time} · approximate`,
      saved: `Saved water ${time} · approximate`,
      delayed: `Water delayed ${time} · approximate`,
      unavailable: "Water status unavailable",
      loading: "Checking City water list",
      snapshot: "City water · locations approximate",
    };
    credit.textContent = labels[waterState.status];
    const details = waterState.mode === "frozen"
      ? `Snapshot captured ${DashboardData.frozenAt}. City register last checked ${checkedLabel(waterState.checked_at, true)}. Only stations listed potable and open at that check are shown. Illustrated positions are approximate.`
      : waterState.mode === "live"
      ? `City register last checked ${checkedLabel(waterState.checked_at, true)}. Only stations listed potable and open are shown. Checks repeat every ${waterRefreshSeconds === 3600 ? "hour" : `${Math.round(waterRefreshSeconds / 60)} minutes`}; this is register status, not a water-flow sensor. Illustrated positions are approximate.`
      : `Saved City register checked ${checkedLabel(manifest.water_source?.captured_at, true)}. Illustrated positions are approximate.`;
    credit.title = details;
    credit.setAttribute("aria-label", `${credit.textContent}. ${details}`);
  }

  function replaceWaterPoints(points) {
    const signature = JSON.stringify(points);
    if (signature === waterSignature) return;
    const scene = manifest.scenes[0];
    scene.points = points;
    waterSignature = signature;
    const previous = groups[0];
    addMarkers(scene);
    const replacement = groups.pop();
    groups[0] = replacement;
    previous.replaceWith(replacement);
  }

  function expireWater() {
    const now = waterState.mode === "frozen" && DashboardData.matchesFrozen(waterState) ? DashboardData.now() : Date.now();
    const age = now - Date.parse(waterState.checked_at);
    if (!Number.isFinite(age) || age > waterMaxAgeMs || age < -5 * 60_000) {
      waterState.status = "unavailable";
      replaceWaterPoints([]);
    }
  }

  function expireLiveWater() {
    if (loadStatus !== "ready" || waterState.mode !== "live") return;
    const previousStatus = waterState.status;
    const previousSignature = waterSignature;
    expireWater();
    if (previousStatus !== waterState.status || previousSignature !== waterSignature) showScene(sceneIndex);
  }

  function recoverVisibleWater() {
    expireLiveWater();
    if (!document.hidden && waterUpdatesStarted) pollWater();
  }

  function applyWater(payload) {
    if (DashboardData.isFrozen && !DashboardData.matchesFrozen(payload)) throw new Error("The water snapshot does not match this dashboard.");
    const emptyUnavailable = payload.status === "unavailable" && payload.checked_at === null
      && Array.isArray(payload.points) && payload.points.length === 0;
    if (payload.mode !== "live" || !["current", "saved", "delayed", "unavailable", "loading"].includes(payload.status)
        || !Array.isArray(payload.points) || (!Number.isFinite(Date.parse(payload.checked_at)) && !emptyUnavailable)
        || payload.points.some(point => point.water_quality !== "Eau potable" || point.listed_status !== "Ouvert")
        || (payload.status === "unavailable" && payload.points.length)) {
      throw new Error("The local water status is invalid.");
    }
    validate({ ...manifest, scenes: manifest.scenes.map((scene, index) => index === 0 ? { ...scene, points: payload.points } : scene) });
    if (Number.isFinite(payload.refresh_seconds) && payload.refresh_seconds > 0) waterRefreshSeconds = payload.refresh_seconds;
    if (Number.isFinite(payload.max_age_seconds) && payload.max_age_seconds > 0) waterMaxAgeMs = payload.max_age_seconds * 1000;
    waterState = { mode: DashboardData.isFrozen ? "frozen" : "live", status: payload.status, checked_at: payload.checked_at,
      frozen_at: DashboardData.isFrozen ? payload.frozen_at : undefined,
      last_attempt_at: payload.last_attempt_at, updating: Boolean(payload.updating),
      refresh_seconds: waterRefreshSeconds, max_age_seconds: waterMaxAgeMs / 1000 };
    replaceWaterPoints(payload.points);
    expireWater();
    // Updating the water layer must not interrupt the selected scene or timer.
    showScene(sceneIndex);
  }

  async function pollWater() {
    if (!waterUpdatesStarted || waterPolling) return;
    waterPolling = true;
    if (waterPollTimer !== null) clearTimeout(waterPollTimer);
    waterPollTimer = null;
    try {
      const response = await fetch(DashboardData.api("water-points"), { cache: "no-store", signal: AbortSignal.timeout(15_000) });
      if (!response.ok) throw new Error("The water list is unavailable.");
      applyWater(await response.json());
    } catch (error) {
      waterState.status = "delayed";
      waterState.updating = false;
      expireWater();
      showScene(sceneIndex);
    } finally {
      waterPolling = false;
      if (!DashboardData.isFrozen) waterPollTimer = setTimeout(pollWater, waterState.updating ? 2_000 : WATER_POLL_MS);
    }
  }

  async function initializeWaterUpdates() {
    try {
      const response = await fetch(DashboardData.api("config"), { cache: "no-store", signal: AbortSignal.timeout(10_000) });
      if (!response.ok) throw new Error("The dashboard configuration is unavailable.");
      const config = await response.json();
      if (DashboardData.isFrozen) {
        if (!DashboardData.acceptConfig(config)) throw new Error("Invalid frozen water configuration.");
        if (Number.isFinite(config.water_refresh_seconds) && config.water_refresh_seconds > 0) waterRefreshSeconds = config.water_refresh_seconds;
        if (Number.isFinite(config.water_max_age_seconds) && config.water_max_age_seconds > 0) waterMaxAgeMs = config.water_max_age_seconds * 1000;
        waterState = { mode: "frozen", status: "loading", checked_at: null };
        waterUpdatesStarted = true;
        await pollWater();
        return;
      }
      if (!["live", "snapshot"].includes(config.water_updates)) throw new Error("Invalid water-update configuration.");
      if (config.water_updates !== "live") {
        if (config.mode === "design") {
          waterState = { mode: "snapshot", status: "snapshot", checked_at: manifest.water_source?.captured_at };
          replaceWaterPoints(savedWaterPoints);
          showScene(sceneIndex);
        }
        return;
      }
      if (Number.isFinite(config.water_refresh_seconds) && config.water_refresh_seconds > 0) waterRefreshSeconds = config.water_refresh_seconds;
      if (Number.isFinite(config.water_max_age_seconds) && config.water_max_age_seconds > 0) waterMaxAgeMs = config.water_max_age_seconds * 1000;
    } catch (error) {
      if (DashboardData.isFrozen) {
        waterState = { mode: "frozen", status: "unavailable", checked_at: null };
        showScene(sceneIndex);
      } else setTimeout(initializeWaterUpdates, WATER_POLL_MS);
      return; // Keep water empty until the server confirms its update mode.
    }
    waterState = { mode: "live", status: "saved", checked_at: manifest.water_source?.captured_at,
      refresh_seconds: waterRefreshSeconds, max_age_seconds: waterMaxAgeMs / 1000 };
    expireWater();
    showScene(sceneIndex);
    waterUpdatesStarted = true;
    await pollWater();
  }

  function addMarkers(scene) {
    const group = svgElement("g", {
      class: "illustrated-scene", "data-scene-id": scene.id,
      "aria-label": scene.title, "aria-hidden": "true", display: "none",
    });
    const size = Number.isFinite(manifest.icon_size) && manifest.icon_size > 0 ? manifest.icon_size : 36;
    const [left, top, width, height] = displayViewBox();
    const inset = size / 2 + 1;
    const clampX = value => Math.max(left + inset, Math.min(value, left + width - inset));
    const clampY = value => Math.max(top + inset, Math.min(value, top + height - inset));
    const placements = visiblePoints(scene).map(point => ({
      point, cx: clampX(point.icon_x ?? point.badge_x ?? point.x),
      cy: clampY(point.icon_y ?? point.badge_y ?? point.y),
    }));
    // Keep edge badges inside the panel and separate neighbours with callouts.
    for (let pass = 0; pass < 60; pass++) {
      let crowded = false;
      placements.forEach((first, index) => placements.slice(index + 1).forEach(second => {
        const dx = second.cx - first.cx;
        const dy = second.cy - first.cy;
        const distance = Math.hypot(dx, dy);
        if (distance >= size + 2 - 0.01) return;
        crowded = true;
        const shift = (size + 2 - distance) / 2 + 0.01;
        const ux = distance ? dx / distance : 1;
        const uy = distance ? dy / distance : 0;
        first.cx = clampX(first.cx - ux * shift);
        first.cy = clampY(first.cy - uy * shift);
        second.cx = clampX(second.cx + ux * shift);
        second.cy = clampY(second.cy + uy * shift);
      }));
      if (!crowded) break;
    }
    placements.forEach(({ point, cx, cy }) => {
      const marker = svgElement("g", {
        class: "illustrated-marker", "data-point-id": point.id,
        "data-anchor-x": point.x, "data-anchor-y": point.y,
        "data-icon-x": cx, "data-icon-y": cy,
        "data-swimming-kind": point.swimming_kind || "",
        "data-city-object-id": point.city_object_id || "",
        "data-station-type": point.station_type || "",
        "data-grouped-outlets": point.grouped_outlets || "",
      });
      const kind = point.station_type ? `Drinking water · City register: potable and open when checked ${checkedLabel(point.checked_at, true)} · illustrated location approximate`
        : point.swimming_kind === "lake" ? "Lake bathing area"
        : point.pool_type ? `${point.pool_type === "indoor" ? "Indoor" : "Outdoor"} pool` : null;
      const groupNote = point.grouped_outlets > 1 ? `One City record representing ${point.grouped_outlets} outlets` : null;
      const details = [point.name || `${scene.title} ${point.reference}`, kind, groupNote, point.access_note, point.address].filter(Boolean);
      marker.setAttribute("aria-label", details.join(" · "));
      if (point.callout || Math.hypot(cx - point.x, cy - point.y) > 0.01) {
        marker.append(
          svgElement("line", {
            x1: point.x, y1: point.y, x2: cx, y2: cy,
            stroke: scene.color, "stroke-width": 1.1, "stroke-linecap": "round",
          }),
          svgElement("circle", { cx: point.x, cy: point.y, r: 1.8, fill: scene.color, stroke: "#ffffff", "stroke-width": 0.5 }),
        );
      }
      // Supplied SVG icons already contain their circular badge. The water PNG
      // is the original reference icon. No extra frame changes their footprint.
      const markerImage = svgElement("image", {
        href: assetUrl(markerIconFile(scene, point)), x: cx - size / 2, y: cy - size / 2, width: size, height: size,
        preserveAspectRatio: "xMidYMid meet", "aria-hidden": "true",
      });
      if (point.city_map_url) {
        const link = svgElement("a", { href: point.city_map_url, target: "_blank", rel: "noopener noreferrer",
          "aria-label": `${point.name}: exact location on the City map` });
        link.append(markerImage);
        marker.append(link);
      } else marker.append(markerImage);
      group.append(marker);
    });
    content.append(group);
    groups.push(group);
  }

  function restartTimer() {
    if (timer !== null) clearInterval(timer);
    timer = null;
    if (DashboardData.isFrozen) return;
    timer = setInterval(() => showScene((sceneIndex + 1) % manifest.scenes.length), ROTATION_MS);
  }

  function showScene(index) {
    const scene = manifest.scenes[index];
    groups.forEach((group, groupIndex) => {
      const active = groupIndex === index;
      group.setAttribute("display", active ? "inline" : "none");
      group.setAttribute("aria-hidden", String(!active));
      buttons[groupIndex].setAttribute("aria-pressed", String(active));
      icons[groupIndex].hidden = !active;
    });
    sceneIndex = index;
    heading.textContent = scene.title;
    heading.style.setProperty("--scene-color", scene.color);
    updateMapCredit();
    frame.dataset.scene = scene.id;
    svg.setAttribute("aria-label", `${scene.title} · Lausanne and Ouchy`);
    const shown = visiblePoints(scene).length;
    frame.setAttribute("aria-label", `${scene.title}: ${shown} visible locations out of ${scene.points.length} in this category.`);
  }

  function setScene(idOrIndex) {
    if (loadStatus !== "ready") throw new Error("The illustrated map is not ready.");
    const index = typeof idOrIndex === "string"
      ? manifest.scenes.findIndex((scene) => scene.id === idOrIndex) : idOrIndex;
    if (!Number.isInteger(index) || index < 0 || index >= manifest.scenes.length) {
      throw new RangeError("Unknown illustrated map category.");
    }
    expireLiveWater();
    showScene(index);
    restartTimer();
    return getState();
  }

  function getState() {
    return {
      loadStatus, sceneId: sceneIndex >= 0 ? manifest.scenes[sceneIndex].id : null,
      sceneIndex, pointCount: sceneIndex >= 0 ? manifest.scenes[sceneIndex].points.length : 0,
      visiblePointCount: sceneIndex >= 0 ? visiblePoints(manifest.scenes[sceneIndex]).length : 0,
      displayViewBox: manifest ? [...displayViewBox()] : null,
      visibleGroups: groups.filter((group) => group.getAttribute("display") !== "none").length,
      totalScenes: manifest?.scenes.length || 0, rotationMs: DashboardData.isFrozen ? 0 : ROTATION_MS, error: errorMessage,
      water: { ...waterState },
    };
  }

  async function initialize() {
    try {
      const response = await fetch(ASSET_ROOT + "category-scenes.json", { cache: "no-store" });
      if (!response.ok) throw new Error("The local illustrated scene manifest is unavailable.");
      manifest = validate(await response.json());
      // Saved fountain positions are a design preview, never a live fallback.
      savedWaterPoints = manifest.scenes[0].points;
      manifest.scenes[0] = { ...manifest.scenes[0], points: [] };
      waterState = { mode: DashboardData.isFrozen ? "frozen" : "live", status: "loading", checked_at: null };
      const assets = new Set([assetUrl(manifest.background),
        ...manifest.scenes.map(scene => assetUrl(iconFile(scene))),
        ...manifest.scenes.flatMap(scene => scene.points.map(point => assetUrl(markerIconFile(scene, point)))),
      ]);
      await Promise.all([...assets].map(loadImage));
      const definitions = svgElement("defs");
      const clip = svgElement("clipPath", { id: "illustrated-view-clip", clipPathUnits: "userSpaceOnUse" });
      viewClip = svgElement("rect");
      clip.append(viewClip);
      definitions.append(clip);
      content = svgElement("g", { id: "illustrated-content", "clip-path": "url(#illustrated-view-clip)" });
      svg.append(definitions, content);
      const [x, y, width, height] = manifest.viewBox;
      content.append(svgElement("image", {
        id: "illustrated-background", href: assetUrl(manifest.background), x, y, width, height,
        preserveAspectRatio: "xMidYMid meet", "aria-hidden": "true",
      }));
      manifest.scenes.forEach((scene, index) => {
        const headerIcon = document.createElement("img");
        headerIcon.src = assetUrl(iconFile(scene));
        headerIcon.alt = "";
        headerIcon.hidden = true;
        headingIcons.append(headerIcon);
        icons.push(headerIcon);
        const button = document.createElement("button");
        button.type = "button";
        button.className = "map-scene-button";
        button.dataset.sceneId = scene.id;
        button.setAttribute("aria-label", scene.title);
        button.setAttribute("aria-pressed", "false");
        button.style.setProperty("--scene-color", scene.color);
        const image = document.createElement("img");
        image.src = assetUrl(iconFile(scene));
        image.alt = "";
        button.append(image);
        button.addEventListener("click", () => setScene(index));
        controls.append(button);
        buttons.push(button);
      });
      loadStatus = "ready";
      frame.dataset.state = "ready";
      status.hidden = true;
      updateViewport();
      showScene(0);
      restartTimer();
      if (!DashboardData.isFrozen) {
        setInterval(expireLiveWater, WATER_EXPIRY_MS);
        document.addEventListener("visibilitychange", recoverVisibleWater);
        window.addEventListener("focus", recoverVisibleWater);
        window.addEventListener("pageshow", recoverVisibleWater);
        window.addEventListener("online", recoverVisibleWater);
      }
      new ResizeObserver(updateViewport).observe(frame);
      initializeWaterUpdates();
      window.MapTemperature?.initialize({
        manifest, background: document.getElementById("illustrated-background"),
        getViewBox: () => [...displayViewBox()],
      });
    } catch (error) {
      loadStatus = "error";
      errorMessage = error.message;
      if (timer !== null) clearInterval(timer);
      groups.forEach((group) => group.setAttribute("display", "none"));
      svg.hidden = true;
      frame.dataset.state = "error";
      status.hidden = false;
      status.textContent = "The illustrated map is currently unavailable.";
    }
  }

  window.IllustratedMap = Object.freeze({ getState, setScene });
  initialize();
})();
