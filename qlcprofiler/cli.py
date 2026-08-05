"""qlc-midi: learn a MIDI controller and emit a QLC+ input profile."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from . import feedback as fb
from . import ports
from .events import from_mido, qlc_channel
from .learn import DeviceMap, learn_auto, learn_interactive, relabel_interactive
from .profile import (
    build_qxi,
    build_qxm,
    default_filename,
    install_dir,
    resolve_channel_mode,
)


def cmd_ports(args) -> int:
    ins, outs = ports.list_ports()
    print("Inputs:")
    for n in ins or ["  (none)"]:
        print(f"  {n}")
    print("Outputs:")
    for n in outs or ["  (none)"]:
        print(f"  {n}")
    return 0


def cmd_monitor(args) -> int:
    port = ports.open_input(args.port)
    print(f"Monitoring {port.name}  (Ctrl-C to stop)")
    try:
        while True:
            for msg in port.iter_pending():
                ev = from_mido(msg)
                if ev is None:
                    if args.all:
                        print(f"  {msg}")
                    continue
                bare = qlc_channel(ev.kind, ev.number, ev.channel, False)
                enc = qlc_channel(ev.kind, ev.number, ev.channel, True)
                chan = f"{bare}" if bare == enc else f"{bare} / {enc}"
                print(f"  {ev.describe():<20} val={ev.value:<6} qlc_channel={chan}")
            time.sleep(0.002)
    except KeyboardInterrupt:
        print()
    return 0


def _load_or_new(path: Path, args) -> DeviceMap:
    if path.exists() and not args.overwrite:
        dmap = DeviceMap.load(path)
        print(f"Extending existing map {path} ({len(dmap.controls)} controls)")
    else:
        dmap = DeviceMap()
    if args.manufacturer:
        dmap.manufacturer = args.manufacturer
    if args.model:
        dmap.model = args.model
    return dmap


def cmd_learn(args) -> int:
    port = ports.open_input(args.port)
    out_path = Path(args.map)
    dmap = _load_or_new(out_path, args)
    if dmap.manufacturer == "Unknown":
        dmap.manufacturer = port.name.split("|")[0].strip() or "Unknown"
    if dmap.model == "Unknown":
        dmap.model = port.name.split("|")[-1].strip()

    if args.auto:
        learn_auto(port, dmap, port.name, idle_stop=args.idle_stop)
    elif args.relabel:
        out = None
        if not args.no_lights:
            out_name = args.out or dmap.output_port or ports.guess_output_for(args.port)
            if out_name:
                out = ports.open_output(out_name)
                dmap.output_port = out.name
            else:
                print("No output port found; naming without LED guidance "
                      "(pass --out, or --no-lights to silence this).")
        relabel_interactive(port, dmap, port.name, out=out,
                            dim=args.dim, bright=args.bright,
                            encode=_led_encoder(dmap, args.raw_levels))
    else:
        learn_interactive(port, dmap, port.name)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    dmap.save(out_path)
    print(f"\nSaved {len(dmap.controls)} controls to {out_path}")
    for c in dmap.controls:
        print(f"  {c.name:<24} {c.describe()}")
    return 0


def _led_encoder(dmap: DeviceMap, raw: bool):
    """Pick how a brightness becomes an LED value, or None to send it as-is.

    OpenDeck reads pulse speed out of the same byte as brightness, so a value
    chosen purely for its brightness can set the LED blinking.  Other devices
    treat the byte as plain velocity and must be left alone.
    """
    if raw or not dmap.manufacturer.lower().startswith("opendeck"):
        return None
    from .flash import opendeck_value

    return opendeck_value


def cmd_import(args) -> int:
    """Seed a map from an existing QLC+ input profile."""
    from .profile import parse_qxi

    src = Path(args.profile)
    if not src.exists():
        stock = Path("/Applications/QLC+.app/Contents/Resources/InputProfiles") / src.name
        if stock.exists():
            src = stock
        else:
            print(f"{args.profile} not found", file=sys.stderr)
            return 2

    try:
        dmap = parse_qxi(src)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    skipped = getattr(dmap, "skipped_channels", [])
    kinds = {}
    for c in dmap.controls:
        kinds[c.type] = kinds.get(c.type, 0) + 1

    print(f"{src.name}\n  {dmap.manufacturer} {dmap.model}")
    print(f"  {len(dmap.controls)} controls  ("
          + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())) + ")")
    print(f"  {sum(1 for c in dmap.controls if c.feedback)} with feedback, "
          f"{len(dmap.colors)} colours")
    if skipped:
        print(f"  skipped {len(skipped)} non-MIDI channel number(s): {skipped[:8]}")

    channels = dmap.midi_channels()
    if channels == {0}:
        print("\n  Every channel decoded to MIDI channel 1.  A profile written for a\n"
              "  fixed MIDI channel records no channel, so confirm against hardware.")

    out_path = Path(args.map)
    if out_path.exists() and not args.overwrite:
        print(f"\n{out_path} exists; pass --overwrite to replace it.", file=sys.stderr)
        return 2
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dmap.save(out_path)
    print(f"\nSaved to {out_path}")
    print("Confirm it against the hardware:  qlc-midi learn <port> -m "
          + str(out_path) + " --relabel")
    return 0


def cmd_colors(args) -> int:
    """Show a device's colour table - as a list, or painted onto its pads."""
    from .flash import set_level

    map_path = Path(args.map)
    if not map_path.exists():
        print(f"{map_path} not found; run `import` or `learn` first.", file=sys.stderr)
        return 2
    dmap = DeviceMap.load(map_path)
    if not dmap.colors:
        print(f"{map_path} has no colour table.\n"
              "Import one from a stock profile, or build one with\n"
              "  qlc-midi feedback --mode colors --control <name>", file=sys.stderr)
        return 2

    if args.list:
        print(f"{len(dmap.colors)} colours in {dmap.manufacturer} {dmap.model}:")
        for entry in dmap.colors:
            print(f"  {entry['value']:>3}  {entry.get('rgb',''):<9} {entry.get('label','')}")
        if dmap.midi_channel_table:
            print(f"\n{len(dmap.midi_channel_table)} LED behaviours, selected by the "
                  "MIDI channel the feedback is sent on:")
            for entry in dmap.midi_channel_table:
                print(f"  channel {entry['value'] + 1:>2}  {entry['label']}")
        return 0

    out_name = args.out or dmap.output_port or ports.guess_output_for(args.port or "")
    if not out_name:
        print("No output port; pass --out.", file=sys.stderr)
        return 2
    out = ports.open_output(out_name)

    pads = [c for c in dmap.controls if c.feedback and c.kind == "note"]
    if not pads:
        print("No LED-backed note controls in the map.", file=sys.stderr)
        return 2

    # Paint the table across the pads, a screenful at a time, so the palette can
    # be compared against the labels instead of stepped through one value at a
    # time.
    colors = dmap.colors[args.start:]
    page = colors[:len(pads)]
    print(f"Painting {len(page)} colours onto {len(pads)} pads "
          f"(values {page[0]['value']}-{page[-1]['value']}):\n")
    from .events import dmx_to_midi

    for pad, entry in zip(pads, page):
        velocity = dmx_to_midi(entry["value"])
        set_level(out, pad, velocity)
        print(f"  {pad.name:<16} value {entry['value']:>3} -> vel {velocity:>3}  "
              f"{entry.get('rgb',''):<9} {entry.get('label','')}")
    remaining = len(colors) - len(page)
    if remaining > 0:
        print(f"\n{remaining} more; next page:  --start {args.start + len(page)}")
    return 0


