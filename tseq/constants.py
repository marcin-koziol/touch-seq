"""Sizes, colours and fixed tables shared by the whole app: layout units, the drum kit, palettes, the
plugin slot layout and the parameter-name colour families."""
import os


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


SLOTS = 3  # per plugin chain: tracks have an instrument and 2 effects, the master 3 effects


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


XYFX_LABELS = {"touchseq.xyfx", "urn:touch-seq:xyfx"}  # CLAP id, LV2 URI (xyfx/ in this repo)
# a transparent gain/pan node the app puts in an audio track with no effects (monitor/ in this repo)
MONITOR_LABELS = {"touchseq.monitor", "urn:touch-seq:monitor"}
