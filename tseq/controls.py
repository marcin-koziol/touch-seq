"""The performance controls module: the fader bank (CC/pitch-bend or a plugin parameter) and its `Fader`
gesture."""
import time

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor

from .constants import BG, BORDER, DIM, PANEL, TEXT, WARN
from .gestures import Gesture
from .ui import Panel, param_color, split


class Fader(Gesture):
    """Touch fader: the finger's height inside the track is the value (0..1)."""

    def __init__(self, panel, i, track, pos):
        self.panel, self.i, self.track = panel, i, track
        panel.held.add(i)
        self.move(pos)

    def move(self, pos):
        f = (self.track.bottom() - pos.y()) / self.track.height()
        if self.panel.project.controls[self.i].bipolar and abs(f - 0.5) < 0.025:  # center detent
            f = 0.5
        self.panel.set_value(self.i, f)

    def release(self, pos):
        self.panel.held.discard(self.i)

    def cancel(self):
        self.panel.held.discard(self.i)


class Controls(Panel):
    """Faders for pitch bend (springs back to center like a wheel), mod wheel and CCs. EDIT sets each
    fader's target: a track's instrument (or the selected track), a MIDI channel, or (CC faders, assigned by
    holding a knob in the plugin host) a plugin parameter directly."""
    COLORS = {"pb": "#ff8c42", "mod": "#a78bfa", "cc": "#38d6ff"}
    PB_RETURN_HALF_LIFE = 0.06  # s

    def __init__(self, project, engine, host=None):
        super().__init__()
        self.project, self.engine, self.host = project, engine, host
        self.record_cc = lambda channel, number, value: None  # the rack: a performer writes faders down
        self.selected = lambda: project.tracks[0]  # the rack: the sequencer's selected track
        self.values = [0.5 if c.centered else 0.0 for c in project.controls]
        self.held = set()  # faders under a finger
        self.sent = {}  # fader -> last messages, so unchanged values aren't resent
        self.editing = False
        self.clock = time.monotonic()

    def target_track(self, c):
        if c.track == 0:
            return self.selected()
        return next((t for t in self.project.tracks if t.number == c.track), None)

    def set_value(self, i, f):
        self.values[i] = f = max(0.0, min(1.0, f))
        c = self.project.controls[i]
        if c.target == "param":
            if self.host:
                self.host.set_link(c.param, f)
            return
        tr = self.target_track(c) if c.target == "track" else None
        if c.target == "track" and tr is None:  # its track was deleted
            return
        channel = tr.channel if tr else c.channel
        v, status = round(f * c.max), (0xE0 if c.kind == "pb" else 0xB0) | (channel - 1)
        if c.kind == "pb":
            msgs = [[status, v & 0x7F, v >> 7]]
        elif c.fine:  # MSB first: receivers may reset the LSB when a new MSB arrives
            msgs = [[status, c.cc, v >> 7], [status, c.cc + 32, v & 0x7F]]
        else:
            msgs = [[status, c.cc, v]]
        last = self.sent.get(i, [])
        if msgs != last:
            if len(msgs) == 2 and last[:1] == msgs[:1]:  # only the LSB changed
                self.engine.send(msgs[1], tr)
            else:
                for m in msgs:
                    self.engine.send(m, tr)
            self.sent[i] = msgs
            if c.kind != "pb" and not c.fine:  # a plain CC: recorded into the pattern while REC/OVR is armed
                self.record_cc(channel, c.cc, v)

    def tick(self):
        super().tick()
        now = time.monotonic()
        keep = 0.5 ** ((now - self.clock) / self.PB_RETURN_HALF_LIFE)
        self.clock = now
        for i, c in enumerate(self.project.controls):
            f = self.values[i]
            if c.kind == "pb" and i not in self.held and f != 0.5:
                f = 0.5 + (f - 0.5) * keep
                self.set_value(i, 0.5 if abs(f - 0.5) < 0.002 else f)

    def animating(self):
        # a pitch-bend fader springs back for a moment after the finger lifts
        return super().animating() or any(
            c.kind == "pb" and self.values[i] != 0.5 for i, c in enumerate(self.project.controls))

    def draw(self, p):
        s, g = self.s, 8 * self.s
        p.fillRect(self.rect(), QColor(BG))
        header = QRectF(g, g, self.w - 2 * g, 56 * s)
        info, edit = split(header, [0, 130 * s], 6 * s)
        hint = ("TARGET: tap to switch · a plugin parameter: ASSIGN on its knob in HOST" if self.editing
                else "pitch bend · mod wheel · CC")
        self.text(p, info.adjusted(6 * s, 0, 0, 0), hint, DIM, 15, Qt.AlignLeft | Qt.AlignVCenter)
        self.button(p, edit, "EDIT", lambda: setattr(self, "editing", not self.editing), on=self.editing,
                    color=WARN, sub="assign")

        controls = self.project.controls
        area = QRectF(g, header.bottom() + 12 * s, self.w - 2 * g, self.h - header.bottom() - 12 * s - g)
        strips = split(area, [0, 0, 14 * s] + [0] * (len(controls) - 2), 10 * s)
        del strips[2]  # gap between the wheels and the CC faders
        for i, (c, r) in enumerate(zip(controls, strips)):
            self.draw_strip(p, r, i, c)

    def where(self, c):
        """Short description of a fader's target, for its label."""
        if c.target == "param":
            owner = f"T{c.param[0]}" if c.param[0] else "M"
            return f"{owner}.{c.param[1] + 1}"
        if c.target == "track":
            return "→ SEL" if c.track == 0 else f"→ T{c.track}"
        return f"CH {c.channel}"

    def draw_strip(self, p, r, i, c):
        s = self.s
        linked = c.target == "param"
        if linked and self.host and i not in self.held:  # follow the knob
            v = self.host.link_value(c.param)
            if v is not None:
                self.values[i] = v
        f = self.values[i]
        color = QColor(param_color(c.param[3]) if linked else self.COLORS[c.kind])
        held = i in self.held
        name = c.param[3] if linked else {"pb": "PITCH", "mod": "MOD"}.get(c.kind, f"CC {c.cc}")
        self.text(p, QRectF(r.left(), r.top(), r.width(), 24 * s), name, color.name(), 16 if not linked else 13,
                  bold=True, elide=True)
        flags = " · 14b" if c.fine and not linked else ""
        self.text(p, QRectF(r.left(), r.top() + 24 * s, r.width(), 20 * s), self.where(c) + flags, DIM, 12, elide=True)
        body = QRectF(r.left(), r.top() + 52 * s, r.width(), r.height() - 52 * s - 34 * s)

        if self.editing:
            targets = ("track", "channel")
            label = {"track": "TRACK", "channel": "CHANNEL", "param": "PARAM"}[c.target]
            tb = QRectF(body.left(), body.top(), body.width(), 48 * s)
            self.button(p, tb, label, lambda: self.assign(i, "target", "track" if c.target != "track" else "channel"),
                        on=True, color=color, sub="target")
            y = tb.bottom() + 8 * s
            if linked:
                info = QRectF(body.left(), y, body.width(), 90 * s)
                self.box(p, info, PANEL)
                self.text(p, info.adjusted(4 * s, 4 * s, -4 * s, -info.height() / 2), c.param[3], color.name(), 13,
                          bold=True, elide=True)
                self.text(p, info.adjusted(4 * s, info.height() / 2, -4 * s, -4 * s), self.where(c), DIM, 12)
                unlink = QRectF(body.left(), info.bottom() + 8 * s, body.width(), 48 * s)
                self.button(p, unlink, "UNLINK", lambda: self.assign(i, "target", "track"), color=WARN, sub="→ track")
                return
            if c.target == "track":
                options = [0] + [t.number for t in self.project.tracks]
                at = options.index(c.track) if c.track in options else 0
                box = QRectF(body.left(), y, body.width(), 90 * s)
                self.value_box(p, box, "TRACK", "SEL" if c.track == 0 else f"T{c.track}", lambda: at,
                               lambda v: self.assign(i, "track", options[int(v)]), 0, len(options) - 1, 14)
            else:
                box = QRectF(body.left(), y, body.width(), 90 * s)
                self.value_box(p, box, "CHANNEL", str(c.channel), lambda: c.channel,
                               lambda v: self.assign(i, "channel", int(v)), 1, 16, 14)
            y = box.bottom() + 8 * s
            if c.kind == "cc":
                cc_box = QRectF(body.left(), y, body.width(), 90 * s)
                self.value_box(p, cc_box, "CC", str(c.cc), lambda: c.cc, lambda v: self.assign(i, "cc", int(v)),
                               0, 31 if c.fine else 127, 6)
                y = cc_box.bottom() + 8 * s
            if c.kind != "pb":
                fine = QRectF(body.left(), y, body.width(), 46 * s)
                self.button(p, fine, "14-BIT", lambda: self.assign(i, "fine", not c.fine), on=c.fine, color=color,
                            enabled=c.fine or c.cc < 32, sub=f"+CC{c.cc + 32}" if c.cc < 32 else "CC ≤ 31")
                bip = QRectF(body.left(), fine.bottom() + 8 * s, body.width(), 46 * s)
                self.button(p, bip, "BIPOLAR", lambda: self.assign(i, "bipolar", not c.bipolar), on=c.bipolar,
                            color=color, sub="center")
            return

        tw = min(body.width() * 0.62, 64 * s)
        track = QRectF(body.center().x() - tw / 2, body.top() + 8 * s, tw, body.height() - 16 * s)
        self.box(p, track, "#141821", BORDER, radius=10)
        y = track.bottom() - f * track.height()
        base = track.center().y() if c.centered else track.bottom()
        fill = QColor(color)
        fill.setAlpha(230 if held else 160)
        self.box(p, QRectF(track.left(), min(y, base), tw, abs(base - y)), fill, None, radius=6)
        if c.centered:
            p.fillRect(QRectF(track.left() - 6 * s, base - 1, tw + 12 * s, 2), QColor("#5b6577"))
        cap = QRectF(track.left() - 6 * s, y - 7 * s, tw + 12 * s, 14 * s)
        self.box(p, cap, "#ffffff" if held else "#c9d1de", color.name() if held else None, 2, radius=5)

        value = round(f * c.max)
        text = f"{value - (c.max + 1) // 2:+d}" if c.centered else str(value)
        if linked and self.host:  # the plugin's own reading of the value
            text = self.host.link_text(c.param) or "—"
        self.text(p, QRectF(r.left(), r.bottom() - 30 * s, r.width(), 30 * s), text, TEXT, 16, bold=True)
        self.region(r, lambda pos: Fader(self, i, track, pos))

    def assign(self, i, attr, value):
        c = self.project.controls[i]
        setattr(c, attr, value)
        if attr == "bipolar":  # move the cap to the new resting point; it's sent on the next touch
            self.values[i] = 0.5 if c.centered else 0.0
        self.sent.pop(i, None)
