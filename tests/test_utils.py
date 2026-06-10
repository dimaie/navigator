# tests/test_utils.py
"""
Tests for utils.py — project_mercator, inverse_mercator, simplify_*, stitch_node_loops.
"""

import math
import pytest
from PySide6.QtCore import QPointF

from utils import (
    project_mercator,
    inverse_mercator,
    simplify_radial,
    simplify_rdp,
    simplify_path,
    stitch_node_loops,
)


# ---------------------------------------------------------------------------
# project_mercator / inverse_mercator
# ---------------------------------------------------------------------------

class TestProjectMercator:
    """Tests for project_mercator."""

    def test_dublin_known_coordinate_x(self):
        """Dublin (53.3498, -6.2603) should project to X ≈ -696,893 m (±100 m)."""
        x, _ = project_mercator(53.3498, -6.2603)
        assert abs(x - (-696_893.0)) < 100.0, f"X={x}"

    def test_dublin_known_coordinate_y(self):
        """Dublin (53.3498, -6.2603) should project to Y ≈ 7,047,965 m (±100 m)."""
        _, y = project_mercator(53.3498, -6.2603)
        assert abs(y - 7_047_965.0) < 100.0, f"Y={y}"

    def test_longitude_zero_gives_x_zero(self):
        """lon=0 → x=0."""
        x, _ = project_mercator(10.0, 0.0)
        assert abs(x) < 1e-6

    def test_latitude_zero_gives_y_zero(self):
        """lat=0 → y=0."""
        _, y = project_mercator(0.0, 5.0)
        assert abs(y) < 1e-6

    def test_latitude_clamping_no_domain_error(self):
        """lat=90 should be clamped to 85.05112878 without raising MathDomainError."""
        try:
            x, y = project_mercator(90.0, 0.0)
        except ValueError as e:
            pytest.fail(f"project_mercator raised ValueError for lat=90: {e}")
        # Clamped result should match lat=85.05112878
        x2, y2 = project_mercator(85.05112878, 0.0)
        assert abs(y - y2) < 1.0

    @pytest.mark.parametrize("lat,lon", [
        (0.0, 0.0),
        (53.3498, -6.2603),
        (-33.8688, 151.2093),  # Sydney
        (51.5074, -0.1278),    # London
        (40.7128, -74.0060),   # New York
        (-80.0, 0.0),          # near south limit
        (80.0, 180.0),
    ])
    def test_round_trip(self, lat, lon):
        """inverse_mercator(project_mercator(lat, lon)) ≈ (lat, lon) within 1e-6°."""
        # Clamp lat to valid Mercator range
        lat_clamped = max(-85.05112878, min(85.05112878, lat))
        x, y = project_mercator(lat_clamped, lon)
        lat2, lon2 = inverse_mercator(x, y)
        assert abs(lat2 - lat_clamped) < 1e-6, f"lat mismatch: {lat2} vs {lat_clamped}"
        assert abs(lon2 - lon) < 1e-6, f"lon mismatch: {lon2} vs {lon}"


class TestInverseMercator:
    """Tests for inverse_mercator."""

    def test_origin_gives_zero_zero(self):
        """inverse_mercator(0, 0) → (0, 0)."""
        lat, lon = inverse_mercator(0.0, 0.0)
        assert abs(lat) < 1e-6
        assert abs(lon) < 1e-6


# ---------------------------------------------------------------------------
# simplify_radial
# ---------------------------------------------------------------------------

class TestSimplifyRadial:
    """Tests for simplify_radial."""

    def test_empty_list_returns_empty(self):
        result = simplify_radial([], 10.0)
        assert result == []

    def test_two_points_returned_unchanged(self):
        pts = [QPointF(0, 0), QPointF(100, 0)]
        result = simplify_radial(pts, 10.0)
        assert result is pts  # passthrough, same object

    def test_cluster_within_min_dist_collapsed(self):
        """5 points all within min_dist → first and last are always preserved."""
        pts = [QPointF(0, 0), QPointF(1, 0), QPointF(2, 0), QPointF(3, 0), QPointF(4, 0)]
        result = simplify_radial(pts, 10.0)  # min_dist=10, all gaps=1 < 10
        assert result[0] == pts[0]
        assert result[-1] == pts[-1]
        assert len(result) < len(pts)

    def test_points_spaced_above_min_dist_all_kept(self):
        """Points spaced at min_dist+1 → all kept."""
        min_dist = 5.0
        step = min_dist + 1
        pts = [QPointF(i * step, 0) for i in range(5)]
        result = simplify_radial(pts, min_dist)
        assert len(result) == len(pts)

    def test_first_and_last_always_preserved(self):
        """First and last points are always in the result."""
        pts = [QPointF(i, 0) for i in range(10)]
        result = simplify_radial(pts, 1000.0)
        assert result[0] == pts[0]
        assert result[-1] == pts[-1]


