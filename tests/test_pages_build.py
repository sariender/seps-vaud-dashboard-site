"""Pages exports only public assets and honest, independently fetched snapshots."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
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


if __name__ == "__main__":
    unittest.main()
