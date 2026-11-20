"""Pages exports only public assets and honest, independently fetched snapshots."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import build_pages as pages


class PagesBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.web = self.directory / "web"
        self.output = self.directory / "published"
        for name in pages.PUBLIC_WEB_FILES:
            path = self.web / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"public asset")
        (self.web / "index.html").write_text('<!doctype html>\n<html lang="en"><body>Dashboard</body></html>')
        manifest = {
            "scenes": [{"id": "points_eau", "points": [{"id": "historical-water"}]},
                       {"id": "swimming", "points": [{"id": "pool"}]}],
            "reference_scenes": [{"id": "points_eau", "points": [{"id": "framing-anchor"}]}],
            "water_source": {"captured_at": "2026-11-16T12:00:00Z"},
        }
        (self.web / pages.MANIFEST).write_text(json.dumps(manifest))

    def api(self, name):
        return json.loads((self.output / "api" / f"{name}.json").read_text())

    def live_snapshots(self):
        fetched = datetime(2026, 11, 20, 10, 17, tzinfo=timezone.utc)
        store = pages.server.WeatherStore()
        store.update({
            "observed_at": "2026-11-20T10:10:00Z", "air_temperature_c": 12.3,
            "relative_humidity_percent": 80, "wind_speed_m_s": 2, "solar_w_m2": 100,
            "pressure_hpa": 970, "wbgt_c": 10.5,
        }, fetched_at=fetched)
        heat = {
            **pages.heat_service.disabled_snapshot(), "mode": "live", "status": "live",
            "message": None, "valid_at": "2026-11-20T10:00:00Z",
            "fetched_at": "2026-11-20T10:17:03Z", "valid_until": "2026-11-20T11:18:23Z",
            "points": [
                {"longitude": 6.61, "latitude": 46.51, "temperature_c": 12},
                {"longitude": 6.62, "latitude": 46.51, "temperature_c": 12.5},
                {"longitude": 6.62, "latitude": 46.52, "temperature_c": 11.5},
            ], "requested_locations": 49, "grid_cells": 3,
            "conditions": {"weather_code": 3, "is_day": True, "longitude": 6.62,
                           "latitude": 46.51, "valid_at": "2026-11-20T10:00:00Z",
                           "location": "Lausanne · Ouchy", "source": pages.heat_service.SOURCE["name"]},
        }
        water = {
            "mode": "live", "status": "current", "checked_at": "2026-11-20T10:17:05Z",
            "last_attempt_at": "2026-11-20T10:17:01Z", "updating": False,
            "refresh_seconds": 3600, "max_age_seconds": 3680, "available_count": 1, "error": None,
            "points": [{"id": "city-water-1", "city_object_id": 1,
                        "water_quality": "Eau potable", "listed_status": "Ouvert",
                        "checked_at": "2026-11-20T10:17:05Z", "x": 120, "y": 230,
                        "badge_x": 120, "badge_y": 230, "icon_x": 120, "icon_y": 230,
                        "longitude": 6.62, "latitude": 46.51}],
        }
        return {"config": pages.config_snapshot(), "weather": store.snapshot(now=fetched),
                "water-points": water, "map-temperature": heat}

    def frozen_capture(self):
        capture = self.directory / "source" / "dashboard-frozen.json"
        captured = datetime(2026, 11, 20, 10, 17, 6, tzinfo=timezone.utc)
        with patch.object(pages, "collect_snapshots", return_value=self.live_snapshots()), \
             patch.object(pages, "_utc_now", return_value=captured):
            pages.capture_frozen(capture, web_root=self.web)
        return capture

    def test_offline_artifact_excludes_private_files_and_saved_live_values(self):
        (self.web / "design-snapshot.json").write_text('{"observation":{"air_temperature_c":99}}')
        (self.web / ".env").write_text("private")
        (self.web / "private-notebook.ipynb").write_text("private")
        with patch.object(pages.server, "fetch_weather") as weather, \
             patch.object(pages.heat_service, "fetch_response") as heat, \
             patch.object(pages.water_service, "fetch_register") as water:
            report = pages.build(self.output, web_root=self.web, offline=True)
        weather.assert_not_called()
        heat.assert_not_called()
        water.assert_not_called()
        expected = set(pages.PUBLIC_WEB_FILES) | {f"api/{name}.json" for name in pages.API_NAMES} | {".nojekyll"}
        actual = {path.relative_to(self.output).as_posix() for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(actual, expected)
        self.assertEqual(report["files"], len(expected))
        self.assertIn('data-delivery="static"', (self.output / "index.html").read_text())
        self.assertNotIn('data-delivery="static"', (self.web / "index.html").read_text())
        self.assertEqual(self.api("config")["mode"], "live")
        self.assertEqual(self.api("config")["water_max_age_seconds"], 3680)
        for name in ("weather", "water-points", "map-temperature"):
            self.assertEqual(self.api(name)["status"], "unavailable")
        self.assertIsNone(self.api("weather")["observation"])
        self.assertIsNone(self.api("weather")["fetched_at"])
        self.assertEqual(self.api("map-temperature")["points"], [])
        self.assertIsNone(self.api("map-temperature")["conditions"])
        self.assertIsNone(self.api("water-points")["checked_at"])
        manifest = json.loads((self.output / pages.MANIFEST).read_text())
        self.assertEqual(manifest["scenes"][0]["points"], [])
        self.assertEqual(manifest["scenes"][1]["points"], [{"id": "pool"}])
        self.assertEqual(manifest["reference_scenes"][0]["points"], [{"id": "framing-anchor"}])
        self.assertEqual(manifest["water_source"]["captured_at"], "2026-11-16T12:00:00Z")

    def test_export_preserves_source_times_and_deadlines_without_extending_them(self):
        snapshots = pages.collect_snapshots(self.web, offline=True)
        snapshots["weather"].update(status="live", fetched_at="2026-11-20T10:17:00Z",
                                   valid_until="2026-11-20T11:18:20Z")
        snapshots["map-temperature"].update(status="live", valid_at="2026-11-20T10:00:00Z",
                                           fetched_at="2026-11-20T10:17:03Z", valid_until="2026-11-20T11:18:23Z")
        snapshots["water-points"].update(status="current", checked_at="2026-11-20T10:17:05Z")
        with patch.object(pages, "collect_snapshots", return_value=copy.deepcopy(snapshots)):
            pages.build(self.output, web_root=self.web)
        for name, expected in snapshots.items():
            self.assertEqual(self.api(name), expected)

    def test_upstream_failure_does_not_discard_other_sources(self):
        heat_store = Mock()
        heat_store.snapshot.return_value = {"mode": "live", "status": "live", "valid_at": "2026-11-20T10:00:00Z"}
        water_store = Mock()
        water_store.snapshot.return_value = {"mode": "live", "status": "current", "checked_at": "2026-11-20T10:17:00Z"}
        with patch.object(pages.server, "fetch_weather", side_effect=OSError("offline")), \
             patch.object(pages.heat_service, "HeatStore", return_value=heat_store), \
             patch.object(pages.water_service, "WaterStore", return_value=water_store):
            snapshots = pages.collect_snapshots(self.web)
        self.assertEqual(snapshots["weather"]["status"], "unavailable")
        self.assertIsNone(snapshots["weather"]["fetched_at"])
        self.assertEqual(snapshots["map-temperature"]["status"], "live")
        self.assertEqual(snapshots["water-points"]["status"], "current")
        heat_store.refresh.assert_called_once()
        water_store.refresh.assert_called_once()

    def test_failed_water_fetch_cannot_publish_a_seed_or_load_workstation_cache(self):
        store = Mock()
        store.refresh.side_effect = OSError("provider-private-error-text")
        store.snapshot.return_value = {"mode": "live", "status": "saved", "points": [{"id": "old"}],
                                       "checked_at": "2026-11-20T10:00:00Z"}
        with patch.object(pages.water_service, "WaterStore", return_value=store) as constructor:
            payload = pages._water_snapshot(self.web)
        store.snapshot.assert_not_called()
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["points"], [])
        self.assertIsNone(payload["checked_at"])
        self.assertNotIn("provider-private-error-text", json.dumps(payload))
        cache = constructor.call_args.kwargs["cache_path"]
        self.assertNotIn(".cache", cache.parts)
        self.assertFalse(cache.parent.exists())

    def test_weather_success_keeps_original_observation_and_existing_validity(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        original = {
            "observed_at": pages.server.iso_utc(now - timedelta(minutes=10)),
            "air_temperature_c": 12.3, "relative_humidity_percent": 80,
            "wind_speed_m_s": 2, "solar_w_m2": 100, "pressure_hpa": 970, "wbgt_c": 10.5,
        }
        with patch.object(pages.server, "fetch_weather", return_value=original):
            payload = pages._weather_snapshot()
        self.assertEqual(payload["status"], "live")
        self.assertEqual(payload["observation"], original)
        fetched = datetime.fromisoformat(payload["fetched_at"].replace("Z", "+00:00"))
        deadline = datetime.fromisoformat(payload["valid_until"].replace("Z", "+00:00"))
        self.assertEqual((deadline - fetched).total_seconds(), 3680)

    def test_symlink_assets_or_output_are_rejected_without_copying_private_content(self):
        private = self.directory / "private.txt"
        private.write_text("private")
        asset = self.web / "app.js"
        asset.unlink()
        asset.symlink_to(private)
        with self.assertRaisesRegex(ValueError, "symlink"):
            pages.build(self.output, web_root=self.web, offline=True)
        self.assertFalse(self.output.exists())
        asset.unlink()
        asset.write_text("public")
        target = self.directory / "target"
        target.mkdir()
        self.output.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            pages.build(self.output, web_root=self.web, offline=True)
        self.assertEqual(list(target.iterdir()), [])

    def test_existing_unrelated_output_and_source_subdirectories_are_rejected(self):
        self.output.mkdir()
        private = self.output / "private.txt"
        private.write_text("keep me")
        with self.assertRaisesRegex(ValueError, "unexpected entry"):
            pages.build(self.output, web_root=self.web, offline=True)
        self.assertEqual(private.read_text(), "keep me")
        with self.assertRaisesRegex(ValueError, "separate"):
            pages.build(self.web / "export", web_root=self.web, offline=True)
        self.assertFalse((self.web / "export").exists())

    def test_invalid_json_leaves_a_previous_valid_artifact_unchanged(self):
        pages.build(self.output, web_root=self.web, offline=True)
        before = {path.relative_to(self.output).as_posix(): path.read_bytes()
                  for path in self.output.rglob("*") if path.is_file()}
        snapshots = pages.collect_snapshots(self.web, offline=True)
        snapshots["map-temperature"]["points"] = [{"temperature_c": float("nan")}]
        with patch.object(pages, "collect_snapshots", return_value=snapshots), self.assertRaises(ValueError):
            pages.build(self.output, web_root=self.web)
        after = {path.relative_to(self.output).as_posix(): path.read_bytes()
                 for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_capture_reads_clock_after_all_sources_and_preserves_source_values(self):
        sources = self.live_snapshots()
        original = copy.deepcopy(sources)
        calls = []
        capture = self.directory / "dashboard-frozen.json"
        def collect(web_root):
            calls.append("fetched")
            return sources
        def now():
            calls.append("clock")
            return datetime(2026, 11, 20, 10, 17, 6, tzinfo=timezone.utc)
        with patch.object(pages, "collect_snapshots", side_effect=collect), \
             patch.object(pages, "_utc_now", side_effect=now):
            report = pages.capture_frozen(capture, web_root=self.web)
        self.assertEqual(calls, ["fetched", "clock"])
        self.assertEqual(sources, original)
        snapshot = json.loads(capture.read_text())
        self.assertEqual(report["frozen_at"], "2026-11-20T10:17:06Z")
        self.assertEqual(set(snapshot), {"frozen_at", *pages.API_NAMES})
        for name in pages.SOURCE_NAMES:
            expected = {**original[name], "frozen_at": report["frozen_at"]}
            self.assertEqual(snapshot[name], expected)
        expected_config = {**original["config"], "mode": "frozen", "frozen_at": report["frozen_at"],
                           "water_updates": "frozen", "heat_updates": "frozen"}
        self.assertEqual(snapshot["config"], expected_config)

    def test_capture_requires_every_source_and_does_not_replace_previous_capture(self):
        capture = self.directory / "dashboard-frozen.json"
        capture.write_text("previous capture")
        for name in pages.SOURCE_NAMES:
            with self.subTest(source=name):
                sources = self.live_snapshots()
                sources[name]["status"] = "unavailable"
                with patch.object(pages, "collect_snapshots", return_value=sources), \
                     patch.object(pages, "_utc_now", return_value=datetime(2026, 11, 20, 10, 17, 6, tzinfo=timezone.utc)), \
                     self.assertRaisesRegex(ValueError, name):
                    pages.capture_frozen(capture, web_root=self.web)
                self.assertEqual(capture.read_text(), "previous capture")

    def test_frozen_build_is_reproducible_years_later_without_upstream_calls(self):
        capture = self.frozen_capture()
        source_hash = hashlib.sha256(capture.read_bytes()).hexdigest()
        snapshot = json.loads(capture.read_text())
        with patch.object(pages, "collect_snapshots", side_effect=AssertionError("no collection")) as collect, \
             patch.object(pages.server, "fetch_weather", side_effect=AssertionError("no network")) as weather, \
             patch.object(pages.heat_service, "fetch_response", side_effect=AssertionError("no network")) as heat, \
             patch.object(pages.water_service, "fetch_register", side_effect=AssertionError("no network")) as water, \
             patch.object(pages, "_utc_now", return_value=datetime(2046, 11, 20, tzinfo=timezone.utc)):
            report = pages.build(self.output, web_root=self.web, frozen=capture)
        for mock in (collect, weather, heat, water):
            mock.assert_not_called()
        self.assertEqual(report["files"], 27)
        self.assertEqual(report["frozen_at"], snapshot["frozen_at"])
        self.assertEqual(hashlib.sha256(capture.read_bytes()).hexdigest(), source_hash)
        expected = set(pages.PUBLIC_WEB_FILES) | {f"api/{name}.json" for name in pages.API_NAMES} | {".nojekyll"}
        actual = {path.relative_to(self.output).as_posix() for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(actual, expected)
        for name in pages.API_NAMES:
            self.assertEqual(self.api(name), snapshot[name])
        html = (self.output / "index.html").read_text()
        self.assertIn('<html lang="en" data-delivery="static" data-frozen-at="2026-11-20T10:17:06Z">', html)
        first = {path.relative_to(self.output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in self.output.rglob("*") if path.is_file()}
        with patch.object(pages, "collect_snapshots", side_effect=AssertionError("no collection")):
            pages.build(self.output, web_root=self.web, frozen=capture)
        second = {path.relative_to(self.output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(first, second)

    def test_frozen_html_versions_local_scripts_styles_and_preloaded_fonts_only(self):
        capture = self.frozen_capture()
        html = '''<html lang="en"><head>
<link rel="preload" href="assets/fonts/jetbrains-mono-latin.woff2" as="font">
<link rel="stylesheet" href="./styles.css?old=1">
<script defer src="app.js"></script><script src='dashboard-data.js?v=old'></script>
<link href="https://example.test/styles.css" rel="stylesheet">
<img src="assets/maps/icon-points_eau.png"></head></html>'''
        (self.web / "index.html").write_text(html)
        pages.build(self.output, web_root=self.web, frozen=capture)
        actual = (self.output / "index.html").read_text()
        for asset in ("assets/fonts/jetbrains-mono-latin.woff2", "./styles.css", "app.js", "dashboard-data.js"):
            self.assertIn(asset + "?v=frozen-20261120T101706Z", actual)
        self.assertIn('href="https://example.test/styles.css"', actual)
        self.assertIn('src="assets/maps/icon-points_eau.png"', actual)
        self.assertNotIn("?old=1", actual)

    def test_invalid_or_mismatched_frozen_captures_fail_before_changing_output(self):
        capture = self.frozen_capture()
        original = json.loads(capture.read_text())
        pages.build(self.output, web_root=self.web, frozen=capture)
        before = {path.relative_to(self.output).as_posix(): path.read_bytes()
                  for path in self.output.rglob("*") if path.is_file()}
        mutations = {
            "missing source": lambda value: value.pop("water-points"),
            "wrong marker": lambda value: value["weather"].update(frozen_at="2026-11-20T10:17:07Z"),
            "wrong config": lambda value: value["config"].update(mode="live"),
            "future weather": lambda value: value["weather"].update(fetched_at="2026-11-20T10:18:00Z"),
            "old observation": lambda value: value["weather"]["observation"].update(observed_at="2026-11-20T09:40:00Z"),
            "future observation": lambda value: value["weather"]["observation"].update(observed_at="2026-11-20T10:20:00Z"),
            "false risk": lambda value: value["weather"]["risk"].update(id="danger"),
            "long weather deadline": lambda value: value["weather"].update(valid_until="2026-11-21T10:17:00Z"),
            "stale heat": lambda value: value["map-temperature"].update(valid_until="2026-11-20T10:17:05Z"),
            "wrong model": lambda value: value["map-temperature"]["source"].update(model="auto"),
            "missing conditions": lambda value: value["map-temperature"].update(conditions=None),
            "wrong condition hour": lambda value: value["map-temperature"]["conditions"].update(valid_at="2026-11-20T09:00:00Z"),
            "old water": lambda value: value["water-points"].update(checked_at="2026-11-20T09:00:00Z"),
            "future water": lambda value: value["water-points"].update(checked_at="2026-11-20T10:18:00Z"),
            "closed water": lambda value: value["water-points"]["points"][0].update(listed_status="Fermé"),
            "water missing points": lambda value: value["water-points"].update(points=[]),
            "invalid number": lambda value: value["map-temperature"]["points"][0].update(temperature_c=float("nan")),
        }
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                value = copy.deepcopy(original)
                mutate(value)
                capture.write_text(json.dumps(value))
                with patch.object(pages, "collect_snapshots", side_effect=AssertionError("no fallback")), \
                     self.assertRaises(ValueError):
                    pages.build(self.output, web_root=self.web, frozen=capture)
                after = {path.relative_to(self.output).as_posix(): path.read_bytes()
                         for path in self.output.rglob("*") if path.is_file()}
                self.assertEqual(after, before)

    def test_frozen_capture_rejects_duplicate_keys_naive_time_and_symlinks(self):
        capture = self.frozen_capture()
        valid = capture.read_text()
        invalid = [valid.replace('{"frozen_at":', '{"frozen_at":"2026-11-20T10:17:06Z","frozen_at":', 1),
                   valid.replace('"2026-11-20T10:17:06Z"', '"2026-11-20T10:17:06"'), "[]", "{}"]
        for body in invalid:
            capture.write_text(body)
            with self.assertRaises(ValueError):
                pages.build(self.output, web_root=self.web, frozen=capture)
            self.assertFalse(self.output.exists())
        capture.write_text(valid)
        link = self.directory / "linked.json"
        link.symlink_to(capture)
        with self.assertRaisesRegex(ValueError, "symlink"):
            pages.build(self.output, web_root=self.web, frozen=link)
        with self.assertRaisesRegex(ValueError, "symlink"):
            pages.capture_frozen(link, web_root=self.web)
        self.assertEqual(capture.read_text(), valid)

    def test_frozen_build_and_offline_mode_are_mutually_exclusive(self):
        with self.assertRaisesRegex(ValueError, "Choose"):
            pages.build(self.output, web_root=self.web, offline=True, frozen=self.directory / "missing.json")

    def test_cli_routes_frozen_capture_and_build_without_live_fallback(self):
        path = self.directory / "dashboard-frozen.json"
        with patch.object(pages, "capture_frozen", return_value={"frozen_at": "captured"}) as capture, \
             patch.object(pages, "build") as build, patch("builtins.print"):
            pages.main(["--capture-frozen", str(path)])
        capture.assert_called_once_with(path)
        build.assert_not_called()
        with patch.object(pages, "capture_frozen") as capture, \
             patch.object(pages, "build", return_value={"frozen_at": "captured"}) as build, patch("builtins.print"):
            pages.main(["--frozen", str(path), "--output", str(self.output)])
        build.assert_called_once_with(self.output, offline=False, frozen=path)
        capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
