"""The live water feed can change independently of frozen weather and map layout."""
from __future__ import annotations

import ast
import copy
import importlib.util
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "water_service.py"
SPEC = importlib.util.spec_from_file_location("preview_water_service", SOURCE)
water = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(water)
NOW = datetime(2026, 11, 18, 14, 20, tzinfo=timezone.utc)


def record(object_id=1, *, status="Ouvert", quality="Eau potable", name="Fontaine test", east=2538000, north=1152000):
    return {"object_id": object_id, "name": name, "statut": status, "qualiteeau": quality,
            "east": east, "north": north, "href_go": f"https://example.test/?idObjet={object_id}"}


def gml(records, *, declared=None):
    members = []
    for row in records:
        fields = "".join(f"<ms:{key}>{escape(str(row[key]))}</ms:{key}>"
                         for key in ("name", "statut", "qualiteeau", "href_go") if key in row)
        members.append(f'<gml:featureMember><ms:planville_gofontaine>{fields}'
                       f'<ms:geom><gml:Point><gml:pos>{row["east"]} {row["north"]}</gml:pos></gml:Point></ms:geom>'
                       '</ms:planville_gofontaine></gml:featureMember>')
    return (f'<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs" '
            f'xmlns:gml="{water.GML}" xmlns:ms="{water.MS}" '
            f'numberOfFeatures="{len(records) if declared is None else declared}">'
            + "".join(members) + '</wfs:FeatureCollection>').encode()


class LiveWaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.alignment_path = self.directory / "alignment.json"
        self.seed_path = self.directory / "scenes.json"
        self.cache_path = self.directory / "private-cache" / "water.json"
        longitude, latitude = water.lv95_to_wgs84(2538000, 1152000)
        ground = [[-1000, -1000], [1000, -1000], [1000, 1000], [-1000, 1000]]
        artwork = [[500 + x / 10, 700 + y / 10] for x, y in ground]
        alignment = {
            "origin": {"longitude": longitude, "latitude": latitude, "east_scale": 76600, "north_scale": 111320},
            "placement_bounds": [22, 404, 1051, 1228],
            "triangles": [{"ground": [ground[i] for i in indices], "artwork": [artwork[i] for i in indices]}
                          for indices in ((0, 1, 2), (0, 2, 3))],
        }
        self.alignment_path.write_text(json.dumps(alignment))
        mapper = water.IllustrationAlignment(self.alignment_path)
        points = water._points([record()], mapper, water._iso(NOW))
        self.seed = {"water_source": {"captured_at": water._iso(NOW), "citywide_records": 2},
                     "scenes": [{"id": "points_eau", "points": points}]}
        self.seed_path.write_text(json.dumps(self.seed))
        self.clock = patch.object(water, "datetime")
        self.mock_datetime = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.mock_datetime.now.return_value = NOW
        self.mock_datetime.fromisoformat.side_effect = datetime.fromisoformat

    def store(self, fetcher=None):
        return water.WaterStore(self.alignment_path, self.seed_path, self.cache_path, fetcher=fetcher)

    def reviewed_stations(self):
        records = [record(10 + index, east=2540000 + index * 100) for index in range(6)]
        placements = []
        for index, row in enumerate(records):
            longitude, latitude = water.lv95_to_wgs84(row["east"], row["north"])
            placements.append({"city_object_id": row["object_id"], "longitude": longitude, "latitude": latitude,
                               "x": 100 + index * 60, "y": 1000, "coordinate_tolerance_m": 20,
                               "grouped_outlets": 8 if index == 0 else 1,
                               "placement_method": "reviewed_illustrated_location",
                               "placement_note": "Reviewed against the illustrated landmark",
                               "access_note": "Check site access" if index == 0 else ""})
        return records, placements

    def add_placements(self, placements):
        alignment = json.loads(self.alignment_path.read_text())
        alignment["station_placements"] = placements
        self.alignment_path.write_text(json.dumps(alignment))

    def test_constructor_does_not_fetch_and_saved_timestamp_is_unchanged(self):
        fetcher = Mock()
        store = self.store(fetcher)
        fetcher.assert_not_called()
        saved = store.snapshot(NOW)
        self.assertEqual(saved["status"], "saved")
        self.assertEqual(saved["checked_at"], "2026-11-18T14:20:00Z")
        self.assertIsNone(saved["last_attempt_at"])
        self.assertFalse(saved["updating"])

    def test_station_opening_closing_quality_change_and_new_station(self):
        fetcher = Mock(side_effect=[
            gml([record(1, status="Fermé"), record(2)]),
            gml([record(1), record(2, status="Fermé"), record(3, east=2538100)]),
            gml([record(1, quality="Eau non potable"), record(2, status="Fermé"), record(3, east=2538100)]),
        ])
        store = self.store(fetcher)
        self.assertEqual([p["city_object_id"] for p in store.refresh()["points"]], [2])
        self.assertEqual([p["city_object_id"] for p in store.refresh()["points"]], [1, 3])
        final = store.refresh()
        self.assertEqual([p["city_object_id"] for p in final["points"]], [3])
        self.assertEqual(final["status"], "current")
        self.assertFalse(final["updating"])

    def test_all_closed_is_a_valid_empty_eligible_list(self):
        store = self.store(lambda: gml([record(1, status="Fermé"), record(2, status="Fermé")]))
        result = store.refresh()
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["points"], [])
        self.assertEqual(result["available_count"], 0)

    def test_strict_quality_shower_filter_and_stable_sort(self):
        records = [record(5, name="Robinet test"), record(4, name="Douche test"),
                   record(3, quality="Eau de source non contrôlée"), record(2, quality="Eau non potable"),
                   record(1, name="Fontaine à boire test")]
        result = self.store(lambda: gml(records)).refresh()
        self.assertEqual([p["city_object_id"] for p in result["points"]], [1, 5])
        self.assertTrue(all(p["station_type"] == "drinking_tap" for p in result["points"]))

    def test_invalid_or_truncated_responses_retain_last_good_list(self):
        invalid = [b"not XML", b"<ServiceException>down</ServiceException>", gml([]),
                   gml([record()], declared=2), gml([record(), record()]),
                   gml([record(east=float("nan"))]), gml([{**record(), "qualiteeau": ""}])]
        for body in invalid:
            with self.subTest(body=body[:70]):
                store = self.store(lambda: body)
                with self.assertRaises((ValueError, water.ET.ParseError)):
                    store.refresh()
                snapshot = store.snapshot(NOW)
                self.assertEqual(snapshot["status"], "delayed")
                self.assertEqual(snapshot["available_count"], 1)
                self.assertEqual(snapshot["checked_at"], water._iso(NOW))
                self.assertFalse(snapshot["updating"])

    def test_mass_record_drop_is_rejected_but_half_is_allowed(self):
        self.seed["water_source"]["citywide_records"] = 6
        self.seed_path.write_text(json.dumps(self.seed))
        fetcher = Mock(side_effect=[gml([record(1), record(2)]), gml([record(1), record(2), record(3)])])
        store = self.store(fetcher)
        with self.assertRaisesRegex(ValueError, "half"):
            store.refresh()
        self.assertEqual(store.refresh()["available_count"], 3)

    def test_failure_is_delayed_then_unavailable_and_recovers(self):
        fetcher = Mock(side_effect=[OSError("offline"), gml([record(1), record(2)])])
        store = self.store(fetcher)
        self.mock_datetime.now.return_value = NOW + timedelta(minutes=10)
        with self.assertRaises(OSError):
            store.refresh()
        delayed = store.snapshot(NOW + timedelta(minutes=20))
        self.assertEqual(delayed["status"], "delayed")
        self.assertEqual(delayed["checked_at"], water._iso(NOW))
        self.assertEqual(delayed["last_attempt_at"], water._iso(NOW + timedelta(minutes=10)))
        self.assertIn("offline", delayed["error"])
        self.assertEqual(store.snapshot(NOW + timedelta(minutes=60))["available_count"], 1)
        grace = store.snapshot(NOW + timedelta(seconds=3650))
        self.assertEqual(grace["status"], "delayed")
        self.assertEqual(grace["available_count"], 1)
        expired = store.snapshot(NOW + timedelta(minutes=62))
        self.assertEqual(expired["status"], "unavailable")
        self.assertEqual(expired["points"], [])
        self.mock_datetime.now.return_value = NOW + timedelta(minutes=70)
        recovered = store.refresh()
        self.assertEqual(recovered["status"], "current")
        self.assertEqual(recovered["available_count"], 2)
        self.assertIsNone(recovered["error"])

    def test_hourly_checks_allow_request_and_browser_poll_grace_before_expiry(self):
        store = self.store(lambda: gml([record(1), record(2)]))
        result = store.refresh()
        self.assertEqual(result["refresh_seconds"], 3600)
        self.assertEqual(result["max_age_seconds"], 3680)
        during_poll_grace = store.snapshot(NOW + timedelta(hours=1, seconds=59))
        self.assertEqual(during_poll_grace["status"], "current")
        self.assertEqual(during_poll_grace["available_count"], 2)
        self.assertEqual(during_poll_grace["checked_at"], water._iso(NOW))
        self.assertEqual(store.snapshot(NOW + timedelta(seconds=3680))["available_count"], 2)
        expired = store.snapshot(NOW + timedelta(seconds=3681))
        self.assertEqual(expired["status"], "unavailable")
        self.assertEqual(expired["points"], [])
        self.assertEqual(expired["checked_at"], water._iso(NOW))

    def test_cache_is_atomic_newer_saved_data_and_not_a_fresh_check(self):
        store = self.store(lambda: gml([record(1), record(2)]))
        later = NOW + timedelta(minutes=10)
        self.mock_datetime.now.return_value = later
        store.refresh()
        cached = json.loads(self.cache_path.read_text())
        self.assertEqual(cached["checked_at"], water._iso(later))
        self.assertEqual(cached["alignment_sha256"], water.IllustrationAlignment(self.alignment_path).signature)
        self.assertEqual(list(self.cache_path.parent.glob("*.tmp")), [])
        reloaded = self.store(Mock())
        self.assertEqual(reloaded.snapshot(later)["status"], "saved")
        self.assertEqual(reloaded.snapshot(later)["checked_at"], water._iso(later))
        self.assertEqual(reloaded.snapshot(later)["available_count"], 2)

    def test_invalid_cache_does_not_discard_seed(self):
        self.cache_path.parent.mkdir()
        self.cache_path.write_text('{"checked_at":"invalid"}')
        with self.assertLogs(level="WARNING"):
            store = self.store()
        self.assertEqual(store.snapshot(NOW)["available_count"], 1)

    def test_future_cache_does_not_replace_seed(self):
        self.cache_path.parent.mkdir()
        self.cache_path.write_text(json.dumps({"checked_at": water._iso(NOW + timedelta(days=1)),
                                              "raw_count": 2, "points": [],
                                              "alignment_sha256": water.IllustrationAlignment(self.alignment_path).signature}))
        with self.assertLogs(level="WARNING"):
            store = self.store()
        self.assertEqual(store.snapshot(NOW)["checked_at"], water._iso(NOW))

    def test_snapshot_cannot_mutate_the_cached_points(self):
        store = self.store()
        snapshot = store.snapshot(NOW)
        snapshot["points"][0]["lv95"][0] = 0
        snapshot["points"].clear()
        self.assertEqual(store.snapshot(NOW)["points"][0]["lv95"][0], 2538000)

    def test_updating_state_is_exposed_during_fetch(self):
        observed = []
        store = None
        def fetcher():
            observed.append(store.snapshot(NOW))
            return gml([record(1), record(2)])
        store = self.store(fetcher)
        store.refresh()
        self.assertTrue(observed[0]["updating"])
        self.assertEqual(observed[0]["last_attempt_at"], water._iso(NOW))

    def test_piecewise_mapping_edges_and_no_extrapolation_without_scipy(self):
        alignment = water.IllustrationAlignment(self.alignment_path)
        origin = alignment.origin
        lon, lat = origin["longitude"], origin["latitude"]
        self.assertEqual(alignment.position(lon, lat), (500, 700))
        self.assertEqual(alignment.position(lon + 1000 / 76600, lat), (600, 700))
        self.assertIsNone(alignment.position(lon + 1001 / 76600, lat))
        imported = {node.module.split(".")[0] for node in ast.walk(ast.parse(SOURCE.read_text()))
                    if isinstance(node, ast.ImportFrom) and node.module}
        imported.update(alias.name.split(".")[0] for node in ast.walk(ast.parse(SOURCE.read_text()))
                        if isinstance(node, ast.Import) for alias in node.names)
        self.assertFalse(imported & {"numpy", "scipy", "scripts"})

    def test_new_station_outside_control_hull_is_not_shown(self):
        store = self.store(lambda: gml([record(1), record(2, east=2540000)]))
        self.assertEqual([p["city_object_id"] for p in store.refresh()["points"]], [1])

    def test_six_reviewed_stations_outside_hull_are_shown_only_for_matching_ids(self):
        records, placements = self.reviewed_stations()
        self.add_placements(placements)
        alignment = water.IllustrationAlignment(self.alignment_path)
        for row, placement in zip(records, placements):
            longitude, latitude = placement["longitude"], placement["latitude"]
            self.assertIsNone(alignment.position(longitude, latitude))
            self.assertIsNone(alignment.position(longitude, latitude, 999))
            self.assertEqual(alignment.position(longitude, latitude, row["object_id"]),
                             (placement["x"], placement["y"]))
        result = self.store(lambda: gml(records)).refresh()
        self.assertEqual(result["available_count"], 6)
        for point, placement in zip(result["points"], placements):
            self.assertEqual(point["anchor_type"], "reviewed_city_location")
            self.assertEqual(point["position_accuracy"], "approximate_on_illustration")
            for key in ("grouped_outlets", "placement_note", "access_note"):
                self.assertEqual(point[key], placement[key])

    def test_reviewed_placements_cannot_override_closed_or_nonpotable_status(self):
        records, placements = self.reviewed_stations()
        self.add_placements(placements)
        records[0]["statut"] = "Fermé"
        records[1]["qualiteeau"] = "Eau non potable"
        records[2]["name"] = "Douche test"
        result = self.store(lambda: gml(records)).refresh()
        self.assertEqual([point["city_object_id"] for point in result["points"]], [13, 14, 15])

    def test_moved_record_beyond_tolerance_is_not_pinned_to_reviewed_location(self):
        records, placements = self.reviewed_stations()
        self.add_placements(placements)
        records[0]["east"] += 30
        result = self.store(lambda: gml(records)).refresh()
        self.assertEqual([point["city_object_id"] for point in result["points"]], [11, 12, 13, 14, 15])
        alignment = water.IllustrationAlignment(self.alignment_path)
        origin = placements[0]
        self.assertEqual(alignment.position(origin["longitude"] + 19 / 76600, origin["latitude"], 10),
                         (origin["x"], origin["y"]))
        self.assertIsNone(alignment.position(origin["longitude"] + 21 / 76600, origin["latitude"], 10))

    def test_moved_reviewed_record_uses_triangle_fallback_without_reviewed_metadata(self):
        longitude, latitude = water.lv95_to_wgs84(2538000, 1152000)
        placement = {"city_object_id": 1, "longitude": longitude, "latitude": latitude,
                     "x": 100, "y": 500, "coordinate_tolerance_m": 20, "grouped_outlets": 1,
                     "placement_method": "reviewed_illustrated_location", "placement_note": "Reviewed", "access_note": ""}
        self.add_placements([placement])
        alignment = water.IllustrationAlignment(self.alignment_path)
        self.assertEqual(alignment.position(longitude, latitude), (500, 700))
        self.assertEqual(alignment.position(longitude, latitude, 1), (100, 500))
        result = self.store(lambda: gml([record(1, east=2538030), record(2)])).refresh()
        moved = result["points"][0]
        self.assertEqual(moved["anchor_type"], "calibrated_city_location")
        self.assertNotEqual((moved["x"], moved["y"]), (100, 500))
        for key in ("grouped_outlets", "placement_note", "access_note"):
            self.assertNotIn(key, moved)

    def test_malformed_reviewed_placement_metadata_is_rejected(self):
        _, placements = self.reviewed_stations()
        valid = placements[0]
        invalid = [None, {**valid, "city_object_id": True}, {**valid, "city_object_id": 0},
                   {**valid, "x": 21}, {**valid, "y": 1229}, {**valid, "x": float("nan")},
                   {**valid, "longitude": 181}, {**valid, "latitude": float("inf")},
                   {**valid, "longitude": "6.6"}, {**valid, "coordinate_tolerance_m": 0},
                   {**valid, "grouped_outlets": 0}, {**valid, "grouped_outlets": True},
                   {**valid, "placement_method": "unreviewed"}, {**valid, "placement_note": ""},
                   {**valid, "access_note": None}]
        for placement in invalid:
            with self.subTest(placement=placement):
                self.add_placements([placement])
                with self.assertRaises(ValueError):
                    water.IllustrationAlignment(self.alignment_path)
        self.add_placements([valid, copy.deepcopy(valid)])
        with self.assertRaisesRegex(ValueError, "unique"):
            water.IllustrationAlignment(self.alignment_path)
        self.add_placements({})
        with self.assertRaisesRegex(ValueError, "array"):
            water.IllustrationAlignment(self.alignment_path)

    def test_changed_alignment_cache_cannot_replace_seed_with_new_reviewed_stations(self):
        self.mock_datetime.now.return_value = NOW + timedelta(minutes=10)
        self.store(lambda: gml([record(1), record(2)])).refresh()
        records, placements = self.reviewed_stations()
        self.add_placements(placements)
        mapper = water.IllustrationAlignment(self.alignment_path)
        self.seed["scenes"][0]["points"] = water._points([record(1), *records], mapper, water._iso(NOW))
        self.seed["water_source"]["citywide_records"] = 7
        self.seed_path.write_text(json.dumps(self.seed))
        with self.assertLogs(level="WARNING"):
            reloaded = self.store(Mock())
        snapshot = reloaded.snapshot(NOW + timedelta(minutes=10))
        self.assertEqual(snapshot["available_count"], 7)
        self.assertEqual(snapshot["checked_at"], water._iso(NOW))

    def test_legacy_cache_without_alignment_signature_is_ignored(self):
        self.cache_path.parent.mkdir()
        self.cache_path.write_text(json.dumps({"checked_at": water._iso(NOW + timedelta(minutes=10)),
                                              "raw_count": 2, "points": []}))
        with self.assertLogs(level="WARNING"):
            store = self.store()
        self.assertEqual(store.snapshot(NOW)["available_count"], 1)

    def test_loop_rechecks_wall_clock_without_refreshing_before_one_hour(self):
        stop = threading.Event()
        store = Mock()
        attempts = []

        def refresh():
            attempts.append(self.mock_datetime.now.return_value)
            if len(attempts) == 1:
                # Measure the interval from completion, including fetch time.
                self.mock_datetime.now.return_value += timedelta(seconds=20)
            else:
                stop.set()

        def advance(seconds):
            self.assertEqual(len(attempts), 1)
            self.mock_datetime.now.return_value += timedelta(seconds=seconds)

        store.refresh.side_effect = refresh
        with patch.object(stop, "wait", side_effect=advance) as wait:
            water.refresh_loop(store, stop)
        self.assertEqual(attempts, [NOW, NOW + timedelta(seconds=3620)])
        self.assertEqual(wait.call_count, 720)
        self.assertTrue(all(call.args == (5,) for call in wait.call_args_list))

    def test_loop_refreshes_after_one_short_wait_when_wall_clock_jumps_forward(self):
        stop = threading.Event()
        store = Mock()
        attempts = []

        def refresh():
            attempts.append(self.mock_datetime.now.return_value)
            if len(attempts) == 2:
                stop.set()

        def resume_after_sleep(seconds):
            self.mock_datetime.now.return_value += timedelta(hours=4)

        store.refresh.side_effect = refresh
        with patch.object(stop, "wait", side_effect=resume_after_sleep) as wait:
            water.refresh_loop(store, stop)
        self.assertEqual(attempts, [NOW, NOW + timedelta(hours=4)])
        wait.assert_called_once_with(5)

    def test_loop_retries_one_hour_after_a_failed_attempt_completes(self):
        stop = threading.Event()
        store = Mock()
        attempts = []

        def refresh():
            attempts.append(self.mock_datetime.now.return_value)
            if len(attempts) == 1:
                self.mock_datetime.now.return_value += timedelta(seconds=20)
                raise OSError("offline")
            stop.set()

        def advance(seconds):
            self.mock_datetime.now.return_value += timedelta(seconds=seconds)

        store.refresh.side_effect = refresh
        with self.assertLogs(level="ERROR"):
            with patch.object(stop, "wait", side_effect=advance) as wait:
                water.refresh_loop(store, stop)
        self.assertEqual(attempts, [NOW, NOW + timedelta(seconds=3620)])
        self.assertEqual(wait.call_count, 720)
        self.assertTrue(all(call.args == (5,) for call in wait.call_args_list))

    def test_loop_stops_during_wait_without_another_refresh(self):
        stop = threading.Event()
        store = Mock()
        with patch.object(stop, "wait", side_effect=lambda seconds: stop.set()) as wait:
            water.refresh_loop(store, stop)
        store.refresh.assert_called_once_with()
        wait.assert_called_once_with(5)


if __name__ == "__main__":
    unittest.main()
