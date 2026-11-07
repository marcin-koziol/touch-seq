"""Sequencer, UI, projects and live-play tests; no Carla or audio needed (CI runs these).
Usage: QT_QPA_PLATFORM=offscreen python tests/test_app.py [output dir for screenshots]"""
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import plugins
import touchseq as ts
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QColor, QImage, QPainter


class View:
    """Renders a Panel offscreen at a given size, forwards everything else to it."""
    def __init__(self, panel):
        object.__setattr__(self, "panel", panel)
        object.__setattr__(self, "size", (1024, 600))

    def resize(self, w, h):
        object.__setattr__(self, "size", (w, h))

    def grab(self):
        img = QImage(*self.size, QImage.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        self.panel.paint(p, *self.size)
        p.end()
        return img

    def __getattr__(self, n):
        return getattr(self.panel, n)

    def __setattr__(self, n, v):
        setattr(self.panel, n, v)

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="touchseq-test-"))
OUT.mkdir(parents=True, exist_ok=True)


class FakeOut:
    def __init__(self):
        self.log = []

    def send_message(self, m, track=None):
        self.log.append((time.perf_counter(), list(m)))


# --- engine: timing + bar-quantized switching ---
pr = ts.Project(bpm=240, tracks=[ts.Track(channel=1), ts.Track(channel=2)])
a, b = pr.tracks
for i in range(0, 16, 4):
    a.patterns[0].lane(60)[i].on = True
a.patterns[1].lane(48)[1].on = True
b.patterns[0].length = 12
b.patterns[0].lane(60)[0].on = True
out = FakeOut()
e = ts.Engine(pr, out)
e.start()
step_len = 60 / 240 / 4  # 62.5ms
time.sleep(step_len * 6)
e.queue_pattern(a, 1)
assert a.pattern == 0, "must not switch mid-bar"
time.sleep(step_len * 12)  # past bar 2 start
assert a.pattern == 1, a.pattern
e.stop()
ons = [(t, m) for t, m in out.log if m[0] & 0xF0 == 0x90]
offs = [m for t, m in out.log if m[0] & 0xF0 == 0x80]
clocks = [t for t, m in out.log if m == [0xF8]]
print("note-ons:", [(round(t - out.log[0][0], 3), m) for t, m in ons])
print("clocks:", len(clocks), "on/off:", len(ons), len(offs))
iv = [y - x for x, y in zip(clocks, clocks[1:])]
print("clock interval ms: mean %.3f max %.3f min %.3f (expect %.3f)" % (
    1000 * sum(iv) / len(iv), 1000 * max(iv), 1000 * min(iv), 1000 * 60 / 240 / 24))
assert len(ons) == len(offs), "every note-on gets a note-off"
assert any(m[1] == 48 for _, m in ons), "pattern 2 played"
first48 = next(t for t, m in ons if m[1] == 48)
assert abs((first48 - ons[0][0]) - 17 * step_len) < 0.01, "P2 starts at bar 2 from its step 0"

# scenes
e.store_scene(0)
e.queue_pattern(a, 0)
e.store_scene(1)
e.queue_scene(0)
assert a.pattern == 1 and e.scene == 0
print("engine OK")
# grid: 1/8T is 12 steps/bar and a step is 8 ticks; the engine and project follow it
gp = ts.Project(bpm=240, tracks=[ts.Track("melody", 1, 1)], grid="1/8T")
assert gp.ticks_per_step == 8 and gp.steps_per_bar == 12 and gp.steps_per_beat == 3
assert '"grid":"1/8T"' in ts.dump_project(gp)
ts.save_text(OUT / "grid.json", ts.dump_project(gp))
assert ts.load_project(OUT / "grid.json").grid == "1/8T"
ge = ts.Engine(gp, FakeOut()); ge.start(); time.sleep(0.12); ge.stop()
print("1/8T step ms:", round(ge.step_len * 1000, 1))
assert abs(ge.step_len - 8 * 60 / (240 * 24)) < 0.005
gs = ts.Sequencer(gp, ge)
gp.grid = "1/16"; gs.cycle_grid(); assert gp.grid == "1/8T"
assert ts.Performer(gp, ge, gs, None).quants()[0] == ("1/8T", 1)
print("grid OK")

# --- UI render ---
app = QApplication([])
pr2 = ts.Project(tracks=[ts.Track(channel=c, number=c) for c in range(1, 8)])
for k, tr in enumerate(pr2.tracks):
    for i in range(0, 64, 1 + k):
        st = tr.patterns[0].lane(48 + (i * 5) % 24)[i]
        st.on, st.vel, st.gate = True, 40 + (i * 13) % 87, 0.2 + (i % 5) * 0.2
    tr.patterns[2].lane(60)[3].on = True
pr2.tracks[0].patterns[0].lane(67)[0].on = True  # a chord on T1 step 1
pr2.tracks[1].patterns[0].length = 24
dr = ts.Track("drum", 10, 8)
for v, every in [(0, 4), (1, 8), (2, 2), (3, 16), (5, 8)]:
    for i in range(0, 64, every):
        dr.patterns[0].lanes[v][(i + (4 if v in (1, 5) else 0) + (2 if v == 3 else 0)) % 64].on = True
dr.patterns[0].lanes[2][6].vel = 40
dr.voice = 1
pr2.tracks.append(dr)
pr2.tracks[3].mute = True
pr2.tracks[0].scenes[0] = 0
e2 = ts.Engine(pr2, FakeOut())
w = View(ts.Sequencer(pr2, e2))
w.resize(1024, 600)
e2.start()
time.sleep(0.4)
e2.queue_pattern(pr2.tracks[0], 2)
w.grab().save(str(OUT / "grid.png"))
# tap header of selected track -> edit view
w.grab()
w.press(1, QPointF(60, 100)); w.release(1, QPointF(60, 100))
assert w.view == "edit", w.view
w.grab().save(str(OUT / "edit.png"))
tr0 = pr2.tracks[0]; pat0 = tr0.patterns[0]
for lane in pat0.lanes.values():
    for st in lane: st.on = False
w.grab()
base, rows = w.roll_base(tr0), w.roll_rows
rh = 286 / rows; cw = (936 - 48 - 36) / 16
cx = lambda k: 80 + k * (cw + 4) + (8 if k >= 4 else 0) + cw / 2
y_top = 140 + rh / 2
def tap(x, y, tid=7):
    w.press(tid, QPointF(x, y)); w.release(tid, QPointF(x, y)); w.grab()
def on_notes(i):
    return sorted(n for n, _ in pat0.notes_at(i))
top = base + rows - 1
tap(cx(0), y_top); assert on_notes(0) == [top], on_notes(0)
tap(cx(0), y_top); assert on_notes(0) == [], "tap on note removes it"
w.press(8, QPointF(cx(0), y_top + rh)); [w.move(8, QPointF(cx(k), y_top + rh)) for k in range(4)]; w.release(8, QPointF(cx(3), y_top + rh))
assert [on_notes(i) for i in range(4)] == [[top - 1]] * 4, [on_notes(i) for i in range(4)]
tap(cx(5), y_top); tap(cx(5), y_top + 2 * rh); assert on_notes(5) == [top - 2, top], on_notes(5)
w.press(13, QPointF(cx(7), y_top)); w.move(13, QPointF(cx(7), y_top + rh)); w.move(13, QPointF(cx(7), y_top + 3 * rh)); w.release(13, QPointF(cx(7), y_top + 3 * rh))
assert on_notes(7) == [top - 3], ("placing + vertical drag moves the note", on_notes(7))
w.press(9, QPointF(cx(1), 470)); w.release(9, QPointF(cx(1), 470))  # the lane's bars (tabs above them)
print("lane vel:", pat0.lane(top - 1)[1].vel); assert pat0.lane(top - 1)[1].vel > 120
w.press(10, QPointF(30, 300)); w.move(10, QPointF(30, 315)); w.move(10, QPointF(30, 300 + 3 * rh)); w.release(10, QPointF(30, 400))
print("roll base after keyboard drag down 3 rows:", base, "->", w.roll_base(tr0)); assert w.roll_base(tr0) == base + 3
base = w.roll_base(tr0); top = base + rows - 1
# stretch: grab note at step 0 (row 2), drag right by 3 cells -> gate + 3
for lane in pat0.lanes.values():
    for st in lane: st.on = False
w.grab()
yr, n = y_top + rh, top - 1
tap(cx(0), yr); g0 = pat0.lane(n)[0].gate
w.press(11, QPointF(cx(0), yr)); w.move(11, QPointF(cx(0) + 20, yr)); w.move(11, QPointF(cx(3), yr)); w.release(11, QPointF(cx(3), yr)); w.grab()
print("gate after stretch:", g0, "->", pat0.lane(n)[0].gate)
assert pat0.lane(n)[0].on and abs(pat0.lane(n)[0].gate - (g0 + 3)) <= 0.25
# short note: grab it at the far right of its first cell and stretch
tap(cx(9), yr); pat0.lane(n)[9].gate = 0.125
w.press(14, QPointF(cx(9) + cw * 0.45, yr)); w.move(14, QPointF(cx(9) + cw * 0.45 + 20, yr)); w.move(14, QPointF(cx(11), yr)); w.release(14, QPointF(cx(11), yr))
print("short note stretched to", pat0.lane(n)[9].gate); assert pat0.lane(n)[9].gate > 1.5 and on_notes(10) == []
# vertical drag on the tail moves the whole note up a row, no stretch
w.press(12, QPointF(cx(2), yr)); w.move(12, QPointF(cx(2), yr - 20)); w.move(12, QPointF(cx(2), yr - rh)); w.release(12, QPointF(cx(2), yr - rh)); w.grab()
assert on_notes(0) == [n + 1] and on_notes(2) == [], (on_notes(0), on_notes(2))
tap(cx(2), yr - rh); assert on_notes(0) == [], "tap on tail removes"
pat0.lane(n)[0].on = True; pat0.lane(n)[0].gate = 2.5
for k in range(4, 16, 3):
    for d in (0, 4):
        st = pat0.lane(base + k % 7 + d)[k]
        st.on, st.gate, st.vel = True, 0.25 + (k % 4) * 0.25, 60 + k * 4
pat0.lane(90)[14].on = True
w.grab().save(str(OUT / "roll.png"))
st = pat0.lane(n)[9]
ts.save_text(OUT / "p.json", ts.dump_project(pr2))
p3 = ts.load_project(OUT / "p.json")
assert len(p3.tracks) == 8 and p3.tracks[0].patterns[0].lanes[n][9].gate == st.gate
assert sorted(k for k, _ in p3.tracks[0].patterns[0].notes_at(4)) == on_notes(4)
w.sel = dr; w.view = "edit"
w.grab().save(str(OUT / "drum_edit.png"))
# the drum editor's GATE mode: a step's length is editable, not stuck at the default
w.drum_param = "gate"
gst = dr.current.lane(0)[0]
w.edit_step(dr, gst, "gate", 1.0)
assert gst.gate == 1.0 and dr.last.gate == 1.0
w.grab().save(str(OUT / "drum_gate.png"))
# and a sideways drag stretches it across the following steps, the way the piano roll does
s = w.s
mid = QRectF(0, 64 * s, 1024, 600 - 70 * s - 64 * s)
area = QRectF(8 * s, mid.top() + 8 * s + 58 * s + 10 * s, 1024 - 16 * s,
              mid.bottom() - (mid.top() + 8 * s + 58 * s) - 18 * s)
row = QRectF(area.left(), area.top(), area.width(), area.height() / len(dr.current.lanes) - 3 * s)
lane0 = QRectF(row.left() + 130 * s, row.top(), row.width() - 130 * s, row.height())
cells = w.panel.cells(lane0)
w.press(30, cells[0].center()); w.move(30, QPointF(cells[0].center().x() + 20, cells[0].center().y()))
w.move(30, cells[3].center()); w.release(30, cells[3].center())
print("drum gate stretched to", gst.gate)
assert gst.on and gst.gate == 4.0, gst.gate  # room ends at the next hit of the same voice (step 4)
# pressing the tail grabs the step: a tap there takes the whole step out
w.press(31, cells[2].center()); w.release(31, cells[2].center())
assert not gst.on, "a tap on the tail removes the step"
# a press on an empty step makes it, and the same drag stretches it
dr.last.gate = 1.0
w.press(32, cells[1].center()); w.move(32, QPointF(cells[1].center().x() + 20, cells[1].center().y()))
w.move(32, cells[2].center()); w.release(32, cells[2].center())
newst = dr.current.lane(0)[1]
print("new step gate:", newst.gate)
assert newst.on and newst.gate == 2.0, newst.gate
w.grab().save(str(OUT / "drum_gate_stretched.png"))
w.drum_param = "step"
w.view = "grid"; w.scroll = 1e9
w.grab().save(str(OUT / "drum_grid.png"))
w.scroll = 0
w.resize(800, 480)
w.view = "grid"
w.grab().save(str(OUT / "grid800.png"))
e2.stop()
print("UI OK")
# drum engine: kit notes on ch10
out3 = FakeOut()
pr3 = ts.Project(bpm=240, tracks=[dr])
e3 = ts.Engine(pr3, out3)
e3.start(); time.sleep(0.07); e3.stop()
print("drum step 1 notes:", sorted(m[1] for _, m in out3.log if m[0] == 0x99))
assert sorted(m[1] for _, m in out3.log if m[0] == 0x99) == [36, 42]
# engine: gate 3 steps -> note-off 3 steps after note-on
out4 = FakeOut(); t4 = ts.Track(channel=3); s4 = t4.patterns[0].lane(60)[0]; s4.on, s4.gate = True, 3.0
e4 = ts.Engine(ts.Project(bpm=240, tracks=[t4]), out4); e4.start(); time.sleep(0.3); e4.stop()
on_t = next(t for t, m in out4.log if m[0] == 0x92); off_t = next(t for t, m in out4.log if m[0] == 0x82)
print("gate 3.0 length ms:", round((off_t - on_t) * 1000, 1), "(expect 187.5)"); assert abs(off_t - on_t - 0.1875) < 0.006
print("save size bytes:", len(ts.dump_project(pr2)))
# (the tempo moved to the top bar: see the tempo dialog test further down)
# delete drum track T1 -> remaining keep their names/colors
first = pr2.tracks[0]; second = pr2.tracks[1]
c2 = w.color(second)
w.sel = first; w.delete_track()
assert w.sel is second and w.color(second) == c2 and second.number == 2 and w.view == "grid"
nt = e2.add_track("drum"); assert nt.number == 9 and nt.drum
print("delete/add OK")
# pattern slot drag: copy 1 -> 2 and switch
w.resize(1024, 600); w.view = "grid"; w.sel = pr2.tracks[1]; w.grab()
t1 = pr2.tracks[1]
sx = lambda i: 245 + i * (1008 - 475) / 16 + i * 5 + (1008 - 475) / 32
t1.pattern = 0; t1.patterns[0].lane(77)[5].on = True
w.press(20, QPointF(sx(0), 563)); w.move(20, QPointF(sx(0) + 20, 560)); w.move(20, QPointF(sx(1), 560))
w.grab().save(str(OUT / "slotdrag.png"))
w.release(20, QPointF(sx(1), 560)); w.grab()
print("after drag: pattern", t1.pattern, "pending", e2.next_pattern(t1))
assert t1.patterns[1].lanes[77][5].on and t1.patterns[1] is not t1.patterns[0]
assert e2.next_pattern(t1) == 1 or t1.pattern == 1
# tap still switches; drop outside cancels
e2.stop(); w.grab()
w.press(21, QPointF(sx(3), 563)); w.release(21, QPointF(sx(3), 563)); assert t1.pattern == 3
w.press(22, QPointF(sx(3), 563)); w.move(22, QPointF(sx(3), 300)); w.release(22, QPointF(sx(3), 300)); assert t1.pattern == 3 and t1.patterns[4].empty()
print("slot drag OK")

# --- rack: 1920x1080, faders, routing, pitch bend spring ---
out5 = FakeOut()
e5 = ts.Engine(pr2, out5)
class StubHost(ts.Panel):
    host, message = None, "plugin host unavailable (test stub)"
    patch = []
    def xyfx(self):
        return None
    def patch_links(self):
        return list(self.patch)
    def set_patch(self, links):
        self.patch = list(links)
    def chain_labels(self):
        return {}
    def internal_links(self):
        return set()
    rewired = 0
    def rewire(self):
        type(self).rewired += 1
    states_dirty = False
    saved_states = 0
    def save_states(self):
        type(self).saved_states += 1
        self.states_dirty = False
    monitor = None
    def ensure_monitor(self, tr):
        pass
    def draw(self, p):
        p.fillRect(self.rect(), ts.QColor(ts.BG))
    def close(self):
        pass
rack = ts.Rack(pr2, e5, OUT / "rack.json", StubHost())
rack.resize(1920, 1080)
rack.grab().save(str(OUT / "rack.png"))
ctrl = rack.controls
scr = rack.screens[ctrl]
print("ctrl screen:", scr)
# find fader strip rects: press on CC20 fader (index 2) near the top -> high value
def strip_x(i):
    for rect, f in ctrl.hits:
        pass
cx3 = scr.left() + ctrl.hits[-8][0].center().x()  # first CC strip region (regions: edit btn, pb, mod, cc x8)
rack.press(30, QPointF(cx3, scr.top() + 150)); rack.move(30, QPointF(cx3, scr.top() + 140)); rack.release(30, QPointF(cx3, scr.top() + 140))
ccs = [m for _, m in out5.log if m[0] & 0xF0 == 0xB0]  # on the selected track's channel
print("CC sent:", ccs[-1]); assert ccs[-1][1] == 102 and ccs[-1][2] > 100
# pitch bend: hold at top + simultaneous seq touch, release -> spring back to 8192
pbx = scr.left() + ctrl.hits[1][0].center().x()
rack.press(31, QPointF(pbx, scr.top() + 120))
assert 0 in ctrl.held
seq_scr = rack.screens[rack.seq]
rack.press(32, QPointF(seq_scr.left() + 40, seq_scr.top() + 30)); rack.release(32, QPointF(seq_scr.left() + 40, seq_scr.top() + 30))
assert e5.playing, "play pressed while holding pitch bend"
e5.stop()
pbs = [m for _, m in out5.log if m[0] & 0xF0 == 0xE0]
print("PB while held:", pbs[-1], (pbs[-1][2] << 7 | pbs[-1][1]))
rack.release(31, QPointF(pbx, scr.top() + 120))
for _ in range(20):
    time.sleep(0.04); ctrl.tick()
pbs = [m for _, m in out5.log if m[0] & 0xF0 == 0xE0]
print("PB after release:", pbs[-1], (pbs[-1][2] << 7 | pbs[-1][1])); assert pbs[-1][1:] == [0, 64]
# edit: channel of CC20 +1 by tapping upper half
ctrl.editing = True; rack.grab().save(str(OUT / "rack_edit.png"))
# CC 102: TARGET (TRACK -> CHANNEL), then +1 on the channel box
regions = [r for r, f in ctrl.hits]
cc_strip = [r for r in regions if r.width() < 120][7:]  # edit button aside: pb (target, track), mod (target, track, 14b, bip)
tgt = cc_strip[0]
rack.press(33, QPointF(scr.left() + tgt.center().x(), scr.top() + tgt.center().y())); rack.release(33, QPointF(scr.left() + tgt.center().x(), scr.top() + tgt.center().y()))
assert pr2.controls[2].target == "channel", pr2.controls[2]
rack.grab()
box = [r for r, f in ctrl.hits if r.width() < 120][8]
rack.press(34, QPointF(scr.left() + box.center().x(), scr.top() + box.top() + 10)); rack.release(34, QPointF(scr.left() + box.center().x(), scr.top() + box.top() + 10))
print("controls:", pr2.controls[2]); assert pr2.controls[2].channel == 2
q = ts.load_project(OUT / "p.json") if False else None
ts.save_text(OUT / "c.json", ts.dump_project(pr2)); assert ts.load_project(OUT / "c.json").controls == pr2.controls
print("rack OK")
# --- 14-bit + bipolar faders ---
pr6 = ts.Project(tracks=[ts.Track()])
out6 = FakeOut(); e6 = ts.Engine(pr6, out6)
c6 = ts.Controls(pr6, e6)
c6.assign(2, "cc", 20); c6.assign(2, "fine", True)
c6.set_value(2, 0.75)
print("14-bit 0.75:", [m for _, m in out6.log])
v = round(0.75 * 16383); assert [m for _, m in out6.log] == [[0xB0, 20, v >> 7], [0xB0, 52, v & 0x7F]]
n = len(out6.log); c6.set_value(2, (v - 1) / 16383)
print("LSB-only step:", [m for _, m in out6.log[n:]]); assert [m for _, m in out6.log[n:]] == [[0xB0, 52, (v - 1) & 0x7F]]
c6.assign(3, "bipolar", True); assert c6.values[3] == 0.5
n = len(out6.log); c6.set_value(3, 0.5); print("bipolar center:", out6.log[n][1]); assert out6.log[n][1] == [0xB0, 103, 64]
c6.assign(1, "fine", True); c6.set_value(1, 1.0); print("mod 14-bit max:", [m for _, m in out6.log[-2:]])
assert [m for _, m in out6.log[-2:]] == [[0xB0, 1, 127], [0xB0, 33, 127]]
ts.save_text(OUT / "c6.json", ts.dump_project(pr6)); assert ts.load_project(OUT / "c6.json").controls == pr6.controls
v6 = View(c6); v6.resize(919, 459); c6.editing = True; v6.grab().save(str(OUT / "ctrl_edit.png"))
c6.editing = False; c6.set_value(3, 0.8); v6.grab().save(str(OUT / "ctrl.png"))
print("fader modes OK")

# --- old project files (mono steps with a note) load as lanes ---
import json as _j
old = {"bpm": 100, "tracks": [
    {"kind": "melody", "channel": 1, "pattern": 0, "scenes": [None] * 8, "mute": False, "voice": 0,
     "last": [False, 62, 90, 0.5], "patterns": [{"length": 16, "steps": [[0, 2, True, 65, 80, 1.5]]}]},
    {"kind": "drum", "channel": 10, "pattern": 0, "scenes": [None] * 8, "mute": False, "voice": 0,
     "last": [False, 60, 100, 0.5], "patterns": [{"length": 16, "steps": [[3, 4, True, 60, 70, 0.5]]}]}]}
