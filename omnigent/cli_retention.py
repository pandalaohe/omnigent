"""Host-scoped policy for retaining idle main-session CLIs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from omnigent.harness_aliases import canonicalize_harness

CLI_RETENTION_POLICY_VERSION = 1
DEFAULT_IDLE_THRESHOLD_MINUTES = 60
MAX_IDLE_THRESHOLD_MINUTES = 10_080
MAX_IDLE_CLIS_LIMIT = 100
_RESIDENT_HARNESS_CLI_FAMILIES = {
    "claude-sdk": "claude",
    "codex": "codex",
}


def cli_family_for_resident_harness(harness: str | None) -> str | None:
    """Map verified resident non-pane harnesses onto user-facing CLI families."""
    canonical = canonicalize_harness(harness)
    return _RESIDENT_HARNESS_CLI_FAMILIES.get(canonical) if canonical is not None else None


@dataclass(frozen=True)
class CliRetentionPolicy:
    """One Host rule, applied independently to every supported CLI family."""

    idle_threshold_minutes: int = DEFAULT_IDLE_THRESHOLD_MINUTES
    max_idle_clis: int | None = None
    close_on_archive: bool = True
    version: int = CLI_RETENTION_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.version != CLI_RETENTION_POLICY_VERSION:
            raise ValueError(f"unsupported CLI retention policy version: {self.version!r}")
        threshold = self.idle_threshold_minutes
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, int)
            or not 1 <= threshold <= MAX_IDLE_THRESHOLD_MINUTES
        ):
            raise ValueError(
                f"idle_threshold_minutes must be an integer from 1 to {MAX_IDLE_THRESHOLD_MINUTES}"
            )
        limit = self.max_idle_clis
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 0 <= limit <= MAX_IDLE_CLIS_LIMIT
        ):
            raise ValueError(
                f"max_idle_clis must be null or an integer from 0 to {MAX_IDLE_CLIS_LIMIT}"
            )
        if not isinstance(self.close_on_archive, bool):
            raise ValueError("close_on_archive must be a boolean")

    def to_dict(self) -> dict[str, int | bool | None]:
        """Return the stable JSON representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CliRetentionPolicy:
        """Parse a strict persisted/request representation."""
        expected = {
            "version",
            "idle_threshold_minutes",
            "max_idle_clis",
            "close_on_archive",
        }
        if set(value) != expected:
            raise ValueError("CLI retention policy has unknown or missing fields")
        return cls(
            version=value["version"],
            idle_threshold_minutes=value["idle_threshold_minutes"],
            max_idle_clis=value["max_idle_clis"],
            close_on_archive=value["close_on_archive"],
        )


DEFAULT_CLI_RETENTION_POLICY = CliRetentionPolicy()
