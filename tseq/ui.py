"""The drawing toolkit and the module window: `Panel` (immediate-mode drawing in its own coordinates, hit
regions, one gesture per touch), the primitives every panel draws with, and `Window` (faceplates, modular
layout, dialogs over the rack, the repaint clock)."""
import math
import re
import sys
import time
import traceback

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPolygonF, QRadialGradient)
from PySide6.QtWidgets import QWidget

from .constants import BG, BORDER, DIM, MIN_TEXT, MOUSE_ID, PANEL, PARAM_FAMILIES, TEXT, TOUCH_LOG, WARN
from .gestures import Drag, Gesture


def split(r, widths, gap):
    """Lay boxes out left to right; a width of 0 shares the remaining space."""
    fixed = sum(widths) + gap * (len(widths) - 1)
    flex = max(0.0, (r.width() - fixed) / max(1, widths.count(0)))
    out, x = [], r.left()
    for w in widths:
        w = w or flex
        out.append(QRectF(x, r.top(), w, r.height()))
        x += w + gap
    return out


class Panel:
    """A rack module's touch surface: immediate-mode drawing in its own coordinates (0,0 = top left of
    its screen) plus one gesture per touch point. Hit regions are collected while drawing."""

    def __init__(self):
        self.w = self.h = 0.0
        self.s = 1.0  # UI scale relative to a 1024x600 layout
        self.gestures = {}
        self.hits = []
        self.stale = False  # changed outside a touch: repaint even though nothing is animating
        self.zoomed = False  # set by the window: this panel fills the rack (HOST-4 keeps its chain narrow)

    def invalidate(self):
        """Ask for a repaint after a change made without a touch (touches repaint by themselves)."""
        self.stale = True

    def animating(self):
        """Is anything in this panel changing on its own? While a gesture is down (a drag, a held knob's
        progress ring) the UI needs frames; otherwise a repaint has to be asked for."""
        return bool(self.gestures)

    def rect(self):
        return QRectF(0, 0, self.w, self.h)

    def paint(self, p, w, h):
        self.w, self.h, self.s = w, h, min(w / 1024, h / 600)
        self.hits = []
        self.draw(p)

    def draw(self, p):
        raise NotImplementedError

    def tick(self):
        for g in list(self.gestures.values()):
            g.tick()

    def cancel(self, tid):
        g = self.gestures.pop(tid, None)
        if g:
            g.cancel()

    # --- input --------------------------------------------------------
    def region(self, rect, factory):
        """factory(pos) -> Gesture or None; later regions win."""
        self.hits.append((rect, factory))

    def on_press(self, rect, fn):
        self.region(rect, lambda pos: fn())

    def press(self, tid, pos):
        for rect, factory in reversed(self.hits):
            if rect.contains(pos):
                g = factory(pos)
                if isinstance(g, Gesture):
                    self.gestures[tid] = g
                break

    def move(self, tid, pos):
        if tid in self.gestures:
            self.gestures[tid].move(pos)

    def release(self, tid, pos):
        g = self.gestures.pop(tid, None)
        if g:
            g.release(pos)

    # --- drawing primitives ---------------------------------------------
    def box(self, p, r, fill, border=BORDER, width=1.5, radius=8):
        p.setPen(QPen(QColor(border), width) if border else Qt.NoPen)
        p.setBrush(QColor(fill))
        p.drawRoundedRect(r, radius * self.s, radius * self.s)

    def text(self, p, r, s, color=TEXT, size=15, align=Qt.AlignCenter, bold=False, elide=False):
        f = QFont("sans-serif")
        f.setPixelSize(max(6, round(max(size, MIN_TEXT) * self.s)))
        f.setBold(bold)
        p.setFont(f)
        p.setPen(QColor(color))
        if elide:
            s = QFontMetrics(f).elidedText(s, Qt.ElideRight, int(r.width()))
        p.drawText(r, align, s)

    @staticmethod
    def blink():
        return int(time.monotonic() * 4) % 2 == 0

    def button(self, p, r, label, fn, on=False, color="#38d6ff", pending=False, enabled=True, sub=None):
        c = QColor(color)
        if on:
            fill, fg = c, BG
        else:
            fill, fg = QColor(PANEL), (TEXT if enabled else "#3a4252")
        border = c if pending and self.blink() else (c.darker(130) if on else BORDER)
        self.box(p, r, fill, border, 2 if pending else 1.5)
        if sub:
            self.text(p, r.adjusted(0, 0, 0, -r.height() * 0.3), label, fg, 16, bold=True)
            self.text(p, r.adjusted(0, r.height() * 0.45, 0, 0), sub, fg if on else DIM, 11)
        else:
            self.text(p, r, label, fg, 16, bold=True)
        if enabled:
            self.on_press(r, fn)

    def value_box(self, p, r, label, value, get, set_, lo, hi, px, unit=1, on_tap=None):
        """Drag vertically to change; without on_tap, a tap on the upper/lower half steps by +/- unit."""
        s = self.s
        self.box(p, r, PANEL)
        self.text(p, r.adjusted(0, 4 * s, 0, 0), label, DIM, 11, Qt.AlignHCenter | Qt.AlignTop)
        self.text(p, r.adjusted(0, 12 * s, 0, 0), value, TEXT, 20, bold=True)
        if on_tap is None:
            arrows = r.adjusted(0, 6 * s, -7 * s, -6 * s)
            self.text(p, arrows, "▲", "#5b6577", 10, Qt.AlignRight | Qt.AlignTop)
            self.text(p, arrows, "▼", "#5b6577", 10, Qt.AlignRight | Qt.AlignBottom)

        def tap_step(pos):
            return lambda: set_(max(lo, min(hi, get() + (unit if pos.y() < r.center().y() else -unit))))

        self.region(r, lambda pos: Drag(pos, get, set_, lo, hi, px * s, unit, on_tap or tap_step(pos)))


