"""Dialog panels: the project chooser (start screen and FILE), the settings (project key/scale, audio
output), and the two MIDI ones: a new track (drum, melody or external MIDI) and an external track's output."""
import json
import math
import shutil
import time

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor

import plugins

from .constants import BG, BORDER, DIM, NOTE_NAMES, PANEL, TEXT, WARN
from .engine import midi_outputs
from .gestures import Hold, ScrollOrTap
from .model import dump_project, free_project_path, new_project, project_files, save_text
from .music import SCALES
from .ui import Panel, split


class Chooser(Panel):
    """Projects, most recently changed first: the start screen, and the FILE dialog (which also marks
    the `current` project and offers to `duplicate` it). Tap opens, NEW starts an empty one, DELETE (hold)
    removes a project with its plugin states."""

    def __init__(self, folder, open_, current=None, duplicate=None):
        super().__init__()
        self.folder, self.open, self.current, self.duplicate = folder, open_, current, duplicate
        self.scroll = 0.0
        self.info = {}  # path -> (mtime, summary line)

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
        path = free_project_path(self.folder, "Project")
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


def draw_ports(panel, p, r, ports, get_scroll, set_scroll, pick, empty="no MIDI outputs found"):
    """A scrollable list of MIDI outputs, a row each: tapping a row calls pick(name). The scroll offset lives
    in the panel, hence the get/set pair; an empty name renders as "no port yet", for choosing one later."""
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
        panel.box(p, row, PANEL, BORDER, radius=6)
        panel.text(p, row.adjusted(14 * s, 0, -14 * s, 0),
                   label or "NO PORT — choose one later", TEXT if port else DIM, 16,
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
                   lambda v: setattr(self, "scroll", v), self.choose)


class AudioSources(MidiPorts):
    """The input an audio track takes its sound from: one of the interface's capture ports."""
    TITLE = "AUDIO INPUT"

    def __init__(self, choose, current="", list_ports=plugins.audio_input_choices):
        super().__init__(choose, current, list_ports)
