# tests/test_tts_navigation.py
"""
Tests for TTSManager and get_speakable_action in gps/speech.py.
All Qt audio/TTS objects are replaced with MagicMocks so no audio hardware is needed.
"""

import sys
import math
import importlib
import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from PySide6.QtCore import QPointF

from utils import project_mercator


# ---------------------------------------------------------------------------
# Module-level patching helper
# ---------------------------------------------------------------------------

def _make_tts_manager():
    """
    Imports and instantiates TTSManager with all Qt audio classes mocked.
    Returns (manager, module) so callers can access module-level functions too.
    """
    # Force reimport so patches take effect even if module was already imported
    if "gps.speech" in sys.modules:
        del sys.modules["gps.speech"]

    with patch("gps.speech.QTextToSpeech") as mock_tts_cls, \
         patch("gps.speech.QMediaPlayer") as mock_player_cls, \
         patch("gps.speech.QSoundEffect") as mock_sfx_cls, \
         patch("gps.speech.QAudioOutput") as mock_audio_cls, \
         patch("gps.speech.generate_beep_wav"):

        from gps.speech import TTSManager
        import gps.speech as speech_mod

        # Make tts.state() return a non-Speaking sentinel
        from PySide6.QtTextToSpeech import QTextToSpeech as RealQTTS
        mock_tts_instance = mock_tts_cls.return_value
        mock_tts_instance.state.return_value = RealQTTS.State.Ready
        mock_player_cls.return_value = MagicMock()
        mock_sfx_cls.return_value = MagicMock()
        mock_audio_cls.return_value = MagicMock()

        manager = TTSManager()
        return manager, speech_mod


# ---------------------------------------------------------------------------
# get_speakable_action
# ---------------------------------------------------------------------------

class TestGetSpeakableAction:
    """Tests for the module-level get_speakable_action function."""

    def _gspa(self, text):
        """Helper: import and call get_speakable_action with patches active."""
        if "gps.speech" in sys.modules:
            del sys.modules["gps.speech"]
        with patch("gps.speech.QTextToSpeech"), \
             patch("gps.speech.QMediaPlayer"), \
             patch("gps.speech.QSoundEffect"), \
             patch("gps.speech.QAudioOutput"), \
             patch("gps.speech.generate_beep_wav"):
            from gps.speech import get_speakable_action
            return get_speakable_action(text)

    def test_strips_bullet_and_coordinate(self):
        """'- turn left at (53.349, -6.260)' → 'turn left'."""
        result = self._gspa("- turn left at (53.34900, -6.26000)")
        assert result == "turn left"

    def test_longer_instruction_strips_coordinate(self):
        result = self._gspa(
            "- Drive on Main Street for 500 m and turn right at (53.0, -6.0)"
        )
        assert result == "Drive on Main Street for 500 m and turn right"

    def test_roundabout_instruction_strips_coordinate(self):
        result = self._gspa(
            "- At the roundabout, take the 2nd exit onto N11 at (53.1, -6.1)"
        )
        assert result == "At the roundabout, take the 2nd exit onto N11"

    def test_no_coordinate_only_strips_bullet(self):
        """No coordinate → returns text with only leading '- ' stripped."""
        result = self._gspa("- Proceed to destination")
        assert result == "Proceed to destination"

    def test_no_dash_prefix_returns_as_is(self):
        """Text with no leading dash → returns stripped text."""
        result = self._gspa("continue straight")
        assert result == "continue straight"


# ---------------------------------------------------------------------------
# TTSManager.set_route_directions  (legacy shim)
# ---------------------------------------------------------------------------

