"""Template and static-asset support for the organizer review UI."""

from __future__ import annotations

import json
import mimetypes
from importlib.resources import files
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape


_environment = Environment(
    loader=PackageLoader("tv.organizer.web", "templates"),
    autoescape=select_autoescape(("html", "jinja", "xml")),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _json_pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _basename(value: Any) -> str:
    return Path(str(value)).name


def _minutes(value: Any) -> str:
    return f"{float(value or 0) / 60:.1f}"


_environment.filters.update(
    basename=_basename,
    json_pretty=_json_pretty,
    minutes=_minutes,
)

_STATIC_TYPES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}


def render_template(template_name: str, **context: Any) -> bytes:
    """Render one packaged, auto-escaped HTML template as UTF-8."""

    template = _environment.get_template(template_name)
    return template.render(**context).encode("utf-8")


def static_asset(asset_name: str) -> tuple[bytes, str]:
    """Load an allow-listed packaged static asset and its response type."""

    if asset_name not in _STATIC_TYPES:
        raise KeyError(asset_name)
    asset = files(__package__).joinpath("static", asset_name)
    content_type = _STATIC_TYPES.get(
        asset_name,
        mimetypes.guess_type(asset_name)[0] or "application/octet-stream",
    )
    return asset.read_bytes(), content_type


__all__ = ["render_template", "static_asset"]
