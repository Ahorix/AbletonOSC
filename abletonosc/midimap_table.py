"""
Name-based MIDI mapping table, owned by the Manager.

Entries are stored by name (track, device name path, parameter or clip name) under a
scope (e.g. "song:<id>", "global"), persisted to a JSON file next to the Remote Script,
and resolved against the current song every time Live asks for the MIDI map. That way
the mappings survive importing tracks into another set and restarting Live.

No Live import at module level: the Live.MidiMap module is passed in, so this file can
be unit tested with fakes (tests_offline/test_midimap_table.py).

Entry dict:
    {"ch": 0-15, "kind": "cc"|"note", "num": 0-127, "mode": int (0 = absolute),
     "encoder": "absolute" | "relative_two_compliment" | "relative_binary_offset" | "relative_signed_bit"
                (Live.MidiMap.MapMode names; overrides "mode" when set),
     "min": float|None, "max": float|None,
     "target": {"type": "param", "track": str, "device": [str, ...], "param": str}
             | {"type": "clip", "track": str, "clip": str}}
"""
import json
import logging
import os
import re

logger = logging.getLogger("abletonosc")

MIXER = "Mixer"
SEND_LETTERS = "ABCDEFGHIJKL"


def _norm(name):
    return re.sub(r"[^0-9a-z]", "", str(name).lower())


def describe(entry):
    t = entry.get("target", {})
    trig = "ch%d %s%d" % (int(entry.get("ch", 0)) + 1, "CC" if entry.get("kind") == "cc" else "note ", int(entry.get("num", 0)))
    if t.get("type") == "clip":
        what = "%s / clip %s" % (t.get("track"), t.get("clip"))
    else:
        what = "%s / %s / %s" % (t.get("track"), " > ".join(t.get("device") or []), t.get("param"))
    return "%s -> %s" % (trig, what)


# --------------------------------------------------------------------------------
# Name resolution against the LOM
# --------------------------------------------------------------------------------

def find_track(song, name):
    tracks = list(song.tracks) + list(song.return_tracks)
    for t in tracks:
        if t.name == name:
            return t
    if str(name).lower() == "master":
        return song.master_track
    for t in tracks:
        if _norm(t.name) == _norm(name):
            return t
    return None


def all_track_names(song):
    return [t.name for t in song.tracks] + [t.name for t in song.return_tracks] + ["Master"]


def flatten_devices(devices, prefix=()):
    """[(name_path_tuple, device)] in the same DFS order as /live/flat_device (chains, not return chains)."""
    result = []
    for device in devices:
        path = prefix + (device.name,)
        result.append((path, device))
        if getattr(device, "can_have_chains", False):
            for chain in device.chains:
                result.extend(flatten_devices(chain.devices, path))
    return result


def mixer_parameters(track):
    """[(name, parameter)] of a track's mixer, in display order."""
    mixer = track.mixer_device
    out = []
    for attr, label in (("volume", "Volume"), ("panning", "Pan"), ("track_activator", "Track Activator"),
                        ("cue_volume", "Cue Volume"), ("song_tempo", "Song Tempo"), ("crossfader", "Crossfader")):
        p = getattr(mixer, attr, None)
        if p is not None:
            out.append((label, p))
    for i, send in enumerate(getattr(mixer, "sends", []) or []):
        out.append(("Send " + (SEND_LETTERS[i] if i < len(SEND_LETTERS) else str(i + 1)), send))
    return out


def find_device(track, path):
    path = tuple(path or ())
    if not path:
        return None
    flat = flatten_devices(track.devices)
    for p, d in flat:
        if p == path:
            return d
    # Fallback: the last name anywhere in the track (single names, or the rack was renamed)
    for p, d in flat:
        if p[-1] == path[-1]:
            return d
    for p, d in flat:
        if _norm(p[-1]) == _norm(path[-1]):
            return d
    return None


def find_parameter(named_params, name):
    for n, p in named_params:
        if n == name:
            return p
    for n, p in named_params:
        if _norm(n) == _norm(name):
            return p
    return None


