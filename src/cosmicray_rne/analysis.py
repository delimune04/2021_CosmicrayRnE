"""Daily aggregation, weather correction, and Forbush-decrease detection."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from math import isclose, sqrt
from statistics import fmean
from typing import Iterable, Mapping, Sequence

from .models import (
    DailyObservation,
    DetectorEvent,
    ForbushCandidate,
    RegressionModel,
    WeatherObservation,
)


def aggregate_daily(events: Iterable[DetectorEvent]) -> list[DailyObservation]:
    """Aggregate event count and energy by date without retaining every event."""

    buckets: dict[date, list[object]] = {}
    for event in events:
        day = event.timestamp.date()
        existing = buckets.get(day)
        if existing is None:
            buckets[day] = [1, event.edep_ch2, event.edep_ch3, event.timestamp, event.timestamp]
            continue
        existing[0] = int(existing[0]) + 1
        existing[1] = float(existing[1]) + event.edep_ch2
        existing[2] = float(existing[2]) + event.edep_ch3
        existing[3] = min(existing[3], event.timestamp)
        existing[4] = max(existing[4], event.timestamp)

    daily: list[DailyObservation] = []
    for day, values in sorted(buckets.items()):
        daily.append(
            DailyObservation(
                day=day,
                flux=int(values[0]),
                edep_ch2=float(values[1]),
                edep_ch3=float(values[2]),
                first_event=values[3],
                last_event=values[4],
            )
        )
    return daily


def attach_weather(
    daily: Iterable[DailyObservation], weather: Iterable[WeatherObservation]
) -> None:
    """Attach weather values by matching calendar day in place."""

    by_day = {observation.day: dict(observation.values) for observation in weather}
    for observation in daily:
        observation.weather = by_day.get(observation.day, {})


def _solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Solve Ax=b by Gaussian elimination with partial pivoting."""

    size = len(vector)
    augmented = [row[:] + [value] for row, value in zip(matrix, vector, strict=True)]
    for pivot_column in range(size):
        pivot_row = max(range(pivot_column, size), key=lambda row: abs(augmented[row][pivot_column]))
        pivot = augmented[pivot_row][pivot_column]
        if isclose(pivot, 0.0, abs_tol=1e-12):
            raise ValueError("Weather features are linearly dependent or have no variation")
        augmented[pivot_column], augmented[pivot_row] = augmented[pivot_row], augmented[pivot_column]
        pivot = augmented[pivot_column][pivot_column]
        augmented[pivot_column] = [value / pivot for value in augmented[pivot_column]]
        for row in range(size):
            if row == pivot_column:
                continue
            factor = augmented[row][pivot_column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[pivot_column], strict=True)
            ]
    return [augmented[row][-1] for row in range(size)]


def _ordinary_linear_fit(x_values: Sequence[float], y_values: Sequence[float]) -> tuple[float, float, float]:
    """Return slope, intercept, and R-squared for a one-variable least-squares fit."""

    if len(x_values) != len(y_values) or len(x_values) < 2:
        raise ValueError("At least two paired values are required")
    mean_x = fmean(x_values)
    mean_y = fmean(y_values)
    denominator = sum((x - mean_x) ** 2 for x in x_values)
    if isclose(denominator, 0.0, abs_tol=1e-12):
        raise ValueError("Independent values have no variation")
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(x_values, y_values, strict=True)) / denominator
    intercept = mean_y - slope * mean_x
    predictions = [intercept + slope * x for x in x_values]
    sse = sum((actual - predicted) ** 2 for actual, predicted in zip(y_values, predictions, strict=True))
    sst = sum((actual - mean_y) ** 2 for actual in y_values)
    r_squared = 1.0 if isclose(sst, 0.0, abs_tol=1e-12) and isclose(sse, 0.0, abs_tol=1e-12) else 1.0 - sse / sst
    return slope, intercept, r_squared


