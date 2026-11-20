"use strict";

const $ = (id) => document.getElementById(id);
const board = $("board");
const ZONE = "Europe/Zurich";
let weatherPollMs = 60_000;
const MAX_SOURCE_AGE_AT_CHECK_MS = 30 * 60_000;
let lastWeather = null;
let weatherRequestInFlight = false;
let displayMode = "unknown";

const riskStyles = {
  low: { color: "#367450", label: "Low" },
  caution: { color: "#857018", label: "Caution" },
  warning: { color: "#b46b20", label: "Warning" },
  severe: { color: "#bc4d27", label: "Severe" },
  danger: { color: "#b8404b", label: "Danger" },
};

const wbgtGauge = new DialGauge($("wbgt-gauge"), {
  id: "wbgt", min: -6, max: 33, label: "WBGT · heat stress", unit: "°C",
  ticks: [-6, 0, 10, 20, 30, 33], minorTicks: 39, color: riskStyles.low.color,
  bands: [
    { from: -6, to: 21, color: riskStyles.low.color },
    { from: 21, to: 25, color: riskStyles.caution.color },
    { from: 25, to: 28, color: riskStyles.warning.color },
    { from: 28, to: 31, color: riskStyles.severe.color },
    { from: 31, to: 33, color: riskStyles.danger.color },
  ],
});

const guidance = {
  low: ["Keep water nearby and take regular drinking breaks.", "water"],
  caution: ["Choose shade, drink regularly and take breaks.", "shade"],
  warning: ["Ease the pace and take frequent breaks in shade.", "rest"],
  severe: ["Avoid strenuous exercise and choose a cooler place.", "shade"],
  danger: ["Stop outdoor exercise and move to a cooler place.", "stop"],
};