def glyph(p, r, name, color, width=0.075, lit=None):
    """Small line icons for the top bar and the track headers: the app carries no image files, and a drawn
    glyph keeps its crispness at any scale."""
    s = min(r.width(), r.height())
    cx, cy = r.center().x(), r.center().y()
    pen = QPen(QColor(color), max(1.1, s * width))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if name == "folder":  # FILE: a folder with its tab
        body = QRectF(cx - 0.30 * s, cy - 0.20 * s, 0.60 * s, 0.42 * s)
        p.drawRoundedRect(body, 0.08 * s, 0.08 * s)
        tab = QPolygonF([QPointF(body.left() + 0.05 * s, body.top()),
                         QPointF(body.left() + 0.20 * s, body.top()),
                         QPointF(body.left() + 0.27 * s, body.top() - 0.12 * s),
                         QPointF(body.left() + 0.50 * s, body.top() - 0.12 * s)])
        p.drawPolyline(tab)
    elif name == "sliders":  # SETTINGS: three sliders
        for j, k in enumerate((0.30, 0.68, 0.46)):
            y = cy + (j - 1) * 0.22 * s
            p.drawLine(QPointF(cx - 0.30 * s, y), QPointF(cx + 0.30 * s, y))
            p.setBrush(QColor(color))
            p.drawEllipse(QPointF(cx + (k - 0.5) * 0.52 * s, y), 0.075 * s, 0.075 * s)
            p.setBrush(Qt.NoBrush)
    elif name == "cables":  # PATCHBAY: a jack cable, a plug at each end
        cable = QPainterPath()
        cable.moveTo(cx - 0.16 * s, cy + 0.20 * s)
        cable.cubicTo(cx + 0.06 * s, cy + 0.26 * s, cx - 0.06 * s, cy - 0.26 * s, cx + 0.16 * s, cy - 0.20 * s)
        p.drawPath(cable)
        p.setBrush(QColor(color))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(cx - 0.34 * s, cy + 0.13 * s, 0.18 * s, 0.15 * s), 0.04 * s, 0.04 * s)
        p.drawRoundedRect(QRectF(cx + 0.16 * s, cy - 0.28 * s, 0.18 * s, 0.15 * s), 0.04 * s, 0.04 * s)
        p.setBrush(Qt.NoBrush)
    elif name == "metronome":  # CLICK
        body = QPolygonF([QPointF(cx - 0.26 * s, cy + 0.28 * s), QPointF(cx + 0.26 * s, cy + 0.28 * s),
                          QPointF(cx + 0.10 * s, cy - 0.28 * s), QPointF(cx - 0.10 * s, cy - 0.28 * s)])
        p.drawPolyline(QPolygonF(list(body) + [body[0]]))
        p.drawLine(QPointF(cx, cy + 0.22 * s), QPointF(cx + 0.13 * s, cy - 0.20 * s))  # the arm
        p.drawLine(QPointF(cx - 0.36 * s, cy + 0.30 * s), QPointF(cx + 0.36 * s, cy + 0.30 * s))  # the base
    elif name == "sticks":  # COUNT-IN: the drummer's crossed sticks
        sticks = ((QPointF(cx - 0.38 * s, cy + 0.30 * s), QPointF(cx + 0.30 * s, cy - 0.30 * s)),
                  (QPointF(cx + 0.38 * s, cy + 0.30 * s), QPointF(cx - 0.30 * s, cy - 0.30 * s)))
        for butt, tip in sticks:
            p.drawLine(butt, tip)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        for _, tip in sticks:
            p.drawEllipse(tip, 0.085 * s + 0.7, 0.085 * s + 0.7)
    elif name == "din":  # a track's MIDI input: the five pins of the socket, in their arc
        rad = 0.40 * s
        dot = 0.085 * s + 0.7
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        for deg in (200, 235, 270, 305, 340):
            a = math.radians(deg)
            p.drawEllipse(QPointF(cx + rad * math.cos(a), cy + rad * math.sin(a) + 0.16 * s), dot, dot)