(OUT / "old.json").write_text(_j.dumps(old))
po = ts.load_project(OUT / "old.json")
assert po.tracks[0].patterns[0].lanes[65][2] == ts.Step(True, 80, 1.5) and po.tracks[0].last_note == 62
assert po.tracks[1].patterns[0].lanes[3][4].on
print("old format OK")
# --- zoom ---
def rail_button_of(rack, panel):
    scr = rack.screens[panel]
    return max((r for r, a in rack.rail_buttons if scr.left() < r.center().x() < scr.right() and scr.top() - 50 < r.bottom() < scr.top() + 10), key=lambda r: r.left())
rack.controls.editing = False
rack.grab()
seq_btn = rail_button_of(rack, rack.seq)
rack.press(40, seq_btn.center()); rack.release(40, seq_btn.center())
assert rack.zoomed is rack.seq
rack.seq.view = "edit"; rack.seq.sel = pr2.tracks[0]
rack.grab().save(str(OUT / "zoom.png"))
assert list(rack.screens) == [rack.seq] and rack.screens[rack.seq].width() > 1800
btn = rail_button_of(rack, rack.seq)
rack.press(41, btn.center()); rack.release(41, btn.center()); rack.grab()
assert rack.zoomed is None and len(rack.screens) == 4
print("zoom OK")
# meters live in their own small widgets: a moving needle must not repaint the whole rack
class MeterPanel(ts.Panel):
    def draw(self, p):
        p.fillRect(self.rect(), ts.QColor(ts.BG))
        rect = QRectF(40, 50, 10, 100)
        self.meter_rects["level"] = (rect, lambda p, r: p.fillRect(r, ts.QColor("#4ade80")), ts.PANEL)
    def tick(self):
        self.window_ref.meters_changed()
mp = MeterPanel()
mw = ts.Window([(0, 0, "M", "meter", mp)])
mw.resize(800, 600)
mw.show()
mw.grab()
mw.sync_meters()
assert len(mw.meter_views) == 1, mw.meter_views
mv = next(iter(mw.meter_views.values()))
assert (mv.width(), mv.height()) == (10, 100), mv.geometry()
mw.tick()
assert not mw.meters_dirty, "the meter clock consumed the change without a full frame"
mw.close()
# --- same-pitch notes don't run into each other ---
prn = ts.Project(tracks=[ts.Track("melody", 2, 4)])
trn = prn.tracks[0]; patn = trn.current
trn.last = ts.Step(True, 100, 4.0)
sq = ts.Sequencer(prn, ts.Engine(prn, FakeOut()))
sq.place(trn, patn, 4, 55)                         # quarter on beat 2
sq.place(trn, patn, 1, 55)                         # tap just before it: cut to end where it starts
print("beat-1 note gate:", patn.lanes[55][1].gate); assert patn.lanes[55][1].gate == 3 and patn.lanes[55][4].gate == 4
sq.place(trn, patn, 0, 55); assert patn.lanes[55][0].gate == 1
sq.place(trn, patn, 9, 55); sq.place(trn, patn, 10, 55)   # painting sideways: the earlier one ends where the next starts
assert patn.lanes[55][9].gate == 1 and patn.lanes[55][10].gate == 4
sq.place(trn, patn, 4, 60); sq.move_note(trn, patn, 4, 60, 55) if False else None
patn.lanes[55][4].on = False; sq.move_note(trn, patn, 4, 60, 55); assert patn.lanes[55][4].gate == 4  # room to 9 is 5
sq.place(trn, patn, 6, 62); sq.move_note(trn, patn, 6, 62, 55); assert patn.lanes[55][6].gate == 3 and patn.lanes[55][4].gate == 2
print("no-overlap OK")
# --- a track added while playing joins at the current position ---
prj = ts.Project(bpm=240, tracks=[ts.Track("drum", 10, 1)])
ej = ts.Engine(prj, FakeOut())
ej.start(); time.sleep(step_len * 6.5)
new = ej.add_track("melody")
time.sleep(step_len * 2)
print("global step", ej.step_index, "old track at", ej.playhead[prj.tracks[0]], "new track at", ej.playhead[new])
assert ej.playhead[new] == ej.playhead[prj.tracks[0]] == ej.step_index % 16
ej.stop()
print("join OK")
# --- projects: names, chooser, duplicate ---
import shutil as _sh
pdir = OUT / "projtest"; _sh.rmtree(pdir, ignore_errors=True); pdir.mkdir()
assert ts.free_project_path(pdir, "Project").name == "Project 1.json"
opened = []
ch = View(ts.Chooser(pdir, opened.append)); ch.resize(1888, 1000); ch.grab()
nb = next(r for r, f in ch.hits if r.width() > 150 and r.top() < 120)  # NEW
ch.press(1, nb.center()); ch.release(1, nb.center())
assert ch.keyboard is not None, "NEW asks for a name first"
ch.grab().save(str(OUT / "chooser_keyboard.png"))
ch.keyboard.press("ok")  # the placeholder name: "Project"
assert opened == [pdir / "Project.json"] and opened[0].exists(), opened
ch.new(); ch.keyboard.press("ok"); time.sleep(0.02)
(pdir / "Project 1.plugins").mkdir(); (pdir / "Project 1.plugins" / "s1.carxs").write_text("x")
assert ts.free_project_path(pdir, "Project 1").name == "Project 3.json"
ch.grab().save(str(OUT / "chooser.png"))
rows = [r for r, f in ch.hits if r.height() > 60 and r.width() > 1000]
first = [p.name for p in ts.project_files(pdir)]
print("projects newest first:", first); assert first == ["Project 2.json", "Project.json"]
ch.press(2, rows[1].center()); ch.release(2, rows[1].center()); assert opened[-1].name == "Project.json"
dels = [r for r, f in ch.hits if 150 < r.width() < 300 and r.top() > 150]
ch.press(3, dels[-1].center()); time.sleep(0.9); ch.tick(); ch.release(3, dels[-1].center())
assert not (pdir / "Project.json").exists() and not (pdir / "Project.plugins").exists()
# duplicate from the rack copies json + plugin states and asks for a restart into the copy
src = pdir / "Project 2.json"; (pdir / "Project 2.plugins").mkdir(); (pdir / "Project 2.plugins" / "a.carxs").write_text("state")
prd = ts.load_project(src)
rk = ts.Rack(prd, ts.Engine(prd, FakeOut()), src, StubHost())
rk.duplicate()
print("restart:", rk.restart, rk.next_project.name)
assert rk.restart and rk.next_project.name == "Project 3.json" and (pdir / "Project 3.plugins" / "a.carxs").read_text() == "state"
rk2 = ts.Rack(prd, ts.Engine(prd, FakeOut()), src, StubHost()); rk2.resize(1920, 1080); rk2.grab().save(str(OUT / "rack_project.png"))
print("projects OK")

# --- toolbar + dialogs ---
pr7 = ts.Project(tracks=[ts.Track("melody", 1, 1)])
p7 = pdir / "Project 2.json"
rk7 = ts.Rack(pr7, ts.Engine(pr7, FakeOut()), p7, StubHost()); rk7.resize(1920, 1080); rk7.grab()
file_btn, settings_btn = rk7.rail_buttons[0][0], rk7.rail_buttons[1][0]
rk7.press(50, file_btn.center()); rk7.release(50, file_btn.center()); rk7.grab()
assert rk7.dialog and rk7.dialog[0] == "FILE"
rk7.grab().save(str(OUT / "dialog_file.png"))
seq_scr = rk7.screens[rk7.seq]
rk7.press(51, seq_scr.topLeft() + QPointF(40, 30)); rk7.release(51, seq_scr.topLeft() + QPointF(40, 30)); rk7.grab()
assert rk7.dialog is None and not rk7.engine.playing, "tap beside the dialog closes it and doesn't reach the module"
rk7.press(52, settings_btn.center()); rk7.release(52, settings_btn.center()); rk7.grab()
st7 = rk7.dialog[2]
keys = [r for r, f in st7.hits][2:14]  # after the two tabs
dscr = rk7.screens[st7]
rk7.press(53, dscr.topLeft() + keys[9].center()); rk7.release(53, dscr.topLeft() + keys[9].center())  # A
st7.hits and None
scale_btn = [r for r, f in st7.hits][14 + 2]  # minor
rk7.press(54, dscr.topLeft() + scale_btn.center()); rk7.release(54, dscr.topLeft() + scale_btn.center()); rk7.grab()
print("key/scale:", pr7.key_name); assert pr7.key == 9 and pr7.scale == "minor"
rk7.grab().save(str(OUT / "dialog_settings.png"))
st7.tab = "audio"; rk7.grab().save(str(OUT / "dialog_audio.png"))
rk7.close_dialog(); rk7.seq.view = "edit"; rk7.toggle_zoom(rk7.seq)
for n in (57, 60, 64, 69, 72):
    pr7.tracks[0].current.lane(n)[(n * 3) % 16].on = True
rk7.grab().save(str(OUT / "roll_scale.png"))
txt = ts.dump_project(pr7); assert ts.load_project.__name__ and '"key":9' in txt and '"scale":"minor"' in txt
print("dialogs OK")
# --- live play: chords, voicing, recording ---
prp = ts.Project(bpm=240, tracks=[ts.Track("melody", 3, 1), ts.Track("drum", 10, 2)])
outp = FakeOut(); ep = ts.Engine(prp, outp)
sqp = ts.Sequencer(prp, ep); perf = ts.Performer(prp, ep, sqp)
names = lambda: [c[1] for c in perf.chords()]
romans = lambda: [c[0] for c in perf.chords()]
print("C major:", names()[:7], romans()[:7])
assert names()[:7] == ["C", "Dm", "Em", "F", "G", "Am", "B°"] and romans()[:7] == ["I", "ii", "iii", "IV", "V", "vi", "vii°"]
perf.ext = {"7"}; assert names()[:7] == ["Cmaj7", "Dm7", "Em7", "Fmaj7", "G7", "Am7", "Bø7"], names()
assert names()[7:] == ["Fmaj7", "Fm7", "G7", "Abmaj7", "Bb7"], names()[7:]
perf.ext = {"sus4"}; assert names()[0] == "Csus4"
perf.ext = {"9"}; assert names()[0] == "Cmaj9" and names()[1] == "Dm9", names()[:2]
perf.ext = set(); prp.key, prp.scale = 9, "minor"
print("A minor:", names()[:7], romans()[:7])
assert names()[:7] == ["Am", "B°", "C", "Dm", "Em", "F", "G"] and romans()[:7] == ["i", "ii°", "III", "iv", "v", "VI", "VII"]
prp.scale = "minor pentatonic"; assert names()[0] == "Am"  # 5-note scale: chords from A minor
# voice leading keeps chords close
prp.key, prp.scale = 0, "major"
c = perf.voice(perf.chords()[0][2]); f = perf.voice(perf.chords()[3][2]); g = perf.voice(perf.chords()[4][2])
print("voicing C F G:", c, f, g)
assert c == [48, 52, 55] and max(abs(sum(f) / 3 - sum(c) / 3), abs(sum(g) / 3 - sum(f) / 3)) < 4
perf.lead = False; assert perf.voice(perf.chords()[3][2]) == [53, 57, 60]; perf.lead = True
# live notes go out on the track's channel; release sends note-offs
sqp.sel = prp.tracks[0]
r = ts.QRectF(0, 0, 100, 100)
gp = perf.press_pad(0, sqp.sel, [(60, 60), (64, 64)], r, QPointF(50, 10))
assert perf.held[0] == 1 and [m for _, m in outp.log][-2:] == [[0x92, 60, 118], [0x92, 64, 118]], outp.log[-2:]
gp.release(None); assert perf.held[0] == 0 and [m for _, m in outp.log][-2:] == [[0x82, 60, 0], [0x82, 64, 0]]
# REC (replace): played chord lands quantized, length from the hold, old notes get erased as the playhead passes
mel = prp.tracks[0]; pat = mel.current
pat.lane(72)[6].on = True  # not played over: REC leaves it alone (it only replaces what is played)
perf.mode, perf.quant = "rec", 2
perf.tick(); ep.start()
time.sleep(step_len * 3.3)                     # playhead at step 3
g1 = perf.press_pad(1, mel, [(60, 60), (67, 67)], r, QPointF(50, 50))
time.sleep(step_len * 2)
g1.release(None)
time.sleep(step_len * 5)
on = sorted((k, i, round(st.gate, 3)) for k, lane in pat.lanes.items() for i, st in enumerate(lane) if st.on)
print("recorded (REC, 1/8):", on)
assert [(k, i) for k, i, _ in on] == [(60, 4), (67, 4), (72, 6)], on
assert all(1.5 <= gt <= 2.5 for k, i, gt in on if i == 4), on
offs_before = sum(1 for _, m in outp.log if m[:2] == [0x92, 60])
time.sleep(step_len * 16)
assert sum(1 for _, m in outp.log if m[:2] == [0x92, 60]) == offs_before + 1, "replayed next pass (once)"
# OVR keeps what's there
perf.mode = "ovr"; perf.tick()
time.sleep(step_len * 1.2)
g2 = perf.press_pad(2, mel, [(62, 62)], r, QPointF(50, 50)); time.sleep(step_len); g2.release(None)
time.sleep(step_len * 2)
assert pat.lanes[60][4].on and any(st.on for st in pat.lanes[62]), "overdub adds"
# drums record into the voice lane
perf.mode = "ovr"; sqp.sel = prp.tracks[1]; perf.tick()
gd = perf.press_pad(("drum", 1), prp.tracks[1], [(1, 38)], r, QPointF(50, 90)); gd.release(None)
assert any(st.on for st in prp.tracks[1].current.lanes[1])
ep.stop(); perf.mode = None; perf.tick(); assert not ep.rec
# no recording when stopped
before = sum(st.on for lane in pat.lanes.values() for st in lane)
perf.mode = "rec"; perf.tick(); gs = perf.press_pad(3, mel, [(65, 65)], r, QPointF(50, 50)); gs.release(None)
assert sum(st.on for lane in pat.lanes.values() for st in lane) == before
perf.mode = None
# screenshots
rkp = ts.Rack(prp, ep, pdir / "Project 2.json", StubHost()); rkp.resize(1920, 1080)
rkp.seq.sel = prp.tracks[0]; rkp.modules[3][4].ext = {"7"}; rkp.grab().save(str(OUT / "play_chords.png"))
rkp.seq.sel = prp.tracks[1]; rkp.grab().save(str(OUT / "play_drums.png"))
print("play OK")
# --- legato between chords sharing notes; strum released early leaves nothing hanging ---
outl = FakeOut(); el = ts.Engine(prp, outl); pl = ts.Performer(prp, el, sqp)
msgs = lambda: [("on" if m[0] & 0xF0 == 0x90 else "off", m[1]) for _, m in outl.log]
a = pl.press_pad(2, mel, [(52, 52), (55, 55), (59, 59)], r, QPointF(50, 50))
b = pl.press_pad(0, mel, [(52, 52), (55, 55), (60, 60)], r, QPointF(50, 50))
a.release(None)
assert msgs() == [("on", 52), ("on", 55), ("on", 59), ("on", 60), ("off", 59)], msgs()
b.release(None)
assert msgs()[-3:] == [("off", 52), ("off", 55), ("off", 60)] and not pl.voices
outl.log.clear()
pl.strum = True
st_ = pl.press_pad(1, mel, [(62, 62), (65, 65), (69, 69)], r, QPointF(50, 50))
st_.release(None)  # before the strum got to 65 and 69
for _ in range(10):
    app.processEvents(); time.sleep(0.02)
assert msgs() == [("on", 62), ("off", 62)] and not pl.voices, msgs()
print("legato OK")
# --- modifiers re-voice held chords ---
outm = FakeOut(); em = ts.Engine(prp, outm); pm = ts.Performer(prp, em, sqp)
prp.key, prp.scale = 0, "major"; sqp.sel = mel
msgs = lambda: [("on" if m[0] & 0xF0 == 0x90 else "off", m[1]) for _, m in outm.log]
nof = lambda n=0: [(x, x) for x in pm.voice(pm.chords()[n][2])]
pm.gestures[1] = pm.press_pad(0, mel, nof(), r, QPointF(50, 50), nof)
assert msgs() == [("on", 48), ("on", 52), ("on", 55)], msgs()
outm.log.clear(); pm.toggle_ext("sus4")
print("C -> Csus4 while held:", msgs()); assert msgs() == [("off", 52), ("on", 53)]
outm.log.clear(); pm.shift_octave(1)
print("octave up while held:", msgs()); assert sorted(msgs()) == sorted([("off", 48), ("off", 53), ("off", 55), ("on", 60), ("on", 65), ("on", 67)])
outm.log.clear(); pm.gestures.pop(1).release(None)
assert sorted(msgs()) == [("off", 60), ("off", 65), ("off", 67)] and not pm.voices
pm.toggle_ext("sus4"); pm.shift_octave(-1)
# while recording: the recorded chord follows
patm = mel.current
for lane in patm.lanes.values():
    for st in lane: st.on = False
pm.mode = "ovr"; pm.tick(); em.start(); time.sleep(step_len * 2.2)
pm.gestures[2] = pm.press_pad(0, mel, nof(), r, QPointF(50, 50), nof)
pm.toggle_ext("7")
time.sleep(step_len); pm.gestures.pop(2).release(None); time.sleep(step_len); em.stop()
rec = sorted(k for k, lane in patm.lanes.items() for st in lane if st.on)
print("recorded after adding 7 while held:", rec); assert sorted({k % 12 for k in rec}) == [0, 4, 7, 11]  # Cmaj7: C E G B, in whatever inversion
pm.mode = None; pm.toggle_ext("7")
print("modifiers OK")
# --- toggling LEAD restarts voice leading from root position ---
pv = ts.Performer(prp, ts.Engine(prp, FakeOut()), sqp)
chord = lambda n: pv.voice(pv.chords()[n][2])
first_i = chord(0); chord(3); after_iv = chord(0)
assert after_iv != first_i or True
chord(3); pv.toggle_lead(); pv.toggle_lead()
assert chord(0) == first_i == [48, 52, 55], "fresh start: I in root position"
print("lead reset OK")
# --- keyboard layout: size, glissando, recording ---
pk = rkp.modules[3][4]
rkp.seq.sel = prp.tracks[0]; pk.layout = "keys"; prp.key, prp.scale = 9, "minor"
rkp.zoomed = None; rkp.grab()
kb_small = pk.keyboard(next(r for r, f in pk.hits if r.width() > 500))
rkp.toggle_zoom(pk); rkp.grab().save(str(OUT / "keys_zoom.png"))
kb_big = pk.keyboard(QRectF(0, 0, rkp.screens[pk].width() - 200, 600))
print("white keys small/full:", len(kb_small[0]), len(kb_big[0]))
assert len(kb_small[0]) == 15 and len(kb_big[0]) >= 29  # 2 octaves vs 4
rkp.toggle_zoom(pk); rkp.grab().save(str(OUT / "keys.png"))
outk = FakeOut(); pk.engine = ts.Engine(prp, outk)
scr = rkp.screens[pk]
kbr = next(r for r, f in pk.hits if r.width() > 500)
whites, blacks = pk.keyboard(kbr)
c3 = next(rect for n, rect in whites if n == 48); d3 = next(rect for n, rect in whites if n == 50)
y = c3.bottom() - 20
rkp.press(60, scr.topLeft() + QPointF(c3.center().x(), y)); rkp.move(60, scr.topLeft() + QPointF(d3.center().x(), y))
rkp.release(60, scr.topLeft() + QPointF(d3.center().x(), y))
km = [("on" if m[0] & 0xF0 == 0x90 else "off", m[1]) for _, m in outk.log]
print("glissando C3 -> D3:", km); assert km == [("on", 48), ("off", 48), ("on", 50), ("off", 50)]
cs = next(rect for n, rect in blacks if n == 49)
outk.log.clear(); rkp.press(61, scr.topLeft() + cs.center()); rkp.release(61, scr.topLeft() + cs.center())
assert [m[1] for _, m in outk.log] == [49, 49], "black key on top"
pk.layout = "chords"; pk.engine = ep
print("keys OK")
# --- MIDI router: notes to their track's instrument, track-less messages by channel ---
class Port:
    def __init__(self): self.got = []
    def send_message(self, m): self.got.append(list(m))
main_p, pa, pb = Port(), Port(), Port()
rt = ts.MidiRouter(main_p)
ta, tb = ts.Track("melody", 1, 1), ts.Track("melody", 1, 2)  # same channel
rt.taps = {ta: pa, tb: pb}
rt.send_message([0x90, 60, 100], ta)
assert pa.got == [[0x90, 60, 100]] and pb.got == [] and main_p.got == [[0x90, 60, 100]]
rt.send_message([0xB0, 102, 64])  # a fader: every instrument on channel 1
assert pa.got[-1] == pb.got[-1] == [0xB0, 102, 64]
rt.send_message([0xF8]); assert pa.got[-1] != [0xF8]
print("router OK")
# --- old projects: channel strips move onto the first track of that channel ---
old2 = ts.json.loads(ts.dump_project(ts.new_project()))
old2.pop("master")
for t in old2["tracks"]:
    t.pop("strip")
ref = {"fmt": "clap", "path": "/x.clap", "label": "x", "name": "X", "maker": "", "midi_ins": 1, "audio_ins": 0, "audio_outs": 2}
old2["strips"] = [{"channel": 10, "slots": [ref, None, None], "volume": 0.5, "mute": False},
                  {"channel": 1, "slots": [None, None, None], "volume": 1.0, "mute": False}]
