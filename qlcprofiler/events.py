"""Normalized MIDI events and QLC+ input-channel encoding.

QLC+ encodes every MIDI message as a single integer "input channel".
Constants come from plugins/midi/src/common/midiprotocol.h:

    control change      0   .. 127
    note                128 .. 255
    note aftertouch     256 .. 383
    program change      384 .. 511
    channel aftertouch  512
    pitch wheel         513

When the QLC+ MIDI plugin's input line is set to a specific MIDI channel the
number is used bare.  When it is set to "any" channel, the 0-based MIDI channel
is OR'd in at bit 12 (midiprotocol.cpp: `midiChannel = channel >> 12`), i.e.
`channel += midi_channel * 4096`.
"""

from __future__ import annotations

from dataclasses import dataclass

MIDI_CHANNEL_SHIFT = 12

# Message kinds we care about, and their QLC+ channel offsets.
OFFSETS = {
    "cc": 0,
    "note": 128,
    "at": 256,  # polyphonic / note aftertouch
    "pc": 384,
    "cat": 512,  # channel aftertouch (no number)
    "pb": 513,  # pitch wheel (no number)
}

# Kinds whose data byte 1 participates in the channel number.
NUMBERED = {"cc", "note", "at", "pc"}


@dataclass(frozen=True)
class Event:
    """One inbound MIDI message, reduced to what a profile cares about."""

    kind: str
    channel: int  # 0-based MIDI channel
    number: int  # CC/note/program number; 0 for cat/pb
    value: int  # 0..127, or 0..16383 for pitch bend

    @property
    def key(self) -> tuple[str, int, int]:
        """Identity of the physical control that produced this event."""
        return (self.kind, self.channel, self.number)

    def describe(self) -> str:
        if self.kind in NUMBERED:
            return f"{self.kind.upper()} {self.number} ch{self.channel + 1}"
        return f"{self.kind.upper()} ch{self.channel + 1}"


def from_mido(msg) -> Event | None:
    """Convert a mido message to an Event, or None if it is not a control."""
    t = msg.type
    if t == "note_on":
        return Event("note", msg.channel, msg.note, msg.velocity)
    if t == "note_off":
        return Event("note", msg.channel, msg.note, 0)
    if t == "control_change":
        return Event("cc", msg.channel, msg.control, msg.value)
    if t == "program_change":
        return Event("pc", msg.channel, msg.program, 127)
    if t == "polytouch":
        return Event("at", msg.channel, msg.note, msg.value)
    if t == "aftertouch":
        return Event("cat", msg.channel, 0, msg.value)
    if t == "pitchwheel":
        # mido reports -8192..8191; QLC+ thinks in 0..16383.
        return Event("pb", msg.channel, 0, msg.pitch + 8192)
    return None


def qlc_channel(kind: str, number: int, midi_channel: int, per_channel: bool) -> int:
    """Encode a control as a QLC+ input-profile channel number.

    per_channel=True embeds the MIDI channel at bit 12, which is what QLC+
    expects when the input line is configured for "any" MIDI channel.
    """
    if kind not in OFFSETS:
        raise ValueError(f"unsupported message kind: {kind}")
    base = OFFSETS[kind] + (number if kind in NUMBERED else 0)
    if per_channel:
        base += midi_channel << MIDI_CHANNEL_SHIFT
    return base