def cmd_probe(args) -> int:
    from .probe import describe, probe

    inp = ports.open_input(args.port)
    out_name = args.out or ports.guess_output_for(args.port)
    if not out_name:
        print("No matching output port; pass --out.", file=sys.stderr)
        return 2
    print(describe(probe(inp, ports.open_output(out_name))))
    return 0


def cmd_init(args) -> int:
    """Send a QLC+ MIDI template's InitMessage straight at the device."""
    from .profile import parse_qxm_init, split_midi_stream

    out_name = args.out or ports.guess_output_for(args.port or "")
    if not out_name:
        print("No output port; pass --out.", file=sys.stderr)
        return 2
    out = ports.open_output(out_name)

    name, data = parse_qxm_init(Path(args.template))
    messages = split_midi_stream(data)
    if not messages:
        print(f"Could not parse any MIDI out of {args.template}", file=sys.stderr)
        return 1
    print(f"{name}\n  {len(data)} bytes -> {len(messages)} message(s) to {out.name}")
    for msg in messages:
        out.send(msg)
        print(f"    {msg}")
    return 0


def cmd_lights(args) -> int:
    """Light the board so the keycaps can be read."""
    from .flash import light_all
    from .learn import is_auto_name

    map_path = Path(args.map)
    if not map_path.exists():
        print(f"{map_path} not found; run `learn` first.", file=sys.stderr)
        return 2
    dmap = DeviceMap.load(map_path)
    out_name = args.out or dmap.output_port or ports.guess_output_for(args.port or "")
    if not out_name:
        print("No output port; pass --out (see `qlc-midi ports`).", file=sys.stderr)
        return 2
    out = ports.open_output(out_name)

    encode = _led_encoder(dmap, args.raw_levels)
    pulse = args.pulse

    def value(level: int) -> int:
        return encode(level, pulse) if encode else level

    if args.off:
        lit = light_all(out, dmap.controls, 0)
        print(f"Cleared {lit} LED(s).")
        return 0

    if args.progress:
        todo = [c for c in dmap.controls if is_auto_name(c)]
        done = [c for c in dmap.controls if not is_auto_name(c)]
        light_all(out, todo, value(args.dim))
        lit = light_all(out, done, value(args.level)) + sum(1 for c in todo if c.feedback)
        print(f"Lit {lit} LEDs: bright = named ({len(done)}), "
              f"dim = still auto-named ({len(todo)}).")
        return 0

    lit = light_all(out, dmap.controls, value(args.level))
    detail = f" at level {args.level}"
    if encode and value(args.level) != args.level:
        detail += f" (sent as {value(args.level)}, {pulse})"
    print(f"Lit {lit} LED(s){detail}.")
    if not args.off:
        print("They stay lit until something else drives them (QLC+, or --off).")
    return 0


