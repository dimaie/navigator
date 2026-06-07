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

def get_font_scale_multiplier(scale):
    """
    Computes a dynamic font scaling multiplier based on zoom scale, clamping between 1.0 and 1.6.
    """
    return 1.0 + max(0.0, min(0.6, math.log10(scale / 0.001) * 0.22))

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
    font_scale = get_font_scale_multiplier(scale)
    
    # 5. Retrieve pre-queried visible features
    visible_coastlines = map_data.get("coastlines", [])
    visible_forests = map_data.get("forests", [])
    visible_wetlands = map_data.get("wetlands", [])
    visible_waterbodies = map_data.get("waterbodies", [])
    visible_rivers = map_data.get("rivers", [])
    visible_railways = map_data.get("railways", [])
    visible_boundaries = map_data.get("boundaries", [])
    
    visible_roads = {}
    for rtype in rules.ROAD_CATEGORIES:
        road_idx = rules.ROAD_CATEGORIES.index(rtype)
        if road_idx < lod_roads_threshold:
            visible_roads[rtype] = map_data.get("roads", {}).get(rtype, [])
            
    active_sim_key = sim_key
        
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
        
    # Administrative Boundaries
    if visible_boundaries:
        painter.setBrush(Qt.NoBrush)
        for item in visible_boundaries:
            poly = item.simplified_polygons.get(active_sim_key, item.polygon) if item.simplified_polygons else item.polygon
            if item.sub_type == '2':
                # Country boundary: thicker plum/purple dot-dash line
                pen = QPen(QColor(colors.get("boundary_country", "#8E44AD")), 1.5, Qt.DashDotLine)
            else:
                # County/provincial boundary: thinner dashed line
                pen = QPen(QColor(colors.get("boundary_county", "#C39BD3")), 0.8, Qt.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawPolyline(poly)

    # Railways (double layered pen for distinct train track styling)
    if visible_railways:
        painter.setBrush(Qt.NoBrush)
        rail_base_pen = QPen(QColor(colors.get("railway", "#566573")), 1.8, Qt.SolidLine)
        rail_base_pen.setCosmetic(True)
        rail_top_pen = QPen(QColor(colors.get("railway_dash", "#FCFAF2")), 1.0, Qt.DashLine)
        rail_top_pen.setCosmetic(True)
        for item in visible_railways:
            poly = item.simplified_polygons.get(active_sim_key, item.polygon) if item.simplified_polygons else item.polygon
            painter.setPen(rail_base_pen)
            painter.drawPolyline(poly)
            painter.setPen(rail_top_pen)
            painter.drawPolyline(poly)
        
    # Roads Outline (Casing)
    active_lod = zoom_details.get(active_sim_key, {}).get("roads", 1)
    
    for rtype in rules.ROAD_CATEGORIES:
        if rtype in ('track', 'path', 'footway', 'cycleway'):
            continue  # Skip casing for minor paths/tracks
        rlist = visible_roads.get(rtype, [])

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
        style = Qt.DashLine if rtype in ('track', 'path', 'footway', 'cycleway') else Qt.SolidLine
        pen = QPen(color, width_px, style, Qt.RoundCap, Qt.RoundJoin)
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
        ptype = place["place_type"]
        if ptype == "county" and scale < 0.0008:
            continue
        if place["population"] < places_threshold and ptype != "county":
            continue
        if not viewport_rect.contains(QPointF(place["x"], place["y"])):
            continue
            
        px, py = to_screen(place["x"], place["y"], center_x, center_y, scale, width, height)
        
        font_size, font_bold = rules.get_place_font_style(ptype)
        scaled_font_size = max(6, int(font_size * font_scale))
        font_weight = QFont.Bold if font_bold else QFont.Normal
        font = QFont("Segoe UI", scaled_font_size, font_weight)
        
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
            if dot_size > 0.0:
                dot_color = QColor(dot_color_hex)
                painter.setPen(Qt.NoPen)
                painter.setBrush(dot_color)
                painter.drawEllipse(QPointF(px, py), dot_size, dot_size)
            
            # semi-translucent background
            if ptype != "county":
                painter.setBrush(QColor(252, 250, 242, 210))
                painter.drawRect(label_rect)
            
            # draw label text
            painter.setFont(font)
            if ptype == "county":
                painter.setPen(QPen(QColor(colors.get("boundary_country", "#8E44AD"))))
            else:
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
                
            fsize_scaled = fsize * font_scale
            w = len(name) * (fsize_scaled * 0.7) + 4
            h = fsize_scaled + 2.0
            
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
                    scaled_size = max(5, int(style["size"] * font_scale))
                    font = QFont("Segoe UI", scaled_size, style["weight"])
                    font.setItalic(True)
                    painter.setFont(font)
                    painter.setPen(style["color"])
                else:
                    # River label
                    scaled_size = max(5, int(7 * font_scale))
                    font = QFont("Segoe UI", scaled_size)
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
        scaled_font_size = max(6, int(font_size * font_scale))
        w = len(place["name"]) * (scaled_font_size * 0.55) + 4
        h = scaled_font_size + 4
        
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
            scaled_font_size = max(6, int(font_size * font_scale))
            font_weight = QFont.Bold if font_bold else QFont.Normal
            painter.setFont(QFont("Segoe UI", scaled_font_size, font_weight))
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
        fsize = 7.0 * font_scale
        w = len(name) * (fsize * 0.7) + 4
        h = fsize + 2.0
        
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
                
            scaled_size = max(5, int(7 * font_scale))
            font = QFont("Segoe UI", scaled_size)
            font.setItalic(True)
            painter.setFont(font)
            painter.setPen(color)
            painter.drawText(rect, Qt.AlignCenter, name)
            painter.restore()
            
            drawn_rects.append(label_rect)
            occupied_cells.add(cell)
            
    painter.end()
    return image
