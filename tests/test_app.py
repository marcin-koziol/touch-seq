"""Sequencer, UI, projects and live-play tests; no Carla or audio needed (CI runs these).
Usage: QT_QPA_PLATFORM=offscreen python tests/test_app.py [output dir for screenshots]"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import touchseq as ts
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QPainter


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
w.press(9, QPointF(cx(1), 441)); w.release(9, QPointF(cx(1), 441))
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
# BPM popup
w.resize(1024, 600); w.view = "grid"; w.sel = pr2.tracks[0]; w.grab()
def tap(x, y):
    w.press(5, QPointF(x, y)); w.release(5, QPointF(x, y)); w.grab()
b0 = pr2.bpm
tap(150, 30); assert w.popup == "bpm" and pr2.bpm == b0
w.grab().save(str(OUT / "bpm_popup.png"))
# find +10 button: steps row; use hit regions by label position: panel centered, buttons row y ~ 300+
import touchseq
tap(512 + 250, 300 - 190 + 18 + 26 + 120 + 16 + 35)  # rightmost (+10)
print("after +10:", pr2.bpm); assert pr2.bpm == b0 + 10
w.press(6, QPointF(512, 200)); w.move(6, QPointF(512, 190)); w.move(6, QPointF(512, 150)); w.release(6, QPointF(512, 150)); w.grab()
print("after drag up 40px:", pr2.bpm); assert pr2.bpm == b0 + 20
for t in (0, 0.5, 1.0, 1.5):
    w.taps = [x - 0.5 for x in w.taps]  # simulate time
w.taps = []
import time as _t
now = _t.monotonic(); w.taps = [now - 1.5, now - 1.0, now - 0.5]; w.tap_tempo(); print("tap tempo 120 ->", pr2.bpm); assert pr2.bpm == 120
tap(5, 5); assert w.popup is None
print("popup OK")
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
    def draw(self, p):
        p.fillRect(self.rect(), ts.QColor(ts.BG))
    def save_states(self):
        pass
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
assert opened == [pdir / "Project 1.json"] and opened[0].exists(), opened
ch.new(); time.sleep(0.02)
(pdir / "Project 1.plugins").mkdir(); (pdir / "Project 1.plugins" / "s1.carxs").write_text("x")
assert ts.free_project_path(pdir, "Project 1").name == "Project 3.json"
ch.grab().save(str(OUT / "chooser.png"))
rows = [r for r, f in ch.hits if r.height() > 60 and r.width() > 1000]
first = [p.name for p in ts.project_files(pdir)]
print("projects newest first:", first); assert first == ["Project 2.json", "Project 1.json"]
ch.press(2, rows[1].center()); ch.release(2, rows[1].center()); assert opened[-1].name == "Project 1.json"
dels = [r for r, f in ch.hits if 150 < r.width() < 300 and r.top() > 150]
ch.press(3, dels[-1].center()); time.sleep(0.9); ch.tick(); ch.release(3, dels[-1].center())
assert not (pdir / "Project 1.json").exists() and not (pdir / "Project 1.plugins").exists()
# duplicate from the rack copies json + plugin states and asks for a restart into the copy
src = pdir / "Project 2.json"; (pdir / "Project 2.plugins").mkdir(); (pdir / "Project 2.plugins" / "a.carxs").write_text("state")
prd = ts.load_project(src)
rk = ts.Rack(prd, ts.Engine(prd, FakeOut()), src, StubHost())
rk.duplicate()
print("restart:", rk.restart, rk.next_project.name)
assert rk.restart and rk.next_project.name == "Project 1.json" and (pdir / "Project 1.plugins" / "a.carxs").read_text() == "state"
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
pat.lane(72)[6].on = True  # will be erased by REC
perf.mode, perf.quant = "rec", 2
perf.tick(); ep.start()
time.sleep(step_len * 3.3)                     # playhead at step 3
g1 = perf.press_pad(1, mel, [(60, 60), (67, 67)], r, QPointF(50, 50))
time.sleep(step_len * 2)
g1.release(None)
time.sleep(step_len * 5)
on = sorted((k, i, round(st.gate, 3)) for k, lane in pat.lanes.items() for i, st in enumerate(lane) if st.on)
print("recorded (REC, 1/8):", on)
assert [(k, i) for k, i, _ in on] == [(60, 4), (67, 4)] and all(1.5 <= gt <= 2.5 for _, _, gt in on), on
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
ep.stop(); perf.mode = None; perf.tick(); assert ep.rec is None
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
pk = rkp.modules[3][4]; QRectF = ts.QRectF
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
vn.grab(); nt.press(2, QPointF(850, 100))  # EXTERNAL: shows the outputs instead
assert nt.picking and len(picked) == 2
vn.grab().save(str(OUT / "new_track_ports.png"))
row = next(r for r, f in nt.hits if r.top() > 70).center()  # hits: BACK, RESCAN, then a row per output
nt.press(3, row); nt.release(3, row)
assert picked[-1] == ("external", "Synth MIDI 1"), picked
nt.press(4, QPointF(850, 100))  # ... and the last row adds the track without a port
vn.grab(); row = [r for r, f in nt.hits if r.top() > 70][-1].center()
nt.press(5, row); nt.release(5, row); assert picked[-1] == ("external", ""), picked
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
        self.client, self.ref = client, type("Ref", (), {"name": name, "midi_ins": 1})()
class FakeCarla:
    def __init__(self):
        self.volumes = []
    def ports(self, plugin):
        return 2, 2
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

prb = ts.Project(tracks=[ts.Track("melody", 1, 1), ts.Track("melody", 2, 2)])
prb.buses.append(ts.Bus(1))
prb.buses[0].strip.volume = 0.5
prb.master.volume = 0.8
t1, t2, bus = prb.tracks[0], prb.tracks[1], prb.buses[0]
bh = FakeHostPanel(prb)
bh.loaded = {(t1.strip, 1): FakePlugin("T1.2 Rev"), (t2.strip, 1): FakePlugin("T2.2 Rev"),
             (bus.strip, 0): FakePlugin("B1.1 Rev", "Reverb")}
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
