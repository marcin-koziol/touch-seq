"""The sampler editor: a WAV waveform, chop markers, 16 MPC-style pads and the selected pad's controls. It
draws in HOST-4's parameter area (the sampler has no window of its own) and keeps the pad map in the project's
plugins folder, where the sampler plugin reads it."""
import math
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPen, QPolygonF
from PySide6.QtWidgets import QFileDialog

from .constants import (BG, BORDER, DIM, DRUM_COLORS, PANEL, PLAY, SAMPLER_GROUPS, SAMPLER_NOTE_BASE,
                        SAMPLER_PADS, TEXT, WARN)
from .gestures import Drag, Gesture, Hold, ScrollOrTap
from .ui import Keyboard, split
from .wav import peaks_range, read_wav, table_peaks


def sampler_dir(state_dir):
    return Path(state_dir) / "sampler"


def manifest_path(state_dir, slot):
    return sampler_dir(state_dir) / f"sampler-{int(slot)}.txt"


def presets_dir(panel):
    """Where sampler presets live: a machine folder (the host passes it), not the project's plugin folder, so
    deleting a track or a project does not take them with it."""
    folder = getattr(panel, "presets_dir", None)
    if folder is None:  # tests and bare panels: beside the projects
        folder = Path(getattr(panel, "state_dir", ".")).parent / "sampler-presets"
    return Path(folder)


def preset_folders(folder):
    """Saved presets, newest first: one folder each, with preset.txt and the samples it uses."""
    try:
        dirs = [d for d in Path(folder).iterdir() if (d / "preset.txt").is_file()]
    except OSError:
        return []
    return sorted(dirs, key=lambda d: (d / "preset.txt").stat().st_mtime, reverse=True)


def free_preset_dir(folder, stem="Preset"):
    """A fresh folder name for a preset: the sample's name, then -2, -3, ..."""
    folder = Path(folder)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "Preset"
    path, number = folder / stem, 2
    while path.exists():
        path = folder / f"{stem}-{number}"
        number += 1
    return path


def store_sample(source, folder, name=None):
    """Put a sample in another folder: a hard link when the filesystem allows it (samples are never rewritten
    in place - import always picks a free name), else a copy."""
    source, destination = Path(source), Path(folder) / (name or Path(source).name)
    if not source.is_file() or destination.exists():
        return destination
    try:
        os.link(source, destination)
    except OSError:
        shutil.copyfile(source, destination)
    return destination


def free_sample_name(folder, name):
    """A free file name in `folder`, for a sample whose name is already taken by a different file."""
    path = Path(name)
    candidate, number = path.name, 2
    while (Path(folder) / candidate).exists():
        candidate = f"{path.stem}-{number}{path.suffix}"
        number += 1
    return candidate


def read_manifest(path):
    """(current file, pads, mono): the pad map the plugin reads, or an empty one."""
    pads = [None] * SAMPLER_PADS
    current = ""
    mono = 0
    try:
        for line in Path(path).read_text().splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "mono" and len(parts) >= 2:
                mono = int(parts[1])
            elif parts[0] == "current" and len(parts) >= 2:
                current = parts[1]
            elif parts[0] == "pad" and len(parts) >= 11:
                index = int(parts[1])
                if 0 <= index < SAMPLER_PADS:
                    pad = {"file": parts[2], "start": int(parts[3]), "end": int(parts[4]),
                           "root": int(parts[5]), "gain": float(parts[6]), "pan": float(parts[7]),
                           "pitch": float(parts[8]), "loop": int(parts[9]), "reverse": int(parts[10]),
                           "gate": int(parts[11]) if len(parts) >= 12 else 0,
                           "group": int(parts[12]) if len(parts) >= 13 else 0,
                           "stretch": int(parts[13]) if len(parts) >= 14 and parts[13].isdigit() else 0}
                    if len(parts) >= 25:  # envelopes and the filter
                        pad["amp"] = [float(parts[k]) for k in (14, 15, 16, 17)]
                        pad["filter"] = [float(parts[k]) for k in (18, 19, 20, 21)]
                        pad["cutoff"], pad["reso"], pad["env"] = (float(parts[22]), float(parts[23]),
                                                                  float(parts[24]))
                    pads[index] = pad
    except (OSError, ValueError):
        pass
    if not current:
        current = next((pad["file"] for pad in pads if pad), "")
    return current, pads, mono


def write_manifest(path, current, pads, mono=0):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"mono {1 if mono else 0}"]
    if current:
        lines.append(f"current {current}")
    for index, pad in enumerate(pads):
        if pad:
            lines.append(f"pad {index} {pad['file']} {int(pad['start'])} {int(pad['end'])} {int(pad['root'])} "
                         f"{pad['gain']:.6f} {pad['pan']:.6f} {pad['pitch']:.6f} "
                         f"{int(pad['loop'])} {int(pad['reverse'])} {int(pad.get('gate', 0))} "
                         f"{int(pad.get('group', 0))} {int(pad.get('stretch', 0))} "
                         + " ".join(f"{v:.6f}" for v in (pad.get("amp") or (0, 0, 1, 0)))
                         + " " + " ".join(f"{v:.6f}" for v in (pad.get("filter") or (0, 0, 1, 0)))
                         + f" {pad.get('cutoff', 20000.0):.3f} {pad.get('reso', 0.0):.6f} "
                           f"{pad.get('env', 0.0):.6f}")
    # write beside it and rename: the plugin reads the manifest from the audio thread, and a half-written file
    # (or a light map update during a slice drag) would make pads vanish for a moment
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(lines) + ("\n" if lines else ""))
    os.replace(temporary, path)


def import_sample(source, state_dir):
    """Copy (or convert to) a mono WAV in the project's sampler folder; returns its name."""
    source = Path(source)
    folder = sampler_dir(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem).strip("._") or "sample"
    name = f"{stem}.wav"
    number = 2
    while (folder / name).exists():
        name = f"{stem}-{number}.wav"
        number += 1
    destination = folder / name
    if source.suffix.lower() == ".wav":
        shutil.copyfile(source, destination)
    else:
        try:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-ac", "1", "-ar", "48000",
                            str(destination)], check=True)
        except FileNotFoundError:
            raise RuntimeError("WAV only (ffmpeg not found for this format)") from None
        except subprocess.CalledProcessError as error:
            raise RuntimeError("could not decode this file (ffmpeg)") from error
    return name


def chop_regions(frames, count=SAMPLER_PADS):
    count = max(1, min(SAMPLER_PADS, int(count)))
    return [(round(i * frames / count), round((i + 1) * frames / count)) for i in range(count)]


class PadDrag(Drag):
    """A pad control drag: the value changes live, the manifest is written when the finger lifts."""

    def __init__(self, pos, get, set_, lo, hi, px, unit, done, snap=None, live=None):
        super().__init__(pos, get, set_, lo, hi, px, unit)
        self.done, self.snap, self.live = done, snap, live

    def move(self, pos):
        super().move(pos)
        if self.dragging and self.live:
            self.live()  # the map follows the finger, not only the release

    def release(self, pos):
        super().release(pos)
        if self.snap and self.dragging:  # a pitch drag lands on the semitone it was near
            self.snap()
        self.done()