class TestSetRouteDirections:
    """Tests for TTSManager.set_route_directions (backward-compat shim)."""

    def test_one_instruction_with_coordinate_adds_navigation_step(self):
        manager, _ = _make_tts_manager()
        lat, lon = 53.349, -6.260
        directions = [f"- turn left at ({lat:.5f}, {lon:.5f})"]
        route_points = [QPointF(*project_mercator(lat, lon))]
        manager.set_route_directions(directions, route_points)
        assert len(manager.navigation_steps) == 1

    def test_step_junc_pt_has_correct_mercator_coords(self):
        manager, _ = _make_tts_manager()
        lat, lon = 53.349, -6.260
        mx, my = project_mercator(lat, lon)
        directions = [f"- turn left at ({lat:.5f}, {lon:.5f})"]
        route_points = [QPointF(mx, my)]
        manager.set_route_directions(directions, route_points)
        step = manager.navigation_steps[0]
        assert abs(step["junc_pt"].x() - mx) < 1.0
        assert abs(step["junc_pt"].y() - my) < 1.0

    def test_step_junc_idx_points_to_nearest_route_point(self):
        manager, _ = _make_tts_manager()
        lat, lon = 53.349, -6.260
        mx, my = project_mercator(lat, lon)
        route_points = [QPointF(mx - 500, my), QPointF(mx, my), QPointF(mx + 500, my)]
        directions = [f"- turn left at ({lat:.5f}, {lon:.5f})"]
        manager.set_route_directions(directions, route_points)
        assert manager.navigation_steps[0]["junc_idx"] == 1

    def test_summary_line_without_coordinate_not_added(self):
        """Lines without (lat, lon) are ignored (e.g. the summary '\nTotal...' line)."""
        manager, _ = _make_tts_manager()
        directions = [
            "- turn left at (53.34900, -6.26000)",
            "\nTotal route length: 1.5 km (Estimated travel time: 2m)",
        ]
        lat, lon = 53.349, -6.260
        route_points = [QPointF(*project_mercator(lat, lon))]
        manager.set_route_directions(directions, route_points)
        assert len(manager.navigation_steps) == 1

    def test_calling_twice_resets_navigation_steps(self):
        """A second call to set_route_directions clears the previous steps."""
        manager, _ = _make_tts_manager()
        lat, lon = 53.349, -6.260
        directions = [f"- turn left at ({lat:.5f}, {lon:.5f})"]
        route_points = [QPointF(*project_mercator(lat, lon))]
        manager.set_route_directions(directions, route_points)
        manager.set_route_directions(directions, route_points)
        assert len(manager.navigation_steps) == 1  # not 2

    def test_shim_sets_speakable_true_for_turn_instructions(self):
        """The legacy shim infers speakable=True for turn/bear/roundabout instructions."""
        manager, _ = _make_tts_manager()
        lat, lon = 53.349, -6.260
        directions = [f"- turn left at ({lat:.5f}, {lon:.5f})"]
        route_points = [QPointF(*project_mercator(lat, lon))]
        manager.set_route_directions(directions, route_points)
        assert manager.navigation_steps[0]["speakable"] is True

    def test_shim_sets_speakable_false_for_continue_instructions(self):
        """The legacy shim infers speakable=False for continue-straight instructions."""
        manager, _ = _make_tts_manager()
        lat, lon = 53.349, -6.260
        directions = [f"- Drive on Main Street for 200 m and continue onto N11 at ({lat:.5f}, {lon:.5f})"]
        route_points = [QPointF(*project_mercator(lat, lon))]
        manager.set_route_directions(directions, route_points)
        assert manager.navigation_steps[0]["speakable"] is False


# ---------------------------------------------------------------------------
# TTSManager.set_route  (structured-data path)
# ---------------------------------------------------------------------------

def _make_mock_route(steps, route_points):
    """Builds a minimal ComputedRoute-like object with .steps and .points."""
    class _Route:
        pass
    r = _Route()
    r.steps = steps
    r.points = route_points
    return r


def _make_step(junc_pt, speakable=True, step_type="turn", angle=90.0,
               junction_choices=2, exit_number=0, total_exits=0,
               driving_side="left", speakable_action="turn left"):
    return {
        "type":             step_type,
        "angle":            angle,
        "junction_choices": junction_choices,
        "exit_number":      exit_number,
        "total_exits":      total_exits,
        "junc_pt":          junc_pt,
        "speakable":        speakable,
        "speakable_action": speakable_action,
        "driving_side":     driving_side,
    }


