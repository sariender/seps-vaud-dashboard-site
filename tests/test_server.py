"""Offline checks for the dashboard's data and freshness guarantees."""

import io
import json
import math
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
HEADER = "station_abbr;reference_timestamp;tre200s0;ure200s0;fu3010z0;gre000z0;prestas0\n"
ROW = "PUY;16.11.2026 11:50;10.2;85;9.4;85;961.1\n"


def observation(age_minutes=10, wbgt_c=9.7):
    return {
        "observed_at": server.iso_utc(NOW - timedelta(minutes=age_minutes)),
        "air_temperature_c": 10.2,
        "relative_humidity_percent": 85.0,
        "wind_speed_m_s": 9.4 / 3.6,
        "solar_w_m2": 85.0,
        "pressure_hpa": 961.1,
        "wbgt_c": wbgt_c,
    }


class RiskTests(unittest.TestCase):
    def test_thresholds_enter_higher_band(self):
        for value, risk_id in [
            (-5, "low"), (20.999, "low"), (21, "caution"),
            (24.999, "caution"), (25, "warning"),
            (27.999, "warning"), (28, "severe"),
            (30.999, "severe"), (31, "danger"), (40, "danger"),
        ]:
            with self.subTest(value=value):
                self.assertEqual(server.classify_risk(value)["id"], risk_id)

    def test_nonfinite_risk_is_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ValueError):
                server.classify_risk(value)


class ParseTests(unittest.TestCase):
    def test_selects_latest_pully_and_converts_units(self):
        text = (
            "\ufeff" + HEADER
            + "PUY;16.11.2026 11:40;9.2;85;0;75;961.1\n"
            + "LAU;16.11.2026 11:55;30;30;10;800;960\n"
            + ROW
        )
        result = server.parse_observation(text, now=NOW)
        self.assertEqual(result["observed_at"], "2026-11-16T11:50:00Z")
        self.assertEqual(result["air_temperature_c"], 10.2)
        self.assertAlmostEqual(result["wind_speed_m_s"], 9.4 / 3.6)
        self.assertEqual(result["pressure_hpa"], 961.1)

    def test_empty_other_station_and_bad_timestamp_fail(self):
        for text in (HEADER, HEADER + ROW.replace("PUY", "LAU"), HEADER + ROW.replace("16.11.2026 11:50", "bad")):
            with self.subTest(text=text), self.assertRaises(ValueError):
                server.parse_observation(text, now=NOW)

    def test_missing_sentinel_nonfinite_and_implausible_values_fail(self):
        for field_index, replacement in [(2, ""), (2, "-999"), (3, "101"), (3, "nan"), (4, "-1"), (5, "inf"), (6, "0")]:
            row = ROW.strip().split(";")
            row[field_index] = replacement
            with self.subTest(field=field_index, value=replacement), self.assertRaises(ValueError):
                server.parse_observation(HEADER + ";".join(row), now=NOW)

    def test_skips_latest_incomplete_or_invalid_row(self):
        expected = server.parse_observation(HEADER + ROW, now=NOW)
        for field_index, replacement in [(2, ""), (3, "nan"), (4, "-999"), (5, "inf"), (6, "0")]:
            latest = ["PUY", "16.11.2026 11:55", "29", "45", "18", "400", "980"]
            latest[field_index] = replacement
            text = HEADER + ROW + ";".join(latest) + "\n"
            with self.subTest(field=field_index, value=replacement):
                self.assertEqual(server.parse_observation(text, now=NOW), expected)

    def test_multiple_partial_rows_do_not_mix_measurements(self):
        partial = (
            "PUY;16.11.2026 11:56;29;;18;400;980\n"
            "PUY;16.11.2026 11:55;;45;12;300;975\n"
        )
        expected = server.parse_observation(HEADER + ROW, now=NOW)
        self.assertEqual(server.parse_observation(HEADER + partial + ROW, now=NOW), expected)

    def test_complementary_partial_rows_without_a_complete_row_are_rejected(self):
        text = (
            HEADER
            + "PUY;16.11.2026 11:56;29;;18;400;980\n"
            + "PUY;16.11.2026 11:55;;45;12;300;975\n"
        )
        with self.assertRaisesRegex(ValueError, "No complete valid Pully"):
            server.parse_observation(text, now=NOW)

    def test_future_observation_is_not_silently_used(self):
        with self.assertRaises(ValueError):
            server.parse_observation(HEADER + ROW.replace("11:50", "12:10"), now=NOW)

    def test_future_partial_row_is_rejected_even_with_an_older_valid_row(self):
        text = HEADER + ROW + "PUY;16.11.2026 12:10;;45;12;300;975\n"
        with self.assertRaisesRegex(ValueError, "future"):
            server.parse_observation(text, now=NOW)

    def test_nighttime_zero_radiation_is_valid(self):
        self.assertEqual(server.parse_observation(HEADER + ROW.replace(";85;961.1", ";0;961.1"), now=NOW)["solar_w_m2"], 0)


