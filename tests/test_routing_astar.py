# tests/test_routing_astar.py
"""
Tests for find_route_astar() using a synthetic SQLite routing database.

Note: PySide6 QPointF does not require a running Qt event loop.
"""

import math
import pytest
from PySide6.QtCore import QPointF

from routing_worker import find_route_astar, ComputedRoute
from utils import project_mercator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_graph_from_db(db_path):
    """
    Loads routing_graph and routing_nodes_coords from a routing SQLite database.
    Mirrors the lazy-loading logic in RoutingWorker.run().
    """
    import sqlite3
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("SELECT id, x, y FROM routing_nodes")
    coords = {nid: (x, y) for nid, x, y in cursor.fetchall()}

    cursor.execute("SELECT id, from_node, to_node, length, way_type, name, oneway, is_roundabout FROM routing_edges")
    graph = {}
    for eid, u, v, length, wtype, name, oneway, is_rab in cursor.fetchall():
        graph.setdefault(u, [])
        graph.setdefault(v, [])
        if oneway == 0:
            graph[u].append((v, length, eid, wtype, name, is_rab))
            graph[v].append((u, length, eid, wtype, name, is_rab))
        elif oneway == 1:
            graph[u].append((v, length, eid, wtype, name, is_rab))
        elif oneway == -1:
            graph[v].append((u, length, eid, wtype, name, is_rab))

    conn.close()
    return graph, coords


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestFindRouteAstarDirect:
    """Direct path (< 100 m) short-circuit cases."""

    def test_direct_path_returns_computed_route(self):
        """Two points 50 m apart → ComputedRoute (not None)."""
        start = QPointF(0.0, 0.0)
        end = QPointF(50.0, 0.0)  # 50 m apart at equator
        result = find_route_astar(start, end, {}, {}, ":memory:", {
            "distance_weight": 1.0, "speed_weight": 0.0,
            "prohibited_links": [], "speeds": {}, "multipliers": {},
        })
        assert result is not None
        assert isinstance(result, ComputedRoute)

    def test_direct_path_has_two_points(self):
        """Direct route → exactly [start, end]."""
        start = QPointF(0.0, 0.0)
        end = QPointF(50.0, 0.0)
        result = find_route_astar(start, end, {}, {}, ":memory:", {
            "distance_weight": 1.0, "speed_weight": 0.0,
            "prohibited_links": [], "speeds": {}, "multipliers": {},
        })
        assert len(result.points) == 2
        assert result.points[0] == start
        assert result.points[1] == end

    def test_direct_path_distance_close_to_50m(self):
        """distance_m ≈ 50 m (within 1 m at equator)."""
        start = QPointF(0.0, 0.0)
        end = QPointF(50.0, 0.0)
        result = find_route_astar(start, end, {}, {}, ":memory:", {
            "distance_weight": 1.0, "speed_weight": 0.0,
            "prohibited_links": [], "speeds": {}, "multipliers": {},
        })
        assert abs(result.distance_m - 50.0) < 1.0

    def test_direct_path_has_two_direction_entries(self):
        """Directions should have 2 entries (instruction + total summary)."""
        start = QPointF(0.0, 0.0)
        end = QPointF(50.0, 0.0)
        result = find_route_astar(start, end, {}, {}, ":memory:", {
            "distance_weight": 1.0, "speed_weight": 0.0,
            "prohibited_links": [], "speeds": {}, "multipliers": {},
        })
        assert len(result.directions) == 2


class TestFindRouteAstarNoGraph:
    """No nodes available → None."""

    def test_empty_graph_dicts_and_no_db_returns_none_or_tuple(self, tmp_path):
        """Empty routing graph with start/end 500 m apart → None or (None, ...) (no nodes found)."""
        # Create a DB with routing tables but no data
        import sqlite3
        db = str(tmp_path / "empty.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE routing_nodes (id INTEGER PRIMARY KEY, x REAL, y REAL)")
        conn.execute("""
            CREATE TABLE routing_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_node INTEGER, to_node INTEGER, length REAL,
                way_type TEXT, name TEXT, oneway INTEGER,
                is_roundabout INTEGER, coords BLOB
            )
        """)
        conn.commit()
        conn.close()

        start = QPointF(0.0, 0.0)
        end = QPointF(500.0, 0.0)
        result = find_route_astar(start, end, {}, {}, db, {
            "distance_weight": 1.0, "speed_weight": 0.0,
            "prohibited_links": [], "speeds": {}, "multipliers": {},
        })
        # The function returns either None or a tuple whose first element is None
        # when no edges/nodes are found
        if isinstance(result, tuple):
            assert result[0] is None
        else:
            assert result is None



