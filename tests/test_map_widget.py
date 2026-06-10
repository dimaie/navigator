# tests/test_map_widget.py
"""
Widget-level behavioural tests for MapWidget.
These tests do NOT perform visual/pixel assertions — they test state and signal behaviour.
Requires pytest-qt (provides qtbot and a QApplication).
"""

import pytest
from unittest.mock import patch, MagicMock
from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import QMessageBox

from routing_worker import ComputedRoute


# ---------------------------------------------------------------------------
# Fixture: MapWidget pre-loaded with minimal data
# ---------------------------------------------------------------------------

@pytest.fixture
def widget(qtbot):
    """
    Instantiate MapWidget with minimal injected data so data_loaded=True
    and routing is enabled. No real SQLite DB is used.

    The render debounce timer is disconnected from trigger_background_render
    so it can never launch a MapRenderWorker against the :memory: DB — which
    would raise OperationalError and bleed Qt exceptions into subsequent tests.
    """
    from viewer import MapWidget

    w = MapWidget()
    qtbot.addWidget(w)

    # ── Isolate the render pipeline ──────────────────────────────────────────
    # start_interaction() arms a 150 ms QTimer that normally calls
    # trigger_background_render, which spawns MapRenderWorker and opens db_path.
    # With db_path=":memory:" and no 'ways' table this raises an
    # OperationalError inside the Qt thread, bleeding into later tests.
    # Disconnect the slot and wire a no-op instead.
    w.render_debounce_timer.timeout.disconnect(w.trigger_background_render)
    w.render_debounce_timer.timeout.connect(lambda: None)
    # ─────────────────────────────────────────────────────────────────────────

    w.resize(800, 600)

    # Inject minimal data to bypass the loader thread
    w.global_bbox = {
        "min_x": -1_000_000.0,
        "max_x":  1_000_000.0,
        "min_y": -1_000_000.0,
        "max_y":  1_000_000.0,
    }
    w.data_loaded = True
    w.has_routing = True
    w.db_path = ":memory:"

    yield w

    # Teardown: stop timer in case it was restarted during the test
    w.render_debounce_timer.stop()


def _make_route(distance_m=1500.0, duration_s=120.0, profile_name="Car"):
    """Helper to build a ComputedRoute for tests."""
    return ComputedRoute(
        points=[QPointF(0, 0), QPointF(1, 1)],
        distance_m=distance_m,
        duration_s=duration_s,
        directions=[
            f"- Drive 1.5 km at (53.00000, -6.00000)",
            "\nTotal route length: 1.5 km (Estimated travel time: 2m)",
        ],
        profile_name=profile_name,
    )


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

class TestMapWidgetInitialState:
    def test_current_route_is_none(self, widget):
        assert widget.current_route is None

    def test_route_start_is_none(self, widget):
        assert widget.route_start is None

    def test_route_end_is_none(self, widget):
        assert widget.route_end is None

    def test_navigation_state_is_inactive(self, widget):
        assert widget.navigation_state == "inactive"


# ---------------------------------------------------------------------------
# on_route_completed
# ---------------------------------------------------------------------------

class TestOnRouteCompleted:
    def test_stores_route(self, widget):
        route = _make_route()
        widget.on_route_completed(route, {}, {})
        assert widget.current_route is route

    def test_distance_m_preserved(self, widget):
        route = _make_route(distance_m=1500.0)
        widget.on_route_completed(route, {}, {})
        assert widget.current_route.distance_m == 1500.0

    def test_emits_route_completed_signal(self, widget, qtbot):
        route = _make_route()
        with qtbot.waitSignal(widget.route_completed_signal, timeout=1000):
            widget.on_route_completed(route, {}, {})

    def test_status_message_contains_distance_km(self, widget, qtbot):
        route = _make_route(distance_m=1500.0)
        with qtbot.waitSignal(widget.status_message, timeout=1000) as blocker:
            widget.on_route_completed(route, {}, {})
        assert "1.5 km" in blocker.args[0]

    def test_status_message_with_fallback_profile_mentions_fallback(self, widget, qtbot):
        """If route.profile_name != active_profile_name, 'fallback' appears in message."""
        widget.active_profile_name = "Car"
        route = _make_route(profile_name="Bicycle")
        with qtbot.waitSignal(widget.status_message, timeout=1000) as blocker:
            widget.on_route_completed(route, {}, {})
        assert "fallback" in blocker.args[0].lower()

    def test_status_message_no_fallback_when_same_profile(self, widget, qtbot):
        """If route.profile_name == active_profile_name, no 'fallback' in message."""
        widget.active_profile_name = "Car"
        route = _make_route(profile_name="Car")
        with qtbot.waitSignal(widget.status_message, timeout=1000) as blocker:
            widget.on_route_completed(route, {}, {})
        assert "fallback" not in blocker.args[0].lower()


# ---------------------------------------------------------------------------
# handle_map_click
# ---------------------------------------------------------------------------

class TestHandleMapClick:
    """Tests for the 3-state click cycle: set start → set end → reset."""

    def _setup_widget_viewport(self, widget):
        """Set up a predictable viewport for to_mercator math."""
        widget.center_x = 0.0
        widget.center_y = 0.0
        widget.scale = 0.001

    def test_first_click_sets_route_start(self, widget, qtbot):
        self._setup_widget_viewport(widget)
        with qtbot.waitSignal(widget.route_start_changed, timeout=1000):
            widget.handle_map_click(400, 300)  # center of 800×600 screen
        assert widget.route_start is not None

    def test_first_click_clears_current_route(self, widget, qtbot):
        self._setup_widget_viewport(widget)
        widget.current_route = _make_route()
        qtbot.waitSignal(widget.route_start_changed, timeout=1000,
                         raising=False)
        widget.handle_map_click(400, 300)
        assert widget.current_route is None

    def test_third_click_resets_start_and_clears_end(self, widget, qtbot):
        """Third click: route_start is set to new point, route_end → None."""
        self._setup_widget_viewport(widget)
        widget.route_start = QPointF(0, 0)
        widget.route_end = QPointF(1, 1)
        widget.handle_map_click(400, 300)
        assert widget.route_end is None
        assert widget.route_start is not None

    def test_handle_map_click_does_nothing_if_no_routing(self, widget):
        """If has_routing=False, clicks are ignored."""
        widget.has_routing = False
        before_start = widget.route_start
        widget.handle_map_click(400, 300)
        assert widget.route_start == before_start


# ---------------------------------------------------------------------------
# show_route_directions
# ---------------------------------------------------------------------------

class TestShowRouteDirections:
    def test_no_route_shows_info_dialog_without_crash(self, widget, qtbot):
        """With no current_route, QMessageBox.information should be called (no crash)."""
        widget.current_route = None
        with patch("viewer.QMessageBox.information") as mock_info:
            widget.show_route_directions()
            mock_info.assert_called_once()

    def test_with_route_and_directions_no_crash(self, widget, qtbot):
        """With a valid route, RouteDirectionsDialog is instantiated and exec'd."""
        widget.current_route = _make_route()
        with patch("viewer.RouteDirectionsDialog") as mock_dialog_cls:
            mock_dialog = MagicMock()
            mock_dialog_cls.return_value = mock_dialog
            widget.show_route_directions()
            mock_dialog.exec.assert_called_once()