def cmd_feedback(args) -> int:
    map_path = Path(args.map)
    dmap = DeviceMap.load(map_path) if map_path.exists() else DeviceMap()
    out_name = args.out or dmap.output_port or ports.guess_output_for(args.port or "")
    if not out_name:
        print("No output port; pass --out (see `qlc-midi ports`).", file=sys.stderr)
        return 2
    out = ports.open_output(out_name)
    dmap.output_port = out.name
    print(f"Feedback output: {out.name}")

    if args.mode == "scan":
        chans = [c - 1 for c in args.channels]
        fb.scan(
            out, kind=args.kind, channels=chans, first=args.first, last=args.last,
            value=args.value, delay=args.delay, hold=args.hold,
        )
        return 0

    if args.mode == "flash":
        from .flash import sweep

        chans = [c - 1 for c in args.channels]
        sweep(
            out, kind=args.kind, channels=chans, first=args.first, last=args.last,
            on_value=args.value, dwell=max(args.delay, 0.5),
        )
        return 0

    if args.mode == "colors":
        matches = [c for c in dmap.controls if c.name == args.control]
        if not matches:
            print(f"No control named {args.control!r} in {map_path}", file=sys.stderr)
            return 2
        table = fb.color_table(out, matches[0], step=args.step)
        colors_path = map_path.with_suffix(".colors.json")
        colors_path.write_text(json.dumps(table, indent=2) + "\n")
        print(f"\nWrote {len(table)} colours to {colors_path}")
        print("Fill in the 'rgb' fields, then pass --colors to `generate`.")
        return 0

    # mode == "echo"
    if not dmap.controls:
        print(f"{map_path} has no controls; run `learn` first.", file=sys.stderr)
        return 2
    if not args.no_blanket and not fb.blanket_test(out, dmap):
        print(
            "\nNothing lit from the buttons' own addresses.  That does not mean the\n"
            "device has no LEDs - try `feedback --mode scan` to sweep other\n"
            "addresses, or check whether it needs a mode-change SysEx first."
        )
        dmap.save(map_path)
        return 0

    count = fb.echo_walk(out, dmap, on_value=args.value)
    dmap.save(map_path)
    print(f"\nConfirmed feedback on {count} controls; saved to {map_path}")
    return 0


def _open_opendeck(args):
    """Open the port pair and return (OpenDeck, inport, outport)."""
    from .opendeck import OpenDeck

    inp = ports.open_input(args.port)
    out_name = args.out or ports.guess_output_for(args.port)
    if not out_name:
        raise SystemExit("No matching output port; pass --out.")
    out = ports.open_output(out_name)
    return OpenDeck(inp, out), inp, out


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower().startswith("y")
    except EOFError:
        return False


def _auto_backup(od, label: str) -> Path:
    """Snapshot the whole config before any write.  Always."""
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = Path("backups") / f"{label}-{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tables = od.dump_all()
    total = sum(len(v) for v in tables.values())
    path.write_text(
        json.dumps({"device": label, "created": stamp, "tables": tables}, indent=2) + "\n"
    )
    print(f"Backup: {path}  ({len(tables)} sections, {total} values)")
    return path


def _device_label(inp) -> str:
    raw = inp.name.split("|")[-1].strip() or inp.name
    return "".join(c if c.isalnum() else "-" for c in raw).strip("-").lower()


