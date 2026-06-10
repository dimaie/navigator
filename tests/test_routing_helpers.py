# tests/test_routing_helpers.py
"""
Tests for pure helper functions in routing_worker.py:
  - ComputedRoute dataclass
  - get_ground_distance
  - project_point_to_segment
  - get_look_ahead_point
  - trim_route
"""

import math
import pytest
from PySide6.QtCore import QPointF

from routing_worker import (
    ComputedRoute,
    get_ground_distance,
    project_point_to_segment,
    get_look_ahead_point,
    trim_route,
)

# ---------------------------------------------------------------------------
# ComputedRoute dataclass
# ---------------------------------------------------------------------------

class TestComputedRoute:
    """Tests for the ComputedRoute dataclass."""

    def test_default_construction_has_empty_lists_and_zero_floats(self):
        route = ComputedRoute()
        assert route.points == []
        assert route.directions == []
        assert route.distance_m == 0.0
        assert route.duration_s == 0.0
        assert route.profile_name == ""

    def test_field_assignment_works(self):
        route = ComputedRoute()
        route.profile_name = "Car"
        assert route.profile_name == "Car"

    def test_points_accepts_list_of_qpointf(self):
        pts = [QPointF(0, 0), QPointF(100, 0)]
        route = ComputedRoute(points=pts, distance_m=100.0)
        assert len(route.points) == 2
        assert route.distance_m == 100.0

    def test_independent_default_lists(self):
        """Two default-constructed instances must not share mutable defaults."""
        r1 = ComputedRoute()
        r2 = ComputedRoute()
        r1.points.append(QPointF(1, 2))
        assert len(r2.points) == 0


# ---------------------------------------------------------------------------
# get_ground_distance
# ---------------------------------------------------------------------------

class TestGetGroundDistance:
    """Tests for get_ground_distance."""

    def test_empty_list_returns_zero(self):
        assert get_ground_distance([]) == 0.0

    def test_single_point_returns_zero(self):
        assert get_ground_distance([QPointF(0, 0)]) == 0.0

    def test_two_points_at_equator_equals_euclidean(self):
        """At y=0 (equator), cosh(0)=1 so ground distance == Euclidean distance."""
        p1 = QPointF(0.0, 0.0)
        p2 = QPointF(1000.0, 0.0)
        dist = get_ground_distance([p1, p2])
        assert abs(dist - 1000.0) < 0.01

    def test_two_points_at_high_latitude_less_than_euclidean(self):
        """At y≈6.4M m (~55°N), scale_factor < 1 so ground dist < Euclidean."""
        y_high = 6_400_000.0  # roughly 55°N
        p1 = QPointF(0.0, y_high)
        p2 = QPointF(1000.0, y_high)
        ground = get_ground_distance([p1, p2])
        euclidean = 1000.0
        assert ground < euclidean

    def test_three_collinear_points_sum_equals_total(self):
        """dist(A,B) + dist(B,C) == dist(A,B,C) for collinear points."""
        a = QPointF(0.0, 0.0)
        b = QPointF(500.0, 0.0)
        c = QPointF(1000.0, 0.0)
        d_ab = get_ground_distance([a, b])
        d_bc = get_ground_distance([b, c])
        d_abc = get_ground_distance([a, b, c])
        assert abs((d_ab + d_bc) - d_abc) < 1e-6


# ---------------------------------------------------------------------------
# project_point_to_segment
# ---------------------------------------------------------------------------

class TestProjectPointToSegment:
    """Tests for project_point_to_segment."""

    def test_point_at_a_returns_a_zero_distance(self):
        a = QPointF(0.0, 0.0)
        b = QPointF(100.0, 0.0)
        proj, d2 = project_point_to_segment(a, a, b)
        assert abs(proj.x() - a.x()) < 1e-9
        assert abs(proj.y() - a.y()) < 1e-9
        assert abs(d2) < 1e-9

    def test_point_at_b_returns_b_zero_distance(self):
        a = QPointF(0.0, 0.0)
        b = QPointF(100.0, 0.0)
        proj, d2 = project_point_to_segment(b, a, b)
        assert abs(proj.x() - b.x()) < 1e-9
        assert abs(d2) < 1e-9

    def test_midpoint_projects_to_midpoint_zero_distance(self):
        a = QPointF(0.0, 0.0)
        b = QPointF(100.0, 0.0)
        mid = QPointF(50.0, 0.0)
        proj, d2 = project_point_to_segment(mid, a, b)
        assert abs(proj.x() - 50.0) < 1e-9
        assert abs(d2) < 1e-9

    def test_perpendicular_point_projects_to_midpoint(self):
        """Point perp. to midpoint at distance d → projection is midpoint, dist²=d²."""
        a = QPointF(0.0, 0.0)
        b = QPointF(100.0, 0.0)
        d = 30.0
        p = QPointF(50.0, d)
        proj, d2 = project_point_to_segment(p, a, b)
        assert abs(proj.x() - 50.0) < 1e-6
        assert abs(proj.y() - 0.0) < 1e-6
        assert abs(d2 - d * d) < 1e-4

    def test_degenerate_segment_returns_a_and_dist_to_a(self):
        """Degenerate segment (a == b) → returns a, distance = |pa|²."""
        a = QPointF(5.0, 5.0)
        p = QPointF(8.0, 9.0)
        proj, d2 = project_point_to_segment(p, a, a)
        assert abs(proj.x() - a.x()) < 1e-9
        assert abs(proj.y() - a.y()) < 1e-9
        expected_d2 = (p.x() - a.x()) ** 2 + (p.y() - a.y()) ** 2
        assert abs(d2 - expected_d2) < 1e-6

    def test_point_beyond_b_clamps_to_b(self):
        """Point beyond B along the segment line → projection is clamped to B."""
        a = QPointF(0.0, 0.0)
        b = QPointF(100.0, 0.0)
        p = QPointF(200.0, 0.0)  # beyond b
        proj, d2 = project_point_to_segment(p, a, b)
        assert abs(proj.x() - 100.0) < 1e-9
        assert abs(proj.y() - 0.0) < 1e-9


