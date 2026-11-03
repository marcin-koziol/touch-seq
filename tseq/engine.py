"""The MIDI side: the output port and the clock/playback thread that turns patterns into MIDI. Everything
that touches playback state or sends MIDI holds `Engine.lock`."""
import threading
import time

import rtmidi

from .click import Clicker
from .constants import PPQN, STEPS_PER_BAR, TICKS_PER_STEP
from .model import Track


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


def midi_outputs():
    """Names of the system's MIDI outputs: what an external track can send to (empty when this machine has no
    MIDI API up, e.g. no ALSA sequencer)."""
    try:
        probe = rtmidi.MidiOut()
    except (rtmidi.RtMidiError, OSError):
        return []
    try:
        return list(probe.get_ports())
    finally:
        del probe


def open_output(name):
    """Opens the output whose name contains `name` (an external track's device), or None when it is not there
    (unplugged, or not plugged in yet)."""
    try:
        out = rtmidi.MidiOut()
    except (rtmidi.RtMidiError, OSError):
        return None
    matches = [i for i, port in enumerate(out.get_ports()) if name.lower() in port.lower()]
    if not matches:
        del out
        return None
    try:
        out.open_port(matches[0])
    except rtmidi.RtMidiError:
        del out
        return None
    return out


class MidiRouter:
    """The engine's MIDI output: everything goes to the main port; channel messages also go to the plugin
    host: a track's notes to that track's instrument, messages without a track (CC, pitch bend from the
    faders) to the instruments of the tracks on that channel.

    An external track is different: its channel messages go out to the hardware on the track's own MIDI
    output and nowhere else (it has no plugins, so it must not reach the main port's listeners twice)."""

    RETRY = 2.0  # s between looks for an external port that is not there yet

    def __init__(self, main, opener=open_output):
        self.main, self.opener = main, opener
        self.lock = threading.RLock()  # the UI opens ports too: one opener at a time per device
        self.taps = {}  # Track -> MIDI out port feeding the track's instrument
        self.devices = {}  # port name -> MIDI out of an external track, or None while the device is away
        self.looked = {}  # port name -> when it was last looked for
        self.externals = []  # tracks that send to a device (refreshed by the UI clock)

    # --- external tracks ------------------------------------------------------
    def device(self, name):
        """The MIDI out for an external track's port name, opening it when it appears (throttled: scanning
        ports is not something to do per note)."""
        if not name:
            return None
        with self.lock:
            port = self.devices.get(name)
            if port:
                return port
            now = time.monotonic()
            if now - self.looked.get(name, 0.0) >= self.RETRY:
                self.looked[name] = now
                port = self.devices[name] = self.opener(name)
            return port

    def is_open(self, name):
        return bool(self.devices.get(name))

    def refresh(self, tracks):
        """Look for the external tracks' ports again (called on a clock: devices come and go) and let go of
        ports no track uses any more."""
        with self.lock:
            self.externals = [tr for tr in tracks if tr.external and tr.port]
            used = set()
            for tr in self.externals:
                used.add(tr.port)
                self.device(tr.port)
            for name in [n for n in self.devices if n not in used]:
                port = self.devices.pop(name)
                self.looked.pop(name, None)
                if port and hasattr(port, "close"):
                    port.close()

    def send_message(self, msg, track=None):
        if msg[0] >= 0xF0:  # clock, start/stop: the main port and every external device
            self.main.send_message(msg)
            for port in list(self.devices.values()):
                if port:
                    port.send_message(msg)
            return
        if track is not None:
            if track.external:
                port = self.device(track.port)
                if port:
                    port.send_message(msg)
                return
            self.main.send_message(msg)
            port = self.taps.get(track)
            if port:
                port.send_message(msg)
            return
        channel = (msg[0] & 0x0F) + 1
        for tr, port in list(self.taps.items()):
            if tr.channel == channel:
                port.send_message(msg)
        for tr in self.externals:  # hardware on that channel hears faders without a track too
            port = self.device(tr.port)
            if port and tr.channel == channel:
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
        self.rec = {}  # Track -> replace (bool): the tracks recording live playing into them
        self.rec_fresh = set()  # (lane, step) a held take is sounding: the pattern leaves it to the fingers
        self.rec_taken = set()  # (lane, step) recorded since recording started: REC doesn't erase them
        self.cc_sent = {}  # (track, cc) -> the value last sent, so a lane only talks when it changes
        self.arp_notes = {}  # Track -> {note: (velocity, when the sequencer lets it go or None)}
        self.arp_order = {}  # Track -> the held notes in the order they were pressed
        self.arp_keys = {}  # Track -> keys physically down (LATCH keeps the chord when they are all up)
        self.arp_step = {}  # Track -> how far through the arp's sequence it is
        self.song_step = None  # the song step playing (None: not started)
        self.song_at = 0  # the bar that step began at
        self.song_jump = None  # a step asked for: taken at the next bar
        self.clicker = Clicker()  # the metronome's tick (silent until it is switched on)
        self.click_tap = None  # the click plugin: (accent) -> None, inside the audio graph; None: the stream
        self.counting = False  # the count-in before the first bar
        self.count_left = 0  # count-in beats still to play
        self.count_beat = 0.0  # when the next count-in tick is due
        threading.Thread(target=self.run, daemon=True).start()

    # --- thread ---------------------------------------------------------
    def run(self):
        while True:
            with self.lock:
                now = time.perf_counter()
                for key, (off, _) in list(self.sounding.items()):
                    if off <= now:
                        self.note_off(key)
                if self.counting:
                    self.count(now)
                elif self.playing and now >= self.next_tick:
                    self.tick(now)
                wake = min([self.next_tick if self.playing else now + 0.005,
                            self.count_beat if self.counting else now + 0.005,
                            *(o for o, _ in self.sounding.values())])
            time.sleep(max(0.0, min(wake - time.perf_counter(), 0.005)))

    def tick(self, now):
        if now - self.next_tick > 0.05:  # fell far behind (e.g. system stall): don't burst-catch-up
            self.next_tick = now
        self.out.send_message([0xF8])
        if self.ticks % TICKS_PER_STEP == 0:
            self.step(self.next_tick)
        self.automate(time.perf_counter())
        self.ticks += 1
        self.next_tick += 60 / (self.project.bpm * PPQN)

    def step(self, t):
        self.step_index = self.ticks // TICKS_PER_STEP
        if self.step_index % STEPS_PER_BAR == 0:
            if self.project.song_on and self.project.song:
                self.song_tick(self.step_index // STEPS_PER_BAR)  # the arrangement decides first
            self.apply_pending()
        step_len = TICKS_PER_STEP * 60 / (self.project.bpm * PPQN)
        self.step_time, self.step_len = t, step_len
        if self.project.click and self.step_index % 4 == 0:  # a beat: higher on the first of the bar
            if self.click_tap is not None:  # the graph plays it with the notes: send it as the step plays
                self.click_beat(t, self.step_index % STEPS_PER_BAR == 0)
            else:  # a stream of our own runs a buffer behind: queue the next beat a whole beat early
                nxt = self.step_index + 4
                self.clicker.at(t + 4 * step_len, accent=nxt % STEPS_PER_BAR == 0)
        for tr in self.project.tracks:
            pat = tr.current
            i = self.pos.get(tr, self.step_index) % pat.length  # a track added mid-play joins in step
            self.playhead[tr] = i
            self.pos[tr] = (i + 1) % pat.length
            lanes = list(pat.lanes.items())  # a copy: the UI thread may add a lane meanwhile
            if tr.mute:
                continue
            for k, lane in lanes:
                s = lane[i]
                if s.on:
                    if (k, i) in self.rec_fresh:  # a take is sounding it live: it ends with the key
                        continue
                    self.note_on(tr, tr.note(k), s.vel, t + max(0.005, s.gate * step_len))

    def take_now(self, tr, pat, i, lanes, seconds):
        """Start what a finished take recorded, right now instead of on the next pass: its step is behind the
        playhead, so the pattern would only reach it a bar later and the take would seem to have been lost."""
        with self.lock:
            if not self.playing or seconds <= 0.02:
                return
            for k in lanes:
                self.note_on(tr, tr.note(k), pat.lane(k)[i].vel, time.perf_counter() + seconds)

    def note_on(self, tr, note, vel, off):
        """A note from the sequencer: with the track's arp on it joins the arp instead of sounding, so the
        pattern keeps its chords and the arp plays them (non-destructive: switch it off and they are chords)."""
        with self.lock:
            if self.arp_hold(tr, note, vel, off):
                return
            self._play(tr, note, vel, off)

    def _play(self, tr, note, vel, off):
        """Send the note now, and end it at `off`."""
        key = (tr.channel, note)
        if key in self.sounding:  # retrigger: close the previous note first
            self.note_off(key)
        self.out.send_message([0x90 | (tr.channel - 1), note, vel], tr)
        self.sounding[key] = (off, tr)

    # --- the arpeggiator --------------------------------------------------------
    def arp_hold(self, tr, note, vel, ends=None):
        """The track is holding this note: the arp plays it, the chord does not sound. `ends` is when the
        sequencer would let it go (None: when the key comes up)."""
        with self.lock:
            if not tr.arp.on:
                return False
            held = self.arp_notes.setdefault(tr, {})
            order = self.arp_order.setdefault(tr, [])
            keys = self.arp_keys.get(tr, 0)
            if keys == 0 and tr.arp.latch and held:  # a new chord after a latched one starts afresh
                held.clear()
                order.clear()
                self.arp_step[tr] = 0
            if note not in held:
                order.append(note)
            held[note] = (vel, ends)
            if ends is None:  # a key, not the sequencer: the latch waits for all of them to be up
                self.arp_keys[tr] = keys + 1
            return True

    def arp_release(self, tr, note):
        """A key up: the note leaves the arp, unless LATCH keeps the chord until the next one."""
        with self.lock:
            held = self.arp_notes.get(tr) or {}
            if note not in held:
                return False
            if held[note][1] is not None:  # a sequencer note: only its own gate lets it go
                return True
            self.arp_keys[tr] = max(0, self.arp_keys.get(tr, 0) - 1)
            if not tr.arp.latch:
                held.pop(note, None)
                if note in self.arp_order.get(tr, []):
                    self.arp_order[tr].remove(note)
            return True

    def arp_tick(self, now, track):
        """One tick of the arp: the notes come on the transport's own grid, so they sit with the sequencer's."""
        held = self.arp_notes.get(track)
        order = self.arp_order.get(track, [])
        if not track.arp.on:  # switched off: let go of what it was holding
            if held:
                self.arp_notes.pop(track, None)
                self.arp_order.pop(track, None)
                self.arp_keys.pop(track, None)
            return
        if not held:
            return
        for note, (_, ends) in list(held.items()):  # a sequencer note ends with its own gate
            if ends is not None and ends <= now:
                held.pop(note, None)
                if note in order:
                    order.remove(note)
        if not held:
            return
        rate = max(1, track.arp.rate)
        if self.ticks % rate:  # its own grid, anchored where the transport started
            return
        sequence = track.arp.sequence(order or list(held))
        if not sequence:
            return
        step = self.arp_step.get(track, 0)
        self.arp_step[track] = step + 1
        gap = rate / TICKS_PER_STEP * self.step_len  # one arp step, in seconds
        vel = max(v for v, _ in held.values())
        self._play(track, sequence[step % len(sequence)], vel,
                   now + max(0.01, gap * max(0.05, min(1.0, track.arp.gate))))

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
    def start(self, count=True):
        """Play. With a count-in set on the project, the bars of clicks come first (and the pattern waits)."""
        with self.lock:
            if self.playing or self.counting:
                return
            bars = self.project.count_in if count else 0
            if not bars:
                self.begin(first=self.project.click)
                return
            self.counting, self.count_left = True, bars * 4
            self.count_beat = time.perf_counter() + 0.03  # a moment for the click stream to get going
            self.clicker.prepare()

    def begin(self, first=False):
        """The pattern itself: called by the count-in when its last beat is done (its last click is the
        downbeat), or straight from `start`."""
        self.song_step, self.song_at, self.song_jump = None, 0, None  # a song starts at its first step
        self.cc_sent.clear()  # the lanes talk again from the top
        for d in (self.arp_notes, self.arp_order, self.arp_keys, self.arp_step):
            d.clear()  # and the arp starts its sequence over
        self.playing, self.ticks, self.step_index = True, 0, -1
        self.pos.clear()
        self.playhead.clear()
        self.next_tick = time.perf_counter() + 0.005
        if self.project.click:
            if self.click_tap is None:  # no plugin: our own stream has to be up before the first beat
                self.clicker.prepare()
            if first:  # no count-in to give the downbeat: play it now
                self.click_beat(self.next_tick, True)
        self.out.send_message([0xFA])

    def count(self, now):
        """The count-in: a tick per beat, the first of each bar higher, and the pattern starts on the next."""
        beat = 60.0 / self.project.bpm
        lead = 0.0 if self.click_tap is not None else self.clicker.lead  # a stream is written before it is due
        while self.count_left > 0 and now >= self.count_beat - lead:
            self.click_beat(self.count_beat, int(self.count_left) % 4 == 0)
            self.count_left -= 1
            self.count_beat += beat
        if self.count_left <= 0 and now >= self.count_beat:
            self.counting = False
            self.begin(first=False)

    def click_beat(self, when, accent):
        """One beat of the metronome: the click plugin's note (the graph renders it with the instruments), or
        an event for the app's own stream when there is no plugin."""
        if self.click_tap is not None:
            self.click_tap(accent)
        else:
            self.clicker.at(when, accent)

    def set_click(self, on):
        """The metronome: on keeps the click stream ready (a silent node between beats), off lets it go."""
        self.project.click = bool(on)
        if self.click_tap is not None:  # the graph has it: nothing to open or close
            return
        if self.project.click:
            self.clicker.prepare()
        elif not self.counting:
            self.clicker.close()

    def stop(self):
        with self.lock:
            if self.counting:  # still counting in: just give up before anything started
                self.counting, self.count_left = False, 0
                self.clicker.flush()
                return
            self.playing = False
            self.song_step, self.song_at, self.song_jump = None, 0, None
            self.cc_sent.clear()
            for d in (self.arp_notes, self.arp_order, self.arp_keys, self.arp_step):
                d.clear()
            self.clicker.flush()  # a beat queued for a moment from now must not sound after a stop
            self.out.send_message([0xFC])
            for key in list(self.sounding):
                self.note_off(key)
            self.playhead.clear()
            self.rec_fresh.clear()
            self.rec_taken.clear()
            self.step_index = -1
            self.apply_pending()

    def beats(self):
        """The sequencer's own position in fractional steps since it started: unlike a track's position it
        never wraps, so a note held over the end of the pattern still knows how long it was held."""
        with self.lock:
            if not self.playing or self.step_index < 0:
                return None
            return self.step_index + (time.perf_counter() - self.step_time) / self.step_len

    def position(self, tr):
        """Where in its pattern the track is right now, in (fractional) steps; None when stopped."""
        with self.lock:
            if not self.playing or tr not in self.playhead:
                return None
            return self.playhead[tr] + (time.perf_counter() - self.step_time) / self.step_len

    def refresh_ports(self):
        """Reopen the external tracks' MIDI outputs (devices come and go); called from the UI clock."""
        refresh = getattr(self.out, "refresh", None)  # a bare MIDI port has nothing to refresh
        if refresh:
            refresh(self.project.tracks)

    def live(self, tr, note, vel):
        """Note on (vel > 0) / off from a live pad, outside the sequencer's own note bookkeeping.

        With the track's arp on, a key goes into the arp instead of sounding the chord. A release never cuts a
        note the sequencer is playing at that moment: a take recorded a moment before the pattern's step comes
        round again is picked up by the pattern while the key is still held, and lifting the finger then used
        to silence it until the next pass."""
        with self.lock:
            if vel:
                if self.arp_hold(tr, note, vel):
                    return
            elif self.arp_release(tr, note):
                return
            if not vel and (tr.channel, note) in self.sounding:
                return
            self.out.send_message([(0x90 if vel else 0x80) | (tr.channel - 1), note, vel], tr)

    def send(self, msg, track=None):
        with self.lock:
            self.out.send_message(msg, track)

    def preview(self, tr, note):
        with self.lock:
            self._play(tr, note, 100, time.perf_counter() + 0.15)  # heard, arp or not

    def automate(self, now):
        """Once a tick (24 to a beat): the controller lanes, so a sweep can move between the steps, and the arp,
        whose notes come on the same grid."""
        for tr in self.project.tracks:
            if tr.mute:
                continue
            self.arp_tick(now, tr)
            if not tr.current.cc:
                continue
            pos = self.playhead.get(tr, 0) + (self.ticks % TICKS_PER_STEP) / TICKS_PER_STEP
            self.send_cc(tr, pos)

    def send_cc(self, tr, pos):
        """The pattern's controller lanes: a value holds until its next point, and a CC goes out only when it
        changes - so a held value is quiet and a ramp speaks only as often as it has to."""
        points = tr.current.cc
        if not points:
            return
        for number in points:
            value = tr.current.cc_at(number, pos)
            key = (tr, number)
            if value is None or self.cc_sent.get(key) == value:
                continue
            self.cc_sent[key] = value
            self.out.send_message([0xB0 | (tr.channel - 1), number, value], tr)

    # --- the song ---------------------------------------------------------
    def queue_song_step(self, i):
        """Jump to a song step at the next bar (a tap on it while the song plays)."""
        with self.lock:
            self.song_jump = i

    def song_position(self):
        """(step index, bars into it) while the song plays, else (None, 0)."""
        with self.lock:
            if self.song_step is None or not self.playing:
                return None, 0
            return self.song_step, max(0, self.step_index // STEPS_PER_BAR - self.song_at)

    def song_tick(self, bar):
        """At every bar: move on when the step's bars are done, and hand the tracks their patterns for the step
        that starts now. The song is a list of steps above the patterns - see SongStep."""
        steps = self.project.song
        if not steps:
            self.song_step = None
            return
        jump, self.song_jump = self.song_jump, None
        if jump is not None:
            self.song_start(jump, bar)
            return
        if self.song_step is None or self.song_step >= len(steps):
            self.song_start(0, bar)
            return
        if bar - self.song_at >= steps[self.song_step].span:
            nxt = self.song_step + 1
            if nxt >= len(steps):
                if not self.project.song_loop:
                    return  # the last step goes on until it is stopped
                nxt = 0
            self.song_start(nxt, bar)

    def song_start(self, i, bar):
        """Begin a song step: the tracks it names take its patterns, the others keep what they were playing."""
        steps = self.project.song
        if not steps:
            return
        i = max(0, min(len(steps) - 1, int(i)))
        self.song_step, self.song_at = i, bar
        for tr in self.project.tracks:
            pat = steps[i].patterns.get(tr.number)
            if pat is None or not 0 <= pat < len(tr.patterns):
                continue  # "keep previous" (or a pattern that is not there any more)
            self.pending.pop(tr, None)  # the song decides here, not a queued tap
            self.set_pattern(tr, pat)

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

    def add_track(self, kind, value=""):
        """A new track; `value` is the MIDI output of an external one or the input of an audio one."""
        with self.lock:
            number = max((t.number for t in self.project.tracks), default=0) + 1
            tr = Track(kind, 10 if kind == "drum" else min(len(self.project.tracks) + 1, 16), number,
                       port=value if kind == "external" else "", source=value if kind == "audio" else "")
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
        self.clicker.close()
