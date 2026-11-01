# SPDX-License-Identifier: GPL-3.0-or-later
"""Plugin hosting for Touch-Seq through Carla's engine library (LV2 / VST3 / CLAP).

Carla runs the plugins in its own audio threads (JACK, which PipeWire provides), one JACK client per
plugin; Python only controls it. Audio and MIDI wiring between those clients is done with pw-link."""
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# Carla as laid out by its `make install`: CARLA_PREFIX=/usr (system), the AppImage points it into itself
CARLA_PREFIX = Path(os.environ.get("CARLA_PREFIX", "/usr"))
CARLA_LIB = str(CARLA_PREFIX / "lib/carla/libcarla_standalone2.so")
CARLA_PY = str(CARLA_PREFIX / "share/carla")
CARLA_BIN = str(CARLA_PREFIX / "lib/carla")
CARLA_RES = str(CARLA_PREFIX / "share/carla/resources")
DISCOVERY = CARLA_BIN + "/carla-discovery-native"

HOME = Path.home()


def search_path(var, defaults):
    """Plugin folders: the standard $CLAP_PATH / $VST3_PATH / $LV2_PATH first (e.g. the AppImage's own
    XYFX), then the usual places."""
    extra = [Path(p) for p in os.environ.get(var, "").split(":") if p]
    return extra + [d for d in defaults if d not in extra]


SEARCH = {"clap": search_path("CLAP_PATH", [HOME / ".clap", Path("/usr/lib/clap"), Path("/usr/local/lib/clap")]),
          "vst3": search_path("VST3_PATH", [HOME / ".vst3", Path("/usr/lib/vst3"), Path("/usr/local/lib/vst3")])}
LV2_DIRS = search_path("LV2_PATH", [HOME / ".lv2", Path("/usr/lib/lv2"), Path("/usr/local/lib/lv2")])


@dataclass
class PluginRef:
    fmt: str  # "clap", "vst3" or "lv2"
    path: str  # plugin file / bundle ("" for LV2, which is found by URI)
    label: str  # which plugin inside it: CLAP id, VST3 class id, LV2 URI
    name: str
    maker: str = ""
    midi_ins: int = 0
    audio_ins: int = 0
    audio_outs: int = 0

    @property
    def instrument(self):
        return self.midi_ins > 0 and self.audio_outs > 0

    @property
    def effect(self):
        return self.audio_ins > 0 and self.audio_outs > 0


# --- discovery ---------------------------------------------------------------
def parse_discovery(text, fmt, path):
    refs, cur = [], None
    for line in text.splitlines():
        if not line.startswith("carla-discovery::"):
            continue
        _, key, value = line.split("::", 2)
        if key == "init":
            cur = {}
        elif key == "end" and cur is not None:
            if cur.get("name"):
                label = cur.get("label", "")
                if fmt == "lv2":  # discovery reports "bundle.lv2/URI", Carla loads by URI
                    label = label.split("/", 1)[-1]
                refs.append(PluginRef(fmt, path, label, cur["name"], cur.get("maker", ""),
                                      int(cur.get("midi.ins", 0)), int(cur.get("audio.ins", 0)),
                                      int(cur.get("audio.outs", 0))))
            cur = None
        elif cur is not None:
            cur[key] = value
    return refs


def run_discovery(fmt, target):
    # coreutils `timeout` kills a hanging scanner even if we exit meanwhile (an orphan would spin forever)
    try:
        out = subprocess.run(["timeout", "-s", "KILL", "60", DISCOVERY, fmt, target], capture_output=True,
                             text=True, errors="replace", timeout=65).stdout
    except subprocess.TimeoutExpired:
        return []
    return parse_discovery(out, fmt, "" if fmt == "lv2" else target)


def plugin_files(fmt):
    """CLAP plugins are files, VST3 plugins are bundle directories; both may sit in vendor subfolders."""
    ext = "." + fmt
    for root in SEARCH[fmt]:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            for d in [d for d in dirnames if d.endswith(ext)]:
                dirnames.remove(d)
                yield os.path.join(dirpath, d)
            for f in filenames:
                if f.endswith(ext):
                    yield os.path.join(dirpath, f)


def stamp(fmt, target):
    if fmt == "lv2":  # adding/removing a bundle changes its parent directory's mtime
        return max((d.stat().st_mtime for d in LV2_DIRS if d.is_dir()), default=0)
    return os.path.getmtime(target)


