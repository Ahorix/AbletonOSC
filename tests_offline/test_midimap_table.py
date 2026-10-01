"""Offline tests for abletonosc/midimap_table.py (no Live needed).
Run: python3 -m unittest discover -s tests_offline   (from the AbletonOSC root)"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("midimap_table", os.path.join(HERE, "..", "abletonosc", "midimap_table.py"))
mt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mt)


class Param:
    def __init__(self, name, lo=0.0, hi=1.0, quantized=False, value=0.0):
        self.name, self.min, self.max, self.is_quantized, self.value = name, lo, hi, quantized, value


class Device:
    def __init__(self, name, params=(), chains=(), class_name="X"):
        self.name, self.parameters, self.class_name = name, list(params), class_name
        self.chains = [Chain(c) for c in chains]
        self.can_have_chains = bool(chains)


class Chain:
    def __init__(self, devices):
        self.devices = list(devices)


class Clip:
    def __init__(self, name):
        self.name = name


class Slot:
    def __init__(self, clip=None):
        self.clip, self.has_clip, self.fired = clip, clip is not None, 0

    def fire(self):
        self.fired += 1


class Mixer:
    def __init__(self):
        self.volume, self.panning = Param("Track Volume"), Param("Track Panning", -1, 1)
        self.track_activator, self.sends = Param("Speaker On", 0, 1, True, 1), [Param("A"), Param("B")]


class Track:
    def __init__(self, name, devices=(), slots=()):
        self.name, self.devices, self.clip_slots, self.mixer_device = name, list(devices), list(slots), Mixer()


class Song:
    def __init__(self, tracks, returns=(), master=None):
        self.tracks, self.return_tracks = list(tracks), list(returns)
        self.master_track = master or Track("Master")


class FakeMidiMap:
    class MapMode:
        absolute = "abs"
        values = {0: "abs", 2: "rel"}

    def __init__(self):
        self.calls = []

    def map_midi_cc(self, h, p, ch, cc, mode, takeover):
        self.calls.append(("map_cc", p.name, ch, cc, mode))

    def map_midi_note(self, h, p, ch, note):
        self.calls.append(("map_note", p.name, ch, note))

    def forward_midi_cc(self, sh, h, ch, cc):
        self.calls.append(("fwd_cc", ch, cc))

    def forward_midi_note(self, sh, h, ch, note):
        self.calls.append(("fwd_note", ch, note))


def entry(ch, num, target, kind="cc", **kw):
    e = {"ch": ch, "kind": kind, "num": num, "mode": 0, "target": target}
    e.update(kw)
    return e


def param(track, device, name):
    return {"type": "param", "track": track, "device": device, "param": name}


class MidiMapTableTest(unittest.TestCase):
    def setUp(self):
        self.delay = Param("Delay", 0, 127)
        self.rack = Device("fx rack", [Param("Device On", 0, 1, True), self.delay],
                           chains=[[Device("Delay", [Param("Feedback")])]], class_name="AudioEffectGroupDevice")
        self.panic = Param("panic", 0, 127)
        self.slot = Slot(Clip("osszeolv spoken"))
        self.song = Song(
            [Track("lead_osszeolv", [self.rack]), Track("spoken_hd_osszeolv", [], [Slot(), self.slot])],
            returns=[Track("A-Reverb")],
            master=Track("Master", [Device("panic", [Param("Device On"), self.panic])]),
        )
        self.dir = tempfile.mkdtemp()
        self.table = mt.MidiMapTable(os.path.join(self.dir, "midimap.json"))

    def test_resolves_nested_and_fallback_paths(self):
        p, r = mt.resolve_target(self.song, param("lead_osszeolv", ["fx rack", "Delay"], "Feedback"))
        self.assertEqual((p.name, r), ("Feedback", ""))
        p, r = mt.resolve_target(self.song, param("lead_osszeolv", ["Delay"], "feedback"))
        self.assertEqual(p.name, "Feedback")
        p, r = mt.resolve_target(self.song, param("lead_osszeolv", ["fx rack"], "Delay"))
        self.assertIs(p, self.delay)

    def test_master_returns_mixer_and_reasons(self):
        self.assertIs(mt.resolve_target(self.song, param("Master", ["panic"], "panic"))[0], self.panic)
        self.assertEqual(mt.resolve_target(self.song, param("A-Reverb", ["Mixer"], "Volume"))[0].name, "Track Volume")
        self.assertEqual(mt.resolve_target(self.song, param("lead_osszeolv", ["Mixer"], "Send B"))[0].name, "B")
        self.assertEqual(mt.resolve_target(self.song, param("nope", ["x"], "y"))[1], "track not in set")
        self.assertEqual(mt.resolve_target(self.song, param("lead_osszeolv", ["x"], "y"))[1], "device not found")
        self.assertEqual(mt.resolve_target(self.song, param("lead_osszeolv", ["fx rack"], "y"))[1], "param not found")

    def test_build_native_vs_forwarded(self):
        self.table.set_scope("song:a", [
            entry(0, 41, param("lead_osszeolv", ["fx rack"], "Delay")),                    # native
            entry(0, 46, param("Master", ["panic"], "panic")),                              # shared trigger
            entry(0, 46, param("lead_osszeolv", ["fx rack", "Delay"], "Feedback")),
            entry(0, 47, param("lead_osszeolv", ["fx rack"], "Delay"), min=0, max=64),      # ranged
            entry(0, 92, {"type": "clip", "track": "spoken_hd_osszeolv", "clip": "osszeolv spoken"}, kind="note"),
            entry(0, 93, param("lead_osszeolv", ["Mixer"], "Track Activator"), kind="note"),  # native note
            entry(0, 50, param("missing", ["x"], "y")),
        ])
        mm = FakeMidiMap()
        self.table.build(self.song, "h", mm, "sh")
        self.assertIn(("map_cc", "Delay", 0, 41, "abs"), mm.calls)
        self.assertIn(("map_note", "Speaker On", 0, 93), mm.calls)
        self.assertIn(("fwd_cc", 0, 46), mm.calls)
        self.assertIn(("fwd_cc", 0, 47), mm.calls)
        self.assertIn(("fwd_note", 0, 92), mm.calls)
        self.assertNotIn(("fwd_cc", 0, 50), mm.calls)
        self.assertEqual([r for _, r in self.table.last_status].count("track not in set"), 1)

        # Forwarded CC 46 drives both targets
        self.assertTrue(self.table.handle_midi((0xB0, 46, 127)))
        self.assertEqual(self.panic.value, 127)
        # Ranged CC 47 scales into 0..64
        self.table.handle_midi((0xB0, 47, 127))
        self.assertAlmostEqual(self.delay.value, 64)
        # Note 92 fires the clip on note-on only
        self.table.handle_midi((0x90, 92, 100))
        self.table.handle_midi((0x80, 92, 0))
        self.assertEqual(self.slot.fired, 1)
        # Unknown trigger is not consumed
        self.assertFalse(self.table.handle_midi((0xB0, 99, 1)))

    def test_persistence_and_scopes(self):
        self.table.set_scope("song:a", [entry(0, 1, param("t", ["d"], "p"))])
        self.table.set_scope("global", [entry(0, 2, param("t", ["d"], "p"))])
        self.table.set_scope("song:a", [entry(0, 3, param("t", ["d"], "p"))])
        again = mt.MidiMapTable(self.table.path)
        again.load()
        self.assertEqual(sorted(e["num"] for e in again.entries), [2, 3])
        again.clear("global")
        self.assertEqual([e["scope"] for e in again.entries], ["song:a"])
        again.clear()
        with open(self.table.path) as f:
            self.assertEqual(json.load(f)["entries"], [])

    def test_note_toggles_and_onoff_cc(self):
        p = Param("On", 0, 1, True, 0)
        mt.apply_to_parameter({}, p, "note", 100)
        self.assertEqual(p.value, 1)
        mt.apply_to_parameter({}, p, "note", 100)
        self.assertEqual(p.value, 0)
        mt.apply_to_parameter({}, p, "cc", 80)
        self.assertEqual(p.value, 1)

    def test_device_tree_lists_nested_and_mixer(self):
        tree = mt.device_tree(self.song.tracks[0])
        self.assertEqual([d["path"] for d in tree], [["Mixer"], ["fx rack"], ["fx rack", "Delay"]])
        self.assertIn("Send A", mt.parameter_names(self.song.tracks[0], ["Mixer"]))


if __name__ == "__main__":
    unittest.main()
