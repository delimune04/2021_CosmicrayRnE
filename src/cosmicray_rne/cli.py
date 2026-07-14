"""Command-line entry point for the portable R&E analysis pipeline."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Iterable

from .analysis import (
    aggregate_daily,
    apply_weather_correction,
    attach_weather,
    detect_forbush_decreases,
    fit_weather_model,
)
from .ingest import iter_detector_events, load_weather_observations
from .models import DailyObservation, ForbushCandidate, RegressionModel


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate COREA observations, correct their weather dependence, and "
            "search for Forbush-decrease candidates."
        )
    )
    parser.add_argument("--detector", required=True, help="Processed COREA data file or directory")
    parser.add_argument("--weather", help="Daily weather file or directory")
    parser.add_argument("--output-dir", default="output", help="Directory for CSV and JSON outputs")
    parser.add_argument(
        "--detector-format",
        choices=("auto", "processed", "level1"),
        default="auto",
        help="Detector layout; auto detects 9-column processed and 102x5 Level-1 files",
    )
    parser.add_argument("--adc-ceiling", type=int, default=1024, help="ADC full-scale value")
    parser.add_argument(
        "--pedestal",
        type=int,
        default=0,
        help="Value subtracted after ADC inversion; use 100 to reproduce the legacy Level-1 processor",
    )
    parser.add_argument(
        "--features",
        nargs="+",
        default=("temperature", "humidity"),
        help="Weather feature names used by the OLS correction",
    )
    parser.add_argument(
        "--skip-correction",
        action="store_true",
        help="Use uncorrected Flux for FD detection even when weather data is supplied",
    )
    parser.add_argument("--baseline-days", type=int, default=7)
    parser.add_argument("--minimum-drop", type=float, default=0.02)
    parser.add_argument("--min-decline-days", type=int, default=1)
    parser.add_argument("--max-decline-days", type=int, default=7)
    parser.add_argument("--min-recovery-days", type=int, default=5)
    parser.add_argument("--max-recovery-days", type=int, default=12)
    parser.add_argument("--recovered-fraction", type=float, default=0.96)
    parser.add_argument("--minimum-recovery-r-squared", type=float, default=0.9198)
    return parser


def _daily_row(observation: DailyObservation, weather_names: Iterable[str]) -> dict[str, object]:
    row: dict[str, object] = {
        "date": observation.day.isoformat(),
        "flux": observation.flux,
        "edep_ch2": observation.edep_ch2,
        "edep_ch3": observation.edep_ch3,
        "edep_ratio": observation.edep_ratio,
        "first_event": observation.first_event.isoformat(sep=" "),
        "last_event": observation.last_event.isoformat(sep=" "),
        "observation_seconds": observation.observation_seconds,
        "event_rate_hz": observation.event_rate_hz,
        "corrected_flux": observation.corrected_flux,
    }
    row.update({name: observation.weather.get(name) for name in weather_names})
    return row


def _write_daily(path: Path, daily: list[DailyObservation]) -> None:
    weather_names = sorted({name for observation in daily for name in observation.weather})
    field_names = list(_daily_row(daily[0], weather_names)) if daily else ["date", "flux"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=field_names)
        writer.writeheader()
        writer.writerows(_daily_row(observation, weather_names) for observation in daily)


_CANDIDATE_FIELDS = [
    "start_day",
    "nadir_day",
    "recovery_day",
    "baseline_flux",
    "nadir_flux",
    "recovery_flux",
    "drop_fraction",
    "recovery_fraction",
    "decline_days",
    "recovery_days",
    "recovery_slope",
    "recovery_r_squared",
]


def _write_candidates(path: Path, candidates: list[ForbushCandidate]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CANDIDATE_FIELDS)
        writer.writeheader()
        writer.writerows(candidate.as_dict() for candidate in candidates)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(arguments: argparse.Namespace) -> dict[str, object]:
    events, detector_report = iter_detector_events(
        arguments.detector,
        data_format=arguments.detector_format,
        adc_ceiling=arguments.adc_ceiling,
        pedestal=arguments.pedestal,
    )
    daily = aggregate_daily(events)
    if not daily:
        raise ValueError("No valid detector events were parsed")

    weather_report = None
    model: RegressionModel | None = None
    if arguments.weather:
        weather, weather_report = load_weather_observations(arguments.weather)
        attach_weather(daily, weather)
        if not arguments.skip_correction:
            model = fit_weather_model(daily, arguments.features)
            apply_weather_correction(daily, model)

    candidates = detect_forbush_decreases(
        daily,
        use_corrected=model is not None,
        baseline_days=arguments.baseline_days,
        minimum_drop=arguments.minimum_drop,
        min_decline_days=arguments.min_decline_days,
        max_decline_days=arguments.max_decline_days,
        min_recovery_days=arguments.min_recovery_days,
        max_recovery_days=arguments.max_recovery_days,
        recovered_fraction=arguments.recovered_fraction,
        minimum_recovery_r_squared=arguments.minimum_recovery_r_squared,
    )

    output = Path(arguments.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    _write_daily(output / "daily_observations.csv", daily)
    _write_candidates(output / "forbush_candidates.csv", candidates)
    if model is not None:
        _write_json(output / "weather_correction_model.json", model.as_dict())

    summary: dict[str, object] = {
        "detector_ingestion": detector_report.as_dict(),
        "weather_ingestion": weather_report.as_dict() if weather_report else None,
        "daily_observations": len(daily),
        "weather_correction_applied": model is not None,
        "forbush_candidates": len(candidates),
        "outputs": {
            "daily_observations": str(output / "daily_observations.csv"),
            "forbush_candidates": str(output / "forbush_candidates.csv"),
            "weather_model": str(output / "weather_correction_model.json") if model else None,
        },
    }
    _write_json(output / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    try:
        summary = run(arguments)
    except (FileNotFoundError, ValueError) as error:
        _parser().error(str(error))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
