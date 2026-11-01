"""The plugin host module (Carla): per-track chains and the master bus, the parameter knobs, the mixer and
the plugin browser, plus the `Slide` gesture they use."""
import math
import threading
import time
import zlib

import rtmidi

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPen

import plugins

from .constants import BG, BORDER, DIM, PALETTE, PANEL, SLOTS, TEXT, WARN, XYFX_LABELS
from .gestures import Drag, Gesture, Hold, ScrollOrTap
from .ui import Panel, param_color, split


class Slide(Gesture):
    """Vertical slider: the finger's height inside `track` is the value (0..1)."""

    def __init__(self, track, set_, pos):
        self.track, self.set = track, set_
        self.move(pos)

    def move(self, pos):
        self.set(max(0.0, min(1.0, (self.track.bottom() - pos.y()) / self.track.height())))


class HostPanel(Panel):
    """Plugin host (Carla). Every track has a chain (instrument -> 2 effects); all track chains go through
    the master's effects to the audio output. Views: TRACK (the chain of the track selected in the
    sequencer, the chosen plugin's parameters as knobs), MIXER (every track's level, and the master) and
    MASTER (the master chain, like TRACK). If Carla or JACK isn't available the panel says so and the rest
    of the rack works as before."""
    KNOB_COLS, KNOB_ROWS = 5, 3
    LETTERS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def __init__(self, project, router, state_dir, cache_path, audio_out="default", remember=None, name="Touch-Seq",
                 patch=None, buffer=0):
        super().__init__()
        self.project, self.router, self.state_dir, self.audio_out, self.name = project, router, state_dir, audio_out, name
        self.remember = remember  # (key, value) -> None: keep a machine setting (audio output, patchbay links)
        self.selected = lambda: project.tracks[0]  # the track whose chain TRACK shows (the rack: the sequencer's)
        self.select_track = lambda tr: None  # the rack: select a track in the sequencer
        self.choose_port = lambda tr: None  # the rack: choose the MIDI output of an external track
        self.view = "track"  # "track", "mixer" or "master"
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
        if self.buffer:
            plugins.set_quantum(self.buffer)  # before the engine starts, so the plugins see that size
        try:
            self.host = plugins.Host(name)
        except (RuntimeError, OSError, ImportError) as e:
            self.host, self.message = None, f"plugin host unavailable: {e}"
            return
        self.catalog = plugins.Catalog(cache_path)
        self.sink = self.current_sink()
        self.sink_changed = False
        for strip in self.strips():
            for k, ref in enumerate(strip.slots):
                if ref:
                    self.load(strip, k, ref, restore=True)
        self.rewire()
        threading.Thread(target=self.watch_output, daemon=True).start()

    # --- chains ---------------------------------------------------------------
    def strips(self):
        """Chains: every plugin track and the master. An external track has no chain (its notes go out to
        hardware), so it never shows a slot that could take a plugin."""
        return [t.strip for t in self.project.tracks if not t.external] + [self.project.master]

    def track_of(self, strip):
        return next((t for t in self.project.tracks if t.strip is strip), None)  # None: the master

    def instrument_slot(self, strip, k):
        return k == 0 and strip is not self.project.master

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

    # --- audio graph ----------------------------------------------------------
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
        tr = self.track_of(strip)
        tag = zlib.crc32(f"{ref.fmt}|{ref.path}|{ref.label}".encode())
        prefix = prefix or (f"t{tr.number}" if tr else "master")
        return self.state_dir / f"{prefix}-{k + 1}-{tag:08x}.carxs"

    def load(self, strip, k, ref, restore=False):
        tr = self.track_of(strip)
        client = f"{f'T{tr.number}' if tr else 'M'}.{k + 1} {ref.name}".replace(":", " ")[:48]
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
            self.host.load_state(plugin, state)
            self.host.refresh_params(plugin)

    def ensure_port(self, tr):
        """The JACK MIDI port feeding this track's instrument (one JACK client per track)."""
        if tr not in self.router.taps:
            port = rtmidi.MidiOut(rtmidi.API_UNIX_JACK, name=f"{self.name} T{tr.number}")
            port.open_virtual_port("midi")
            self.router.taps[tr] = port

    def unload(self, strip, k):
        plugin = self.loaded.pop((strip, k), None)
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
            for f in self.state_dir.glob(f"t{tr.number}-*.carxs"):
                f.unlink()
        if gone:
            for strip in gone:
                self.wiring.replace(strip, [])
            self.rewire()

    def rewire(self):
        """Link every chain: a track's MIDI port to its instrument, plugin to plugin, the end of each track
        chain into the master's effects (or the output if it has none), the master to the output; and set
        the levels."""
        outs = lambda pl: [f"{pl.client}:output_{n}" for n in range(1, self.host.ports(pl)[1] + 1)]
        ins = lambda pl: [f"{pl.client}:input_{n}" for n in range(1, self.host.ports(pl)[0] + 1)]
        sink = [f"{self.sink}:playback_FL", f"{self.sink}:playback_FR"] if self.sink else []
        master = self.chain(self.project.master)
        into_master = ins(master[0]) if master else sink
        for strip in self.strips():
            chain, tr = self.chain(strip), self.track_of(strip)
            links = []
            if tr and chain and chain[0].ref.midi_ins:
                links.append((f"{self.name} T{tr.number}:midi", f"{chain[0].client}:events-in"))
            for a, b in zip(chain, chain[1:]):
                links += self.pair(outs(a), ins(b))
            if chain:
                links += self.pair(outs(chain[-1]), sink if strip is self.project.master else into_master)
            self.wiring.replace(strip, links)
        self.apply_levels()

    def apply_levels(self):
        """A chain's level sits on its last plugin; without master effects the master level joins each track's."""
        m = self.project.master
        master_chain = self.chain(m)
        master_level = 0.0 if m.mute else m.volume
        for strip in self.strips():
            chain = self.chain(strip)
            for pl in chain:
                self.host.set_volume(pl, 1.0)
            if chain:
                level = 0.0 if strip.mute else strip.volume
                if strip is not m and not master_chain:
                    level *= master_level
                self.host.set_volume(chain[-1], level if strip is not m else master_level)

    @staticmethod
    def pair(outs, ins):
        """Stereo links: first two outputs to first two inputs; a mono side feeds / takes both."""
        outs, ins = outs[:2], ins[:2]
        if not outs or not ins:
            return []
        return [(outs[min(j, len(outs) - 1)], ins[min(j, len(ins) - 1)]) for j in range(max(len(outs), len(ins)))]

    def save_states(self):
        if not self.host or not self.loaded:
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for (strip, k), plugin in self.loaded.items():
            self.host.save_state(plugin, self.state_file(strip, k, plugin.ref))

    def close(self):
        if self.host:
            self.save_states()
            self.host.close()
            self.host, self.message = None, "plugin host closed"  # UI timers may still tick
        if self.buffer:
            plugins.set_quantum(0)  # the machine goes back to its own size; ours comes back on the next start

    def tick(self):
        super().tick()
        self.router.refresh(self.project.tracks)  # external tracks: look for their MIDI outputs again
        if self.host:
            self.host.idle()
            self.forget_deleted_tracks()
            if self.xyfx() and self.xy_values().get("bpm") != self.project.bpm:  # repeats follow the tempo
                self.xy(bpm=self.project.bpm)
            if self.sink_changed:  # Carla calls belong in this (the UI) thread
                self.sink_changed = False
                self.rewire()

    def animating(self):
        # knobs read the plugins' live values: their own LFOs and envelopes move them
        return super().animating() or bool(self.loaded)

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
            self.host.set_value(plugin, prm.index, round(v) if prm.integer or prm.boolean else v)

    def link_to_fader(self, i):
        """Make CC fader i drive the parameter whose knob was held (self.assigning)."""
        strip, slot, prm = self.assigning
        tr = self.track_of(strip)
        c = self.project.controls[i]
        c.target, c.param = "param", [tr.number if tr else 0, slot, prm.index, prm.name]
        self.assigning = None

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
                    self.host.set_value(plugin, prm.index, values[prm.name.lower()])

    def xy_values(self):
        plugin = self.xyfx()
        return {prm.name.lower(): self.host.value(plugin, prm.index) for prm in plugin.params} if plugin else {}

    def set_mix(self, strip, volume=None, mute=None):
        if volume is not None:
            strip.volume = volume
        if mute is not None:
            strip.mute = mute
        self.apply_levels()

    # --- drawing ------------------------------------------------------------
    def draw(self, p):
        s, g = self.s, 8 * self.s
        p.fillRect(self.rect(), QColor(BG))
        if not self.host:
            self.text(p, self.rect().adjusted(20 * s, 0, -20 * s, 0), self.message, WARN, 15)
            return
        if self.browser:
            self.draw_browser(p)
            return
        if self.assigning:
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
        else:
            tr = self.selected()
            if tr.external:  # no chain: the notes go out to hardware instead
                self.draw_external(p, body, tr)
                return
            strip = tr.strip
        left, right = split(body, [body.width() * 0.28, 0], 12 * s)  # narrow chain, room for 5 knob columns
        self.draw_chain(p, left, strip)
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
        self.view, self.sel, self.page = view, sel, 0

    def draw_chain(self, p, r, strip):
        """Title, the slots top to bottom, and the chain's level fader + mute beside them."""
        s, tr = self.s, self.track_of(strip)
        color = PALETTE[(tr.number - 1) % len(PALETTE)] if tr else "#facc15"
        title = QRectF(r.left(), r.top(), r.width(), 34 * s)
        self.box(p, QRectF(title.left(), title.top() + 6 * s, 5 * s, title.height() - 12 * s), color, None, radius=2)
        name = f"T{tr.number}  {tr.kind}  CH{tr.channel}" if tr else "MASTER  ·  all tracks → output"
        self.text(p, title.adjusted(14 * s, 0, 0, 0), name, color, 16, Qt.AlignLeft | Qt.AlignVCenter, bold=True,
                  elide=True)
        slots, fader = split(QRectF(r.left(), title.bottom() + 8 * s, r.width(), r.bottom() - title.bottom() - 8 * s),
                             [0, 56 * s], 10 * s)
        sh = (slots.height() - 2 * 8 * s) / SLOTS
        for k in range(SLOTS):
            self.draw_slot(p, QRectF(slots.left(), slots.top() + k * (sh + 8 * s), slots.width(), sh), strip, k)
        self.draw_fader(p, fader, strip)

    def draw_fader(self, p, r, strip, label=True):
        s = self.s
        mute = QRectF(r.left(), r.bottom() - 40 * s, r.width(), 40 * s)
        self.button(p, mute, "M", lambda: self.set_mix(strip, mute=not strip.mute), on=strip.mute, color=WARN)
        bottom = mute.top() - (24 * s if label else 8 * s)
        track = QRectF(r.center().x() - 14 * s, r.top() + 4 * s, 28 * s, bottom - r.top() - 4 * s)
        self.box(p, track, "#141821", BORDER, radius=8)
        level = track.height() * strip.volume
        self.box(p, QRectF(track.left(), track.bottom() - level, track.width(), level), QColor(74, 222, 128, 150),
                 None, radius=6)
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
        """A column per track (header: tap = edit its chain), the master column on the right."""
        s, tracks = self.s, self.project.tracks
        cols, master = split(r, [0, 150 * s], 16 * s)
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
            self.text(p, head.adjusted(6 * s, head.height() * 0.5, -6 * s, 0), inst.name if inst else "no instrument",
                      TEXT if inst else "#5b6577", 12, elide=True)
            self.on_press(head, lambda tr=tr: self.open_track(tr))
            self.draw_fader(p, QRectF(col.left() + 8 * s, head.bottom() + 6 * s, col.width() - 16 * s,
                                      col.bottom() - head.bottom() - 14 * s), tr.strip)
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
                        m)

    def open_track(self, tr):
        self.select_track(tr)
        self.show("track")

    def draw_params(self, p, r, strip):
        s = self.s
        sel = self.sel if self.sel and self.sel[0] is strip else None
        plugin = self.loaded.get(sel) if sel else None
        self.box(p, r, "#11141b", "#2a3040", 1.5)
        if not plugin:
            msg = "tap a plugin to edit it" if sel is None else "plugin not loaded"
            self.text(p, r, msg, "#5b6577", 14)
            if sel:
                self.button(p, QRectF(r.center().x() - 60 * s, r.center().y() + 24 * s, 120 * s, 44 * s), "CHANGE",
                            lambda: self.open_browser(*sel))
            return
        inner = r.adjusted(10 * s, 8 * s, -10 * s, -8 * s)
        title, ui, change, remove = split(QRectF(inner.left(), inner.top(), inner.width(), 44 * s),
                                          [0, 56 * s, 84 * s, 84 * s], 6 * s)
        self.text(p, title, plugin.ref.name, TEXT, 15, Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
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

        per_page = self.KNOB_COLS * self.KNOB_ROWS
        pages = max(1, math.ceil(len(plugin.params) / per_page))
        self.page = min(self.page, pages - 1)
        nav = QRectF(inner.left(), inner.bottom() - 40 * s, inner.width(), 40 * s)
        prev, label, nxt = split(nav, [70 * s, 0, 70 * s], 6 * s)
        self.button(p, prev, "◀", lambda: setattr(self, "page", max(0, self.page - 1)), enabled=self.page > 0)
        self.text(p, label, f"{self.page + 1} / {pages}  ·  {len(plugin.params)} params", DIM, 12)
        self.button(p, nxt, "▶", lambda: setattr(self, "page", min(pages - 1, self.page + 1)),
                    enabled=self.page < pages - 1)
        grid = QRectF(inner.left(), title.bottom() + 8 * s, inner.width(), nav.top() - title.bottom() - 16 * s)
        kw, kh = grid.width() / self.KNOB_COLS, grid.height() / self.KNOB_ROWS
        for n, prm in enumerate(plugin.params[self.page * per_page:(self.page + 1) * per_page]):
            cell = QRectF(grid.left() + (n % self.KNOB_COLS) * kw, grid.top() + (n // self.KNOB_COLS) * kh, kw, kh)
            self.draw_knob(p, cell.adjusted(3 * s, 3 * s, -3 * s, -3 * s), plugin, prm)

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
            host.set_value(plugin, prm.index, round(v) if prm.integer or prm.boolean else v)

        toggle = (lambda: set_(0.0 if get() > 0.5 else 1.0)) if prm.boolean else None
        strip = self.sel[0]
        hold = lambda: setattr(self, "assigning", (strip, self.sel[1], prm))  # hold: drive it from a fader
        self.region(r, lambda pos: Drag(pos, get, set_, 0.0, 1.0, 1.5 * s, 1 / 150, toggle, on_hold=hold))

    def draw_assign(self, p):
        """Which CTRL fader should drive the held knob's parameter."""
        s, g = self.s, 16 * self.s
        strip, slot, prm = self.assigning
        color = param_color(prm.name)
        self.text(p, QRectF(g, g, self.w - 2 * g, 40 * s), f"Fader for  {prm.name}", color, 22,
                  Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
        self.text(p, QRectF(g, g + 40 * s, self.w - 2 * g, 26 * s),
                  "CTRL-10 fader that will move this parameter directly (no MIDI):", DIM, 14,
                  Qt.AlignLeft | Qt.AlignVCenter)
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

    def browser_items(self):
        instrument = self.instrument_slot(*self.browser)
        return [r for r in self.catalog.plugins
                if (r.instrument if instrument else r.effect) and self.browser_fmt in (None, r.fmt)]

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
        title, rescan, *fmts, close = split(header, [0, 96 * s] + [70 * s] * 4 + [60 * s], 6 * s)
        kind = "instrument" if self.instrument_slot(strip, k) else "effect"
        self.text(p, title.adjusted(6 * s, 0, 0, 0), f"{f'T{tr.number}' if tr else 'MASTER'} · choose {kind}", TEXT, 16,
                  Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        cat = self.catalog
        self.button(p, rescan, "RESCAN", cat.rescan, pending=cat.scanning,
                    sub=cat.progress if cat.scanning else "new plugins")
        for rect, fmt in zip(fmts, (None, "clap", "lv2", "vst3")):
            self.button(p, rect, (fmt or "all").upper(), lambda fmt=fmt: self.set_browser_fmt(fmt),
                        on=self.browser_fmt == fmt)
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
        # A-Z strip: tap or slide along it to jump; the letter of the top of the list is lit
        self.box(p, letters, PANEL, BORDER, radius=8)
        current = self.letter(items[min(first, len(items) - 1)])
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

    def jump(self, items, ch, rh):
        """Scroll the list to the first plugin at or after letter `ch` ('#' = digits and symbols)."""
        want = self.LETTERS.index(ch)
        n = next((n for n, ref in enumerate(items) if self.LETTERS.index(self.letter(ref)) >= want), len(items) - 1)
        self.browser_scroll = n * rh
