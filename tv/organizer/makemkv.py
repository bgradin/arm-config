from __future__ import annotations

import csv
import re
from collections import defaultdict
from typing import Any


ATTRIBUTE_NAMES = {
    1: "type",
    2: "name",
    3: "language_code",
    4: "language_name",
    5: "codec_id",
    6: "codec_short",
    7: "codec_long",
    8: "chapter_count",
    9: "duration",
    10: "disk_size",
    11: "disk_size_bytes",
    13: "bitrate",
    14: "audio_channels",
    15: "angle_info",
    16: "source_file",
    17: "sample_rate",
    18: "sample_size",
    19: "video_size",
    20: "aspect_ratio",
    21: "frame_rate",
    22: "stream_flags",
    23: "date_time",
    24: "original_title_id",
    25: "segment_count",
    26: "segment_map",
    27: "output_file",
    28: "metadata_language",
    29: "tree_info",
    30: "panel_title",
    31: "volume_name",
    32: "order_weight",
    33: "output_format",
}

_VERSION = re.compile(r"\bMakeMKV\s+v([^\s]+)", re.IGNORECASE)


def _row(value: str) -> list[str]:
    return next(csv.reader([value], skipinitialspace=True))


def parse_robot_output(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "disc": {},
        "titles": {},
        "streams": {},
        "messages": [],
        "raw_lines": [],
    }
    title_values: dict[int, dict[str, Any]] = defaultdict(dict)
    title_raw: dict[int, dict[str, Any]] = defaultdict(dict)
    streams: dict[tuple[int, int], dict[str, Any]] = defaultdict(dict)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        result["raw_lines"].append(line)
        if match := _VERSION.search(line):
            result["version"] = match.group(1)
        prefix, separator, body = line.partition(":")
        if not separator:
            continue
        try:
            fields = _row(body)
        except (csv.Error, StopIteration):
            result["messages"].append(line)
            continue

        try:
            if prefix == "CINFO" and len(fields) >= 3:
                attribute = int(fields[0])
                value = fields[-1]
                name = ATTRIBUTE_NAMES.get(attribute, f"attribute_{attribute}")
                result["disc"][name] = value
            elif prefix == "TINFO" and len(fields) >= 4:
                title = int(fields[0])
                attribute = int(fields[1])
                value = fields[-1]
                name = ATTRIBUTE_NAMES.get(attribute, f"attribute_{attribute}")
                title_values[title][name] = value
                title_raw[title][str(attribute)] = value
            elif prefix == "SINFO" and len(fields) >= 5:
                title = int(fields[0])
                stream = int(fields[1])
                attribute = int(fields[2])
                value = fields[-1]
                name = ATTRIBUTE_NAMES.get(attribute, f"attribute_{attribute}")
                streams[(title, stream)][name] = value
            elif prefix in {"MSG", "DRV", "PRGV"}:
                result["messages"].append(line)
        except (ValueError, IndexError):
            result["messages"].append(line)

    for title, values in title_values.items():
        values["title_id"] = title
        values["attributes"] = title_raw[title]
        values["streams"] = [
            {"stream_id": stream_id, **stream_values}
            for (title_id, stream_id), stream_values in sorted(streams.items())
            if title_id == title
        ]
        result["titles"][str(title)] = values
    return result


_DURATION = re.compile(
    r"^(?:(?P<hours>\d+):)?(?P<minutes>\d{1,2}):(?P<seconds>\d{1,2}(?:\.\d+)?)$"
)


def duration_seconds(value: str | None) -> float | None:
    if not value:
        return None
    match = _DURATION.match(value.strip())
    if not match:
        return None
    return (
        int(match.group("hours") or 0) * 3600
        + int(match.group("minutes")) * 60
        + float(match.group("seconds"))
    )