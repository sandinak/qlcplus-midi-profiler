"""qlc-midi: learn a MIDI controller and emit a QLC+ input profile."""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from . import feedback as fb
from . import ports
from .events import from_mido, qlc_channel
from .learn import DeviceMap, learn_auto, learn_interactive
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
    else:
        learn_interactive(port, dmap, port.name)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    dmap.save(out_path)
    print(f"\nSaved {len(dmap.controls)} controls to {out_path}")
    for c in dmap.controls:
        print(f"  {c.name:<24} {c.describe()}")
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

    if args.mode == "colors":
        matches = [c for c in dmap.controls if c.name == args.control]
        if not matches:
            print(f"No control named {args.control!r} in {map_path}", file=sys.stderr)
            return 2
        table = fb.color_table(out, matches[0], step=args.step)
        colors_path = map_path.with_suffix(".colors.json")
        import json

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


def cmd_opendeck(args) -> int:
    from .opendeck import OpenDeck, OpenDeckError, summarize, to_device_map

    inp = ports.open_input(args.port)
    out_name = args.out or ports.guess_output_for(args.port)
    if not out_name:
        print("No matching output port; pass --out.", file=sys.stderr)
        return 2
    out = ports.open_output(out_name)

    try:
        dump = OpenDeck(inp, out).dump()
    except OpenDeckError as exc:
        print(f"OpenDeck query failed: {exc}", file=sys.stderr)
        return 1

    dmap = DeviceMap(
        manufacturer=args.manufacturer or "OpenDeck",
        model=args.model or inp.name.split("|")[-1].strip(),
        input_port=inp.name,
        output_port=out.name,
    )
    to_device_map(dump, dmap)

    map_path = Path(args.map)
    map_path.parent.mkdir(parents=True, exist_ok=True)
    dmap.save(map_path)
    print(f"Read config from {inp.name}\n")
    print(summarize(dump, dmap))
    print(f"\nSaved to {map_path}")

    if args.raw:
        import json

        raw_path = map_path.with_suffix(".raw.json")
        raw_path.write_text(json.dumps(dump, indent=2) + "\n")
        print(f"Raw config tables: {raw_path}")
    return 0


def cmd_generate(args) -> int:
    dmap = DeviceMap.load(Path(args.map))
    colors = None
    if args.colors:
        import json

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
    sp.add_argument("--idle-stop", type=float, default=0.0,
                    help="auto mode: stop after N seconds of silence")
    sp.add_argument("--manufacturer", default="")
    sp.add_argument("--model", default="")
    sp.add_argument("--overwrite", action="store_true", help="start a fresh map")
    sp.set_defaults(func=cmd_learn)

    sp = sub.add_parser("feedback", help="explore LED feedback")
    sp.add_argument("--mode", choices=["echo", "scan", "colors"], default="echo")
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

    sp = sub.add_parser(
        "opendeck",
        help="read an OpenDeck board's config over SysEx (no button pressing)",
    )
    sp.add_argument("port", help="input port name or substring")
    sp.add_argument("-o", "--out", default="", help="output port name or substring")
    sp.add_argument("-m", "--map", default="maps/device.json")
    sp.add_argument("--manufacturer", default="")
    sp.add_argument("--model", default="")
    sp.add_argument("--raw", action="store_true", help="also dump raw config tables")
    sp.set_defaults(func=cmd_opendeck)

    sp = sub.add_parser("generate", help="write the QLC+ .qxi input profile")
    sp.add_argument("-m", "--map", default="maps/device.json")
    sp.add_argument("-o", "--out", default="", help="output .qxi path")
    sp.add_argument("--author", default="")
    sp.add_argument("--channel-mode", choices=["auto", "any", "fixed"], default="auto")
    sp.add_argument("--colors", default="", help="colour table JSON from feedback mode")
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
