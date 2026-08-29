from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cosmicray_rne.analysis import (  # noqa: E402
    aggregate_daily,
    apply_weather_correction,
    attach_weather,
    detect_forbush_decreases,
    fit_weather_model,
)
from cosmicray_rne.cli import main  # noqa: E402
from cosmicray_rne.ingest import load_detector_events, load_weather_observations  # noqa: E402
from cosmicray_rne.models import DailyObservation, DetectorEvent, WeatherObservation  # noqa: E402


class IngestionTests(unittest.TestCase):
    def test_processed_detector_and_weather_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            detector = root / "detector.txt"
            detector.write_text(
                "1 2020 6 13 12 59 26 252 105\n"
                "2 2020 6 13 12 59 27 200 300\n"
                "bad row\n",
                encoding="utf-8",
            )
            weather = root / "weather.txt"
            weather.write_text(
                "2020 6 13 72 0 0 650000 20000 3900 150000000 780000 3 6 0 20.0 70.0\n"
                "2020 6 14 19.0 65.0 12\n",
                encoding="utf-8",
            )

            events, detector_report = load_detector_events(detector)
            observations, weather_report = load_weather_observations(weather)

            self.assertEqual(detector_report.parsed_records, 2)
            self.assertEqual(detector_report.skipped_records, 1)
            self.assertEqual(events[0].edep_ch2, 772.0)
            self.assertEqual(events[1].edep_ch3, 724.0)
            self.assertEqual(weather_report.parsed_records, 2)
            self.assertEqual(observations[0].values["temperature"], 20.0)
            self.assertEqual(observations[0].values["humidity"], 70.0)
            self.assertEqual(observations[1].values["sunspots"], 12.0)

    def test_level1_block_reconstructs_time_and_energy(self) -> None:
        rows = [[0, 0, 0, 0, 0] for _ in range(102)]
        rows[1] = [0, 0, 7, 229, 4]  # 2021-04
        rows[2] = [0, 5, 17, 53, 40]
        for index in range(7, 102):
            rows[index] = [index, 0, 900, 800, 0]
        with tempfile.TemporaryDirectory() as temp_dir:
            detector = Path(temp_dir) / "level1.txt"
            detector.write_text("\n".join(" ".join(map(str, row)) for row in rows), encoding="utf-8")
            events, report = load_detector_events(detector)

        self.assertEqual(report.formats, {"level1"})
        self.assertEqual(events[0].timestamp, datetime(2021, 4, 5, 17, 53, 40))
        self.assertEqual(events[0].edep_ch2, 95 * 124)
        self.assertEqual(events[0].edep_ch3, 95 * 224)


class AnalysisTests(unittest.TestCase):
    def _daily(self, day: date, flux: int) -> DailyObservation:
        timestamp = datetime.combine(day, datetime.min.time())
        return DailyObservation(day, flux, 0, 0, timestamp, timestamp)

    def test_aggregation_and_weather_correction(self) -> None:
        events = [
            DetectorEvent(datetime(2020, 6, 13, 12, 0, 0), 924, 824, "test"),
            DetectorEvent(datetime(2020, 6, 13, 12, 0, 1), 824, 724, "test"),
        ]
        daily = aggregate_daily(events)
        self.assertEqual(daily[0].flux, 2)
        self.assertEqual(daily[0].edep_ch2, 1748)
        self.assertEqual(daily[0].edep_ratio, 1748 / 1548)

        start = date(2020, 1, 1)
        synthetic = [self._daily(start + timedelta(days=index), 100 + 2 * index + 3 * (index % 2)) for index in range(8)]
        weather = [
            WeatherObservation(
                observation.day,
                {"temperature": float(index), "humidity": float(index % 2)},
                "test",
            )
            for index, observation in enumerate(synthetic)
        ]
        attach_weather(synthetic, weather)
        model = fit_weather_model(synthetic, ["temperature", "humidity"])
        apply_weather_correction(synthetic, model)
        self.assertAlmostEqual(model.slopes[0], 2.0, places=9)
        self.assertAlmostEqual(model.slopes[1], 3.0, places=9)
        corrected = [observation.corrected_flux for observation in synthetic]
        self.assertAlmostEqual(max(corrected) - min(corrected), 0.0, places=9)

    def test_forbush_definition_detects_one_candidate(self) -> None:
        start = date(2020, 1, 1)
        fluxes = [100] * 8 + [95, 90, 92, 93, 94, 95, 96] + [100] * 4
        daily = [self._daily(start + timedelta(days=index), flux) for index, flux in enumerate(fluxes)]
        candidates = detect_forbush_decreases(daily, use_corrected=False, baseline_days=7)

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.nadir_day, start + timedelta(days=9))
        self.assertAlmostEqual(candidate.drop_fraction, 0.1)
        self.assertEqual(candidate.recovery_days, 5)
        self.assertGreaterEqual(candidate.recovery_r_squared, 0.9198)

    def test_forbush_rejects_date_gaps_and_invalid_windows(self) -> None:
        start = date(2020, 1, 1)
        fluxes = [100] * 8 + [95, 90, 92, 93, 94, 95, 96]
        daily = [self._daily(start + timedelta(days=index), flux) for index, flux in enumerate(fluxes)]
        del daily[11]

        self.assertEqual(
            detect_forbush_decreases(daily, use_corrected=False, baseline_days=7),
            [],
        )
        with self.assertRaisesRegex(ValueError, "min_recovery_days"):
            detect_forbush_decreases(
                daily,
                use_corrected=False,
                min_recovery_days=8,
                max_recovery_days=5,
            )


class CommandTests(unittest.TestCase):
    def test_command_writes_auditable_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            detector = root / "detector.txt"
            weather = root / "weather.txt"
            output = root / "result"
            start = date(2020, 1, 1)
            detector_rows = []
            weather_rows = []
            for offset in range(15):
                day = start + timedelta(days=offset)
                temperature = 10 + offset
                for event_number in range(100 + temperature):
                    detector_rows.append(
                        f"{event_number} {day.year} {day.month} {day.day} 12 0 {event_number % 60} 900 900"
                    )
                weather_rows.append(f"{day.year} {day.month} {day.day} {temperature} 60 0")
            detector.write_text("\n".join(detector_rows), encoding="utf-8")
            weather.write_text("\n".join(weather_rows), encoding="utf-8")

            main(["--detector", str(detector), "--weather", str(weather), "--output-dir", str(output), "--features", "temperature"])

            self.assertTrue((output / "daily_observations.csv").exists())
            self.assertTrue((output / "weather_correction_model.json").exists())
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["weather_correction_applied"])
            with (output / "daily_observations.csv").open(encoding="utf-8", newline="") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 15)


if __name__ == "__main__":
    unittest.main()
