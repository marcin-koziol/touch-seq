"""The step sequencer module: the pattern/scene grid, the piano roll and the drum lanes, the transport and
tempo, and the step-painting gestures that belong to it."""
import copy
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPolygonF

from .constants import (BG, BORDER, DIM, DRUM_COLORS, DRUM_KIT, GATE_RES, MAX_GATE, MAX_STEPS,
                        PALETTE, PANEL, PATTERNS, PLAY, SCENES, STEPS_PER_BAR, TEXT, WARN)
from .gestures import Drag, Gesture, Hold, ScrollOrTap
from .model import DEFAULT_STEP, SongStep, Step
from .music import note_name, transpose
from .ui import Panel, glyph, split


class Paint(Gesture):
    """Step row: first touch toggles a step, sliding sets the following steps to the same state.
    `is_on(i)` / `set_on(i, on)` define what a step is (see Sequencer.painter)."""

    def __init__(self, ui, lane, is_on, set_on, pos):
        self.ui, self.lane, self.set_on = ui, lane, set_on
        i = ui.cell_at(lane, pos)
        self.target = not is_on(i)
        self.done = {i}
        set_on(i, self.target)

    def move(self, pos):
        i = self.ui.cell_at(self.lane, pos)
        if i not in self.done:
            self.done.add(i)
            self.set_on(i, self.target)


class Roll(Gesture):
    """Polyphonic piano roll; a note is (step, pitch).
    On an empty spot: a touch places a note; sliding sideways places the pitch on further steps, sliding
    up/down within the column moves the note just placed (no trail of chord notes).
    On a note (anywhere in its first cell or along its tail): sideways stretches it (gate may span several
    steps), up/down moves it to another pitch, a tap removes it."""

    def __init__(self, ui, tr, cell_at, step_pos, pitch_at, note_at, pos):
        self.ui, self.tr, self.cell_at, self.step_pos, self.pitch_at = ui, tr, cell_at, step_pos, pitch_at
        self.pattern = tr.current
        i, n = cell_at(pos), pitch_at(pos)
        grab = note_at(i, n)  # step where the note under the finger starts
        self.p0, self.axis = pos, None
        if grab is None:
            self.held, self.placing = (i, n), True
            ui.place(tr, self.pattern, i, n)
        else:
            self.held, self.placing = (grab, n), False
            self.gate0 = self.pattern.lane(n)[grab].gate

    def move(self, pos):
        i, n = self.held
        if self.placing:
            at = (self.cell_at(pos), self.pitch_at(pos))
            if at[0] == i and at[1] != n:
                self.ui.move_note(self.tr, self.pattern, i, n, at[1])
            elif at[0] != i:
                self.ui.place(self.tr, self.pattern, *at)
            self.held = at
            return
        if self.axis is None:  # lock to the first clear direction so a stretch doesn't re-pitch
            dx, dy = pos.x() - self.p0.x(), pos.y() - self.p0.y()
            if max(abs(dx), abs(dy)) < Drag.THRESHOLD:
                return
            self.axis = "x" if abs(dx) > abs(dy) else "y"
        if self.axis == "x":
            lane = self.pattern.lane(n)
            gate = self.gate0 + self.step_pos(pos) - self.step_pos(self.p0)
            lane[i].gate = max(GATE_RES, min(self.ui.room(lane, i), round(gate / GATE_RES) * GATE_RES))
            st = lane[i]
            self.tr.last = copy.copy(st)
        elif self.pitch_at(pos) != n:
            self.ui.move_note(self.tr, self.pattern, i, n, self.pitch_at(pos))
            self.held = (i, self.pitch_at(pos))

    def release(self, pos):
        if not self.placing and self.axis is None:
            i, n = self.held
            self.pattern.lane(n)[i].on = False


class SlotDrag(Gesture):
    """Pattern slot: a tap selects it; dropping it on another slot copies the pattern there and switches to it."""

    def __init__(self, ui, src, slot_at):
        self.ui, self.src, self.slot_at, self.target = ui, src, slot_at, src

    def move(self, pos):
        self.target = self.slot_at(pos)

    def release(self, pos):
        target = self.slot_at(pos)
        if target == self.src:
            self.ui.press_pattern(self.src)
        elif target is not None:
            self.ui.drag_pattern(self.src, target)


class LaneDraw(Gesture):
    """Velocity/gate lane: the finger's height sets the value of the notes starting at the step under it."""

    def __init__(self, ui, tr, lane, param, pos):
        self.ui, self.tr, self.lane, self.param, self.pattern = ui, tr, lane, param, tr.current
        self.move(pos)

    def move(self, pos):
        f = max(0.0, min(1.0, (self.lane.bottom() - pos.y()) / self.lane.height()))
        for _, st in self.pattern.notes_at(self.ui.cell_at(self.lane, pos)):
            if self.param == "vel":
                st.vel = max(1, round(f * 127))
            else:
                st.gate = max(0.05, round(f * 20) / 20)
            self.tr.last = copy.copy(st)


