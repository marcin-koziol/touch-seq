"""The plugin host module (Carla): per-track chains and the master bus, the parameter knobs, the mixer and
the plugin browser, plus the `Slide` gesture they use."""
import json
import math
import os
import re
import threading
import time
import zlib
from pathlib import Path

import rtmidi

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPen

import plugins

from .model import Bus
from .constants import (BG, BORDER, BUILTIN_LABELS, CLICK_ACCENT, CLICK_BEAT, CLICK_LABELS, DIM, EQ_LABELS,
                        INTERNAL_LABELS, MONITOR_LABELS, PALETTE, PANEL, PARAM_LOG, SAMPLER_LABELS,
                        SAMPLER_NOTE_BASE, SLOTS, SPACE_LABELS, TEXT, WARN, XYFX_LABELS)
from .eq import EqEditor
from .gestures import Drag, Hold, ScrollOrTap, Slide
from .sampler import SamplerEditor, manifest_path as sampler_manifest_file, read_manifest
from .ui import Panel, param_color, param_family, param_section, split


def log_param(action, plugin, index, value, source):
    """One line per parameter write and plugin-state event when TOUCHSEQ_PARAM_LOG names a file: what wrote
    what, and why. `source` is "knob", "bar", "eq", "fader", "tempo", "xyfx", "restore", or "plugin" when the
    plugin changed a parameter itself (its UI, a preset)."""
    if not PARAM_LOG:
        return
    detail = f" #{index} = {value:.6g}" if index is not None else ""
    try:
        with open(PARAM_LOG, "a") as f:
            f.write(f"{time.monotonic():.3f} {action:4} {plugin.ref.name} [{plugin.ref.label}]"
                    f"{detail} ({source})\n")
    except OSError:
        pass


def param_group(name):
    """What parameters of this name belong together: the name's section, or for a bare name ("Attack",
    "Cutoff") its kind, so a plain ADSR is one group and a plain cutoff/resonance another."""
    return param_section(name) or param_family(name)


def _page_rows(page, cols):
    """Knob rows a page takes: a section's box is as wide as its knobs need, and boxes share a line while
    they fit, so a page of small sections is not one full-width bar each."""
    lines = []  # [columns used, rows]
    for sec in page:
        width, height = section_box(sec, cols)
        if lines and lines[-1][0] + width <= cols:
            lines[-1][0] += width
            lines[-1][1] = max(lines[-1][1], height)
        else:
            lines.append([width, height])
    return sum(rows for _, rows in lines)


def param_pages(params, cols, rows):
    """A plugin's knobs in pages of whole sections, each section a box of its own. The parameters of a
    section are brought together even when the plugin lists them apart ("Hat Open Tune" and "Hat Open
    Attack" are one HAT OPEN box), and a page takes as many whole boxes as fit, so Attack is never on one
    page with Decay/Sustain/Release on the next. Returns pages of sections (each a list of parameters)."""
    groups, order = {}, []
    for prm in params:  # sections in the order they first appear, each with all of its parameters
        key = param_group(prm.name)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(prm)
    groups = [groups[key] for key in order]
    pages, page = [], []
    for group in groups:
        width, need = section_box(group, cols)
        if need > rows:  # a section taller than a page has to split somewhere
            if page:
                pages.append(page)
                page = []
            step = (BARS_PER_ROW if is_step_section(group) else cols) * rows
            pages.extend([group[j:j + step]] for j in range(0, len(group), step))
            continue
        if page and _page_rows(page + [group], cols) > rows:
            pages.append(page)
            page = []
        page.append(group)
    if page or not pages:
        pages.append(page)
    return pages


BARS_PER_ROW = 16  # a step sequence (LFO Step 1..16) draws as one line of bars


def is_step_section(section):
    """A section of numbered parameters ("LFO Step 1".."LFO Step 16", "Env 1"..): a sequence, so its
    parameters are drawn as vertical bars instead of knobs."""
    return len(section) >= 4 and all(re.search(r"\d+\s*$", prm.name) for prm in section)