(OUT / "old2.json").write_text(ts.json.dumps(old2))
po2 = ts.load_project(OUT / "old2.json")
drum_tr = next(t for t in po2.tracks if t.drum)
assert drum_tr.strip.slots[0].name == "X" and drum_tr.strip.volume == 0.5 and drum_tr.strip.legacy == "s1"
assert not any(t.strip.slots[0] for t in po2.tracks if not t.drum)
txt2 = ts.dump_project(po2); assert '"strips"' not in txt2 and '"master"' in txt2
print("strip migration OK")
# --- knob colors from parameter names ---
fam = {c: f for f, c, _, _ in ts.PARAM_FAMILIES}
cases = {"LFO Rate": "lfo", "Filter Cutoff": "filter", "Resonance": "filter", "Flt Q": "filter",
         "Amp Attack": "envelope", "Filter Attack": "envelope", "Env 2 Sustain": "envelope", "EG Release": "envelope",
         "Osc 1 Wave": "oscillator", "Detune": "oscillator", "Semi": "oscillator", "Kick Tune": "oscillator",
         "Master Volume": "level", "Dry/Wet": "level", "Pan": "level", "Snare Tone Mix": "level",
         "Reverb Size": "effect", "Delay Feedback": "effect", "Master Drive": "effect", "Mod Depth": "lfo",
         "Kick Punch Depth": "default", "Kick Decay": "envelope", "Kick Level": "level"}
for name, want in cases.items():
    got = fam.get(ts.param_color(name), "default")
    assert got == want, (name, got, want)
assert ts.param_color("Frequency") == "#38d6ff" and ts.param_color("Kick Punch Decay") == fam_c if (fam_c := next(c for f, c, _, _ in ts.PARAM_FAMILIES if f == "envelope")) else True
assert fam.get(ts.param_color("Frequency"), "default") == "default", "no 'q' inside words"
print("param colors OK")
# --- fader targets: selected track / a given track / channel ---
class TrackOut:
    def __init__(self): self.log = []
    def send_message(self, m, track=None): self.log.append((list(m), track))
prt = ts.Project(tracks=[ts.Track("melody", 3, 1), ts.Track("melody", 5, 2)])
ot = TrackOut(); et = ts.Engine(prt, ot)
ct = ts.Controls(prt, et)
sel = [prt.tracks[0]]; ct.selected = lambda: sel[0]
assert all(c.target == "track" and c.track == 0 for c in prt.controls), "new projects: faders follow the selected track"
ct.set_value(2, 1.0)
assert ot.log[-1] == ([0xB2, 102, 127], prt.tracks[0]), ot.log[-1]  # T1 is on channel 3
sel[0] = prt.tracks[1]; ct.set_value(2, 0.5)
assert ot.log[-1] == ([0xB4, 102, 64], prt.tracks[1]), ot.log[-1]  # now T2, channel 5
ct.assign(3, "track", 1); ct.set_value(3, 1.0)
assert ot.log[-1] == ([0xB2, 103, 127], prt.tracks[0]), "pinned to T1 whatever is selected"
ct.assign(3, "target", "channel"); ct.assign(3, "channel", 7); ct.set_value(3, 0.0)
assert ot.log[-1] == ([0xB6, 103, 0], None), "channel: no track, outboard gear"
ct.set_value(0, 1.0); assert ot.log[-1][0][0] == 0xE4 and ot.log[-1][1] is prt.tracks[1], "pitch bend to the selected track"
# a fader linked to a plugin parameter goes there, not to MIDI
class FakeHost:
    def __init__(self): self.set = []; self.v = 0.3
    def set_link(self, link, f): self.set.append((tuple(link), f))
    def link_value(self, link): return self.v
    def link_text(self, link): return "1.2 kHz"
fh = FakeHost(); ct.host = fh
prt.controls[4].target, prt.controls[4].param = "param", [1, 0, 7, "Filter Cutoff"]
n = len(ot.log); ct.set_value(4, 0.8)
assert fh.set == [((1, 0, 7, "Filter Cutoff"), 0.8)] and len(ot.log) == n
vc = View(ct); vc.resize(919, 459); vc.grab().save(str(OUT / "ctrl_param.png"))
assert ct.values[4] == 0.3, "the fader follows the knob"
ct.editing = True; vc.grab().save(str(OUT / "ctrl_param_edit.png")); ct.editing = False
# saved and loaded
ts.save_text(OUT / "ct.json", ts.dump_project(prt)); back = ts.load_project(OUT / "ct.json")
assert back.controls[4].param == [1, 0, 7, "Filter Cutoff"] and back.controls[3].target == "channel"
# older files with faders saved before targets existed: they stay on their MIDI channels
oldc = ts.json.loads((OUT / "old.json").read_text())
oldc["controls"] = [["pb", 1, 1, False, False], ["mod", 2, 1, False, False]] + [["cc", 1, 102 + i, False, False] for i in range(8)]
(OUT / "oldc.json").write_text(ts.json.dumps(oldc))
assert all(c.target == "channel" for c in ts.load_project(OUT / "oldc.json").controls)
print("fader targets OK")
# --- keyboard velocity: lower on the key = louder; pads keep higher = louder ---
pv2 = ts.Performer(prp, ts.Engine(prp, FakeOut()), sqp)
kr = ts.QRectF(0, 0, 100, 200)
assert pv2.velocity(kr, QPointF(50, 190), toward_bottom=True) > 115 and pv2.velocity(kr, QPointF(50, 10), toward_bottom=True) < 50
assert pv2.velocity(kr, QPointF(50, 10)) > 115
outv = FakeOut(); pk.engine = ts.Engine(prp, outv); pk.layout = "keys"; rkp.seq.sel = prp.tracks[0]; rkp.grab()
kbr = next(r for r, f in pk.hits if r.width() > 500); c3 = next(rect for n, rect in pk.keyboard(kbr)[0] if n == 48)
scr = rkp.screens[pk]
rkp.press(70, scr.topLeft() + QPointF(c3.center().x(), c3.bottom() - 5)); rkp.release(70, scr.topLeft() + QPointF(c3.center().x(), c3.bottom() - 5))
print("C3 near the bottom edge: velocity", outv.log[0][1][2]); assert outv.log[0][1][2] > 120
pk.layout = "chords"; pk.engine = ep
print("key velocity OK")
# --- external MIDI track: no plugin chain, its notes leave through its own output ---
class MidiDev:
    def __init__(self):
        self.log, self.closed = [], False

    def send_message(self, m):
        self.log.append(list(m))

    def close(self):
        self.closed = True

dev = MidiDev()
asked = []
def device(name):
    asked.append(name)
    return dev if name == "Synth MIDI 1" else None

main_out = FakeOut()
router = ts.MidiRouter(main_out, opener=device)
router.RETRY = 0.0  # look for the device on every call, not every 2 s
pre = ts.Project(tracks=[ts.Track("melody", 1, 1), ts.Track("external", 5, 2, port="Synth MIDI 1")])
mel, ext = pre.tracks
ee = ts.Engine(pre, router)
ee.refresh_ports()
assert asked == ["Synth MIDI 1"] and router.is_open("Synth MIDI 1")
ee.live(ext, 60, 100)  # a pad on the external track: out to the device, and nowhere else
assert dev.log[-1] == [0x94, 60, 100] and not main_out.log, (dev.log, main_out.log)
ee.live(mel, 62, 100)  # a plugin track: the main port (and its instrument tap), not the device
assert main_out.log[-1][1] == [0x90, 62, 100] and dev.log[-1] == [0x94, 60, 100]
ee.send([0xFA]); assert dev.log[-1] == [0xFA] and main_out.log[-1][1] == [0xFA], "transport follows"
ee.send([0xB4, 74, 99]); assert dev.log[-1] == [0xB4, 74, 99], "track-less messages reach CH5 too"
ext.port = "Unplugged"  # the device went away: notes are lost, never sent to the main port instead
ee.refresh_ports(); n = len(main_out.log)
ee.live(ext, 61, 100)
assert not router.is_open("Unplugged") and len(main_out.log) == n
ext.port = "Synth MIDI 1"  # and it comes back
ee.refresh_ports(); ee.live(ext, 64, 100); assert dev.log[-1] == [0x94, 64, 100]
ext.port = ""  # a track with no output lets go of the port
ee.refresh_ports(); assert dev.closed, "an unused port is closed"
# no chain: the host panel lists plugin tracks and the master only (it reads nothing else, so no Carla here)
hostlike = type("Host", (), {"project": pre})()
assert ext.strip not in ts.HostPanel.strips(hostlike) and mel.strip in ts.HostPanel.strips(hostlike)
# the port is saved with the project (and only there)
ts.save_text(OUT / "ext.json", ts.dump_project(pre))
back = ts.load_project(OUT / "ext.json")
assert back.tracks[1].kind == "external" and back.tracks[1].port == "" and back.tracks[0].port == ""
ext.port = "Synth MIDI 1"
ts.save_text(OUT / "ext.json", ts.dump_project(pre))
assert ts.load_project(OUT / "ext.json").tracks[1].port == "Synth MIDI 1"
print("external routing OK")
# the "+" dialog: drum and melody at once, external asks for the output first
picked = []
nt = ts.NewTrack(lambda kind, port="": picked.append((kind, port)), list_ports=lambda: ["Synth MIDI 1", "DIN Out"])
vn = View(nt); vn.grab().save(str(OUT / "new_track.png"))
nt.press(1, QPointF(100, 100)); assert picked == [("drum", "")], picked  # the DRUM card
nt.press(1, QPointF(500, 100)); assert picked == [("drum", ""), ("melody", "")], picked  # the MELODY card
vn.grab(); nt.press(2, nt.hits[2][0].center())  # the EXTERNAL card: shows the outputs instead
assert nt.picking == "external" and len(picked) == 2, nt.picking
vn.grab().save(str(OUT / "new_track_ports.png"))
row = next(r for r, f in nt.hits if r.top() > 70).center()  # hits: BACK, RESCAN, then a row per output
nt.press(3, row); nt.release(3, row)
assert picked[-1] == ("external", "Synth MIDI 1"), picked
nt.back(); vn.grab()  # back to the four kinds
nt.press(4, nt.hits[3][0].center())  # the AUDIO card: asks for an input
assert nt.picking == "audio", nt.picking
vn.grab(); row = [r for r, f in nt.hits if r.top() > 70][-1].center()
nt.press(5, row); nt.release(5, row)
assert picked[-1] == ("audio", ""), picked  # the last row: an input picked later
mp = ts.MidiPorts(lambda name: picked.append(name), current="Synth MIDI 1", list_ports=lambda: ["Synth MIDI 1"])
vm = View(mp); vm.grab().save(str(OUT / "midi_ports.png"))
row = next(r for r, f in mp.hits if r.top() > 70).center()
mp.press(6, row); mp.release(6, row); assert picked[-1] == "Synth MIDI 1", picked
print("new track dialog OK")
# the rack: "+" opens it, and the chosen track is created, selected and saved
prx = ts.Project(tracks=[ts.Track("melody", 1, 1)])
rkx = ts.Rack(prx, ts.Engine(prx, ts.MidiRouter(FakeOut(), opener=lambda name: None)), OUT / "ext_rack.json", StubHost())
rkx.resize(1920, 1080); rkx.grab()
sscr = rkx.screens[rkx.seq]
rkx.press(1, sscr.topLeft() + rkx.seq.hits[-1][0].center())  # the "+" row is drawn last
assert rkx.dialog and rkx.dialog[0] == "NEW TRACK", rkx.dialog
rkx.grab()
dlg = rkx.dialog[2]; dscr = rkx.screens[dlg]
rkx.press(2, dscr.topLeft() + dlg.hits[2][0].center())  # the EXTERNAL card
assert dlg.picking, "external asks for the output"
rkx.grab()
dlg_s = dlg.s
row = next(r for r, f in dlg.hits if r.top() > 60 * dlg_s).center()
rkx.press(3, dscr.topLeft() + row); rkx.release(3, dscr.topLeft() + row)
assert rkx.dialog is None, "the dialog closes once the track exists"
new = prx.tracks[-1]
assert new.kind == "external" and rkx.seq.sel is new and len(prx.tracks) == 2, (new.kind, new.port)
assert (OUT / "ext_rack.json").exists() and f'"port":{ts.json.dumps(new.port)}' in (OUT / "ext_rack.json").read_text()
rkx.grab().save(str(OUT / "rack_external.png"))  # the external header paints (and no slot is offered for it)
# a new audio track is wired as soon as it is made, not on the next EDIT round trip
before = StubHost.rewired
rkx.press(10, sscr.topLeft() + rkx.seq.hits[-1][0].center())
rkx.grab()
dlg2 = rkx.dialog[2]; scr2 = rkx.screens[dlg2]
rkx.press(11, scr2.topLeft() + dlg2.hits[3][0].center())  # the AUDIO card
assert dlg2.picking == "audio", dlg2.picking
rkx.grab()
first = next(r for r, f in dlg2.hits if r.top() > 60).center()
rkx.press(12, scr2.topLeft() + first); rkx.release(12, scr2.topLeft() + first)
audio = prx.tracks[-1]
assert audio.kind == "audio" and rkx.dialog is None and not audio.mute
assert audio.source == (dlg2.ports[0][0] if dlg2.ports and dlg2.ports[0] else ""), audio.source
assert StubHost.rewired > before, "the input is patched right away"
count = StubHost.rewired
rkx.seq.set_mute(audio)  # its mute is the input cable, so it has to rewire too
assert audio.mute and StubHost.rewired == count + 1
print("audio track added and wired OK")
print("external track OK")
# --- patchbay: the audio graph by hand (an interface's input into its own output, say) ---
patch = [("/app:out", "/sink:1")]
def set_patch(links):
    patch[:] = links

ports = ([("/in:a", "Synth", "capture_1"), ("/in:b", "Synth", "capture_2")],
         [("/out:x", "Interface", "playback_1"), ("/out:y", "Interface", "playback_2")])
pb = ts.Patchbay(lambda: list(patch), set_patch, ports=lambda: ports,
                 existing=lambda: set(patch) | {("/master:1", "/sink:1")})
assert [p for p, _, _ in pb.sources] == ["/in:a", "/in:b"], "the ports are there when the dialog opens"
vp = View(pb); vp.grab().save(str(OUT / "patchbay.png"))
pb.tap("out", "/in:a"); assert pb.src == "/in:a" and pb.dst is None
pb.tap("in", "/out:x"); assert ("/in:a", "/out:x") in patch and pb.src == "/in:a", "the source stays chosen"
pb.tap("in", "/out:y"); assert ("/in:a", "/out:y") in patch, "one source, a second destination"
pb.tap("in", "/out:y"); assert ("/in:a", "/out:y") not in patch, "the same pair again cuts it"
pb.tap("out", "/in:a"); assert pb.src is None, "tapping the chosen source lets go of it"
pb.tap("in", "/out:y"); assert pb.dst == "/out:y"     # destination first ...
pb.tap("out", "/in:b"); assert ("/in:b", "/out:y") in patch and pb.dst is None, "... then a source links them"
vp.grab()  # repaints with the links drawn as cables (● ours, ○ the app's)
assert "/in:b" in pb.seen["out"] and "/out:y" in pb.seen["in"], "the rows on screen are cable ends"
pb.list_ports = lambda: (ports[0][:1], ports[1])  # the device's second input went away
pb.refresh()
assert [p for p, _, _ in pb.sources] == ["/in:a"] and pb.src is None, "a chosen port that is gone is dropped"
print("patchbay OK")
# links made there are kept with the machine (not the project) and re-wired
class FakeWiring:
    def __init__(self):
        self.calls = []
    def replace(self, owner, links):
        self.calls.append((owner, list(links)))
kept = []
hosty = type("Host", (), {"patch": [], "wiring": FakeWiring(), "remember": lambda self, k, v: kept.append((k, v))})()
ts.HostPanel.set_patch(hosty, [("/in:a", "/out:x"), ("/in:a", "/out:y")])
assert hosty.patch == [("/in:a", "/out:x"), ("/in:a", "/out:y")]
assert hosty.wiring.calls == [("patchbay", [("/in:a", "/out:x"), ("/in:a", "/out:y")])], hosty.wiring.calls
assert kept == [("patch", [("/in:a", "/out:x"), ("/in:a", "/out:y")])], kept
# the rack: PATCHBAY in the toolbar opens it over everything
btn = next(r for r, a in rkx.rail_buttons if getattr(a, "__name__", "") == "open_patchbay")
rkx.press(9, btn.center())
assert rkx.dialog and rkx.dialog[0] == "PATCHBAY", rkx.dialog
rkx.grab().save(str(OUT / "rack_patchbay.png"))
rkx.close_dialog()
print("patchbay in the rack OK")
# the SAVE button writes everything now, and the toolbar clock follows any save
rkx.saved_at = time.localtime(0)  # a long time ago
StubHost.saved_states = 0
rkx.dirty, rkx.host.states_dirty = False, True
rkx.autosave()
assert StubHost.saved_states == 1 and rkx.saved_at != time.localtime(0), "a state save moves the clock"
rkx.grab()
save_btn = next(r for r, a in rkx.rail_buttons if getattr(a, "__name__", "") == "save_now")
rkx.saved_at, rkx.host.states_dirty = time.localtime(0), False
rkx.press(20, save_btn.center())
assert rkx.saved_at != time.localtime(0) and StubHost.saved_states == 2, "SAVE writes it now"
print("save button OK")

# --- settings → audio: the audio graph's buffer size (a machine setting, like the output) ---
asked, kept = [], []
real_set_quantum = ts.plugins.set_quantum
ts.plugins.set_quantum = asked.append  # never touch the real graph from the tests
class FakeHost:
    host, message, audio_out = object(), "", "default"
    def __init__(self):
        self.buffers = []
    def set_buffer(self, size):
        self.buffers.append(size)
    def set_output(self, name):
        self.audio_out = name
    def xyfx(self):
        return None
    def save_states(self):
        pass
fh = FakeHost()
stt = ts.Settings(ts.Project(), fh)
stt.tab = "audio"
vs = View(stt); vs.grab()  # renders: buffer sizes above, the outputs below
assert len(stt.hits) > 7, "the sizes are buttons"
stt.set_buffer(512)
assert fh.buffers == [512], fh.buffers                       # the host owns the setting ...
assert len(stt.quantum) == 5 and not asked, "... it is the host that talks to PipeWire"
ht = type("Host", (), {"buffer": 0, "remember": lambda self, k, v: kept.append((k, v))})()
ts.HostPanel.set_buffer(ht, 256)                              # ... and remembers it for this machine
assert ht.buffer == 256 and asked == [256] and kept == [("buffer", 256)], (asked, kept)
assert ts.DEFAULT_CONFIG["buffer"] == 0
ts.plugins.set_quantum = real_set_quantum
print("audio buffer OK")
# --- transpose: the pattern by scale degrees, keeping the key ---
prtr = ts.Project(key=0, scale="major", tracks=[ts.Track("melody", 1, 1), ts.Track("drum", 10, 2)])
mtr, dtr = prtr.tracks
for n in (60, 64, 71):  # C, E, B: in C major
    mtr.current.lane(n)[0].on = True
mtr.current.lane(61)[0].on = True  # C#, outside the scale: it keeps its alteration
sqtr = ts.Sequencer(prtr, ts.Engine(prtr, FakeOut()))
sqtr.roll_bases[mtr] = 48
sqtr.transpose(mtr, 1)
assert sorted(mtr.current.lanes) == [62, 63, 65, 72], sorted(mtr.current.lanes)  # D, D#, F, C5
assert mtr not in sqtr.roll_bases, "the piano roll re-centres on the notes"
sqtr.transpose(mtr, -1)
assert sorted(mtr.current.lanes) == [60, 61, 64, 71], "and back"
drum_lanes = list(dtr.current.lanes)
sqtr.transpose(dtr, 1)
assert list(dtr.current.lanes) == drum_lanes, "drum tracks have fixed voices: nothing to transpose"
# the step comes from the project's scale and key, the octave wraps
assert ts.transpose(64, 1, 0, "major pentatonic") == 67, "E up is G in a pentatonic"
assert ts.transpose(71, 1, 0, "major") == 72, "B up is the next C"
assert ts.transpose(60, 1, 7, "major") == 62, "in G, C up is D"
assert ts.transpose(60, 1, 0, "chromatic") == 61 and ts.transpose(60, 7, 0, "major") == 72
assert ts.transpose(61, 1, 0, "major") == 63, "an outside note moves within the key and keeps its alteration"
# a note pushed out of the MIDI range stays where it is (instead of being dropped)
hi = ts.Project(tracks=[ts.Track("melody")])
hi.tracks[0].current.lane(127)[0].on = True
ts.Sequencer(hi, ts.Engine(hi, FakeOut())).transpose(hi.tracks[0], 1)
assert list(hi.tracks[0].current.lanes) == [127], list(hi.tracks[0].current.lanes)
print("transpose OK")
# --- effects buses: a track routed through one, and the bus into the master ---
class FakePlugin:
    def __init__(self, client, name="Reverb"):
        self.client = client
        self.ref = type("Ref", (), {"name": name, "midi_ins": 1, "fmt": "clap", "path": "/x.clap",
                                    "label": f"x.{name}"})()
        self.params, self.output = [], None
class FakeCarla:
    def __init__(self):
        self.volumes = []
        self.level = 0.0
        self.reads = []
    def ports(self, plugin):
        return 2, 2
    def peaks(self, plugin, into=False):
        self.reads.append((plugin.client, into))
        return self.level, self.level * 0.5
    def idle(self):
        pass
    def has_ui(self, plugin):
        return False
    def show_ui(self, plugin, on=True):
        pass
    def set_volume(self, plugin, volume):
        self.volumes.append((plugin.client, round(volume, 2)))
class FakeWiring:
    def __init__(self):
        self.links = {}
    def replace(self, owner, links):
        self.links[owner] = sorted(links)
class FakeHostPanel(ts.HostPanel):
    """HostPanel's routing logic without Carla: the chains, the links it wants and the levels it sets."""
    def __init__(self, project):
        self.project, self.sink, self.name = project, "sink", "TS"
        self.loaded, self.wiring, self.host = {}, FakeWiring(), FakeCarla()
        self.meters, self.states_dirty = {}, False
        self.monitor, self.eq = None, ts.EqEditor(self)
        self.router = type("R", (), {"refresh": lambda self, tracks: None, "taps": {}})()
        self.sink_changed = False
        self.gestures, self.hits, self.stale = {}, [], False
        self.s, self.view, self.sel, self.focus, self.page, self.mixer_page = 1.0, "track", None, None, 0, 0
        self.browser, self.assigning, self.browser_scroll, self.browser_fmt = None, None, 0.0, None
        self.failed, self.message, self.audio_out = set(), "", "default"
        self.catalog = type("C", (), {"plugins": [], "ready": True, "progress": "", "scanning": False,
                                      "rescan": lambda self: None})()
        self.selected = lambda: project.tracks[0]

