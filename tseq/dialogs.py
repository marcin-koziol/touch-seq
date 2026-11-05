"""Dialog panels: the project chooser (start screen and FILE), the settings (project key/scale, audio
output), and the two MIDI ones: a new track (drum, melody or external MIDI) and an external track's output."""
import json
import math
import shutil
import time

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor

import plugins

from .constants import (ARP_GATES, ARP_MODES, ARP_RATES, BG, BORDER, BPM_MAX, BPM_MIN, DIM, NOTE_NAMES, PANEL,
                        PLAY, TEXT, WARN)
from .engine import midi_outputs
from .midiin import midi_inputs
from .gestures import Drag, Hold, ScrollOrTap
from .model import dump_project, named_project_path, new_project, project_files, save_text
from .music import SCALES
from .ui import Keyboard, Panel, split


class Chooser(Panel):
    """Projects, most recently changed first: the start screen, and the FILE dialog (which also marks
    the `current` project and offers to `duplicate` it). Tap opens, NEW names and starts an empty one,
    DELETE (hold) removes a project with its plugin states."""

    def __init__(self, folder, open_, current=None, duplicate=None):
        super().__init__()
        self.folder, self.open, self.current, self.duplicate = folder, open_, current, duplicate
        self.scroll = 0.0
        self.info = {}  # path -> (mtime, summary line)
        self.keyboard = None  # naming a new project

    def summary(self, path, mtime):
        if self.info.get(path, (None,))[0] != mtime:
            try:
                d = json.loads(path.read_text())
                line = f"{len(d['tracks'])} tracks  ·  {d['bpm']} BPM"
            except (OSError, ValueError, KeyError):
                line = "can't read"
            self.info[path] = (mtime, line)
        return self.info[path][1]

    def new(self):
        self.keyboard = Keyboard("", "New project", self.create, lambda: setattr(self, "keyboard", None),
                                 placeholder="Project")

    def create(self, name):
        self.keyboard = None
        path = named_project_path(self.folder, name or "Project")
        save_text(path, dump_project(new_project()))
        self.open(path)

    def delete(self, path):
        path.unlink(missing_ok=True)
        shutil.rmtree(path.with_suffix(".plugins"), ignore_errors=True)

    def draw(self, p):
        s, g = self.s, 16 * self.s
        p.fillRect(self.rect(), QColor(BG))
        header = QRectF(g, g, self.w - 2 * g, 70 * s)
        rects = split(header, [0] + [200 * s] * (2 if self.duplicate else 1), 12 * s)
        title, new = rects[0], rects[-1]
        self.text(p, title, "Projects", TEXT, 30, Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        if self.duplicate:
            self.button(p, rects[1], "DUPLICATE", self.duplicate, sub="continue in a copy")
        self.button(p, new, "NEW", self.new, on=True, sub="empty project")
        listing = QRectF(g, header.bottom() + 16 * s, self.w - 2 * g, self.h - header.bottom() - 16 * s - g)
        files = project_files(self.folder)
        rh = 84 * s
        self.scroll = max(0.0, min(self.scroll, len(files) * rh - listing.height()))
        held = next((x for x in self.gestures.values() if isinstance(x, Hold)), None)
        p.save()
        p.setClipRect(listing)
        for n, path in enumerate(files):
            row = QRectF(listing.left(), listing.top() + n * rh - self.scroll, listing.width(), rh - 8 * s)
            if not row.intersects(listing):
                continue
            mtime = path.stat().st_mtime
            lit = path == self.current if self.current else n == 0  # the open project, else the newest
            self.box(p, row, "#1b2130" if lit else PANEL, "#38d6ff" if lit else BORDER, 2 if lit else 1.5, radius=10)
            info, delete = split(row.adjusted(18 * s, 8 * s, -8 * s, -8 * s), [0, 130 * s], 12 * s)
            name = path.stem + ("   ·   open now" if path == self.current else "")
            self.text(p, info, name, TEXT, 22, Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
            self.text(p, info, f"{time.strftime('%d.%m.%Y %H:%M', time.localtime(mtime))}     "
                               f"{self.summary(path, mtime)}", DIM, 14, Qt.AlignRight | Qt.AlignVCenter)
            self.region(row.intersected(listing), lambda pos, path=path: ScrollOrTap(
                pos, lambda: self.scroll, lambda v: setattr(self, "scroll", v), lambda: self.open(path)))
            if path == self.current:  # the open project can't be deleted from under itself
                continue
            self.button(p, delete, "DELETE", lambda: None, color=WARN, sub="hold")
            if held and held.key == path:
                p.save()
                p.setClipRect(QRectF(delete.left(), delete.top(), delete.width() * held.progress(), delete.height()))
                self.box(p, delete, QColor(255, 93, 93, 140), None)
                p.restore()
            self.region(delete.intersected(listing), lambda pos, path=path: Hold(lambda: self.delete(path), key=path))
        p.restore()
        if not files:
            self.text(p, listing, "no projects yet — NEW starts one", "#5b6577", 18)
        if self.keyboard:
            self.keyboard.draw(self, p, QRectF(g, g, self.w - 2 * g, self.h - 2 * g))


class Settings(Panel):
    """Settings in two tabs: PROJECT (key and scale; the piano roll dims notes outside the scale) and
    AUDIO (where the plugin host plays: the system's default output, followed when it changes, or a
    chosen one)."""

    def __init__(self, project, host=None):
        super().__init__()
        self.project, self.host = project, host
        self.tab = "project"
        self.outputs = plugins.sinks()
        self.scroll = 0.0
        self.quantum = plugins.quantum()  # (forced, default, min, max, rate) of the audio graph
        self.age = 0.0

    def tick(self):
        super().tick()
        if self.tab != "audio" or time.monotonic() - self.age < 1.0:
            return
        self.age = time.monotonic()
        quantum = plugins.quantum()  # another program may have changed the size meanwhile
        if quantum != self.quantum:
            self.quantum = quantum
            self.invalidate()

    def draw(self, p):
        s, g = self.s, 20 * self.s
        p.fillRect(self.rect(), QColor(BG))
        tabs = split(QRectF(g, g, 400 * s, 52 * s), [0, 0], 8 * s)
        for r, tab in zip(tabs, ("project", "audio")):
            self.button(p, r, tab.upper(), lambda tab=tab: setattr(self, "tab", tab), on=self.tab == tab)
        body = QRectF(g, tabs[0].bottom() + 16 * s, self.w - 2 * g, self.h - tabs[0].bottom() - 16 * s - g)
        (self.draw_project if self.tab == "project" else self.draw_audio)(p, body)

    def title(self, p, r, text):
        self.text(p, r, text, TEXT, 22, Qt.AlignLeft | Qt.AlignVCenter, bold=True)

    def draw_project(self, p, r):
        s, pr = self.s, self.project
        y = r.top()
        self.title(p, QRectF(r.left(), y, r.width(), 34 * s), "Key")
        y += 40 * s
        for n, b in enumerate(split(QRectF(r.left(), y, r.width(), 58 * s), [0] * 12, 8 * s)):
            self.button(p, b, NOTE_NAMES[n], lambda n=n: setattr(pr, "key", n), on=pr.key == n)
        y += 76 * s
        self.title(p, QRectF(r.left(), y, r.width(), 34 * s), "Scale")
        y += 40 * s
        names, cols, rh = list(SCALES), 4, 56 * s
        for row in range(math.ceil(len(names) / cols)):
            rects = split(QRectF(r.left(), y + row * (rh + 8 * s), r.width(), rh), [0] * cols, 8 * s)
            for name, b in zip(names[row * cols:(row + 1) * cols], rects):
                self.button(p, b, name.upper(), lambda name=name: setattr(pr, "scale", name), on=pr.scale == name)
        notes = "  ".join(NOTE_NAMES[(pr.key + i) % 12] for i in SCALES[pr.scale])
        self.text(p, QRectF(r.left(), r.bottom() - 36 * s, r.width(), 36 * s), f"{pr.key_name}:   {notes}", DIM, 18,
                  Qt.AlignLeft | Qt.AlignVCenter)

    def draw_buffer(self, p, r):
        """The audio graph's buffer size: what it runs at now, and the sizes to force it to (0 = system).
        Returns the y where the rest of the tab starts."""
        s = self.s
        force, default, lo, hi, rate = self.quantum
        now = force or default
        title = QRectF(r.left(), r.top(), r.width(), 34 * s)
        self.title(p, title, "Buffer size")
        if now:
            self.text(p, title, f"now: {now} samples · {now / (rate or 48000) * 1000:.1f} ms", DIM, 16,
                      Qt.AlignRight | Qt.AlignVCenter)
        y = title.bottom() + 6 * s
        sizes = (0, 64, 128, 256, 512, 1024, 2048)
        for size, b in zip(sizes, split(QRectF(r.left(), y, r.width(), 56 * s), [0] * len(sizes), 8 * s)):
            self.button(p, b, "SYSTEM" if size == 0 else str(size), lambda size=size: self.set_buffer(size),
                        on=size == force or (not force and size == 0))
        y += 68 * s
        if force:
            note = f"forced for the whole audio graph while set (the system's own default is {default})"
        elif lo and lo == hi:
            note = (f"the system's default {default}, pinned by min-quantum = max-quantum; picking a size here "
                    f"overrides it while set")
        elif lo:
            note = f"the system's default; it allows {lo} to {hi} samples"
        else:
            note = "PipeWire is not answering, so the sizes are set through pw-metadata"
        self.text(p, QRectF(r.left(), y, r.width(), 24 * s), note, DIM, 14, Qt.AlignLeft | Qt.AlignVCenter, elide=True)
        return y + 34 * s

    def set_buffer(self, size):
        """The plugin host saves the choice and applies it to the graph; on its own, straight to PipeWire."""
        if self.host and hasattr(self.host, "set_buffer"):
            self.host.set_buffer(size)
        else:
            plugins.set_quantum(size)
        self.quantum = plugins.quantum()

    def draw_audio(self, p, r):
        s, host = self.s, self.host
        y = self.draw_buffer(p, r)
        if not host or not host.host:
            self.text(p, QRectF(r.left(), y, r.width(), 34 * s), host.message if host else "plugin host unavailable",
                      WARN, 18)
            return
        self.title(p, QRectF(r.left(), y, r.width(), 34 * s), "Plugin output")
        listing = QRectF(r.left(), y + 44 * s, r.width(), r.bottom() - y - 44 * s)
        names = dict(self.outputs)
        current = plugins.default_sink()
        rows = [("default", "SYSTEM DEFAULT", f"follows the system  ·  now: {names.get(current, current or '-')}")]
        rows += [(name, desc, name) for name, desc in self.outputs]
        rh = 74 * s
        self.scroll = max(0.0, min(self.scroll, len(rows) * rh - listing.height()))
        p.save()
        p.setClipRect(listing)
        for n, (name, label, sub) in enumerate(rows):
            row = QRectF(listing.left(), listing.top() + n * rh - self.scroll, listing.width(), rh - 8 * s)
            on = host.audio_out == name
            self.box(p, row, "#1b2130" if on else PANEL, "#38d6ff" if on else BORDER, 2 if on else 1.5, radius=10)
            inner = row.adjusted(18 * s, 6 * s, -18 * s, -6 * s)
            self.text(p, inner.adjusted(0, 0, 0, -inner.height() * 0.45), label, TEXT, 18,
                      Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
            self.text(p, inner.adjusted(0, inner.height() * 0.5, 0, 0), sub, DIM, 13, Qt.AlignLeft | Qt.AlignVCenter,
                      elide=True)
            self.region(row.intersected(listing), lambda pos, name=name: ScrollOrTap(
                pos, lambda: self.scroll, lambda v: setattr(self, "scroll", v), lambda: host.set_output(name)))
        p.restore()


def draw_ports(panel, p, r, ports, get_scroll, set_scroll, pick, empty="no MIDI outputs found", current="",
               none="NO PORT — choose one later"):
    """A scrollable list of ports, a row each: tapping a row calls pick(name). The scroll offset lives in the
    panel, hence the get/set pair; an empty name renders as "no port yet", for choosing one later, and the
    row of `current` is lit."""
    s = panel.s
    panel.box(p, r, "#11141b", BORDER, radius=8)
    if not ports:
        panel.text(p, r.adjusted(16 * s, 0, -16 * s, 0), empty, DIM, 14)
        return
    rh = 52 * s
    scroll = max(0.0, min(get_scroll(), len(ports) * rh - r.height()))
    set_scroll(scroll)
    p.save()
    p.setClipRect(r)
    first = int(scroll // rh)
    for n in range(first, min(len(ports), first + int(r.height() / rh) + 2)):
        entry = ports[n]
        port, label = entry if isinstance(entry, tuple) else (entry, entry)  # (port, longer label) or a name
        row = QRectF(r.left() + 6 * s, r.top() + 4 * s + n * rh - scroll, r.width() - 12 * s, rh - 8 * s)
        used = bool(port) and bool(current) and current.lower() in port.lower()
        panel.box(p, row, PANEL, "#4ade80" if used else BORDER, 2 if used else 1, radius=6)
        panel.text(p, row.adjusted(14 * s, 0, -14 * s, 0),
                   label or none, "#4ade80" if used else (TEXT if port else DIM), 16,
                   Qt.AlignLeft | Qt.AlignVCenter, bold=bool(port), elide=True)
        panel.region(row.intersected(r), lambda pos, port=port: ScrollOrTap(
            pos, get_scroll, set_scroll, lambda: pick(port)))
    p.restore()


class NewTrack(Panel):
    """The sequencer's "+": a drum track, a melody track, or an external one that sends to a hardware synth.
    External tracks have no plugins, so that choice asks for the MIDI output first."""

    KINDS = [("drum", "DRUM", "one voice per pad, 12 drum lanes"),
             ("melody", "MELODY", "piano roll, its own instrument"),
             ("external", "EXTERNAL", "no plugins, out to a MIDI port"),
             ("audio", "AUDIO", "an input port, through the rack")]

    def __init__(self, add, list_ports=midi_outputs, list_inputs=plugins.audio_input_choices):
        super().__init__()
        self.add = add  # (kind, port) -> None: the rack creates the track and closes this dialog
        self.list_ports, self.list_inputs = list_ports, list_inputs
        self.picking = ""  # "external" or "audio" while choosing the port
        self.ports = []
        self.scroll = 0.0

    def choose(self, kind):
        if kind not in ("external", "audio"):
            self.add(kind, "")
            return
        self.picking, self.scroll = kind, 0.0
        self.rescan()

    def rescan(self):
        ports = self.list_inputs() if self.picking == "audio" else self.list_ports()
        self.ports = ports + [""]  # last row: no port yet, pick one later (EDIT / HOST-4)

    def back(self):
        self.picking = ""

    def draw(self, p):
        s, w, h, g = self.s, self.w, self.h, 14 * self.s
        p.fillRect(self.rect(), QColor(BG))
        if self.picking:
            head = QRectF(g, g, w - 2 * g, 54 * s)
            back, title, again = split(head, [150 * s, 0, 130 * s], 12 * s)
            self.button(p, back, "‹ BACK", self.back, sub="track kind")
            heading = "EXTERNAL MIDI — choose the output" if self.picking == "external" else "AUDIO — choose the input"
            self.text(p, title.adjusted(8 * s, 0, 0, 0), heading, TEXT, 17,
                      Qt.AlignLeft | Qt.AlignVCenter, bold=True)
            self.button(p, again, "RESCAN", self.rescan, sub="plugged in?")
            area = QRectF(g, head.bottom() + 12 * s, w - 2 * g, h - head.bottom() - 12 * s - g)
            draw_ports(self, p, area, self.ports, lambda: self.scroll, lambda v: setattr(self, "scroll", v),
                       lambda port: self.add(self.picking, port))
            return
        cards = split(QRectF(g, g, w - 2 * g, h - 2 * g - 64 * s), [0] * len(self.KINDS), 14 * s)
        for rect, (kind, label, desc) in zip(cards, self.KINDS):
            self.box(p, rect, PANEL, BORDER, radius=10)
            self.text(p, rect.adjusted(0, 0, 0, 18 * s), label, TEXT, 26 if len(self.KINDS) > 3 else 30,
                      Qt.AlignCenter, bold=True)
            self.text(p, rect.adjusted(14 * s, 0, -14 * s, -26 * s), desc, DIM, 14,
                      Qt.AlignHCenter | Qt.AlignBottom, elide=True)
            self.on_press(rect, lambda kind=kind: self.choose(kind))
        self.text(p, QRectF(g, h - g - 46 * s, w - 2 * g, 46 * s),
                  "Melody and drum tracks play MIDI into HOST-4 plugins; external sends MIDI to hardware; "
                  "audio takes an interface input through the rack.",
                  DIM, 14, Qt.AlignLeft | Qt.AlignVCenter, elide=True)


class MidiPorts(Panel):
    """A port a track sends to or takes from: chosen when the track is created, changeable afterwards (HOST-4)."""

    TITLE = "MIDI OUTPUT"

    def __init__(self, choose, current="", list_ports=midi_outputs):
        super().__init__()
        self.choose, self.current = choose, current  # choose(name) -> None: the rack saves and closes
        self.list_ports = list_ports
        self.scroll = 0.0
        self.rescan()

    def rescan(self):
        self.ports = self.list_ports() + [""]  # the empty row: no output (silent)

    def draw(self, p):
        s, w, h, g = self.s, self.w, self.h, 14 * self.s
        p.fillRect(self.rect(), QColor(BG))
        head = QRectF(g, g, w - 2 * g, 54 * s)
        title, now, again = split(head, [0, 300 * s, 130 * s], 12 * s)
        self.text(p, title, self.TITLE, TEXT, 20, Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        self.text(p, now, f"now: {self.current or 'none'}", DIM, 13, Qt.AlignRight | Qt.AlignVCenter, elide=True)
        self.button(p, again, "RESCAN", self.rescan, sub="plugged in?")
        area = QRectF(g, head.bottom() + 12 * s, w - 2 * g, h - head.bottom() - 12 * s - g)
        draw_ports(self, p, area, self.ports, lambda: self.scroll,
                   lambda v: setattr(self, "scroll", v), self.choose, current=self.current)


class Tempo(Panel):
    """The project's tempo, opened from the top bar's BPM: drag the number, nudge it by 1/5/10, or tap it out."""

    def __init__(self, project, done=lambda: None):
        super().__init__()
        self.project = project
        self.done = done  # the rack: close this dialog
        self.taps = []  # tap-tempo times

    def set_bpm(self, v):
        self.project.bpm = int(max(BPM_MIN, min(BPM_MAX, v)))

    def tap_tempo(self):
        now = time.monotonic()
        if self.taps and now - self.taps[-1] > 2:  # a long pause starts a new measurement
            self.taps = []
        self.taps = (self.taps + [now])[-5:]
        if len(self.taps) >= 2:
            self.set_bpm(round(60 * (len(self.taps) - 1) / (self.taps[-1] - self.taps[0])))

    def draw(self, p):
        s, w, h = self.s, self.w, self.h
        p.fillRect(self.rect(), QColor(BG))
        col = QRectF(0, 0, min(620 * s, w - 40 * s), min(560 * s, h - 32 * s))
        col.moveCenter(QRectF(0, 0, w, h).center())
        self.text(p, QRectF(col.left(), col.top(), col.width(), 20 * s), "TEMPO", DIM, 13,
                  Qt.AlignHCenter | Qt.AlignTop, bold=True)
        big = QRectF(col.left(), col.top() + 26 * s, col.width(), 180 * s)
        self.box(p, big, PANEL, BORDER, radius=14)
        self.text(p, big.adjusted(0, -12 * s, 0, 0), str(self.project.bpm), TEXT, 76, bold=True)
        self.text(p, big.adjusted(0, 0, 0, -12 * s), "drag ↕", "#5b6577", 12, Qt.AlignHCenter | Qt.AlignBottom)
        self.region(big, lambda pos: Drag(pos, lambda: self.project.bpm, self.set_bpm, BPM_MIN, BPM_MAX, 4 * s))
        steps = QRectF(col.left(), big.bottom() + 16 * s, col.width(), 78 * s)
        for d, r in zip((-10, -5, -1, 1, 5, 10), split(steps, [0] * 6, 10 * s)):
            self.button(p, r, f"{d:+d}", lambda d=d: self.set_bpm(self.project.bpm + d))
        rest = QRectF(col.left(), steps.bottom() + 14 * s, col.width(),
                      min(100 * s, col.bottom() - steps.bottom() - 14 * s))
        tap, done = split(rest, [0, 170 * s], 12 * s)
        self.button(p, tap, "TAP", self.tap_tempo, color=PLAY,
                    pending=bool(self.taps) and time.monotonic() - self.taps[-1] < 2, sub="tap tempo")
        self.button(p, done, "DONE", self.done, on=True)


class ArpSettings(Panel):
    """A track's arpeggiator: the chord it hears played one note at a time, on the transport's grid. Not
    destructive: the pattern keeps its chords, switching it off plays them again. Taps apply at once."""

    def __init__(self, track, changed=lambda: None):
        super().__init__()
        self.track, self.changed = track, changed  # changed() -> None: the rack saves

    @property
    def arp(self):
        return self.track.arp

    def set(self, **values):
        for key, value in values.items():
            setattr(self.arp, key, value)
        self.changed()

    def rate_name(self):
        return next((name for ticks, name in ARP_RATES if ticks == self.arp.rate), "1/16")

    def draw(self, p):
        s, w, h = self.s, self.w, self.h
        a = self.arp
        p.fillRect(self.rect(), QColor(BG))
        g = 16 * s
        inner = QRectF(g, g, w - 2 * g, h - 2 * g)
        self.text(p, QRectF(inner.left(), inner.top(), inner.width(), 30 * s),
                  f"ARPEGGIATOR · track T{self.track.number}", TEXT, 17, Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        self.text(p, QRectF(inner.left(), inner.top() + 30 * s, inner.width(), 24 * s),
                  "the chord it hears, one note at a time - the pattern keeps its chords (nothing to undo)", DIM, 12,
                  Qt.AlignLeft | Qt.AlignVCenter, elide=True)
        on = QRectF(inner.left(), inner.top() + 64 * s, 260 * s, 62 * s)
        self.button(p, on, "ARP ON" if a.on else "ARP OFF", lambda: self.set(on=not a.on), on=a.on,
                    color="#4ade80", sub="tap to switch")
        self.text(p, QRectF(on.right() + 20 * s, on.top(), inner.right() - on.right() - 20 * s, on.height()),
                  "the notes land on the transport's grid, not the key's: RATE is the distance, GATE how much sounds",
                  DIM, 12, Qt.AlignLeft | Qt.AlignVCenter, elide=True)

        def label(text, y):
            self.text(p, QRectF(inner.left(), y, 120 * s, 22 * s), text, DIM, 11,
                      Qt.AlignLeft | Qt.AlignVCenter, bold=True)

        y = on.bottom() + 22 * s
        label("RATE", y)
        rates = split(QRectF(inner.left(), y + 22 * s, inner.width(), 58 * s), [0] * len(ARP_RATES), 8 * s)
        for rect, (ticks, name) in zip(rates, ARP_RATES):
            self.button(p, rect, name, lambda ticks=ticks: self.set(rate=ticks), on=a.rate == ticks)
        y += 92 * s
        label("MODE", y)
        modes = split(QRectF(inner.left(), y + 22 * s, inner.width(), 58 * s), [0] * len(ARP_MODES), 8 * s)
        for rect, (mode, name) in zip(modes, ARP_MODES):
            self.button(p, rect, name, lambda mode=mode: self.set(mode=mode), on=a.mode == mode)
        y += 92 * s
        octs = split(QRectF(inner.left(), y, inner.width(), 58 * s), [0] * 4, 8 * s)
        for j, n in enumerate((1, 2, 3)):
            self.button(p, octs[j], f"{n} OCT", lambda n=n: self.set(octaves=n), on=a.octaves == n)
        self.button(p, octs[3], "LATCH", lambda: self.set(latch=not a.latch), on=a.latch, sub="keeps the chord")
        y += 74 * s
        label("GATE", y)
        gates = split(QRectF(inner.left(), y + 22 * s, inner.width(), 54 * s), [0] * len(ARP_GATES), 8 * s)
        for rect, gate in zip(gates, ARP_GATES):
            self.button(p, rect, f"{round(gate * 100)}%", lambda gate=gate: self.set(gate=gate),
                        on=abs(a.gate - gate) < 0.01)

        # what it will play for a held C major chord: the mode's order and the octaves, at a glance
        y += 90 * s
        sequence = a.order([60, 64, 67])
        self.text(p, QRectF(inner.left(), y, inner.width(), 22 * s),
                  f"WHAT A C-E-G CHORD PLAYS · {len(sequence)} STEPS" +
                  (" · each one picked at random" if a.mode == "random" else ""), DIM, 11,
                  Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        strip = QRectF(inner.left(), y + 22 * s, inner.width(), max(56 * s, inner.bottom() - (y + 22 * s)))
        self.box(p, strip, PANEL)
        shown = sequence[:16]
        area = strip.adjusted(8 * s, 8 * s, -8 * s, -8 * s)
        cells = split(area, [0] * max(1, len(shown)), 5 * s)
        lo, hi = min(sequence), max(sequence)
        for rect, n in zip(cells, shown):
            tall = rect.height() * (0.45 + 0.55 * (0.5 if hi == lo else (n - lo) / (hi - lo)))
            block = QRectF(rect.left(), rect.bottom() - tall, rect.width(), tall)
            self.box(p, block, QColor("#38d6ff") if a.on else QColor("#3a4252"), None, radius=3)
            self.text(p, block, NOTE_NAMES[n % 12] + str(n // 12 - 1), BG if a.on else DIM, 11, bold=True)
        if len(sequence) > len(shown):
            self.text(p, strip.adjusted(0, 0, -8 * s, 0), f"+{len(sequence) - len(shown)}", DIM, 11,
                      Qt.AlignRight | Qt.AlignVCenter)



class MidiInputs(Panel):
    """What a MIDI track listens to: one of the system's inputs and the channel it takes (ALL or one of 16).
    Every tap applies at once, so the sequencer's socket lights up as you go; the faceplate's x closes."""

    def __init__(self, apply, port="", channel=0, list_ports=midi_inputs):
        super().__init__()
        self.apply, self.port, self.channel = apply, port, channel  # apply(port, channel) -> None
        self.list_ports = list_ports
        self.scroll = 0.0
        self.rescan()

    def rescan(self):
        self.ports = self.list_ports() + [""]  # the empty row: no input, the track plays its patterns only

    def pick(self, port):
        self.port = port
        self.apply(port, self.channel)

    def take(self, channel):
        self.channel = channel
        self.apply(self.port, channel)

    def draw(self, p):
        s, w, h, g = self.s, self.w, self.h, 14 * self.s
        p.fillRect(self.rect(), QColor(BG))
        head = QRectF(g, g, w - 2 * g, 54 * s)
        title, now, again = split(head, [0, 340 * s, 130 * s], 12 * s)
        self.text(p, title, "MIDI INPUT", TEXT, 20, Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        self.text(p, now, f"now: {self.port or 'none'}", DIM, 13, Qt.AlignRight | Qt.AlignVCenter, elide=True)
        self.button(p, again, "RESCAN", self.rescan, sub="plugged in?")
        chans = split(QRectF(g, head.bottom() + 12 * s, w - 2 * g, 50 * s), [0] * 17, 6 * s)
        for n, rect in enumerate(chans):
            self.button(p, rect, "ALL" if not n else str(n), lambda n=n: self.take(n), on=self.channel == n)
        area = QRectF(g, chans[0].bottom() + 12 * s, w - 2 * g, h - chans[0].bottom() - 12 * s - g)
        draw_ports(self, p, area, self.ports, lambda: self.scroll, lambda v: setattr(self, "scroll", v),
                   self.pick, empty="no MIDI inputs found", current=self.port,
                   none="NONE — the track plays only its patterns")


class AudioSources(MidiPorts):
    """The input an audio track takes its sound from: one of the interface's capture ports."""
    TITLE = "AUDIO INPUT"

    def __init__(self, choose, current="", list_ports=plugins.audio_input_choices):
        super().__init__(choose, current, list_ports)
