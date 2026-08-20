from __future__ import annotations

import os
import platform
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from . import bdmv
from . import mpls
from .config import Config
from .makemkv import parse_robot_output
from .util import atomic_json_write, hash_file, hash_json, utc_now


class CollectionError(RuntimeError):
    pass


def _child_casefold(root: Path, name: str) -> Path | None:
    wanted = name.casefold()
    for child in root.iterdir():
        if child.name.casefold() == wanted:
            return child
    return None


def detect_disc_type(root: Path) -> tuple[str, Path]:
    if not root.is_dir():
        raise CollectionError(f"Disc root is not a directory: {root}")
    bluray = _child_casefold(root, "BDMV")
    if bluray and bluray.is_dir():
        return "bluray", bluray
    dvd = _child_casefold(root, "VIDEO_TS")
    if dvd and dvd.is_dir():
        return "dvd", dvd
    if root.name.casefold() == "bdmv":
        return "bluray", root
    if root.name.casefold() == "video_ts":
        return "dvd", root
    raise CollectionError(f"No BDMV or VIDEO_TS directory found under {root}")


def _copy(path: Path, source_root: Path, target_root: Path) -> dict[str, Any]:
    relative = path.relative_to(source_root)
    target = target_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return {
        "path": relative.as_posix(),
        "size": target.stat().st_size,
        "sha256": hash_file(target),
    }


def _bluray_paths(bdmv_root: Path) -> list[Path]:
    result = []
    for child in bdmv_root.rglob("*"):
        if not child.is_file():
            continue
        relative = child.relative_to(bdmv_root)
        top = relative.parts[0].casefold()
        suffix = child.suffix.casefold()
        if child.name.casefold() in {"index.bdmv", "movieobject.bdmv"}:
            result.append(child)
        elif top in {"playlist", "clipinf", "meta", "bdjo"}:
            if suffix in {".mpls", ".clpi", ".xml", ".bdjo", ".txt"}:
                result.append(child)
    return sorted(set(result))


def _dvd_paths(video_ts_root: Path, include_menu_assets: bool) -> list[Path]:
    result = []
    for child in video_ts_root.iterdir():
        if not child.is_file():
            continue
        suffix = child.suffix.casefold()
        if suffix in {".ifo", ".bup"}:
            result.append(child)
        elif include_menu_assets and suffix == ".vob" and child.stem.endswith("_0"):
            result.append(child)
        elif include_menu_assets and child.name.casefold() == "video_ts.vob":
            result.append(child)
    return sorted(result)


def _descriptive_titles(captured_root: Path) -> list[str]:
    values = []
    for xml_path in captured_root.rglob("*.xml"):
        try:
            root = ET.parse(xml_path).getroot()
        except (ET.ParseError, OSError):
            continue
        for element in root.iter():
            local_name = element.tag.rsplit("}", 1)[-1].casefold()
            if local_name in {"name", "title", "di_name"} and element.text:
                text = " ".join(element.text.split())
                if text and text not in values:
                    values.append(text)
    return values


def _portable_paths(value: Any, captured_root: Path) -> Any:
    if isinstance(value, dict):
        return {
            key: _portable_paths(item, captured_root)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_portable_paths(item, captured_root) for item in value]
    if isinstance(value, str):
        try:
            path = Path(value)
            if path.is_absolute() and (
                path == captured_root or captured_root in path.parents
            ):
                return path.relative_to(captured_root).as_posix()
        except (OSError, ValueError):
            pass
    return value


def _parse_navigation(disc_type: str, captured_root: Path) -> dict[str, Any]:
    if disc_type == "dvd":
        return {
            "format": "dvd",
            "status": "captured",
            "analyzer": "makemkv_and_dvdnav_required",
        }

    index_path = next(
        (path for path in captured_root.iterdir() if path.name.casefold() == "index.bdmv"),
        None,
    )
    movie_path = next(
        (
            path
            for path in captured_root.iterdir()
            if path.name.casefold() == "movieobject.bdmv"
        ),
        None,
    )
    navigation: dict[str, Any] = {"format": "bluray", "playlists": {}}
    if index_path:
        navigation["index"] = _portable_paths(
            bdmv.parse_file(index_path), captured_root
        )
    if movie_path:
        navigation["movie_object"] = _portable_paths(
            bdmv.parse_file(movie_path), captured_root
        )
    playlist_root = _child_casefold(captured_root, "PLAYLIST")
    if playlist_root:
        for path in sorted(playlist_root.iterdir()):
            if path.is_file() and path.suffix.casefold() == ".mpls":
                playlist_id = path.stem
                navigation["playlists"][playlist_id] = _portable_paths(
                    mpls.parse_mpls(path), captured_root
                )
    return navigation