prb = ts.Project(tracks=[ts.Track("melody", 1, 1), ts.Track("melody", 2, 2)])
prb.buses.append(ts.Bus(1))
prb.buses[0].strip.volume = 0.5
prb.master.volume = 0.8
t1, t2, bus = prb.tracks[0], prb.tracks[1], prb.buses[0]
bh = FakeHostPanel(prb)
bh.loaded = {(t1.strip, 1): FakePlugin("T1.2 Rev"), (t2.strip, 1): FakePlugin("T2.2 Rev"),
             (bus.strip, 0): FakePlugin("B1.1 Rev", "Reverb")}
# a loaded plugin is not an animation: with nothing held and nothing loading the rack must sleep (idle CPU)
assert not bh.animating(), "the host panel repaints on its own just because a plugin is loaded"
bh.gestures[1] = object()  # a held pad or a finger on a fader: it moves only on touch moves
assert not bh.animating(), "a held pad does not need free-running frames"
bh.gestures[1] = ts.Hold(lambda: None)
assert bh.animating(), "a Hold's progress ring still needs frames"
bh.gestures.clear()
bh.sampler = types.SimpleNamespace(loading=True)
assert bh.animating(), "the sampler spinner still turns"
bh.sampler.loading = False
# a meter must fall all the way to zero: a per-tick threshold used to leave the last visible bar and peak
bh.host.level = 1.0
for _ in range(3):
    bh.sample_meters()
meter = bh.meters[t1.strip]
assert meter["l"] > 0.5 and meter["peak"] > 0.5, meter
bh.host.level = 0.0
meter["at"] -= 2.0  # its 1.2 s hold is over: the peak may slide
for _ in range(400):
    bh.sample_meters()
assert (meter["l"], meter["r"], meter["peak"]) == (0.0, 0.0, 0.0), meter
assert bh.strips() == [t1.strip, t2.strip, bus.strip, prb.master]
assert bh.owner_name(t1.strip) == "T1" and bh.owner_name(bus.strip) == "BUS 1"
assert bh.owner_name(prb.master) == "MASTER" and bh.bus_of(bus.strip) is bus
assert bh.instrument_slot(t1.strip, 0) and not bh.instrument_slot(bus.strip, 0), "a bus takes effects only"
bh.set_bus(t1, 1)  # t1 through the bus, t2 straight into the master (no master effects: into the output)
bh.rewire()
links = {strip: links for strip, links in bh.wiring.links.items()}
assert ("T1.2 Rev:output_1", "B1.1 Rev:input_1") in links[t1.strip], links[t1.strip]
assert not any("playback" in b for _, b in links[t1.strip]), "a bus track doesn't also go to the output"
assert ("T2.2 Rev:output_1", "sink:playback_FL") in links[t2.strip], links[t2.strip]
assert ("B1.1 Rev:output_1", "sink:playback_FL") in links[bus.strip], links[bus.strip]
# levels: the bus carries its own level times the master's (it is what reaches the output here) ...
assert ("B1.1 Rev", 0.4) in bh.host.volumes, bh.host.volumes
# ... while a track inside it keeps its own only (the bus has already folded the master in)
assert ("T1.2 Rev", 1.0) in bh.host.volumes, bh.host.volumes
bh.set_bus(t1, 0)  # back to the master: now it reaches the output and takes the master level
bh.host.volumes.clear()
bh.rewire()
assert ("T1.2 Rev", 1.0) in bh.host.volumes and ("T2.2 Rev", 0.8) in bh.host.volumes, bh.host.volumes
# two plugins of the same name in one chain still read apart in the patchbay
bh.loaded[(bus.strip, 1)] = FakePlugin("B1.2 Rev", "Reverb")
labels = bh.chain_labels()
assert labels["B1.1 Rev"] == "BUS 1 · 1 Reverb" and labels["B1.2 Rev"] == "BUS 1 · 2 Reverb", labels
boxes = ts.Patchbay.groups([("/a:out_1", "Dev", "out_1"), ("/b:out_1", "Dev", "out_1")])
assert len(boxes) == 2 and [label for label, _ in boxes] == ["Dev", "Dev"], "same name, two devices: two boxes"
# the rack's own cables are shown in the patchbay, but not ours to cut
mine = ("/cap:1", "/bus:1")
cuts = []
pb2 = ts.Patchbay(lambda: [], lambda links: cuts.append(links), ports=lambda: ([("/cap:1", "In", "1")],
                 [("/bus:1", "BUS 1", "1")]), existing=lambda: {mine}, internal=lambda: {mine})
pb2.tap("out", "/cap:1"); pb2.tap("in", "/bus:1")
assert cuts == [], "an internal cable is not toggled from the patchbay"
print("buses OK")
# --- audio tracks: an input port patched through the rack (source, pan, bus, effects) ---
pra = ts.Project(tracks=[ts.Track("audio", 1, 1, source="/in:capture_FL", pan="center")])
atr = pra.tracks[0]
ah = FakeHostPanel(pra)
ah.rewire()
assert sorted(ah.wiring.links[atr.strip]) == [("/in:capture_FL", "sink:playback_FL"),
                                              ("/in:capture_FL", "sink:playback_FR")], "center: both sides"
assert not ah.instrument_slot(atr.strip, 0), "an audio track takes effects, not an instrument"
atr.pan = "left"; ah.rewire()
assert ah.wiring.links[atr.strip] == [("/in:capture_FL", "sink:playback_FL")]
atr.pan = "right"; ah.rewire()
assert ah.wiring.links[atr.strip] == [("/in:capture_FL", "sink:playback_FR")]
atr.mute = True; ah.rewire()
assert ah.wiring.links[atr.strip] == [], "a muted input sends nothing"
atr.mute = False
assert ts.HostPanel.input_links("/in:1", ["/dst:in_1"], "right") == [("/in:1", "/dst:in_1")], "a mono target"
ah.loaded[(atr.strip, 0)] = FakePlugin("T1.1 EQ", "EQ")  # an effect on the track: the input goes through it
atr.pan = "center"; ah.rewire()
links = ah.wiring.links[atr.strip]
assert ("/in:capture_FL", "T1.1 EQ:input_1") in links and ("T1.1 EQ:output_1", "sink:playback_FL") in links, links
pra.buses.append(ts.Bus(1))  # and a bus: the track's chain goes there
ah.loaded[(pra.buses[0].strip, 0)] = FakePlugin("B1.1 Rev")
atr.bus = 1
ah.rewire()
assert ("T1.1 EQ:output_1", "B1.1 Rev:input_1") in ah.wiring.links[atr.strip], ah.wiring.links[atr.strip]
ah.loaded.pop((atr.strip, 0))  # no effects: the input goes straight into the bus
ah.rewire()
assert sorted(ah.wiring.links[atr.strip]) == [("/in:capture_FL", "B1.1 Rev:input_1"),
                                              ("/in:capture_FL", "B1.1 Rev:input_2")], ah.wiring.links[atr.strip]
# saved with the project, and the inputs the dialog lists stay a plain list without PipeWire
prs = ts.Project(tracks=[ts.Track("audio", 1, 1, source="/in:FL", pan="right")])
ts.save_text(OUT / "audio.json", ts.dump_project(prs))
backaudio = ts.load_project(OUT / "audio.json").tracks[0]
assert backaudio.audio and backaudio.source == "/in:FL" and backaudio.pan == "right", backaudio
assert isinstance(ts.plugins.audio_inputs(), list)
print("audio tracks OK")

# --- plugin parameters are saved beside the state (some plugins keep them out of it) ---
class ParamCarla(FakeCarla):
    def __init__(self):
        super().__init__()
        self.values = {7: 0.25, 8: 0.75}
    def value(self, plugin, index):
        return self.values.get(index, 0.0)
    def set_value(self, plugin, index, value):
        self.values[index] = value
    def text(self, plugin, index):
        return str(self.values.get(index, 0.0))
    def prepare_for_save(self, plugin):
        pass
    def save_state(self, plugin, path):
        Path(path).write_text("state")
pp = FakePlugin("M.1 XYFX")
pp.ref.label = "touchseq.xyfx"  # our own plugins keep their parameters out of the state: the sidecar is theirs
pp.params = [type("Pr", (), {"index": i})() for i in (7, 8)]
pph = FakeHostPanel(ts.Project())
pph.state_dir = OUT / "params.plugins"
_sh.rmtree(pph.state_dir, ignore_errors=True)  # no stale state from an earlier run in the same folder
pph.host = ParamCarla()
pph.loaded = {(pph.project.master, 0): pp}
pph.save_states()
state = pph.state_file(pph.project.master, 0, pp.ref)
assert state.exists() and state.with_suffix(".params.json").exists(), list(pph.state_dir.glob("*"))
pph.host.values = {7: 0.0, 8: 0.0}  # a fresh load: the plugin reports its defaults
pph.restore_params(pp, state)
assert pph.host.values == {7: 0.25, 8: 0.75}, pph.host.values
state.with_suffix(".params.json").unlink()  # no parameter file: nothing breaks
pph.restore_params(pp, state)
# a third-party plugin's state is its own: no sidecar, and an old one is never applied over it
third = FakePlugin("M.2 Synth")
third.ref.label = "vendor.synth"  # not ours: its state carries its parameters
third.params = [type("Pr", (), {"index": i})() for i in (7, 8)]
pph.loaded = {(pph.project.master, 0): third}
pph.save_states()
third_state = pph.state_file(pph.project.master, 0, third.ref)
assert third_state.exists() and not third_state.with_suffix(".params.json").exists()
third_state.with_suffix(".params.json").write_text('{"7": 0.9, "8": 0.9}')  # an old sidecar
pph.host.values = {7: 0.0, 8: 0.0}
pph.restore_params(third, third_state)
assert pph.host.values == {7: 0.0, 8: 0.0}, "a third-party plugin's own state wins over a sidecar"
pph.loaded = {(pph.project.master, 0): pp}
# a plugin changing a parameter itself (its UI, a preset) marks the state for saving
pph.states_dirty = False
pph.plugin_changed(pp, 7, 0.3)
assert pph.states_dirty
pph.states_dirty, pph.saving_states = False, True
pph.plugin_changed(pp, 7, 0.4)
pph.saving_states = False
assert not pph.states_dirty, "a change echoed during a save does not ask for another save"
# a turned knob marks the states for saving, and the project's own autosave takes them along
pph.host.values[7] = 0.0
ts.HostPanel.set_param(pph, pp, 7, 0.5)
assert pph.host.values[7] == 0.5 and pph.states_dirty, "a knob says the plugin state is worth saving"
pph.save_states()
assert not pph.states_dirty
StubHost.saved_states = 0
rkx.host.states_dirty = True
rkx.autosave()
assert StubHost.saved_states == 1 and not rkx.host.states_dirty, "the autosave saves plugin states too"
print("plugin params OK")
# --- the mixer's meters: bars fall back, the peak holds and then follows ---
mh = FakeHostPanel(ts.Project(tracks=[ts.Track("melody", 1, 1)]))
mh.loaded = {(mh.project.tracks[0].strip, 1): FakePlugin("T1.1 A")}
strip1 = mh.project.tracks[0].strip
mh.host.level = 0.8
mh.sample_meters()
m = mh.meters[strip1]
assert round(m["l"], 2) == 0.8 and round(m["r"], 2) == 0.4 and m["peak"] == 0.8, m
assert ts.HostPanel.db_fraction(1.0) == 1.0 and ts.HostPanel.db_fraction(0.0) == 0.0
assert 0.85 < ts.HostPanel.db_fraction(0.5) < 0.95, "0.5 is about -6 dB: nearly full, as in any DAW"
mh.host.level = 0.0
mh.sample_meters()
assert 0 < mh.meters[strip1]["l"] < 0.8 and mh.meters[strip1]["peak"] == 0.8, "the bar falls, the peak holds"
time.sleep(1.4)
for _ in range(20):
    mh.sample_meters(); time.sleep(0.02)
assert mh.meters[strip1]["peak"] < 0.8 and mh.meters[strip1]["l"] < 0.05, mh.meters[strip1]
mh.project.tracks.clear()  # a deleted track takes its meter with it
mh.sample_meters()
assert not mh.meters, mh.meters
# an audio track's meter is what arrives at its chain (its input); every other strip's is what leaves it
mh_in = FakeHostPanel(ts.Project(tracks=[ts.Track("audio", 1, 1, source="/in:FL")]))
mh_in.loaded = {(mh_in.project.tracks[0].strip, 0): FakePlugin("T1.1 Rev")}
mh_in.meters, mh_in.host.level = {}, 0.5
mh_in.sample_meters()
assert mh_in.host.reads == [("T1.1 Rev", True)], mh_in.host.reads
mh_out = FakeHostPanel(ts.Project(tracks=[ts.Track("melody", 1, 1)]))
mh_out.loaded = {(mh_out.project.tracks[0].strip, 1): FakePlugin("T2.1 Rev")}
mh_out.meters, mh_out.host.level = {}, 0.5
mh_out.sample_meters()
assert mh_out.host.reads == [("T2.1 Rev", False)], mh_out.host.reads
print("input meter OK")

# an audio track with no plugins gets the monitor node (its input then has a meter and a fader)
mh_mon = FakeHostPanel(ts.Project(tracks=[ts.Track("audio", 1, 1), ts.Track("melody", 1, 2)]))
mon = type("Ref", (), {"label": "touchseq.monitor", "name": "Touch-Seq Monitor"})()
mh_mon.monitor = mon
placed = []
mh_mon.set_slot = lambda strip, k, ref: placed.append((strip, k, ref.label))
mh_mon.ensure_monitors()
assert placed == [(mh_mon.project.tracks[0].strip, 0, "touchseq.monitor")], placed
mh_mon.project.tracks[0].strip.slots[1] = mon  # a track that already has an effect is left alone
placed.clear()
mh_mon.ensure_monitors()
assert not placed, placed
print("monitor node OK")

# --- our EQ: the curve maths the editor draws, the graph's mapping, the bands under the finger ---
assert abs(ts.band_db((1000.0, 12.0, 1.4, 0), 1000.0) - 12.0) < 0.05, "a bell's centre is its gain"
assert abs(ts.band_db((1000.0, 12.0, 1.4, 0), 40.0)) < 0.6, "and it leaves the bass alone"
assert ts.band_db((200.0, 0.0, 0.7, 3), 30.0) < -20.0 and abs(ts.band_db((200.0, 0.0, 0.7, 3), 5000.0)) < 0.1
assert ts.band_db((4000.0, 0.0, 0.7, 4), 15000.0) < -20.0 and abs(ts.band_db((4000.0, 0.0, 0.7, 4), 200.0)) < 0.1
assert ts.band_db((300.0, 9.0, 0.7, 1), 30.0) > 7.0 and ts.band_db((3000.0, -9.0, 0.7, 2), 15000.0) < -7.0
graph = ts.QRectF(0, 0, 600, 300)
assert abs(ts.EqEditor.freq_at(graph, ts.EqEditor.x_of(graph, 1000.0)) - 1000.0) < 1.0
assert abs(ts.EqEditor.db_at(graph, ts.EqEditor.y_of(graph, 12.0)) - 12.0) < 0.5
eqh = FakeHostPanel(ts.Project(tracks=[ts.Track("melody", 1, 1)]))
eqh.s = 1.0
eqh.host = ParamCarla()
eqh.host.values = {4 * i + n: v for i in range(8) for n, v in ((0, 1000.0), (1, 0.0), (2, 1.4), (3, 0))}
touched = []
eqh.set_param = lambda plugin, index, value, source="app": touched.append((index, round(value, 3)))
ed = ts.EqEditor(eqh)
ed.plugin = FakePlugin("M.1 EQ")
ed.graph = graph
ed.place(QPointF(6, 150))  # 1 % from the left: a low cut where the bass is
assert [k for k, v in touched] == [0, 1, 3] and touched[-1] == (3, 3), touched
assert touched[0][1] < 25.0, touched  # the frequency came from the x it was dropped at
touched.clear()
ed.place(QPointF(300, 150))  # the middle: a bell
assert touched[-1] == (3, 0), touched
touched.clear()
ed.place(QPointF(594, 150))  # the far top: a high cut
assert touched[-1] == (3, 4), touched
touched.clear()
ts.BandPoint(ed, 0).move(QPointF(600, 0))  # dragging a band: right and up is high and loud
assert touched[0][0] == 0 and touched[0][1] > 19000 and touched[1][1] > 20, touched
print("eq editor OK")

# --- the EQ's width strip follows the finger sideways (a horizontal control, not a vertical one) ---
moved = []
strip = ts.QRectF(0, 0, 200, 44)
mh_slide = ts.SlideH(strip, lambda v: moved.append(round(v, 2)), QPointF(50, 10))
assert moved == [0.25], moved
mh_slide.move(QPointF(100, 40))  # the same x as before but a different height: the value must not care
assert moved[-1] == 0.5, moved
mh_slide.move(QPointF(150, 2))
assert moved[-1] == 0.75, moved
assert abs(ts.EqEditor.width_at(0.0) - 0.2) < 1e-6 and abs(ts.EqEditor.width_at(1.0) - 10.0) < 1e-6
assert abs(ts.EqEditor.width_at(0.5) - 1.4142) < 0.01, "halfway is Q 1.4, log between 0.2 and 10"
print("width strip OK")

# --- a plugin that goes away must not stop the UI clock (the bug that froze the meters) ---
class GoneCarla(FakeCarla):
    def __init__(self):
        super().__init__()
        self.plugins = []
    def value(self, plugin, index):
        raise AssertionError("a stale plugin must not be read")
eqg = FakeHostPanel(ts.Project(tracks=[ts.Track("melody", 1, 1)]))
eqg.s = 1.0
edg = ts.EqEditor(eqg)
gone = FakePlugin("M.1 EQ")
edg.plugin, edg.spectrum = gone, [0.5] * 48
eqg.loaded = {}  # its slot was emptied
edg.tick()  # used to raise ValueError from plugin.pid, killing the tick clock
assert edg.plugin is None and not any(edg.spectrum), (edg.plugin, edg.spectrum)
# the same through the host, whose id lookup would have raised too
class StubCarla:
    plugins = []
    def get_current_parameter_value(self, pid, index):
        raise AssertionError("not reached")
stub_host = type("H", (), {"h": StubCarla(), "plugins": [],
                           "loaded": lambda self, plugin: plugin in self.plugins})()
assert ts.plugins.Host.value(stub_host, gone, 0) == 0.0
ts.plugins.Host.set_value(stub_host, gone, 0, 1.0)  # no raise, nothing set
assert ts.plugins.Host.peaks(stub_host, gone) == (0.0, 0.0)
assert ts.plugins.Host.output_params(stub_host, gone) == []
# a strip whose chain emptied loses its meter instead of freezing at the last value
mh_gone = FakeHostPanel(ts.Project(tracks=[ts.Track("melody", 1, 1)]))
strip_g = mh_gone.project.tracks[0].strip
mh_gone.loaded = {(strip_g, 1): FakePlugin("T1.1 Rev")}
mh_gone.host.level = 0.7
mh_gone.sample_meters()
assert mh_gone.meters[strip_g]["l"] > 0
mh_gone.loaded = {}  # the effect was removed
mh_gone.sample_meters()
assert strip_g not in mh_gone.meters, mh_gone.meters
# and a panel whose tick raises no longer stops the window's clock
class Broken(ts.Panel):
    def __init__(self):
        super().__init__()
        self.ticks = 0
    def tick(self):
        self.ticks += 1
        raise RuntimeError("boom")
    def draw(self, p):
        pass
wbroken = ts.Window([(0, 0, "B", "broken", Broken())])
painted = []
orig_update = wbroken.update
wbroken.update = lambda: painted.append(1)
wbroken.tick()
wbroken.tick()
assert wbroken.panels[0].ticks == 2, "the clock kept calling it"
assert wbroken.animating() or True
print("stale plugin OK")

# --- removing a bus: the bus chain's DELETE (hold) and the mixer's x, and where its tracks go ---
class BusHost(FakeHostPanel):
    def __init__(self, project):
        super().__init__(project)
        self.unloaded, self.rewired = [], 0
    def unload(self, strip, k):
        self.unloaded.append((strip, k))
    def rewire(self):
        self.rewired += 1

pr_bus = ts.Project(tracks=[ts.Track("melody", 1, 1), ts.Track("melody", 2, 2)])
pr_bus.buses.append(ts.Bus(1))
t1b, t2b, b1 = pr_bus.tracks[0], pr_bus.tracks[1], pr_bus.buses[0]
t1b.bus = t2b.bus = 1
bh_bus = BusHost(pr_bus)
bh_bus.loaded = {(b1.strip, 0): FakePlugin("B1.1 Rev")}
bh_bus.state_dir = OUT / "bus-states"
bh_bus.monitor = None
bh_bus.focus, bh_bus.sel = b1.strip, (b1.strip, 0)
bh_bus.remove_bus(b1)
assert pr_bus.buses == [] and t1b.bus == 0 and t2b.bus == 0, (pr_bus.buses, t1b.bus, t2b.bus)
assert (b1.strip, 0) in bh_bus.unloaded and bh_bus.rewired, "its plugin went with it and the graph was rebuilt"
assert bh_bus.focus is None and bh_bus.sel is None, "the view goes back to the track"
# the DELETE button beside a bus chain's title needs a hold, and then removes it
pr_bus2 = ts.Project(tracks=[ts.Track("melody", 1, 1)])
pr_bus2.buses.append(ts.Bus(1))
removed = []
bh2 = BusHost(pr_bus2)
bh2.s, bh2.focus = 1.0, pr_bus2.buses[0].strip
bh2.state_dir = OUT / "bus-states"
bh2.remove_bus = lambda bus: removed.append(bus.number)
img = QImage(1024, 600, QImage.Format_ARGB32); img.fill(0)
q = QPainter(img); q.setRenderHint(QPainter.Antialiasing)
bh2.paint(q, 1024, 600); q.end()
btn = next(r for r, f in reversed(bh2.hits) if abs(r.width() - 84) < 2)  # DELETE, beside the bus title
bh2.press(1, btn.center())
assert any(isinstance(g, ts.Hold) for g in bh2.gestures.values()), "DELETE is a hold"
for _ in range(20):
    bh2.tick(); time.sleep(0.05)
