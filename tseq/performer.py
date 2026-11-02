"""The live-play module: chord pads, the keyboard and the XY pad, with the chord/XY gestures that belong to
them."""
import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPen

from .constants import (BG, BORDER, BPM_MAX, BPM_MIN, DIM, DRUM_COLORS, DRUM_KIT, GATE_RES, MAX_GATE, PANEL,
                        TEXT, WARN)
from .gestures import Gesture
from .midiin import Inputs
from .music import (BORROWED, CHORD_SCALE, SCALES, XYFX_MODES, chord_name, note_name, roman, stack, xyfx_filter,
                    xyfx_repeat)
from .ui import Panel, split


class Pad(Gesture):
    """A held live pad: notes sound while the finger is down; when the track records, they are written
    to it on press and get their length on release."""

    def __init__(self, perf, key, tr, notes, vel, strum=0.0, rebuild=None):
        self.perf, self.key, self.tr, self.notes, self.vel = perf, key, tr, notes, vel  # notes: [(lane, midi note)]
        self.rebuild = rebuild  # chord pads: () -> the pad's notes under the current modifiers
        self.down = True
        perf.held[key] = perf.held.get(key, 0) + 1  # lights the pad
        self.sounding = []  # notes this pad has turned on (a strum may be cut short by the release)
        for n, (_, note) in enumerate(notes):
            if strum and n:
                QTimer.singleShot(round(n * strum * 1000), lambda note=note: self.down and self.on(note))
            else:
                self.on(note)
        self.take = perf.record_start(tr, [lane for lane, _ in notes], vel)

    def on(self, note):
        self.perf.on(self.tr, note, self.vel)
        self.sounding.append(note)

    def change(self, notes):
        """Become another chord while held (a modifier changed): only the notes that differ stop/start,
        and a recording takes the new chord at its step."""
        gone = [n for n in self.sounding if n not in [m for _, m in notes]]
        for note in gone:
            self.perf.off(self.tr, note)
            self.sounding.remove(note)
        for _, note in notes:
            if note not in self.sounding:
                self.on(note)
        if self.take:
            pat, i, start, lanes = self.take
            for k in lanes:
                pat.lane(k)[i].on = False
            self.take = (pat, i, start, [k for k, _ in notes])
            for k in self.take[3]:
                st = pat.lane(k)[i]
                st.on, st.vel, st.gate = True, self.vel, GATE_RES
                self.perf.engine.rec_taken.add((k, i))
        self.notes = notes

    def release(self, pos):
        self.down = False
        self.perf.held[self.key] -= 1
        for note in self.sounding:
            self.perf.off(self.tr, note)
        if self.take:
            self.perf.record_end(self.tr, self.take)

    def cancel(self):
        self.release(None)


class InNote:
    """A note from a MIDI keyboard or controller: it plays on the track it is routed to, lights that pad or
    key, and is recorded there like a held pad while the track is armed."""

    def __init__(self, perf, tr, note, vel):
        self.perf, self.tr, self.note = perf, tr, note
        perf.on(tr, note, vel)
        notes = perf.midi_in.setdefault(tr, {})
        notes[note] = notes.get(note, 0) + 1
        if perf.mode:  # this track records what it hears, whatever is selected
            perf.engine.rec[tr] = perf.mode == "rec"
        lane = note  # a melody's lane is its pitch; a drum's is the kit voice that note plays
        if tr.drum:
            lane = next((v for v, (_, n) in enumerate(DRUM_KIT) if n == note), None)
        self.take = perf.record_start(tr, [] if lane is None else [lane], vel)

    def release(self):
        perf = self.perf
        notes = perf.midi_in.get(self.tr, {})
        notes[self.note] = notes.get(self.note, 0) - 1
        if notes[self.note] <= 0:  # the same note on two devices: the last one off ends it
            notes.pop(self.note, None)
            if not notes:
                perf.midi_in.pop(self.tr, None)
        perf.off(self.tr, self.note)
        if self.take:
            perf.record_end(self.tr, self.take)


