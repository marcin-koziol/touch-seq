"""The metronome's click: synthesized here and streamed into PipeWire by `pw-cat` (the app has no audio engine
of its own — audio is Carla's job), so it needs nothing but a player the machine already has.

A click is a short sine tick, higher on the first beat of the bar. The writer thread fills blocks a few
milliseconds ahead of the audio, which is what puts each tick on the right sample: the engine only says *when*
(`at(when)` with a perf_counter time)."""
import array
import math
import shutil
import subprocess
import threading
import time

RATE = 48000
BLOCK = 256  # samples (~5 ms): small enough that a click is placed where it belongs
LENGTH = int(RATE * 0.05)  # a tick's tail, in samples
DECAY = 0.006  # s: the tick dies away fast, so beats don't smear into each other
PLAYERS = (
    ("pw-cat", ["--playback", "--raw", "--format=s16", "--rate=%d" % RATE, "--channels=1",
                "--latency=20ms", "-P", "node.name=touch-seq-click", "-"]),
    ("pacat", ["--playback", "--raw", "--format=s16le", "--rate=%d" % RATE, "--channels=1",
               "--latency-msec=20", "--client-name=touch-seq-click"]),
    ("aplay", ["-q", "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", "1"]),
)


def player():
    """The command that plays raw mono samples to the machine's output, or None when there is none."""
    for name, args in PLAYERS:
        if shutil.which(name):
            return [name] + args
    return None


class Clicker:
    """A stream of silence with a tick where the beat is."""

    def __init__(self, rate=RATE, opener=player):
        self.rate, self.opener = rate, opener
        self.lock = threading.Lock()
        self.events = []  # (when, accent) not written yet
        self.proc = None
        self.thread = None
        self.running = False
        self.error = ""

    # --- the UI/engine side ---------------------------------------------
    def prepare(self):
        """Start streaming (silence until a click is queued). False when there is no player to stream with."""
        with self.lock:
            if self.running:
                return True
            cmd = self.opener()
            if cmd is None:
                self.error = "no audio player (pw-cat, pacat or aplay)"
                return False
            try:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError as e:
                self.error = str(e)
                return False
            self.running, self.events = True, []
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            return True

    def at(self, when, accent=False):
        """Queue a tick at a perf_counter time (in the past is fine: it is dropped when it is too late)."""
        with self.lock:
            if self.running:
                self.events.append((when, accent))

    @property
    def open(self):
        return self.running

    def close(self):
        with self.lock:
            self.running = False
            proc, self.proc, self.thread = self.proc, None, None
            self.events = []
        if proc is None:
            return
        try:
            proc.stdin.close()  # the player plays out what it has and leaves
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            proc.kill()

    # --- the writing thread ---------------------------------------------
    def run(self):
        proc, rate, block = self.proc, self.rate, BLOCK
        t0 = time.perf_counter()  # the stream's own sample clock
        done = 0  # samples written
        voices = []  # (first sample, frequency, amplitude) still ringing
        while self.running:
            with self.lock:
                due = [e for e in self.events if (e[0] - t0) * rate < done + block]
                self.events = [e for e in self.events if (e[0] - t0) * rate >= done + block]
            for when, accent in due:
                first = int((when - t0) * rate)
                if first < done - rate * 0.02:  # more than 20 ms late: better to skip than to stumble
                    continue
                voices.append((max(done, first), 1660.0 if accent else 1180.0, 0.55 if accent else 0.30))
            samples = array.array("h")
            for x in range(done, done + block):
                v = 0.0
                for first, freq, amp in voices:
                    i = x - first
                    if 0 <= i < LENGTH:
                        v += amp * math.exp(-i / (rate * DECAY)) * math.sin(6.283185307 * freq * i / rate)
                samples.append(int(max(-1.0, min(1.0, v)) * 32767))
            voices = [v for v in voices if done + block - v[0] < LENGTH]
            try:
                proc.stdin.write(samples.tobytes())
                proc.stdin.flush()
            except (OSError, ValueError):  # the player went away
                return
            done += block
            time.sleep(max(0.0, t0 + done / rate - time.perf_counter()))