def cmd_od_dump(args) -> int:
    from .opendeck import OpenDeckError, summarize, to_device_map

    od, inp, out = _open_opendeck(args)
    try:
        dump = od.dump()
    except OpenDeckError as exc:
        print(f"OpenDeck query failed: {exc}", file=sys.stderr)
        return 1

    map_path = Path(args.map)
    # Keep any pairing already discovered by `identify`.
    prior_leds = DeviceMap.load(map_path).leds if map_path.exists() else []
    prior_pairs = {r["index"]: r.get("button") for r in prior_leds if r.get("button")}

    dmap = DeviceMap(
        manufacturer=args.manufacturer or "OpenDeck",
        model=args.model or inp.name.split("|")[-1].strip(),
        input_port=inp.name,
        output_port=out.name,
    )
    to_device_map(dump, dmap, include_buttons=args.include_buttons)
    for record in dmap.leds:
        if record["index"] in prior_pairs:
            record["button"] = prior_pairs[record["index"]]

    map_path.parent.mkdir(parents=True, exist_ok=True)
    dmap.save(map_path)
    print(f"Read config from {inp.name}\n")
    print(summarize(dump, dmap))
    print(f"\nSaved to {map_path}")

    if args.raw:
        raw_path = map_path.with_suffix(".raw.json")
        raw_path.write_text(json.dumps(dump, indent=2) + "\n")
        print(f"Raw config tables: {raw_path}")
    return 0


def cmd_od_backup(args) -> int:
    od, inp, _ = _open_opendeck(args)
    path = Path(args.file) if args.file else None
    if path is None:
        _auto_backup(od, _device_label(inp))
        return 0
    tables = od.dump_all()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"device": inp.name, "tables": tables}, indent=2) + "\n")
    print(f"Backup: {path}  ({len(tables)} sections, "
          f"{sum(len(v) for v in tables.values())} values)")
    return 0


def cmd_od_restore(args) -> int:
    od, inp, _ = _open_opendeck(args)
    data = json.loads(Path(args.file).read_text())
    tables = data.get("tables", data)

    result = od.restore_all(tables, dry_run=True)
    if not result["changed"]:
        print("Device already matches the backup; nothing to write.")
        return 0

    print(f"{len(result['changed'])} value(s) differ from {args.file}:")
    for key, index, have, want in result["changed"][:40]:
        print(f"  block/section {key} index {index}: {have} -> {want}")
    if len(result["changed"]) > 40:
        print(f"  ... and {len(result['changed']) - 40} more")

    if args.dry_run:
        print("\nDry run - nothing written.")
        return 0
    if not _confirm(f"\nWrite {len(result['changed'])} value(s) to the device?", args.yes):
        print("Aborted.")
        return 1

    # Snapshot the current state too, so a restore is itself undoable.
    _auto_backup(od, _device_label(inp) + "-pre-restore")
    result = od.restore_all(tables)
    print(f"\nRestored {len(result['changed'])} value(s), "
          f"{result['unchanged']} already correct.")
    for key, index, why in result["failed"]:
        print(f"  failed {key}[{index}]: {why}")
    return 1 if result["failed"] else 0


def cmd_od_identify(args) -> int:
    from .flash import flash_and_pair
    from .opendeck import set_led_identity, to_device_map

    od, inp, out = _open_opendeck(args)
    dump = od.dump()
    dmap = DeviceMap(input_port=inp.name, output_port=out.name)
    to_device_map(dump, dmap)
    led_count = len(dmap.leds)
    if not led_count:
        print("Device reports no LEDs.", file=sys.stderr)
        return 1

    print(
        f"\nThis rewrites all {led_count} LED entries so LED i responds to note i\n"
        "on MIDI channel 1, making every LED discoverable regardless of how it\n"
        "is configured now.  A full backup is taken first and restored at the\n"
        "end unless you pass --keep."
    )
    if not _confirm("Proceed?", args.yes):
        print("Aborted; nothing written.")
        return 1

    targets = args.indices if args.indices else list(range(led_count))
    bad = [i for i in targets if i >= led_count]
    if bad:
        print(f"LED index out of range: {bad} (device has {led_count})", file=sys.stderr)
        return 2

    backup = _auto_backup(od, _device_label(inp) + "-pre-identify")
    applied = set_led_identity(od, led_count, channel=1)
    print(f"Set {applied}/{led_count} LEDs to identity mapping.\n")

    addresses = [("note", 0, i) for i in targets]
    found = flash_and_pair(inp, out, addresses, timeout=args.timeout)
    # flash_and_pair keys by position in `addresses`; map back to LED index.
    pairs = {targets[pos]: pressed for pos, pressed in found.items()}

    by_address = {(c.kind, c.channel, c.number): i for i, c in enumerate(dmap.controls)}
    map_path = Path(args.map)
    saved = DeviceMap.load(map_path) if map_path.exists() else dmap
    for record in saved.leds:
        pressed = pairs.get(record["index"])
        if pressed:
            record["button"] = {
                "kind": pressed[0], "channel": pressed[1], "number": pressed[2],
            }
            idx = by_address.get(pressed)
            if idx is not None:
                record["button"]["name"] = dmap.controls[idx].name
    map_path.parent.mkdir(parents=True, exist_ok=True)
    saved.save(map_path)

    unlit = len(targets) - len(pairs)
    print(f"\nPaired {len(pairs)}/{len(targets)} LEDs; {unlit} did not light or were skipped.")
    print(f"Saved pairing to {map_path}")

    if args.keep:
        print("\nLEDs left in identity mapping (--keep).")
        print(f"Restore with: qlc-midi opendeck restore {args.port!r} -f {backup}")
    else:
        data = json.loads(backup.read_text())
        result = od.restore_all(data["tables"])
        print(f"\nRestored original config ({len(result['changed'])} values).")
    print("\nNext: qlc-midi opendeck align <port> -m " + str(map_path))
    return 0


