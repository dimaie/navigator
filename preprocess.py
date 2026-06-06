import sqlite3
import time
import math
import struct
import json
import array
import osmiter
import os

class ProgressFileWrapper:
    """
    A binary file-like object wrapper that forwards read operations
    and reports reading progress percentage via a callback.
    """
    def __init__(self, filepath, progress_callback):
        self._file = open(filepath, 'rb')
        self._total_size = os.path.getsize(filepath)
        self._bytes_read = 0
        self._progress_callback = progress_callback
        self._last_reported_pct = -1

    def read(self, size=-1):
        data = self._file.read(size)
        self._bytes_read += len(data)
        if self._total_size > 0:
            pct = int((self._bytes_read / self._total_size) * 100)
            # Clamp to 100
            pct = min(100, max(0, pct))
            if pct != self._last_reported_pct:
                self._last_reported_pct = pct
                self._progress_callback(pct)
        return data

    def __getattr__(self, name):
        return getattr(self._file, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


from utils import project_mercator, stitch_node_loops
import rules

def run_preprocess(pbf_path, db_path, progress_callback=None):
    start_time = time.time()
    
    # Establish SQLite connection
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    # Speed optimization pragmas for SQLite insertions
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=OFF")
    cursor.execute("PRAGMA cache_size=-2000000") # 2GB cache
    
    cursor.execute("DROP TABLE IF EXISTS raw_nodes")
    cursor.execute("DROP TABLE IF EXISTS raw_ways")
    cursor.execute("DROP TABLE IF EXISTS raw_relations")
    
    cursor.execute("""
    CREATE TABLE raw_nodes (
        id INTEGER PRIMARY KEY,
        lat REAL,
        lon REAL,
        tags TEXT
    )
    """)
    cursor.execute("""
    CREATE TABLE raw_ways (
        id INTEGER PRIMARY KEY,
        nodes BLOB,
        tags TEXT
    )
    """)
    cursor.execute("""
    CREATE TABLE raw_relations (
        id INTEGER PRIMARY KEY,
        members TEXT,
        tags TEXT
    )
    """)
    conn.commit()
    
    print("=== PASS 1: Scanning PBF for ways, places, and raw features ===")
    if progress_callback:
        progress_callback(0, "Pass 1: Scanning elements (0%)")
    
    needed_nodes = set()
    places = []
    kept_ways = []
    coastline_node_loops = []
    highways_map = {}
    rivers_map = {}
    railways_map = {}
    
    if progress_callback:
        def cb1(pct):
            progress_callback(int(pct * 0.45), f"Pass 1: Scanning elements ({pct}%)")
        pbf_file = ProgressFileWrapper(pbf_path, cb1)
        osm_iter = osmiter.iter_from_osm(pbf_file, file_format="pbf")
    else:
        pbf_file = None
        osm_iter = osmiter.iter_from_osm(pbf_path)
        
    nodes_batch = []
    ways_batch = []
    relations_batch = []
    
    count = 0
    try:
        for feature in osm_iter:
            count += 1
            if not progress_callback and count % 5000000 == 0:
                print(f"  Processed {count} elements in Pass 1...")
                
            ftype = feature["type"]
            tags = feature.get("tag", {})
            tags_json = json.dumps(tags) if tags else None
            
            # 1. Lossless Storage insertions
            if ftype == "node":
                nodes_batch.append((feature["id"], feature["lat"], feature["lon"], tags_json))
                if len(nodes_batch) >= 100000:
                    cursor.executemany("INSERT INTO raw_nodes (id, lat, lon, tags) VALUES (?, ?, ?, ?)", nodes_batch)
                    nodes_batch.clear()
            elif ftype == "way":
                nd_ids = feature.get("nd", [])
                nodes_blob = struct.pack(f"<{len(nd_ids)}q", *nd_ids) if nd_ids else None
                ways_batch.append((feature["id"], nodes_blob, tags_json))
                if len(ways_batch) >= 100000:
                    cursor.executemany("INSERT INTO raw_ways (id, nodes, tags) VALUES (?, ?, ?)", ways_batch)
                    ways_batch.clear()
            elif ftype == "relation":
                members = feature.get("member", [])
                members_data = [(m["type"], m["ref"], m.get("role", "")) for m in members]
                members_json = json.dumps(members_data) if members_data else None
                relations_batch.append((feature["id"], members_json, tags_json))
                if len(relations_batch) >= 10000:
                    cursor.executemany("INSERT INTO raw_relations (id, members, tags) VALUES (?, ?, ?)", relations_batch)
                    relations_batch.clear()
            
            # 2. Render Extraction logic
            if ftype == "node":
                place_type = tags.get("place")
                if rules.is_city_town_village(place_type, tags.get("name")):
                    lat, lon = feature["lat"], feature["lon"]
                    x, y = project_mercator(lat, lon)
                    places.append({
                        "id": feature["id"],
                        "name": tags["name"],
                        "place_type": place_type,
                        "x": x,
                        "y": y,
                        "population": int(tags.get("population", 0))
                    })
            elif ftype == "way":
                way_nodes = feature.get("nd", [])
                if way_nodes:
                    highway = tags.get("highway")
                    natural = tags.get("natural")
                    waterway = tags.get("waterway")
                    landuse = tags.get("landuse")
                    railway = tags.get("railway")
                    
                    is_kept = False
                    feature_type = None
                    sub_type = None
                    
                    if rules.is_coastline(natural):
                        is_kept = True
                        feature_type = "coastline"
                        sub_type = "coastline"
                        coastline_node_loops.append(way_nodes)
                    elif rules.is_valid_highway(highway):
                        name = tags.get("name", "")
                        ref = tags.get("ref", "")
                        if ref and name:
                            full_name = f"{ref} ({name})"
                        elif ref:
                            full_name = ref
                        else:
                            full_name = name
                        highways_map.setdefault((highway, full_name), []).append(way_nodes)
                        for nid in way_nodes:
                            needed_nodes.add(nid)
                    elif railway in ('rail', 'tram', 'light_rail', 'subway', 'narrow_gauge', 'monorail'):
                        name = tags.get("name", "")
                        railways_map.setdefault((railway, name), []).append(way_nodes)
                        for nid in way_nodes:
                            needed_nodes.add(nid)
                    elif rules.is_river_canal(waterway):
                        name = tags.get("name", "")
                        rivers_map.setdefault((waterway, name), []).append(way_nodes)
                        for nid in way_nodes:
                            needed_nodes.add(nid)
                    elif rules.is_wetland(natural):
                        is_kept = True
                        feature_type = "wetland"
                        sub_type = "wetland"
                    elif rules.is_forest(natural, landuse):
                        is_kept = True
                        feature_type = "forest"
                        sub_type = natural or landuse
                    elif rules.is_waterbody(natural, waterway, landuse):
                        is_kept = True
                        feature_type = "waterbody"
                        sub_type = natural or waterway or landuse
                        
                    if is_kept and feature_type not in ("coastline", "highway", "river"):
                        if not rules.is_valid_polygon(feature_type, len(way_nodes)):
                            continue
                            
                        name = tags.get("name", "")
                        kept_ways.append({
                            "id": feature["id"],
                            "feature_type": feature_type,
                            "sub_type": sub_type,
                            "name": name,
                            "nodes": way_nodes
                        })
                        for nid in way_nodes:
                            needed_nodes.add(nid)
        
        # Flush remaining batches
        if nodes_batch:
            cursor.executemany("INSERT INTO raw_nodes (id, lat, lon, tags) VALUES (?, ?, ?, ?)", nodes_batch)
        if ways_batch:
            cursor.executemany("INSERT INTO raw_ways (id, nodes, tags) VALUES (?, ?, ?)", ways_batch)
        if relations_batch:
            cursor.executemany("INSERT INTO raw_relations (id, members, tags) VALUES (?, ?, ?)", relations_batch)
        conn.commit()
        
        print("Extracting administrative boundaries from relations...")
        cursor.execute("SELECT members, tags FROM raw_relations WHERE tags LIKE '%\"boundary\"%' AND tags LIKE '%\"administrative\"%'")
        boundary_ways = {}
        for members_json, tags_json in cursor.fetchall():
            try:
                tags = json.loads(tags_json)
                if tags.get("boundary") == "administrative":
                    admin_level = tags.get("admin_level")
                    if admin_level in ("2", "4", "6"):
                        members = json.loads(members_json)
                        al = int(admin_level)
                        for mtype, mref, mrole in members:
                            if mtype == "way":
                                if mref not in boundary_ways or al < int(boundary_ways[mref]):
                                    boundary_ways[mref] = str(al)
            except Exception as e:
                pass

        print(f"Found {len(boundary_ways)} boundary way segments of interest.")
        
        boundary_loaded = 0
        for wid, al in boundary_ways.items():
            cursor.execute("SELECT nodes, tags FROM raw_ways WHERE id = ?", (wid,))
            row = cursor.fetchone()
            if row and row[0]:
                nodes_blob, tags_json = row
                num_nodes = len(nodes_blob) // 8
                nd_ids = struct.unpack(f"<{num_nodes}q", nodes_blob)
                tags = json.loads(tags_json) if tags_json else {}
                name = tags.get("name", "")
                kept_ways.append({
                    "id": wid,
                    "feature_type": "boundary",
                    "sub_type": al,
                    "name": name,
                    "nodes": list(nd_ids)
                })
                for nid in nd_ids:
                    needed_nodes.add(nid)
                boundary_loaded += 1
        print(f"Loaded {boundary_loaded} boundary way segments into kept_ways.")
    finally:
        if pbf_file:
            pbf_file.close()
            
    print(f"Pass 1 finished in {time.time() - start_time:.1f}s.")
    
    if progress_callback:
        progress_callback(45, "Stitching road networks...")
    print("Stitching highways...")
    stitched_highways = 0
    for (sub_type, name), node_lists in highways_map.items():
        loops, open_paths = stitch_node_loops(node_lists)
        for path in loops + open_paths:
            kept_ways.append({
                "feature_type": "highway",
                "sub_type": sub_type,
                "name": name,
                "nodes": path
            })
            stitched_highways += 1
    print(f"Stitched {stitched_highways} highway paths from raw segments.")
    
    if progress_callback:
        progress_callback(45, "Stitching railway networks...")
    print("Stitching railways...")
    stitched_railways = 0
    for (sub_type, name), node_lists in railways_map.items():
        loops, open_paths = stitch_node_loops(node_lists)
        for path in loops + open_paths:
            kept_ways.append({
                "feature_type": "railway",
                "sub_type": sub_type,
                "name": name,
                "nodes": path
            })
            stitched_railways += 1
    print(f"Stitched {stitched_railways} railway paths.")
    
    if progress_callback:
        progress_callback(45, "Stitching river networks...")
    print("Stitching rivers...")
    stitched_rivers = 0
    for (sub_type, name), node_lists in rivers_map.items():
        loops, open_paths = stitch_node_loops(node_lists)
        for path in loops + open_paths:
            kept_ways.append({
                "feature_type": "river",
                "sub_type": sub_type,
                "name": name,
                "nodes": path
            })
            stitched_rivers += 1
    print(f"Stitched {stitched_rivers} river paths.")
    print(f"Found {len(kept_ways)} ways to keep, referencing {len(needed_nodes)} nodes.")
    print(f"Found {len(places)} places.")
    
    print("\n=== PASS 2: Retrieving node coordinates ===")
    pass2_start = time.time()
    node_coords = {}
    
    if progress_callback:
        def cb2(pct):
            progress_callback(45 + int(pct * 0.45), f"Pass 2: Loading coordinates ({pct}%)")
        with ProgressFileWrapper(pbf_path, cb2) as pbf_file:
            osm_iter = osmiter.iter_from_osm(pbf_file, file_format="pbf")
            for feature in osm_iter:
                if feature["type"] == "node":
                    nid = feature["id"]
                    if nid in needed_nodes:
                        lat, lon = feature["lat"], feature["lon"]
                        node_coords[nid] = project_mercator(lat, lon)
    else:
        count = 0
        for feature in osmiter.iter_from_osm(pbf_path):
            count += 1
            if count % 5000000 == 0:
                print(f"  Scanned {count} elements in Pass 2...")
            if feature["type"] == "node":
                nid = feature["id"]
                if nid in needed_nodes:
                    lat, lon = feature["lat"], feature["lon"]
                    node_coords[nid] = project_mercator(lat, lon)
                
    print(f"Pass 2 finished in {time.time() - pass2_start:.1f}s.")
    print(f"Resolved {len(node_coords)} node coordinates.")
    
    print("\n=== Saving data to SQLite database ===")
    db_start = time.time()
    
    cursor.execute("DROP TABLE IF EXISTS ways")
    cursor.execute("DROP TABLE IF EXISTS places")
    cursor.execute("DROP TABLE IF EXISTS config")
    
    cursor.execute("""
    CREATE TABLE ways (
        id INTEGER PRIMARY KEY,
        feature_type TEXT,
        sub_type TEXT,
        name TEXT,
        min_x REAL,
        min_y REAL,
        max_x REAL,
        max_y REAL,
        coords BLOB
    )
    """)
    
    cursor.execute("""
    CREATE TABLE places (
        id INTEGER PRIMARY KEY,
        name TEXT,
        place_type TEXT,
        x REAL,
        y REAL,
        population INTEGER
    )
    """)
    
    cursor.execute("""
    CREATE TABLE config (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    
    # 1. Save Places
    if progress_callback:
        progress_callback(90, "Saving places to database...")
    for p in places:
        cursor.execute(
            "INSERT INTO places (id, name, place_type, x, y, population) VALUES (?, ?, ?, ?, ?, ?)",
            (p["id"], p["name"], p["place_type"], p["x"], p["y"], p["population"])
        )
        
    # 1b. Save County Centroids as Places
    print("Computing and saving county centroids...")
    cursor.execute("SELECT id, tags, members FROM raw_relations WHERE tags LIKE '%\"boundary\"%' AND tags LIKE '%\"administrative\"%' AND tags LIKE '%\"admin_level\": \"6\"%'")
    county_rows = cursor.fetchall()
    county_idx = -1000
    for rel_id, tags_json, members_json in county_rows:
        try:
            tags = json.loads(tags_json)
            name = tags.get("name", "")
            if name:
                members = json.loads(members_json)
                xs, ys = [], []
                for mtype, mref, mrole in members:
                    if mtype == "way":
                        cursor.execute("SELECT nodes FROM raw_ways WHERE id=?", (mref,))
                        row = cursor.fetchone()
                        if row and row[0]:
                            nodes_blob = row[0]
                            num_nodes = len(nodes_blob) // 8
                            nd_ids = struct.unpack(f"<{num_nodes}q", nodes_blob)
                            for nid in nd_ids:
                                cursor.execute("SELECT lat, lon FROM raw_nodes WHERE id=?", (nid,))
                                node_row = cursor.fetchone()
                                if node_row:
                                    lat, lon = node_row
                                    x, y = project_mercator(lat, lon)
                                    xs.append(x)
                                    ys.append(y)
                if xs and ys:
                    cx = sum(xs) / len(xs)
                    cy = sum(ys) / len(ys)
                    cursor.execute(
                        "INSERT INTO places (id, name, place_type, x, y, population) VALUES (?, ?, ?, ?, ?, ?)",
                        (county_idx, name, "county", cx, cy, 99999999)
                    )
                    county_idx -= 1
        except Exception as e:
            pass
        
    # 2. Stitch and Save Coastlines
    if progress_callback:
        progress_callback(92, "Stitching and saving coastlines...")
    print("Stitching coastline...")
    closed_coastlines, open_coastlines = stitch_node_loops(coastline_node_loops)
    print(f"Stitched {len(closed_coastlines)} closed coastlines, {len(open_coastlines)} open coastlines.")
    
    coastline_idx = 1
    for loop in closed_coastlines:
        coords = [node_coords[nid] for nid in loop if nid in node_coords]
        if len(coords) < 3:
            continue
        xs = [pt[0] for pt in coords]
        ys = [pt[1] for pt in coords]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        
        blob = struct.pack(f"<{len(coords)*2}d", *[val for pt in coords for val in pt])
        cursor.execute(
            "INSERT INTO ways (id, feature_type, sub_type, name, min_x, min_y, max_x, max_y, coords) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (coastline_idx, "coastline", "coastline", "Coastline", min_x, min_y, max_x, max_y, blob)
        )
        coastline_idx += 1
        
    # 3. Save other Ways
    if progress_callback:
        progress_callback(94, "Saving ways to database (0%)...")
    print("Saving ways to database...")
    way_db_id = coastline_idx + 1
    total_kept_ways = len(kept_ways)
    for idx, way in enumerate(kept_ways):
        if progress_callback and total_kept_ways > 0 and idx % max(1, total_kept_ways // 100) == 0:
            way_pct = int((idx / total_kept_ways) * 100)
            overall_pct = 94 + int((idx / total_kept_ways) * 4)
            progress_callback(overall_pct, f"Saving ways to database ({way_pct}%)...")
            
        if way["feature_type"] == "coastline":
            continue
            
        coords = [node_coords[nid] for nid in way["nodes"] if nid in node_coords]
        if len(coords) < 2:
            continue
            
        xs = [pt[0] for pt in coords]
        ys = [pt[1] for pt in coords]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        
        blob = struct.pack(f"<{len(coords)*2}d", *[val for pt in coords for val in pt])
        cursor.execute(
            "INSERT INTO ways (id, feature_type, sub_type, name, min_x, min_y, max_x, max_y, coords) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (way_db_id, way["feature_type"], way["sub_type"], way["name"], min_x, min_y, max_x, max_y, blob)
        )
        way_db_id += 1
        
    # Calculate global bounding box
    cursor.execute("SELECT MIN(min_x), MIN(min_y), MAX(max_x), MAX(max_y) FROM ways WHERE feature_type='coastline'")
    global_bbox = cursor.fetchone()
    if not global_bbox or global_bbox[0] is None:
        cursor.execute("SELECT MIN(min_x), MIN(min_y), MAX(max_x), MAX(max_y) FROM ways")
        global_bbox = cursor.fetchone()
        
    if global_bbox and global_bbox[0] is not None:
        bbox_dict = {
            "min_x": global_bbox[0],
            "min_y": global_bbox[1],
            "max_x": global_bbox[2],
            "max_y": global_bbox[3]
        }
        cursor.execute("INSERT INTO config (key, value) VALUES (?, ?)", ("bbox", json.dumps(bbox_dict)))
        print(f"Global bounding box: {bbox_dict}")
        
    if progress_callback:
        progress_callback(98, "Creating database indexes...")
    print("Creating database indexes...")
    cursor.execute("CREATE INDEX idx_ways_bbox ON ways (min_x, max_x, min_y, max_y)")
    cursor.execute("CREATE INDEX idx_ways_type ON ways (feature_type)")
    cursor.execute("CREATE INDEX idx_places_coords ON places (x, y)")
    cursor.execute("CREATE INDEX idx_raw_nodes_coords ON raw_nodes (lat, lon)")
    
    # 5. Extract and Index Postcodes/Eircodes
    try:
        if progress_callback:
            progress_callback(99, "Extracting and indexing Eircodes...")
        print("Extracting and indexing Eircodes...")
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS postcodes (
            postcode TEXT,
            normalized_postcode TEXT PRIMARY KEY,
            x REAL,
            y REAL
        )
        """)
        
        postcode_data = {} # normalized_postcode -> (postcode, x, y)
        
        # Query all nodes with postcode from raw_nodes
        cursor.execute("SELECT lat, lon, tags FROM raw_nodes WHERE tags LIKE '%postcode%'")
        for lat, lon, tags_json in cursor.fetchall():
            try:
                tags = json.loads(tags_json)
                pc = tags.get("addr:postcode") or tags.get("postal_code")
                if pc:
                    pc = pc.strip()
                    norm_pc = pc.replace(" ", "").upper()
                    if norm_pc and norm_pc not in postcode_data:
                        x, y = project_mercator(lat, lon)
                        postcode_data[norm_pc] = (pc, x, y)
            except:
                pass
                
        # Query all ways with postcode from raw_ways
        cursor.execute("SELECT nodes, tags FROM raw_ways WHERE tags LIKE '%postcode%'")
        for nodes_blob, tags_json in cursor.fetchall():
            try:
                tags = json.loads(tags_json)
                pc = tags.get("addr:postcode") or tags.get("postal_code")
                if pc:
                    pc = pc.strip()
                    norm_pc = pc.replace(" ", "").upper()
                    if norm_pc and norm_pc not in postcode_data:
                        num_nodes = len(nodes_blob) // 8
                        node_ids = struct.unpack(f"<{num_nodes}q", nodes_blob)
                        if node_ids:
                            cursor.execute("SELECT lat, lon FROM raw_nodes WHERE id = ?", (node_ids[0],))
                            node_row = cursor.fetchone()
                            if node_row:
                                lat, lon = node_row
                                x, y = project_mercator(lat, lon)
                                postcode_data[norm_pc] = (pc, x, y)
            except:
                pass
                
        if postcode_data:
            cursor.executemany(
                "INSERT OR IGNORE INTO postcodes (postcode, normalized_postcode, x, y) VALUES (?, ?, ?, ?)",
                [(pc, npc, x, y) for npc, (pc, x, y) in postcode_data.items()]
            )
        
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_postcodes_normalized ON postcodes (normalized_postcode)")
    except Exception as e:
        print("Postcode indexing skipped/failed (not an error):", e)
    
    conn.commit()
    conn.close()
    
    if progress_callback:
        progress_callback(100, "Done!")
    print(f"Database saved successfully. DB Size on disk: {time.time() - db_start:.1f}s.")
    print(f"Total preprocessing finished in {time.time() - start_time:.1f}s.")

if __name__ == "__main__":
    import sys
    import os
    
    pbf = None
    db = None
    
    if len(sys.argv) > 1:
        pbf = sys.argv[1]
    if len(sys.argv) > 2:
        db = sys.argv[2]
        
    if not pbf:
        # Scan current directory for any .osm.pbf files
        pbf_files = [f for f in os.listdir(".") if f.endswith(".osm.pbf")]
        if len(pbf_files) == 1:
            pbf = pbf_files[0]
            print(f"Auto-detected PBF file: {pbf}")
        elif len(pbf_files) > 1:
            print("Multiple .osm.pbf files found in current directory:")
            for f in pbf_files:
                print(f"  {f}")
            print("\nPlease specify which one to process: python preprocess.py <file.osm.pbf> [output.db]")
            sys.exit(1)
        else:
            print("No .osm.pbf files found in the current directory.")
            print("Usage: python preprocess.py <file.osm.pbf> [output.db]")
            sys.exit(1)
            
    if not db:
        # Generate db path based on the input PBF filename
        base_name = os.path.splitext(pbf)[0]
        # If the base name ends with .osm, strip it too
        if base_name.endswith(".osm"):
            base_name = base_name[:-4]
        db = base_name + ".db"
        print(f"Target database file: {db}")
        
    run_preprocess(pbf, db)
