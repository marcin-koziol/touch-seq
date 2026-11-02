# Touch-Seq

**A touchscreen live-performance front end for the [Carla](https://github.com/falkTX/Carla) plugin host.**
Carla runs the plugins (LV2, VST3, CLAP); Touch-Seq is the instrument you play them with: a step sequencer,
per-track plugin chains with a master bus, chord pads / keyboard / drum pads, an MPC-style XY effects pad
and performance faders — laid out as modules in a modular-synth rack, made for fingers on a touchscreen
(multi-touch; a mouse works too).

![Touch-Seq: faders, plugin host, sequencer and chord pads](docs/screenshots/overview.png)

Version 0.6.1 — early, but already played with.

## The rack

The window is a 2×2 rack under a toolbar (**FILE**: projects, **SETTINGS**: key/scale and audio output,
**PATCHBAY**: audio routing, **SAVE**: write now — it lights up while there is something unsaved, and the time
next to it is the last save of anything, project or plugin). **⤢** on a module's rail blows it up to full
screen; everything else keeps running.

### SEQ-64 — step sequencer

- any number of **melody**, **drum**, **external MIDI** and **audio** tracks; **+** adds one (the dialog picks
  the kind, and for external the MIDI output it sends to, for audio the interface input it takes)
- 16 patterns per track, 1–64 steps (polymetric tracks are fine), 8 **scenes**
- pattern and scene changes are **quantized to the bar**: the current pattern plays to the end of the bar
- rows are **yours to order**: hold a track's header for a moment, then drag it up or down — the track keeps its
  number and colour, and the same works for the steps of a song (hold a step, drag it in the list)
- **SONG** is the arrangement above the patterns: a list of steps, each lasting 1–16 bars (×1–8 repeats) and
  saying which pattern every track plays in it — a track left as **—** keeps what it was playing, so a step
  can change just one part. Tap a step to play it from the next bar, tap its bars or ×repeats to change them,
  hold **✕** to delete, **+ STEP** appends one; the **SONG** button switches the transport between following
  the song and playing the patterns you pick by hand
- the top bar carries the transport's own settings, apart from the app's buttons (FILE, SETTINGS, PATCHBAY
  are icons; a line, then the metronome's **CLICK**, the **COUNT**-in of 0/1/2 bars — the count-in plays its
  clicks first and the pattern starts on the downbeat after them — and the **BPM**, which opens the tempo)
- **GRID** shows all tracks; **EDIT** opens the selected one:
  - melody: a **polyphonic piano roll** with a velocity/gate lane; notes can be stretched over several steps;
    rows outside the project's scale are dimmed
  - drums: a voice × step matrix, voices in fixed colors (kick red, snare orange, hats yellow, …)
  - external MIDI: the same piano roll, but the track has **no plugins**: its notes, velocity, gate and the
    faders go straight out to the chosen hardware output (and nowhere else), and its header shows the port
  - audio: an **interface input** (a mono capture port, e.g. the UMC404HD's FL) with **L / C / R** panning —
    the one track kind with no MIDI at all. It is patched by the rack, so it survives chain changes: its input
    goes through the track's effects (HOST-4) and on into its bus or the master, and that is how a synth
    plugged into an input is heard through the master's reverb
- every MIDI track (melody, drum, external) can play and record from its **own MIDI keyboard**: the socket
  at the left of its EDIT header opens a dialog with the system's MIDI inputs and a channel (**ALL** or 1–16,
  so one keyboard can drive several tracks on different channels). The socket is **green with the channel**
  once configured, **amber** when that device is away, dim when the track only plays its patterns (its row in
  GRID shows the same socket, small, so you can see which tracks have a keyboard from there). Incoming
  notes play the track's instrument (or its hardware output), light its pads/keys in PLAY-12 and are recorded
  when REC/OVR is armed — quantized like the on-screen pads, into the track the keyboard is routed to (which
  need not be the selected one). Pitch bend and the mod wheel reach the instrument too. A note keeps the
  length you held it for, over the bar line included
- the same keyboard is the **transport**: its play/stop buttons start and stop the sequencer (MIDI
  Start/Continue/Stop, or CC 115/116), and its **clock** sets the tempo — 24 pulses per beat, smoothed, so
  the project runs at the keyboard's bpm
- in EDIT, **▼ trans / ▲ trans** moves the whole pattern one scale degree down or up: the step comes from the
  project's key and scale, so the pattern stays in key (a chromatic note keeps its alteration, octaves wrap);
  drum tracks have fixed voices and don't offer it

![The metronome: CLICK and COUNT in the top bar, counting in before the pattern](docs/screenshots/metronome.png)
![SONG: a list of steps, each with its bars, repeats and a pattern per track](docs/screenshots/song.png)
![Piano roll](docs/screenshots/piano-roll.png)
![Drum editor](docs/screenshots/drums.png)
![Adding a track: drum, melody, external MIDI or audio](docs/screenshots/new-track.png)
![An audio track: an interface input, panned, through the track's chain and into a bus](docs/screenshots/audio.png)
![A track's MIDI keyboard: the socket in EDIT and the dialog with the inputs and the channel](docs/screenshots/midi-in.png)

### EQ — eight bands, drawn in the app

Touch-Seq EQ is our own CLAP/LV2 plugin (`eq/`) with **no window of its own**: pick it in a chain and HOST-4's
parameter area becomes its editor. Eight bands on a log frequency axis with the summed curve and the plugin's
**spectrum** behind it: drag a dot for frequency (sideways) and boost/cut (up and down), tap an empty spot to
put the chosen band there — near the bass end it becomes a **low cut**, near the top a **high cut**, in the
middle a bell — and the menu underneath sets its **mode** (bell, low/high shelf, low/high cut) and **width**.
The spectrum is measured in the plugin and read back through output parameters, because Touch-Seq has no audio
buffers of its own; the curve is drawn from the same biquad maths the plugin runs.

![The EQ: the curve, the spectrum, the bands and their menu](docs/screenshots/eq.png)

### HOST-4 — plugins (Carla)

Carla runs inside the app's process as a JACK client named **Touch-Seq**, and every plugin gets a client of
its own under that name ("Touch-Seq/T1.1 Surge XT"); the audio links are built from those names. Two engines
with one name would make Carla rename one ("Touch-Seq.173/…") and every link the app builds would then fail —
silence, while the app looks fine. So a second Touch-Seq (or one still closing after a project switch) takes
the first free name (*Touch-Seq 2*, …) and says so in HOST-4's status line: both keep playing.

- every plugin track has a chain: **instrument → 2 effects → level/mute**; all tracks go through the
  **MASTER** (3 effects, e.g. a global reverb) to the audio output. **External MIDI** tracks have no chain at
  all — the panel shows their output port instead, with **MIDI OUT** to change it
- **audio tracks** have a chain too (slot 0 is an effect, not an instrument): the input is patched into it, so
  effects and the fader work as soon as there is one. A track with no effects gets **Touch-Seq Monitor**, a
  transparent gain/pan node (`monitor/` in this repo, built like XYFX) — it is what gives a bare input a
  level to meter and a fader to set; remove it and the input is a plain patch again (no meter, no fader).
  The **metronome** is the same idea the other way round: `click/` sits between the master and the output and
  plays a click for every beat note the sequencer sends it
- **effects buses**: a track's chain can go into a bus instead of straight into the master — **→ MASTER** under
  the chain cycles through the buses, **+ BUS** makes a new one. A bus has its own 3 slots and its own level,
  so one reverb can process just the two of five tracks you route into it; buses are their own columns in the
  MIXER and boxes in the PATCHBAY (`BUS 1 · Reverb`), so an interface's input can be patched through one too
- **TRACK** view: the chain of the track selected in the sequencer (or of the bus you tapped in the mixer) and
  the plugin's parameters as knobs, colored by what they are (filter, envelope, LFO, oscillator, level, effect)
- **MIXER** view: a column per plugin track, one per bus, plus the master, each with a **stereo peak meter**
  (Carla's own data, taken after the chain's fader; an **audio track's shows its input** instead) whose peak
  line holds for a moment and then falls.
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
- **REC** (replaces the steps you play over) or **OVR** (overdub: leaves them and adds) records what you play
  into the pattern, quantized to 1/16, 1/8 or 1/4 — fingers on the pads and notes from a track's MIDI
  keyboard alike. REC only touches the steps you play: a pass with nothing played leaves the pattern as it
  was. What you record is
  **heard at once**: while you hold it, the pattern leaves that step to your fingers, and when you lift them
  it plays from there — a bar-long chord keeps ringing into the loop instead of waiting for the next pass
- **CLICK** and **COUNT** in the top bar: a tick on every beat (higher on the bar's first) while playing, and
  0/1/2 bars of them before the pattern starts, so you can come in on time
- with **XYFX** on the master, a **MASTER** row appears in the sequencer; selecting it turns this module
  into an **XY pad**: beat repeat left–right (straight on the right half, triplets or reverse on the left),
  low-pass down / high-pass up, the centre is "dry"

![Keyboard](docs/screenshots/keys.png)
![XY pad](docs/screenshots/xy-pad.png)

### CTRL-10 — performance faders

Pitch bend (springs back), mod wheel and 8 CC faders. Each fader has a **target**:

- **TRACK** — that track's instrument, on its channel; **SEL** = whichever track is selected
- **CHANNEL** — plain MIDI on a channel (outboard gear on the MIDI port)
- **PARAM** — a plugin parameter directly, no MIDI learn needed: **ASSIGN** at the top of HOST-4 puts its
  knobs into a choice mode, tap the knob you want, then tap the fader — fader and knob then move together
  (tap ASSIGN again to cancel, or CANCEL in the fader list)

Optional 14-bit (MSB/LSB pairs) and bipolar faders.

![Fader targets](docs/screenshots/faders-edit.png)

### Projects and settings

FILE lists projects, newest first (open, NEW, DUPLICATE, hold DELETE); switching restarts the app with the
other project. SETTINGS sets the project's key and scale (chord pads, piano roll), where plugins play, and the
audio graph's **buffer size** (SYSTEM or 64 … 2048 samples): the whole graph follows — plugins included — and
the machine's own size comes back when the app closes.

![Settings](docs/screenshots/settings.png)
![Audio settings: the output and the buffer size](docs/screenshots/settings-audio.png)

### PATCHBAY — audio routing

Every audio port of the machine, grouped by the device or plugin it belongs to: sources on the left,
destinations on the right, and a **cable** drawn for every link between the two. Tap a port and then one on
the other side to patch them, the same pair again to cut it — one source stays chosen, so a 4-channel input is
patched with a tap per channel. That is how a synth plugged into an interface's input is heard through that
interface's output, sent through a bus' reverb, or how a plugin's output is sent to another device. Cables are
drawn for the ports on screen; the links you make here are kept with this machine (config.json, not the
project) and re-made when a device comes back. The rack's own cables — an interface input into its audio
track, a chain into its bus and the master, everything into the output — are thin and solid, cables another
program made are dashed, and neither is yours to cut here.

![Patchbay: devices as boxes, links as cables](docs/screenshots/patchbay.png)

## Install

Download the AppImage for your machine from [Releases](https://github.com/marcin-koziol/touch-seq/releases),
then:

```sh
chmod +x Touch-Seq-0.6.1-x86_64.AppImage
./Touch-Seq-0.6.1-x86_64.AppImage
```

It contains Python, Qt (PySide6), Carla and Touch-Seq's own plugins (XYFX, Monitor, EQ and Click). The machine needs:

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
| top bar | tap **BPM** | the tempo: drag the number, ±1/5/10, TAP it out |
| sequencer | tap a track header twice | opens it in EDIT |
| SONG | tap a step | plays it from the next bar; its bars / ×repeats change on a tap, ✕ deletes on a hold |
| GRID / SONG | hold a row, then drag | moves a track (GRID) or a step (SONG) to another place in the list |
| top bar | **CLICK** / **COUNT** | the metronome, and 0/1/2 bars of count-in before the pattern |
| sequencer | drag a **pattern slot** onto another | copies the pattern there and switches to it (at the bar) |
| sequencer | drag along a row | paints steps on/off |
| piano roll | tap empty / tap a note | place / remove; drag a note sideways = length, up/down = pitch |
| piano roll | drag the keyboard on the left | scroll; OCT ▼▲ jumps an octave |
| plugin knobs | drag up/down | turn; to drive one from a CTRL fader, **ASSIGN** in HOST-4 then tap the knob and the fader |
| chord pads | 7 / 9 / SUS while holding a chord | the held chord changes right away |
| LEAD | toggle off and on | restarts voice leading from root position |
| XY pad | LATCH | keeps the effect on after the finger lifts |
| keyboard | lower on the key | louder (pads: higher = louder) |
| EDIT header | the **DIN socket** at the left | a track's MIDI keyboard and channel (green = it is there) |

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
| `buffer` | audio buffer size in samples for the whole graph while the app runs, `0` = the system's own |
| `click_lead` | the metronome stream's own delay in ms — only for the fallback without the click plugin |

The metronome is **Touch-Seq Click**, our own plugin (`click/`, CLAP/LV2, like the monitor and the EQ): the
master's audio goes through it and the engine plays it one note per beat, so the click is rendered inside the
same audio graph as the instruments — sample-locked to them, under Carla's care, and with nothing to drift.
Without the plugin installed the app falls back to its own stream (`pw-cat`), which is fine but a buffer
behind; `click_lead` shifts that one by hand if a machine needs it.

`buffer` is applied with `clock.force-quantum`, so it wins over a `min-quantum = max-quantum` pin in
`pipewire.conf.d` (such a pin is why a size may look stuck in the settings); the machine's own size is put
back when the app closes.

Projects save themselves (every couple of seconds while editing, and on exit); plugin states are saved next
to each project in `<name>.plugins/`, together with their parameter values (some plugins, the XYFX among them,
keep theirs out of the state) — a knob turned in HOST-4 is saved on that same couple-of-seconds clock.

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
xyfx/install.sh monitor/install.sh eq/install.sh click/install.sh   # our plugins into ~/.clap and ~/.lv2
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
