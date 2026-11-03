"""The rack: the four modules (controls, sequencer, plugin host, live play) composed in one window with the
toolbar, autosave and project switching."""
import shutil
import time

from PySide6.QtCore import QPointF, QTimer
from PySide6.QtGui import QColor, QFont, QPen, Qt

from .constants import WARN
from .controls import Controls
from .dialogs import ArpSettings, AudioSources, Chooser, MidiInputs, MidiPorts, NewTrack, Settings, Tempo
from .model import dump_project, free_project_path, save_text
from .patchbay import Patchbay
from .performer import Performer
from .sequencer import Sequencer
from .ui import Window, split


class Rack(Window):
    """The main window: toolbar (FILE, SETTINGS), performance controls, sequencer and plugin host."""
    TOOLBAR = 54

    def __init__(self, project, engine, path, host):
        self.project, self.engine, self.path, self.host = project, engine, path, host
        engine.click_tap = getattr(host, "click_note", None)  # the metronome lives in the graph with the plugin
        self.seq = Sequencer(project, engine)
        self.controls = Controls(project, engine, host)
        self.controls.selected = lambda: self.seq.sel
        host.selected = lambda: self.seq.sel
        self.seq.master_fx = lambda: host.xyfx() is not None
        host.select_track = lambda tr: setattr(self.seq, "sel", tr)
        self.seq.new_track = self.open_new_track
        self.seq.choose_source = self.open_source_choice
        self.seq.choose_input = self.open_input_choice
        self.seq.reroute = host.rewire
        host.choose_port = self.open_port_choice
        self.perf = Performer(project, engine, self.seq, host)  # owns the tracks' MIDI inputs
        self.controls.record_cc = self.perf.record_cc_channel  # faders write into the pattern when armed
        self.perf.choose_arp = self.open_arp  # its ARP button opens the track's arpeggiator
        self.seq.input_ok = self.perf.input_ok  # its socket says when a keyboard is away
        levels = getattr(host, "levels", None)  # its rows carry each track's level (a stub host has none)
        self.seq.levels = (lambda tr: levels(tr.strip)) if levels else (lambda tr: (0.0, 0.0))
        super().__init__([(0, 0, "CTRL-10", "performance controls", self.controls),
                          (0, 1, "SEQ-64", "step sequencer", self.seq),
                          (1, 0, "HOST-4", "plugin host", host),
                          (1, 1, "PLAY-12", "live chords · pads", self.perf)])
        self.saved = dump_project(project)
        self.saved_at = time.localtime()
        self.restart = False  # close and start again: on the start screen, or on `next_project`
        self.next_project = None
        QTimer(self, interval=2000, timeout=self.autosave).start()
        # plugin states change inside the plugins (their own UIs too), so they're saved on a clock
        QTimer(self, interval=30000, timeout=host.save_states).start()

    def animating(self):
        # the playhead, the count-in and the blinking borders of queued pattern/scene switches
        return self.engine.playing or self.engine.counting or super().animating()

    def autosave(self):
        # Runs in the UI thread, which is the only one changing the project structure; the engine
        # only writes Track.pattern, so no lock is needed (and holding it would stall playback).
        saved = False
        if self.dirty:
            text = dump_project(self.project)
            if text != self.saved:
                save_text(self.path, text)
                self.saved, saved = text, True
            e = self.engine
            self.dirty = bool(e.pending) or e.pending_scene is not None  # queued switches change it later
        if self.host.states_dirty:  # a parameter was turned: the plugin states go with the project
            self.host.save_states()
            saved = True
        if saved:
            self.saved_at = time.localtime()  # the toolbar says when anything was last saved
            self.update()

    def save_now(self):
        """The SAVE button: write the project and the plugins' states now, changed or not."""
        self.dirty, self.host.states_dirty = True, True
        self.autosave()
        self.saved_at = time.localtime()
        self.update()

    def closeEvent(self, e):
        self.engine.close()
        self.perf.close()  # let go of the MIDI keyboards (the next instance opens them again)
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
        logo, *cols = split(
            inner, [150 * k, 50 * k, 50 * k, 50 * k, 14 * k, 54 * k, 54 * k, 14 * k, 96 * k, 0, 250 * k,
                    108 * k], 10 * k)
        file_, settings, patch, sep1, click, count, sep2, bpm, name, info, save = cols
        f = QFont("sans-serif")
        f.setPixelSize(round(19 * k))
        f.setBold(True)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 2 * k)
        p.setFont(f)
        p.setPen(QColor("#e6e9ee"))
        p.drawText(logo, Qt.AlignLeft | Qt.AlignVCenter, "TOUCH-SEQ")
        self.rail_button(p, file_, "FILE", self.open_file, icon="folder",
                         on=bool(self.dialog) and self.dialog[0] == "FILE")
        self.rail_button(p, settings, "SETTINGS", self.open_settings, icon="sliders",
                         on=bool(self.dialog) and self.dialog[0] == "SETTINGS")
        self.rail_button(p, patch, "PATCHBAY", self.open_patchbay, icon="cables",
                         on=bool(self.dialog) and self.dialog[0] == "PATCHBAY")
        for sep in (sep1, sep2):  # a line between the app's own buttons and the transport's
            p.setPen(QPen(QColor("#4c535e"), 1.2 * k))
            p.drawLine(QPointF(sep.center().x(), inner.top() + 2 * k),
                       QPointF(sep.center().x(), inner.bottom() - 2 * k))
        on = self.project.click
        self.rail_button(p, click, "CLICK", lambda: self.engine.set_click(not self.project.click),
                         icon="metronome", on=on, color="#4ade80")
        bars = self.project.count_in
        self.rail_button(p, count, "COUNT", self.cycle_count_in, icon="sticks", on=bars > 0,
                         sub=str(bars) if bars else None)
        self.draw_bpm(p, bpm)
        f.setPixelSize(round(22 * k))
        p.setFont(f)
        p.drawText(name.adjusted(20 * k, 0, 0, 0), Qt.AlignLeft | Qt.AlignVCenter, self.path.stem)
        dirty = self.dirty or self.host.states_dirty
        self.rail_button(p, save, "SAVE", self.save_now, size=16, on=dirty)
        f.setPixelSize(round(16 * k))
        f.setBold(False)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1 * k)
        p.setFont(f)
        p.setPen(QColor(WARN if dirty else "#9aa3b0"))
        state = "UNSAVED" if dirty else f"SAVED {time.strftime('%H:%M:%S', self.saved_at)}"
        p.drawText(info.adjusted(0, 0, -10 * k, 0), Qt.AlignRight | Qt.AlignVCenter,
                   f"{self.project.key_name.upper()}   ·   {state}")

    def draw_bpm(self, p, r):
        """The project's tempo, where the transport is: tap it for the tempo dialog (drag, nudge, tap it out)."""
        k = self.k
        p.setPen(QPen(QColor("#5a606b"), 1))
        p.setBrush(QColor("#2a2e35"))
        p.drawRoundedRect(r, 4 * k, 4 * k)
        f = QFont("sans-serif")
        f.setPixelSize(round(10 * k))
        p.setFont(f)
        p.setPen(QColor("#8a929e"))
        p.drawText(r.adjusted(0, 1 * k, 0, -r.height() * 0.5), Qt.AlignCenter, "BPM")
        f.setPixelSize(round(19 * k))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#e6e9ee"))
        p.drawText(r.adjusted(0, r.height() * 0.3, 0, 0), Qt.AlignCenter, str(self.project.bpm))
        self.rail_buttons.append((r.adjusted(-6 * k, -5 * k, 6 * k, 5 * k), self.open_tempo))

    def open_tempo(self):
        if self.dialog:
            return self.close_dialog()
        self.open_dialog("TEMPO", "drag · nudge · tap", Tempo(self.project, self.close_dialog))

    def cycle_count_in(self):
        """The count-in: off, one bar of clicks, two (saved with the project)."""
        self.project.count_in = (self.project.count_in + 1) % 3

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

    def open_patchbay(self):
        """The audio graph by hand: e.g. an interface's input into its own output, to hear a synth."""
        if self.dialog:
            return self.close_dialog()
        self.open_dialog("PATCHBAY", "audio routing", Patchbay(self.host.patch_links, self.host.set_patch, labels=self.host.chain_labels,
                         internal=self.host.internal_links))

    def open_new_track(self):
        """The sequencer's "+": which kind of track, and for an external one which MIDI output."""
        if self.dialog:
            return self.close_dialog()
        self.open_dialog("NEW TRACK", "drum · melody · external MIDI · audio", NewTrack(self.add_track))

    def add_track(self, kind, value=""):
        self.seq.add_track(kind, value)
        self.host.ensure_monitor(self.seq.sel)  # a new audio track gets its monitor node right away
        self.dirty = True
        self.autosave()
        self.close_dialog()

    def open_source_choice(self, tr):
        if self.dialog:
            return self.close_dialog()
        self.open_dialog("AUDIO IN", f"track T{tr.number}",
                         AudioSources(lambda port: self.set_track_source(tr, port), tr.source))

    def set_track_source(self, tr, port):
        tr.source = port
        self.host.rewire()  # the input is wired by the rack: it survives chain changes
        self.dirty = True
        self.autosave()
        self.close_dialog()

    def open_arp(self, tr):
        """A track's arpeggiator: every tap in the dialog is heard at once and saved; the x closes it."""
        if self.dialog:
            return self.close_dialog()
        self.open_dialog("ARP", f"track T{tr.number} · one note at a time", ArpSettings(tr, self.touched))

    def touched(self):
        self.dirty = True
        self.autosave()

    def open_input_choice(self, tr):
        """A track's MIDI keyboard: the device and the channel it takes. Taps apply at once (the socket in
        EDIT lights up); the faceplate's x closes."""
        if self.dialog:
            return self.close_dialog()
        self.open_dialog("MIDI IN", f"track T{tr.number} · where it plays from",
                         MidiInputs(lambda port, channel: self.set_track_input(tr, port, channel),
                                    tr.in_port, tr.in_channel))

    def set_track_input(self, tr, port, channel):
        tr.in_port, tr.in_channel = port, channel
        self.dirty = True
        self.autosave()

    def open_port_choice(self, tr):
        if self.dialog:
            return self.close_dialog()
        self.open_dialog("MIDI OUT", f"external track T{tr.number}",
                         MidiPorts(lambda name: self.set_track_port(tr, name), tr.port))

    def set_track_port(self, tr, name):
        tr.port = name
        self.engine.refresh_ports()  # let go of the old port, open the new one
        self.dirty = True
        self.autosave()
        self.close_dialog()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.dialog:
            self.close_dialog()
        elif e.key() == Qt.Key_Space:
            self.seq.toggle_play()
        else:
            super().keyPressEvent(e)
