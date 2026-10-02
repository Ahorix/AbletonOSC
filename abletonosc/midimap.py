from .handler import AbletonOSCHandler
from . import midimap_table
from typing import Tuple, Any
import json

# Replies go out as single UDP datagrams; macOS caps those at ~9 KB, so lists are paged.
PAGE = 64


class MidiMapHandler(AbletonOSCHandler):
    def __init__(self, manager):
        super().__init__(manager)
        self.class_identifier = "midimap"
        self.midi_map_handle = None

    def init_api(self):
        table = lambda: self.manager.midimap_table

        def make_midi_map(params: Tuple[Any] = ()):
            # Index based, transient (not persisted). Kept for compatibility.
            track_index = int(params[0])
            device_index = int(params[1])
            parameter_index = int(params[2])
            channel = int(params[3])
            cc = int(params[4])

            parameter = self.song.tracks[track_index].devices[device_index].parameters[parameter_index]
            self.manager.midi_mappings[(channel, cc)] = parameter

            self.manager.request_rebuild_midi_map()

        def set_scope(params: Tuple[Any] = ()):
            """[scope, json array of entries] -> (scope, n_ok, n_unresolved, descriptions...)"""
            scope = str(params[0])
            entries = json.loads(params[1]) if len(params) > 1 and params[1] else []
            table().set_scope(scope, entries)
            self.manager.request_rebuild_midi_map()
            status = table().resolve(self.song, table().scope_entries(scope))
            bad = [midimap_table.describe(e) + ": " + r for e, _, r in status if r]
            return (scope, len(status) - len(bad), len(bad), *bad[:20])

        def clear(params: Tuple[Any] = ()):
            table().clear(str(params[0]) if params else None)
            self.manager.request_rebuild_midi_map()

        def resolve(params: Tuple[Any] = ()):
            """[json array of entries] -> (json array of reasons, "" = found). Nothing is stored."""
            entries = json.loads(params[0])
            return (json.dumps([r for _, _, r in table().resolve(self.song, entries)]),)

        def get_scopes(params: Tuple[Any] = ()):
            counts = {}
            for e in table().entries:
                counts[e.get("scope")] = counts.get(e.get("scope"), 0) + 1
            return tuple(x for kv in sorted(counts.items()) for x in kv)

        def get_tracks(params: Tuple[Any] = ()):
            return tuple(midimap_table.all_track_names(self.song))

        def get_devices(params: Tuple[Any] = ()):
            track = midimap_table.find_track(self.song, str(params[0]))
            if track is None:
                return (str(params[0]), "[]")
            return (str(params[0]), json.dumps(midimap_table.device_tree(track)))

        def get_params(params: Tuple[Any] = ()):
            """[track, json path, offset] -> (total, offset, names...)"""
            track = midimap_table.find_track(self.song, str(params[0]))
            offset = int(params[2]) if len(params) > 2 else 0
            names = midimap_table.parameter_names(track, json.loads(params[1])) if track is not None else None
            if names is None:
                return (-1, offset)
            return (len(names), offset, *names[offset:offset + PAGE])

        def get_param_range(params: Tuple[Any] = ()):
            """[track, json path, param] -> (min, max, min as shown in Live, max as shown in Live)"""
            track = midimap_table.find_track(self.song, str(params[0]))
            if track is None:
                return ()
            param, _ = midimap_table.resolve_target(self.song, {
                "type": "param", "track": str(params[0]), "device": json.loads(params[1]), "param": str(params[2])})
            if param is None:
                return ()
            show = lambda v: param.str_for_value(v) if hasattr(param, "str_for_value") else str(v)
            return (float(param.min), float(param.max), show(param.min), show(param.max))

        def rebuild(params: Tuple[Any] = ()):
            self.manager.request_rebuild_midi_map()

        self.osc_server.add_handler("/live/midimap/map_cc", make_midi_map)
        self.osc_server.add_handler("/live/midimap/set_scope", set_scope)
        self.osc_server.add_handler("/live/midimap/clear", clear)
        self.osc_server.add_handler("/live/midimap/resolve", resolve)
        self.osc_server.add_handler("/live/midimap/get/scopes", get_scopes)
        self.osc_server.add_handler("/live/midimap/get/tracks", get_tracks)
        self.osc_server.add_handler("/live/midimap/get/devices", get_devices)
        self.osc_server.add_handler("/live/midimap/get/params", get_params)
        self.osc_server.add_handler("/live/midimap/get/param_range", get_param_range)
        self.osc_server.add_handler("/live/midimap/rebuild", rebuild)