def section_box(section, cols):
    """(columns wide, rows tall) for a section: knobs pack into the columns, a step sequence takes a line."""
    if is_step_section(section):
        return cols, max(1, -(-len(section) // BARS_PER_ROW))
    return min(len(section), cols), max(1, -(-len(section) // cols))


def section_name(group):
    """A section's name for its box: what the parameters call themselves ("AMP", "FILTER ENV", "OSC 1"),
    else their kind, else GENERAL."""
    section = param_section(group[0].name)
    if section:
        return section.upper()
    family = param_family(group[0].name)
    return family.upper() if family else "GENERAL"


class HostPanel(Panel):
    """Plugin host (Carla). Every track has a chain (instrument -> 2 effects); all track chains go through
    the master's effects to the audio output. Views: TRACK (the chain of the track selected in the
    sequencer, the chosen plugin's parameters as knobs), MIXER (every track's level, and the master) and
    MASTER (the master chain, like TRACK). If Carla or JACK isn't available the panel says so and the rest
    of the rack works as before."""
    KNOB_COLS, KNOB_ROWS = 5, 3
    LETTERS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    click_plugin = None  # the metronome node (click/ in this repo); None: the app's own stream
    click_out = None  # the MIDI port that plays it
    click_note = None  # (accent) -> None, one beat of the metronome

    def __init__(self, project, router, state_dir, cache_path, audio_out="default", remember=None, name="Touch-Seq",
                 patch=None, buffer=0, presets=None):
        super().__init__()
        self.project, self.router, self.state_dir, self.audio_out, self.name = project, router, state_dir, audio_out, name
        # sampler presets belong to this machine, not to a project: deleting a track or a project keeps them
        self.presets_dir = Path(presets) if presets else Path(state_dir).parent / "sampler-presets"
        self.remember = remember  # (key, value) -> None: keep a machine setting (audio output, patchbay links)
        self.selected = lambda: project.tracks[0]  # the track whose chain TRACK shows (the rack: the sequencer's)
        self.select_track = lambda tr: None  # the rack: select a track in the sequencer
        self.choose_port = lambda tr: None  # the rack: choose the MIDI output of an external track
        self.view = "track"  # "track", "mixer" or "master"
        self.focus = None  # a bus whose chain the TRACK view shows instead of the selected track's
        self.loaded = {}  # (Strip, slot) -> plugins.Plugin
        self.failed = set()  # (Strip, slot) whose saved plugin couldn't be loaded
        self.sel = None  # (Strip, slot) whose parameters are shown
        self.page = 0
        self.mixer_page = 0
        self.browser = None  # (Strip, slot) while choosing a plugin for it
        self.assigning = None  # (Strip, slot, Param) while choosing a fader for a held knob
        self.browser_fmt = None  # None = all formats
        self.browser_scroll = 0.0
        self.message = ""
        self.wiring = plugins.Wiring()
        self.patch = [tuple(p) for p in (patch or [])]  # patchbay links: (out, in), kept with the machine
        self.wiring.replace("patchbay", self.patch)  # from the last time; heal keeps them alive
        self.buffer = buffer  # the audio graph's buffer size in samples this machine was left on (0: system)
        self.states_dirty = False  # a parameter changed since the plugins' states were last saved
        self.saving_states = False  # inside save_states: a plugin echoing a change must not ask for another save
        self.meters = {}  # Strip -> {"l", "r", "peak", "at"}: what its chain's output is doing
        self.monitor = None  # the monitor plugin's ref, looked up once (False: not installed)
        self.click_ref_cache = None  # the click plugin's ref, the same way
        self.click_plugin = None  # the metronome node between the master and the output
        self.click_out = None  # the MIDI port that plays it
        self.click_note = None  # (accent) -> None: one beat of the metronome, None without the plugin
        self._builtin = None  # our own plugins, found on disk once (a stale catalog must not hide them)
        self.eq = EqEditor(self)  # the EQ's own editor, drawn in the parameter area
        self.sampler = SamplerEditor(self, background=True)  # the sampler's editor, drawn in the parameter area
        self._sampler_cache = {}  # manifest path -> (mtime, pads), so PLAY-12 doesn't read a file every frame
        self._preview_tokens = {}  # pad -> last audition, so an old note-off can't stop a newer retrigger
        os.environ["TOUCHSEQ_SAMPLER_DIR"] = str(self.state_dir)  # where the sampler finds its pad maps
        if self.buffer:
            plugins.set_quantum(self.buffer)  # before the engine starts, so the plugins see that size
        free = plugins.free_engine_name(name)  # a name already in the graph would rename our clients, and
        if free != name:                      # every link built from the old name would then fail
            self.message = f"another Touch-Seq is running: this one is “{free}”"
        self.name = free
        try:
            self.host = plugins.Host(self.name)
        except (RuntimeError, OSError, ImportError) as e:
            self.host, self.message = None, f"plugin host unavailable: {e}"
            return
        self.host.on_parameter_changed = self.plugin_changed
        self.catalog = plugins.Catalog(cache_path)
        self.sink = self.current_sink()
        self.sink_changed = False
        self.click_plugin = self.load_click()  # before the first rewire: the master goes through it
        for strip in self.strips():
            for k, ref in enumerate(strip.slots):
                if ref:
                    self.load(strip, k, ref, restore=True)
        self.rewire()
        self.ensure_monitors()  # audio tracks without effects: a node for their input
        threading.Thread(target=self.watch_output, daemon=True).start()

    # --- chains ---------------------------------------------------------------
    def strips(self):
        """Chains: every plugin track, every bus and the master. An external track has no chain (its notes go
        out to hardware), so it never shows a slot that could take a plugin."""
        return ([t.strip for t in self.project.tracks if not t.external]
                + [b.strip for b in self.project.buses] + [self.project.master])

    def track_of(self, strip):
        return next((t for t in self.project.tracks if t.strip is strip), None)  # None: a bus or the master

    def bus_of(self, strip):
        return next((b for b in self.project.buses if b.strip is strip), None)  # None: a track or the master

    def owner_name(self, strip):
        """How the patchbay, the state files and the clients name a chain: T3, BUS 2 or MASTER."""
        tr, bus = self.track_of(strip), self.bus_of(strip)
        return f"T{tr.number}" if tr else f"BUS {bus.number}" if bus else "MASTER"

    def instrument_slot(self, strip, k):
        """Slot 0 holds the instrument of a track's chain; a bus, the master and an audio track (which takes an
        input port, not MIDI) only take effects."""
        tr = self.track_of(strip)
        return k == 0 and tr is not None and not tr.audio

    def chain(self, strip):
        return [self.loaded[(strip, k)] for k in range(SLOTS) if (strip, k) in self.loaded]

    def current_sink(self):
        return plugins.default_sink() if self.audio_out == "default" else self.audio_out

    def set_output(self, name):
        """'default' (follow the system's default output) or a sink's node name."""
        self.audio_out, self.sink = name, None
        self.sink = self.current_sink()
        self.rewire()
        if self.remember:
            self.remember("audio_out", name)

    def set_param(self, plugin, index, value, source="app"):
        """A parameter turned in the app: the plugin's state is then worth saving with the project's autosave,
        not just on the 30 s clock. `source` says what wrote it, for TOUCHSEQ_PARAM_LOG."""
        log_param("set", plugin, index, value, source)
        self.host.set_value(plugin, index, value)
        self.states_dirty = True
        self.invalidate()  # a knob whose value changed draws the new one: no free-running animation needed

    def plugin_changed(self, plugin, index, value):
        """A plugin changed one of its own parameters (its UI, a preset): its state is worth saving now, so a
        project switch before the 30 s clock does not lose the preset."""
        if self.saving_states:
            return
        log_param("set", plugin, index, value, "plugin")
        self.states_dirty = True
        self.invalidate()  # its editor or a preset moved a knob

    # --- audio graph ----------------------------------------------------------
    def chain_labels(self):
        """The patchbay names our plugins by what they are in the rack: a track's chain and the master, so an
        interface's input can be patched into MASTER (through its effects) as if it were a device."""
        labels = {}
        for strip in self.strips():
            owner = self.owner_name(strip)
            chain = [(k, self.loaded[(strip, k)]) for k in range(SLOTS) if (strip, k) in self.loaded]
            for k, plugin in chain:
                name = plugin.ref.name
                if sum(1 for _, other in chain if other.ref.name == name) > 1:  # two of the same in one chain
                    name = f"{k + 1} {name}"
                labels[plugin.client] = f"{owner} · {name}"
        return labels

    def internal_links(self):
        """The cables the rack makes itself (tracks into their bus or the master, chains into the output). The
        patchbay shows them as the rack's own and doesn't touch them."""
        with self.wiring.lock:
            return {link for owner, links in self.wiring.links.items() if owner != "patchbay" for link in links}

    def set_buffer(self, size):
        """The audio graph's buffer size in samples, 0 for the system's default. System-wide while set."""
        self.buffer = size
        plugins.set_quantum(size)
        if self.remember:
            self.remember("buffer", size)

    # --- patchbay -------------------------------------------------------------
    def patch_links(self):
        """The links made in the patchbay; the dialog keeps them and asks for a rewire on every change."""
        return list(self.patch)

    def set_patch(self, links):
        self.patch = [tuple(p) for p in links]
        self.wiring.replace("patchbay", self.patch)  # heals them too, like the app's own wiring
        if self.remember:
            self.remember("patch", self.patch)

    def watch_output(self):
        """Background: follow the default output (it changes e.g. when headphones are plugged in or out)
        and put back links that disappeared with a device. Rewiring itself happens in tick()."""
        while self.host:
            time.sleep(2)
            sink = self.current_sink()
            if sink and sink != self.sink:
                self.sink, self.sink_changed = sink, True
            else:
                self.wiring.heal()

    def state_file(self, strip, k, ref, prefix=None):
        """Per chain, slot and plugin, so a state never gets loaded into a different plugin."""
        tr, bus = self.track_of(strip), self.bus_of(strip)
        tag = zlib.crc32(f"{ref.fmt}|{ref.path}|{ref.label}".encode())
        prefix = prefix or (f"t{tr.number}" if tr else f"b{bus.number}" if bus else "master")
        return self.state_dir / f"{prefix}-{k + 1}-{tag:08x}.carxs"

    def sampler_slot(self, strip, k):
        """A stable number per chain/slot: the sampler's pad map is sampler-<slot>.txt."""
        tr = self.track_of(strip)
        if tr:
            return (tr.number - 1) * SLOTS + k
        bus = self.bus_of(strip)
        if bus:
            return 256 + (bus.number - 1) * SLOTS + k
        return 512 + k

    def sampler_manifest_path(self, strip, k):
        return sampler_manifest_file(self.state_dir, self.sampler_slot(strip, k))

    def preview_start(self, plugin, index):
        """Note-on for a sampler pad; returns its note-off (token-safe, so an old stop can't cut a newer tap),
        or None when the track has no port. The editor's pads hold the note while the finger is down."""
        for (strip, _k), loaded in self.loaded.items():
            if loaded is not plugin:
                continue
            tr = self.track_of(strip)
            if tr is None:
                return None
            self.ensure_port(tr)
            port = self.router.taps.get(tr)
            if port is None:
                return None
            note = SAMPLER_NOTE_BASE + int(index)
            status = 0x90 | (max(1, min(16, tr.channel)) - 1)
            token = self._preview_tokens.get(int(index), 0) + 1
            self._preview_tokens[int(index)] = token

            def stop():
                if self._preview_tokens.get(int(index)) != token:
                    return  # a newer tap restarted this pad: its own note-off will stop it
                try:
                    port.send_message([0x80 | (status & 0x0F), note, 0])
                except Exception:
                    pass

            try:
                port.send_message([status, note, 100])
            except Exception:
                return None
            return stop
        return None

    def preview_pad(self, plugin, index):
        """Audition one sampler pad for a moment through its track's MIDI port."""
        stop = self.preview_start(plugin, index)
        if stop:
            QTimer.singleShot(1200, stop)  # a looped pad stops; a one-shot ignores the note-off

    def stop_pads(self, plugin):
        """Stop every voice the sampler is playing (CC 123, which the plugin turns into allNotesOff)."""
        for (strip, _k), loaded in self.loaded.items():
            if loaded is not plugin:
                continue
            tr = self.track_of(strip)
            if tr is None:
                return
            port = self.router.taps.get(tr)
            if port is None:
                return
            status = 0xB0 | (max(1, min(16, tr.channel)) - 1)
            try:
                port.send_message([status, 123, 0])
            except Exception:
                pass
            return

            return

    def sampler_pads(self, tr):
        """The sampler's 16 pad maps for PLAY-12, or None when the track has no sampler."""
        plugin = self.loaded.get((tr.strip, 0))
        if not plugin or plugin.ref.label not in SAMPLER_LABELS:
            return None
        path = self.sampler_manifest_path(tr.strip, 0)
        try:
            modified = path.stat().st_mtime
        except OSError:
            return None
        cached = self._sampler_cache.get(path)
        if cached and cached[0] == modified:
            return cached[1]
        _, pads, _ = read_manifest(path)
        self._sampler_cache[path] = (modified, pads)
        return pads

    def load(self, strip, k, ref, restore=False):
        tr, bus = self.track_of(strip), self.bus_of(strip)
        side = f"T{tr.number}" if tr else f"B{bus.number}" if bus else "M"
        client = f"{side}.{k + 1} {ref.name}".replace(":", " ")[:48]
        try:
            if tr and k == 0:
                self.ensure_port(tr)
            plugin = self.host.add(ref, client)
        except (RuntimeError, rtmidi.RtMidiError) as e:
            self.failed.add((strip, k))
            self.message = str(e)
            return
        self.failed.discard((strip, k))
        self.loaded[(strip, k)] = plugin
        state = self.state_file(strip, k, ref)
        if restore and not state.exists() and strip.legacy:
            state = self.state_file(strip, k, ref, strip.legacy)
        if restore and state.exists():
            log_param("load", plugin, None, None, "project")
            self.host.load_state(plugin, state)
            self.host.refresh_params(plugin)
            self.restore_params(plugin, state)
        if ref.label in SAMPLER_LABELS:  # tell it which pad map in the project's sampler folder to read
            parameter = next((q for q in plugin.params if q.name == "Slot"), None)
            if parameter:
                self.set_param(plugin, parameter.index, float(self.sampler_slot(strip, k)), source="sampler")
            bpm = next((q for q in plugin.params if q.name.lower() == "bpm"), None)
            if bpm:  # a tempo-synced LFO follows the project
                self.set_param(plugin, bpm.index, float(self.project.bpm), source="tempo")

    def ensure_port(self, tr):
        """The JACK MIDI port feeding this track's instrument (one JACK client per track)."""
        if tr not in self.router.taps:
            port = rtmidi.MidiOut(rtmidi.API_UNIX_JACK, name=f"{self.name} T{tr.number}")
            port.open_virtual_port("midi")
            self.router.taps[tr] = port

    def unload(self, strip, k):
        plugin = self.loaded.pop((strip, k), None)
        if plugin is self.eq.plugin:
            self.eq.forget()
        if plugin:
            self.host.show_ui(plugin, False)
            self.host.remove(plugin)
        self.failed.discard((strip, k))

    def set_slot(self, strip, k, ref):
        old = strip.slots[k]
        self.unload(strip, k)
        if old:
            self.state_file(strip, k, old).unlink(missing_ok=True)
        strip.slots[k] = ref
        if ref:
            self.load(strip, k, ref)
        self.rewire()
        self.sel, self.page = ((strip, k) if ref else None), 0

    def forget_deleted_tracks(self):
        """Tracks deleted in the sequencer take their plugins, MIDI port and saved states with them."""
        live = set(self.strips())
        gone = {strip for strip, _ in self.loaded if strip not in live}
        for strip, k in [key for key in self.loaded if key[0] in gone]:
            self.unload(strip, k)
        for tr in [tr for tr in self.router.taps if tr not in self.project.tracks]:
            self.router.taps.pop(tr).close_port()
            for pattern in (f"t{tr.number}-*.carxs", f"t{tr.number}-*.params.json"):
                for f in self.state_dir.glob(pattern):
                    f.unlink()
        if gone:
            for strip in gone:
                self.wiring.replace(strip, [])
            self.rewire()

    def rewire(self):
        """Link every chain: a track's MIDI port to its instrument, plugin to plugin, a track into its bus (or
        the master), a bus and the master into what comes next, the master to the output; then set the levels."""
        outs = lambda pl: [f"{pl.client}:output_{n}" for n in range(1, self.host.ports(pl)[1] + 1)]
        ins = lambda pl: [f"{pl.client}:input_{n}" for n in range(1, self.host.ports(pl)[0] + 1)]
        sink = [f"{self.sink}:playback_FL", f"{self.sink}:playback_FR"] if self.sink else []
        click = self.click_plugin
        click_in, click_out = (ins(click), outs(click)) if click else ([], [])
        to_output = click_in or sink  # everything that reaches the output goes through the click when it is there
        master = self.chain(self.project.master)
        into_master = ins(master[0]) if master else to_output

        def bus_into(number):
            """Where a track routed to bus `number` goes; an empty bus is transparent."""
            bus = next((b for b in self.project.buses if b.number == number), None)
            chain = self.chain(bus.strip) if bus else []
            return ins(chain[0]) if chain else into_master

        def audio_track(tr, chain):
            """An audio track's input port, through its effects when it has any, and on into where it goes.
            Panning a mono input is picking one side or both."""
            if tr.mute:
                return []
            into = ins(chain[0]) if chain else (bus_into(tr.bus) if tr.bus else into_master)
            return self.input_links(tr.source, into, tr.pan)

        for strip in self.strips():
            chain, tr, bus = self.chain(strip), self.track_of(strip), self.bus_of(strip)
            links = []
            if tr and tr.audio:
                links += audio_track(tr, chain)
            elif tr and chain and chain[0].ref.midi_ins:
                links.append((f"{self.name} T{tr.number}:midi", f"{chain[0].client}:events-in"))
            for a, b in zip(chain, chain[1:]):
                links += self.pair(outs(a), ins(b))
            if chain:
                if strip is self.project.master:
                    links += self.pair(outs(chain[-1]), to_output)
                elif bus:
                    links += self.pair(outs(chain[-1]), into_master)
                else:
                    links += self.pair(outs(chain[-1]), bus_into(tr.bus) if tr and tr.bus else into_master)
            self.wiring.replace(strip, links)
        if click:  # the metronome's own node: the audio out of it, and the notes that play it
            self.wiring.replace("click", self.pair(click_out, sink) + [(f"{self.name} Click:midi", f"{click.client}:events-in")])
        self.apply_levels()

    def apply_levels(self):
        """A chain's level sits on its last plugin. Without master effects the master's level joins whatever
        reaches the output instead: the buses, and the tracks that don't go through one."""
        m = self.project.master
        master_chain = self.chain(m)
        master_level = 0.0 if m.mute else m.volume
        for strip in self.strips():
            chain = self.chain(strip)
            for pl in chain:
                self.host.set_volume(pl, 1.0)
            if not chain:
                continue
            if strip is m:
                self.host.set_volume(chain[-1], master_level)
                continue
            level = 0.0 if strip.mute else strip.volume
            if not master_chain:
                tr, bus = self.track_of(strip), self.bus_of(strip)
                if bus is not None or not (tr and tr.bus):  # it reaches the output itself
                    level *= master_level
            self.host.set_volume(chain[-1], level)

    @staticmethod
    def input_links(source, ins, pan="center"):
        """One input port into one or both sides of a stereo destination: a mono channel panned."""
        if not source or not ins:
            return []
        if pan == "right":
            return [(source, ins[min(1, len(ins) - 1)])]
        if pan == "left":
            return [(source, ins[0])]
        return [(source, port) for port in ins[:2]]

    @staticmethod
    def pair(outs, ins):
        """Stereo links: first two outputs to first two inputs; a mono side feeds / takes both."""
        outs, ins = outs[:2], ins[:2]
        if not outs or not ins:
            return []
        return [(outs[min(j, len(outs) - 1)], ins[min(j, len(ins) - 1)]) for j in range(max(len(outs), len(ins)))]

    def save_states(self):
        self.states_dirty = False
        if not self.host or not self.loaded:
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.saving_states = True
        try:
            for (strip, k), plugin in self.loaded.items():
                state = self.state_file(strip, k, plugin.ref)
                self.host.prepare_for_save(plugin)
                self.host.save_state(plugin, state)
                log_param("save", plugin, None, None, "state")
                # Our own plugins keep their parameters outside their state blob (XYFX, Space, EQ, Click: none
                # of them declares WANT_STATE), so the knob positions would come back at their defaults: keep
                # them beside the state. A third-party plugin's own state already carries its parameters, and a
                # sidecar there would override a preset loaded in the plugin's own UI on the next load.
                if plugin.ref.label in BUILTIN_LABELS:
                    params = {str(prm.index): self.host.value(plugin, prm.index) for prm in plugin.params}
                    state.with_suffix(".params.json").write_text(json.dumps(params))
        finally:
            self.saving_states = False

    def restore_params(self, plugin, state):
        """The parameter values saved next to a plugin's state, for what the state itself doesn't carry.
        Only our own plugins need it: a third-party plugin's state already carries its parameters, and
        applying a sidecar over it would undo a preset loaded in the plugin's own UI."""
        if plugin.ref.label not in BUILTIN_LABELS:
            return
        try:
            values = json.loads(state.with_suffix(".params.json").read_text())
        except (OSError, ValueError):
            return
        for prm in plugin.params:
            value = values.get(str(prm.index))
            if isinstance(value, (int, float)):
                log_param("set", plugin, prm.index, value, "restore")
                self.host.set_value(plugin, prm.index, value)

    def close(self):
        if self.click_out:
            self.click_out.close_port()
            self.click_out, self.click_note = None, None
        if self.host:
            self.save_states()
            self.host.close()
            self.host, self.message = None, "plugin host closed"  # UI timers may still tick
        if self.buffer:
            plugins.set_quantum(0)  # the machine goes back to its own size; ours comes back on the next start

    def tick(self):
        super().tick()
        sampler = getattr(self, "sampler", None)
        if sampler:
            sampler.poll()  # a background waveform read lands on the UI thread here
        self.router.refresh(self.project.tracks)  # external tracks: look for their MIDI outputs again
        if self.host:
            self.host.idle()
            self.sample_meters()
            self.eq.tick()  # the EQ's analyser, when one is open
            self.forget_deleted_tracks()
            if self.xyfx() and self.xy_values().get("bpm") != self.project.bpm:  # repeats follow the tempo
                self.xy(bpm=self.project.bpm)
            self.follow_tempo()  # and so does a synced delay
            if self.sink_changed:  # Carla calls belong in this (the UI) thread
                self.sink_changed = False
                self.rewire()

    def dsp(self):
        """The plugin host's DSP load (percent) and xruns, or None when it is not running."""
        runtime = getattr(self.host, "runtime", None)
        return runtime() if runtime else None

    def follow_tempo(self):
        """Plugins that keep their own tempo (Space's synced delay): give them the project's, when it changes."""
        for plugin in list(self.loaded.values()):
            if plugin.ref.label not in SPACE_LABELS and plugin.ref.label not in SAMPLER_LABELS:
                continue
            prm = next((q for q in plugin.params if q.name.lower() == "bpm"), None)
            if prm and round(self.host.value(plugin, prm.index)) != self.project.bpm:
                self.set_param(plugin, prm.index, float(self.project.bpm), source="tempo")

    def animating(self):
        """Only what really moves: a held gesture, the sampler's spinner while a waveform is read. A loaded
        plugin used to count as "animating", which repainted the whole rack 25 times a second for nothing -
        a plugin's parameters only change when we set them, and that invalidates the panel itself."""
        sampler = getattr(self, "sampler", None)
        return super().animating() or bool(sampler and sampler.loading)

    # --- fader links (Controls target "param") ------------------------------------
    def link_plugin(self, link):
        owner, slot = link[0], link[1]
        strip = self.project.master if owner == 0 else next(
            (t.strip for t in self.project.tracks if t.number == owner), None)
        return self.loaded.get((strip, slot)) if strip is not None and self.host else None

    def link_param(self, link):
        plugin = self.link_plugin(link)
        prm = next((q for q in plugin.params if q.index == link[2]), None) if plugin else None
        return plugin, prm

    def link_value(self, link):
        plugin, prm = self.link_param(link)
        return (self.host.value(plugin, prm.index) - prm.lo) / (prm.hi - prm.lo) if prm else None

    def link_text(self, link):
        plugin, prm = self.link_param(link)
        return self.short(self.host.text(plugin, prm.index)) if prm else None

    def set_link(self, link, norm):
        plugin, prm = self.link_param(link)
        if prm:
            v = prm.lo + norm * (prm.hi - prm.lo)
            self.set_param(plugin, prm.index, round(v) if prm.integer or prm.boolean else v, source="fader")

    def link_to_fader(self, i):
        """Make CC fader i drive the parameter whose knob was held (self.assigning)."""
        strip, slot, prm = self.assigning
        self.assigning = None
        if self.bus_of(strip):
            return  # a fader link can point at a track or the master, not at a bus
        tr = self.track_of(strip)
        self.project.controls[i].target, self.project.controls[i].param = "param", [
            tr.number if tr else 0, slot, prm.index, prm.name]

    def xyfx(self):
        """Touch-Seq XYFX on the master chain (its UI is the XY pad in PLAY-12), or None."""
        m = self.project.master
        return next((pl for k in range(SLOTS) if (pl := self.loaded.get((m, k))) and pl.ref.label in XYFX_LABELS),
                    None) if self.host else None

    def xy(self, **values):
        """Set XYFX parameters by name: engage, x, y, bpm (0..1 except bpm)."""
        plugin = self.xyfx()
        if plugin:
            for prm in plugin.params:
                if prm.name.lower() in values:
                    self.set_param(plugin, prm.index, values[prm.name.lower()], source="xyfx")

    def xy_values(self):
        plugin = self.xyfx()
        return {prm.name.lower(): self.host.value(plugin, prm.index) for prm in plugin.params} if plugin else {}

    # --- buses ----------------------------------------------------------------
    def add_bus(self, route=None):
        """A new effects bus: tracks routed into it are summed, its chain processes them, then it goes into the
        master. `route` is a track to send through it right away."""
        bus = Bus(max((b.number for b in self.project.buses), default=0) + 1)
        self.project.buses.append(bus)
        if route is not None:
            route.bus = bus.number
        self.rewire()
        return bus

    def remove_bus(self, bus):
        """Its tracks go back to the master, its plugins and saved states go with it."""
        for tr in self.project.tracks:
            if tr.bus == bus.number:
                tr.bus = 0
        for k in range(SLOTS):
            self.unload(bus.strip, k)
        for pattern in (f"b{bus.number}-*.carxs", f"b{bus.number}-*.params.json"):
            for f in self.state_dir.glob(pattern):
                f.unlink()
        self.wiring.replace(bus.strip, [])
        self.project.buses.remove(bus)
        if self.focus is bus.strip:
            self.focus, self.sel = None, None  # the view goes back to the selected track
        self.rewire()

    def set_bus(self, tr, number):
        """Route a track through a bus; 0 sends it straight into the master."""
        tr.bus = number
        self.rewire()

    def cycle_bus(self, tr):
        """The next destination of a track: master, then each bus."""
        options = [0] + [b.number for b in self.project.buses]
        self.set_bus(tr, options[(options.index(tr.bus) + 1) % len(options)] if tr.bus in options else 0)

    def set_mix(self, strip, volume=None, mute=None):
        if volume is not None:
            strip.volume = volume
        if mute is not None:
            strip.mute = mute
        self.apply_levels()
        tr = self.track_of(strip)
        if mute is not None and tr is not None and tr.audio:
            self.rewire()  # an audio track's mute is its input cable, not a level

    # --- drawing ------------------------------------------------------------
    def paint_partial(self, p, screen, region):
        """A small repaint of this panel (the sampler playhead): draw just the waveform, never the chain or
        the params. The panel's hits stay as the last full frame left them; the draw's own are thrown away."""
        if (self.view != "track" or self.browser or isinstance(self.assigning, tuple)
                or self.sel not in self.loaded):
            return  # a full frame is needed (view/dialog changed); this strip is not the sampler's any more
        p.save()
        p.translate(screen.topLeft())
        self.w, self.h = screen.width(), screen.height()
        self.s = min(self.w / 1024, self.h / 600)
        wave = getattr(self.sampler, "wave_rect", None)
        if wave is not None:
            p.fillRect(wave, QColor(BG))
            hits = self.hits
            self.hits = []
            x0 = screen.left()
            bounds = region.boundingRect()
            self.sampler.partial_x = [(bounds.left() - x0 - 3, bounds.right() - x0 + 3)]
            try:
                self.sampler.draw_wave(p, wave)
            finally:
                self.sampler.partial_x = None
                self.hits = hits
        p.restore()

    def draw(self, p):
        s, g = self.s, 8 * self.s
        p.fillRect(self.rect(), QColor(BG))
        if not self.host:
            self.text(p, self.rect().adjusted(20 * s, 0, -20 * s, 0), self.message, WARN, 15)
            return
        if self.browser:
            self.draw_browser(p)
            return
        if isinstance(self.assigning, tuple):  # a knob was chosen with ASSIGN: pick the fader
            self.draw_assign(p)
            return
        header = QRectF(g, g, self.w - 2 * g, 44 * s)
        *views, status = split(header, [100 * s, 100 * s, 100 * s, 0], 6 * s)
        for rect, view in zip(views, ("track", "mixer", "master")):
            self.button(p, rect, view.upper(), lambda view=view: self.show(view), on=self.view == view)
        text = self.message or (f"{len(self.catalog.plugins)} plugins" if self.catalog.ready
                                else f"scanning plugins… {self.catalog.progress}")
        self.text(p, status.adjusted(10 * s, 0, 0, 0), text, WARN if self.message else DIM, 13,
                  Qt.AlignRight | Qt.AlignVCenter, elide=True)
        body = QRectF(g, header.bottom() + 10 * s, self.w - 2 * g, self.h - header.bottom() - 10 * s - g)
        if self.view == "mixer":
            self.draw_mixer(p, body)
            return
        if self.view == "master":
            strip = self.project.master
        elif self.focus is not None:
            strip = self.focus
        else:
            tr = self.selected()
            if tr.external:  # no chain: the notes go out to hardware instead
                self.draw_external(p, body, tr)
                return
            strip = tr.strip
        # In fullscreen the chain keeps the small layout's width (and its own scale), so the editor gets the
        # room instead of the plugin strip growing with the whole window.
        zoomed = getattr(self, "zoomed", False)
        chain_s = s * 0.5 if zoomed else s
        chain_w = body.width() * (0.14 if zoomed else 0.28)
        left, right = split(body, [chain_w, 0], 12 * chain_s)  # narrow chain, room for the editor
        saved_s = self.s
        self.s = chain_s
        self.draw_chain(p, left, strip)
        self.s = saved_s
        self.draw_params(p, right, strip)

    def draw_external(self, p, r, tr):
        """An external track in the TRACK view: it has no chain, its notes leave through a MIDI output, so
        the panel shows that output (and where to change it) instead of slots."""
        s = self.s
        color = PALETTE[(tr.number - 1) % len(PALETTE)]
        title = QRectF(r.left(), r.top(), r.width(), 34 * s)
        self.box(p, QRectF(title.left(), title.top() + 6 * s, 5 * s, title.height() - 12 * s), color, None, radius=2)
        self.text(p, title.adjusted(14 * s, 0, 0, 0), f"T{tr.number}  EXTERNAL MIDI  CH{tr.channel}", color, 16,
                  Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
        card = QRectF(r.left(), title.bottom() + 8 * s, r.width(), r.bottom() - title.bottom() - 8 * s)
        self.box(p, card, "#11141b", BORDER, radius=10)
        inner = card.adjusted(18 * s, 16 * s, -18 * s, -16 * s)
        live = self.router.is_open(tr.port)
        self.text(p, QRectF(inner.left(), inner.top(), inner.width(), 20 * s), "MIDI OUT", DIM, 12,
                  Qt.AlignLeft | Qt.AlignTop, bold=True)
        self.text(p, QRectF(inner.left(), inner.top() + 20 * s, inner.width(), 34 * s),
                  tr.port or "no output chosen", TEXT if tr.port else WARN, 20,
                  Qt.AlignLeft | Qt.AlignTop, bold=True, elide=True)
        state = ("connected" if live else "waiting for the device…" if tr.port else "silent until an output is chosen")
        self.text(p, QRectF(inner.left(), inner.top() + 60 * s, inner.width(), 22 * s), state,
                  "#4ade80" if live else WARN, 13, Qt.AlignLeft | Qt.AlignTop)
        self.text(p, QRectF(inner.left(), inner.top() + 88 * s, inner.width(), 40 * s),
                  "No plugins on this track: its notes and controllers go straight out to the port above.",
                  DIM, 13, Qt.AlignLeft | Qt.AlignTop, elide=True)
        btn = QRectF(inner.left(), card.bottom() - 18 * s - 52 * s, 200 * s, 52 * s)
        self.button(p, btn, "MIDI OUT", lambda: self.choose_port(tr), sub="choose the port")

    def show(self, view, sel=None):
        self.view, self.sel, self.page, self.focus = view, sel, 0, None

    def draw_chain(self, p, r, strip):
        """Title, where the chain goes, the slots top to bottom, and its level fader + mute beside them."""
        s, tr, bus = self.s, self.track_of(strip), self.bus_of(strip)
        color = PALETTE[(tr.number - 1) % len(PALETTE)] if tr else "#38d6ff" if bus else "#facc15"
        title = QRectF(r.left(), r.top(), r.width(), 34 * s)
        self.box(p, QRectF(title.left(), title.top() + 6 * s, 5 * s, title.height() - 12 * s), color, None, radius=2)
        name = (f"T{tr.number}  {tr.kind}  CH{tr.channel}" if tr else
                f"BUS {bus.number}" if bus else "MASTER  ·  all tracks → output")
        self.text(p, title.adjusted(14 * s, 0, -92 * s if bus else 0, 0), name, color, 16,
                  Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
        if bus:  # a bus is ours to remove (its tracks go back to the master, its effects go with it)
            delete = QRectF(title.right() - 84 * s, title.top(), 84 * s, title.height() - 4 * s)
            self.delete_button(p, delete, lambda bus=bus: self.remove_bus(bus), key=bus)
        top = title.bottom() + 8 * s
        if tr:  # a track picks where its chain goes: the master or a bus
            top = self.draw_destination(p, QRectF(r.left(), top, r.width(), 38 * s), tr)
        slots, fader = split(QRectF(r.left(), top, r.width(), r.bottom() - top), [0, 56 * s], 10 * s)
        sh = (slots.height() - 2 * 8 * s) / SLOTS
        for k in range(SLOTS):
            self.draw_slot(p, QRectF(slots.left(), slots.top() + k * (sh + 8 * s), slots.width(), sh), strip, k)
        self.draw_fader(p, fader, strip)

    def draw_destination(self, p, r, tr):
        """A track's chain goes into the master, or through a bus (whose effects then process just the tracks
        routed into it). Returns where the rest of the column starts."""
        s, buses = self.s, self.project.buses
        to, plus = split(r, [0, 96 * s], 8 * s)
        label = "MASTER" if not tr.bus else f"BUS {tr.bus}"
        self.button(p, to, f"→ {label}", lambda: self.cycle_bus(tr), enabled=bool(buses),
                    sub="tap: next bus" if buses else "no bus yet")
        self.button(p, plus, "+ BUS", lambda: self.add_bus(tr), sub="effects")
        return r.bottom() + 8 * s

    # --- the monitor node -----------------------------------------------------
    def monitor_ref(self):
        """The transparent gain/pan plugin (monitor/ in this repo) an audio track needs to have a node at all:
        without one Carla has nothing to meter and no fader to set. None when it is not installed."""
        if self.monitor is None:
            catalog = getattr(self, "catalog", None)
            if catalog is not None and catalog.ready:
                self.monitor = next((r for r in catalog.plugins if r.label in MONITOR_LABELS), None)
            if not self.monitor:  # not in the catalog (installed after the scan): look for the file itself
                self.monitor = plugins.find_monitor()
            self.monitor = self.monitor or False
        return self.monitor or None

    # --- the click node ---------------------------------------------------------
    def click_ref(self):
        """The metronome plugin (click/ in this repo): in the catalog when it is there, else found on disk."""
        if self.click_ref_cache is None:
            catalog = getattr(self, "catalog", None)
            if catalog is not None and catalog.ready:
                self.click_ref_cache = next((r for r in catalog.plugins if r.label in CLICK_LABELS), None)
            if not self.click_ref_cache:
                self.click_ref_cache = plugins.find_click()
            self.click_ref_cache = self.click_ref_cache or False
        return self.click_ref_cache or None

    def load_click(self):
        """Put the metronome in the graph: the master's audio goes through it and the engine plays it one note
        per beat, so the click is rendered with the instruments instead of through a stream of its own."""
        ref = self.click_ref()
        if not ref or not self.host:
            return None
        try:
            plugin = self.host.add(ref, "Click")
            self.click_out = rtmidi.MidiOut(rtmidi.API_UNIX_JACK, name=f"{self.name} Click")
            self.click_out.open_virtual_port("midi")
        except (RuntimeError, rtmidi.RtMidiError) as e:
            self.message = str(e)
            return None
        self.click_note = lambda accent: self.click_out.send_message(
            [0x90, CLICK_ACCENT if accent else CLICK_BEAT, 127 if accent else 100])
        return plugin

    def ensure_monitor(self, tr):
        """Put the monitor in an audio track that has no plugins (an empty one is a bare patch: no meter, no
        fader). Once per track: removing it during a session keeps it removed until the next load."""
        if not tr.audio or any(tr.strip.slots):
            return
        ref = self.monitor_ref()
        if ref:
            self.set_slot(tr.strip, 0, ref)

    def ensure_monitors(self):
        for tr in self.project.tracks:
            self.ensure_monitor(tr)

    # --- meters ---------------------------------------------------------------
    @staticmethod
    def db_fraction(value):
        """A peak as a 0..1 bar height: -60 dB at the bottom, 0 dB at the top."""
        if value <= 0.0:
            return 0.0
        db = 20 * math.log10(value)
        return max(0.0, min(1.0, (db + 60.0) / 60.0))

    def levels(self, strip):
        """(left, right) heights, 0..1 on the -60..0 dB scale, for the small meters in the sequencer."""
        meter = self.meters.get(strip)
        if not meter:
            return 0.0, 0.0
        return self.db_fraction(meter["l"]), self.db_fraction(meter["r"])

    def sample_meters(self):
        """Read every chain's level on a clock: an audio track's *input* (its first plugin), every other
        chain's output (the last plugin, so after its fader). The bars fall back, the peak holds, then
        follows."""
        now = time.monotonic()
        live = set(self.strips())
        changed = False
        floor = 1e-4  # linear (-80 dB): below this the bar has no height, so snap to true silence
        for strip in live:
            chain = self.chain(strip)
            if not chain:  # no plugin, no node: its meter goes rather than freezing at the last value
                self.meters.pop(strip, None)
                continue
            tr = self.track_of(strip)
            into = bool(tr and tr.audio)  # an audio track shows what arrives at its chain, not what leaves it
            left, right = self.host.peaks(chain[0] if into else chain[-1], into=into)
            meter = self.meters.setdefault(strip, {"l": 0.0, "r": 0.0, "peak": 0.0, "at": 0.0})
            before = (meter["l"], meter["r"], meter["peak"])
            meter["l"] = max(left, meter["l"] * 0.72)  # a needle that falls back between frames
            meter["r"] = max(right, meter["r"] * 0.72)
            if meter["l"] < floor:
                meter["l"] = 0.0
            if meter["r"] < floor:
                meter["r"] = 0.0
            top = max(left, right)
            if top >= meter["peak"]:
                meter["peak"], meter["at"] = top, now
            elif now - meter["at"] > 1.2:  # the peak holds, then slides down
                meter["peak"] = max(meter["l"], meter["r"], meter["peak"] * 0.94)
                if meter["peak"] < floor:
                    meter["peak"] = 0.0
            if max(abs(a - b) for a, b in zip(before, (meter["l"], meter["r"], meter["peak"]))) > 1e-6:
                changed = True
        for strip in [s for s in self.meters if s not in live]:
            del self.meters[strip]
        if changed:
            window = getattr(self, "window_ref", None)
            if window is not None:
                window.meters_changed()  # the bars move: repaint their boxes, not the rack
            else:
                self.invalidate()

    def draw_meter(self, p, r, strip):
        """A stereo peak meter: two bars from -60 to 0 dB, with a peak line that holds and falls."""
        s = self.s
        self.box(p, r, "#0f1117", BORDER, 1, radius=3)
        meter = self.meters.get(strip)
        if not meter:
            return
        gradient = QLinearGradient(0, r.bottom(), 0, r.top())
        gradient.setColorAt(0.0, QColor("#4ade80"))
        gradient.setColorAt(0.72, QColor("#facc15"))
        gradient.setColorAt(1.0, QColor("#ff5d5d"))
        p.setPen(Qt.NoPen)
        p.setBrush(gradient)
        half = r.width() / 2
        for value, left in ((meter["l"], r.left()), (meter["r"], r.center().x())):
            height = r.height() * self.db_fraction(value)
            if height > 0.5:
                p.drawRect(QRectF(left + 1.2 * s, r.bottom() - height, half - 2.4 * s, height))
        peak = r.height() * self.db_fraction(meter["peak"])
        if peak > 0.5:
            p.setPen(QPen(QColor("#ff5d5d" if meter["peak"] > 0.95 else "#e6e9ee"), 1.6 * s))
            p.drawLine(QPointF(r.left() + 1 * s, r.bottom() - peak), QPointF(r.right() - 1 * s, r.bottom() - peak))

    def hold_button(self, p, r, label, action, key, color=WARN):
        """A small button that does something destructive only after a held touch: it fills as it is held."""
        self.box(p, r, PANEL, BORDER, radius=5)
        self.text(p, r, label, color, 13, bold=True)
        held = next((g for g in self.gestures.values() if isinstance(g, Hold) and g.key == key), None)
        if held:
            fill = QRectF(r.left(), r.top(), r.width() * held.progress(), r.height())
            p.save()
            p.setClipRect(fill)
            self.box(p, r, QColor(255, 93, 93, 150), None, radius=5)
            p.restore()
        self.region(r, lambda pos: Hold(action, key=key))

    def delete_button(self, p, r, action, key):
        """The wider version used beside a chain's title: DELETE, hold to make it happen."""
        self.button(p, r, "DELETE", None, color=WARN, sub="hold")
        held = next((g for g in self.gestures.values() if isinstance(g, Hold) and g.key == key), None)
        if held:
            fill = QRectF(r.left(), r.top(), r.width() * held.progress(), r.height())
            p.save()
            p.setClipRect(fill)
            self.box(p, r, QColor(255, 93, 93, 140), None)
            p.restore()
        self.region(r, lambda pos: Hold(action, key=key))

    def draw_fader(self, p, r, strip, label=True, background=BG):
        s = self.s
        mute = QRectF(r.left(), r.bottom() - 40 * s, r.width(), 40 * s)
        self.button(p, mute, "M", lambda: self.set_mix(strip, mute=not strip.mute), on=strip.mute, color=WARN)
        bottom = mute.top() - (24 * s if label else 8 * s)
        top = r.top() + 4 * s
        track = QRectF(r.center().x() - 21 * s, top, 20 * s, bottom - top)  # the fader, with the meter beside it
        meter = QRectF(r.center().x() + 3 * s, top, 13 * s, bottom - top)
        self.box(p, track, "#141821", BORDER, radius=8)
        level = track.height() * strip.volume
        self.box(p, QRectF(track.left(), track.bottom() - level, track.width(), level), QColor(74, 222, 128, 150),
                 None, radius=6)
        self.meter_rects[("fader", id(strip))] = (
            meter, lambda p, r, strip=strip: self.draw_meter(p, r, strip), background)
        self.draw_meter(p, meter, strip)
        if label:
            self.text(p, QRectF(r.left(), track.bottom() + 2 * s, r.width(), 20 * s), f"{round(strip.volume * 100)}%",
                      DIM, 11)
        self.region(QRectF(r.left(), track.top(), r.width(), track.height()),
                    lambda pos: Slide(track, lambda v: self.set_mix(strip, volume=v), pos))

    def draw_slot(self, p, r, strip, k):
        s, ref = self.s, strip.slots[k]
        selected = self.sel == (strip, k)
        kind = "INSTRUMENT" if self.instrument_slot(strip, k) else "EFFECT"
        if ref:
            missing = (strip, k) in self.failed
            self.box(p, r, "#1b2130" if selected else PANEL, "#38d6ff" if selected else BORDER, 2 if selected else 1.5)
            inner = r.adjusted(8 * s, 4 * s, -8 * s, -4 * s)
            self.text(p, inner.adjusted(0, 0, 0, -inner.height() * 0.4), ref.name, WARN if missing else TEXT, 14,
                      bold=True, elide=True)
            self.text(p, inner.adjusted(0, inner.height() * 0.55, 0, 0),
                      "missing" if missing else f"{ref.fmt.upper()} · {kind.lower()}", DIM, 12, elide=True)
            self.on_press(r, lambda: self.select(strip, k))
        else:
            self.box(p, r, "#11141b", "#2a3040", 1.5)
            self.text(p, r, "+ INSTRUMENT" if kind == "INSTRUMENT" else "+ EFFECT", "#5b6577", 13, bold=True)
            self.on_press(r, lambda: self.open_browser(strip, k))

    def select(self, strip, k):
        if self.sel != (strip, k):
            self.sel, self.page = (strip, k), 0

    def draw_mixer(self, p, r):
        """A column per track (header: tap = edit its chain), a column per bus, the master on the right."""
        s, tracks, buses = self.s, self.project.tracks, self.project.buses
        bus_shown = buses[:3]
        bus_w = (len(bus_shown) * (128 * s + 10 * s) + 118 * s) if buses else 0
        cols, bus_area, master = split(r, [0, bus_w, 150 * s], 12 * s)
        cw = 96 * s
        per_page = max(1, int((cols.width() + 8 * s) // (cw + 8 * s)))
        pages = max(1, math.ceil(len(tracks) / per_page))
        self.mixer_page = min(self.mixer_page, pages - 1)
        if pages > 1:  # page arrows under the columns
            nav = QRectF(cols.left(), cols.bottom() - 36 * s, cols.width(), 36 * s)
            prev, label, nxt = split(nav, [70 * s, 0, 70 * s], 6 * s)
            self.button(p, prev, "◀", lambda: setattr(self, "mixer_page", max(0, self.mixer_page - 1)),
                        enabled=self.mixer_page > 0)
            self.text(p, label, f"{self.mixer_page + 1} / {pages}", DIM, 13)
            self.button(p, nxt, "▶", lambda: setattr(self, "mixer_page", min(pages - 1, self.mixer_page + 1)),
                        enabled=self.mixer_page < pages - 1)
            cols = cols.adjusted(0, 0, 0, -44 * s)
        shown = tracks[self.mixer_page * per_page:(self.mixer_page + 1) * per_page]
        for n, tr in enumerate(shown):
            col = QRectF(cols.left() + n * (cw + 8 * s), cols.top(), cw, cols.height())
            color = PALETTE[(tr.number - 1) % len(PALETTE)]
            self.box(p, col, PANEL, color if tr is self.selected() else BORDER, 2 if tr is self.selected() else 1.5,
                     radius=8)
            head = QRectF(col.left(), col.top(), col.width(), 58 * s)
            self.text(p, head.adjusted(0, 4 * s, 0, -head.height() * 0.45), f"T{tr.number}", color, 18, bold=True)
            inst = tr.strip.slots[0]
            below = (tr.source.split(":")[-1] or "no input") if tr.audio else (inst.name if inst else "no instrument")
            self.text(p, head.adjusted(6 * s, head.height() * 0.5, -6 * s, 0), below,
                      TEXT if (inst or tr.audio) else "#5b6577", 12, elide=True)
            if tr.bus:
                self.text(p, QRectF(head.right() - 40 * s, head.top() + 3 * s, 36 * s, 18 * s), f"→ B{tr.bus}",
                          "#38d6ff", 11, Qt.AlignRight | Qt.AlignTop, bold=True)
            self.on_press(head, lambda tr=tr: self.open_track(tr))
            self.draw_fader(p, QRectF(col.left() + 8 * s, head.bottom() + 6 * s, col.width() - 16 * s,
                                      col.bottom() - head.bottom() - 14 * s), tr.strip, background=PANEL)
        for n, bus in enumerate(bus_shown):
            col = QRectF(bus_area.left() + n * (128 * s + 10 * s), bus_area.top(), 128 * s,
                         bus_area.height() - 44 * s)
            self.draw_bus_column(p, col, bus)
        if buses:
            plus = QRectF(bus_area.left(), bus_area.bottom() - 38 * s, 118 * s, 38 * s)
            self.button(p, plus, "+ BUS", self.add_bus, sub="another effects bus")
            if len(buses) > len(bus_shown):
                self.text(p, QRectF(plus.right() + 6 * s, plus.top(), 40 * s, plus.height()),
                          f"+{len(buses) - len(bus_shown)}", DIM, 13)
        m = self.project.master
        self.box(p, master, "#1a1810", "#facc15", 2, radius=8)
        self.text(p, QRectF(master.left(), master.top(), master.width(), 34 * s), "MASTER", "#facc15", 17, bold=True)
        y = master.top() + 38 * s
        for k in range(SLOTS):
            slot = QRectF(master.left() + 8 * s, y, master.width() - 16 * s, 44 * s)
            ref = m.slots[k]
            self.box(p, slot, PANEL if ref else "#11141b", BORDER if ref else "#2a3040", 1.5, radius=6)
            self.text(p, slot.adjusted(6 * s, 0, -6 * s, 0), ref.name if ref else "+ EFFECT",
                      TEXT if ref else "#5b6577", 12, bold=True, elide=True)
            self.on_press(slot, lambda k=k, ref=ref: self.show("master", (m, k)) if ref else self.open_browser(m, k))
            y = slot.bottom() + 6 * s
        self.draw_fader(p, QRectF(master.left() + 8 * s, y + 6 * s, master.width() - 16 * s, master.bottom() - y - 14 * s),
                        m, background="#1a1810")

    def draw_bus_column(self, p, col, bus):
        """A bus in the mixer: its chain (tap a slot to fill or edit it) and its level."""
        s, strip = self.s, bus.strip
        chain = self.chain(strip)
        focused = self.focus is strip
        self.box(p, col, PANEL, "#38d6ff" if focused else BORDER, 2 if focused else 1.5, radius=8)
        head = QRectF(col.left(), col.top(), col.width(), 58 * s)
        self.text(p, head.adjusted(0, 4 * s, 0, -head.height() * 0.45), f"BUS {bus.number}", "#38d6ff", 17, bold=True)
        self.text(p, head.adjusted(6 * s, head.height() * 0.5, -6 * s, 0),
                  chain[0].ref.name if chain else "no effects", TEXT if chain else "#5b6577", 12, elide=True)
        self.on_press(head, lambda strip=strip: self.focus_on(strip))
        close = QRectF(col.right() - 34 * s, col.top() + 4 * s, 28 * s, 26 * s)
        self.hold_button(p, close, "✕", lambda bus=bus: self.remove_bus(bus), key=bus)
        y = head.bottom() + 4 * s
        for k in range(SLOTS):
            slot = QRectF(col.left() + 6 * s, y, col.width() - 12 * s, 40 * s)
            self.draw_slot(p, slot, strip, k)
            self.hits[-1] = (slot, lambda pos, strip=strip, k=k: self.bus_slot(strip, k))
            y = slot.bottom() + 4 * s
        self.draw_fader(p, QRectF(col.left() + 8 * s, y + 4 * s, col.width() - 16 * s, col.bottom() - y - 14 * s),
                        strip, background=PANEL)

    def focus_first(self, strip):
        """Choose the first plugin of a chain when nothing on it is chosen yet (the track just changed)."""
        if self.sel and self.sel[0] is strip:
            return
        first = next((k for k, ref in enumerate(strip.slots) if ref), None)
        if first is not None:
            self.sel, self.page = (strip, first), 0

    def focus_on(self, strip):
        """Show a bus' chain in the TRACK view (any tab tap goes back to the selected track)."""
        self.view, self.focus, self.page = "track", strip, 0

    def bus_slot(self, strip, k):
        self.focus_on(strip)
        if strip.slots[k]:
            self.select(strip, k)
        else:
            self.open_browser(strip, k)

    def open_track(self, tr):
        self.select_track(tr)
        self.show("track")

    def draw_params(self, p, r, strip):
        s = self.s
        sel = self.sel if self.sel and self.sel[0] is strip else None
        if sel is None:  # the track (or bus) just changed: edit the first plugin it has, not an empty panel
            self.focus_first(strip)
            sel = self.sel if self.sel and self.sel[0] is strip else None
        plugin = self.loaded.get(sel) if sel else None
        self.box(p, r, "#11141b", "#2a3040", 1.5)
        if not plugin or plugin.ref.label not in EQ_LABELS:
            if self.eq.plugin is not None:
                self.eq.forget()  # not our EQ (or nothing) on screen: stop reading its analyser
        if not plugin:
            msg = "tap a plugin to edit it" if sel is None else "plugin not loaded"
            self.text(p, r, msg, "#5b6577", 14)
            if sel:
                self.button(p, QRectF(r.center().x() - 60 * s, r.center().y() + 24 * s, 120 * s, 44 * s), "CHANGE",
                            lambda: self.open_browser(*sel))
            return
        inner = r.adjusted(10 * s, 8 * s, -10 * s, -8 * s)
        ours = plugin.ref.label in BUILTIN_LABELS  # no native UI to open: its knobs are this panel
        widths = [0, 86 * s, 84 * s, 84 * s] if ours else [0, 86 * s, 56 * s, 84 * s, 84 * s]
        parts = split(QRectF(inner.left(), inner.top(), inner.width(), 44 * s), widths, 6 * s)
        title, assign = parts[0], parts[1]
        ui, change, remove = (None, parts[2], parts[3]) if ours else (parts[2], parts[3], parts[4])
        self.text(p, title, plugin.ref.name, TEXT, 15, Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
        picking = self.assigning == "pick"
        self.button(p, assign, "ASSIGN", self.toggle_assign, on=picking, sub="to a fader")
        if not ours:
            self.button(p, ui, "UI", lambda: self.host.show_ui(plugin), enabled=self.host.has_ui(plugin))
        self.button(p, change, "CHANGE", lambda: self.open_browser(*sel))
        self.button(p, remove, "REMOVE", lambda: None, color=WARN, sub="hold")
        held = next((x for x in self.gestures.values() if isinstance(x, Hold)), None)
        if held:
            p.save()
            p.setClipRect(QRectF(remove.left(), remove.top(), remove.width() * held.progress(), remove.height()))
            self.box(p, remove, QColor(255, 93, 93, 140), None)
            p.restore()
        self.region(remove, lambda pos: Hold(lambda: self.set_slot(*sel, None)))

        if plugin.ref.label in SAMPLER_LABELS:  # our sampler: the waveform and pads, not a grid of knobs
            self.sampler.draw(p, QRectF(inner.left(), title.bottom() + 8 * s, inner.width(),
                                        inner.bottom() - title.bottom() - 8 * s), plugin,
                              self.sampler_slot(strip, sel[1]), self.sampler_manifest_path(strip, sel[1]))
            return
        if plugin.ref.label in EQ_LABELS:  # our EQ: the curve, not a grid of knobs
            self.eq.draw(p, QRectF(inner.left(), title.bottom() + 8 * s, inner.width(),
                                   inner.bottom() - title.bottom() - 8 * s), plugin)
            return

        pages = param_pages(plugin.params, self.KNOB_COLS, self.KNOB_ROWS)  # whole sections per page
        self.page = min(self.page, len(pages) - 1)
        nav = QRectF(inner.left(), inner.bottom() - 40 * s, inner.width(), 40 * s)
        prev, label, nxt = split(nav, [70 * s, 0, 70 * s], 6 * s)
        self.button(p, prev, "◀", lambda: setattr(self, "page", max(0, self.page - 1)), enabled=self.page > 0)
        hint = "tap a knob to drive it from a CTRL fader" if picking else f"{self.page + 1} / {len(pages)}  ·  " \
            f"{len(plugin.params)} params"
        self.text(p, label, hint, "#38d6ff" if picking else DIM, 12)
        self.button(p, nxt, "▶", lambda: setattr(self, "page", min(len(pages) - 1, self.page + 1)),
                    enabled=self.page < len(pages) - 1)
        grid = QRectF(inner.left(), title.bottom() + 8 * s, inner.width(), nav.top() - title.bottom() - 16 * s)
        page = pages[self.page]
        gap, pad = 8 * s, 22 * s  # between boxes/lines, and the room for a box's name
        lines = []  # [columns used, rows, [sections]]
        for sec in page:
            width, height = section_box(sec, self.KNOB_COLS)
            if lines and lines[-1][0] + width <= self.KNOB_COLS:
                lines[-1][0] += width
                lines[-1][1] = max(lines[-1][1], height)
                lines[-1][2].append(sec)
            else:
                lines.append([width, height, [sec]])
        rows = sum(line[1] for line in lines)
        kh = (grid.height() - gap * max(0, len(lines) - 1)) / max(1, rows)
        kw = grid.width() / self.KNOB_COLS
        y = grid.top()
        for used, line_rows, sections in lines:
            x = grid.left()
            for sec in sections:
                width, need = section_box(sec, self.KNOB_COLS)
                box = QRectF(x, y, width * kw - gap, line_rows * kh)
                self.box(p, box, "#0f131b", "#242a36", 1.5, radius=9)
                color = param_color(sec[0].name)
                self.box(p, QRectF(box.left() + 9 * s, box.top() + 9 * s, 4 * s, 13 * s), color, None, radius=2)
                self.text(p, QRectF(box.left() + 18 * s, box.top() + 5 * s, box.width() - 24 * s, 16 * s),
                          section_name(sec), color, 10, Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
                if is_step_section(sec):  # a sequence: bars, all of them in the box's width
                    columns = min(len(sec), BARS_PER_ROW)
                    cell_w, cell_h = box.width() / columns, (box.height() - pad) / need
                    for n, prm in enumerate(sec):
                        cell = QRectF(box.left() + (n % columns) * cell_w, box.top() + pad + (n // columns) * cell_h,
                                      cell_w, cell_h)
                        self.draw_bar(p, cell.adjusted(2 * s, 2 * s, -2 * s, -2 * s), plugin, prm)
                else:
                    cell_w = box.width() / width
                    for n, prm in enumerate(sec):
                        cell = QRectF(box.left() + (n % width) * cell_w, box.top() + pad + (n // width) * kh,
                                      cell_w, kh - pad)
                        self.draw_knob(p, cell.adjusted(3 * s, 3 * s, -3 * s, -3 * s), plugin, prm)
                x += width * kw
            y += line_rows * kh + gap

    def draw_knob(self, p, r, plugin, prm):
        """Drag up/down to turn; tap toggles on/off parameters."""
        s, host = self.s, self.host
        f = (host.value(plugin, prm.index) - prm.lo) / (prm.hi - prm.lo)
        size = min(r.width(), r.height() - 22 * s) - 8 * s
        dial = QRectF(r.center().x() - size / 2, r.top() + 4 * s, size, size)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor("#262b36"), 5 * s, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(dial, 225 * 16, -270 * 16)
        color = QColor(param_color(prm.name))
        p.setPen(QPen(color, 5 * s, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(dial, 225 * 16, round(-270 * 16 * max(0.0, min(1.0, f))))
        self.text(p, dial.adjusted(6 * s, 0, -6 * s, 0), self.short(host.text(plugin, prm.index)), TEXT, 11,
                  elide=True)
        self.text(p, QRectF(r.left(), dial.bottom() + 2 * s, r.width(), 18 * s), prm.name, color.lighter(115).name(),
                  11, elide=True)

        def get():
            return (host.value(plugin, prm.index) - prm.lo) / (prm.hi - prm.lo)

        def set_(norm):
            v = prm.lo + norm * (prm.hi - prm.lo)
            self.set_param(plugin, prm.index, round(v) if prm.integer or prm.boolean else v, source="knob")

        if self.assigning == "pick":  # the ASSIGN button is on: this tap chooses the knob, it does not turn it
            self.box(p, r, QColor(0, 0, 0, 0), "#38d6ff", 1.2, radius=8)
            self.on_press(r, lambda prm=prm: self.assign_knob(prm))
            return
        toggle = (lambda: set_(0.0 if get() > 0.5 else 1.0)) if prm.boolean else None
        self.region(r, lambda pos: Drag(pos, get, set_, 0.0, 1.0, 1.5 * s, 1 / 150, toggle))

    def draw_bar(self, p, r, plugin, prm):
        """A step's value as a vertical bar: bipolar parameters (an LFO step is -1..1) grow both ways from the
        middle line. Drag up/down to set it, like a knob."""
        s, host = self.s, self.host
        f = (host.value(plugin, prm.index) - prm.lo) / (prm.hi - prm.lo)
        f = max(0.0, min(1.0, f))
        zero = (0.0 - prm.lo) / (prm.hi - prm.lo) if prm.lo < 0 < prm.hi else None
        color = param_color(prm.name)
        label = 16 * s
        width = min(16 * s, r.width() * 0.62)
        track = QRectF(r.center().x() - width / 2, r.top() + 2 * s, width, r.height() - label - 2 * s)
        self.box(p, track, "#141821", BORDER, 1, radius=4)
        value_y = track.top() + track.height() * (1 - f)
        if zero is None:
            bar = QRectF(track.left() + 1 * s, value_y, track.width() - 2 * s, track.bottom() - value_y)
        else:
            middle = track.top() + track.height() * (1 - zero)
            bar = QRectF(track.left() + 1 * s, min(middle, value_y), track.width() - 2 * s, abs(middle - value_y))
            p.setPen(QPen(QColor("#3a4252"), 1))
            p.drawLine(QPointF(track.left() + 1 * s, middle), QPointF(track.right() - 1 * s, middle))
        if bar.height() > 0.4:
            self.box(p, bar, color, None, radius=2)
        number = re.search(r"(\d+)\s*$", prm.name)
        self.text(p, QRectF(r.left(), track.bottom() + 1 * s, r.width(), label), number.group(1) if number else "",
                  DIM, 10)

        def get():
            return (host.value(plugin, prm.index) - prm.lo) / (prm.hi - prm.lo)

        def set_(norm):
            v = prm.lo + norm * (prm.hi - prm.lo)
            self.set_param(plugin, prm.index, round(v) if prm.integer or prm.boolean else v, source="bar")

        if self.assigning == "pick":  # ASSIGN: the tap chooses this parameter for a fader, it does not set it
            self.box(p, r, QColor(0, 0, 0, 0), "#38d6ff", 1.2, radius=6)
            self.on_press(r, lambda prm=prm: self.assign_knob(prm))
            return
        self.region(r, lambda pos: Drag(pos, get, set_, 0.0, 1.0, 1.5 * s, 1 / 150, None))

    def toggle_assign(self):
        """The ASSIGN button: the next knob tapped is the one a CTRL fader will drive (tap again to cancel)."""
        self.assigning = None if self.assigning else "pick"

    def assign_knob(self, prm):
        """A knob chosen in ASSIGN mode: pick the fader that will drive it."""
        self.assigning = (self.sel[0], self.sel[1], prm)

    def draw_assign(self, p):
        """Which CTRL fader should drive the knob chosen with ASSIGN."""
        s, g = self.s, 16 * self.s
        strip, slot, prm = self.assigning
        color = param_color(prm.name)
        self.text(p, QRectF(g, g, self.w - 2 * g, 40 * s), f"Fader for  {prm.name}", color, 22,
                  Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
        self.text(p, QRectF(g, g + 40 * s, self.w - 2 * g, 26 * s),
                  "CTRL-10 fader that will move this parameter directly (no MIDI); CANCEL keeps it as it is:",
                  DIM, 14, Qt.AlignLeft | Qt.AlignVCenter)
        faders = [(i, c) for i, c in enumerate(self.project.controls) if c.kind == "cc"]
        grid = QRectF(g, g + 80 * s, self.w - 2 * g, self.h - 2 * g - 80 * s - 64 * s)
        cols = 4
        rows = math.ceil(len(faders) / cols)
        for n, (i, c) in enumerate(faders):
            row = QRectF(grid.left(), grid.top() + (n // cols) * grid.height() / rows, grid.width(), grid.height() / rows)
            r = split(row.adjusted(0, 4 * s, 0, -4 * s), [0] * cols, 10 * s)[n % cols]
            now = c.param[3] if c.target == "param" else f"CC {c.cc}"
            self.button(p, r, f"FADER {n + 1}", lambda i=i: self.link_to_fader(i), sub=f"now: {now}")
        cancel = QRectF(self.w - g - 160 * s, self.h - g - 52 * s, 160 * s, 52 * s)
        self.button(p, cancel, "CANCEL", lambda: setattr(self, "assigning", None))

    @staticmethod
    def short(text):
        """Plugins often print bare floats with 6 decimals; keep 3-4 significant digits."""
        try:
            v = float(text)
        except ValueError:
            return text
        return f"{v:.0f}" if abs(v) >= 100 else f"{v:.1f}" if abs(v) >= 10 else f"{v:.2f}"

    # --- plugin browser -----------------------------------------------------
    def open_browser(self, strip, k):
        self.browser, self.browser_scroll = (strip, k), 0.0

    def builtin_plugins(self):
        """The plugins that ship with the app, found on disk the first time this is asked."""
        if self._builtin is None:
            self._builtin = plugins.builtin_refs()
        return self._builtin

    def browser_items(self):
        """The plugins the browser lists: by format, or only the ones that ship with the app (BUILTIN), which
        come first in the full list so they are found without hunting."""
        instrument = self.instrument_slot(*self.browser)

        def shown(ref):
            if ref.label in INTERNAL_LABELS:  # the app puts those in place: nothing to add by hand
                return False
            if self.browser_fmt == "builtin":
                return ref.label in BUILTIN_LABELS
            return self.browser_fmt in (None, ref.fmt)

        def kind(ref):
            return ref.instrument if instrument else ref.effect

        items = [r for r in self.catalog.plugins if kind(r) and shown(r)]
        have = {str(r.path) for r in self.catalog.plugins}
        # one of ours found on disk is shown by the same rule (a plugin the app puts in place stays out too)
        items += [r for r in self.builtin_plugins() if kind(r) and shown(r) and str(r.path) not in have]
        ours = self.browser_fmt in (None, "builtin")
        if ours:
            items.sort(key=lambda r: (r.label not in BUILTIN_LABELS, r.name.lower(), r.fmt != "clap"))
            unique, seen = [], set()  # one of ours in both formats: one row, and the CLAP first
            for r in items:
                if r.label in BUILTIN_LABELS:
                    if r.name in seen:
                        continue
                    seen.add(r.name)
                unique.append(r)
            items = unique
        else:
            items.sort(key=lambda r: r.name.lower())
        return items

    def choose(self, ref):
        strip, k = self.browser
        self.browser = None
        self.message = ""
        self.set_slot(strip, k, ref)

    def draw_browser(self, p):
        s, g = self.s, 8 * self.s
        strip, k = self.browser
        tr = self.track_of(strip)
        header = QRectF(g, g, self.w - 2 * g, 48 * s)
        title, rescan, *fmts, close = split(header, [0, 96 * s] + [74 * s] * 5 + [60 * s], 6 * s)
        kind = "instrument" if self.instrument_slot(strip, k) else "effect"
        self.text(p, title.adjusted(6 * s, 0, 0, 0), f"{f'T{tr.number}' if tr else 'MASTER'} · choose {kind}", TEXT, 16,
                  Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        cat = self.catalog
        self.button(p, rescan, "RESCAN", cat.rescan, pending=cat.scanning,
                    sub=cat.progress if cat.scanning else "new plugins")
        for rect, fmt, label in zip(fmts, (None, "builtin", "clap", "lv2", "vst3"),
                                    ("ALL", "BUILTIN", "CLAP", "LV2", "VST3")):
            self.button(p, rect, label, lambda fmt=fmt: self.set_browser_fmt(fmt), on=self.browser_fmt == fmt)
        self.button(p, close, "✕", lambda: setattr(self, "browser", None))
        area = QRectF(g, header.bottom() + 8 * s, self.w - 2 * g, self.h - header.bottom() - 8 * s - g)
        if not cat.ready:
            self.text(p, area, f"scanning plugins… {cat.progress}", DIM, 15)
            return
        items = self.browser_items()
        listing, letters = split(area, [0, 46 * s], 8 * s)
        rh = 50 * s
        self.browser_scroll = max(0.0, min(self.browser_scroll, len(items) * rh - listing.height()))
        scroll = (lambda: self.browser_scroll), (lambda v: setattr(self, "browser_scroll", v))
        p.save()
        p.setClipRect(listing)
        first = int(self.browser_scroll // rh)
        for n in range(first, min(len(items), first + int(listing.height() / rh) + 2)):
            ref = items[n]
            row = QRectF(listing.left(), listing.top() + n * rh - self.browser_scroll, listing.width(), rh - 5 * s)
            self.box(p, row, PANEL, BORDER, radius=6)
            name, info, add = split(row.adjusted(10 * s, 4 * s, -4 * s, -4 * s), [0, row.width() * 0.32, 90 * s],
                                    10 * s)
            self.text(p, name, ref.name, TEXT, 15, Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
            self.text(p, info, f"{ref.maker}  {ref.fmt.upper()}", DIM, 13, Qt.AlignRight | Qt.AlignVCenter, elide=True)
            # the row only scrolls; ADD adds, also only on a tap, so a scroll started on it adds nothing
            self.region(row.intersected(listing), lambda pos: ScrollOrTap(pos, *scroll, lambda: None))
            self.button(p, add, "ADD", None, on=True)
            self.hits[-1] = (add.intersected(listing), lambda pos, ref=ref: ScrollOrTap(
                pos, *scroll, lambda: self.choose(ref)))
        p.restore()
        if not items:
            self.text(p, listing, "nothing here", "#5b6577", 15)
            return
        # A-Z strip: tap or slide along it to jump; the letter of the top of the free list is lit (the pinned
        # block of our own plugins does not have a letter of its own)
        self.box(p, letters, PANEL, BORDER, radius=8)
        pinned = self.pinned(items)
        current = self.letter(items[min(max(first, pinned), len(items) - 1)])
        lh = letters.height() / len(self.LETTERS)
        for n, ch in enumerate(self.LETTERS):
            r = QRectF(letters.left(), letters.top() + n * lh, letters.width(), lh)
            self.text(p, r, ch, "#38d6ff" if ch == current else TEXT, 14, bold=True)
        pick = lambda v: self.jump(items, self.LETTERS[min(len(self.LETTERS) - 1, int((1 - v) * len(self.LETTERS)))], rh)
        self.region(letters, lambda pos: Slide(letters, pick, pos))

    def set_browser_fmt(self, fmt):
        self.browser_fmt, self.browser_scroll = fmt, 0.0

    def letter(self, ref):
        c = ref.name[:1].upper()
        return c if c in self.LETTERS[1:] else "#"

    def pinned(self, items):
        """How many of our own plugins the full list pins to the top. The A-Z strip indexes the rest, so a
        letter lands on the plugins that letter really sorts instead of on the pinned block."""
        if self.browser_fmt not in (None, "builtin"):
            return 0
        n = next((n for n, ref in enumerate(items) if ref.label not in BUILTIN_LABELS), len(items))
        return n if n < len(items) else 0  # the BUILTIN list is all ours: it jumps like any other list

    def jump(self, items, ch, rh):
        """Scroll the list to the first plugin at or after letter `ch` ('#' = digits and symbols), skipping
        the pinned block of our own plugins at the top."""
        want = self.LETTERS.index(ch)
        start = self.pinned(items)
        n = next((n for n in range(start, len(items))
                  if self.LETTERS.index(self.letter(items[n])) >= want), len(items) - 1)
        self.browser_scroll = n * rh
