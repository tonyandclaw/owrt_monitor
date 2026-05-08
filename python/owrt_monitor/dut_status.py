from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# `ubus call system board` returns a JSON object. The serial transcript also
# includes the echoed command and the trailing prompt, so we extract the JSON
# bounded by the outermost balanced braces. Greedy DOTALL `.*` works because
# the JSON object is the only `{...}` pair in the typical output.
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class DutStatus:
    """Best-effort post-boot snapshot parsed from `ubus call system board` output.

    `parse_error` is non-None when parsing failed. Workflow treats that as a
    soft warning — the boot succeeded, we just couldn't characterise it.
    """

    raw_output: str = ""
    parsed: dict[str, Any] = field(default_factory=dict)
    parse_error: str | None = None

    @property
    def kernel(self) -> str | None:
        value = self.parsed.get("kernel")
        return value if isinstance(value, str) else None

    @property
    def hostname(self) -> str | None:
        value = self.parsed.get("hostname")
        return value if isinstance(value, str) else None

    @property
    def board(self) -> str | None:
        value = self.parsed.get("board_name") or self.parsed.get("board")
        return value if isinstance(value, str) else None

    @property
    def model(self) -> str | None:
        value = self.parsed.get("model")
        return value if isinstance(value, str) else None

    @property
    def release_summary(self) -> str | None:
        rel = self.parsed.get("release")
        if not isinstance(rel, dict):
            return None
        distribution = rel.get("distribution")
        version = rel.get("version") or rel.get("revision")
        if distribution and version:
            return f"{distribution} {version}"
        return distribution or version or None

    def to_dict(self) -> dict[str, Any]:
        rel = self.parsed.get("release")
        return {
            "kernel": self.kernel,
            "hostname": self.hostname,
            "board": self.board,
            "model": self.model,
            "release": dict(rel) if isinstance(rel, dict) else None,
            "parse_error": self.parse_error,
        }


def parse_ubus_system_board(output: str) -> DutStatus:
    """Parse a `ubus call system board` blob out of a serial transcript.

    Best-effort: returns DutStatus with `parse_error` set on failure, never
    raises. Caller decides whether to surface the failure or carry on.
    """
    if not output:
        return DutStatus(parse_error="empty output")
    match = _JSON_BLOCK.search(output)
    if match is None:
        return DutStatus(raw_output=output, parse_error="no JSON object found")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return DutStatus(raw_output=output, parse_error=f"json decode failed: {exc}")
    if not isinstance(parsed, dict):
        return DutStatus(raw_output=output, parse_error="JSON root is not an object")
    return DutStatus(raw_output=output, parsed=parsed)
