"""Read an OpenDeck board's configuration over SysEx.

OpenDeck (shanteacontrols/OpenDeck) is open firmware for DIY MIDI controllers.
Every control's MIDI assignment lives in an on-board database that is readable
over SysEx, so the whole input map can be dumped without touching the device -
no press-every-button session, no missed controls, and the LED addresses come
out too.

Request format (verified against firmware 2.x hardware):

    F0 00 53 43 <status> <part> <wish> <amount> <block> <section>
                <index MSB> <index LSB> <value MSB> <value LSB> F7

The value bytes are required even for reads.  Replies echo the request and
append the result as 7-bit MSB/LSB pairs; `amount=ALL` returns every index in
the section in one message.

Everything in this module is read-only.
"""

from __future__ import annotations

import time

import mido

SYSEX_ID = [0x00, 0x53, 0x43]

STATUS_REQUEST = 0x00
STATUS_ACK = 0x01

WISH_GET = 0x00
AMOUNT_SINGLE = 0x00
AMOUNT_ALL = 0x01

SPECIAL_CONNECT = 0x01
SPECIAL_FIRMWARE = 0x02
SPECIAL_HARDWARE_UID = 0x03

# Offset of the first payload byte in a reply (after the echoed request).
_VALUES_AT = 13

STATUS_NAMES = {
    0x01: "ack",
    0x02: "status error",
    0x03: "handshake error",
    0x04: "wish not supported",
    0x05: "amount not supported",
    0x06: "block not supported",
    0x07: "section not supported",
    0x08: "part not supported",
    0x09: "index out of range",
    0x0A: "value out of range",
    0x0B: "message length error",
    0x0C: "write error",
    0x0D: "not supported",
    0x0E: "read error",
}

BLOCK_GLOBAL = 0
BLOCK_BUTTON = 1
BLOCK_ENCODER = 2
BLOCK_ANALOG = 3
BLOCK_LED = 4

# Section numbers confirmed by reading a live board; a section that a given
# firmware does not implement simply answers with an error and is skipped.
BUTTON_TYPE, BUTTON_MESSAGE, BUTTON_MIDI_ID, BUTTON_VALUE, BUTTON_CHANNEL = 0, 1, 2, 3, 4
ENCODER_ENABLED, ENCODER_MIDI_ID, ENCODER_CHANNEL = 0, 3, 4
ANALOG_ENABLED, ANALOG_INVERT, ANALOG_TYPE, ANALOG_MIDI_ID = 0, 1, 2, 3
ANALOG_LOWER, ANALOG_UPPER, ANALOG_CHANNEL = 5, 7, 9
LED_LEVEL, LED_ACTIVATION_ID, LED_RGB, LED_CONTROL_TYPE = 0, 3, 4, 5
LED_ACTIVATION_VALUE, LED_CHANNEL = 6, 7

# io/outputs/shared/common.h :: ControlType.  Only the MidiIn* variants react to
# incoming MIDI; Local* mirror the board's own control, Static ignores MIDI.
LED_CONTROL_TYPES = {
    0: "MidiInNoteSingleVal",
    1: "LocalNoteSingleVal",
    2: "MidiInCcSingleVal",
    3: "LocalCcSingleVal",
    4: "PcSingleVal",
    5: "Preset",
    6: "MidiInNoteMultiVal",
    7: "LocalNoteMultiVal",
    8: "MidiInCcMultiVal",
    9: "LocalCcMultiVal",
    10: "Static",
}
LED_MIDI_DRIVEN = {0, 2, 6, 8}

# Button message types that produce something QLC+ can bind to.
BUTTON_MESSAGE_KINDS = {0: "note", 1: "pc", 2: "cc"}


class OpenDeckError(RuntimeError):
    pass


class OpenDeck:
    """A read-only SysEx session with an OpenDeck board."""

    def __init__(self, inport, outport, timeout: float = 1.0):
        self.inp = inport
        self.out = outport
        self.timeout = timeout

    # -- transport ---------------------------------------------------------

    def _exchange(self, payload: list[int]) -> list[int]:
        for _ in self.inp.iter_pending():
            pass
        self.out.send(mido.Message("sysex", data=SYSEX_ID + payload))
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            for msg in self.inp.iter_pending():
                if msg.type == "sysex" and list(msg.data)[:3] == SYSEX_ID:
                    return list(msg.data)
            time.sleep(0.002)
        raise OpenDeckError("no SysEx reply (is this an OpenDeck board?)")

    def _special(self, wish: int) -> list[int]:
        reply = self._exchange([STATUS_REQUEST, 0x00, wish])
        if reply[3] != STATUS_ACK:
            raise OpenDeckError(
                f"special request {wish:#04x}: {STATUS_NAMES.get(reply[3], reply[3])}"
            )
        return reply[5:]

    def connect(self) -> None:
        self._special(SPECIAL_CONNECT)

    def firmware_version(self) -> list[int] | None:
        try:
            return self._special(SPECIAL_FIRMWARE)[1:]
        except OpenDeckError:
            return None

    # -- config reads ------------------------------------------------------

    def read_section(self, block: int, section: int) -> list[int] | None:
        """Every value in one section, or None if the firmware lacks it."""
        reply = self._exchange(
            [STATUS_REQUEST, 0x00, WISH_GET, AMOUNT_ALL, block, section, 0, 0, 0, 0]
        )
        if reply[3] != STATUS_ACK:
            return None
        raw = reply[_VALUES_AT:]
        return [(raw[i] << 7) | raw[i + 1] for i in range(0, len(raw) - 1, 2)]

    def read_block(self, block: int, sections: dict[str, int]) -> dict[str, list[int]]:
        out = {}
        for name, section in sections.items():
            values = self.read_section(block, section)
            if values is not None:
                out[name] = values
        return out

    def dump(self) -> dict:
        self.connect()
        return {
            "firmware": self.firmware_version(),
            "buttons": self.read_block(BLOCK_BUTTON, {
                "type": BUTTON_TYPE, "message": BUTTON_MESSAGE,
                "midi_id": BUTTON_MIDI_ID, "value": BUTTON_VALUE,
                "channel": BUTTON_CHANNEL,
            }),
            "encoders": self.read_block(BLOCK_ENCODER, {
                "enabled": ENCODER_ENABLED, "midi_id": ENCODER_MIDI_ID,
                "channel": ENCODER_CHANNEL,
            }),
            "analog": self.read_block(BLOCK_ANALOG, {
                "enabled": ANALOG_ENABLED, "invert": ANALOG_INVERT,
                "type": ANALOG_TYPE, "midi_id": ANALOG_MIDI_ID,
                "lower": ANALOG_LOWER, "upper": ANALOG_UPPER,
                "channel": ANALOG_CHANNEL,
            }),
            "leds": self.read_block(BLOCK_LED, {
                "level": LED_LEVEL, "activation_id": LED_ACTIVATION_ID,
                "rgb": LED_RGB, "control_type": LED_CONTROL_TYPE,
                "activation_value": LED_ACTIVATION_VALUE, "channel": LED_CHANNEL,
            }),
        }


