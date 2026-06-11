# routing_worker.py

import sqlite3
import struct
import array
import math
import heapq
from dataclasses import dataclass, field
from PySide6.QtCore import QThread, Signal, QPointF
from utils import inverse_mercator


@dataclass
class ComputedRoute:
    """
    Encapsulates the complete result of a successful A* pathfinding operation.

    Attributes:
        points:       Ordered list of QPointF Web Mercator coordinates forming the route polyline.
        distance_m:   Total route length in metres.
        duration_s:   Estimated travel time in seconds.
        directions:   Human-readable turn-by-turn instruction strings.
        steps:        Structured per-step metadata for TTS and HUD rendering.
                      Each entry is a dict with keys:
                        type            – "turn" | "roundabout" | "continue" | "destination"
                        angle           – signed float degrees; negative=right, positive=left, 0=straight
                        junction_choices– int: accessible outgoing roads at the junction (excl. arrival)
                                          1 = forced (no real choice), >1 = genuine decision
                        exit_number     – int: which exit to take (roundabout only)
                        total_exits     – int: total exits on the roundabout ring (roundabout only)
                        junc_pt         – QPointF: Mercator coordinates of the junction / exit point
                        speakable       – bool: True when TTS should announce this step
                        speakable_action– str: ready-to-speak instruction text
                        driving_side    – str: "left" | "right" from the active profile
        profile_name: Name of the routing profile (or fallback) that produced this route.
    """
    points:       list = field(default_factory=list)   # list[QPointF]
    distance_m:   float = 0.0
    duration_s:   float = 0.0
    directions:   list = field(default_factory=list)   # list[str]
    steps:        list = field(default_factory=list)   # list[dict]
    profile_name: str  = ""


