# Touch-Seq

**A touchscreen live-performance front end for the [Carla](https://github.com/falkTX/Carla) plugin host.**
Carla runs the plugins (LV2, VST3, CLAP); Touch-Seq is the instrument you play them with: a step sequencer,
per-track plugin chains with a master bus, chord pads / keyboard / drum pads, an MPC-style XY effects pad
and performance faders — laid out as modules in a modular-synth rack, made for fingers on a touchscreen
(multi-touch; a mouse works too).

![Touch-Seq: faders, plugin host, sequencer and chord pads](docs/screenshots/overview.png)

Version 0.1.1 — early, but already played with.

## The rack

The window is a 2×2 rack under a toolbar (**FILE**: projects, **SETTINGS**: key/scale and audio output,
**PATCHBAY**: audio routing). **⤢** on a module's rail blows it up to full screen; everything else keeps
running.

### SEQ-64 — step sequencer

- any number of **melody**, **drum** and **external MIDI** tracks, each on its own MIDI channel; **+** adds
  one (the dialog picks the kind, and for external the MIDI output it sends to)
- 16 patterns per track, 1–64 steps (polymetric tracks are fine), 8 **scenes**
- pattern and scene changes are **quantized to the bar**: the current pattern plays to the end of the bar
- **GRID** shows all tracks; **EDIT** opens the selected one:
  - melody: a **polyphonic piano roll** with a velocity/gate lane; notes can be stretched over several steps;
    rows outside the project's scale are dimmed
  - drums: a voice × step matrix, voices in fixed colors (kick red, snare orange, hats yellow, …)
  - external MIDI: the same piano roll, but the track has **no plugins**: its notes, velocity, gate and the
    faders go straight out to the chosen hardware output (and nowhere else), and its header shows the port

![Piano roll](docs/screenshots/piano-roll.png)
![Drum editor](docs/screenshots/drums.png)

### HOST-4 — plugins (Carla)

- every plugin track has a chain: **instrument → 2 effects → level/mute**; all tracks go through the
  **MASTER** (3 effects, e.g. a global reverb) to the audio output. **External MIDI** tracks have no chain at
  all — the panel shows their output port instead, with **MIDI OUT** to change it
- **TRACK** view: the chain of the track selected in the sequencer and the plugin's parameters as knobs,
  colored by what they are (filter, envelope, LFO, oscillator, level, effect)
- **MIXER** view: a column per plugin track plus the master
- a touch-friendly plugin browser (format filter, A–Z strip, **ADD** buttons so scrolling never adds),
  plugin GUIs open in their own windows (**UI**)
- the audio output follows PipeWire's default (headphones ↔ speakers) or is pinned in SETTINGS → AUDIO

![Mixer](docs/screenshots/mixer.png)

### PLAY-12 — live play

Plays the track selected in the sequencer:

- melody tracks: **chord pads** in the project's key (7 diatonic chords colored by function + 5 borrowed
  ones), with 7/9/sus2/sus4, strum, octave and **voice leading** (each chord takes the inversion nearest
  the last one), or a **keyboard** (2 octaves in the module, 4 full screen, glissando)
- drum tracks: 12 pads in the voices' colors
- external MIDI tracks: like melody tracks (chords, keyboard), straight out to their output port
- **REC** (replace, like tape) or **OVR** (overdub) records what you play into the pattern, quantized to
  1/16, 1/8 or 1/4
- with **XYFX** on the master, a **MASTER** row appears in the sequencer; selecting it turns this module
  into an **XY pad**: beat repeat left–right (straight on the right half, triplets or reverse on the left),
  low-pass down / high-pass up, the centre is "dry"

![Keyboard](docs/screenshots/keys.png)
![XY pad](docs/screenshots/xy-pad.png)

### CTRL-10 — performance faders

Pitch bend (springs back), mod wheel and 8 CC faders. Each fader has a **target**:

- **TRACK** — that track's instrument, on its channel; **SEL** = whichever track is selected
- **CHANNEL** — plain MIDI on a channel (outboard gear on the MIDI port)
- **PARAM** — a plugin parameter directly, no MIDI learn needed: hold the parameter's knob in HOST-4 and
  pick a fader; fader and knob then move together

