#!/usr/bin/env python3

"""
MPLS playlist parser

Parse Blu-ray MPLS (Movie Playlist) files.

Features:
  - MPLS header / version
  - PlayItems / clip IDs
  - IN / OUT timestamps and durations
  - Multi-angle clips
  - Stream Table (STN)
  - Video/audio/subtitle/graphics streams
  - Stream PIDs
  - Codec names
  - ISO-639 language codes
  - Video resolution / frame-rate metadata
  - Audio channel / sample-rate metadata
  - Playlist marks / chapters
  - Playlist-relative chapter timestamps
  - JSON to stdout by default
  - Optional JSON file output

Usage:
    python -m tv.organizer.mpls 00001.mpls

    python -m tv.organizer.mpls 00001.mpls -o playlist.json

    python -m tv.organizer.mpls 00001.mpls --compact

No third-party packages are required.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path
from typing import Any, BinaryIO


TICKS_PER_SECOND = 45000.0


# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

CODECS = {
    0x01: "MPEG-1 Video",
    0x02: "MPEG-2 Video",
    0x1B: "H.264 / AVC",
    0x20: "H.264 MVC",
    0x24: "H.265 / HEVC",
    0xEA: "VC-1",

    0x03: "MPEG-1 Audio",
    0x04: "MPEG-2 Audio",
    0x80: "LPCM",
    0x81: "Dolby Digital / AC-3",
    0x82: "DTS",
    0x83: "Dolby TrueHD",
    0x84: "Dolby Digital Plus / E-AC-3",
    0x85: "DTS-HD High Resolution",
    0x86: "DTS-HD Master Audio",
    0xA1: "Dolby Digital Plus / Secondary Audio",
    0xA2: "DTS-HD / Secondary Audio",

    0x90: "Presentation Graphics (PGS)",
    0x91: "Interactive Graphics",
    0x92: "Text Subtitle",
}


VIDEO_FORMATS = {
    1: "480i",
    2: "576i",
    3: "480p",
    4: "1080i",
    5: "720p",
    6: "1080p",
    7: "576p",
    8: "2160p",
}


VIDEO_FRAME_RATES = {
    1: 23.976,
    2: 24.0,
    3: 25.0,
    4: 29.97,
    6: 50.0,
    7: 59.94,
}


ASPECT_RATIOS = {
    2: "4:3",
    3: "16:9",
}


AUDIO_FORMATS = {
    1: "Mono",
    3: "Stereo",
    6: "Multi-channel",
    12: "Stereo + Multi-channel",
}


AUDIO_SAMPLE_RATES = {
    1: "48 kHz",
    4: "96 kHz",
    5: "192 kHz",
    12: "48/192 kHz",
    14: "48/96 kHz",
}


STREAM_TYPES = {
    1: "PlayItem",
    2: "SubPath + SubClip",
    3: "SubPath",
    4: "SubPath + SubClip",
}


MARK_TYPES = {
    1: "Entry Mark",
    2: "Link Point",
}


class MPLSError(Exception):
    """Raised when an MPLS file cannot be parsed."""


# ---------------------------------------------------------------------------
# Binary helpers
# ---------------------------------------------------------------------------

def read_exact(f: BinaryIO, size: int) -> bytes:
    data = f.read(size)

    if len(data) != size:
        raise MPLSError(
            f"Unexpected end of file: wanted {size} bytes, got {len(data)}"
        )

    return data


def u8(f: BinaryIO) -> int:
    return read_exact(f, 1)[0]


def u16(f: BinaryIO) -> int:
    return struct.unpack(">H", read_exact(f, 2))[0]


def u32(f: BinaryIO) -> int:
    return struct.unpack(">I", read_exact(f, 4))[0]


def ascii_string(data: bytes) -> str:
    return data.decode("ascii", errors="replace").rstrip("\x00")


def ticks_to_seconds(ticks: int) -> float:
    return ticks / TICKS_PER_SECOND


def format_timestamp_from_seconds(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60

    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


def format_timestamp(ticks: int) -> str:
    return format_timestamp_from_seconds(ticks_to_seconds(ticks))


# ---------------------------------------------------------------------------
# Stream parsing
# ---------------------------------------------------------------------------

def parse_stream_entry(f: BinaryIO) -> dict[str, Any]:
    """
    Parse one MPLS StreamEntry block.

    StreamEntry is length-prefixed independently from StreamAttributes.
    """

    length = u8(f)
    start = f.tell()
    end = start + length

    result: dict[str, Any] = {
        "entry_length": length,
    }

    if length == 0:
        return result

    stream_type = u8(f)

    result["stream_type"] = stream_type
    result["stream_type_name"] = STREAM_TYPES.get(
        stream_type,
        f"Unknown ({stream_type:#04x})",
    )

    try:
        if stream_type == 1:
            result["pid"] = u16(f)

        elif stream_type in (2, 4):
            result["subpath_id"] = u8(f)
            result["subclip_id"] = u8(f)
            result["pid"] = u16(f)

        elif stream_type == 3:
            result["subpath_id"] = u8(f)
            result["pid"] = u16(f)

    finally:
        # Always obey the declared block boundary.
        f.seek(end)

    if "pid" in result:
        result["pid_hex"] = f"0x{result['pid']:04X}"

    return result


def parse_stream_attributes(f: BinaryIO) -> dict[str, Any]:
    """
    Parse a length-prefixed StreamAttributes block.
    """

    length = u8(f)
    start = f.tell()
    end = start + length

    result: dict[str, Any] = {
        "attribute_length": length,
    }

    if length == 0:
        return result

    coding_type = u8(f)

    result["coding_type"] = coding_type
    result["coding_type_hex"] = f"0x{coding_type:02X}"
    result["codec"] = CODECS.get(
        coding_type,
        f"Unknown ({coding_type:#04x})",
    )

    remaining = lambda: end - f.tell()

    try:
        # ---------------------------------------------------------------
        # Video
        # ---------------------------------------------------------------

        if coding_type in {
            0x01,  # MPEG-1
            0x02,  # MPEG-2
            0x1B,  # AVC
            0x20,  # MVC
            0x24,  # HEVC
            0xEA,  # VC-1
        }:
            if remaining() >= 1:
                value = u8(f)

                video_format = (value >> 4) & 0x0F
                frame_rate = value & 0x0F

                result["video_format_code"] = video_format
                result["video_format"] = VIDEO_FORMATS.get(
                    video_format,
                    f"Unknown ({video_format})",
                )

                result["frame_rate_code"] = frame_rate

                if frame_rate in VIDEO_FRAME_RATES:
                    result["frame_rate"] = VIDEO_FRAME_RATES[frame_rate]

            if remaining() >= 1:
                value = u8(f)

                aspect_ratio = (value >> 4) & 0x0F

                result["aspect_ratio_code"] = aspect_ratio
                result["aspect_ratio"] = ASPECT_RATIOS.get(
                    aspect_ratio,
                    f"Unknown ({aspect_ratio})",
                )

            # Later MPLS versions may carry extra video metadata here.
            # Keep it rather than discarding it.
            if remaining() > 0:
                result["extra_attributes_hex"] = read_exact(
                    f,
                    remaining(),
                ).hex()

        # ---------------------------------------------------------------
        # Audio
        # ---------------------------------------------------------------

        elif coding_type in {
            0x03,
            0x04,
            0x80,
            0x81,
            0x82,
            0x83,
            0x84,
            0x85,
            0x86,
            0xA1,
            0xA2,
        }:
            if remaining() >= 1:
                value = u8(f)

                audio_format = (value >> 4) & 0x0F
                sample_rate = value & 0x0F

                result["audio_format_code"] = audio_format
                result["audio_format"] = AUDIO_FORMATS.get(
                    audio_format,
                    f"Unknown ({audio_format})",
                )

                result["sample_rate_code"] = sample_rate
                result["sample_rate"] = AUDIO_SAMPLE_RATES.get(
                    sample_rate,
                    f"Unknown ({sample_rate})",
                )

            if remaining() >= 3:
                result["language"] = ascii_string(read_exact(f, 3))

            if remaining() > 0:
                result["extra_attributes_hex"] = read_exact(
                    f,
                    remaining(),
                ).hex()

        # ---------------------------------------------------------------
        # Presentation / interactive graphics
        # ---------------------------------------------------------------

        elif coding_type in {0x90, 0x91}:
            if remaining() >= 3:
                result["language"] = ascii_string(read_exact(f, 3))

            if remaining() > 0:
                result["extra_attributes_hex"] = read_exact(
                    f,
                    remaining(),
                ).hex()

        # ---------------------------------------------------------------
        # Text subtitle
        # ---------------------------------------------------------------

        elif coding_type == 0x92:
            if remaining() >= 1:
                result["character_code"] = u8(f)

            if remaining() >= 3:
                result["language"] = ascii_string(read_exact(f, 3))

            if remaining() > 0:
                result["extra_attributes_hex"] = read_exact(
                    f,
                    remaining(),
                ).hex()

        else:
            if remaining() > 0:
                result["raw_attributes_hex"] = read_exact(
                    f,
                    remaining(),
                ).hex()

    finally:
        f.seek(end)

    return result


def parse_stream(
    f: BinaryIO,
    category: str,
    index: int,
) -> dict[str, Any]:

    result = {
        "index": index,
        "category": category,
    }

    result.update(parse_stream_entry(f))
    result.update(parse_stream_attributes(f))

    return result


def parse_secondary_audio_refs(
    f: BinaryIO,
    section_end: int,
) -> dict[str, Any]:
    """
    Secondary audio streams may reference primary audio streams.
    """

    result: dict[str, Any] = {}

    if f.tell() >= section_end:
        return result

    count = u8(f)

    result["primary_audio_reference_count"] = count
    result["primary_audio_references"] = []

    for _ in range(count):
        if f.tell() >= section_end:
            break

        result["primary_audio_references"].append(u8(f))

    # Reference lists are padded to word alignment.
    if count % 2 == 0 and f.tell() < section_end:
        f.read(1)

    return result


def parse_secondary_video_refs(
    f: BinaryIO,
    section_end: int,
) -> dict[str, Any]:

    result: dict[str, Any] = {}

    if f.tell() >= section_end:
        return result

    audio_count = u8(f)

    result["secondary_audio_reference_count"] = audio_count
    result["secondary_audio_references"] = []

    for _ in range(audio_count):
        if f.tell() >= section_end:
            break

        result["secondary_audio_references"].append(u8(f))

    if audio_count % 2 == 0 and f.tell() < section_end:
        f.read(1)

    if f.tell() < section_end:
        pg_count = u8(f)

        result["pip_pg_reference_count"] = pg_count
        result["pip_pg_references"] = []

        for _ in range(pg_count):
            if f.tell() >= section_end:
                break

            result["pip_pg_references"].append(u8(f))

        if pg_count % 2 == 0 and f.tell() < section_end:
            f.read(1)

    return result


def parse_stn_table(f: BinaryIO, play_item_end: int) -> dict[str, Any]:
    """
    Parse the Stream Number Table (STN_table).
    """

    if f.tell() + 2 > play_item_end:
        return {}

    table_length = u16(f)
    table_start = f.tell()
    table_end = min(table_start + table_length, play_item_end)

    result: dict[str, Any] = {
        "length": table_length,
    }

    if table_length == 0:
        return result

    if table_end - f.tell() < 14:
        result["raw_hex"] = read_exact(
            f,
            max(0, table_end - f.tell()),
        ).hex()

        return result

    # Reserved
    read_exact(f, 2)

    counts = {
        "primary_video": u8(f),
        "primary_audio": u8(f),
        "presentation_graphics": u8(f),
        "interactive_graphics": u8(f),
        "secondary_audio": u8(f),
        "secondary_video": u8(f),
        "pip_presentation_graphics": u8(f),
        "dolby_vision": u8(f),
    }

    result["counts"] = counts

    # Reserved
    read_exact(f, 4)

    streams: dict[str, list[dict[str, Any]]] = {}

    def parse_category(name: str, count: int) -> None:
        category_streams = []

        for index in range(count):
            if f.tell() >= table_end:
                break

            category_streams.append(
                parse_stream(f, name, index)
            )

        streams[name] = category_streams

    parse_category(
        "primary_video",
        counts["primary_video"],
    )

    parse_category(
        "primary_audio",
        counts["primary_audio"],
    )

    parse_category(
        "presentation_graphics",
        counts["presentation_graphics"],
    )

    parse_category(
        "interactive_graphics",
        counts["interactive_graphics"],
    )

    # Secondary audio can contain reference information.
    streams["secondary_audio"] = []

    for index in range(counts["secondary_audio"]):
        if f.tell() >= table_end:
            break

        stream = parse_stream(
            f,
            "secondary_audio",
            index,
        )

        stream.update(
            parse_secondary_audio_refs(
                f,
                table_end,
            )
        )

        streams["secondary_audio"].append(stream)

    # Secondary video can also contain references to other streams.
    streams["secondary_video"] = []

    for index in range(counts["secondary_video"]):
        if f.tell() >= table_end:
            break

        stream = parse_stream(
            f,
            "secondary_video",
            index,
        )

        stream.update(
            parse_secondary_video_refs(
                f,
                table_end,
            )
        )

        streams["secondary_video"].append(stream)

    parse_category(
        "pip_presentation_graphics",
        counts["pip_presentation_graphics"],
    )

    parse_category(
        "dolby_vision",
        counts["dolby_vision"],
    )

    result["streams"] = streams

    if f.tell() < table_end:
        result["unparsed_bytes"] = table_end - f.tell()
        result["unparsed_hex"] = read_exact(
            f,
            table_end - f.tell(),
        ).hex()

    f.seek(table_end)

    return result


# ---------------------------------------------------------------------------
# PlayItems
# ---------------------------------------------------------------------------

def parse_play_item(
    f: BinaryIO,
    index: int,
) -> dict[str, Any]:

    item_length = u16(f)
    item_start = f.tell()
    item_end = item_start + item_length

    clip_id = ascii_string(read_exact(f, 5))
    codec_id = ascii_string(read_exact(f, 4))

    flags = u16(f)

    multi_angle = bool(flags & 0x0010)
    connection_condition = flags & 0x000F

    stc_id = u8(f)

    in_time = u32(f)
    out_time = u32(f)

    # User Operation mask
    uo_mask = read_exact(f, 8)

    random_access_byte = u8(f)
    random_access_flag = bool(random_access_byte & 0x80)

    still_mode = u8(f)
    still_time = u16(f)

    result: dict[str, Any] = {
        "index": index,
        "clip_id": clip_id,
        "clip_file": f"{clip_id}.m2ts",
        "codec_id": codec_id,
        "stc_id": stc_id,
        "connection_condition": connection_condition,
        "multi_angle": multi_angle,

        "in_time": in_time,
        "out_time": out_time,

        "in_time_seconds": round(
            ticks_to_seconds(in_time),
            6,
        ),

        "out_time_seconds": round(
            ticks_to_seconds(out_time),
            6,
        ),

        "duration_seconds": round(
            ticks_to_seconds(out_time - in_time),
            6,
        ),

        "in_timestamp": format_timestamp(in_time),
        "out_timestamp": format_timestamp(out_time),

        "random_access": random_access_flag,

        "still_mode": still_mode,
        "still_time": still_time,

        "uo_mask_hex": uo_mask.hex(),
    }

    # ------------------------------------------------------------------
    # Multi-angle information
    # ------------------------------------------------------------------

    if multi_angle and f.tell() < item_end:
        angle_count = u8(f)
        angle_flags = u8(f)

        angles = [
            {
                "angle": 1,
                "clip_id": clip_id,
                "clip_file": f"{clip_id}.m2ts",
                "codec_id": codec_id,
                "stc_id": stc_id,
            }
        ]

        for angle_index in range(1, angle_count):
            if f.tell() + 10 > item_end:
                break

            angle_clip = ascii_string(read_exact(f, 5))
            angle_codec = ascii_string(read_exact(f, 4))
            angle_stc = u8(f)

            angles.append(
                {
                    "angle": angle_index + 1,
                    "clip_id": angle_clip,
                    "clip_file": f"{angle_clip}.m2ts",
                    "codec_id": angle_codec,
                    "stc_id": angle_stc,
                }
            )

        result["angle_count"] = angle_count
        result["angle_flags"] = angle_flags
        result["angles"] = angles

    # ------------------------------------------------------------------
    # STN table
    # ------------------------------------------------------------------

    if f.tell() < item_end:
        try:
            result["stn_table"] = parse_stn_table(
                f,
                item_end,
            )
        except (MPLSError, struct.error):
            # Preserve the remainder rather than making the entire
            # playlist unusable on an unfamiliar extension.
            if f.tell() < item_end:
                result["stn_parse_error_offset"] = f.tell()
                result["remaining_hex"] = read_exact(
                    f,
                    item_end - f.tell(),
                ).hex()

    f.seek(item_end)

    return result


# ---------------------------------------------------------------------------
# Playlist
# ---------------------------------------------------------------------------

def parse_playlist(
    f: BinaryIO,
    playlist_offset: int,
) -> dict[str, Any]:

    f.seek(playlist_offset)

    section_length = u32(f)
    section_start = f.tell()
    section_end = section_start + section_length

    # Reserved
    u16(f)

    play_item_count = u16(f)
    sub_path_count = u16(f)

    play_items = []

    cumulative_seconds = 0.0

    for index in range(play_item_count):
        item = parse_play_item(f, index)

        item["playlist_start_seconds"] = round(
            cumulative_seconds,
            6,
        )

        item["playlist_start_timestamp"] = (
            format_timestamp_from_seconds(cumulative_seconds)
        )

        cumulative_seconds += item["duration_seconds"]

        item["playlist_end_seconds"] = round(
            cumulative_seconds,
            6,
        )

        item["playlist_end_timestamp"] = (
            format_timestamp_from_seconds(cumulative_seconds)
        )

        play_items.append(item)

    # ------------------------------------------------------------------
    # SubPaths
    # ------------------------------------------------------------------

    sub_paths = []

    for index in range(sub_path_count):
        if f.tell() + 4 > section_end:
            break

        offset = f.tell()
        length = u32(f)

        end = min(
            f.tell() + length,
            section_end,
        )

        raw = read_exact(
            f,
            max(0, end - f.tell()),
        )

        sub_paths.append(
            {
                "index": index,
                "offset": offset,
                "length": length,
                "raw_hex": raw.hex(),
            }
        )

        f.seek(end)

    return {
        "length": section_length,
        "play_item_count": play_item_count,
        "sub_path_count": sub_path_count,
        "duration_seconds": round(cumulative_seconds, 6),
        "duration_timestamp": format_timestamp_from_seconds(
            cumulative_seconds
        ),
        "play_items": play_items,
        "sub_paths": sub_paths,
    }


# ---------------------------------------------------------------------------
# Playlist marks / chapters
# ---------------------------------------------------------------------------

def calculate_playlist_mark_time(
    mark: dict[str, Any],
    play_items: list[dict[str, Any]],
) -> float | None:

    ref = mark["play_item_ref"]

    if ref >= len(play_items):
        return None

    item = play_items[ref]

    # Mark timestamps use the referenced clip's presentation timeline.
    offset_ticks = mark["mark_time"] - item["in_time"]

    return (
        item["playlist_start_seconds"]
        + ticks_to_seconds(offset_ticks)
    )


def parse_marks(
    f: BinaryIO,
    marks_offset: int,
    play_items: list[dict[str, Any]],
) -> dict[str, Any] | None:

    if not marks_offset:
        return None

    f.seek(marks_offset)

    section_length = u32(f)

    if section_length < 2:
        return {
            "length": section_length,
            "mark_count": 0,
            "marks": [],
        }

    mark_count = u16(f)

    marks = []

    for index in range(mark_count):
        # A playlist mark record is 14 bytes.
        reserved = u8(f)

        mark_type = u8(f)
        play_item_ref = u16(f)
        mark_time = u32(f)
        entry_es_pid = u16(f)
        duration = u32(f)

        mark = {
            "index": index,

            "reserved": reserved,

            "type": mark_type,
            "type_name": MARK_TYPES.get(
                mark_type,
                f"Unknown ({mark_type})",
            ),

            "play_item_ref": play_item_ref,

            "mark_time": mark_time,
            "mark_time_seconds": round(
                ticks_to_seconds(mark_time),
                6,
            ),

            "mark_timestamp": format_timestamp(mark_time),

            "entry_es_pid": entry_es_pid,
            "entry_es_pid_hex": f"0x{entry_es_pid:04X}",

            "duration": duration,

            "duration_seconds": round(
                ticks_to_seconds(duration),
                6,
            ),
        }

        playlist_time = calculate_playlist_mark_time(
            mark,
            play_items,
        )

        if playlist_time is not None:
            mark["playlist_time_seconds"] = round(
                playlist_time,
                6,
            )

            mark["playlist_timestamp"] = (
                format_timestamp_from_seconds(
                    playlist_time
                )
            )

        marks.append(mark)

    chapters = [
        {
            "chapter": chapter_number,
            "play_item_ref": mark["play_item_ref"],
            "timestamp": mark.get("playlist_timestamp"),
            "seconds": mark.get("playlist_time_seconds"),
        }
        for chapter_number, mark in enumerate(
            (
                m
                for m in marks
                if m["type"] == 1
            ),
            start=1,
        )
    ]

    return {
        "length": section_length,
        "mark_count": mark_count,
        "marks": marks,
        "chapters": chapters,
    }


# ---------------------------------------------------------------------------
# Extension data
# ---------------------------------------------------------------------------

def parse_extension_data(
    f: BinaryIO,
    extension_offset: int,
    file_size: int,
) -> dict[str, Any] | None:
    """
    Extension data varies between Blu-ray profiles.

    Preserve it losslessly and expose its size. Some 3D, PiP and newer
    profile metadata can live here, but interpreting every registered
    extension requires profile-specific parsers.
    """

    if not extension_offset:
        return None

    if extension_offset >= file_size:
        return {
            "offset": extension_offset,
            "error": "Extension offset is beyond EOF",
        }

    f.seek(extension_offset)

    raw = read_exact(
        f,
        file_size - extension_offset,
    )

    return {
        "offset": extension_offset,
        "length": len(raw),
        "raw_hex": raw.hex(),
    }


# ---------------------------------------------------------------------------
# Top-level parser
# ---------------------------------------------------------------------------

def parse_mpls(path: Path) -> dict[str, Any]:
    file_size = path.stat().st_size

    with path.open("rb") as f:
        signature = ascii_string(read_exact(f, 4))

        if signature != "MPLS":
            raise MPLSError(
                f"{path} is not an MPLS file "
                f"(signature: {signature!r})"
            )

        version = ascii_string(read_exact(f, 4))

        playlist_offset = u32(f)
        marks_offset = u32(f)
        extension_offset = u32(f)

        # Reserved MPLS header bytes
        read_exact(f, 20)

        playlist = parse_playlist(
            f,
            playlist_offset,
        )

        marks = parse_marks(
            f,
            marks_offset,
            playlist["play_items"],
        )

        extension_data = parse_extension_data(
            f,
            extension_offset,
            file_size,
        )

    return {
        "file": str(path),
        "file_size": file_size,

        "signature": signature,
        "version": version,

        "offsets": {
            "playlist": playlist_offset,
            "marks": marks_offset,
            "extension_data": extension_offset,
        },

        "playlist": playlist,
        "marks": marks,
        "extension_data": extension_data,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Parse a Blu-ray MPLS playlist and output JSON."
        )
    )

    parser.add_argument(
        "input",
        type=Path,
        help="Input .mpls file",
    )

    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help=(
            "Write parsed JSON to a file instead of stdout"
        ),
    )

    parser.add_argument(
        "--compact",
        action="store_true",
        help="Output compact JSON",
    )

    args = parser.parse_args()

    if not args.input.exists():
        parser.error(
            f"Input file does not exist: {args.input}"
        )

    if not args.input.is_file():
        parser.error(
            f"Input path is not a file: {args.input}"
        )

    try:
        result = parse_mpls(args.input)

    except (
        OSError,
        MPLSError,
        struct.error,
    ) as exc:
        print(
            f"Error: {exc}",
            file=sys.stderr,
        )

        return 1

    if args.compact:
        text = json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    else:
        text = json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )

    if args.output:
        try:
            args.output.write_text(
                text + "\n",
                encoding="utf-8",
            )

        except OSError as exc:
            print(
                f"Error writing {args.output}: {exc}",
                file=sys.stderr,
            )

            return 1

    else:
        print(text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