# ---------------------------------------------------------------------------
# get_look_ahead_point
# ---------------------------------------------------------------------------

class TestGetLookAheadPoint:
    """Tests for get_look_ahead_point."""

    def test_empty_list_returns_none(self):
        assert get_look_ahead_point([], is_exiting=True) is None

    def test_single_point_returns_that_point(self):
        p = QPointF(1.0, 2.0)
        result = get_look_ahead_point([p], is_exiting=True)
        assert abs(result.x() - 1.0) < 1e-9

    def test_exiting_zero_target_returns_first_point(self):
        """is_exiting=True, target_dist=0 → returns first point (t=0)."""
        pts = [QPointF(0, 0), QPointF(100, 0), QPointF(200, 0)]
        result = get_look_ahead_point(pts, is_exiting=True, target_dist=0.0)
        assert abs(result.x() - 0.0) < 1e-3

    def test_exiting_target_equals_first_segment_returns_second_point(self):
        """is_exiting=True, target_dist = first segment length → at or near second point."""
        # Segment 0→1 at y=0 → scale_factor≈1, ground_len≈100
        pts = [QPointF(0, 0), QPointF(100, 0), QPointF(300, 0)]
        result = get_look_ahead_point(pts, is_exiting=True, target_dist=100.0)
        # Should be at or very near the second point (x=100)
        assert abs(result.x() - 100.0) < 2.0

    def test_exiting_target_beyond_total_returns_last_point(self):
        """is_exiting=True, target_dist > total length → returns last point."""
        pts = [QPointF(0, 0), QPointF(100, 0), QPointF(200, 0)]
        result = get_look_ahead_point(pts, is_exiting=True, target_dist=99999.0)
        assert abs(result.x() - 200.0) < 1e-9

    def test_entering_zero_target_returns_last_point(self):
        """is_exiting=False, target_dist=0 → returns last point."""
        pts = [QPointF(0, 0), QPointF(100, 0), QPointF(200, 0)]
        result = get_look_ahead_point(pts, is_exiting=False, target_dist=0.0)
        assert abs(result.x() - 200.0) < 1e-3

    def test_entering_target_equals_last_segment_returns_second_to_last(self):
        """is_exiting=False, target_dist = last segment length → near second-to-last point."""
        pts = [QPointF(0, 0), QPointF(100, 0), QPointF(200, 0)]
        result = get_look_ahead_point(pts, is_exiting=False, target_dist=100.0)
        assert abs(result.x() - 100.0) < 2.0


# ---------------------------------------------------------------------------
# trim_route
# ---------------------------------------------------------------------------

class TestTrimRoute:
    """Tests for trim_route."""

    def test_empty_input_returns_empty(self):
        assert trim_route([], QPointF(0, 0), QPointF(1, 0)) == []

    def test_single_point_input_returned_unchanged(self):
        pts = [QPointF(0, 0)]
        result = trim_route(pts, QPointF(0, 0), QPointF(1, 0))
        assert result == pts

    def test_start_at_first_end_at_last_no_extra_duplicates(self):
        """When start/end match route endpoints, result has no spurious extra points."""
        pts = [QPointF(0, 0), QPointF(100, 0), QPointF(200, 0)]
        result = trim_route(pts, QPointF(0, 0), QPointF(200, 0))
        assert len(result) >= 2
        assert abs(result[0].x() - 0.0) < 1e-3
        assert abs(result[-1].x() - 200.0) < 1e-3

    def test_start_projected_to_middle_of_first_segment(self):
        """start_pt at (50, 5) near midpoint of segment [0,0]→[100,0]."""
        pts = [QPointF(0, 0), QPointF(100, 0), QPointF(200, 0)]
        start_pt = QPointF(50.0, 0.0)
        end_pt = QPointF(200.0, 0.0)
        result = trim_route(pts, start_pt, end_pt)
        # First point of result should be start_pt itself (or very close)
        assert abs(result[0].x() - 50.0) < 1e-3

    def test_no_adjacent_duplicates_in_result(self):
        """Result should not contain adjacent identical points."""
        pts = [QPointF(i * 100.0, 0.0) for i in range(5)]
        result = trim_route(pts, QPointF(0, 0), QPointF(400, 0))
        for i in range(len(result) - 1):
            dx = result[i+1].x() - result[i].x()
            dy = result[i+1].y() - result[i].y()
            assert dx * dx + dy * dy > 1e-4, f"Duplicate points at index {i}"