def _column(table: dict, name: str, index: int, default=0):
    values = table.get(name)
    if values is None or index >= len(values):
        return default
    return values[index]


def to_device_map(dump: dict, dmap):
    """Turn a raw dump into controls + LED records on an existing DeviceMap.

    OpenDeck stores MIDI channels 1-based; everything downstream is 0-based.
    """
    from .learn import Control

    controls: list[Control] = []

    buttons = dump.get("buttons", {})
    count = len(buttons.get("midi_id", []))
    for i in range(count):
        message = _column(buttons, "message", i)
        kind = BUTTON_MESSAGE_KINDS.get(message)
        if kind is None:
            continue  # transport control, MMC, preset change - nothing to bind
        controls.append(Control(
            name=f"Button {i + 1}",
            kind=kind,
            channel=max(_column(buttons, "channel", i, 1) - 1, 0),
            number=_column(buttons, "midi_id", i),
            type="Button",
        ))

    encoders = dump.get("encoders", {})
    for i in range(len(encoders.get("midi_id", []))):
        if not _column(encoders, "enabled", i):
            continue
        controls.append(Control(
            name=f"Encoder {i + 1}",
            kind="cc",
            channel=max(_column(encoders, "channel", i, 1) - 1, 0),
            number=_column(encoders, "midi_id", i),
            type="Encoder",
        ))

    analog = dump.get("analog", {})
    for i in range(len(analog.get("midi_id", []))):
        if not _column(analog, "enabled", i):
            continue
        controls.append(Control(
            name=f"Analog {i + 1}",
            kind="cc",
            channel=max(_column(analog, "channel", i, 1) - 1, 0),
            number=_column(analog, "midi_id", i),
            type="Slider",
        ))

    leds = dump.get("leds", {})
    led_records = []
    for i in range(len(leds.get("activation_id", []))):
        ctype = _column(leds, "control_type", i)
        led_records.append({
            "index": i,
            "activation_id": _column(leds, "activation_id", i),
            "channel": max(_column(leds, "channel", i, 1) - 1, 0),
            "activation_value": _column(leds, "activation_value", i),
            "control_type": ctype,
            "control_type_name": LED_CONTROL_TYPES.get(ctype, f"unknown({ctype})"),
            "midi_driven": ctype in LED_MIDI_DRIVEN,
        })

    # QLC+ sends feedback on the same channel number it received input on, so a
    # button only gets working feedback when an LED listens on that exact
    # note/CC and MIDI channel.  Link only those; the rest need the board
    # reconfigured (see README).
    by_address = {
        (r["channel"], r["activation_id"]): r for r in led_records if r["midi_driven"]
    }
    for ctl in controls:
        led = by_address.get((ctl.channel, ctl.number))
        if led and ctl.kind == "note":
            ctl.feedback = {
                "kind": "note",
                "channel": led["channel"],
                "number": led["activation_id"],
                "lower": 0,
                "upper": 127,
            }
            ctl.note = f"LED {led['index']}"

    dmap.controls = controls
    dmap.leds = led_records
    return dmap


def summarize(dump: dict, dmap) -> str:
    leds = dmap.leds
    driven = [r for r in leds if r["midi_driven"]]
    linked = [c for c in dmap.controls if c.feedback]
    kinds: dict[str, int] = {}
    for c in dmap.controls:
        kinds[c.type] = kinds.get(c.type, 0) + 1

    lines = [
        f"Controls: {len(dmap.controls)}  ("
        + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
        + ")",
        f"LEDs: {len(leds)} total, {len(driven)} driven by incoming MIDI, "
        f"{len(leds) - len(driven)} local/static",
        f"Buttons with usable QLC+ feedback: {len(linked)}",
    ]
    if leds and not linked:
        lines.append(
            "\nNo button's LED listens on that button's own note/channel, so QLC+\n"
            "cannot light them as-is.  See README 'Making feedback work'."
        )
    return "\n".join(lines)