class GraphOverlay:
    def __init__(self, base_graph, g_minus_1, g_minus_2, g_modified):
        self.base_graph = base_graph
        self.g_minus_1 = g_minus_1
        self.g_minus_2 = g_minus_2
        self.g_modified = g_modified

    def __contains__(self, key):
        if key == -1 or key == -2:
            return True
        if key in self.g_modified:
            return True
        return key in self.base_graph

    def __getitem__(self, key):
        if key == -1:
            return self.g_minus_1
        if key == -2:
            return self.g_minus_2
        if key in self.g_modified:
            return self.g_modified[key]
        return self.base_graph[key]

    def get(self, key, default=None):
        if key == -1:
            return self.g_minus_1
        if key == -2:
            return self.g_minus_2
        if key in self.g_modified:
            return self.g_modified[key]
        return self.base_graph.get(key, default)


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
    Returns a ComputedRoute on success, or None on failure.
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
        return ComputedRoute(
            points=[start_coord, end_coord],
            distance_m=direct_meters,
            duration_s=total_seconds,
            directions=directions,
        )

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
                SELECT id, from_node, to_node, length, way_type, name, oneway, is_roundabout, coords 
                FROM routing_edges
                WHERE from_node IN ({placeholders}) OR to_node IN ({placeholders})
            """, node_ids + node_ids)
            
            min_dist2 = float('inf')
            best_edge = None
            best_proj = None
            best_proj_idx = 0
            best_pts = []
            
            for row in cursor.fetchall():
                eid, f_node, t_node, length, wtype, name, oneway, is_rab, blob = row
                coords = array.array('d', blob)
                pts = [QPointF(coords[i], coords[i+1]) for i in range(0, len(coords), 2)]
                
                for i in range(len(pts) - 1):
                    proj, d2 = project_point_to_segment(coord, pts[i], pts[i+1])
                    if d2 < min_dist2:
                        min_dist2 = d2
                        best_edge = {
                            "id": eid, "from_node": f_node, "to_node": t_node,
                            "length": length, "way_type": wtype, "name": name, "oneway": oneway,
                            "is_roundabout": is_rab
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
        # Avoid cloning huge dictionaries
        g_minus_1 = []
        g_minus_2 = []
        g_modified = {}
        
        def add_edge_to_g(from_node, to_node, length, edge_id, way_type, name, is_rab):
            if from_node == -1:
                g_minus_1.append((to_node, length, edge_id, way_type, name, is_rab))
            elif from_node == -2:
                g_minus_2.append((to_node, length, edge_id, way_type, name, is_rab))
            else:
                if from_node not in g_modified:
                    g_modified[from_node] = list(routing_graph.get(from_node, []))
                g_modified[from_node].append((to_node, length, edge_id, way_type, name, is_rab))
                
        g = GraphOverlay(routing_graph, g_minus_1, g_minus_2, g_modified)
        coords_overlay = {-1: (sx, sy), -2: (ex, ey)}
        
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
                return ComputedRoute(
                    points=[start_coord, end_coord],
                    distance_m=direct_meters,
                    duration_s=total_seconds,
                    directions=directions,
                )
            
            if oneway == 0:
                add_edge_to_g(-1, -2, dist_ab, edge_a["id"], edge_a["way_type"], edge_a["name"], edge_a["is_roundabout"])
                add_edge_to_g(-2, -1, dist_ab, edge_a["id"], edge_a["way_type"], edge_a["name"], edge_a["is_roundabout"])
            elif oneway == 1:
                if a_before_b:
                    add_edge_to_g(-1, -2, dist_ab, edge_a["id"], edge_a["way_type"], edge_a["name"], edge_a["is_roundabout"])
                else:
                    add_edge_to_g(-1, v, l_v_start, edge_a["id"], edge_a["way_type"], edge_a["name"], edge_a["is_roundabout"])
                    add_edge_to_g(u, -2, l_u_end, edge_a["id"], edge_a["way_type"], edge_a["name"], edge_a["is_roundabout"])
            elif oneway == -1:
                if not a_before_b:
                    add_edge_to_g(-1, -2, dist_ab, edge_a["id"], edge_a["way_type"], edge_a["name"], edge_a["is_roundabout"])
                else:
                    add_edge_to_g(-1, u, l_u_start, edge_a["id"], edge_a["way_type"], edge_a["name"], edge_a["is_roundabout"])
                    add_edge_to_g(v, -2, l_v_end, edge_a["id"], edge_a["way_type"], edge_a["name"], edge_a["is_roundabout"])
        else:
            # Different edges routing
            l_u_start, l_v_start = get_split_lengths(edge_a, pts_a, proj_a, idx_a)
            u_a, v_a = edge_a["from_node"], edge_a["to_node"]
            oneway_a = edge_a["oneway"]
            
            if oneway_a == 0:
                add_edge_to_g(-1, u_a, l_u_start, edge_a["id"], edge_a["way_type"], edge_a["name"], edge_a["is_roundabout"])
                add_edge_to_g(u_a, -1, l_u_start, edge_a["id"], edge_a["way_type"], edge_a["name"], edge_a["is_roundabout"])
                add_edge_to_g(-1, v_a, l_v_start, edge_a["id"], edge_a["way_type"], edge_a["name"], edge_a["is_roundabout"])
                add_edge_to_g(v_a, -1, l_v_start, edge_a["id"], edge_a["way_type"], edge_a["name"], edge_a["is_roundabout"])
            elif oneway_a == 1:
                add_edge_to_g(-1, v_a, l_v_start, edge_a["id"], edge_a["way_type"], edge_a["name"], edge_a["is_roundabout"])
                add_edge_to_g(u_a, -1, l_u_start, edge_a["id"], edge_a["way_type"], edge_a["name"], edge_a["is_roundabout"])
            elif oneway_a == -1:
                add_edge_to_g(-1, u_a, l_u_start, edge_a["id"], edge_a["way_type"], edge_a["name"], edge_a["is_roundabout"])
                add_edge_to_g(v_a, -1, l_v_start, edge_a["id"], edge_a["way_type"], edge_a["name"], edge_a["is_roundabout"])
                
            l_u_end, l_v_end = get_split_lengths(edge_b, pts_b, proj_b, idx_b)
            u_b, v_b = edge_b["from_node"], edge_b["to_node"]
            oneway_b = edge_b["oneway"]
            
            if oneway_b == 0:
                add_edge_to_g(-2, u_b, l_u_end, edge_b["id"], edge_b["way_type"], edge_b["name"], edge_b["is_roundabout"])
                add_edge_to_g(u_b, -2, l_u_end, edge_b["id"], edge_b["way_type"], edge_b["name"], edge_b["is_roundabout"])
                add_edge_to_g(-2, v_b, l_v_end, edge_b["id"], edge_b["way_type"], edge_b["name"], edge_b["is_roundabout"])
                add_edge_to_g(v_b, -2, l_v_end, edge_b["id"], edge_b["way_type"], edge_b["name"], edge_b["is_roundabout"])
            elif oneway_b == 1:
                add_edge_to_g(u_b, -2, l_u_end, edge_b["id"], edge_b["way_type"], edge_b["name"], edge_b["is_roundabout"])
                add_edge_to_g(-2, v_b, l_v_end, edge_b["id"], edge_b["way_type"], edge_b["name"], edge_b["is_roundabout"])
            elif oneway_b == -1:
                add_edge_to_g(v_b, -2, l_v_end, edge_b["id"], edge_b["way_type"], edge_b["name"], edge_b["is_roundabout"])
                add_edge_to_g(-2, u_b, l_u_end, edge_b["id"], edge_b["way_type"], edge_b["name"], edge_b["is_roundabout"])

        # Heuristic function using dynamically computed min_cost_per_meter
        def get_heuristic(node_id):
            if node_id in coords_overlay:
                nx, ny = coords_overlay[node_id]
            elif node_id in routing_nodes_coords:
                nx, ny = routing_nodes_coords[node_id]
            else:
                return 0.0
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
                
            for v, length, edge_id, way_type, name, is_rab in g[u]:
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
            return None
            
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
        # Also capture the last edge id and the actual end node per leg for junction-choice counting.
        legs = []
        leg_last_edge_id = []  # parallel to legs: edge_id of the last transition in each leg
        leg_end_node_id  = []  # parallel to legs: the node at the end of each leg
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
            
            # Determine actual traversal direction to resolve end node
            # (virtual nodes -1, -2 never appear as the permanent junction end node)
            if v >= 0:
                real_end_node = v
            elif u >= 0:
                real_end_node = u
            else:
                real_end_node = -1   # start==-1, end==-2 (single-edge route)
            
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
                # Update last-edge info for this merged leg
                leg_last_edge_id[-1] = edge_id
                leg_end_node_id[-1]  = real_end_node
            else:
                legs.append({
                    "name": norm_name,
                    "length": ground_length,
                    "pts": list(sub_pts),
                    "is_roundabout": is_rab,
                    "nodes": [u, v] if is_rab else []
                })
                leg_last_edge_id.append(edge_id)
                leg_end_node_id.append(real_end_node)
                
        # Generate directions and structured step metadata
        directions = []
        steps = []
        driving_side = profile.get("driving_side", "left")
        
        def _count_junction_choices_mem(node_id, incoming_edge_id):
            """
            Counts genuine outgoing road choices at node_id using the in-memory
            adjacency list, excluding the incoming edge and roundabout/prohibited edges.
            No SQL round-trip needed — g already holds the full graph.
            """
            count = 0
            for (neighbor, _length, eid, way_type, _name, is_rab) in g.get(node_id, []):
                if eid == incoming_edge_id:
                    continue  # exclude the road we arrived on
                if way_type in prohibited_links:
                    continue
                if is_rab:
                    continue  # exclude roundabout arcs
                count += 1
            return count
        
        def _traverse_roundabout_ring_mem(entry_node):
            """
            BFS on the in-memory adjacency list, following only roundabout edges,
            to collect all node IDs on the full ring.
            No SQL queries needed — is_roundabout is pre-loaded in graph tuples.
            """
            visited = {entry_node}
            queue = [entry_node]
            while queue:
                node = queue.pop(0)
                for (neighbor, _length, eid, _wtype, _name, is_rab) in g.get(node, []):
                    if is_rab and neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)
            return visited

        def _count_roundabout_exits_mem(ring_nodes):
            """
            Counts non-roundabout, non-prohibited exit arms across all ring nodes
            using the in-memory adjacency list.
            """
            total = 0
            for node in ring_nodes:
                for (_neighbor, _length, eid, way_type, _name, is_rab) in g.get(node, []):
                    if way_type in prohibited_links:
                        continue
                    if not is_rab:
                        total += 1
            return max(total, 1)

        if legs:
            for i, leg in enumerate(legs):
                name = leg["name"]
                length = leg["length"]
                pts = leg["pts"]
                last_eid  = leg_last_edge_id[i]
                end_node  = leg_end_node_id[i]
                
                if leg.get("is_roundabout", False):
                    rab_nodes = leg["nodes"]
                    
                    # Exit number: count exits passed at intermediate nodes using in-memory graph
                    exit_count = 1
                    for node in rab_nodes[1:-1]:
                        for (_nb, _l, eid, wtype, _nm, is_rab) in g.get(node, []):
                            if wtype in prohibited_links:
                                continue
                            if not is_rab:
                                exit_count += 1
                    
                    # Total exits: BFS the full ring using in-memory graph
                    ring_nodes  = _traverse_roundabout_ring_mem(rab_nodes[0])
                    total_exits = _count_roundabout_exits_mem(ring_nodes)
                    
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
                    steps.append({
                        "type":             "roundabout",
                        "angle":            0.0,
                        "junction_choices": 0,
                        "exit_number":      exit_count,
                        "total_exits":      total_exits,
                        "junc_pt":          QPointF(exit_pt.x(), exit_pt.y()),
                        "speakable":        True,
                        "speakable_action": f"At the roundabout, take the {suffix} exit onto {next_name}",
                        "driving_side":     driving_side,
                    })
                else:
                    if i == len(legs) - 1:
                        dest_pt = pts[-1]
                        dest_lat, dest_lon = inverse_mercator(dest_pt.x(), dest_pt.y())
                        direction = f"- Drive on {name} for {int(length)} m to destination at ({dest_lat:.5f}, {dest_lon:.5f})"
                        directions.append(direction)
                        steps.append({
                            "type":             "destination",
                            "angle":            0.0,
                            "junction_choices": 0,
                            "exit_number":      0,
                            "total_exits":      0,
                            "junc_pt":          QPointF(dest_pt.x(), dest_pt.y()),
                            "speakable":        False,
                            "speakable_action": "Arrive at destination",
                            "driving_side":     driving_side,
                        })
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
                        
                        len_in  = math.sqrt(v_in_x**2 + v_in_y**2)
                        len_out = math.sqrt(v_out_x**2 + v_out_y**2)
                        
                        angle = 0.0
                        if len_in < 1e-9 or len_out < 1e-9:
                            turn_type = "continue straight"
                        else:
                            dx1, dy1 = v_in_x / len_in, v_in_y / len_in
                            dx2, dy2 = v_out_x / len_out, v_out_y / len_out
                            
                            dot   = dx1 * dx2 + dy1 * dy2
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
                        
                        # Count genuine outgoing choices at junction node J using in-memory graph
                        junction_choices = _count_junction_choices_mem(end_node, last_eid) \
                                           if end_node >= 0 else 1
                        
                        is_real_turn = (turn_type != "continue straight")
                        speakable    = is_real_turn and (junction_choices > 1)
                        step_type    = "turn" if is_real_turn else "continue"
                        
                        junc_lat, junc_lon = inverse_mercator(J.x(), J.y())
                        if turn_type == "continue straight":
                            direction = f"- Drive on {name} for {int(length)} m and continue onto {next_name} at ({junc_lat:.5f}, {junc_lon:.5f})"
                        else:
                            direction = f"- Drive on {name} for {int(length)} m and {turn_type} to {next_name} at ({junc_lat:.5f}, {junc_lon:.5f})"
                        directions.append(direction)
                        steps.append({
                            "type":             step_type,
                            "angle":            angle,
                            "junction_choices": junction_choices,
                            "exit_number":      0,
                            "total_exits":      0,
                            "junc_pt":          QPointF(J.x(), J.y()),
                            "speakable":        speakable,
                            "speakable_action": f"Drive on {name} for {int(length)} m and {turn_type} to {next_name}",
                            "driving_side":     driving_side,
                        })
        else:
            directions = ["- Proceed to destination"]
            
        dur_mins = total_seconds / 60.0
        dur_str = f"{max(1, int(dur_mins))}m" if dur_mins < 60.0 else f"{int(dur_mins // 60)}h {int(dur_mins % 60)}m"
        len_str = f"{total_meters/1000.0:.1f} km" if total_meters >= 1000.0 else f"{int(total_meters)} m"
        directions.append(f"\nTotal route length: {len_str} (Estimated travel time: {dur_str})")
        return ComputedRoute(
            points=route_points,
            distance_m=total_meters,
            duration_s=total_seconds,
            directions=directions,
            steps=steps,
        )
    finally:
        conn.close()


class RoutingWorker(QThread):
    """
    Background worker thread that lazily loads the routing graph on-demand
    and calculates path calculations using A* search.
    """
    route_completed = Signal(object, object, object)  # ComputedRoute, graph, coords
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
                cursor.execute("SELECT id, from_node, to_node, length, way_type, name, oneway, is_roundabout FROM routing_edges")
                loaded_graph = {}
                for edge_id, u, v, length, wtype, rname, oneway, is_rab in cursor.fetchall():
                    if u not in loaded_graph:
                        loaded_graph[u] = []
                    if v not in loaded_graph:
                        loaded_graph[v] = []
                        
                    # oneway: 0 = two-way, 1 = u->v, -1 = v->u
                    if oneway == 0:
                        loaded_graph[u].append((v, length, edge_id, wtype, rname, is_rab))
                        loaded_graph[v].append((u, length, edge_id, wtype, rname, is_rab))
                    elif oneway == 1:
                        loaded_graph[u].append((v, length, edge_id, wtype, rname, is_rab))
                    elif oneway == -1:
                        loaded_graph[v].append((u, length, edge_id, wtype, rname, is_rab))
                        
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
                route = find_route_astar(
                    self.start_coord, self.end_coord,
                    self.routing_graph, self.routing_nodes_coords,
                    self.db_path, profile_dict
                )
                if route is not None:
                    route.profile_name = curr_profile_name
                    self.route_completed.emit(route, self.routing_graph, self.routing_nodes_coords)
                    return
                    
                # Try fallback profile
                curr_profile_name = profile_dict.get("fallback_profile")
                
            self.route_failed.emit("No route could be found between the selected points using the selected profile or its fallbacks.")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.route_failed.emit(f"Routing error: {str(e)}")