# ---------------------------------------------------------------------------
# simplify_rdp
# ---------------------------------------------------------------------------

class TestSimplifyRdp:
    """Tests for simplify_rdp (Ramer-Douglas-Peucker)."""

    def test_empty_list_returned_unchanged(self):
        result = simplify_rdp([], 1.0)
        assert result == []

    def test_two_points_returned_unchanged(self):
        pts = [QPointF(0, 0), QPointF(10, 0)]
        result = simplify_rdp(pts, 1.0)
        assert result is pts

    def test_perfectly_collinear_keeps_only_endpoints(self):
        """All collinear points → only first and last remain."""
        pts = [QPointF(i * 10.0, 0.0) for i in range(10)]
        result = simplify_rdp(pts, 0.5)
        assert len(result) == 2
        assert result[0] == pts[0]
        assert result[-1] == pts[-1]

    def test_spike_above_epsilon_is_preserved(self):
        """V-shape with spike above epsilon → spike is kept."""
        pts = [QPointF(0, 0), QPointF(50, 100), QPointF(100, 0)]
        result = simplify_rdp(pts, 1.0)
        # All three must survive because spike height=50 >> epsilon=1
        xs = {p.x() for p in result}
        assert 50.0 in xs, "Spike at x=50 should be preserved"

    def test_all_within_epsilon_keeps_only_endpoints(self):
        """All points within epsilon of the line → only endpoints remain."""
        # Points on a line y = 0 with tiny y perturbation < epsilon
        pts = [QPointF(float(i) * 10, 0.1) for i in range(8)]
        pts[0] = QPointF(0.0, 0.0)
        pts[-1] = QPointF(70.0, 0.0)
        result = simplify_rdp(pts, 5.0)
        assert len(result) == 2

    def test_single_point_returned_unchanged(self):
        pts = [QPointF(0, 0)]
        result = simplify_rdp(pts, 1.0)
        assert result is pts


# ---------------------------------------------------------------------------
# simplify_path
# ---------------------------------------------------------------------------

class TestSimplifyPath:
    """Tests for simplify_path (combined radial + RDP)."""

    def test_noisy_line_with_clusters_is_reduced(self):
        """Result has fewer points than a noisy, clustered input."""
        import random
        random.seed(42)
        pts = []
        for i in range(100):
            # cluster every 10 points within a tiny area
            base_x = (i // 10) * 100.0
            pts.append(QPointF(base_x + random.uniform(0, 1), random.uniform(0, 1)))
        result = simplify_path(pts, 5.0)
        assert len(result) < len(pts)

    def test_two_points_passthrough(self):
        pts = [QPointF(0, 0), QPointF(1, 1)]
        result = simplify_path(pts, 1.0)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# stitch_node_loops
# ---------------------------------------------------------------------------

class TestStitchNodeLoops:
    """Tests for stitch_node_loops."""

    def test_empty_input_returns_empty_tuples(self):
        closed, open_paths = stitch_node_loops([])
        assert closed == []
        assert open_paths == []

    def test_single_two_node_way_is_open_path(self):
        ways = [[1, 2]]
        closed, open_paths = stitch_node_loops(ways)
        assert closed == []
        assert len(open_paths) == 1
        assert open_paths[0] == [1, 2]

    def test_two_ways_sharing_endpoint_stitched_to_open_path(self):
        """[1,2] and [2,3] share node 2 → one open path [1,2,3]."""
        ways = [[1, 2], [2, 3]]
        closed, open_paths = stitch_node_loops(ways)
        assert closed == []
        assert len(open_paths) == 1
        path = open_paths[0]
        assert path[0] in (1, 3)
        assert path[-1] in (1, 3)
        assert 2 in path
        assert len(path) == 3

    def test_three_ways_forming_triangle_become_closed_loop(self):
        """[1,2], [2,3], [3,1] → one closed loop."""
        ways = [[1, 2], [2, 3], [3, 1]]
        closed, open_paths = stitch_node_loops(ways)
        assert len(closed) == 1
        assert open_paths == []
        loop = closed[0]
        assert loop[0] == loop[-1]

    def test_two_disconnected_ways_become_two_open_paths(self):
        """[1,2] and [3,4] share no endpoints → two separate open paths."""
        ways = [[1, 2], [3, 4]]
        closed, open_paths = stitch_node_loops(ways)
        assert closed == []
        assert len(open_paths) == 2

    def test_single_node_ways_excluded(self):
        """Ways with fewer than 2 nodes are skipped."""
        ways = [[1], [2, 3]]
        closed, open_paths = stitch_node_loops(ways)
        total = len(closed) + len(open_paths)
        # Only the valid [2,3] way contributes
        assert total == 1
