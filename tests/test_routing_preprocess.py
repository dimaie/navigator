# tests/test_routing_preprocess.py
"""
Tests for compile_routing() in routing_preprocess.py.
Uses in-memory or tmp_path SQLite databases — never touches the production DB.
"""

import array
import json
import sqlite3
import struct
import pytest

from routing_preprocess import compile_routing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pack_node_ids(*node_ids):
    """Pack a list of node IDs as int64 little-endian blob (as stored in raw_ways)."""
    return struct.pack(f"<{len(node_ids)}q", *node_ids)


def _create_minimal_raw_db(db_path, way_tags=None, extra_nodes=None):
    """
    Creates a minimal SQLite DB with raw_nodes and raw_ways.

    Nodes: id=1 lat=53.0 lon=-6.0, id=2 lat=53.0 lon=-5.99, id=3 lat=53.01 lon=-5.99
    Way: nodes=[1,2,3], tags = way_tags (default: residential no-oneway)
    """
    if way_tags is None:
        way_tags = {"highway": "residential", "name": "Test Road", "oneway": "no"}

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "CREATE TABLE raw_nodes (id INTEGER PRIMARY KEY, lat REAL, lon REAL, tags TEXT)"
    )
    nodes = [
        (1, 53.0, -6.0, "{}"),
        (2, 53.0, -5.99, "{}"),
        (3, 53.01, -5.99, "{}"),
    ]
    if extra_nodes:
        nodes += extra_nodes
    cursor.executemany("INSERT INTO raw_nodes VALUES (?, ?, ?, ?)", nodes)

    cursor.execute(
        "CREATE TABLE raw_ways (id INTEGER PRIMARY KEY, nodes BLOB, tags TEXT)"
    )
    nodes_blob = _pack_node_ids(1, 2, 3)
    cursor.execute(
        "INSERT INTO raw_ways VALUES (?, ?, ?)",
        (1001, nodes_blob, json.dumps(way_tags)),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Test: missing raw_ways table
# ---------------------------------------------------------------------------

class TestCompileRoutingMissingTable:
    def test_missing_raw_ways_returns_false(self, tmp_path):
        """compile_routing should return False when raw_ways table is absent."""
        db = str(tmp_path / "noways.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE raw_nodes (id INTEGER PRIMARY KEY, lat REAL, lon REAL, tags TEXT)")
        conn.commit()
        conn.close()
        result = compile_routing(db)
        assert result is False


# ---------------------------------------------------------------------------
# Test: successful compile
# ---------------------------------------------------------------------------

class TestCompileRoutingSuccess:
    def test_compile_returns_true(self, tmp_path):
        db = str(tmp_path / "r.db")
        _create_minimal_raw_db(db)
        assert compile_routing(db) is True

    def test_routing_nodes_table_created(self, tmp_path):
        db = str(tmp_path / "r.db")
        _create_minimal_raw_db(db)
        compile_routing(db)
        conn = sqlite3.connect(db)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='routing_nodes'")
        assert cursor.fetchone() is not None
        conn.close()

    def test_routing_edges_table_created(self, tmp_path):
        db = str(tmp_path / "r.db")
        _create_minimal_raw_db(db)
        compile_routing(db)
        conn = sqlite3.connect(db)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='routing_edges'")
        assert cursor.fetchone() is not None
        conn.close()

    def test_routing_nodes_has_correct_columns(self, tmp_path):
        db = str(tmp_path / "r.db")
        _create_minimal_raw_db(db)
        compile_routing(db)
        conn = sqlite3.connect(db)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(routing_nodes)")
        cols = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert {"id", "x", "y"}.issubset(cols)

    def test_routing_edges_has_correct_columns(self, tmp_path):
        db = str(tmp_path / "r.db")
        _create_minimal_raw_db(db)
        compile_routing(db)
        conn = sqlite3.connect(db)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(routing_edges)")
        cols = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert {"id", "from_node", "to_node", "length", "way_type", "name", "oneway", "is_roundabout", "coords"}.issubset(cols)

    def test_node_count_at_least_two(self, tmp_path):
        """A 3-node way should produce at least 2 junction routing nodes."""
        db = str(tmp_path / "r.db")
        _create_minimal_raw_db(db)
        compile_routing(db)
        conn = sqlite3.connect(db)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM routing_nodes")
        count = cursor.fetchone()[0]
        conn.close()
        assert count >= 2

    def test_edge_count_at_least_one(self, tmp_path):
        db = str(tmp_path / "r.db")
        _create_minimal_raw_db(db)
        compile_routing(db)
        conn = sqlite3.connect(db)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM routing_edges")
        count = cursor.fetchone()[0]
        conn.close()
        assert count >= 1

    def test_way_type_is_residential(self, tmp_path):
        db = str(tmp_path / "r.db")
        _create_minimal_raw_db(db)
        compile_routing(db)
        conn = sqlite3.connect(db)
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT way_type FROM routing_edges")
        types = {row[0] for row in cursor.fetchall()}
        conn.close()
        assert "residential" in types


# ---------------------------------------------------------------------------
# Test: oneway parsing
# ---------------------------------------------------------------------------

class TestCompileRoutingOneWay:
    @pytest.mark.parametrize("oneway_tag,expected_oneway", [
        ("yes", 1),
        ("-1", -1),
        ("no", 0),
    ])
    def test_oneway_tag_parsing(self, tmp_path, oneway_tag, expected_oneway):
        db = str(tmp_path / f"ow_{oneway_tag.replace('-','m')}.db")
        tags = {"highway": "residential", "oneway": oneway_tag}
        _create_minimal_raw_db(db, way_tags=tags)
        compile_routing(db)
        conn = sqlite3.connect(db)
        cursor = conn.cursor()
        cursor.execute("SELECT oneway FROM routing_edges LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        assert row is not None
        assert row[0] == expected_oneway

    def test_roundabout_junction_sets_oneway_and_is_roundabout(self, tmp_path):
        """junction=roundabout → oneway=1, is_roundabout=1."""
        db = str(tmp_path / "rab.db")
        tags = {"highway": "residential", "junction": "roundabout"}
        _create_minimal_raw_db(db, way_tags=tags)
        compile_routing(db)
        conn = sqlite3.connect(db)
        cursor = conn.cursor()
        cursor.execute("SELECT oneway, is_roundabout FROM routing_edges LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        assert row is not None
        oneway, is_rab = row
        assert oneway == 1
        assert is_rab == 1


# ---------------------------------------------------------------------------
# Test: non-drivable highway excluded
# ---------------------------------------------------------------------------

class TestCompileRoutingExclusion:
    def test_non_drivable_highway_not_inserted(self, tmp_path):
        """'motorway_junction' is not in DRIVABLE_HIGHWAYS → zero edges inserted."""
        db = str(tmp_path / "excl.db")
        tags = {"highway": "motorway_junction"}
        _create_minimal_raw_db(db, way_tags=tags)
        compile_routing(db)
        conn = sqlite3.connect(db)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM routing_edges")
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 0


# ---------------------------------------------------------------------------
# Test: coordinate blob round-trip
# ---------------------------------------------------------------------------

class TestCompileRoutingCoordsBlob:
    def test_coords_blob_contains_valid_mercator_values(self, tmp_path):
        """Unpacking the coords BLOB should yield Web Mercator X/Y values in range."""
        db = str(tmp_path / "blob.db")
        _create_minimal_raw_db(db)
        compile_routing(db)
        conn = sqlite3.connect(db)
        cursor = conn.cursor()
        cursor.execute("SELECT coords FROM routing_edges LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        assert row is not None
        blob = row[0]
        arr = array.array("d", blob)
        # Should have at least 4 doubles (x1,y1,x2,y2)
        assert len(arr) >= 4
        # Check first x and y are plausible Web Mercator values
        x1, y1 = arr[0], arr[1]
        assert -20_000_000 < x1 < 20_000_000, f"x1={x1} out of Mercator range"
        assert -20_000_000 < y1 < 20_000_000, f"y1={y1} out of Mercator range"
