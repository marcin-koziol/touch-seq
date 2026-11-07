"""The Grain editor: a sampler slice as the source, its grain cloud, and the synth's knobs. Drawn in HOST-4's
parameter area when a Touch-Seq Grain plugin is selected; the plugin itself has no window of its own."""
import math
import threading
import time
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPen

from .constants import BORDER, DIM, PANEL, TEXT, WARN
from .sampler import manifest_path, read_manifest
from .wav import peaks_range, read_wav

PLAY = "#4ade80"


class GrainEditor:
    """The source waveform and grain cloud above the plugin's parameters. The source is the sampler map of the
    Grain plugin's Slot/Pad; the sampler editor is still where samples are loaded and sliced."""

    def __init__(self, panel):
        self.panel = panel
        self.plugin = None
        self.slot = 0
        self.pad = 0
        self.pads = [None] * 16
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
        self.seen = None  # (manifest path, mtime, pad) of the last source read
        self.wave_text = "load a sample in the sampler, then pick its pad"

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
        pad = int(round(self.value("Pad", self.pad)))
        path = manifest_path(self.panel.state_dir, slot)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        key = (str(path), mtime, pad)
        if not force and key == self.seen:
            return
        self.seen = key
        self.slot, self.pad = slot, pad
        _, pads, _ = read_manifest(path)
        self.pads = pads
        source = pads[pad] if 0 <= pad < len(pads) else None
        if not source:
            self.name, self.samples, self.frames = "", None, 0
            self.wave_text = f"pad {pad + 1} is empty — pick another pad, or load a sample in the sampler"
            return
        self.name = source["file"]
        self.start, self.end = int(source["start"]), int(source["end"])
        cached = self.cache.get(self.name)
        if cached:
            self.samples, self.rate, self.frames = cached
            self.loading = False
        elif self.name != getattr(self, "_loading_name", None):
            self.start_load(self.name)

    def start_load(self, name):
        self._loading_name = name
        self.loading = True
        token = self._token = self._token + 1
        folder = Path(self.panel.state_dir) / "sampler"
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

    # --- drawing ----------------------------------------------------------------
    def draw(self, p, r, plugin, slot):
        self.use(plugin, slot)
        s = self.panel.s
        header = QRectF(r.left(), r.top(), r.width(), 26 * s)
        source = f"PAD {self.pad + 1}" if self.pads else "PAD -"
        title = f"{source}  ·  {self.name}" if self.name else source
        self.panel.text(p, header.adjusted(2 * s, 0, -90 * s, 0), title, DIM if not self.name else TEXT, 12,
                        Qt.AlignLeft | Qt.AlignVCenter, elide=True)
        reload_btn = QRectF(header.right() - 84 * s, header.top(), 82 * s, 24 * s)
        self.panel.button(p, reload_btn, "RELOAD", self.reload_source)
        wave = QRectF(r.left(), header.bottom() + 5 * s, r.width(), max(80 * s, (r.height() - 31 * s) * 0.42))
        self.draw_wave(p, wave)
        body = QRectF(r.left(), wave.bottom() + 8 * s, r.width(), r.bottom() - wave.bottom() - 8 * s)
        params = [q for q in plugin.params if q.name != "Reload"]
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
        start, end = self.start, self.end
        if end <= start or end > self.frames:
            start, end = 0, self.frames
        span = max(1, end - start)
        view_start = max(0, int(start - span * 0.15))
        view_end = min(self.frames, int(end + span * 0.15))
        view_span = max(1, view_end - view_start)

        def x_of(frame):
            return r.left() + r.width() * (frame - view_start) / view_span

        # the selected slice, and the position/spray band inside it
        x0, x1 = x_of(max(view_start, start)), x_of(min(view_end, end))
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

        # the grain cloud: its density, size and spray make the dots
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

        p.setPen(QPen(QColor("#facc15"), 1.6 * s))
        p.drawLine(x_of(at), r.top() + 2 * s, x_of(at), r.bottom() - 2 * s)
        p.setPen(QPen(QColor("#ffffff"), 1.2 * s))
        for frame in (start, end):
            if view_start <= frame <= view_end:
                p.drawLine(x_of(frame), r.top() + 2 * s, x_of(frame), r.bottom() - 2 * s)
