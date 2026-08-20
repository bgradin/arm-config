"""Isolated, replaceable disc playback-order inference strategies."""

from .bdj_runtime_trace import BdjRuntimeTraceStrategy
from .dvd_navigation import DvdNavigationStrategy
from .hdmv_navigation import HdmvNavigationStrategy
from .playlist_heuristics import PlaylistHeuristicStrategy

DEFAULT_ORDER_STRATEGIES = (
    HdmvNavigationStrategy(),
    BdjRuntimeTraceStrategy(),
    DvdNavigationStrategy(),
    PlaylistHeuristicStrategy(),
)

__all__ = [
    "DEFAULT_ORDER_STRATEGIES",
    "BdjRuntimeTraceStrategy",
    "DvdNavigationStrategy",
    "HdmvNavigationStrategy",
    "PlaylistHeuristicStrategy",
]