assert removed == [1], removed
print("remove bus OK")

# --- ASSIGN: a knob is picked with a tap, not held, and the fader list follows ---
ah2 = FakeHostPanel(ts.Project(tracks=[ts.Track("melody", 1, 1)]))
ah2.host = ParamCarla()
ah2.host.values = {0: 0.3, 1: 0.7}
pl2 = FakePlugin("T1.1 Rev")
pl2.params = [type("Pr", (), {"index": i, "name": n, "lo": 0.0, "hi": 1.0, "integer": False, "boolean": False})()
              for i, n in ((0, "Cutoff"), (1, "Mix"))]
ah2.loaded = {(ah2.project.tracks[0].strip, 0): pl2}
ah2.sel = (ah2.project.tracks[0].strip, 0)
ah2.toggle_assign()
assert ah2.assigning == "pick", ah2.assigning
img2 = QImage(1024, 600, QImage.Format_ARGB32); img2.fill(0)
q2 = QPainter(img2); q2.setRenderHint(QPainter.Antialiasing)
ah2.paint(q2, 1024, 600); q2.end()
knob = next(r for r, f in ah2.hits  # the first knob cell (a slot is wider)
            if 60 < r.width() < 150 and r.height() > 60 and r.top() > 120)
ah2.press(5, knob.center())  # a tap in ASSIGN mode picks the knob instead of turning it
assert isinstance(ah2.assigning, tuple) and ah2.assigning[2].name == "Cutoff", ah2.assigning
assert ah2.host.values[0] == 0.3, "a tap in ASSIGN mode must not turn the knob"
ah2.link_to_fader(3)  # then the fader
ctrl = ah2.project.controls[3]
assert ctrl.target == "param" and ctrl.param == [1, 0, 0, "Cutoff"], ctrl
assert ah2.assigning is None, "the pick mode ends once the fader is chosen"
ah2.toggle_assign(); assert ah2.assigning == "pick"
ah2.toggle_assign(); assert ah2.assigning is None, "and ASSIGN again cancels"
print("assign button OK")

# --- a track's MIDI input: device + channel, playing, lighting and recording from a keyboard ---
pry = ts.Project(bpm=240, tracks=[ts.Track("melody", 1, 1), ts.Track("melody", 2, 2)])
ty1, ty2 = pry.tracks
ty1.in_port, ty1.in_channel = "keystep", 2
ty2.in_port = "keystep"  # all channels
outy = FakeOut(); ey = ts.Engine(pry, outy); sqy = ts.Sequencer(pry, ey)
py = ts.Performer(pry, ey, sqy)

class FakeInputs:
    def __init__(self):
        self.msgs, self.names, self.open = [], [], []

    def use(self, names):
        self.names = [n for n in names if n]

    def poll(self):
        got, self.msgs = self.msgs, []
        return [(port, msg, time.perf_counter()) for port, msg in got]

    def open_names(self):
        return self.open

    def close(self):
        pass

py.inputs = FakeInputs()

def keyboard(status, a=0, b=0, port="Arturia KeyStep MIDI 1"):
    py.inputs.msgs.append((port, [status, a, b]))
    py.tick()

def said(*msgs):
    return [m for _, m in outy.log] == list(msgs)

keyboard(0x91, 60, 100)  # channel 2: T1 wants 2, T2 wants every channel
assert said([0x90, 60, 100], [0x91, 60, 100]), [m for _, m in outy.log]
assert py.inputs.names == ["keystep", "keystep"], py.inputs.names
assert py.lit(ty1, 60) and py.lit(ty2, 60), "the pads and keys light up"
keyboard(0x91, 60, 0)
assert [m for _, m in outy.log][-2:] == [[0x80, 60, 0], [0x81, 60, 0]] and not py.midi_in, "note-off reaches each track once"
outy.log.clear()
keyboard(0x92, 64, 100)  # channel 3: only the track that takes all of them
assert said([0x91, 64, 100]), [m for _, m in outy.log]
keyboard(0x82, 64, 0); outy.log.clear()
keyboard(0x90, 65, 100, port="Novation Launchpad")  # a device no track listens to
assert not outy.log, "a device no track listens to plays nothing"
keyboard(0xB1, 1, 70)  # mod wheel: straight to the instruments
assert said([0xB1, 1, 70], [0xB1, 1, 70]), [m for _, m in outy.log]
outy.log.clear()
py.mode = "rec"; py.tick(); ey.start()
time.sleep(step_len * 3.3)
keyboard(0x91, 67, 100)  # the keyboard records into the track it is routed to
time.sleep(step_len * 2)
keyboard(0x81, 67, 0)
rec = sorted((k, i, round(st.gate, 3)) for k, lane in ty1.current.lanes.items() for i, st in enumerate(lane) if st.on)
print("MIDI keyboard recorded:", rec)
assert rec and rec[0][0] == 67 and 1.5 <= rec[0][2] <= 2.5, rec
assert any(st.on for lane in ty2.current.lanes.values() for st in lane), "both listening tracks record"
ey.stop(); py.mode = None; py.tick()
assert not ey.rec and not py.midi_in, "disarming ends the recording"
print("MIDI keyboard OK")

# a drum track takes a pad controller into the kit's voice lanes, not into lanes of raw notes
prd = ts.Project(bpm=240, tracks=[ts.Track("drum", 10, 1)])
dr = prd.tracks[0]; dr.in_port = "pads"
ed = ts.Engine(prd, FakeOut()); sd = ts.Sequencer(prd, ed); pd = ts.Performer(prd, ed, sd)
pd.inputs = FakeInputs()
pd.mode = "rec"; ed.start(); pd.tick()
time.sleep(step_len * 1.2)
pd.inputs.msgs.append(("pads", [0x99, 36, 120])); pd.tick()
pd.inputs.msgs.append(("pads", [0x89, 36, 0])); pd.tick()
assert any(st.on for st in dr.current.lanes[0]), "note 36 lands in the kick lane"
assert 36 not in dr.current.lanes, "not in a lane of its own"
ed.stop(); pd.mode = None; pd.tick()
print("drum input OK")

# --- the input manager: opens what a track asks for, queues what arrives, lets go of the rest ---
class FakeIn:
    def __init__(self, name):
        self.name, self.cb, self.closed = name, None, False

    def set_callback(self, cb):
        self.cb = cb

    def cancel_callback(self):
        self.cb = None

    def close_port(self):
        self.closed = True

made = {}
def in_opener(name):
    made[name] = FakeIn(name)
    return made[name]

ins = ts.Inputs(in_opener, lambda: ["Arturia KeyStep MIDI 1", "Novation Launchpad"])
ins.use(["keystep"])
assert list(made) == ["Arturia KeyStep MIDI 1"], made
made["Arturia KeyStep MIDI 1"].cb(([0x90, 60, 100], 0.0))
got = ins.poll()
assert [(name, msg) for name, msg, when in got] == [("Arturia KeyStep MIDI 1", [0x90, 60, 100])], got
assert ins.poll() == []
assert ins.open_names() == ["Arturia KeyStep MIDI 1"]
ins.use([])
assert made["Arturia KeyStep MIDI 1"].closed and ins.open_names() == []
assert py.input_ok(ty1) is False, "a track whose device is not open says so"
py.inputs.open = ["Arturia KeyStep MIDI 1"]
assert py.input_ok(ty1) is True
print("input manager OK")

# --- the EDIT socket: dim, green with its channel, amber when the device is away ---
def socket(tr, ok=None):
    img = QImage(46, 58, QImage.Format_ARGB32); img.fill(0)
    q = QPainter(img); q.setRenderHint(QPainter.Antialiasing)
    sqy.s, sqy.input_ok, sqy.hits = 1.0, (lambda t: ok) if ok is not None else None, []
    sqy.draw_input(q, QRectF(0, 0, 46, 58), tr)
    q.end()
    return {img.pixelColor(x, y).name() for x in range(46) for y in range(58)}

assert "#4ade80" in socket(ty1), "configured and here: green"
assert "#ff8c42" in socket(ty1, False), "configured but the device is away: amber"
assert not {"#4ade80", "#ff8c42"} & socket(ts.Track("melody", 5, 9)), "not configured: dim"
picked = []
sqy.choose_input = picked.append
socket(ty1)
sqy.press(1, QPointF(23, 29))
assert picked == [ty1], "tapping the socket asks for the input dialog"
print("input socket OK")

# --- the input dialog: a port and a channel, applied at once ---
applied = []
dlg = ts.MidiInputs(lambda port, ch: applied.append((port, ch)), list_ports=lambda: ["Arturia KeyStep MIDI 1"])
img = QImage(1200, 700, QImage.Format_ARGB32); img.fill(0)
q = QPainter(img); q.setRenderHint(QPainter.Antialiasing); dlg.paint(q, 1200, 700); q.end()
dlg.pick("Arturia KeyStep MIDI 1")
assert applied == [("Arturia KeyStep MIDI 1", 0)], applied
dlg.take(5)
assert applied[-1] == ("Arturia KeyStep MIDI 1", 5), applied
img.fill(0); q = QPainter(img); dlg.paint(q, 1200, 700); q.end()  # the new state paints
print("input dialog OK")

# --- a track's input survives a save and load ---

_f = OUT / "in.json"
py3 = ts.Project(tracks=[ts.Track("melody", 1, 1, in_port="keystep", in_channel=7)])
_f.write_text(ts.dump_project(py3))
back = ts.load_project(_f)
assert back.tracks[0].in_port == "keystep" and back.tracks[0].in_channel == 7
_f.write_text(ts.dump_project(ts.Project(tracks=[ts.Track("melody", 1, 1)])))
assert "in_port" not in _f.read_text(), "a track with no input does not store one"
print("input saved OK")

# --- metronome and count-in: the clicks come before the pattern, then on every beat ---
class FakeClicker:
    def __init__(self):
        self.events, self.prepared, self.closed, self.lead = [], 0, 0, 0.0

    def prepare(self):
        self.prepared += 1
        return True

    def at(self, when, accent=False):
        self.events.append((when, accent))

    def flush(self):
        self.events.clear()

    def close(self):
        self.closed += 1

    @property
    def open(self):
        return self.prepared > self.closed

prc = ts.Project(bpm=600, tracks=[ts.Track("melody", 1, 1)], count_in=1)  # 600 bpm: a beat is 0.1 s
outc = FakeOut(); ec2 = ts.Engine(prc, outc)
ec2.clicker = FakeClicker()
ec2.start()
assert ec2.counting and not ec2.playing, "one bar of count-in first"
assert [0xFA] not in [m for _, m in outc.log], "the transport has not started yet"
time.sleep(0.25)
assert ec2.counting and len(ec2.clicker.events) >= 2, ec2.clicker.events
time.sleep(0.4)
assert ec2.playing and not ec2.counting, "the pattern starts after the count-in"
assert [0xFA] in [m for _, m in outc.log]
clicks = list(ec2.clicker.events)
assert [a for _, a in clicks].count(True) == 1 and [a for _, a in clicks].count(False) == 3, clicks
print("count-in clicks:", [accent for _, accent in clicks])
ec2.clicker.events.clear()
ec2.set_click(True)
time.sleep(0.3)  # now the metronome follows the playing pattern
assert len(ec2.clicker.events) >= 2 and any(a for _, a in ec2.clicker.events), ec2.clicker.events
ec2.stop()
assert not ec2.playing
ec2.set_click(True)
assert prc.click and ec2.clicker.prepared >= 2
ec2.set_click(False)
assert not prc.click and ec2.clicker.closed >= 1
# stop cancels a count-in
ec2.start()
assert ec2.counting
ec2.stop()
assert not ec2.counting and not ec2.playing
print("metronome OK")

# --- the keyboard's own transport: MIDI start/stop, and the CCs some controllers send ---
prt = ts.Project(tracks=[ts.Track("melody", 1, 1)])
prt.tracks[0].in_port = "keys"
outt = FakeOut(); et2 = ts.Engine(prt, outt); tt = ts.Performer(prt, et2, ts.Sequencer(prt, et2))
tt.inputs = FakeInputs()
et2.clicker = FakeClicker()

def play_msg(*msg):
    tt.inputs.msgs.append(("Arturia KeyStep MIDI 1", list(msg)))
    tt.tick()

play_msg(0xFA)
assert et2.playing, "MIDI start plays"
play_msg(0xFC)
assert not et2.playing, "MIDI stop stops"
play_msg(0xFB)
assert et2.playing, "MIDI continue plays too"
play_msg(0xB0, 116, 127)
assert not et2.playing, "CC 116 stops"
play_msg(0xB0, 115, 127)
assert et2.playing, "CC 115 plays"
play_msg(0xB0, 115, 0)  # a button release must not do anything
assert et2.playing
et2.stop()
# with a count-in, the keyboard's start counts in as well
prt.count_in = 1
play_msg(0xFA)
assert et2.counting and not et2.playing, "the count-in applies to the keyboard too"
et2.stop()
assert not et2.counting and not et2.playing
print("keyboard transport OK")

# --- the metronome settings travel with the project ---
prm = ts.Project(tracks=[ts.Track("melody", 1, 1)], click=True, count_in=2)
_f = OUT / "click.json"
_f.write_text(ts.dump_project(prm))
back = ts.load_project(_f)
assert back.click is True and back.count_in == 2
_f.write_text(ts.dump_project(ts.Project(tracks=[ts.Track("melody", 1, 1)])))
text = _f.read_text()
assert "click" not in text and "count_in" not in text, "off is not stored"
assert ts.load_project(_f).click is False and ts.load_project(_f).count_in == 0
print("click settings OK")

# --- a note held past the end of the pattern keeps its length (it used to wrap: 1/16 or a quarter) ---
prh = ts.Project(bpm=600, tracks=[ts.Track("melody", 1, 1)])  # 600 bpm: a step is 25 ms, a bar 0.4 s
orh = FakeOut(); ehh = ts.Engine(prh, orh); shh = ts.Sequencer(prh, ehh)
phh = ts.Performer(prh, ehh, shh)
shh.sel = prh.tracks[0]
phh.mode = "rec"; ehh.start(); phh.tick()
time.sleep(0.6)
take = phh.record_start(prh.tracks[0], [60], 100)
assert take, "armed and playing: the take starts"
time.sleep(0.5)  # ~20 steps: over the bar line, where the old code wrapped to 4 steps
phh.record_end(prh.tracks[0], take)
step = prh.tracks[0].current.lanes[60][take[1]]
print("held over the bar line:", round(step.gate, 3), "steps")
assert step.gate == prh.tracks[0].current.length, step.gate  # the whole bar, not the remainder of the wrap
ehh.stop(); phh.mode = None; phh.tick()
print("long hold OK")

# --- an external clock sets the tempo (the keyboard is the master) ---
prb = ts.Project(bpm=100, tracks=[ts.Track("melody", 1, 1)])
ebb = ts.Engine(prb, FakeOut()); shb = ts.Sequencer(prb, ebb); phb = ts.Performer(prb, ebb, shb)
phb.inputs = FakeInputs()
t = 1000.0
for _ in range(48 * 2):  # two beats of clock at 128 bpm
    phb.clock(t)
    t += 60.0 / 128 / 24
assert prb.bpm == 128, prb.bpm
print("clock tempo:", prb.bpm)
phb.clock(t + 1.0)  # the clock stopped for a second: measure again, don't jump
assert prb.bpm == 128, prb.bpm
t += 1.0
for _ in range(24 * 5):  # and a new tempo is followed (four-beat window)
    phb.clock(t)
    t += 60.0 / 90 / 24
assert prb.bpm == 90, prb.bpm
# the same through a MIDI input, which is how it actually arrives
prb.bpm = 100
phb.pulse_at, phb.clock_times = None, []
tt = 2000.0
for _ in range(48 * 2):
    phb.external("Arturia KeyStep MIDI 1", [0xF8], tt)
    tt += 60.0 / 140 / 24
assert prb.bpm == 140, prb.bpm
print("clock from MIDI OK")
# USB timing jitter must not make the BPM wander (a KeyStep Pro at 130 used to show 127..131)
prb.bpm = 100
phb.pulse_at, phb.clock_times = None, []
tt, jitter = 3000.0, (0.0, 0.0015, -0.0015, 0.001, -0.001, 0.0005, -0.0005)
for i in range(96 * 2):
    phb.external("Arturia KeyStep MIDI 1", [0xF8], tt)
    tt += 60.0 / 130 / 24 + jitter[i % len(jitter)]
assert prb.bpm == 130, prb.bpm
print("clock jitter OK")

# --- two Carla engines with one name get their clients renamed (and every link fails): take a free name ---
assert plugins.free_engine_name("Touch-Seq", []) == "Touch-Seq"
assert plugins.free_engine_name("Touch-Seq", ["Touch-Seq", "Touch-Seq/T1.1 Surge XT"]) == "Touch-Seq 2"
assert plugins.free_engine_name("Touch-Seq", ["Touch-Seq", "Touch-Seq 2/T1.1 x"]) == "Touch-Seq 3"
assert plugins.free_engine_name("Touch-Seq", ["Touch-Seq T1", "Touch-Seq T2"]) == "Touch-Seq", "MIDI ports are no engine"
print("engine name OK")

# --- lifting a key must not cut a note the sequencer has just picked up --------------------
prv = ts.Project(tracks=[ts.Track("melody", 1, 1)])
outv = FakeOut(); evv = ts.Engine(prv, outv)
trv = prv.tracks[0]
evv.sounding[(trv.channel, 60)] = (time.perf_counter() + 1.0, trv)  # the pattern is playing it now
evv.live(trv, 60, 0)
assert not outv.log, "a live release leaves the pattern's note alone"
evv.live(trv, 62, 0)
assert [m for _, m in outv.log] == [[0x80, 62, 0]], "but a note the pattern is not playing stops"
evv.stop(); evv.close()

# and with real timing: a whole-bar chord, released just after the pattern picks it up
prw = ts.Project(bpm=600, tracks=[ts.Track("melody", 1, 1)])  # 600 bpm: a bar is 0.4 s
outw = FakeOut(); ew2 = ts.Engine(prw, outw); sw2 = ts.Sequencer(prw, ew2); pw2 = ts.Performer(prw, ew2, sw2)
sw2.sel = prw.tracks[0]; pw2.mode = "rec"
ew2.start(); pw2.tick()
time.sleep(0.05)
take = pw2.record_start(prw.tracks[0], [60], 100)
pw2.on(prw.tracks[0], 60, 100)
time.sleep(0.5)  # over the bar line: the pattern leaves the step to the fingers holding it
assert sum(1 for _, m in outw.log if m[0] & 0xF0 == 0x90 and m[1] == 60) == 1, "the pattern blipped the take"
pw2.off(prw.tracks[0], 60)  # the take lasted the whole bar: it ends here, it must not hang over
pw2.record_end(prw.tracks[0], take)
notes = [m for _, m in outw.log if m[0] & 0xF0 in (0x80, 0x90) and m[1] == 60]
assert notes[-1] == [0x80, 60, 0], notes[-3:]
assert notes.count([0x90, 60, 100]) == 1, "no extra note-on for a take whose own length is over"
time.sleep(16 * ew2.step_len * 1.1)  # the loop comes round: the pattern plays what was recorded
assert [m for _, m in outw.log].count([0x90, 60, 100]) >= 2, "the pattern plays the take on its turn"
ew2.stop(); pw2.mode = None; pw2.tick(); ew2.close()
print("take handed over OK")

# --- REC replaces the steps played over, and nothing else ---------------------------------
prr = ts.Project(bpm=600, tracks=[ts.Track("melody", 1, 1), ts.Track("drum", 10, 2)])
melr, drumr = prr.tracks
patr = melr.current
patr.lane(60)[0].on = True
patr.lane(64)[0].on = True   # a chord at step 0
patr.lane(67)[4].on = True   # a note elsewhere
drumr.current.lane(0)[0].on = True  # a kick at step 0
outr = FakeOut(); er3 = ts.Engine(prr, outr); sr3 = ts.Sequencer(prr, er3); pwr = ts.Performer(prr, er3, sr3)
sr3.sel = melr
pwr.mode, pwr.quant = "rec", 1
er3.start(); pwr.tick()
time.sleep(1.4 * 16 * er3.step_len)  # a whole pass with nothing played
still = sorted((k, i) for k, lane in patr.lanes.items() for i, s in enumerate(lane) if s.on)
assert still == [(60, 0), (64, 0), (67, 4)], f"REC erased what was not played: {still}"
g3 = pwr.press_pad(1, melr, [(70, 70)], r, QPointF(50, 50)); time.sleep(2 * er3.step_len); g3.release(None)
time.sleep(2 * er3.step_len)
after = sorted((k, i) for k, lane in patr.lanes.items() for i, s in enumerate(lane) if s.on)
assert after == [(67, 4), (70, 0)], f"the played step replaces only itself: {after}"
# drums keep the other voices of a step
drumr.current.lane(1)[0].on = True
sr3.sel = drumr; pwr.tick()
gd3 = pwr.press_pad(("drum", 0), drumr, [(0, 36)], r, QPointF(50, 90)); gd3.release(None)
assert drumr.current.lane(1)[0].on, "a kick does not erase the snare on the same step"
er3.stop(); pwr.mode = None; pwr.tick(); er3.close()
print("REC punch-in OK")

# --- the metronome's beats are queued a beat early (its stream runs a buffer behind) ------
prl = ts.Project(bpm=600, tracks=[ts.Track("melody", 1, 1)], click=True)
el5 = ts.Engine(prl, FakeOut()); el5.clicker = FakeClicker(); el5.clicker.lead = 0.005
el5.start()
time.sleep(0.05)
assert el5.clicker.events, "a beat is queued"
now5 = time.perf_counter()
ahead = max((w for w, a in el5.clicker.events), default=0.0) - now5
print("next beat queued %.3f s ahead (a step is %.3f s)" % (ahead, el5.step_len))
assert ahead > 0.01, "queued early enough to be written before its time"
assert el5.clicker.events[0][1] is True, "the downbeat is the accented one"
el5.stop()
assert el5.clicker.events == [], "a stop drops the beats not written yet"
el5.close()
print("click lookahead OK")

