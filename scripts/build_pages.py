"""Build the public website and live or explicitly frozen JSON snapshots.

Only the explicitly listed browser assets enter the output. A frozen capture is
validated once at its real capture time and can then be rebuilt without requests.
"""
from __future__ import annotations

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import logging
import math
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import heat_service
import server
import water_service


PUBLIC_WEB_FILES = (
    "index.html", "styles.css", "dashboard-data.js", "app.js", "gauges.js",
    "temperature-field.js", "weather-condition.js", "map-temperature.js",
    "illustrated-map.js",
    "assets/font-license-jetbrains-mono.txt",
    "assets/font-license-sofia-sans-extra-condensed.txt",
    "assets/fonts/jetbrains-mono-latin.woff2",
    "assets/fonts/sofia-sans-extra-condensed-latin.woff2",
    "assets/maps/sample-map-clean.svg", "assets/maps/water-alignment.json",
    "assets/maps/category-scenes.json", "assets/maps/icon-points_eau.png",
    "assets/maps/icon-exterieurs.svg", "assets/maps/icon-exterieurs_jeux.svg",
    "assets/maps/icon-interieurs.svg", "assets/maps/icon-pool.svg",
    "assets/maps/icon-lake-swimming.svg",
)
API_NAMES = ("config", "weather", "water-points", "map-temperature")
MANIFEST = "assets/maps/category-scenes.json"
SOURCE_NAMES = ("weather", "water-points", "map-temperature")
MAX_AGE = timedelta(seconds=3680)
UTC = timezone.utc


def config_snapshot() -> dict:
    return {
        "mode": "live", "delivery": "static", "refresh_seconds": 3600,
        "weather_poll_seconds": 60, "water_updates": "live",
        "water_refresh_seconds": 3600, "water_max_age_seconds": 3680,
        "heat_updates": "live", "heat_poll_seconds": 60,
        "heat_refresh_seconds": 3600,
    }


def unavailable_water() -> dict:
    return {
        "mode": "live", "status": "unavailable", "checked_at": None,
        "last_attempt_at": None, "updating": False, "refresh_seconds": 3600,
        "max_age_seconds": 3680, "points": [], "available_count": 0,
        "error": "City register unavailable; retrying on the next scheduled build.",
    }


def unavailable_heat() -> dict:
    return {
        **heat_service.disabled_snapshot(), "mode": "live", "status": "unavailable",
        "message": "Map temperatures unavailable; retrying on the next scheduled build.",
    }


def _weather_snapshot() -> dict:
    store = server.WeatherStore()
    try:
        store.update(server.fetch_weather())
    except Exception as error:
        store.record_failure()
        logging.warning("Weather refresh failed (%s)", type(error).__name__)
    return store.snapshot()


def _heat_snapshot(web_root: Path) -> dict:
    try:
        store = heat_service.HeatStore(web_root / "assets/maps/water-alignment.json")
        store.refresh()
        return store.snapshot()
    except Exception as error:
        logging.warning("Map temperature refresh failed (%s)", type(error).__name__)
        return unavailable_heat()


def _water_snapshot(web_root: Path) -> dict:
    # Each build starts independently. Never load a workstation's runtime cache.
    with tempfile.TemporaryDirectory(prefix="seps-pages-water-") as temporary:
        try:
            store = water_service.WaterStore(
                alignment_path=web_root / "assets/maps/water-alignment.json",
                seed_path=web_root / MANIFEST,
                cache_path=Path(temporary) / "water.json",
            )
            store.refresh()
            payload = store.snapshot()
            payload["error"] = None
            return payload
        except Exception as error:
            logging.warning("City water refresh failed (%s)", type(error).__name__)
            # A bundled seed is historical reference data, never a live fallback.
            return unavailable_water()


def collect_snapshots(web_root: Path, *, offline: bool = False) -> dict:
    if offline:
        weather = server.WeatherStore()
        weather.record_failure()
        return {
            "config": config_snapshot(), "weather": weather.snapshot(),
            "water-points": unavailable_water(), "map-temperature": unavailable_heat(),
        }
    with ThreadPoolExecutor(max_workers=3) as executor:
        weather = executor.submit(_weather_snapshot)
        water = executor.submit(_water_snapshot, web_root)
        heat = executor.submit(_heat_snapshot, web_root)
        return {
            "config": config_snapshot(), "weather": weather.result(),
            "water-points": water.result(), "map-temperature": heat.result(),
        }


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value, name: str) -> datetime:
    try:
        if not isinstance(value, str):
            raise ValueError
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(UTC)
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError(f"Invalid frozen {name} timestamp") from error


