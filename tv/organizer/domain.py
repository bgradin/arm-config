from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


JOB_STATES = {
    "disc_seen",
    "capturing",
    "ripping",
    "awaiting_assets",
    "analyzing",
    "needs_review",
    "approved",
    "organizing",
    "complete",
    "failed",
}

ALLOWED_TRANSITIONS = {
    "disc_seen": {"capturing", "failed"},
    "capturing": {"ripping", "awaiting_assets", "failed"},
    "ripping": {"awaiting_assets", "failed"},
    "awaiting_assets": {"analyzing", "failed"},
    "analyzing": {"needs_review", "failed"},
    "needs_review": {"analyzing", "approved", "organizing", "failed"},
    "approved": {"needs_review", "analyzing", "organizing", "failed"},
    "organizing": {"complete", "approved", "failed"},
    "complete": {"analyzing", "organizing"},
    "failed": {
        "capturing",
        "awaiting_assets",
        "analyzing",
        "approved",
        "organizing",
    },
}

ASSET_DISPOSITIONS = {
    "unresolved",
    "episode",
    "extra",
    "duplicate",
    "ignore",
}

HIGH_ORDER_CONFIDENCE = 0.90


@dataclass
class Evidence:
    rule: str
    message: str
    contribution: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Suggestion:
    kind: str
    value: Any
    confidence: float
    evidence: list[Evidence] = field(default_factory=list)
    contradictions: list[Evidence] = field(default_factory=list)
    analyzer: str = "unknown"
    analyzer_version: str = "1.0.0"

    def to_record(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value,
            "confidence": max(0.0, min(1.0, self.confidence)),
            "evidence": [item.to_dict() for item in self.evidence],
            "contradictions": [
                item.to_dict() for item in self.contradictions
            ],
            "analyzer": self.analyzer,
            "analyzer_version": self.analyzer_version,
        }


def validate_transition(current: str, target: str) -> None:
    if current not in JOB_STATES or target not in JOB_STATES:
        raise ValueError(f"Unknown job transition: {current} -> {target}")
    if current == target:
        return
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"Invalid job transition: {current} -> {target}")