class Catalog:
    """All installed plugins. Startup only reads the cache; a scan (background thread) runs when there
    is no cache yet or on rescan(), and then only discovers files that are new or changed."""

    def __init__(self, cache_path):
        self.cache_path = cache_path
        self.plugins = []
        self.ready = False  # plugins is usable
        self.scanning = False
        self.progress = ""
        try:
            self.use(json.loads(cache_path.read_text()))
        except (OSError, ValueError):
            self.rescan()

    def use(self, cache):
        self.plugins = sorted((PluginRef(**r) for refs in cache.values() for r in refs),
                              key=lambda r: (r.name.lower(), r.fmt))
        self.ready = True

    def rescan(self):
        if not self.scanning:
            self.scanning = True
            threading.Thread(target=self.scan, daemon=True).start()

    def scan(self):
        try:
            cache = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            cache = {}
        jobs = [(fmt, p) for fmt in SEARCH for p in plugin_files(fmt)] + [("lv2", ":all")]
        fresh = {}
        for n, (fmt, target) in enumerate(jobs):
            self.progress = f"{n}/{len(jobs)}"
            key = f"{fmt}|{target}|{stamp(fmt, target)}"
            fresh[key] = cache[key] if key in cache else [asdict(r) for r in run_discovery(fmt, target)]
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(fresh))
        os.replace(tmp, self.cache_path)
        self.use(fresh)
        self.scanning = False


# --- engine ------------------------------------------------------------------
@dataclass
class Param:
    index: int
    name: str
    lo: float
    hi: float
    integer: bool
    boolean: bool


class Plugin:
    """Handle to a loaded plugin. Carla identifies plugins by position, which shifts when an earlier one
    is removed, so the id is looked up on use."""

    def __init__(self, host, ref, client):
        self.host, self.ref, self.client = host, ref, client
        self.params = []

    @property
    def pid(self):
        return self.host.plugins.index(self)


class Host:
    def __init__(self, client="Touch-Seq"):
        if CARLA_PY not in sys.path:
            sys.path.insert(0, CARLA_PY)
        import carla_backend as cb
        self.cb = cb
        self.h = h = cb.CarlaHostDLL(CARLA_LIB, False)
        h.set_engine_option(cb.ENGINE_OPTION_PROCESS_MODE, cb.ENGINE_PROCESS_MODE_MULTIPLE_CLIENTS, "")
        h.set_engine_option(cb.ENGINE_OPTION_TRANSPORT_MODE, cb.ENGINE_TRANSPORT_MODE_DISABLED, "")
        h.set_engine_option(cb.ENGINE_OPTION_OSC_ENABLED, 0, "")
        h.set_engine_option(cb.ENGINE_OPTION_PATH_BINARIES, 0, CARLA_BIN)
        h.set_engine_option(cb.ENGINE_OPTION_PATH_RESOURCES, 0, CARLA_RES)
        if not h.engine_init("JACK", client):
            raise RuntimeError(h.get_last_error() or "Carla engine failed to start")
        self.plugins = []

    def add(self, ref, client):
        cb, h = self.cb, self.h
        ptype = {"clap": cb.PLUGIN_CLAP, "vst3": cb.PLUGIN_VST3, "lv2": cb.PLUGIN_LV2}[ref.fmt]
        count = h.get_current_plugin_count()
        h.add_plugin(cb.BINARY_NATIVE, ptype, ref.path, client, ref.label, 0, None, cb.PLUGIN_OPTIONS_NULL)
        if h.get_current_plugin_count() != count + 1:  # add_plugin's return value isn't reliable
            raise RuntimeError(h.get_last_error() or f"can't load {ref.name}")
        plugin = Plugin(self, ref, client)
        self.plugins.append(plugin)
        self.refresh_params(plugin)
        return plugin

    def refresh_params(self, plugin):
        cb, h, pid = self.cb, self.h, plugin.pid
        plugin.params = []
        for i in range(h.get_parameter_count(pid)):
            data = h.get_parameter_data(pid, i)
            if data["type"] != cb.PARAMETER_INPUT or not data["hints"] & cb.PARAMETER_IS_ENABLED:
                continue
            rng = h.get_parameter_ranges(pid, i)
            if rng["max"] <= rng["min"]:
                continue
            plugin.params.append(Param(i, h.get_parameter_info(pid, i)["name"], rng["min"], rng["max"],
                                       bool(data["hints"] & cb.PARAMETER_IS_INTEGER),
                                       bool(data["hints"] & cb.PARAMETER_IS_BOOLEAN)))

    def remove(self, plugin):
        self.h.remove_plugin(plugin.pid)
        self.plugins.remove(plugin)

    def value(self, plugin, index):
        return self.h.get_current_parameter_value(plugin.pid, index)

    def text(self, plugin, index):
        return self.h.get_parameter_text(plugin.pid, index)

    def set_value(self, plugin, index, value):
        self.h.set_parameter_value(plugin.pid, index, value)

    def peaks(self, plugin):
        """A plugin's output peaks as (left, right), linear (0.0 = silence): Carla's own meter data."""
        return (self.h.get_output_peak_value(plugin.pid, True), self.h.get_output_peak_value(plugin.pid, False))

    def set_volume(self, plugin, volume):
        self.h.set_volume(plugin.pid, volume)

    def ports(self, plugin):
        info = self.h.get_audio_port_count_info(plugin.pid)
        return info["ins"], info["outs"]

    def has_ui(self, plugin):
        return bool(self.h.get_plugin_info(plugin.pid)["hints"] & self.cb.PLUGIN_HAS_CUSTOM_UI)

    def show_ui(self, plugin, on=True):
        self.h.show_custom_ui(plugin.pid, on)

    def save_state(self, plugin, path):
        return self.h.save_plugin_state(plugin.pid, str(path))

    def load_state(self, plugin, path):
        return self.h.load_plugin_state(plugin.pid, str(path))

    def idle(self):
        self.h.engine_idle()

    def close(self):
        self.h.engine_close()


