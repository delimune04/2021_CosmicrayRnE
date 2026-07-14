"""Small, dependency-free data models used by the analysis pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Mapping


@dataclass(frozen=True, slots=True)
class DetectorEvent:
    """One detector event after ADC values have been converted to energy deposition."""

    timestamp: datetime
    edep_ch2: float
    edep_ch3: float
    source: str


@dataclass(frozen=True, slots=True)
class WeatherObservation:
    """Weather and solar measurements associated with one calendar day."""

    day: date
    values: Mapping[str, float]
    source: str


@dataclass(slots=True)
class DailyObservation:
    """Detector measurements aggregated to the daily cadence used in the R&E study."""

    day: date
    flux: int
    edep_ch2: float
    edep_ch3: float
    first_event: datetime
    last_event: datetime
    weather: dict[str, float] = field(default_factory=dict)
    corrected_flux: float | None = None

    @property
    def edep_ratio(self) -> float | None:
        if self.edep_ch3 == 0:
            return None
        return self.edep_ch2 / self.edep_ch3

    @property
    def observation_seconds(self) -> float:
        return max(0.0, (self.last_event - self.first_event).total_seconds())

    @property
    def event_rate_hz(self) -> float | None:
        seconds = self.observation_seconds
        return self.flux / seconds if seconds > 0 else None


@dataclass(frozen=True, slots=True)
class RegressionModel:
    """Ordinary least-squares model in the original units of each feature."""

    feature_names: tuple[str, ...]
    intercept: float
    slopes: tuple[float, ...]
    r_squared: float
    sample_size: int
    reference_values: Mapping[str, float]

    def predict(self, values: Mapping[str, float]) -> float:
        return self.intercept + sum(
            slope * float(values[name])
            for name, slope in zip(self.feature_names, self.slopes, strict=True)
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "feature_names": list(self.feature_names),
            "intercept": self.intercept,
            "slopes": dict(zip(self.feature_names, self.slopes, strict=True)),
            "r_squared": self.r_squared,
            "sample_size": self.sample_size,
            "reference_values": dict(self.reference_values),
        }


@dataclass(frozen=True, slots=True)
class ForbushCandidate:
    """A candidate that satisfies the R&E project's operational FD definition."""

    start_day: date
    nadir_day: date
    recovery_day: date
    baseline_flux: float
    nadir_flux: float
    recovery_flux: float
    drop_fraction: float
    recovery_fraction: float
    decline_days: int
    recovery_days: int
    recovery_slope: float
    recovery_r_squared: float

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "start_day": self.start_day.isoformat(),
            "nadir_day": self.nadir_day.isoformat(),
            "recovery_day": self.recovery_day.isoformat(),
            "baseline_flux": self.baseline_flux,
            "nadir_flux": self.nadir_flux,
            "recovery_flux": self.recovery_flux,
            "drop_fraction": self.drop_fraction,
            "recovery_fraction": self.recovery_fraction,
            "decline_days": self.decline_days,
            "recovery_days": self.recovery_days,
            "recovery_slope": self.recovery_slope,
            "recovery_r_squared": self.recovery_r_squared,
        }


@dataclass(slots=True)
class IngestionReport:
    """Counts that make rejected records auditable instead of silently ignored."""

    source_files: int = 0
    parsed_records: int = 0
    skipped_records: int = 0
    formats: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, object]:
        return {
            "source_files": self.source_files,
            "parsed_records": self.parsed_records,
            "skipped_records": self.skipped_records,
            "formats": sorted(self.formats),
        }
