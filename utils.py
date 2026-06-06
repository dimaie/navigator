# utils.py

import math

def project_mercator(lat, lon):
    """
    Projects latitude and longitude (degrees) to Web Mercator EPSG:3857 coordinates (meters).
    """
    x = lon * (math.pi / 180.0) * 6378137.0
    lat = max(-85.05112878, min(85.05112878, lat))
    lat_rad = lat * (math.pi / 180.0)
    y = math.log(math.tan(math.pi / 4.0 + lat_rad / 2.0)) * 6378137.0
    return x, y


def inverse_mercator(x, y):
    """
    Projects Web Mercator coordinates (meters) back to latitude and longitude (degrees).
    """
    lon = x / (6378137.0 * (math.pi / 180.0))
    lat_rad = 2.0 * math.atan(math.exp(y / 6378137.0)) - math.pi / 2.0
    lat = lat_rad * (180.0 / math.pi)
    return lat, lon


def simplify_radial(points, min_dist):
    """
    Performs radial distance culling on a list of QPointF points.
    """
    if len(points) < 3:
        return points
    min_dist_sq = min_dist * min_dist
    simplified = [points[0]]
    prev_pt = points[0]
    for i in range(1, len(points) - 1):
        pt = points[i]
        dx = pt.x() - prev_pt.x()
        dy = pt.y() - prev_pt.y()
        if dx * dx + dy * dy >= min_dist_sq:
            simplified.append(pt)
            prev_pt = pt
    simplified.append(points[-1])
    return simplified


def simplify_rdp(points, epsilon):
    """
    Ramer-Douglas-Peucker simplification algorithm.
    """
    if len(points) < 3:
        return points
    epsilon_sq = epsilon * epsilon
    keep = [True] * len(points)
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        if end - start < 2:
            continue
        x1, y1 = points[start].x(), points[start].y()
        x2, y2 = points[end].x(), points[end].y()
        dx = x2 - x1
        dy = y2 - y1
        line_len_sq = dx * dx + dy * dy
        max_dist_sq = -1.0
        max_idx = -1
        for i in range(start + 1, end):
            px, py = points[i].x(), points[i].y()
            if line_len_sq == 0.0:
                dist_sq = (px - x1) ** 2 + (py - y1) ** 2
            else:
                t = ((px - x1) * dx + (py - y1) * dy) / line_len_sq
                t = max(0.0, min(1.0, t))
                proj_x = x1 + t * dx
                proj_y = y1 + t * dy
                dist_sq = (px - proj_x) ** 2 + (py - proj_y) ** 2
            if dist_sq > max_dist_sq:
                max_dist_sq = dist_sq
                max_idx = i
        if max_dist_sq > epsilon_sq:
            stack.append((start, max_idx))
            stack.append((max_idx, end))
        else:
            for i in range(start + 1, end):
                keep[i] = False
    return [points[i] for i, k in enumerate(keep) if k]


def simplify_path(points, epsilon):
    """
    Combined radial and RDP path simplification pipeline.
    """
    radial = simplify_radial(points, epsilon * 0.5)
    return simplify_rdp(radial, epsilon)


def stitch_node_loops(ways):
    """
    Connects linear segments that share endpoint nodes into continuous loops or longer lines.
    """
    paths = [list(w) for w in ways if len(w) > 1]
    closed_loops = []
    open_paths = []
    
    node_to_paths = {}
    for p in paths:
        node_to_paths.setdefault(p[0], []).append(p)
        node_to_paths.setdefault(p[-1], []).append(p)
        
    while paths:
        curr = paths.pop(0)
        if curr[0] == curr[-1] and len(curr) > 1:
            closed_loops.append(curr)
            continue
            
        extended = True
        while extended:
            extended = False
            for node, check_idx in [(curr[-1], -1), (curr[0], 0)]:
                candidates = node_to_paths.get(node, [])
                candidates = [c for c in candidates if c is not curr and c in paths]
                if candidates:
                    other = candidates[0]
                    paths.remove(other)
                    
                    if check_idx == -1:
                        if curr[-1] == other[0]:
                            curr.extend(other[1:])
                        elif curr[-1] == other[-1]:
                            curr.extend(reversed(other[:-1]))
                    else:
                        if curr[0] == other[-1]:
                            curr = other + curr[1:]
                        elif curr[0] == other[0]:
                            curr = list(reversed(other)) + curr[1:]
                            
                    node_to_paths.setdefault(curr[0], []).append(curr)
                    node_to_paths.setdefault(curr[-1], []).append(curr)
                    extended = True
                    break
                    
        if curr[0] == curr[-1] and len(curr) > 1:
            closed_loops.append(curr)
        else:
            open_paths.append(curr)
            
    return closed_loops, open_paths