def cmd_od_enable_leds(args) -> int:
    from .opendeck import LED_CONTROL_TYPES, enable_leds

    od, inp, _ = _open_opendeck(args)
    od.connect()

    from .opendeck import BLOCK_LED, LED_CONTROL_TYPE, pair_by_address

    map_path = Path(args.map)
    saved = DeviceMap.load(map_path) if map_path.exists() else DeviceMap()

    if args.all:
        indices = list(range(len(od.read_section(BLOCK_LED, LED_CONTROL_TYPE) or [])))
        print(f"Targeting all {len(indices)} LED slots.")
    elif args.paired_only:
        indices = [r["index"] for r in saved.leds if r.get("button")]
        if not indices:
            print("No LEDs paired by `identify` in this map.", file=sys.stderr)
            return 2
        print(f"Targeting {len(indices)} LED(s) paired by `identify`.")
    else:
        # Default: LEDs already listening on an address some control sends.
        # Lighting a slot no control drives just wastes writes.
        if not saved.controls:
            print(f"{map_path} has no controls; run `learn` first, or use --all.",
                  file=sys.stderr)
            return 2
        pairs = pair_by_address(od.dump(), saved)
        indices = sorted(pairs)
        if not indices:
            print(
                "No LED listens on an address any learned control sends.\n"
                "Run `opendeck identify` to pair them physically, then\n"
                "`opendeck align`, or pass --all.",
                file=sys.stderr,
            )
            return 2
        print(f"Targeting {len(indices)} LED(s) whose address matches a learned control.")

    plan = enable_leds(od, indices, dry_run=True)
    if not plan["changed"]:
        print(f"All {len(indices)} LED(s) already respond to incoming MIDI.")
        return 0

    print(f"Will make {len(plan['changed'])} of {len(indices)} LED(s) MIDI-driven "
          "(activation notes and channels are left untouched):")
    for i, was, now in plan["changed"][:40]:
        print(f"  LED {i:>2}: {LED_CONTROL_TYPES.get(was, was)} -> "
              f"{LED_CONTROL_TYPES.get(now, now)}")
    if len(plan["changed"]) > 40:
        print(f"  ... and {len(plan['changed']) - 40} more")

    if args.dry_run:
        print("\nDry run - nothing written.")
        return 0
    if not _confirm(f"\nWrite {len(plan['changed'])} value(s)?", args.yes):
        print("Aborted.")
        return 1

    _auto_backup(od, _device_label(inp) + "-pre-enable-leds")
    result = enable_leds(od, indices)
    print(f"\nEnabled {len(result['changed']) - len(result['failed'])} LED(s).")
    for i, why in result["failed"]:
        print(f"  LED {i} failed: {why}")
    return 1 if result["failed"] else 0


