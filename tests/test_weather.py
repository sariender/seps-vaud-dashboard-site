"""Checks for the dashboard's independent hourly weather updates."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hourly_preview_server_tests", ROOT / "server.py"
)
preview = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preview)
NOW = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)


def observation(age_minutes=10):
    return {
        "observed_at": preview.iso_utc(NOW - timedelta(minutes=age_minutes)),
        "air_temperature_c": 10.2,
        "relative_humidity_percent": 85,
        "wind_speed_m_s": 2.5,
        "solar_w_m2": 85,
        "pressure_hpa": 961.1,
        "wbgt_c": 9.7,
    }


class HourlyFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.store = preview.WeatherStore()

    def test_fresh_source_remains_visible_through_hourly_interval(self):
        self.store.update(observation(), fetched_at=NOW)
        deadline = NOW + timedelta(seconds=3600 + preview.DISPLAY_GRACE_SECONDS)
        for instant in (NOW, NOW + timedelta(minutes=35), NOW + timedelta(minutes=59), deadline):
            with self.subTest(now=instant):
                result = self.store.snapshot(now=instant)
                self.assertEqual(result["status"], "live")
                self.assertIsNotNone(result["risk"])
                self.assertEqual(result["fetched_at"], preview.iso_utc(NOW))
                self.assertEqual(result["valid_until"], preview.iso_utc(deadline))
                self.assertEqual(result["refresh_seconds"], 3600)

    def test_missed_hourly_deadline_hides_risk_then_expires(self):
        self.store.update(observation(), fetched_at=NOW)
        deadline = NOW + timedelta(seconds=3600 + preview.DISPLAY_GRACE_SECONDS)
        expires = deadline + preview.EXPIRES_AFTER
        for instant, expected in ((deadline + timedelta(seconds=1), "stale"),
                                  (expires, "stale"), (expires + timedelta(seconds=1), "unavailable")):
            with self.subTest(now=instant):
                result = self.store.snapshot(now=instant)
                self.assertEqual(result["status"], expected)
                self.assertIsNone(result["risk"])
                self.assertEqual(result["expires_at"], preview.iso_utc(expires))

    def test_source_age_at_fetch_controls_qualification(self):
        for age, expected in ((30, "live"), (30.01, "stale"), (50, "stale"),
                              (60, "stale"), (60.01, "unavailable"), (-1, "unavailable")):
            with self.subTest(age=age):
                self.store.update(observation(age), fetched_at=NOW)
                result = self.store.snapshot(now=NOW)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["risk"] is not None, expected == "live")

    def test_refetching_same_old_source_cannot_renew_live_guidance(self):
        value = observation()
        self.store.update(value, fetched_at=NOW)
        self.assertEqual(self.store.snapshot(now=NOW + timedelta(minutes=50))["status"], "live")
        later = NOW + timedelta(hours=1)
        self.store.update(value, fetched_at=later)
        result = self.store.snapshot(now=later)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["risk"])
        self.assertEqual(result["observation"]["observed_at"], value["observed_at"])

    def test_failure_does_not_extend_check_or_validity_times(self):
        self.store.update(observation(), fetched_at=NOW)
        before = self.store.snapshot(now=NOW)
        self.store.record_failure()
        retained = self.store.snapshot(now=NOW + timedelta(minutes=40))
        self.assertEqual(retained["status"], "live")
        self.assertIsNotNone(retained["message"])
        self.assertEqual(retained["fetched_at"], before["fetched_at"])
        self.assertEqual(retained["valid_until"], before["valid_until"])
        late = NOW + timedelta(seconds=3601 + preview.DISPLAY_GRACE_SECONDS)
        self.assertEqual(self.store.snapshot(now=late)["status"], "stale")

    def test_display_grace_covers_browser_poll_phase_then_expires(self):
        self.store.update(observation(), fetched_at=NOW)
        self.assertEqual(preview.DISPLAY_GRACE_SECONDS,
                         preview.REQUEST_TIMEOUT_SECONDS + preview.WEATHER_POLL_SECONDS)
        # A browser can hold the preceding API response until its next minute.
        before_next_poll = self.store.snapshot(now=NOW + timedelta(hours=1, seconds=59))
        self.assertEqual(before_next_poll["status"], "live")
        self.assertIsNotNone(before_next_poll["risk"])
        after_grace = NOW + timedelta(seconds=3601 + preview.DISPLAY_GRACE_SECONDS)
        result = self.store.snapshot(now=after_grace)
        self.assertEqual(result["status"], "stale")
        self.assertIsNone(result["risk"])

    def test_future_check_and_observation_times_remain_unavailable(self):
        for value, fetched, now in ((observation(), NOW + timedelta(minutes=1), NOW),
                                    (observation(-1), NOW, NOW + timedelta(minutes=2))):
            with self.subTest(fetched=fetched, observed=value["observed_at"]):
                self.store.update(value, fetched_at=fetched)
                result = self.store.snapshot(now=now)
                self.assertEqual(result["status"], "unavailable")
                self.assertIsNone(result["risk"])

    def test_no_observation_has_no_invented_validity_window(self):
        result = self.store.snapshot(now=NOW)
        self.assertEqual(result["status"], "loading")
        self.assertIsNone(result["observation"])
        self.assertIsNone(result["valid_until"])
        self.assertIsNone(result["expires_at"])


class HourlySchedulingTests(unittest.TestCase):
    def run_loop(self, *, jump=False, fail_first=False, stop_on_wait=False):
        now = [NOW]
        attempts = []
        store = Mock()
        stop = threading.Event()

        def fetch():
            attempts.append(now[0])
            if len(attempts) == 1:
                now[0] += timedelta(seconds=20)
                if fail_first:
                    raise OSError("MeteoSwiss temporarily unavailable")
            else:
                stop.set()
            return observation()

        def wait(seconds):
            if stop_on_wait:
                stop.set()
            now[0] += timedelta(hours=4) if jump else timedelta(seconds=seconds)

        with patch.object(preview, "datetime") as clock, \
             patch.object(preview, "fetch_weather", side_effect=fetch), \
             patch.object(stop, "wait", side_effect=wait) as waits:
            clock.now.side_effect = lambda zone: now[0]
            if fail_first:
                with self.assertLogs(level="ERROR"):
                    preview.refresh_loop(store, stop)
            else:
                preview.refresh_loop(store, stop)
        return attempts, waits, store

    def test_source_fetch_runs_hourly_after_previous_attempt_completes(self):
        attempts, waits, store = self.run_loop()
        self.assertEqual(attempts, [NOW, NOW + timedelta(seconds=3620)])
        self.assertEqual(sum(call.args[0] for call in waits.call_args_list), 3600)
        self.assertLessEqual(max(call.args[0] for call in waits.call_args_list), 5)
        self.assertEqual(store.update.call_count, 2)

    def test_resume_from_sleep_fetches_after_one_short_wait(self):
        attempts, waits, _ = self.run_loop(jump=True)
        self.assertEqual(attempts, [NOW, NOW + timedelta(hours=4, seconds=20)])
        self.assertEqual(waits.call_count, 1)
        self.assertLessEqual(waits.call_args.args[0], 5)

    def test_failed_fetch_retries_at_same_hourly_cadence(self):
        attempts, waits, store = self.run_loop(fail_first=True)
        self.assertEqual(attempts, [NOW, NOW + timedelta(seconds=3620)])
        self.assertEqual(sum(call.args[0] for call in waits.call_args_list), 3600)
        store.record_failure.assert_called_once_with()
        store.update.assert_called_once()

    def test_stop_interrupts_wait_without_another_source_request(self):
        attempts, waits, _ = self.run_loop(stop_on_wait=True)
        self.assertEqual(attempts, [NOW])
        self.assertEqual(waits.call_count, 1)


class HourlyApiTests(unittest.TestCase):
    def request(self, handler_type, path):
        handler = handler_type.__new__(handler_type)
        handler.path = path
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.do_GET()
        handler.send_header.assert_any_call("Cache-Control", "no-store")
        return json.loads(handler.wfile.getvalue())

    def test_config_separates_hourly_weather_local_reads_and_water_checks(self):
        handler = preview.make_handler(preview.WeatherStore(), mode="live", water_store=Mock())
        result = self.request(handler, "/api/config")
        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["refresh_seconds"], 3600)
        self.assertEqual(result["weather_poll_seconds"], 60)
        self.assertEqual(result["water_updates"], "live")
        self.assertEqual(result["water_refresh_seconds"], 3600)
        self.assertEqual(result["water_max_age_seconds"], 3680)

    def test_default_design_mode_still_uses_saved_snapshot_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved = {"captured_at": "2026-11-16T12:00:00Z", "observation": observation(1000),
                     "risk": {"id": "low"}, "message": None}
            (root / "design-snapshot.json").write_text(json.dumps(saved))
            store = Mock()
            handler = preview.make_handler(store, root, mode="design")
            with patch.object(preview, "fetch_weather") as fetch:
                result = self.request(handler, "/api/weather")
            self.assertEqual(result["mode"], "design")
            self.assertEqual(result["status"], "preview")
            self.assertEqual(result["observation"], saved["observation"])
            store.snapshot.assert_not_called()
            fetch.assert_not_called()

    def test_live_flag_starts_hourly_weather_worker(self):
        with patch.object(preview, "ThreadingHTTPServer"), \
             patch.object(preview.threading, "Thread") as worker, patch("builtins.print"), \
             patch.object(sys, "path", [str(ROOT), *sys.path]):
            preview.main(["--live"])
        self.assertEqual(worker.call_count, 2)
        self.assertIs(worker.call_args_list[0].kwargs["target"], preview.refresh_loop)
        self.assertEqual(worker.call_args_list[1].kwargs["target"].__module__, "heat_service")
        self.assertEqual(worker.return_value.start.call_count, 2)
        self.assertEqual(preview.REFRESH_SECONDS, 3600)


if __name__ == "__main__":
    unittest.main()
