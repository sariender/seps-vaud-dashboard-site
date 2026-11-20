"""Build the public website and one set of live JSON snapshots for GitHub Pages.

Only the explicitly listed browser assets enter the output. The build runs once;
GitHub Actions schedules the next refresh without committing generated data.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import sys
import tempfile

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


def _read_assets(web_root: Path) -> dict[str, bytes]:
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
    assets["index.html"] = html.replace(
        '<html lang="en">', '<html lang="en" data-delivery="static">', 1
    ).encode("utf-8")
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


def build(output: Path, *, web_root: Path | None = None, offline: bool = False) -> dict:
    web_root = Path(web_root or ROOT / "web")
    assets = _read_assets(web_root)
    expected = set(assets) | {f"api/{name}.json" for name in API_NAMES} | {".nojekyll"}
    output = _validate_output(Path(output), web_root, expected)
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
    return {
        "output": str(output), "files": len(assets), "delivery": "static",
        "sources": {name: payloads[name].get("status") for name in API_NAMES if name != "config"},
        "offline": offline,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "_site")
    parser.add_argument("--offline", action="store_true", help="Build with unavailable live data and no upstream requests")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(json.dumps(build(args.output, offline=args.offline), ensure_ascii=False))


if __name__ == "__main__":
    main()
