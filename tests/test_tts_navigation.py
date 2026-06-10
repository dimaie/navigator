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
# TTSManager.set_route_directions
# ---------------------------------------------------------------------------

class TestSetRouteDirections:
    """Tests for TTSManager.set_route_directions."""

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
        # Place a step at route index 2, GPS is at route[9] → all steps behind GPS
        manager.navigation_steps = [
            {"junc_idx": 2, "speakable_action": "turn left", "original_text": "- turn left at (0,0)"}
        ]
        gps = QPointF(900.0, 0.0)  # beyond all steps
        manager.update_navigation(gps, route, self._make_profile())
        manager.speak.assert_not_called()

    def test_first_call_sets_baseline_no_audio(self):
        """First update call (prev_step_idx==-1) sets baseline but does not trigger audio."""
        manager, _ = _make_tts_manager()
        manager.speak = MagicMock()
        manager.play_beep = MagicMock()
        route = [QPointF(i * 100.0, 0.0) for i in range(10)]
        manager.navigation_steps = [
            {"junc_idx": 8, "speakable_action": "turn left", "original_text": "- turn left at (0,0)"}
        ]
        # prev_step_idx == -1 on fresh manager
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
        manager.navigation_steps = [
            {"junc_idx": 15, "speakable_action": "turn left", "original_text": "- turn left at (0,0)"}
        ]

        # First call to set baseline (step changes → baseline set)
        gps_far = QPointF(0.0, 0.0)  # ~1500 m from junction
        manager.update_navigation(gps_far, route, self._make_profile(), tts_mode="disabled")
        manager.speak.reset_mock()
        manager.play_beep.reset_mock()

        # Second call: prev_step_idx already set, baseline set; simulate crossing 200m threshold
        # Set prev_dist_to_turn manually to above threshold, dist will be < threshold
        manager.prev_dist_to_turn = 250.0  # above 200m
        manager.prev_step_idx = 0
        gps_close = QPointF(1300.0, 0.0)  # ~200m from junction at 1500
        manager.update_navigation(gps_close, route, self._make_profile(), tts_mode="disabled")
        manager.speak.assert_not_called()
        manager.play_beep.assert_not_called()

    def test_after_next_action_set_when_second_step_exists(self):
        """after_next_action is populated when a second step exists beyond next_step."""
        manager, _ = _make_tts_manager()
        route = [QPointF(i * 100.0, 0.0) for i in range(20)]
        manager.navigation_steps = [
            {"junc_idx": 5,  "speakable_action": "turn left",  "original_text": "..."},
            {"junc_idx": 12, "speakable_action": "turn right", "original_text": "..."},
        ]
        gps = QPointF(0.0, 0.0)
        manager.update_navigation(gps, route, self._make_profile())
        # after_next_action should reference the second step
        assert manager.after_next_action is not None
        assert manager.after_next_action["speakable_action"] == "turn right"

    def test_threshold_cross_triggers_beep_in_sound_only_mode(self):
        """When a distance threshold is crossed in 'sound_only' mode, play_beep is called."""
        manager, _ = _make_tts_manager()
        manager.play_beep = MagicMock()
        manager.speak = MagicMock()

        route = [QPointF(i * 100.0, 0.0) for i in range(20)]
        manager.navigation_steps = [
            {"junc_idx": 10, "speakable_action": "turn left", "original_text": "..."},
        ]

        # Simulate: baseline already set, prev_step_idx correct
        manager.prev_step_idx = 0
        manager.prev_dist_to_turn = 250.0   # was 250 m, now below 200 m threshold

        # GPS at position 800 m (junction at 1000 m → dist ~200 m)
        gps = QPointF(800.0, 0.0)
        manager.update_navigation(gps, route, self._make_profile(), tts_mode="sound_only")
        # Beep should have been called (threshold 200 m crossed)
        manager.play_beep.assert_called()
