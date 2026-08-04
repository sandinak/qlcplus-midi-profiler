"""MIDI port discovery and fuzzy matching."""

from __future__ import annotations

import mido


def list_ports() -> tuple[list[str], list[str]]:
    return mido.get_input_names(), mido.get_output_names()


def _match(names: list[str], pattern: str, what: str) -> str:
    if pattern in names:
        return pattern
    lowered = pattern.lower()
    hits = [n for n in names if lowered in n.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SystemExit(
            f"No {what} port matching {pattern!r}.\nAvailable:\n  "
            + "\n  ".join(names or ["(none)"])
        )
    raise SystemExit(
        f"{pattern!r} is ambiguous for {what}; matches:\n  " + "\n  ".join(hits)
    )


def open_input(pattern: str):
    return mido.open_input(_match(mido.get_input_names(), pattern, "input"))


def open_output(pattern: str):
    return mido.open_output(_match(mido.get_output_names(), pattern, "output"))


def guess_output_for(input_pattern: str) -> str | None:
    """Find the output port that pairs with an input port.

    Most controllers expose in/out under the same base name; some suffix them
    with 'MIDI In'/'MIDI Out' or 'Port 1'.
    """
    outs = mido.get_output_names()
    lowered = input_pattern.lower()
    for suffix in (" midi out", " out", " midi in", " in"):
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
            break
    hits = [n for n in outs if lowered.strip() in n.lower()]
    return hits[0] if hits else None
