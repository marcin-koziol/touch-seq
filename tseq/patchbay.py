"""The audio patchbay: every audio port of the machine in two columns, so anything can be played into
anything - an interface's input into its own output to hear a synth plugged into it, a plugin into another
device. The links made here are kept (host.set_patch) and re-made when a device comes back; the app's own
wiring (plugin chains, the master output) is shown but not touched."""
import time

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor

import plugins

from .constants import BG, BORDER, DIM, PANEL, PLAY, TEXT
from .gestures import ScrollOrTap
from .ui import Panel, split


class Patchbay(Panel):
    """Sources on the left, destinations on the right: tap one of each to connect them, tap the same pair
    again to cut it. A tap on a row with nothing chosen on the other side marks it instead, so either order
    works; the choice stays put, so one source can be patched to several destinations in a row."""

    ROW = 54  # layout units per port row
    AGE = 1.0  # s between looks at the audio graph: a device plugged in shows up by itself

    def __init__(self, links, set_links, ports=plugins.audio_ports, existing=plugins.existing_links):
        super().__init__()
        self.get_links = links  # () -> [(out, in)]: the links this patchbay made and keeps
        self.set_links = set_links  # ([(out, in)]) -> None: the host saves them and wires them
        self.list_ports = ports  # () -> (sources, destinations)
        self.list_existing = existing  # () -> {(out, in)}: every link on the graph, ours too
        self.sources, self.dests = [], []
        self.patch, self.live = set(), set()
        self.src = self.dst = None  # the port chosen on each side
        self.scroll = [0.0, 0.0]
        self.age = 0.0
        self.load()  # the first frame already shows the graph

    # --- state ----------------------------------------------------------
    def tick(self):
        super().tick()
        if time.monotonic() - self.age >= self.AGE:
            self.load()

    def load(self):
        """Re-read the graph: ports, our links and every other link on it."""
        self.age = time.monotonic()
        sources, dests = self.list_ports()
        patch = set(map(tuple, self.get_links()))
        live = set(map(tuple, self.list_existing()))
        if (sources, dests, patch, live) == (self.sources, self.dests, self.patch, self.live):
            return
        self.sources, self.dests, self.patch, self.live = sources, dests, patch, live
        if self.src not in [p for p, _, _ in sources]:
            self.src = None
        if self.dst not in [p for p, _, _ in dests]:
            self.dst = None
        self.invalidate()  # the graph changed: one frame, nothing here is animating

    def refresh(self):
        self.load()

    # --- linking ---------------------------------------------------------
    def tap(self, side, port):
        if side == "in":
            if self.src is not None:
                self.toggle(self.src, port)
            else:
                self.dst = None if self.dst == port else port
        elif self.dst is not None:
            self.toggle(port, self.dst)
            self.dst = None
        else:
            self.src = None if self.src == port else port

    def toggle(self, out, into):
        self.patch = set(self.patch) ^ {(out, into)}
        self.set_links(sorted(self.patch))
        self.invalidate()

    def is_mine(self, side, port):
        return any((port == a if side == "out" else port == b) for a, b in self.patch)

    def is_live(self, side, port):
        return any((port == a if side == "out" else port == b) for a, b in self.live)

    # --- drawing ---------------------------------------------------------
    def draw(self, p):
        s, w, h, g = self.s, self.w, self.h, 14 * self.s
        p.fillRect(self.rect(), QColor(BG))
        head = QRectF(g, g, w - 2 * g, 50 * s)
        title, hint, again = split(head, [0, 0, 130 * s], 12 * s)
        self.text(p, title, "AUDIO PATCHBAY", TEXT, 20, Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        self.text(p, hint, "tap a source, then where it should go — and again to cut it", DIM, 13,
                  Qt.AlignRight | Qt.AlignVCenter, elide=True)
        self.button(p, again, "REFRESH", self.refresh, sub="new device?")
        foot = QRectF(g, h - g - 26 * s, w - 2 * g, 26 * s)
        self.text(p, QRectF(foot.left(), foot.top(), 16 * s, foot.height()), "●", PLAY, 13, bold=True)
        self.text(p, QRectF(foot.left() + 16 * s, foot.top(), 120 * s, foot.height()), "linked here", DIM, 12,
                  Qt.AlignLeft | Qt.AlignVCenter)
        self.text(p, QRectF(foot.left() + 140 * s, foot.top(), 16 * s, foot.height()), "○", DIM, 13, bold=True)
        self.text(p, QRectF(foot.left() + 156 * s, foot.top(), w - 2 * g - 156 * s, foot.height()),
                  "linked by the app or another program", DIM, 12, Qt.AlignLeft | Qt.AlignVCenter, elide=True)
        body = QRectF(g, head.bottom() + 8 * s, w - 2 * g, foot.top() - head.bottom() - 14 * s)
        left, right = split(body, [0, 0], 14 * s)
        self.column(p, left, 0, "FROM  ·  sources", self.sources, "out")
        self.column(p, right, 1, "TO  ·  destinations", self.dests, "in")

    def column(self, p, r, which, title, items, side):
        s = self.s
        head = QRectF(r.left(), r.top(), r.width(), 26 * s)
        self.box(p, QRectF(head.left(), head.top() + 4 * s, 5 * s, head.height() - 8 * s), "#38d6ff", None, radius=2)
        self.text(p, head.adjusted(14 * s, 0, 0, 0), title, DIM, 13, Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        listing = QRectF(r.left(), head.bottom() + 6 * s, r.width(), r.bottom() - head.bottom() - 6 * s)
        self.box(p, listing, "#0b0d12", BORDER, radius=8)
        rh = self.ROW * s
        self.scroll[which] = scroll = max(0.0, min(self.scroll[which], len(items) * rh - listing.height() + 8 * s))
        p.save()
        p.setClipRect(listing.adjusted(3, 3, -3, -3))  # keep the box edge visible around a cut row
        first = int(scroll // rh)
        for n in range(first, min(len(items), first + int(listing.height() / rh) + 2)):
            port, label, name = items[n]
            row = QRectF(listing.left() + 6 * s, listing.top() + 4 * s + n * rh - scroll,
                         listing.width() - 12 * s, rh - 8 * s)
            chosen = (self.src if side == "out" else self.dst) == port
            self.box(p, row, "#1b2130" if chosen else PANEL, "#38d6ff" if chosen else BORDER,
                     2 if chosen else 1.5, radius=6)
            if self.is_mine(side, port) or self.is_live(side, port):
                self.text(p, QRectF(row.right() - 30 * s, row.top(), 22 * s, row.height()),
                          "●" if self.is_mine(side, port) else "○", PLAY if self.is_mine(side, port) else DIM,
                          15, bold=True)
            inner = row.adjusted(12 * s, 4 * s, -34 * s, -4 * s)
            self.text(p, QRectF(inner.left(), inner.top(), inner.width(), 20 * s), label, DIM, 12,
                      Qt.AlignLeft | Qt.AlignTop, elide=True)
            self.text(p, QRectF(inner.left(), inner.top() + 18 * s, inner.width(), 24 * s), name, TEXT, 16,
                      Qt.AlignLeft | Qt.AlignTop, bold=True, elide=True)
            self.region(row.intersected(listing), lambda pos, port=port, side=side: ScrollOrTap(
                pos, lambda: self.scroll[which], lambda v: self.scroll.__setitem__(which, v),
                lambda: self.tap(side, port)))
        p.restore()
        if not items:
            self.text(p, listing, "nothing here — plug the device in and tap REFRESH", DIM, 14)
