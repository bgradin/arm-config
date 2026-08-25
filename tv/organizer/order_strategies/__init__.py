"""Isolated, replaceable disc playback-order inference strategies."""

from .bdj_runtime_trace import BdjRuntimeTraceStrategy
from .dvd_navigation import DvdNavigationStrategy
from .hdmv_navigation import HdmvNavigationStrategy

DEFAULT_ORDER_STRATEGIES = (
    HdmvNavigationStrategy(),
    BdjRuntimeTraceStrategy(),
    DvdNavigationStrategy(),
)

__all__ = [
    "DEFAULT_ORDER_STRATEGIES",
    "BdjRuntimeTraceStrategy",
    "DvdNavigationStrategy",
    "HdmvNavigationStrategy",
]