class Sequencer(Panel):
    def __init__(self, project, engine):
        super().__init__()
        self.project, self.engine = project, engine
        self.sel = project.tracks[0]
        self.view = "grid"
        self.page = 0
        self.param = "vel"  # melody edit view: what the lane under the piano roll edits ("vel" / "gate")
        self.roll_bases = {}  # Track -> lowest visible piano roll note
        self.roll_rows = 12
        self.drum_param = "step"  # drum edit view: "step" paints on/off, "vel" drags velocity
        self.mode = None  # "copy" / "clear" act on pattern slots, "save" on scenes
        self.scroll = 0.0
        self.song_sel = 0  # the song step the panel below shows
        self.song_scroll = 0.0
        self.song_pick = None  # (step, track number) while choosing a pattern for it
        self.master_fx = lambda: False  # the rack: is XYFX on the master (adds a MASTER row)
        self.master_focus = False  # the MASTER row is selected (PLAY-12 shows the XY pad)
        self.new_track = lambda: None  # the rack: the "+" dialog (drum, melody, external MIDI, audio)
        self.choose_source = lambda tr: None  # the rack: pick the input of an audio track
        self.choose_input = lambda tr: None  # the rack: pick the MIDI keyboard a track plays from
        self.input_ok = None  # the rack: is the track's MIDI input open? (None: unknown, assume it is)
        self.reroute = lambda: None  # the rack: rebuild the audio wiring after an input or pan change

    # --- actions --------------------------------------------------------
    def set_step(self, tr, s, on):
        if on and not s.on:
            s.vel, s.gate = tr.last.vel, tr.last.gate
        s.on = on

    def painter(self, tr, k):
        """(is_on, set_on) for painting steps of lane k; with k=None (a melody in the grid view) a step is
        the whole column: it is on if any note starts there, and painting adds the latest pitch."""
        pat = tr.current
        if k is not None:
            steps = pat.lane(k)
            return (lambda i: steps[i].on), (lambda i, on: self.set_step(tr, steps[i], on))

        def set_on(i, on):
            if on:
                self.set_step(tr, pat.lane(tr.last_note)[i], True)
            for _, st in ([] if on else pat.notes_at(i)):
                st.on = False

        return (lambda i: bool(pat.notes_at(i))), set_on

    def edit_step(self, tr, s, param, value):
        s.on = True
        setattr(s, param, value)
        tr.last = copy.copy(s)

    def pick_voice(self, tr, v):
        tr.voice = v
        self.engine.preview(tr, DRUM_KIT[v][1])

    def toggle_play(self):
        e = self.engine
        e.stop() if (e.playing or e.counting) else e.start()

    def toggle_mode(self, mode):
        self.mode = None if self.mode == mode else mode

    def press_pattern(self, i):
        tr = self.sel
        with self.engine.lock:
            if self.mode == "copy":
                tr.patterns[i] = copy.deepcopy(tr.current)
            elif self.mode == "clear":
                length = tr.patterns[i].length  # clearing takes the notes out, not the length you set
                tr.patterns[i] = tr.new_pattern()
                tr.patterns[i].length = length
            else:
                self.engine.queue_pattern(tr, i)
        self.mode = None

    def drag_pattern(self, src, dst):
        """Copy pattern src into slot dst and switch to it (at the next bar while playing)."""
        tr = self.sel
        with self.engine.lock:
            tr.patterns[dst] = copy.deepcopy(tr.patterns[src])
            self.engine.queue_pattern(tr, dst)
        self.mode = None

    def press_scene(self, i):
        if self.mode == "save":
            self.engine.store_scene(i)
            self.mode = None
        else:
            self.engine.queue_scene(i)

    def select(self, tr):
        if tr is self.sel and not self.master_focus:
            self.view = "edit"
        self.sel, self.master_focus = tr, False

    def add_track(self, kind, value=""):
        self.sel = self.engine.add_track(kind, value)
        self.scroll = 1e9  # clamped on paint: shows the new row
        self.reroute()  # a new audio track's input is wired right away, not on the next edit

    def set_mute(self, tr):
        """Muting an audio track is cutting its input cable; for the others the host applies the level again
        (a rewire does both, and changes nothing when the routing is already right)."""
        tr.mute = not tr.mute
        self.reroute()

    def set_pan(self, tr, pan):
        """Where a mono input goes: left, both (center) or right."""
        tr.pan = pan
        self.reroute()

    def cycle_destination(self, tr):
        """A track's chain or input goes into the master or the next bus."""
        options = [0] + [b.number for b in self.project.buses]
        tr.bus = options[(options.index(tr.bus) + 1) % len(options)] if tr.bus in options else 0
        self.reroute()

    def transpose(self, tr, steps):
        """The track's current pattern up (down for a negative `steps`) by scale degrees, in the project's key:
        the intervals stay, notes outside the scale keep their alteration, and a note pushed out of range
        stays where it is. Drum tracks have fixed voices, so they are left alone."""
        if tr.drum:
            return
        moved = {}
        for note, lane in tr.current.lanes.items():
            to = transpose(note, steps, self.project.key, self.project.scale)
            moved[to if 0 <= to <= 127 else note] = lane
        tr.current.lanes = moved
        tr.last_note = min(127, max(0, transpose(tr.last_note, steps, self.project.key, self.project.scale)))
        self.roll_bases.pop(tr, None)  # the roll re-centres on where the notes are now

    def delete_track(self):
        tracks = self.project.tracks
        if len(tracks) > 1:
            i = tracks.index(self.sel)
            self.engine.remove_track(self.sel)
            self.sel = tracks[min(i, len(tracks) - 1)]
            self.view = "grid"

    def color(self, tr):
        return QColor(PALETTE[(tr.number - 1) % len(PALETTE)])

    def step_fill(self, color, st, head, k):
        """Background of a step cell: track color by velocity when on, else a 4-step grouped gray."""
        if st.on:
            fill = QColor(color)
            fill.setAlpha(round(90 + 165 * st.vel / 127))
            return fill
        return QColor("#323a4a" if head else "#1d2230" if k % 4 == 0 else "#181c26")

    # --- frame ----------------------------------------------------------
    def draw(self, p):
        w, h, s = self.w, self.h, self.s
        p.fillRect(self.rect(), QColor(BG))
        if self.sel not in self.project.tracks:
            self.sel = self.project.tracks[0]
        top = QRectF(0, 0, w, 64 * s)
        bottom = QRectF(0, h - 70 * s, w, 70 * s)
        mid = QRectF(0, top.bottom(), w, bottom.top() - top.bottom())
        self.draw_top(p, top.adjusted(8 * s, 8 * s, -8 * s, -4 * s))
        self.draw_bottom(p, bottom.adjusted(8 * s, 4 * s, -8 * s, -8 * s))
        {"grid": self.draw_grid, "edit": self.draw_edit, "song": self.draw_song}[self.view](p, mid)

    def draw_top(self, p, r):
        s, e = self.s, self.engine
        play, bar, song, grid, edit, _, *scenes, save = split(
            r, [86 * s, 110 * s, 72 * s, 72 * s, 72 * s, 0] + [54 * s] * SCENES + [72 * s], 6 * s)

        active = e.playing or e.counting
        self.button(p, play, "", self.toggle_play, on=e.playing, color=PLAY, pending=e.counting)
        c, k = play.center(), play.height() * 0.22
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(BG if active else TEXT))
        if active:
            p.drawRect(QRectF(c.x() - k, c.y() - k, 2 * k, 2 * k))
        else:
            p.drawPolygon(QPolygonF([QPointF(c.x() - k, c.y() - k * 1.2), QPointF(c.x() - k, c.y() + k * 1.2),
                                     QPointF(c.x() + k * 1.3, c.y())]))

        pr = self.project
        self.box(p, bar, PANEL)
        si, counting = e.step_index, e.counting
        if counting:  # the count-in: beats left, dots walking to the downbeat
            label, beat = f"-{int(e.count_left)}", (4 - int(e.count_left)) % 4
        else:
            label = f"{si // STEPS_PER_BAR + 1}" if si >= 0 else "-"
            beat = (si % STEPS_PER_BAR) // 4 if si >= 0 else -1
        self.text(p, bar.adjusted(10 * s, 0, 0, 0), label, TEXT, 20, Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        cell = (bar.width() - 54 * s) / 4
        for b in range(4):
            br = QRectF(bar.left() + 44 * s + b * cell, bar.center().y() - 7 * s, cell - 6 * s, 14 * s)
            self.box(p, br, PLAY if b == beat else "#222734", None, radius=3)

        self.button(p, song, "SONG", lambda: setattr(self, "view", "song"), on=self.view == "song")
        self.button(p, grid, "GRID", lambda: setattr(self, "view", "grid"), on=self.view == "grid")
        self.button(p, edit, "EDIT", lambda: setattr(self, "view", "edit"), on=self.view == "edit")

        save_mode = self.mode == "save"
        for i, r_ in enumerate(scenes):
            has = any(tr.scenes[i] is not None for tr in pr.tracks)
            self.button(p, r_, f"S{i + 1}", lambda i=i: self.press_scene(i), on=e.scene == i,
                        color=WARN if save_mode else "#facc15", pending=e.pending_scene == i or save_mode,
                        sub="●" if has else "·")
        self.button(p, save, "SAVE", lambda: self.toggle_mode("save"), on=save_mode, color=WARN, sub="scene")

    def draw_bottom(self, p, r):
        s, tr, e = self.s, self.sel, self.engine
        if tr.audio:  # an input has no patterns to switch or copy
            self.text(p, r, "an audio track has no patterns: EDIT picks its input, HOST-4 its effects", DIM, 14,
                      Qt.AlignLeft | Qt.AlignVCenter, elide=True)
            return
        widths = [52 * s] * 4 + [4 * s] + [0] * PATTERNS + [4 * s, 72 * s, 72 * s]
        rects = split(r, widths, 5 * s)
        pages, slots, (copy_r, clear_r) = rects[:4], rects[5:5 + PATTERNS], rects[-2:]
        color = self.color(tr)
        head = e.playhead.get(tr)
        for i, pr_ in enumerate(pages):
            self.button(p, pr_, str(i + 1), lambda i=i: setattr(self, "page", i), on=self.page == i,
                        enabled=True, sub="▸" if head is not None and head // 16 == i else
                        ("" if i * 16 < tr.current.length else "—"))
        nxt = e.next_pattern(tr)

        def slot_at(pos):
            for i, sr in enumerate(slots):
                if sr.adjusted(-3 * s, -40 * s, 3 * s, 40 * s).contains(pos):
                    return i
            return None

        drag = next((g for g in self.gestures.values() if isinstance(g, SlotDrag)), None)
        for i, sr in enumerate(slots):
            used = not tr.patterns[i].empty()
            self.button(p, sr, str(i + 1), None, on=tr.pattern == i, color=color,
                        pending=(nxt == i and nxt != tr.pattern) or self.mode in ("copy", "clear"),
                        sub="●" if used else "·")
            self.hits[-1] = (sr, lambda pos, i=i: SlotDrag(self, i, slot_at))
            if drag and drag.target == i and i != drag.src:  # drop target; red = will overwrite
                self.box(p, sr.adjusted(-2 * s, -2 * s, 2 * s, 2 * s), QColor(0, 0, 0, 0),
                         WARN if used else "#ffffff", 3)
        self.button(p, copy_r, "COPY", lambda: self.toggle_mode("copy"), on=self.mode == "copy", sub="to slot")
        self.button(p, clear_r, "CLR", lambda: self.toggle_mode("clear"), on=self.mode == "clear", color=WARN,
                    sub="slot")

    # --- grid view: all tracks, one page ---------------------------------
    def cells(self, lane):
        s = self.s
        gap, group = 4 * s, 12 * s
        w = (lane.width() - 12 * gap - 3 * group) / 16
        out, x = [], lane.left()
        for k in range(16):
            out.append(QRectF(x, lane.top(), w, lane.height()))
            x += w + (group if k % 4 == 3 else gap)
        return out

    def cell_at(self, lane, pos):
        cells = self.cells(lane)
        k = min(range(16), key=lambda k: abs(cells[k].center().x() - pos.x()))
        return self.page * 16 + k

    def step_pos(self, lane, pos):
        """Fractional position (in steps, from the page start) under pos.x()."""
        cells = self.cells(lane)
        k = max([k for k in range(16) if cells[k].left() <= pos.x()], default=0)
        return k + max(0.0, min(1.0, (pos.x() - cells[k].left()) / cells[k].width()))

    @staticmethod
    def step_x(cells, e, right):
        """x of the position `e` steps into the page (gaps between cells are skipped)."""
        j = math.ceil(e) - 1
        if j >= len(cells):
            return right
        return cells[j].left() + (e - j) * cells[j].width()

    # --- song view: the arrangement above the patterns ---------------------
    def toggle_song(self):
        """The transport follows the song (or stops following it, back to the patterns playing)."""
        pr = self.project
        pr.song_on = not pr.song_on
        if pr.song_on:
            self.engine.song_jump = self.song_sel  # where it starts (also while stopped)

    def add_song_step(self):
        """A new step at the end: everything keeps playing what it plays, then you change what should change."""
        pr = self.project
        patterns = {tr.number: tr.pattern for tr in pr.tracks if not tr.audio}
        pr.song.append(SongStep(bars=4, repeats=1, patterns=patterns))
        self.song_sel = len(pr.song) - 1
        self.song_scroll = 1e9  # clamped on paint: the new step shows up
        if len(pr.song) == 1 and not pr.song_on:
            pr.song_on = True  # the first step also switches the song on

    def delete_song_step(self, n):
        steps = self.project.song
        if 0 <= n < len(steps):
            steps.pop(n)
            self.song_sel = max(0, min(self.song_sel, len(steps) - 1))
            if not steps:
                self.project.song_on = False

    def cycle_song_bars(self, n):
        step = self.project.song[n]
        step.bars = {1: 2, 2: 4, 4: 8, 8: 16, 16: 1}.get(step.bars, 4)

    def cycle_song_repeats(self, n):
        step = self.project.song[n]
        step.repeats = {1: 2, 2: 4, 4: 8, 8: 1}.get(step.repeats, 1)

    def select_song_step(self, n):
        """Tap a step: it is the one edited on the right, and the song jumps there at the next bar."""
        self.song_sel = n
        if self.project.song_on:
            self.engine.queue_song_step(n)

    def open_song_pick(self, n, number):
        self.song_pick = (n, number)

    def pick_song_pattern(self, k):
        if not self.song_pick:
            return
        n, number = self.song_pick
        if 0 <= n < len(self.project.song):
            self.project.song[n].patterns[number] = k
        self.song_pick = None

    def clear_song_pattern(self):
        """Leave the track out of the step: it keeps playing the pattern it had."""
        if not self.song_pick:
            return
        n, number = self.song_pick
        if 0 <= n < len(self.project.song):
            self.project.song[n].patterns.pop(number, None)
        self.song_pick = None

    def draw_song(self, p, area):
        s, pr, e = self.s, self.project, self.engine
        if self.song_pick:
            self.draw_song_picker(p, area)
            return
        steps = pr.song
        left, right = split(area, [area.width() * 0.54, 0], 12 * s)
        head = QRectF(left.left(), left.top(), left.width(), 44 * s)
        on_b, loop_b, count = split(head, [124 * s, 104 * s, 0], 8 * s)
        self.button(p, on_b, "SONG", self.toggle_song, on=pr.song_on, sub="the steps play")
        self.button(p, loop_b, "LOOP", lambda: setattr(pr, "song_loop", not pr.song_loop), on=pr.song_loop,
                    sub="at the end")
        self.text(p, count, f"{len(steps)} step" + ("s" if len(steps) != 1 else "") if steps else "no steps yet", DIM, 12,
                  Qt.AlignRight | Qt.AlignVCenter)
        now, bar = e.song_position()
        rows = QRectF(left.left(), head.bottom() + 6 * s, left.width(),
                      left.bottom() - head.bottom() - 6 * s - 52 * s)
        rh = 62 * s
        self.song_scroll = max(0.0, min(self.song_scroll, len(steps) * rh - rows.height()))
        p.save()
        p.setClipRect(rows)
        if not steps:
            self.text(p, rows, "no steps yet: + STEP makes the first one", "#5b6577", 14)
        first = int(self.song_scroll // rh)
        for n in range(first, min(len(steps), first + int(rows.height() // rh) + 2)):
            step = steps[n]
            row = QRectF(rows.left(), rows.top() + n * rh - self.song_scroll, rows.width(), rh - 6 * s)
            playing, chosen = now == n, n == self.song_sel
            self.box(p, row, "#1b2130" if playing or chosen else PANEL,
                     PLAY if playing else "#98a2b3" if chosen else BORDER, 2 if playing or chosen else 1.5,
                     radius=8)
            num, chips, bars_b, reps_b, close = split(row.adjusted(4 * s, 4 * s, -4 * s, -4 * s),
                                                      [38 * s, 0, 86 * s, 50 * s, 30 * s], 6 * s)
            self.text(p, num, str(n + 1), PLAY if playing else TEXT, 19, bold=True)
            if playing:  # how far into the step the playhead is
                done = bar / max(1, step.span)
                self.box(p, QRectF(num.left() + 5 * s, num.bottom() - 8 * s, (num.width() - 10 * s) * done, 3 * s),
                         PLAY, None, radius=2)
            tracks = [t for t in pr.tracks if not t.audio]
            shown = tracks[:5]
            cw = chips.width() / max(1, len(shown))
            for j, tr in enumerate(shown):
                cell = QRectF(chips.left() + j * cw, chips.top(), cw - 4 * s, chips.height())
                pat = step.patterns.get(tr.number)
                self.box(p, cell, "#11141b", "#2a3040", 1, radius=5)
                self.text(p, cell.adjusted(0, 1 * s, 0, -cell.height() * 0.42), f"T{tr.number}", self.color(tr), 9,
                          Qt.AlignHCenter | Qt.AlignTop, bold=True)
                self.text(p, cell.adjusted(0, cell.height() * 0.36, 0, 0),
                          f"P{pat + 1}" if pat is not None else "—", TEXT if pat is not None else "#5b6577",
                          13, bold=True)
            if len(tracks) > len(shown):
                self.text(p, QRectF(chips.right() - 30 * s, chips.top(), 26 * s, chips.height()), f"+{len(tracks) - len(shown)}",
                          DIM, 10)
            self.button(p, bars_b, f"{step.bars} bars", None)
            self.button(p, reps_b, f"×{step.repeats}", None, on=step.repeats > 1)
            self.button(p, close, "✕", None, color=WARN)
            self.region(row.intersected(rows), lambda pos, n=n: ScrollOrTap(
                pos, lambda: self.song_scroll, lambda v: setattr(self, "song_scroll", v),
                lambda: self.select_song_step(n)))
            self.on_press(bars_b.intersected(rows), lambda n=n: self.cycle_song_bars(n))
            self.on_press(reps_b.intersected(rows), lambda n=n: self.cycle_song_repeats(n))
            self.region(close.intersected(rows), lambda pos, n=n: Hold(lambda: self.delete_song_step(n),
                                                                       key=("song", n)))
        p.restore()
        plus = QRectF(left.left(), left.bottom() - 44 * s, left.width(), 40 * s)
        self.button(p, plus, "+ STEP", self.add_song_step, sub="append it at the end")
        self.draw_song_step(p, right, steps)

    def draw_song_step(self, p, r, steps):
        """The tracks of the selected step: tap a pattern to change what that track plays in it."""
        s, pr = self.s, self.project
        n = min(self.song_sel, len(steps) - 1) if steps else 0
        self.text(p, QRectF(r.left(), r.top(), r.width(), 44 * s),
                  f"STEP {n + 1} · which pattern" if steps else "STEP", DIM, 12,
                  Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        if not steps:
            self.text(p, QRectF(r.left(), r.top() + 48 * s, r.width(), 60 * s),
                      "add a step on the left, then pick the patterns here", "#5b6577", 13,
                      Qt.AlignLeft | Qt.AlignTop, elide=True)
            return
        step = steps[n]
        tracks = [t for t in pr.tracks if not t.audio]
        top = r.top() + 48 * s
        rh = min(70 * s, (r.bottom() - top - 58 * s) / max(1, len(tracks)))
        for j, tr in enumerate(tracks):
            row = QRectF(r.left(), top + j * rh, r.width(), rh - 6 * s)
            color = self.color(tr)
            self.box(p, row, PANEL, BORDER, 1.5, radius=8)
            self.box(p, QRectF(row.left() + 4 * s, row.top() + 10 * s, 4 * s, row.height() - 20 * s), color, None,
                     radius=2)
            self.text(p, row.adjusted(18 * s, 0, 0, 0), f"T{tr.number}", color, 15,
                      Qt.AlignLeft | Qt.AlignVCenter, bold=True)
            pick = QRectF(row.left() + 74 * s, row.top() + 6 * s, row.width() - 82 * s, row.height() - 12 * s)
            pat = step.patterns.get(tr.number)
            self.button(p, pick, f"P{pat + 1}" if pat is not None else "— keep previous", None, sub="tap: pick")
            self.on_press(pick, lambda n=n, number=tr.number: self.open_song_pick(n, number))
        self.text(p, QRectF(r.left(), r.bottom() - 46 * s, r.width(), 20 * s),
                  "tap a step: play it from the next bar · tap bars/×repeats: change · hold ✕: delete", DIM, 11,
                  Qt.AlignLeft | Qt.AlignVCenter, elide=True)
        self.text(p, QRectF(r.left(), r.bottom() - 26 * s, r.width(), 20 * s),
                  "a pattern keeps its own length and loops inside the step", DIM, 11,
                  Qt.AlignLeft | Qt.AlignVCenter, elide=True)

    def draw_song_picker(self, p, area):
        """Which of the track's patterns that step plays."""
        s, pr = self.s, self.project
        n, number = self.song_pick
        step = pr.song[n] if 0 <= n < len(pr.song) else None
        p.fillRect(area, QColor(0, 0, 0, 170))
        self.on_press(area, lambda: setattr(self, "song_pick", None))  # tap outside closes
        panel = QRectF(0, 0, min(area.width() - 24 * s, 760 * s), min(area.height() - 16 * s, 380 * s))
        panel.moveCenter(area.center())
        self.box(p, panel, "#11141b", "#38d6ff", 2, radius=14)
        self.on_press(panel, lambda: None)
        inner = panel.adjusted(18 * s, 12 * s, -18 * s, -14 * s)
        self.text(p, QRectF(inner.left(), inner.top(), inner.width(), 26 * s),
                  f"T{number} · pattern in step {n + 1}", TEXT, 16, Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        grid = QRectF(inner.left(), inner.top() + 30 * s, inner.width(), inner.height() - 30 * s - 50 * s)
        halves = [split(QRectF(grid.left(), grid.top() + j * grid.height() / 2, grid.width(), grid.height() / 2),
                        [0] * 8, 8 * s) for j in range(2)]
        current = step.patterns.get(number) if step else None
        for k in range(PATTERNS):
            cell = halves[k // 8][k % 8]
            used = not self.project.tracks and False
            tr = next((t for t in pr.tracks if t.number == number), None)
            used = bool(tr and not tr.patterns[k].empty())
            self.button(p, cell, f"P{k + 1}", lambda k=k: self.pick_song_pattern(k), on=current == k,
                        sub="·" if used else "")
        foot = QRectF(inner.left(), grid.bottom() + 8 * s, inner.width(), 40 * s)
        keep, cancel = split(foot, [0, 140 * s], 10 * s)
        self.button(p, keep, "— keep previous", self.clear_song_pattern)
        self.button(p, cancel, "CANCEL", lambda: setattr(self, "song_pick", None))

    def draw_grid(self, p, r):
        s, tracks = self.s, self.project.tracks
        g, rh, hw = 6 * s, 64 * s, 176 * s
        master = self.master_fx()
        self.master_focus = self.master_focus and master
        rows = tracks + (["master"] if master else []) + [None]
        self.scroll = max(0.0, min(self.scroll, len(rows) * rh + g - r.height()))
        p.save()
        p.setClipRect(r)
        for idx, tr in enumerate(rows):
            y = r.top() + g + idx * rh - self.scroll
            head = QRectF(r.left() + 8 * s, y, hw, rh - g)
            if not head.intersects(r):
                continue
            if tr == "master":
                self.draw_master_row(p, head, r, QRectF(head.right() + 10 * s, y, r.right() - head.right() - 18 * s, rh - g))
                continue
            if tr is None:  # the one "+" that adds a track: the rack opens the choice of kind (and MIDI port)
                self.button(p, head, "+", self.new_track, sub="new track")
                self.hits[-1] = (head.intersected(r), self.hits[-1][1])
                continue
            lane = QRectF(head.right() + 10 * s, y, r.right() - head.right() - 18 * s, rh - g)
            self.draw_header(p, head, r, tr)
            if tr.audio:  # no steps: the row shows the input it takes and where it goes
                self.draw_audio_row(p, lane, tr)
                continue
            self.draw_lane(p, lane, tr)
            k = tr.voice if tr.drum else None
            self.region(lane.intersected(r), lambda pos, tr=tr, k=k, lane=lane: Paint(self, lane, *self.painter(tr, k), pos))
        p.restore()

    def draw_master_row(self, p, head, clip, lane):
        """The master bus as a row: selecting it shows the XY pad (XYFX on the master) in PLAY-12."""
        s, on = self.s, self.master_focus
        self.box(p, head, "#1f1d12" if on else PANEL, "#facc15" if on else BORDER, 2 if on else 1.5)
        self.text(p, head.adjusted(18 * s, 6 * s, -8 * s, -6 * s), "MASTER", "#facc15", 19,
                  Qt.AlignLeft | Qt.AlignTop, bold=True)
        self.text(p, head.adjusted(18 * s, 6 * s, -8 * s, -6 * s), "XYFX", DIM, 12, Qt.AlignLeft | Qt.AlignBottom)
        self.on_press(head.intersected(clip), lambda: setattr(self, "master_focus", True))
        self.box(p, lane, "#11141b", "#2a3040", 1.5, radius=6)
        self.text(p, lane, "XY pad → PLAY-12", "#5b6577", 14)

    def draw_header(self, p, head, clip, tr):
        s, e, color = self.s, self.engine, self.color(tr)
        selected = tr is self.sel
        self.box(p, head, "#1b2130" if selected else PANEL, color if selected else BORDER, 2 if selected else 1.5)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(QRectF(head.left() + 5 * s, head.top() + 8 * s, 5 * s, head.height() - 16 * s), 2, 2)
        info = head.adjusted(18 * s, 6 * s, -56 * s, -6 * s)
        self.text(p, info, f"T{tr.number}", TEXT, 19, Qt.AlignLeft | Qt.AlignTop, bold=True)
        right = info
        if tr.in_port:  # a MIDI keyboard is set for this track: a small socket says so (green when it is here)
            ready = self.input_ok is None or self.input_ok(tr)
            socket = QRectF(info.right() - 17 * s, info.top() - 2 * s, 17 * s, 17 * s)
            self.box(p, socket, "#11141b", "#4ade80" if ready else "#ff8c42", 1.2, radius=4)
            glyph(p, socket.adjusted(1.5 * s, 1.5 * s, -1.5 * s, -1.5 * s), "din",
                  "#4ade80" if ready else "#ff8c42", width=0.12)
            right = info.adjusted(0, 0, -20 * s, 0)
        self.text(p, right, "MONO" if tr.audio else f"CH{tr.channel}", DIM, 12, Qt.AlignRight | Qt.AlignTop)
        if tr.drum:
            self.text(p, info, DRUM_KIT[tr.voice][0], color, 12, Qt.AlignRight | Qt.AlignBottom, bold=True)
        elif tr.audio:  # the input it takes and how it is panned, instead of a drum voice and a pattern
            self.text(p, info, f"{self.source_name(tr) or 'no input'} · {tr.pan.upper()}",
                      TEXT if tr.source else WARN, 12, Qt.AlignRight | Qt.AlignBottom, bold=True, elide=True)
        elif tr.external:  # where its notes go, instead of the drum voice
            self.text(p, info, tr.port or "no MIDI out", TEXT if tr.port else WARN, 12,
                      Qt.AlignRight | Qt.AlignBottom, bold=True, elide=True)
        if not tr.audio:  # an input has no patterns
            nxt = e.next_pattern(tr)
            pat = f"P{tr.pattern + 1}" + (f"→{nxt + 1}" if nxt is not None and nxt != tr.pattern else "")
            self.text(p, info, pat, "#facc15" if nxt not in (None, tr.pattern) else DIM, 12,
                      Qt.AlignLeft | Qt.AlignBottom)
        self.region(head.intersected(clip), lambda pos, tr=tr: ScrollOrTap(
            pos, lambda: self.scroll, lambda v: setattr(self, "scroll", v), lambda: self.select(tr)))
        mute = QRectF(head.right() - 50 * s, head.top() + 6 * s, 44 * s, head.height() - 12 * s)
        self.button(p, mute, "M", lambda tr=tr: self.set_mute(tr), on=tr.mute, color=WARN)
        self.hits[-1] = (mute.intersected(clip), self.hits[-1][1])

    @staticmethod
    def source_name(tr):
        """'FL' from an input port like '...source:capture_FL'."""
        return tr.source.split(":")[-1].replace("capture_", "").replace("playback_", "") if tr.source else ""

    def draw_audio_row(self, p, lane, tr):
        """An audio track's row: the input it takes, panned into where its chain goes."""
        s, color = self.s, self.color(tr)
        to = "MASTER" if not tr.bus else f"BUS {tr.bus}"
        self.box(p, lane, "#11141b", "#2a3040", 1.5, radius=6)
        name = self.source_name(tr) or "no input — tap the track"
        self.text(p, lane.adjusted(12 * s, 0, -110 * s, 0), f"IN {name}", TEXT if tr.source else WARN, 14,
                  Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
        self.text(p, lane.adjusted(12 * s, 0, -12 * s, 0), f"{tr.pan.upper()}   →  {to}", color, 13,
                  Qt.AlignRight | Qt.AlignVCenter, bold=True)
        self.on_press(lane, lambda tr=tr: self.select(tr))

    def draw_lane(self, p, lane, tr):
        s, color, pat = self.s, self.color(tr), tr.current
        head = self.engine.playhead.get(tr)
        for k, c in enumerate(self.cells(lane)):
            i = self.page * 16 + k
            if i >= pat.length:
                self.box(p, c, "#10131a", None, radius=5)
                continue
            if tr.drum:
                st = pat.lanes[tr.voice][i]
            else:  # the column: loudest note decides the shade
                notes = sorted(pat.notes_at(i), reverse=True)
                st = max((n[1] for n in notes), key=lambda x: x.vel, default=DEFAULT_STEP)
            self.box(p, c, self.step_fill(color, st, i == head, k), "#ffffff" if i == head else None, 2.5, radius=5)
            if tr.drum:  # mini matrix of the other voices, kick at the top
                vh = (c.height() - 8 * s) / len(DRUM_KIT)
                for v, voice in pat.lanes.items():
                    if v != tr.voice and voice[i].on:
                        self.box(p, QRectF(c.left() + 5 * s, c.top() + 4 * s + v * vh, c.width() - 10 * s,
                                           max(1.5, vh - 1.5 * s)), BG if st.on else DRUM_COLORS[v], None, radius=1)
            elif notes and c.width() > 34 * s:  # top note, "+n" for the rest of a chord
                more = f"+{len(notes) - 1}" if len(notes) > 1 else ""
                self.text(p, c, note_name(notes[0][0]) + more, BG, 11, bold=True)

    # --- edit view: selected track --------------------------------------
    def draw_edit(self, p, r):
        s, tr = self.s, self.sel
        pat, color = tr.current, self.color(tr)
        g = 8 * s
        ctrl = QRectF(r.left() + g, r.top() + g, r.width() - 2 * g, 58 * s)
        params = () if tr.audio else ("step", "vel") if tr.drum else ("vel", "gate")
        octaves = [] if tr.drum or tr.audio else [68 * s, 68 * s]
        shifts = [] if tr.drum or tr.audio else [64 * s, 64 * s]  # transpose; drums and inputs have no pitch
        cols = split(ctrl, ([46 * s] if not tr.audio else []) + [100 * s, 80 * s, 80 * s, 72 * s, 0]
                     + [84 * s] * len(params) + octaves + shifts + [0, 100 * s], 6 * s)
        if not tr.audio:
            icon, *cols = cols
            self.draw_input(p, icon, tr)
        name, ch, length, mute, _, *param_r, _, delete = cols
        if octaves:
            *param_r, down, up, shift_dn, shift_up = param_r
            self.button(p, down, "OCT ▼", lambda: self.scroll_roll(tr, -12))
            self.button(p, up, "OCT ▲", lambda: self.scroll_roll(tr, 12))
            self.button(p, shift_dn, "▼", lambda: self.transpose(tr, -1), sub="trans")
            self.button(p, shift_up, "▲", lambda: self.transpose(tr, 1), sub="trans")

        self.box(p, name, color, None)
        self.text(p, name.adjusted(0, 0, 0, -name.height() * 0.3),
                  f"T{tr.number}" if tr.audio else f"T{tr.number}  P{tr.pattern + 1}", BG, 18, bold=True)
        self.text(p, name.adjusted(0, name.height() * 0.45, 0, 0), tr.kind.upper(), BG, 11)
        if tr.audio:
            self.box(p, ch, PANEL)
            self.text(p, ch, "MONO", DIM, 13, bold=True)
        else:
            self.value_box(p, ch, "CHANNEL", str(tr.channel), lambda: tr.channel,
                           lambda v: setattr(tr, "channel", int(v)), 1, 16, 14)

        def cycle_length():
            pat.length = 16 if pat.length >= 64 else (pat.length // 16 + 1) * 16

        if tr.audio:  # an input has no steps to sequence
            self.button(p, length, "AUDIO IN", lambda: self.choose_source(tr), sub="pick the input")
        else:
            self.value_box(p, length, "LENGTH", str(pat.length), lambda: pat.length,
                           lambda v: setattr(pat, "length", int(v)), 1, MAX_STEPS, 8, on_tap=cycle_length)
        self.button(p, mute, "MUTE", lambda: setattr(tr, "mute", not tr.mute), on=tr.mute, color=WARN)
        attr = "drum_param" if tr.drum else "param"
        for key, rect in zip(params, param_r):
            self.button(p, rect, key.upper(), lambda key=key: setattr(self, attr, key),
                        on=getattr(self, attr) == key, color=color)

        can_delete = len(self.project.tracks) > 1
        self.button(p, delete, "DELETE", lambda: None, enabled=can_delete, color=WARN, sub="hold")
        held = next((x for x in self.gestures.values() if isinstance(x, Hold)), None)
        if held:
            fill = QRectF(delete.left(), delete.top(), delete.width() * held.progress(), delete.height())
            p.save()
            p.setClipRect(fill)
            self.box(p, delete, QColor(255, 93, 93, 140), None)
            p.restore()
        if can_delete:
            self.region(delete, lambda pos: Hold(self.delete_track))

        area = QRectF(r.left() + g, ctrl.bottom() + 10 * s, r.width() - 2 * g, r.bottom() - ctrl.bottom() - 18 * s)
        if tr.audio:
            self.draw_audio_edit(p, area, tr, color)
        else:
            (self.draw_drum_matrix if tr.drum else self.draw_roll)(p, area, tr, color)

    def draw_input(self, p, r, tr):
        """The track's MIDI input as a DIN socket: lit green with its channel once a keyboard is chosen,
        amber while that device is away, dim when the track only plays its patterns."""
        s = self.s
        on, ready = bool(tr.in_port), self.input_ok is None or self.input_ok(tr)
        color = "#4ade80" if on and ready else ("#ff8c42" if on else DIM)
        self.box(p, r, "#11141b", color, 1.6, radius=8)
        glyph(p, QRectF(r.left(), r.top() + 1 * s, r.width(), r.height() - 20 * s), "din", color, width=0.095)
        label = ("ALL" if not tr.in_channel else f"CH{tr.in_channel}") if on else "IN"
        self.text(p, QRectF(r.left(), r.bottom() - 18 * s, r.width(), 15 * s), label, color, 9)
        self.on_press(r, lambda: self.choose_input(tr))

    def draw_audio_edit(self, p, area, tr, color):
        """An audio track's edit view: which input, how it is panned, and where it goes. Its effects live in
        the HOST-4 chain like any other track's."""
        s = self.s
        row = QRectF(area.left(), area.top(), area.width(), 70 * s)
        source, pan, to = split(row, [0, 330 * s, 200 * s], 10 * s)
        self.button(p, source, self.source_name(tr) or "NO INPUT", lambda: self.choose_source(tr),
                    sub="tap to pick the input", enabled=True)
        self.text(p, pan.adjusted(0, 0, 0, pan.height() * 0.35), "PAN", DIM, 12, Qt.AlignHCenter | Qt.AlignTop)
        for n, (name, rect) in enumerate(zip(("left", "center", "right"), split(pan.adjusted(0, 18 * s, 0, 0),
                                                                              [0, 0, 0], 6 * s))):
            self.button(p, rect, name[0].upper(), lambda name=name: self.set_pan(tr, name), on=tr.pan == name,
                        color=color)
        buses = self.project.buses
        label = "MASTER" if not tr.bus else f"BUS {tr.bus}"
        self.button(p, to, f"→ {label}", lambda: self.cycle_destination(tr), enabled=bool(buses),
                    sub="tap: next" if buses else "no bus yet")
        hint = QRectF(area.left(), row.bottom() + 12 * s, area.width(), 40 * s)
        self.text(p, hint, "Effects for this input are its chain in HOST-4; the level is the bus'/master's.",
                  DIM, 14, Qt.AlignLeft | Qt.AlignVCenter, elide=True)
        self.on_press(QRectF(area.left(), area.top(), area.width(), area.height()),
                      lambda: self.select(tr))

    def roll_base(self, tr):
        """Lowest visible note of the track's piano roll; starts at the C below its lowest note."""
        if tr not in self.roll_bases:
            notes = [n for n, lane in tr.current.lanes.items() if any(st.on for st in lane)]
            self.roll_bases[tr] = min(notes) // 12 * 12 if notes else 48
        return self.roll_bases[tr]

    def scroll_roll(self, tr, delta):
        self.roll_bases[tr] = max(0, min(128 - self.roll_rows, round(self.roll_base(tr) + delta)))

    @staticmethod
    def room(lane, i):
        """How long a note starting at step i may be: up to the next note of the same pitch."""
        return next((j - i for j in range(i + 1, MAX_STEPS) if lane[j].on), MAX_GATE)

    def fit(self, lane, i):
        """Notes of one pitch don't overlap: the note at step i ends where the next one starts, and an
        earlier note running into it ends at step i."""
        lane[i].gate = min(lane[i].gate, self.room(lane, i))
        prev = next((j for j in range(i - 1, -1, -1) if lane[j].on), None)
        if prev is not None and prev + lane[prev].gate > i:
            lane[prev].gate = i - prev

    def place(self, tr, pat, i, n):
        """Note n on step i (new notes get the track's latest velocity/gate); plays it when added."""
        lane = pat.lane(n)
        if not lane[i].on:
            self.set_step(tr, lane[i], True)
            self.fit(lane, i)
            self.engine.preview(tr, n)
        tr.last_note = n

    def move_note(self, tr, pat, i, n, to):
        src, lane = pat.lane(n)[i], pat.lane(to)
        lane[i].on, lane[i].vel, lane[i].gate = True, src.vel, src.gate
        src.on = False
        self.fit(lane, i)
        tr.last_note = to
        self.engine.preview(tr, to)

    def draw_roll(self, p, area, tr, color):
        s, pat = self.s, tr.current
        head = self.engine.playhead.get(tr)
        roll = QRectF(area.left(), area.top(), area.width(), area.height() - 96 * s)
        keys = QRectF(roll.left(), roll.top(), 64 * s, roll.height())
        grid = QRectF(keys.right() + 8 * s, roll.top(), roll.width() - keys.width() - 8 * s, roll.height())
        rows = self.roll_rows = max(6, int(roll.height() // (26 * s)))
        rh = roll.height() / rows
        self.scroll_roll(tr, 0)  # clamp to the current number of rows
        base = self.roll_base(tr)
        cells = self.cells(grid)

        def row_top(n):
            return roll.top() + (base + rows - 1 - n) * rh

        def pitch_at(pos):
            return base + rows - 1 - max(0, min(rows - 1, int((pos.y() - roll.top()) / rh)))

        pr = self.project
        scaled = pr.scale != "chromatic"  # rows outside the project's scale are dimmed, its key is tinted
        for n in range(base, base + rows):
            y, black = row_top(n), n % 12 in (1, 3, 6, 8, 10)
            outside, root = scaled and not pr.in_scale(n), scaled and (n - pr.key) % 12 == 0
            key = QRectF(keys.left(), y + 1, keys.width(), rh - 2)
            self.box(p, key, "#1a1f2a" if black else ("#7d8594" if outside else "#c9d1de"), None, radius=3)
            if root:
                self.box(p, QRectF(key.left(), key.top(), 5 * s, key.height()), "#38d6ff", None, radius=2)
            self.text(p, key.adjusted(0, 0, -6 * s, 0), note_name(n), DIM if black else BG, 10,
                      Qt.AlignRight | Qt.AlignVCenter, bold=n % 12 == 0)
            band = QColor("#0f1117" if outside else "#141821" if black else "#1a1f29")
            if root:
                band = QColor("#1b2733")
            for k, c in enumerate(cells):
                i = self.page * 16 + k
                p.fillRect(QRectF(c.left(), y + 1, c.width(), rh - 2), QColor("#10131a") if i >= pat.length else band)
            if n % 12 == 0:  # octave boundary
                p.fillRect(QRectF(grid.left(), y + rh - 1, grid.width(), 1.5 * s), QColor("#3a4252"))

        for k, c in enumerate(cells):
            i = self.page * 16 + k
            if i == head:
                p.fillRect(QRectF(c.left(), roll.top(), c.width(), roll.height()), QColor(255, 255, 255, 28))
            above = below = False
            for n, st in pat.notes_at(i):
                if n < base or n >= base + rows:  # off screen: arrow at the edge it lies beyond
                    above, below = above or n >= base + rows, below or n < base
                    continue
                fill = QColor(color)
                fill.setAlpha(round(110 + 145 * st.vel / 127))
                end = max(c.left() + c.width() * 0.3, self.step_x(cells, k + st.gate, grid.right()))
                note = QRectF(c.left(), row_top(n) + 1, end - c.left(), rh - 2)
                self.box(p, note, fill, "#ffffff" if i == head else None, 2, radius=4)
                if note.width() > 30 * s:
                    self.text(p, note, note_name(n), BG, 10, bold=True)
            column = QRectF(c.left(), roll.top(), c.width(), roll.height())
            for flag, arrow, align in ((above, "▲", Qt.AlignTop), (below, "▼", Qt.AlignBottom)):
                if flag:
                    self.text(p, column, arrow, color.name(), 12, Qt.AlignHCenter | align)

        self.region(keys, lambda pos: ScrollOrTap(pos, lambda: self.roll_base(tr),
                                                   lambda v: self.scroll_roll(tr, v - self.roll_base(tr)),
                                                   lambda: self.engine.preview(tr, pitch_at(pos)), -rh))
        page0 = self.page * 16

        def note_at(i, n):
            """Step on this page where the note of pitch n sounding at step i starts (its head or tail)."""
            lane = pat.lanes.get(n)
            for j in range(i, page0 - 1, -1) if lane else ():
                if lane[j].on and j + lane[j].gate > i:
                    return j
            return None

        self.region(grid, lambda pos: Roll(self, tr, lambda q: self.cell_at(grid, q),
                                           lambda q: self.step_pos(grid, q), pitch_at, note_at, pos))

        # velocity / gate lane
        lane = QRectF(grid.left(), roll.bottom() + 12 * s, grid.width(), area.bottom() - roll.bottom() - 12 * s)
        label = QRectF(keys.left(), lane.top(), keys.width(), lane.height())
        self.box(p, label, PANEL, None, radius=5)
        self.text(p, label, self.param.upper(), DIM, 12, bold=True)
        for k, c in enumerate(self.cells(lane)):
            i = self.page * 16 + k
            self.box(p, c, "#10131a" if i >= pat.length else "#181c26", "#ffffff" if i == head else None, 1.5,
                     radius=4)
            notes = [st for _, st in pat.notes_at(i)]
            if notes:  # a chord shows its highest value
                v = max(st.vel / 127 if self.param == "vel" else st.gate for st in notes)
                f = min(1.0, v)
                self.box(p, QRectF(c.left(), c.bottom() - c.height() * f, c.width(), c.height() * f), color, None,
                         radius=4)
                if self.param == "gate" and v > 1:  # the lane draws 0-100%; longer notes say how long
                    self.text(p, c, f"×{v:g}", BG, 12, bold=True)
        self.region(lane, lambda pos: LaneDraw(self, tr, lane, self.param, pos))

    def draw_drum_matrix(self, p, area, tr, _):
        """One row per kit voice, each in its own fixed color; the name is a pad that selects the voice
        and plays it."""
        s, pat = self.s, tr.current
        head = self.engine.playhead.get(tr)
        rh = area.height() / len(DRUM_KIT)
        for v, (name, note) in enumerate(DRUM_KIT):
            row = QRectF(area.left(), area.top() + v * rh, area.width(), rh - 3 * s)
            label = QRectF(row.left(), row.top(), 120 * s, row.height())
            selected, color = v == tr.voice, QColor(DRUM_COLORS[v])
            self.box(p, label, "#1b2130" if selected else PANEL, color if selected else BORDER, 2 if selected else 1,
                     radius=5)
            self.box(p, QRectF(label.left() + 4 * s, label.top() + 5 * s, 4 * s, label.height() - 10 * s), color, None,
                     radius=2)
            inner = label.adjusted(14 * s, 0, -8 * s, 0)
            self.text(p, inner, name, TEXT if selected else DIM, 14, Qt.AlignLeft | Qt.AlignVCenter, bold=selected)
            self.text(p, inner, str(note), "#5b6577", 11, Qt.AlignRight | Qt.AlignVCenter)
            self.on_press(label, lambda v=v: self.pick_voice(tr, v))

            lane = QRectF(label.right() + 10 * s, row.top(), row.right() - label.right() - 10 * s, row.height())
            steps = pat.lanes[v]
            for k, c in enumerate(self.cells(lane)):
                i = self.page * 16 + k
                st = steps[i]
                if i >= pat.length:
                    self.box(p, c, "#10131a", None, radius=4)
                    continue
                self.box(p, c, self.step_fill(color, Step(), i == head, k), None, radius=4)
                if st.on:  # dim full cell + bright part as tall as the velocity
                    dim = QColor(color)
                    dim.setAlpha(80)
                    self.box(p, c, dim, None, radius=4)
                    p.save()
                    p.setClipRect(QRectF(c.left(), c.bottom() - c.height() * st.vel / 127, c.width(), c.height()))
                    self.box(p, c, color, None, radius=4)
                    p.restore()
                if i == head:
                    self.box(p, c, QColor(0, 0, 0, 0), "#ffffff", 2, radius=4)
                if self.drum_param == "vel":
                    self.region(c, lambda pos, st=st: Drag(
                        pos, lambda: st.vel, lambda val: self.edit_step(tr, st, "vel", int(val)), 1, 127, 3 * s,
                        on_tap=lambda: self.set_step(tr, st, not st.on)))
            if self.drum_param == "step":
                self.region(lane, lambda pos, v=v, lane=lane: Paint(self, lane, *self.painter(tr, v), pos))
