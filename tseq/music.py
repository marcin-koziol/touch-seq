"""Scales, chords and the XYFX repeat/filter mappings: the app's musical vocabulary (no Qt, no I/O)."""
from .constants import NOTE_NAMES


def note_name(n):
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


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
