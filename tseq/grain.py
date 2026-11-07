"""The Grain editor: its own sample and slice, a grain cloud, and the synth's knobs. Drawn in HOST-4's
parameter area when a Touch-Seq Grain plugin is selected; the plugin itself has no window of its own."""
import math
import threading
import time
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QFileDialog

from .constants import BORDER, DIM, PANEL, TEXT, WARN
from .gestures import Gesture
from .sampler import import_sample
from .wav import peaks_range, read_wav

PLAY = "#4ade80"


def grain_dir(state_dir):
    return Path(state_dir) / "grain"


def grain_manifest(state_dir, slot):
    return grain_dir(state_dir) / f"grain-{int(slot)}.txt"


class GrainSliceDrag(Gesture):
    """A white handle on the Grain editor's waveform: the slice the grains read."""

    def __init__(self, editor, edge, rect, pos):
        self.editor, self.edge, self.rect = editor, edge, rect
        self.move(pos)

    def move(self, pos):
        self.editor.set_slice(self.edge, self.rect, pos)

    def release(self, pos):
        self.editor.panel.invalidate()


class GrainEditor:
    """The grain synth's source: one WAV, a start/end slice, a position/spray band and the knobs. LOAD imports
    a file into the project's grain folder; it does not touch the sampler's pads."""

    def __init__(self, panel):
        self.panel = panel
        self.plugin = None
        self.slot = 0
        self.name = ""
        self.start = 0
        self.end = 0
        self.samples = None
        self.rate = 48000
        self.frames = 0
        self.peak_cache = {}
        self.cache = {}
        self.loading = False
        self._token = 0
        self._loaded = None
        self._lock = threading.Lock()
        self._loading_name = None
        self.seen = None  # (manifest path, mtime)
        self.wave_text = "LOAD a WAV to see the waveform"

    # --- source -----------------------------------------------------------------
    def param(self, name):
        if not self.plugin:
            return None
        return next((q for q in self.plugin.params if q.name == name), None)

    def value(self, name, default):
        prm = self.param(name)
        host = getattr(self.panel, "host", None)
        if prm is None or host is None:
            return default
        try:
            return host.value(self.plugin, prm.index)
        except Exception:
            return default

    def set_param(self, name, value):
        prm = self.param(name)
        if prm is not None:
            try:
                self.panel.set_param(self.plugin, prm.index, value, source="grain")
            except Exception:
                pass

    def visible(self):
        sel = getattr(self.panel, "sel", None)
        loaded = getattr(self.panel, "loaded", {})
        if not self.plugin or (sel is not None and loaded.get(sel) is not self.plugin):
            return False
        if getattr(self.panel, "view", "track") != "track" or getattr(self.panel, "browser", None):
            return False
        return not isinstance(getattr(self.panel, "assigning", None), tuple)

    def use(self, plugin, slot):
        if plugin is not self.plugin or slot != self.slot:
            self.plugin, self.slot = plugin, slot
            self.seen = None
            self.refresh(force=True)

    def refresh(self, force=False):
        if not self.plugin:
            return
        slot = int(round(self.value("Slot", self.slot)))
        path = grain_manifest(self.panel.state_dir, slot)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        key = (str(path), mtime)
        if not force and key == self.seen:
            return
        self.seen = key
        self.slot = slot
        name = ""
        try:
            for line in path.read_text().splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[0] == "sample":
                    name = parts[1].strip()
                    break
        except OSError:
            pass
        self.name = name
        if not name:
            self.samples, self.frames = None, 0
            self.wave_text = "LOAD a WAV to see the waveform"
            return
        cached = self.cache.get(name)
        if cached:
            self.samples, self.rate, self.frames = cached
            self.loading = False
        elif name != self._loading_name:
            self.start_load(name)

    def start_load(self, name):
        self._loading_name = name
        self.loading = True
        token = self._token = self._token + 1
        folder = grain_dir(self.panel.state_dir)
        panel = self.panel

        def work():
            try:
                data = read_wav(folder / name)
            except Exception:
                data = None
            with self._lock:
                self._loaded = (token, name, data)
            panel.invalidate()

        threading.Thread(target=work, daemon=True).start()

    def apply_loaded(self):
        with self._lock:
            pending, self._loaded = self._loaded, None
        if not pending:
            return
        token, name, data = pending
        if token != self._token:
            return
        self.loading = False
        if data:
            self.cache[name] = data
            if name == self.name:
                self.samples, self.rate, self.frames = data
        self.panel.invalidate()

    def poll(self):
        if not self.plugin or not self.visible():
            return
        self.apply_loaded()
        self.refresh()

    def load_sample(self):
        """LOAD: import a file into the project and make it this Grain's source."""
        path, _ = QFileDialog.getOpenFileName(None, "Load sample", "",
                                              "Audio (*.wav *.aif *.aiff *.flac *.mp3 *.ogg);;All files (*)")
        if path:
            self.use_sample(path)

    def use_sample(self, path):
        slot = int(round(self.value("Slot", self.slot)))
        try:
            name = import_sample(path, self.panel.state_dir, "grain")
            samples, rate, frames = read_wav(grain_dir(self.panel.state_dir) / name)
            self.cache[name] = (samples, rate, frames)
            folder = grain_dir(self.panel.state_dir)
            folder.mkdir(parents=True, exist_ok=True)
            grain_manifest(self.panel.state_dir, slot).write_text(f"sample {name}\n")
            self.slot, self.name = slot, name
            self.samples, self.rate, self.frames = samples, rate, frames
            self.start, self.end = 0, frames
            self.set_param("Start", 0.0)
            self.set_param("End", 1.0)
            self.seen = None
            self.reload_source()
            self.panel.message = f"grain: loaded {name}"
            self.panel.invalidate()
        except Exception as error:
            self.panel.message = f"grain: {error}"
            self.panel.invalidate()

    def reload_source(self):
        prm = self.param("Reload")
        host = getattr(self.panel, "host", None)
        if prm is not None and host is not None:
            try:
                self.panel.set_param(self.plugin, prm.index,
                                     host.value(self.plugin, prm.index) + 1.0, source="grain")
            except Exception:
                pass
        self.seen = None
        self.refresh(force=True)

    # --- slice ------------------------------------------------------------------
    def slice_bounds(self):
        if not self.frames:
            return 0, 0
        start = max(0.0, min(1.0, float(self.value("Start", 0.0))))
        end = max(0.0, min(1.0, float(self.value("End", 1.0))))
        a = int(start * self.frames)
        b = int(end * self.frames)
        if b <= a + 1:
            a, b = 0, self.frames
        return a, b

    def set_slice(self, edge, rect, pos):
        if not self.frames or not self.plugin:
            return
        part = max(0.0, min(1.0, (pos.x() - rect.left()) / max(1.0, rect.width())))
        frame = part * self.frames
        start = float(self.value("Start", 0.0))
        end = float(self.value("End", 1.0))
        gap = 2.0 / max(1, self.frames)
        if edge == "start":
            start = min(frame / self.frames, end - gap)
            start = max(0.0, start)
            self.set_param("Start", start)
            self.start = int(start * self.frames)
        else:
            end = max(frame / self.frames, start + gap)
            end = min(1.0, end)
            self.set_param("End", end)
            self.end = int(end * self.frames)
        self.panel.invalidate()

    # --- drawing ----------------------------------------------------------------
    def draw(self, p, r, plugin, slot):
        self.use(plugin, slot)
        s = self.panel.s
        header = QRectF(r.left(), r.top(), r.width(), 26 * s)
        title = f"SAMPLE  ·  {self.name}" if self.name else "SAMPLE  ·  none"
        self.panel.text(p, header.adjusted(2 * s, 0, -170 * s, 0), title, DIM if not self.name else TEXT, 12,
                        Qt.AlignLeft | Qt.AlignVCenter, elide=True)
        reload_btn = QRectF(header.right() - 84 * s, header.top(), 82 * s, 24 * s)
        load_btn = QRectF(reload_btn.left() - 78 * s, header.top(), 76 * s, 24 * s)
        self.panel.button(p, load_btn, "LOAD", self.load_sample)
        self.panel.button(p, reload_btn, "RELOAD", self.reload_source)
        wave = QRectF(r.left(), header.bottom() + 5 * s, r.width(), max(80 * s, (r.height() - 31 * s) * 0.42))
        self.draw_wave(p, wave)
        body = QRectF(r.left(), wave.bottom() + 8 * s, r.width(), r.bottom() - wave.bottom() - 8 * s)
        params = [q for q in plugin.params if q.name not in ("Slot", "Reload", "Start", "End")]
        if not params:
            return
        cols = 7
        rows = max(1, math.ceil(len(params) / cols))
        cw, ch = body.width() / cols, body.height() / rows
        for n, prm in enumerate(params):
            cell = QRectF(body.left() + (n % cols) * cw, body.top() + (n // cols) * ch,
                          cw - 4 * s, ch - 2 * s)
            self.panel.draw_knob(p, cell, plugin, prm)

    def draw_wave(self, p, r):
        s = self.panel.s
        self.panel.box(p, r, "#0e1116", BORDER, radius=8)
        if self.loading:
            self.panel.text(p, r, "reading sample…", DIM, 13)
            return
        if not self.samples or not self.frames:
            self.panel.text(p, r, self.wave_text, DIM, 13, elide=True)
            return
        start, end = self.slice_bounds()
        self.start, self.end = start, end
        span = max(1, end - start)
        view_start, view_end = 0, self.frames
        view_span = max(1, view_end - view_start)

        def x_of(frame):
            return r.left() + r.width() * (frame - view_start) / view_span

        x0, x1 = x_of(start), x_of(end)
        p.fillRect(QRectF(x0, r.top() + 1 * s, max(1.0, x1 - x0), r.height() - 2 * s),
                   QColor(56, 214, 255, 28))
        position = max(0.0, min(1.0, self.value("Position", 0.35)))
        spray = max(0.0, min(1.0, self.value("Spray", 0.08)))
        at = start + position * span
        band0 = start + max(0.0, position - spray) * span
        band1 = start + min(1.0, position + spray) * span
        p.fillRect(QRectF(x_of(band0), r.top() + 1 * s, max(1.0, x_of(band1) - x_of(band0)),
                          r.height() - 2 * s), QColor(250, 204, 21, 18))

        columns = max(1, int(r.width() / (2 * s)))
        key = (self.name, view_start, view_end, columns)
        values = self.peak_cache.get(key)
        if values is None:
            values = peaks_range(self.samples, view_start, view_end, columns)
            self.peak_cache[key] = values
        p.setPen(QPen(QColor("#38d6ff"), 1.1 * s))
        for n, (low, high) in enumerate(values):
            x = r.left() + (n + 0.5) * r.width() / max(1, len(values))
            p.drawLine(QPointF(x, r.center().y() - high * r.height() * 0.42),
                       QPointF(x, r.center().y() - low * r.height() * 0.42))

        density = max(1.0, self.value("Density", 30.0))
        size = max(0.005, self.value("Size", 0.08))
        count = max(6, min(140, int(density * size * 6) + 8))
        seed = (int(position * 1e6) ^ int(spray * 1e6) ^ int(size * 1e6) ^ int(density * 1000)
                ^ 0x9e3779b9) & 0xffffffff
        p.setPen(Qt.NoPen)
        for i in range(count):
            seed = (1664525 * seed + 1013904223) & 0xffffffff
            u1 = seed / 4294967296.0
            seed = (1664525 * seed + 1013904223) & 0xffffffff
            u2 = seed / 4294967296.0
            seed = (1664525 * seed + 1013904223) & 0xffffffff
            u3 = seed / 4294967296.0
            frame = start + (position + (u1 * 2.0 - 1.0) * spray) * span
            if not (view_start <= frame <= view_end):
                continue
            x = x_of(frame)
            y = r.center().y() + (u2 * 2.0 - 1.0) * r.height() * 0.34
            radius = (1.3 + 2.6 * u3) * s
            p.setBrush(QColor(250, 204, 21, 55 + int(110 * (1.0 - u3))))
            p.drawEllipse(QRectF(x - radius, y - radius, 2 * radius, 2 * radius))

        for edge, frame in (("start", start), ("end", end)):
            x = x_of(frame)
            p.setPen(QPen(QColor("#ffffff"), 2 * s))
            p.drawLine(QPointF(x, r.top() + 2 * s), QPointF(x, r.bottom() - 2 * s))
            p.setBrush(QColor("#ffffff"))
            p.setPen(QPen(QColor("#0e1116"), 1.5 * s))
            p.drawEllipse(QPointF(x, r.top() + 10 * s), 6 * s, 6 * s)
            self.panel.region(QRectF(x - 14 * s, r.top(), 28 * s, r.height()),
                              lambda pos, edge=edge: GrainSliceDrag(self, edge, r, pos))

        p.setPen(QPen(QColor("#facc15"), 1.6 * s))
        p.drawLine(QPointF(x_of(at), r.top() + 2 * s), QPointF(x_of(at), r.bottom() - 2 * s))