def fit_weather_model(
    daily: Iterable[DailyObservation], feature_names: Iterable[str]
) -> RegressionModel:
    """Fit Flux ~ weather features with OLS, preserving feature units in the result."""

    names = tuple(feature_names)
    if not names:
        raise ValueError("At least one weather feature is required")
    if len(set(names)) != len(names):
        raise ValueError("Weather feature names cannot be repeated")

    rows = [
        observation
        for observation in daily
        if all(name in observation.weather for name in names)
    ]
    if len(rows) <= len(names):
        raise ValueError("Need more complete daily observations than weather features")

    feature_columns = [[float(row.weather[name]) for row in rows] for name in names]
    means = [fmean(column) for column in feature_columns]
    scales = [sqrt(sum((value - mean) ** 2 for value in column) / len(column)) for column, mean in zip(feature_columns, means, strict=True)]
    if any(isclose(scale, 0.0, abs_tol=1e-12) for scale in scales):
        no_variation = names[next(index for index, scale in enumerate(scales) if isclose(scale, 0.0, abs_tol=1e-12))]
        raise ValueError(f"Weather feature has no variation: {no_variation}")

    design = [
        [1.0] + [
            (float(row.weather[name]) - mean) / scale
            for name, mean, scale in zip(names, means, scales, strict=True)
        ]
        for row in rows
    ]
    target = [float(row.flux) for row in rows]
    width = len(names) + 1
    normal_matrix = [
        [sum(row[left] * row[right] for row in design) for right in range(width)]
        for left in range(width)
    ]
    normal_vector = [sum(row[column] * value for row, value in zip(design, target, strict=True)) for column in range(width)]
    standard_coefficients = _solve_linear_system(normal_matrix, normal_vector)

    slopes = tuple(
        coefficient / scale
        for coefficient, scale in zip(standard_coefficients[1:], scales, strict=True)
    )
    intercept = standard_coefficients[0] - sum(
        slope * mean for slope, mean in zip(slopes, means, strict=True)
    )
    predictions = [intercept + sum(slope * value for slope, value in zip(slopes, feature_values, strict=True)) for feature_values in zip(*feature_columns, strict=True)]
    mean_target = fmean(target)
    sse = sum((actual - predicted) ** 2 for actual, predicted in zip(target, predictions, strict=True))
    sst = sum((actual - mean_target) ** 2 for actual in target)
    r_squared = 1.0 if isclose(sst, 0.0, abs_tol=1e-12) and isclose(sse, 0.0, abs_tol=1e-12) else 1.0 - sse / sst
    return RegressionModel(
        feature_names=names,
        intercept=intercept,
        slopes=slopes,
        r_squared=r_squared,
        sample_size=len(rows),
        reference_values=dict(zip(names, means, strict=True)),
    )


def apply_weather_correction(
    daily: Iterable[DailyObservation], model: RegressionModel
) -> None:
    """Flatten weather dependence around the study-period mean, in place.

    corrected = observed + prediction(mean weather) - prediction(actual weather)
    """

    reference_prediction = model.predict(model.reference_values)
    for observation in daily:
        if all(name in observation.weather for name in model.feature_names):
            observation.corrected_flux = (
                float(observation.flux)
                + reference_prediction
                - model.predict(observation.weather)
            )
        else:
            observation.corrected_flux = None


def _is_consecutive(observations: Sequence[DailyObservation]) -> bool:
    return all(
        (right.day - left.day).days == 1
        for left, right in zip(observations, observations[1:])
    )


def _flux_value(observation: DailyObservation, use_corrected: bool) -> float | None:
    if use_corrected:
        return observation.corrected_flux
    return float(observation.flux)


