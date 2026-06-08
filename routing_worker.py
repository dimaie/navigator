# routing_worker.py

import sqlite3
import struct
import array
import math
import heapq
from PySide6.QtCore import QThread, Signal, QPointF
from utils import inverse_mercator


def get_ground_distance(pts):
    """
    Computes the true ground distance of a list of QPointF Mercator points
    by correcting the scale distortion at their latitude.
    """
    if len(pts) < 2:
        return 0.0
    total = 0.0
    for i in range(len(pts) - 1):
        p1, p2 = pts[i], pts[i+1]
        dx = p2.x() - p1.x()
        dy = p2.y() - p1.y()
        merc_dist = math.sqrt(dx*dx + dy*dy)
        y_mid = (p1.y() + p2.y()) / 2.0
        scale_factor = 1.0 / math.cosh(y_mid / 6378137.0)
        total += merc_dist * scale_factor
    return total


def get_look_ahead_point(pts, is_exiting, target_dist=25.0):
    """
    Finds a point along the list of QPointF Mercator points at a specified ground distance
    away from the start (for exiting leg) or end (for entering leg) of the points list.
    Used for look-ahead angle calculations to smooth out short intersection details.
    """
    if not pts:
        return None
    if len(pts) < 2:
        return pts[0]
        
    if is_exiting:
        accum = 0.0
        for i in range(len(pts) - 1):
            p1, p2 = pts[i], pts[i+1]
            dx = p2.x() - p1.x()
            dy = p2.y() - p1.y()
            segment_len = math.sqrt(dx*dx + dy*dy)
            y_mid = (p1.y() + p2.y()) / 2.0
            scale_factor = 1.0 / math.cosh(y_mid / 6378137.0)
            ground_len = segment_len * scale_factor
            
            if accum + ground_len >= target_dist:
                t = (target_dist - accum) / ground_len
                t = max(0.0, min(1.0, t))
                return QPointF(p1.x() + t*dx, p1.y() + t*dy)
            accum += ground_len
        return pts[-1]
    else:
        accum = 0.0
        for i in range(len(pts) - 2, -1, -1):
            p1, p2 = pts[i], pts[i+1]
            dx = p2.x() - p1.x()
            dy = p2.y() - p1.y()
            segment_len = math.sqrt(dx*dx + dy*dy)
            y_mid = (p1.y() + p2.y()) / 2.0
            scale_factor = 1.0 / math.cosh(y_mid / 6378137.0)
            ground_len = segment_len * scale_factor
            
            if accum + ground_len >= target_dist:
                t = (target_dist - accum) / ground_len
                t = max(0.0, min(1.0, t))
                return QPointF(p2.x() - t*dx, p2.y() - t*dy)
            accum += ground_len
        return pts[0]


def project_point_to_segment(p, a, b):
    ab_x = b.x() - a.x()
    ab_y = b.y() - a.y()
    ap_x = p.x() - a.x()
    ap_y = p.y() - a.y()
    
    ab2 = ab_x*ab_x + ab_y*ab_y
    if ab2 < 1e-9:
        return a, ap_x*ap_x + ap_y*ap_y
        
    t = (ap_x*ab_x + ap_y*ab_y) / ab2
    t = max(0.0, min(1.0, t))
    
    proj = QPointF(a.x() + t*ab_x, a.y() + t*ab_y)
    dx = p.x() - proj.x()
    dy = p.y() - proj.y()
    return proj, dx*dx + dy*dy

