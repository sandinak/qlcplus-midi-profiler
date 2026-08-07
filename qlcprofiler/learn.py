"""Capture MIDI from a controller and classify each physical control."""

from __future__ import annotations

import contextlib
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .events import Event, from_mido

# Value patterns emitted by relative (endless) encoders.
_TWOS_COMPLEMENT = {1, 2, 3, 4, 5, 6, 7, 127, 126, 125, 124, 123, 122, 121}
_SIGN_MAGNITUDE = {62, 63, 64, 65, 66, 67, 65 + 64, 66 + 64, 67 + 64}


@dataclass
class Control:
    name: str
    kind: str
    channel: int
    number: int
    type: str  # QLC+ channel type: Button / Slider / Knob / Encoder
    values: list[int] = field(default_factory=list)
    feedback: dict | None = None
    note: str = ""  # free-form comment

    @property
    def key(self):
        return (self.kind, self.channel, self.number)

    def describe(self) -> str:
        num = "" if self.kind in ("cat", "pb") else f" {self.number}"
        return f"{self.type:<7} {self.kind.upper()}{num} ch{self.channel + 1}"


@dataclass
class DeviceMap:
    manufacturer: str = "Unknown"
    model: str = "Unknown"
    # Firmware protocol, when known ("opendeck").  Distinct from manufacturer:
    # LED value encoding follows the firmware, not the brand on the case.
    protocol: str = ""
    input_port: str = ""
    output_port: str = ""
    controls: list[Control] = field(default_factory=list)
    # Device-reported LED records, when the device can be interrogated
    # (see opendeck.py).  Empty for controllers learned by ear.
    leds: list[dict] = field(default_factory=list)
    # value -> colour, for RGB pads.  Feeds the profile's <ColorTable>.
    colors: list[dict] = field(default_factory=list)
    # MIDI channel -> LED behaviour.  Some devices (APC40 mkII) select
    # brightness and blink rate by the channel a feedback note is sent on,
    # while velocity picks the colour.  Feeds <MidiChannelTable>.
    midi_channel_table: list[dict] = field(default_factory=list)

    def by_key(self) -> dict:
        return {c.key: c for c in self.controls}

    def midi_channels(self) -> set[int]:
        return {c.channel for c in self.controls}

    def save(self, path: Path) -> None:
        data = asdict(self)
        data.pop("skipped_channels", None)
        path.write_text(json.dumps(data, indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> "DeviceMap":
        data = json.loads(Path(path).read_text())
        controls = [Control(**c) for c in data.pop("controls", [])]
        return cls(controls=controls, **data)


def classify(events: list[Event]) -> str:
    """Guess the QLC+ channel type from a burst of events for one control."""
    kinds = {e.kind for e in events}
    if kinds == {"note"} or kinds == {"pc"}:
        return "Button"
    if "pb" in kinds:
        return "Slider"
    if "cat" in kinds or "at" in kinds:
        return "Slider"

    values = [e.value for e in events]
    distinct = set(values)

    # Relative encoders repeat a tiny set of increment codes, so test them
    # before the "few distinct values means button" rule.  A value of 0 marks a
    # release, which encoders never send - that is what separates a CC button
    # sending 127/0 from an encoder sending 127/1.
    if (
        len(events) >= 4
        and len(distinct) >= 2
        and 0 not in distinct
        and (distinct <= _TWOS_COMPLEMENT or distinct <= _SIGN_MAGNITUDE)
    ):
        return "Encoder"
    if len(distinct) <= 2:
        return "Button"
    if max(values) - min(values) >= 32:
        return "Slider"
    return "Knob"


def _mark_observed(ctl: Control) -> None:
    """Drop an 'unconfirmed' note once the control has actually been seen.

    Addresses guessed from a device's config carry a warning; hearing the
    control emit is exactly the confirmation that warning was waiting for.
    """
    if "unconfirmed" in ctl.note.lower() or "not yet confirmed" in ctl.note.lower():
        ctl.note = ""


def collect_burst(port, quiet: float = 0.7, timeout: float = 20.0) -> list[Event]:
    """Read events until the control goes quiet, or timeout with nothing."""
    events: list[Event] = []
    start = time.time()
    last = None
    while True:
        got = False
        for msg in port.iter_pending():
            ev = from_mido(msg)
            if ev is not None:
                events.append(ev)
                got = True
        now = time.time()
        if got:
            last = now
        if last is not None and now - last >= quiet:
            return events
        if last is None and now - start >= timeout:
            return events
        time.sleep(0.002)


def dominant_key(events: list[Event]) -> tuple | None:
    """The (kind, channel, number) that most of the burst belongs to.

    Filters out stray traffic (e.g. a neighbouring pad brushed by accident).
    """
    if not events:
        return None
    counts = Counter(e.key for e in events)
    return counts.most_common(1)[0][0]


def _drain(port) -> None:
    for _ in port.iter_pending():
        pass


def learn_interactive(port, dmap: DeviceMap, port_name: str) -> DeviceMap:
    """Name-then-press loop.  Blank name ends the session."""
    dmap.input_port = port_name
    existing = dmap.by_key()
    print(
        "\nInteractive learn.  For each control: type a name, press Enter,\n"
        "then operate the control on the device.  Blank name = done.\n"
    )
    while True:
        try:
            name = input("Control name (blank to finish): ").strip()
        except EOFError:
            break
        if not name:
            break

        _drain(port)
        print("  ...operate it now")
        events = collect_burst(port)
        if not events:
            print("  nothing received - skipped\n")
            continue

        key = dominant_key(events)
        matching = [e for e in events if e.key == key]
        ctype = classify(matching)
        kind, ch, num = key
        values = sorted({e.value for e in matching})

        if key in existing:
            # Expected when relabelling a map that came from a device dump:
            # the address is already known, the physical name is not.
            prior = existing[key]
            print(f"  -> {prior.describe()}  (was {prior.name!r})\n")
            prior.name = name
            prior.type = ctype
            prior.values = values[:16]
            _mark_observed(prior)
            continue

        ctl = Control(
            name=name, kind=kind, channel=ch, number=num, type=ctype,
            values=values[:16],
        )
        dmap.controls.append(ctl)
        existing[key] = ctl
        extra = len({e.key for e in events}) - 1
        stray = f"  (+{extra} other control(s) ignored)" if extra else ""
        print(f"  -> {ctl.describe()}   {len(matching)} msgs{stray}\n")

    return dmap


_AUTO_NAME = re.compile(r"^(Analog|Button|Encoder|Slider|Knob) \d+$|^Button \(LED \d+\)$")


def is_auto_name(ctl: Control) -> bool:
    """True if the control still carries a generated name rather than a real one."""
    if _AUTO_NAME.match(ctl.name):
        return True
    number = "" if ctl.kind in ("cat", "pb") else f" {ctl.number}"
    return ctl.name == f"{ctl.kind.upper()}{number} ch{ctl.channel + 1}"


def relabel_interactive(port, dmap: DeviceMap, port_name: str, out=None,
                        dim: int = 20, bright: int = 127,
                        encode=None) -> DeviceMap:
    """Press-then-name, for putting real names on an already-complete map.

    The reverse of `learn_interactive`, and the better order once every address
    is known: you are standing at the board, so touching the control first and
    naming it second means never having to work out what to call something you
    have not touched yet.

    Given an output port, the board becomes the progress display.  Unlit keycaps
    are unreadable, so everything is lit dim to start; the control being named
    blinks, and named ones stay bright.  What is left to do is then visible on
    the hardware instead of having to be tracked in your head.
    """
    from .flash import Flasher, set_level

    dmap.input_port = dmap.input_port or port_name
    existing = dmap.by_key()
    named = 0

    # With an encoder, the device does the blinking itself and we send one
    # message instead of driving a thread that toggles the LED forever.
    device_pulse = encode is not None
    dim_value = encode(dim, "steady") if encode else dim
    bright_value = encode(bright, "steady") if encode else bright
    active_value = encode(bright, "fast") if encode else bright

    def level_for(ctl: Control) -> int:
        return dim_value if is_auto_name(ctl) else bright_value

    lit = [c for c in dmap.controls if c.feedback]
    if out and lit:
        for ctl in lit:
            set_level(out, ctl, level_for(ctl))
        done = sum(1 for c in lit if not is_auto_name(c))
        print(f"\nLit {len(lit)} LEDs: bright = already named ({done}), dim = to do.")

    print(
        f"\nRelabelling {len(dmap.controls)} controls.\n"
        "Operate a control, then type its name.  Blank keeps the current name.\n"
        "Ctrl-C when finished.\n"
    )
    try:
        while True:
            _drain(port)
            events = collect_burst(port, timeout=3600.0)
            if not events:
                continue
            key = dominant_key(events)
            matching = [e for e in events if e.key == key]
            ctl = existing.get(key)
            if ctl is None:
                kind, ch, num = key
                ctl = Control(
                    name=f"{kind.upper()} {num} ch{ch + 1}",
                    kind=kind, channel=ch, number=num,
                    type=classify(matching),
                )
                dmap.controls.append(ctl)
                existing[key] = ctl
                print(f"  (new) {ctl.describe()}")

            has_led = "  [has LED]" if ctl.feedback else "  [no LED]"
            prompt = f"  {ctl.describe()}{has_led}\n  name [{ctl.name}]: "
            if out and ctl.feedback and device_pulse:
                set_level(out, ctl, active_value)
                blinker = contextlib.nullcontext()
            elif out and ctl.feedback:
                blinker = Flasher(out, ctl.kind, ctl.feedback["channel"],
                                  ctl.feedback["number"], bright, 0.22)
            else:
                blinker = contextlib.nullcontext()
            try:
                with blinker:
                    name = input(prompt).strip()
            except EOFError:
                break
            if name:
                ctl.name = name
                _mark_observed(ctl)
                named += 1
            if out and ctl.feedback:
                set_level(out, ctl, level_for(ctl))
            print()
    except KeyboardInterrupt:
        print()
    if out and lit:
        # Leave the board readable rather than dark on the way out.
        for ctl in lit:
            set_level(out, ctl, level_for(ctl))
    print(f"Named {named} control(s).")
    return dmap


def learn_auto(port, dmap: DeviceMap, port_name: str, idle_stop: float = 0.0) -> DeviceMap:
    """Sniff everything, registering controls as they first appear.

    Names are auto-generated; rename them in the JSON map afterwards.  Stops on
    Ctrl-C, or after idle_stop seconds of silence when idle_stop > 0.
    """
    dmap.input_port = port_name
    existing = dmap.by_key()
    buckets: dict[tuple, list[Event]] = defaultdict(list)
    counters: Counter = Counter()

    print(
        "\nAuto learn.  Operate every control on the device once (buttons: press\n"
        "and release; faders/knobs: full sweep).  Ctrl-C when finished.\n"
    )
    last = time.time()
    try:
        while True:
            for msg in port.iter_pending():
                ev = from_mido(msg)
                if ev is None:
                    continue
                last = time.time()
                buckets[ev.key].append(ev)
                if ev.key not in existing:
                    kind, ch, num = ev.key
                    counters[kind] += 1
                    ctl = Control(
                        name=f"{kind.upper()} {num} ch{ch + 1}",
                        kind=kind, channel=ch, number=num, type="Button",
                    )
                    dmap.controls.append(ctl)
                    existing[ev.key] = ctl
                    print(f"  new: {ev.describe()}")
            if idle_stop and time.time() - last > idle_stop:
                break
            time.sleep(0.002)
    except KeyboardInterrupt:
        print()

    # Re-classify each control now that the whole burst history is known.
    for key, evs in buckets.items():
        ctl = existing[key]
        ctl.type = classify(evs)
        ctl.values = sorted({e.value for e in evs})[:16]
        _mark_observed(ctl)

    dmap.controls.sort(key=lambda c: (c.channel, c.kind, c.number))
    return dmap