def cmd_od_align(args) -> int:
    from .opendeck import align_leds, to_device_map

    od, inp, out = _open_opendeck(args)
    dump = od.dump()
    live = DeviceMap()
    to_device_map(dump, live)

    map_path = Path(args.map)
    if not map_path.exists():
        print(f"{map_path} not found; run `opendeck dump` first.", file=sys.stderr)
        return 2
    saved = DeviceMap.load(map_path)

    by_address = {(c.kind, c.channel, c.number): i for i, c in enumerate(live.controls)}
    pairing: dict[int, int] = {}
    for record in saved.leds:
        button = record.get("button")
        if button:
            idx = by_address.get((button["kind"], button["channel"], button["number"]))
            if idx is not None:
                pairing[record["index"]] = idx
        elif args.assume_index_order and record["index"] < len(live.controls):
            control = live.controls[record["index"]]
            if control.kind == "note":
                pairing[record["index"]] = record["index"]

    if not pairing:
        print(
            "No LED-to-button pairing available.\n"
            "Run `qlc-midi opendeck identify` to discover it, or pass\n"
            "--assume-index-order to assume LED i belongs to control i.",
            file=sys.stderr,
        )
        return 2

    plan = align_leds(od, live, pairing, dry_run=True)
    print(f"Will point {len(plan['planned'])} LED(s) at their button's address:")
    for led_index, name, number, channel in plan["planned"][:40]:
        print(f"  LED {led_index:>2} -> {name} (note {number} ch{channel})")
    if len(plan["planned"]) > 40:
        print(f"  ... and {len(plan['planned']) - 40} more")

    if args.dry_run:
        print("\nDry run - nothing written.")
        return 0
    if not _confirm(f"\nWrite {len(plan['planned']) * 4} value(s) to the device?", args.yes):
        print("Aborted.")
        return 1

    _auto_backup(od, _device_label(inp) + "-pre-align")
    result = align_leds(od, live, pairing)
    print(f"\nAligned {len(result['planned']) - len(result['failed'])} LED(s).")
    for led_index, why in result["failed"]:
        print(f"  LED {led_index} failed: {why}")

    # Re-read so the map reflects what the device now actually does.
    refreshed = DeviceMap(
        manufacturer=saved.manufacturer, model=saved.model,
        input_port=inp.name, output_port=out.name,
    )
    to_device_map(od.dump(), refreshed)
    prior = {r["index"]: r.get("button") for r in saved.leds if r.get("button")}
    for record in refreshed.leds:
        if record["index"] in prior:
            record["button"] = prior[record["index"]]
    for control, old in zip(refreshed.controls, saved.controls):
        if old.name and not old.name.startswith(("Button ", "Analog ", "Encoder ")):
            control.name = old.name  # keep hand-given names
    refreshed.save(map_path)
    linked = sum(1 for c in refreshed.controls if c.feedback)
    print(f"{linked} control(s) now have working QLC+ feedback; map updated.")
    return 0


def cmd_generate(args) -> int:
    dmap = DeviceMap.load(Path(args.map))
    colors = None
    if args.colors:
        colors = json.loads(Path(args.colors).read_text())

    send_note_off = None
    if args.note_off is not None:
        send_note_off = args.note_off

    xml = build_qxi(
        dmap,
        author=args.author,
        channel_mode=args.channel_mode,
        send_note_off=send_note_off,
        color_table=colors,
        idle_level=args.idle_level,
    )
    out_path = Path(args.out) if args.out else Path("profiles") / default_filename(dmap)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(xml)

    per_channel = resolve_channel_mode(dmap, args.channel_mode)
    print(f"Wrote {out_path}  ({len(dmap.controls)} channels)")
    print(
        "MIDI channel encoding: "
        + (
            "embedded at bit 12 - set the QLC+ input line to 'any' MIDI channel"
            if per_channel
            else "bare - set the QLC+ input line to MIDI channel "
            f"{min(dmap.midi_channels(), default=0) + 1}"
        )
    )

    lit = [c for c in dmap.controls if c.feedback]
    if args.idle_level:
        print(f"Idle LED level: {args.idle_level} (LEDs stay lit when the widget is off)")
        # QLC+ sends this value as the velocity, and on OpenDeck the velocity
        # also selects a blink speed - so an idle level chosen for brightness
        # alone can leave the whole board pulsing.
        if _led_encoder(dmap, raw=False):
            from .flash import STEADY_LEVELS, opendeck_value

            steady = opendeck_value(args.idle_level, "steady")
            if steady != args.idle_level:
                print(f"  warning: {args.idle_level} makes OpenDeck LEDs blink; "
                      f"{steady} is the nearest steady value")
                print(f"  steady levels: {', '.join(str(v) for v in STEADY_LEVELS)}")

    if args.init_template and lit:
        from .profile import idle_init_message

        level = args.idle_level or 127
        message = idle_init_message(dmap, level)
        template = build_qxm(
            f"{dmap.manufacturer} {dmap.model} idle lights",
            f"Lights all {len(lit)} mapped LEDs at level {level} on connect, so the "
            "board is not dark before QLC+ sends its first feedback.",
            [message],
        )
        tpl_path = Path(args.init_template)
        tpl_path.parent.mkdir(parents=True, exist_ok=True)
        tpl_path.write_text(template)
        print(f"Wrote {tpl_path}  ({len(lit)} LEDs lit at level {level})")
        print("Select it in QLC+: Inputs/Outputs -> the MIDI output -> MIDI template.")

    if args.install:
        dest_dir = install_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / out_path.name
        shutil.copy2(out_path, dest)
        print(f"Installed to {dest}\nRestart QLC+ to pick it up.")
    return 0


