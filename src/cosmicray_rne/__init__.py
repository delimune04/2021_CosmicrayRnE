"""Tools for analysing COREA cosmic-ray observations."""

from .analysis import (
    aggregate_daily,
    apply_weather_correction,
    detect_forbush_decreases,
    fit_weather_model,
)
from .ingest import iter_detector_events, load_detector_events, load_weather_observations

__all__ = [
    "aggregate_daily",
    "apply_weather_correction",
    "detect_forbush_decreases",
    "fit_weather_model",
    "iter_detector_events",
    "load_detector_events",
    "load_weather_observations",
]
