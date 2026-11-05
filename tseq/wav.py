"""A small PCM WAV reader for the sampler editor: mono samples as floats, plus the rate and frame count. The
plugin loads the same file itself; this is only for drawing the waveform and finding chop points. Samples are
an array('f'), not a list, so a three-minute recording does not cost hundreds of megabytes."""
import array
import sys
import wave


def read_wav(path):
    """(samples, rate, frames): samples is a mono array('f') in -1..1."""
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        frames = w.getnframes()
        raw = w.readframes(frames)

    if width == 1:
        values = array.array("f", ((b - 128) / 128.0 for b in raw))
    elif width == 2:
        ints = array.array("h")
        ints.frombytes(raw)
        if sys.byteorder == "big":
            ints.byteswap()
        values = array.array("f", (v / 32768.0 for v in ints))
    elif width == 3:
        values = array.array("f")
        for i in range(0, len(raw), 3):
            v = raw[i] | (raw[i + 1] << 8) | (raw[i + 2] << 16)
            if v & 0x800000:
                v -= 0x1000000
            values.append(v / 8388608.0)
    elif width == 4:
        ints = array.array("i")
        ints.frombytes(raw)
        if sys.byteorder == "big":
            ints.byteswap()
        values = array.array("f", (v / 2147483648.0 for v in ints))
    else:
        raise ValueError(f"unsupported WAV sample width: {width * 8} bit")

    if channels == 2:
        left, right = values[0::2], values[1::2]
        values = array.array("f", ((a + b) * 0.5 for a, b in zip(left, right)))
    elif channels > 2:
        mono = array.array("f", [0.0]) * frames
        for i in range(frames):
            base = i * channels
            total = 0.0
            for c in range(channels):
                total += values[base + c]
            mono[i] = total / channels
        values = mono
    return values, rate, frames


def peaks_range(samples, start, end, columns, max_scan=200000):
    """(low, high) per column for a visible frame range. A full-file overview strides the samples, so a long
    recording never costs a full scan on every repaint."""
    start = max(0, int(start))
    end = min(len(samples), int(end))
    columns = max(1, int(columns))
    if end <= start:
        return []
    step = max(1, (end - start) // max_scan)
    span = (end - start) / columns
    out = []
    for c in range(columns):
        a = start + int(c * span)
        b = start + int((c + 1) * span)
        if b <= a:
            b = a + 1
        low, high = 1.0, -1.0
        for i in range(a, min(b, end), step):
            v = samples[i]
            if v < low:
                low = v
            if v > high:
                high = v
        out.append((low, high) if high >= low else (0.0, 0.0))
    return out


def peaks(samples, columns):
    """(low, high) per column for the whole file."""
    return peaks_range(samples, 0, len(samples), columns)


def table_peaks(table, columns):
    """Aggregate a precomputed peak table to fewer drawing columns."""
    if not table:
        return []
    if len(table) <= columns:
        return table
    step = len(table) / columns
    out = []
    for c in range(columns):
        a = int(c * step)
        b = max(a + 1, int((c + 1) * step))
        chunk = table[a:b]
        out.append((min(low for low, _ in chunk), max(high for _, high in chunk)))
    return out