class KeySlide(Gesture):
    """Keyboard: a key plays while held; sliding onto another key moves the note there (glissando)."""

    def __init__(self, perf, tr, key_at, velocity, pos):
        self.perf, self.tr, self.key_at, self.velocity = perf, tr, key_at, velocity
        self.note, self.pad = None, None
        self.move(pos)

    def move(self, pos):
        note = self.key_at(pos)
        if note is not None and note != self.note:
            if self.pad:
                self.pad.release(pos)
            self.note = note
            self.pad = Pad(self.perf, ("key", note), self.tr, [(note, note)], self.velocity(pos))

    def release(self, pos):
        if self.pad:
            self.pad.release(pos)
            self.pad = None

    def cancel(self):
        self.release(None)


class XYTouch(Gesture):
    """A finger on the XY pad: sets X/Y and engages; lifting the last finger disengages (unless LATCH)."""

    def __init__(self, perf, pad, pos):
        self.perf, self.pad = perf, pad
        perf.xy_fingers += 1
        self.move(pos)

    def move(self, pos):
        x = max(0.0, min(1.0, (pos.x() - self.pad.left()) / self.pad.width()))
        y = max(0.0, min(1.0, (self.pad.bottom() - pos.y()) / self.pad.height()))
        self.perf.xy_at = (x, y)
        self.perf.host.xy(x=x, y=y, engage=1.0)

    def release(self, pos):
        self.perf.xy_fingers -= 1
        if not self.perf.xy_fingers and not self.perf.latch:
            self.perf.host.xy(engage=0.0)

    def cancel(self):
        self.release(None)


