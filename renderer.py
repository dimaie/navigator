# renderer.py

from PySide6.QtCore import Qt, QPointF, QRectF, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QFontMetrics, QPolygonF
import constants
import rules

def paint_map(painter, widget):
    """
    Renders vector geometries, places, labels, and status overlays onto the viewport.
    """
    # Fill base canvas with ocean color
    painter.fillRect(widget.rect(), widget.color_ocean)
    
    # Display loader notification screen if data hasn't loaded yet
    if not widget.data_loaded:
        painter.setFont(QFont("Segoe UI", 14))
        painter.setPen(QColor("#5D6D7E"))
        if hasattr(widget, "db_name") and widget.db_name:
            painter.drawText(widget.rect(), Qt.AlignCenter, constants.STR_LOADING_DATA)
        else:
            painter.drawText(widget.rect(), Qt.AlignCenter, constants.STR_NO_MAP_PROMPT)
        return
        
    # Get active viewport bounding rect in Web Mercator meters
    vx1, vy2 = widget.to_mercator(0, 0)
    vx2, vy1 = widget.to_mercator(widget.width(), widget.height())
    viewport_rect = QRectF(vx1, vy1, vx2 - vx1, vy2 - vy1)
    
    # Compute dynamic scale details for this paint pass
    widget.current_details = widget.get_details_for_scale(widget.scale)
    
    # Resolve simplification scale key once for this paint pass
    sim_key = None
    for k in sorted(widget.zoom_details.keys(), key=float):
        if float(k) <= widget.scale:
            sim_key = k
        else:
            break
    if sim_key is None:
        sim_key = "0.0001"
        
    # Setup matrix transformations to paint in Web Mercator meters directly
    painter.save()
    painter.translate(widget.width() / 2.0, widget.height() / 2.0)  # Center camera focus
    painter.scale(widget.scale, -widget.scale)                      # Invert Y-axis for standard cartesian grid
    painter.translate(-widget.center_x, -widget.center_y)           # Translate to current coordinates
    
    # A. Coastlines (Landmass boundaries)
    land_brush = QBrush(widget.color_land)
    land_pen = QPen(widget.color_land_border, 1.0)
    land_pen.setCosmetic(True)  # Width remains 1.0 pixel regardless of zoom scale
    painter.setBrush(land_brush)
    painter.setPen(land_pen)
    
    # Estimate the point budget using the full static detailed view to prevent detail popping during dragging
    visible_coastlines = widget.coastlines_index[sim_key].query(viewport_rect)
    visible_forests = widget.forests_index[sim_key].query(viewport_rect)
    visible_wetlands = widget.wetlands_index[sim_key].query(viewport_rect)
    visible_waterbodies = widget.waterbodies_index[sim_key].query(viewport_rect)
    visible_rivers = widget.rivers_index[sim_key].query(viewport_rect)
    visible_roads = {}
    road_types = rules.ROAD_CATEGORIES
    for rtype in road_types:
        width = widget.get_road_width(rtype, ignore_interaction=True)
        if width > 0.0:
            visible_roads[rtype] = widget.roads_index[sim_key][rtype].query(viewport_rect)
            
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
    scale_keys_sorted = sorted(widget.zoom_details.keys(), key=float)
    current_idx = scale_keys_sorted.index(sim_key)
    
    # Degrade detail key if total coordinates exceed frame budget
    while total_points > widget.frame_budget and current_idx > 0:
        current_idx -= 1
        test_key = scale_keys_sorted[current_idx]
        
        test_coastlines = widget.coastlines_index[test_key].query(viewport_rect)
        test_forests = widget.forests_index[test_key].query(viewport_rect)
        test_wetlands = widget.wetlands_index[test_key].query(viewport_rect)
        test_waterbodies = widget.waterbodies_index[test_key].query(viewport_rect)
        test_rivers = widget.rivers_index[test_key].query(viewport_rect)
        test_roads = {}
        for rtype in road_types:
            width = widget.get_road_width(rtype, ignore_interaction=True)
            if width > 0.0:
                test_roads[rtype] = widget.roads_index[test_key][rtype].query(viewport_rect)
                
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
        
    # If interacting, filter down the visible sets to the provisional subset to keep rendering fast
    if widget.is_interacting:
        visible_forests = []
        visible_wetlands = []
        visible_rivers = []
        provisional_roads = {}
        for rtype in ['motorway', 'trunk']:
            if rtype in visible_roads:
                provisional_roads[rtype] = visible_roads[rtype]
        visible_roads = provisional_roads
        
    # A. Coastlines (Landmass boundaries)
    for item in visible_coastlines:
        poly = item.simplified_polygons.get(active_sim_key, item.polygon) if item.simplified_polygons else item.polygon
        painter.drawPolygon(poly)
        
    # B. Forests (Skip during panning/zooming to keep interaction at 60 FPS)
    if not widget.is_interacting:
        forest_brush = QBrush(widget.color_forest)
        painter.setBrush(forest_brush)
        painter.setPen(Qt.NoPen)
        for item in visible_forests:
            poly = item.simplified_polygons.get(active_sim_key, item.polygon) if item.simplified_polygons else item.polygon
            painter.drawPolygon(poly)
            
    # C. Wetlands / Peat bogs (Skip during interaction)
    if not widget.is_interacting:
        wetland_brush = QBrush(widget.color_wetland)
        wetland_pen = QPen(widget.color_wetland_border, 0.5)
        wetland_pen.setCosmetic(True)
        painter.setBrush(wetland_brush)
        painter.setPen(wetland_pen)
        for item in visible_wetlands:
            poly = item.simplified_polygons.get(active_sim_key, item.polygon) if item.simplified_polygons else item.polygon
            painter.drawPolygon(poly)
            
    # D. Water bodies (Lakes)
    water_brush = QBrush(widget.color_waterbody)
    water_pen = QPen(widget.color_waterbody_border, 0.5)
    water_pen.setCosmetic(True)
    painter.setBrush(water_brush)
    painter.setPen(water_pen)
    for item in visible_waterbodies:
        poly = item.simplified_polygons.get(active_sim_key, item.polygon) if item.simplified_polygons else item.polygon
        painter.drawPolygon(poly)
        
    # E. Rivers (Skip during interaction)
    if not widget.is_interacting:
        river_pen = QPen(widget.color_river, 1.0)
        river_pen.setCosmetic(True)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(river_pen)
        for item in visible_rivers:
            poly = item.simplified_polygons.get(active_sim_key, item.polygon) if item.simplified_polygons else item.polygon
            painter.drawPolyline(poly)
            
    # F. Roads (Double-Pass outline technique to create distinguishable borders)
    # Pass 1: Outlines (Casing)
    for rtype in road_types:
        width = widget.get_road_width(rtype)
        if width <= 0.0:
            continue
            
        casing_width = width + 1.2
        casing_color = widget.road_casing_colors.get(rtype, QColor("#BDC3C7"))
        
        pen = QPen(casing_color, casing_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        pen.setCosmetic(True)
        painter.setPen(pen)
        
        rlist = visible_roads.get(rtype, [])
        for item in rlist:
            poly = item.simplified_polygons.get(active_sim_key, item.polygon) if item.simplified_polygons else item.polygon
            painter.drawPolyline(poly)
            
    # Pass 2: Cores
    for rtype in road_types:
        width = widget.get_road_width(rtype)
        if width <= 0.0:
            continue
            
        color = widget.road_colors.get(rtype, QColor("#FFFFFF"))
        pen = QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        pen.setCosmetic(True)
        painter.setPen(pen)
        
        rlist = visible_roads.get(rtype, [])
        for item in rlist:
            poly = item.simplified_polygons.get(active_sim_key, item.polygon) if item.simplified_polygons else item.polygon
            painter.drawPolyline(poly)
            
    painter.restore()
    
    # G. Place Labels (Skip during interaction to maintain fast panning speeds)
    widget.main_drawn_places = set()
    widget.main_drawn_rects = []
    if not widget.is_interacting:
        places_threshold = widget.current_details.get("places", 0.0)
        for place in widget.places:
            # Cull by population threshold
            if place["population"] < places_threshold:
                continue
                
            if not viewport_rect.contains(QPointF(place["x"], place["y"])):
                continue
                
            ptype = place["place_type"]
            
            # Project coordinates to screen pixels
            px, py = widget.to_screen(place["x"], place["y"])
            
            # Determine font styles dynamically by place class
            font_size, font_bold = rules.get_place_font_style(ptype)
            font_weight = QFont.Bold if font_bold else QFont.Normal
            font = QFont("Segoe UI", font_size, font_weight)
            
            fm = QFontMetrics(font)
            name = place["name"]
            text_w = fm.horizontalAdvance(name)
            text_h = fm.height()
            
            # Offset text slightly to the right of the dot marker
            label_rect = QRectF(px + 6, py - text_h / 2.0, text_w + 4, text_h)
            
            # Simple bounding-box intersection collision check
            collision = False
            for rect in widget.main_drawn_rects:
                if rect.intersects(label_rect):
                    collision = True
                    break
                    
            if not collision:
                # Draw place marker dot
                dot_size, dot_color_hex = rules.get_place_marker_style(ptype)
                dot_color = QColor(dot_color_hex)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(dot_color))
                painter.drawEllipse(QPointF(px, py), dot_size, dot_size)
                
                # Draw a semi-translucent backdrop behind label text for readability
                painter.setBrush(QBrush(QColor(252, 250, 242, 210)))
                painter.drawRect(label_rect)
                
                # Paint text label
                painter.setFont(font)
                painter.setPen(QPen(QColor("#2C3E50")))
                painter.drawText(label_rect.left() + 2, label_rect.top() + fm.ascent(), name)
                
                # Register rect in the drawn list to prevent overlap collisions
                widget.main_drawn_rects.append(label_rect)
                widget.main_drawn_places.add(place["id"])
                
    # H. Idle Labels (Rotated road and river names, places, and area names computed in idle background thread)
    if not widget.is_interacting and hasattr(widget, "idle_labels") and widget.idle_labels:
        for lbl in widget.idle_labels:
            if lbl["type"] == "road":
                painter.save()
                painter.translate(lbl["x"], lbl["y"])
                painter.rotate(lbl["angle"])
                
                text = lbl["text"]
                rtype = lbl.get("road_type", "residential")
                
                # Cartographic style mapping based on road significance
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
                
                # Use the text size to draw backdrop and text
                w = len(text) * (style["size"] * 0.7) + 4
                h = style["size"] + 2.0
                rect = QRectF(-w / 2.0, -h / 2.0, w, h)
                
                # Semi-translucent backdrop
                painter.setBrush(QBrush(QColor(252, 250, 242, 180)))
                painter.setPen(Qt.NoPen)
                painter.drawRect(rect)
                
                # Paint street name in matched size and color, italicized
                font = QFont("Segoe UI", style["size"], style["weight"])
                font.setItalic(True)
                painter.setFont(font)
                painter.setPen(style["color"])
                painter.drawText(rect, Qt.AlignCenter, text)
                painter.restore()
            elif lbl["type"] == "river":
                painter.save()
                painter.translate(lbl["x"], lbl["y"])
                painter.rotate(lbl["angle"])
                
                text = lbl["text"]
                w = len(text) * 5.0 + 4
                h = 9.0
                rect = QRectF(-w / 2.0, -h / 2.0, w, h)
                
                # Semi-translucent backdrop
                painter.setBrush(QBrush(QColor(252, 250, 242, 150)))
                painter.setPen(Qt.NoPen)
                painter.drawRect(rect)
                
                # Paint river name in water blue, size 7 italicized font (#2980B9)
                font = QFont("Segoe UI", 7)
                font.setItalic(True)
                painter.setFont(font)
                painter.setPen(QColor("#2980B9"))
                painter.drawText(rect, Qt.AlignCenter, text)
                painter.restore()
            elif lbl["type"] == "place":
                # Draw place marker dot
                ptype = lbl["place_type"]
                dot_size, dot_color_hex = rules.get_place_marker_style(ptype)
                dot_color = QColor(dot_color_hex)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(dot_color))
                painter.drawEllipse(QPointF(lbl["x"], lbl["y"]), dot_size, dot_size)
                
                # Draw a semi-translucent backdrop
                painter.setBrush(QBrush(QColor(252, 250, 242, 210)))
                painter.drawRect(lbl["rect"])
                
                # Paint text label (in regular font size)
                font_size, font_bold = rules.get_place_font_style(ptype)
                font_weight = QFont.Bold if font_bold else QFont.Normal
                painter.setFont(QFont("Segoe UI", font_size, font_weight))
                painter.setPen(QPen(QColor("#2C3E50")))
                painter.drawText(lbl["rect"], Qt.AlignLeft | Qt.AlignVCenter, " " + lbl["text"])
            elif lbl["type"] == "area":
                painter.save()
                painter.translate(lbl["x"], lbl["y"])
                
                text = lbl["text"]
                w = len(text) * 5.0 + 4
                h = 9.0
                rect = QRectF(-w / 2.0, -h / 2.0, w, h)
                
                # Semi-translucent backdrop
                painter.setBrush(QBrush(QColor(252, 250, 242, 150)))
                painter.setPen(Qt.NoPen)
                painter.drawRect(rect)
                
                # Pick color based on area type
                atype = lbl["area_type"]
                if atype == "waterbody":
                    color = QColor("#2980B9") # Water blue
                elif atype == "forest":
                    color = QColor("#2E7D32") # Dark green
                else:
                    color = QColor("#795548") # Peat bog brown
                    
                font = QFont("Segoe UI", 7)
                font.setItalic(True)
                painter.setFont(font)
                painter.setPen(color)
                painter.drawText(rect, Qt.AlignCenter, text)
                painter.restore()
                
    # 3. Draw Instantaneous Rendering Indicator (Overlay in Top-Right)
    if widget.rendering_in_progress:
        painter.save()
        margin_right = 20
        margin_top = 20
        indicator_w = 120
        indicator_h = 30
        
        rect = QRectF(widget.width() - indicator_w - margin_right, margin_top, indicator_w, indicator_h)
        
        # Semi-translucent dark grey rounded pill backplate
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(44, 62, 80, 200)))
        painter.drawRoundedRect(rect, 15, 15)
        
        # White label text
        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(rect, Qt.AlignCenter, "⏳ RENDERING...")
        painter.restore()
        
    if not widget.is_interacting and hasattr(widget, "is_rendering_detailed") and widget.is_rendering_detailed:
        QTimer.singleShot(0, widget.reset_rendering_detailed)