Optional 14-bit (MSB/LSB pairs) and bipolar faders.

![Fader targets](docs/screenshots/faders-edit.png)

### Projects and settings

FILE lists projects, newest first (open, NEW, DUPLICATE, hold DELETE); switching restarts the app with the
other project. SETTINGS sets the project's key and scale (chord pads, piano roll) and where plugins play.

![Settings](docs/screenshots/settings.png)

### PATCHBAY — audio routing

Every audio port of the machine, grouped by the device or plugin it belongs to: sources on the left,
destinations on the right, and a **cable** drawn for every link between the two. Tap a port and then one on
the other side to patch them, the same pair again to cut it — one source stays chosen, so a 4-channel input is
patched with a tap per channel. That is how a synth plugged into an interface's input is heard through that
interface's output, or how a plugin's output is sent to another device. Cables are drawn for the ports on
screen; the links themselves are kept with this machine (config.json, not the project) and re-made when a
device comes back. The app's own cables — plugin chains and the master output — are thin and dashed and are
left to the app.

## Install

Download the AppImage for your machine from [Releases](https://github.com/marcin-koziol/touch-seq/releases),
then:

```sh
chmod +x Touch-Seq-0.1.1-x86_64.AppImage
./Touch-Seq-0.1.1-x86_64.AppImage
```

It contains Python, Qt (PySide6), Carla and XYFX. The machine needs:

- Linux, x86_64 or aarch64 (e.g. Raspberry Pi 4/5 with a 64-bit OS), glibc 2.35 or newer
  (Ubuntu 22.04+, Debian 12+, Fedora 36+, Arch, Raspberry Pi OS bookworm)
- **PipeWire** with its JACK support (`pipewire-jack`) and the `pw-link` tool — the plugin host runs on
  JACK and wires plugins with `pw-link`. Without them the sequencer, pads and faders still work over MIDI;
  HOST-4 just says it's unavailable.
- your plugins in the usual places (`~/.clap`, `~/.lv2`, `~/.vst3`, `/usr/lib/…`); the first start scans
  them in the background (a minute or two), later starts use the cache — **RESCAN** in the plugin browser
  picks up new ones

Touch-Seq also sends everything to a MIDI port (a virtual "Touch-Seq" port by default) with clock and
start/stop, so it can drive outboard gear or a DAW too. An **external MIDI** track is the other way round: it
sends to its own output (chosen when the track is created, changeable in HOST-4) and to nothing else — if the
device is unplugged its notes are simply not sent, and they come back when it reappears.

## Less obvious things

| where | gesture | does |
|---|---|---|
| everywhere | **hold** (~1 s) on DELETE / REMOVE | destructive actions need a hold |
| module rail | **⤢ / ⤡** | module full screen and back |
| sequencer | tap **BPM** | tempo popup: drag the number, ±1/5/10, TAP tempo |
| sequencer | tap a track header twice | opens it in EDIT |
| sequencer | drag a **pattern slot** onto another | copies the pattern there and switches to it (at the bar) |
| sequencer | drag along a row | paints steps on/off |
| piano roll | tap empty / tap a note | place / remove; drag a note sideways = length, up/down = pitch |
| piano roll | drag the keyboard on the left | scroll; OCT ▼▲ jumps an octave |
| plugin knobs | drag up/down; **hold** | turn; hold = drive this parameter from a CTRL fader |
| chord pads | 7 / 9 / SUS while holding a chord | the held chord changes right away |
| LEAD | toggle off and on | restarts voice leading from root position |
| XY pad | LATCH | keeps the effect on after the finger lifts |
| keyboard | lower on the key | louder (pads: higher = louder) |

Keys (with a keyboard attached): **Space** play/stop, **Esc** closes a dialog/popup, then quits; **Ctrl+C** quits.

## Configuration

`~/.config/touch-seq/config.json` (created on first start):

| key | meaning |
|---|---|
| `screen` | output to go full screen on, e.g. `HDMI-1`; empty = primary screen |
| `midi_port` | MIDI out: an existing port whose name contains this, otherwise a virtual port of that name |
| `projects` | projects folder (default `~/.local/share/touch-seq/projects`) |
| `audio_out` | `default` (follow PipeWire's default output) or a sink node name — easier set in SETTINGS → AUDIO |
| `patch` | audio links made in PATCHBAY, as `[[out port, in port], …]` — this machine's cables |

Projects save themselves (every couple of seconds while editing, and on exit); plugin states are saved next
to each project in `<name>.plugins/`.

**Touchscreen on a multi-monitor GNOME/Wayland desktop:** tell Mutter which monitor the touch panel belongs
to — the touch device's vendor:product from `lsusb`, the monitor's vendor/product/serial from
`gdbus call --session --dest org.gnome.Mutter.DisplayConfig --object-path /org/gnome/Mutter/DisplayConfig --method org.gnome.Mutter.DisplayConfig.GetCurrentState`:

```sh
gsettings set org.gnome.desktop.peripherals.touchscreen:/org/gnome/desktop/peripherals/touchscreens/0eef:c000/ \
  output "['IVM', 'PLT2215      ', '0x0000007d']"
```

Raw touch events can be logged with `TOUCHSEQ_TOUCH_LOG=touch.log` to debug a touch panel.

## Running from source

```sh
git clone --recursive https://github.com/marcin-koziol/touch-seq.git && cd touch-seq
python3 -m venv .venv && .venv/bin/pip install -r packaging/requirements.txt
.venv/bin/python touchseq.py
```

The plugin host needs Carla **with CLAP support**, which released Carla (2.5.x) doesn't have yet: install a
git build (e.g. `carla-git` on Arch) or build the engine with `packaging/build-carla.sh ~/carla-touchseq`
and run with `CARLA_PREFIX=~/carla-touchseq`. A `config.json` next to `touchseq.py` takes precedence over
the one in `~/.config` (handy for development).

## XYFX

[`xyfx/`](xyfx) is Touch-Seq's own effect: a CLAP + LV2 plugin (no GUI — the XY pad is its UI) built with
[DPF](https://github.com/DISTRHO/DPF). The AppImage includes it; to use it from a source checkout or in a
DAW:

```sh
xyfx/install.sh    # build, run its DSP test, install to ~/.clap and ~/.lv2
```

Releases also have `touchseq-xyfx-*.tar.gz` with the built plugin. Its parameters (Engage, X, Y, BPM, Mode)
can be automated from any host.

## Development

```sh
QT_QPA_PLATFORM=offscreen python tests/test_app.py   # sequencer, UI, projects, live play: no audio (CI)
QT_QPA_PLATFORM=offscreen python tests/test_host.py  # plugin host with real Carla + PipeWire (local only)
QT_QPA_PLATFORM=offscreen python tests/test_xyfx.py  # XYFX + XY pad with real Carla (local only)
packaging/build-appimage.sh                           # dist/Touch-Seq-<version>-<arch>.AppImage
```

CI runs the app tests and the XYFX build and DSP test on every push; a `v*` tag builds AppImages for x86_64
and aarch64 and publishes a release.

The app is `touchseq.py` (the entry point: it flattens the package back into one namespace, so `import
touchseq as ts` in the tests sees everything) plus the `tseq/` package, one module per concern:
`model` (the project and its files), `engine` (MIDI out and the playback thread), `ui` (the drawing toolkit,
`Panel` and the module window), one module per rack panel (`sequencer`, `controls`, `host`, `performer`),
then `patchbay` (audio routing), `rack`, `dialogs` and `app` (startup). `tseq/__init__.py` lists them in
dependency order; nothing in the package imports anything above it.

## License

Touch-Seq is GPL-3.0-or-later ([LICENSE](LICENSE)). XYFX is ISC ([xyfx/LICENSE](xyfx/LICENSE)). The AppImage
bundles Carla (GPL-2.0-or-later, built from a pinned git commit; its source is attached to each release),
PySide6/Qt (LGPL-3.0) and python-rtmidi (MIT).