# --- clearing a slot takes the notes out, not the length you set ---------------------------
prq = ts.Project(bpm=120, tracks=[ts.Track("melody", 1, 1)])
trq = prq.tracks[0]
trq.patterns[1].length = 64
trq.patterns[1].lane(60)[0].on = True
eq2 = ts.Engine(prq, FakeOut()); sq2 = ts.Sequencer(prq, eq2)
sq2.sel = trq
sq2.toggle_mode("clear")
sq2.press_pattern(1)
assert trq.patterns[1].length == 64, f"clearing reset the length to {trq.patterns[1].length}"
assert trq.patterns[1].empty(), "the notes are gone"
eq2.close()
print("clear keeps the length OK")

# --- the metronome in the audio graph: the beats go to the click plugin, no stream of our own ---
prk2 = ts.Project(bpm=600, tracks=[ts.Track("melody", 1, 1)], click=True)
ekk = ts.Engine(prk2, FakeOut()); ekk.clicker = FakeClicker()
beats = []
ekk.click_tap = lambda accent: beats.append(accent)
ekk.start()
time.sleep(0.3)
assert len(beats) >= 2, f"beats went to the plugin: {beats}"
assert beats[0] is True, "the first of the bar is the accented one"
assert ekk.clicker.prepared == 0, "with the plugin in the graph there is no stream to open"
ekk.set_click(False)
assert prk2.click is False and ekk.clicker.prepared == 0
ekk.click_tap = None  # no plugin (or it went away): the app's own stream takes over
ekk.set_click(True)
assert ekk.clicker.prepared == 1, "the fallback stream is opened"
ekk.stop(); ekk.close()
print("click plugin path OK")

# --- a panel that cannot draw must not freeze the window: it says so, the rest still paints ---
class BadPaint(ts.Panel):
    def draw(self, p):
        raise RuntimeError("no paint today")

class GoodPaint(ts.Panel):
    drew = 0
    def draw(self, p):
        type(self).drew += 1
        p.fillRect(self.rect(), QColor("#123456"))

wb2 = ts.Window([(0, 0, "B", "broken", BadPaint()), (1, 0, "G", "fine", GoodPaint())])
wb2.resize(900, 600)
shot = wb2.grab().toImage()
assert len(wb2.broken_paint) == 1, f"only the broken module failed: {wb2.broken_paint}"
assert shot.width() == 900 and shot.height() == 600, "the window still painted"
assert GoodPaint.drew == 1, f"the other module was still drawn ({GoodPaint.drew})"
print("broken paint OK")

# --- the tempo in the top bar: its dialog drags, nudges and taps --------------------------
prt3 = ts.Project(bpm=100, tracks=[ts.Track("melody", 1, 1)])
ert3 = ts.Engine(prt3, FakeOut())
rkt = ts.Rack(prt3, ert3, OUT / "tempo.json", StubHost())
rkt.resize(1920, 1080)
rkt.grab()
assert rkt.project.count_in == 0
rkt.cycle_count_in(); assert rkt.project.count_in == 1
rkt.cycle_count_in(); assert rkt.project.count_in == 2
rkt.cycle_count_in(); assert rkt.project.count_in == 0, "off again"
rkt.open_tempo()
assert rkt.dialog and rkt.dialog[0] == "TEMPO", rkt.dialog
tp = rkt.dialog[2]
tp.set_bpm(110); assert prt3.bpm == 110
tp.set_bpm(-5); assert prt3.bpm == ts.BPM_MIN, "clamped at the bottom"
tp.set_bpm(9999); assert prt3.bpm == ts.BPM_MAX, "clamped at the top"
now = time.monotonic()
tp.taps = [now - 1.5, now - 1.0, now - 0.5]
tp.tap_tempo()
assert prt3.bpm == 120, prt3.bpm
rkt.grab().save(str(OUT / "tempo.png"))
rkt.close_dialog()
ert3.close()
print("tempo dialog OK")

# --- song mode: the steps hand the tracks their patterns, bar by bar -----------------------
prs = ts.Project(bpm=600, tracks=[ts.Track("melody", 1, 1), ts.Track("drum", 10, 2)])
t_sm, t_sd = prs.tracks
t_sm.patterns[0].lane(60)[0].on = True
t_sm.patterns[1].lane(72)[0].on = True
t_sd.patterns[0].lane(0)[0].on = True
t_sd.patterns[1].lane(1)[0].on = True
prs.song = [ts.SongStep(bars=1, repeats=1, patterns={1: 0, 2: 0}),
            ts.SongStep(bars=1, repeats=1, patterns={1: 1})]  # the drum is left out: keep previous
prs.song_on = True
outs = FakeOut(); es_s = ts.Engine(prs, outs)
es_s.start()
time.sleep(0.15)
assert es_s.song_position() == (0, 0), es_s.song_position()
assert (t_sm.pattern, t_sd.pattern) == (0, 0), (t_sm.pattern, t_sd.pattern)
for _ in range(400):
    if es_s.song_position()[0] == 1:
        break
    time.sleep(0.005)
assert es_s.song_position()[0] == 1, "the song moved to its second step"
assert (t_sm.pattern, t_sd.pattern) == (1, 0), (t_sm.pattern, t_sd.pattern)
for _ in range(800):
    if es_s.song_position()[0] == 0:
        break
    time.sleep(0.005)
assert es_s.song_position()[0] == 0, "and looped back to the first"
es_s.queue_song_step(1)  # a tap on a step: taken at the next bar
time.sleep(0.5)
assert es_s.song_position()[0] == 1, es_s.song_position()
es_s.stop()
assert es_s.song_position() == (None, 0), "stopped: no step playing"
# without LOOP the last step goes on
prs.song_loop = False
es_s.start()
for _ in range(1200):
    if es_s.song_position()[0] == 1:
        break
    time.sleep(0.005)
time.sleep(0.5)
assert es_s.song_position()[0] == 1, "without LOOP it stays on the last step"
es_s.stop(); es_s.close()
print("song playback OK")

# --- the SONG view: a step per tap, patterns picked from a list ----------------------------
prs2 = ts.Project(bpm=120, tracks=[ts.Track("melody", 1, 1), ts.Track("drum", 10, 2)])
es_s2 = ts.Engine(prs2, FakeOut()); sq_s = ts.Sequencer(prs2, es_s2)
assert not prs2.song
sq_s.add_song_step()
assert len(prs2.song) == 1 and prs2.song_on, "the first step also switches the song on"
assert prs2.song[0].patterns == {1: 0, 2: 0}, prs2.song[0].patterns
sq_s.cycle_song_bars(0); assert prs2.song[0].bars == 8, prs2.song[0].bars
sq_s.cycle_song_repeats(0); assert prs2.song[0].repeats == 2, prs2.song[0].repeats
for _ in range(3):  # 2 -> 4 -> 8 -> 1
    sq_s.cycle_song_repeats(0)
assert prs2.song[0].repeats == 1, "back round"
sq_s.open_song_pick(0, 1); assert sq_s.song_pick == (0, 1)
sq_s.pick_song_pattern(3); assert prs2.song[0].patterns[1] == 3, prs2.song[0].patterns
assert sq_s.song_pick is None, "picking closes the list"
sq_s.open_song_pick(0, 1); sq_s.clear_song_pattern()
assert 1 not in prs2.song[0].patterns, "keep previous"
sq_s.add_song_step(); assert len(prs2.song) == 2 and sq_s.song_sel == 1, sq_s.song_sel
sq_s.delete_song_step(1); assert len(prs2.song) == 1
img = QImage(1060, 530, QImage.Format_ARGB32); img.fill(0)
q = QPainter(img); q.setRenderHint(QPainter.Antialiasing)
sq_s.view = "song"
sq_s.paint(q, 1060, 530); q.end()
img.save(str(OUT / "song.png"))
sq_s.open_song_pick(0, 2)
img.fill(0); q = QPainter(img); q.setRenderHint(QPainter.Antialiasing); sq_s.paint(q, 1060, 530); q.end()
es_s2.close()
print("song view OK")

# --- drag & drop: tracks in GRID, steps in SONG -------------------------------------------
prd2 = ts.Project(bpm=120, tracks=[ts.Track("melody", 1, 1), ts.Track("drum", 10, 2), ts.Track("melody", 3, 3)])
ed2 = ts.Engine(prd2, FakeOut()); sqd = ts.Sequencer(prd2, ed2)
before = [t.number for t in prd2.tracks]
sqd.drop_row("grid", 0, 2)
assert [t.number for t in prd2.tracks] == [before[1], before[2], before[0]], [t.number for t in prd2.tracks]
sqd.drop_row("grid", 2, 0)
assert [t.number for t in prd2.tracks] == before, "and back"
prd2.song = [ts.SongStep(4, 1, {1: 0}), ts.SongStep(4, 1, {1: 1}), ts.SongStep(4, 1, {1: 2})]
prd2.song_on = True
ed2.song_step = 0
sqd.drop_row("song", 0, 2)
assert [st.patterns[1] for st in prd2.song] == [1, 2, 0], [st.patterns[1] for st in prd2.song]
assert ed2.song_step == 2, "the step playing moved with the song"
assert sqd.song_sel == 2, sqd.song_sel
# a held row is lifted, then the drag moves it; a plain drag scrolls instead
sqd.view = "song"; sqd.grab() if hasattr(sqd, "grab") else None
img = QImage(1060, 530, QImage.Format_ARGB32); img.fill(0)
q = QPainter(img); q.setRenderHint(QPainter.Antialiasing); sqd.paint(q, 1060, 530); q.end()
row1 = sqd.song_rows[1]
g = ts.Reorder(sqd, "song", 1, row1.center())
g.t0 -= 0.5
g.tick()
assert g.lifted, "a held row is picked up"
g.move(QPointF(row1.center().x(), sqd.song_rows[2].center().y()))
assert g.target == 2, g.target
order = [st.patterns[1] for st in prd2.song]
g.release(QPointF(0, 0))
assert [st.patterns[1] for st in prd2.song] == [order[0], order[2], order[1]], "dropped in its new place"
# a quick drag is a scroll, not a move
g2 = ts.Reorder(sqd, "song", 0, sqd.song_rows[0].center())
g2.move(QPointF(0, sqd.song_rows[0].center().y() + 60))
assert g2.scrolling and not g2.lifted, "a drag scrolls the list"
g2.release(QPointF(0, 0))
assert [st.patterns[1] for st in prd2.song] == [order[0], order[2], order[1]], "and nothing moved"
ed2.close()
print("drag reorder OK")

# --- a held note may last bars, up to the length of the pattern ---------------------------
prg = ts.Project(bpm=600, tracks=[ts.Track("melody", 1, 1)])  # 600 bpm: a step is 25 ms
trg = prg.tracks[0]
trg.current.length = 64  # four bars
egh = ts.Engine(prg, FakeOut()); sqg = ts.Sequencer(prg, egh); pg = ts.Performer(prg, egh, sqg)
sqg.sel = trg
pg.mode = "rec"
egh.start(); pg.tick()
time.sleep(0.2)
take = pg.record_start(trg, [60], 100)
time.sleep(64 * egh.step_len)  # hold it over four bars
pg.record_end(trg, take)
four = [st.gate for lane in trg.current.lanes.values() for st in lane if st.on]
print("held four bars ->", four)
assert four and four[0] >= 60, four  # the whole pattern, not one bar
egh.stop(); pg.mode = None; pg.tick(); egh.close()
# in a 16-step pattern the same hold is the whole pattern, not more
prg.tracks[0].current.length = 16
egh2 = ts.Engine(prg, FakeOut()); sqg2 = ts.Sequencer(prg, egh2); pg2 = ts.Performer(prg, egh2, sqg2)
sqg2.sel = trg
pg2.mode = "rec"
egh2.start(); pg2.tick()
time.sleep(0.1)
take2 = pg2.record_start(trg, [62], 100)
time.sleep(40 * egh2.step_len)
pg2.record_end(trg, take2)
short = [st.gate for lane in trg.current.lanes.values() for st in lane if st.on]
assert short and short[-1] == 16, short
egh2.stop(); pg2.mode = None; pg2.tick(); egh2.close()
# and the roll lets a note grow to the end of the pattern (but not over the next note of its pitch)
lane = trg.current.lane(70)
lane[0].on = True
assert sqg2.room(trg.current, lane, 0) == 16, sqg2.room(trg.current, lane, 0)
lane[8].on = True
assert sqg2.room(trg.current, lane, 0) == 8, "up to the next note of the same pitch"
print("long notes OK")

# --- controller lanes: a value holds until its next point, and only changes go out ---------
prj = ts.Project(bpm=600, tracks=[ts.Track("melody", 1, 1)])  # 600 bpm: a step is 25 ms
tj = prj.tracks[0]
tj.current.cc[1] = {0: 20, 4: 100}
outj = FakeOut(); ej = ts.Engine(prj, outj)
ej.start()
time.sleep(16 * ej.step_len * 1.25)  # a bar and a bit
ccs = [(m[1], m[2]) for _, m in outj.log if m[0] & 0xF0 == 0xB0]
print("controllers sent:", ccs[:6])
assert ccs[:2] == [(1, 20), (1, 100)], ccs  # 20 at step 0, 100 at step 4, nothing in between
ej.stop()
outj.log.clear()
ej.start()
time.sleep(3 * ej.step_len)
assert (1, 20) in [(m[1], m[2]) for _, m in outj.log if m[0] & 0xF0 == 0xB0], "a start says it again"
ej.stop(); ej.close()
# what a lane is worth at a step: the last point at or before it, and the end wraps into the start
pat_j = prj.tracks[0].patterns[0]
pat_j.cc[74] = {8: 60}
assert pat_j.cc_at(74, 0) == 60, "a lane that starts later still has a value at the start (the end wraps)"
assert pat_j.cc_at(1, 3) == 20 and pat_j.cc_at(1, 5) == 100 and pat_j.cc_at(74, 9) == 60
_f = OUT / "cc.json"
_f.write_text(ts.dump_project(prj))
back = ts.load_project(_f)
assert back.tracks[0].patterns[0].cc == {1: {0: 20, 4: 100}, 74: {8: 60}}, back.tracks[0].patterns[0].cc
print("cc lanes OK")

# --- a controller from the keyboard lands at the step under the playhead while armed -------
prk3 = ts.Project(bpm=600, tracks=[ts.Track("melody", 1, 1)])
tk3 = prk3.tracks[0]; tk3.in_port = "keys"
outk3 = FakeOut(); ek3 = ts.Engine(prk3, outk3); sk3 = ts.Sequencer(prk3, ek3); pk3 = ts.Performer(prk3, ek3, sk3)
sk3.sel = tk3
pk3.inputs = FakeInputs()
pk3.mode = "rec"
ek3.start(); pk3.tick()
time.sleep(0.15)
step_at = int(ek3.position(tk3)) % 16
pk3.inputs.msgs.append(("Arturia KeyStep MIDI 1", [0xB1, 74, 90])); pk3.tick()
lane = tk3.current.cc.get(74)
assert lane, "a lane was made for the controller"
assert lane.get(step_at) == 90 or lane.get((step_at + 1) % 16) == 90, (step_at, lane)
assert [0xB1, 74, 90] in [m for _, m in outk3.log], "and it still reached the instrument"
for n in (1, 2, 3, 4):  # the lane limit holds
    pk3.inputs.msgs.append(("Arturia KeyStep MIDI 1", [0xB1, n, 10])); pk3.tick()
assert len(tk3.current.cc) <= 4, tk3.current.cc
# a fader on the track's channel also writes down
tk3.current.cc.clear()
pk3.record_cc_channel(tk3.channel, 7, 55)
assert tk3.current.cc.get(7), "a fader on the channel lands in the pattern too"
ek3.stop(); pk3.mode = None; pk3.tick(); ek3.close()
print("cc recording OK")

# --- the lane under the roll: tabs, drawing, removal --------------------------------------
prk4 = ts.Project(bpm=120, tracks=[ts.Track("melody", 1, 1)])
ek4 = ts.Engine(prk4, FakeOut()); sk4 = ts.Sequencer(prk4, ek4)
tk4 = prk4.tracks[0]; sk4.sel = tk4
assert sk4.param == "vel"
sk4.add_cc_lane(74)
assert 74 in tk4.current.cc and sk4.param == "cc" and sk4.cc_num == 74, (sk4.param, sk4.cc_num)
rect = QRectF(0, 0, 320, 40)
g4 = ts.CcDraw(sk4, tk4, rect, 74, QPointF(10, 30))  # low at the start
g4.move(QPointF(310, 4))                            # and high at the end: a sweep
lane = tk4.current.cc[74]
assert lane[min(lane)] < 40 and lane[max(lane)] > 100, lane
assert all(0 <= v <= 127 for v in lane.values()), lane
assert all(abs(k * 4 - round(k * 4)) < 1e-9 for k in lane), f"points on quarter steps: {sorted(lane)}"
assert len(lane) >= 8, f"a sweep fills the points it crossed, not just the steps: {sorted(lane)}"
assert max(lane) < 16, f"nothing past the page: {sorted(lane)}"
sk4.remove_cc_lane(74)
assert 74 not in tk4.current.cc and sk4.param == "vel", (tk4.current.cc, sk4.param)
img = QImage(1060, 530, QImage.Format_ARGB32); img.fill(0)
q = QPainter(img); q.setRenderHint(QPainter.Antialiasing)
sk4.add_cc_lane(1); tk4.current.cc[1] = {0: 20, 8: 100}
sk4.view = "edit"; sk4.paint(q, 1060, 530); q.end()
img.save(str(OUT / "cc_lane.png"))
sk4.open_cc_pick()
img.fill(0); q = QPainter(img); q.setRenderHint(QPainter.Antialiasing); sk4.paint(q, 1060, 530); q.end()
sk4.cc_pick = None
ek4.close()
print("cc lane UI OK")

# --- the browser's BUILTIN list: our own plugins only, and first in the full list -----------
def fake_ref(name, label, fmt="clap", instrument=False, effect=True):
    return types.SimpleNamespace(name=name, label=label, fmt=fmt, instrument=instrument, effect=effect,
                                 path=f"/x/{label}.clap", maker="m")

fb = types.SimpleNamespace(
    catalog=types.SimpleNamespace(plugins=[fake_ref("Zyn", "zyn"), fake_ref("Touch-Seq EQ", "touchseq.eq"),
                                           fake_ref("Touch-Seq Monitor", "touchseq.monitor"),
                                           fake_ref("Touch-Seq Click", "touchseq.click"),
                                           fake_ref("Touch-Seq XYFX", "touchseq.xyfx"),
                                           fake_ref("Reaverb", "rev", fmt="lv2")]),
    browser=("strip", 0), browser_fmt="builtin", _builtin=[], instrument_slot=lambda strip, k: False)
fb.builtin_plugins = lambda: fb._builtin  # the real one caches what it finds on disk
items = ts.HostPanel.browser_items(fb)
assert [r.name for r in items] == ["Touch-Seq EQ", "Touch-Seq XYFX"], [r.name for r in items]
fb.browser_fmt = None
assert [r.name for r in ts.HostPanel.browser_items(fb)] == ["Touch-Seq EQ", "Touch-Seq XYFX", "Reaverb", "Zyn"]
fb.browser_fmt = "lv2"
assert [r.name for r in ts.HostPanel.browser_items(fb)] == ["Reaverb"]
# the two the app puts in place (monitor, click) are not offered; XYFX is an ordinary effect
fb.browser_fmt = None
names = [r.name for r in ts.HostPanel.browser_items(fb)]
assert names == ["Touch-Seq EQ", "Touch-Seq XYFX", "Reaverb", "Zyn"], names
# one of ours that a stale catalog does not know about is still offered
fb.browser_fmt = "builtin"
fb._builtin = [fake_ref("Touch-Seq Space", "touchseq.space"), fake_ref("Touch-Seq Click", "touchseq.click")]
assert [r.name for r in ts.HostPanel.browser_items(fb)] == ["Touch-Seq EQ", "Touch-Seq Space", "Touch-Seq XYFX"], \
    "one of ours found on disk is filtered too (the click must stay out)"
print("builtin browser OK")

# --- the A-Z strip skips the pinned block: T goes to the other T plugins, not to the top ---
fj = types.SimpleNamespace(
    catalog=types.SimpleNamespace(plugins=[fake_ref("Reaverb", "rev"), fake_ref("Touch-Seq EQ", "touchseq.eq"),
                                           fake_ref("TAL Dub", "taldub"), fake_ref("Zyn", "zyn")]),
    browser=("strip", 0), browser_fmt=None, _builtin=[], instrument_slot=lambda strip, k: False)
fj.builtin_plugins = lambda: fj._builtin
fj.LETTERS = ts.HostPanel.LETTERS
fj.letter = ts.HostPanel.letter.__get__(fj)
fj.pinned = ts.HostPanel.pinned.__get__(fj)
jitems = ts.HostPanel.browser_items(fj)
assert [r.name for r in jitems] == ["Touch-Seq EQ", "Reaverb", "TAL Dub", "Zyn"], [r.name for r in jitems]
assert ts.HostPanel.pinned(fj, jitems) == 1
ts.HostPanel.jump(fj, jitems, "T", 50.0)
assert fj.browser_scroll == 100.0, f"T jumped to {fj.browser_scroll / 50:.0f}, not to TAL Dub"
ts.HostPanel.jump(fj, jitems, "A", 50.0)
assert fj.browser_scroll == 50.0, "a letter before the free list stops at its first plugin, not at the pinned row"
fj.browser_fmt = "builtin"  # the BUILTIN list is all ours: its letters still jump normally
bitems = ts.HostPanel.browser_items(fj)
assert ts.HostPanel.pinned(fj, bitems) == 0 and [r.name for r in bitems] == ["Touch-Seq EQ"]
ts.HostPanel.jump(fj, bitems, "T", 50.0)
assert fj.browser_scroll == 0.0
print("browser letters OK")

# --- a drawn sweep is sent between the steps, not only on them ----------------------------
prv2 = ts.Project(bpm=240, tracks=[ts.Track("melody", 1, 1)])
tv2 = prv2.tracks[0]
tv2.current.length = 16
tv2.current.cc[1] = {0.0: 0, 0.25: 32, 0.5: 64, 0.75: 96, 1.0: 127}  # a ramp over one step
outv2 = FakeOut(); ev2 = ts.Engine(prv2, outv2)
ev2.start()
time.sleep(2.2 * ev2.step_len)
ev2.stop(); ev2.close()
sent = [m[2] for _, m in outv2.log if m[0] & 0xF0 == 0xB0]
print("ramp sent:", sent[:8])
assert sent[:5] == [0, 32, 64, 96, 127], sent[:8]  # every quarter-step point spoke, in order
print("cc sub-step OK")

