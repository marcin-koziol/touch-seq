"""Startup: config file, screen, the project chooser, and the fresh process a project switch re-execs into."""
import json
import os
import signal
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from .dialogs import Chooser
from .engine import Engine, MidiRouter, open_midi
from .host import HostPanel
from .model import load_project, save_text
from .rack import Rack
from .ui import Window


APP_DIR = Path(__file__).resolve().parent.parent  # touchseq.py and config.json sit beside the package

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "touch-seq"


DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "touch-seq"


DEFAULT_CONFIG = {
    "screen": "",  # output name (e.g. HDMI-1) to go full screen on; "" = the primary screen
    "midi_port": "Touch-Seq",  # MIDI out: an existing port whose name contains this, else a virtual one
    "projects": str(DATA_HOME / "projects"),  # relative paths are relative to the config file
    "audio_out": "default",  # plugin audio: "default" (follow PipeWire's default) or a sink's node name
    "patch": [],  # patchbay links: [[out port, in port], ...] (this machine's cables, not the project's)
}


def config_path():
    """The config: the command line's, else config.json beside the program (a source checkout), else
    ~/.config/touch-seq/config.json, written with the defaults the first time."""
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    beside = APP_DIR / "config.json"
    if beside.exists():
        return beside
    path = CONFIG_HOME / "config.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        save_text(path, json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    return path


def main(version):
    if sys.argv[1:2] == ["--version"]:
        print(f"Touch-Seq {version}")
        return
    cfg_path = config_path()
    cfg = {**DEFAULT_CONFIG, **json.loads(cfg_path.read_text())}
    folder = cfg_path.parent / Path(cfg["projects"]).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    legacy = cfg_path.parent / "project.json"  # the single project from before there was a folder
    if legacy.exists() and not (folder / legacy.name).exists():
        for old in (legacy, legacy.with_suffix(".plugins")):
            if old.exists():
                old.rename(folder / old.name)
    sys.setswitchinterval(0.001)  # let the engine thread grab the GIL quickly while the UI is busy
    app = QApplication(sys.argv[:1])
    screens = {s.name(): s for s in app.screens()}
    screen = screens.get(cfg["screen"])
    if screen is None:
        if cfg["screen"]:
            print(f"screen '{cfg['screen']}' not found (have: {', '.join(screens)}), using primary")
        screen = app.primaryScreen()

    def show(w):
        # Ctrl+C in the terminal: the UI timers keep Python running, so the handler gets a chance to fire
        signal.signal(signal.SIGINT, lambda *_: w.close())
        w.setScreen(screen)
        w.setGeometry(screen.geometry())
        w.setCursor(Qt.BlankCursor)
        w.showFullScreen()

    racks = []

    def remember(key, value):  # audio output and patchbay cables belong to this machine, not to a project
        cfg[key] = value
        save_text(cfg_path, json.dumps(cfg, indent=2) + "\n")

    def open_project(path):
        project = load_project(path)
        router = MidiRouter(open_midi(cfg["midi_port"]))
        engine = Engine(project, router)
        host = HostPanel(project, router, path.with_suffix(".plugins"),
                         Path.home() / ".cache" / "touch-seq" / "plugins.json", cfg.get("audio_out", "default"),
                         remember, patch=cfg.get("patch", []))
        racks.append(Rack(project, engine, path, host))
        show(racks[-1])

    if len(sys.argv) > 2:
        open_project(Path(sys.argv[2]))
    else:
        start = Chooser(folder, lambda path: (open_project(path), chooser.close()))  # rack first, so the app stays up
        chooser = Window([(0, 0, "TOUCH-SEQ", "projects", start)])
        show(chooser)
    code = app.exec()
    if racks and racks[-1].restart:  # project switch: a fresh process frees Carla and the MIDI ports cleanly
        nxt = racks[-1].next_project
        sys.stdout.flush()  # exec drops unflushed output
        os.execv(sys.executable, [sys.executable, str(APP_DIR / "touchseq.py"), str(cfg_path)] + ([str(nxt)] if nxt else []))
    sys.exit(code)
