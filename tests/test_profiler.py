"""Offline tests - no MIDI hardware required.  Run: python -m tests.test_profiler"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qlcprofiler.events import Event, qlc_channel
from qlcprofiler.learn import Control, DeviceMap, classify, dominant_key
from qlcprofiler.profile import build_qxi, resolve_channel_mode

failures = 0


def check(label: str, got, want) -> None:
    global failures
    ok = got == want
    failures += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label:<28} {got!r}" + ("" if ok else f" != {want!r}"))


def evs(kind, values, number=0, channel=0):
    return [Event(kind, channel, number, v) for v in values]


def test_classify():
    check("note button", classify(evs("note", [127, 0], 36)), "Button")
    check("cc button", classify(evs("cc", [127, 0, 127, 0], 10)), "Button")
    check("encoder 2s-complement", classify(evs("cc", [1, 1, 127, 127, 1], 20)), "Encoder")
    check("encoder sign-magnitude", classify(evs("cc", [65, 65, 63, 63, 65], 21)), "Encoder")
    check("fader", classify(evs("cc", [0, 12, 40, 77, 100, 127], 7)), "Slider")
    check("short-throw knob", classify(evs("cc", [60, 62, 64, 66, 68], 8)), "Knob")
    check("pitch bend", classify(evs("pb", [0, 8000, 16383])), "Slider")
    check("program change", classify(evs("pc", [127], 3)), "Button")


def test_encoding():
    # Cross-checked against QLC+'s own profiles:
    # Novation LaunchControl XL uses 61676 for note 108 on MIDI channel 16.
    check("note 108 ch16", qlc_channel("note", 108, 15, True), 61676)
    # Zoom R16 uses 33281 for pitch wheel on MIDI channel 9.
    check("pitchwheel ch9", qlc_channel("pb", 0, 8, True), 33281)
    # APC Mini slider 1 is CC 48 with the input line pinned to one channel.
    check("cc 48 bare", qlc_channel("cc", 48, 0, False), 48)
    check("note 0 bare", qlc_channel("note", 0, 0, False), 128)
    check("pc 5 ch2", qlc_channel("pc", 5, 1, True), 4096 + 384 + 5)


def test_channel_mode():
    single = DeviceMap(controls=[Control("a", "note", 0, 36, "Button")])
    multi = DeviceMap(controls=[Control("b", "cc", 5, 7, "Slider")])
    check("auto: all on ch1", resolve_channel_mode(single, "auto"), False)
    check("auto: uses ch6", resolve_channel_mode(multi, "auto"), True)
    check("forced any", resolve_channel_mode(single, "any"), True)
    check("forced fixed", resolve_channel_mode(multi, "fixed"), False)


def test_qxi():
    dmap = DeviceMap(
        "OpenDeck", "PMJ_BLACK_1",
        controls=[
            Control("Pad 1", "note", 0, 36, "Button",
                    feedback={"kind": "note", "channel": 0, "number": 36,
                              "lower": 0, "upper": 127}),
            Control("Pad 2", "note", 0, 37, "Button",
                    feedback={"kind": "note", "channel": 5, "number": 37,
                              "lower": 0, "upper": 127}),
            Control("Fader 1", "cc", 0, 7, "Slider"),
            Control("Enc 1", "cc", 0, 20, "Encoder"),
        ],
    )
    xml = build_qxi(dmap, author="tests", channel_mode="fixed")
    root = ElementTree.fromstring(xml)
    ns = "{http://www.qlcplus.org/InputProfile}"
    channels = root.findall(f"{ns}Channel")
    check("channel count", len(channels), 4)
    check("pad 1 number", channels[0].get("Number"), "164")
    check("fader number", channels[2].get("Number"), "7")
    check("encoder movement",
          channels[3].find(f"{ns}Movement") is not None, True)
    # Same MIDI channel as the input, so no MidiChannel attribute.
    check("feedback same channel",
          channels[0].find(f"{ns}Feedback").get("MidiChannel"), None)
    # Different MIDI channel, so it must be pinned.
    check("feedback other channel",
          channels[1].find(f"{ns}Feedback").get("MidiChannel"), "5")


def test_collision_detected():
    dmap = DeviceMap(controls=[
        Control("a", "note", 0, 36, "Button"),
        Control("b", "note", 5, 36, "Button"),
    ])
    try:
        build_qxi(dmap, channel_mode="fixed")
        check("collision raises", False, True)
    except ValueError:
        check("collision raises", True, True)
    # With channel encoding on, the two no longer collide.
    build_qxi(dmap, channel_mode="any")
    check("no collision when encoded", True, True)


def test_dominant_key():
    burst = evs("note", [127, 0], 36) + evs("cc", [4], 1)
    check("dominant key", dominant_key(burst), ("note", 0, 36))
    check("empty burst", dominant_key([]), None)


class FakeOpenDeck:
    """An in-memory OpenDeck that speaks the real SysEx encoding.

    Exercises the actual request builders and reply parsers, so a change to the
    wire format breaks these tests rather than only breaking on hardware.
    """

    def __init__(self, tables):
        self.tables = {k: list(v) for k, v in tables.items()}
        self.writes = []

    def _exchange(self, payload):
        from qlcprofiler.opendeck import STATUS_ACK, SYSEX_ID, WISH_SET

        wish, amount, block, section = payload[2], payload[3], payload[4], payload[5]
        index = (payload[6] << 7) | payload[7]
        value = (payload[8] << 7) | payload[9]
        key = f"{block}/{section}"
        if key not in self.tables:
            return SYSEX_ID + [0x07, 0] + payload[2:10]  # section not supported
        table = self.tables[key]
        head = SYSEX_ID + [STATUS_ACK, 0] + payload[2:10]

        if wish == WISH_SET:
            if index >= len(table):
                return SYSEX_ID + [0x09, 0] + payload[2:10]  # index out of range
            table[index] = value
            self.writes.append((block, section, index, value))
            return head
        values = table if amount else [table[index]]
        for v in values:
            head += [v >> 7, v & 0x7F]
        return head


def _fake(tables):
    from qlcprofiler.opendeck import OpenDeck

    fake = FakeOpenDeck(tables)
    device = OpenDeck.__new__(OpenDeck)
    device._exchange = fake._exchange
    device.connect = lambda: None
    return device, fake


def test_read_write_roundtrip():
    device, fake = _fake({"4/3": [23, 1, 2, 4], "1/2": [0, 53, 38, 40]})
    check("read_section", device.read_section(4, 3), [23, 1, 2, 4])
    check("read_single", device.read_single(1, 2, 1), 53)
    check("unsupported section", device.read_section(9, 9), None)
    device.write_checked(4, 3, 2, 77)
    check("write lands", fake.tables["4/3"], [23, 1, 77, 4])
    # 14-bit values must survive the 7-bit split.
    device.write_checked(4, 3, 0, 16000)
    check("14-bit write", device.read_single(4, 3, 0), 16000)


def test_restore_only_writes_differences():
    device, fake = _fake({"4/3": [23, 1, 2, 4]})
    backup = {"4/3": [23, 9, 2, 8]}

    plan = device.restore_all(backup, dry_run=True)
    check("dry run finds 2 diffs", len(plan["changed"]), 2)
    check("dry run writes nothing", fake.writes, [])

    result = device.restore_all(backup)
    check("restore writes 2", len(result["changed"]), 2)
    check("restore skips 2", result["unchanged"], 2)
    check("device now matches", fake.tables["4/3"], [23, 9, 2, 8])

    again = device.restore_all(backup, dry_run=True)
    check("restore is idempotent", again["changed"], [])


def test_align_leds_plan():
    from qlcprofiler.opendeck import (
        BLOCK_LED, LED_ACTIVATION_ID, LED_CHANNEL, LED_CONTROL_TYPE, align_leds,
    )

    device, fake = _fake({
        f"{BLOCK_LED}/{LED_CONTROL_TYPE}": [10, 10],
        f"{BLOCK_LED}/{LED_ACTIVATION_ID}": [99, 99],
        f"{BLOCK_LED}/{LED_CHANNEL}": [1, 1],
        f"{BLOCK_LED}/6": [0, 0],
    })
    dmap = DeviceMap(controls=[
        Control("Pad A", "note", 8, 53, "Button"),   # MIDI channel 9
        Control("Fader", "cc", 8, 7, "Slider"),
    ])

    plan = align_leds(device, dmap, {0: 0}, dry_run=True)
    check("plan has 1 LED", len(plan["planned"]), 1)
    check("dry run writes nothing", fake.writes, [])

    align_leds(device, dmap, {0: 0, 1: 1})
    check("LED 0 note", fake.tables[f"{BLOCK_LED}/{LED_ACTIVATION_ID}"][0], 53)
    # OpenDeck stores channels 1-based, so 0-based 8 must land as 9.
    check("LED 0 channel", fake.tables[f"{BLOCK_LED}/{LED_CHANNEL}"][0], 9)
    check("LED 0 note type", fake.tables[f"{BLOCK_LED}/{LED_CONTROL_TYPE}"][0], 6)
    check("LED 1 cc type", fake.tables[f"{BLOCK_LED}/{LED_CONTROL_TYPE}"][1], 8)


def test_align_rejects_undrivable_kind():
    from qlcprofiler.opendeck import BLOCK_LED, LED_ACTIVATION_ID, align_leds

    device, _ = _fake({f"{BLOCK_LED}/{LED_ACTIVATION_ID}": [0]})
    dmap = DeviceMap(controls=[Control("Wheel", "pb", 0, 0, "Slider")])
    result = align_leds(device, dmap, {0: 0})
    check("pitch bend refused", len(result["failed"]), 1)


def test_to_device_map_channel_base():
    from qlcprofiler.opendeck import to_device_map

    dump = {
        "buttons": {"message": [0, 0], "midi_id": [53, 7], "channel": [9, 1]},
        "leds": {"activation_id": [53, 7], "channel": [9, 1],
                 "control_type": [10, 6], "activation_value": [0, 127]},
    }
    # Switch slots are phantom-heavy, so they are opt-in.
    check("buttons excluded by default",
          [c.type for c in to_device_map(dump, DeviceMap()).controls], [])

    dmap = to_device_map(dump, DeviceMap(), include_buttons=True)
    check("channels are 0-based", [c.channel for c in dmap.controls], [8, 0])
    # LED 0 is Static so it ignores MIDI; LED 1 is MidiInNoteMultiVal and matches.
    check("static LED not linked", dmap.controls[0].feedback, None)
    check("driven LED linked", dmap.controls[1].feedback["number"], 7)


class PagedOpenDeck(FakeOpenDeck):
    """Splits section reads into 32-value pages, like real firmware does."""

    def _exchange(self, payload):
        from qlcprofiler.opendeck import STATUS_ACK, SYSEX_ID, WISH_SET

        part, wish, amount = payload[1], payload[2], payload[3]
        key = f"{payload[4]}/{payload[5]}"
        if key not in self.tables or wish == WISH_SET:
            return super()._exchange(payload)
        if not amount:
            return super()._exchange(payload)
        page = self.tables[key][part * 32:(part + 1) * 32]
        if not page:
            return SYSEX_ID + [0x08, 0] + payload[2:10]  # part not supported
        head = SYSEX_ID + [STATUS_ACK, 0] + payload[2:10]
        for v in page:
            head += [v >> 7, v & 0x7F]
        return head


def test_paginated_read():
    from qlcprofiler.opendeck import OpenDeck

    big = list(range(70))  # 3 pages: 32 + 32 + 6
    fake = PagedOpenDeck({"1/2": big})
    device = OpenDeck.__new__(OpenDeck)
    device._exchange = fake._exchange
    # Reading only part 0 would return 32 values and look complete.
    check("all pages concatenated", device.read_section(1, 2), big)

    exact = list(range(64))  # 2 full pages, then an empty one
    fake2 = PagedOpenDeck({"1/2": exact})
    device2 = OpenDeck.__new__(OpenDeck)
    device2._exchange = fake2._exchange
    check("exact page multiple", device2.read_section(1, 2), exact)


def test_pair_by_address():
    from qlcprofiler.opendeck import pair_by_address

    dump = {"leds": {"activation_id": [99, 53, 7], "channel": [9, 9, 1],
                     "control_type": [6, 6, 6], "activation_value": [0, 0, 0]}}
    dmap = DeviceMap(controls=[
        Control("Pad A", "note", 8, 53, "Button"),   # matches LED 1
        Control("Pad B", "note", 0, 7, "Button"),    # matches LED 2
        Control("Pad C", "note", 8, 41, "Button"),   # no LED listens
        Control("Fader", "cc", 8, 53, "Slider"),     # right number, wrong kind
    ])
    pairs = pair_by_address(dump, dmap)
    check("matched two", pairs, {1: 0, 2: 1})
    check("cc not matched to note LED", 3 in pairs.values(), False)


def _decode_pulse(value):
    """Mirror of OpenDeck's mapper.cpp, to check the encoder against."""
    if value < 16:
        return "steady"
    return ["slow", "medium", "fast", "steady"][(value % 16) // 4]


def test_opendeck_led_encoding():
    from qlcprofiler.flash import STEADY_LEVELS, opendeck_value

    # The bug: a value picked purely for brightness can select a blink speed.
    check("raw 20 blinks", _decode_pulse(20), "medium")
    check("raw 40 blinks", _decode_pulse(40), "fast")

    for level in range(0, 128):
        value = opendeck_value(level, "steady")
        if _decode_pulse(value) != "steady":
            check(f"steady at level {level}", _decode_pulse(value), "steady")
            break
    else:
        check("every steady level is steady", True, True)

    for pulse in ("slow", "medium", "fast"):
        bad = [lvl for lvl in range(1, 128)
               if _decode_pulse(opendeck_value(lvl, pulse)) != pulse]
        check(f"{pulse} always {pulse}", bad, [])

    check("zero stays off", opendeck_value(0, "steady"), 0)
    check("full stays full", opendeck_value(127, "steady"), 127)
    check("steady ladder is steady",
          [_decode_pulse(v) for v in STEADY_LEVELS], ["steady"] * 8)
    # Brightness must still track the request, not collapse to one value.
    ladder = [opendeck_value(lvl, "steady") for lvl in (0, 20, 60, 100, 127)]
    check("brightness still increases", ladder == sorted(ladder), True)
    check("distinct brightness steps", len(set(ladder)), 5)


if __name__ == "__main__":
    for fn in (test_classify, test_encoding, test_channel_mode, test_qxi,
               test_collision_detected, test_dominant_key,
               test_read_write_roundtrip, test_restore_only_writes_differences,
               test_align_leds_plan, test_align_rejects_undrivable_kind,
               test_to_device_map_channel_base, test_paginated_read,
               test_pair_by_address, test_opendeck_led_encoding):
        print(f"\n-- {fn.__name__}")
        fn()
    print(f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