def _number(value, lower: float, upper: float, name: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f"Invalid frozen {name}")


def _fresh(payload: dict, key: str, captured: datetime, name: str) -> datetime:
    fetched = _timestamp(payload.get(key), f"{name} {key}")
    if not timedelta(0) <= captured - fetched <= MAX_AGE:
        raise ValueError(f"Frozen {name} was not fresh at capture")
    return fetched


def _deadline(payload: dict, fetched: datetime, captured: datetime, name: str) -> None:
    deadline = _timestamp(payload.get("valid_until"), f"{name} valid_until")
    if not fetched < deadline <= fetched + MAX_AGE or captured > deadline:
        raise ValueError(f"Invalid frozen {name} freshness deadline")


def _validate_sources(payloads: dict, captured: datetime) -> None:
    """Reject unavailable, synthetic or stale values before enabling freeze mode."""
    if not isinstance(payloads, dict) or set(payloads) != set(API_NAMES):
        raise ValueError("The frozen dashboard requires all four API snapshots")
    if any(not isinstance(payloads[name], dict) for name in API_NAMES):
        raise ValueError("Frozen API snapshots must be objects")
    for name, status in (("weather", "live"), ("water-points", "current"), ("map-temperature", "live")):
        if payloads[name].get("mode") != "live" or payloads[name].get("status") != status:
            raise ValueError(f"Cannot freeze {name}: a fresh {status} source is required")

    weather = payloads["weather"]
    fetched = _fresh(weather, "fetched_at", captured, "weather")
    _deadline(weather, fetched, captured, "weather")
    observation = weather.get("observation")
    if not isinstance(observation, dict):
        raise ValueError("Frozen weather needs a complete observation")
    observed = _timestamp(observation.get("observed_at"), "weather observation")
    if not timedelta(0) <= fetched - observed <= timedelta(minutes=30):
        raise ValueError("Frozen weather observation was not fresh when fetched")
    for key, bounds in {
        "air_temperature_c": (-80, 65), "relative_humidity_percent": (0, 100),
        "wind_speed_m_s": (0, 300 / 3.6), "solar_w_m2": (0, 1600),
        "pressure_hpa": (500, 1100), "wbgt_c": (-80, 80),
    }.items():
        _number(observation.get(key), *bounds, f"weather {key}")
    if weather.get("station", {}).get("id") != "PUY":
        raise ValueError("Frozen weather must use the Pully station")
    if weather.get("risk") != server.classify_risk(observation["wbgt_c"]):
        raise ValueError("Frozen WBGT risk does not match the observation")
    expiry = _timestamp(weather.get("expires_at"), "weather expires_at")
    deadline = _timestamp(weather["valid_until"], "weather valid_until")
    if expiry != deadline + timedelta(minutes=60):
        raise ValueError("Invalid frozen weather expiry")

    water = payloads["water-points"]
    checked = _fresh(water, "checked_at", captured, "water-points")
    attempt = _timestamp(water.get("last_attempt_at"), "water-points last_attempt_at")
    if attempt > checked or water.get("updating") is not False or water.get("error") is not None:
        raise ValueError("Frozen water-points require a completed successful check")
    if water.get("max_age_seconds") != int(MAX_AGE.total_seconds()):
        raise ValueError("Invalid frozen water-points freshness deadline")
    points = water.get("points")
    if not isinstance(points, list) or not points or water.get("available_count") != len(points):
        raise ValueError("Frozen water-points need the current drinking-water list")
    seen = set()
    for point in points:
        if (not isinstance(point, dict) or type(point.get("city_object_id")) is not int
                or point.get("id") != f"city-water-{point['city_object_id']}"
                or point["city_object_id"] in seen
                or point.get("water_quality") != "Eau potable" or point.get("listed_status") != "Ouvert"
                or point.get("checked_at") != water["checked_at"]):
            raise ValueError("Invalid frozen drinking-water point")
        for key in ("x", "y", "badge_x", "badge_y", "icon_x", "icon_y"):
            _number(point.get(key), 0, 100_000, f"drinking-water {key}")
        _number(point.get("longitude"), -180, 180, "drinking-water longitude")
        _number(point.get("latitude"), -90, 90, "drinking-water latitude")
        seen.add(point["city_object_id"])

    heat = payloads["map-temperature"]
    fetched = _fresh(heat, "fetched_at", captured, "map-temperature")
    _deadline(heat, fetched, captured, "map-temperature")
    valid = _timestamp(heat.get("valid_at"), "map-temperature valid_at")
    if valid != fetched.replace(minute=0, second=0, microsecond=0):
        raise ValueError("Frozen map temperatures must use the current forecast hour")
    if (heat.get("variable") != "temperature_2m" or heat.get("unit") != "°C"
            or heat.get("source") != heat_service.SOURCE):
        raise ValueError("Invalid frozen map-temperature model or units")
    points = heat.get("points")
    if not isinstance(points, list) or len(points) < 3 or heat.get("grid_cells") != len(points):
        raise ValueError("Frozen map temperatures need at least three native cells")
    cells = set()
    for point in points:
        if not isinstance(point, dict):
            raise ValueError("Invalid frozen map-temperature cell")
        for key, bounds in {"longitude": (-180, 180), "latitude": (-90, 90), "temperature_c": (-80, 65)}.items():
            _number(point.get(key), *bounds, f"map-temperature {key}")
        cell = (round(point["longitude"], 6), round(point["latitude"], 6))
        if cell in cells:
            raise ValueError("Duplicate frozen map-temperature cells")
        cells.add(cell)
    condition = heat.get("conditions")
    if (not isinstance(condition, dict) or condition.get("valid_at") != heat["valid_at"]
            or type(condition.get("weather_code")) is not int or condition["weather_code"] not in heat_service.WEATHER_CODES
            or type(condition.get("is_day")) is not bool):
        raise ValueError("Frozen weather conditions must match the map-temperature hour")
    _number(condition.get("longitude"), -180, 180, "weather-condition longitude")
    _number(condition.get("latitude"), -90, 90, "weather-condition latitude")
    if (round(condition["longitude"], 6), round(condition["latitude"], 6)) not in cells:
        raise ValueError("Frozen weather condition must use a map-temperature cell")
    _json_bytes(payloads)


