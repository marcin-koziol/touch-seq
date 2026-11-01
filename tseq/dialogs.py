"""Dialog panels: the project chooser (start screen and FILE) and the settings (project key/scale, audio
output)."""
import json
import math
import shutil
import time

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor

import plugins

from .constants import BG, BORDER, DIM, NOTE_NAMES, PANEL, TEXT, WARN
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

    def draw_audio(self, p, r):
        s, host = self.s, self.host
        if not host or not host.host:
            self.text(p, r, host.message if host else "plugin host unavailable", WARN, 18)
            return
        self.title(p, QRectF(r.left(), r.top(), r.width(), 34 * s), "Plugin output")
        listing = QRectF(r.left(), r.top() + 44 * s, r.width(), r.height() - 44 * s)
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