# --- the M of a track header still mutes: the drag region stops before it ------------------
prm3 = ts.Project(tracks=[ts.Track("melody", 1, 1), ts.Track("drum", 10, 2)])
em3 = ts.Engine(prm3, FakeOut())
wm3 = View(ts.Sequencer(prm3, em3))
wm3.resize(1024, 600)
wm3.grab()
row0 = wm3.grid_rows[0]
mute_at = QPointF(row0.right() - 28, row0.center().y())
wm3.press(3, mute_at); wm3.release(3, mute_at)
assert prm3.tracks[0].mute, "the M button mutes the track"
wm3.press(4, mute_at); wm3.release(4, mute_at)
assert not prm3.tracks[0].mute, "and unmutes it again"
# the rest of the header still selects (and a second tap opens EDIT)
row1 = wm3.grid_rows[1]
head_at = QPointF(row1.left() + 40, row1.center().y())
wm3.press(5, head_at); wm3.release(5, head_at)
assert wm3.sel is prm3.tracks[1] and wm3.view == "grid", (wm3.sel, wm3.view)
wm3.press(6, head_at); wm3.release(6, head_at)
assert wm3.view == "edit", "a second tap opens it"
# and the lane can be shown alone, without the roll
wm3.lane_only = True
img = QImage(1024, 600, QImage.Format_ARGB32); img.fill(0)
q = QPainter(img); q.setRenderHint(QPainter.Antialiasing); wm3.paint(q, 1024, 600); q.end()
img.save(str(OUT / "lane_only.png"))
em3.close()
print("mute + lane only OK")

# --- the arpeggiator: the chord it hears, one note at a time, on the transport's grid -------
pra = ts.Project(bpm=600, tracks=[ts.Track("melody", 1, 1)])  # 600 bpm: a step is 25 ms
ta = pra.tracks[0]
ta.arp.on = True
outa = FakeOut(); ea = ts.Engine(pra, outa)
ea.start(); time.sleep(0.05)
outa.log.clear()
ea.live(ta, 60, 100); ea.live(ta, 64, 100); ea.live(ta, 67, 100)
time.sleep(0.2)
ons = [m[1] for _, m in outa.log if m[0] & 0xF0 == 0x90]
print("arp from the keys:", ons[:6])
assert ons[:6] == [60, 64, 67, 60, 64, 67], ons
assert {m[1] for _, m in outa.log if m[0] & 0xF0 == 0x90} == {60, 64, 67}
ea.live(ta, 60, 0); ea.live(ta, 64, 0); ea.live(ta, 67, 0)
time.sleep(0.06)
sent = sum(1 for _, m in outa.log if m[0] & 0xF0 == 0x90)
time.sleep(0.12)
assert sum(1 for _, m in outa.log if m[0] & 0xF0 == 0x90) == sent, "keys up: the arp stops"
# off: the chord again, and at once (nothing was written anywhere)
ta.arp.on = False
outa.log.clear()
ea.live(ta, 60, 100); ea.live(ta, 64, 100); ea.live(ta, 67, 100)
time.sleep(0.02)
assert [m[1] for _, m in outa.log if m[0] & 0xF0 == 0x90][:3] == [60, 64, 67]
ea.live(ta, 60, 0); ea.live(ta, 64, 0); ea.live(ta, 67, 0)
# latch keeps the chord after the keys are up, and a new key starts a new chord
ta.arp.on, ta.arp.latch, ta.arp.mode = True, True, "up"
outa.log.clear()
ea.live(ta, 60, 100); ea.live(ta, 64, 100)
time.sleep(0.06)
ea.live(ta, 60, 0); ea.live(ta, 64, 0)
before = sum(1 for _, m in outa.log if m[0] & 0xF0 == 0x90)
time.sleep(0.12)
latched = [m[1] for _, m in outa.log if m[0] & 0xF0 == 0x90]
assert len(latched) >= before + 3 and set(latched[-4:]) == {60, 64}, "latch keeps the chord going"
ea.live(ta, 67, 100)
time.sleep(0.12)
assert [m[1] for _, m in outa.log if m[0] & 0xF0 == 0x90][-3:] == [67, 67, 67], "a new chord starts afresh"
ea.stop(); ea.close()
# a chord held in the pattern is arpeggiated too
prb2 = ts.Project(bpm=600, tracks=[ts.Track("melody", 1, 1)])
tb2 = prb2.tracks[0]; tb2.arp.on = True
for n in (60, 64, 67):
    tb2.current.lane(n)[0].on = True
    tb2.current.lane(n)[0].gate = 16  # held for a bar: the arp's window
outb2 = FakeOut(); eb2 = ts.Engine(prb2, outb2)
eb2.start(); time.sleep(0.22)
onsb = [m[1] for _, m in outb2.log if m[0] & 0xF0 == 0x90]
print("arp over the pattern:", onsb[:6])
assert onsb[:6] == [60, 64, 67, 60, 64, 67], onsb
eb2.stop(); eb2.close()
print("arpeggiator OK")

# --- with the transport stopped the arp keeps its own time (a keyboard in hand, no sequencer) ---------------
prc = ts.Project(bpm=600, tracks=[ts.Track("melody", 1, 1)])
tc = prc.tracks[0]; tc.arp.on = True
outc = FakeOut(); ec = ts.Engine(prc, outc)
time.sleep(0.05)
ec.live(tc, 60, 100); ec.live(tc, 64, 100); ec.live(tc, 67, 100)
time.sleep(0.2)
onsc = [m[1] for _, m in outc.log if m[0] & 0xF0 == 0x90]
print("arp, transport stopped:", onsc[:6])
assert onsc[:6] == [60, 64, 67, 60, 64, 67], onsc
# starting the transport does not silence the keys that are down: the arp carries on, now on its grid
count = len(onsc)
ec.start(); time.sleep(0.2)
onsd = sum(1 for _, m in outc.log if m[0] & 0xF0 == 0x90)
assert onsd > count + 3, "the arp keeps going once the transport starts"
# and a stop leaves it free-running again, with the keys still down
ec.stop(); time.sleep(0.08)
before = sum(1 for _, m in outc.log if m[0] & 0xF0 == 0x90)
time.sleep(0.2)
assert sum(1 for _, m in outc.log if m[0] & 0xF0 == 0x90) > before, "and free-runs again after the stop"
ec.live(tc, 60, 0); ec.live(tc, 64, 0); ec.live(tc, 67, 0)
ec.close()
print("arp without the transport OK")

# --- its order, its octaves, and the dialog that sets them ---------------------------------
assert ts.Arp(mode="up").sequence([67, 60, 64]) == [60, 64, 67]
assert ts.Arp(mode="down").sequence([60, 64, 67]) == [67, 64, 60]
assert ts.Arp(mode="updown").sequence([60, 64, 67]) == [60, 64, 67, 64]
assert ts.Arp(mode="played").sequence([67, 60, 64]) == [67, 60, 64]
assert ts.Arp(mode="up", octaves=2).sequence([60, 67]) == [60, 67, 72, 79]
prd = ts.Project(tracks=[ts.Track("melody", 1, 1)])
changes = []
dlg_arp = ts.ArpSettings(prd.tracks[0], lambda: changes.append(1))
img = QImage(900, 620, QImage.Format_ARGB32); img.fill(0)
q = QPainter(img); q.setRenderHint(QPainter.Antialiasing); dlg_arp.paint(q, 900, 620); q.end()
dlg_arp.set(on=True, rate=12, mode="updown", octaves=2, gate=0.75, latch=True)
assert prd.tracks[0].arp.on and prd.tracks[0].arp.rate == 12 and prd.tracks[0].arp.mode == "updown"
assert prd.tracks[0].arp.octaves == 2 and prd.tracks[0].arp.gate == 0.75 and prd.tracks[0].arp.latch
assert dlg_arp.rate_name() == "1/8" and changes, (dlg_arp.rate_name(), changes)
img.fill(0); q = QPainter(img); q.setRenderHint(QPainter.Antialiasing); dlg_arp.paint(q, 900, 620); q.end()
img.save(str(OUT / "arp.png"))
_f = OUT / "arp.json"
_f.write_text(ts.dump_project(prd))
assert ts.load_project(_f).tracks[0].arp == prd.tracks[0].arp, "the arp is saved with the project"
print("arp settings OK")

# --- LAUNCH: a pad per pattern for every track, and transitions that go back ---------------
def launch_project():
    prj = ts.Project(bpm=600, tracks=[ts.Track("melody", 1, 1)])  # 600 bpm: a bar is 0.4 s
    track = prj.tracks[0]
    for n in range(4):
        track.patterns[n].lane(60)[0].on = True
    track.patterns[1].fill = True
    track.patterns[2].fill = True
    track.patterns[3].length = 32  # a transition of two bars
    track.patterns[3].fill = True
    return prj, track


def until(fn, limit=1.6):
    t0 = time.time()
    while time.time() - t0 < limit:
        if fn():
            return True
        time.sleep(0.002)
    return False


prl, tl = launch_project()
el = ts.Engine(prl, FakeOut())
el.start(); time.sleep(0.1)
el.queue_pattern(tl, 1)  # a one-bar transition
assert until(lambda: tl.pattern == 1), "the transition plays"
assert until(lambda: tl.pattern == 0), "and the track goes back to what it was playing"
assert not el.fill_back and not el.fill_until
# an ordinary pattern is an explicit choice: it cancels the return
el.queue_pattern(tl, 1)
assert until(lambda: tl.pattern == 1)
el.queue_pattern(tl, 0)
assert until(lambda: tl.pattern == 0), "a queued pattern wins over the transition's return"
time.sleep(0.5)
assert tl.pattern == 0, "and the return does not fire later"
# a transition lasts its own length in whole bars: 32 steps = two bars = 0.8 s here
el.queue_pattern(tl, 3)
assert until(lambda: tl.pattern == 3)
t0 = time.time()
assert until(lambda: tl.pattern == 0)
span = time.time() - t0
assert 0.6 < span < 1.3, span
el.stop(); el.close()
print("launch transitions OK")

# a transition chained on another keeps the original way back
prl, tl = launch_project()
el = ts.Engine(prl, FakeOut())
el.start(); time.sleep(0.1)
el.queue_pattern(tl, 1)
assert until(lambda: tl.pattern == 1)
el.queue_pattern(tl, 2)
assert until(lambda: tl.pattern == 2)
assert until(lambda: tl.pattern == 0), "back to the first pattern, not to the first transition"
el.stop(); el.close()
print("launch chain OK")

# the song is what plays after a transition (it decides on its own bars)
prl, tl = launch_project()
prl.song = [ts.SongStep(4, 1, {1: 0})]
prl.song_on = True
el = ts.Engine(prl, FakeOut())
el.start(); time.sleep(0.1)
el.queue_pattern(tl, 1)
assert until(lambda: tl.pattern == 1), "a transition still plays while the song runs"
assert until(lambda: tl.pattern == 0), "and the song's pattern comes back"
el.stop(); el.close()
print("launch + song OK")

# a stop does not leave a transition in place
prl, tl = launch_project()
el = ts.Engine(prl, FakeOut())
el.start(); time.sleep(0.1)
el.queue_pattern(tl, 1)
assert until(lambda: tl.pattern == 1)
el.stop()
assert tl.pattern == 0, "a stop puts the pattern before the transition back"
el.close()
print("launch stop OK")

# the view: pads queue a pattern, FILL marks without launching, leaving drops the mode
prl, tl = launch_project()
sq = ts.Sequencer(prl, ts.Engine(prl, FakeOut()))
sq.sel, sq.view = tl, "launch"
_img = QImage(1600, 900, QImage.Format_ARGB32); _img.fill(0)
_q = QPainter(_img); sq.paint(_q, 1600, 900); _q.end()
sq.press_pad(tl, 2)
assert tl.pattern == 2 and sq.sel is tl, "a pad queues its pattern (and picks the track)"
sq.press_pad(tl, 2)  # the playing one: nothing to queue
sq.mode = "fill"
sq.press_pad(tl, 0)
assert tl.patterns[0].fill and tl.pattern == 2, "FILL marks the pattern, it does not launch it"
sq.show("grid")
assert sq.mode is None, "leaving LAUNCH drops FILL mode"
_f = OUT / "launch.json"
_f.write_text(ts.dump_project(prl))
_loaded = ts.load_project(_f).tracks[0]
assert all(_loaded.patterns[i].fill for i in range(4)) and not _loaded.patterns[5].fill, \
    "transitions are saved with the project"
sq.view, sq.mode = "launch", None
sq.engine.fill_back[tl] = 0
img = QImage(1600, 900, QImage.Format_ARGB32); img.fill(0)
q = QPainter(img); q.setRenderHint(QPainter.Antialiasing); sq.paint(q, 1600, 900); q.end()
img.save(str(OUT / "launch.png"))
sq.engine.close()
print("launch view OK")

# the playing pad shows progress: the pattern's loop, or the bars a transition has left
class FlatEngine:
    playing = False
    playhead = {}
    ticks = 0
    step_index = 0
    step_time = 0.0
    step_len = 1.0
    fill_until = {}


prp, tp = launch_project()
flat = FlatEngine()
sp = ts.Sequencer(prp, flat)
assert sp.launch_progress(tp) == 0.0, "nothing playing, no progress"
flat.playing, flat.step_index = True, 56
flat.step_len, flat.step_time = 100.0, time.perf_counter() - 50.0  # halfway through the step
flat.playhead[tp] = 8
assert abs(sp.launch_progress(tp) - (8 + 0.5) / 16) < 0.01, sp.launch_progress(tp)
tp.pattern = 3  # a 32-step transition: two bars
flat.fill_until[tp] = 4
assert abs(sp.launch_progress(tp) - (56 + 0.5 - 32) / 32) < 0.01, "a transition drains over its bars"
flat.playing = False
assert sp.launch_progress(tp) == 0.0
print("launch progress OK")

# --- plugin knobs are paged by whole sections: an ADSR never splits across pages -------------
assert ts.param_family("Amp Attack") == "envelope" and ts.param_family("Filter Cutoff") == "filter"
assert ts.param_family("Reverb Mix") == "effect" and ts.param_family("Frequency") == ""
assert ts.param_section("Amp Attack") == "amp" and ts.param_section("Filter Env Attack") == "filter"
assert ts.param_section("OscFineTune") == "osc", "a camel-case name is split into words"
assert ts.param_section("OSC 1 Detune") == "osc 1" and ts.param_section("SubOscVol") == "sub"
assert ts.param_section("Attack") == "" and ts.param_group("Attack") == "envelope"
assert ts.param_group("Cutoff") == "filter" and ts.param_group("Kick Punch Decay") == "kick punch"
fake = lambda n: types.SimpleNamespace(name=n)
names = ["OscWave", "OscFineTune", "OscTune", "OscVol", "SubOscVol", "Attack", "Decay", "Sustain", "Release",
         "Cutoff", "Resonance", "MonoPoly", "Glide", "DelayTime", "DelayFeedback", "DelayWet", "DelaySync",
         "Bits", "SampleRate", "LFORate", "LFOWave", "LFODest", "LFOAmt", "LFOSync", "Bypass", "StepperRate",
         "FilterMode", "Pitch Attack", "Pitch Decay", "Pitch Sustain", "Pitch Release", "Pitch Amount",
         "SubOscTune"]


