"""Probe a controller's LED feedback: does sending MIDI back light anything?

Three strategies, cheapest first:

  echo    send each learned control's own message back at full value
  scan    sweep an address space (notes or CCs) looking for any lit LED
  colors  walk velocities on one control to build a QLC+ ColorTable
"""

from __future__ import annotations

import time

import mido

from .learn import Control, DeviceMap


def _send(out, kind: str, ch: int, num: int, val: int) -> None:
    if kind == "note":
        out.send(mido.Message("note_on", channel=ch, note=num, velocity=val))
    elif kind == "cc":
        out.send(mido.Message("control_change", channel=ch, control=num, value=val))
    elif kind == "pc":
        out.send(mido.Message("program_change", channel=ch, program=num))
    elif kind == "pb":
        out.send(mido.Message("pitchwheel", channel=ch, pitch=min(val * 128, 8191)))
    else:
        raise ValueError(f"cannot send feedback for kind {kind!r}")


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


def blanket_test(out, dmap: DeviceMap, on_value: int = 127, delay: float = 0.02) -> bool:
    """Light everything at once - a fast yes/no on whether feedback exists."""
    targets = [c for c in dmap.controls if c.kind in ("note", "cc")]
    if not targets:
        print("No note/CC controls in the map to test.")
        return False
    print(f"Sending {on_value} to all {len(targets)} note/CC addresses...")
    for c in targets:
        _send(out, c.kind, c.channel, c.number, on_value)
        time.sleep(delay)
    answer = _ask("Did ANY LED light up? [y/N] ")
    for c in targets:
        _send(out, c.kind, c.channel, c.number, 0)
        time.sleep(delay)
    return answer.startswith("y")


def echo_walk(out, dmap: DeviceMap, on_value: int = 127, hold: float = 0.6) -> int:
    """Light one control at a time, confirming each.  Returns count confirmed."""
    targets = [c for c in dmap.controls if c.kind in ("note", "cc")]
    confirmed = 0
    print(
        f"\nWalking {len(targets)} controls.  For each: y = it lit, n = it did not,\n"
        "s = skip rest, q = quit.\n"
    )
    for i, c in enumerate(targets, 1):
        _send(out, c.kind, c.channel, c.number, on_value)
        time.sleep(hold)
        answer = _ask(f"[{i}/{len(targets)}] {c.name} ({c.describe()}) lit? [y/N/s/q] ")
        _send(out, c.kind, c.channel, c.number, 0)
        if answer == "q":
            break
        if answer == "s":
            print("  skipping remaining")
            break
        if answer.startswith("y"):
            c.feedback = {
                "kind": c.kind,
                "channel": c.channel,
                "number": c.number,
                "lower": 0,
                "upper": on_value,
            }
            confirmed += 1
    return confirmed


def scan(
    out,
    kind: str = "note",
    channels: list[int] | None = None,
    first: int = 0,
    last: int = 127,
    value: int = 127,
    delay: float = 0.15,
    hold: bool = False,
) -> None:
    """Sweep an address space so you can watch which addresses light which LED.

    Use this when the LEDs are not addressed the same way the buttons report -
    common on custom builds where LED and button share a body but not a number.
    """
    channels = channels or [0]
    print(
        f"\nScanning {kind.upper()} {first}..{last} on MIDI channel(s) "
        f"{[c + 1 for c in channels]} at {delay}s per step.\n"
        "Watch the device and note which address lights which LED.\n"
    )
    for ch in channels:
        for num in range(first, last + 1):
            print(f"  ch{ch + 1:<3} {kind} {num}", flush=True)
            _send(out, kind, ch, num, value)
            time.sleep(delay)
            if not hold:
                _send(out, kind, ch, num, 0)
    print("\nScan complete.")


def color_table(out, ctl: Control, step: int = 1, hold: float = 0.35) -> list[dict]:
    """Walk values on one control, recording what colour each produces.

    Enter a label at each stop (blank = skip, 'q' = finish).  The result feeds
    the <ColorTable> block of the QLC+ profile.
    """
    table: list[dict] = []
    print(
        f"\nColour walk on {ctl.name} ({ctl.describe()}).\n"
        "Type a label for each visible colour, blank to skip, q to stop.\n"
    )
    for val in range(0, 128, step):
        _send(out, ctl.kind, ctl.channel, ctl.number, val)
        time.sleep(hold)
        label = _ask(f"  value {val:>3}: ")
        if label == "q":
            break
        if label:
            table.append({"value": val, "label": label, "rgb": ""})
    _send(out, ctl.kind, ctl.channel, ctl.number, 0)
    return table
