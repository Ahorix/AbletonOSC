import os
import sys
import tempfile
import Live
import json
from functools import partial
from typing import Tuple, Any

from .handler import AbletonOSCHandler


def _select_track_unfolded(song, track_index):
    """Select a track for browser loading. Live refuses to select a track inside a folded
    group ("The given Track is invisible"), which left browser loads without a reply, so
    unfold its parent groups first."""
    track = song.tracks[track_index]
    group = track.group_track
    while group is not None:
        group.fold_state = False
        group = group.group_track
    song.view.selected_track = track

class SongHandler(AbletonOSCHandler):
    def __init__(self, manager):
        super().__init__(manager)
        self.class_identifier = "song"

    def init_api(self):
        #--------------------------------------------------------------------------------
        # Callbacks for Song: methods
        #--------------------------------------------------------------------------------
        for method in [
            "capture_and_insert_scene",
            "capture_midi",
            "continue_playing",
            "create_audio_track",
            "create_midi_track",
            "create_return_track",
            "create_scene",
            "delete_return_track",
            "delete_scene",
            "delete_track",
            "duplicate_scene",
            "duplicate_track",
            "force_link_beat_time",
            "jump_by",
            "jump_to_prev_cue",
            "jump_to_next_cue",
            "redo",
            "re_enable_automation",
            "set_or_delete_cue",
            "start_playing",
            "stop_all_clips",
            "stop_playing",
            "tap_tempo",
            "trigger_session_record",
            "undo"
        ]:
            callback = partial(self._call_method, self.song, method)
            self.osc_server.add_handler("/live/song/%s" % method, callback)

        #--------------------------------------------------------------------------------
        # Callbacks for Song: properties (read/write)
        #--------------------------------------------------------------------------------
        properties_rw = [
            "arrangement_overdub",
            "back_to_arranger",
            "clip_trigger_quantization",
            "current_song_time",
            "groove_amount",
            "is_ableton_link_enabled",
            "loop",
            "loop_length",
            "loop_start",
            "metronome",
            "midi_recording_quantization",
            "nudge_down",
            "nudge_up",
            "punch_in",
            "punch_out",
            "record_mode",
            "root_note",
            "scale_name",
            "session_record",
            "signature_denominator",
            "signature_numerator",
            "tempo"
        ]

        #--------------------------------------------------------------------------------
        # Callbacks for Song: properties (read-only)
        #--------------------------------------------------------------------------------
        properties_r = [
            "can_redo",
            "can_undo",
            "is_playing",
            "song_length",
            "session_record_status"
        ]

        for prop in properties_r + properties_rw:
            self.osc_server.add_handler("/live/song/get/%s" % prop, partial(self._get_property, self.song, prop))
            self.osc_server.add_handler("/live/song/start_listen/%s" % prop, partial(self._start_listen, self.song, prop))
            self.osc_server.add_handler("/live/song/stop_listen/%s" % prop, partial(self._stop_listen, self.song, prop))
        for prop in properties_rw:
            self.osc_server.add_handler("/live/song/set/%s" % prop, partial(self._set_property, self.song, prop))

        #--------------------------------------------------------------------------------
        # Callbacks for Song: Track properties
        #--------------------------------------------------------------------------------
        self.osc_server.add_handler("/live/song/get/num_tracks", lambda _: (len(self.song.tracks),))

        def song_get_track_names(params):
            if len(params) == 0:
                track_index_min, track_index_max = 0, len(self.song.tracks)
            else:
                track_index_min, track_index_max = params
                if track_index_max == -1:
                    track_index_max = len(self.song.tracks)
            return tuple(self.song.tracks[index].name for index in range(track_index_min, track_index_max))
        self.osc_server.add_handler("/live/song/get/track_names", song_get_track_names)

        def song_get_track_data(params):
            """
            Retrieve one more properties of a block of tracks and their clips.
            Properties must be of the format track.property_name or clip.property_name.

            For example:
                /live/song/get/track_data 0 12 track.name clip.name clip.length

            Queries tracks 0..11, and returns a list of values comprising:

            [track_0_name, clip_0_0_name,   clip_0_1_name,   ... clip_0_7_name,
                           clip_1_0_length, clip_0_1_length, ... clip_0_7_length,
             track_1_name, clip_1_0_name,   clip_1_1_name,   ... clip_1_7_name, ...]
            """
            track_index_min, track_index_max, *properties = params
            track_index_min = int(track_index_min)
            track_index_max = int(track_index_max)
            self.logger.info("Getting track data: %s (tracks %d..%d)" %
                             (properties, track_index_min, track_index_max))
            if track_index_max == -1:
                track_index_max = len(self.song.tracks)
            rv = []
            for track_index in range(track_index_min, track_index_max):
                track = self.song.tracks[track_index]
                for prop in properties:
                    obj, property_name = prop.split(".")
                    if obj == "track":
                        if property_name == "num_devices":
                            value = len(track.devices)
                        else:
                            value = getattr(track, property_name)
                            if isinstance(value, Live.Track.Track):
                                #--------------------------------------------------------------------------------
                                # Map Track objects to their track_index to return via OSC
                                #--------------------------------------------------------------------------------
                                value = list(self.song.tracks).index(value)
                        rv.append(value)
                    elif obj == "clip":
                        for clip_slot in track.clip_slots:
                            if clip_slot.clip is not None:
                                rv.append(getattr(clip_slot.clip, property_name))
                            else:
                                rv.append(None)
                    elif obj == "clip_slot":
                        for clip_slot in track.clip_slots:
                            rv.append(getattr(clip_slot, property_name))
                    elif obj == "device":
                        for device in track.devices:
                            rv.append(getattr(device, property_name))
                    else:
                        self.logger.error("Unknown object identifier in get/track_data: %s" % obj)
            return tuple(rv)
        self.osc_server.add_handler("/live/song/get/track_data", song_get_track_data)


        def song_export_structure(params):
            tracks = []
            for track_index, track in enumerate(self.song.tracks):
                group_track = None
                if track.group_track is not None:
                    group_track = list(self.song.tracks).index(track.group_track)
                track_data = {
                    "index": track_index,
                    "name": track.name,
                    "is_foldable": track.is_foldable,
                    "group_track": group_track,
                    "clips": [],
                    "devices": []
                }
                for clip_index, clip_slot in enumerate(track.clip_slots):
                    if clip_slot.clip:
                        clip_data = {
                            "index": clip_index,
                            "name": clip_slot.clip.name,
                            "length": clip_slot.clip.length,
                        }
                        track_data["clips"].append(clip_data)

                for device_index, device in enumerate(track.devices):
                    device_data = {
                        "class_name": device.class_name,
                        "type": device.type,
                        "name": device.name,
                        "parameters": []
                    }
                    for parameter in device.parameters:
                        device_data["parameters"].append({
                            "name": parameter.name,
                            "value": parameter.value,
                            "min": parameter.min,
                            "max": parameter.max,
                            "is_quantized": parameter.is_quantized,
                        })
                    track_data["devices"].append(device_data)

                tracks.append(track_data)
            song = {
                "tracks": tracks
            }

            if sys.platform == "darwin":
                #--------------------------------------------------------------------------------
                # On macOS, TMPDIR by default points to a process-specific directory.
                # We want to use a global temp dir (typically, tmp) so that other processes
                # know where to find this output .json, so unset TMPDIR.
                #--------------------------------------------------------------------------------
                os.environ["TMPDIR"] = ""
            fd = open(os.path.join(tempfile.gettempdir(), "abletonosc-song-structure.json"), "w")
            json.dump(song, fd)
            fd.close()
            self.logger.warning("Exported song structure to directory %s" % tempfile.gettempdir())
            return (1,)
        self.osc_server.add_handler("/live/song/export/structure", song_export_structure)

        #--------------------------------------------------------------------------------
        # Callbacks for Song: Scene properties
        #--------------------------------------------------------------------------------
        self.osc_server.add_handler("/live/song/get/num_scenes", lambda _: (len(self.song.scenes),))

        def song_get_scene_names(params):
            if len(params) == 0:
                scene_index_min, scene_index_max = 0, len(self.song.scenes)
            else:
                scene_index_min, scene_index_max = params
            return tuple(self.song.scenes[index].name for index in range(scene_index_min, scene_index_max))
        self.osc_server.add_handler("/live/song/get/scenes/name", song_get_scene_names)

        #--------------------------------------------------------------------------------
        # Callbacks for Song: Cue point properties
        #--------------------------------------------------------------------------------
        def song_get_cue_points(song, _):
            cue_points = song.cue_points
            cue_point_pairs = [(cue_point.name, cue_point.time) for cue_point in cue_points]
            return tuple(element for pair in cue_point_pairs for element in pair)
        self.osc_server.add_handler("/live/song/get/cue_points", partial(song_get_cue_points, self.song))

        def song_jump_to_cue_point(song, params: Tuple[Any] = ()):
            cue_point_index = params[0]
            if isinstance(cue_point_index, str):
                for cue_point in song.cue_points:
                    if cue_point.name == cue_point_index:
                        cue_point.jump()
            elif isinstance(cue_point_index, int):
                cue_point = song.cue_points[cue_point_index]
                cue_point.jump()
        self.osc_server.add_handler("/live/song/cue_point/jump", partial(song_jump_to_cue_point, self.song))

        #--------------------------------------------------------------------------------
        # Song: move_device(device, target_track, position)
        # Moves a device from one track to another.
        # Args: source_track_idx, device_idx, dest_track_idx, dest_position
        #--------------------------------------------------------------------------------
        def song_move_device(params):
            src_track_idx = int(params[0])
            device_idx = int(params[1])
            dst_track_idx = int(params[2])
            dst_position = int(params[3])
            device = self.song.tracks[src_track_idx].devices[device_idx]
            dst_track = self.song.tracks[dst_track_idx]
            self.song.move_device(device, dst_track, dst_position)
            self.logger.info("Moved device %d from track %d to track %d position %d" %
                             (device_idx, src_track_idx, dst_track_idx, dst_position))
        self.osc_server.add_handler("/live/song/move_device", song_move_device)

        #--------------------------------------------------------------------------------
        # Browser: load item from user library by path
        # Args: path segments as a single slash-separated string, e.g.
        #   "Audio Effects/Audio Effect Rack/_routing_template.adg"
        # Optionally prefix with track_index to select target track first.
        # Format: /live/browser/load_user_library_item track_index(int) path(str)
        #    or:  /live/browser/load_user_library_item path(str)
        #--------------------------------------------------------------------------------
        def browser_load_user_library_item(params):
            app = Live.Application.get_application()
            browser = app.browser

            if len(params) >= 2 and isinstance(params[0], (int, float)):
                track_index = int(params[0])
                path_str = str(params[1])
                _select_track_unfolded(self.song, track_index)
            else:
                path_str = str(params[0])

            segments = [s for s in path_str.split("/") if s]
            current = browser.user_library

            for segment in segments:
                found = False
                for child in current.iter_children:
                    if child.name == segment:
                        current = child
                        found = True
                        break
                if not found:
                    self.logger.error("Browser: could not find '%s' in '%s'" % (segment, path_str))
                    return ("error", "not_found", segment)

            if not current.is_loadable:
                self.logger.error("Browser: item '%s' is not loadable" % path_str)
                return ("error", "not_loadable", path_str)

            self.logger.info("Browser: loading '%s'" % current.name)
            self.manager.schedule_message(1, partial(browser.load_item, current))
            return ("ok", current.name)

        self.osc_server.add_handler("/live/browser/load_user_library_item", browser_load_user_library_item)

        #--------------------------------------------------------------------------------
        # Browser: load a built-in audio effect onto a track
        # Navigates browser.audio_effects instead of browser.user_library.
        # Format: /live/browser/load_audio_effect track_index(int) path(str)
        #    or:  /live/browser/load_audio_effect path(str)
        # Path example: "Utility"
        #--------------------------------------------------------------------------------
        def browser_load_audio_effect(params):
            app = Live.Application.get_application()
            browser = app.browser

            if len(params) >= 2 and isinstance(params[0], (int, float)):
                track_index = int(params[0])
                path_str = str(params[1])
                _select_track_unfolded(self.song, track_index)
            else:
                path_str = str(params[0])

            segments = [s for s in path_str.split("/") if s]
            current = browser.audio_effects

            for segment in segments:
                found = False
                for child in current.iter_children:
                    if child.name == segment:
                        current = child
                        found = True
                        break
                if not found:
                    self.logger.error("Browser: could not find '%s' in audio_effects path '%s'" % (segment, path_str))
                    return ("error", "not_found", segment)

            if not current.is_loadable:
                self.logger.error("Browser: audio effect '%s' is not loadable" % path_str)
                return ("error", "not_loadable", path_str)

            self.logger.info("Browser: loading audio effect '%s'" % current.name)
            self.manager.schedule_message(1, partial(browser.load_item, current))
            return ("ok", current.name)

        self.osc_server.add_handler("/live/browser/load_audio_effect", browser_load_audio_effect)

        #--------------------------------------------------------------------------------
        # Browser: list items under browser.audio_effects
        # Format: /live/browser/list_audio_effects [path(str)]
        #--------------------------------------------------------------------------------
        def browser_list_audio_effects(params):
            app = Live.Application.get_application()
            browser = app.browser
            path_str = str(params[0]) if params else ""

            current = browser.audio_effects
            if path_str:
                segments = [s for s in path_str.split("/") if s]
                for segment in segments:
                    found = False
                    for child in current.iter_children:
                        if child.name == segment:
                            current = child
                            found = True
                            break
                    if not found:
                        return ("error", "not_found", segment)

            names = []
            for child in current.iter_children:
                names.append(child.name)
            return tuple(names)

        self.osc_server.add_handler("/live/browser/list_audio_effects", browser_list_audio_effects)

        #--------------------------------------------------------------------------------
        # Browser: generic navigation + loading for any browser root
        # Roots: plugins, instruments, audio_effects, midi_effects, drums,
        #        sounds, packs, samples, clips, max_for_live
        # Format: /live/browser/list root(str) [path(str)]
        #         /live/browser/load root(str) track_index(int) path(str)
        #--------------------------------------------------------------------------------
        def _browser_navigate(browser, root_name, path_str):
            root = getattr(browser, root_name, None)
            if root is None:
                return None, ("error", "unknown_root", root_name)
            current = root
            if path_str:
                segments = [s for s in path_str.split("/") if s]
                for segment in segments:
                    found = False
                    for child in current.iter_children:
                        if child.name == segment:
                            current = child
                            found = True
                            break
                    if not found:
                        return None, ("error", "not_found", segment)
            return current, None

        def browser_list(params):
            app = Live.Application.get_application()
            browser = app.browser
            root_name = str(params[0])
            path_str = str(params[1]) if len(params) > 1 else ""
            node, err = _browser_navigate(browser, root_name, path_str)
            if err:
                return err
            names = []
            for child in node.iter_children:
                names.append(child.name)
            return tuple(names)

        def browser_load(params):
            app = Live.Application.get_application()
            browser = app.browser
            root_name = str(params[0])
            track_index = int(params[1])
            path_str = str(params[2])
            _select_track_unfolded(self.song, track_index)
            node, err = _browser_navigate(browser, root_name, path_str)
            if err:
                return err
            if not node.is_loadable:
                return ("error", "not_loadable", path_str)
            self.logger.info("Browser: loading '%s' from %s" % (node.name, root_name))
            self.manager.schedule_message(1, partial(browser.load_item, node))
            return ("ok", node.name)

        self.osc_server.add_handler("/live/browser/list", browser_list)
        self.osc_server.add_handler("/live/browser/load", browser_load)

        #--------------------------------------------------------------------------------
        # Browser: list items at a path in user library
        # Returns names of children at the given path.
        # Format: /live/browser/list_user_library path(str)
        #--------------------------------------------------------------------------------
        def browser_list_user_library(params):
            app = Live.Application.get_application()
            browser = app.browser
            path_str = str(params[0]) if params else ""

            current = browser.user_library
            if path_str:
                segments = [s for s in path_str.split("/") if s]
                for segment in segments:
                    found = False
                    for child in current.iter_children:
                        if child.name == segment:
                            current = child
                            found = True
                            break
                    if not found:
                        return ("error", "not_found", segment)

            names = []
            for child in current.iter_children:
                names.append(child.name)
            return tuple(names)

        self.osc_server.add_handler("/live/browser/list_user_library", browser_list_user_library)

        self.osc_server.add_handler("/live/song/cue_point/add_or_delete", partial(self._call_method, self.song, "set_or_delete_cue"))
        def song_cue_point_set_name(song, params: Tuple[Any] = ()):
            cue_point_index = params[0]
            new_name = params[1]
            cue_point = song.cue_points[cue_point_index]
            cue_point.name = new_name
        self.osc_server.add_handler("/live/song/cue_point/set/name", partial(song_cue_point_set_name, self.song))

        #--------------------------------------------------------------------------------
        # Listener for /live/song/get/beat
        #--------------------------------------------------------------------------------
        self.last_song_time = -1.0
        
        def stop_beat_listener(params: Tuple[Any] = ()):
            try:
                self.song.remove_current_song_time_listener(self.current_song_time_changed)
                self.logger.info("Removing beat listener")
            except:
                pass

        def start_beat_listener(params: Tuple[Any] = ()):
            stop_beat_listener()
            self.logger.info("Adding beat listener")
            self.song.add_current_song_time_listener(self.current_song_time_changed)

        self.osc_server.add_handler("/live/song/start_listen/beat", start_beat_listener)
        self.osc_server.add_handler("/live/song/stop_listen/beat", stop_beat_listener)

        def memory_pressure_relief(params: Tuple[Any] = ()):
            try:
                module_dir = os.path.dirname(os.path.realpath(__file__))
                if module_dir not in sys.path:
                    sys.path.insert(0, module_dir)
                parent_dir = os.path.dirname(module_dir)
                if parent_dir not in sys.path:
                    sys.path.insert(0, parent_dir)
                import _memory_relief
                freed = _memory_relief.pressure_relief()
                self.logger.info("malloc_zone_pressure_relief freed %d bytes" % freed)
                return (int(freed),)
            except ImportError as e:
                self.logger.error("_memory_relief import failed: %s (path: %s)" % (str(e), sys.path[:5]))
                return (-2,)
            except Exception as e:
                self.logger.error("pressure_relief failed: %s" % str(e))
                return (-1,)

        self.osc_server.add_handler("/live/memory/pressure_relief", memory_pressure_relief)

    def current_song_time_changed(self):
        #--------------------------------------------------------------------------------
        # If song has rewound or skipped to next beat, sent a /live/beat message
        #--------------------------------------------------------------------------------
        if (self.song.current_song_time < self.last_song_time) or \
                (int(self.song.current_song_time) > int(self.last_song_time)):
            self.osc_server.send("/live/song/get/beat", (int(self.song.current_song_time),))
        self.last_song_time = self.song.current_song_time

    def clear_api(self):
        super().clear_api()
        try:
            self.song.remove_current_song_time_listener(self.current_song_time_changed)
        except:
            pass
