"""Readers for the two COREA detector layouts and R&E weather exports."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator

from .models import DetectorEvent, IngestionReport, WeatherObservation

_TEXT_EXTENSIONS = {".txt", ".csv", ".tsv"}
_NUMBER_SPLIT = re.compile(r"[\s,]+")


def discover_data_files(path: str | Path) -> list[Path]:
    """Return deterministic text-file input paths, accepting a file or directory."""

    root = Path(path)
    if root.is_file():
        return [root]
    if not root.is_dir():
        raise FileNotFoundError(f"Input path does not exist: {root}")
    return sorted(
        candidate
        for candidate in root.rglob("*")
        if candidate.is_file() and candidate.suffix.lower() in _TEXT_EXTENSIONS
    )


def _numbers(line: str) -> list[str]:
    return [part for part in _NUMBER_SPLIT.split(line.strip()) if part]


def _energy(adc: int, adc_ceiling: int, pedestal: int) -> float:
    return float(max(0, adc_ceiling - adc - pedestal))


def _event_from_processed_fields(
    fields: list[str], source: str, adc_ceiling: int, pedestal: int
) -> DetectorEvent:
    if len(fields) < 9:
        raise ValueError("Processed detector record needs nine numeric fields")
    year, month, day, hour, minute, second = (int(value) for value in fields[1:7])
    timestamp = datetime(year, month, day, hour, minute, second)
    return DetectorEvent(
        timestamp=timestamp,
        edep_ch2=_energy(int(fields[7]), adc_ceiling, pedestal),
        edep_ch3=_energy(int(fields[8]), adc_ceiling, pedestal),
        source=source,
    )


def _parse_processed_file(
    path: Path, report: IngestionReport, adc_ceiling: int, pedestal: int
) -> Iterator[DetectorEvent]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            fields = _numbers(line)
            if not fields:
                continue
            try:
                event = _event_from_processed_fields(
                    fields, str(path), adc_ceiling, pedestal
                )
            except (TypeError, ValueError, OverflowError):
                report.skipped_records += 1
                continue
            report.parsed_records += 1
            yield event


def _parse_level1_file(
    path: Path, report: IngestionReport, adc_ceiling: int, pedestal: int
) -> Iterator[DetectorEvent]:
    """Parse the 102 x 5 integer COREA Level-1 event block documented in 2021."""

    block_size = 102
    row_buffer: list[int] = []
    block: list[list[int]] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row_buffer.extend(int(value) for value in _numbers(line))
            except ValueError:
                report.skipped_records += 1
                continue
            while len(row_buffer) >= 5:
                block.append(row_buffer[:5])
                del row_buffer[:5]
                if len(block) != block_size:
                    continue
                yield from _level1_block_event(
                    block, path, report, adc_ceiling, pedestal
                )
                block = []
    if row_buffer or block:
        report.skipped_records += 1


def _level1_block_event(
    block: list[list[int]],
    path: Path,
    report: IngestionReport,
    adc_ceiling: int,
    pedestal: int,
) -> Iterator[DetectorEvent]:
    try:
        timestamp = datetime(
            block[1][2] * 256 + block[1][3],
            block[1][4],
            block[2][1],
            block[2][2],
            block[2][3],
            block[2][4],
        )
    except ValueError:
        report.skipped_records += 1
        return
    channel_rows = block[7:102]
    report.parsed_records += 1
    yield DetectorEvent(
        timestamp=timestamp,
        edep_ch2=sum(_energy(row[2], adc_ceiling, pedestal) for row in channel_rows),
        edep_ch3=sum(_energy(row[3], adc_ceiling, pedestal) for row in channel_rows),
        source=str(path),
    )


def _detect_detector_format(path: Path) -> str:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            fields = _numbers(line)
            if fields:
                return "processed" if len(fields) >= 9 else "level1"
    return "empty"


def iter_detector_events(
    path: str | Path,
    *,
    data_format: str = "auto",
    adc_ceiling: int = 1024,
    pedestal: int = 0,
) -> tuple[Iterator[DetectorEvent], IngestionReport]:
    """Stream COREA detector data without retaining every event in memory."""

    if data_format not in {"auto", "processed", "level1"}:
        raise ValueError("data_format must be 'auto', 'processed', or 'level1'")
    if adc_ceiling <= 0 or pedestal < 0:
        raise ValueError("adc_ceiling must be positive and pedestal cannot be negative")

    report = IngestionReport()

    def stream() -> Iterator[DetectorEvent]:
        for file_path in discover_data_files(path):
            report.source_files += 1
            detected = _detect_detector_format(file_path) if data_format == "auto" else data_format
            if detected == "empty":
                continue
            if detected == "processed":
                yield from _parse_processed_file(file_path, report, adc_ceiling, pedestal)
            else:
                yield from _parse_level1_file(file_path, report, adc_ceiling, pedestal)
            report.formats.add(detected)

    return stream(), report


def load_detector_events(
    path: str | Path,
    *,
    data_format: str = "auto",
    adc_ceiling: int = 1024,
    pedestal: int = 0,
) -> tuple[list[DetectorEvent], IngestionReport]:
    """Load all events; suitable for small files and unit tests.

    For production-sized directories use :func:`iter_detector_events` together
    with :func:`cosmicray_rne.analysis.aggregate_daily`.
    """

    stream, report = iter_detector_events(
        path,
        data_format=data_format,
        adc_ceiling=adc_ceiling,
        pedestal=pedestal,
    )
    return sorted(stream, key=lambda event: event.timestamp), report


def _normalise_name(name: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]", "", name.lower())


_WEATHER_ALIASES: dict[str, tuple[str, ...]] = {
    "temperature": ("temperature", "temp", "온도", "기온"),
    "humidity": ("humidity", "moisture", "relativehumidity", "습도", "상대습도"),
    "pressure": ("pressure", "airpressure", "기압"),
    "sunspots": ("sunspots", "sunspot", "흑점", "흑점수"),
    "solar_flux": ("solarflux", "radioflux", "sfu", "태양플럭스"),
    "geomagnetic_ap": ("geomagneticap", "ap", "지자기ap"),
    "geomagnetic_sum": ("geomagneticsum", "kp", "지자기합"),
}


def _as_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


def _header_value(row: dict[str, str], aliases: Iterable[str]) -> float | None:
    normalised = {_normalise_name(key): value for key, value in row.items() if key}
    for alias in aliases:
        value = _as_float(normalised.get(_normalise_name(alias)))
        if value is not None:
            return value
    return None


def _date_from_header(row: dict[str, str]) -> date | None:
    normalised = {_normalise_name(key): value for key, value in row.items() if key}
    for date_key in ("date", "day", "날짜", "일자"):
        text = normalised.get(_normalise_name(date_key))
        if text:
            try:
                return date.fromisoformat(text.strip().replace("/", "-"))
            except ValueError:
                pass
    year = _as_float(normalised.get("year") or normalised.get("연도"))
    month = _as_float(normalised.get("month") or normalised.get("월"))
    day = _as_float(normalised.get("day") or normalised.get("일"))
    if year is None or month is None or day is None:
        return None
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _parse_headered_weather(path: Path, report: IngestionReport) -> Iterator[WeatherObservation]:
    sample = path.read_text(encoding="utf-8", errors="replace")
    delimiter = "\t" if "\t" in sample.splitlines()[0] else ","
    reader = csv.DictReader(sample.splitlines(), delimiter=delimiter)
    for row in reader:
        day = _date_from_header(row)
        if day is None:
            report.skipped_records += 1
            continue
        values = {
            name: value
            for name, aliases in _WEATHER_ALIASES.items()
            if (value := _header_value(row, aliases)) is not None
        }
        if not values:
            report.skipped_records += 1
            continue
        report.parsed_records += 1
        yield WeatherObservation(day=day, values=values, source=str(path))


def _parse_whitespace_weather(path: Path, report: IngestionReport) -> Iterator[WeatherObservation]:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = _numbers(line)
        if not fields:
            continue
        try:
            day = date(int(fields[0]), int(fields[1]), int(fields[2]))
            numeric = [float(value) for value in fields[3:]]
        except (IndexError, ValueError):
            report.skipped_records += 1
            continue

        if len(numeric) == 3:
            values = {
                "temperature": numeric[0],
                "humidity": numeric[1],
                "sunspots": numeric[2],
            }
        elif len(numeric) >= 13:
            # R&E's 16-column daily export: date, solar data, magnetic data, temp, humidity.
            values = {
                "solar_flux": numeric[0],
                "sunspots": numeric[1],
                "sunspot_area": numeric[2],
                "proton_1mev": numeric[3],
                "proton_10mev": numeric[4],
                "proton_100mev": numeric[5],
                "electron_08mev": numeric[6],
                "electron_2mev": numeric[7],
                "geomagnetic_ap": numeric[8],
                "geomagnetic_sum": numeric[9],
                "smf": numeric[10],
                "temperature": numeric[11],
                "humidity": numeric[12],
            }
        elif len(numeric) >= 2:
            values = {"temperature": numeric[0], "humidity": numeric[1]}
        else:
            report.skipped_records += 1
            continue
        report.parsed_records += 1
        yield WeatherObservation(day=day, values=values, source=str(path))


def _looks_like_header(line: str) -> bool:
    return any(character.isalpha() or "가" <= character <= "힣" for character in line)


def load_weather_observations(
    path: str | Path,
) -> tuple[list[WeatherObservation], IngestionReport]:
    """Load daily weather exports and average duplicate values by calendar day."""

    report = IngestionReport()
    collected: dict[date, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    sources: dict[date, list[str]] = defaultdict(list)
    for file_path in discover_data_files(path):
        report.source_files += 1
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        first = next((line for line in lines if line.strip()), "")
        parser = _parse_headered_weather if _looks_like_header(first) else _parse_whitespace_weather
        parser_name = "headered" if parser is _parse_headered_weather else "whitespace"
        report.formats.add(parser_name)
        for observation in parser(file_path, report):
            sources[observation.day].append(observation.source)
            for name, value in observation.values.items():
                collected[observation.day][name].append(value)

    observations = [
        WeatherObservation(
            day=day,
            values={name: sum(values) / len(values) for name, values in values_by_name.items()},
            source=";".join(sorted(set(sources[day]))),
        )
        for day, values_by_name in sorted(collected.items())
    ]
    return observations, report
