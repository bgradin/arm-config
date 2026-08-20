#!/usr/bin/env python3
"""
bdmv_parser.py

Parse Blu-ray BDMV navigation files:

    index.bdmv
    MovieObject.bdmv

No third-party dependencies.

Examples:
    python bdmv_parser.py /path/to/BDMV/index.bdmv
    python bdmv_parser.py /path/to/BDMV/MovieObject.bdmv
    python bdmv_parser.py /path/to/BDMV
    python bdmv_parser.py /path/to/BDMV --pretty

The BDMV directory mode parses both files when present.

The parser understands the core INDX/MOBJ structures and preserves
extension-data blocks as raw metadata rather than attempting to interpret
vendor/UHD-specific extension payloads.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional


class BDMVError(Exception):
    """Raised when a malformed or unsupported BDMV file is encountered."""


class BinaryReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int) -> None:
        if offset < 0 or offset > len(self.data):
            raise BDMVError(
                f"Seek outside file: 0x{offset:x} "
                f"(file size 0x{len(self.data):x})"
            )
        self.pos = offset

    def skip(self, count: int) -> None:
        self.seek(self.pos + count)

    def require(self, count: int) -> None:
        if self.pos + count > len(self.data):
            raise BDMVError(
                f"Unexpected EOF at 0x{self.pos:x}: "
                f"need {count} bytes, have {self.remaining()}"
            )

    def read(self, count: int) -> bytes:
        self.require(count)
        result = self.data[self.pos:self.pos + count]
        self.pos += count
        return result

    def u8(self) -> int:
        return self.read(1)[0]

    def u16(self) -> int:
        return struct.unpack(">H", self.read(2))[0]

    def u32(self) -> int:
        return struct.unpack(">I", self.read(4))[0]

    def ascii(self, count: int) -> str:
        raw = self.read(count)
        try:
            return raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise BDMVError(
                f"Invalid ASCII at offset 0x{self.pos - count:x}"
            ) from exc


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

OBJECT_TYPES = {
    1: "HDMV",
    2: "BD-J",
}

ACCESS_TYPES = {
    0: "permitted",
    1: "prohibited",
    3: "hidden",
}

HDMV_PLAYBACK_TYPES = {
    0: "movie",
    1: "interactive",
}

BDJ_PLAYBACK_TYPES = {
    2: "movie",
    3: "interactive",
}

VIDEO_FORMATS = {
    0: "ignored",
    1: "480i",
    2: "576i",
    3: "480p",
    4: "1080i",
    5: "720p",
    6: "1080p",
    7: "576p",
}

FRAME_RATES = {
    0: "reserved",
    1: "23.976",
    2: "24",
    3: "25",
    4: "29.97",
    5: "reserved",
    6: "50",
    7: "59.94",
}


def enum_value(value: int, names: dict[int, str]) -> dict[str, Any]:
    return {
        "value": value,
        "name": names.get(value, "unknown"),
    }


def hex_bytes(data: bytes) -> str:
    return data.hex()


def extension_info(data: bytes, offset: int) -> Optional[dict[str, Any]]:
    if offset == 0:
        return None

    if offset >= len(data):
        return {
            "offset": offset,
            "valid_offset": False,
            "size": 0,
        }

    block = data[offset:]
    return {
        "offset": offset,
        "valid_offset": True,
        "size": len(block),
        "raw_hex": block.hex(),
    }


def validate_header_version(version: str) -> None:
    """
    BDMV versions commonly include 0100, 0200, and newer 0300 variants.

    Don't hard-reject an otherwise structurally readable version merely
    because it is newer than this parser.
    """
    if len(version) != 4 or not version.isdigit():
        raise BDMVError(f"Invalid BDMV version field: {version!r}")


# ---------------------------------------------------------------------------
# index.bdmv
# ---------------------------------------------------------------------------

def parse_hdmv_object(r: BinaryReader) -> dict[str, Any]:
    """
    HDMV object body: 8 bytes

        playback_type : 2
        reserved      : 14
        id_ref        : 16
        reserved      : 32
    """
    first = r.u16()

    playback_type = (first >> 14) & 0x03
    id_ref = r.u16()
    reserved = r.u32()

    return {
        "playback_type": enum_value(
            playback_type,
            HDMV_PLAYBACK_TYPES,
        ),
        "id_ref": id_ref,
        "reserved": reserved,
    }


def parse_bdj_object(r: BinaryReader) -> dict[str, Any]:
    """
    BD-J object body: 8 bytes

        playback_type : 2
        reserved      : 14
        name          : 5 bytes
        reserved      : 8
    """
    first = r.u16()
    playback_type = (first >> 14) & 0x03

    raw_name = r.read(5)
    reserved = r.u8()

    try:
        name = raw_name.decode("ascii")
    except UnicodeDecodeError:
        name = raw_name.decode("ascii", errors="replace")

    return {
        "playback_type": enum_value(
            playback_type,
            BDJ_PLAYBACK_TYPES,
        ),
        "name": name,
        "reserved": reserved,
    }


def parse_index_playback_object(r: BinaryReader) -> dict[str, Any]:
    """
    First Playback / Top Menu entry.

    First 32 bits:
        object_type : 2
        reserved    : 30

    Followed by an 8-byte HDMV or BD-J object.
    """
    header = r.u32()
    object_type = (header >> 30) & 0x03

    result: dict[str, Any] = {
        "object_type": enum_value(object_type, OBJECT_TYPES),
        "reserved_header": header & 0x3FFFFFFF,
    }

    if object_type == 1:
        result["object"] = parse_hdmv_object(r)
    elif object_type == 2:
        result["object"] = parse_bdj_object(r)
    else:
        # Still consume the fixed 8-byte object body so parsing can continue.
        result["object"] = {
            "raw_hex": r.read(8).hex()
        }

    return result


def parse_index_title(r: BinaryReader, number: int) -> dict[str, Any]:
    """
    Title entry.

    First 32 bits:
        object_type : 2
        access_type : 2
        reserved    : 28

    Followed by an 8-byte HDMV or BD-J object.
    """
    header = r.u32()

    object_type = (header >> 30) & 0x03
    access_type = (header >> 28) & 0x03

    result: dict[str, Any] = {
        "title_number": number,
        "object_type": enum_value(object_type, OBJECT_TYPES),
        "access_type": enum_value(access_type, ACCESS_TYPES),
        "reserved_header": header & 0x0FFFFFFF,
    }

    if object_type == 1:
        result["object"] = parse_hdmv_object(r)
    elif object_type == 2:
        result["object"] = parse_bdj_object(r)
    else:
        result["object"] = {
            "raw_hex": r.read(8).hex()
        }

    return result


def parse_index_bdmv(data: bytes) -> dict[str, Any]:
    r = BinaryReader(data)

    if len(data) < 44:
        raise BDMVError("index.bdmv is too small")

    signature = r.ascii(4)
    version = r.ascii(4)

    if signature != "INDX":
        raise BDMVError(
            f"Not an index.bdmv file: expected INDX, got {signature!r}"
        )

    validate_header_version(version)

    indexes_start = r.u32()
    extension_data_start = r.u32()

    if indexes_start >= len(data):
        raise BDMVError(
            f"IndexesStartAddress 0x{indexes_start:x} lies outside file"
        )

    # Bytes 16..39 are reserved in the normal BDMV header.
    reserved_header = data[16:40]

    # ------------------------------------------------------------------
    # AppInfoBDMV
    # ------------------------------------------------------------------

    r.seek(40)

    app_info_length = r.u32()

    if app_info_length > r.remaining():
        raise BDMVError(
            f"AppInfoBDMV length {app_info_length} exceeds remaining file"
        )

    app_start = r.tell()

    # First byte:
    #
    # reserved                       1
    # initial_output_mode_preference 1
    # content_exist_flag             1
    # reserved                       5
    flags = r.u8()

    initial_output_mode_preference = (flags >> 6) & 1
    content_exist_flag = (flags >> 5) & 1

    vf_fr = r.u8()
    video_format = (vf_fr >> 4) & 0x0F
    frame_rate = vf_fr & 0x0F

    user_data = r.read(32)

    app_consumed = r.tell() - app_start

    # Preserve anything beyond the normal 34-byte application body.
    app_extra = b""
    if app_info_length > app_consumed:
        app_extra = r.read(app_info_length - app_consumed)

    app_info = {
        "length": app_info_length,
        "initial_output_mode_preference": {
            "value": initial_output_mode_preference,
            "name": (
                "3D"
                if initial_output_mode_preference
                else "2D"
            ),
        },
        "content_exist_flag": bool(content_exist_flag),
        "video_format": enum_value(video_format, VIDEO_FORMATS),
        "frame_rate": enum_value(frame_rate, FRAME_RATES),
        "user_data_hex": user_data.hex(),
    }

    if app_extra:
        app_info["extra_data_hex"] = app_extra.hex()

    # ------------------------------------------------------------------
    # Indexes()
    # ------------------------------------------------------------------

    r.seek(indexes_start)

    indexes_length = r.u32()
    indexes_body_start = r.tell()
    indexes_end = indexes_body_start + indexes_length

    if indexes_end > len(data):
        raise BDMVError(
            f"Indexes block ends at 0x{indexes_end:x}, "
            f"past file size 0x{len(data):x}"
        )

    first_playback = parse_index_playback_object(r)
    top_menu = parse_index_playback_object(r)

    num_titles = r.u16()

    # Every title consumes exactly 12 bytes.
    required = num_titles * 12
    available = indexes_end - r.tell()

    if required > available:
        raise BDMVError(
            f"Index declares {num_titles} titles requiring "
            f"{required} bytes, but only {available} remain "
            f"in the Indexes block"
        )

    titles = [
        parse_index_title(r, i + 1)
        for i in range(num_titles)
    ]

    trailing = b""
    if r.tell() < indexes_end:
        trailing = data[r.tell():indexes_end]

    result: dict[str, Any] = {
        "type": "index.bdmv",
        "signature": signature,
        "version": version,
        "file_size": len(data),
        "indexes_start_address": indexes_start,
        "extension_data_start_address": extension_data_start,
        "reserved_header_hex": reserved_header.hex(),
        "app_info": app_info,
        "indexes": {
            "length": indexes_length,
            "first_playback": first_playback,
            "top_menu": top_menu,
            "num_titles": num_titles,
            "titles": titles,
        },
        "extension_data": extension_info(
            data,
            extension_data_start,
        ),
    }

    if trailing:
        result["indexes"]["trailing_data_hex"] = trailing.hex()

    return result


# ---------------------------------------------------------------------------
# MovieObject.bdmv
# ---------------------------------------------------------------------------

@dataclass
class HDMVInstruction:
    raw_hex: str

    op_cnt: int
    grp: int
    sub_grp: int

    imm_op1: bool
    imm_op2: bool

    branch_opt: int
    cmp_opt: int
    set_opt: int

    dst: int
    src: int


def parse_hdmv_instruction(raw: bytes) -> HDMVInstruction:
    """
    Parse one 12-byte HDMV navigation command.

    Layout:

      bits  0..2   op_cnt
      bits  3..4   grp
      bits  5..7   sub_grp
      bit      8   imm_op1
      bit      9   imm_op2
      bits 10..11  reserved
      bits 12..15  branch_opt

      bits 16..19  reserved
      bits 20..23  cmp_opt

      bits 24..26  reserved
      bits 27..31  set_opt

      bits 32..63  dst
      bits 64..95  src
    """
    if len(raw) != 12:
        raise BDMVError(
            f"HDMV command must be exactly 12 bytes, got {len(raw)}"
        )

    # Treat the 12 bytes as one big-endian 96-bit bit string.
    value = int.from_bytes(raw, "big")

    # First 32-bit instruction word.
    instruction = int.from_bytes(raw[0:4], "big")

    op_cnt = (instruction >> 29) & 0x07
    grp = (instruction >> 27) & 0x03
    sub_grp = (instruction >> 24) & 0x07

    imm_op1 = bool((instruction >> 23) & 1)
    imm_op2 = bool((instruction >> 22) & 1)

    branch_opt = (instruction >> 16) & 0x0F
    cmp_opt = (instruction >> 8) & 0x0F
    set_opt = instruction & 0x1F

    dst = int.from_bytes(raw[4:8], "big")
    src = int.from_bytes(raw[8:12], "big")

    return HDMVInstruction(
        raw_hex=raw.hex(),
        op_cnt=op_cnt,
        grp=grp,
        sub_grp=sub_grp,
        imm_op1=imm_op1,
        imm_op2=imm_op2,
        branch_opt=branch_opt,
        cmp_opt=cmp_opt,
        set_opt=set_opt,
        dst=dst,
        src=src,
    )


def parse_movie_object(
    r: BinaryReader,
    object_number: int,
    block_end: int,
) -> dict[str, Any]:
    if r.tell() + 4 > block_end:
        raise BDMVError(
            f"Movie Object #{object_number} header exceeds data block"
        )

    flags = r.u16()

    resume_intention_flag = bool((flags >> 15) & 1)
    menu_call_mask = bool((flags >> 14) & 1)
    title_search_mask = bool((flags >> 13) & 1)
    reserved_flags = flags & 0x1FFF

    num_commands = r.u16()

    command_bytes = num_commands * 12
    if r.tell() + command_bytes > block_end:
        raise BDMVError(
            f"Movie Object #{object_number} declares {num_commands} "
            f"commands ({command_bytes} bytes), extending past the "
            f"MovieObject data block"
        )

    commands = []

    for command_number in range(num_commands):
        raw = r.read(12)
        command = asdict(parse_hdmv_instruction(raw))
        command["command_number"] = command_number + 1
        commands.append(command)

    return {
        "object_number": object_number,
        "resume_intention_flag": resume_intention_flag,
        "menu_call_mask": menu_call_mask,
        "title_search_mask": title_search_mask,
        "reserved_flags": reserved_flags,
        "num_commands": num_commands,
        "commands": commands,
    }


def parse_movieobject_bdmv(data: bytes) -> dict[str, Any]:
    r = BinaryReader(data)

    if len(data) < 50:
        raise BDMVError("MovieObject.bdmv is too small")

    signature = r.ascii(4)
    version = r.ascii(4)

    if signature != "MOBJ":
        raise BDMVError(
            f"Not a MovieObject.bdmv file: expected MOBJ, "
            f"got {signature!r}"
        )

    validate_header_version(version)

    extension_data_start = r.u32()

    # Bytes 12..39 are reserved.
    reserved_header = data[12:40]

    # MovieObjects() block begins at byte 40.
    r.seek(40)

    data_length = r.u32()
    block_start = r.tell()
    block_end = block_start + data_length

    if block_end > len(data):
        raise BDMVError(
            f"MovieObject data block ends at 0x{block_end:x}, "
            f"past file size 0x{len(data):x}"
        )

    reserved = r.u32()
    num_objects = r.u16()

    objects = []

    for i in range(num_objects):
        objects.append(
            parse_movie_object(
                r,
                object_number=i,
                block_end=block_end,
            )
        )

    trailing = b""
    if r.tell() < block_end:
        trailing = data[r.tell():block_end]

    result: dict[str, Any] = {
        "type": "MovieObject.bdmv",
        "signature": signature,
        "version": version,
        "file_size": len(data),
        "extension_data_start_address": extension_data_start,
        "reserved_header_hex": reserved_header.hex(),
        "movie_objects": {
            "length": data_length,
            "reserved": reserved,
            "num_objects": num_objects,
            "objects": objects,
        },
        "extension_data": extension_info(
            data,
            extension_data_start,
        ),
    }

    if trailing:
        result["movie_objects"]["trailing_data_hex"] = trailing.hex()

    return result


# ---------------------------------------------------------------------------
# Detection / filesystem interface
# ---------------------------------------------------------------------------

def detect_bdmv_type(data: bytes) -> str:
    if len(data) < 8:
        raise BDMVError("File too small to contain a BDMV header")

    signature = data[:4]

    if signature == b"INDX":
        return "index"

    if signature == b"MOBJ":
        return "movieobject"

    try:
        printable = data[:8].decode("ascii", errors="replace")
    except Exception:
        printable = repr(data[:8])

    raise BDMVError(
        f"Unsupported BDMV signature {printable!r}; "
        f"expected INDXxxxx or MOBJxxxx"
    )


def parse_file(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    kind = detect_bdmv_type(data)

    if kind == "index":
        result = parse_index_bdmv(data)
    elif kind == "movieobject":
        result = parse_movieobject_bdmv(data)
    else:
        raise AssertionError(kind)

    result["path"] = str(path)
    return result


def find_bdmv_files(directory: Path) -> list[Path]:
    """
    Accept either:
        /disc/BDMV
    or:
        /disc

    and locate index.bdmv / MovieObject.bdmv.
    """
    candidates = [directory]

    bdmv = directory / "BDMV"
    if bdmv.is_dir():
        candidates.insert(0, bdmv)

    results: list[Path] = []

    for base in candidates:
        for filename in ("index.bdmv", "MovieObject.bdmv"):
            path = base / filename

            if path.is_file() and path not in results:
                results.append(path)

    return results


def parse_path(path: Path) -> dict[str, Any]:
    if path.is_file():
        return parse_file(path)

    if not path.is_dir():
        raise BDMVError(f"Path does not exist: {path}")

    files = find_bdmv_files(path)

    if not files:
        raise BDMVError(
            f"No index.bdmv or MovieObject.bdmv found under {path}"
        )

    parsed: dict[str, Any] = {
        "type": "BDMV directory",
        "path": str(path),
        "files": {},
    }

    for file_path in files:
        result = parse_file(file_path)
        parsed["files"][result["type"]] = result

    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Parse Blu-ray index.bdmv and MovieObject.bdmv files"
        )
    )

    parser.add_argument(
        "path",
        type=Path,
        help=(
            "A BDMV file, BDMV directory, or disc root containing BDMV/"
        ),
    )

    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON",
    )

    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Write JSON to this file instead of stdout",
    )

    args = parser.parse_args()

    try:
        result = parse_path(args.path)

    except (OSError, BDMVError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(
        result,
        indent=2 if args.pretty else None,
        ensure_ascii=False,
    )

    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())