function updateClock(now = new Date()) {
  $("clock").textContent = new Intl.DateTimeFormat("en-GB", {
    timeZone: ZONE, hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).format(now);
  const weekday = new Intl.DateTimeFormat("en-GB", { timeZone: ZONE, weekday: "long" }).format(now);
  const dayAndMonth = new Intl.DateTimeFormat("en-GB", { timeZone: ZONE, day: "numeric", month: "long" }).format(now);
  $("date").textContent = `${weekday}, ${dayAndMonth}`;
}

function renderPedestrianCount(count = null) {
  $("pedestrian-status").textContent = Number.isSafeInteger(count) && count >= 0
    ? `${count} passages · last 10 min`
    : "Pedestrian counter not connected";
}

function resetGuidance() {
  $("recommendation").textContent = "Current activity guidance needs a fresh observation.";
  $("recommendation-icon").setAttribute("href", "#icon-wait");
}

function renderNumericValue(id, value) {
  $(id).textContent = Number.isFinite(value) ? value.toFixed(1) : "—";
}

function renderWeather(data) {
  const preview = displayMode === "design" && data.mode === "design";
  const observation = data.observation;
  const observedAt = observation ? new Date(observation.observed_at) : null;
  const validTime = observedAt && Number.isFinite(observedAt.getTime());
  const ageMs = validTime ? Date.now() - observedAt.getTime() : Infinity;
  const checkedAt = Date.parse(data.fetched_at);
  const sourceAgeAtCheck = validTime ? checkedAt - observedAt.getTime() : Infinity;
  const validUntil = Date.parse(data.valid_until);
  const expiresAt = Date.parse(data.expires_at);
  const fresh = preview || (data.status === "live" && ageMs >= 0 && checkedAt <= Date.now()
    && sourceAgeAtCheck >= 0 && sourceAgeAtCheck <= MAX_SOURCE_AGE_AT_CHECK_MS
    && Number.isFinite(validUntil) && Date.now() <= validUntil);
  const risk = fresh && Number.isFinite(observation?.wbgt_c) && riskStyles[data.risk?.id] ? data.risk : null;
  let state = preview ? "preview" : (data.status || "unavailable");
  if (state === "live" && !risk) {
    state = ageMs >= 0 && Number.isFinite(expiresAt) && Date.now() <= expiresAt ? "stale" : "unavailable";
  }
  board.dataset.state = state;

  const current = Boolean(risk);
  wbgtGauge.setValue(current ? observation.wbgt_c : null);
  renderNumericValue("temperature-value", current ? observation.air_temperature_c : null);
  renderNumericValue("humidity-value", current ? observation.relative_humidity_percent : null);
  renderNumericValue("wind-value", current && Number.isFinite(observation.wind_speed_m_s) ? observation.wind_speed_m_s * 3.6 : null);

  if (risk) {
    const style = riskStyles[risk.id];
    board.style.setProperty("--accent", style.color);
    $("risk-label").textContent = style.label;
    $("risk-label").setAttribute("aria-label", `${risk.label}: environmental heat stress`);
    $("status-text").textContent = preview ? "WBGT estimate" : (data.message ? "Retrying update" : "Hourly estimate");
    const [recommendation, icon] = guidance[risk.id];
    $("heat-message").textContent = `${risk.label}. ${recommendation}`;
    $("recommendation").textContent = recommendation;
    $("recommendation-icon").setAttribute("href", `#icon-${icon}`);
  } else {
    resetGuidance();
    $("risk-label").removeAttribute("aria-label");
    if (state === "loading") {
      $("risk-label").textContent = "Connecting";
      $("status-text").textContent = "Pully reference";
      $("heat-message").textContent = "Connecting to the latest Pully observation.";
    } else if (state === "stale") {
      $("risk-label").textContent = "Data delayed";
      $("status-text").textContent = "Retrying";
      $("heat-message").textContent = "A fresh weather update is delayed. Current heat guidance is unavailable.";
    } else {
      $("risk-label").textContent = "No fresh data";
      $("status-text").textContent = "Retrying";
      $("heat-message").textContent = "Fresh weather data and current heat guidance are unavailable.";
    }
  }

  const updatedAt = new Date(data.fetched_at || data.captured_at || observation?.observed_at);
  if (Number.isFinite(updatedAt.getTime())) {
    const time = new Intl.DateTimeFormat("en-GB", {
      timeZone: ZONE, hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).format(updatedAt);
    $("observation-note").textContent = `Weather updated at ${time}`;
    $("observation-note").title = validTime
      ? `Source: MeteoSwiss Pully. Observation time: ${observedAt.toISOString()}.`
      : "Source: MeteoSwiss Pully.";
  } else {
    $("observation-note").textContent = "Weather update unavailable";
    $("observation-note").removeAttribute("title");
  }
  renderPedestrianCount(data.pedestrian_count_last_10_min);
}

async function fetchWeather() {
  if (displayMode !== "live" || weatherRequestInFlight) return;
  weatherRequestInFlight = true;
  try {
    const response = await fetch(DashboardData.api("weather"), { cache: "no-store", signal: AbortSignal.timeout(12_000) });
    if (!response.ok) throw new Error(`Weather request failed: ${response.status}`);
    const data = await response.json();
    lastWeather = data;
    renderWeather(data);
  } catch {
    if (lastWeather) {
      lastWeather = { ...lastWeather, message: "Weather update unavailable; retrying." };
      renderWeather(lastWeather);
    } else {
      renderWeather({ status: "unavailable", observation: null, risk: null });
    }
  } finally {
    weatherRequestInFlight = false;
  }
}

function refreshVisibleWeather() {
  if (displayMode === "live" && !document.hidden) { updateClock(); fetchWeather(); }
}

async function initializeDashboard() {
  // Saved observations are allowed only after the server confirms design mode.
  try {
    const response = await fetch(DashboardData.api("config"), { cache: "no-store", signal: AbortSignal.timeout(10_000) });
    if (!response.ok) throw new Error("Dashboard configuration unavailable");
    const config = await response.json();
    if (!["live", "design"].includes(config.mode)) throw new Error("Invalid dashboard mode");
    displayMode = config.mode;
    if (Number.isFinite(config.weather_poll_seconds) && config.weather_poll_seconds > 0) {
      weatherPollMs = config.weather_poll_seconds * 1_000;
    }
  } catch {
    // A failed config request must never turn a live site into a frozen preview.
    board.dataset.mode = displayMode;
    updateClock();
    renderWeather({ status: "unavailable", observation: null, risk: null });
    setTimeout(initializeDashboard, 60_000);
    return;
  }
  board.dataset.mode = displayMode;

  if (displayMode === "design") {
    try {
      const response = await fetch("design-snapshot.json", { cache: "no-store" });
      if (!response.ok) throw new Error("Design snapshot unavailable");
      const data = await response.json();
      lastWeather = data;
      updateClock(new Date(data.captured_at || data.fetched_at || data.observation.observed_at));
      renderWeather(data);
    } catch {
      $("risk-label").textContent = "Preview unavailable";
      $("status-text").textContent = "WBGT estimate";
    }
    return;
  }

  updateClock();
  setInterval(updateClock, 1_000);
  fetchWeather();
  setInterval(fetchWeather, weatherPollMs);
  setInterval(() => { if (lastWeather) renderWeather(lastWeather); }, 15_000);
  document.addEventListener("visibilitychange", refreshVisibleWeather);
  window.addEventListener("focus", refreshVisibleWeather);
  window.addEventListener("pageshow", refreshVisibleWeather);
  window.addEventListener("online", refreshVisibleWeather);
}

initializeDashboard();
