"""Sizes, colours and fixed tables shared by the whole app: layout units, the drum kit, palettes, the
plugin slot layout and the parameter-name colour families."""
import os


MAX_STEPS = 96


PATTERNS = 16


SCENES = 8


PPQN = 24


TICKS_PER_STEP = PPQN // 4  # the default grid: steps are 16th notes


STEPS_PER_BAR = 16


# The project's grid: (MIDI ticks per step, steps per bar). The default is 1/16; the triplets and 1/32
# let a drum pattern play rolls that the 16-step grid cannot hold.
GRIDS = {
    "1/16": (6, 16),
    "1/8T": (8, 12),
    "1/16T": (4, 24),
    "1/32": (3, 32),
}
GRID_ORDER = ("1/16", "1/8T", "1/16T", "1/32")
# recording quantization per grid: (label, steps) - the finest is one step of that grid
GRID_QUANTS = {
    "1/16": (("1/16", 1), ("1/8", 2), ("1/4", 4)),
    "1/8T": (("1/8T", 1), ("1/4", 2), ("1/2", 4)),
    "1/16T": (("1/16T", 1), ("1/8T", 2), ("1/4T", 4)),
    "1/32": (("1/32", 1), ("1/16", 2), ("1/8", 4)),
}


BPM_MIN, BPM_MAX = 30, 300


MAX_GATE, GATE_RES = float(MAX_STEPS), 0.125  # gate in steps: up to the longest pattern, in 1/8-step steps


MOUSE_ID = -1  # pseudo touch id for desktop testing with a mouse


TOUCH_LOG = os.environ.get("TOUCHSEQ_TOUCH_LOG")  # file to log raw touch/mouse events to, for debugging
PARAM_LOG = os.environ.get("TOUCHSEQ_PARAM_LOG")  # file to log every parameter write, with its source


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
# our eight band EQ, whose editor the host panel draws (eq/ in this repo)
EQ_LABELS = {"touchseq.eq", "urn:touch-seq:eq"}
CLICK_LABELS = {"touchseq.click", "urn:touch-seq:click"}  # CLAP id, LV2 URI (click/ in this repo)
CLICK_BEAT, CLICK_ACCENT = 77, 76  # the metronome plugin's notes: a beat, and the first beat of the bar
CC_LANES = 4  # a pattern takes this many controller lanes at most
# the arpeggiator: how often it plays (in the engine's ticks, 6 to a 1/16) and in which order
ARP_RATES = ((3, "1/32"), (4, "1/16T"), (6, "1/16"), (8, "1/8T"), (12, "1/8"), (16, "1/4T"), (24, "1/4"))
ARP_MODES = (("up", "UP"), ("down", "DOWN"), ("updown", "UP-DOWN"), ("random", "RANDOM"),
             ("played", "AS PLAYED"))
ARP_GATES = (0.25, 0.5, 0.75, 1.0)
# a delay into a reverb, the two effects a live set always needs (space/ in this repo)
SPACE_LABELS = {"touchseq.space", "urn:touch-seq:space"}
# the 303-style acid bass, a monophonic instrument (acid/ in this repo)
ACID_LABELS = {"touchseq.acid", "urn:touch-seq:acid"}
# the 16-pad MPC-style sampler (sampler/ in this repo)
SAMPLER_LABELS = {"touchseq.sampler", "urn:touch-seq:sampler"}
SAMPLER_PADS = 16
SAMPLER_GROUPS = 8  # playback groups: pads in the same group cut each other (0 = no group)
SAMPLER_NOTE_BASE = 36  # pad 1 is MIDI note 36, as on an MPC
# the plugins that ship with the app, whatever their format
BUILTIN_LABELS = (XYFX_LABELS | MONITOR_LABELS | EQ_LABELS | CLICK_LABELS | SPACE_LABELS | ACID_LABELS
                  | SAMPLER_LABELS)
# the ones the app puts in place itself - the monitor in a bare audio track, the click between master and
# output: not for the browser, there is nothing to add by hand. XYFX is not one of them: it is an effect like
# any other, and on the master it is what PLAY-12's XY pad drives
INTERNAL_LABELS = MONITOR_LABELS | CLICK_LABELS
