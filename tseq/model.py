"""The persisted project: steps, patterns, tracks, controls, plugin strips, and reading/writing the
project files. No Qt, no MIDI: the engine and the panels read this."""
import itertools
import json
import os
import re
from dataclasses import asdict, astuple, dataclass, field

from plugins import PluginRef

from .constants import DRUM_KIT, MAX_STEPS, NOTE_NAMES, PATTERNS, SCENES, SLOTS
from .music import SCALES


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


@dataclass(eq=False)
class SongStep:
    """One step of a song: how long it lasts, and which pattern each track plays in it. A track left out keeps
    the pattern it had, so a step can change just one part of the music."""
    bars: int = 4  # 1..16 bars
    repeats: int = 1  # 1..8: the whole step again
    patterns: dict = field(default_factory=dict)  # track number -> pattern index

    @property
    def span(self):
        """How many bars the step lasts, repeats and all."""
        return max(1, self.bars) * max(1, self.repeats)


@dataclass(eq=False)  # identity hash: tracks are dict keys in the engine
class Track:
    kind: str = "melody"  # "melody", "drum", "external" (MIDI to hardware) or "audio" (an input port)
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
    port: str = ""  # external: the MIDI output it sends to (a name from the system's outputs)
    bus: int = 0  # 0: straight into the master, else the number of the bus it goes through
    source: str = ""  # audio: the input port it takes its sound from (a mono capture port)
    pan: str = "center"  # audio: "left", "center" or "right" - a mono input into one or both sides
    in_port: str = ""  # MIDI: the input it plays and records from (a name from the system's inputs)
    in_channel: int = 0  # MIDI: the channel that input is taken on, 0 = any

    def __post_init__(self):
        if not self.patterns:
            self.patterns = [self.new_pattern() for _ in range(PATTERNS)]

    @property
    def drum(self):
        return self.kind == "drum"

    @property
    def external(self):
        return self.kind == "external"

    @property
    def audio(self):
        return self.kind == "audio"

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


@dataclass(eq=False)  # identity hash: strips are dict keys in the plugin host
class Bus:
    """An effects bus: the tracks routed into it are summed, its chain processes them, and the result goes
    into the master like a track. It is how one reverb serves only the tracks you choose."""
    number: int = 1  # stable name ("BUS 2"), kept when other buses are deleted
    strip: Strip = field(default_factory=Strip)


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


@dataclass
class Project:
    bpm: int = 120
    tracks: list = field(default_factory=list)
    controls: list = field(default_factory=default_controls)
    master: Strip = field(default_factory=Strip)  # effects every track goes through to the output
    key: int = 0  # 0 = C
    scale: str = "chromatic"
    buses: list = field(default_factory=list)  # effects buses between the tracks and the master
    click: bool = False  # the metronome: a tick on every beat while playing
    count_in: int = 0  # bars of clicks before the pattern starts (0-2)
    song: list = field(default_factory=list)  # SongStep list: the arrangement above the patterns
    song_on: bool = False  # the transport follows the song instead of the patterns playing
    song_loop: bool = True  # back to the first step at the end, or stay on the last

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
         **({"port": t.port} if t.external else {}), **({"bus": t.bus} if t.bus else {}),
         **({"source": t.source, "pan": t.pan} if t.audio else {}),
         **({"in_port": t.in_port, "in_channel": t.in_channel} if t.in_port else {}),
         "patterns": [pattern(p) for p in t.patterns]}
        for t in pr.tracks], "controls": [astuple(c) for c in pr.controls],
        "master": dump_strip(pr.master),
        "buses": [{"number": b.number, "strip": dump_strip(b.strip)} for b in pr.buses],
        **({"click": True} if pr.click else {}), **({"count_in": pr.count_in} if pr.count_in else {}),
        **({"song": [{"bars": s.bars, "repeats": s.repeats, "patterns": s.patterns} for s in pr.song]}
           if pr.song else {}),
        **({"song_on": True} if pr.song_on else {}), **({"song_loop": False} if not pr.song_loop else {})},
        separators=(",", ":"))


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
                   voice=t["voice"], last=Step(*last), last_note=last_note, port=t.get("port", ""),
                   bus=t.get("bus", 0), source=t.get("source", ""), pan=t.get("pan", "center"),
                   in_port=t.get("in_port", ""), in_channel=t.get("in_channel", 0),
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
    buses = [Bus(b.get("number", n), load_strip(b["strip"])) for n, b in enumerate(d.get("buses", []), 1)]
    return Project(d["bpm"], tracks, controls, master, d.get("key", 0), d.get("scale", "chromatic"), buses,
                   d.get("click", False), d.get("count_in", 0),
                   [SongStep(bars=int(st.get("bars", 4)), repeats=int(st.get("repeats", 1)),
                             patterns={int(k): int(v) for k, v in (st.get("patterns") or {}).items()})
                    for st in d.get("song", [])],
                   d.get("song_on", False), d.get("song_loop", True))


def save_text(path, text):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