def _validate_frozen(snapshot: dict) -> tuple[str, dict]:
    if not isinstance(snapshot, dict) or set(snapshot) != {"frozen_at", *API_NAMES}:
        raise ValueError("A frozen capture must contain its timestamp and all four API snapshots")
    stamp = snapshot["frozen_at"]
    captured = _timestamp(stamp, "capture")
    if server.iso_utc(captured) != stamp:
        raise ValueError("The frozen capture timestamp must use UTC whole seconds")
    payloads = {name: snapshot[name] for name in API_NAMES}
    _validate_sources(payloads, captured)
    expected_config = {**config_snapshot(), "mode": "frozen", "frozen_at": stamp,
                       "water_updates": "frozen", "heat_updates": "frozen"}
    if payloads["config"] != expected_config:
        raise ValueError("Frozen dashboard config does not match the capture")
    if any(payloads[name].get("frozen_at") != stamp for name in API_NAMES):
        raise ValueError("Frozen API timestamps do not match the capture")
    return stamp, payloads


def capture_frozen(path: Path, *, web_root: Path | None = None) -> dict:
    """Fetch once, then save only a complete current capture for future builds."""
    web_root = Path(web_root or ROOT / "web")
    _read_assets(web_root)
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("The frozen capture must be a regular file, not a symlink")
    if path.resolve().is_relative_to(web_root.resolve()):
        raise ValueError("The frozen capture must be separate from the browser source")
    payloads = copy.deepcopy(collect_snapshots(web_root))
    # Read the clock after all three upstream requests have completed.
    stamp = server.iso_utc(_utc_now())
    captured = _timestamp(stamp, "capture")
    _validate_sources(payloads, captured)
    if payloads["config"] != config_snapshot() or any("frozen_at" in payloads[name] for name in API_NAMES):
        raise ValueError("A frozen capture requires freshly collected live snapshots")
    payloads["config"].update(mode="frozen", water_updates="frozen", heat_updates="frozen")
    for payload in payloads.values():
        payload["frozen_at"] = stamp
    snapshot = {"frozen_at": stamp, **payloads}
    _validate_frozen(snapshot)
    body = _json_bytes(snapshot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return {"capture": str(path.resolve()), "frozen_at": stamp,
            "sources": {name: payloads[name]["status"] for name in SOURCE_NAMES}}


def _load_frozen(path: Path) -> tuple[str, dict]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("The frozen capture must be a regular file, not a symlink")

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate frozen capture key: {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"Invalid frozen capture number: {value}")

    snapshot = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=object_pairs,
                          parse_constant=invalid_constant)
    return _validate_frozen(snapshot)


