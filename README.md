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
./venv/bin/python qlc-midi opendeck OpenDeck -m maps/mydeck.json --raw
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
| `opendeck PORT` | Read an OpenDeck board's config over SysEx |
| `feedback` | Probe LED feedback (`--mode echo\|scan\|colors`) |
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
   button's note and channel. On OpenDeck this is a SysEx write to the LED
   block; the OpenDeck web configurator does it too. Afterwards QLC+ feedback
   works natively.
2. **Send init SysEx** if the device has a mode that aligns them — put it in a
   `.qxm` template via `qlc-midi template` and select it in the QLC+ MIDI
   plugin.
3. **Translate in the middle** with a small MIDI bridge. Most flexible, but it
   is another process to keep running.

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