def detect_forbush_decreases(
    daily: Iterable[DailyObservation],
    *,
    use_corrected: bool = True,
    baseline_days: int = 7,
    minimum_drop: float = 0.02,
    min_decline_days: int = 1,
    max_decline_days: int = 7,
    min_recovery_days: int = 5,
    max_recovery_days: int = 12,
    recovered_fraction: float = 0.96,
    minimum_recovery_r_squared: float = 0.9198,
) -> list[ForbushCandidate]:
    """Find candidates using the thresholds reported in the 2021 R&E presentation.

    The candidate start is the last pre-decrease day.  Its preceding *baseline_days*
    observations establish the expected Flux.  Missing dates reject a candidate rather
    than allowing a detector outage to masquerade as a physical FD event.
    """

    if baseline_days < 2 or min_decline_days < 1 or min_recovery_days < 1:
        raise ValueError("Baseline, decline, and recovery windows must be positive")
    if min_decline_days > max_decline_days:
        raise ValueError("min_decline_days cannot exceed max_decline_days")
    if min_recovery_days > max_recovery_days:
        raise ValueError("min_recovery_days cannot exceed max_recovery_days")
    if not 0 < minimum_drop < 1 or not 0 < recovered_fraction <= 1:
        raise ValueError("Fraction thresholds must be between zero and one")
    if not 0 <= minimum_recovery_r_squared <= 1:
        raise ValueError("minimum_recovery_r_squared must be between zero and one")

    ordered = sorted(daily, key=lambda observation: observation.day)
    values = [_flux_value(observation, use_corrected) for observation in ordered]
    candidates: list[ForbushCandidate] = []
    index = baseline_days
    while index < len(ordered):
        baseline_slice = ordered[index - baseline_days : index]
        if not _is_consecutive(baseline_slice + [ordered[index]]):
            index += 1
            continue
        baseline_values = values[index - baseline_days : index]
        if any(value is None or value <= 0 for value in baseline_values):
            index += 1
            continue
        baseline = fmean(value for value in baseline_values if value is not None)

        final_nadir_index = min(index + max_decline_days, len(ordered) - 1)
        decline_indices = list(range(index + min_decline_days, final_nadir_index + 1))
        if not decline_indices:
            break
        valid_declines = [candidate_index for candidate_index in decline_indices if values[candidate_index] is not None]
        if not valid_declines:
            index += 1
            continue
        nadir_index = min(valid_declines, key=lambda candidate_index: float(values[candidate_index]))
        nadir = float(values[nadir_index])
        drop_fraction = (baseline - nadir) / baseline
        if drop_fraction < minimum_drop:
            index += 1
            continue

        recovery_indices = range(
            nadir_index + min_recovery_days,
            min(nadir_index + max_recovery_days, len(ordered) - 1) + 1,
        )
        recovery_index = next(
            (
                candidate_index
                for candidate_index in recovery_indices
                if values[candidate_index] is not None
                and float(values[candidate_index]) >= baseline * recovered_fraction
            ),
            None,
        )
        if recovery_index is None:
            index += 1
            continue
        event_window = ordered[index : recovery_index + 1]
        if not _is_consecutive(event_window):
            index += 1
            continue

        recovery_values = [float(value) for value in values[nadir_index : recovery_index + 1] if value is not None]
        if len(recovery_values) != recovery_index - nadir_index + 1:
            index += 1
            continue
        slope, _, r_squared = _ordinary_linear_fit(
            list(range(len(recovery_values))), recovery_values
        )
        if slope <= 0 or r_squared < minimum_recovery_r_squared:
            index += 1
            continue
        recovery = float(values[recovery_index])
        candidates.append(
            ForbushCandidate(
                start_day=ordered[index].day,
                nadir_day=ordered[nadir_index].day,
                recovery_day=ordered[recovery_index].day,
                baseline_flux=baseline,
                nadir_flux=nadir,
                recovery_flux=recovery,
                drop_fraction=drop_fraction,
                recovery_fraction=recovery / baseline,
                decline_days=nadir_index - index,
                recovery_days=recovery_index - nadir_index,
                recovery_slope=slope,
                recovery_r_squared=r_squared,
            )
        )
        index = recovery_index + 1
    return candidates
