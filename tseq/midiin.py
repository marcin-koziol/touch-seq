"""The MIDI keyboards and controllers playing into the app: the system's input ports, opened on demand for
the tracks that listen to them.

Messages arrive on rtmidi's own thread, so the callback only queues them; the UI clock drains the queue and
routes them (performing.py), which keeps every project change on the UI thread. A device that is not there
yet (unplugged, or switched on later) is looked for again every RETRY seconds, like the external outputs."""
import queue
import re
import threading
import time

import rtmidi


def clean(name):
    """A port name without the client:port number ALSA puts at its end ("KeyStep Pro MIDI 1 28:0"): a device
    gets a new one every time it is plugged in, and a track should still find it then."""
    return re.sub(r"\s+\d+:\d+$", "", name).strip()


def midi_inputs():
    """Names of the system's MIDI inputs: what a track can listen to (empty when this machine has no MIDI API
    up, e.g. no ALSA sequencer)."""
    try:
        probe = rtmidi.MidiIn()
    except (rtmidi.RtMidiError, OSError):
        return []
    try:
        names = []
        for name in map(clean, probe.get_ports()):
            if name not in names:  # the same device can show twice (a raw and a jack port, say)
                names.append(name)
        return names
    finally:
        del probe


def open_input(name):
    """Opens the input whose name is `name` (as listed; a part of it is enough), or None when it cannot be
    opened. Every channel is taken: the channel is chosen per track, so one keyboard can play several tracks
    at once."""
    try:
        port = rtmidi.MidiIn()
    except (rtmidi.RtMidiError, OSError):
        return None
    try:
        ports = list(port.get_ports())
        matches = [i for i, p in enumerate(ports) if clean(p).lower() == name.lower()] or \
                  [i for i, p in enumerate(ports) if name.lower() in clean(p).lower()]
        if not matches:
            del port
            return None
        port.open_port(matches[0])
        port.ignore_types(sysex=True, timing=False, active_sense=True)  # the clock tells us the tempo
    except (rtmidi.RtMidiError, ValueError):
        del port
        return None
    return port


class Inputs:
    """The MIDI inputs the tracks ask for. `use` opens what is needed and lets go of what is not, `poll`
    drains what came in since the last look."""

    RETRY = 2.0  # s between looks for a device that is not there yet

    def __init__(self, opener=open_input, lister=midi_inputs):
        self.opener, self.lister = opener, lister
        self.lock = threading.RLock()
        self.queue = queue.Queue()  # (port name, message): filled by rtmidi's threads
        self.ports = {}  # port name -> opened MidiIn, or None while the device is away
        self.looked = 0.0  # when the ports were last scanned

    def use(self, names):
        """Open the ports the tracks ask for, and close the ones no track uses any more. A port that is not
        there is looked for again every RETRY seconds, so plugging a keyboard in later just works."""
        wanted = [n for n in names if n]
        with self.lock:
            for name in list(self.ports):
                if not self.wanted(name, wanted):  # no track listens to it any more
                    self.drop(name)
            opened = [name for name, port in self.ports.items() if port]
            if all(any(w.lower() in name.lower() for name in opened) for w in wanted):
                return  # every input a track asks for is open
            now = time.monotonic()
            if now - self.looked < self.RETRY:  # one scan serves every device: don't feel for ports per tick
                return
            self.looked = now
            for name in [a for a in self.lister() if self.wanted(a, wanted)]:
                if name in self.ports:
                    continue
                port = self.opener(name)
                if port is None:  # opened a moment ago by another app, say: look again later
                    continue
                try:
                    # stamped on rtmidi's thread: a clock's own tempo needs it to the millisecond
                    port.set_callback(lambda msg, data=None, name=name:
                                      self.queue.put((name, msg[0], time.perf_counter())))
                except (rtmidi.RtMidiError, RuntimeError):  # went away between the scan and the callback
                    port.close_port()
                    continue
                self.ports[name] = port

    @staticmethod
    def wanted(name, wanted):
        """A track's port name is part of the device's: "keys" finds "Arturia KeyStep 32 MIDI 1"."""
        return any(w.lower() in name.lower() for w in wanted)

    def drop(self, name):
        with self.lock:
            port = self.ports.pop(name, None)
            if port is not None:
                try:
                    port.cancel_callback()
                    port.close_port()
                except (rtmidi.RtMidiError, RuntimeError):
                    pass

    def close(self):
        with self.lock:
            for name in list(self.ports):
                self.drop(name)

    def open_names(self):
        """The inputs that are actually open right now (a track's device may be away)."""
        with self.lock:
            return [name for name, port in self.ports.items() if port]

    def poll(self):
        """[(port name, message, when it arrived)] since the last look, oldest first (an empty message list
        is dropped: rtmidi sends one for a port that just went away)."""
        out = []
        while True:
            try:
                name, msg, when = self.queue.get_nowait()
            except queue.Empty:
                return out
            if msg:
                out.append((name, msg, when))