# --- wiring ------------------------------------------------------------------
def settings_metadata():
    """PipeWire's "settings" metadata as {key: value string}, from one `pw-metadata` ({} when it doesn't
    answer, e.g. no PipeWire)."""
    try:
        out = subprocess.run(["pw-metadata", "-n", "settings", "0"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    values = {}
    for line in out.splitlines():
        if "key:'" in line and "value:'" in line:
            key = line.split("key:'", 1)[1].split("'", 1)[0]
            values[key] = line.split("value:'", 1)[1].split("'", 1)[0]
    return values


def audio_inputs():
    """The mono inputs an audio track can take its sound from: the capture ports of the machine's audio
    devices (Audio/Source nodes) - not a sink's monitor ports, and not the outputs of plugins, which are
    sources on the graph too but not an input you'd record."""
    try:
        dump = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    devices = {}
    for obj in dump:
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") == "Audio/Source" and props.get("node.name"):
            devices[obj["id"]] = (props["node.name"], props.get("node.description") or props.get("node.nick")
                                  or props["node.name"])
    inputs = []
    for obj in dump:
        info = obj.get("info") or {}
        props = info.get("props") or {}
        if (obj.get("type") != "PipeWire:Interface:Port" or props.get("port.direction") != "out"
                or "audio" not in (props.get("format.dsp") or "") or props.get("node.id") not in devices):
            continue
        node = devices[props["node.id"]]
        inputs.append((f"{node[0]}:{props.get('port.name')}", node[1], props.get("port.name", "")))
    return sorted(inputs, key=lambda i: (i[1].lower(), port_order(i[2])))


def audio_input_choices():
    """The inputs of audio_inputs() as (port, "device · channel"), the shape the picker dialogs take."""
    return [(port, f"{label} · {name}") for port, label, name in audio_inputs()]


def quantum():
    """PipeWire's audio buffer size in samples: (forced, system default, min, max, rate), zeros when it does
    not answer. Nothing forced means the default is what the whole graph runs at."""
    values = settings_metadata()

    def number(key):
        try:
            return int(values.get(key, "0"))
        except ValueError:
            return 0

    return (number("clock.force-quantum"), number("clock.quantum"), number("clock.min-quantum"),
            number("clock.max-quantum"), number("clock.rate"))


def set_quantum(size):
    """Ask PipeWire for a buffer size in samples (0 puts the system's default back). It applies to the whole
    audio graph while it is set: that is how a machine's latency is chosen, not just this app's."""
    try:
        subprocess.run(["pw-metadata", "-n", "settings", "0", "clock.force-quantum", str(int(size))],
                       capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def default_sink():
    """PipeWire's default audio output node name, or None."""
    try:
        out = subprocess.run(["pw-metadata", "0", "default.audio.sink"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"value:'(\{.*?\})'", out)
    return json.loads(m.group(1)).get("name") if m else None


def sinks():
    """Audio outputs as [(node name, description)], e.g. ('alsa_output…Headphones__sink', 'Built-in Audio Headphones')."""
    try:
        dump = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    out = []
    for obj in dump:
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") == "Audio/Sink" and props.get("node.name"):
            out.append((props["node.name"], props.get("node.description") or props["node.name"]))
    return out


def existing_links():
    """All current port links as (out port, in port), from one `pw-link -l`."""
    try:
        out = subprocess.run(["pw-link", "-l"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    links, port = set(), None
    for line in out.splitlines():
        if not line.startswith(" "):
            port = line.strip()
        elif "|->" in line:
            links.add((port, line.split("|->", 1)[1].strip()))
    return links


CHANNELS = {"FL": 0, "FR": 1, "FC": 2, "LFE": 3}  # then the numbered channels, in order


def port_order(name):
    """Sort key that keeps a device's ports in a natural order: capture_FL, capture_FR, capture_FC, ...,
    then numbered ones - rather than alphabetically (FC before FL, 10 before 2)."""
    base, _, channel = name.rpartition("_")
    if channel.isdigit():
        return base, 4, int(channel), 0
    return base, CHANNELS.get(channel, 9), 0, 0


def audio_ports():
    """Audio ports as (sources, destinations), each a list of (port, node label, port name) with `port` being
    what pw-link takes ("node:port"). Sources are the ports audio comes out of (an interface's inputs, a
    plugin's outputs), destinations the ones it goes into - so a device's ports end up together, in channel
    order. One `pw-dump`; MIDI and video ports are left out."""
    try:
        dump = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return [], []
    nodes = {}
    for obj in dump:
        props = (obj.get("info") or {}).get("props") or {}
        name = props.get("node.name") or props.get("client.name")
        if name:
            nodes[obj["id"]] = (name, props.get("node.description") or props.get("node.nick") or name)
    outs, ins = [], []
    for obj in dump:
        info = obj.get("info") or {}
        props = info.get("props") or {}
        if obj.get("type") != "PipeWire:Interface:Port" or "audio" not in (props.get("format.dsp") or ""):
            continue
        node = nodes.get(props.get("node.id"))
        if not node:
            continue
        item = (props.get("port.id", 0), f"{node[0]}:{props.get('port.name')}", node[1], props.get("port.name", ""))
        (outs if props.get("port.direction") == "out" else ins).append(item)

    def sorted_ports(ports):
        return [(port, label, name) for _, port, label, name
                in sorted(ports, key=lambda i: (i[2].lower(), port_order(i[3]), i[0]))]

    return sorted_ports(outs), sorted_ports(ins)


class Wiring:
    """Port links made with pw-link, grouped by owner so an owner's links can be replaced at once.
    Runs in worker threads: freshly created JACK ports take a moment to appear in PipeWire, and links
    vanish when a device goes away (e.g. headphones unplugged), so heal() puts missing ones back."""

    def __init__(self):
        self.links = {}  # owner -> set of (out port, in port) wanted
        self.lock = threading.Lock()

    def replace(self, owner, links):
        threading.Thread(target=self._replace, args=(owner, set(links)), daemon=True).start()

    def _replace(self, owner, links):
        with self.lock:
            old = self.links.get(owner, set())
            for a, b in old - links:
                subprocess.run(["pw-link", "-d", a, b], capture_output=True)
            for a, b in links - old:
                for _ in range(20):
                    r = subprocess.run(["pw-link", a, b], capture_output=True, text=True)
                    if r.returncode == 0 or "exists" in r.stderr.lower():
                        break
                    time.sleep(0.1)
                else:
                    print(f"pw-link {a} -> {b} failed: {r.stderr.strip()}")
            self.links[owner] = links

    def heal(self):
        """Recreate wanted links that are gone (a link to a port that doesn't exist just fails quietly)."""
        with self.lock:
            missing = set().union(*self.links.values()) - existing_links()
            for a, b in missing:
                subprocess.run(["pw-link", a, b], capture_output=True)
