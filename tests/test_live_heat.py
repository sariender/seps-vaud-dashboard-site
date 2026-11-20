"""Model temperatures and local weather conditions share one hourly request."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT
SPEC = importlib.util.spec_from_file_location("live_heat_tests", DIRECTORY / "heat_service.py")
heat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(heat)
SERVER_SPEC = importlib.util.spec_from_file_location("heat_preview_server_tests", DIRECTORY / "server.py")
server = importlib.util.module_from_spec(SERVER_SPEC)
SERVER_SPEC.loader.exec_module(server)
NOW = datetime(2026, 11, 19, 18, 25, tzinfo=timezone.utc)
HOUR = NOW.replace(minute=0)
ALIGNMENT = DIRECTORY / "web/assets/maps/water-alignment.json"
REQUESTED = [{"longitude": 6.62, "latitude": 46.51}, {"longitude": 6.6201, "latitude": 46.5101},
             {"longitude": 6.63, "latitude": 46.51}, {"longitude": 6.62, "latitude": 46.52}]


def response():
    rows = []
    for index, temperature in enumerate((10, 10, 11, 9)):
        grid = REQUESTED[0 if index == 1 else index]
        rows.append({**grid, "utc_offset_seconds": 0,
                     "hourly_units": {"time": "unixtime", "temperature_2m": "°C"},
                     "hourly": {"time": [int((HOUR + timedelta(hours=h)).timestamp()) for h in (-1, 0, 1)],
                                "temperature_2m": [temperature - 1, temperature, temperature + 1],
                                "weather_code": [1, 61 if index == 2 else 0, 3],
                                "is_day": [1, 0 if index == 2 else 1, 1]}})
    return rows


class NativeHeatDataTests(unittest.TestCase):
    def test_sampler_covers_alignment_with_buffer_in_one_small_batch(self):
        points = heat.sampling_points(ALIGNMENT)
        self.assertEqual(len(points), 49)
        self.assertEqual(len({(p["longitude"], p["latitude"]) for p in points}), 49)
        alignment = json.loads(ALIGNMENT.read_text())
        origin = alignment["origin"]
        ground = [p for t in alignment["triangles"] for p in t["ground"]]
        for key, axis, scale in (("longitude", 0, "east_scale"), ("latitude", 1, "north_scale")):
            low = origin[key] + min(p[axis] for p in ground) / origin[scale]
            high = origin[key] + max(p[axis] for p in ground) / origin[scale]
            self.assertLess(min(p[key] for p in points), low)
            self.assertGreater(max(p[key] for p in points), high)

    def test_request_pins_model_native_cells_and_no_elevation_downscaling(self):
        query = parse_qs(urlsplit(heat.request_url(REQUESTED)).query)
        self.assertEqual(query["models"], ["meteoswiss_icon_ch1"])
        self.assertEqual(query["hourly"], ["temperature_2m,weather_code,is_day"])
        self.assertEqual(query["cell_selection"], ["nearest"])
        self.assertEqual(query["elevation"], ["nan,nan,nan,nan"])
        self.assertEqual(query["timezone"], ["GMT"])
        self.assertEqual(query["timeformat"], ["unixtime"])

    def test_common_current_hour_and_duplicate_cells_are_preserved_honestly(self):
        parsed = heat.parse_response(response(), REQUESTED, now=NOW)
        self.assertEqual(parsed["valid_at"], "2026-11-19T18:00:00Z")
        self.assertEqual(parsed["grid_cells"], 3)
        self.assertEqual(parsed["requested_locations"], 4)
        self.assertEqual([p["temperature_c"] for p in parsed["points"]], [10, 11, 9])
        self.assertEqual(parsed["points"][0]["longitude"], REQUESTED[0]["longitude"])

    def test_conditions_use_current_hour_of_nearest_native_ouchy_cell(self):
        parsed = heat.parse_response(response(), REQUESTED, now=NOW)
        self.assertEqual(parsed["conditions"], {
            "weather_code": 61, "is_day": False,
            "longitude": 6.63, "latitude": 46.51,
            "valid_at": "2026-11-19T18:00:00Z", "location": "Lausanne · Ouchy",
            "source": "Open-Meteo · MeteoSwiss ICON CH1",
        })

    def test_conditions_select_returned_cell_not_requested_coordinate(self):
        rows = response()
        for row in rows[:2]:
            row.update(longitude=6.6277, latitude=46.5079)
            row["hourly"]["weather_code"][1] = 45
        rows[2]["longitude"] = 6.638
        parsed = heat.parse_response(rows, REQUESTED, now=NOW)
        self.assertEqual(parsed["conditions"]["longitude"], 6.6277)
        self.assertEqual(parsed["conditions"]["latitude"], 46.5079)
        self.assertEqual(parsed["conditions"]["weather_code"], 45)

    def test_supported_weather_codes_and_day_night_are_preserved(self):
        for code in (0, 3, 48, 57, 65, 67, 75, 77, 82, 86, 95, 96, 97, 99):
            for day in (0, 1):
                rows = response()
                rows[2]["hourly"]["weather_code"][1] = code
                rows[2]["hourly"]["is_day"][1] = day
                with self.subTest(code=code, day=day):
                    condition = heat.parse_response(rows, REQUESTED, now=NOW)["conditions"]
                    self.assertEqual(condition["weather_code"], code)
                    self.assertIs(condition["is_day"], bool(day))

    def test_missing_or_invalid_conditions_do_not_hide_temperatures_or_use_neighbors(self):
        mutations = [
            lambda hourly: hourly.pop("weather_code"),
            lambda hourly: hourly.pop("is_day"),
            lambda hourly: hourly.__setitem__("weather_code", None),
            lambda hourly: hourly.__setitem__("is_day", None),
            lambda hourly: hourly.__setitem__("weather_code", (0, 61, 3)),
            lambda hourly: hourly["weather_code"].pop(),
            lambda hourly: hourly["is_day"].pop(),
        ]
        for value in (None, 999, -1, True, 61.0, float("nan"), float("inf"), "61"):
            mutations.append(lambda hourly, value=value: hourly["weather_code"].__setitem__(1, value))
        for value in (None, -1, 2, False, 0.0, float("nan"), "0"):
            mutations.append(lambda hourly, value=value: hourly["is_day"].__setitem__(1, value))
        expected_points = heat.parse_response(response(), REQUESTED, now=NOW)["points"]
        for mutation in mutations:
            rows = response()
            mutation(rows[2]["hourly"])
            with self.subTest(hourly=rows[2]["hourly"]):
                parsed = heat.parse_response(rows, REQUESTED, now=NOW)
                self.assertIsNone(parsed["conditions"])
                self.assertEqual(parsed["points"], expected_points)

    def test_inconsistent_conditions_for_duplicate_native_cell_are_unavailable(self):
        rows = response()
        rows[1]["hourly"]["weather_code"][1] = 3
        with patch.dict(heat.CONDITION_LOCATION, REQUESTED[0]):
            parsed = heat.parse_response(rows, REQUESTED, now=NOW)
        self.assertIsNone(parsed["conditions"])
        self.assertEqual(parsed["grid_cells"], 3)

    def test_missing_current_hour_does_not_substitute_previous_or_future_hour(self):
        for instant in (HOUR - timedelta(hours=2), HOUR + timedelta(hours=2)):
            with self.subTest(instant=instant), self.assertRaisesRegex(ValueError, "current forecast hour"):
                heat.parse_response(response(), REQUESTED, now=instant)

    def test_incomplete_location_batch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            heat.parse_response(response()[:-1], REQUESTED, now=NOW)

    def test_malformed_temperatures_units_time_and_remote_coordinates_are_rejected(self):
        mutations = [
            lambda row: row["hourly"]["temperature_2m"].__setitem__(1, None),
            lambda row: row["hourly"]["temperature_2m"].__setitem__(1, float("nan")),
            lambda row: row["hourly"]["temperature_2m"].__setitem__(1, 80),
            lambda row: row["hourly"]["temperature_2m"].pop(),
            lambda row: row["hourly"]["time"].__setitem__(1, row["hourly"]["time"][0]),
            lambda row: row["hourly_units"].__setitem__("temperature_2m", "°F"),
            lambda row: row["hourly_units"].__setitem__("time", "iso8601"),
            lambda row: row.__setitem__("utc_offset_seconds", 3600),
            lambda row: row.__setitem__("longitude", 7.1),
            lambda row: row.__setitem__("latitude", float("inf")),
        ]
        for mutation in mutations:
            rows = response()
            mutation(rows[0])
            with self.subTest(response=rows[0]), self.assertRaises(ValueError):
                heat.parse_response(rows, REQUESTED, now=NOW)

    def test_duplicate_cells_cannot_have_different_temperature_values(self):
        rows = response()
        rows[1]["hourly"]["temperature_2m"][1] += 1
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            heat.parse_response(rows, REQUESTED, now=NOW)

    def test_too_few_independent_cells_are_rejected(self):
        rows = response()
        rows[2].update(longitude=6.62, latitude=46.51)
        rows[2]["hourly"]["temperature_2m"][1] = 10
        with self.assertRaisesRegex(ValueError, "Too few distinct"):
            heat.parse_response(rows, REQUESTED, now=NOW)


class HeatFreshnessTests(unittest.TestCase):
    def store(self, fetcher=None):
        store = heat.HeatStore(ALIGNMENT, fetcher=fetcher or Mock(return_value=response()))
        store.requested = copy.deepcopy(REQUESTED)
        return store

    def refresh(self, store, instant=NOW):
        with patch.object(heat, "datetime") as clock:
            clock.now.return_value = instant
            clock.fromisoformat.side_effect = datetime.fromisoformat
            return store.refresh()

    def test_constructor_and_api_reads_do_not_fetch_upstream(self):
        fetcher = Mock()
        store = self.store(fetcher)
        for _ in range(3):
            result = store.snapshot(now=NOW)
            self.assertEqual(result["status"], "loading")
            self.assertEqual(result["points"], [])
            self.assertIsNone(result["conditions"])
            self.assertIsNone(result["valid_until"])
        fetcher.assert_not_called()

    def test_fixed_hourly_deadline_includes_request_and_browser_poll_grace(self):
        store = self.store()
        first = self.refresh(store)
        self.assertEqual(first["status"], "live")
        self.assertIsNotNone(first["conditions"])
        deadline = NOW + timedelta(seconds=3680)
        self.assertEqual(first["valid_until"], heat._iso(deadline))
        self.assertEqual(first["source"]["resolution_m"], 1000)
        self.assertEqual(store.snapshot(now=deadline)["points"], first["points"])
        self.assertEqual(store.snapshot(now=deadline)["conditions"], first["conditions"])
        expired = store.snapshot(now=deadline + timedelta(seconds=1))
        self.assertEqual(expired["status"], "stale")
        self.assertEqual(expired["points"], [])
        self.assertIsNone(expired["conditions"])

    def test_failure_retains_timestamp_and_then_hides_old_colours(self):
        fetcher = Mock(side_effect=[response(), OSError("offline")])
        store = self.store(fetcher)
        first = self.refresh(store)
        with self.assertRaises(OSError):
            self.refresh(store, NOW + timedelta(minutes=20))
        retained = store.snapshot(now=NOW + timedelta(minutes=30))
        self.assertEqual(retained["status"], "live")
        self.assertIn("delayed", retained["message"])
        for key in ("valid_at", "fetched_at", "valid_until", "points", "conditions"):
            self.assertEqual(retained[key], first[key])
        self.assertEqual(store.snapshot(now=NOW + timedelta(hours=2))["points"], [])
        self.assertIsNone(store.snapshot(now=NOW + timedelta(hours=2))["conditions"])

    def test_failed_first_fetch_is_unavailable_then_recovers(self):
        store = self.store(Mock(side_effect=[ValueError("bad data"), response()]))
        with self.assertRaises(ValueError):
            self.refresh(store)
        self.assertEqual(store.snapshot(now=NOW)["status"], "unavailable")
        self.assertIsNone(store.snapshot(now=NOW)["conditions"])
        self.assertEqual(self.refresh(store)["status"], "live")

    def test_refetch_cannot_relabel_old_hour_as_current(self):
        store = self.store()
        first = self.refresh(store)
        with self.assertRaisesRegex(ValueError, "current forecast hour"):
            self.refresh(store, HOUR + timedelta(hours=2))
        self.assertEqual(store.snapshot(now=NOW)["valid_at"], first["valid_at"])

    def test_future_fetch_after_clock_change_hides_colours(self):
        store = self.store()
        self.refresh(store)
        result = store.snapshot(now=NOW - timedelta(minutes=1))
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["points"], [])
        self.assertIsNone(result["conditions"])


class HeatSchedulingTests(unittest.TestCase):
    def run_loop(self, *, jump=False, fail=False, stop_on_wait=False):
        now, attempts = [NOW], []
        stop = threading.Event()
        store = Mock()

        def refresh():
            attempts.append(now[0])
            if len(attempts) == 1:
                now[0] += timedelta(seconds=20)
                if fail:
                    raise OSError("offline")
            else:
                stop.set()

        def wait(seconds):
            if stop_on_wait:
                stop.set()
            now[0] += timedelta(hours=4) if jump else timedelta(seconds=seconds)

        store.refresh.side_effect = refresh
        with patch.object(heat, "datetime") as clock, patch.object(stop, "wait", side_effect=wait) as waits:
            clock.now.side_effect = lambda zone: now[0]
            if fail:
                with self.assertLogs(level="ERROR"):
                    heat.refresh_loop(store, stop)
            else:
                heat.refresh_loop(store, stop)
        return attempts, waits

    def test_hourly_after_attempt_and_short_interruptible_waits(self):
        attempts, waits = self.run_loop()
        self.assertEqual(attempts, [NOW, NOW + timedelta(seconds=3620)])
        self.assertEqual(sum(c.args[0] for c in waits.call_args_list), 3600)
        self.assertLessEqual(max(c.args[0] for c in waits.call_args_list), 5)

    def test_sleep_recovery_and_failure_keep_hourly_schedule(self):
        attempts, waits = self.run_loop(jump=True)
        self.assertEqual(attempts, [NOW, NOW + timedelta(hours=4, seconds=20)])
        self.assertEqual(waits.call_count, 1)
        attempts, _ = self.run_loop(fail=True)
        self.assertEqual(attempts, [NOW, NOW + timedelta(seconds=3620)])

    def test_stop_interrupts_wait(self):
        attempts, waits = self.run_loop(stop_on_wait=True)
        self.assertEqual(attempts, [NOW])
        self.assertEqual(waits.call_count, 1)


class HeatApiTests(unittest.TestCase):
    def request(self, handler_type, path):
        handler = handler_type.__new__(handler_type)
        handler.path, handler.wfile = path, io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        with patch.object(sys, "path", [str(DIRECTORY), *sys.path]):
            handler.do_GET()
        handler.send_header.assert_any_call("Cache-Control", "no-store")
        return json.loads(handler.wfile.getvalue())

    def test_disabled_endpoint_is_explicit_and_does_not_fetch(self):
        handler = server.make_handler(server.WeatherStore(), mode="design")
        with patch.object(heat, "urlopen") as fetch:
            config = self.request(handler, "/api/config")
            payload = self.request(handler, "/api/map-temperature")
        fetch.assert_not_called()
        self.assertEqual(config["heat_updates"], "disabled")
        self.assertEqual(payload["mode"], "disabled")
        self.assertEqual(payload["points"], [])
        self.assertIsNone(payload["conditions"])

    def test_live_endpoint_serves_store_without_extra_fetches(self):
        store = Mock()
        store.snapshot.return_value = {"mode": "live", "status": "loading", "points": []}
        handler = server.make_handler(server.WeatherStore(), mode="design", heat_store=store)
        config = self.request(handler, "/api/config")
        self.assertEqual(config["mode"], "design")
        self.assertEqual(config["heat_updates"], "live")
        self.assertEqual(config["heat_refresh_seconds"], 3600)
        self.assertEqual(config["heat_poll_seconds"], 60)
        self.assertEqual(self.request(handler, "/api/map-temperature"), store.snapshot.return_value)
        store.snapshot.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