def cmd_template(args) -> int:
    xml = build_qxm(args.name, args.description, args.init)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(xml)
    print(f"Wrote {out_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qlc-midi", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ports", help="list MIDI ports")
    sp.set_defaults(func=cmd_ports)

    sp = sub.add_parser("monitor", help="print incoming MIDI with QLC+ channel numbers")
    sp.add_argument("port", help="input port name or substring")
    sp.add_argument("--all", action="store_true", help="also show clock/sysex traffic")
    sp.set_defaults(func=cmd_monitor)

    sp = sub.add_parser("learn", help="build a control map from the device")
    sp.add_argument("port", help="input port name or substring")
    sp.add_argument("-m", "--map", default="maps/device.json", help="map JSON path")
    sp.add_argument("--auto", action="store_true", help="sniff everything, auto-name")
    sp.add_argument("--relabel", action="store_true",
                    help="press-then-name: operate a control, then type its name")
    sp.add_argument("-o", "--out", default="",
                    help="relabel: output port, for lighting the board")
    sp.add_argument("--no-lights", action="store_true",
                    help="relabel: do not drive the LEDs")
    sp.add_argument("--dim", type=int, default=20,
                    help="relabel: brightness for controls still to name")
    sp.add_argument("--bright", type=int, default=127,
                    help="relabel: brightness for controls already named")
    sp.add_argument("--raw-levels", action="store_true",
                    help="relabel: send brightness bytes as-is (no pulse-safe encoding)")
    sp.add_argument("--idle-stop", type=float, default=0.0,
                    help="auto mode: stop after N seconds of silence")
    sp.add_argument("--manufacturer", default="")
    sp.add_argument("--model", default="")
    sp.add_argument("--overwrite", action="store_true", help="start a fresh map")
    sp.set_defaults(func=cmd_learn)

    sp = sub.add_parser("import", help="seed a map from an existing QLC+ .qxi profile")
    sp.add_argument("profile", help="path to a .qxi (or a bare name QLC+ ships)")
    sp.add_argument("-m", "--map", default="maps/device.json")
    sp.add_argument("--overwrite", action="store_true")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("colors", help="list a colour table, or paint it onto the pads")
    sp.add_argument("-m", "--map", default="maps/device.json")
    sp.add_argument("-p", "--port", default="", help="input port, used to guess output")
    sp.add_argument("-o", "--out", default="", help="output port name or substring")
    sp.add_argument("--list", action="store_true", help="print the table, send nothing")
    sp.add_argument("--start", type=int, default=0, help="first colour index to paint")
    sp.set_defaults(func=cmd_colors)

    sp = sub.add_parser("probe", help="identify a device and its discovery options")
    sp.add_argument("port", help="input port name or substring")
    sp.add_argument("-o", "--out", default="", help="output port name or substring")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("init", help="send a QLC+ .qxm template's InitMessage to a device")
    sp.add_argument("template", help="path to a .qxm MIDI template")
    sp.add_argument("-p", "--port", default="", help="input port, used to guess output")
    sp.add_argument("-o", "--out", default="", help="output port name or substring")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("lights", help="light the board's LEDs so keycaps are readable")
    sp.add_argument("-m", "--map", default="maps/device.json")
    sp.add_argument("-p", "--port", default="", help="input port, used to guess output")
    sp.add_argument("-o", "--out", default="", help="output port name or substring")
    sp.add_argument("-l", "--level", type=int, default=127, help="brightness 0-127")
    sp.add_argument("--dim", type=int, default=20, help="brightness for --progress")
    sp.add_argument("--progress", action="store_true",
                    help="bright = named, dim = still auto-named")
    sp.add_argument("--off", action="store_true", help="turn them all off")
    sp.add_argument("--pulse", choices=["steady", "slow", "medium", "fast"],
                    default="steady", help="OpenDeck: blink instead of holding steady")
    sp.add_argument("--raw-levels", action="store_true",
                    help="send brightness bytes as-is, without pulse-safe encoding")
    sp.set_defaults(func=cmd_lights)

    sp = sub.add_parser("feedback", help="explore LED feedback")
    sp.add_argument("--mode", choices=["echo", "scan", "flash", "colors"], default="echo")
    sp.add_argument("-m", "--map", default="maps/device.json")
    sp.add_argument("-p", "--port", default="", help="input port, used to guess output")
    sp.add_argument("-o", "--out", default="", help="output port name or substring")
    sp.add_argument("--value", type=int, default=127, help="on value / velocity")
    sp.add_argument("--no-blanket", action="store_true",
                    help="echo mode: skip the all-at-once pre-test")
    sp.add_argument("--kind", choices=["note", "cc", "pc"], default="note",
                    help="scan mode: message type to sweep")
    sp.add_argument("--channels", type=int, nargs="+", default=[1],
                    help="scan mode: 1-based MIDI channels")
    sp.add_argument("--first", type=int, default=0, help="scan mode: first number")
    sp.add_argument("--last", type=int, default=127, help="scan mode: last number")
    sp.add_argument("--delay", type=float, default=0.15, help="scan mode: seconds/step")
    sp.add_argument("--hold", action="store_true", help="scan mode: leave LEDs lit")
    sp.add_argument("--control", default="", help="colors mode: control name")
    sp.add_argument("--step", type=int, default=1, help="colors mode: value step")
    sp.set_defaults(func=cmd_feedback)

    od = sub.add_parser("opendeck", help="talk to an OpenDeck board over SysEx")
    odsub = od.add_subparsers(dest="action", required=True)

    def od_common(parser):
        parser.add_argument("port", help="input port name or substring")
        parser.add_argument("-o", "--out", default="",
                            help="output port name or substring")
        return parser

    sp = od_common(odsub.add_parser("dump", help="read the config into a map (no writes)"))
    sp.add_argument("-m", "--map", default="maps/device.json")
    sp.add_argument("--manufacturer", default="")
    sp.add_argument("--model", default="")
    sp.add_argument("--raw", action="store_true", help="also dump raw config tables")
    sp.add_argument("--include-buttons", action="store_true",
                    help="also emit a control per switch slot (mostly phantoms)")
    sp.set_defaults(func=cmd_od_dump)

    sp = od_common(odsub.add_parser("backup", help="snapshot the whole config to JSON"))
    sp.add_argument("-f", "--file", default="", help="output path (default: backups/)")
    sp.set_defaults(func=cmd_od_backup)

    sp = od_common(odsub.add_parser("restore", help="write a backup back to the device"))
    sp.add_argument("-f", "--file", required=True, help="backup JSON to restore")
    sp.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    sp.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    sp.set_defaults(func=cmd_od_restore)

    sp = od_common(odsub.add_parser(
        "identify", help="flash each LED and pair it with the button you press"))
    sp.add_argument("-m", "--map", default="maps/device.json")
    sp.add_argument("--keep", action="store_true",
                    help="leave the identity mapping in place instead of restoring")
    sp.add_argument("--timeout", type=float, default=30.0,
                    help="seconds to wait per LED before moving on")
    sp.add_argument("--indices", type=int, nargs="+",
                    help="only these LED indices, to redo the ones that were missed")
    sp.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    sp.set_defaults(func=cmd_od_identify)

    sp = od_common(odsub.add_parser(
        "enable-leds",
        help="make LEDs respond to incoming MIDI, without changing their notes"))
    sp.add_argument("-m", "--map", default="maps/device.json")
    sp.add_argument("--paired-only", action="store_true",
                    help="only LEDs paired to a button by `identify`")
    sp.add_argument("--all", action="store_true",
                    help="every LED slot the firmware exposes, matched or not")
    sp.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    sp.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    sp.set_defaults(func=cmd_od_enable_leds)

    sp = od_common(odsub.add_parser(
        "align", help="point each LED at its own button's note so QLC+ can light it"))
    sp.add_argument("-m", "--map", default="maps/device.json")
    sp.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    sp.add_argument("--assume-index-order", action="store_true",
                    help="assume LED i belongs to control i (unverified)")
    sp.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    sp.set_defaults(func=cmd_od_align)

    sp = sub.add_parser("generate", help="write the QLC+ .qxi input profile")
    sp.add_argument("-m", "--map", default="maps/device.json")
    sp.add_argument("-o", "--out", default="", help="output .qxi path")
    sp.add_argument("--author", default="")
    sp.add_argument("--channel-mode", choices=["auto", "any", "fixed"], default="auto")
    sp.add_argument("--colors", default="", help="colour table JSON from feedback mode")
    sp.add_argument("--idle-level", type=int, default=0, metavar="N",
                    help="keep LEDs lit at brightness N (1-127) when the widget "
                         "is off, instead of dark")
    sp.add_argument("--init-template", default="", metavar="PATH",
                    help="also write a .qxm that lights the LEDs on connect")
    sp.add_argument("--note-off", dest="note_off", action="store_true", default=None,
                    help="emit MIDISendNoteOff=True")
    sp.add_argument("--no-note-off", dest="note_off", action="store_false",
                    help="emit MIDISendNoteOff=False")
    sp.add_argument("--install", action="store_true",
                    help="copy into the QLC+ user InputProfiles directory")
    sp.set_defaults(func=cmd_generate)

    sp = sub.add_parser("template", help="write a QLC+ .qxm MIDI template (init SysEx)")
    sp.add_argument("name")
    sp.add_argument("--description", default="")
    sp.add_argument("--init", nargs="+", required=True,
                    help='SysEx bytes, e.g. "F0 00 53 43 ... F7"')
    sp.add_argument("-o", "--out", default="profiles/template.qxm")
    sp.set_defaults(func=cmd_template)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