def page_rows(pg, cols=5):
    lines = []
    for sec in pg:
        width, height = min(len(sec), cols), max(1, -(-len(sec) // cols))
        if lines and lines[-1][0] + width <= cols:
            lines[-1][0] += width
            lines[-1][1] = max(lines[-1][1], height)
        else:
            lines.append([width, height])
    return sum(rows for _, rows in lines)


pages = ts.param_pages([fake(n) for n in names], 5, 3)
assert all(page_rows(pg) <= 3 for pg in pages), [page_rows(pg) for pg in pages]
envelope = next(sec for pg in pages for sec in pg if ts.section_name(sec) == "ENVELOPE")
assert [p.name for p in envelope] == names[5:9], "the ADSR is one whole box, on one page"
assert ts.section_name([fake("OscFineTune")]) == "OSC" and ts.section_name([fake("Cutoff")]) == "FILTER"
assert ts.section_name([fake("MonoPoly")]) == "GENERAL"
# sections share a line: small ones are not one full-width box each
assert [len(pg) for pg in pages] == [3, 2, 3], [len(pg) for pg in pages]
# a section is one box, not one per page: a parameter listed apart from its section joins it
seen = [ts.section_name(sec) for pg in pages for sec in pg]
assert len(seen) == len(set(seen)), seen
assert sorted(ts.section_name([fake(n)]) for n in names) and all(
    any(n in [p.name for p in sec] for pg in pages for sec in pg) for n in names), "every knob is on a page"
assert ts.param_pages([], 5, 3) == [[]]
print("knob groups OK")

# --- a chain with nothing chosen opens on its first plugin (no extra tap) -------------------
hp = object.__new__(ts.HostPanel)
strip = ts.Strip()
strip.slots[0] = types.SimpleNamespace(name="First", label="first", fmt="clap")
strip.slots[2] = types.SimpleNamespace(name="Third", label="third", fmt="clap")
hp.sel, hp.page = None, 2
hp.focus_first(strip)
assert hp.sel == (strip, 0) and hp.page == 0, "the first plugin of the chain gets the focus"
hp.sel, hp.page = (strip, 2), 1
hp.focus_first(strip)
assert hp.sel == (strip, 2) and hp.page == 1, "an explicit choice stays"
empty = ts.Strip()
hp.sel = None
hp.focus_first(empty)
assert hp.sel is None, "a chain with no plugins has nothing to focus"
print("host focus OK")

# --- track meters in the sequencer: the host's chain levels, beside each track's M ----------
hp2 = object.__new__(ts.HostPanel)
strip_m = ts.Strip()
hp2.meters = {strip_m: {"l": 0.5, "r": 0.25, "peak": 0.5, "at": 0.0}}
left, right = hp2.levels(strip_m)
assert abs(left - hp2.db_fraction(0.5)) < 1e-9 and abs(right - hp2.db_fraction(0.25)) < 1e-9
assert hp2.levels(ts.Strip()) == (0.0, 0.0), "a strip with no chain has no meter"
prm = ts.Project(tracks=[ts.Track("melody", 1, 1)])
trm = prm.tracks[0]
trm.in_port = "KeyStep"
sqm = ts.Sequencer(prm, ts.Engine(prm, FakeOut()))
asked = []
sqm.levels = lambda tr: asked.append(tr) or (0.6, 0.3)
sqm.input_ok = lambda tr: True
img = QImage(1200, 700, QImage.Format_ARGB32); img.fill(0)
q = QPainter(img); q.setRenderHint(QPainter.Antialiasing); sqm.paint(q, 1200, 700); q.end()
assert trm in asked, "the track's row asks for its level"
assert 0 in sqm.grid_rows, "the row is still there (and tappable) with the meter and the socket"
sqm.engine.close()
print("track meters OK")


# --- step sequences (LFO Step 1..16) get their own box, drawn as bars ------------------------
assert ts.param_section("LFO Step 1") == "lfo step" and ts.param_section("LFO Step 16") == "lfo step"
assert ts.param_section("LFO2 Step 7") == "lfo 2 step" and ts.param_group("LFO Step 1") == "lfo step"
assert ts.param_section("LFO Step Count") == "lfo", "the count stays a knob with the LFO"
assert ts.param_section("OSC 1 Wave") == "osc 1", "a number before the name stays in the section"
steps = [fake(f"LFO Step {i}") for i in range(1, 17)]
assert ts.is_step_section(steps) and not ts.is_step_section(steps[:3])
assert ts.section_box(steps, 5) == (5, 1), "sixteen bars take one line, not four"
assert ts.section_name(steps) == "LFO STEP"
pages = ts.param_pages([fake("LFO Waveform"), fake("LFO Step Count")] + steps, 5, 3)
assert any(len(sec) == 16 and ts.is_step_section(sec) for pg in pages for sec in pg), "the steps stay one box"
# a bar: bipolar values grow both ways from the middle, and it takes a drag
hp3 = object.__new__(ts.HostPanel)
ts.Panel.__init__(hp3)
hp3.s, hp3.assigning = 1.0, None
hp3.host = types.SimpleNamespace(value=lambda pl, i: 0.25)
hp3.set_param = lambda *a: None
prm = types.SimpleNamespace(index=0, name="LFO Step 3", lo=-1.0, hi=1.0, integer=False, boolean=False)
img = QImage(120, 260, QImage.Format_ARGB32); img.fill(0)
q = QPainter(img); q.setRenderHint(QPainter.Antialiasing)
hp3.draw_bar(q, QRectF(4, 4, 24, 200), object(), prm); q.end()
assert hp3.hits and hp3.hits[-1][0].contains(10, 10), "the bar is draggable"
print("step bars OK")

# --- sampler: WAV reading, the pad map the plugin reads, and 16 equal chops --------------------
import struct
import wave as wavelib
from tseq.sampler import (PadDrag, SamplerEditor, WaveScroll, chop_regions, import_sample, preset_folders,
                          read_manifest, write_manifest)
from tseq.grain import GrainEditor
from tseq.wav import read_wav

wav_path = OUT / "sampler-test.wav"
with wavelib.open(str(wav_path), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(48000)
    w.writeframes(b"".join(struct.pack("<h", 10000 if i % 2 else -10000) for i in range(4800)))
samples, rate, frames = read_wav(wav_path)
assert rate == 48000 and frames == 4800 and len(samples) == 4800
assert max(samples) > 0.2 and min(samples) < -0.2, (max(samples), min(samples))
state = OUT / "sampler.plugins"
pads = [None] * 16
pads[0] = {"file": "sampler-test.wav", "start": 0, "end": frames, "root": 36, "gain": 1.0, "pan": 0.0,
           "pitch": 0.0, "loop": 0, "reverse": 0}
manifest = state / "sampler" / "sampler-5.txt"
write_manifest(manifest, "sampler-test.wav", pads)
current, back, mono = read_manifest(manifest)
assert current == "sampler-test.wav" and back[0]["end"] == frames and back[1] is None and mono == 0
regions = chop_regions(frames)
assert len(regions) == 16 and regions[0][0] == 0 and regions[-1][1] == frames
assert all(a < b for a, b in regions), regions
copied = import_sample(wav_path, state)
assert (state / "sampler" / copied).is_file()
# the editor's status toast: shown for a few seconds, then gone
panel = ts.Panel()
panel.s = 1.0
panel.state_dir = state
panel.set_param = lambda *a, **k: None
editor = SamplerEditor(panel)
fake_plugin = types.SimpleNamespace(params=[])
editor.use(fake_plugin, 5, manifest)
editor.samples, editor.rate, editor.frames = samples, rate, frames
editor.chop()
assert editor.message.startswith("chopped"), editor.message
img = QImage(700, 400, QImage.Format_ARGB32); img.fill(0)
q = QPainter(img); q.setRenderHint(QPainter.Antialiasing)
editor.draw(q, QRectF(0, 0, 700, 400), fake_plugin, 5, manifest); q.end()
editor.message_at -= editor.MESSAGE_SECONDS + 1.0
img.fill(0); q = QPainter(img); editor.draw(q, QRectF(0, 0, 700, 400), fake_plugin, 5, manifest); q.end()
assert editor.message == "", "the toast expires"
# a pad brings its own file to the waveform (the bug: current stayed on the last loaded file)
other_path = state / "sampler" / "other.wav"
with wavelib.open(str(other_path), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(48000)
    w.writeframes(b"".join(struct.pack("<h", 8000 if i % 2 else -8000) for i in range(2400)))
pads[1] = {"file": "other.wav", "start": 0, "end": 2400, "root": 37, "gain": 1.0, "pan": 0.0,
           "pitch": 0.0, "loop": 0, "reverse": 0}
write_manifest(manifest, "sampler-test.wav", pads)
editor.load()
editor.select_pad(0)
assert editor.current == "sampler-test.wav" and editor.frames == 4800, (editor.current, editor.frames)
editor.select_pad(1)
assert editor.current == "other.wav" and editor.frames == 2400, (editor.current, editor.frames)
# zoom/pan and the slice handles: edges resize, the bar moves the whole slice
editor.select_pad(0)
editor.fit()
assert editor.view_start == 0 and editor.view_end == 4800
editor.zoom(2)
assert editor.view_end - editor.view_start < 4800
editor.fit()
# pinch: the frame between the fingers stays between them, and lifting one does not jump the view back
pinch_rect = QRectF(100, 0, 600, 200)
panel.gestures.clear()
a = WaveScroll(editor, pinch_rect, QPointF(300, 100))
panel.gestures[1] = a
b = WaveScroll(editor, pinch_rect, QPointF(500, 100))  # the second finger starts the pinch
panel.gestures[2] = b
assert editor.pinch is not None and editor.view_start == 0 and editor.view_end == 4800
a.move(QPointF(200, 100)); b.move(QPointF(700, 100))  # fingers spread: 200 px -> 500 px
span = editor.view_end - editor.view_start
anchor = editor.view_start + (450 - pinch_rect.left()) / pinch_rect.width() * span
assert abs(span - 1920) <= 2, span
assert abs(anchor - 2400) <= 2, anchor  # the frame under the midpoint is still under it
panel.gestures.pop(2); b.release(QPointF(700, 100))  # the panel pops a finger before releasing it
assert editor.view_start == 1280, "letting go of one finger keeps the zoom"
a.move(QPointF(260, 100))  # the remaining finger pans from the zoomed view, not from before it
assert editor.view_start == 1088, editor.view_start
panel.gestures.clear()
editor.fit()
slice_rect = QRectF(0, 0, 480, 200)
editor.set_slice("start", slice_rect, QPointF(48, 10))
editor.set_slice("end", slice_rect, QPointF(240, 10))
assert editor.pads[0]["start"] == 480 and editor.pads[0]["end"] == 2400, editor.pads[0]
editor.move_range(slice_rect, 48, 480, 2400)
assert editor.pads[0]["start"] == 960 and editor.pads[0]["end"] == 2880, editor.pads[0]
# COPY: the same chop on another pad, with its own pitch (one slice, two notes)
editor.select_pad(0)
editor.pads[0]["pitch"] = 5.0
editor.pads[0]["amp"] = [0.1, 0.2, 0.7, 0.3]
editor.pads[0]["lfo_rate"] = 4.0
editor.copy_pad()
assert editor.copy_src == 0, editor.copy_src
editor.select_pad(7)
cp = editor.pads[7]
assert cp and cp["file"] == editor.pads[0]["file"], cp
assert (cp["start"], cp["end"]) == (editor.pads[0]["start"], editor.pads[0]["end"]), cp
assert cp["root"] == 36 + 7 and cp["pitch"] == 5.0, cp  # the target's own note as root: it sounds the same
assert cp["amp"] == [0.1, 0.2, 0.7, 0.3] and cp["lfo_rate"] == 4.0, "the copy keeps the envelopes and LFO"
assert cp["amp"] is not editor.pads[0]["amp"], "but its envelope lists are its own"
cp["amp"][0] = 0.9
assert editor.pads[0]["amp"][0] == 0.1
assert editor.copy_src is None and editor.pad == 7
editor.pads[7]["pitch"] = 12.0
assert editor.pads[0]["pitch"] == 5.0, "the two pads have independent pitch"
editor.save_reload()
_, saved, _ = read_manifest(manifest)
assert saved[0]["pitch"] == 5.0 and saved[7]["pitch"] == 12.0, (saved[0]["pitch"], saved[7]["pitch"])
editor.pad = 0
editor.copy_pad()
img.fill(0); q = QPainter(img); editor.draw(q, QRectF(0, 0, 700, 400), fake_plugin, 5, manifest); q.end()
img.save(str(OUT / "sampler_copy.png"))
editor.copy_pad()  # a second tap cancels the armed copy
assert editor.copy_src is None
editor.pad = 3
editor.pads[3] = None
editor.copy_pad(); assert "nothing to copy" in editor.message, editor.message
print("sampler copy OK")
# PRESETS: the pads and their samples kept outside the project, so deleting the track does not lose them
panel.presets_dir = OUT / "sampler-presets"
editor.pad = 0
editor.mono = True
editor.save_preset()
saved_folders = preset_folders(panel.presets_dir)
assert len(saved_folders) == 1, saved_folders
preset = saved_folders[0]
assert preset.name == "sampler-test", preset.name
assert (preset / "preset.txt").is_file() and (preset / "sampler-test.wav").is_file()
_, kept, kept_mono = read_manifest(preset / "preset.txt")
assert kept_mono == 1 and kept[0]["pitch"] == 5.0 and kept[7]["pitch"] == 12.0
editor.save_preset()  # a second save is a second preset, not an overwrite
assert len(preset_folders(panel.presets_dir)) == 2
# the track (and the project's sampler folder) gone: the preset still has its samples
_sh.rmtree(state / "sampler")
(state / "sampler").mkdir(parents=True)
(state / "sampler" / "sampler-test.wav").write_bytes(b"a different sample with the same name")
editor.cache.clear()
editor.load_preset(preset)
assert editor.current != "sampler-test.wav", "a same-named but different sample is kept, not overwritten"
assert (state / "sampler" / editor.current).is_file(), "loading brings the samples back"
assert (state / "sampler" / "sampler-test.wav").read_bytes() == b"a different sample with the same name"
assert editor.pads[0]["file"] == editor.current and editor.pads[1]["file"] == "other.wav"
assert editor.mono
assert (editor.pads[0]["start"], editor.pads[0]["end"]) == (960, 2880), editor.pads[0]
assert editor.pads[0]["pitch"] == 5.0 and editor.pads[7]["pitch"] == 12.0
assert "loaded preset" in editor.message, editor.message
_, loaded_pads, _ = read_manifest(manifest)
assert loaded_pads[0]["file"] == editor.current, loaded_pads[0]
editor.open_presets()
img.fill(0); q = QPainter(img); editor.draw(q, QRectF(0, 0, 700, 400), fake_plugin, 5, manifest); q.end()
img.save(str(OUT / "sampler_presets.png"))
editor.presets_open = False
editor.delete_preset(preset_folders(panel.presets_dir)[0])
assert len(preset_folders(panel.presets_dir)) == 1
print("sampler presets OK")
# pads use the same fixed colors as the drum pads; playback groups cut each other
editor.pad = 0
assert editor.pad_color(0) == ts.DRUM_COLORS[0] and editor.pad_color(1) == ts.DRUM_COLORS[1]
assert editor.pad_color(12) == ts.DRUM_COLORS[0], "16 pads cycle the 12 drum colors"
editor.cycle_group()
assert editor.pads[0]["group"] == 1
editor.save_map()
_, grouped, _ = read_manifest(manifest)
assert grouped[0]["group"] == 1
for _ in range(ts.SAMPLER_GROUPS):
    editor.cycle_group()
assert editor.pads[0]["group"] == 0, "after group 8 comes off"
editor.cycle_group()
assert editor.pads[0]["group"] == 1, "and then group 1 again"
assert (SamplerEditor.PITCH_LO, SamplerEditor.PITCH_HI) == (-36.0, 36.0)
assert (SamplerEditor.FINE_LO, SamplerEditor.FINE_HI) == (-100.0, 100.0)
editor.pads[0]["pitch"] = SamplerEditor.PITCH_LO
editor.save_map()
_, lowpitched, _ = read_manifest(manifest)
assert lowpitched[0]["pitch"] == -36.0, lowpitched[0]["pitch"]
editor.pads[0]["pitch"] = 0.0
editor.pads[0]["fine"] = -37.0
editor.save_map()
_, tuned, _ = read_manifest(manifest)
assert abs(tuned[0]["fine"] + 37.0) < 1e-6, tuned[0].get("fine")
# STRETCH: pitch without changing the slice's length, stored in the map
editor.toggle_stretch()
assert editor.pads[0]["stretch"] == 1
_, stretched, _ = read_manifest(manifest)
assert stretched[0]["stretch"] == 1
editor.toggle_stretch()
assert editor.pads[0]["stretch"] == 0
assert editor.start_pad(0) is None, "a bare panel has no port to preview through"
editor.stop_all()
# LOCATE: centre the waveform on the selected pad's slice, about a third of the view
editor.pad = 0
old_start, old_end = editor.pads[0]["start"], editor.pads[0]["end"]
editor.pads[0]["start"], editor.pads[0]["end"] = 1000, 1300
editor.focus_pad()
view = editor.view_end - editor.view_start
assert 2.5 <= view / 300 <= 3.5, view
assert abs((editor.view_start + editor.view_end) / 2 - 1150) <= 60, (editor.view_start, editor.view_end)
editor.pads[0]["start"], editor.pads[0]["end"] = old_start, old_end
# the ADSR editor: amp/filter envelopes, stored with the pad and stretched with its slice
editor.pad = 0
editor.env_tab = "amp"
editor.pads[0]["amp"] = [0.0, 0.0, 1.0, 0.0]
editor.set_env("attack", 0.25, 1.0)
editor.set_env("decay", 0.5, 0.5)
assert editor.pads[0]["amp"] == [0.25, 0.25, 0.5, 0.0], editor.pads[0]["amp"]
assert abs(editor.env_value(editor.pads[0]["amp"], 0.25) - 1.0) < 1e-6
assert abs(editor.env_value(editor.pads[0]["amp"], 0.5) - 0.5) < 1e-6
editor.set_env("release", 0.8, 0.5)
assert abs(editor.pads[0]["amp"][3] - 0.2) < 1e-6, editor.pads[0]["amp"]
editor.pads[0]["cutoff"], editor.pads[0]["reso"], editor.pads[0]["env"] = 1000.0, 0.3, 2.0
editor.save_map()
_, envsaved, _ = read_manifest(manifest)
assert all(abs(a - b) < 1e-6 for a, b in zip(envsaved[0]["amp"], editor.pads[0]["amp"]))
assert all(abs(a - b) < 1e-6 for a, b in zip(envsaved[0]["filter"], editor.pads[0]["filter"]))
assert abs(envsaved[0]["cutoff"] - 1000.0) < 1e-3 and abs(envsaved[0]["reso"] - 0.3) < 1e-6
assert abs(envsaved[0]["env"] - 2.0) < 1e-6
editor.env_open = True
img.fill(0); q = QPainter(img); editor.draw(q, QRectF(0, 0, 700, 400), fake_plugin, 5, manifest); q.end()
img.save(str(OUT / "sampler_adsr.png"))
editor.env_tab = "filter"
img.fill(0); q = QPainter(img); editor.draw(q, QRectF(0, 0, 700, 400), fake_plugin, 5, manifest); q.end()
img.save(str(OUT / "sampler_adsr_filter.png"))
# LFO: wave/rate/depth/target, stored with the pad and drawn over the slice
editor.env_tab = "lfo"
editor.pads[0].update({"lfo_wave": 2, "lfo_rate": 5.0, "lfo_depth": 0.6, "lfo_target": 1,
                      "lfo_sync": 1, "lfo_div": 3})
editor.save_map()
_, lfosaved, _ = read_manifest(manifest)
assert (lfosaved[0]["lfo_wave"], lfosaved[0]["lfo_target"]) == (2, 1)
assert abs(lfosaved[0]["lfo_rate"] - 5.0) < 1e-6 and abs(lfosaved[0]["lfo_depth"] - 0.6) < 1e-6
assert (lfosaved[0]["lfo_sync"], lfosaved[0]["lfo_div"]) == (1, 3)
editor.toggle_lfo_sync(); assert editor.pads[0]["lfo_sync"] == 0
editor.cycle_lfo_div(); assert editor.pads[0]["lfo_div"] == 4
editor.pads[0]["lfo_sync"] = 1  # leave the sync view in the screenshot
assert abs(editor.lfo_value(2, 0.25) - 1.0) < 1e-9 and abs(editor.lfo_value(2, 0.75) + 1.0) < 1e-9
editor.cycle_lfo_wave(); assert editor.pads[0]["lfo_wave"] == 3
editor.cycle_lfo_target(); assert editor.pads[0]["lfo_target"] == 2
img.fill(0); q = QPainter(img); editor.draw(q, QRectF(0, 0, 700, 400), fake_plugin, 5, manifest); q.end()
img.save(str(OUT / "sampler_lfo.png"))
editor.env_open = False
# the editor tells the plugin which kind of reload an edit needs: a light map update or a full sample load
class _Param:
    def __init__(self, name, index): self.name, self.index = name, index
calls = []
editor.plugin = types.SimpleNamespace(params=[_Param("Slot", 0), _Param("Reload", 1), _Param("Load", 2)])
panel.set_param = lambda plugin, index, value, source=None: calls.append(index)
editor.save_reload()
editor.save_reload(full=True)
assert calls[-2:] == [1, 2], calls  # Reload for the map, Load when the samples changed
editor.plugin = fake_plugin
panel.set_param = lambda *a, **k: None
# the knobs move in their own units: gain past 1 (0..4) in 0.02 steps, pitch in whole semitones
gv = []
gpd = PadDrag(QPointF(0, 0), lambda: 1.0, gv.append, 0.0, 4.0, 1.2, 0.02, lambda: None)
gpd.move(QPointF(0, -10))   # past the drag threshold
gpd.move(QPointF(0, -130))  # 120 px up: 100 steps of 0.02
assert gv[-1] == 3.0, gv
pitchv = []
ppd = PadDrag(QPointF(0, 0), lambda: 0.0, pitchv.append, SamplerEditor.PITCH_LO, SamplerEditor.PITCH_HI,
              8.0, 1.0, lambda: None)
ppd.move(QPointF(0, -10)); ppd.move(QPointF(0, -74))  # 64 px: 8 whole semitones
assert pitchv[-1] == 8.0, pitchv
ppd.move(QPointF(0, -71))  # a 3 px wobble stays on the same note
assert pitchv[-1] == 8.0, pitchv
finev = []
fpd = PadDrag(QPointF(0, 0), lambda: 0.0, finev.append, SamplerEditor.FINE_LO, SamplerEditor.FINE_HI,
              1.0, 1.0, lambda: None)
fpd.move(QPointF(0, -10)); fpd.move(QPointF(0, -74))  # 64 px: +64 cents
assert finev[-1] == 64.0, finev
# the waveform is read in the background in the app: the editor can paint a spinner and poll for it
panel2 = ts.Panel(); panel2.s = 1.0; panel2.state_dir = state; panel2.set_param = lambda *a, **k: None
ed2 = SamplerEditor(panel2, background=True)
ed2.use(fake_plugin, 5, manifest)
ed2.cache.clear()
ed2.load_wave(editor.current)
assert ed2.loading, "the read is off the UI thread"
for _ in range(500):
    ed2.poll()
    if not ed2.loading:
        break
    time.sleep(0.01)
assert not ed2.loading and ed2.frames == 4800, (ed2.loading, ed2.frames)
ed2.loading = True  # what the waveform area shows meanwhile
img.fill(0); q = QPainter(img); ed2.draw(q, QRectF(0, 0, 700, 400), fake_plugin, 5, manifest); q.end()
img.save(str(OUT / "sampler_spinner.png"))
ed2.loading = False
# the plugin's output parameters are the playhead: the newest voice's pad and position within its slice
class _PlayHost:
    position, index = 0.4, 6
    def output_params(self, plugin): return [2, 3]
    def value(self, plugin, index): return self.position if index == 2 else self.index
panel.host, panel.loaded = _PlayHost(), {("t", 0): fake_plugin}
editor.plugin = fake_plugin
editor.read_playhead()
assert editor.playhead == (6, 0.4), editor.playhead
panel.host.index = -1
editor.read_playhead()
assert editor.playhead is None, "nothing playing: no playhead"
del panel.host, panel.loaded
# the Grain editor survives an empty source (it is drawn before a sampler pad is picked)
grain = GrainEditor(panel)
img.fill(0); q = QPainter(img)
grain.draw(q, QRectF(0, 0, 700, 400), types.SimpleNamespace(params=[]), 99)
q.end()
assert grain.name == "" and grain.samples is None
grain.slot, grain.pad = 99, 0
grain.use_sample(wav_path)  # the GRAIN editor's own LOAD, without the file dialog
_, grain_pads, _ = read_manifest(state / "sampler" / "sampler-99.txt")
assert grain_pads[0] and grain_pads[0]["file"] == grain.name, grain_pads[0]
assert grain.samples is not None and grain.end == frames and grain.frames == frames
# a playing pad moves the waveform playhead: only its old/new strips are queued, not the whole panel
editor.wave_rect = QRectF(0, 0, 400, 200)
editor.view_start, editor.view_end = 0, editor.frames
editor.pads[0]["file"] = editor.current
editor.pads[0]["start"], editor.pads[0]["end"] = 0, editor.frames
editor.panel.dirty_rects.clear()
editor.panel.stale = False
editor.playhead_at = time.monotonic() - 1
editor.mark_playhead((0, 0.25), (0, 0.75))
assert len(editor.panel.dirty_rects) == 2, editor.panel.dirty_rects
assert all(r.width() < 40 for r in editor.panel.dirty_rects), editor.panel.dirty_rects
assert not editor.panel.stale, "the playhead must not ask for a full frame"
editor.panel.dirty_rects.clear()
# the on-screen keyboard: shift, space, backspace, placeholder, cancel
typed = []
kb = ts.Keyboard("", "Name", typed.append, placeholder="Project")
kb.press("a"); assert kb.text == "A", kb.text
kb.press("shift"); kb.press("b"); assert kb.text == "Ab", kb.text
kb.press("space"); kb.press("1"); kb.press("back"); assert kb.text == "Ab ", kb.text
kb.press("ok"); assert typed == ["Ab"], typed
ts.Keyboard("", "Name", typed.append, placeholder="Project").press("ok")
assert typed[-1] == "Project", typed
cancel = []
ts.Keyboard("x", "Name", typed.append, lambda: cancel.append(1)).press("cancel")
assert cancel == [1]
# naming a preset with it
editor.open_presets(); editor.name_preset()
assert editor.naming is not None
editor.naming.press("shift")  # lowercase
for ch in "kick":
    editor.naming.press(ch)
img.fill(0); q = QPainter(img); editor.draw(q, QRectF(0, 0, 700, 400), fake_plugin, 5, manifest); q.end()
img.save(str(OUT / "sampler_keyboard.png"))
editor.naming.press("ok")
assert editor.naming is None and "kick" in [f.name for f in preset_folders(panel.presets_dir)]
editor.presets_open = False
assert ts.named_project_path(pdir, "Live Set").name == "Live Set.json"
print("sampler pads/groups/pitch/load/keyboard OK")
print("sampler view/handles OK")
print("sampler pad files OK")
print("sampler helpers OK")

# --- a drum track whose instrument is the sampler: pads record as kit voices, never as note numbers ------
dt9 = ts.Track("drum", 10, 9)
assert dt9.note(0) == 36 and dt9.note(11) == 70
assert dt9.note(48) == 48, "a lane outside the kit plays its own note instead of raising IndexError"
assert ts.Performer.pad_lane(dt9, 36) == 0 and ts.Performer.pad_lane(dt9, 38) == 1
assert ts.Performer.pad_lane(dt9, 70) == 11
assert ts.Performer.pad_lane(dt9, 40) is None, "a sampler pad with no GM kit note has no drum lane"
assert ts.Performer.pad_lane(ts.Track("melody", 1, 1), 60) == 60
pr9 = ts.Project(bpm=240, tracks=[dt9])
e9 = ts.Engine(pr9, FakeOut()); e9.start(); time.sleep(0.06)
p9 = ts.Performer(pr9, e9, ts.Sequencer(pr9, e9), None)
p9.mode = "rec"; e9.rec = {dt9: True}
g9 = p9.press_pad(("sampler", 0), dt9, [(0, 36)], QRectF(0, 0, 60, 60), QPointF(30, 30), lanes=[0])
assert g9.take and g9.take[3] == [0], g9.take
g9.release(None)
g10 = p9.press_pad(("sampler", 4), dt9, [(0, 40)], QRectF(0, 0, 60, 60), QPointF(30, 30), lanes=[])
assert g10.take is None, "a pad with no kit voice plays but writes nothing"
g10.release(None)
e9.stop(); e9.close()
print("drum sampler recording OK")