def trim_route(pts, start_pt, end_pt):
    if not pts or len(pts) < 2:
        return pts
        
    # 1. Trim start
    min_dist2 = float('inf')
    best_idx = 0
    best_proj = pts[0]
    for i in range(len(pts) - 1):
        proj, d2 = project_point_to_segment(start_pt, pts[i], pts[i+1])
        if d2 < min_dist2:
            min_dist2 = d2
            best_idx = i
            best_proj = proj
            
    start_trimmed = [start_pt, best_proj] + pts[best_idx+1:]
    
    # 2. Trim end on start-trimmed path
    min_dist2 = float('inf')
    best_idx = 0
    best_proj = start_trimmed[-1]
    for i in range(len(start_trimmed) - 1):
        proj, d2 = project_point_to_segment(end_pt, start_trimmed[i], start_trimmed[i+1])
        if d2 < min_dist2:
            min_dist2 = d2
            best_idx = i
            best_proj = proj
            
    end_trimmed = start_trimmed[:best_idx+1] + [best_proj, end_pt]
    
    # De-duplicate adjacent points
    final_pts = []
    for p in end_trimmed:
        if not final_pts:
            final_pts.append(p)
        else:
            dx = p.x() - final_pts[-1].x()
            dy = p.y() - final_pts[-1].y()
            if dx*dx + dy*dy > 1e-4:
                final_pts.append(p)
    return final_pts

