# render_worker.py

import sqlite3
import struct
import array
import math
from PySide6.QtCore import QThread, Signal, QPointF
from PySide6.QtGui import QImage, QPolygonF
import renderer
import rules
from utils import simplify_path

class MapPolygon:
    """
    Mock class for MapPolygon supporting the viewer.py/renderer.py interface
    without upfront in-memory grids.
    """
    def __init__(self, polygon, sub_type=None, name=None, min_x=0.0, min_y=0.0, max_x=0.0, max_y=0.0):
        self.polygon = polygon
        self.sub_type = sub_type
        self.simplified_polygons = None
        self.name = name
        self.min_x = min_x
        self.min_y = min_y
        self.max_x = max_x
        self.max_y = max_y
        self.area = (max_x - min_x) * (max_y - min_y)
        self.length = max(max_x - min_x, max_y - min_y)


class MapRenderWorker(QThread):
    """
    Asynchronous rendering thread that queries only visible map features from SQLite,
    simplifies them on-the-fly, and renders them onto an off-screen QImage canvas.
    """
    render_completed = Signal(QImage)
    
    def __init__(self, width, height, center_x, center_y, scale, db_path, map_data, zoom_details, colors, frame_budget):
        super().__init__()
        self.width = width
        self.height = height
        self.center_x = center_x
        self.center_y = center_y
        self.scale = scale
        self.db_path = db_path
        self.map_data = map_data # contains places list
        self.zoom_details = zoom_details
        self.colors = colors
        self.frame_budget = frame_budget
        
    def run(self):
        # 1. Compute viewport bounds in Mercator meters
        vx_half = (self.width / 2.0) / self.scale
        vy_half = (self.height / 2.0) / self.scale
        vx1 = self.center_x - vx_half
        vx2 = self.center_x + vx_half
        vy1 = self.center_y - vy_half
        vy2 = self.center_y + vy_half
        
        # 2. Resolve LOD details
        sim_key = None
        for k in sorted(self.zoom_details.keys(), key=float):
            if float(k) <= self.scale:
                sim_key = k
            else:
                break
        if sim_key is None:
            sim_key = "0.0001"
            
        current_details = self.zoom_details.get(sim_key, {})
        lod_roads_threshold = current_details.get("roads", 1)
        tol = current_details.get("simplification", 0.0)
        
        # 3. Query visible features from SQLite
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Optimization: retrieve only items that overlap the viewport bounding box
        cursor.execute("""
            SELECT feature_type, sub_type, name, min_x, min_y, max_x, max_y, coords 
            FROM ways 
            WHERE NOT (max_x < ? OR min_x > ? OR max_y < ? OR min_y > ?)
        """, (vx1, vx2, vy1, vy2))
        
        rows = cursor.fetchall()
        conn.close()
        
        # Initialize visibility lists
        render_data = {
            "places": self.map_data.get("places", []),
            "coastlines": [],
            "forests": [],
            "wetlands": [],
            "waterbodies": [],
            "rivers": [],
            "boundaries": [],
            "railways": [],
            "roads": {rtype: [] for rtype in rules.ROAD_CATEGORIES}
        }
        
        for ftype, sub_type, name, min_x, min_y, max_x, max_y, coords_blob in rows:
            if self.isInterruptionRequested():
                return
                
            # Perform LOD pre-filtering
            if ftype == "highway":
                parent_type = rules.get_parent_road_type(sub_type)
                if parent_type not in rules.ROAD_CATEGORIES:
                    continue
                road_idx = rules.ROAD_CATEGORIES.index(parent_type)
                if road_idx >= lod_roads_threshold:
                    continue
            elif ftype == "boundary":
                if sub_type == '2':
                    if self.scale < 0.0004:
                        continue
                else:
                    if self.scale < 0.003:
                        continue
            elif ftype == "railway":
                if self.scale < 0.003:
                    continue
                    
            # Size-based filtering (skip tiny features relative to scale)
            if ftype in ("wetland", "forest", "waterbody"):
                area = (max_x - min_x) * (max_y - min_y)
                if area < tol * tol:
                    continue
            elif ftype in ("river", "railway", "boundary", "highway"):
                length = max(max_x - min_x, max_y - min_y)
                if length < tol:
                    continue
                    
            # Parse double coordinates blob
            coords = array.array('d', coords_blob)
            points = [QPointF(coords[i], coords[i+1]) for i in range(0, len(coords), 2)]
            if len(points) < 2:
                continue
                
            # On-the-fly path simplification
            if tol > 0.0 and len(points) > 50:
                points = simplify_path(points, tol)
                
            poly = QPolygonF(points)
            item = MapPolygon(poly, sub_type, name, min_x, min_y, max_x, max_y)
            
            if ftype == "coastline":
                render_data["coastlines"].append(item)
            elif ftype == "forest":
                render_data["forests"].append(item)
            elif ftype == "wetland":
                render_data["wetlands"].append(item)
            elif ftype == "waterbody":
                render_data["waterbodies"].append(item)
            elif ftype == "river":
                render_data["rivers"].append(item)
            elif ftype == "railway":
                render_data["railways"].append(item)
            elif ftype == "boundary":
                render_data["boundaries"].append(item)
            elif ftype == "highway":
                parent_type = rules.get_parent_road_type(sub_type)
                render_data["roads"][parent_type].append(item)
                
        if self.isInterruptionRequested():
            return
            
        # 4. Render the visible shapes onto QImage
        img = renderer.render_map(
            self.width, self.height,
            self.center_x, self.center_y, self.scale,
            render_data, self.zoom_details, self.colors,
            self.frame_budget,
            is_interruption_requested=self.isInterruptionRequested
        )
        
        if img and not self.isInterruptionRequested():
            self.render_completed.emit(img)
