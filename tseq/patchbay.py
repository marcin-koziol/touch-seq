"""The audio patchbay: every audio port of the machine in two columns, grouped by the device or plugin it
belongs to, with the links drawn as cables - an interface's input into its own output to hear a synth
plugged into it, a plugin into another device. The links made here are kept (host.set_patch) and re-made
when a device comes back; the app's own wiring (plugin chains, the master output) is shown but not touched."""
import time

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainterPath, QPen

import plugins

from .constants import BG, BORDER, DIM, PALETTE, PANEL, PLAY, TEXT
from .gestures import ScrollOrTap
from .ui import Panel, split


class Patchbay(Panel):
    """Sources on the left, destinations on the right, one box per device (its ports inside it) and a cable for
    every link: tap a port and then one on the other side to patch them, the same pair again to cut it. A tap
    on a port with nothing chosen on the other side marks it instead, so either order works; the choice stays
    put, so one source can be patched to several destinations in a row."""

    HEAD = 30  # layout units: a device's title bar, and its port rows
    ROW = 40
    GAP = 130  # between the columns: room for the cables
    AGE = 1.0  # s between looks at the audio graph: a device plugged in shows up by itself

    def __init__(self, links, set_links, ports=plugins.audio_ports, existing=plugins.existing_links, labels=None,
                 internal=None):
        super().__init__()
        self.get_links = links  # () -> [(out, in)]: the links this patchbay made and keeps
        self.set_links = set_links  # ([(out, in)]) -> None: the host saves them and wires them
        self.list_ports = ports  # () -> (sources, destinations)
        self.list_existing = existing  # () -> {(out, in)}: every link on the graph, ours too
        self.list_labels = labels  # () -> {node name: friendlier name}: our chains read as a track / BUS / MASTER
        self.list_internal = internal  # () -> {(out, in)}: the cables the rack makes itself, shown but not ours
        self.sources, self.dests = [], []
        self.colors = {}  # device label -> its color (the same in both columns and on its cables)
        self.patch, self.live, self.internal = set(), set(), set()
        self.src = self.dst = None  # the port chosen on each side
        self.scroll = [0.0, 0.0]
        self.seen = {"out": {}, "in": {}}  # port -> (y, color, edge x) of the rows drawn last frame
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
        names = self.list_labels() if self.list_labels else {}  # T2 · Surge, MASTER · Reverb, ...

        def friendly(node):
            if node in names:
                return names[node]
            return next((label for client, label in names.items() if node.endswith("/" + client)),
                        None)  # Carla namespaces a second engine's clients ("Touch-Seq.42/M.1 Reverb")

        def named(ports):
            return [(port, friendly(port.split(":", 1)[0]) or label, name) for port, label, name in ports]

        sources, dests = named(sources), named(dests)
        patch = set(map(tuple, self.get_links()))
        live = set(map(tuple, self.list_existing()))
        internal = set(map(tuple, self.list_internal())) if self.list_internal else set()
        if (sources, dests, patch, live, internal) == (self.sources, self.dests, self.patch, self.live, self.internal):
            return
        self.sources, self.dests, self.patch, self.live, self.internal = sources, dests, patch, live, internal
        labels = sorted({label for _, label, _ in sources} | {label for _, label, _ in dests})
        self.colors = {label: PALETTE[i % len(PALETTE)] for i, label in enumerate(labels)}
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
        if (out, into) in self.internal:
            return  # the rack wires its own chains (track → bus → master); here they are only shown
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
        head = QRectF(g, g, w - 2 * g, 46 * s)
        title, hint, again = split(head, [300 * s, 0, 130 * s], 12 * s)
        self.text(p, title, "AUDIO PATCHBAY", TEXT, 20, Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        self.text(p, hint, "tap a port, then one on the other side — the same pair again cuts the cable", DIM, 13,
                  Qt.AlignRight | Qt.AlignVCenter, elide=True)
        self.button(p, again, "REFRESH", self.refresh, sub="new device?")
        foot = QRectF(g, h - g - 26 * s, w - 2 * g, 26 * s)
        self.legend(p, foot)
        body = QRectF(g, head.bottom() + 8 * s, w - 2 * g, foot.top() - head.bottom() - 14 * s)
        left, right = split(body, [0, 0], self.GAP * s)
        self.seen = {"out": {}, "in": {}}
        self.column(p, left, 0, "FROM  ·  sources", self.sources, "out")
        self.column(p, right, 1, "TO  ·  destinations", self.dests, "in")
        self.cables(p)

    def legend(self, p, r):
        s, y = self.s, r.center().y()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(PLAY))
        p.drawRect(QRectF(r.left(), y - 1.5 * s, 30 * s, 3 * s))
        self.text(p, QRectF(r.left() + 36 * s, r.top(), 110 * s, r.height()), "your patch", DIM, 12,
                  Qt.AlignLeft | Qt.AlignVCenter)
        p.setPen(QPen(QColor("#7d8594"), 1.6 * s))
        p.drawLine(QPointF(r.left() + 156 * s, y), QPointF(r.left() + 186 * s, y))
        self.text(p, QRectF(r.left() + 192 * s, r.top(), 140 * s, r.height()), "the rack's own", DIM, 12,
                  Qt.AlignLeft | Qt.AlignVCenter)
        p.setPen(QPen(QColor("#3f4654"), 1.6 * s, Qt.DashLine))
        p.drawLine(QPointF(r.left() + 300 * s, y), QPointF(r.left() + 330 * s, y))
        self.text(p, QRectF(r.left() + 336 * s, r.top(), r.width() - 336 * s, r.height()),
                  "another program", DIM, 12, Qt.AlignLeft | Qt.AlignVCenter, elide=True)

    @staticmethod
    def groups(items):
        """[(device label, [(port, port name)])], one entry per device (its ports together, in the order they
        come). Two devices can share a label - the two halves of an interface, two plugins of the same name -
        and still stay two boxes, so their ports never blend into one."""
        order, boxes = [], {}
        for port, label, name in items:
            node = port.split(":", 1)[0]
            if node not in boxes:
                order.append(node)
                boxes[node] = (label, [])
            boxes[node][1].append((port, name))
        return [boxes[node] for node in order]

    def column(self, p, r, which, title, items, side):
        s = self.s
        head = QRectF(r.left(), r.top(), r.width(), 24 * s)
        self.text(p, head.adjusted(12 * s, 0, 0, 0), title, DIM, 13, Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        listing = QRectF(r.left(), head.bottom() + 6 * s, r.width(), r.bottom() - head.bottom() - 6 * s)
        self.box(p, listing, "#0b0d12", BORDER, radius=8)
        groups = self.groups(items)
        content = sum(self.HEAD * s + len(ports) * self.ROW * s for _, ports in groups) + 8 * s
        self.scroll[which] = scroll = max(0.0, min(self.scroll[which], content - listing.height()))
        p.save()
        p.setClipRect(listing.adjusted(3, 3, -3, -3))  # the box edge stays visible around a cut row
        y = listing.top() + 4 * s - scroll
        for label, ports in groups:
            height = self.HEAD * s + len(ports) * self.ROW * s
            box = QRectF(listing.left() + 6 * s, y, listing.width() - 12 * s, height - 4 * s)
            y += height
            if box.bottom() < listing.top() or box.top() > listing.bottom():
                continue
            color = self.colors.get(label, PALETTE[0])
            self.box(p, box, "#141821", BORDER, 1.5, radius=7)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            p.drawRoundedRect(QRectF(box.left() + 8 * s, box.top() + 8 * s, 5 * s, 14 * s), 2, 2)
            self.text(p, QRectF(box.left() + 20 * s, box.top(), box.width() - 60 * s, self.HEAD * s), label,
                      color, 13, Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
            self.text(p, QRectF(box.right() - 46 * s, box.top(), 38 * s, self.HEAD * s), str(len(ports)),
                      DIM, 12, Qt.AlignRight | Qt.AlignVCenter)
            ry = box.top() + self.HEAD * s
            for port, name in ports:
                row = QRectF(box.left() + 8 * s, ry, box.width() - 16 * s, self.ROW * s - 3 * s)
                ry += self.ROW * s
                chosen = (self.src if side == "out" else self.dst) == port
                self.box(p, row, "#1b2130" if chosen else PANEL, "#38d6ff" if chosen else BORDER,
                         2 if chosen else 1.5, radius=5)
                if self.is_mine(side, port) or self.is_live(side, port):
                    self.text(p, QRectF(row.right() - 26 * s, row.top(), 20 * s, row.height()),
                              "●" if self.is_mine(side, port) else "○", PLAY if self.is_mine(side, port) else DIM,
                              13, bold=True)
                self.text(p, row.adjusted(10 * s, 0, -30 * s, 0), name, TEXT, 15,
                          Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
                self.region(row.intersected(listing), lambda pos, port=port, side=side: ScrollOrTap(
                    pos, lambda: self.scroll[which], lambda v: self.scroll.__setitem__(which, v),
                    lambda: self.tap(side, port)))
                if listing.contains(row.center()):  # only what is on screen can be cabled up
                    self.seen[side][port] = (row.center().y(), color, row.right() if side == "out" else row.left())
        p.restore()
        if not items:
            self.text(p, listing, "nothing here — plug the device in and tap REFRESH", DIM, 14)

    def cables(self, p):
        """A curve per link between the two rows it joins (only the ones on screen), then its jack sockets.
        Ours are thick and in the device's color, the rack's own thin grey, another program's thin dashed."""
        s = self.s
        rack = self.internal - self.patch
        others = self.live - self.patch - rack
        for links, mine, fixed in ((rack, False, "#7d8594"), (others, False, None), (self.patch, True, None)):
            for a, b in links:
                src, dst = self.seen["out"].get(a), self.seen["in"].get(b)
                if not src or not dst:
                    continue
                x1, y1, x2, y2 = src[2], src[0], dst[2], dst[0]
                color = QColor(fixed or (src[1] if mine else "#3f4654"))
                if mine:
                    color.setAlpha(225)
                style = Qt.SolidLine if (mine or fixed) else Qt.DashLine
                pen = QPen(color, (3.0 if mine else 1.6) * s, style, Qt.RoundCap)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                bend = max(30 * s, (x2 - x1) * 0.55)
                path = QPainterPath(QPointF(x1, y1))
                path.cubicTo(QPointF(x1 + bend, y1), QPointF(x2 - bend, y2), QPointF(x2, y2))
                p.drawPath(path)
        for links, mine, fixed in ((rack, False, "#7d8594"), (others, False, "#3f4654"), (self.patch, True, None)):
            color = QColor(fixed or (PLAY if mine else "#3f4654"))
            for a, b in links:
                for side, port in (("out", a), ("in", b)):
                    row = self.seen[side].get(port)
                    if row:  # the socket the cable plugs into
                        p.setPen(Qt.NoPen)
                        p.setBrush(color)
                        p.drawEllipse(QPointF(row[2], row[0]), 4.5 * s, 4.5 * s)
