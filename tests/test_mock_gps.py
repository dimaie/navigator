# tests/test_mock_gps.py
"""
Tests for MockGPSDevice in gps/mock_gps.py.
Requires a QApplication (provided by pytest-qt's qapp fixture).
"""

import math
import pytest
from unittest.mock import MagicMock, patch
from PySide6.QtCore import QPointF, QTimer

from utils import project_mercator, inverse_mercator
from gps.mock_gps import MockGPSDevice


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mercator_pts(*lat_lons):
    """Return a list of QPointF from [(lat,lon), ...] pairs."""
    return [QPointF(*project_mercator(lat, lon)) for lat, lon in lat_lons]


# ---------------------------------------------------------------------------
# update_route tests
# ---------------------------------------------------------------------------

class TestMockGPSDeviceUpdateRoute:
    """Tests for MockGPSDevice.update_route."""

    def test_empty_list_stores_empty_route_no_signal(self, qapp, qtbot):
        """Empty list → route_points=[], no position_updated emitted."""
        device = MockGPSDevice()
        signals_received = []
        device.position_updated.connect(lambda *a: signals_received.append(a))
        device.update_route([])
        assert device.route_points == []
        assert signals_received == []

    def test_two_point_route_stored_and_state_reset(self, qapp):
        """Two-point route → stored, current_segment_idx=0, distance_along_segment=0."""
        device = MockGPSDevice()
        pts = _mercator_pts((53.0, -6.0), (53.01, -6.0))
        device.update_route(pts)
        assert len(device.route_points) == 2
        assert device.current_segment_idx == 0
        assert device.distance_along_segment == 0.0

    def test_position_updated_emitted_once_with_initial_lat_lon(self, qapp, qtbot):
        """update_route emits position_updated exactly once with the first point's lat/lon."""
        device = MockGPSDevice()
        lat, lon = 53.0, -6.0
        pts = _mercator_pts((lat, lon), (53.01, -6.0))

        received = []
        device.position_updated.connect(lambda la, lo, sp, hd, q: received.append((la, lo)))

        device.update_route(pts)
        assert len(received) == 1
        got_lat, got_lon = received[0]
        assert abs(got_lat - lat) < 1e-4
        assert abs(got_lon - lon) < 1e-4

    def test_initial_heading_east_for_eastward_route(self, qapp):
        """Route going due east (dy=0, dx>0) → heading ≈ 90°."""
        device = MockGPSDevice()
        # Both points at same lat, second is east (larger lon)
        pts = _mercator_pts((0.0, 0.0), (0.0, 1.0))
        device.update_route(pts)
        # East direction: atan2(0, positive) → angle 0 → heading = (450-0)%360 = 90
        assert abs(device.last_heading - 90.0) < 1.0

    def test_initial_heading_north_for_northward_route(self, qapp):
        """Route going due north (dx=0, dy>0) → heading ≈ 0° (or 360°)."""
        device = MockGPSDevice()
        pts = _mercator_pts((0.0, 0.0), (1.0, 0.0))
        device.update_route(pts)
        # North: atan2(positive, 0) = 90°, heading = (450-90)%360 = 0
        heading = device.last_heading
        assert abs(heading) < 1.0 or abs(heading - 360.0) < 1.0


# ---------------------------------------------------------------------------
# _on_timer tests
# ---------------------------------------------------------------------------

class TestMockGPSDeviceOnTimer:
    """Tests for MockGPSDevice._on_timer (simulation math)."""

    def test_no_route_emits_stationary_position(self, qapp):
        """With no route, _on_timer emits the last known position unchanged."""
        device = MockGPSDevice()
        device.last_lat = 53.349805
        device.last_lon = -6.260310

        received = []
        device.position_updated.connect(lambda la, lo, sp, hd, q: received.append((la, lo)))
        device._on_timer()
        assert len(received) == 1
        assert abs(received[0][0] - device.last_lat) < 1e-9
        assert abs(received[0][1] - device.last_lon) < 1e-9

    def test_advances_position_along_route(self, qapp):
        """After N timer ticks, position should have advanced along the route."""
        device = MockGPSDevice(speed_kmh=50.0)
        # Create a 1000 m eastward route at equator (x changes, y=0)
        pts = [QPointF(0.0, 0.0), QPointF(1000.0, 0.0)]
        device.update_route(pts)

        # Flush the initial position signal
        initial_lat = device.last_lat

        # Advance a few ticks manually
        for _ in range(5):
            device._on_timer()

        # Speed=50 km/h, dt=0.5 s, so 5 ticks = 5 * 50/3.6 * 0.5 ≈ 34.7 m
        expected_advance = 5 * (50.0 / 3.6) * device.update_interval_sec  # metres Mercator
        # The Mercator x should have advanced by about that amount
        end_x = QPointF(*project_mercator(device.last_lat, device.last_lon)).x()
        # x started at 0, should now be ~34.7
        assert end_x > 1.0, f"Expected position to advance, but x={end_x}"
        assert end_x < 1000.0, "Should not have reached the end yet"

    def test_timer_stops_and_final_position_emitted_when_route_complete(self, qapp):
        """When the last segment is traversed, the timer stops and final position is emitted."""
        device = MockGPSDevice(speed_kmh=50.0)
        # Very short route: 10 m, one tick covers it
        pts = [QPointF(0.0, 0.0), QPointF(10.0, 0.0)]
        device.update_route(pts)
        device.timer.start(500)  # start so stop() can be tested

        statuses = []
        device.status_message.connect(statuses.append)

        # Force _on_timer calls until done (max 100)
        for _ in range(100):
            if not device.timer.isActive() and device.current_segment_idx >= len(pts) - 1:
                break
            device._on_timer()

        # Timer should be stopped
        assert not device.timer.isActive()
        # A status "Simulation reached destination." should have been emitted
        assert any("destination" in s.lower() for s in statuses)

    def test_heading_east_direction(self, qapp):
        """Eastward route (dx>0, dy=0) → heading ≈ 90°."""
        device = MockGPSDevice(speed_kmh=50.0)
        pts = [QPointF(0.0, 0.0), QPointF(1000.0, 0.0)]
        device.update_route(pts)
        device._on_timer()
        assert abs(device.last_heading - 90.0) < 2.0

    def test_heading_north_direction(self, qapp):
        """Northward route (dx=0, dy>0) → heading ≈ 0°."""
        device = MockGPSDevice(speed_kmh=50.0)
        pts = [QPointF(0.0, 0.0), QPointF(0.0, 1000.0)]
        device.update_route(pts)
        device._on_timer()
        heading = device.last_heading
        assert abs(heading) < 2.0 or abs(heading - 360.0) < 2.0
