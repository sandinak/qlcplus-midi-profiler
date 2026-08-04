"""Emit QLC+ input profiles (.qxi) and MIDI templates (.qxm)."""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from .events import qlc_channel
from .learn import DeviceMap

QLC_VERSION = "4.13.1"

# Where QLC+ looks for user profiles on each platform.
USER_PROFILE_DIRS = {
    "darwin": "~/Library/Application Support/QLC+/InputProfiles",
    "linux": "~/.qlcplus/InputProfiles",
    "win32": "%HOMEPATH%/QLC+/InputProfiles",
}


def _attr(value) -> str:
    return quoteattr(str(value))


def resolve_channel_mode(dmap: DeviceMap, mode: str) -> bool:
    """Return per_channel: should the MIDI channel be encoded at bit 12?

    'any'   - profile is used with the QLC+ input line set to "any" channel.
    'fixed' - input line pinned to one MIDI channel; QLC+ strips the channel.
    'auto'  - encode unless every control already sits on MIDI channel 1, where
              the two encodings are identical anyway.
    """
    if mode == "any":
        return True
    if mode == "fixed":
        return False
    if mode != "auto":
        raise ValueError(f"unknown channel mode {mode!r}")
    channels = dmap.midi_channels()
    return bool(channels - {0})


def build_qxi(
    dmap: DeviceMap,
    author: str = "",
    channel_mode: str = "auto",
    send_note_off: bool | None = None,
    color_table: list[dict] | None = None,
    idle_level: int = 0,
) -> str:
    per_channel = resolve_channel_mode(dmap, channel_mode)

    lines = [
        "<?xml version='1.0' encoding='UTF-8'?>",
        "<!DOCTYPE InputProfile>",
        '<InputProfile xmlns="http://www.qlcplus.org/InputProfile">',
        " <Creator>",
        "  <Name>Q Light Controller Plus</Name>",
        f"  <Version>{QLC_VERSION}</Version>",
        f"  <Author>{escape(author)}</Author>",
        " </Creator>",
        f" <Manufacturer>{escape(dmap.manufacturer)}</Manufacturer>",
        f" <Model>{escape(dmap.model)}</Model>",
        " <Type>MIDI</Type>",
    ]
    if send_note_off is not None:
        lines.append(
            f" <MIDISendNoteOff>{'True' if send_note_off else 'False'}</MIDISendNoteOff>"
        )

    seen: dict[int, str] = {}
    for ctl in dmap.controls:
        number = qlc_channel(ctl.kind, ctl.number, ctl.channel, per_channel)
        if number in seen:
            raise ValueError(
                f"channel {number} claimed by both {seen[number]!r} and {ctl.name!r}; "
                "with channel_mode='fixed' controls on different MIDI channels collide"
            )
        seen[number] = ctl.name

        lines.append(f' <Channel Number={_attr(number)}>')
        lines.append(f"  <Name>{escape(ctl.name)}</Name>")
        lines.append(f"  <Type>{ctl.type}</Type>")
        if ctl.type == "Encoder":
            lines.append('  <Movement Sensitivity="1"/>')
        fb = ctl.feedback
        if fb:
            # A non-zero lower value keeps the LED glowing when the widget is
            # off, instead of going dark - the on/off contrast becomes
            # dim-vs-bright.  Only useful where velocity drives brightness.
            lower = idle_level if idle_level else fb.get("lower", 0)
            attrs = [
                f'LowerValue={_attr(lower)}',
                f'UpperValue={_attr(fb.get("upper", 127))}',
            ]
            # Only pin the feedback MIDI channel when it differs from the input.
            if fb.get("channel") is not None and fb["channel"] != ctl.channel:
                attrs.append(f'MidiChannel={_attr(fb["channel"])}')
            lines.append(f'  <Feedback {" ".join(attrs)}/>')
        lines.append(" </Channel>")

    if color_table:
        lines.append(" <ColorTable>")
        for entry in color_table:
            rgb = entry.get("rgb") or "#ffffff"
            lines.append(
                f'  <Color Value={_attr(entry["value"])} '
                f'Label={_attr(entry["label"])} RGB={_attr(rgb)}/>'
            )
        lines.append(" </ColorTable>")

    lines.append("</InputProfile>")
    return "\n".join(lines) + "\n"


def idle_init_message(dmap: DeviceMap, level: int = 127) -> str:
    """Hex bytes that light every LED-backed control, for a .qxm InitMessage.

    QLC+ only sends feedback when a widget changes state, so an output that is
    MIDI-driven sits dark until something happens.  Sending note-ons at connect
    time gives the board its resting glow back.  QLC+ writes these bytes raw,
    so plain channel messages work here - it is not restricted to SysEx.
    """
    if not 0 <= level <= 127:
        raise ValueError("level must be 0..127")
    parts: list[str] = []
    for ctl in dmap.controls:
        fb = ctl.feedback
        if not fb:
            continue
        channel = fb.get("channel", ctl.channel)
        number = fb.get("number", ctl.number)
        if fb.get("kind", ctl.kind) == "cc":
            status = 0xB0 | (channel & 0x0F)
        else:
            status = 0x90 | (channel & 0x0F)
        parts += [f"{status:02X}", f"{number:02X}", f"{level:02X}"]
    return " ".join(parts)


def build_qxm(name: str, description: str, init_messages: list[str]) -> str:
    """A MIDI template: bytes QLC+ sends at connect time.

    QLC+ overwrites rather than appends when it reads multiple InitMessage
    elements, so everything that must be sent has to live in a single element.
    """
    lines = [
        "<!DOCTYPE MidiTemplate>",
        "<MidiTemplate>",
        " <Creator>",
        "  <Author>qlcplus-midi-profiler</Author>",
        " </Creator>",
        f" <Description>{escape(description)}</Description>",
        f" <Name>{escape(name)}</Name>",
    ]
    for msg in init_messages:
        lines.append(f" <InitMessage>{escape(msg)}</InitMessage>")
    lines.append("</MidiTemplate>")
    return "\n".join(lines) + "\n"


def default_filename(dmap: DeviceMap) -> str:
    def slug(s: str) -> str:
        return "".join(ch if ch.isalnum() else "-" for ch in s).strip("-")

    return f"{slug(dmap.manufacturer)}-{slug(dmap.model)}.qxi"


def install_dir() -> Path:
    import sys

    return Path(USER_PROFILE_DIRS.get(sys.platform, USER_PROFILE_DIRS["linux"])).expanduser()