def _run_makemkv(source: str) -> tuple[str, dict[str, Any]]:
    executable = shutil.which("makemkvcon")
    if not executable:
        return "", {"available": False, "reason": "makemkvcon_not_installed"}
    try:
        completed = subprocess.run(
            [executable, "--robot", "info", source],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return "", {"available": False, "reason": "makemkv_failed", "error": str(exc)}
    output = completed.stdout + completed.stderr
    parsed = parse_robot_output(output)
    parsed["available"] = completed.returncode == 0
    parsed["returncode"] = completed.returncode
    try:
        version = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        parsed["version"] = (version.stdout + version.stderr).strip() or "unknown"
    except (subprocess.SubprocessError, OSError):
        parsed["version"] = "unknown"
    return output, parsed


def _xml_value(element: ET.Element) -> Any:
    children = list(element)
    if not children:
        return (element.text or "").strip()
    result: dict[str, Any] = {}
    for child in children:
        key = child.tag.rsplit("}", 1)[-1]
        value = _xml_value(child)
        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(value)
        else:
            result[key] = value
    if element.attrib:
        result["@attributes"] = dict(element.attrib)
    return result


def _run_lsdvd(disc_root: Path) -> tuple[str, dict[str, Any]]:
    executable = shutil.which("lsdvd")
    if not executable:
        return "", {"available": False, "reason": "lsdvd_not_installed"}
    try:
        completed = subprocess.run(
            [executable, "-Ox", str(disc_root)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return "", {"available": False, "reason": "lsdvd_failed", "error": str(exc)}
    output = completed.stdout
    try:
        root = ET.fromstring(output)
        parsed = {root.tag.rsplit("}", 1)[-1]: _xml_value(root)}
    except ET.ParseError as exc:
        parsed = {"parse_error": str(exc)}
    parsed.update(
        {"available": completed.returncode == 0, "returncode": completed.returncode}
    )
    if completed.stderr:
        parsed["stderr"] = completed.stderr.strip()
    return output, parsed


def collect_disc(
    config: Config,
    disc_root: Path,
    *,
    arm_job_id: str | None = None,
    makemkv_source: str | None = None,
    makemkv_info_file: Path | None = None,
    include_menu_assets: bool = False,
    volume_label: str | None = None,
    producer_versions: dict[str, str] | None = None,
) -> Path:
    config.ensure_directories()
    disc_root = disc_root.resolve()
    disc_type, navigation_root = detect_disc_type(disc_root)
    capture_id = str(uuid.uuid4())
    temporary = config.captures_root / f".{capture_id}.tmp"
    captured_navigation = temporary / ("BDMV" if disc_type == "bluray" else "VIDEO_TS")
    captured_navigation.mkdir(parents=True, exist_ok=False)

    try:
        source_files = (
            _bluray_paths(navigation_root)
            if disc_type == "bluray"
            else _dvd_paths(navigation_root, include_menu_assets)
        )
        files = [
            _copy(path, navigation_root, captured_navigation)
            for path in source_files
        ]
        navigation_directory = "BDMV" if disc_type == "bluray" else "VIDEO_TS"
        for item in files:
            item["path"] = f"{navigation_directory}/{item['path']}"
        if not files:
            raise CollectionError("No navigation files were captured")

        if makemkv_info_file:
            robot_text = makemkv_info_file.read_text(
                encoding="utf-8", errors="replace"
            )
            ripper = parse_robot_output(robot_text)
            ripper["available"] = True
            ripper["source"] = "file"
        elif makemkv_source:
            robot_text, ripper = _run_makemkv(makemkv_source)
            ripper["source"] = makemkv_source
        else:
            robot_text = ""
            ripper = {
                "available": False,
                "reason": "makemkv_source_not_provided",
                "titles": {},
            }
        if robot_text:
            robot_path = temporary / "makemkv-robot.txt"
            robot_path.write_text(
                robot_text,
                encoding="utf-8",
            )

        navigation = _parse_navigation(disc_type, captured_navigation)
        if disc_type == "dvd":
            dvd_xml, dvd_structure = _run_lsdvd(disc_root)
            navigation["structure"] = dvd_structure
            navigation["runtime_traces"] = {
                "dvd": {
                    "status": "not_captured",
                    "reason": "no_configured_menu_interaction_script",
                }
            }
            if dvd_xml:
                dvd_path = temporary / "dvd-structure.xml"
                dvd_path.write_text(dvd_xml, encoding="utf-8")
        elif any(
            item.get("object_type", {}).get("name") == "BD-J"
            for item in navigation.get("index", {}).get("indexes", {}).get("titles", [])
        ):
            navigation["runtime_traces"] = {
                "bdj": {
                    "status": "not_captured",
                    "reason": "sandboxed_bdj_runner_not_configured",
                }
            }

        structure = [
            {"path": item["path"], "size": item["size"], "sha256": item["sha256"]}
            for item in files
        ]
        fingerprint = hash_json({"disc_type": disc_type, "files": structure})
        manifest = {
            "schema_version": 1,
            "capture": {
                "id": capture_id,
                "created_at": utc_now(),
                "source_root": str(disc_root),
                "arm_job_id": arm_job_id,
                "collector_version": "0.1.0",
                "menu_assets_included": include_menu_assets,
                "versions": {
                    "python": platform.python_version(),
                    "bdmv_parser": "0.1.0",
                    "mpls_parser": "0.1.0",
                    **(producer_versions or {}),
                },
            },
            "disc": {
                "type": disc_type,
                "fingerprint": fingerprint,
                "volume_label": volume_label or disc_root.name,
                "descriptive_titles": _descriptive_titles(captured_navigation),
                "filesystem": {
                    "source_name": disc_root.name,
                    "device": disc_root.stat().st_dev,
                    "block_size": os.statvfs(disc_root).f_bsize,
                },
            },
            "files": files,
            "navigation": navigation,
            "ripper": ripper,
        }
        for captured_input in (temporary / "makemkv-robot.txt", temporary / "dvd-structure.xml"):
            if captured_input.is_file():
                manifest["files"].append(
                    {
                        "path": captured_input.relative_to(temporary).as_posix(),
                        "size": captured_input.stat().st_size,
                        "sha256": hash_file(captured_input),
                    }
                )
        final = config.captures_root / fingerprint / capture_id
        final.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(temporary / "manifest.json", manifest)
        os.replace(temporary, final)
        return final / "manifest.json"
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise