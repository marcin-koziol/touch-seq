#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Touch-Seq: a touchscreen live-performance front end for the Carla plugin host — step sequencer (any
number of melody/drum tracks, 16 patterns per track, 8 scenes, bar-quantized switching), per-track plugin
chains and a master bus, live pads/keyboard/XY pad and performance faders, laid out as modular-synth style
modules."""
import copy
import itertools
import json
import math
import os
import re
import shutil
import signal
import sys
import threading
import time
import zlib
from dataclasses import asdict, astuple, dataclass, field
from pathlib import Path

import rtmidi

import plugins
from plugins import PluginRef
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPen, QPolygonF, QRadialGradient
from PySide6.QtWidgets import QApplication, QWidget

__version__ = "0.1.0"

MAX_STEPS = 64
PATTERNS = 16
SCENES = 8
PPQN = 24
TICKS_PER_STEP = PPQN // 4  # steps are 16th notes
STEPS_PER_BAR = 16
BPM_MIN, BPM_MAX = 30, 300
MAX_GATE, GATE_RES = 16.0, 0.125  # gate in steps: up to a bar long, stretched in 1/8-step increments
MOUSE_ID = -1  # pseudo touch id for desktop testing with a mouse
TOUCH_LOG = os.environ.get("TOUCHSEQ_TOUCH_LOG")  # file to log raw touch/mouse events to, for debugging
# drum voices top to bottom, notes as in Sideous Drums
DRUM_KIT = [("Kick", 36), ("Snare", 38), ("HH Cl", 42), ("HH Op", 46), ("Crash", 49), ("Clap", 39),
            ("Rim", 37), ("Tom L", 41), ("Tom M", 45), ("Tom H", 48), ("Ride", 51), ("Maracas", 70)]
# fixed per voice, by family: kick red, snare orange, hats yellow/lime, cymbals blue, claps/rim pink/violet,
# toms greens, shaker lilac
DRUM_COLORS = ["#ff4d4d", "#ff9f1c", "#ffd60a", "#c5e15a", "#4cc9f0", "#f472b6", "#c084fc", "#16a34a",
               "#22c55e", "#86efac", "#60a5fa", "#e9a8f5"]

NOTE_NAMES = "C C# D D# E F F# G G# A A# B".split()
PALETTE = ["#38d6ff", "#ff8c42", "#a3e635", "#f472b6", "#facc15", "#a78bfa", "#2dd4bf", "#fb7185"]
MIN_TEXT = 13  # smallest text size anywhere (in 1024x600 layout units)
BG, PANEL, BORDER = "#0d0f14", "#151922", "#262b36"
TEXT, DIM, PLAY, WARN = "#d7deea", "#8a97ab", "#4ade80", "#ff5d5d"


def note_name(n):
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


# --- model (persisted) -------------------------------------------------------
@dataclass
class Step:
    on: bool = False
    vel: int = 100
    gate: float = 0.5  # in steps; may span several


DEFAULT_STEP = Step()


def new_lane():
    return [Step() for _ in range(MAX_STEPS)]


@dataclass
class Pattern:
    """Steps per lane. Drum lanes are the kit voices (0..11); melody lanes are MIDI notes, created on
    first use, so any number of notes can sound on one step."""
    length: int = 16
    lanes: dict = field(default_factory=dict)

    def lane(self, k):
        if k not in self.lanes:
            self.lanes[k] = new_lane()
        return self.lanes[k]

    def notes_at(self, i):
        return [(k, lane[i]) for k, lane in self.lanes.items() if lane[i].on]

    def empty(self):
        return not any(s.on for lane in self.lanes.values() for s in lane)


SLOTS = 3  # per plugin chain: tracks have an instrument and 2 effects, the master 3 effects


@dataclass(eq=False)  # identity hash: chains are keys in the plugin host
class Strip:
    """Plugin chain: plugins.PluginRef per slot (or None), then volume / mute at its end."""
    slots: list = field(default_factory=lambda: [None] * SLOTS)
    volume: float = 1.0
    mute: bool = False
    legacy: str = field(default="", repr=False)  # state-file prefix from before chains belonged to tracks


def dump_strip(s):
    return {"slots": [asdict(r) if r else None for r in s.slots], "volume": s.volume, "mute": s.mute}


def load_strip(d):
    return Strip([PluginRef(**r) if r else None for r in d["slots"]], d["volume"], d["mute"])


@dataclass(eq=False)  # identity hash: tracks are dict keys in the engine
class Track:
    kind: str = "melody"  # or "drum"
    channel: int = 1
    number: int = 1  # stable name/color, kept when other tracks are deleted
    patterns: list = field(default_factory=list)
    pattern: int = 0
    scenes: list = field(default_factory=lambda: [None] * SCENES)  # pattern index per scene
    mute: bool = False
    voice: int = 0  # drum voice the grid view edits
    last: Step = field(default_factory=Step)  # velocity/gate a newly enabled step gets
    last_note: int = 60  # melody: pitch the grid view paints, and the latest one placed
    strip: Strip = field(default_factory=Strip)  # its plugins: instrument + effects

    def __post_init__(self):
        if not self.patterns:
            self.patterns = [self.new_pattern() for _ in range(PATTERNS)]

    @property
    def drum(self):
        return self.kind == "drum"

    @property
    def current(self):
        return self.patterns[self.pattern]

    def new_pattern(self):
        return Pattern(lanes={k: new_lane() for k in range(len(DRUM_KIT))} if self.drum else {})

    def note(self, lane):
        return DRUM_KIT[lane][1] if self.drum else lane

    @property
    def edit_lane(self):
        """Lane the grid view paints: the selected drum voice, or the melody's latest pitch."""
        return self.voice if self.drum else self.last_note


@dataclass
class Control:
    kind: str  # "pb" (pitch bend), "mod" (mod wheel = CC1) or "cc"
    channel: int = 1
    cc: int = 1
    fine: bool = False  # 14-bit: CC n carries the MSB, CC n+32 the LSB (only for CC 0-31)
    bipolar: bool = False  # rests at and fills from the center
    # where it goes: "channel" (MIDI on `channel`, e.g. outboard gear), "track" (that track's instrument, on the
    # track's channel) or "param" (a plugin parameter directly, no MIDI; CC faders only)
    target: str = "channel"
    track: int = 0  # target "track": a track number; 0 = whichever track the sequencer has selected
    param: list = None  # target "param": [track number (0 = master), slot, parameter index, parameter name]

    @property
    def wide(self):
        """Sends a 14-bit value (pitch bend always does)."""
        return self.kind == "pb" or self.fine

    @property
    def centered(self):
        return self.kind == "pb" or self.bipolar

    @property
    def max(self):
        return 16383 if self.wide else 127


def default_controls():
    # after PB and mod wheel: CCs the MIDI spec leaves undefined, so they don't collide with what synths
    # map by default (volume, pan, cutoff, sustain, ...); 102+ because 0-63 are 14-bit MSB/LSB pairs
    # (e.g. Surge XT learns CC20 as the MSB of a 20/52 pair)
    controls = [Control("pb"), Control("mod", cc=1)] + [Control("cc", cc=102 + i) for i in range(8)]
    for c in controls:  # play whatever track is selected
        c.target = "track"
    return controls


SCALES = {  # name -> semitones above the key
    "chromatic": range(12),
    "major": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10),
    "dorian": (0, 2, 3, 5, 7, 9, 10),
    "phrygian": (0, 1, 3, 5, 7, 8, 10),
    "lydian": (0, 2, 4, 6, 7, 9, 11),
    "mixolydian": (0, 2, 4, 5, 7, 9, 10),
    "locrian": (0, 1, 3, 5, 6, 8, 10),
    "harmonic minor": (0, 2, 3, 5, 7, 8, 11),
    "melodic minor": (0, 2, 3, 5, 7, 9, 11),
    "major pentatonic": (0, 2, 4, 7, 9),
    "minor pentatonic": (0, 3, 5, 7, 10),
    "blues": (0, 3, 5, 6, 7, 10),
}


@dataclass
class Project:
    bpm: int = 120
    tracks: list = field(default_factory=list)
    controls: list = field(default_factory=default_controls)
    master: Strip = field(default_factory=Strip)  # effects every track goes through to the output
    key: int = 0  # 0 = C
    scale: str = "chromatic"

    def in_scale(self, note):
        return (note - self.key) % 12 in SCALES[self.scale]

    @property
    def key_name(self):
        return NOTE_NAMES[self.key] + ("" if self.scale == "chromatic" else f" {self.scale}")


def dump_project(pr):
    """Compact JSON: a pattern stores only the steps that differ from the default."""
    def pattern(p):
        return {"length": p.length, "steps": [[k, i, *astuple(s)] for k, lane in p.lanes.items()
                                              for i, s in enumerate(lane) if s != DEFAULT_STEP]}

    return json.dumps({"bpm": pr.bpm, "key": pr.key, "scale": pr.scale, "tracks": [
        {"kind": t.kind, "channel": t.channel, "number": t.number, "pattern": t.pattern, "scenes": t.scenes, "mute": t.mute,
         "voice": t.voice, "last": astuple(t.last), "last_note": t.last_note, "strip": dump_strip(t.strip),
         "patterns": [pattern(p) for p in t.patterns]}
        for t in pr.tracks], "controls": [astuple(c) for c in pr.controls],
        "master": dump_strip(pr.master)}, separators=(",", ":"))


def new_project():
    return Project(tracks=[Track("drum", 10, 1), Track("melody", 1, 2)])


def project_files(folder):
    """Projects in `folder`, most recently changed first (autosave keeps that current)."""
    return sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def free_project_path(folder, stem):
    """First free '<stem> N.json' (N = 1, 2, ...), its plugin-state folder free too; a trailing number on
    `stem` is replaced, so a copy of 'Project 3' becomes the next free 'Project N'."""
    stem = re.sub(r" \d+$", "", stem)
    for n in itertools.count(1):
        path = folder / f"{stem} {n}.json"
        if not path.exists() and not path.with_suffix(".plugins").exists():
            return path


def load_project(path):
    if not path.exists():
        return new_project()
    d = json.loads(path.read_text())
    tracks = []
    for n, t in enumerate(d["tracks"], 1):
        last, last_note = t["last"], t.get("last_note", 60)
        if len(last) == 4:  # older files: steps carried a note (on, note, vel, gate)
            last, last_note = [last[0], last[2], last[3]], last[1]
        tr = Track(t["kind"], t["channel"], t.get("number", n), pattern=t["pattern"], scenes=t["scenes"], mute=t["mute"],
                   voice=t["voice"], last=Step(*last), last_note=last_note,
                   strip=load_strip(t["strip"]) if "strip" in t else Strip())
        for p, pd in zip(tr.patterns, t["patterns"]):
            p.length = pd["length"]
            for row in pd["steps"]:
                if len(row) == 6:  # older files: [lane, step, on, note, vel, gate]; a melody's lane is its note
                    k, i, on, note, vel, gate = row
                    row = [k if tr.drum else note, i, on, vel, gate]
                k, i, *values = row
                p.lane(k)[i] = Step(*values)
        tracks.append(tr)
    controls = [Control(*c) for c in d["controls"]] if "controls" in d else default_controls()  # c may lack newer fields
    # older files: 4 strips on MIDI channels; each goes to the first track on its channel with no plugins
    for n, s in enumerate(d.get("strips", []), 1):
        tr = next((t for t in tracks if t.channel == s["channel"] and not any(t.strip.slots)), None)
        if tr and any(s["slots"]):
            tr.strip = load_strip(s)
            tr.strip.legacy = f"s{n}"
    master = load_strip(d["master"]) if "master" in d else Strip()
    return Project(d["bpm"], tracks, controls, master, d.get("key", 0), d.get("scale", "chromatic"))


def save_text(path, text):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# --- engine ------------------------------------------------------------------
def open_midi(port):
    """Opens an existing output port whose name contains `port`, else creates a virtual one."""
    out = rtmidi.MidiOut()
    matches = [i for i, name in enumerate(out.get_ports()) if port.lower() in name.lower()]
    if matches:
        out.open_port(matches[0])
        print(f"MIDI: connected to {out.get_ports()[matches[0]]}")
    else:
        out.open_virtual_port(port)
        print(f"MIDI: virtual port '{port}'")
    return out


class MidiRouter:
    """The engine's MIDI output: everything goes to the main port; channel messages also go to the plugin
    host: a track's notes to that track's instrument, messages without a track (CC, pitch bend from the
    faders) to the instruments of the tracks on that channel."""

    def __init__(self, main):
        self.main = main
        self.taps = {}  # Track -> MIDI out port feeding the track's instrument

    def send_message(self, msg, track=None):
        self.main.send_message(msg)
        if msg[0] >= 0xF0:
            return
        if track is not None:
            port = self.taps.get(track)
            if port:
                port.send_message(msg)
            return
        channel = (msg[0] & 0x0F) + 1
        for tr, port in list(self.taps.items()):
            if tr.channel == channel:
                port.send_message(msg)


class Engine:
    """Clock + playback thread. Everything that touches playback state or sends MIDI holds `lock`."""

    def __init__(self, project, out):
        self.project, self.out = project, out
        self.lock = threading.RLock()
        self.playing = False
        self.ticks = 0
        self.next_tick = 0.0
        self.step_index = -1  # global step last played
        self.pos = {}  # Track -> next step
        self.playhead = {}  # Track -> step last played
        self.pending = {}  # Track -> pattern queued for the next bar
        self.pending_scene = None
        self.scene = None  # last launched scene
        self.sounding = {}  # (channel, note) -> (note-off time, track)
        self.step_time, self.step_len = 0.0, 0.125  # when the last step played, and a step's length (s)
        self.rec = None  # (track, replace) while recording live playing into a track
        self.rec_fresh = set()  # (lane, step) just played in ahead of the playhead: not replayed, not erased
        self.rec_taken = set()  # (lane, step) recorded since recording started: REC doesn't erase them
        threading.Thread(target=self.run, daemon=True).start()

    # --- thread ---------------------------------------------------------
    def run(self):
        while True:
            with self.lock:
                now = time.perf_counter()
                for key, (off, _) in list(self.sounding.items()):
                    if off <= now:
                        self.note_off(key)
                if self.playing and now >= self.next_tick:
                    self.tick(now)
                wake = min([self.next_tick if self.playing else now + 0.005, *(o for o, _ in self.sounding.values())])
            time.sleep(max(0.0, min(wake - time.perf_counter(), 0.005)))

    def tick(self, now):
        if now - self.next_tick > 0.05:  # fell far behind (e.g. system stall): don't burst-catch-up
            self.next_tick = now
        self.out.send_message([0xF8])
        if self.ticks % TICKS_PER_STEP == 0:
            self.step(self.next_tick)
        self.ticks += 1
        self.next_tick += 60 / (self.project.bpm * PPQN)

    def step(self, t):
        self.step_index = self.ticks // TICKS_PER_STEP
        if self.step_index % STEPS_PER_BAR == 0:
            self.apply_pending()
        step_len = TICKS_PER_STEP * 60 / (self.project.bpm * PPQN)
        self.step_time, self.step_len = t, step_len
        for tr in self.project.tracks:
            pat = tr.current
            i = self.pos.get(tr, self.step_index) % pat.length  # a track added mid-play joins in step
            self.playhead[tr] = i
            self.pos[tr] = (i + 1) % pat.length
            recording = self.rec is not None and self.rec[0] is tr
            lanes = list(pat.lanes.items())  # a copy: the UI thread may add a lane meanwhile
            if recording and self.rec[1]:  # REC replaces what was there before this recording
                for k, lane in lanes:
                    if (k, i) not in self.rec_taken:
                        lane[i].on = False
            if tr.mute:
                continue
            for k, lane in lanes:
                s = lane[i]
                if s.on:
                    if recording and (k, i) in self.rec_fresh:  # already heard live
                        self.rec_fresh.discard((k, i))
                        continue
                    self.note_on(tr, tr.note(k), s.vel, t + max(0.005, s.gate * step_len))

    def note_on(self, tr, note, vel, off):
        key = (tr.channel, note)
        if key in self.sounding:  # retrigger: close the previous note first
            self.note_off(key)
        self.out.send_message([0x90 | (tr.channel - 1), note, vel], tr)
        self.sounding[key] = (off, tr)

    def note_off(self, key):
        channel, note = key
        self.out.send_message([0x80 | (channel - 1), note, 0], self.sounding.pop(key)[1])

    def apply_pending(self):
        if self.pending_scene is not None:
            for tr in self.project.tracks:
                if tr.scenes[self.pending_scene] is not None:
                    self.set_pattern(tr, tr.scenes[self.pending_scene])
            self.scene, self.pending_scene = self.pending_scene, None
        for tr, p in self.pending.items():
            self.set_pattern(tr, p)
        self.pending.clear()

    def set_pattern(self, tr, p):
        tr.pattern = p
        self.pos[tr] = 0

    # --- control (UI thread) --------------------------------------------
    def start(self):
        with self.lock:
            self.playing, self.ticks, self.step_index = True, 0, -1
            self.pos.clear()
            self.playhead.clear()
            self.next_tick = time.perf_counter() + 0.005
            self.out.send_message([0xFA])

    def stop(self):
        with self.lock:
            self.playing = False
            self.out.send_message([0xFC])
            for key in list(self.sounding):
                self.note_off(key)
            self.playhead.clear()
            self.rec_fresh.clear()
            self.rec_taken.clear()
            self.step_index = -1
            self.apply_pending()

    def position(self, tr):
        """Where in its pattern the track is right now, in (fractional) steps; None when stopped."""
        with self.lock:
            if not self.playing or tr not in self.playhead:
                return None
            return self.playhead[tr] + (time.perf_counter() - self.step_time) / self.step_len

    def live(self, tr, note, vel):
        """Note on (vel > 0) / off from a live pad, outside the sequencer's own note bookkeeping."""
        with self.lock:
            self.out.send_message([(0x90 if vel else 0x80) | (tr.channel - 1), note, vel], tr)

    def send(self, msg, track=None):
        with self.lock:
            self.out.send_message(msg, track)

    def preview(self, tr, note):
        with self.lock:
            self.note_on(tr, note, 100, time.perf_counter() + 0.15)

    def queue_pattern(self, tr, p):
        with self.lock:
            self.pending[tr] = p
            if not self.playing:
                self.apply_pending()

    def queue_scene(self, sc):
        with self.lock:
            self.pending_scene = sc
            for tr in self.project.tracks:  # the scene overrides earlier per-track requests
                if tr.scenes[sc] is not None:
                    self.pending.pop(tr, None)
            if not self.playing:
                self.apply_pending()

    def store_scene(self, sc):
        with self.lock:
            for tr in self.project.tracks:
                nxt = self.next_pattern(tr)
                tr.scenes[sc] = tr.pattern if nxt is None else nxt
            self.scene = sc

    def next_pattern(self, tr):
        if tr in self.pending:
            return self.pending[tr]
        if self.pending_scene is not None:
            return tr.scenes[self.pending_scene]
        return None

    def add_track(self, kind):
        with self.lock:
            number = max((t.number for t in self.project.tracks), default=0) + 1
            tr = Track(kind, 10 if kind == "drum" else min(len(self.project.tracks) + 1, 16), number)
            self.project.tracks.append(tr)
            return tr

    def remove_track(self, tr):
        with self.lock:
            self.project.tracks.remove(tr)
            for d in (self.pos, self.playhead, self.pending):
                d.pop(tr, None)

    def close(self):
        if self.playing:
            self.stop()


# --- gestures ----------------------------------------------------------------
class Gesture:
    """Interaction owned by one touch point, created by a hit region on press."""

    def move(self, pos):
        pass

    def release(self, pos):
        pass

    def cancel(self):
        """The touch was taken away (TouchCancel): undo any 'held' state, but don't act."""

    def tick(self):
        pass


class Drag(Gesture):
    """Vertical drag adjusts a value; a release without movement counts as a tap."""
    THRESHOLD = 8

    HOLD = 0.6  # s without moving: on_hold

    def __init__(self, pos, get, set_, lo, hi, px, unit=1, on_tap=None, on_hold=None):
        self.y0, self.v0 = pos.y(), get()
        self.set, self.lo, self.hi, self.px, self.unit, self.on_tap = set_, lo, hi, px, unit, on_tap
        self.on_hold, self.t0, self.held = on_hold, time.monotonic(), False
        self.dragging = False

    def tick(self):
        if self.on_hold and not self.dragging and not self.held and time.monotonic() - self.t0 >= self.HOLD:
            self.held = True
            self.on_hold()

    def move(self, pos):
        if self.held:
            return
        dy = self.y0 - pos.y()
        if not self.dragging:
            if abs(dy) >= self.THRESHOLD:
                self.dragging, self.y0 = True, pos.y()
            return
        v = self.v0 + round(dy / self.px) * self.unit
        self.set(round(max(self.lo, min(self.hi, v)), 2))

    def release(self, pos):
        if not self.dragging and not self.held and self.on_tap:
            self.on_tap()


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


class Hold(Gesture):
    """Fires after being held for `secs` (for destructive actions)."""

    def __init__(self, fn, secs=0.8, key=None):
        self.fn, self.secs, self.key, self.t0, self.fired = fn, secs, key, time.monotonic(), False

    def progress(self):
        return min(1.0, (time.monotonic() - self.t0) / self.secs)

    def tick(self):
        if not self.fired and self.progress() >= 1:
            self.fired = True
            self.fn()


class ScrollOrTap(Gesture):
    """Vertical drag scrolls (value moves by -dy/px); a release without movement counts as a tap."""

    def __init__(self, pos, get, set_, on_tap, px=1.0):
        self.y0, self.v0, self.set, self.on_tap, self.px, self.moved = pos.y(), get(), set_, on_tap, px, False

    def move(self, pos):
        dy = pos.y() - self.y0
        self.moved = self.moved or abs(dy) > Drag.THRESHOLD
        if self.moved:
            self.set(self.v0 - dy / self.px)

    def release(self, pos):
        if not self.moved:
            self.on_tap()


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


# --- UI ----------------------------------------------------------------------
class Panel:
    """A rack module's touch surface: immediate-mode drawing in its own coordinates (0,0 = top left of
    its screen) plus one gesture per touch point. Hit regions are collected while drawing."""

    def __init__(self):
        self.w = self.h = 0.0
        self.s = 1.0  # UI scale relative to a 1024x600 layout
        self.gestures = {}
        self.hits = []

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
        self.popup = None  # "bpm" while the tempo popup is open
        self.taps = []  # tap-tempo times
        self.scroll = 0.0
        self.master_fx = lambda: False  # the rack: is XYFX on the master (adds a MASTER row)
        self.master_focus = False  # the MASTER row is selected (PLAY-12 shows the XY pad)

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
        self.engine.stop() if self.engine.playing else self.engine.start()

    def toggle_mode(self, mode):
        self.mode = None if self.mode == mode else mode

    def press_pattern(self, i):
        tr = self.sel
        with self.engine.lock:
            if self.mode == "copy":
                tr.patterns[i] = copy.deepcopy(tr.current)
            elif self.mode == "clear":
                tr.patterns[i] = tr.new_pattern()
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

    def add_track(self, kind):
        self.sel = self.engine.add_track(kind)
        self.scroll = 1e9  # clamped on paint: shows the new row

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
        (self.draw_grid if self.view == "grid" else self.draw_edit)(p, mid)
        if self.popup == "bpm":
            self.draw_bpm_popup(p)

    def draw_bpm_popup(self, p):
        s, pr = self.s, self.project
        p.fillRect(self.rect(), QColor(0, 0, 0, 170))
        self.on_press(self.rect(), lambda: setattr(self, "popup", None))  # tap outside closes
        panel = QRectF(0, 0, 600 * s, 380 * s)
        panel.moveCenter(self.rect().center())
        self.box(p, panel, "#11141b", "#38d6ff", 2, radius=16)
        self.on_press(panel, lambda: None)  # swallow taps on the panel background
        inner = panel.adjusted(24 * s, 18 * s, -24 * s, -24 * s)

        self.text(p, inner, "TEMPO", DIM, 14, Qt.AlignHCenter | Qt.AlignTop, bold=True)
        big = QRectF(inner.left(), inner.top() + 26 * s, inner.width(), 120 * s)
        self.box(p, big, PANEL, BORDER, radius=12)
        self.text(p, big.adjusted(0, -8 * s, 0, 0), str(pr.bpm), TEXT, 78, bold=True)
        self.text(p, big.adjusted(0, 0, 0, -10 * s), "drag ↕", "#5b6577", 11, Qt.AlignHCenter | Qt.AlignBottom)
        self.region(big, lambda pos: Drag(pos, lambda: pr.bpm, self.set_bpm, BPM_MIN, BPM_MAX, 4 * s))

        steps = QRectF(inner.left(), big.bottom() + 16 * s, inner.width(), 70 * s)
        for d, r in zip((-10, -5, -1, 1, 5, 10), split(steps, [0] * 6, 10 * s)):
            self.button(p, r, f"{d:+d}", lambda d=d: self.set_bpm(pr.bpm + d))

        tap, done = split(QRectF(inner.left(), steps.bottom() + 16 * s, inner.width(), inner.bottom() - steps.bottom()
                                 - 16 * s), [0, 160 * s], 10 * s)
        self.button(p, tap, "TAP", self.tap_tempo, color=PLAY, pending=bool(self.taps) and time.monotonic() - self.taps[-1] < 2, sub="tap tempo")
        self.button(p, done, "DONE", lambda: setattr(self, "popup", None), on=True)

    def set_bpm(self, v):
        self.project.bpm = int(max(BPM_MIN, min(BPM_MAX, v)))

    def tap_tempo(self):
        now = time.monotonic()
        if self.taps and now - self.taps[-1] > 2:  # long pause starts a new measurement
            self.taps = []
        self.taps = (self.taps + [now])[-5:]
        if len(self.taps) >= 2:
            self.set_bpm(round(60 * (len(self.taps) - 1) / (self.taps[-1] - self.taps[0])))

    def draw_top(self, p, r):
        s, e = self.s, self.engine
        play, bpm, bar, grid, edit, _, *scenes, save = split(
            r, [86 * s, 96 * s, 110 * s, 72 * s, 72 * s, 0] + [50 * s] * SCENES + [72 * s], 6 * s)

        self.button(p, play, "", self.toggle_play, on=e.playing, color=PLAY)
        c, k = play.center(), play.height() * 0.22
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(BG if e.playing else TEXT))
        if e.playing:
            p.drawRect(QRectF(c.x() - k, c.y() - k, 2 * k, 2 * k))
        else:
            p.drawPolygon(QPolygonF([QPointF(c.x() - k, c.y() - k * 1.2), QPointF(c.x() - k, c.y() + k * 1.2),
                                     QPointF(c.x() + k * 1.3, c.y())]))

        pr = self.project
        self.value_box(p, bpm, "BPM", str(pr.bpm), lambda: pr.bpm, self.set_bpm, BPM_MIN, BPM_MAX, 6,
                       on_tap=lambda: setattr(self, "popup", "bpm"))

        self.box(p, bar, PANEL)
        si = e.step_index
        beat = (si % STEPS_PER_BAR) // 4 if si >= 0 else -1
        self.text(p, bar.adjusted(10 * s, 0, 0, 0), f"{si // STEPS_PER_BAR + 1 if si >= 0 else '-'}",
                  TEXT, 20, Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        cell = (bar.width() - 54 * s) / 4
        for b in range(4):
            br = QRectF(bar.left() + 44 * s + b * cell, bar.center().y() - 7 * s, cell - 6 * s, 14 * s)
            self.box(p, br, PLAY if b == beat else "#222734", None, radius=3)

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
            if tr is None:
                for rect, kind, label in zip(split(head, [0, 0], g), ("melody", "drum"), ("+ MELODY", "+ DRUM")):
                    self.button(p, rect, label, lambda kind=kind: self.add_track(kind))
                    self.hits[-1] = (rect.intersected(r), self.hits[-1][1])
                continue
            lane = QRectF(head.right() + 10 * s, y, r.right() - head.right() - 18 * s, rh - g)
            self.draw_header(p, head, r, tr)
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
        self.text(p, info, f"CH{tr.channel}", DIM, 12, Qt.AlignRight | Qt.AlignTop)
        if tr.drum:
            self.text(p, info, DRUM_KIT[tr.voice][0], color, 12, Qt.AlignRight | Qt.AlignBottom, bold=True)
        nxt = e.next_pattern(tr)
        pat = f"P{tr.pattern + 1}" + (f"→{nxt + 1}" if nxt is not None and nxt != tr.pattern else "")
        self.text(p, info, pat, "#facc15" if nxt not in (None, tr.pattern) else DIM, 12, Qt.AlignLeft | Qt.AlignBottom)
        self.region(head.intersected(clip), lambda pos, tr=tr: ScrollOrTap(
            pos, lambda: self.scroll, lambda v: setattr(self, "scroll", v), lambda: self.select(tr)))
        mute = QRectF(head.right() - 50 * s, head.top() + 6 * s, 44 * s, head.height() - 12 * s)
        self.button(p, mute, "M", lambda: setattr(tr, "mute", not tr.mute), on=tr.mute, color=WARN)
        self.hits[-1] = (mute.intersected(clip), self.hits[-1][1])

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
        params = ("step", "vel") if tr.drum else ("vel", "gate")
        octaves = [] if tr.drum else [80 * s, 80 * s]
        name, ch, length, mute, _, *param_r, _, delete = split(
            ctrl, [110 * s, 90 * s, 90 * s, 80 * s, 0] + [90 * s] * len(params) + octaves + [0, 110 * s], 6 * s)
        if octaves:
            *param_r, down, up = param_r
            self.button(p, down, "OCT ▼", lambda: self.scroll_roll(tr, -12))
            self.button(p, up, "OCT ▲", lambda: self.scroll_roll(tr, 12))

        self.box(p, name, color, None)
        self.text(p, name.adjusted(0, 0, 0, -name.height() * 0.3), f"T{tr.number}  P{tr.pattern + 1}",
                  BG, 18, bold=True)
        self.text(p, name.adjusted(0, name.height() * 0.45, 0, 0), tr.kind.upper(), BG, 11)
        self.value_box(p, ch, "CHANNEL", str(tr.channel), lambda: tr.channel,
                       lambda v: setattr(tr, "channel", int(v)), 1, 16, 14)

        def cycle_length():
            pat.length = 16 if pat.length >= 64 else (pat.length // 16 + 1) * 16

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
        (self.draw_drum_matrix if tr.drum else self.draw_roll)(p, area, tr, color)

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

    def draw(self, p):
        s, g = self.s, 8 * self.s
        p.fillRect(self.rect(), QColor(BG))
        header = QRectF(g, g, self.w - 2 * g, 56 * s)
        info, edit = split(header, [0, 130 * s], 6 * s)
        hint = ("TARGET: tap to switch · a plugin parameter: hold its knob in HOST" if self.editing
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


class Slide(Gesture):
    """Vertical slider: the finger's height inside `track` is the value (0..1)."""

    def __init__(self, track, set_, pos):
        self.track, self.set = track, set_
        self.move(pos)

    def move(self, pos):
        self.set(max(0.0, min(1.0, (self.track.bottom() - pos.y()) / self.track.height())))


# knob colors by what a parameter's name says it is; first family that matches wins (so "LFO Rate" is an
# LFO and "Filter Attack" an envelope). (substrings, whole words) per family
PARAM_FAMILIES = [
    ("lfo", "#a78bfa", ("lfo", "vibrato", "tremolo"), ("mod", "rate")),  # not "depth": "Punch Depth" isn't one
    ("envelope", "#4ade80", ("attack", "decay", "sustain", "release", "adsr", "envelope"), ("env", "eg", "hold")),
    ("filter", "#ff8c42", ("cutoff", "filter", "reso", "hpf", "lpf", "bpf", "vcf"), ("q", "flt")),
    ("effect", "#f472b6", ("reverb", "delay", "chorus", "phaser", "flanger", "echo", "feedback", "room", "dist",
                           "drive"), ()),
    ("oscillator", "#60a5fa", ("osc", "wave", "pitch", "tune", "detune", "octave", "pulse", "shape"),
     ("semi", "fine", "pw")),
    ("level", "#facc15", ("volume", "level", "gain"), ("mix", "amp", "vol", "pan", "wet", "dry")),
]


def param_color(name):
    """Knob color for a plugin parameter, guessed from its name (unknown: the default cyan)."""
    low = name.lower()
    words = set(re.split(r"[^a-z0-9]+", low))
    for _, color, parts, whole in PARAM_FAMILIES:
        if any(p in low for p in parts) or words & set(whole):
            return color
    return "#38d6ff"


XYFX_LABELS = {"touchseq.xyfx", "urn:touch-seq:xyfx"}  # CLAP id, LV2 URI (xyfx/ in this repo)


class HostPanel(Panel):
    """Plugin host (Carla). Every track has a chain (instrument -> 2 effects); all track chains go through
    the master's effects to the audio output. Views: TRACK (the chain of the track selected in the
    sequencer, the chosen plugin's parameters as knobs), MIXER (every track's level, and the master) and
    MASTER (the master chain, like TRACK). If Carla or JACK isn't available the panel says so and the rest
    of the rack works as before."""
    KNOB_COLS, KNOB_ROWS = 5, 3
    LETTERS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def __init__(self, project, router, state_dir, cache_path, audio_out="default", remember=None, name="Touch-Seq"):
        super().__init__()
        self.project, self.router, self.state_dir, self.audio_out, self.name = project, router, state_dir, audio_out, name
        self.remember = remember  # called with a newly chosen audio output, to keep it for next time
        self.selected = lambda: project.tracks[0]  # the track whose chain TRACK shows (the rack: the sequencer's)
        self.select_track = lambda tr: None  # the rack: select a track in the sequencer
        self.view = "track"  # "track", "mixer" or "master"
        self.loaded = {}  # (Strip, slot) -> plugins.Plugin
        self.failed = set()  # (Strip, slot) whose saved plugin couldn't be loaded
        self.sel = None  # (Strip, slot) whose parameters are shown
        self.page = 0
        self.mixer_page = 0
        self.browser = None  # (Strip, slot) while choosing a plugin for it
        self.assigning = None  # (Strip, slot, Param) while choosing a fader for a held knob
        self.browser_fmt = None  # None = all formats
        self.browser_scroll = 0.0
        self.message = ""
        self.wiring = plugins.Wiring()
        try:
            self.host = plugins.Host(name)
        except (RuntimeError, OSError, ImportError) as e:
            self.host, self.message = None, f"plugin host unavailable: {e}"
            return
        self.catalog = plugins.Catalog(cache_path)
        self.sink = self.current_sink()
        self.sink_changed = False
        for strip in self.strips():
            for k, ref in enumerate(strip.slots):
                if ref:
                    self.load(strip, k, ref, restore=True)
        self.rewire()
        threading.Thread(target=self.watch_output, daemon=True).start()

    # --- chains ---------------------------------------------------------------
    def strips(self):
        return [t.strip for t in self.project.tracks] + [self.project.master]

    def track_of(self, strip):
        return next((t for t in self.project.tracks if t.strip is strip), None)  # None: the master

    def instrument_slot(self, strip, k):
        return k == 0 and strip is not self.project.master

    def chain(self, strip):
        return [self.loaded[(strip, k)] for k in range(SLOTS) if (strip, k) in self.loaded]

    def current_sink(self):
        return plugins.default_sink() if self.audio_out == "default" else self.audio_out

    def set_output(self, name):
        """'default' (follow the system's default output) or a sink's node name."""
        self.audio_out, self.sink = name, None
        self.sink = self.current_sink()
        self.rewire()
        if self.remember:
            self.remember(name)

    def watch_output(self):
        """Background: follow the default output (it changes e.g. when headphones are plugged in or out)
        and put back links that disappeared with a device. Rewiring itself happens in tick()."""
        while self.host:
            time.sleep(2)
            sink = self.current_sink()
            if sink and sink != self.sink:
                self.sink, self.sink_changed = sink, True
            else:
                self.wiring.heal()

    def state_file(self, strip, k, ref, prefix=None):
        """Per chain, slot and plugin, so a state never gets loaded into a different plugin."""
        tr = self.track_of(strip)
        tag = zlib.crc32(f"{ref.fmt}|{ref.path}|{ref.label}".encode())
        prefix = prefix or (f"t{tr.number}" if tr else "master")
        return self.state_dir / f"{prefix}-{k + 1}-{tag:08x}.carxs"

    def load(self, strip, k, ref, restore=False):
        tr = self.track_of(strip)
        client = f"{f'T{tr.number}' if tr else 'M'}.{k + 1} {ref.name}".replace(":", " ")[:48]
        try:
            if tr and k == 0:
                self.ensure_port(tr)
            plugin = self.host.add(ref, client)
        except (RuntimeError, rtmidi.RtMidiError) as e:
            self.failed.add((strip, k))
            self.message = str(e)
            return
        self.failed.discard((strip, k))
        self.loaded[(strip, k)] = plugin
        state = self.state_file(strip, k, ref)
        if restore and not state.exists() and strip.legacy:
            state = self.state_file(strip, k, ref, strip.legacy)
        if restore and state.exists():
            self.host.load_state(plugin, state)
            self.host.refresh_params(plugin)

    def ensure_port(self, tr):
        """The JACK MIDI port feeding this track's instrument (one JACK client per track)."""
        if tr not in self.router.taps:
            port = rtmidi.MidiOut(rtmidi.API_UNIX_JACK, name=f"{self.name} T{tr.number}")
            port.open_virtual_port("midi")
            self.router.taps[tr] = port

    def unload(self, strip, k):
        plugin = self.loaded.pop((strip, k), None)
        if plugin:
            self.host.show_ui(plugin, False)
            self.host.remove(plugin)
        self.failed.discard((strip, k))

    def set_slot(self, strip, k, ref):
        old = strip.slots[k]
        self.unload(strip, k)
        if old:
            self.state_file(strip, k, old).unlink(missing_ok=True)
        strip.slots[k] = ref
        if ref:
            self.load(strip, k, ref)
        self.rewire()
        self.sel, self.page = ((strip, k) if ref else None), 0

    def forget_deleted_tracks(self):
        """Tracks deleted in the sequencer take their plugins, MIDI port and saved states with them."""
        live = set(self.strips())
        gone = {strip for strip, _ in self.loaded if strip not in live}
        for strip, k in [key for key in self.loaded if key[0] in gone]:
            self.unload(strip, k)
        for tr in [tr for tr in self.router.taps if tr not in self.project.tracks]:
            self.router.taps.pop(tr).close_port()
            for f in self.state_dir.glob(f"t{tr.number}-*.carxs"):
                f.unlink()
        if gone:
            for strip in gone:
                self.wiring.replace(strip, [])
            self.rewire()

    def rewire(self):
        """Link every chain: a track's MIDI port to its instrument, plugin to plugin, the end of each track
        chain into the master's effects (or the output if it has none), the master to the output; and set
        the levels."""
        outs = lambda pl: [f"{pl.client}:output_{n}" for n in range(1, self.host.ports(pl)[1] + 1)]
        ins = lambda pl: [f"{pl.client}:input_{n}" for n in range(1, self.host.ports(pl)[0] + 1)]
        sink = [f"{self.sink}:playback_FL", f"{self.sink}:playback_FR"] if self.sink else []
        master = self.chain(self.project.master)
        into_master = ins(master[0]) if master else sink
        for strip in self.strips():
            chain, tr = self.chain(strip), self.track_of(strip)
            links = []
            if tr and chain and chain[0].ref.midi_ins:
                links.append((f"{self.name} T{tr.number}:midi", f"{chain[0].client}:events-in"))
            for a, b in zip(chain, chain[1:]):
                links += self.pair(outs(a), ins(b))
            if chain:
                links += self.pair(outs(chain[-1]), sink if strip is self.project.master else into_master)
            self.wiring.replace(strip, links)
        self.apply_levels()

    def apply_levels(self):
        """A chain's level sits on its last plugin; without master effects the master level joins each track's."""
        m = self.project.master
        master_chain = self.chain(m)
        master_level = 0.0 if m.mute else m.volume
        for strip in self.strips():
            chain = self.chain(strip)
            for pl in chain:
                self.host.set_volume(pl, 1.0)
            if chain:
                level = 0.0 if strip.mute else strip.volume
                if strip is not m and not master_chain:
                    level *= master_level
                self.host.set_volume(chain[-1], level if strip is not m else master_level)

    @staticmethod
    def pair(outs, ins):
        """Stereo links: first two outputs to first two inputs; a mono side feeds / takes both."""
        outs, ins = outs[:2], ins[:2]
        if not outs or not ins:
            return []
        return [(outs[min(j, len(outs) - 1)], ins[min(j, len(ins) - 1)]) for j in range(max(len(outs), len(ins)))]

    def save_states(self):
        if not self.host or not self.loaded:
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for (strip, k), plugin in self.loaded.items():
            self.host.save_state(plugin, self.state_file(strip, k, plugin.ref))

    def close(self):
        if self.host:
            self.save_states()
            self.host.close()
            self.host, self.message = None, "plugin host closed"  # UI timers may still tick

    def tick(self):
        super().tick()
        if self.host:
            self.host.idle()
            self.forget_deleted_tracks()
            if self.xyfx() and self.xy_values().get("bpm") != self.project.bpm:  # repeats follow the tempo
                self.xy(bpm=self.project.bpm)
            if self.sink_changed:  # Carla calls belong in this (the UI) thread
                self.sink_changed = False
                self.rewire()

    # --- fader links (Controls target "param") ------------------------------------
    def link_plugin(self, link):
        owner, slot = link[0], link[1]
        strip = self.project.master if owner == 0 else next(
            (t.strip for t in self.project.tracks if t.number == owner), None)
        return self.loaded.get((strip, slot)) if strip is not None and self.host else None

    def link_param(self, link):
        plugin = self.link_plugin(link)
        prm = next((q for q in plugin.params if q.index == link[2]), None) if plugin else None
        return plugin, prm

    def link_value(self, link):
        plugin, prm = self.link_param(link)
        return (self.host.value(plugin, prm.index) - prm.lo) / (prm.hi - prm.lo) if prm else None

    def link_text(self, link):
        plugin, prm = self.link_param(link)
        return self.short(self.host.text(plugin, prm.index)) if prm else None

    def set_link(self, link, norm):
        plugin, prm = self.link_param(link)
        if prm:
            v = prm.lo + norm * (prm.hi - prm.lo)
            self.host.set_value(plugin, prm.index, round(v) if prm.integer or prm.boolean else v)

    def link_to_fader(self, i):
        """Make CC fader i drive the parameter whose knob was held (self.assigning)."""
        strip, slot, prm = self.assigning
        tr = self.track_of(strip)
        c = self.project.controls[i]
        c.target, c.param = "param", [tr.number if tr else 0, slot, prm.index, prm.name]
        self.assigning = None

    def xyfx(self):
        """Touch-Seq XYFX on the master chain (its UI is the XY pad in PLAY-12), or None."""
        m = self.project.master
        return next((pl for k in range(SLOTS) if (pl := self.loaded.get((m, k))) and pl.ref.label in XYFX_LABELS),
                    None) if self.host else None

    def xy(self, **values):
        """Set XYFX parameters by name: engage, x, y, bpm (0..1 except bpm)."""
        plugin = self.xyfx()
        if plugin:
            for prm in plugin.params:
                if prm.name.lower() in values:
                    self.host.set_value(plugin, prm.index, values[prm.name.lower()])

    def xy_values(self):
        plugin = self.xyfx()
        return {prm.name.lower(): self.host.value(plugin, prm.index) for prm in plugin.params} if plugin else {}

    def set_mix(self, strip, volume=None, mute=None):
        if volume is not None:
            strip.volume = volume
        if mute is not None:
            strip.mute = mute
        self.apply_levels()

    # --- drawing ------------------------------------------------------------
    def draw(self, p):
        s, g = self.s, 8 * self.s
        p.fillRect(self.rect(), QColor(BG))
        if not self.host:
            self.text(p, self.rect().adjusted(20 * s, 0, -20 * s, 0), self.message, WARN, 15)
            return
        if self.browser:
            self.draw_browser(p)
            return
        if self.assigning:
            self.draw_assign(p)
            return
        header = QRectF(g, g, self.w - 2 * g, 44 * s)
        *views, status = split(header, [100 * s, 100 * s, 100 * s, 0], 6 * s)
        for rect, view in zip(views, ("track", "mixer", "master")):
            self.button(p, rect, view.upper(), lambda view=view: self.show(view), on=self.view == view)
        text = self.message or (f"{len(self.catalog.plugins)} plugins" if self.catalog.ready
                                else f"scanning plugins… {self.catalog.progress}")
        self.text(p, status.adjusted(10 * s, 0, 0, 0), text, WARN if self.message else DIM, 13,
                  Qt.AlignRight | Qt.AlignVCenter, elide=True)
        body = QRectF(g, header.bottom() + 10 * s, self.w - 2 * g, self.h - header.bottom() - 10 * s - g)
        if self.view == "mixer":
            self.draw_mixer(p, body)
            return
        strip = self.project.master if self.view == "master" else self.selected().strip
        left, right = split(body, [body.width() * 0.28, 0], 12 * s)  # narrow chain, room for 5 knob columns
        self.draw_chain(p, left, strip)
        self.draw_params(p, right, strip)

    def show(self, view, sel=None):
        self.view, self.sel, self.page = view, sel, 0

    def draw_chain(self, p, r, strip):
        """Title, the slots top to bottom, and the chain's level fader + mute beside them."""
        s, tr = self.s, self.track_of(strip)
        color = PALETTE[(tr.number - 1) % len(PALETTE)] if tr else "#facc15"
        title = QRectF(r.left(), r.top(), r.width(), 34 * s)
        self.box(p, QRectF(title.left(), title.top() + 6 * s, 5 * s, title.height() - 12 * s), color, None, radius=2)
        name = f"T{tr.number}  {tr.kind}  CH{tr.channel}" if tr else "MASTER  ·  all tracks → output"
        self.text(p, title.adjusted(14 * s, 0, 0, 0), name, color, 16, Qt.AlignLeft | Qt.AlignVCenter, bold=True,
                  elide=True)
        slots, fader = split(QRectF(r.left(), title.bottom() + 8 * s, r.width(), r.bottom() - title.bottom() - 8 * s),
                             [0, 56 * s], 10 * s)
        sh = (slots.height() - 2 * 8 * s) / SLOTS
        for k in range(SLOTS):
            self.draw_slot(p, QRectF(slots.left(), slots.top() + k * (sh + 8 * s), slots.width(), sh), strip, k)
        self.draw_fader(p, fader, strip)

    def draw_fader(self, p, r, strip, label=True):
        s = self.s
        mute = QRectF(r.left(), r.bottom() - 40 * s, r.width(), 40 * s)
        self.button(p, mute, "M", lambda: self.set_mix(strip, mute=not strip.mute), on=strip.mute, color=WARN)
        bottom = mute.top() - (24 * s if label else 8 * s)
        track = QRectF(r.center().x() - 14 * s, r.top() + 4 * s, 28 * s, bottom - r.top() - 4 * s)
        self.box(p, track, "#141821", BORDER, radius=8)
        level = track.height() * strip.volume
        self.box(p, QRectF(track.left(), track.bottom() - level, track.width(), level), QColor(74, 222, 128, 150),
                 None, radius=6)
        if label:
            self.text(p, QRectF(r.left(), track.bottom() + 2 * s, r.width(), 20 * s), f"{round(strip.volume * 100)}%",
                      DIM, 11)
        self.region(QRectF(r.left(), track.top(), r.width(), track.height()),
                    lambda pos: Slide(track, lambda v: self.set_mix(strip, volume=v), pos))

    def draw_slot(self, p, r, strip, k):
        s, ref = self.s, strip.slots[k]
        selected = self.sel == (strip, k)
        kind = "INSTRUMENT" if self.instrument_slot(strip, k) else "EFFECT"
        if ref:
            missing = (strip, k) in self.failed
            self.box(p, r, "#1b2130" if selected else PANEL, "#38d6ff" if selected else BORDER, 2 if selected else 1.5)
            inner = r.adjusted(8 * s, 4 * s, -8 * s, -4 * s)
            self.text(p, inner.adjusted(0, 0, 0, -inner.height() * 0.4), ref.name, WARN if missing else TEXT, 14,
                      bold=True, elide=True)
            self.text(p, inner.adjusted(0, inner.height() * 0.55, 0, 0),
                      "missing" if missing else f"{ref.fmt.upper()} · {kind.lower()}", DIM, 12, elide=True)
            self.on_press(r, lambda: self.select(strip, k))
        else:
            self.box(p, r, "#11141b", "#2a3040", 1.5)
            self.text(p, r, "+ INSTRUMENT" if kind == "INSTRUMENT" else "+ EFFECT", "#5b6577", 13, bold=True)
            self.on_press(r, lambda: self.open_browser(strip, k))

    def select(self, strip, k):
        if self.sel != (strip, k):
            self.sel, self.page = (strip, k), 0

    def draw_mixer(self, p, r):
        """A column per track (header: tap = edit its chain), the master column on the right."""
        s, tracks = self.s, self.project.tracks
        cols, master = split(r, [0, 150 * s], 16 * s)
        cw = 96 * s
        per_page = max(1, int((cols.width() + 8 * s) // (cw + 8 * s)))
        pages = max(1, math.ceil(len(tracks) / per_page))
        self.mixer_page = min(self.mixer_page, pages - 1)
        if pages > 1:  # page arrows under the columns
            nav = QRectF(cols.left(), cols.bottom() - 36 * s, cols.width(), 36 * s)
            prev, label, nxt = split(nav, [70 * s, 0, 70 * s], 6 * s)
            self.button(p, prev, "◀", lambda: setattr(self, "mixer_page", max(0, self.mixer_page - 1)),
                        enabled=self.mixer_page > 0)
            self.text(p, label, f"{self.mixer_page + 1} / {pages}", DIM, 13)
            self.button(p, nxt, "▶", lambda: setattr(self, "mixer_page", min(pages - 1, self.mixer_page + 1)),
                        enabled=self.mixer_page < pages - 1)
            cols = cols.adjusted(0, 0, 0, -44 * s)
        shown = tracks[self.mixer_page * per_page:(self.mixer_page + 1) * per_page]
        for n, tr in enumerate(shown):
            col = QRectF(cols.left() + n * (cw + 8 * s), cols.top(), cw, cols.height())
            color = PALETTE[(tr.number - 1) % len(PALETTE)]
            self.box(p, col, PANEL, color if tr is self.selected() else BORDER, 2 if tr is self.selected() else 1.5,
                     radius=8)
            head = QRectF(col.left(), col.top(), col.width(), 58 * s)
            self.text(p, head.adjusted(0, 4 * s, 0, -head.height() * 0.45), f"T{tr.number}", color, 18, bold=True)
            inst = tr.strip.slots[0]
            self.text(p, head.adjusted(6 * s, head.height() * 0.5, -6 * s, 0), inst.name if inst else "no instrument",
                      TEXT if inst else "#5b6577", 12, elide=True)
            self.on_press(head, lambda tr=tr: self.open_track(tr))
            self.draw_fader(p, QRectF(col.left() + 8 * s, head.bottom() + 6 * s, col.width() - 16 * s,
                                      col.bottom() - head.bottom() - 14 * s), tr.strip)
        m = self.project.master
        self.box(p, master, "#1a1810", "#facc15", 2, radius=8)
        self.text(p, QRectF(master.left(), master.top(), master.width(), 34 * s), "MASTER", "#facc15", 17, bold=True)
        y = master.top() + 38 * s
        for k in range(SLOTS):
            slot = QRectF(master.left() + 8 * s, y, master.width() - 16 * s, 44 * s)
            ref = m.slots[k]
            self.box(p, slot, PANEL if ref else "#11141b", BORDER if ref else "#2a3040", 1.5, radius=6)
            self.text(p, slot.adjusted(6 * s, 0, -6 * s, 0), ref.name if ref else "+ EFFECT",
                      TEXT if ref else "#5b6577", 12, bold=True, elide=True)
            self.on_press(slot, lambda k=k, ref=ref: self.show("master", (m, k)) if ref else self.open_browser(m, k))
            y = slot.bottom() + 6 * s
        self.draw_fader(p, QRectF(master.left() + 8 * s, y + 6 * s, master.width() - 16 * s, master.bottom() - y - 14 * s),
                        m)

    def open_track(self, tr):
        self.select_track(tr)
        self.show("track")

    def draw_params(self, p, r, strip):
        s = self.s
        sel = self.sel if self.sel and self.sel[0] is strip else None
        plugin = self.loaded.get(sel) if sel else None
        self.box(p, r, "#11141b", "#2a3040", 1.5)
        if not plugin:
            msg = "tap a plugin to edit it" if sel is None else "plugin not loaded"
            self.text(p, r, msg, "#5b6577", 14)
            if sel:
                self.button(p, QRectF(r.center().x() - 60 * s, r.center().y() + 24 * s, 120 * s, 44 * s), "CHANGE",
                            lambda: self.open_browser(*sel))
            return
        inner = r.adjusted(10 * s, 8 * s, -10 * s, -8 * s)
        title, ui, change, remove = split(QRectF(inner.left(), inner.top(), inner.width(), 44 * s),
                                          [0, 56 * s, 84 * s, 84 * s], 6 * s)
        self.text(p, title, plugin.ref.name, TEXT, 15, Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
        self.button(p, ui, "UI", lambda: self.host.show_ui(plugin), enabled=self.host.has_ui(plugin))
        self.button(p, change, "CHANGE", lambda: self.open_browser(*sel))
        self.button(p, remove, "REMOVE", lambda: None, color=WARN, sub="hold")
        held = next((x for x in self.gestures.values() if isinstance(x, Hold)), None)
        if held:
            p.save()
            p.setClipRect(QRectF(remove.left(), remove.top(), remove.width() * held.progress(), remove.height()))
            self.box(p, remove, QColor(255, 93, 93, 140), None)
            p.restore()
        self.region(remove, lambda pos: Hold(lambda: self.set_slot(*sel, None)))

        per_page = self.KNOB_COLS * self.KNOB_ROWS
        pages = max(1, math.ceil(len(plugin.params) / per_page))
        self.page = min(self.page, pages - 1)
        nav = QRectF(inner.left(), inner.bottom() - 40 * s, inner.width(), 40 * s)
        prev, label, nxt = split(nav, [70 * s, 0, 70 * s], 6 * s)
        self.button(p, prev, "◀", lambda: setattr(self, "page", max(0, self.page - 1)), enabled=self.page > 0)
        self.text(p, label, f"{self.page + 1} / {pages}  ·  {len(plugin.params)} params", DIM, 12)
        self.button(p, nxt, "▶", lambda: setattr(self, "page", min(pages - 1, self.page + 1)),
                    enabled=self.page < pages - 1)
        grid = QRectF(inner.left(), title.bottom() + 8 * s, inner.width(), nav.top() - title.bottom() - 16 * s)
        kw, kh = grid.width() / self.KNOB_COLS, grid.height() / self.KNOB_ROWS
        for n, prm in enumerate(plugin.params[self.page * per_page:(self.page + 1) * per_page]):
            cell = QRectF(grid.left() + (n % self.KNOB_COLS) * kw, grid.top() + (n // self.KNOB_COLS) * kh, kw, kh)
            self.draw_knob(p, cell.adjusted(3 * s, 3 * s, -3 * s, -3 * s), plugin, prm)

    def draw_knob(self, p, r, plugin, prm):
        """Drag up/down to turn; tap toggles on/off parameters."""
        s, host = self.s, self.host
        f = (host.value(plugin, prm.index) - prm.lo) / (prm.hi - prm.lo)
        size = min(r.width(), r.height() - 22 * s) - 8 * s
        dial = QRectF(r.center().x() - size / 2, r.top() + 4 * s, size, size)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor("#262b36"), 5 * s, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(dial, 225 * 16, -270 * 16)
        color = QColor(param_color(prm.name))
        p.setPen(QPen(color, 5 * s, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(dial, 225 * 16, round(-270 * 16 * max(0.0, min(1.0, f))))
        self.text(p, dial.adjusted(6 * s, 0, -6 * s, 0), self.short(host.text(plugin, prm.index)), TEXT, 11,
                  elide=True)
        self.text(p, QRectF(r.left(), dial.bottom() + 2 * s, r.width(), 18 * s), prm.name, color.lighter(115).name(),
                  11, elide=True)

        def get():
            return (host.value(plugin, prm.index) - prm.lo) / (prm.hi - prm.lo)

        def set_(norm):
            v = prm.lo + norm * (prm.hi - prm.lo)
            host.set_value(plugin, prm.index, round(v) if prm.integer or prm.boolean else v)

        toggle = (lambda: set_(0.0 if get() > 0.5 else 1.0)) if prm.boolean else None
        strip = self.sel[0]
        hold = lambda: setattr(self, "assigning", (strip, self.sel[1], prm))  # hold: drive it from a fader
        self.region(r, lambda pos: Drag(pos, get, set_, 0.0, 1.0, 1.5 * s, 1 / 150, toggle, on_hold=hold))

    def draw_assign(self, p):
        """Which CTRL fader should drive the held knob's parameter."""
        s, g = self.s, 16 * self.s
        strip, slot, prm = self.assigning
        color = param_color(prm.name)
        self.text(p, QRectF(g, g, self.w - 2 * g, 40 * s), f"Fader for  {prm.name}", color, 22,
                  Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
        self.text(p, QRectF(g, g + 40 * s, self.w - 2 * g, 26 * s),
                  "CTRL-10 fader that will move this parameter directly (no MIDI):", DIM, 14,
                  Qt.AlignLeft | Qt.AlignVCenter)
        faders = [(i, c) for i, c in enumerate(self.project.controls) if c.kind == "cc"]
        grid = QRectF(g, g + 80 * s, self.w - 2 * g, self.h - 2 * g - 80 * s - 64 * s)
        cols = 4
        rows = math.ceil(len(faders) / cols)
        for n, (i, c) in enumerate(faders):
            row = QRectF(grid.left(), grid.top() + (n // cols) * grid.height() / rows, grid.width(), grid.height() / rows)
            r = split(row.adjusted(0, 4 * s, 0, -4 * s), [0] * cols, 10 * s)[n % cols]
            now = c.param[3] if c.target == "param" else f"CC {c.cc}"
            self.button(p, r, f"FADER {n + 1}", lambda i=i: self.link_to_fader(i), sub=f"now: {now}")
        cancel = QRectF(self.w - g - 160 * s, self.h - g - 52 * s, 160 * s, 52 * s)
        self.button(p, cancel, "CANCEL", lambda: setattr(self, "assigning", None))

    @staticmethod
    def short(text):
        """Plugins often print bare floats with 6 decimals; keep 3-4 significant digits."""
        try:
            v = float(text)
        except ValueError:
            return text
        return f"{v:.0f}" if abs(v) >= 100 else f"{v:.1f}" if abs(v) >= 10 else f"{v:.2f}"

    # --- plugin browser -----------------------------------------------------
    def open_browser(self, strip, k):
        self.browser, self.browser_scroll = (strip, k), 0.0

    def browser_items(self):
        instrument = self.instrument_slot(*self.browser)
        return [r for r in self.catalog.plugins
                if (r.instrument if instrument else r.effect) and self.browser_fmt in (None, r.fmt)]

    def choose(self, ref):
        strip, k = self.browser
        self.browser = None
        self.message = ""
        self.set_slot(strip, k, ref)

    def draw_browser(self, p):
        s, g = self.s, 8 * self.s
        strip, k = self.browser
        tr = self.track_of(strip)
        header = QRectF(g, g, self.w - 2 * g, 48 * s)
        title, rescan, *fmts, close = split(header, [0, 96 * s] + [70 * s] * 4 + [60 * s], 6 * s)
        kind = "instrument" if self.instrument_slot(strip, k) else "effect"
        self.text(p, title.adjusted(6 * s, 0, 0, 0), f"{f'T{tr.number}' if tr else 'MASTER'} · choose {kind}", TEXT, 16,
                  Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        cat = self.catalog
        self.button(p, rescan, "RESCAN", cat.rescan, pending=cat.scanning,
                    sub=cat.progress if cat.scanning else "new plugins")
        for rect, fmt in zip(fmts, (None, "clap", "lv2", "vst3")):
            self.button(p, rect, (fmt or "all").upper(), lambda fmt=fmt: self.set_browser_fmt(fmt),
                        on=self.browser_fmt == fmt)
        self.button(p, close, "✕", lambda: setattr(self, "browser", None))
        area = QRectF(g, header.bottom() + 8 * s, self.w - 2 * g, self.h - header.bottom() - 8 * s - g)
        if not cat.ready:
            self.text(p, area, f"scanning plugins… {cat.progress}", DIM, 15)
            return
        items = self.browser_items()
        listing, letters = split(area, [0, 46 * s], 8 * s)
        rh = 50 * s
        self.browser_scroll = max(0.0, min(self.browser_scroll, len(items) * rh - listing.height()))
        scroll = (lambda: self.browser_scroll), (lambda v: setattr(self, "browser_scroll", v))
        p.save()
        p.setClipRect(listing)
        first = int(self.browser_scroll // rh)
        for n in range(first, min(len(items), first + int(listing.height() / rh) + 2)):
            ref = items[n]
            row = QRectF(listing.left(), listing.top() + n * rh - self.browser_scroll, listing.width(), rh - 5 * s)
            self.box(p, row, PANEL, BORDER, radius=6)
            name, info, add = split(row.adjusted(10 * s, 4 * s, -4 * s, -4 * s), [0, row.width() * 0.32, 90 * s],
                                    10 * s)
            self.text(p, name, ref.name, TEXT, 15, Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
            self.text(p, info, f"{ref.maker}  {ref.fmt.upper()}", DIM, 13, Qt.AlignRight | Qt.AlignVCenter, elide=True)
            # the row only scrolls; ADD adds, also only on a tap, so a scroll started on it adds nothing
            self.region(row.intersected(listing), lambda pos: ScrollOrTap(pos, *scroll, lambda: None))
            self.button(p, add, "ADD", None, on=True)
            self.hits[-1] = (add.intersected(listing), lambda pos, ref=ref: ScrollOrTap(
                pos, *scroll, lambda: self.choose(ref)))
        p.restore()
        if not items:
            self.text(p, listing, "nothing here", "#5b6577", 15)
            return
        # A-Z strip: tap or slide along it to jump; the letter of the top of the list is lit
        self.box(p, letters, PANEL, BORDER, radius=8)
        current = self.letter(items[min(first, len(items) - 1)])
        lh = letters.height() / len(self.LETTERS)
        for n, ch in enumerate(self.LETTERS):
            r = QRectF(letters.left(), letters.top() + n * lh, letters.width(), lh)
            self.text(p, r, ch, "#38d6ff" if ch == current else TEXT, 14, bold=True)
        pick = lambda v: self.jump(items, self.LETTERS[min(len(self.LETTERS) - 1, int((1 - v) * len(self.LETTERS)))], rh)
        self.region(letters, lambda pos: Slide(letters, pick, pos))

    def set_browser_fmt(self, fmt):
        self.browser_fmt, self.browser_scroll = fmt, 0.0

    def letter(self, ref):
        c = ref.name[:1].upper()
        return c if c in self.LETTERS[1:] else "#"

    def jump(self, items, ch, rh):
        """Scroll the list to the first plugin at or after letter `ch` ('#' = digits and symbols)."""
        want = self.LETTERS.index(ch)
        n = next((n for n, ref in enumerate(items) if self.LETTERS.index(self.letter(ref)) >= want), len(items) - 1)
        self.browser_scroll = n * rh


# --- chords ------------------------------------------------------------------
# scales without 7 notes can't stack thirds; their chords come from the nearest 7-note scale
CHORD_SCALE = {"chromatic": "major", "major pentatonic": "major", "minor pentatonic": "minor", "blues": "minor"}
# chords from outside the key: (label, semitones above the key, scale they're stacked in)
BORROWED = [("IV", 5, "major"), ("iv", 5, "minor"), ("V", 7, "mixolydian"), ("bVI", 8, "major"),
            ("bVII", 10, "mixolydian")]
ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII"]


def stack(scale, degree, ext):
    """Chord on `degree` of `scale` in thirds: semitones above the scale's root (ascending).
    ext: '7', '9', 'sus2', 'sus4'."""
    def at(d):
        return scale[d % 7] + 12 * (d // 7)
    steps = [0, 1 if "sus2" in ext else 3 if "sus4" in ext else 2, 4]
    steps += [6] if "7" in ext or "9" in ext else []
    steps += [8] if "9" in ext else []
    return [at(degree + s) for s in steps]


FLAT_NAMES = "C Db D Eb E F Gb G Ab A Bb B".split()


def chord_name(tones, flats=False):
    """Name of a chord given in root position (first tone = root), e.g. 'Am7', 'G7', 'B°', 'Csus4'."""
    root, iv = tones[0], {(t - tones[0]) % 12 for t in tones[1:]}
    if 4 in iv:
        base = "+" if 8 in iv else ""
    elif 3 in iv:
        base = "°" if 6 in iv else "m"
    else:
        base = "sus4" if 5 in iv else "sus2" if 2 in iv else "5"
    if 10 in iv:
        base = "ø7" if base == "°" else base + ("9" if 2 in iv and base in ("", "m") else "7")
    elif 11 in iv:
        base = "m(maj7)" if base == "m" else base + ("maj9" if 2 in iv else "maj7")
    elif 9 in iv and base == "°":
        base = "°7"
    elif 2 in iv and base in ("", "m"):
        base += "add9"
    return (FLAT_NAMES if flats else NOTE_NAMES)[root % 12] + base


def roman(degree, tones):
    """I/ii/iii... by quality: upper case major, lower case minor, ° diminished."""
    iv = {(t - tones[0]) % 12 for t in tones[1:]}
    r = ROMAN[degree]
    return r.lower() + "°" if {3, 6} <= iv else r.lower() if 3 in iv else r


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


XYFX_DIVISIONS = ("1/2", "1/4", "1/8", "1/16")
XYFX_MODES = ("triplets", "reverse")  # what the pad's left half does (the plugin's Mode parameter)


def xyfx_division(x):
    """Which repeat XYFX plays at pad position x, as in the plugin (xyfx/XYFXCore.hpp): 8 columns, 1/2 next
    to the centre and 1/16 at the edges -> index into XYFX_DIVISIONS."""
    col = max(0, min(7, int(x * 8)))
    return 3 - col if col < 4 else col - 4


def xyfx_repeat(x, mode):
    """Label of the repeat at x: straight on the right half; triplets ('1/8T') or backwards ('◀1/8') on the left."""
    name = XYFX_DIVISIONS[xyfx_division(x)]
    if x >= 0.5:
        return name
    return name + "T" if XYFX_MODES[mode] == "triplets" else "◀" + name


def xyfx_filter(y):
    """The plugin's filter at pad height y: ('LPF', Hz) below the centre line, ('HPF', Hz) above it."""
    amount = min(1.0, abs(y - 0.5) * 2)
    return ("HPF", 20 * 300 ** amount) if y > 0.5 else ("LPF", 20000 / 250 ** amount)


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
        rec = (self.seq.sel, self.mode == "rec") if self.mode else None
        if rec != self.engine.rec:  # a new recording starts: earlier takes may be replaced again
            self.engine.rec_taken.clear()
            self.engine.rec = rec

    def record_start(self, tr, lanes, vel):
        """Write notes at the quantized position; returns what record_end needs, or None (not recording)."""
        e = self.engine
        pos = e.position(tr) if self.mode and e.rec and e.rec[0] is tr else None
        if pos is None:
            return None
        pat = tr.current
        at = round(pos / self.quant) * self.quant
        i = int(at) % pat.length
        ahead = at > int(pos)  # the playhead hasn't played that step yet in this pass
        for k in lanes:
            st = pat.lane(k)[i]
            st.on, st.vel, st.gate = True, vel, GATE_RES
            e.rec_taken.add((k, i))
            if ahead:
                e.rec_fresh.add((k, i))
        return pat, i, pos, lanes

    def record_end(self, tr, take):
        pat, i, start, lanes = take
        pos = self.engine.position(tr)
        held = (pos - start) % pat.length if pos is not None else 1.0
        gate = max(GATE_RES, min(MAX_GATE, round(held / GATE_RES) * GATE_RES))
        for k in lanes:
            lane = pat.lane(k)
            lane[i].gate = gate
            if not tr.drum:
                self.seq.fit(lane, i)

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
        draw = self.draw_drum_pads if tr.drum else self.draw_keys if self.layout == "keys" else self.draw_chord_pads
        draw(p, body, tr)

    def pad(self, p, r, key, color, title, sub):
        lit = self.held.get(key, 0) > 0
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
            self.pad(p, rect, n, color, label, name)

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
                lit = self.held.get(("key", note), 0) > 0
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
            self.pad(p, rect, ("drum", v), DRUM_COLORS[v], name, str(note))
            self.region(rect, lambda pos, v=v, note=note, rect=rect: self.press_pad(("drum", v), tr, [(v, note)],
                                                                                  rect, pos))


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
        self.dirty = False  # something may have been edited since the last save
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
        if e.source() == Qt.MouseEventNotSynthesized:
            self.press(MOUSE_ID, e.position())

    def mouseMoveEvent(self, e):
        if e.source() == Qt.MouseEventNotSynthesized:
            self.move(MOUSE_ID, e.position())

    def mouseReleaseEvent(self, e):
        if e.source() == Qt.MouseEventNotSynthesized:
            self.release(MOUSE_ID, e.position())

    def keyPressEvent(self, e):
        ctrl_c = e.key() == Qt.Key_C and e.modifiers() & Qt.ControlModifier
        if e.key() in (Qt.Key_Escape, Qt.Key_Q) or ctrl_c:
            self.close()

    def tick(self):
        for panel in self.panels + ([self.dialog[2]] if self.dialog else []):
            panel.tick()
        self.update()

    # --- drawing --------------------------------------------------------
    def paintEvent(self, _):
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

    @staticmethod
    def paint_panel(p, panel, screen):
        p.save()
        p.translate(screen.topLeft())
        p.setClipRect(QRectF(0, 0, screen.width(), screen.height()))
        panel.paint(p, screen.width(), screen.height())
        p.restore()

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

    def rail_button(self, p, r, label, action, size=24, on=False, frame=True):
        """Button on metal; without a frame only thin bars at the sides mark it (for the module rails)."""
        k = self.k
        if frame:
            p.setPen(QPen(QColor("#38d6ff" if on else "#5a606b"), 1.5 if on else 1))
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


class Rack(Window):
    """The main window: toolbar (FILE, SETTINGS), performance controls, sequencer and plugin host."""
    TOOLBAR = 54

    def __init__(self, project, engine, path, host):
        self.project, self.engine, self.path, self.host = project, engine, path, host
        self.seq = Sequencer(project, engine)
        self.controls = Controls(project, engine, host)
        self.controls.selected = lambda: self.seq.sel
        host.selected = lambda: self.seq.sel
        self.seq.master_fx = lambda: host.xyfx() is not None
        host.select_track = lambda tr: setattr(self.seq, "sel", tr)
        super().__init__([(0, 0, "CTRL-10", "performance controls", self.controls),
                          (0, 1, "SEQ-64", "step sequencer", self.seq),
                          (1, 0, "HOST-4", "plugin host", host),
                          (1, 1, "PLAY-12", "live chords · pads", Performer(project, engine, self.seq, host))])
        self.saved = dump_project(project)
        self.saved_at = time.localtime()
        self.restart = False  # close and start again: on the start screen, or on `next_project`
        self.next_project = None
        QTimer(self, interval=2000, timeout=self.autosave).start()
        # plugin states change inside the plugins (their own UIs too), so they're saved on a clock
        QTimer(self, interval=30000, timeout=host.save_states).start()

    def autosave(self):
        # Runs in the UI thread, which is the only one changing the project structure; the engine
        # only writes Track.pattern, so no lock is needed (and holding it would stall playback).
        if not self.dirty:
            return
        text = dump_project(self.project)
        if text != self.saved:
            save_text(self.path, text)
            self.saved, self.saved_at = text, time.localtime()
        e = self.engine
        self.dirty = bool(e.pending) or e.pending_scene is not None  # queued switches change it later

    def closeEvent(self, e):
        self.engine.close()
        self.host.close()
        self.dirty = True
        self.autosave()
        super().closeEvent(e)

    def switch(self, path=None):
        """Save everything and start again with project `path` (None: the start screen)."""
        self.restart, self.next_project = True, path
        self.close()

    def duplicate(self):
        self.dirty = True
        self.autosave()
        self.host.save_states()
        copy_path = free_project_path(self.path.parent, self.path.stem)
        shutil.copyfile(self.path, copy_path)
        if self.path.with_suffix(".plugins").is_dir():
            shutil.copytree(self.path.with_suffix(".plugins"), copy_path.with_suffix(".plugins"))
        self.switch(copy_path)

    def draw_toolbar(self, p, r):
        k = self.k
        inner = r.adjusted(14 * k, 7 * k, -14 * k, -7 * k)
        logo, file_, settings, name, info = split(inner, [150 * k, 110 * k, 140 * k, 0, 520 * k], 14 * k)
        f = QFont("sans-serif")
        f.setPixelSize(round(19 * k))
        f.setBold(True)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 2 * k)
        p.setFont(f)
        p.setPen(QColor("#e6e9ee"))
        p.drawText(logo, Qt.AlignLeft | Qt.AlignVCenter, "TOUCH-SEQ")
        self.rail_button(p, file_, "FILE", self.open_file, size=17, on=bool(self.dialog) and self.dialog[0] == "FILE")
        self.rail_button(p, settings, "SETTINGS", self.open_settings, size=17,
                         on=bool(self.dialog) and self.dialog[0] == "SETTINGS")
        f.setPixelSize(round(22 * k))
        p.setFont(f)
        p.drawText(name.adjusted(20 * k, 0, 0, 0), Qt.AlignLeft | Qt.AlignVCenter, self.path.stem)
        f.setPixelSize(round(16 * k))
        f.setBold(False)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1 * k)
        p.setFont(f)
        p.setPen(QColor("#9aa3b0"))
        p.drawText(info, Qt.AlignRight | Qt.AlignVCenter,
                   f"{self.project.key_name.upper()}   ·   SAVED {time.strftime('%H:%M:%S', self.saved_at)}")

    def open_file(self):
        if self.dialog and self.dialog[0] == "FILE":
            return self.close_dialog()
        here = self.path
        self.open_dialog("FILE", "projects", Chooser(
            self.path.parent, lambda path: self.close_dialog() if path == here else self.switch(path),
            current=here, duplicate=self.duplicate))

    def open_settings(self):
        if self.dialog and self.dialog[0] == "SETTINGS":
            return self.close_dialog()
        self.open_dialog("SETTINGS", "project & audio", Settings(self.project, self.host))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.dialog:
            self.close_dialog()
        elif e.key() == Qt.Key_Escape and self.seq.popup:
            self.seq.popup = None
        elif e.key() == Qt.Key_Space:
            self.seq.toggle_play()
        else:
            super().keyPressEvent(e)


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


CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "touch-seq"
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "touch-seq"
DEFAULT_CONFIG = {
    "screen": "",  # output name (e.g. HDMI-1) to go full screen on; "" = the primary screen
    "midi_port": "Touch-Seq",  # MIDI out: an existing port whose name contains this, else a virtual one
    "projects": str(DATA_HOME / "projects"),  # relative paths are relative to the config file
    "audio_out": "default",  # plugin audio: "default" (follow PipeWire's default) or a sink's node name
}


def config_path():
    """The config: the command line's, else config.json beside the program (a source checkout), else
    ~/.config/touch-seq/config.json, written with the defaults the first time."""
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    beside = Path(__file__).with_name("config.json")
    if beside.exists():
        return beside
    path = CONFIG_HOME / "config.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        save_text(path, json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    return path


def main():
    if sys.argv[1:2] == ["--version"]:
        print(f"Touch-Seq {__version__}")
        return
    cfg_path = config_path()
    cfg = {**DEFAULT_CONFIG, **json.loads(cfg_path.read_text())}
    folder = cfg_path.parent / Path(cfg["projects"]).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    legacy = cfg_path.parent / "project.json"  # the single project from before there was a folder
    if legacy.exists() and not (folder / legacy.name).exists():
        for old in (legacy, legacy.with_suffix(".plugins")):
            if old.exists():
                old.rename(folder / old.name)
    sys.setswitchinterval(0.001)  # let the engine thread grab the GIL quickly while the UI is busy
    app = QApplication(sys.argv[:1])
    screens = {s.name(): s for s in app.screens()}
    screen = screens.get(cfg["screen"])
    if screen is None:
        if cfg["screen"]:
            print(f"screen '{cfg['screen']}' not found (have: {', '.join(screens)}), using primary")
        screen = app.primaryScreen()

    def show(w):
        # Ctrl+C in the terminal: the UI timers keep Python running, so the handler gets a chance to fire
        signal.signal(signal.SIGINT, lambda *_: w.close())
        w.setScreen(screen)
        w.setGeometry(screen.geometry())
        w.setCursor(Qt.BlankCursor)
        w.showFullScreen()

    racks = []

    def remember_output(name):  # the audio output belongs to this machine, not to a project
        cfg["audio_out"] = name
        save_text(cfg_path, json.dumps(cfg, indent=2) + "\n")

    def open_project(path):
        project = load_project(path)
        router = MidiRouter(open_midi(cfg["midi_port"]))
        engine = Engine(project, router)
        host = HostPanel(project, router, path.with_suffix(".plugins"),
                         Path.home() / ".cache" / "touch-seq" / "plugins.json", cfg.get("audio_out", "default"),
                         remember_output)
        racks.append(Rack(project, engine, path, host))
        show(racks[-1])

    if len(sys.argv) > 2:
        open_project(Path(sys.argv[2]))
    else:
        start = Chooser(folder, lambda path: (open_project(path), chooser.close()))  # rack first, so the app stays up
        chooser = Window([(0, 0, "TOUCH-SEQ", "projects", start)])
        show(chooser)
    code = app.exec()
    if racks and racks[-1].restart:  # project switch: a fresh process frees Carla and the MIDI ports cleanly
        nxt = racks[-1].next_project
        sys.stdout.flush()  # exec drops unflushed output
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), str(cfg_path)] + ([str(nxt)] if nxt else []))
    sys.exit(code)


if __name__ == "__main__":
    main()
