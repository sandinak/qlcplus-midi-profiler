"""Flash LEDs so you can see which addresses are actually wired to something.

The point of flashing rather than just lighting: a steady LED is easy to miss on
a board where half the LEDs are already on as backlight, but a blinking one is
obvious.  The flasher runs on its own thread so the LED keeps blinking while you
look at the device and press the button underneath it.
"""

from __future__ import annotations

import select
import sys
import threading
import time

import mido


def _send(out, kind: str, ch: int, num: int, val: int) -> None:
    if kind == "note":
        out.send(mido.Message("note_on", channel=ch, note=num, velocity=val))
    elif kind == "cc":
        out.send(mido.Message("control_change", channel=ch, control=num, value=val))
    else:
        raise ValueError(f"cannot flash message kind {kind!r}")


# OpenDeck packs blink speed and brightness into one 7-bit value
# (io/outputs/instance/impl/mapper.cpp):
#
#   value < 16              -> steady
#   otherwise (value % 16) // 4 selects the pulse speed:
#       0 -> 1000ms   1 -> 500ms   2 -> 250ms   3 -> steady
#
# and brightness is the value scaled across 0..127 independently.  So a plain
# "dim" value like 20 is not dim - it is half-brightness blinking twice a
# second, because 20 % 16 // 4 == 1.  Only every fourth band is steady.
PULSE_OFFSETS = {"slow": 0, "medium": 4, "fast": 8, "steady": 12}

# The eight steady brightness steps, one per 16-value band.
STEADY_LEVELS = [15, 31, 47, 63, 79, 95, 111, 127]


def opendeck_value(level: int, pulse: str = "steady") -> int:
    """Encode a brightness plus pulse behaviour into one OpenDeck LED value."""
    if pulse not in PULSE_OFFSETS:
        raise ValueError(f"unknown pulse {pulse!r}; use {sorted(PULSE_OFFSETS)}")
    level = max(0, min(127, level))
    if level == 0:
        return 0
    band = level // 16
    if pulse == "steady":
        # The lowest band is steady whatever its offset, so it can pass through
        # and keep the fine brightness control that the banding otherwise costs.
        return level if band == 0 else band * 16 + 15
    if band == 0:
        band = 1  # pulsing needs a value of at least 16
    return band * 16 + PULSE_OFFSETS[pulse] + 3


def set_level(out, ctl, level: int) -> None:
    """Drive one control's LED to a brightness, using its feedback address."""
    fb = ctl.feedback
    if not fb:
        return
    _send(out, fb.get("kind", ctl.kind), fb.get("channel", ctl.channel),
          fb.get("number", ctl.number), level)


def light_all(out, controls, level: int, delay: float = 0.004) -> int:
    """Set every LED-backed control to one brightness.  Returns how many."""
    count = 0
    for ctl in controls:
        if ctl.feedback:
            set_level(out, ctl, level)
            count += 1
            time.sleep(delay)  # a long burst can outrun a slow USB endpoint
    return count


class Flasher:
    """Blinks one address until stopped, then leaves it off."""

    def __init__(self, out, kind: str, channel: int, number: int,
                 on_value: int = 127, period: float = 0.25):
        self.out, self.kind, self.channel, self.number = out, kind, channel, number
        self.on_value, self.period = on_value, period
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        state = False
        while not self._stop.is_set():
            state = not state
            _send(self.out, self.kind, self.channel, self.number,
                  self.on_value if state else 0)
            self._stop.wait(self.period)
        _send(self.out, self.kind, self.channel, self.number, 0)

    def __enter__(self) -> "Flasher":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)


def _enter_pressed() -> bool:
    """True if the user hit Enter, without blocking if they have not."""
    if not sys.stdin.isatty():
        return False
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if ready:
        sys.stdin.readline()
        return True
    return False


def _drain(port) -> None:
    for _ in port.iter_pending():
        pass


def sweep(out, kind: str, channels: list[int], first: int, last: int,
          on_value: int = 127, dwell: float = 1.2, period: float = 0.2) -> None:
    """Flash every address in a range, one at a time, so you can watch.

    Purely observational - nothing is written to the device and nothing is
    recorded.  Use this to find out whether an address space is live at all.
    """
    print(
        f"\nFlashing {kind.upper()} {first}..{last} on MIDI channel(s) "
        f"{[c + 1 for c in channels]}, {dwell}s each.\n"
        "Ctrl-C to stop.\n"
    )
    try:
        for ch in channels:
            for num in range(first, last + 1):
                print(f"  ch{ch + 1:<3} {kind} {num}", flush=True)
                with Flasher(out, kind, ch, num, on_value, period):
                    time.sleep(dwell)
    except KeyboardInterrupt:
        print("\nstopped")


def flash_and_pair(inp, out, addresses: list[tuple[str, int, int]],
                   on_value: int = 127, period: float = 0.22,
                   timeout: float = 30.0) -> dict[int, tuple[str, int, int]]:
    """Flash each address and record whichever control the user then presses.

    Returns {position in `addresses` -> (kind, midi channel, number) pressed}.
    Pressing the button under the blinking LED is what pairs them, so the
    mapping is observed rather than assumed.
    """
    pairs: dict[int, tuple[str, int, int]] = {}
    print(
        f"\nPairing {len(addresses)} LEDs.\n"
        "For each blinking LED: press the button underneath it.\n"
        "Enter = skip (nothing is blinking), Ctrl-C = stop early.\n"
    )
    try:
        for i, (kind, ch, num) in enumerate(addresses):
            _drain(inp)
            label = f"[{i + 1}/{len(addresses)}] {kind} {num} ch{ch + 1}"
            print(f"{label} ... ", end="", flush=True)
            found = None
            with Flasher(out, kind, ch, num, on_value, period):
                deadline = time.time() + timeout
                while time.time() < deadline:
                    if _enter_pressed():
                        break
                    for msg in inp.iter_pending():
                        if msg.type == "note_on" and msg.velocity > 0:
                            found = ("note", msg.channel, msg.note)
                        elif msg.type == "control_change" and msg.value > 0:
                            found = ("cc", msg.channel, msg.control)
                        if found:
                            break
                    if found:
                        break
                    time.sleep(0.005)
            if found:
                pairs[i] = found
                kind2, ch2, num2 = found
                print(f"paired with {kind2.upper()} {num2} ch{ch2 + 1}")
            else:
                print("skipped")
    except KeyboardInterrupt:
        print("\nstopped early")
    return pairs