def find_route_astar(start_coord, end_coord, routing_graph, routing_nodes_coords, db_path, profile):
    """
    Finds the shortest/fastest path between start_coord and end_coord in Web Mercator coordinates.
    - profile: dict containing weighting and speed parameters.
    Returns (route_points, total_distance_meters, total_time_seconds)
    """
    # Direct path routing for very close points
    direct_meters = get_ground_distance([start_coord, end_coord])
    if direct_meters < 100.0:
        walk_speed_mps = 5.0 / 3.6
        dest_lat, dest_lon = inverse_mercator(end_coord.x(), end_coord.y())
        directions = [f"- Drive for {int(direct_meters)} m to destination at ({dest_lat:.5f}, {dest_lon:.5f})"]
        total_seconds = direct_meters / walk_speed_mps
        dur_mins = total_seconds / 60.0
        dur_str = f"{max(1, int(dur_mins))}m" if dur_mins < 60.0 else f"{int(dur_mins // 60)}h {int(dur_mins % 60)}m"
        len_str = f"{direct_meters/1000.0:.1f} km" if direct_meters >= 1000.0 else f"{int(direct_meters)} m"
        directions.append(f"\nTotal route length: {len_str} (Estimated travel time: {dur_str})")
        return [start_coord, end_coord], direct_meters, total_seconds, directions

    # Parse profile values
    distance_weight = profile.get("distance_weight", 1.0)
    speed_weight = profile.get("speed_weight", 0.0)
    prohibited_links = set(profile.get("prohibited_links", []))
    speeds = profile.get("speeds", {})
    multipliers = profile.get("multipliers", {})
    
    # Compute min_cost_per_meter for A* heuristic admissibility
    min_cost_per_meter = float('inf')
    all_road_types = [
        'motorway', 'motorway_link', 'trunk', 'trunk_link', 'primary', 'primary_link',
        'secondary', 'secondary_link', 'tertiary', 'tertiary_link', 'unclassified',
        'residential', 'living_street', 'service', 'track', 'path', 'footway', 'cycleway', 'pedestrian'
    ]
    for rtype in all_road_types:
        if rtype in prohibited_links:
            continue
        mult = multipliers.get(rtype, 1.0)
        speed_kmh = speeds.get(rtype, 50)
        speed_mps = speed_kmh / 3.6
        cost_factor = mult * (distance_weight + speed_weight / speed_mps)
        if cost_factor < min_cost_per_meter:
            min_cost_per_meter = cost_factor
            
    if min_cost_per_meter == float('inf') or min_cost_per_meter < 1e-9:
        min_cost_per_meter = 1.0
        
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        
        # Helper to find nearest edge by querying local routing nodes
        def find_nearest_edge_local(coord):
            cx, cy = coord.x(), coord.y()
            
            # 1. Find nodes within 2km, expanding up to 200km if none found
            for radius in (2000, 10000, 50000, 200000):
                cursor.execute("""
                    SELECT id FROM routing_nodes 
                    WHERE x BETWEEN ? AND ? AND y BETWEEN ? AND ?
                """, (cx - radius, cx + radius, cy - radius, cy + radius))
                node_ids = [r[0] for r in cursor.fetchall()]
                if node_ids:
                    break
            else:
                return None, None, 0, []
                
            # 2. Query edges connected to these nodes
            placeholders = ",".join("?" for _ in node_ids)
            cursor.execute(f"""
                SELECT id, from_node, to_node, length, way_type, name, oneway, coords 
                FROM routing_edges
                WHERE from_node IN ({placeholders}) OR to_node IN ({placeholders})
            """, node_ids + node_ids)
            
            min_dist2 = float('inf')
            best_edge = None
            best_proj = None
            best_proj_idx = 0
            best_pts = []
            
            for row in cursor.fetchall():
                eid, f_node, t_node, length, wtype, name, oneway, blob = row
                coords = array.array('d', blob)
                pts = [QPointF(coords[i], coords[i+1]) for i in range(0, len(coords), 2)]
                
                for i in range(len(pts) - 1):
                    proj, d2 = project_point_to_segment(coord, pts[i], pts[i+1])
                    if d2 < min_dist2:
                        min_dist2 = d2
                        best_edge = {
                            "id": eid, "from_node": f_node, "to_node": t_node,
                            "length": length, "way_type": wtype, "name": name, "oneway": oneway
                        }
                        best_proj = proj
                        best_proj_idx = i
                        best_pts = pts
                        
            return best_edge, best_proj, best_proj_idx, best_pts

        # 1. Snap coordinates to nearest edges
        edge_a, proj_a, idx_a, pts_a = find_nearest_edge_local(start_coord)
        edge_b, proj_b, idx_b, pts_b = find_nearest_edge_local(end_coord)
        
        if not edge_a or not edge_b:
            return None, 0.0, 0.0, []
            
        sx, sy = proj_a.x(), proj_a.y()
        ex, ey = proj_b.x(), proj_b.y()
        
        # 2. In-memory virtual graph creation
        # Clone graph and coords to prevent modifying cache
        g = {k: list(v) for k, v in routing_graph.items()}
        coords = dict(routing_nodes_coords)
        
        # Set coordinates for virtual nodes
        coords[-1] = (sx, sy)
        coords[-2] = (ex, ey)
        
        g[-1] = []
        g[-2] = []
        
        # Helper to calculate split ratios for edge lengths
        def get_split_lengths(edge, pts, proj, idx):
            def dist_m(p1, p2):
                return math.sqrt((p1.x() - p2.x())**2 + (p1.y() - p2.y())**2)
                
            m_total = sum(dist_m(pts[i], pts[i+1]) for i in range(len(pts) - 1))
            if m_total < 1e-9:
                return 0.0, 0.0
                
            m1 = sum(dist_m(pts[i], pts[i+1]) for i in range(idx)) + dist_m(pts[idx], proj)
            m2 = dist_m(proj, pts[idx+1]) + sum(dist_m(pts[i], pts[i+1]) for i in range(idx+1, len(pts) - 1))
            
            r1 = m1 / m_total
            r2 = m2 / m_total
            return r1 * edge["length"], r2 * edge["length"]

        def is_a_before_b_on_edge(pts, proj_a, idx_a, proj_b, idx_b):
            if idx_a < idx_b:
                return True
            if idx_a > idx_b:
                return False
            def dist2(p1, p2):
                return (p1.x() - p2.x())**2 + (p1.y() - p2.y())**2
            return dist2(pts[idx_a], proj_a) < dist2(pts[idx_a], proj_b)

        # Populate virtual node connections
        if edge_a["id"] == edge_b["id"]:
            # Same edge routing
            u, v = edge_a["from_node"], edge_a["to_node"]
            oneway = edge_a["oneway"]
            l_u_start, l_v_start = get_split_lengths(edge_a, pts_a, proj_a, idx_a)
            l_u_end, l_v_end = get_split_lengths(edge_b, pts_b, proj_b, idx_b)
            dist_ab = abs(l_u_start - l_u_end)
            a_before_b = is_a_before_b_on_edge(pts_a, proj_a, idx_a, proj_b, idx_b)
            
            # Check if direct line path is shorter than theSnapped detour
            d_start_proj = get_ground_distance([start_coord, proj_a])
            d_end_proj = get_ground_distance([end_coord, proj_b])
            y_mid_ab = (proj_a.y() + proj_b.y()) / 2.0
            dist_ab_ground = dist_ab / math.cosh(y_mid_ab / 6378137.0)
            detour_meters = d_start_proj + dist_ab_ground + d_end_proj
            if direct_meters < detour_meters:
                walk_speed_mps = 5.0 / 3.6
                dest_lat, dest_lon = inverse_mercator(end_coord.x(), end_coord.y())
                directions = [f"- Drive for {int(direct_meters)} m to destination at ({dest_lat:.5f}, {dest_lon:.5f})"]
                total_seconds = direct_meters / walk_speed_mps
                dur_mins = total_seconds / 60.0
                dur_str = f"{max(1, int(dur_mins))}m" if dur_mins < 60.0 else f"{int(dur_mins // 60)}h {int(dur_mins % 60)}m"
                len_str = f"{direct_meters/1000.0:.1f} km" if direct_meters >= 1000.0 else f"{int(direct_meters)} m"
                directions.append(f"\nTotal route length: {len_str} (Estimated travel time: {dur_str})")
                return [start_coord, end_coord], direct_meters, total_seconds, directions
            
            if oneway == 0:
                g[-1].append((-2, dist_ab, edge_a["id"], edge_a["way_type"], edge_a["name"]))
                g[-2].append((-1, dist_ab, edge_a["id"], edge_a["way_type"], edge_a["name"]))
            elif oneway == 1:
                if a_before_b:
                    g[-1].append((-2, dist_ab, edge_a["id"], edge_a["way_type"], edge_a["name"]))
                else:
                    g[-1].append((v, l_v_start, edge_a["id"], edge_a["way_type"], edge_a["name"]))
                    g[u].append((-2, l_u_end, edge_a["id"], edge_a["way_type"], edge_a["name"]))
            elif oneway == -1:
                if not a_before_b:
                    g[-1].append((-2, dist_ab, edge_a["id"], edge_a["way_type"], edge_a["name"]))
                else:
                    g[-1].append((u, l_u_start, edge_a["id"], edge_a["way_type"], edge_a["name"]))
                    g[v].append((-2, l_v_end, edge_a["id"], edge_a["way_type"], edge_a["name"]))
        else:
            # Different edges routing
            l_u_start, l_v_start = get_split_lengths(edge_a, pts_a, proj_a, idx_a)
            u_a, v_a = edge_a["from_node"], edge_a["to_node"]
            oneway_a = edge_a["oneway"]
            
            if oneway_a == 0:
                g[-1].append((u_a, l_u_start, edge_a["id"], edge_a["way_type"], edge_a["name"]))
                g[u_a].append((-1, l_u_start, edge_a["id"], edge_a["way_type"], edge_a["name"]))
                g[-1].append((v_a, l_v_start, edge_a["id"], edge_a["way_type"], edge_a["name"]))
                g[v_a].append((-1, l_v_start, edge_a["id"], edge_a["way_type"], edge_a["name"]))
            elif oneway_a == 1:
                g[-1].append((v_a, l_v_start, edge_a["id"], edge_a["way_type"], edge_a["name"]))
                g[u_a].append((-1, l_u_start, edge_a["id"], edge_a["way_type"], edge_a["name"]))
            elif oneway_a == -1:
                g[-1].append((u_a, l_u_start, edge_a["id"], edge_a["way_type"], edge_a["name"]))
                g[v_a].append((-1, l_v_start, edge_a["id"], edge_a["way_type"], edge_a["name"]))
                
            l_u_end, l_v_end = get_split_lengths(edge_b, pts_b, proj_b, idx_b)
            u_b, v_b = edge_b["from_node"], edge_b["to_node"]
            oneway_b = edge_b["oneway"]
            
            if oneway_b == 0:
                g[-2].append((u_b, l_u_end, edge_b["id"], edge_b["way_type"], edge_b["name"]))
                g[u_b].append((-2, l_u_end, edge_b["id"], edge_b["way_type"], edge_b["name"]))
                g[-2].append((v_b, l_v_end, edge_b["id"], edge_b["way_type"], edge_b["name"]))
                g[v_b].append((-2, l_v_end, edge_b["id"], edge_b["way_type"], edge_b["name"]))
            elif oneway_b == 1:
                g[u_b].append((-2, l_u_end, edge_b["id"], edge_b["way_type"], edge_b["name"]))
                g[-2].append((v_b, l_v_end, edge_b["id"], edge_b["way_type"], edge_b["name"]))
            elif oneway_b == -1:
                g[v_b].append((-2, l_v_end, edge_b["id"], edge_b["way_type"], edge_b["name"]))
                g[-2].append((u_b, l_u_end, edge_b["id"], edge_b["way_type"], edge_b["name"]))

        # Heuristic function using dynamically computed min_cost_per_meter
        def get_heuristic(node_id):
            if node_id not in coords:
                return 0.0
            nx, ny = coords[node_id]
            dist = math.sqrt((nx - ex)**2 + (ny - ey)**2)
            return dist * min_cost_per_meter
            
        g_score = {-1: 0.0}
        parents = {} # child_node -> (parent_node, edge_id)
        
        # Priority queue stores (f_score, node_id)
        pq = [(get_heuristic(-1), -1)]
        visited = set()
        found = False
        
        while pq:
            f, u = heapq.heappop(pq)
            
            if u in visited:
                continue
            visited.add(u)
            
            if u == -2:
                found = True
                break
                
            if u not in g:
                continue
                
            for v, length, edge_id, way_type, name in g[u]:
                if v in visited:
                    continue
                if way_type in prohibited_links:
                    continue
                    
                multiplier = multipliers.get(way_type, 1.0)
                speed_kmh = speeds.get(way_type, 50)
                speed_mps = speed_kmh / 3.6
                
                # Dynamic formula-driven costing (selection of roads, distance, speed/time)
                edge_cost = length * multiplier * (distance_weight + speed_weight / speed_mps)
                    
                tentative_g = g_score[u] + edge_cost
                if tentative_g < g_score.get(v, float('inf')):
                    g_score[v] = tentative_g
                    parents[v] = (u, edge_id)
                    f_score = tentative_g + get_heuristic(v)
                    heapq.heappush(pq, (f_score, v))
                    
        if not found:
            return None, 0.0, 0.0, []
            
        # Reconstruct transitions sequence (from_node, to_node, edge_id)
        transitions = []
        curr = -2
        while curr != -1:
            parent, edge_id = parents[curr]
            transitions.append((parent, curr, edge_id))
            curr = parent
        transitions.reverse()
        
        # Fetch edge geometries from database in chunks
        edge_ids = [t[2] for t in transitions]
        edge_data = {}
        fallback_speeds = {
            'motorway': 120, 'motorway_link': 80,
            'trunk': 100, 'trunk_link': 60,
            'primary': 80, 'primary_link': 50,
            'secondary': 80, 'secondary_link': 50,
            'tertiary': 60, 'tertiary_link': 40,
            'unclassified': 50, 'residential': 30,
            'living_street': 20, 'service': 10
        }
        
        total_meters = 0.0
        total_seconds = 0.0
        
        chunk_size = 200
        for idx in range(0, len(edge_ids), chunk_size):
            chunk = edge_ids[idx:idx+chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            cursor.execute(f"SELECT id, from_node, to_node, length, way_type, name, is_roundabout, coords FROM routing_edges WHERE id IN ({placeholders})", chunk)
            
            for row in cursor.fetchall():
                eid, f_node, t_node, length, wtype, rname, is_rab, blob = row
                coords = array.array('d', blob)
                pts = [QPointF(coords[i], coords[i+1]) for i in range(0, len(coords), 2)]
                edge_data[eid] = {
                    "from_node": f_node, "to_node": t_node, "pts": pts, 
                    "length": length, "way_type": wtype, "name": rname, "is_roundabout": is_rab
                }
                
        route_points = []
        
        for u, v, edge_id in transitions:
            if edge_id not in edge_data:
                continue
            data = edge_data[edge_id]
            f_node = data["from_node"]
            t_node = data["to_node"]
            pts = data["pts"]
            length = data["length"]
            wtype = data["way_type"]
            
            sub_pts = []
            if u == -1 and v == -2:
                # Same edge
                a_before_b = is_a_before_b_on_edge(pts, proj_a, idx_a, proj_b, idx_b)
                if a_before_b:
                    sub_pts = [proj_a] + pts[idx_a + 1:idx_b + 1] + [proj_b]
                else:
                    sub_pts = [proj_a] + list(reversed(pts[idx_b + 1:idx_a + 1])) + [proj_b]
            elif u == -1:
                # Start edge split
                if v == t_node:
                    sub_pts = [proj_a] + pts[idx_a + 1:]
                else:
                    sub_pts = [proj_a] + list(reversed(pts[:idx_a + 1]))
            elif v == -2:
                # End edge split
                if u == f_node:
                    sub_pts = pts[:idx_b + 1] + [proj_b]
                else:
                    sub_pts = list(reversed(pts[idx_b + 1:])) + [proj_b]
            else:
                # Intermediate edge
                if u == f_node and v == t_node:
                    sub_pts = pts
                else:
                    sub_pts = list(reversed(pts))
                    
            # Compute total distance and duration (using traversed path segment coords for accuracy)
            ground_length = get_ground_distance(sub_pts)
            total_meters += ground_length
            speed_kmh = speeds.get(wtype) or fallback_speeds.get(wtype, 50)
            total_seconds += ground_length / (speed_kmh / 3.6)
                    
            for pt in sub_pts:
                if not route_points or route_points[-1] != pt:
                    route_points.append(pt)
                    
        # Ensure we connect exactly to the clicked coordinates start_coord and end_coord at the boundaries
        if route_points:
            # Prepend start_coord if not already present
            if route_points[0] != start_coord:
                route_points.insert(0, start_coord)
            # Append end_coord if not already present
            if route_points[-1] != end_coord:
                route_points.append(end_coord)
        else:
            route_points = [start_coord, end_coord]
            
        # Leg Grouping
        legs = []
        for u, v, edge_id in transitions:
            if edge_id not in edge_data:
                continue
            data = edge_data[edge_id]
            is_rab = data.get("is_roundabout", 0) == 1
            name = data.get("name") or ""
            norm_name = name.strip()
            if is_rab:
                norm_name = "roundabout"
            elif not norm_name:
                norm_name = "unnamed road"
                
            length = data["length"]
            pts = data["pts"]
            
            sub_pts = []
            if u == -1 and v == -2:
                a_before_b = is_a_before_b_on_edge(pts, proj_a, idx_a, proj_b, idx_b)
                if a_before_b:
                    sub_pts = [proj_a] + pts[idx_a + 1:idx_b + 1] + [proj_b]
                else:
                    sub_pts = [proj_a] + list(reversed(pts[idx_b + 1:idx_a + 1])) + [proj_b]
            elif u == -1:
                if v == data["to_node"]:
                    sub_pts = [proj_a] + pts[idx_a + 1:]
                else:
                    sub_pts = [proj_a] + list(reversed(pts[:idx_a + 1]))
            elif v == -2:
                if u == data["from_node"]:
                    sub_pts = pts[:idx_b + 1] + [proj_b]
                else:
                    sub_pts = list(reversed(pts[idx_b + 1:])) + [proj_b]
            else:
                if u == data["from_node"] and v == data["to_node"]:
                    sub_pts = pts
                else:
                    sub_pts = list(reversed(pts))
                    
            ground_length = get_ground_distance(sub_pts)
            if legs and legs[-1]["is_roundabout"] == is_rab and (is_rab or legs[-1]["name"] == norm_name):
                legs[-1]["length"] += ground_length
                for pt in sub_pts:
                    if not legs[-1]["pts"] or legs[-1]["pts"][-1] != pt:
                        legs[-1]["pts"].append(pt)
                if is_rab:
                    legs[-1]["nodes"].append(v)
            else:
                legs.append({
                    "name": norm_name,
                    "length": ground_length,
                    "pts": list(sub_pts),
                    "is_roundabout": is_rab,
                    "nodes": [u, v] if is_rab else []
                })
                
        # Generate directions
        directions = []
        if legs:
            for i in range(len(legs)):
                leg = legs[i]
                name = leg["name"]
                length = leg["length"]
                pts = leg["pts"]
                
                if leg.get("is_roundabout", False):
                    # Count exits
                    exit_count = 1
                    rab_nodes = leg["nodes"]
                    proh_list = list(prohibited_links)
                    placeholders = ",".join("?" for _ in proh_list)
                    
                    # Skip the entry node (rab_nodes[0]) when counting intermediate exits
                    for node in rab_nodes[1:-1]:
                        query = """
                            SELECT COUNT(*) FROM routing_edges 
                            WHERE (
                                (from_node = ? AND oneway >= 0) OR 
                                (to_node = ? AND oneway <= 0)
                            ) 
                            AND is_roundabout = 0
                        """
                        if proh_list:
                            query += f" AND way_type NOT IN ({placeholders})"
                        
                        cursor.execute(query, [node, node] + proh_list)
                        count = cursor.fetchone()[0]
                        exit_count += count
                        
                    if exit_count == 1:
                        suffix = "1st"
                    elif exit_count == 2:
                        suffix = "2nd"
                    elif exit_count == 3:
                        suffix = "3rd"
                    else:
                        suffix = f"{exit_count}th"
                        
                    next_name = legs[i+1]["name"] if i < len(legs) - 1 else "destination"
                    exit_pt = pts[-1]
                    exit_lat, exit_lon = inverse_mercator(exit_pt.x(), exit_pt.y())
                    
                    direction = f"- At the roundabout, take the {suffix} exit onto {next_name} at ({exit_lat:.5f}, {exit_lon:.5f})"
                    directions.append(direction)
                else:
                    if i == len(legs) - 1:
                        dest_pt = pts[-1]
                        dest_lat, dest_lon = inverse_mercator(dest_pt.x(), dest_pt.y())
                        direction = f"- Drive on {name} for {int(length)} m to destination at ({dest_lat:.5f}, {dest_lon:.5f})"
                        directions.append(direction)
                    else:
                        next_leg = legs[i+1]
                        next_name = next_leg["name"]
                        J = pts[-1]
                        
                        P_in = get_look_ahead_point(pts, is_exiting=False, target_dist=25.0)
                            
                        next_pts = next_leg["pts"]
                        P_out = get_look_ahead_point(next_pts, is_exiting=True, target_dist=25.0)
                            
                        v_in_x = J.x() - P_in.x()
                        v_in_y = J.y() - P_in.y()
                        v_out_x = P_out.x() - J.x()
                        v_out_y = P_out.y() - J.y()
                        
                        len_in = math.sqrt(v_in_x**2 + v_in_y**2)
                        len_out = math.sqrt(v_out_x**2 + v_out_y**2)
                        
                        if len_in < 1e-9 or len_out < 1e-9:
                            turn_type = "continue straight"
                        else:
                            dx1, dy1 = v_in_x / len_in, v_in_y / len_in
                            dx2, dy2 = v_out_x / len_out, v_out_y / len_out
                            
                            dot = dx1 * dx2 + dy1 * dy2
                            cross = dx1 * dy2 - dy1 * dx2
                            angle = math.degrees(math.atan2(cross, dot))
                            
                            if -10 <= angle <= 10:
                                turn_type = "continue straight"
                            elif 10 < angle <= 45:
                                turn_type = "bear left"
                            elif 45 < angle <= 135:
                                turn_type = "turn left"
                            elif angle > 135:
                                turn_type = "make a sharp left turn"
                            elif -45 <= angle < -10:
                                turn_type = "bear right"
                            elif -135 <= angle < -45:
                                turn_type = "turn right"
                            else:
                                turn_type = "make a sharp right turn"
                                
                        junc_lat, junc_lon = inverse_mercator(J.x(), J.y())
                        if turn_type == "continue straight":
                            direction = f"- Drive on {name} for {int(length)} m and continue onto {next_name} at ({junc_lat:.5f}, {junc_lon:.5f})"
                        else:
                            direction = f"- Drive on {name} for {int(length)} m and {turn_type} to {next_name} at ({junc_lat:.5f}, {junc_lon:.5f})"
                        directions.append(direction)
        else:
            directions = ["- Proceed to destination"]
            
        dur_mins = total_seconds / 60.0
        dur_str = f"{max(1, int(dur_mins))}m" if dur_mins < 60.0 else f"{int(dur_mins // 60)}h {int(dur_mins % 60)}m"
        len_str = f"{total_meters/1000.0:.1f} km" if total_meters >= 1000.0 else f"{int(total_meters)} m"
        directions.append(f"\nTotal route length: {len_str} (Estimated travel time: {dur_str})")
        return route_points, total_meters, total_seconds, directions
    finally:
        conn.close()


class RoutingWorker(QThread):
    """
    Background worker thread that lazily loads the routing graph on-demand
    and calculates path calculations using A* search.
    """
    route_completed = Signal(object, float, float, object, object, str, list) # points, distance, duration, graph, coords, used_profile, directions
    route_failed = Signal(str)
    graph_loaded = Signal(object, object) # Emitted immediately when graph completes lazy-loading
    
    def __init__(self, start_coord, end_coord, routing_graph, routing_nodes_coords, db_path, profile_name, routing_profiles):
        super().__init__()
        self.start_coord = start_coord
        self.end_coord = end_coord
        self.routing_graph = routing_graph
        self.routing_nodes_coords = routing_nodes_coords
        self.db_path = db_path
        self.profile_name = profile_name
        self.routing_profiles = routing_profiles
        
    def run(self):
        try:
            # Lazy load the routing graph if not currently loaded in memory
            if not self.routing_graph or not self.routing_nodes_coords:
                print("Lazy loading routing graph from SQLite database...")
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                # Retrieve nodes
                cursor.execute("SELECT id, x, y FROM routing_nodes")
                loaded_coords = {}
                for nid, rx, ry in cursor.fetchall():
                    loaded_coords[nid] = (rx, ry)
                    
                # Retrieve edges
                cursor.execute("SELECT id, from_node, to_node, length, way_type, name, oneway FROM routing_edges")
                loaded_graph = {}
                for edge_id, u, v, length, wtype, rname, oneway in cursor.fetchall():
                    if u not in loaded_graph:
                        loaded_graph[u] = []
                    if v not in loaded_graph:
                        loaded_graph[v] = []
                        
                    # oneway: 0 = two-way, 1 = u->v, -1 = v->u
                    if oneway == 0:
                        loaded_graph[u].append((v, length, edge_id, wtype, rname))
                        loaded_graph[v].append((u, length, edge_id, wtype, rname))
                    elif oneway == 1:
                        loaded_graph[u].append((v, length, edge_id, wtype, rname))
                    elif oneway == -1:
                        loaded_graph[v].append((u, length, edge_id, wtype, rname))
                        
                conn.close()
                self.routing_graph = loaded_graph
                self.routing_nodes_coords = loaded_coords
                print(f"Lazy loading complete: {len(self.routing_nodes_coords)} nodes cached.")
                self.graph_loaded.emit(self.routing_graph, self.routing_nodes_coords)
                
            curr_profile_name = self.profile_name
            while curr_profile_name:
                profile_dict = self.routing_profiles.get(curr_profile_name)
                if not profile_dict:
                    break
                    
                print(f"Attempting pathfinding with profile: {curr_profile_name}")
                res = find_route_astar(
                    self.start_coord, self.end_coord,
                    self.routing_graph, self.routing_nodes_coords,
                    self.db_path, profile_dict
                )
                pts, dist, duration, directions = res
                if pts:
                    self.route_completed.emit(pts, dist, duration, self.routing_graph, self.routing_nodes_coords, curr_profile_name, directions)
                    return
                    
                # Try fallback profile
                curr_profile_name = profile_dict.get("fallback_profile")
                
            self.route_failed.emit("No route could be found between the selected points using the selected profile or its fallbacks.")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.route_failed.emit(f"Routing error: {str(e)}")
