"""Device permission policy for alarm playback.

Deny by default: a target is only usable when it appears in the explicit
allowlist and is not explicitly denied. Nothing is granted automatically.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


class DeviceDenied(PermissionError):
    """A playback target is not permitted by the active policy."""

    def __init__(self, entity_id: str, reason: str) -> None:
        super().__init__(f"{entity_id}: {reason}")
        self.entity_id = entity_id
        self.reason = reason


def _normalise(values: Iterable[str], field_name: str) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field_name} must be a collection of entity IDs, not a string")
    result = frozenset(str(value).strip().lower() for value in values if str(value).strip())
    for entity_id in result:
        if "." not in entity_id:
            raise ValueError(f"{field_name} contains an invalid entity ID: {entity_id!r}")
    return result


@dataclass(frozen=True)
class DevicePolicy:
    """Allowlist plus explicit denylist; the denylist always wins."""

    allowed: frozenset[str] = field(default_factory=frozenset)
    denied: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed", _normalise(self.allowed, "allowed"))
        object.__setattr__(self, "denied", _normalise(self.denied, "denied"))

    def check(self, entity_id: str) -> str:
        """Return the normalised target or raise :class:`DeviceDenied`."""
        target = str(entity_id).strip().lower()
        if target in self.denied:
            raise DeviceDenied(target, "explicitly_denied")
        if target not in self.allowed:
            raise DeviceDenied(target, "not_allowlisted")
        return target

    def with_allowed(self, entity_id: str) -> DevicePolicy:
        """Explicit, reviewed widening of the allowlist; never implicit."""
        return DevicePolicy(
            allowed=self.allowed | _normalise([entity_id], "allowed"),
            denied=self.denied,
        )
