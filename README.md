# qlcplus-midi-profiler

Build a [QLC+](https://www.qlcplus.org/) MIDI input profile (`.qxi`) from a real
controller, instead of hand-writing a hundred `<Channel>` elements and guessing
channel numbers.

Two ways to get a map:

- **Ask the device.** [OpenDeck](https://github.com/shanteacontrols/OpenDeck)
  boards keep their whole configuration in a SysEx-readable database, so the
  full map — every button's note, every fader's CC, every LED address — comes
  out in one command without pressing anything.
- **Listen to the device.** For everything else, press and wiggle each control
  and the tool records, classifies, and names it.

It also probes whether the controller's LEDs respond to MIDI sent back to it,
which is what QLC+ needs for button feedback.

## Install

```sh
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python qlc-midi ports
```

## Quick start

### OpenDeck board

```sh
./venv/bin/python qlc-midi opendeck dump OpenDeck -m maps/mydeck.json --raw
./venv/bin/python qlc-midi generate -m maps/mydeck.json --install
```

The dump names controls positionally (`Button 1`, `Analog 3`). To attach real
names, run the interactive learner over the same map — it recognises addresses
it already knows and just relabels them:

```sh
./venv/bin/python qlc-midi learn OpenDeck -m maps/mydeck.json
```

### Any other controller

```sh
# sniff everything: operate every control once, Ctrl-C when done
./venv/bin/python qlc-midi learn "Launchkey MK4 49 MIDI Out" -m maps/lk.json --auto

# or name-then-press, one control at a time
./venv/bin/python qlc-midi learn "Launchkey MK4 49 MIDI Out" -m maps/lk.json
```

`monitor` prints incoming MIDI alongside the QLC+ channel number each message
maps to, which is handy when a binding in QLC+ is not firing.

## Commands

| Command | What it does |
| --- | --- |
| `ports` | List MIDI inputs and outputs |
| `monitor PORT` | Print incoming MIDI with QLC+ channel numbers |
| `learn PORT` | Build a control map (`--auto` to sniff everything at once) |
| `opendeck dump` | Read an OpenDeck board's config over SysEx |
| `opendeck backup` | Snapshot the entire config to JSON |
| `opendeck restore` | Write a backup back to the device |
| `opendeck identify` | Flash each LED, pair it with the button you press |
| `opendeck align` | Point each LED at its own button's note |
| `lights` | Light the board so the keycaps can be read (`--progress`, `--off`) |
| `feedback` | Probe LED feedback (`--mode echo\|scan\|flash\|colors`) |
| `generate` | Write the `.qxi` (`--install` to drop it in QLC+'s profile dir) |
| `template` | Write a `.qxm` MIDI template carrying init SysEx |

The map is plain JSON. Editing names by hand is expected and easy.

## How QLC+ numbers MIDI channels

QLC+ flattens every MIDI message into one integer, per
`plugins/midi/src/common/midiprotocol.h`:

| Message | Range |
| --- | --- |
| Control change | 0–127 |
| Note | 128–255 |
| Note aftertouch | 256–383 |
| Program change | 384–511 |
| Channel aftertouch | 512 |
| Pitch wheel | 513 |

When the QLC+ input line is pinned to one MIDI channel, that number is used
bare. When it is set to **any** channel, the 0-based MIDI channel is added at
bit 12 — `channel += midi_channel * 4096`.

`generate --channel-mode` picks between them:

- `auto` (default) — encode the MIDI channel unless every control is already on
  MIDI channel 1, where both encodings are identical anyway
- `any` — always encode; pair with the QLC+ input line set to "any" channel
- `fixed` — never encode; pair with the input line pinned to one channel

`generate` prints which one it used and what to set in QLC+. If a device spans
several MIDI channels, `fixed` will collide and the tool refuses rather than
silently dropping controls.

## Making feedback work

QLC+ button feedback sends the message **back on the same channel number it
received**. The `<Feedback>` element only tunes the on/off values and,
optionally, the outgoing MIDI channel — it cannot redirect note 40 to light an
LED that listens on note 12.

So feedback works only when a button's LED listens on that same note (or CC) and
MIDI channel. `qlc-midi opendeck` links the ones that already match and reports
the rest; `qlc-midi feedback` finds out empirically on other controllers:

```sh
# does anything light at all, and which controls?
./venv/bin/python qlc-midi feedback -m maps/mydeck.json --mode echo

# sweep an address space and watch which LED each address lights
./venv/bin/python qlc-midi feedback --mode scan --out OpenDeck --channels 1 9

# build a <ColorTable> for an RGB pad
./venv/bin/python qlc-midi feedback --mode colors -m maps/mydeck.json \
    --control "Pad 1" --out OpenDeck
```

If the LEDs answer on different addresses than the buttons report, there are
three ways out, in order of preference:

1. **Reconfigure the controller** so each LED's activation note matches its
   button's note and channel. On OpenDeck, `opendeck align` does exactly this —
   see below. Afterwards QLC+ feedback works natively.
2. **Send init SysEx** if the device has a mode that aligns them — put it in a
   `.qxm` template via `qlc-midi template` and select it in the QLC+ MIDI
   plugin.
3. **Translate in the middle** with a small MIDI bridge. Most flexible, but it
   is another process to keep running.

### Which LEDs are even addressable?

A board can look dead to MIDI simply because its outputs are configured as
`Static` or `Local` — they ignore incoming MIDI no matter what you send.
`feedback --mode flash` walks an address space and blinks each address, which
is far easier to spot than a steady light on a board that already has LEDs on:

```sh
./venv/bin/python qlc-midi feedback --mode flash --out OpenDeck \
    --kind note --channels 1 --first 0 --last 63
```

If nothing blinks anywhere, the outputs are not MIDI-driven yet — on OpenDeck,
`opendeck identify` fixes that as part of its run.

### Pairing LEDs to buttons on OpenDeck

`opendeck identify` answers the question the config alone cannot: which LED sits
under which button. Nothing in the device's database says so, and LED index *i*
is not reliably button *i*.

```sh
./venv/bin/python qlc-midi opendeck identify OpenDeck -m maps/mydeck.json
```

It backs up the config, temporarily makes every LED respond to note *i* on MIDI
channel 1 so all of them are discoverable, then blinks them one at a time. You
press the button under the blinking one and the pairing is recorded — observed,
not assumed. Enter skips an LED that does not light. The original config is
restored at the end unless you pass `--keep`.

Then apply the alignment:

```sh
./venv/bin/python qlc-midi opendeck align OpenDeck -m maps/mydeck.json --dry-run
./venv/bin/python qlc-midi opendeck align OpenDeck -m maps/mydeck.json
```

For each paired LED this writes activation note, MIDI channel, activation value
and control type (`MidiInNoteMultiVal`, so velocity drives brightness). It
re-reads the device afterwards and updates the map, so `generate` then emits
`<Feedback>` for every button that really works.

`--assume-index-order` skips `identify` and assumes LED *i* belongs to control
*i*. Quick, and wrong on plenty of boards — verify with `identify` if the result
looks scrambled.

## OpenDeck LED values are not just brightness

An OpenDeck output reads **blink speed and brightness out of the same 7-bit
value** (`io/outputs/instance/impl/mapper.cpp`):

```
value < 16                -> steady
otherwise (value % 16) / 4 -> 0: 1000ms   1: 500ms   2: 250ms   3: steady
```

Brightness is the value scaled across 0–127, independently. So a value picked
purely for brightness can set the LED blinking — `20` is not "dim", it is
half-brightness pulsing twice a second. Only every fourth band holds steady:

| Steady levels | 15 | 31 | 47 | 63 | 79 | 95 | 111 | 127 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |

`lights` and `learn --relabel` encode brightness through this automatically for
maps whose manufacturer is OpenDeck, and `--pulse slow|medium|fast` asks for a
blink on purpose rather than by accident. `--raw-levels` sends the byte
untouched, which is what other manufacturers need. `generate --idle-level`
warns when the level you chose would leave the board pulsing.

## Undo and redo

Every command that writes to the device takes a full config backup first, into
`backups/`, without being asked. A backup is a complete snapshot of every
section the firmware will hand over, so restoring one puts the board back
exactly as it was:

```sh
# undo: put the board back
./venv/bin/python qlc-midi opendeck restore OpenDeck -f backups/mydeck-20260804-162202.json
```

`restore` diffs against the live device first and writes only what actually
differs, showing you the list before touching anything. `--dry-run` shows the
diff and stops.

Redo works because `restore` also snapshots the *current* state before it
overwrites it, as `...-pre-restore-<timestamp>.json`. So undoing an alignment
leaves you a backup of the aligned state; restore that one to get it back.
Backups are ordinary JSON — keep the ones that matter and delete the rest.

```sh
./venv/bin/python qlc-midi opendeck backup OpenDeck -f backups/factory.json
```

## Control classification

`learn` guesses the QLC+ channel type from the values a control emits:

| Emitted | Type |
| --- | --- |
| Note on/off, program change | `Button` |
| CC with only two values, one of them 0 | `Button` |
| CC cycling a small set of increments, never 0 (e.g. 1/127 or 63/65) | `Encoder` |
| CC sweeping a wide range | `Slider` |
| CC over a narrow range | `Knob` |
| Pitch wheel, aftertouch | `Slider` |

Guesses land in the JSON map; correct them there if a control is misread.

## Tests

No hardware needed — the encodings are checked against channel numbers taken
from QLC+'s own shipped profiles:

```sh
./venv/bin/python tests/test_profiler.py
```
