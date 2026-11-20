/* Modelled Ouchy weather conditions share the hourly map-temperature request. */
(function (root) {
  "use strict";
  const TYPES = {
    2: ["Partly cloudy", "☁️"], 3: ["Cloudy", "☁️"],
    45: ["Fog", "🌫️"], 48: ["Freezing fog", "🌫️"],
    51: ["Light drizzle", "🌦️"], 53: ["Drizzle", "🌦️"], 55: ["Dense drizzle", "🌧️"],
    56: ["Icy drizzle", "🌧️"], 57: ["Icy drizzle", "🌧️"],
    61: ["Light rain", "🌧️"], 63: ["Rain", "🌧️"], 65: ["Heavy rain", "🌧️"],
    66: ["Icy rain", "🌧️"], 67: ["Icy rain", "🌧️"],
    71: ["Light snow", "🌨️"], 73: ["Snow", "🌨️"], 75: ["Heavy snow", "❄️"],
    77: ["Snow grains", "❄️"], 80: ["Showers", "🌦️"], 81: ["Showers", "🌦️"],
    82: ["Heavy showers", "🌧️"], 85: ["Snow showers", "🌨️"], 86: ["Snow showers", "🌨️"],
    95: ["Storms", "⛈️"], 96: ["Storm & hail", "⛈️"],
    97: ["Heavy storms", "⛈️"], 99: ["Storm & hail", "⛈️"],
  };
  let lastPayload = null;
  let state = { status: "loading", condition: null };

  function describe(code, isDay) {
    if (!Number.isInteger(code) || typeof isDay !== "boolean") return null;
    if (code === 0) return { label: isDay ? "Sunny" : "Clear", icon: isDay ? "☀️" : "🌙" };
    if (code === 1) return { label: "Mainly clear", icon: isDay ? "🌤️" : "🌙" };
    const type = TYPES[code];
    return type ? { label: type[0], icon: !isDay && type[1] === "🌦️" ? "🌧️" : type[1] } : null;
  }

  function select(payload, now) {
    const data = root.DashboardData;
    if (data?.isFrozen && !data.matchesFrozen(payload)) return null;
    if (now === undefined) now = data?.isFrozen ? data.now() : Date.now();
    const condition = payload?.conditions;
    if (payload?.mode !== "live" || payload.status !== "live" || !condition) return null;
    const valid = Date.parse(payload.valid_at), fetched = Date.parse(payload.fetched_at);
    const until = Date.parse(payload.valid_until);
    if (![now, valid, fetched, until].every(Number.isFinite)
        || valid > now || fetched > now + 1000 || fetched < valid || fetched - valid > 70 * 60_000
        || until <= fetched || until - fetched > 3680_000 || now > until
        || Date.parse(condition.valid_at) !== valid) return null;
    const weather = describe(condition.weather_code, condition.is_day);
    return weather ? { ...weather, valid_at: payload.valid_at, weather_code: condition.weather_code,
      is_day: condition.is_day, fetched_at: payload.fetched_at, valid_until: payload.valid_until } : null;
  }

  function unavailable(message) {
    const byId = id => root.document.getElementById(id);
    byId("condition-icon").textContent = "—";
    byId("weather-condition").setAttribute("aria-label", `Ouchy modelled weather: ${message.toLowerCase()}.`);
    state = { status: message === "Loading" ? "loading" : "unavailable", condition: null };
  }

  function apply(payload) {
    const weather = select(payload);
    if (!weather) {
      lastPayload = null;
      const message = payload?.status === "disabled" ? "Paused"
        : payload?.status === "loading" ? "Loading"
        : payload?.status === "stale" ? "Update delayed" : "Unavailable";
      unavailable(message);
      return;
    }
    lastPayload = payload;
    const byId = id => root.document.getElementById(id);
    const time = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/Zurich", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).format(new Date(weather.valid_at));
    byId("condition-icon").textContent = weather.icon;
    const updateDescription = root.DashboardData?.isFrozen ? `snapshot captured ${root.DashboardData.frozenAt}` : "checked hourly";
    byId("weather-condition").setAttribute("aria-label",
      `Ouchy modelled weather: ${weather.label}. Forecast valid ${time} Europe/Zurich. Open-Meteo, MeteoSwiss ICON CH1; ${updateDescription}.`);
    state = { status: root.DashboardData?.isFrozen ? "frozen" : payload.message ? "delayed" : "live", condition: weather };
  }

  function delayed() {
    if (select(lastPayload)) apply({ ...lastPayload, message: "Update delayed" });
    else unavailable("Update delayed");
  }

  function expire() {
    if (lastPayload && !select(lastPayload)) {
      lastPayload = null;
      unavailable("Update delayed");
    }
  }

  const api = Object.freeze({ describe, select, apply, delayed, getState: () => ({ ...state }) });
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else {
    root.WeatherCondition = api;
    if (!root.DashboardData?.isFrozen) {
      setInterval(expire, 15_000);
      root.document.addEventListener("visibilitychange", expire);
    }
  }
})(typeof window !== "undefined" ? window : globalThis);