def resolve_target(song, target):
    """Returns (object, reason). object is a DeviceParameter for "param", the Track for "clip"."""
    track = find_track(song, target.get("track"))
    if track is None:
        return None, "track not in set"
    if target.get("type") == "clip":
        if find_clip_slot(track, target.get("clip")) is None:
            return track, "clip not found (resolved again when triggered)"
        return track, ""
    device_path = target.get("device") or []
    if list(device_path) == [MIXER]:
        param = find_parameter(mixer_parameters(track), target.get("param"))
        return (param, "") if param is not None else (None, "mixer param not found")
    device = find_device(track, device_path)
    if device is None:
        return None, "device not found"
    param = find_parameter([(p.name, p) for p in device.parameters], target.get("param"))
    if param is None:
        return None, "param not found"
    return param, ""


def find_clip_slot(track, clip_name):
    for slot in track.clip_slots:
        if slot.has_clip and slot.clip.name == clip_name:
            return slot
    return None


# --------------------------------------------------------------------------------
# Table
# --------------------------------------------------------------------------------

class MidiMapTable:
    def __init__(self, path):
        self.path = path
        self.entries = []        # entry dicts, each with "scope"
        self.forwarded = {}      # (kind, ch, num) -> [(entry, target_object)]
        self.last_status = []    # [(entry, reason)] from the last build

    # ---- persistence ----

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path) as f:
                    data = json.load(f)
                self.entries = [e for e in data.get("entries", []) if isinstance(e, dict)]
                logger.info("MIDI map table: loaded %d entries from %s" % (len(self.entries), self.path))
        except Exception as e:
            logger.warning("MIDI map table: could not load %s (%s)" % (self.path, e))
            self.entries = []

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"version": 1, "entries": self.entries}, f, indent=1)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.warning("MIDI map table: could not save %s (%s)" % (self.path, e))

    # ---- edits ----

    def set_scope(self, scope, entries):
        clean = []
        for e in entries:
            e = dict(e)
            e["scope"] = scope
            e["ch"] = int(e.get("ch", 0))
            e["num"] = int(e.get("num", 0))
            e["kind"] = "note" if e.get("kind") == "note" else "cc"
            e["mode"] = int(e.get("mode") or 0)
            clean.append(e)
        self.entries = [e for e in self.entries if e.get("scope") != scope] + clean
        self.save()

    def clear(self, scope=None):
        if scope:
            self.entries = [e for e in self.entries if e.get("scope") != scope]
        else:
            self.entries = []
        self.save()

    def scope_entries(self, scope):
        return [e for e in self.entries if e.get("scope") == scope]

    # ---- resolution ----

    def resolve(self, song, entries=None):
        """[(entry, object, reason)]; reason "" = fully resolved."""
        out = []
        for e in (self.entries if entries is None else entries):
            try:
                obj, reason = resolve_target(song, e.get("target") or {})
            except Exception as ex:
                obj, reason = None, "error: %s" % ex
            out.append((e, obj, reason))
        return out

    @staticmethod
    def _map_mode(entry, midi_map):
        mode = midi_map.MapMode.absolute
        encoder = entry.get("encoder")
        if encoder and encoder != "absolute":
            return getattr(midi_map.MapMode, encoder, mode)
        if entry.get("mode"):
            return midi_map.MapMode.values.get(entry["mode"], mode)
        return mode

    @staticmethod
    def _full_range(entry, param):
        lo, hi = entry.get("min"), entry.get("max")
        if lo is None and hi is None:
            return True
        lo = param.min if lo is None else float(lo)
        hi = param.max if hi is None else float(hi)
        eps = 1e-6 * max(1.0, abs(param.max - param.min))
        return abs(lo - param.min) <= eps and abs(hi - param.max) <= eps

    def build(self, song, midi_map_handle, midi_map, script_handle):
        """Install the table into Live's MIDI map. midi_map = Live.MidiMap."""
        self.forwarded = {}
        groups = {}
        self.last_status = []
        for entry, obj, reason in self.resolve(song):
            self.last_status.append((entry, reason))
            if obj is None:
                continue
            key = (entry["kind"], entry["ch"], entry["num"])
            groups.setdefault(key, []).append((entry, obj))

        for key, items in groups.items():
            kind, ch, num = key
            entry, obj = items[0]
            native = (len(items) == 1 and entry["target"].get("type") == "param"
                      and self._full_range(entry, obj))
            if native:
                try:
                    if kind == "cc":
                        midi_map.map_midi_cc(midi_map_handle, obj, ch, num, self._map_mode(entry, midi_map), True)
                    else:
                        midi_map.map_midi_note(midi_map_handle, obj, ch, num)
                    continue
                except Exception as e:
                    logger.warning("MIDI map table: native map failed for %s (%s), forwarding" % (describe(entry), e))
            try:
                if kind == "cc":
                    midi_map.forward_midi_cc(script_handle, midi_map_handle, ch, num)
                else:
                    midi_map.forward_midi_note(script_handle, midi_map_handle, ch, num)
                self.forwarded[key] = items
            except Exception as e:
                logger.warning("MIDI map table: forward failed for %s (%s)" % (describe(entry), e))

        unresolved = [s for s in self.last_status if s[1] and not s[1].startswith("clip not found")]
        logger.info("MIDI map table: %d entries, %d unresolved, %d forwarded triggers"
                    % (len(self.entries), len(unresolved), len(self.forwarded)))

    # ---- forwarded MIDI ----

    def handle_midi(self, midi_bytes):
        """Apply a forwarded message. Returns True if it belonged to the table."""
        if len(midi_bytes) < 3:
            return False
        status, ch = midi_bytes[0] & 0xF0, midi_bytes[0] & 0x0F
        if status == 0xB0:
            kind, value = "cc", midi_bytes[2]
        elif status == 0x90:
            kind, value = "note", midi_bytes[2]
        elif status == 0x80:
            kind, value = "note", 0
        else:
            return False
        items = self.forwarded.get((kind, ch, midi_bytes[1]))
        if not items:
            return False
        for entry, obj in items:
            try:
                if entry["target"].get("type") == "clip":
                    if value > 0:
                        slot = find_clip_slot(obj, entry["target"].get("clip"))
                        if slot is not None:
                            slot.fire()
                else:
                    apply_to_parameter(entry, obj, kind, value)
            except Exception as e:
                logger.warning("MIDI map table: applying %s failed (%s)" % (describe(entry), e))
        return True


