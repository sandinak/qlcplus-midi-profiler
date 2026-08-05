"""Identify a controller and work out which discovery paths it supports.

Two questions before mapping anything: what is this, and can it be asked about
itself?  Most devices answer the MIDI Universal Device Inquiry with a
manufacturer and firmware version.  A few - OpenDeck among them - expose their
whole configuration, which is far richer.  Everything else has to be learned by
ear.
"""

from __future__ import annotations

import time

import mido

# Universal, non-realtime: "who are you?"  Broadcast device id 0x7F.
DEVICE_INQUIRY = [0x7E, 0x7F, 0x06, 0x01]
INQUIRY_REPLY = 0x02

# Three-byte ids start with 0x00; anything else is a one-byte id.
MANUFACTURERS = {
    (0x47,): "Akai",
    (0x41,): "Roland",
    (0x42,): "Korg",
    (0x43,): "Yamaha",
    (0x00, 0x01, 0x06): "Mark of the Unicorn",
    (0x00, 0x20, 0x29): "Novation / Focusrite",
    (0x00, 0x20, 0x32): "Behringer",
    (0x00, 0x21, 0x09): "Ableton",
    (0x00, 0x53, 0x43): "OpenDeck",
}


def _drain(port) -> None:
    for _ in port.iter_pending():
        pass


def device_inquiry(inp, out, timeout: float = 1.5) -> dict | None:
    """Ask a device to identify itself.  None if it stays quiet."""
    _drain(inp)
    out.send(mido.Message("sysex", data=DEVICE_INQUIRY))
    deadline = time.time() + timeout
    while time.time() < deadline:
        for msg in inp.iter_pending():
            if msg.type != "sysex":
                continue
            data = list(msg.data)
            # 7E <dev> 06 02 <manufacturer...> <family x2> <member x2> <version x4>
            if len(data) < 6 or data[0] != 0x7E or data[3] != INQUIRY_REPLY:
                continue
            rest = data[4:]
            if rest and rest[0] == 0x00 and len(rest) >= 3:
                mid, rest = tuple(rest[:3]), rest[3:]
            else:
                mid, rest = tuple(rest[:1]), rest[1:]
            result = {
                "manufacturer_id": mid,
                "manufacturer": MANUFACTURERS.get(mid, "unknown"),
                "raw": data,
            }
            if len(rest) >= 4:
                result["family"] = rest[0] | (rest[1] << 7)
                result["member"] = rest[2] | (rest[3] << 7)
            if len(rest) >= 8:
                result["version"] = ".".join(str(b) for b in rest[4:8])
            return result
        time.sleep(0.005)
    return None


def opendeck_available(inp, out) -> bool:
    """True if the device answers the OpenDeck configuration handshake."""
    from .opendeck import OpenDeck, OpenDeckError

    try:
        OpenDeck(inp, out, timeout=1.0).connect()
        return True
    except (OpenDeckError, IndexError):
        return False


def qlc_assets_for(name: str, identity: dict | None) -> dict[str, list[str]]:
    """Profiles and MIDI templates QLC+ already ships that look like this device.

    Worth knowing before building one: a stock profile is a cross-check, and for
    devices that need a mode-change SysEx to drive their LEDs, the bundled
    template is usually the thing that unlocks them.
    """
    from pathlib import Path

    roots = {
        "profiles": Path("/Applications/QLC+.app/Contents/Resources/InputProfiles"),
        "templates": Path("/Applications/QLC+.app/Contents/Resources/MidiTemplates"),
    }
    # Model words come from the port name and identify this exact device; the
    # manufacturer alone only says "same brand", which on a range like the APC
    # matches half a dozen unrelated profiles.  Keep the two apart.
    model_words = {w for w in name.lower().replace("|", " ").split() if len(w) > 2}
    brand_words = set()
    if identity and identity["manufacturer"] != "unknown":
        brand_words = {w.lower() for w in identity["manufacturer"].split(" / ")[0].split()}
    model_words -= brand_words

    found: dict[str, list[str]] = {}
    for label, root in roots.items():
        if not root.is_dir():
            continue
        exact, brand = [], []
        for path in sorted(root.iterdir()):
            stem = path.stem.lower().replace("-", " ").replace("_", " ")
            flat = stem.replace(" ", "")
            if any(w in flat for w in model_words):
                exact.append(path.name)
            elif any(w in flat for w in brand_words):
                brand.append(path.name)
        if exact:
            found[label] = exact
        elif brand:
            found[f"{label} (same brand only)"] = brand
    return found


def probe(inp, out) -> dict:
    identity = device_inquiry(inp, out)
    return {
        "port": inp.name,
        "identity": identity,
        "opendeck": opendeck_available(inp, out),
        "qlc_assets": qlc_assets_for(inp.name, identity),
    }


def describe(result: dict) -> str:
    lines = [f"Port: {result['port']}"]
    identity = result["identity"]
    if identity:
        mid = " ".join(f"{b:02X}" for b in identity["manufacturer_id"])
        lines.append(f"Identity: {identity['manufacturer']}  (manufacturer id {mid})")
        if "family" in identity:
            lines.append(f"  family {identity['family']}, member {identity['member']}")
        if "version" in identity:
            lines.append(f"  firmware {identity['version']}")
    else:
        lines.append("Identity: no reply to Universal Device Inquiry")

    if result["opendeck"]:
        lines.append(
            "\nConfiguration readable: YES (OpenDeck protocol)\n"
            "  The whole control map can be read without pressing anything:\n"
            "    qlc-midi opendeck dump <port> -m <map>"
        )
    else:
        lines.append(
            "\nConfiguration readable: no\n"
            "  Nothing here exposes its control map, so learn it by ear:\n"
            "    qlc-midi learn <port> -m <map> --auto"
        )

    assets = result["qlc_assets"]
    if assets:
        lines.append("\nQLC+ already ships:")
        for label, names in assets.items():
            for name in names:
                lines.append(f"  {label.rstrip('s') if not label.endswith(')') else label}: {name}")
        if "templates" in assets:
            lines.append(
                "  A MIDI template usually carries the SysEx that unlocks LED\n"
                "  control - select it in QLC+ before expecting feedback to work."
            )
    return "\n".join(lines)
