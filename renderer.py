# renderer.py

import math
import array
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QImage, QPainter, QColor, QPen, QBrush, QFont, QFontMetrics, QPolygonF
import rules
import constants

def to_mercator(px, py, center_x, center_y, scale, width, height):
    """
    Converts screen pixel coordinates back to Web Mercator coordinates (meters).
    """
    x = center_x + (px - width / 2.0) / scale
    y = center_y - (py - height / 2.0) / scale
    return x, y

def to_screen(x, y, center_x, center_y, scale, width, height):
    """
    Converts Web Mercator coordinates (meters) to screen pixel coordinates.
    """
    px = width / 2.0 + (x - center_x) * scale
    py = height / 2.0 - (y - center_y) * scale
    return px, py

def make_brush(color):
    """
    Safely creates a solid color QBrush to avoid PySide6 QGradient.Preset overload bugs.
    """
    brush = QBrush()
    brush.setColor(color)
    brush.setStyle(Qt.SolidPattern)
    return brush

def render_map(width, height, center_x, center_y, scale, map_data, zoom_details, colors, frame_budget, is_interruption_requested=None):
    """
    Renders the map (geometries, roads, rivers, labels) onto a QImage.
    Supports early abort via the is_interruption_requested callback.
    """
    if is_interruption_requested and is_interruption_requested():
        return None
        
    # 1. Resolve colors
    color_ocean = QColor(colors.get("ocean", "#D4E6F1"))
    color_land = QColor(colors.get("land", "#FCFAF2"))
    color_land_border = QColor(colors.get("land_border", "#D5D8DC"))
    color_forest = QColor(colors.get("forest", "#DCEBD6"))
    color_wetland = QColor(colors.get("wetland", "#E3E5D5"))
    color_wetland_border = QColor(colors.get("wetland_border", "#D2D5C5"))
    color_waterbody = QColor(colors.get("waterbody", "#D4E6F1"))
    color_waterbody_border = QColor(colors.get("waterbody_border", "#A9CCE3"))
    color_river = QColor(colors.get("river", "#A9CCE3"))
    
    road_colors = {
        rtype: QColor(colors.get(f"road_{rtype}", "#FFFFFF"))
        for rtype in rules.ROAD_CATEGORIES
    }
    road_casing_colors = {
        rtype: QColor(colors.get(f"road_casing_{rtype}", "#BDC3C7"))
        for rtype in rules.ROAD_CATEGORIES
    }

    # 2. Setup QImage canvas
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    image.fill(color_ocean)
    
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    
    # 3. Viewport rect in Web Mercator
    vx1, vy2 = to_mercator(0, 0, center_x, center_y, scale, width, height)
    vx2, vy1 = to_mercator(width, height, center_x, center_y, scale, width, height)
    viewport_rect = QRectF(vx1, vy1, vx2 - vx1, vy2 - vy1)
    
    # 4. Resolve LOD scale key
    sim_key = None
    for k in sorted(zoom_details.keys(), key=float):
        if float(k) <= scale:
            sim_key = k
        else:
            break
    if sim_key is None:
        sim_key = "0.0001"
        
    # Get details for the active scale key
    current_details = zoom_details.get(sim_key, {})
    lod_roads_threshold = current_details.get("roads", 1)
    places_threshold = current_details.get("places", 50000)
    
    # 5. Retrieve visible features from Spatial Index
    visible_coastlines = map_data["coastlines_index"][sim_key].query(viewport_rect)
    visible_forests = map_data["forests_index"][sim_key].query(viewport_rect)
    visible_wetlands = map_data["wetlands_index"][sim_key].query(viewport_rect)
    visible_waterbodies = map_data["waterbodies_index"][sim_key].query(viewport_rect)
    visible_rivers = map_data["rivers_index"][sim_key].query(viewport_rect)
    
    visible_roads = {}
    for rtype in rules.ROAD_CATEGORIES:
        # Check if this road type is visible at this LOD rank
        road_idx = rules.ROAD_CATEGORIES.index(rtype)
        if road_idx < lod_roads_threshold:
            visible_roads[rtype] = map_data["roads_index"][sim_key][rtype].query(viewport_rect)
            
    # 6. Coordinate Budgeting Fallback
    def get_point_count(item, key):
        if item.simplified_polygons and key in item.simplified_polygons:
            return item.simplified_polygons[key].size()
        return item.polygon.size()
        
    total_points = 0
    total_points += sum(get_point_count(item, sim_key) for item in visible_coastlines)
    total_points += sum(get_point_count(item, sim_key) for item in visible_forests)
    total_points += sum(get_point_count(item, sim_key) for item in visible_wetlands)
    total_points += sum(get_point_count(item, sim_key) for item in visible_waterbodies)
    total_points += sum(get_point_count(item, sim_key) for item in visible_rivers)
    for rtype, rlist in visible_roads.items():
        total_points += sum(get_point_count(item, sim_key) for item in rlist)
        
    active_sim_key = sim_key
    scale_keys_sorted = sorted(zoom_details.keys(), key=float)
    current_idx = scale_keys_sorted.index(sim_key)
    
    while total_points > frame_budget and current_idx > 0:
        current_idx -= 1
        test_key = scale_keys_sorted[current_idx]
        
        test_coastlines = map_data["coastlines_index"][test_key].query(viewport_rect)
        test_forests = map_data["forests_index"][test_key].query(viewport_rect)
        test_wetlands = map_data["wetlands_index"][test_key].query(viewport_rect)
        test_waterbodies = map_data["waterbodies_index"][test_key].query(viewport_rect)
        test_rivers = map_data["rivers_index"][test_key].query(viewport_rect)
        
        test_roads = {}
        test_lod = zoom_details.get(test_key, {}).get("roads", 1)
        for rtype in rules.ROAD_CATEGORIES:
            road_idx = rules.ROAD_CATEGORIES.index(rtype)
            if road_idx < test_lod:
                test_roads[rtype] = map_data["roads_index"][test_key][rtype].query(viewport_rect)
                
        test_total = 0
        test_total += sum(get_point_count(item, test_key) for item in test_coastlines)
        test_total += sum(get_point_count(item, test_key) for item in test_forests)
        test_total += sum(get_point_count(item, test_key) for item in test_wetlands)
        test_total += sum(get_point_count(item, test_key) for item in test_waterbodies)
        test_total += sum(get_point_count(item, test_key) for item in test_rivers)
        for rtype, rlist in test_roads.items():
            test_total += sum(get_point_count(item, test_key) for item in rlist)
            
        total_points = test_total
        active_sim_key = test_key
        visible_coastlines = test_coastlines
        visible_forests = test_forests
        visible_wetlands = test_wetlands
        visible_waterbodies = test_waterbodies
        visible_rivers = test_rivers
        visible_roads = test_roads
        
    if is_interruption_requested and is_interruption_requested():
        painter.end()
        return None
        
    # 7. Apply coordinate transformation matrix
    painter.save()
    painter.translate(width / 2.0, height / 2.0)
    painter.scale(scale, -scale)
    painter.translate(-center_x, -center_y)
    
    # Draw vector layers
    # Coastlines
    land_brush = make_brush(color_land)
    land_pen = QPen(color_land_border, 1.0)
    land_pen.setCosmetic(True)
    painter.setBrush(land_brush)
    painter.setPen(land_pen)
    for item in visible_coastlines:
        poly = item.simplified_polygons.get(active_sim_key, item.polygon) if item.simplified_polygons else item.polygon
        painter.drawPolygon(poly)
        
    # Forests
    forest_brush = make_brush(color_forest)
    painter.setBrush(forest_brush)
    painter.setPen(Qt.NoPen)
    for item in visible_forests:
        poly = item.simplified_polygons.get(active_sim_key, item.polygon) if item.simplified_polygons else item.polygon
        painter.drawPolygon(poly)
        
    # Wetlands
    wetland_brush = make_brush(color_wetland)
    wetland_pen = QPen(color_wetland_border, 0.5)
    wetland_pen.setCosmetic(True)
    painter.setBrush(wetland_brush)
    painter.setPen(wetland_pen)
    for item in visible_wetlands:
        poly = item.simplified_polygons.get(active_sim_key, item.polygon) if item.simplified_polygons else item.polygon
        painter.drawPolygon(poly)
        
    # Waterbodies
    water_brush = make_brush(color_waterbody)
    water_pen = QPen(color_waterbody_border, 0.5)
    water_pen.setCosmetic(True)
    painter.setBrush(water_brush)
    painter.setPen(water_pen)
    for item in visible_waterbodies:
        poly = item.simplified_polygons.get(active_sim_key, item.polygon) if item.simplified_polygons else item.polygon
        painter.drawPolygon(poly)
        
    # Rivers
    river_pen = QPen(color_river, 1.0)
    river_pen.setCosmetic(True)
    painter.setBrush(Qt.NoBrush)
    painter.setPen(river_pen)
    for item in visible_rivers:
        poly = item.simplified_polygons.get(active_sim_key, item.polygon) if item.simplified_polygons else item.polygon
        painter.drawPolyline(poly)
        
    # Roads Outline (Casing)
    active_lod = zoom_details.get(active_sim_key, {}).get("roads", 1)
    
    # Diagnostic check for Athlone Services target road
    target_x, target_y = -871624.79, 7057459.85
    is_diag = viewport_rect.contains(QPointF(target_x, target_y))
    if is_diag:
        print(f"\n[DIAGNOSTIC] Viewport contains Athlone Services target point!")
        print(f"  Current scale: {scale:.5f}, active_sim_key: {active_sim_key}, active_lod (threshold): {active_lod}")
        
    for rtype in rules.ROAD_CATEGORIES:
        rlist = visible_roads.get(rtype, [])
        if is_diag:
            print(f"  Road type '{rtype}': queried {len(rlist)} visible features.")
            for item in rlist:
                if item.min_x - 50.0 <= target_x <= item.max_x + 50.0 and item.min_y - 50.0 <= target_y <= item.max_y + 50.0:
                    width_px = rules.get_road_width_for_scale(rtype, scale, False, active_lod)
                    print(f"    -> FOUND road in index covering target! Name: '{item.name or ''}', sub_type: '{item.sub_type}', bbox: [{item.min_x:.1f}, {item.min_y:.1f}, {item.max_x:.1f}, {item.max_y:.1f}]")
                    print(f"       Width: {width_px} pixels")

        if not rlist:
            continue
        width_px = rules.get_road_width_for_scale(rtype, scale, False, active_lod)
        if width_px <= 0.0:
            continue
        casing_width = width_px + 1.2
        casing_color = road_casing_colors.get(rtype, QColor("#BDC3C7"))
        pen = QPen(casing_color, casing_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        pen.setCosmetic(True)
        painter.setPen(pen)
        for item in rlist:
            poly = item.simplified_polygons.get(active_sim_key, item.polygon) if item.simplified_polygons else item.polygon
            painter.drawPolyline(poly)
            
    # Roads Core
    for rtype in rules.ROAD_CATEGORIES:
        rlist = visible_roads.get(rtype, [])
        if not rlist:
            continue
        width_px = rules.get_road_width_for_scale(rtype, scale, False, active_lod)
        if width_px <= 0.0:
            continue
        color = road_colors.get(rtype, QColor("#FFFFFF"))
        pen = QPen(color, width_px, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        pen.setCosmetic(True)
        painter.setPen(pen)
        for item in rlist:
            poly = item.simplified_polygons.get(active_sim_key, item.polygon) if item.simplified_polygons else item.polygon
            painter.drawPolyline(poly)
            
    painter.restore()
    
    if is_interruption_requested and is_interruption_requested():
        painter.end()
        return None
        
    # 8. Post-Rendering Labels
    drawn_place_ids = set()
    drawn_rects = []
    occupied_cells = set()
    
    # A. Draw primary places first
    for place in map_data["places"]:
        if place["population"] < places_threshold:
            continue
        if not viewport_rect.contains(QPointF(place["x"], place["y"])):
            continue
            
        ptype = place["place_type"]
        px, py = to_screen(place["x"], place["y"], center_x, center_y, scale, width, height)
        
        font_size, font_bold = rules.get_place_font_style(ptype)
        font_weight = QFont.Bold if font_bold else QFont.Normal
        font = QFont("Segoe UI", font_size, font_weight)
        
        fm = QFontMetrics(font)
        name = place["name"]
        text_w = fm.horizontalAdvance(name)
        text_h = fm.height()
        
        label_rect = QRectF(px + 6, py - text_h / 2.0, text_w + 4, text_h)
        
        collision = False
        for rect in drawn_rects:
            if rect.intersects(label_rect):
                collision = True
                break
                
        if not collision:
            dot_size, dot_color_hex = rules.get_place_marker_style(ptype)
            dot_color = QColor(dot_color_hex)
            painter.setPen(Qt.NoPen)
            painter.setBrush(dot_color)
            painter.drawEllipse(QPointF(px, py), dot_size, dot_size)
            
            # semi-translucent background
            painter.setBrush(QColor(252, 250, 242, 210))
            painter.drawRect(label_rect)
            
            # draw label text
            painter.setFont(font)
            painter.setPen(QPen(QColor("#2C3E50")))
            painter.drawText(label_rect.left() + 2, label_rect.top() + fm.ascent(), name)
            
            drawn_rects.append(label_rect)
            drawn_place_ids.add(place["id"])
            cell = (int(px / 80), int(py / 80))
            occupied_cells.add(cell)

    if is_interruption_requested and is_interruption_requested():
        painter.end()
        return None
        
    # B. Label candidates collection (from visible features in the viewport)
    road_candidates = []
    for rtype in rules.ROAD_CATEGORIES:
        rlist = visible_roads.get(rtype, [])
        for item in rlist:
            if item.name:
                points = [(pt.x(), pt.y()) for pt in item.polygon]
                road_candidates.append({
                    "name": item.name,
                    "points": points,
                    "road_type": rtype
                })
                
    river_candidates = []
    for item in visible_rivers:
        if item.name:
            points = [(pt.x(), pt.y()) for pt in item.polygon]
            river_candidates.append({
                "name": item.name,
                "points": points
            })
            
    place_candidates = []
    for place in map_data["places"]:
        if place["id"] in drawn_place_ids:
            continue
        if viewport_rect.contains(QPointF(place["x"], place["y"])):
            place_candidates.append(place)
            
    area_candidates = []
    # Combine wetlands, forests, waterbodies
    for item in visible_waterbodies:
        if item.name:
            area_candidates.append({"name": item.name, "min_x": item.min_x, "min_y": item.min_y, "max_x": item.max_x, "max_y": item.max_y, "area_type": "waterbody"})
    for item in visible_forests:
        if item.name:
            area_candidates.append({"name": item.name, "min_x": item.min_x, "min_y": item.min_y, "max_x": item.max_x, "max_y": item.max_y, "area_type": "forest"})
    for item in visible_wetlands:
        if item.name:
            area_candidates.append({"name": item.name, "min_x": item.min_x, "min_y": item.min_y, "max_x": item.max_x, "max_y": item.max_y, "area_type": "wetland"})
            
    # C. Run layout placement and draw rotated/centered labels directly
    # 1. Process Roads & Rivers
    sc_x = width / 2.0
    sc_y = height / 2.0
    
    def draw_linear_labels(candidates, label_type):
        for feat in candidates:
            if is_interruption_requested and is_interruption_requested():
                return False
                
            name = feat["name"]
            points = feat["points"]
            if not points or len(points) < 2:
                continue
                
            screen_pts = [to_screen(pt[0], pt[1], center_x, center_y, scale, width, height) for pt in points]
            
            best_seg = None
            min_dist_sq = float('inf')
            
            for i in range(len(screen_pts) - 1):
                p1 = screen_pts[i]
                p2 = screen_pts[i+1]
                mx = (p1[0] + p2[0]) / 2.0
                my = (p1[1] + p2[1]) / 2.0
                
                if 0 <= mx <= width and 0 <= my <= height:
                    dx = mx - sc_x
                    dy = my - sc_y
                    dist_sq = dx * dx + dy * dy
                    if dist_sq < min_dist_sq:
                        min_dist_sq = dist_sq
                        best_seg = (p1, p2, mx, my)
                        
            if not best_seg:
                continue
                
            p1, p2, mx, my = best_seg
            cell = (int(mx / 80), int(my / 80))
            if cell in occupied_cells:
                continue
                
            dx_s = p2[0] - p1[0]
            dy_s = p2[1] - p1[1]
            if dx_s == 0 and dy_s == 0:
                continue
                
            angle_rad = math.atan2(dy_s, dx_s)
            angle_deg = math.degrees(angle_rad)
            if angle_deg > 90:
                angle_deg -= 180
            elif angle_deg < -90:
                angle_deg += 180
                
            rtype = feat.get("road_type")
            if rtype:
                font_sizes = {
                    'motorway': 9.0, 'trunk': 8.0, 'primary': 8.0,
                    'secondary': 7.0, 'tertiary': 7.0, 'unclassified': 6.0, 'residential': 6.0
                }
                fsize = font_sizes.get(rtype, 6.0)
            else:
                fsize = 7.0
                
            w = len(name) * (fsize * 0.7) + 4
            h = fsize + 2.0
            
            rad = math.radians(angle_deg)
            cos_a = math.cos(rad)
            sin_a = math.sin(rad)
            hw = w / 2.0
            hh = h / 2.0
            
            corners = [(-hw, -hh), (hw, -hh), (-hw, hh), (hw, hh)]
            xs, ys = [], []
            for cx, cy in corners:
                rx = cx * cos_a - cy * sin_a
                ry = cx * sin_a + cy * cos_a
                xs.append(mx + rx)
                ys.append(my + ry)
                
            aabb = QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
            
            collision = False
            for r in drawn_rects:
                if r.intersects(aabb):
                    collision = True
                    break
                    
            if not collision:
                # Draw the label directly onto the image
                painter.save()
                painter.translate(mx, my)
                painter.rotate(angle_deg)
                
                rect = QRectF(-w / 2.0, -h / 2.0, w, h)
                painter.setBrush(QColor(252, 250, 242, 180 if rtype else 150))
                painter.setPen(Qt.NoPen)
                painter.drawRect(rect)
                
                if rtype:
                    # Road label
                    style_map = {
                        'motorway':     {"size": 9, "weight": QFont.Bold,   "color": QColor("#932E22")},
                        'trunk':        {"size": 8, "weight": QFont.Bold,   "color": QColor("#B25E00")},
                        'primary':      {"size": 8, "weight": QFont.Normal, "color": QColor("#856404")},
                        'secondary':    {"size": 7, "weight": QFont.Normal, "color": QColor("#2C3E50")},
                        'tertiary':     {"size": 7, "weight": QFont.Normal, "color": QColor("#5D6D7E")},
                        'unclassified': {"size": 6, "weight": QFont.Normal, "color": QColor("#7F8C8D")},
                        'residential':  {"size": 6, "weight": QFont.Normal, "color": QColor("#7F8C8D")}
                    }
                    style = style_map.get(rtype, style_map['residential'])
                    font = QFont("Segoe UI", style["size"], style["weight"])
                    font.setItalic(True)
                    painter.setFont(font)
                    painter.setPen(style["color"])
                else:
                    # River label
                    font = QFont("Segoe UI", 7)
                    font.setItalic(True)
                    painter.setFont(font)
                    painter.setPen(QColor("#2980B9"))
                    
                painter.drawText(rect, Qt.AlignCenter, name)
                painter.restore()
                
                drawn_rects.append(aabb)
                occupied_cells.add(cell)
        return True
        
    if not draw_linear_labels(road_candidates, "road"):
        painter.end()
        return None
        
    if not draw_linear_labels(river_candidates, "river"):
        painter.end()
        return None
        
    # 2. Process minor places
    for place in place_candidates:
        if is_interruption_requested and is_interruption_requested():
            painter.end()
            return None
            
        px, py = to_screen(place["x"], place["y"], center_x, center_y, scale, width, height)
        cell = (int(px / 80), int(py / 80))
        if cell in occupied_cells:
            continue
            
        ptype = place["place_type"]
        font_size = 11 if ptype == "city" else (9 if ptype == "town" else 8)
        w = len(place["name"]) * (font_size * 0.55) + 4
        h = font_size + 4
        
        label_rect = QRectF(px + 6, py - h / 2.0, w, h)
        collision = False
        for r in drawn_rects:
            if r.intersects(label_rect):
                collision = True
                break
                
        if not collision:
            dot_size, dot_color_hex = rules.get_place_marker_style(ptype)
            dot_color = QColor(dot_color_hex)
            painter.setPen(Qt.NoPen)
            painter.setBrush(dot_color)
            painter.drawEllipse(QPointF(px, py), dot_size, dot_size)
            
            painter.setBrush(QColor(252, 250, 242, 210))
            painter.drawRect(label_rect)
            
            font_size, font_bold = rules.get_place_font_style(ptype)
            font_weight = QFont.Bold if font_bold else QFont.Normal
            painter.setFont(QFont("Segoe UI", font_size, font_weight))
            painter.setPen(QPen(QColor("#2C3E50")))
            painter.drawText(label_rect, Qt.AlignLeft | Qt.AlignVCenter, " " + place["name"])
            
            drawn_rects.append(label_rect)
            occupied_cells.add(cell)
            
    # 3. Process areas (lakes, forests, wetlands)
    for area in area_candidates:
        if is_interruption_requested and is_interruption_requested():
            painter.end()
            return None
            
        cx = (area["min_x"] + area["max_x"]) / 2.0
        cy = (area["min_y"] + area["max_y"]) / 2.0
        px, py = to_screen(cx, cy, center_x, center_y, scale, width, height)
        
        if not (0 <= px <= width and 0 <= py <= height):
            continue
        cell = (int(px / 80), int(py / 80))
        if cell in occupied_cells:
            continue
            
        name = area["name"]
        w = len(name) * 5.0 + 4
        h = 9.0
        
        label_rect = QRectF(px - w / 2.0, py - h / 2.0, w, h)
        collision = False
        for r in drawn_rects:
            if r.intersects(label_rect):
                collision = True
                break
                
        if not collision:
            painter.save()
            painter.translate(px, py)
            
            rect = QRectF(-w / 2.0, -h / 2.0, w, h)
            painter.setBrush(QColor(252, 250, 242, 150))
            painter.setPen(Qt.NoPen)
            painter.drawRect(rect)
            
            atype = area["area_type"]
            if atype == "waterbody":
                color = QColor("#2980B9")
            elif atype == "forest":
                color = QColor("#2E7D32")
            else:
                color = QColor("#795548")
                
            font = QFont("Segoe UI", 7)
            font.setItalic(True)
            painter.setFont(font)
            painter.setPen(color)
            painter.drawText(rect, Qt.AlignCenter, name)
            painter.restore()
            
            drawn_rects.append(label_rect)
            occupied_cells.add(cell)
            
    painter.end()
    return image
