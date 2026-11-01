"""Plugin host with the real Carla + PipeWire (run locally, not in CI): catalog and browser, a track's chain and
the master bus, wiring and following the audio output, knobs and fader links, state save/restore, cleanup.
Needs Carla (git, with CLAP), PipeWire and the Sideous Drums CLAP + an installed XYFX / Dragonfly reverb.
Usage: QT_QPA_PLATFORM=offscreen python tests/test_host.py [output dir]"""
import dataclasses
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import touchseq as ts  # noqa: E402
import plugins  # noqa: E402
from PySide6.QtCore import QPointF  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="touchseq-test-"))
OUT.mkdir(parents=True, exist_ok=True)
app = QApplication([])


class Log:
    def __init__(self):
        self.log = []

    def send_message(self, m, track=None):
        self.log.append(list(m))


def render(panel, name, w=919, h=459):
    img = QImage(w, h, QImage.Format_ARGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    panel.paint(p, w, h)
    p.end()
    img.save(str(OUT / name))


def links():
    return subprocess.run(["pw-link", "-l"], capture_output=True, text=True).stdout


pr = ts.Project(tracks=[ts.Track("drum", 10, 1)])
router = ts.MidiRouter(Log())
SLOT0 = (pr.tracks[0].strip, 0)  # the drum track's instrument slot
state_dir = OUT / "host-test.plugins"
host = ts.HostPanel(pr, router, state_dir, OUT / "plugins-cache.json", name="TS-Test")
assert host.host, host.message
print("catalog scanning…", end=" ", flush=True)
t0 = time.time()
while not host.catalog.ready:
    time.sleep(0.2)
print(f"{len(host.catalog.plugins)} plugins in {time.time() - t0:.1f}s")
drums = next(r for r in host.catalog.plugins if r.fmt == "clap" and r.label == "sideous.sideousdrums")
print("found:", drums)
import dataclasses
# a name of its own: a running Touch-Seq may have "S1.1 Sideous Drums" too, and port names would clash
cat_drums, drums = drums, dataclasses.replace(drums, name="TEST Drums")
assert drums.instrument

host.browser, host.browser_fmt = SLOT0, "clap"
render(host, "host_browser.png")
items = host.browser_items()
rh = 50 * min(919 / 1024, 459 / 600)
letters = host.hits[-1][0]
lh = letters.height() / len(host.LETTERS)
host.press(5, QPointF(letters.center().x(), letters.top() + (host.LETTERS.index("A") + 0.5) * lh))
host.move(5, QPointF(letters.center().x(), letters.top() + (host.LETTERS.index("S") + 0.5) * lh)); host.release(5, QPointF(0, 0))
first_s = next(n for n, r in enumerate(items) if r.name.upper().startswith("S"))
print("letter slide to S ->", items[round(host.browser_scroll / rh)].name); assert round(host.browser_scroll / rh) == first_s
host.browser_scroll = items.index(cat_drums) * rh
render(host, "host_browser_s.png")
adds = [r for r, f in host.hits if 40 < r.width() < 90 and r.top() > 50]
rows = [r for r, f in host.hits if r.width() > 500]
add0, row0 = min(adds, key=lambda r: r.top()), min(rows, key=lambda r: r.top())
host.press(6, QPointF(row0.left() + 50, row0.center().y())); host.release(6, QPointF(row0.left() + 50, row0.center().y()))
assert host.browser and not host.loaded, "tapping a row must not add"
host.press(7, add0.center()); host.move(7, add0.center() + QPointF(0, -40)); host.release(7, add0.center() + QPointF(0, -40))
assert host.browser and not host.loaded, "a scroll started on ADD must not add"
host.browser_scroll = items.index(cat_drums) * rh
render(host, "host_browser_s.png")
add0 = min([r for r, f in host.hits if 40 < r.width() < 90 and r.top() > 50], key=lambda r: r.top())
host.press(8, add0.center()); host.release(8, add0.center())
assert SLOT0 in host.loaded and host.loaded[SLOT0].ref == cat_drums, "ADD tap adds"
host.set_slot(*SLOT0, drums)
print("browser: row tap / scroll on ADD add nothing, ADD tap adds")
assert SLOT0 in host.loaded and host.sel == SLOT0, host.message
time.sleep(1.5)  # wiring thread
l = links()
client = host.loaded[SLOT0].client
print("client:", client)
assert f"{client}:events-in" in l and "TS-Test T1:midi" in l, l
sink = plugins.default_sink()
assert f"{client}:output_1" in l and sink in l
print("wired: midi + audio ->", sink)

# a link that disappears (device went away / unlinked by hand) comes back
subprocess.run(["pw-link", "-d", f"{client}:output_1", f"{sink}:playback_FL"])
assert f"{client}:output_1" not in [a for a, b in plugins.existing_links() if b == f"{sink}:playback_FL"]
time.sleep(4.5)
assert (f"{client}:output_1", f"{sink}:playback_FL") in plugins.existing_links(), "healed"
print("removed link healed")
# the default output changes (headphones <-> speakers): strips follow it
other = "alsa_output.pci-0000_01_00.1.hdmi-stereo"
orig_sink = plugins.default_sink
plugins.default_sink = lambda: other
for _ in range(80):
    host.tick(); time.sleep(0.05)
l = plugins.existing_links()
assert (f"{client}:output_1", f"{other}:playback_FL") in l and (f"{client}:output_1", f"{sink}:playback_FL") not in l, l
print("followed default output to", other)
plugins.default_sink = orig_sink
for _ in range(80):
    host.tick(); time.sleep(0.05)
assert (f"{client}:output_1", f"{sink}:playback_FL") in plugins.existing_links()
print("and back")
# picking an output by hand pins it (and is remembered); "default" follows the system again
kept = []
host.remember = lambda key, value: kept.append((key, value))
host.set_output(other)
time.sleep(1.5)
l = plugins.existing_links()
assert (f"{client}:output_1", f"{other}:playback_FL") in l and (f"{client}:output_1", f"{sink}:playback_FL") not in l
for _ in range(60):  # the default doesn't move a pinned output
    host.tick(); time.sleep(0.05)
assert (f"{client}:output_1", f"{other}:playback_FL") in plugins.existing_links() and kept == [("audio_out", other)]
host.set_output("default"); time.sleep(1.5)
assert (f"{client}:output_1", f"{sink}:playback_FL") in plugins.existing_links() and kept == [("audio_out", other), ("audio_out", "default")]
print("output pinned + back to default; sinks:", [d for _, d in plugins.sinks()])

# notes on ch10 reach strip 1's port (and the main port), ch1 doesn't
engine = ts.Engine(pr, router)
for i in (0, 4, 8, 12):
    pr.tracks[0].patterns[0].lanes[0][i].on = True
engine.start()
time.sleep(0.6)
engine.stop()
print("main port got", sum(1 for m in router.main.log if m[0] == 0x99), "note-ons on ch10")

# knob: drag first param up
render(host, "host.png")
plugin = host.loaded[SLOT0]
prm = plugin.params[1]
before = host.host.value(plugin, prm.index)
knob_hit = [r for r, f in host.hits][-12 - 0]  # regions are registered in order; find first knob below
for r, f in host.hits:
    if r.width() < 200 and r.height() > 60 and r.left() > 460:
        knob_hit = r
        break
host.press(1, knob_hit.center()); host.move(1, knob_hit.center() - QPointF(0, 20)); host.move(1, knob_hit.center() - QPointF(0, 60))
host.release(1, knob_hit.center())
for _ in range(5):
    host.tick(); time.sleep(0.02)
after = host.host.value(plugin, plugin.params[0].index), host.host.value(plugin, prm.index)
print("knob:", plugin.params[0].name, "->", after[0])

# state: set a param, save, reload in a fresh host
host.host.set_value(plugin, prm.index, prm.lo + (prm.hi - prm.lo) * 0.25)
want = host.host.value(plugin, prm.index)
for _ in range(10):  # param changes reach the plugin via the audio thread
    host.tick(); time.sleep(0.02)
host.save_states()
saved = list(state_dir.glob("*.carxs"))
print("state files:", [p.name for p in saved])
assert saved
# hold a knob -> pick a fader -> the fader drives that parameter (and follows the knob)
host.view, host.sel, host.page = "track", SLOT0, 0
render(host, "host_before_hold.png")
knob = next(r for r, f in host.hits if 60 < r.width() < 200 and r.height() > 60 and r.left() > 250)
host.press(20, knob.center())
for _ in range(20):
    host.tick(); time.sleep(0.05)
assert host.assigning, "holding a knob opens the fader choice"
host.release(20, knob.center())
render(host, "host_assign.png")
fader1 = next(r for r, f in host.hits if r.height() > 40 and r.top() > 60 and r.width() > 100)
host.press(21, fader1.center()); host.release(21, fader1.center())
c2 = pr.controls[2]
assert host.assigning is None and c2.target == "param" and c2.param[0] == 1 and c2.param[1] == 0, c2
print("linked fader 1 to", c2.param)
ctl = ts.Controls(pr, engine, host)
ctl.set_value(2, 0.25)
for _ in range(5):
    host.tick(); time.sleep(0.02)
assert abs(host.link_value(c2.param) - 0.25) < 0.02, host.link_value(c2.param)
lplugin, lprm = host.link_param(c2.param)
host.host.set_value(lplugin, lprm.index, lprm.lo + 0.75 * (lprm.hi - lprm.lo))
for _ in range(5):
    host.tick(); time.sleep(0.02)
print("fader <-> knob:", round(host.link_value(c2.param), 2), host.link_text(c2.param))
assert abs(host.link_value(c2.param) - 0.75) < 0.02
render(ctl, "ctrl_linked.png")
assert abs(ctl.values[2] - 0.75) < 0.02, "the fader follows the knob"
st = ts.Settings(pr, host); st.tab = "audio"; render(st, "settings_audio.png", 1368, 790)

# master effect: track -> master fx -> output, and back when it's removed
fx = next(r for r in host.catalog.plugins if r.fmt == "clap" and r.effect and "Dragonfly" in r.name)
fx = dataclasses.replace(fx, name="TEST Verb")
host.set_slot(pr.master, 0, fx)
time.sleep(2)
m_client = host.loaded[(pr.master, 0)].client
l = plugins.existing_links()
assert (f"{client}:output_1", f"{m_client}:input_1") in l and (f"{client}:output_1", f"{sink}:playback_FL") not in l, l
assert (f"{m_client}:output_1", f"{sink}:playback_FL") in l
print("track -> master", fx.name, "-> output")
host.view = "mixer"; render(host, "host_mixer.png"); host.view = "track"
host.set_slot(pr.master, 0, None); time.sleep(2)
assert (f"{client}:output_1", f"{sink}:playback_FL") in plugins.existing_links()
print("master fx removed: track straight to output again")

host.close()
time.sleep(0.5)
host2 = ts.HostPanel(pr, ts.MidiRouter(Log()), state_dir, OUT / "plugins-cache.json", name="TS-Test")
got = host2.host.value(host2.loaded[SLOT0], prm.index)
print(f"restored {prm.name}: {got:.3f} (want {want:.3f})")
assert abs(got - want) < 1e-3
render(host2, "host_restored.png")
# deleting the track takes its plugin, MIDI port and saved state with it
host2.save_states(); assert list(state_dir.glob("t1-*.carxs"))
pr.tracks.remove(pr.tracks[0]); pr.tracks.append(ts.Track("melody", 1, 2))
host2.tick(); time.sleep(1)
assert not host2.loaded and not host2.router.taps and not list(state_dir.glob("t1-*.carxs"))
print("deleted track cleaned up")
host2.close()
print("host OK")
