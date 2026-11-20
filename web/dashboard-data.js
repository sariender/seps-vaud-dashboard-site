/* Shared endpoint paths for the local server and the GitHub Pages export. */
(() => {
  "use strict";
  const names = new Set(["config", "weather", "water-points", "map-temperature"]);
  const delivery = document.documentElement.dataset.delivery || "dynamic";
  if (!["dynamic", "static"].includes(delivery)) throw new Error("Invalid data delivery mode.");
  const stamp = document.documentElement.dataset.frozenAt;
  const frozenEpoch = typeof stamp === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$/.test(stamp)
    ? Date.parse(stamp) : NaN;
  const isFrozen = delivery === "static" && Number.isFinite(frozenEpoch);
  const frozenAt = isFrozen ? stamp : null;
  let frozenConfigAccepted = false;

  function acceptConfig(config) {
    frozenConfigAccepted = isFrozen && config?.mode === "frozen" && config.frozen_at === frozenAt
      && config.water_updates === "frozen" && config.heat_updates === "frozen";
    return isFrozen ? frozenConfigAccepted : ["live", "design"].includes(config?.mode);
  }

  function matchesFrozen(payload) {
    return isFrozen && frozenConfigAccepted && payload?.frozen_at === frozenAt;
  }

  function now() { return isFrozen ? frozenEpoch : Date.now(); }

  function api(name) {
    if (!names.has(name)) throw new Error("Unknown dashboard endpoint.");
    const suffix = delivery === "static" ? ".json" : "";
    const url = new URL(`api/${name}${suffix}`, document.baseURI);
    if (delivery === "static") url.searchParams.set("v", isFrozen ? `frozen-${frozenAt}` : String(Math.floor(Date.now() / 60_000)));
    return url.href;
  }

  globalThis.DashboardData = Object.freeze({ api, isFrozen, frozenAt, now, acceptConfig, matchesFrozen });
})();