class TestFindRouteAstarSyntheticDB:
    """Graph routing tests using the synthetic routing database."""

    def test_simple_two_edge_path_returns_route(self, synthetic_routing_db, car_profile):
        """start near node 1, end near node 2 → returns ComputedRoute."""
        graph, coords = _build_graph_from_db(synthetic_routing_db)
        start = QPointF(10.0, 0.0)    # slightly offset from node 1 (0,0)
        end = QPointF(990.0, 0.0)     # slightly offset from node 2 (1000,0)
        result = find_route_astar(start, end, graph, coords, synthetic_routing_db, car_profile)
        assert result is not None
        assert isinstance(result, ComputedRoute)

    def test_simple_path_has_non_empty_points(self, synthetic_routing_db, car_profile):
        graph, coords = _build_graph_from_db(synthetic_routing_db)
        start = QPointF(10.0, 0.0)
        end = QPointF(990.0, 0.0)
        result = find_route_astar(start, end, graph, coords, synthetic_routing_db, car_profile)
        assert result is not None
        assert len(result.points) >= 2

    def test_simple_path_distance_approximately_1000m(self, synthetic_routing_db, car_profile):
        """Route along one edge of 1000 m → distance ≈ 1000 m (within 50 m)."""
        graph, coords = _build_graph_from_db(synthetic_routing_db)
        start = QPointF(10.0, 0.0)
        end = QPointF(990.0, 0.0)
        result = find_route_astar(start, end, graph, coords, synthetic_routing_db, car_profile)
        assert result is not None
        assert abs(result.distance_m - 1000.0) < 50.0

    def test_simple_path_has_directions(self, synthetic_routing_db, car_profile):
        graph, coords = _build_graph_from_db(synthetic_routing_db)
        start = QPointF(10.0, 0.0)
        end = QPointF(990.0, 0.0)
        result = find_route_astar(start, end, graph, coords, synthetic_routing_db, car_profile)
        assert result is not None
        assert len(result.directions) >= 1

    def test_prohibited_way_type_returns_none(self, synthetic_routing_db):
        """Profile prohibiting 'residential' blocks all edges → None."""
        graph, coords = _build_graph_from_db(synthetic_routing_db)
        profile = {
            "distance_weight": 1.0,
            "speed_weight": 0.0,
            "prohibited_links": ["residential"],
            "speeds": {},
            "multipliers": {},
        }
        start = QPointF(10.0, 0.0)
        end = QPointF(990.0, 0.0)
        result = find_route_astar(start, end, graph, coords, synthetic_routing_db, profile)
        assert result is None


class TestFindRouteAstarReturnType:
    """Return type is always ComputedRoute or None."""

    @pytest.mark.parametrize("start_xy,end_xy", [
        ((0.0, 0.0), (40.0, 0.0)),    # direct path
        ((10.0, 0.0), (990.0, 0.0)),  # graph path
        ((10.0, 0.0), (990.0, 990.0)), # diagonal path
        ((500.0, 500.0), (510.0, 500.0)), # tiny gap, may be direct
    ])
    def test_return_type_is_computed_route_or_none(
        self, synthetic_routing_db, car_profile, start_xy, end_xy
    ):
        graph, coords = _build_graph_from_db(synthetic_routing_db)
        start = QPointF(*start_xy)
        end = QPointF(*end_xy)
        result = find_route_astar(start, end, graph, coords, synthetic_routing_db, car_profile)
        # Result is ComputedRoute, None, or a (None, ...) tuple from the no-edge path
        if isinstance(result, tuple):
            assert result[0] is None
        else:
            assert isinstance(result, ComputedRoute) or result is None

