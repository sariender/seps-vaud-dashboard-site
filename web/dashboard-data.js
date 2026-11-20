/* Shared endpoint paths for the local server and the GitHub Pages export. */
(() => {
  "use strict";
  const names = new Set(["config", "weather", "water-points", "map-temperature"]);
  const delivery = document.documentElement.dataset.delivery || "dynamic";
  if (!["dynamic", "static"].includes(delivery)) throw new Error("Invalid data delivery mode.");

  function api(name) {
    if (!names.has(name)) throw new Error("Unknown dashboard endpoint.");
    const suffix = delivery === "static" ? ".json" : "";
    const url = new URL(`api/${name}${suffix}`, document.baseURI);
    if (delivery === "static") url.searchParams.set("v", String(Math.floor(Date.now() / 60_000)));
    return url.href;
  }

  globalThis.DashboardData = Object.freeze({ api });
})();
