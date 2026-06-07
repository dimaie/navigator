# routing_preprocess.py

import sqlite3
import json
import struct
import math
import time
from utils import project_mercator

DRIVABLE_HIGHWAYS = {
    'motorway', 'trunk', 'primary', 'secondary', 'tertiary',
    'unclassified', 'residential', 'living_street', 'service',
    'motorway_link', 'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link'
}

def compile_routing(db_path, progress_callback=None):
    """
    Compiles the routing graph from raw_nodes and raw_ways in the SQLite database
    and stores it in routing_nodes and routing_edges.
    """
    start_time = time.time()
    if progress_callback:
        progress_callback(5, "Connecting to database...")
        
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Enable performance optimization pragmas
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=OFF")
    cursor.execute("PRAGMA cache_size=-1000000") # 1GB cache
    
    # 1. Check if raw tables exist
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='raw_ways'")
    if not cursor.fetchone():
        print("Error: raw_ways table not found in database. Cannot compile routing.")
        conn.close()
        return False
        
    if progress_callback:
        progress_callback(10, "Scanning highway ways...")
        
    print("=== Phase 1: Scanning raw ways for highway networks ===")
    t0 = time.time()
    
    # Query ways with highway tags
    cursor.execute("SELECT id, nodes, tags FROM raw_ways WHERE tags LIKE '%\"highway\":%'")
    rows = cursor.fetchall()
    print(f"Found {len(rows)} raw ways with potential highway tags.")
    
    ways_data = {}
    needed_node_ids = set()
    node_counts = {}
    
    for way_id, nodes_blob, tags_json in rows:
        if not nodes_blob or not tags_json:
            continue
        try:
            tags = json.loads(tags_json)
        except:
            continue
            
        highway = tags.get("highway")
        if highway not in DRIVABLE_HIGHWAYS:
            continue
            
        num_nodes = len(nodes_blob) // 8
        node_ids = struct.unpack(f"<{num_nodes}q", nodes_blob)
        if len(node_ids) < 2:
            continue
            
        ways_data[way_id] = (node_ids, tags)
        for nid in node_ids:
            node_counts[nid] = node_counts.get(nid, 0) + 1
            needed_node_ids.add(nid)
            
    print(f"Loaded {len(ways_data)} drivable highway ways referencing {len(needed_node_ids)} unique nodes in {time.time() - t0:.1f}s.")
    
    if progress_callback:
        progress_callback(30, "Identifying junction nodes...")
        
    # Identify junction nodes (count >= 2, or first/last node of any way)
    junctions = set()
    for way_id, (node_ids, _) in ways_data.items():
        junctions.add(node_ids[0])
        junctions.add(node_ids[-1])
        for nid in node_ids:
            if node_counts.get(nid, 0) >= 2:
                junctions.add(nid)
                
    print(f"Identified {len(junctions)} junction nodes (intersections/endpoints).")
    
    if progress_callback:
        progress_callback(40, "Loading node coordinates...")
        
    print("=== Phase 2: Resolving node coordinates ===")
    t0 = time.time()
    
    # Optimization: create temp table for fast join coordinate resolution
    cursor.execute("DROP TABLE IF EXISTS temp_needed_nodes")
    cursor.execute("CREATE TEMP TABLE temp_needed_nodes (id INTEGER PRIMARY KEY)")
    
    # Insert in batches
    nodes_list = [(nid,) for nid in needed_node_ids]
    cursor.executemany("INSERT OR IGNORE INTO temp_needed_nodes (id) VALUES (?)", nodes_list)
    conn.commit()
    
    # Retrieve coordinates via join
    cursor.execute("SELECT id, lat, lon FROM raw_nodes JOIN temp_needed_nodes USING (id)")
    node_coords = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
    
    # Clean up temp table
    cursor.execute("DROP TABLE IF EXISTS temp_needed_nodes")
    conn.commit()
    
    print(f"Loaded {len(node_coords)} node coordinates in {time.time() - t0:.1f}s.")
    
    if progress_callback:
        progress_callback(60, "Segmenting roads into edges...")
        
    print("=== Phase 3: Segmenting roads into edges ===")
    t0 = time.time()
    
    edges_to_insert = []
    
    def save_edge(from_node, to_node, segment_nodes, name, way_type, oneway):
        coords = []
        for nid in segment_nodes:
            if nid in node_coords:
                lat, lon = node_coords[nid]
                x, y = project_mercator(lat, lon)
                coords.append((x, y))
        if len(coords) < 2:
            return
            
        # Compute length in meters
        length = 0.0
        for j in range(len(coords) - 1):
            x1, y1 = coords[j]
            x2, y2 = coords[j+1]
            length += math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
            
        # Pack coordinates into double-precision binary blob
        coord_flat = [val for pt in coords for val in pt]
        blob = struct.pack(f"<{len(coord_flat)}d", *coord_flat)
        
        edges_to_insert.append((from_node, to_node, length, way_type, name, oneway, blob))
        
    for way_id, (node_ids, tags) in ways_data.items():
        name = tags.get("name", "")
        way_type = tags.get("highway", "")
        
        # Parse one-way attribute
        oneway_tag = tags.get("oneway", "no")
        oneway = 0
        if oneway_tag in ("yes", "1", "true"):
            oneway = 1
        elif oneway_tag == "-1":
            oneway = -1
            
        current_segment = []
        start_junction = node_ids[0]
        
        for i, nid in enumerate(node_ids):
            current_segment.append(nid)
            # If this node is a junction (excluding the starting junction itself)
            if nid in junctions and nid != start_junction:
                save_edge(start_junction, nid, current_segment, name, way_type, oneway)
                start_junction = nid
                current_segment = [nid]
            elif i == len(node_ids) - 1 and nid != start_junction:
                save_edge(start_junction, nid, current_segment, name, way_type, oneway)
                
    print(f"Generated {len(edges_to_insert)} directed routing edges in {time.time() - t0:.1f}s.")
    
    if progress_callback:
        progress_callback(80, "Writing routing tables to SQLite...")
        
    print("=== Phase 4: Writing routing tables to SQLite ===")
    t0 = time.time()
    
    cursor.execute("DROP TABLE IF EXISTS routing_nodes")
    cursor.execute("DROP TABLE IF EXISTS routing_edges")
    
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
        coords BLOB
    )
    """)
    
    # Save routing nodes
    nodes_to_insert = []
    for nid in junctions:
        if nid in node_coords:
            lat, lon = node_coords[nid]
            x, y = project_mercator(lat, lon)
            nodes_to_insert.append((nid, x, y))
            
    cursor.executemany("INSERT INTO routing_nodes (id, x, y) VALUES (?, ?, ?)", nodes_to_insert)
    print(f"Inserted {len(nodes_to_insert)} routing nodes.")
    
    # Save routing edges
    cursor.executemany(
        "INSERT INTO routing_edges (from_node, to_node, length, way_type, name, oneway, coords) VALUES (?, ?, ?, ?, ?, ?, ?)",
        edges_to_insert
    )
    print(f"Inserted {len(edges_to_insert)} routing edges.")
    
    conn.commit()
    
    if progress_callback:
        progress_callback(95, "Creating indexes...")
        
    print("Creating indexes...")
    cursor.execute("CREATE INDEX idx_routing_nodes_coords ON routing_nodes (x, y)")
    cursor.execute("CREATE INDEX idx_routing_edges_nodes ON routing_edges (from_node, to_node)")
    
    conn.commit()
    conn.close()
    
    if progress_callback:
        progress_callback(100, "Done!")
        
    print(f"Routing compilation finished successfully in {time.time() - start_time:.1f}s.")
    return True
