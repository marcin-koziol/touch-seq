"""The Touch-Seq EQ's editor: the summed curve, the bands you drag, the spectrum behind them, and the menu for
the chosen band's mode and width. It draws into the HOST panel's parameter area - the plugin has no UI of its
own - and talks to it through its parameters (the spectrum is read back from the plugin's output parameters:
the app has no audio buffers of its own, so the plugin measures)."""
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainterPath, QPen, QPolygonF

from .constants import BG, BORDER, DIM, PALETTE, TEXT
from .gestures import Gesture, SlideH

F_MIN, F_MAX, MAX_DB = 20.0, 20000.0, 24.0
TYPES = ("BELL", "LO SHELF", "HI SHELF", "LOW CUT", "HI CUT")
BANDS = 8


def band_coeffs(freq, gain, q, kind, fs=48000.0):
    """One band as an RBJ biquad, the maths the plugin runs: (b0, b1, b2, a1, a2) with a0 normalised away."""
    freq = max(20.0, min(fs * 0.49, freq))
    q = max(0.05, q)
    a = 10.0 ** (gain / 40.0)
    w0 = 2.0 * math.pi * freq / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2.0 * q)
    if kind == 1:  # low shelf
        beta = 2.0 * math.sqrt(a) * alpha
        b0, b1, b2 = a * ((a + 1) - (a - 1) * cw + beta), 2 * a * ((a - 1) - (a + 1) * cw), \
            a * ((a + 1) - (a - 1) * cw - beta)
        a0, a1, a2 = (a + 1) + (a - 1) * cw + beta, -2 * ((a - 1) + (a + 1) * cw), (a + 1) + (a - 1) * cw - beta
    elif kind == 2:  # high shelf
        beta = 2.0 * math.sqrt(a) * alpha
        b0, b1, b2 = a * ((a + 1) + (a - 1) * cw + beta), -2 * a * ((a - 1) + (a + 1) * cw), \
            a * ((a + 1) + (a - 1) * cw - beta)
        a0, a1, a2 = (a + 1) - (a - 1) * cw + beta, 2 * ((a - 1) - (a + 1) * cw), (a + 1) - (a - 1) * cw - beta
    elif kind == 3:  # low cut (high pass)
        b0, b1, b2 = (1 + cw) / 2, -(1 + cw), (1 + cw) / 2
        a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    elif kind == 4:  # high cut (low pass)
        b0, b1, b2 = (1 - cw) / 2, 1 - cw, (1 - cw) / 2
        a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    else:  # bell
        b0, b1, b2 = 1 + alpha * a, -2 * cw, 1 - alpha * a
        a0, a1, a2 = 1 + alpha / a, -2 * cw, 1 - alpha / a
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


def band_db(band, at, fs=48000.0):
    """The band's response at `at` Hz, in dB."""
    b0, b1, b2, a1, a2 = band_coeffs(band[0], band[1], band[2], band[3], fs)
    w = 2.0 * math.pi * at / fs
    zr, zi = math.cos(-w), math.sin(-w)
    z2r, z2i = zr * zr - zi * zi, 2 * zr * zi
    nr, ni = b0 + b1 * zr + b2 * z2r, b1 * zi + b2 * z2i
    dr, di = 1 + a1 * zr + a2 * z2r, a1 * zi + a2 * z2i
    mag = math.hypot(nr, ni) / max(1e-12, math.hypot(dr, di))
    return 20.0 * math.log10(max(1e-12, mag))


def hz(value):
    return f"{value / 1000:.1f}k" if value >= 1000 else f"{value:.0f}"


class BandPoint(Gesture):
    """A band's dot under the finger: sideways is frequency, up and down is boost/cut. A cut keeps its corner
    and takes no gain (the plugin ignores it there)."""

    def __init__(self, editor, index):
        self.editor, self.index = editor, index
        editor.band = index

    def move(self, pos):
        e, graph = self.editor, self.editor.graph
        if graph is None:
            return
        other = e.bands()[self.index]
        e.set_band(self.index, freq=e.freq_at(graph, pos.x()),
                   gain=other[1] if other[3] in (3, 4) else e.db_at(graph, pos.y()))


