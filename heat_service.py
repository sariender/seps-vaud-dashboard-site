"""Hourly modelled map temperatures and local weather conditions.

Use explicit MeteoSwiss ICON CH1 native cells through Open-Meteo. Coordinates
sample the area; they do not create independent district/street measurements.
All runtime dependencies are in the Python standard library.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UTC = timezone.utc
REFRESH_SECONDS = 3600
BROWSER_POLL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 20
DISPLAY_SECONDS = REFRESH_SECONDS + REQUEST_TIMEOUT_SECONDS + BROWSER_POLL_SECONDS
MAX_WAIT_SECONDS = 5
API_URL = "https://api.open-meteo.com/v1/forecast"
CONDITION_LOCATION = {"longitude": 6.627694, "latitude": 46.507889}
WEATHER_CODES = frozenset({0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57,
                           61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82,
                           85, 86, 95, 96, 97, 99})
SOURCE = {
    "name": "Open-Meteo · MeteoSwiss ICON CH1",
    "url": "https://open-meteo.com/en/docs/meteoswiss-api",
    "model": "meteoswiss_icon_ch1",
    "resolution_m": 1000,
    "elevation_adjustment": False,
    "attribution_url": "https://open-meteo.com/",
    "licence": "CC BY 4.0",
}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _number(value, lower: float, upper: float, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f"Invalid map-temperature {name}")
    return float(value)


def sampling_points(alignment_path: Path) -> list[dict]:
    """Cover calibrated artwork coordinates with a buffered 7×7 request mesh."""
    alignment = json.loads(Path(alignment_path).read_text(encoding="utf-8"))
    origin = alignment["origin"]
    east_scale = _number(origin["east_scale"], 1, 200_000, "east scale")
    north_scale = _number(origin["north_scale"], 1, 200_000, "north scale")
    longitude = _number(origin["longitude"], -180, 180, "origin longitude")
    latitude = _number(origin["latitude"], -90, 90, "origin latitude")
    ground = [point for triangle in alignment["triangles"] for point in triangle["ground"]]
    if not ground:
        raise ValueError("The map-temperature alignment has no ground controls")
    east = [_number(point[0], -100_000, 100_000, "control east") for point in ground]
    north = [_number(point[1], -100_000, 100_000, "control north") for point in ground]
    west, south = longitude + (min(east) - 1000) / east_scale, latitude + (min(north) - 1000) / north_scale
    width, height = (max(east) - min(east) + 2000) / east_scale, (max(north) - min(north) + 2000) / north_scale
    return [{"longitude": round(west + width * column / 6, 7),
             "latitude": round(south + height * row / 6, 7)}
            for row in range(7) for column in range(7)]


def request_url(points: list[dict]) -> str:
    return API_URL + "?" + urlencode({
        "latitude": ",".join(str(point["latitude"]) for point in points),
        "longitude": ",".join(str(point["longitude"]) for point in points),
        "hourly": "temperature_2m,weather_code,is_day", "models": SOURCE["model"],
        "timezone": "GMT", "timeformat": "unixtime", "forecast_days": 1,
        "cell_selection": "nearest", "elevation": ",".join("nan" for _ in points),
    })


def fetch_response(points: list[dict]):
    request = Request(request_url(points), headers={
        "User-Agent": "seps-vaud-dashboard/0.1 (hourly modelled map temperature)",
    })
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        body = response.read(2_000_001)
    if len(body) > 2_000_000:
        raise ValueError("The Open-Meteo response exceeded its size limit")
    return json.loads(body)


def _condition_values(hourly: dict, index: int, length: int) -> tuple[int, bool] | None:
    """Optional conditions must match the temperature hour without invalidating it."""
    codes, daylight = hourly.get("weather_code"), hourly.get("is_day")
    if (not isinstance(codes, list) or not isinstance(daylight, list)
            or len(codes) != length or len(daylight) != length):
        return None
    code, is_day = codes[index], daylight[index]
    if type(code) is not int or code not in WEATHER_CODES or type(is_day) is not int or is_day not in (0, 1):
        return None
    return code, bool(is_day)


def parse_response(response, requested: list[dict], *, now: datetime | None = None) -> dict:
    """Select one common current UTC forecast hour and deduplicate native cells."""
    now = now or datetime.now(UTC)
    valid_at = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    epoch = int(valid_at.timestamp())
    rows = response if isinstance(response, list) else [response]
    if len(rows) != len(requested):
        raise ValueError("The Open-Meteo location response is incomplete")
    cells = {}
    native_conditions = {}
    for row, location in zip(rows, requested):
        if not isinstance(row, dict) or row.get("error") or row.get("utc_offset_seconds") != 0:
            raise ValueError("Invalid Open-Meteo location response or timezone")
        lon = _number(row.get("longitude"), -180, 180, "grid longitude")
        lat = _number(row.get("latitude"), -90, 90, "grid latitude")
        # Explicit CH1 should select a nearby ~1 km cell, never silently accept
        # a remote/out-of-domain location or low-resolution model fallback.
        distance = math.hypot((lon - location["longitude"]) * 76_600,
                              (lat - location["latitude"]) * 111_320)
        if distance > 3000:
            raise ValueError("Open-Meteo returned a grid cell too far from the map")
        hourly = row.get("hourly", {})
        times, values = hourly.get("time"), hourly.get("temperature_2m")
        units = row.get("hourly_units", {})
        if units.get("temperature_2m") != "°C" or units.get("time") != "unixtime":
            raise ValueError("Unexpected Open-Meteo time or temperature unit")
        if not isinstance(times, list) or not isinstance(values, list) or len(times) != len(values):
            raise ValueError("Incomplete Open-Meteo hourly arrays")
        if (not times or any(type(t) is not int for t in times)
                or any(b <= a for a, b in zip(times, times[1:])) or epoch not in times):
            raise ValueError("Open-Meteo has no temperature for the current forecast hour")
        index = times.index(epoch)
        temperature = _number(values[index], -80, 65, "temperature")
        key = (round(lon, 6), round(lat, 6))
        if key in cells and abs(cells[key]["temperature_c"] - temperature) > 0.05:
            raise ValueError("Duplicate Open-Meteo cells have inconsistent temperatures")
        cells[key] = {"longitude": lon, "latitude": lat, "temperature_c": temperature}
        condition = _condition_values(hourly, index, len(times))
        # Repeated requests may return the same native cell. Inconsistent or
        # incomplete conditions for that cell are unavailable, not a reason to
        # substitute another location or discard otherwise valid temperatures.
        if key not in native_conditions:
            native_conditions[key] = condition
        elif native_conditions[key] != condition:
            native_conditions[key] = None
    points = sorted(cells.values(), key=lambda point: (point["latitude"], point["longitude"]))
    if len(points) < 3:
        raise ValueError("Too few distinct Open-Meteo model cells cover the map")
    nearest = min(points, key=lambda point: math.hypot(
        (point["longitude"] - CONDITION_LOCATION["longitude"]) * 76_600,
        (point["latitude"] - CONDITION_LOCATION["latitude"]) * 111_320))
    condition = native_conditions[(round(nearest["longitude"], 6), round(nearest["latitude"], 6))]
    conditions = None if condition is None else {
        "weather_code": condition[0], "is_day": condition[1],
        "longitude": nearest["longitude"], "latitude": nearest["latitude"],
        "valid_at": _iso(valid_at), "location": "Lausanne · Ouchy", "source": SOURCE["name"],
    }
    return {"valid_at": _iso(valid_at), "points": points, "conditions": conditions,
            "requested_locations": len(requested), "grid_cells": len(points)}


def disabled_snapshot() -> dict:
    return {"mode": "disabled", "status": "disabled", "variable": "temperature_2m", "unit": "°C",
            "source": dict(SOURCE), "valid_at": None, "fetched_at": None, "valid_until": None,
            "refresh_seconds": REFRESH_SECONDS, "points": [], "conditions": None,
            "message": "Map temperature updates are paused."}


class HeatStore:
    def __init__(self, alignment_path: Path, *, fetcher=None) -> None:
        self.requested = sampling_points(alignment_path)
        self.fetcher = fetcher or fetch_response
        self._lock = threading.Lock()
        self._data = None
        self._fetched_at = None
        self._failed = False

    def refresh(self) -> dict:
        try:
            response = self.fetcher(self.requested)
            fetched = datetime.now(UTC)
            data = parse_response(response, self.requested, now=fetched)
        except Exception:
            with self._lock:
                self._failed = True
            raise
        with self._lock:
            self._data = data
            self._fetched_at = fetched
            self._failed = False
        return self.snapshot(now=fetched)

    def snapshot(self, *, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        with self._lock:
            data, fetched_at, failed = copy.deepcopy(self._data), self._fetched_at, self._failed
        payload = {**disabled_snapshot(), "mode": "live", "status": "loading",
                   "message": "Connecting to Open-Meteo…"}
        if data is None:
            if failed:
                payload.update(status="unavailable", message="Map temperatures unavailable. Retrying hourly.")
            return payload
        valid_at = datetime.fromisoformat(data["valid_at"].replace("Z", "+00:00"))
        deadline = fetched_at + timedelta(seconds=DISPLAY_SECONDS)
        payload.update(data, fetched_at=_iso(fetched_at), valid_until=_iso(deadline))
        if fetched_at > now or valid_at > fetched_at or now > deadline:
            payload.update(status="stale", points=[], conditions=None,
                           message="Map temperature update delayed. Retrying hourly.")
        else:
            payload.update(status="live", message="Map temperature update delayed; retrying hourly." if failed else None)
        return payload


def refresh_loop(store: HeatStore, stop: threading.Event) -> None:
    next_refresh = datetime.now(UTC)
    while not stop.is_set():
        remaining = (next_refresh - datetime.now(UTC)).total_seconds()
        if remaining > 0:
            stop.wait(min(remaining, MAX_WAIT_SECONDS))
            continue
        try:
            store.refresh()
        except Exception:
            logging.exception("Could not refresh Open-Meteo map temperatures")
        next_refresh = datetime.now(UTC) + timedelta(seconds=REFRESH_SECONDS)