class TestSetRoute:
    """Tests for TTSManager.set_route (structured ComputedRoute path)."""

    def test_set_route_loads_all_steps(self):
        manager, _ = _make_tts_manager()
        pts = [QPointF(float(i * 100), 0.0) for i in range(5)]
        steps = [
            _make_step(pts[2], speakable_action="turn left"),
            _make_step(pts[4], speakable_action="turn right"),
        ]
        route = _make_mock_route(steps, pts)
        manager.set_route(route)
        assert len(manager.navigation_steps) == 2

    def test_set_route_snaps_junc_idx_correctly(self):
        manager, _ = _make_tts_manager()
        pts = [QPointF(float(i * 100), 0.0) for i in range(5)]
        step = _make_step(pts[3])
        route = _make_mock_route([step], pts)
        manager.set_route(route)
        assert manager.navigation_steps[0]["junc_idx"] == 3

    def test_set_route_preserves_all_metadata(self):
        """All step dict keys survive the set_route snapping process."""
        manager, _ = _make_tts_manager()
        pts = [QPointF(0.0, 0.0), QPointF(100.0, 0.0)]
        step = _make_step(pts[1], step_type="roundabout", exit_number=2,
                          total_exits=4, speakable=True)
        route = _make_mock_route([step], pts)
        manager.set_route(route)
        ns = manager.navigation_steps[0]
        assert ns["type"] == "roundabout"
        assert ns["exit_number"] == 2
        assert ns["total_exits"] == 4
        assert ns["speakable"] is True

    def test_calling_set_route_twice_resets_steps(self):
        manager, _ = _make_tts_manager()
        pts = [QPointF(0.0, 0.0), QPointF(100.0, 0.0)]
        step = _make_step(pts[1])
        route = _make_mock_route([step], pts)
        manager.set_route(route)
        manager.set_route(route)
        assert len(manager.navigation_steps) == 1  # not 2


# ---------------------------------------------------------------------------
# TTSManager.find_closest_route_index
# ---------------------------------------------------------------------------

class TestFindClosestRouteIndex:
    """Tests for TTSManager.find_closest_route_index."""

    def test_gps_exactly_on_route_point_3(self):
        """GPS exactly on route[2] → segment index 2 (segment 2→3)."""
        manager, _ = _make_tts_manager()
        route = [QPointF(float(i * 100), 0.0) for i in range(5)]
        gps = QPointF(200.0, 0.0)  # exactly route[2]
        idx, proj = manager.find_closest_route_index(route, gps)
        # Segment 1→2 ends at x=200, or segment 2→3 starts at x=200.
        # Both are valid; the important thing is the projection ≈ (200, 0).
        assert abs(proj.x() - 200.0) < 1.0
        assert abs(proj.y()) < 1.0

    def test_gps_off_midpoint_returns_correct_segment(self):
        """GPS 10 m off midpoint of segment 1→2 → segment index 1."""
        manager, _ = _make_tts_manager()
        route = [QPointF(0.0, 0.0), QPointF(100.0, 0.0), QPointF(200.0, 0.0)]
        gps = QPointF(50.0, 10.0)  # above midpoint of seg 0→1
        idx, proj = manager.find_closest_route_index(route, gps)
        # Projection should be near x=50, y=0
        assert abs(proj.x() - 50.0) < 1.0
        assert abs(proj.y()) < 1.0
        assert idx == 0


# ---------------------------------------------------------------------------
# TTSManager.update_navigation — state machine
# ---------------------------------------------------------------------------

