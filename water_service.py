"""Poll the City's fountain register without changing frozen weather or map framing.

Only standard-library modules are needed at runtime. The illustration transform
is exported by the asset build step; it never extrapolates outside its controls.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import logging
import math
import os
import re
import tempfile
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REFRESH_SECONDS = 3600
MAX_WAIT_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 20
BROWSER_POLL_SECONDS = 60
MAX_FEATURES = 5000
EXPIRES_AFTER = timedelta(seconds=REFRESH_SECONDS + REQUEST_TIMEOUT_SECONDS + BROWSER_POLL_SECONDS)
UTC = timezone.utc
MS = "http://mapserver.gis.umn.edu/mapserver"
GML = "http://www.opengis.net/gml"
FEATURE_TYPE = "planville_gofontaine"
SOURCE_URL = "https://map.lausanne.ch/mapserv_proxy"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Water-list timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Water-list timestamp needs a timezone")
    return parsed.astimezone(UTC)


def lv95_to_wgs84(east: float, north: float) -> tuple[float, float]:
    """Use the same swisstopo approximation as the saved asset build."""
    y = (east - 2_600_000) / 1_000_000
    x = (north - 1_200_000) / 1_000_000
    lon = 2.6779094 + 4.728982 * y + 0.791484 * y * x + 0.1306 * y * x * x - 0.0436 * y ** 3
    lat = 16.9023892 + 3.238272 * x - 0.270978 * y * y - 0.002528 * x * x - 0.0447 * y * y * x - 0.0140 * x ** 3
    return round(lon * 100 / 36, 6), round(lat * 100 / 36, 6)


def fetch_register() -> bytes:
    params = {"ogcserver": "source for image/png", "SERVICE": "WFS", "VERSION": "1.1.0",
              "REQUEST": "GetFeature", "TYPENAME": FEATURE_TYPE,
              "SRSNAME": "EPSG:2056", "MAXFEATURES": MAX_FEATURES}
    request = Request(SOURCE_URL + "?" + urlencode(params),
                      headers={"User-Agent": "seps-vaud-dashboard/0.1 (water-list refresh)"})
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        # Bound memory use if the upstream response is unexpectedly large.
        body = response.read(10_000_001)
    if len(body) > 10_000_000:
        raise ValueError("The City water response exceeded its size limit")
    return body


def parse_register(body: bytes) -> list[dict]:
    """Reject incomplete responses before replacing any previously good list."""
    root = ET.fromstring(body)
    if root.tag.split("}")[-1] != "FeatureCollection":
        raise ValueError("The City did not return a fountain FeatureCollection")
    records = []
    seen = set()
    for member in root.iter(f"{{{MS}}}{FEATURE_TYPE}"):
        fields = {child.tag.split("}")[-1]: (child.text or "").strip()
                  for child in member if child.tag.startswith(f"{{{MS}}}")}
        position = member.find(f".//{{{GML}}}pos")
        if position is None or not position.text:
            raise ValueError("A City fountain has no LV95 position")
        coordinates = position.text.split()
        if len(coordinates) != 2:
            raise ValueError("A City fountain has an invalid LV95 position")
        east, north = (float(value) for value in coordinates)
        if not (math.isfinite(east) and math.isfinite(north)
                and 2_400_000 <= east <= 2_900_000 and 1_000_000 <= north <= 1_400_000):
            raise ValueError("A City fountain has an implausible LV95 position")
        if not all(fields.get(key) for key in ("name", "qualiteeau", "statut")):
            raise ValueError("A City fountain is missing its name, quality or status")
        match = re.search(r"idObjet=(\d+)", html.unescape(fields.get("href_go", "")))
        if match is None or int(match[1]) in seen:
            raise ValueError("The City response has missing or duplicate object IDs")
        object_id = int(match[1])
        seen.add(object_id)
        records.append({**fields, "object_id": object_id, "east": east, "north": north})
    if not records or len(records) >= MAX_FEATURES:
        raise ValueError("The City fountain response is empty or may be truncated")
    for name in ("numberOfFeatures", "numberReturned", "numberMatched"):
        declared = root.get(name)
        if declared is not None and declared != "unknown":
            try:
                count = int(declared)
            except ValueError as exc:
                raise ValueError("Invalid City fountain response count") from exc
            if count != len(records):
                raise ValueError("The City fountain response is incomplete")
    return records


class IllustrationAlignment:
    def __init__(self, path: Path):
        encoded = Path(path).read_bytes()
        self.signature = hashlib.sha256(encoded).hexdigest()
        data = json.loads(encoded)
        self.origin = data["origin"]
        self.bounds = tuple(float(value) for value in data["placement_bounds"])
        if len(self.bounds) != 4 or not all(math.isfinite(value) for value in self.bounds):
            raise ValueError("Invalid illustration placement bounds")
        if not (self.bounds[0] < self.bounds[2] and self.bounds[1] < self.bounds[3]):
            raise ValueError("Invalid illustration placement bounds")
        for key in ("longitude", "latitude", "east_scale", "north_scale"):
            if not math.isfinite(self.origin[key]):
                raise ValueError("Invalid illustration coordinate origin")
        self.station_placements = {}
        placements = data.get("station_placements", [])
        if not isinstance(placements, list):
            raise ValueError("Reviewed station placements must be an array")
        for placement in placements:
            if not isinstance(placement, dict):
                raise ValueError("Invalid reviewed station placement")
            object_id = placement.get("city_object_id")
            if type(object_id) is not int or object_id < 1 or object_id in self.station_placements:
                raise ValueError("Reviewed station placements need unique positive City IDs")
            for key in ("longitude", "latitude", "x", "y", "coordinate_tolerance_m"):
                value = placement.get(key)
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError("Invalid reviewed station placement coordinates")
            left, top, right, bottom = self.bounds
            if not (-180 <= placement["longitude"] <= 180 and -90 <= placement["latitude"] <= 90
                    and left <= placement["x"] <= right and top <= placement["y"] <= bottom
                    and placement["coordinate_tolerance_m"] > 0):
                raise ValueError("Reviewed station placement is outside its valid bounds")
            if (type(placement.get("grouped_outlets")) is not int or placement["grouped_outlets"] < 1
                    or placement.get("placement_method") != "reviewed_illustrated_location"
                    or not isinstance(placement.get("placement_note"), str) or not placement["placement_note"].strip()
                    or not isinstance(placement.get("access_note"), str)):
                raise ValueError("Invalid reviewed station placement metadata")
            self.station_placements[object_id] = dict(placement)
        self.triangles = []
        for triangle in data["triangles"]:
            ground, artwork = triangle["ground"], triangle["artwork"]
            if any(len(vertices) != 3 or any(len(vertex) != 2 or not all(math.isfinite(v) for v in vertex)
                                           for vertex in vertices) for vertices in (ground, artwork)):
                raise ValueError("Invalid illustration alignment triangle")
            (ax, ay), (bx, by), (cx, cy) = ground
            denominator = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            if abs(denominator) < 1e-12:
                raise ValueError("Degenerate illustration alignment triangle")
            self.triangles.append((ground, artwork, denominator))
        if not self.triangles:
            raise ValueError("The illustration alignment has no triangles")

    def _placement_for(self, longitude: float, latitude: float, city_object_id: int | None) -> dict | None:
        placement = self.station_placements.get(city_object_id) if type(city_object_id) is int else None
        if placement is not None:
            distance = math.hypot((longitude - placement["longitude"]) * self.origin["east_scale"],
                                  (latitude - placement["latitude"]) * self.origin["north_scale"])
            if distance <= placement["coordinate_tolerance_m"]:
                return placement
        return None

    def position(self, longitude: float, latitude: float, city_object_id: int | None = None) -> tuple[float, float] | None:
        if not (math.isfinite(longitude) and math.isfinite(latitude)):
            return None
        placement = self._placement_for(longitude, latitude, city_object_id)
        if placement is not None:
            return round(placement["x"], 6), round(placement["y"], 6)
        px = (longitude - self.origin["longitude"]) * self.origin["east_scale"]
        py = (latitude - self.origin["latitude"]) * self.origin["north_scale"]
        for ground, artwork, denominator in self.triangles:
            (ax, ay), (bx, by), (cx, cy) = ground
            wa = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / denominator
            wb = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / denominator
            wc = 1 - wa - wb
            if min(wa, wb, wc) < -1e-9 or max(wa, wb, wc) > 1 + 1e-9:
                continue
            x = sum(weight * vertex[0] for weight, vertex in zip((wa, wb, wc), artwork))
            y = sum(weight * vertex[1] for weight, vertex in zip((wa, wb, wc), artwork))
            left, top, right, bottom = self.bounds
            if left <= x <= right and top <= y <= bottom:
                return round(x, 6), round(y, 6)
            return None
        return None


def _points(records: list[dict], alignment: IllustrationAlignment, checked_at: str) -> list[dict]:
    points = []
    for record in sorted(records, key=lambda row: row["object_id"]):
        name = record["name"]
        if (record["qualiteeau"] != "Eau potable" or record["statut"] != "Ouvert"
                or name.startswith("Douche")):
            continue
        longitude, latitude = lv95_to_wgs84(record["east"], record["north"])
        object_id = record["object_id"]
        position = alignment.position(longitude, latitude, object_id)
        if position is None:
            continue
        x, y = position
        reviewed = alignment._placement_for(longitude, latitude, object_id)
        city_map = ("https://map.lausanne.ch/?lang=fr&tree_groups=planville_grp"
                    "&tree_group_layers_planville_grp=planville_gofontaine"
                    f"&tree_enable_planville_gofontaine=true&map_x={record['east']}"
                    f"&map_y={record['north']}&map_zoom=7")
        points.append({
            "id": f"city-water-{object_id}", "reference": object_id, "name": name,
            "x": x, "y": y, "badge_x": x, "badge_y": y, "icon_x": x, "icon_y": y, "callout": False,
            "anchor_type": "reviewed_city_location" if reviewed else "calibrated_city_location",
            "position_accuracy": "approximate_on_illustration",
            "city_object_id": object_id,
            "station_type": "drinking_tap" if name.startswith(("Fontaine à boire", "Robinet")) else "potable_fountain",
            "water_quality": record["qualiteeau"], "listed_status": record["statut"],
            "status_note": record.get("statutcomplement", ""), "checked_at": checked_at,
            "lv95": [record["east"], record["north"]], "longitude": longitude, "latitude": latitude,
            "source_url": city_map, "city_map_url": city_map,
            "source_record_url": f"https://golux.lausanne.ch/goeland/objet/getobjetinfo.php?idObjet={object_id}",
            **({key: reviewed[key] for key in ("grouped_outlets", "placement_note", "access_note")} if reviewed else {}),
        })
    return points


class WaterStore:
    """Keep the last complete City list; constructing the store never fetches."""
    def __init__(self, alignment_path: Path, seed_path: Path, cache_path: Path, fetcher=None):
        self._alignment = IllustrationAlignment(alignment_path)
        self._cache_path = Path(cache_path)
        self._fetcher = fetcher or fetch_register
        self._lock = threading.Lock()
        self._points = []
        self._checked_at = None
        self._last_attempt_at = None
        self._raw_count = 0
        self._updating = False
        self._succeeded = False
        self._error = None
        seed = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        source = seed.get("water_source", {})
        saved = {"checked_at": source.get("captured_at"), "raw_count": source.get("citywide_records", 0),
                 "points": next(scene["points"] for scene in seed["scenes"] if scene["id"] == "points_eau")}
        self._load_saved(saved)
        try:
            cached = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if not isinstance(cached, dict) or cached.get("alignment_sha256") != self._alignment.signature:
                raise ValueError("Saved City water cache uses a different or legacy illustration alignment")
            cached_at = _timestamp(cached["checked_at"])
            if cached_at > datetime.now(UTC) + timedelta(minutes=5):
                raise ValueError("Saved City water cache is dated in the future")
            if cached_at > _timestamp(self._checked_at):
                self._load_saved(cached)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError):
            logging.warning("Ignoring an invalid saved City water cache", exc_info=True)

    def _load_saved(self, saved: dict) -> None:
        checked_at = _iso(_timestamp(saved["checked_at"]))
        raw_count = int(saved["raw_count"])
        points = saved["points"]
        if raw_count < 1 or not isinstance(points, list):
            raise ValueError("Invalid saved City water list")
        seen = set()
        for point in points:
            object_id = point["city_object_id"]
            if (point["id"] != f"city-water-{object_id}" or object_id in seen
                    or point["water_quality"] != "Eau potable" or point["listed_status"] != "Ouvert"
                    or not all(math.isfinite(point[key]) for key in ("x", "y"))):
                raise ValueError("Invalid saved City water point")
            seen.add(object_id)
        self._points = copy.deepcopy(points)
        self._checked_at = checked_at
        self._raw_count = raw_count

    def _save_cache(self, payload: dict) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self._cache_path.parent,
                                             prefix=self._cache_path.name + ".", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump(payload, output, ensure_ascii=False, allow_nan=False)
                output.write("\n")
            os.replace(temporary, self._cache_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def refresh(self) -> dict:
        with self._lock:
            if self._updating:
                return self._snapshot(datetime.now(UTC))
            self._updating = True
            self._last_attempt_at = _iso(datetime.now(UTC))
            previous_count = self._raw_count
        try:
            records = parse_register(self._fetcher())
            if previous_count and len(records) < previous_count * 0.5:
                raise ValueError("The City fountain register unexpectedly lost more than half its records")
            checked_at = _iso(datetime.now(UTC))
            points = _points(records, self._alignment, checked_at)
            with self._lock:
                self._points = points
                self._checked_at = checked_at
                self._raw_count = len(records)
                self._succeeded = True
                self._error = None
            try:
                self._save_cache({"checked_at": checked_at, "raw_count": len(records), "points": points,
                                  "alignment_sha256": self._alignment.signature})
            except OSError:
                logging.warning("Could not persist the City water cache", exc_info=True)
        except Exception as exc:
            with self._lock:
                self._error = str(exc) or type(exc).__name__
            raise
        finally:
            with self._lock:
                self._updating = False
        return self.snapshot()

    def _snapshot(self, now: datetime) -> dict:
        age = now - _timestamp(self._checked_at) if self._checked_at else None
        expired = age is None or age > EXPIRES_AFTER or age < -timedelta(minutes=5)
        if expired:
            status = "unavailable"
        elif self._error:
            status = "delayed"
        elif self._succeeded:
            status = "current"
        elif self._checked_at:
            status = "saved"
        else:
            status = "loading" if self._updating else "unavailable"
        return {"mode": "live", "status": status, "checked_at": self._checked_at,
                "last_attempt_at": self._last_attempt_at, "updating": self._updating,
                "refresh_seconds": REFRESH_SECONDS, "max_age_seconds": int(EXPIRES_AFTER.total_seconds()),
                "points": copy.deepcopy([] if expired else self._points),
                "available_count": 0 if expired else len(self._points), "error": self._error}

    def snapshot(self, now: datetime | None = None) -> dict:
        with self._lock:
            return self._snapshot(now or datetime.now(UTC))


def refresh_loop(store: WaterStore, stop: threading.Event) -> None:
    next_refresh = datetime.now(UTC)
    while not stop.is_set():
        remaining = (next_refresh - datetime.now(UTC)).total_seconds()
        if remaining > 0:
            # Short waits let wall-clock deadlines catch up promptly after the
            # computer resumes from sleep, even if its timed waits paused.
            stop.wait(min(remaining, MAX_WAIT_SECONDS))
            continue
        try:
            store.refresh()
        except Exception:
            logging.exception("Could not refresh the City drinking-water list")
        next_refresh = datetime.now(UTC) + timedelta(seconds=REFRESH_SECONDS)