class SliceDrag(Gesture):
    """A white handle on the selected pad's start/end: sideways moves that edge, and the release saves."""

    def __init__(self, editor, edge, rect, pos):
        self.editor, self.edge, self.rect = editor, edge, rect
        self.move(pos)

    def move(self, pos):
        self.editor.set_slice(self.edge, self.rect, pos)
        self.editor.save_live()

    def release(self, pos):
        self.editor.save_reload()


class MarkerDrag(Gesture):
    """A yellow chop marker: sideways moves it between its neighbours (markers are editor-only)."""

    def __init__(self, editor, index, rect, pos):
        self.editor, self.index, self.rect = editor, index, rect
        self.move(pos)

    def move(self, pos):
        self.editor.move_marker(self.index, self.rect, pos)


class WaveScroll(Gesture):
    """One finger on the waveform: a drag pans the view, a tap adds a chop marker."""

    def __init__(self, editor, rect, pos):
        self.editor, self.rect = editor, rect
        self.x = self.x0 = pos.x()
        self.start0, self.end0 = editor.view_start, editor.view_end
        self.moved = False

    def move(self, pos):
        self.x = pos.x()
        others = [g for g in self.editor.panel.gestures.values()
                  if isinstance(g, WaveScroll) and g is not self]
        if others:  # two fingers: pinch to zoom, around the middle of the view
            distance = abs(self.x - others[0].x)
            if self.editor.pinch_last is None:
                self.editor.pinch_last = distance
            elif distance > 1:
                self.editor.zoom(distance / self.editor.pinch_last)
                self.editor.pinch_last = distance
            self.moved = True
            return
        dx = self.x - self.x0
        if abs(dx) > 8:
            self.moved = True
            self.editor.pan(dx, self.rect.width(), self.start0, self.end0)

    def release(self, pos):
        self.editor.pinch_last = None
        if not self.moved:
            self.editor.wave_tap(self.rect, pos)

    def cancel(self):
        self.editor.pinch_last = None


class PadHold(Gesture):
    """A pad in the editor: pressing selects it and starts its note, releasing stops it - the way PLAY-12's
    pads work, so GATE and LOOP are heard while the finger is down."""

    def __init__(self, editor, index):
        self.stop = editor.start_pad(index)

    def release(self, pos):
        if self.stop:
            self.stop()
            self.stop = None


class EnvDrag(Gesture):
    """A point of the amp/filter envelope: sideways moves its time, up/down sets the sustain level."""

    def __init__(self, editor, rect, which, pos):
        self.editor, self.rect, self.which = editor, rect, which
        self.move(pos)

    def move(self, pos):
        t = max(0.0, min(1.0, (pos.x() - self.rect.left()) / max(1.0, self.rect.width())))
        v = max(0.0, min(1.0, (self.rect.bottom() - pos.y()) / max(1.0, self.rect.height())))
        self.editor.set_env(self.which, t, v)
        self.editor.save_live()

    def release(self, pos):
        self.editor.save_reload()


class RangeDrag(Gesture):
    """The white bar between the pad's handles: sideways moves the whole slice."""

    def __init__(self, editor, rect, pos):
        self.editor, self.rect = editor, rect
        self.x0 = pos.x()
        pad = editor.pads[editor.pad]
        self.start0 = pad["start"] if pad else 0
        self.end0 = pad["end"] if pad else 0
        self.moved = False

    def move(self, pos):
        dx = pos.x() - self.x0
        if abs(dx) > 3:
            self.moved = True
            self.editor.move_range(self.rect, dx, self.start0, self.end0)
            self.editor.save_live()

    def release(self, pos):
        if self.moved:
            self.editor.save_reload()