class Performer(Panel):
    """Live playing on the sequencer's selected track: chord pads in the project's key for a melody
    track, finger-drum pads for a drum track. REC (replace, like tape) or OVR (overdub) writes what is
    played into the playing pattern, quantized."""
    QUANTS = (("1/16", 1), ("1/8", 2), ("1/4", 4))
    KEY_WIDTH = 48  # smallest white key, in pixels (~12 mm on a 20" full-HD screen)
    MODS = ("7", "9", "sus2", "sus4")
    FUNCTION = ["#38d6ff", "#4ade80", "#38d6ff", "#4ade80", "#ff8c42", "#38d6ff", "#ff8c42"]  # tonic/sub/dominant

    def __init__(self, project, engine, seq, host=None):
        super().__init__()
        self.project, self.engine, self.seq, self.host = project, engine, seq, host
        self.latch = False  # XY pad: keep the effect on after the finger lifts
        self.xy_fingers = 0
        self.xy_at = None  # (x, y) last touched
        self.mode = None  # None, "rec" or "ovr"
        self.quant = 1  # steps
        self.ext = set()  # chord extensions / suspensions
        self.strum = False
        self.lead = True  # voice leading: next chord takes the inversion nearest the previous one
        self.octave = 0
        self.last = None  # mean pitch of the last chord, for voice leading
        self.held = {}  # pad key -> count of fingers on it (for lighting)
        self.layout = "chords"  # melody tracks: "chords" (pads) or "keys" (keyboard)
        self.voices = {}  # (channel, note) -> number of held pads sounding it
        self.inputs = Inputs()  # the tracks' MIDI keyboards
        self.midi_in = {}  # Track -> {note: count} the MIDI keyboards are holding (for lighting)
        self.midi_keys = {}  # (port, channel, note) -> [InNote]: what is sounding, to release on note-off
        self.arm_key = None  # (selected track, mode) the current recording was armed with
        self.pulse_at = None  # when the last external clock pulse came in
        self.pulse_len = None  # its smoothed length (s)
        self.pulses = 0  # pulses since the last tempo decision

    # --- sound + recording -----------------------------------------------------
    def on(self, tr, note, vel):
        """Chords share notes (e.g. iii and I: E, G); a synth only gets the first note-on and the last
        note-off of a shared note, as if one key stayed down while the fingers change chords."""
        key = (tr.channel, note)
        self.voices[key] = self.voices.get(key, 0) + 1
        if self.voices[key] == 1:
            self.engine.live(tr, note, vel)

    def off(self, tr, note):
        key = (tr.channel, note)
        self.voices[key] -= 1
        if not self.voices[key]:
            del self.voices[key]
            self.engine.live(tr, note, 0)

    def tick(self):
        super().tick()
        self.inputs.use(tr.in_port for tr in self.project.tracks if not tr.audio)
        for port, msg, when in self.inputs.poll():
            self.external(port, msg, when)
        self.arm()

    def arm(self):
        """Which tracks record what is played: the selected one (the pads play it) and every track hearing a
        MIDI keyboard note. Arming again (the mode or the track) starts a new take, so earlier ones may be
        replaced again."""
        wanted = {}
        if self.mode and self.seq.sel is not None:
            wanted[self.seq.sel] = self.mode == "rec"
            for tr in self.midi_in:
                wanted.setdefault(tr, self.mode == "rec")
        if (self.seq.sel, self.mode) != self.arm_key:
            self.arm_key = (self.seq.sel, self.mode)
            self.engine.rec_taken.clear()
        self.engine.rec = wanted

    def close(self):
        self.inputs.close()

    # --- MIDI input ------------------------------------------------------------
    def input_ok(self, tr):
        """Whether the track's input is actually open (its icon says so when the device is away)."""
        return any(tr.in_port.lower() in name.lower() for name in self.inputs.open_names())

    def lit(self, tr, note):
        """A note the track's MIDI keyboard is holding: its pads and keys light up like a finger would."""
        return self.midi_in.get(tr, {}).get(note, 0) > 0

    def listening(self, port, channel):
        """The tracks that take this input and channel: any MIDI track whose chosen device name is part of
        this one, on its own channel or on all of them."""
        return [tr for tr in self.project.tracks
                if not tr.audio and tr.in_port and tr.in_port.lower() in port.lower()
                and (not tr.in_channel or tr.in_channel == channel)]

    def transport(self, msg):
        """A keyboard's transport buttons: MIDI Start/Continue/Stop, or the CCs controllers often send instead
        (115 play, 116 stop). A start counts in when the project has one."""
        status = msg[0]
        if status in (0xFA, 0xFB):
            play = True
        elif status == 0xFC:
            play = False
        elif msg[2] <= 0:  # a button going up: not a command
            return
        else:
            play = msg[1] == 115
        if play:
            if not self.engine.playing and not self.engine.counting:
                self.engine.start()
        elif self.engine.playing or self.engine.counting:
            self.engine.stop()

    CLOCK = 24  # MIDI clock pulses per beat

    def clock(self, when):
        """Follow an external clock, so a keyboard's own tempo plays the sequencer. The pulse length is
        smoothed and the tempo only moves once a beat, so a few late pulses don't make it wobble."""
        last, self.pulse_at = self.pulse_at, when
        if last is None:
            return
        gap = when - last
        if not 0.002 < gap < 0.25:  # the clock stopped (or was replugged): start measuring again
            self.pulse_len = None
            return
        self.pulse_len = gap if self.pulse_len is None else self.pulse_len * 0.85 + gap * 0.15
        self.pulses += 1
        if self.pulses % self.CLOCK:  # decide once a beat, not on every pulse
            return
        bpm = round(60.0 / (self.pulse_len * self.CLOCK))
        if bpm != self.project.bpm and BPM_MIN <= bpm <= BPM_MAX:
            self.project.bpm = bpm

    def external(self, port, msg, when=None):
        """A message from a MIDI input: every track listening to it plays the note (a keyboard's chords can
        go to more than one track) and records it when armed. Bend, mod wheel and the rest go to the
        instrument, so a keyboard's controller works the track like its own UI would."""
        status = msg[0]
        if status == 0xF8:  # the clock: its tempo is the project's
            self.clock(when if when is not None else time.perf_counter())
            return
        if status in (0xFA, 0xFB, 0xFC) or (status == 0xB0 and len(msg) > 2 and msg[1] in (115, 116)):
            self.transport(msg)  # the keyboard's own play/stop
            return
        kind, channel = status & 0xF0, (status & 0x0F) + 1
        tracks = self.listening(port, channel)
        if not tracks:
            return
        if kind in (0x80, 0x90) and len(msg) >= 3:
            note, vel = msg[1], msg[2]
            key = (port, channel, note)
            if kind == 0x90 and vel:
                if key not in self.midi_keys:  # the same note on again without a note-off: no double
                    self.midi_keys[key] = [InNote(self, tr, note, vel) for tr in tracks]
            elif key in self.midi_keys:
                for held in self.midi_keys.pop(key):
                    held.release()
            return
        if status < 0xF0:  # pitch bend, mod wheel, aftertouch: the instrument
            for tr in tracks:
                self.engine.send(msg, tr)

    def record_start(self, tr, lanes, vel):
        """Write notes at the quantized position; returns what record_end needs, or None (not recording)."""
        e = self.engine
        pos = e.position(tr) if self.mode and tr in e.rec else None
        if pos is None or not lanes:  # not armed, not playing, or nothing to write (a note the drum kit has not)
            return None
        pat = tr.current
        at = round(pos / self.quant) * self.quant
        i = int(at) % pat.length
        for k in lanes:
            st = pat.lane(k)[i]
            st.on, st.vel, st.gate = True, vel, GATE_RES
            e.rec_taken.add((k, i))
            e.rec_fresh.add((k, i))  # sounding live while held: the pattern must not play it too
        return pat, i, e.beats(), lanes

    def record_end(self, tr, take):
        pat, i, start, lanes = take
        pos = self.engine.beats()  # not the track's own position: that wraps with the pattern
        held = pos - start if pos is not None and start is not None else 1.0
        gate = max(GATE_RES, min(MAX_GATE, pat.length, round(held / GATE_RES) * GATE_RES))  # never past the end
        for k in lanes:
            lane = pat.lane(k)
            lane[i].gate = gate
            if not tr.drum:
                self.seq.fit(pat, lane, i)
            self.engine.rec_fresh.discard((k, i))  # the key is up: the pattern plays it from now on
        if self.engine.rec.get(tr) and not tr.drum:  # REC replaces the step played - only that step
            for k, lane in list(pat.lanes.items()):
                if k not in lanes and (k, i) not in self.engine.rec_taken:
                    lane[i].on = False
        if pos is not None and start is not None:
            at = round(start / self.quant) * self.quant  # where the pattern puts the note
            left = gate - (pos - at)  # what is left of the note's own length: no more, or it hangs over
            if left > 0.02:
                self.engine.take_now(tr, pat, i, lanes, left * self.engine.step_len)

    # --- chords --------------------------------------------------------------
    def scale(self):
        return SCALES[CHORD_SCALE.get(self.project.scale, self.project.scale)]

    def chords(self):
        """[(label, name, tones above C in root position, color)]: the 7 of the key, then borrowed ones."""
        key, sc, out = self.project.key, self.scale(), []
        for d in range(7):
            tones = [key + t for t in stack(sc, d, self.ext)]
            out.append((roman(d, tones), chord_name(tones), tones, self.FUNCTION[d]))
        for label, offset, mode in BORROWED:
            tones = [key + offset + t for t in stack(SCALES[mode], 0, self.ext)]
            out.append((label, chord_name(tones, label.startswith("b")), tones, "#a78bfa"))
        return out

    def voice(self, tones):
        """MIDI notes for a chord: around C3-C4, in the inversion nearest the previous chord."""
        base = [48 + tones[0] % 12 + (t - tones[0]) for t in tones]
        if not self.lead or self.last is None:
            notes = base
        else:
            options = []
            for r in range(len(base)):
                inv = base[r:] + [n + 12 for n in base[:r]]
                options += [[n + shift for n in inv] for shift in (-12, 0)]
            notes = min(options, key=lambda ns: abs(sum(ns) / len(ns) - self.last))
        notes = [n + 12 * self.octave for n in notes]
        self.last = sum(notes) / len(notes) - 12 * self.octave
        return notes

    def velocity(self, r, pos, toward_bottom=False):
        """Higher on the pad = louder; toward_bottom: lower = louder (keys, like the front of a piano key)."""
        f = (r.bottom() - pos.y()) / r.height()
        return max(1, min(127, round(40 + 87 * (1 - f if toward_bottom else f))))

    # --- drawing -------------------------------------------------------------
    def draw(self, p):
        s, g = self.s, 8 * self.s
        p.fillRect(self.rect(), QColor(BG))
        if self.seq.master_focus and self.host and self.host.xyfx():
            self.draw_xy(p)
            return
        tr = self.seq.sel
        header = QRectF(g, g, self.w - 2 * g, 52 * s)
        layouts = [] if tr.drum else [84 * s, 84 * s]
        target, *rest = split(header, [0] + layouts + [80 * s, 80 * s] + [62 * s] * len(self.QUANTS), 6 * s)
        if layouts:
            for rect, name in zip(rest[:2], ("chords", "keys")):
                self.button(p, rect, name.upper(), lambda name=name: setattr(self, "layout", name),
                            on=self.layout == name, color=self.seq.color(tr))
            rest = rest[2:]
        rec, ovr, *quants = rest
        color = self.seq.color(tr)
        self.text(p, target.adjusted(6 * s, 0, 0, 0), f"→ T{tr.number}  {tr.kind}  CH{tr.channel}", color.name(), 17,
                  Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
        live = self.engine.playing
        for rect, mode, label in ((rec, "rec", "REC"), (ovr, "ovr", "OVR")):
            self.button(p, rect, label, lambda mode=mode: setattr(self, "mode", None if self.mode == mode else mode),
                        on=self.mode == mode, color=WARN, pending=self.mode == mode and live,
                        sub="replace" if mode == "rec" else "overdub")
        for rect, (label, q) in zip(quants, self.QUANTS):
            self.button(p, rect, label, lambda q=q: setattr(self, "quant", q), on=self.quant == q)
        body = QRectF(g, header.bottom() + 10 * s, self.w - 2 * g, self.h - header.bottom() - 10 * s - g)
        if tr.audio:  # an input has nothing to play here
            self.text(p, body, "an audio track: its input is patched in the sequencer (EDIT)", DIM, 16)
            return
        draw = self.draw_drum_pads if tr.drum else self.draw_keys if self.layout == "keys" else self.draw_chord_pads
        draw(p, body, tr)

    def pad(self, p, r, key, color, title, sub, lit=False):
        lit = lit or self.held.get(key, 0) > 0  # a finger on it, or the keyboard playing it
        fill = QColor(color)
        fill.setAlpha(230 if lit else 45)
        self.box(p, r, fill, color if not lit else "#ffffff", 2, radius=12)
        fg = BG if lit else TEXT
        self.text(p, r.adjusted(0, 0, 0, -r.height() * 0.3), title, fg, 26, bold=True, elide=True)
        self.text(p, r.adjusted(0, r.height() * 0.45, 0, 0), sub, fg if lit else DIM, 14, elide=True)

    def press_pad(self, key, tr, notes, r, pos, rebuild=None):
        return Pad(self, key, tr, notes, self.velocity(r, pos), 0.03 if self.strum and len(notes) > 1 else 0.0,
                   rebuild)

    def rebuild_held(self):
        """Modifiers apply to chords already held."""
        for g in self.gestures.values():
            if isinstance(g, Pad) and g.rebuild:
                g.change(g.rebuild())

    def draw_chord_pads(self, p, r, tr):
        s = self.s
        pads, mods = split(r, [0, 170 * s], 10 * s)
        chords = self.chords()
        top = QRectF(pads.left(), pads.top(), pads.width(), pads.height() * 0.58)
        bottom = QRectF(pads.left(), top.bottom() + 8 * s, pads.width(), pads.bottom() - top.bottom() - 8 * s)
        rects = split(top, [0] * 7, 7 * s) + split(bottom, [0] * len(BORROWED), 7 * s)
        for n, ((label, name, tones, color), rect) in enumerate(zip(chords, rects)):
            self.pad(p, rect, n, color, label, name,
                     lit=any(self.lit(tr, note) for note in self.voice(tones)))

            def notes_of(n=n):
                return [(m, m) for m in self.voice(self.chords()[n][2])]

            def press(pos, n=n, rect=rect, notes_of=notes_of):
                return self.press_pad(n, tr, notes_of(), rect, pos, notes_of)
            self.region(rect, press)
        cells = [QRectF(mods.left() + (j % 2) * (mods.width() / 2), mods.top() + (j // 2) * (mods.height() / 4),
                        mods.width() / 2, mods.height() / 4).adjusted(3 * s, 3 * s, -3 * s, -3 * s) for j in range(8)]
        for cell, ext in zip(cells, self.MODS):
            self.button(p, cell, ext.upper(), lambda ext=ext: self.toggle_ext(ext), on=ext in self.ext)
        self.button(p, cells[4], "STRUM", lambda: setattr(self, "strum", not self.strum), on=self.strum)
        self.button(p, cells[5], "LEAD", self.toggle_lead, on=self.lead, sub="voices")
        self.button(p, cells[6], "OCT −", lambda: self.shift_octave(-1))
        self.button(p, cells[7], "OCT +", lambda: self.shift_octave(1), sub=f"{self.octave:+d}" if self.octave else None)

    def toggle_ext(self, ext):
        self.ext ^= {ext}
        if ext in ("sus2", "sus4"):  # one suspension at a time
            self.ext -= {"sus2", "sus4"} - {ext}
        self.rebuild_held()

    def toggle_lead(self):
        """Switching voice leading either way starts it afresh: the next chord in root position."""
        self.lead, self.last = not self.lead, None

    def shift_octave(self, d):
        self.octave = max(-2, min(2, self.octave + d))
        self.rebuild_held()

    def draw_xy(self, p):
        """MPC-style XY pad for Touch-Seq XYFX on the master: 8x8 grid, the centre is the mildest spot.
        Columns: beat repeat, 1/2 next to the centre .. 1/16 at the edges, straight on the right, triplets or
        backwards on the left (MODE); rows: low-pass closing below the centre line, high-pass above it.
        On while touched (or LATCH)."""
        s, g = self.s, 8 * self.s
        values = self.host.xy_values()
        engaged = values.get("engage", 0) > 0.5
        header = QRectF(g, g, self.w - 2 * g, 52 * s)
        title, latch = split(header, [0, 120 * s], 6 * s)
        self.text(p, title.adjusted(6 * s, 0, 0, 0), "→ MASTER  XYFX  ·  beat repeat / filter", "#facc15", 17,
                  Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
        self.button(p, latch, "LATCH", self.toggle_latch, on=self.latch, color="#facc15", sub="hold effect")
        body = QRectF(g, header.bottom() + 10 * s, self.w - 2 * g, self.h - header.bottom() - 10 * s - g)
        pad, side = split(body, [0, 170 * s], 12 * s)
        pad = QRectF(pad.left(), pad.top(), pad.width(), pad.height() - 24 * s)  # room for the division labels

        top = QLinearGradient(pad.topLeft(), pad.topRight())
        for at, c in ((0, "#e0607a"), (0.45, "#b9a05a"), (0.75, "#7fbf5a"), (1, "#2aa198")):
            top.setColorAt(at, QColor(c))
        p.setPen(Qt.NoPen)
        p.setBrush(top)
        p.drawRoundedRect(pad, 8 * s, 8 * s)
        shade = QLinearGradient(pad.topLeft(), pad.bottomLeft())
        shade.setColorAt(0, QColor(70, 60, 200, 0))
        shade.setColorAt(1, QColor(70, 60, 200, 200))
        p.setBrush(shade)
        p.drawRoundedRect(pad, 8 * s, 8 * s)
        cw, ch = pad.width() / 8, pad.height() / 8
        x, y = (values.get("x", 0.5), values.get("y", 0.5)) if self.xy_at is None else self.xy_at
        if engaged:  # the touched cell, like the MPC
            cell = QRectF(pad.left() + min(7, int(x * 8)) * cw, pad.top() + min(7, int((1 - y) * 8)) * ch, cw, ch)
            p.fillRect(cell, QColor(14, 134, 179, 220))
        for n in range(1, 8):  # the centre lines (the mildest spot) stronger
            p.setPen(QPen(QColor(0, 0, 0, 140 if n == 4 else 70), 3 if n == 4 else 1.5))
            p.drawLine(QPointF(pad.left() + n * cw, pad.top()), QPointF(pad.left() + n * cw, pad.bottom()))
            p.drawLine(QPointF(pad.left(), pad.top() + n * ch), QPointF(pad.right(), pad.top() + n * ch))
        if engaged:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 120))
            p.drawEllipse(QPointF(pad.left() + x * pad.width(), pad.top() + (1 - y) * pad.height()), 26 * s, 26 * s)
        mode = round(values.get("mode", 0))
        for col in range(8):
            r = QRectF(pad.left() + col * cw, pad.bottom() + 2 * s, cw, 22 * s)
            lit = engaged and min(7, int(x * 8)) == col
            self.text(p, r, xyfx_repeat((col + 0.5) / 8, mode), TEXT if lit else DIM, 14, bold=True)
        inside = pad.adjusted(10 * s, 6 * s, -10 * s, -6 * s)
        self.text(p, inside, "HPF ↑", "#ffffff", 13, Qt.AlignLeft | Qt.AlignTop, bold=True)
        self.text(p, inside, "LPF ↓", "#ffffff", 13, Qt.AlignLeft | Qt.AlignBottom, bold=True)
        self.region(pad, lambda pos: XYTouch(self, pad, pos))

        kind, hz = xyfx_filter(y)
        filt = "open" if hz in (20, 20000) else f"{kind} {hz / 1000:.1f}k" if hz >= 1000 else f"{kind} {hz:.0f}"
        lines = (("STATE", "ON" if engaged else "off"), ("REPEAT", xyfx_repeat(x, mode)), ("FILTER", filt))
        for n, (label, value) in enumerate(lines):
            r = QRectF(side.left(), side.top() + n * 74 * s, side.width(), 66 * s)
            self.box(p, r, "#1f1d12" if label == "STATE" and engaged else PANEL,
                     "#facc15" if label == "STATE" and engaged else BORDER, radius=8)
            self.text(p, r.adjusted(0, 6 * s, 0, 0), label, DIM, 12, Qt.AlignHCenter | Qt.AlignTop)
            self.text(p, r.adjusted(0, 16 * s, 0, 0), value, TEXT, 20, bold=True)
        # the left half's mode (where the tempo was: XYFX follows the project's BPM by itself)
        modes = QRectF(side.left(), side.top() + 3 * 74 * s, side.width(), side.height() - 3 * 74 * s)
        self.text(p, QRectF(modes.left(), modes.top(), modes.width(), 20 * s), "LEFT HALF", DIM, 12)
        for n, name in enumerate(XYFX_MODES):
            r = QRectF(modes.left(), modes.top() + 24 * s + n * 56 * s, modes.width(), 50 * s)
            self.button(p, r, name.upper(), lambda n=n: self.host.xy(mode=float(n)), on=mode == n, color="#facc15")

    def toggle_latch(self):
        self.latch = not self.latch
        if not self.latch and not self.xy_fingers:
            self.host.xy(engage=0.0)

    def keyboard(self, kb):
        """White and black keys as [(note, rect)]: whole octaves from C (+ the top C), as many as fit
        with white keys at least KEY_WIDTH pixels wide."""
        octaves = max(1, min(4, int((kb.width() / self.KEY_WIDTH - 1) // 7)))
        start = 48 + 12 * self.octave
        notes = [start + 12 * o + n for o in range(octaves) for n in (0, 2, 4, 5, 7, 9, 11)] + [start + 12 * octaves]
        ww = kb.width() / len(notes)
        whites = [(n, QRectF(kb.left() + j * ww, kb.top(), ww, kb.height())) for j, n in enumerate(notes)]
        blacks = [(n + 1, QRectF(rect.right() - ww * 0.3, kb.top(), ww * 0.6, kb.height() * 0.6))
                  for n, rect in whites[:-1] if n % 12 in (0, 2, 5, 7, 9)]
        return whites, blacks

    def draw_keys(self, p, r, tr):
        s, pr = self.s, self.project
        kb, side = split(r, [0, 90 * s], 10 * s)
        whites, blacks = self.keyboard(kb)
        color, scaled = self.seq.color(tr), pr.scale != "chromatic"
        for black, keys in ((False, whites), (True, blacks)):  # black keys drawn (and hit) on top
            for note, rect in keys:
                lit = self.held.get(("key", note), 0) > 0 or self.lit(tr, note)
                outside = scaled and not pr.in_scale(note)
                fill = color if lit else (("#14171d" if outside else "#262b36") if black else
                                          ("#7d8594" if outside else "#d7deea"))
                self.box(p, rect.adjusted(1, 0, -1, 0), fill, "#0d0f14", 1.5, radius=6)
                if scaled and (note - pr.key) % 12 == 0:  # the key's tonic
                    mark = QRectF(rect.left() + rect.width() * 0.25, rect.bottom() - 10 * s, rect.width() * 0.5, 5 * s)
                    self.box(p, mark, "#38d6ff", None, radius=2)
                if note % 12 == 0:
                    self.text(p, rect.adjusted(0, 0, 0, -16 * s), note_name(note), BG, 13,
                              Qt.AlignHCenter | Qt.AlignBottom, bold=True)

        def key_at(pos):
            for note, rect in blacks + whites:
                if rect.contains(pos):
                    return note
            return None

        self.region(kb, lambda pos: KeySlide(self, tr, key_at, lambda q: self.velocity(kb, q, toward_bottom=True), pos))
        up = QRectF(side.left(), side.top(), side.width(), side.height() / 2 - 5 * s)  # stacked vertically
        down = QRectF(side.left(), side.center().y() + 5 * s, side.width(), side.height() / 2 - 5 * s)
        self.button(p, up, "OCT +", lambda: self.shift_octave(1), sub=f"{self.octave:+d}" if self.octave else None)
        self.button(p, down, "OCT −", lambda: self.shift_octave(-1))

    def draw_drum_pads(self, p, r, tr):
        s, cols = self.s, 6
        rows = math.ceil(len(DRUM_KIT) / cols)
        for v, (name, note) in enumerate(DRUM_KIT):
            row = QRectF(r.left(), r.top() + (v // cols) * r.height() / rows, r.width(), r.height() / rows)
            rect = split(row.adjusted(0, 4 * s, 0, -4 * s), [0] * cols, 8 * s)[v % cols]
            self.pad(p, rect, ("drum", v), DRUM_COLORS[v], name, str(note), lit=self.lit(tr, note))
            self.region(rect, lambda pos, v=v, note=note, rect=rect: self.press_pad(("drum", v), tr, [(v, note)],
                                                                                  rect, pos))
