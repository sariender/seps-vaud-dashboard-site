"""Serve a frozen design preview, or opt into live MeteoSwiss observations.

The default needs only Python and reads the saved local design snapshot::

    python server.py

To enable hourly updates, use the notebook's Python environment::

    python server.py --live

The display is local by default. Design mode makes no weather requests.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import threading
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen


WEATHER_URL = "https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/puy/ogd-smn_puy_t_now.csv"
WEB_ROOT = Path(__file__).resolve().parent / "web"
REFRESH_SECONDS = 3600
MAX_WAIT_SECONDS = 5
WEATHER_POLL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 20
DISPLAY_GRACE_SECONDS = REQUEST_TIMEOUT_SECONDS + WEATHER_POLL_SECONDS
FRESH_FOR = timedelta(minutes=30)
EXPIRES_AFTER = timedelta(minutes=60)
UTC = timezone.utc

RISK_BANDS = (
    (21.0, "low", "Low heat stress", 0),
    (25.0, "caution", "Caution", 1),
    (28.0, "warning", "Warning", 2),
    (31.0, "severe", "Severe warning", 3),
    (math.inf, "danger", "Danger", 4),
)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def classify_risk(wbgt_c: float) -> dict:
    """Return the five exercise WBGT bands; a boundary enters the higher band."""
    if not math.isfinite(wbgt_c):
        raise ValueError("WBGT must be finite.")
    for upper, risk_id, label, level in RISK_BANDS:
        if wbgt_c < upper:
            return {"id": risk_id, "label": label, "level": level}
    raise ValueError("Invalid WBGT.")


def parse_observation(csv_text: str, *, now: datetime | None = None) -> dict:
    """Select the newest complete Pully row, keeping every field at one timestamp."""
    now = now or datetime.now(UTC)
    rows = [
        row
        for row in csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")), delimiter=";")
        if row.get("station_abbr", "").strip() == "PUY"
    ]
    if not rows:
        raise ValueError("No Pully observations were returned.")

    def timestamp(row: dict) -> datetime:
        try:
            return datetime.strptime(
                row["reference_timestamp"].strip(), "%d.%m.%Y %H:%M"
            ).replace(tzinfo=UTC)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid observation timestamp.") from exc

    rows.sort(key=timestamp, reverse=True)
    if timestamp(rows[0]) > now:
        raise ValueError("Observation timestamp is in the future.")

    def reading(row: dict, field: str, lower: float, upper: float) -> float:
        try:
            value = float(row[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Missing or invalid {field}.") from exc
        if not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError(f"Missing or implausible {field}.")
        return value

    for row in rows:
        try:
            return {
                "observed_at": iso_utc(timestamp(row)),
                "air_temperature_c": reading(row, "tre200s0", -80.0, 65.0),
                "relative_humidity_percent": reading(row, "ure200s0", 0.0, 100.0),
                "wind_speed_m_s": reading(row, "fu3010z0", 0.0, 300.0) / 3.6,
                "solar_w_m2": reading(row, "gre000z0", 0.0, 1600.0),
                "pressure_hpa": reading(row, "prestas0", 500.0, 1100.0),
            }
        except ValueError:
            # The feed may publish a new row before all its measurements arrive.
            # Discard that whole row rather than combining different intervals.
            continue
    raise ValueError("No complete valid Pully observations were returned.")


def calculate_wbgt(observation: dict) -> float:
    """Use exactly the Pully model settings from the prototype notebook."""
    from lwbgt import Input, calculate

    end = datetime.fromisoformat(observation["observed_at"].replace("Z", "+00:00"))
    weather = Input(
        year=end.year,
        month=end.month,
        day=end.day,
        hour=end.hour,
        minute=end.minute,
        gmt_offset_hours=0,
        averaging_minutes=10,
        latitude_deg_north=46.512283,
        longitude_deg_east=6.667517,
        air_temperature_c=observation["air_temperature_c"],
        relative_humidity_percent=observation["relative_humidity_percent"],
        pressure_hpa=observation["pressure_hpa"],
        solar_w_m2=observation["solar_w_m2"],
        wind_speed_m_s=observation["wind_speed_m_s"],
        wind_height_m=10,
        urban=1,
        vertical_temperature_difference_c=1,
    )
    result = calculate(weather)
    value = float(result.wbgt_c)
    if result.status != 0 or not math.isfinite(value) or not -80 <= value <= 80:
        raise ValueError("Liljegren calculation did not produce a valid WBGT.")
    return value


def fetch_weather() -> dict:
    request = Request(WEATHER_URL, headers={"User-Agent": "seps-vaud-dashboard/0.1"})
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        csv_text = response.read().decode("utf-8-sig")
    observation = parse_observation(csv_text)
    observation["wbgt_c"] = calculate_wbgt(observation)
    return observation


class WeatherStore:
    """Keep one validated observation while upstream updates are retried."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._observation: dict | None = None
        self._fetched_at: datetime | None = None
        self._failed = False

    def update(self, observation: dict, *, fetched_at: datetime | None = None) -> None:
        with self._lock:
            self._observation = dict(observation)
            self._fetched_at = fetched_at or datetime.now(UTC)
            self._failed = False

    def record_failure(self) -> None:
        with self._lock:
            self._failed = True

    def snapshot(self, *, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        with self._lock:
            observation = dict(self._observation) if self._observation else None
            fetched_at = self._fetched_at
            failed = self._failed

        payload = {
            "mode": "live",
            "refresh_seconds": REFRESH_SECONDS,
            "status": "loading",
            "station": {"id": "PUY", "name": "Pully"},
            "observation": observation,
            "fetched_at": iso_utc(fetched_at) if fetched_at else None,
            "risk": None,
            "message": "Connecting to MeteoSwiss…",
            "valid_until": None,
            "expires_at": None,
        }
        if observation is None:
            if failed:
                payload.update(
                    status="unavailable",
                    message="Weather data is unavailable. Retrying automatically.",
                )
            return payload

        observed_at = datetime.fromisoformat(observation["observed_at"].replace("Z", "+00:00"))
        age = now - observed_at
        source_age_at_check = fetched_at - observed_at
        # This is a checked hourly estimate, not a newly sampled observation on
        # every API read. Old source rows cannot become fresh through refetching.
        valid_until = fetched_at + timedelta(seconds=REFRESH_SECONDS + DISPLAY_GRACE_SECONDS)
        expires_at = valid_until + EXPIRES_AFTER
        payload.update(valid_until=iso_utc(valid_until), expires_at=iso_utc(expires_at))
        if age < timedelta(0) or fetched_at > now or source_age_at_check < timedelta(0) or source_age_at_check > EXPIRES_AFTER or now > expires_at:
            payload.update(
                status="unavailable",
                message="The last observation is out of date. Current heat risk is unavailable.",
            )
        elif source_age_at_check > FRESH_FOR or now > valid_until:
            payload.update(
                status="stale",
                message="Weather update delayed. Current heat risk is unavailable until data updates.",
            )
        else:
            payload.update(
                status="live",
                risk=classify_risk(observation["wbgt_c"]),
                message="Weather update delayed; retrying automatically." if failed else None,
            )
        return payload


def refresh_loop(store: WeatherStore, stop: threading.Event) -> None:
    next_refresh = datetime.now(UTC)
    while not stop.is_set():
        remaining = (next_refresh - datetime.now(UTC)).total_seconds()
        if remaining > 0:
            stop.wait(min(remaining, MAX_WAIT_SECONDS))
            continue
        try:
            store.update(fetch_weather())
        except Exception:
            # Keep the last good result; the API derives its freshness on every request.
            logging.exception("Could not update the MeteoSwiss observation")
            store.record_failure()
        next_refresh = datetime.now(UTC) + timedelta(seconds=REFRESH_SECONDS)


def design_snapshot(web_root: Path) -> dict:
    """Read the frozen local values without fetching or re-aging weather data."""
    try:
        payload = json.loads((web_root / "design-snapshot.json").read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Design snapshot must be an object.")
    except (OSError, ValueError):
        payload = {
            "captured_at": None,
            "station": {"id": "PUY", "name": "Pully"},
            "observation": None,
            "risk": None,
            "fetched_at": None,
            "message": "No local design snapshot is available. Live updates are paused.",
        }
    payload.update(mode="design", status="preview")
    return payload


def make_handler(store: WeatherStore, web_root: Path = WEB_ROOT, *, mode: str = "live", water_store=None, heat_store=None):
    root = web_root.resolve()
    if mode not in {"design", "live"}:
        raise ValueError("Dashboard mode must be design or live.")

    class DashboardHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/config":
                self.send_json({"mode": mode, "refresh_seconds": REFRESH_SECONDS,
                                "weather_poll_seconds": WEATHER_POLL_SECONDS,
                                "water_updates": "live" if water_store is not None else "snapshot",
                                "water_refresh_seconds": 3600,
                                "water_max_age_seconds": 3680,
                                "heat_updates": "live" if heat_store is not None else "disabled",
                                "heat_poll_seconds": 60,
                                "heat_refresh_seconds": 3600})
            elif path == "/api/weather":
                self.send_json(design_snapshot(root) if mode == "design" else store.snapshot())
            elif path == "/api/water-points":
                self.send_json(water_store.snapshot() if water_store is not None else {"mode": "snapshot", "status": "snapshot"})
            elif path == "/api/map-temperature":
                from heat_service import disabled_snapshot
                self.send_json(heat_store.snapshot() if heat_store is not None else disabled_snapshot())
            elif path.startswith("/api/"):
                self.send_error(404, "Unknown API endpoint")
            else:
                super().do_GET()

        def send_json(self, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def translate_path(self, path: str) -> str:
            try:
                request_path = unquote(urlsplit(path).path)
                candidate = (root / request_path.lstrip("/")).resolve()
            except (ValueError, OSError):
                # Malformed paths, including encoded NULs, are not files.
                return str(root / ".not-found")
            if not candidate.is_relative_to(root):
                return str(root / ".not-found")
            return str(candidate)

        def list_directory(self, path: str):
            self.send_error(404, "Directory listing is disabled")
            return None

        def end_headers(self) -> None:
            if not urlsplit(self.path).path.startswith("/api/"):
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

    return DashboardHandler


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the SEPS Vaud portrait dashboard locally.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", default=8080, type=int, help="HTTP port (default: 8080)")
    parser.add_argument("--live", action="store_true", help="Update MeteoSwiss weather, WBGT and Open-Meteo map temperatures hourly (default: frozen design preview)")
    parser.add_argument("--live-water", action="store_true", help="Refresh the City's drinking-water register hourly, independently of weather")
    parser.add_argument("--live-heat", action="store_true", help="Update Open-Meteo map temperatures hourly, even with frozen weather (also enabled by --live)")
    args = parser.parse_args(argv)
    if not WEB_ROOT.is_dir():
        parser.error(f"Dashboard files were not found at {WEB_ROOT}.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = WeatherStore()
    stop = threading.Event()
    mode = "live" if args.live else "design"
    water_store = None
    if args.live_water:
        from water_service import WaterStore, refresh_loop as water_refresh_loop
        water_store = WaterStore(
            alignment_path=WEB_ROOT / "assets/maps/water-alignment.json",
            seed_path=WEB_ROOT / "assets/maps/category-scenes.json",
            cache_path=Path(__file__).resolve().parent / ".cache/water.json",
        )
    heat_store = None
    if args.live or args.live_heat:
        from heat_service import HeatStore, refresh_loop as heat_refresh_loop
        heat_store = HeatStore(WEB_ROOT / "assets/maps/water-alignment.json")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, mode=mode, water_store=water_store, heat_store=heat_store))
    if args.live:
        worker = threading.Thread(target=refresh_loop, args=(store, stop), daemon=True)
        worker.start()
    if water_store is not None:
        water_worker = threading.Thread(target=water_refresh_loop, args=(water_store, stop), daemon=True)
        water_worker.start()
    if heat_store is not None:
        heat_worker = threading.Thread(target=heat_refresh_loop, args=(heat_store, stop), daemon=True)
        heat_worker.start()
    print(f"Dashboard: http://{args.host}:{args.port}", flush=True)
    if args.live:
        print("Weather reference: Pully · MeteoSwiss · hourly updates and WBGT recalculation", flush=True)
    else:
        print("Design preview: frozen local snapshot · live weather updates paused", flush=True)
    if water_store is not None:
        print("Water points: City register checked hourly · weather mode unchanged", flush=True)
    if heat_store is not None:
        print("Map temperatures: Open-Meteo / MeteoSwiss ICON CH1 · ~1 km model cells · hourly checks", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
