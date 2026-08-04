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


if __name__ == "__main__":
    for fn in (test_classify, test_encoding, test_channel_mode, test_qxi,
               test_collision_detected, test_dominant_key):
        print(f"\n-- {fn.__name__}")
        fn()
    print(f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