class TestUpdateNavigation:
    """Tests for TTSManager.update_navigation."""

    def _make_profile(self, tts_distances=None):
        return {
            "tts_distances": tts_distances or [500, 200, 50],
            "driving_side": "left",
        }

    def _inject_step(self, manager, junc_idx, speakable=True,
                     step_type="turn", speakable_action="turn left"):
        """Directly plants a pre-built navigation step into manager."""
        manager.navigation_steps = [{
            "junc_idx":        junc_idx,
            "speakable":       speakable,
            "speakable_action": speakable_action,
            "type":            step_type,
            "angle":           90.0,
            "exit_number":     0,
            "total_exits":     0,
            "driving_side":    "left",
            "junc_pt":         QPointF(float(junc_idx * 100), 0.0),
        }]

    def test_no_route_points_returns_immediately(self):
        manager, _ = _make_tts_manager()
        manager.speak = MagicMock()
        manager.play_beep = MagicMock()
        manager.update_navigation(QPointF(0, 0), [], self._make_profile())
        manager.speak.assert_not_called()
        manager.play_beep.assert_not_called()

    def test_no_navigation_steps_returns_immediately(self):
        manager, _ = _make_tts_manager()
        manager.speak = MagicMock()
        route = [QPointF(i * 100.0, 0.0) for i in range(5)]
        manager.navigation_steps = []
        manager.update_navigation(QPointF(0, 0), route, self._make_profile())
        manager.speak.assert_not_called()

    def test_gps_ahead_of_all_steps_returns_immediately(self):
        """When no step has junc_idx > gps_idx, next_step is None → return early."""
        manager, _ = _make_tts_manager()
        manager.speak = MagicMock()
        route = [QPointF(i * 100.0, 0.0) for i in range(10)]
        self._inject_step(manager, junc_idx=2)
        gps = QPointF(900.0, 0.0)  # beyond all steps
        manager.update_navigation(gps, route, self._make_profile())
        manager.speak.assert_not_called()

    def test_first_call_sets_baseline_no_audio(self):
        """First update call (prev_step_idx==-1) sets baseline but does not trigger audio."""
        manager, _ = _make_tts_manager()
        manager.speak = MagicMock()
        manager.play_beep = MagicMock()
        route = [QPointF(i * 100.0, 0.0) for i in range(10)]
        self._inject_step(manager, junc_idx=8)
        assert manager.prev_step_idx == -1
        gps = QPointF(0.0, 0.0)
        manager.update_navigation(gps, route, self._make_profile())
        manager.speak.assert_not_called()
        manager.play_beep.assert_not_called()

    def test_disabled_tts_mode_no_audio_on_threshold_cross(self):
        """Even when distance threshold crossed, tts_mode='disabled' suppresses audio."""
        manager, _ = _make_tts_manager()
        manager.speak = MagicMock()
        manager.play_beep = MagicMock()

        route = [QPointF(i * 100.0, 0.0) for i in range(20)]
        self._inject_step(manager, junc_idx=15)

        # First call to set baseline (step changes → baseline set)
        gps_far = QPointF(0.0, 0.0)  # ~1500 m from junction
        manager.update_navigation(gps_far, route, self._make_profile(), tts_mode="disabled")
        manager.speak.reset_mock()
        manager.play_beep.reset_mock()

        # Second call: simulate crossing 200m threshold
        manager.prev_dist_to_turn = 250.0
        manager.prev_step_idx = 0
        gps_close = QPointF(1300.0, 0.0)
        manager.update_navigation(gps_close, route, self._make_profile(), tts_mode="disabled")
        manager.speak.assert_not_called()
        manager.play_beep.assert_not_called()

    def test_after_next_action_set_when_second_step_exists(self):
        """after_next_action is populated when a second step exists beyond next_step."""
        manager, _ = _make_tts_manager()
        route = [QPointF(i * 100.0, 0.0) for i in range(20)]
        manager.navigation_steps = [
            {"junc_idx": 5,  "speakable": True, "speakable_action": "turn left",
             "type": "turn", "angle": 90.0, "exit_number": 0, "total_exits": 0,
             "driving_side": "left", "junc_pt": QPointF(500.0, 0.0)},
            {"junc_idx": 12, "speakable": True, "speakable_action": "turn right",
             "type": "turn", "angle": -90.0, "exit_number": 0, "total_exits": 0,
             "driving_side": "left", "junc_pt": QPointF(1200.0, 0.0)},
        ]
        gps = QPointF(0.0, 0.0)
        manager.update_navigation(gps, route, self._make_profile())
        assert manager.after_next_action is not None
        assert manager.after_next_action["speakable_action"] == "turn right"

    def test_threshold_cross_triggers_beep_in_sound_only_mode(self):
        """When a distance threshold is crossed in 'sound_only' mode, play_beep is called."""
        manager, _ = _make_tts_manager()
        manager.play_beep = MagicMock()
        manager.speak = MagicMock()

        route = [QPointF(i * 100.0, 0.0) for i in range(20)]
        self._inject_step(manager, junc_idx=10)

        manager.prev_step_idx = 0
        manager.prev_dist_to_turn = 250.0  # was 250 m, now below 200 m threshold

        gps = QPointF(800.0, 0.0)
        manager.update_navigation(gps, route, self._make_profile(), tts_mode="sound_only")
        manager.play_beep.assert_called()

    def test_non_speakable_step_suppresses_audio_on_threshold_cross(self):
        """Steps with speakable=False must never trigger TTS or beep."""
        manager, _ = _make_tts_manager()
        manager.play_beep = MagicMock()
        manager.speak = MagicMock()

        route = [QPointF(i * 100.0, 0.0) for i in range(20)]
        self._inject_step(manager, junc_idx=10, speakable=False,
                          speakable_action="Drive on Main St for 500 m and continue onto N11")

        manager.prev_step_idx = 0
        manager.prev_dist_to_turn = 250.0

        gps = QPointF(800.0, 0.0)
        manager.update_navigation(gps, route, self._make_profile(), tts_mode="sound_only")
        manager.play_beep.assert_not_called()
        manager.speak.assert_not_called()

    def test_forced_turn_single_choice_suppresses_audio(self):
        """speakable=False for forced turns (junction_choices==1) must not trigger TTS."""
        manager, _ = _make_tts_manager()
        manager.play_beep = MagicMock()
        manager.speak = MagicMock()

        route = [QPointF(i * 100.0, 0.0) for i in range(20)]
        # junction_choices=1 means speakable=False at route-build time
        manager.navigation_steps = [{
            "junc_idx":         10,
            "speakable":        False,   # set by router because junction_choices == 1
            "speakable_action": "Drive on Road for 500 m and bear right to Lane",
            "type":             "turn",
            "angle":            -30.0,
            "junction_choices": 1,
            "exit_number":      0,
            "total_exits":      0,
            "driving_side":     "left",
            "junc_pt":          QPointF(1000.0, 0.0),
        }]

        manager.prev_step_idx = 0
        manager.prev_dist_to_turn = 250.0

        gps = QPointF(800.0, 0.0)
        manager.update_navigation(gps, route, self._make_profile(), tts_mode="full")
        manager.play_beep.assert_not_called()
        manager.speak.assert_not_called()

    def test_only_one_notification_per_junction(self):
        """Even if multiple thresholds are crossed in separate calls, TTS fires only once."""
        manager, _ = _make_tts_manager()
        manager.play_beep = MagicMock()
        manager.speak = MagicMock()

        route = [QPointF(i * 100.0, 0.0) for i in range(20)]
        self._inject_step(manager, junc_idx=15)
        manager.prev_step_idx = 0

        # First crossing: 500 m → beep/speak fired
        manager.prev_dist_to_turn = 600.0
        manager.update_navigation(QPointF(1000.0, 0.0), route,
                                  self._make_profile(), tts_mode="sound_only")
        assert manager.play_beep.call_count == 1

        # Second crossing: 200 m → must NOT fire again
        manager.play_beep.reset_mock()
        manager.prev_dist_to_turn = 250.0
        manager.update_navigation(QPointF(1300.0, 0.0), route,
                                  self._make_profile(), tts_mode="sound_only")
        manager.play_beep.assert_not_called()

        # Third crossing: 50 m → still must NOT fire
        manager.play_beep.reset_mock()
        manager.prev_dist_to_turn = 80.0
        manager.update_navigation(QPointF(1450.0, 0.0), route,
                                  self._make_profile(), tts_mode="sound_only")
        manager.play_beep.assert_not_called()

    def test_roundabout_pre_announced_at_approach_distance(self):
        """
        When next_step is a non-speakable 'continue onto roundabout' step
        but after_next_action is a speakable roundabout exit, TTS must
        fire using the APPROACH distance (to the entry junction), not the
        tiny distance from inside the roundabout to the exit.

        This reproduces the Station Grove -> Athlone route bug where the
        roundabout entry and exit are only ~16 m apart.
        """
        manager, _ = _make_tts_manager()
        manager.play_beep = MagicMock()
        manager.speak = MagicMock()

        # Route: 20 points at 100 m spacing.
        # Step 0 (idx=10): continue onto roundabout - speakable=False, entry at x=1000
        # Step 1 (idx=11): roundabout exit       - speakable=True,  exit  at x=1100
        route = [QPointF(i * 100.0, 0.0) for i in range(20)]
        manager.navigation_steps = [
            {
                "junc_idx":         10,
                "speakable":        False,
                "speakable_action": "Drive on unnamed road for 75 m and continue onto roundabout",
                "type":             "continue",
                "angle":            0.0,
                "exit_number":      0,
                "total_exits":      0,
                "driving_side":     "left",
                "junc_pt":          QPointF(1000.0, 0.0),
            },
            {
                "junc_idx":         11,
                "speakable":        True,
                "speakable_action": "At the roundabout, take the 2nd exit onto Station Road",
                "type":             "roundabout",
                "angle":            0.0,
                "exit_number":      2,
                "total_exits":      4,
                "driving_side":     "left",
                "junc_pt":          QPointF(1100.0, 0.0),
            },
        ]

        # Establish baseline: GPS at x=4 (segment 0->1), ~996 m from entry (idx=10, x=1000)
        # prev_dist_to_turn set to 600 m (above the 500 m threshold).
        # On this call GPS is at x=501 → dist to entry ≈ 499 m ≤ 500, crossing the threshold.
        manager.prev_step_idx = 0
        manager.prev_dist_to_turn = 600.0

        # GPS at x=501 → snaps to segment 4->5, distance to idx=10 is ~499 m
        gps = QPointF(501.0, 0.0)
        manager.update_navigation(gps, route, self._make_profile(), tts_mode="sound_only")
        assert manager.play_beep.call_count == 1, (
            "Expected beep for roundabout pre-announcement at 500 m threshold"
        )
        # Roundabout exit step must be marked spoken so it never fires again
        assert 1 in manager.spoken_steps


    def test_non_speakable_step_with_no_speakable_after_next_stays_silent(self):
        """
        A non-speakable step followed by another non-speakable step
        must never trigger any audio even when thresholds are crossed.
        """
        manager, _ = _make_tts_manager()
        manager.play_beep = MagicMock()
        manager.speak = MagicMock()

        route = [QPointF(i * 100.0, 0.0) for i in range(20)]
        manager.navigation_steps = [
            {
                "junc_idx":         10,
                "speakable":        False,
                "speakable_action": "Drive on A for 500 m and continue onto B",
                "type":             "continue",
                "angle":            0.0,
                "exit_number":      0,
                "total_exits":      0,
                "driving_side":     "left",
                "junc_pt":          QPointF(1000.0, 0.0),
            },
            {
                "junc_idx":         15,
                "speakable":        False,
                "speakable_action": "Drive on B for 300 m and continue onto C",
                "type":             "continue",
                "angle":            0.0,
                "exit_number":      0,
                "total_exits":      0,
                "driving_side":     "left",
                "junc_pt":          QPointF(1500.0, 0.0),
            },
        ]

        manager.prev_step_idx = 0
        manager.prev_dist_to_turn = 600.0

        gps = QPointF(100.0, 0.0)
        manager.update_navigation(gps, route, self._make_profile(), tts_mode="sound_only")
        manager.play_beep.assert_not_called()
        manager.speak.assert_not_called()
