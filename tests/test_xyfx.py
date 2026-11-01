"""XYFX on the master with the real Carla (run locally): master row, XY pad drives the plugin, latch, modes,
BPM sync, wiring. Needs xyfx/ installed (xyfx/install.sh).
Usage: QT_QPA_PLATFORM=offscreen python tests/test_xyfx.py [output dir]"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import touchseq as ts  # noqa: E402
import plugins  # noqa: E402
from PySide6.QtCore import QPointF  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="touchseq-test-"))
OUT.mkdir(parents=True, exist_ok=True)
app = QApplication([])


class Log:
    def send_message(self, m, track=None):
        pass


pr = ts.Project(bpm=120, tracks=[ts.Track("drum", 10, 1)])
router = ts.MidiRouter(Log())
host = ts.HostPanel(pr, router, OUT / "xy-test.plugins", OUT / "plugins-cache.json", name="TS-XY")
assert host.host, host.message
host.catalog.rescan()
while host.catalog.scanning:
    time.sleep(0.1)
xy = next(r for r in host.catalog.plugins if r.label == "touchseq.xyfx" and r.fmt == "clap")
drums = next(r for r in host.catalog.plugins if r.label == "sideous.sideousdrums" and r.fmt == "clap")
print("catalog has", xy.name, xy.fmt, "effect:", xy.effect)
host.set_slot(pr.tracks[0].strip, 0, drums)
engine = ts.Engine(pr, router)
rack = ts.Rack(pr, engine, OUT / "xy-test.json", host)
rack.resize(1920, 1080)
rack.grab()
assert not rack.seq.master_fx(), "no MASTER row without XYFX"
host.set_slot(pr.master, 0, xy)
rack.grab()
assert rack.seq.master_fx() and host.xyfx()
# the MASTER row in the grid
seq_scr = rack.screens[rack.seq]
heads = sorted((r for r, f in rack.seq.hits if 100 < r.width() < 200 and r.height() > 30 and r.left() < 50), key=lambda r: r.top())
row = heads[1]  # T1, then MASTER
rack.press(1, seq_scr.topLeft() + row.center()); rack.release(1, seq_scr.topLeft() + row.center()); rack.grab()
assert rack.seq.master_focus, "MASTER row selected"
perf = rack.modules[3][4]
pscr = rack.screens[perf]
pad = next(r for r, f in perf.hits if r.width() > 400)
print("XY pad:", pad)
# touch: right quarter (1/16), near the top (open filter)
at = pscr.topLeft() + QPointF(pad.left() + pad.width() * 0.9, pad.top() + pad.height() * 0.1)
rack.press(2, at)
for _ in range(5):
    rack.tick(); time.sleep(0.02)
v = host.xy_values()
print("touching:", {k: round(x, 2) for k, x in v.items()})
assert v["engage"] == 1 and abs(v["x"] - 0.9) < 0.01 and abs(v["y"] - 0.9) < 0.01 and v["bpm"] == 120
assert ts.xyfx_repeat(v["x"], 0) == "1/16" and ts.xyfx_filter(0.9)[0] == "HPF" and ts.xyfx_filter(0.1)[0] == "LPF"
assert ts.xyfx_repeat(0.05, 0) == "1/16T" and ts.xyfx_repeat(0.05, 1) == "◀1/16" and ts.xyfx_repeat(0.45, 0) == "1/2T"
# the mode buttons set the plugin's Mode
mode_btn = [r for r, f in perf.hits if r.left() > pad.right() and r.height() > 30][-1]  # REVERSE (the last one)
rack.press(9, pscr.topLeft() + mode_btn.center()); rack.release(9, pscr.topLeft() + mode_btn.center())
assert host.xy_values()["mode"] == 1, host.xy_values()
rack.grab().save(str(OUT / "xy_reverse.png"))
rack.grab().save(str(OUT / "xy_rack.png"))
rack.move(2, at + QPointF(-pad.width() * 0.6, pad.height() * 0.5))
v = host.xy_values(); print("moved:", {k: round(x, 2) for k, x in v.items()})
assert v["x"] < 0.4 and v["y"] < 0.5
rack.release(2, at); rack.tick()
assert host.xy_values()["engage"] == 0, "lifting the finger turns it off"
perf.toggle_latch()
rack.press(3, at); rack.release(3, at)
assert host.xy_values()["engage"] == 1, "LATCH keeps it on"
perf.toggle_latch()
assert host.xy_values()["engage"] == 0, "LATCH off turns it off"
# BPM follows the project
pr.bpm = 97
rack.tick()
assert host.xy_values()["bpm"] == 97
# wiring: drums -> XYFX -> output
time.sleep(2)
l = plugins.existing_links()
d, m = host.loaded[(pr.tracks[0].strip, 0)].client, host.loaded[(pr.master, 0)].client
sink = plugins.default_sink()
assert (f"{d}:output_1", f"{m}:input_1") in l and (f"{m}:output_1", f"{sink}:playback_FL") in l, l
print("wired:", d, "->", m, "-> output")
rack.toggle_zoom(perf); rack.grab().save(str(OUT / "xy_zoom.png")); rack.toggle_zoom(perf)
# selecting a track again leaves the master
rack.seq.select(pr.tracks[0]); assert not rack.seq.master_focus
host.close()
print("xy OK")
