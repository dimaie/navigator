# tests/conftest.py
"""
Shared fixtures for the Maps project test suite.
"""

import array
import struct
import sqlite3
import sys
import os

import pytest
from PySide6.QtCore import QPointF

# Ensure project root is on sys.path so all modules resolve correctly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils import project_mercator


# ---------------------------------------------------------------------------
# Simple point helper
# ---------------------------------------------------------------------------

def pt(x, y):
    """Helper function – returns a QPointF(x, y)."""
    return QPointF(x, y)


@pytest.fixture
def line_pts():
    """Five QPointF points forming a horizontal line at y=0, x=0,100,200,300,400."""
    return [QPointF(x, 0.0) for x in (0.0, 100.0, 200.0, 300.0, 400.0)]


@pytest.fixture
def synthetic_routing_db(tmp_path):
    """
    Creates a minimal SQLite routing database at tmp_path/test.db.

    Nodes (Web Mercator metres):
        1 → (0, 0)
        2 → (1000, 0)
        3 → (1000, 1000)
        4 → (0, 1000)

    Edges (all residential, oneway=0, length=1000):
        1→2, 2→3, 3→4, 4→1

    Returns the path string (str).
    """
    db_file = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE routing_nodes (
            id INTEGER PRIMARY KEY,
            x REAL,
            y REAL
        )
    """)
    cursor.execute("""
        CREATE TABLE routing_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_node INTEGER,
            to_node INTEGER,
            length REAL,
            way_type TEXT,
            name TEXT,
            oneway INTEGER,
            is_roundabout INTEGER,
            coords BLOB
        )
    """)

    # Insert nodes
    nodes = [(1, 0.0, 0.0), (2, 1000.0, 0.0), (3, 1000.0, 1000.0), (4, 0.0, 1000.0)]
    cursor.executemany("INSERT INTO routing_nodes (id, x, y) VALUES (?, ?, ?)", nodes)

    def make_coords_blob(x1, y1, x2, y2):
        return array.array("d", [x1, y1, x2, y2]).tobytes()

    edges = [
        (1, 2, 1000.0, "residential", "Test Road A", 0, 0, make_coords_blob(0, 0, 1000, 0)),
        (2, 3, 1000.0, "residential", "Test Road B", 0, 0, make_coords_blob(1000, 0, 1000, 1000)),
        (3, 4, 1000.0, "residential", "Test Road C", 0, 0, make_coords_blob(1000, 1000, 0, 1000)),
        (4, 1, 1000.0, "residential", "Test Road D", 0, 0, make_coords_blob(0, 1000, 0, 0)),
    ]
    cursor.executemany(
        "INSERT INTO routing_edges (from_node, to_node, length, way_type, name, oneway, is_roundabout, coords) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        edges,
    )

    # Create spatial indexes expected by find_route_astar
    cursor.execute("CREATE INDEX idx_routing_nodes_coords ON routing_nodes (x, y)")
    cursor.execute("CREATE INDEX idx_routing_edges_nodes ON routing_edges (from_node, to_node)")

    conn.commit()
    conn.close()
    return db_file


@pytest.fixture
def car_profile():
    """Standard Car routing profile dict (no fallback, residential speed=30)."""
    return {
        "driving_side": "left",
        "fallback_profile": None,
        "distance_weight": 0.1,
        "speed_weight": 0.9,
        "prohibited_links": ["path", "footway", "cycleway", "pedestrian"],
        "speeds": {"residential": 30},
        "multipliers": {},
    }


@pytest.fixture
def simple_route_points():
    """
    10 QPointF points spaced ~100 m apart along a horizontal line near lat=53°.
    Lons range from -6.0 increasing by 0.001° steps (≈ ~65 m each, so we use
    enough steps to space them roughly 100 m apart).
    """
    pts = []
    # At lat=53°, 1° lon ≈ 66,600 m  →  ~0.0015° per 100 m
    for i in range(10):
        lon = -6.0 + i * 0.0015
        x, y = project_mercator(53.0, lon)
        pts.append(QPointF(x, y))
    return pts