class EqEditor:
    """The EQ drawn inside the HOST panel: `panel` is it - the drawing primitives, `region()`, `set_param()` and
    its Carla host are what this uses."""

    def __init__(self, panel):
        self.panel = panel
        self.band = 0
        self.plugin = None
        self.spectrum = [0.0] * 48  # the plugin's analyser, 20 Hz .. 20 kHz, log
        self.graph = None

    # --- the plugin's parameters -------------------------------------------
    def bands(self):
        host, plugin = self.panel.host, self.plugin
        if not (host and plugin):
            return []
        out = []
        for i in range(BANDS):
            k = i * 4
            out.append((host.value(plugin, k), host.value(plugin, k + 1), host.value(plugin, k + 2),
                        int(host.value(plugin, k + 3) + 0.5)))
        return out

    @staticmethod
    def width_at(part):
        """The strip's position (0..1) as a band's Q: 0.2 at the left, 10 at the right, log in between."""
        return 0.2 * (10.0 / 0.2) ** max(0.0, min(1.0, part))

    def set_band(self, index, freq=None, gain=None, q=None, kind=None):
        k = index * 4
        for n, value in ((0, freq), (1, gain), (2, q), (3, kind)):
            if value is not None:
                self.panel.set_param(self.plugin, k + n, value, source="eq")

    def forget(self):
        """The plugin went away (its slot was emptied or replaced): stop reading it."""
        self.plugin, self.spectrum = None, [0.0] * len(self.spectrum)

    def tick(self):
        """The plugin measures its output; read the analyser back (it writes output parameters - the only
        numbers a host can hand us, there are no audio buffers here)."""
        plugin = self.plugin
        if not (plugin and self.panel.host):
            return
        if plugin not in self.panel.loaded.values():  # unloaded under us: it would raise on its id
            self.forget()
            return
        indices = self.panel.host.output_params(plugin)
        if len(indices) == len(self.spectrum):
            spectrum = [self.panel.host.value(plugin, i) for i in indices]
            if spectrum != self.spectrum:  # the curve moves: repaint it, nothing else has to
                self.spectrum = spectrum
                self.panel.invalidate()

    # --- mapping ------------------------------------------------------------
    @staticmethod
    def x_of(graph, freq):
        return graph.left() + graph.width() * math.log(max(F_MIN, freq) / F_MIN) / math.log(F_MAX / F_MIN)

    @staticmethod
    def freq_at(graph, x):
        part = (x - graph.left()) / max(1.0, graph.width())
        return F_MIN * (F_MAX / F_MIN) ** max(0.0, min(1.0, part))

    @staticmethod
    def y_of(graph, db):
        return graph.center().y() - (max(-MAX_DB, min(MAX_DB, db)) / MAX_DB) * (graph.height() * 0.5 - 6)

    @staticmethod
    def db_at(graph, y):
        return max(-MAX_DB, min(MAX_DB, (graph.center().y() - y) / (graph.height() * 0.5 - 6) * MAX_DB))

    # --- drawing ------------------------------------------------------------
    def draw(self, p, r, plugin):
        if plugin is not self.plugin:
            self.spectrum = [0.0] * len(self.spectrum)  # another EQ: its own analyser
        self.plugin = plugin
        s = self.panel.s
        graph = QRectF(r.left(), r.top(), r.width(), r.height() * 0.66)
        menu = QRectF(r.left(), graph.bottom() + 8 * s, r.width(), r.bottom() - graph.bottom() - 8 * s)
        self.graph = graph
        self.panel.box(p, graph, "#0e1116", BORDER, radius=10)
        self.draw_spectrum(p, graph)
        self.draw_grid(p, graph)
        self.draw_curve(p, graph)
        self.draw_bands(p, graph)
        self.draw_menu(p, menu)

    def draw_spectrum(self, p, graph):
        s = self.panel.s
        if not any(self.spectrum):
            return
        points = QPolygonF([QPointF(graph.left(), graph.bottom())])
        for n, value in enumerate(self.spectrum):
            freq = F_MIN * (F_MAX / F_MIN) ** ((n + 0.5) / len(self.spectrum))
            points.append(QPointF(self.x_of(graph, freq), graph.bottom() - value * graph.height() * 0.96))
        points.append(QPointF(graph.right(), graph.bottom()))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(56, 214, 255, 38))
        p.drawPolygon(points)
        p.setPen(QPen(QColor(56, 214, 255, 120), 1.2 * s))
        p.drawPolyline(points)

    def draw_grid(self, p, graph):
        s = self.panel.s
        for freq, label in ((50, "50"), (100, "100"), (200, "200"), (500, "0.5k"), (1000, "1k"), (2000, "2k"),
                            (5000, "5k"), (10000, "10k")):
            x = self.x_of(graph, freq)
            p.setPen(QPen(QColor("#1d2230"), 1 * s))
            p.drawLine(QPointF(x, graph.top()), QPointF(x, graph.bottom()))
            self.panel.text(p, QRectF(x - 20 * s, graph.bottom() - 15 * s, 40 * s, 14 * s), label, "#3f4654", 9)
        for db in (-24, -12, 0, 12, 24):
            y = self.y_of(graph, db)
            p.setPen(QPen(QColor("#232a38" if db == 0 else "#171c26"), 1.2 if db == 0 else 1 * s))
            p.drawLine(QPointF(graph.left(), y), QPointF(graph.right(), y))
            self.panel.text(p, QRectF(graph.left() + 4 * s, y - 7 * s, 30 * s, 14 * s), f"{db:+d}" if db else "0",
                            "#3f4654", 9, Qt.AlignLeft | Qt.AlignVCenter)

    def draw_curve(self, p, graph):
        s, bands = self.panel.s, self.bands()
        if not bands:
            return
        path = QPainterPath()
        steps = 140
        for n in range(steps + 1):
            freq = F_MIN * (F_MAX / F_MIN) ** (n / steps)
            db = sum(band_db(band, freq) for band in bands)
            point = QPointF(self.x_of(graph, freq), self.y_of(graph, db))
            path.moveTo(point) if n == 0 else path.lineTo(point)
        p.setPen(QPen(QColor(120, 220, 255, 60), 5 * s))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.setPen(QPen(QColor("#8ad9ff"), 2 * s))
        p.drawPath(path)

    def place(self, pos):
        """A tap on the graph: the band under the finger, or the chosen band put there - a low cut near the
        bass end, a high cut near the top, a bell in the middle (its mode stays editable in the menu)."""
        graph, s = self.graph, self.panel.s
        bands = self.bands()
        if graph is None or not bands:
            return None
        for i, band in enumerate(bands):
            x, y = self.x_of(graph, band[0]), self.y_of(graph, band[1])
            if abs(pos.x() - x) < 26 * s and abs(pos.y() - y) < 26 * s:
                self.band = i
                return BandPoint(self, i)
        part = (pos.x() - graph.left()) / max(1.0, graph.width())
        kind = 3 if part < 0.15 else 4 if part > 0.85 else 0
        self.set_band(self.band, freq=self.freq_at(graph, pos.x()), gain=self.db_at(graph, pos.y()), kind=kind)
        return BandPoint(self, self.band)

    def draw_bands(self, p, graph):
        s, bands = self.panel.s, self.bands()
        if not bands:
            return

        self.panel.region(graph, lambda pos: self.place(pos))
        for i, band in enumerate(bands):
            x, y = self.x_of(graph, band[0]), self.y_of(graph, band[1])
            colour = PALETTE[i % len(PALETTE)]
            chosen = i == self.band
            radius = (11 if chosen else 8) * s
            p.setPen(QPen(QColor("#0b0d12"), 2 * s))
            p.setBrush(QColor(colour))
            p.drawEllipse(QPointF(x, y), radius, radius)
            if chosen:
                p.setPen(QPen(QColor("#e6e9ee"), 2 * s))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(x, y), radius + 4 * s, radius + 4 * s)
            self.panel.text(p, QRectF(x - 20 * s, y - radius - 18 * s, 40 * s, 16 * s), str(i + 1), colour, 10,
                            bold=True)

    def draw_menu(self, p, menu):
        s = self.panel.s
        bands = self.bands()
        if not bands:
            return
        freq, gain, q, kind = bands[self.band]
        colour = PALETTE[self.band % len(PALETTE)]
        name = QRectF(menu.left(), menu.top(), 76 * s, 42 * s)
        self.panel.box(p, name, colour, None, radius=8)
        self.panel.text(p, name, f"B{self.band + 1}", BG, 17, bold=True)
        types = split_h(QRectF(name.right() + 6 * s, menu.top(), menu.width() - name.width() - 6 * s, 42 * s), 5,
                        5 * s)
        for n, (rect, label) in enumerate(zip(types, TYPES)):
            self.panel.button(p, rect, label, lambda n=n: self.set_band(self.band, kind=n), on=kind == n,
                              color=colour)
        row = QRectF(menu.left(), name.bottom() + 8 * s, menu.width(), 44 * s)
        caption = QRectF(row.left(), row.top(), 74 * s, row.height())
        readout = QRectF(row.right() - 210 * s, row.top(), 210 * s, row.height())
        slider = QRectF(caption.right() + 8 * s, row.top(), readout.left() - caption.right() - 16 * s, row.height())
        self.panel.text(p, caption, "WIDTH", DIM, 12, Qt.AlignLeft | Qt.AlignVCenter)
        self.panel.box(p, slider, "#11141b", BORDER, radius=6)
        part = math.log(q / 0.2) / math.log(10.0 / 0.2) if q > 0.2 else 0.0
        handle = QRectF(slider.left() + part * (slider.width() - 10 * s), slider.top() + 4 * s, 10 * s,
                        slider.height() - 8 * s)
        self.panel.box(p, handle, colour, None, radius=4)
        # the strip is horizontal, so the gesture reads the finger's x (SlideH), not its height
        self.panel.region(slider, lambda pos: SlideH(
            slider, lambda v: self.set_band(self.band, q=self.width_at(v)), pos))
        self.panel.text(p, readout, f"{hz(freq)} Hz   ·   {gain:+.1f} dB   ·   Q {q:.2f}", TEXT, 13,
                        Qt.AlignRight | Qt.AlignVCenter, bold=True)


def split_h(rect, count, gap):
    """`count` equal boxes in a row."""
    width = (rect.width() - gap * (count - 1)) / count
    return [QRectF(rect.left() + n * (width + gap), rect.top(), width, rect.height()) for n in range(count)]