class SamplerEditor:
    def __init__(self, panel, background=False):
        self.panel = panel
        self.background = background  # read WAVs off the UI thread (the app); tests load synchronously
        self.plugin = None
        self.slot = None
        self.manifest = None
        self.current = ""
        self.pads = [None] * SAMPLER_PADS
        self.mono = False
        self.samples = []
        self.rate = 0
        self.frames = 0
        self.markers = [0]
        self.pad = 0
        self.region = 0
        self.copy_src = None  # a pad waiting to be copied onto the next pad tapped
        self.presets_open = False  # the preset list drawn over the editor
        self.preset_scroll = 0.0
        self.naming = None  # the keyboard naming a preset
        self.env_open = False  # the waveform shows the amp/filter envelopes instead
        self.env_tab = "amp"  # "amp" or "filter"
        self.message = ""
        self.message_at = 0.0
        self.message_color = TEXT
        self.cache = {}
        self.loading = False
        self._load_token = 0
        self._loaded = None
        self._load_lock = threading.Lock()
        self.reloads = 0  # bumped after each map write so the plugin rereads it even when the slot is unchanged
        self.playhead = None  # (pad, 0..1 through its slice) of the newest voice, from the plugin's outputs
        self._live_at = 0.0  # when the map was last written during a drag
        self.MESSAGE_SECONDS = 3.0
        self.view_start = 0
        self.view_end = 0
        self.peak_table = []
        self.peak_cache = {}
        self.MIN_VIEW = 64
        self.pinch_last = None

    # --- state ---------------------------------------------------------------
    def use(self, plugin, slot, manifest):
        if plugin is not self.plugin or slot != self.slot or manifest != self.manifest:
            self.plugin, self.slot, self.manifest = plugin, slot, manifest
            self.load()

    def say(self, text, error=False):
        """A short status line over the waveform, gone after a few seconds."""
        self.message = text
        self.message_at = time.monotonic()
        self.message_color = WARN if error else "#4ade80"

    def load(self):
        self.current, self.pads, self.mono = read_manifest(self.manifest)
        self.pad = 0
        self.region = 0
        self.copy_src = None
        self.presets_open = False
        self.naming = None
        self.message, self.message_at = "", 0.0
        self.load_wave(self.current)

    def load_wave(self, name, data=None):
        """Read a sample for the waveform. In the app this runs in the background (the panel shows a spinner
        meanwhile); `data` is an already-read (samples, rate, frames, peaks) tuple."""
        self.samples, self.rate, self.frames = [], 0, 0
        self.markers = [0]
        self.peak_cache, self.peak_table = {}, []
        self._load_token += 1
        token = self._load_token
        self.loading = False
        if not name or self.manifest is None:
            return
        self.current = name  # selecting a pad follows its file, so the next pad compares against the right one
        if data is not None:
            self._apply_wave(token, name, *data)
            return
        folder = self.manifest.parent
        if not self.background:
            try:
                self._apply_wave(token, name, *self._read(name, folder))
            except Exception as error:  # a broken/missing WAV must not take the panel down
                self.say(f"can't read {name}: {error}", error=True)
            return
        self.loading = True

        def read():
            try:
                result, error = self._read(name, folder), None
            except Exception as exc:  # noqa: BLE001 - reported on the UI thread when polled
                result, error = None, exc
            with self._load_lock:
                if token == self._load_token:
                    self._loaded = (token, name, result, error)
            self.panel.invalidate()

        threading.Thread(target=read, daemon=True).start()

    @staticmethod
    def _peaks(samples, frames):
        return peaks_range(samples, 0, frames, min(2048, max(1, frames))) if frames else []

    def _read(self, name, folder):
        with self._load_lock:
            cached = self.cache.get(name)
        if cached is None:
            cached = read_wav(Path(folder) / name)
            with self._load_lock:
                self.cache[name] = cached
        samples, rate, frames = cached
        return samples, rate, frames, self._peaks(samples, frames)

    def _apply_wave(self, token, name, samples, rate, frames, table):
        if token != self._load_token:
            return
        self.samples, self.rate, self.frames = samples, rate, frames
        self.current = name
        self.markers = [0, frames] if frames else [0]
        pad = self.pads[self.pad] if 0 <= self.pad < len(self.pads) else None
        if pad and pad.get("file") == name and frames:  # the pad that asked for it keeps its slice selected
            self.markers = sorted({0, frames, max(0, min(frames, pad["start"])),
                                   max(0, min(frames, pad["end"]))})
            for number, (start, end) in enumerate(self.regions()):
                if start == pad["start"] and end == pad["end"]:
                    self.region = number
                    break
        self.view_start, self.view_end = 0, frames
        self.peak_cache, self.peak_table = {}, table
        self.loading = False

    def read_playhead(self):
        """The newest voice's position, from the plugin's output parameters (the sampler has no UI of its own,
        so this is how the editor learns where a playing pad is)."""
        host = getattr(self.panel, "host", None)
        if not self.plugin or host is None or not hasattr(host, "output_params"):
            self.playhead = None
            return
        if self.plugin not in getattr(self.panel, "loaded", {}).values():
            self.playhead = None
            return
        indices = host.output_params(self.plugin)
        if len(indices) < 2:
            self.playhead = None
            return
        position = host.value(self.plugin, indices[0])
        index = int(round(host.value(self.plugin, indices[1])))
        self.playhead = (index, max(0.0, min(1.0, position))) if index >= 0 else None

    def poll(self):
        """Apply a background read on the UI thread (called from the host's tick)."""
        self.read_playhead()
        with self._load_lock:
            pending, self._loaded = self._loaded, None
        if not pending:
            return
        token, name, result, error = pending
        if token != self._load_token:
            return
        if result is None:
            self.loading = False
            self.say(f"can't read {name}: {error}", error=True)
            return
        self._apply_wave(token, name, *result)

    def save_live(self):
        """Write the map while a finger is dragging, at most every 50 ms, so a held loop follows the edit."""
        now = time.monotonic()
        if now - self._live_at >= 0.05:
            self._live_at = now
            self.save_reload()

    def save_map(self):
        """Write the pad map."""
        if self.manifest is not None:
            write_manifest(self.manifest, self.current, self.pads, self.mono)

    def save_reload(self, full=False):
        """Write the map and tell the plugin to read it: `full` when the samples changed (reread the WAVs),
        otherwise a light map update that keeps playing voices alive and follows slice edits at once."""
        self.save_map()
        if not self.plugin:
            return
        bump = next((p for p in self.plugin.params if p.name == ("Load" if full else "Reload")), None)
        if bump is None and full:  # an older plugin: setting the slot again was how it reloaded
            bump = next((p for p in self.plugin.params if p.name == "Slot"), None)
        if bump is None:
            return
        if bump.name == "Slot":
            self.panel.set_param(self.plugin, bump.index, float(self.slot), source="sampler")
        else:
            self.reloads += 1
            self.panel.set_param(self.plugin, bump.index, float(self.reloads % 1000000), source="sampler")

    # --- actions -------------------------------------------------------------
    def import_file(self):
        self.copy_src = None
        path, _ = QFileDialog.getOpenFileName(None, "Load sample", "",
                                              "Audio (*.wav *.aif *.aiff *.flac *.mp3 *.ogg);;All files (*)")
        if not path:
            return
        try:
            name = import_sample(path, self.panel.state_dir)
            samples, rate, frames = read_wav(sampler_dir(self.panel.state_dir) / name)
            self.cache[name] = (samples, rate, frames)
            self.load_wave(name)  # the cache makes this quick; in the app the peaks are built off the UI thread
            self.pads[self.pad] = self.new_pad(0, frames, SAMPLER_NOTE_BASE + self.pad)
            self.say(f"loaded {name}")
            self.save_reload(full=True)
        except Exception as error:
            self.say(str(error), error=True)

    def chop(self):
        self.copy_src = None
        if not self.frames:
            self.say("load a sample first", error=True)
            return
        regions = chop_regions(self.frames)
        self.markers = [start for start, _ in regions] + [regions[-1][1]]
        for index, (start, end) in enumerate(regions):
            self.pads[index] = self.new_pad(start, end, SAMPLER_NOTE_BASE + index)
        self.pad, self.region = 0, 0
        self.say(f"chopped into {len(regions)}")
        self.save_reload(full=True)

    def clear(self):
        self.copy_src = None
        self.pads = [None] * SAMPLER_PADS
        self.markers = [0, self.frames] if self.frames else [0]
        self.region = 0
        self.say("pads cleared")
        self.save_reload()

    def assign(self):
        self.copy_src = None
        if not self.current or not self.frames:
            self.say("load a sample first", error=True)
            return
        regions = self.regions()
        if not regions:
            self.say("add a chop marker first", error=True)
            return
        start, end = regions[max(0, min(self.region, len(regions) - 1))]
        self.pads[self.pad] = self.new_pad(start, end, SAMPLER_NOTE_BASE + self.pad)
        self.say(f"pad {self.pad + 1} = {start / self.rate:.2f}s")
        self.save_reload(full=True)

    def new_pad(self, start, end, root):
        return {"file": self.current, "start": int(start), "end": int(end), "root": int(root),
                "gain": 1.0, "pan": 0.0, "pitch": 0.0, "loop": 0, "reverse": 0, "gate": 0, "group": 0,
                "stretch": 0, "amp": [0.0, 0.0, 1.0, 0.0], "filter": [0.0, 0.0, 1.0, 0.0],
                "cutoff": 20000.0, "reso": 0.0, "env": 0.0}

    def copy_pad(self):
        """Arm a copy: the next pad tapped gets this slice and its settings. The target keeps its own note as
        root, so it sounds the same until its PITCH is changed - one slice, two pitches."""
        if not self.pads[self.pad]:
            self.say("nothing to copy on this pad", error=True)
            return
        if self.copy_src is not None:
            self.copy_src = None
            self.say("copy cancelled")
            return
        self.copy_src = self.pad
        self.say(f"P{self.pad + 1} → tap a pad to copy the slice there")

    def paste_pad(self, src, index):
        """Copy pad `src` onto pad `index`: same file and slice, but the target's own root note (so the two
        sound the same) and its own PITCH/gain/pan, ready to be tweaked independently."""
        pad = self.pads[src]
        if not pad or index == src:
            return False
        self.pads[index] = {**pad, "root": SAMPLER_NOTE_BASE + index}
        self.say(f"copied P{src + 1} → P{index + 1}")
        self.save_reload()
        return True

    # --- presets: the pad map kept outside the project ---------------------------------------------
    def preset_files(self):
        """The sample files the current pads and waveform use."""
        return {pad["file"] for pad in self.pads if pad} | ({self.current} if self.current else set())

    def open_presets(self):
        self.presets_open, self.preset_scroll, self.naming = True, 0.0, None

    def name_preset(self):
        default = Path(self.current).stem or "Preset"
        self.naming = Keyboard("", "Preset name", self.save_preset,
                               lambda: setattr(self, "naming", None), placeholder=default)

    def save_preset(self, name=None):
        """Keep the pads (and their samples) as a preset in the machine's folder: deleting the track, or even
        the project, does not take it with it."""
        self.naming = None
        if not any(self.pads) and not self.current:
            self.say("nothing to save yet", error=True)
            return
        try:
            folder = free_preset_dir(presets_dir(self.panel), name or Path(self.current).stem or "Preset")
            folder.mkdir(parents=True)
            source = sampler_dir(self.panel.state_dir)
            for sample in self.preset_files():
                store_sample(source / sample, folder)
            write_manifest(folder / "preset.txt", self.current, self.pads, self.mono)
        except OSError as error:
            self.say(f"can't save the preset: {error}", error=True)
            return
        self.say(f"saved preset {folder.name}")

    def load_preset(self, folder):
        """Bring a preset back: its samples into this project's sampler folder, its pads into this slot."""
        folder = Path(folder)
        current, pads, mono = read_manifest(folder / "preset.txt")
        if not any(pads) and not current:
            self.say("that preset is empty", error=True)
            return
        renamed = {}
        try:
            destination = sampler_dir(self.panel.state_dir)
            destination.mkdir(parents=True, exist_ok=True)
            for name in {pad["file"] for pad in pads if pad} | ({current} if current else set()):
                source = folder / name
                if not source.is_file():
                    continue
                taken = destination / name
                if taken.exists():
                    try:  # the same sample (a hard link) or a same-sized copy: keep it, no duplicate
                        same = os.path.samefile(source, taken) or taken.stat().st_size == source.stat().st_size
                    except OSError:
                        same = False
                    if not same:  # another file has that name: keep both and point the pads at the new one
                        renamed[name] = free_sample_name(destination, name)
                store_sample(source, destination, renamed.get(name))
            for pad in pads:
                if pad and pad["file"] in renamed:
                    pad["file"] = renamed[pad["file"]]
            current = renamed.get(current, current)
        except OSError as error:
            self.say(f"can't load the preset: {error}", error=True)
            return
        self.current, self.pads, self.mono = current, pads, mono
        self.pad, self.region = 0, 0
        self.presets_open = False
        self.save_reload(full=True)  # the preset's samples may be new
        self.load()  # refresh the waveform and the pads from what was just written
        self.say(f"loaded preset {folder.name}")

    def delete_preset(self, folder):
        try:
            shutil.rmtree(folder)
        except OSError as error:
            self.say(f"can't delete the preset: {error}", error=True)
            return
        self.say(f"deleted preset {Path(folder).name}")

    def regions(self):
        return [(self.markers[i], self.markers[i + 1]) for i in range(len(self.markers) - 1)]

    def select_pad(self, index):
        src, self.copy_src = self.copy_src, None
        if src is not None:  # a pad tapped while COPY is armed receives the slice instead of just selecting
            self.paste_pad(src, index)
        self.pad = index
        pad = self.pads[index]
        if not pad:
            return
        if pad["file"] != self.current:
            self.load_wave(pad["file"])
        if self.frames:
            self.markers = sorted({0, self.frames, max(0, min(self.frames, pad["start"])),
                                   max(0, min(self.frames, pad["end"]))})
            for number, (start, end) in enumerate(self.regions()):
                if start == pad["start"] and end == pad["end"]:
                    self.region = number
                    break

    def start_pad(self, index):
        """Select a pad and hold its note (the pad gesture stops it on release, so GATE/LOOP behave as in
        PLAY-12). Returns the note-off callable, or None when there is nothing to send through."""
        self.select_pad(index)
        preview = getattr(self.panel, "preview_start", None)
        return preview(self.plugin, index) if preview and self.plugin else None

    def stop_all(self):
        """Stop every sampler voice that is still sounding."""
        stop = getattr(self.panel, "stop_pads", None)
        if stop and self.plugin:
            stop(self.plugin)
            self.say("stopped")

    def frame_at(self, rect, pos):
        if not self.frames:
            return 0
        part = max(0.0, min(1.0, (pos.x() - rect.left()) / max(1.0, rect.width())))
        return int(self.view_start + part * max(1, self.view_end - self.view_start))

    def set_view(self, start, end):
        if not self.frames:
            return
        start = max(0, min(self.frames - 1, start))
        end = max(start + 1, min(self.frames, end))
        self.view_start, self.view_end = int(start), int(end)

    def fit(self):
        self.set_view(0, self.frames)

    def focus_pad(self):
        """Centre the waveform on the selected pad's slice, zoomed so the slice is about a third of the view."""
        pad = self.pads[self.pad]
        if not pad or not self.frames or pad.get("file") != self.current:
            self.say("select a pad with a slice first", error=True)
            return
        span = max(1, int(pad["end"]) - int(pad["start"]))
        width = max(self.MIN_VIEW, span * 3)
        center = (int(pad["start"]) + int(pad["end"])) / 2
        self.set_view(center - width / 2, center + width / 2)

    def zoom(self, factor):
        if not self.frames:
            return
        span = max(1, self.view_end - self.view_start)
        center = (self.view_start + self.view_end) / 2
        new_span = max(self.MIN_VIEW, min(self.frames, span / factor))
        self.set_view(center - new_span / 2, center + new_span / 2)

    def pan(self, dx, width, start0=None, end0=None):
        if not self.frames:
            return
        if start0 is None:
            start0, end0 = self.view_start, self.view_end
        span = max(1, end0 - start0)
        shift = -dx / max(1.0, width) * span
        self.set_view(start0 + shift, start0 + shift + span)

    def visible_peaks(self, rect):
        columns = max(1, int(rect.width() / (2 * self.panel.s)))
        key = (self.current, self.view_start, self.view_end, columns)
        cached = self.peak_cache.get(key)
        if cached is not None:
            return cached
        if self.view_start <= 0 and self.view_end >= self.frames:
            values = table_peaks(self.peak_table, columns)
        else:
            values = peaks_range(self.samples, self.view_start, self.view_end, columns)
        self.peak_cache[key] = values
        return values

    def wave_tap(self, rect, pos):
        if not self.frames:
            return
        frame = self.frame_at(rect, pos)
        if frame not in self.markers:
            self.markers = sorted(self.markers + [frame])
        for number, (start, end) in enumerate(self.regions()):
            if start <= frame < end:
                self.region = number
                break

    def set_slice(self, edge, rect, pos):
        """Move the selected pad's start or end (the white handles on the waveform)."""
        pad = self.pads[self.pad]
        if not pad or not self.frames:
            return
        frame = self.frame_at(rect, pos)
        if edge == "start":
            pad["start"] = max(0, min(pad["end"] - 1, frame))
        else:
            pad["end"] = max(pad["start"] + 1, min(self.frames, frame))

    def move_marker(self, index, rect, pos):
        """Move a chop marker between its neighbours; markers only define the slices, not the pads."""
        if not self.frames or index <= 0 or index >= len(self.markers) - 1:
            return
        frame = self.frame_at(rect, pos)
        low = self.markers[index - 1] + 1
        high = self.markers[index + 1] - 1
        self.markers[index] = max(low, min(high, frame))

    def move_range(self, rect, dx, start0, end0):
        """Move the selected pad's whole slice sideways, keeping its length."""
        pad = self.pads[self.pad]
        if not pad or not self.frames:
            return
        span = max(1, min(end0 - start0, self.frames))
        view_span = max(1, self.view_end - self.view_start)
        shift = int(dx / max(1.0, rect.width()) * view_span)
        start = max(0, min(self.frames - span, start0 + shift))
        pad["start"], pad["end"] = start, start + span

    # --- drawing -------------------------------------------------------------
    def draw(self, p, r, plugin, slot, manifest):
        self.use(plugin, slot, manifest)
        s = self.panel.s
        header = QRectF(r.left(), r.top(), r.width(), 40 * s)
        load, chop, clear, assign, copy, mono, name = split(
            header, [78 * s, 78 * s, 78 * s, 86 * s, 78 * s, 64 * s, 0], 6 * s)
        self.panel.button(p, load, "LOAD", self.import_file)
        self.panel.button(p, chop, "CHOP", self.chop)
        self.panel.button(p, clear, "CLEAR", self.clear, color=WARN)
        self.panel.button(p, assign, "TO PAD", self.assign)
        self.panel.button(p, copy, "COPY", self.copy_pad, on=self.copy_src is not None, color="#facc15")
        self.panel.button(p, mono, "MONO", self.toggle_mono, on=self.mono, color="#facc15")
        self.panel.text(p, name, self.current or "no sample", DIM, 12, elide=True)

        body = QRectF(r.left(), header.bottom() + 6 * s, r.width(), r.bottom() - header.bottom() - 6 * s)
        if self.env_open:  # the envelope editor takes the whole area: the pads stay out of its way
            self.draw_wave(p, body)
            self.draw_message(p, body, r)
            return
        wave_h = body.height() * 0.46
        wave = QRectF(body.left(), body.top(), body.width(), wave_h)
        lower = QRectF(body.left(), wave.bottom() + 8 * s, body.width(), body.bottom() - wave.bottom() - 8 * s)
        pads, controls = split(lower, [0, 240 * s], 10 * s)
        self.draw_wave(p, wave)
        self.draw_pads(p, pads)
        self.draw_controls(p, controls)
        if self.presets_open:
            self.draw_presets(p, r)
        if self.naming:
            self.naming.draw(self.panel, p, r)
        self.draw_message(p, wave, r)

    def draw_message(self, p, wave, area=None):
        """A toast over the waveform's top edge (or the preset list's bottom), gone after a few seconds."""
        if not self.message or self.naming:
            return
        if time.monotonic() - self.message_at > self.MESSAGE_SECONDS:
            self.message = ""
            return
        s = self.panel.s
        width = min(wave.width() * 0.8, max(160 * s, len(self.message) * 7.5 * s))
        toast = QRectF(0, 0, width, 30 * s)
        if self.presets_open and area is not None:
            toast.moveCenter(QPointF(area.center().x(), area.bottom() - 26 * s))
        elif self.env_open:  # the curve owns the middle
            toast.moveCenter(QPointF(wave.center().x(), wave.bottom() - 24 * s))
        elif self.loading:  # the spinner owns the middle
            toast.moveCenter(QPointF(wave.center().x(), wave.bottom() - 24 * s))
        else:
            toast.moveCenter(wave.center())
        self.panel.box(p, toast, QColor(13, 15, 20, 235), BORDER, radius=8)
        self.panel.text(p, toast, self.message, self.message_color, 12, bold=True, elide=True)

    def draw_wave(self, p, rect):
        s = self.panel.s
        self.panel.box(p, rect, "#0e1116", BORDER, radius=8)
        if self.loading:
            self.draw_spinner(p, rect)
            return
        if self.env_open:
            self.draw_envelope(p, rect)
            return
        if not self.samples or not self.frames:
            self.panel.text(p, rect, "LOAD a WAV to see its waveform", DIM, 14)
            return

        span = max(1, self.view_end - self.view_start)

        def x_of(frame):
            return rect.left() + rect.width() * (frame - self.view_start) / span

        if self.regions():
            start, end = self.regions()[max(0, min(self.region, len(self.regions()) - 1))]
            a, b = max(start, self.view_start), min(end, self.view_end)
            if b > a:
                p.fillRect(QRectF(x_of(a), rect.top() + 1 * s, max(1.0, x_of(b) - x_of(a)),
                                  rect.height() - 2 * s), QColor(56, 214, 255, 38))
        p.setPen(QPen(QColor("#38d6ff"), 1.2 * s))
        values = self.visible_peaks(rect)
        for n, (low, high) in enumerate(values):
            x = rect.left() + (n + 0.5) * rect.width() / max(1, len(values))
            y0 = rect.center().y() - high * rect.height() * 0.45
            y1 = rect.center().y() - low * rect.height() * 0.45
            p.drawLine(QPointF(x, y0), QPointF(x, y1))

        # One finger pans; a tap adds a marker. Markers and pad handles are registered after it, so they win.
        self.panel.region(rect, lambda pos: WaveScroll(self, rect, pos))
        for index, marker in enumerate(self.markers):
            if not (self.view_start <= marker <= self.view_end):
                continue
            x = x_of(marker)
            p.setPen(QPen(QColor("#facc15"), 1.5 * s))
            p.drawLine(QPointF(x, rect.top() + 2 * s), QPointF(x, rect.bottom() - 2 * s))
            if 0 < index < len(self.markers) - 1:  # the file's ends stay put; the others are handles
                p.setBrush(QColor("#facc15"))
                p.setPen(QPen(QColor("#0e1116"), 1.5 * s))
                p.drawEllipse(QPointF(x, rect.top() + 10 * s), 4.5 * s, 4.5 * s)
                self.panel.region(QRectF(x - 10 * s, rect.top(), 20 * s, rect.height()),
                                  lambda pos, index=index: MarkerDrag(self, index, rect, pos))

        pad = self.pads[self.pad]
        if pad and pad["file"] == self.current:  # the selected pad's slice, with white drag handles
            start = max(0, min(self.frames, pad["start"]))
            end = max(0, min(self.frames, pad["end"]))
            for edge, frame in (("start", start), ("end", end)):
                if not (self.view_start <= frame <= self.view_end):
                    continue
                x = x_of(frame)
                p.setPen(QPen(QColor("#ffffff"), 2 * s))
                p.drawLine(QPointF(x, rect.top() + 2 * s), QPointF(x, rect.bottom() - 2 * s))
                p.setBrush(QColor("#ffffff"))
                p.setPen(QPen(QColor("#0e1116"), 1.5 * s))
                p.drawEllipse(QPointF(x, rect.top() + 10 * s), 6 * s, 6 * s)
                self.panel.region(QRectF(x - 14 * s, rect.top(), 28 * s, rect.height()),
                                  lambda pos, edge=edge: SliceDrag(self, edge, rect, pos))
            if self.view_start <= start and end <= self.view_end:
                x0, x1 = x_of(start), x_of(end)
                width = min(80 * s, max(0.0, x1 - x0 - 30 * s))
                if width >= 18 * s:
                    bar = QRectF((x0 + x1) / 2 - width / 2, rect.top() + 4 * s, width, 12 * s)
                    self.panel.box(p, bar, QColor("#ffffff"), None, radius=5)
                    self.panel.text(p, bar, "↔", "#0e1116", 10, bold=True)
                    self.panel.region(bar, lambda pos: RangeDrag(self, rect, pos))

        if self.playhead:  # the newest voice on this file: a green line through the waveform
            index, fraction = self.playhead
            playing = self.pads[index] if 0 <= index < len(self.pads) else None
            if playing and playing["file"] == self.current:
                frame = playing["start"] + fraction * (playing["end"] - playing["start"])
                if self.view_start <= frame <= self.view_end:
                    x = x_of(frame)
                    p.setPen(QPen(QColor(PLAY), 2 * s))
                    p.drawLine(QPointF(x, rect.top() + 2 * s), QPointF(x, rect.bottom() - 2 * s))
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(PLAY))
                    p.drawPolygon(QPolygonF([QPointF(x - 5 * s, rect.top() + 2 * s),
                                             QPointF(x + 5 * s, rect.top() + 2 * s),
                                             QPointF(x, rect.top() + 10 * s)]))

        zoom_in = QRectF(rect.right() - 34 * s, rect.top() + 6 * s, 28 * s, 24 * s)
        zoom_out = QRectF(zoom_in.left() - 32 * s, zoom_in.top(), 28 * s, 24 * s)
        fit = QRectF(zoom_out.left() - 46 * s, zoom_in.top(), 42 * s, 24 * s)
        stop = QRectF(fit.left() - 58 * s, zoom_in.top(), 52 * s, 24 * s)
        locate = QRectF(stop.left() - 64 * s, zoom_in.top(), 58 * s, 24 * s)
        env = QRectF(locate.left() - 64 * s, zoom_in.top(), 58 * s, 24 * s)
        self.panel.button(p, env, "ADSR", self.toggle_env)
        self.panel.button(p, locate, "LOCATE", self.focus_pad)
        self.panel.button(p, stop, "STOP", self.stop_all, color=WARN)
        self.panel.button(p, zoom_in, "+", lambda: self.zoom(1.6))
        self.panel.button(p, zoom_out, "−", lambda: self.zoom(1 / 1.6))
        self.panel.button(p, fit, "FIT", self.fit)
        self.panel.text(p, QRectF(rect.left() + 8 * s, rect.bottom() - 16 * s, rect.width() - 16 * s, 14 * s),
                        "drag to pan · yellow lines chop · white handles resize · the white bar moves the slice",
                        DIM, 10, Qt.AlignLeft | Qt.AlignVCenter, elide=True)

    def draw_envelope(self, p, rect):
        """The amp/filter envelopes over the selected slice, with the slice's waveform as a dim ghost."""
        s = self.panel.s
        pad = self.pads[self.pad]
        header = QRectF(rect.left() + 10 * s, rect.top() + 6 * s, rect.width() - 20 * s, 28 * s)
        amp, filt, _, wave = split(header, [62 * s, 66 * s, 0, 58 * s], 6 * s)
        self.panel.button(p, amp, "AMP", lambda: setattr(self, "env_tab", "amp"), on=self.env_tab == "amp")
        self.panel.button(p, filt, "FILTER", lambda: setattr(self, "env_tab", "filter"),
                          on=self.env_tab == "filter")
        self.panel.button(p, wave, "WAVE", self.toggle_env)
        if not pad or not self.frames or pad.get("file") != self.current:
            self.panel.text(p, QRectF(rect.left(), header.bottom(), rect.width(), rect.bottom() - header.bottom()),
                            "select a pad with a slice first", DIM, 14)
            return
        env = pad.get("amp" if self.env_tab == "amp" else "filter") or [0.0, 0.0, 1.0, 0.0]
        body = QRectF(rect.left() + 10 * s, header.bottom() + 8 * s, rect.width() - 20 * s,
                      rect.height() - header.bottom() - 18 * s)
        ghost_h = body.height() * 0.26
        ghost = QRectF(body.left(), body.bottom() - ghost_h, body.width(), ghost_h)
        area = QRectF(body.left(), body.top(), body.width(), body.height() - ghost_h - 8 * s)
        side = None
        if self.env_tab == "filter":
            side = QRectF(area.right() - 138 * s, area.top(), 138 * s, area.height())
            area = QRectF(area.left(), area.top(), area.width() - 148 * s, area.height())

        start = max(0, min(self.frames, int(pad["start"])))
        end = max(start + 1, min(self.frames, int(pad["end"])))
        columns = max(1, int(ghost.width() / (2 * s)))
        key = ("env", self.current, start, end, columns)
        values = self.peak_cache.get(key)
        if values is None:
            values = peaks_range(self.samples, start, end, columns)
            self.peak_cache[key] = values
        p.setPen(QPen(QColor(56, 214, 255, 95), 1.0 * s))
        for n, (low, high) in enumerate(values):
            x = ghost.left() + (n + 0.5) * ghost.width() / max(1, len(values))
            y0 = ghost.center().y() - high * ghost.height() * 0.45
            y1 = ghost.center().y() - low * ghost.height() * 0.45
            p.drawLine(QPointF(x, y0), QPointF(x, y1))
        self.panel.text(p, ghost.adjusted(2 * s, 0, 0, 0), "slice", QColor(56, 214, 255, 140), 10,
                        Qt.AlignLeft | Qt.AlignTop)

        self.panel.box(p, area, "#0e1116", BORDER, radius=6)
        for value in (0.0, 0.5, 1.0):
            y = area.bottom() - value * area.height()
            p.setPen(QPen(QColor(255, 255, 255, 25), 1))
            p.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
        points = []
        steps = 160
        for i in range(steps + 1):
            t = i / steps
            points.append(QPointF(area.left() + t * area.width(),
                                  area.bottom() - self.env_value(env, t) * area.height()))
        colour = "#4ade80" if self.env_tab == "amp" else "#38d6ff"
        p.setPen(QPen(QColor(colour), 2 * s))
        p.setBrush(Qt.NoBrush)
        p.drawPolyline(QPolygonF(points))
        a, d, sus, r = env
        for which, t, v in (("attack", a, 1.0), ("decay", min(1.0, a + d), sus),
                            ("release", max(0.0, 1.0 - r), sus)):
            c = QPointF(area.left() + t * area.width(), area.bottom() - v * area.height())
            p.setBrush(QColor("#ffffff"))
            p.setPen(QPen(QColor("#0e1116"), 1.5 * s))
            p.drawEllipse(c, 6 * s, 6 * s)
            self.panel.region(QRectF(c.x() - 14 * s, c.y() - 14 * s, 28 * s, 28 * s),
                              lambda pos, which=which: EnvDrag(self, area, which, pos))
        self.panel.text(p, QRectF(area.left(), area.top() + 2 * s, area.width(), 14 * s),
                        f"A {a * 100:.0f}%  D {d * 100:.0f}%  S {sus * 100:.0f}%  R {r * 100:.0f}%",
                        DIM, 10, Qt.AlignRight | Qt.AlignTop)

        if side is not None:
            knob_h = (side.height() - 12 * s) / 3
            controls = (
                ("CUTOFF", math.log(max(20.0, pad.get("cutoff", 20000.0)) / 20) / math.log(1000),
                 lambda: math.log(max(20.0, self.pads[self.pad].get("cutoff", 20000.0)) / 20) / math.log(1000),
                 lambda v: self.pads[self.pad].__setitem__("cutoff", 20.0 * 1000.0 ** v),
                 lambda v: f"{20.0 * 1000.0 ** v:.0f} Hz"),
                ("RESO", pad.get("reso", 0.0),
                 lambda: self.pads[self.pad].get("reso", 0.0),
                 lambda v: self.pads[self.pad].__setitem__("reso", v),
                 lambda v: f"{v * 100:.0f}%"),
                ("ENV", pad.get("env", 0.0),
                 lambda: self.pads[self.pad].get("env", 0.0),
                 lambda v: self.pads[self.pad].__setitem__("env", v),
                 lambda v: f"{v:.1f} oct"),
            )
            for n, (label, value, get_, set_, show) in enumerate(controls):
                r_ = QRectF(side.left(), side.top() + n * (knob_h + 6 * s), side.width(), knob_h)
                self.knob(p, r_, label, value, 0, 1 if label != "ENV" else 8, 1.5 * s,
                          0.01 if label != "ENV" else 0.05, get_, set_, show=show)

    def draw_spinner(self, p, rect):
        """A turning arc while a sample is read in the background."""
        s = self.panel.s
        r = QRectF(0, 0, 36 * s, 36 * s)
        r.moveCenter(rect.center())
        p.setPen(QPen(QColor("#38d6ff"), 2.6 * s, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        start = int((time.monotonic() * 240) % 360)
        p.drawArc(r, start * 16, 110 * 16)
        self.panel.text(p, QRectF(rect.left(), r.bottom() + 10 * s, rect.width(), 20 * s),
                        "reading the sample…", DIM, 12)

    def draw_pads(self, p, rect):
        s = self.panel.s
        for index in range(SAMPLER_PADS):
            row = QRectF(rect.left(), rect.top() + (index // 4) * rect.height() / 4, rect.width(),
                         rect.height() / 4)
            cell = split(row.adjusted(0, 3 * s, 0, -3 * s), [0] * 4, 5 * s)[index % 4]
            pad = self.pads[index]
            chosen = index == self.pad
            copying = index == self.copy_src
            color = self.pad_color(index) if pad else "#242a36"
            fill = QColor(color)
            fill.setAlpha(42)
            self.panel.box(p, cell, fill if pad else "#11141b",
                           "#facc15" if copying else "#ffffff" if chosen else color,
                           2 if (chosen or copying) else 1, radius=7)
            self.panel.text(p, cell.adjusted(0, 3 * s, 0, -cell.height() * 0.45), f"P{index + 1}", TEXT, 13,
                            bold=True)
            sub = Path(pad["file"]).stem if pad else "—"
            self.panel.text(p, cell.adjusted(0, cell.height() * 0.48, 0, 0), sub, DIM if pad else "#3a4252", 9,
                            elide=True)
            group = int(pad.get("group", 0)) if pad else 0
            if group:
                badge = QRectF(cell.right() - 28 * s, cell.top() + 3 * s, 24 * s, 14 * s)
                self.panel.box(p, badge, QColor("#facc15"), None, radius=3)
                self.panel.text(p, badge, f"G{group}", BG, 9, bold=True)
            self.panel.region(cell, lambda pos, index=index: PadHold(self, index))

    def draw_presets(self, p, r):
        """The preset list: tap one to load it, hold its ✕ to delete it, SAVE writes the current pads."""
        s = self.panel.s
        self.panel.box(p, r, QColor(13, 15, 20), BORDER, radius=8)
        self.panel.region(r, lambda pos: Gesture())  # the overlay swallows taps that miss its own controls
        header = QRectF(r.left() + 10 * s, r.top() + 8 * s, r.width() - 20 * s, 34 * s)
        title, save, close = split(header, [0, 96 * s, 76 * s], 8 * s)
        self.panel.text(p, title, "Sampler presets", TEXT, 16, Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        self.panel.button(p, save, "SAVE", self.name_preset, sub="current pads")
        self.panel.button(p, close, "CLOSE", lambda: setattr(self, "presets_open", False))
        listing = QRectF(r.left() + 10 * s, header.bottom() + 8 * s, r.width() - 20 * s,
                         r.bottom() - header.bottom() - 18 * s)
        folders = preset_folders(presets_dir(self.panel))
        if not folders:
            self.panel.text(p, listing, "no presets yet: SAVE the pads first", DIM, 13,
                            Qt.AlignHCenter | Qt.AlignTop)
            return
        rh = 48 * s
        self.preset_scroll = max(0.0, min(self.preset_scroll, len(folders) * rh - listing.height()))
        scroll = (lambda: self.preset_scroll), (lambda v: setattr(self, "preset_scroll", v))
        p.save()
        p.setClipRect(listing)
        first = int(self.preset_scroll // rh)
        for n in range(first, min(len(folders), first + int(listing.height() / rh) + 2)):
            folder = folders[n]
            row = QRectF(listing.left(), listing.top() + n * rh - self.preset_scroll, listing.width(), rh - 4 * s)
            self.panel.box(p, row, PANEL, BORDER, radius=6)
            name, info, delete = split(row.adjusted(10 * s, 3 * s, -4 * s, -3 * s),
                                       [0, row.width() * 0.54, 44 * s], 8 * s)
            self.panel.text(p, name, folder.name, TEXT, 14, Qt.AlignLeft | Qt.AlignVCenter, bold=True, elide=True)
            current, pads, _ = read_manifest(folder / "preset.txt")
            used = sum(1 for pad in pads if pad)
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime((folder / "preset.txt").stat().st_mtime))
            self.panel.text(p, info, f"{used} pads · {Path(current).stem or 'empty'} · {when}", DIM, 11,
                            Qt.AlignRight | Qt.AlignVCenter, elide=True)
            # the row scrolls; a tap loads it (also only on a tap, so a scroll started on it loads nothing)
            self.panel.region(row.intersected(listing), lambda pos, folder=folder: ScrollOrTap(
                pos, *scroll, lambda: self.load_preset(folder)))
            self.panel.button(p, delete, "✕", lambda: None, color=WARN)
            self.panel.hits[-1] = (delete.intersected(listing),
                                   lambda pos, folder=folder: Hold(lambda: self.delete_preset(folder)))
        p.restore()

    def draw_controls(self, p, rect):
        s = self.panel.s
        pad = self.pads[self.pad]
        # a compact row that stays reachable even with no pad chosen: loading a preset brings its pads
        row_h = min(28 * s, max(0.0, rect.height() - 140 * s))
        if row_h >= 16 * s:
            group, stretch, presets = split(QRectF(rect.left(), rect.bottom() - row_h, rect.width(), row_h),
                                            [0, 0, 0], 5 * s)
            number = int(pad.get("group", 0)) if pad else 0
            self.panel.button(p, group, f"GRP {number or '–'}", self.cycle_group, on=bool(number),
                              color="#facc15")
            self.panel.button(p, stretch, "STRETCH", self.toggle_stretch,
                              on=bool(pad and pad.get("stretch")), color="#4ade80")
            self.panel.button(p, presets, "PRESET", self.open_presets)
            rect = QRectF(rect.left(), rect.top(), rect.width(), rect.height() - row_h - 6 * s)
        if not pad:
            self.panel.text(p, rect, "tap a pad, then TO PAD", DIM, 12, Qt.AlignHCenter | Qt.AlignVCenter)
            return
        gain = QRectF(rect.left(), rect.top(), rect.width() / 2 - 4 * s, 62 * s)
        pan = QRectF(rect.center().x() + 4 * s, rect.top(), rect.width() / 2 - 4 * s, 62 * s)
        pitch = QRectF(rect.left(), gain.bottom() + 6 * s, rect.width() / 2 - 4 * s, 62 * s)
        buttons = QRectF(rect.center().x() + 4 * s, pitch.top(), rect.width() / 2 - 4 * s, 62 * s)
        # gain goes past unity (a pad can be pushed, up to +12 dB) and moves in 0.02 steps
        self.knob(p, gain, "GAIN", pad["gain"], 0.0, 4.0, 1.2 * s, 0.02,
                  lambda: self.pads[self.pad]["gain"], lambda v: self.pads[self.pad].__setitem__("gain", v))
        self.knob(p, pan, "PAN", pad["pan"], -1.0, 1.0, 1.5 * s, 0.01,
                  lambda: self.pads[self.pad]["pan"], lambda v: self.pads[self.pad].__setitem__("pan", v))
        # 12 px per semitone (it was 4.8, which made 0..2 jump by 1..3); a drag near a whole semitone snaps
        self.knob(p, pitch, "PITCH", pad["pitch"], -12.0, 12.0, 1.2 * s, 0.1,
                  lambda: self.pads[self.pad]["pitch"], lambda v: self.pads[self.pad].__setitem__("pitch", v),
                  snap=self.snap_pitch)
        gate = QRectF(buttons.left(), buttons.top(), buttons.width(), 18 * s)
        loop = QRectF(buttons.left(), gate.bottom() + 2 * s, buttons.width(), 18 * s)
        reverse = QRectF(buttons.left(), loop.bottom() + 2 * s, buttons.width(), 18 * s)
        self.panel.button(p, gate, "GATE", self.toggle_gate, on=bool(pad.get("gate", 0)))
        self.panel.button(p, loop, "LOOP", self.toggle_loop, on=bool(pad["loop"]))
        self.panel.button(p, reverse, "REVERSE", self.toggle_reverse, on=bool(pad["reverse"]))

    def pad_color(self, index=None):
        """The pad's color: the same fixed palette the drum pads use, by pad number (16 pads cycle the 12)."""
        return DRUM_COLORS[(self.pad if index is None else index) % len(DRUM_COLORS)]

    def cycle_group(self):
        """A playback group: pads in the same non-zero group cut each other, like MONO for that group."""
        pad = self.pads[self.pad]
        if not pad:
            self.say("tap a pad first", error=True)
            return
        pad["group"] = (int(pad.get("group", 0)) + 1) % (SAMPLER_GROUPS + 1)
        self.say(f"group {pad['group']}" if pad["group"] else "group off")
        self.save_reload()  # the plugin has to see the new group

    def toggle_stretch(self):
        """Pitch without changing the slice's length (a granular shift) instead of resampling."""
        pad = self.pads[self.pad]
        if not pad:
            self.say("tap a pad first", error=True)
            return
        pad["stretch"] = 0 if pad.get("stretch", 0) else 1
        self.say("stretch: pitch, same length" if pad["stretch"] else "resample: pitch changes the length")
        self.save_reload()

    def snap_pitch(self):
        pad = self.pads[self.pad]
        if pad:
            near = round(pad["pitch"])
            if abs(pad["pitch"] - near) <= 0.08:
                pad["pitch"] = float(near)

    def knob(self, p, rect, label, value, lo, hi, px, unit, get, set_, snap=None, show=None):
        s = self.panel.s
        self.panel.box(p, rect, PANEL, BORDER, radius=8)
        self.panel.text(p, rect.adjusted(0, 3 * s, 0, 0), label, DIM, 10, Qt.AlignHCenter | Qt.AlignTop)
        self.panel.text(p, rect.adjusted(0, 16 * s, 0, 0), show(value) if show else f"{value:.2f}", TEXT, 15,
                        bold=True)
        self.panel.region(rect, lambda pos: PadDrag(pos, get, set_, lo, hi, px, unit, self.save_reload, snap,
                                                    self.save_live))

    def toggle_env(self):
        """The waveform area becomes the amp/filter envelope editor."""
        self.env_open = not self.env_open

    @staticmethod
    def env_value(env, t):
        """The same curve the plugin plays: attack, decay, sustain, release, all fractions of the slice."""
        a, d, s, r = env
        if t <= 0.0:
            return 0.0 if a > 0.0 else 1.0
        if a > 0.0 and t < a:
            return t / a
        if d > 0.0 and t < a + d:
            return 1.0 - (1.0 - s) * (t - a) / d
        if r > 0.0:
            rr = 1.0 - r
            if t >= rr:
                return s * max(0.0, 1.0 - (t - rr) / r)
        return s

    def set_env(self, which, t, v):
        """Move one envelope point: attack, decay (and sustain with its height) or release."""
        pad = self.pads[self.pad]
        if not pad:
            return
        env = pad.setdefault("amp" if self.env_tab == "amp" else "filter", [0.0, 0.0, 1.0, 0.0])
        a, d, s, r = env
        if which == "attack":
            a = min(max(0.0, t), max(0.0, 1.0 - d - r))
        elif which == "decay":
            d = min(max(0.0, t - a), max(0.0, 1.0 - a - r))
            s = max(0.0, min(1.0, v))
        else:
            r = min(max(0.0, 1.0 - t), max(0.0, 1.0 - a - d))
        env[:] = [a, d, s, r]

    def toggle_mono(self):
        self.mono = not self.mono
        self.say("mono: one pad at a time" if self.mono else "poly: pads can overlap")
        self.save_reload()

    def toggle_gate(self):
        pad = self.pads[self.pad]
        if pad:
            pad["gate"] = 0 if pad.get("gate", 0) else 1
            self.say("gate: plays while held" if pad["gate"] else "one-shot: plays to the end")
            self.save_reload()

    def toggle_loop(self):
        pad = self.pads[self.pad]
        if pad:
            pad["loop"] = 0 if pad["loop"] else 1
            self.save_reload()

    def toggle_reverse(self):
        pad = self.pads[self.pad]
        if pad:
            pad["reverse"] = 0 if pad["reverse"] else 1
            self.save_reload()