def _version_frozen_assets(html: str, stamp: str) -> str:
    version = "frozen-" + _timestamp(stamp, "capture").strftime("%Y%m%dT%H%M%SZ")

    def replace(match):
        url = urlsplit(match.group("url"))
        path = url.path.removeprefix("./")
        if (url.scheme or url.netloc or path not in PUBLIC_WEB_FILES
                or not path.endswith((".js", ".css", ".woff2"))):
            return match.group(0)
        return f'{match.group("attribute")}{match.group("quote")}{url.path}?v={version}{match.group("quote")}'

    return re.sub(r'(?P<attribute>\b(?:src|href)\s*=\s*)(?P<quote>["\x27])(?P<url>[^"\x27]*)(?P=quote)', replace, html)


def _read_assets(web_root: Path, *, frozen_at: str | None = None) -> dict[str, bytes]:
    if web_root.is_symlink():
        raise ValueError("The browser source directory must not be a symlink")
    web_root = web_root.resolve()
    assets = {}
    for name in PUBLIC_WEB_FILES:
        path = web_root
        for part in Path(name).parts:
            path = path / part
            if path.is_symlink():
                raise ValueError(f"Refusing symlink in public assets: {name}")
        if not path.is_file() or not path.resolve().is_relative_to(web_root):
            raise ValueError(f"Missing or unsafe public asset: {name}")
        assets[name] = path.read_bytes()
    html = assets["index.html"].decode("utf-8")
    if html.count('<html lang="en">') != 1:
        raise ValueError("The website needs one unambiguous HTML delivery marker")
    marker = '<html lang="en" data-delivery="static"'
    if frozen_at:
        marker += f' data-frozen-at="{frozen_at}"'
        html = _version_frozen_assets(html, frozen_at)
    assets["index.html"] = html.replace('<html lang="en">', marker + ">", 1).encode("utf-8")
    manifest = json.loads(assets[MANIFEST])
    water_scenes = [scene for scene in manifest["scenes"] if scene["id"] == "points_eau"]
    if len(water_scenes) != 1:
        raise ValueError("The website needs one drinking-water category")
    water_scenes[0]["points"] = []
    # reference_scenes retains original map anchors used by the framing code.
    assets[MANIFEST] = _json_bytes(manifest)
    return assets


def _validate_output(output: Path, web_root: Path, expected: set[str]) -> Path:
    if output.is_symlink():
        raise ValueError("The output directory must not be a symlink")
    resolved = output.resolve()
    source = web_root.resolve()
    if resolved == source or resolved.is_relative_to(source) or source.is_relative_to(resolved):
        raise ValueError("The output directory must be separate from the browser source")
    if output.exists():
        if not output.is_dir():
            raise ValueError("The output must be a directory")
        directories = {str(parent) for name in expected for parent in Path(name).parents if str(parent) != "."}
        for path in output.rglob("*"):
            relative = path.relative_to(output).as_posix()
            if (path.is_symlink() or not (path.is_file() or path.is_dir())
                    or (path.is_file() and relative not in expected)
                    or (path.is_dir() and relative not in directories)):
                raise ValueError(f"The output contains an unexpected entry: {relative}")
    return resolved


def build(output: Path, *, web_root: Path | None = None, offline: bool = False,
          frozen: Path | None = None) -> dict:
    web_root = Path(web_root or ROOT / "web")
    if frozen is not None and offline:
        raise ValueError("Choose a frozen capture or an offline live build")
    frozen_at, payloads = _load_frozen(frozen) if frozen is not None else (None, None)
    assets = _read_assets(web_root, frozen_at=frozen_at)
    expected = set(assets) | {f"api/{name}.json" for name in API_NAMES} | {".nojekyll"}
    output = _validate_output(Path(output), web_root, expected)
    if payloads is None:
        payloads = collect_snapshots(web_root, offline=offline)
    if set(payloads) != set(API_NAMES):
        raise ValueError("The Pages build requires all four API snapshots")
    # Validate every payload before replacing any output, including finite JSON.
    assets.update({f"api/{name}.json": _json_bytes(payloads[name]) for name in API_NAMES})
    assets[".nojekyll"] = b""
    for name, body in assets.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    report = {
        "output": str(output), "files": len(assets), "delivery": "static",
        "sources": {name: payloads[name].get("status") for name in API_NAMES if name != "config"},
        "offline": offline,
    }
    if frozen_at:
        report["frozen_at"] = frozen_at
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "_site")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="Build with unavailable live data and no upstream requests")
    mode.add_argument("--capture-frozen", type=Path, metavar="PATH", help="Fetch current sources once and save a complete frozen capture")
    mode.add_argument("--frozen", type=Path, metavar="PATH", help="Build from a validated frozen capture without upstream requests")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = capture_frozen(args.capture_frozen) if args.capture_frozen else build(
        args.output, offline=args.offline, frozen=args.frozen)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