def param_family(name):
    """What a plugin parameter's name says it is - "envelope", "filter", "lfo", "effect", "oscillator",
    "level" - or "" when it says nothing."""
    low = name.lower()
    words = set(re.split(r"[^a-z0-9]+", low))
    for key, _, parts, whole in PARAM_FAMILIES:
        if any(p in low for p in parts) or words & set(whole):
            return key
    return ""


def _name_words(name):
    """A parameter name as lowercase words, split where the name is not one: "OscFineTune" -> osc/fine/tune,
    "Pitch Attack" -> pitch/attack, "OSC1Wave" -> osc/1/wave."""
    words = []
    for token in re.finditer(r"[A-Za-z0-9]+", name):
        for word in re.finditer(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", token.group(0)):
            words.append(word.group(0).lower())
    return words


SECTION_GENERIC = {"rate", "mod"}  # properties of a section, not sections of their own ("SampleRate")


def _is_kind(word):
    """Does this word name what a parameter is (an envelope's attack, a filter's cutoff, an LFO...)? """
    if word in SECTION_GENERIC:
        return False
    return any(word in whole or any(part in word for part in parts) for _, _, parts, whole in PARAM_FAMILIES)


def param_section(name):
    """The section a parameter's name puts it in: "Osc" in "OscWave", "Filter" in "Filter Cutoff", "Amp" in
    "Amp Attack", "Sub" in "SubOscVol", "OSC 1" in "OSC 1 Wave". Empty for a bare parameter ("Attack",
    "Cutoff"): it belongs to the section its kind names (an envelope, a filter)."""
    words = _name_words(name)
    if len(words) > 2 and words[-1].isdigit():  # "LFO Step 1": the number indexes a section, it is not one
        return " ".join(words[:-1])
    for i, word in enumerate(words):
        if not _is_kind(word):
            continue
        if i:
            return " ".join(words[:i])
        section = [word]  # the name starts with its kind: the kind names the section, numbers and all
        for word in words[1:]:
            if not word.isdigit():
                break
            section.append(word)
        return " ".join(section) if len(words) > 1 else ""
    if len(words) > 1 and re.search(r"[\s_-]", name):  # "Crash Tone", "Clap Hands": words before the property
        return " ".join(words[:-1])  # (a compact name like "MonoPoly" is one setting, not a section)
    return ""


def param_color(name):
    """Knob color for a plugin parameter, guessed from its name (unknown: the default cyan)."""
    family = param_family(name)
    return next((color for key, color, _, _ in PARAM_FAMILIES if key == family), "#38d6ff")


class Window(QWidget):
    """Full-screen touch window of modules: panels on modular-synth style faceplates in a 2x2 grid (a
    single module fills the window), an optional toolbar on top and modal dialogs (a module over the rest).
    Each touch belongs to the module it started in, so several modules can be played at once."""
    TOOLBAR = 0  # toolbar height (1920x1080 units); subclasses that draw one set it

    def __init__(self, modules):
        super().__init__()
        self.setAttribute(Qt.WA_AcceptTouchEvents)
        self.setWindowTitle("Touch-Seq")
        self.modules = modules  # (column, row, name, description, panel); None = blank panel
        self.panels = [m[4] for m in modules if m[4]]
        self.screens = {}  # panel -> its screen rect in window coordinates (from the last paint)
        self.rail_buttons = []  # (rect, action) on rails and the toolbar, from the last paint
        self.zoomed = None  # panel shown over the whole window
        self.dialog = None  # (name, description, panel) shown over everything
        self.dialog_rect = QRectF()
        self.owner = {}  # touch id -> panel
        self.mouse = False  # the app sets this when no touchscreen was found: the pointer works everywhere
        self.dirty = False  # something may have been edited since the last save
        self.broken = set()  # panels whose tick raised: reported once, the clock keeps going
        self.broken_paint = set()  # panels whose draw raised: drawn as an error, the rest still paints
        self.slow_paint_at = 0.0  # when a slow frame was last reported
        QTimer(self, interval=40, timeout=self.tick).start()

    # --- input ----------------------------------------------------------
    def press(self, tid, pos):
        self.dirty = True
        for rect, action in self.rail_buttons:
            if rect.contains(pos):
                action()
                self.update()
                return
        if self.dialog:  # modal: its screen takes touches, a tap beside it closes it
            panel = self.dialog[2]
            if self.screens[panel].contains(pos):
                self.owner[tid] = panel
                panel.press(tid, pos - self.screens[panel].topLeft())
            elif not self.dialog_rect.contains(pos):
                self.close_dialog()
            self.update()
            return
        for panel, screen in self.screens.items():
            if screen.contains(pos):
                self.owner[tid] = panel
                panel.press(tid, pos - screen.topLeft())
                break
        self.update()

    def cancel_touches(self):
        for tid, owner in self.owner.items():
            owner.cancel(tid)
        self.owner.clear()

    def open_dialog(self, name, desc, panel):
        self.cancel_touches()  # the modules underneath stop getting touches
        self.dialog = (name, desc, panel)

    def close_dialog(self):
        self.cancel_touches()
        self.dialog = None

    def move(self, tid, pos):
        panel = self.owner.get(tid)
        if panel:
            panel.move(tid, pos - self.screens[panel].topLeft())
            self.update()

    def release(self, tid, pos):
        panel = self.owner.pop(tid, None)
        if panel:
            panel.release(tid, pos - self.screens[panel].topLeft())
            self.update()

    def log_event(self, e):
        points = getattr(e, "points", lambda: [])()
        line = f"{time.monotonic():.3f} {e.type().name} " + " ".join(
            f"[{p.id()} {p.state().name} {p.position().x():.0f},{p.position().y():.0f}]" for p in points)
        if hasattr(e, "source"):
            line += f" source={e.source().name}"
        device = e.pointingDevice() if hasattr(e, "pointingDevice") else None
        with open(TOUCH_LOG, "a") as f:
            f.write(line + (f" dev={device.name()}" if device else "") + "\n")

    def event(self, e):
        t = e.type()
        if TOUCH_LOG and t in (e.Type.TouchBegin, e.Type.TouchUpdate, e.Type.TouchEnd, e.Type.TouchCancel,
                               e.Type.MouseButtonPress, e.Type.MouseButtonRelease):
            self.log_event(e)
        if t in (e.Type.TouchBegin, e.Type.TouchUpdate, e.Type.TouchEnd, e.Type.TouchCancel):
            present = {p.id() for p in e.points()}
            for tid in [tid for tid in self.owner if tid != MOUSE_ID and tid not in present]:
                self.owner.pop(tid).cancel(tid)  # a finger that vanished without a release
            for p in e.points():
                s = p.state()
                if t == e.Type.TouchCancel:
                    panel = self.owner.pop(p.id(), None)
                    if panel:
                        panel.cancel(p.id())
                elif s == p.State.Released:
                    self.release(p.id(), p.position())
                elif s == p.State.Pressed:
                    self.press(p.id(), p.position())
                elif s == p.State.Updated:
                    self.move(p.id(), p.position())
            e.accept()
            return True
        return super().event(e)

    def mousePressEvent(self, e):
        if self.mouse or e.source() == Qt.MouseEventNotSynthesized:
            self.press(MOUSE_ID, e.position())

    def mouseMoveEvent(self, e):
        if self.mouse or e.source() == Qt.MouseEventNotSynthesized:
            self.move(MOUSE_ID, e.position())

    def mouseReleaseEvent(self, e):
        if self.mouse or e.source() == Qt.MouseEventNotSynthesized:
            self.release(MOUSE_ID, e.position())

    def keyPressEvent(self, e):
        ctrl_c = e.key() == Qt.Key_C and e.modifiers() & Qt.ControlModifier
        if e.key() in (Qt.Key_Escape, Qt.Key_Q) or ctrl_c:
            self.close()

    def tick(self):
        panels = self.panels + ([self.dialog[2]] if self.dialog else [])
        for panel in panels:
            try:
                panel.tick()
            except Exception:  # a ticker that breaks must not stop the clock: the screen would freeze
                if id(panel) not in self.broken:
                    self.broken.add(id(panel))
                    traceback.print_exc()
        try:
            animating = self.animating()
        except Exception:  # a panel that cannot say whether it moves still has to let the frame through
            traceback.print_exc()
            animating = True
        if animating or any(p.stale for p in panels):
            for panel in panels:
                panel.stale = False
            self.update()

    def animating(self):
        """Is anything on screen changing by itself? Repainting is expensive (a full 4K frame is a lot of
        software antialiased drawing), so a still rack - stopped, nothing held - must not repaint at all."""
        return any(p.animating() for p in self.panels + ([self.dialog[2]] if self.dialog else []))

    # --- drawing --------------------------------------------------------
    def paintEvent(self, _):
        started = time.perf_counter()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#08090b"))
        self.k = k = min(self.width() / 1920, self.height() / 1080)
        gap = 6 * k
        self.screens, self.rail_buttons = {}, []
        top = 0.0
        if self.TOOLBAR:
            bar = QRectF(gap, gap, self.width() - 2 * gap, self.TOOLBAR * k)
            self.draw_rail(p, bar)
            self.draw_toolbar(p, bar)
            top = bar.bottom()
        area = QRectF(gap, top + gap, self.width() - 2 * gap, self.height() - top - 2 * gap)
        mw, mh = (area.width() - gap) / 2, (area.height() - gap) / 2
        single = len(self.modules) == 1
        for n, (col, row, name, desc, panel) in enumerate(self.modules):
            if self.zoomed and panel is not self.zoomed:
                continue
            r = (area if self.zoomed or single else
                 QRectF(area.left() + col * (mw + gap), area.top() + row * (mh + gap), mw, mh))
            zoom = None
            if panel and not single:
                zoom = ("⤡" if self.zoomed else "⤢", lambda panel=panel: self.toggle_zoom(panel))
            screen = self.draw_faceplate(p, r, n, name, desc, panel, zoom)
            if panel:
                self.screens[panel] = screen
                self.paint_panel(p, panel, screen)
        if self.dialog:
            p.fillRect(self.rect(), QColor(0, 0, 0, 160))
            self.dialog_rect = QRectF(0, 0, min(1400 * k, area.width() - 80 * k), min(860 * k, area.height() - 40 * k))
            self.dialog_rect.moveCenter(area.center())
            name, desc, panel = self.dialog
            self.screens[panel] = self.draw_faceplate(p, self.dialog_rect, 7, name, desc, panel,
                                                      ("✕", self.close_dialog))
            self.paint_panel(p, panel, self.screens[panel])
        spent = time.perf_counter() - started
        if spent > 0.25 and time.monotonic() - self.slow_paint_at > 5.0:  # a frame this slow is a frozen screen
            self.slow_paint_at = time.monotonic()
            print(f"touch-seq: a frame took {spent * 1000:.0f} ms", file=sys.stderr)

    def paint_panel(self, p, panel, screen):
        """Draw a panel. One that raises (a plugin gone mid-frame, say) is reported once and drawn as an error:
        a broken panel used to take the window's whole frame with it, so the screen froze while the app went on."""
        p.save()
        p.translate(screen.topLeft())
        p.setClipRect(QRectF(0, 0, screen.width(), screen.height()))
        panel.zoomed = self.zoomed is panel
        try:
            panel.paint(p, screen.width(), screen.height())
        except Exception as e:  # noqa: BLE001 - anything a panel's drawing does must not stop the frame
            if id(panel) not in self.broken_paint:
                self.broken_paint.add(id(panel))
                traceback.print_exc()
            self.draw_broken(p, screen, e)
        p.restore()

    def draw_broken(self, p, screen, error):
        """What a panel that cannot draw shows instead of nothing."""
        k = self.k
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#2a1512"))
        p.drawRect(QRectF(0, 0, screen.width(), screen.height()))
        f = QFont("sans-serif")
        f.setPixelSize(round(15 * k))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#ff8c42"))
        box = QRectF(16 * k, 0, screen.width() - 32 * k, screen.height())
        p.drawText(box, Qt.AlignCenter | Qt.TextWordWrap,
                   f"this module cannot draw itself\n{type(error).__name__}: {error}\n"
                   f"(the rest keeps playing; the terminal has the traceback)")

    def draw_rail(self, p, r):
        """A strip of brushed metal (module panel without screen)."""
        k = self.k
        grad = QLinearGradient(r.topLeft(), r.bottomLeft())
        grad.setColorAt(0, QColor("#41464f"))
        grad.setColorAt(1, QColor("#2a2e35"))
        p.setPen(QPen(QColor("#5a606b"), 1))
        p.setBrush(grad)
        p.drawRoundedRect(r, 5 * k, 5 * k)

    def draw_toolbar(self, p, r):
        pass

    def rail_button(self, p, r, label, action, size=24, on=False, frame=True, sub=None, color="#38d6ff",
                    icon=None, lit=None):
        """Button on metal; without a frame only thin bars at the sides mark it (for the module rails)."""
        k = self.k
        if frame:
            p.setPen(QPen(QColor(color if on else "#5a606b"), 1.5 if on else 1))
            p.setBrush(QColor("#1d2128" if on else "#2a2e35"))
            p.drawRoundedRect(r, 4 * k, 4 * k)
        else:
            p.setPen(QPen(QColor("#5a606b"), 1.5 * k))
            for x in (r.left(), r.right()):
                p.drawLine(QPointF(x, r.top() + 3 * k), QPointF(x, r.bottom() - 3 * k))
        f = QFont("sans-serif")
        f.setPixelSize(round(size * k))
        f.setBold(True)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.5 * k)
        p.setFont(f)
        p.setPen(QColor("#e6e9ee"))
        if icon:
            area = (QRectF(r.left() + r.width() * 0.20, r.top() + r.height() * 0.08, r.width() * 0.60,
                           r.height() * 0.52) if sub else  # room for the badge under it
                    QRectF(r.left() + r.width() * 0.18, r.top() + r.height() * 0.18, r.width() * 0.64,
                           r.height() * 0.64))
            glyph(p, area, icon, color if on else "#c8cdd6", lit=lit)
            if sub:  # a small badge under the icon (COUNT: how many bars will count in)
                f.setPixelSize(round(10 * k))
                f.setBold(True)
                p.setFont(f)
                p.setPen(QColor(color if on else "#8a929e"))
                p.drawText(QRectF(r.left() + r.width() * 0.1, r.top() + r.height() * 0.58,
                                  r.width() * 0.8, r.height() * 0.36), Qt.AlignCenter, sub)
        elif sub:
            p.drawText(r.adjusted(0, 0, 0, -r.height() * 0.32), Qt.AlignCenter, label)
            f.setPixelSize(round(max(9, size - 6) * k))
            f.setBold(False)
            p.setFont(f)
            p.setPen(QColor(color if on else "#8a929e"))
            p.drawText(r.adjusted(0, r.height() * 0.34, 0, 0), Qt.AlignCenter, sub)
        else:
            p.drawText(r, Qt.AlignCenter, label)
        self.rail_buttons.append((r.adjusted(-6 * k, -5 * k, 6 * k, 5 * k), action))

    def toggle_zoom(self, panel):
        self.cancel_touches()  # panels are about to move or hide
        self.zoomed = None if self.zoomed else panel

    def draw_faceplate(self, p, r, n, name, desc, panel, button=None):
        """Brushed-aluminium module panel with corner screws and an optional (glyph, action) button on the
        rail; returns the screen rect inside it (None for a blank panel)."""
        k = self.k
        has_screen = panel is not None
        grad = QLinearGradient(r.topLeft(), r.bottomRight())
        grad.setColorAt(0, QColor("#41464f"))
        grad.setColorAt(1, QColor("#2a2e35"))
        p.setPen(QPen(QColor("#5a606b"), 1))
        p.setBrush(grad)
        p.drawRoundedRect(r, 5 * k, 5 * k)
        p.setPen(QPen(QColor(255, 255, 255, 30), 1))
        p.drawLine(QPointF(r.left() + 6 * k, r.top() + 1.5), QPointF(r.right() - 6 * k, r.top() + 1.5))

        rail = 36 * k  # band at the top and bottom holding the screws
        for j, (x, y) in enumerate([(r.left(), r.top()), (r.right(), r.top()), (r.left(), r.bottom()),
                                    (r.right(), r.bottom())]):
            c = QPointF(x + (19 if x == r.left() else -19) * k, y + (18 if y == r.top() else -18) * k)
            self.draw_screw(p, c, 8.5 * k, (n * 53 + j * 71) % 180)

        label = QRectF(r.left() + 44 * k, r.top(), r.width() - 88 * k, rail)
        f = QFont("sans-serif")
        f.setPixelSize(round(17 * k))
        f.setBold(True)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 2 * k)
        p.setFont(f)
        p.setPen(QColor("#e6e9ee"))
        p.drawText(label, Qt.AlignLeft | Qt.AlignVCenter, name)
        f.setPixelSize(round(15 * k))
        f.setBold(False)
        p.setFont(f)
        p.setPen(QColor("#9aa3b0"))
        if button:  # sits in the upper part of the rail, clear of the screen bezel below it
            rb = QRectF(label.right() - 40 * k, r.top() + 4 * k, 40 * k, rail - 14 * k)
            label.setRight(rb.left() - 12 * k)
            self.rail_button(p, rb, *button, size=22, frame=False)
            p.setFont(f)
            p.setPen(QColor("#9aa3b0"))
        p.drawText(label, Qt.AlignRight | Qt.AlignVCenter, desc.upper())

        if not has_screen:  # blank panel: a few vent slots
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#1b1e23"))
            for v in range(7):
                slot = QRectF(0, 0, r.width() * 0.4, 9 * k)
                slot.moveCenter(QPointF(r.center().x(), r.center().y() + (v - 3) * 26 * k))
                p.drawRoundedRect(slot, 4.5 * k, 4.5 * k)
            return None

        screen = r.adjusted(16 * k, rail, -16 * k, -rail)
        p.setPen(QPen(QColor("#121418"), 4 * k))  # recessed bezel
        p.setBrush(QColor(BG))
        p.drawRoundedRect(screen.adjusted(-3 * k, -3 * k, 3 * k, 3 * k), 7 * k, 7 * k)
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(screen.adjusted(-6 * k, -6 * k, 6 * k, 6 * k), 9 * k, 9 * k)
        return screen

    @staticmethod
    def draw_screw(p, c, rad, angle):
        g = QRadialGradient(c - QPointF(rad * 0.35, rad * 0.35), rad * 1.4)
        g.setColorAt(0, QColor("#f1f3f6"))
        g.setColorAt(0.55, QColor("#a7adb7"))
        g.setColorAt(1, QColor("#5a606a"))
        p.setPen(QPen(QColor("#1b1e22"), 1.2))
        p.setBrush(g)
        p.drawEllipse(c, rad, rad)
        p.save()
        p.translate(c)
        p.rotate(angle)
        p.setPen(QPen(QColor("#3b4048"), rad * 0.3, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(-rad * 0.6, 0), QPointF(rad * 0.6, 0))
        p.restore()


class Keyboard:
    """An on-screen keyboard for a name: a field, letters and digits, shift, space, backspace, OK and CANCEL.
    It draws into a rect and registers its keys on the panel, so a dialog or an editor can use it as an
    overlay without a window of its own."""

    ROWS = ("1234567890", "QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM-_")
    MAX = 28

    def __init__(self, text="", title="Name", done=None, cancel=None, placeholder=""):
        self.text, self.title, self.done, self.cancel = text, title, done, cancel
        self.placeholder, self.shift = placeholder, True

    def press(self, key):
        if key == "back":
            self.text = self.text[:-1]
        elif key == "space":
            self.text = (self.text + " ")[:self.MAX]
        elif key == "shift":
            self.shift = not self.shift
        elif key == "ok":
            if self.done:
                self.done((self.text.strip() or self.placeholder).strip())
        elif key == "cancel":
            if self.cancel:
                self.cancel()
        else:
            ch = key.upper() if self.shift and key.isalpha() else key.lower() if key.isalpha() else key
            self.text = (self.text + ch)[:self.MAX]

    def key(self, panel, p, r, label, key, color=None, on=False):
        fill = QColor(color) if on and color else QColor(PANEL)
        border = QColor(color).darker(140) if color else QColor(BORDER)
        panel.box(p, r, fill, border, radius=5)
        panel.text(p, r, label, BG if on and color else TEXT, 15, bold=True)
        panel.on_press(r, lambda key=key: self.press(key))

    def draw(self, panel, p, rect):
        s = panel.s
        panel.box(p, rect, QColor(13, 15, 20), BORDER, radius=8)
        panel.region(rect, lambda pos: Gesture())  # taps that miss a key do nothing
        title = QRectF(rect.left() + 12 * s, rect.top() + 8 * s, rect.width() - 24 * s, 22 * s)
        panel.text(p, title, self.title, DIM, 12, Qt.AlignLeft | Qt.AlignVCenter)
        field = QRectF(rect.left() + 12 * s, title.bottom() + 2 * s, rect.width() - 24 * s, 40 * s)
        panel.box(p, field, PANEL, BORDER, radius=6)
        shown = (self.text or self.placeholder) + "▌"
        panel.text(p, field.adjusted(10 * s, 0, -10 * s, 0), shown, TEXT if self.text else "#5b6577", 20,
                   Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
        top = field.bottom() + 8 * s
        bottom_h = 40 * s
        row_h = max(24 * s, (rect.bottom() - bottom_h - 10 * s - top) / len(self.ROWS) - 4 * s)
        for n, row in enumerate(self.ROWS):
            keys = split(QRectF(rect.left() + 12 * s, top + n * (row_h + 4 * s), rect.width() - 24 * s, row_h),
                         [0] * len(row), 5 * s)
            for box, ch in zip(keys, row):
                self.key(panel, p, box, ch, ch)
        bottom = QRectF(rect.left() + 12 * s, rect.bottom() - bottom_h - 8 * s, rect.width() - 24 * s, bottom_h)
        shift, space, back, ok, cancel = split(bottom, [62 * s, 0, 62 * s, 84 * s, 88 * s], 6 * s)
        self.key(panel, p, shift, "⇧" if self.shift else "⇩", "shift", color="#38d6ff", on=self.shift)
        self.key(panel, p, space, "SPACE", "space")
        self.key(panel, p, back, "⌫", "back")
        self.key(panel, p, ok, "OK", "ok", color="#4ade80")
        self.key(panel, p, cancel, "CANCEL", "cancel", color=WARN)
