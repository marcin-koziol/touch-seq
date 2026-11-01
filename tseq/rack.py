"""The rack: the four modules (controls, sequencer, plugin host, live play) composed in one window with the
toolbar, autosave and project switching."""
import shutil
import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QFont, Qt

from .controls import Controls
from .dialogs import Chooser, MidiPorts, NewTrack, Settings
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
        self.seq = Sequencer(project, engine)
        self.controls = Controls(project, engine, host)
        self.controls.selected = lambda: self.seq.sel
        host.selected = lambda: self.seq.sel
        self.seq.master_fx = lambda: host.xyfx() is not None
        host.select_track = lambda tr: setattr(self.seq, "sel", tr)
        self.seq.new_track = self.open_new_track
        host.choose_port = self.open_port_choice
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

    def animating(self):
        # the playhead, and the blinking borders of queued pattern/scene switches
        return self.engine.playing or super().animating()

    def autosave(self):
        # Runs in the UI thread, which is the only one changing the project structure; the engine
        # only writes Track.pattern, so no lock is needed (and holding it would stall playback).
        if not self.dirty:
            return
        text = dump_project(self.project)
        if text != self.saved:
            save_text(self.path, text)
            self.saved, self.saved_at = text, time.localtime()
            self.update()  # the toolbar shows the time of the last save
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
        logo, file_, settings, patch, name, info = split(inner, [150 * k, 110 * k, 130 * k, 140 * k, 0, 500 * k],
                                                         14 * k)
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
        self.rail_button(p, patch, "PATCHBAY", self.open_patchbay, size=17,
                         on=bool(self.dialog) and self.dialog[0] == "PATCHBAY")
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

    def open_patchbay(self):
        """The audio graph by hand: e.g. an interface's input into its own output, to hear a synth."""
        if self.dialog:
            return self.close_dialog()
        self.open_dialog("PATCHBAY", "audio routing", Patchbay(self.host.patch_links, self.host.set_patch))

    def open_new_track(self):
        """The sequencer's "+": which kind of track, and for an external one which MIDI output."""
        if self.dialog:
            return self.close_dialog()
        self.open_dialog("NEW TRACK", "drum · melody · external MIDI", NewTrack(self.add_track))

    def add_track(self, kind, port=""):
        self.seq.add_track(kind, port)
        self.dirty = True
        self.autosave()
        self.close_dialog()

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
        elif e.key() == Qt.Key_Escape and self.seq.popup:
            self.seq.popup = None
        elif e.key() == Qt.Key_Space:
            self.seq.toggle_play()
        else:
            super().keyPressEvent(e)