def relative_delta(encoder, value):
    """Steps encoded by an endless encoder in the given Live map mode."""
    if encoder == "relative_two_compliment":
        return value if value < 64 else value - 128
    if encoder == "relative_binary_offset":
        return value - 64
    if encoder == "relative_signed_bit":
        return -(value & 0x3F) if value & 0x40 else (value & 0x3F)
    return None


def apply_to_parameter(entry, param, kind, value):
    lo = param.min if entry.get("min") is None else max(param.min, float(entry["min"]))
    hi = param.max if entry.get("max") is None else min(param.max, float(entry["max"]))
    quantized = getattr(param, "is_quantized", False)
    delta = relative_delta(entry.get("encoder"), value) if kind == "cc" else None
    if delta is not None:
        v = param.value + delta * (hi - lo) / 127.0
        if quantized:
            v = round(v)
        param.value = min(hi, max(lo, v))
        return
    if kind == "note":
        if value == 0:
            return  # note off: toggles act on note on only
        param.value = lo if abs(param.value - hi) < 1e-6 else hi
        return
    if quantized and (param.max - param.min) <= 1:
        param.value = hi if value >= 64 else lo
        return
    v = lo + (hi - lo) * (value / 127.0)
    if quantized:
        v = round(v)
    param.value = min(param.max, max(param.min, v))


def device_tree(track):
    """[{"path": [...], "class": str}] incl. the track mixer, for the app's dropdowns."""
    out = [{"path": [MIXER], "class": "MixerDevice"}]
    for path, d in flatten_devices(track.devices):
        out.append({"path": list(path), "class": d.class_name})
    return out


def parameter_names(track, path):
    if list(path) == [MIXER]:
        return [n for n, _ in mixer_parameters(track)]
    device = find_device(track, path)
    return [p.name for p in device.parameters] if device is not None else None