class CalculationTests(unittest.TestCase):
    def test_notebook_parameters_and_natural_model_result(self):
        native = SimpleNamespace(Input=Mock(return_value=object()), calculate=Mock(return_value=SimpleNamespace(status=0, wbgt_c=9.77)))
        with patch.dict(sys.modules, {"lwbgt": native}):
            result = server.calculate_wbgt(observation())
        self.assertEqual(result, 9.77)
        kwargs = native.Input.call_args.kwargs
        self.assertEqual(kwargs["gmt_offset_hours"], 0)
        self.assertEqual(kwargs["averaging_minutes"], 10)
        self.assertEqual(kwargs["wind_height_m"], 10)
        self.assertEqual(kwargs["latitude_deg_north"], 46.512283)
        self.assertEqual(kwargs["longitude_deg_east"], 6.667517)
        self.assertEqual(kwargs["urban"], 1)
        self.assertEqual(kwargs["vertical_temperature_difference_c"], 1)
        self.assertAlmostEqual(kwargs["wind_speed_m_s"], 9.4 / 3.6)

    def test_failed_nonfinite_or_sentinel_result_is_rejected(self):
        for status, value in [(1, 9), (0, math.nan), (0, -9999)]:
            native = SimpleNamespace(Input=Mock(), calculate=Mock(return_value=SimpleNamespace(status=status, wbgt_c=value)))
            with patch.dict(sys.modules, {"lwbgt": native}), self.assertRaises(ValueError):
                server.calculate_wbgt(observation())




class StaticPathTests(unittest.TestCase):
    def test_malformed_paths_are_missing_instead_of_crashing_requests(self):
        root = Path(__file__).resolve().parents[1] / "web"
        handler_type = server.make_handler(server.WeatherStore(), root)
        handler = handler_type.__new__(handler_type)
        for path in ("/%00", "/assets/%00.txt", "/%00/../../server.py"):
            with self.subTest(path=path):
                self.assertEqual(Path(handler.translate_path(path)), root.resolve() / ".not-found")

    def test_traversal_targets_outside_web_are_blocked(self):
        root = Path(__file__).resolve().parents[1] / "web"
        handler_type = server.make_handler(server.WeatherStore(), root)
        handler = handler_type.__new__(handler_type)
        for path in ("/../server.py", "/%2e%2e/.git/config", "/../../etc/passwd"):
            with self.subTest(path=path):
                resolved = Path(handler.translate_path(path))
                self.assertTrue(resolved.is_relative_to(root.resolve()))
                self.assertEqual(resolved.name, ".not-found")
        self.assertEqual(Path(handler.translate_path("/assets/app.js?v=1")), root.resolve() / "assets/app.js")

    def test_symlink_cannot_expose_a_file_outside_web(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "web"
            root.mkdir()
            private = directory / "private.txt"
            private.write_text("not public")
            (root / "linked.txt").symlink_to(private)
            handler_type = server.make_handler(server.WeatherStore(), root)
            handler = handler_type.__new__(handler_type)
            self.assertEqual(Path(handler.translate_path("/linked.txt")), root.resolve() / ".not-found")




if __name__ == "__main__":
    unittest.main()
