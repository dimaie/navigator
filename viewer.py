import os
import sys
import math
import sqlite3
import json
import array
import struct
from PySide6.QtCore import Qt, QPointF, QRectF, QThread, Signal, QPoint, QTimer, QStringListModel
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QFontMetrics, QPolygonF
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QLineEdit, QPushButton, QLabel, QStatusBar, QToolBar, QProgressBar, 
                             QMessageBox, QCompleter, QSizePolicy, QFrame, QComboBox, QColorDialog,
                             QFileDialog, QSlider, QDialog, QListWidget, QMenu, QDialogButtonBox,
                             QPlainTextEdit)
import constants
import renderer
import rules



# Settings file path
SETTINGS_PATH = r"./config.json"

from utils import inverse_mercator, simplify_path, project_mercator
from render_worker import MapRenderWorker
from routing_worker import RoutingWorker


class MapPolygon:
    """
    A simple wrapper for PySide6's QPolygonF object.
    
    QPolygonF binds to a C++ representation and is unhashable in Python, meaning
    it cannot be stored directly in Python sets. This class wraps QPolygonF and
    utilizes Python's default object identity hashing, allowing it to be indexed
    efficiently inside our SpatialGridIndex.
    """
    def __init__(self, polygon, sub_type=None, simplified_polygons=None, min_x=0.0, min_y=0.0, max_x=0.0, max_y=0.0, name=None):
        self.polygon = polygon  # The QPolygonF or QPolygon representing coordinates
        self.sub_type = sub_type  # Sub-category identifier (e.g., specific road types or lake types)
        self.simplified_polygons = simplified_polygons  # Dict mapping scale keys to simplified QPolygonF
        self.name = name
        self.min_x = min_x
        self.min_y = min_y
        self.max_x = max_x
        self.max_y = max_y
        
        # O(1) dimensions precalculated from database bounding box columns
        self.area = (max_x - min_x) * (max_y - min_y)
        self.length = max(max_x - min_x, max_y - min_y)


class SpatialGridIndex:
    """
    A high-performance 2D Grid Spatial Index to speed up viewport queries.
    
    This splits the geographic bounding box of Ireland into a fixed grid of 
    rows and columns. Polygons are registered in every cell they overlap. When
    querying, we retrieve elements only from cells overlapping the current screen
    viewport, reducing containment and painting overhead to O(1) grid cell checks.
    """
    def __init__(self, bbox, cols=24, rows=24):
        # Bounding box of the region covered by the grid index
        self.min_x = bbox["min_x"]
        self.max_x = bbox["max_x"]
        self.min_y = bbox["min_y"]
        self.max_y = bbox["max_y"]
        self.cols = cols
        self.rows = rows
        
        # Calculate width and height of each individual grid cell
        self.cell_w = max(1.0, (self.max_x - self.min_x) / cols)
        self.cell_h = max(1.0, (self.max_y - self.min_y) / rows)
        
        # Initialize the 2D grid matrix of empty lists
        self.grid = [[[] for _ in range(rows)] for _ in range(cols)]
        
    def add(self, item, min_x, min_y, max_x, max_y):
        """
        Inserts an item into all grid cells that overlap its bounding box.
        """
        # Convert coordinate bounding box to grid cell indices
        c1 = int((min_x - self.min_x) / self.cell_w)
        c2 = int((max_x - self.min_x) / self.cell_w)
        r1 = int((min_y - self.min_y) / self.cell_h)
        r2 = int((max_y - self.min_y) / self.cell_h)
        
        # Clamp indices to the boundaries of the grid
        c1 = max(0, min(self.cols - 1, c1))
        c2 = max(0, min(self.cols - 1, c2))
        r1 = max(0, min(self.rows - 1, r1))
        r2 = max(0, min(self.rows - 1, r2))
        
        # Add the item to each overlapping cell
        for c in range(c1, c2 + 1):
            for r in range(r1, r2 + 1):
                self.grid[c][r].append(item)
                
    def query(self, viewport_rect):
        """
        Retrieves all unique items stored within the cells overlapping the viewport.
        """
        vx1 = viewport_rect.left()
        vx2 = viewport_rect.right()
        vy1 = viewport_rect.top()
        vy2 = viewport_rect.bottom()
        
        # Map viewport boundaries to cell indices
        c1 = int((vx1 - self.min_x) / self.cell_w)
        c2 = int((vx2 - self.min_x) / self.cell_w)
        r1 = int((vy1 - self.min_y) / self.cell_h)
        r2 = int((vy2 - self.min_y) / self.cell_h)
        
        # Clamp cell range to actual grid limits
        c1 = max(0, min(self.cols - 1, c1))
        c2 = max(0, min(self.cols - 1, c2))
        r1 = max(0, min(self.rows - 1, r1))
        r2 = max(0, min(self.rows - 1, r2))
        
        # Collect distinct elements using a set to eliminate duplicate entries
        results = set()
        for c in range(c1, c2 + 1):
            for r in range(r1, r2 + 1):
                results.update(self.grid[c][r])
        return results


class MapDataLoader(QThread):
    """
    Asynchronous map loader thread that queries SQLite and builds spatial indexes.
    
    This thread queries coordinate data in binary double arrays from the database,
    constructs QPolygonF shapes, registers them into respective SpatialGridIndexes,
    and returns a structured map data dictionary to the main thread.
    """
    data_loaded = Signal(dict)          # Emitted when all data is indexed
    progress_message = Signal(str)      # Updates startup loading progress text
    progress_pct = Signal(int)          # Emitted with load percentage
    error_occurred = Signal(str)        # Emitted when loader encounters an error
    
    def __init__(self, db_path, zoom_details=None):
        super().__init__()
        self.db_path = db_path
        self.zoom_details = zoom_details if zoom_details else {}
        
    def run(self):
        # Define helper to pre-simplify a list of points
        def make_simplified_polygons(points):
            if len(points) <= 50 or not self.zoom_details:
                return None
            simplified = {}
            for scale_str, details in self.zoom_details.items():
                tol = details.get("simplification", 0.0)
                if tol > 0.0:
                    simp_pts = simplify_path(points, tol)
                    simplified[scale_str] = QPolygonF(simp_pts)
            return simplified if simplified else None

        self.progress_pct.emit(0)
        self.progress_message.emit("Opening database...")
        if not os.path.exists(self.db_path):
            self.progress_message.emit("Database not found. Waiting...")
            return
            
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Verify the preprocessing step finished by checking config table
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='config'")
            if not cursor.fetchone():
                conn.close()
                return
                
            # One-time postcode table migration/creation if not exists
            try:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='postcodes'")
                if not cursor.fetchone():
                    self.progress_message.emit("Indexing Eircodes (one-time)...")
                    cursor.execute("""
                    CREATE TABLE postcodes (
                        postcode TEXT,
                        normalized_postcode TEXT PRIMARY KEY,
                        x REAL,
                        y REAL
                    )
                    """)
                    
                    postcode_data = {} # normalized_postcode -> (postcode, x, y)
                    
                    # Check raw_nodes table
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='raw_nodes'")
                    if cursor.fetchone():
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
                                
                    # Check raw_ways table
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='raw_ways'")
                    if cursor.fetchone():
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
                        conn.commit()
                    
                    cursor.execute("CREATE INDEX idx_postcodes_normalized ON postcodes (normalized_postcode)")
                    conn.commit()
            except Exception as e:
                print("Eircode index migration skipped/failed:", e)
                
            self.progress_pct.emit(5)
            self.progress_message.emit("Loading configuration...")
            cursor.execute("SELECT value FROM config WHERE key='bbox'")
            row = cursor.fetchone()
            if row:
                global_bbox = json.loads(row[0])
            else:
                # Dynamic fallback: query ways to get bounding box if config value is missing
                cursor.execute("SELECT MIN(min_x), MIN(min_y), MAX(max_x), MAX(max_y) FROM ways")
                bounds = cursor.fetchone()
                if bounds and bounds[0] is not None:
                    global_bbox = {
                        "min_x": bounds[0],
                        "min_y": bounds[1],
                        "max_x": bounds[2],
                        "max_y": bounds[3]
                    }
                else:
                    global_bbox = constants.WORLD_BBOX
            
            # Define the scale keys for multi-resolution LOD indexes
            scale_keys = ["0.0001", "0.0004", "0.001", "0.003", "0.01", "0.04"]
            
            # Load places (cities, towns, villages) for name search/labeling
            self.progress_pct.emit(10)
            self.progress_message.emit("Loading places...")
            cursor.execute("""
                SELECT p.id, p.name, p.place_type, p.x, p.y, p.population, n.tags 
                FROM places p 
                LEFT JOIN raw_nodes n ON p.id = n.id
                ORDER BY p.population DESC, p.place_type ASC
            """)
            places_rows = cursor.fetchall()
            places = []
            for r in places_rows:
                tags = {}
                if r[6]:
                    try:
                        tags = json.loads(r[6])
                    except:
                        pass
                county = tags.get("place_county") or tags.get("addr:county") or tags.get("is_in:county") or tags.get("addr:state")
                if not county and tags.get("is_in"):
                    is_in_parts = [p.strip() for p in tags["is_in"].split(",")]
                    for part in is_in_parts:
                        if "county" in part.lower():
                            county = part
                            break
                if county:
                    county = county.replace("County", "").replace("Co.", "").strip()
                places.append({
                    "id": r[0],
                    "name": r[1],
                    "place_type": r[2],
                    "x": r[3],
                    "y": r[4],
                    "population": r[5],
                    "county": county
                })
            
            # 8. Load all unique search names (places, roads/ways, postcodes) for incremental autocomplete
            self.progress_message.emit("Loading search index...")
            
            # Build places spatial grid
            if places:
                min_px = min(p["x"] for p in places)
                max_px = max(p["x"] for p in places)
                min_py = min(p["y"] for p in places)
                max_py = max(p["y"] for p in places)
                
                grid_size = 50
                dx = (max_px - min_px) / grid_size if max_px > min_px else 1.0
                dy = (max_py - min_py) / grid_size if max_py > min_py else 1.0
                
                grid = {}
                for p in places:
                    gx = int((p["x"] - min_px) / dx)
                    gy = int((p["y"] - min_py) / dy)
                    grid.setdefault((gx, gy), []).append(p)
            else:
                grid = {}
                
            def find_nearest_place(wx, wy):
                if not places:
                    return None
                gx = int((wx - min_px) / dx)
                gy = int((wy - min_py) / dy)
                best_dist = float('inf')
                best_place = None
                
                for r in range(0, 5):
                    cells_to_check = []
                    if r == 0:
                        cells_to_check = [(gx, gy)]
                    else:
                        for i in range(-r, r + 1):
                            cells_to_check.append((gx + i, gy - r))
                            cells_to_check.append((gx + i, gy + r))
                        for j in range(-r + 1, r):
                            cells_to_check.append((gx - r, gy + j))
                            cells_to_check.append((gx + r, gy + j))
                            
                    for cx, cy in cells_to_check:
                        for p in grid.get((cx, cy), []):
                            dist_sq = (p["x"] - wx)**2 + (p["y"] - wy)**2
                            if dist_sq < best_dist:
                                best_dist = dist_sq
                                best_place = p
                    if best_place and best_dist < (r * min(dx, dy))**2:
                        break
                if not best_place:
                    for p in places:
                        dist_sq = (p["x"] - wx)**2 + (p["y"] - wy)**2
                        if dist_sq < best_dist:
                            best_dist = dist_sq
                            best_place = p
                return best_place

            search_items = []
            seen_display_names = set()
            
            # Add places
            for p in places:
                name = p["name"]
                county = p["county"]
                display = f"{name}, Co. {county}" if county else name
                if display not in seen_display_names:
                    seen_display_names.add(display)
                    search_items.append((name, display))
                    search_items.append((display, display))
                    
                # Generically expand suffixes for stations in the in-memory autocomplete index
                if p.get("place_type") == "station":
                    name_lower = name.lower()
                    if not name_lower.endswith("station"):
                        for suffix in ["Station", "Train Station"]:
                            suffixed_name = f"{name} {suffix}"
                            suffixed_display = f"{suffixed_name}, Co. {county}" if county else suffixed_name
                            search_items.append((suffixed_name, suffixed_display))
                            search_items.append((suffixed_display, suffixed_display))
                    elif not name_lower.endswith("train station") and "train" not in name_lower:
                        # e.g., "Athlone Station" -> "Athlone Train Station"
                        suffixed_name = name.replace("Station", "Train Station").replace("station", "Train Station")
                        suffixed_display = f"{suffixed_name}, Co. {county}" if county else suffixed_name
                        search_items.append((suffixed_name, suffixed_display))
                        search_items.append((suffixed_display, suffixed_display))
                    
            # Add postcodes
            cursor.execute("SELECT DISTINCT postcode FROM postcodes WHERE postcode IS NOT NULL AND postcode != ''")
            for r in cursor.fetchall():
                pc = r[0]
                display = f"{pc} (Postcode)"
                if display not in seen_display_names:
                    seen_display_names.add(display)
                    search_items.append((pc, display))
                    search_items.append((display, display))
                    
            # Add named ways
            cursor.execute("SELECT name, min_x, min_y, max_x, max_y FROM ways WHERE name IS NOT NULL AND name != ''")
            ways_rows = cursor.fetchall()
            import re
            for name, min_x, min_y, max_x, max_y in ways_rows:
                wx = (min_x + max_x) / 2.0
                wy = (min_y + max_y) / 2.0
                nearest = find_nearest_place(wx, wy)
                if nearest:
                    t_name = nearest["name"]
                    t_county = nearest["county"]
                    display = f"{name}, {t_name}"
                    if t_county:
                        display += f", Co. {t_county}"
                else:
                    display = name
                    t_name = None
                    t_county = None
                
                if display not in seen_display_names:
                    seen_display_names.add(display)
                    # Parse combined names like "ref (name)" so both are indexed
                    m = re.match(r"^([^(]+)\s*\(([^)]+)\)$", name)
                    if m:
                        ref_part = m.group(1).strip()
                        inner_part = m.group(2).strip()
                        if ref_part:
                            search_items.append((ref_part, display))
                        if inner_part:
                            search_items.append((inner_part, display))
                        if inner_part and nearest:
                            desc_without_ref = f"{inner_part}, {t_name}"
                            if t_county:
                                desc_without_ref += f", Co. {t_county}"
                            search_items.append((desc_without_ref, display))
                    search_items.append((name, display))
                    search_items.append((display, display))
                    
            search_items.sort(key=lambda x: x[1].lower())
            
            # Check if routing tables exist in SQLite
            has_routing = False
            try:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='routing_nodes'")
                if cursor.fetchone():
                    has_routing = True
            except:
                pass
            
            conn.close()
            
            # Send loaded dictionaries back to the GUI main thread
            self.progress_pct.emit(95)
            self.progress_message.emit("Finalizing maps...")
            data = {
                "bbox": global_bbox,
                "places": places,
                "search_names": search_items,
                "has_routing": has_routing
            }
            self.progress_pct.emit(100)
            self.data_loaded.emit(data)
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.progress_message.emit(f"Error loading map: {str(e)}")
            self.error_occurred.emit(str(e))



class MapWidget(QWidget):
    """
    Custom QWidget designed to render vector geometries onto the screen.
    
    This component handles pan/zoom events, progressive rendering states, view 
    constraining, place-label collision calculations, and double-click feature detection.
    """
    coordinate_hover = Signal(float, float)  # Emitted on mouse-move with hovered Lat/Lon
    color_updated_signal = Signal(str, str)  # Emitted when a layer gets recolored
    route_start_changed = Signal(QPointF, str)
    route_end_changed = Signal(QPointF, str)
    search_requested = Signal(QPointF)
    status_message = Signal(str)

    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        
        # Camera & Navigation states
        self.center_x = 0.0           # Camera focus center in Web Mercator X meters
        self.center_y = 0.0           # Camera focus center in Web Mercator Y meters
        self.scale = 1.0              # Zoom scale factor (pixels per Mercator meter)
        self.min_scale = 0.00005      # Scale limit: Country overview zoom
        self.max_scale = 0.5          # Scale limit: Street level zoom
        
        self.dragging = False         # True when user is left-click dragging the map
        self.last_mouse_pos = QPoint()
        
        # Off-Screen Background Image Rendering States
        self.is_interacting = False
        self.rendering_in_progress = False
        self.map_image = None
        self.image_center_x = 0.0
        self.image_center_y = 0.0
        self.image_scale = 1.0
        self.image_w = 0
        self.image_h = 0
        self.render_worker = None
        
        # Debounce timer for triggering background rendering when panning/zooming stops
        self.render_debounce_timer = QTimer(self)
        self.render_debounce_timer.setSingleShot(True)
        self.render_debounce_timer.timeout.connect(self.trigger_background_render)
        
        # Spatial Indexes & geometries loaded from thread
        self.data_loaded = False
        self.global_bbox = None
        self.places = []
        
        # Routing and A* states
        self.routing_graph = {}
        self.routing_nodes_coords = {}
        self.has_routing = False
        self.route_start = None
        self.route_end = None
        self.current_route = None
        self.route_mode = False
        self.route_distance = 0.0
        self.route_duration = 0.0
        self.current_route_directions = []
        self.active_profile = {"use_speed": False, "multipliers": {}}
        self.db_path = None
        
        # Default modern Light Mode palette settings
        self.default_colors = constants.DEFAULT_COLORS
        
        self.load_settings()
        
        # Run startup benchmark to estimate CPU capacity and frame budget
        self.frame_budget = self.benchmark_cpu_capacity()
        
        # Create premium zoom control panel overlay
        self.zoom_panel = QFrame(self)
        self.zoom_panel.setObjectName("ZoomPanel")
        
        zoom_layout = QVBoxLayout(self.zoom_panel)
        zoom_layout.setContentsMargins(4, 4, 4, 4)
        zoom_layout.setSpacing(6)
        zoom_layout.setAlignment(Qt.AlignCenter)
        
        self.btn_slider_in = QPushButton("＋")
        self.btn_slider_in.setToolTip("Zoom In")
        self.btn_slider_in.clicked.connect(self.zoom_in)
        zoom_layout.addWidget(self.btn_slider_in)
        
        self.zoom_slider = QSlider(Qt.Vertical)
        self.zoom_slider.setRange(0, 100)
        self.zoom_slider.setTickPosition(QSlider.NoTicks)
        self.zoom_slider.valueChanged.connect(self.on_zoom_slider_changed)
        zoom_layout.addWidget(self.zoom_slider)
        
        self.btn_slider_out = QPushButton("－")
        self.btn_slider_out.setToolTip("Zoom Out")
        self.btn_slider_out.clicked.connect(self.zoom_out)
        zoom_layout.addWidget(self.btn_slider_out)
        
        # Apply premium stylesheet style to zoom panel overlay
        self.zoom_panel.setStyleSheet(constants.ZOOM_PANEL_STYLESHEET)

    def benchmark_cpu_capacity(self):
        """
        Measures the CPU performance by running a short mathematical and array operation loop,
        returning the maximum coordinate budget per frame target for a stable 60 FPS.
        """
        import time
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPolygonF
        
        # Generate a synthetic set of points
        points = [QPointF(float(i) * 0.1, float(i) * 0.2) for i in range(100)]
        
        start = time.perf_counter()
        # Perform iterations simulating geometry processing
        for _ in range(300):
            poly = QPolygonF(points)
            for i in range(5):
                pt = poly[i]
                _ = pt.x() * pt.y()
        end = time.perf_counter()
        elapsed = end - start
        
        # 300 iterations * 100 points = 30,000 points processed.
        # Estimate capacity for a target 250ms background render time.
        points_per_sec = 30000.0 / max(0.0001, elapsed)
        budget = points_per_sec * 0.250  # 250ms background rendering target
        
        # Clamp to reasonable bounds: 200,000 to 2,500,000 points
        budget = max(200000.0, min(2500000.0, budget))
        print(f"Machine benchmark capacity: {points_per_sec:.0f} points/sec. Assigned target budget: {budget:.0f} points/frame.")
        return budget
        
    def load_settings(self):
        """
        Loads customized developer options (color palette overrides, reference LOD values)
        from a local JSON settings file. Falls back to default values if unavailable.
        """
        self.colors = dict(self.default_colors)
        # Default reference scale to zoom details mapping
        self.default_zoom_details = constants.DEFAULT_ZOOM_DETAILS
        self.zoom_details = dict(self.default_zoom_details)
        self.routing_profiles = {
            "Car": {"driving_side": "left", "fallback_profile": "Bicycle", "distance_weight": 0.1, "speed_weight": 0.9, "prohibited_links": ["path", "footway", "cycleway", "pedestrian"], "speeds": {}, "multipliers": {}},
            "Bicycle": {"driving_side": "left", "fallback_profile": "Walk", "distance_weight": 0.5, "speed_weight": 0.5, "prohibited_links": ["motorway", "motorway_link"], "speeds": {}, "multipliers": {}},
            "Walk": {"driving_side": "left", "fallback_profile": None, "distance_weight": 0.9, "speed_weight": 0.1, "prohibited_links": ["motorway", "motorway_link", "trunk", "trunk_link"], "speeds": {}, "multipliers": {}}
        }
        
        if os.path.exists(constants.SETTINGS_PATH):
            try:
                with open(constants.SETTINGS_PATH, 'r') as f:
                    data = json.load(f)
                    if "colors" in data:
                        self.colors.update(data["colors"])
                    if "zoom_details" in data:
                        self.zoom_details = data["zoom_details"]
                    if "routing_profiles" in data:
                        self.routing_profiles = data["routing_profiles"]
            except Exception as e:
                print("Error loading settings:", e)
                
        self.apply_theme_colors()

 
    def save_settings(self):
        """
        Saves the current developer modifications (custom colors, LOD mappings) and viewport info to JSON.
        """
        try:
            viewports = {}
            routing_profiles = {}
            # Load existing viewports to prevent overwriting other map states
            if os.path.exists(constants.SETTINGS_PATH):
                try:
                    with open(constants.SETTINGS_PATH, 'r') as f:
                        old_data = json.load(f)
                        viewports = old_data.get("viewports", {})
                        routing_profiles = old_data.get("routing_profiles", {})
                except:
                    pass
            
            # Save the current viewport state under the current map database name
            if hasattr(self, "db_name") and self.db_name:
                viewports[self.db_name] = {
                    "center_x": self.center_x,
                    "center_y": self.center_y,
                    "scale": self.scale
                }
                
            data = {
                "colors": self.colors,
                "zoom_details": self.zoom_details,
                "viewports": viewports,
                "routing_profiles": routing_profiles if routing_profiles else self.routing_profiles
            }
            with open(constants.SETTINGS_PATH, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print("Error saving settings:", e)

 
    def apply_theme_colors(self):
        """
        Initializes PySide6 QColor objects matching the active theme settings.
        """
        self.color_ocean = QColor(self.colors["ocean"])
        self.color_land = QColor(self.colors["land"])
        self.color_land_border = QColor(self.colors["land_border"])
        self.color_forest = QColor(self.colors["forest"])
        self.color_wetland = QColor(self.colors["wetland"])
        self.color_wetland_border = QColor(self.colors["wetland_border"])
        self.color_waterbody = QColor(self.colors["waterbody"])
        self.color_waterbody_border = QColor(self.colors["waterbody_border"])
        self.color_river = QColor(self.colors["river"])
        
        # Pen colors for road cores
        self.road_colors = {
            'motorway': QColor(self.colors["road_motorway"]),
            'trunk': QColor(self.colors["road_trunk"]),
            'primary': QColor(self.colors["road_primary"]),
            'secondary': QColor(self.colors["road_secondary"]),
            'tertiary': QColor(self.colors["road_tertiary"]),
            'unclassified': QColor(self.colors["road_unclassified"]),
            'residential': QColor(self.colors["road_residential"]),
            'living_street': QColor(self.colors["road_living_street"]),
            'service': QColor(self.colors["road_service"]),
            'pedestrian': QColor(self.colors["road_pedestrian"])
        }
        
        # Pen colors for high-contrast road casing borders
        self.road_casing_colors = {
            'motorway': QColor(self.colors["road_casing_motorway"]),
            'trunk': QColor(self.colors["road_casing_trunk"]),
            'primary': QColor(self.colors["road_casing_primary"]),
            'secondary': QColor(self.colors["road_casing_secondary"]),
            'tertiary': QColor(self.colors["road_casing_tertiary"]),
            'unclassified': QColor(self.colors["road_casing_unclassified"]),
            'residential': QColor(self.colors["road_casing_residential"]),
            'living_street': QColor(self.colors["road_casing_living_street"]),
            'service': QColor(self.colors["road_casing_service"]),
            'pedestrian': QColor(self.colors["road_casing_pedestrian"])
        }
 
    def reset_settings(self):
        """
        Deletes dev overrides, reverting both colors and detail scaling back to default configurations.
        """
        if os.path.exists(constants.SETTINGS_PATH):
            try:
                os.remove(constants.SETTINGS_PATH)
            except Exception as e:
                print("Error removing settings file:", e)
        self.load_settings()
        self.update()

    def set_map_data(self, data, db_name=None):
        """
        Receives structural indexes from the loader thread, centering and rendering the map.
        
        Attempts to restore a persisted viewport position (center coordinates and zoom scale)
        for this database, falling back to a full bounding box fit.
        """
        self.db_name = db_name
        self.global_bbox = data["bbox"]
        self.places = data["places"]
        self.has_routing = data.get("has_routing", False)
        
        # Reset routing graph caches for the newly loaded map
        self.routing_graph = {}
        self.routing_nodes_coords = {}
        
        # Calculate dynamic scales based on bounding box size
        if self.global_bbox:
            min_x, max_x = self.global_bbox["min_x"], self.global_bbox["max_x"]
            min_y, max_y = self.global_bbox["min_y"], self.global_bbox["max_y"]
            map_w = max(1.0, max_x - min_x)
            map_h = max(1.0, max_y - min_y)
            
            w_p = self.width() if self.width() > 0 else 1200
            h_p = self.height() if self.height() > 0 else 850
            
            fit_scale = min(w_p / map_w, h_p / map_h)
            self.min_scale = fit_scale * 0.5
            self.max_scale = fit_scale * 5000.0
        
        self.data_loaded = True
        
        # Check if viewport is saved for this specific database
        saved_vp = None
        if db_name and os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, 'r') as f:
                    settings_data = json.load(f)
                    saved_vp = settings_data.get("viewports", {}).get(db_name)
            except:
                pass
                
        if saved_vp:
            self.center_x = saved_vp["center_x"]
            self.center_y = saved_vp["center_y"]
            self.scale = saved_vp["scale"]
            self.constrain_view()
            self.start_interaction()
        else:
            self.fit_to_bbox(self.global_bbox)
        
    def fit_to_bbox(self, bbox):
        """
        Rescales the map viewport to fit the entire bounded box of the country.
        """
        if not bbox:
            return
        w_m = bbox["max_x"] - bbox["min_x"]
        h_m = bbox["max_y"] - bbox["min_y"]
        
        w_p = self.width() if self.width() > 0 else 800
        h_p = self.height() if self.height() > 0 else 600
        
        # Calculate aspect ratio scale factors
        scale_x = (w_p * 0.95) / w_m
        scale_y = (h_p * 0.95) / h_m
        
        self.scale = min(scale_x, scale_y)
        self.scale = max(self.min_scale, min(self.max_scale, self.scale))
        
        # Center the focus camera position
        self.center_x = (bbox["min_x"] + bbox["max_x"]) / 2.0
        self.center_y = (bbox["min_y"] + bbox["max_y"]) / 2.0
        
        self.start_interaction()
        self.constrain_view()
        self.update()

    def to_screen(self, x, y):
        """
        Converts Web Mercator coordinates (meters) to screen pixel coordinates (X, Y).
        """
        px = self.width() / 2.0 + (x - self.center_x) * self.scale
        py = self.height() / 2.0 - (y - self.center_y) * self.scale
        return px, py

    def to_mercator(self, px, py):
        """
        Converts screen pixel coordinates (X, Y) back to Web Mercator coordinates (meters).
        """
        x = self.center_x + (px - self.width() / 2.0) / self.scale
        y = self.center_y - (py - self.height() / 2.0) / self.scale
        return x, y

    def constrain_view(self):
        """
        Clamps the camera focal point to prevent the user from panning away from Ireland's landmass.
        Includes a 15% boundary padding.
        """
        if not self.global_bbox:
            return
        min_x, max_x = self.global_bbox["min_x"], self.global_bbox["max_x"]
        min_y, max_y = self.global_bbox["min_y"], self.global_bbox["max_y"]
        
        map_w = max_x - min_x
        map_h = max_y - min_y
        
        pad_x = map_w * 0.15
        pad_y = map_h * 0.15
        
        self.center_x = max(min_x - pad_x, min(max_x + pad_x, self.center_x))
        self.center_y = max(min_y - pad_y, min(max_y + pad_y, self.center_y))
        
        self.update_zoom_slider()

    def update_zoom_slider(self):
        """
        Updates the zoom slider position to match the current camera scale.
        """
        if hasattr(self, "zoom_slider") and self.zoom_slider:
            self.updating_slider = True
            min_s = self.min_scale
            max_s = self.max_scale
            clamped_scale = max(min_s, min(max_s, self.scale))
            val = int(100 * math.log(clamped_scale / min_s) / math.log(max_s / min_s))
            self.zoom_slider.setValue(val)
            self.updating_slider = False
            
    def on_zoom_slider_changed(self, value):
        """
        Triggers camera scale adjustments corresponding to slider movement.
        """
        if not self.data_loaded:
            return
        if getattr(self, "updating_slider", False):
            return
        min_s = self.min_scale
        max_s = self.max_scale
        new_scale = min_s * math.exp((value / 100.0) * math.log(max_s / min_s))
        new_scale = max(min_s, min(max_s, new_scale))
        
        if new_scale == self.scale:
            return
            
        self.scale = new_scale
        self.start_interaction()
        self.constrain_view()
        self.update()

    def get_details_for_scale(self, scale):
        """
        Interpolates zoom details (roads, places, forests, wetlands, waterbodies, rivers)
        for a given scale.
        """
        # Parse points. keys of self.zoom_details are scales.
        points = []
        for k, v in self.zoom_details.items():
            try:
                points.append((float(k), v))
            except ValueError:
                continue
        points.sort(key=lambda x: x[0])
        
        if not points:
            return {"roads": 7, "places": 0, "forests": 0, "wetlands": 0, "waterbodies": 0, "rivers": 0}
            
        # Helper to interpolate between two dict values
        def interp_dict(v1, v2, t):
            res = {}
            for key in ["roads", "places", "forests", "wetlands", "waterbodies", "rivers"]:
                val1 = v1.get(key, 0.0)
                val2 = v2.get(key, 0.0)
                val = val1 + t * (val2 - val1)
                if key == "roads":
                    res[key] = int(round(val))
                else:
                    res[key] = val
            return res
            
        # Clamp scale to endpoints
        if scale <= points[0][0]:
            res = dict(points[0][1])
            res["roads"] = int(round(res["roads"]))
            return res
        if scale >= points[-1][0]:
            res = dict(points[-1][1])
            res["roads"] = int(round(res["roads"]))
            return res
            
        # Interpolate between intermediate scale points
        for i in range(len(points) - 1):
            s1, v1 = points[i]
            s2, v2 = points[i+1]
            if s1 <= scale <= s2:
                t = (scale - s1) / (s2 - s1)
                return interp_dict(v1, v2, t)
                
        # Fallback
        res = dict(points[-1][1])
        res["roads"] = int(round(res["roads"]))
        return res

    def get_simplified_polygon(self, item, scale):
        """
        Retrieves the pre-simplified polygon/polyline for the given item at the current zoom scale.
        """
        if not hasattr(item, "simplified_polygons") or not item.simplified_polygons:
            return item.polygon
            
        target_key = None
        for k in sorted(item.simplified_polygons.keys(), key=float):
            if float(k) <= scale:
                target_key = k
            else:
                break
                
        if target_key is None:
            target_key = "0.0001"
            
        return item.simplified_polygons.get(target_key, item.polygon)

    def zoom_in(self):
        """
        Zooms in by scaling up coordinates centered on current viewport focus.
        """
        if not self.data_loaded:
            return
        if self.scale >= self.max_scale:
            return
        self.scale *= 1.4
        self.scale = min(self.max_scale, self.scale)
        self.start_interaction()
        self.constrain_view()
        self.update()
        
    def zoom_out(self):
        """
        Zooms out by scaling down coordinates centered on current viewport focus.
        """
        if not self.data_loaded:
            return
        if self.scale <= self.min_scale:
            return
        self.scale /= 1.4
        self.scale = max(self.min_scale, self.scale)
        self.start_interaction()
        self.constrain_view()
        self.update()

    def get_road_width(self, road_type, ignore_interaction=False):
        """
        Computes the target road width in pixels based on category and zoom scale.
        Delegated to rules.py.
        """
        lod = getattr(self, "current_details", {}).get("roads")
        if lod is None:
            lod = self.get_details_for_scale(self.scale)["roads"]
        return rules.get_road_width_for_scale(road_type, self.scale, self.is_interacting, lod, ignore_interaction)

    # Asynchronous Off-Screen Rendering Methods
    def abort_rendering(self):
        """
        Cancels the debounce timer and requests interruption of the worker thread.
        Blocks until the thread is fully joined to guarantee clean shutdown.
        """
        if hasattr(self, "render_debounce_timer") and self.render_debounce_timer.isActive():
            self.render_debounce_timer.stop()
            
        if hasattr(self, "render_worker") and self.render_worker and self.render_worker.isRunning():
            self.render_worker.requestInterruption()
            self.render_worker.wait()
            self.render_worker = None
            
        self.rendering_in_progress = False

    def trigger_background_render(self):
        """
        Launches the background MapRenderWorker thread to render the map asynchronously.
        """
        if not self.data_loaded:
            return
            
        self.abort_rendering()
        
        # Collect data structures to pass to background thread
        map_data = {
            "places": self.places
        }
        
        self.render_worker = MapRenderWorker(
            self.width(), self.height(),
            self.center_x, self.center_y, self.scale,
            self.db_path, map_data, self.zoom_details, self.colors,
            self.frame_budget
        )
        self.render_worker.render_completed.connect(self.on_render_completed)
        self.rendering_in_progress = True
        self.render_worker.start()
        self.update()

    def on_render_completed(self, img):
        """
        Slot triggered when the background QImage rendering is complete.
        Saves the new image and triggers a screen refresh.
        """
        self.map_image = img
        self.image_center_x = self.center_x
        self.image_center_y = self.center_y
        self.image_scale = self.scale
        self.image_w = self.width()
        self.image_h = self.height()
        
        self.is_interacting = False
        self.rendering_in_progress = False
        self.save_settings()
        self.update()

    def start_interaction(self):
        """
        Triggers interactive transform mode. Stretches/translates the current image,
        and starts a 150ms timer to launch a fresh background render once user input halts.
        """
        self.is_interacting = True
        self.render_debounce_timer.start(150)
        self.update()

    def clear_rendering_indicator(self):
        """
        Failsafe method to force clearing the overlay.
        """
        self.rendering_in_progress = False
        self.update()

    # Mouse actions
    def mousePressEvent(self, event):
        """
        Initiates camera panning when left button is pressed.
        """
        if not self.data_loaded:
            return
        if event.button() == Qt.LeftButton:
            self.dragging = True
            self.last_mouse_pos = event.position().toPoint()
            self.press_pos = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseReleaseEvent(self, event):
        """
        Terminates active panning. Sets route points on static click.
        """
        if not self.data_loaded:
            return
        if event.button() == Qt.LeftButton:
            self.dragging = False
            self.setCursor(Qt.ArrowCursor)
            
            if hasattr(self, 'press_pos'):
                release_pos = event.position().toPoint()
                dist = (release_pos - self.press_pos).manhattanLength()
                if dist < 5:
                    self.handle_map_click(event.position().x(), event.position().y())

    def mouseMoveEvent(self, event):
        """
        Pans the map view relative to movement delta and converts pointer to coordinates.
        """
        if not self.data_loaded:
            return
        px = event.position().x()
        py = event.position().y()
        
        # Emit coordinates for status bar coordinates display
        mx, my = self.to_mercator(px, py)
        lat, lon = inverse_mercator(mx, my)
        self.coordinate_hover.emit(lat, lon)
        
        if self.dragging:
            curr_pos = event.position().toPoint()
            dp = curr_pos - self.last_mouse_pos
            
            # Map screen pixel displacement to Web Mercator meters
            dx = dp.x() / self.scale
            dy = -dp.y() / self.scale
            
            self.center_x -= dx
            self.center_y -= dy
            
            self.start_interaction()
            self.constrain_view()
            self.last_mouse_pos = curr_pos
            self.update()

    def wheelEvent(self, event):
        """
        Handles zoom actions keeping the geographic center of the map constant.
        """
        if not self.data_loaded:
            return
        zoom_factor = 1.25 if event.angleDelta().y() > 0 else 0.8
        new_scale = self.scale * zoom_factor
        new_scale = max(self.min_scale, min(self.max_scale, new_scale))
        
        if new_scale == self.scale:
            event.accept()
            return
            
        self.scale = new_scale
        self.start_interaction()
        self.constrain_view()
        self.update()

    def find_nearest_address(self, mx, my):
        if not self.db_path:
            return f"{mx:.1f}, {my:.1f}"
        
        best_place = None
        best_place_dist = float('inf')
        for p in self.places:
            dist = math.sqrt((p["x"] - mx)**2 + (p["y"] - my)**2)
            if dist < best_place_dist:
                best_place_dist = dist
                best_place = p
        
        if best_place and best_place_dist < 1000:
            county = best_place.get("county")
            return f"{best_place['name']}, Co. {county}" if county else best_place['name']
            
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            r = 2000
            cursor.execute("""
                SELECT name, min_x, min_y, max_x, max_y 
                FROM ways 
                WHERE name IS NOT NULL AND name != ''
                  AND min_x <= ? AND max_x >= ? AND min_y <= ? AND max_y >= ?
            """, (mx + r, mx - r, my + r, my - r))
            rows = cursor.fetchall()
            if rows:
                best_way = None
                best_way_dist = float('inf')
                for name, min_x, min_y, max_x, max_y in rows:
                    cx = (min_x + max_x) / 2.0
                    cy = (min_y + max_y) / 2.0
                    dist = (cx - mx)**2 + (cy - my)**2
                    if dist < best_way_dist:
                        best_way_dist = dist
                        best_way = name
                if best_way:
                    conn.close()
                    if best_place:
                        county = best_place.get("county")
                        suffix = f", Co. {county}" if county else ""
                        return f"{best_way}, {best_place['name']}{suffix}"
                    return best_way
            conn.close()
        except Exception as e:
            print("Error finding nearest way:", e)
            
        if best_place:
            county = best_place.get("county")
            return f"{best_place['name']}, Co. {county}" if county else best_place['name']
            
        lat, lon = inverse_mercator(mx, my)
        return f"{lat:.5f}, {lon:.5f}"

    def contextMenuEvent(self, event):
        if not self.data_loaded:
            return
            
        menu = QMenu(self)
        
        px = event.pos().x()
        py = event.pos().y()
        mx, my = self.to_mercator(px, py)
        click_pt = QPointF(mx, my)
        
        act_search = menu.addAction("🔍 Search Place...")
        
        act_directions = None
        if self.current_route is not None:
            act_directions = menu.addAction("📄 Show Route Directions")
            
        action = menu.exec(event.globalPos())
        
        if action == act_search:
            self.search_requested.emit(click_pt)
        elif act_directions and action == act_directions:
            self.show_route_directions()

    def show_route_directions(self):
        if not self.current_route_directions:
            QMessageBox.information(self, "Directions", "No route directions available.")
            return
            
        driving_side = self.active_profile.get("driving_side", "left")
        dialog = RouteDirectionsDialog(self.current_route_directions, driving_side, self)
        dialog.exec()

    def handle_map_click(self, px, py):
        """
        Handles left-click routing on the map. Cycles Start (A) and Destination (B).
        """
        if not self.has_routing:
            return
            
        mx, my = self.to_mercator(px, py)
        click_pt = QPointF(mx, my)
        
        if self.route_start is None:
            self.route_start = click_pt
            self.current_route = None
            self.route_distance = 0.0
            self.route_duration = 0.0
            
            desc = self.find_nearest_address(mx, my)
            self.route_start_changed.emit(click_pt, desc)
            self.status_message.emit(f"Start point set: {desc}")
            self.update()
        elif self.route_end is None:
            self.route_end = click_pt
            
            desc = self.find_nearest_address(mx, my)
            self.route_end_changed.emit(click_pt, desc)
            self.status_message.emit(f"Destination set: {desc}. Calculating route...")
            self.update()
            self.calculate_route()
        else:
            self.route_start = click_pt
            self.route_end = None
            self.current_route = None
            self.route_distance = 0.0
            self.route_duration = 0.0
            
            desc = self.find_nearest_address(mx, my)
            self.route_start_changed.emit(click_pt, desc)
            self.route_end_changed.emit(QPointF(), "")
            self.status_message.emit(f"Start point reset: {desc}")
            self.update()

    def calculate_route(self):
        """
        Launches the background thread to calculate the path using A*.
        """
        if not self.has_routing or not self.route_start or not self.route_end:
            return
            
        if hasattr(self, "routing_worker") and self.routing_worker and self.routing_worker.isRunning():
            return
            
        db_path = self.db_path
        profile_name = getattr(self, "active_profile_name", "Car")
        
        self.routing_worker = RoutingWorker(
            self.route_start, self.route_end,
            self.routing_graph, self.routing_nodes_coords,
            db_path, profile_name, self.routing_profiles
        )
        self.routing_worker.graph_loaded.connect(self.on_graph_loaded)
        self.routing_worker.route_completed.connect(self.on_route_completed)
        self.routing_worker.route_failed.connect(self.on_route_failed)
        self.routing_worker.start()
        
    def on_graph_loaded(self, graph, coords):
        self.routing_graph = graph
        self.routing_nodes_coords = coords
        
    def on_route_completed(self, pts, dist, duration, graph, coords, used_profile, directions):
        self.current_route = pts
        self.route_distance = dist
        self.route_duration = duration
        self.routing_graph = graph
        self.routing_nodes_coords = coords
        self.current_route_directions = directions
        
        dist_km = dist / 1000.0
        dur_mins = duration / 60.0
        if dur_mins >= 60.0:
            dur_hours = int(dur_mins // 60)
            dur_mins_rem = int(dur_mins % 60)
            dur_str = f"{dur_hours}h {dur_mins_rem}m"
        else:
            dur_str = f"{int(dur_mins)}m"
            
        profile_msg = ""
        active_name = getattr(self, "active_profile_name", "Car")
        if used_profile != active_name:
            profile_msg = f" (via fallback: {used_profile})"
            
        msg = f"Route calculated: {dist_km:.1f} km ({dur_str}){profile_msg}"
        self.status_message.emit(msg)
        
        self.start_interaction()
        self.update()

    def on_route_failed(self, err_msg):
        self.route_end = None
        self.current_route_directions = []
        self.status_message.emit(err_msg)
        QMessageBox.warning(self, "Routing Failed", err_msg)
        self.update()

    def mouseDoubleClickEvent(self, event):
        """
        Performs vector hit-detection on the map to trigger a custom color picker.
        """
        if not self.data_loaded:
            return
        if event.button() == Qt.LeftButton:
            px = event.position().x()
            py = event.position().y()
            mx, my = self.to_mercator(px, py)
            click_point = QPointF(mx, my)
            
            matched_type = None
            
            # Query SQLite for polygons overlapping click point (mx, my)
            # Order by area ASC to pick smaller polygons (e.g. islands, lakes, bogs) before larger land mass
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT feature_type, coords 
                    FROM ways 
                    WHERE min_x <= ? AND max_x >= ? AND min_y <= ? AND max_y >= ?
                      AND feature_type IN ('waterbody', 'wetland', 'forest', 'coastline')
                """, (mx, mx, my, my))
                
                rows = cursor.fetchall()
                conn.close()
                
                # Check containment
                candidates = []
                for ftype, coords_blob in rows:
                    coords = array.array('d', coords_blob)
                    points = [QPointF(coords[i], coords[i+1]) for i in range(0, len(coords), 2)]
                    if len(points) >= 3:
                        poly = QPolygonF(points)
                        if poly.containsPoint(click_point, Qt.OddEvenFill):
                            # Calculate approximate bounding box area for sorting
                            min_px = min(p.x() for p in points)
                            max_px = max(p.x() for p in points)
                            min_py = min(p.y() for p in points)
                            max_py = max(p.y() for p in points)
                            area = (max_px - min_px) * (max_py - min_py)
                            candidates.append((area, ftype))
                
                if candidates:
                    candidates.sort(key=lambda x: x[0])
                    matched = candidates[0][1]
                    if matched == "coastline":
                        matched_type = "land"
                    else:
                        matched_type = matched
            except Exception as e:
                print("Error in double-click hit detection:", e)
                
            # Fallback to ocean if within global coordinates
            if not matched_type:
                if (self.global_bbox["min_x"] <= mx <= self.global_bbox["max_x"] and 
                     self.global_bbox["min_y"] <= my <= self.global_bbox["max_y"]):
                    matched_type = "ocean"
                    
            if matched_type:
                self.trigger_color_picker(matched_type)

    def trigger_color_picker(self, feature_type):
        """
        Displays QColorDialog, saving selection and globally updating matching layer colors.
        """
        current_hex = self.colors.get(feature_type)
        initial_color = QColor(current_hex) if current_hex else Qt.white
        
        color = QColorDialog.getColor(initial_color, self, f"Select Color for {feature_type.capitalize()}")
        if color.isValid():
            hex_color = color.name().upper()
            self.colors[feature_type] = hex_color
            
            # Auto-calculate darker border colors for aesthetic consistency
            if feature_type == "waterbody":
                self.colors["waterbody_border"] = color.darker(120).name().upper()
            elif feature_type == "wetland":
                self.colors["wetland_border"] = color.darker(110).name().upper()
            elif feature_type == "land":
                self.colors["land_border"] = color.darker(110).name().upper()
                
            self.apply_theme_colors()
            self.save_settings()
            self.color_updated_signal.emit(feature_type, hex_color)
            self.update()

    def resizeEvent(self, event):
        """
        Adapts view boundaries to new window size when resized.
        """
        self.start_interaction()
        self.constrain_view()
        # Position zoom panel overlay in the bottom-right corner
        if hasattr(self, "zoom_panel") and self.zoom_panel:
            panel_w = 36
            panel_h = 180
            margin_right = 20
            margin_bottom = 40
            self.zoom_panel.setGeometry(
                self.width() - panel_w - margin_right,
                self.height() - panel_h - margin_bottom,
                panel_w,
                panel_h
            )
        super().resizeEvent(event)

    def paintEvent(self, event):
        """
        Main drawing method executed on every repaint request.
        Translates and scales the pre-rendered map image during active panning/zooming.
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        if not self.data_loaded:
            painter.setFont(QFont("Segoe UI", 14))
            painter.setPen(QColor("#5D6D7E"))
            if hasattr(self, "db_name") and self.db_name:
                painter.drawText(self.rect(), Qt.AlignCenter, constants.STR_LOADING_DATA)
            else:
                painter.drawText(self.rect(), Qt.AlignCenter, constants.STR_NO_MAP_PROMPT)
            return
            
        # Draw pre-rendered map image
        if self.map_image:
            painter.save()
            # Center of the screen coordinate system
            painter.translate(self.width() / 2.0, self.height() / 2.0)
            
            # Compute zoom scale ratio relative to the pre-rendered image
            s_ratio = self.scale / self.image_scale
            painter.scale(s_ratio, s_ratio)
            
            # Calculate pixel translation vector based on camera center delta
            dx = (self.image_center_x - self.center_x) * self.image_scale
            dy = -(self.image_center_y - self.center_y) * self.image_scale
            painter.translate(dx, dy)
            
            # Align drawing starting offset to draw image centered
            painter.translate(-self.image_w / 2.0, -self.image_h / 2.0)
            painter.drawImage(0, 0, self.map_image)
            painter.restore()
            
        # Draw Route Overlay
        if self.current_route:
            painter.save()
            painter.translate(self.width() / 2.0, self.height() / 2.0)
            painter.scale(self.scale, -self.scale)
            painter.translate(-self.center_x, -self.center_y)
            
            poly = QPolygonF(self.current_route)
            pen = QPen(QColor("#3B82F6"), 6.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPolyline(poly)
            painter.restore()
            
        # Draw start/end markers
        if self.route_start:
            px, py = self.to_screen(self.route_start.x(), self.route_start.y())
            painter.save()
            painter.setPen(QPen(QColor("#FFFFFF"), 2.0))
            painter.setBrush(QColor("#10B981"))
            painter.drawEllipse(QPointF(px, py), 8.0, 8.0)
            painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(QRectF(px - 10, py - 10, 20, 20), Qt.AlignCenter, "A")
            painter.restore()
            
        if self.route_end:
            px, py = self.to_screen(self.route_end.x(), self.route_end.y())
            painter.save()
            painter.setPen(QPen(QColor("#FFFFFF"), 2.0))
            painter.setBrush(QColor("#EF4444"))
            painter.drawEllipse(QPointF(px, py), 8.0, 8.0)
            painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(QRectF(px - 10, py - 10, 20, 20), Qt.AlignCenter, "B")
            painter.restore()
            
        # Draw "Rendering..." overlay if background thread is active
        if hasattr(self, "render_worker") and self.render_worker and self.render_worker.isRunning():
            painter.save()
            margin_right = 20
            margin_top = 20
            indicator_w = 120
            indicator_h = 30
            rect = QRectF(self.width() - indicator_w - margin_right, margin_top, indicator_w, indicator_h)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(44, 62, 80, 200))
            painter.drawRoundedRect(rect, 15, 15)
            painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(rect, Qt.AlignCenter, "⏳ RENDERING...")
            painter.restore()
import preprocess

class PreprocessorWorker(QThread):
    progress_update = Signal(int, str)  # (percentage, message)
    finished = Signal()
    error = Signal(str)
    
    def __init__(self, pbf_path, db_path):
        super().__init__()
        self.pbf_path = pbf_path
        self.db_path = db_path
        
    def run(self):
        try:
            def cb(pct, msg):
                self.progress_update.emit(pct, msg)
                
            preprocess.run_preprocess(self.pbf_path, self.db_path, progress_callback=cb)
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))


class NonFilteringCompleter(QCompleter):
    def splitPath(self, path):
        return []


class RouteDirectionsDialog(QDialog):
    def __init__(self, directions, driving_side="left", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Route Directions")
        self.resize(600, 450)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        
        self.label = QLabel(f"Turn-by-Turn Directions (Driving Side: {driving_side.upper()}):")
        self.label.setFont(QFont("Segoe UI", 10, QFont.Bold))
        layout.addWidget(self.label)
        
        self.text_edit = QPlainTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setFont(QFont("Consolas" if sys.platform == "win32" else "Monospace", 10))
        self.text_edit.setStyleSheet("background-color: #FAFAFA; border: 1px solid #DDD; padding: 5px;")
        
        self.text_edit.setPlainText("\n\n".join(directions))
        layout.addWidget(self.text_edit)
        
        btn_layout = QHBoxLayout()
        self.btn_copy = QPushButton("Copy to Clipboard")
        self.btn_copy.setFont(QFont("Segoe UI", 9))
        self.btn_copy.setStyleSheet("padding: 6px 12px;")
        self.btn_copy.clicked.connect(self.copy_to_clipboard)
        
        self.btn_close = QPushButton("Close")
        self.btn_close.setFont(QFont("Segoe UI", 9))
        self.btn_close.setStyleSheet("padding: 6px 12px;")
        self.btn_close.clicked.connect(self.accept)
        
        btn_layout.addWidget(self.btn_copy)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_close)
        layout.addLayout(btn_layout)
        
    def copy_to_clipboard(self):
        clipboard = QApplication.clipboard()
        clipboard.setText(self.text_edit.toPlainText())
        QMessageBox.information(self, "Copied", "Directions copied to clipboard!")


class AllMatchesDialog(QDialog):
    def __init__(self, query, matches, parent=None):
        super().__init__(parent)
        self.setWindowTitle("All Matching Results")
        self.resize(550, 450)
        self.selected_match = None
        self.matches = matches
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        
        self.label = QLabel(f"Matching results for '{query}' ({len(matches)} found):")
        self.label.setFont(QFont("Segoe UI", 10, QFont.Bold))
        layout.addWidget(self.label)
        
        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("Type to filter results dynamically...")
        self.filter_box.setFont(QFont("Segoe UI", 10))
        self.filter_box.textChanged.connect(self.filter_items)
        layout.addWidget(self.filter_box)
        
        self.list_widget = QListWidget()
        self.list_widget.setFont(QFont("Segoe UI", 10))
        self.list_widget.itemDoubleClicked.connect(self.accept)
        layout.addWidget(self.list_widget)
        
        self.populate_list(matches)
        
        btn_layout = QHBoxLayout()
        self.btn_select = QPushButton("Select")
        self.btn_select.clicked.connect(self.accept)
        self.btn_select.setFont(QFont("Segoe UI", 10))
        
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_cancel.setFont(QFont("Segoe UI", 10))
        
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_select)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)
        
    def populate_list(self, items):
        self.list_widget.clear()
        self.list_widget.addItems(items)
        if items:
            self.list_widget.setCurrentRow(0)
        
    def filter_items(self, text):
        q = text.lower().strip()
        filtered = [m for m in self.matches if q in m.lower()]
        self.populate_list(filtered)
        
    def get_selected(self):
        item = self.list_widget.currentItem()
        return item.text() if item else None


class SearchDialog(QDialog):
    def __init__(self, parent=None, search_names=None):
        super().__init__(parent)
        self.setWindowTitle("Search Place...")
        self.resize(500, 120)
        self.all_search_names = search_names if search_names else []
        self.selected_match = None
        
        layout = QVBoxLayout(self)
        self.label = QLabel("Enter Eircode, town, or road name:")
        self.label.setFont(QFont("Segoe UI", 10))
        layout.addWidget(self.label)
        
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("e.g. Dublin, R32CH9D, O'Connell Street...")
        layout.addWidget(self.search_box)
        
        self.completer_model = QStringListModel(self)
        self.search_completer = NonFilteringCompleter(self.completer_model, self)
        self.search_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.search_completer.setCompletionMode(QCompleter.PopupCompletion)
        self.search_completer.activated.connect(self.on_completer_activated)
        self.search_box.setCompleter(self.search_completer)
        self.search_box.textEdited.connect(self.on_search_text_edited)
        self.search_box.returnPressed.connect(self.accept)
        
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
        
    def on_search_text_edited(self, text):
        if text.startswith("Show all matches..."):
            return
            
        q = text.strip().lower()
        if not q or len(q) < 2:
            self.completer_model.setStringList([])
            return
            
        prefix_matches = []
        seen = set()
        for search_key, display_name in self.all_search_names:
            if search_key.lower().startswith(q):
                if display_name not in seen:
                    seen.add(display_name)
                    prefix_matches.append(display_name)
                    if len(prefix_matches) >= 30:
                        break
                        
        self.completer_model.setStringList(prefix_matches)
        self.search_completer.setCompletionPrefix("")
        self.search_completer.complete()
        
    def on_completer_activated(self, text):
        self.search_box.setText(text)



class MainWindow(QMainWindow):
    """
    Main Application Window container.
    
    Manages the toolbar, status bar, search query completions, map loading timers,
    and the collapsible developer settings sidebar.
    """
    def __init__(self, db_path):
        super().__init__()
        self.setWindowTitle(constants.STR_APP_TITLE_BASE)
        self.resize(1200, 850)
        self.db_path = db_path
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # Main layout splitting map canvas on the left, developer panel on the right
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # Left frame containing toolbars and the map widget canvas
        map_frame = QWidget()
        map_layout = QVBoxLayout(map_frame)
        map_layout.setContentsMargins(0, 0, 0, 0)
        map_layout.setSpacing(0)
        
        self.map_widget = MapWidget(self)
        map_layout.addWidget(self.map_widget)
        main_layout.addWidget(map_frame)
        
        # (Developer Config Panel sidebar removed)
        
        # Status Bar setup
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        
        self.coord_label = QLabel("Lat: -, Lon: -")
        self.status_bar.addPermanentWidget(self.coord_label)
        self.status_bar.showMessage(constants.STR_READY)
        
        # Connect signals for status updates
        self.map_widget.coordinate_hover.connect(self.update_coordinates)
        self.map_widget.status_message.connect(self.status_bar.showMessage)
        self.map_widget.route_start_changed.connect(self.on_route_start_changed)
        self.map_widget.route_end_changed.connect(self.on_route_end_changed)
        self.map_widget.search_requested.connect(self.on_search_requested)
        
        self.desc_a = ""
        self.desc_b = ""
        self.all_search_names = []
        
        # Toolbar controls setup
        self.toolbar = QToolBar("Controls")
        self.toolbar.setIconSize(self.toolbar.iconSize() * 0.8)
        self.addToolBar(self.toolbar)
        
        self.btn_open_file = QPushButton("📂 Open Map File")
        self.btn_open_file.clicked.connect(self.open_map_file)
        self.toolbar.addWidget(self.btn_open_file)
        self.toolbar.addSeparator()
        
        self.lbl_start = QLabel(" Start (A):")
        self.lbl_start.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.toolbar.addWidget(self.lbl_start)
        
        self.search_box_a = QLineEdit()
        self.search_box_a.setPlaceholderText("Start location...")
        self.search_box_a.setMinimumWidth(180)
        self.search_box_a.setEnabled(False)
        self.toolbar.addWidget(self.search_box_a)
        
        self.lbl_dest = QLabel(" Dest (B):")
        self.lbl_dest.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.toolbar.addWidget(self.lbl_dest)
        
        self.search_box_b = QLineEdit()
        self.search_box_b.setPlaceholderText("Destination...")
        self.search_box_b.setMinimumWidth(180)
        self.search_box_b.setEnabled(False)
        self.toolbar.addWidget(self.search_box_b)
        
        self.toolbar.addSeparator()
        
        self.lbl_profile = QLabel(" Profile:")
        self.lbl_profile.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.toolbar.addWidget(self.lbl_profile)
        
        self.combo_profile = QComboBox()
        self.combo_profile.setEnabled(False)
        self.combo_profile.currentIndexChanged.connect(self.on_profile_index_changed)
        self.toolbar.addWidget(self.combo_profile)
        
        # Populate routing profiles
        profiles = list(self.map_widget.routing_profiles.keys())
        if not profiles:
            profiles = ["Car", "Bicycle", "Walk"]
        self.combo_profile.addItems(profiles)
        self.on_profile_index_changed(self.combo_profile.currentIndex())
        
        self.btn_clear_route = QPushButton("🧹 Clear Route")
        self.btn_clear_route.clicked.connect(self.clear_route)
        self.btn_clear_route.setEnabled(False)
        self.toolbar.addWidget(self.btn_clear_route)
        
        self.toolbar.addSeparator()
        
        # (Toolbar Dev config button removed)
        
        # Startup loading bar overlay
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumHeight(15)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_bar.addWidget(self.progress_bar)
        
        # Map loading worker thread initialization
        if self.db_path:
            if self.db_path.endswith(".osm.pbf"):
                self.db_path_pbf = self.db_path
                self.db_path = None
                self.set_controls_enabled(False)
                QTimer.singleShot(100, self.convert_pbf_on_startup)
            else:
                self.loader = MapDataLoader(self.db_path, self.map_widget.zoom_details)
                self.loader.progress_message.connect(self.show_loading_status)
                self.loader.progress_pct.connect(self.update_load_progress)
                self.loader.data_loaded.connect(self.on_data_loaded)
                self.loader.error_occurred.connect(self.on_data_load_error)
                
                # Check database immediately. If valid, start loading and lock controls.
                db_valid = False
                if os.path.exists(self.db_path):
                    try:
                        conn = sqlite3.connect(self.db_path)
                        cursor = conn.cursor()
                        cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='config'")
                        if cursor.fetchone()[0] > 0:
                            cursor.execute("SELECT count(*) FROM ways")
                            if cursor.fetchone()[0] > 0:
                                db_valid = True
                        conn.close()
                    except:
                        pass
                
                if db_valid:
                    self.set_controls_enabled(False)
                    self.loader.start()
                else:
                    # Wait timer checking for db preprocessing completion
                    self.db_check_timer = QTimer(self)
                    self.db_check_timer.timeout.connect(self.check_database_and_load)
                    self.db_check_timer.start(1000)
        else:
            self.progress_bar.hide()
            self.show_loading_status(constants.STR_NO_MAP_LOADED)
        
    def check_database_and_load(self):
        """
        Polls for SQLite database file availability and starts loader thread once validated.
        """
        if os.path.exists(self.db_path):
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='config'")
                if cursor.fetchone()[0] > 0:
                    cursor.execute("SELECT count(*) FROM ways")
                    if cursor.fetchone()[0] > 0:
                        conn.close()
                        self.db_check_timer.stop()
                        self.set_controls_enabled(False)
                        self.loader.start()
                        return
                conn.close()
            except sqlite3.OperationalError:
                pass
                
        self.show_loading_status(constants.STR_DB_READY_LOADING)
 
    def show_loading_status(self, msg):
        self.status_bar.showMessage(msg)
        
    def update_load_progress(self, pct):
        self.progress_bar.setValue(pct)
  
    def set_controls_enabled(self, enabled):
        self.btn_open_file.setEnabled(enabled)
        self.search_box_a.setEnabled(enabled)
        self.search_box_b.setEnabled(enabled)
        self.map_widget.setEnabled(enabled)
        
        if enabled:
            has_routing = getattr(self.map_widget, "has_routing", False)
            self.combo_profile.setEnabled(has_routing)
            self.btn_clear_route.setEnabled(has_routing)
        else:
            self.combo_profile.setEnabled(False)
            self.btn_clear_route.setEnabled(False)

    def on_data_load_error(self, err_msg):
        self.set_controls_enabled(True)
        self.progress_bar.hide()
        QMessageBox.critical(self, "Error", f"Failed to load map data: {err_msg}")

    def on_data_loaded(self, data):
        """
        Executes on data loaded signal. Setup auto-completer and updates map.
        
        Passes the active database filename to the MapWidget to enable per-map viewport saving.
        """
        self.set_controls_enabled(True)
        self.progress_bar.hide()
        self.status_bar.showMessage(constants.STR_MAP_LOADED_SUCCESS, 5000)
        self.map_widget.set_map_data(data, os.path.basename(self.db_path))
        self.map_widget.db_path = self.db_path
        
        # Enable route controls if graph is loaded
        has_routing = data.get("has_routing", False)
        self.combo_profile.setEnabled(has_routing)
        self.btn_clear_route.setEnabled(has_routing)
        if not has_routing:
            self.combo_profile.setToolTip("Routing tables not found. Run preprocess.py --routing-only first.")
            self.btn_clear_route.setEnabled(False)
        else:
            self.combo_profile.setToolTip("Select routing profile")
        
        # Update window title dynamically based on the database name
        map_name = os.path.splitext(os.path.basename(self.db_path))[0]
        map_name = map_name.replace('_', ' ').replace('-', ' ').title()
        self.setWindowTitle(f"{constants.STR_APP_TITLE_BASE} - {map_name}")
        
        # Setup dynamic search completer using prefix + Levenshtein suggestions
        self.all_search_names = data.get("search_names", [])
        
        # Setup autocomplete on A/B search fields
        self.setup_autocomplete(self.search_box_a, is_start=True)
        self.setup_autocomplete(self.search_box_b, is_start=False)
        
    def setup_autocomplete(self, line_edit, is_start=True):
        completer_model = QStringListModel(self)
        completer = NonFilteringCompleter(completer_model, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        
        if is_start:
            completer.activated.connect(self.on_completer_a_activated)
            line_edit.textEdited.connect(lambda text: self.on_search_text_edited_generic(text, completer_model, completer))
            line_edit.returnPressed.connect(lambda: self.on_search_a_activated(line_edit.text()))
        else:
            completer.activated.connect(self.on_completer_b_activated)
            line_edit.textEdited.connect(lambda text: self.on_search_text_edited_generic(text, completer_model, completer))
            line_edit.returnPressed.connect(lambda: self.on_search_b_activated(line_edit.text()))
            
        line_edit.setCompleter(completer)

    def on_completer_a_activated(self, text):
        if text.startswith("Show all matches..."):
            QTimer.singleShot(0, lambda: self.open_all_matches_dialog_generic(self.search_box_a, True))
        else:
            self.on_search_a_activated(text)

    def on_completer_b_activated(self, text):
        if text.startswith("Show all matches..."):
            QTimer.singleShot(0, lambda: self.open_all_matches_dialog_generic(self.search_box_b, False))
        else:
            self.on_search_b_activated(text)

    def on_search_text_edited_generic(self, text, completer_model, completer):
        if text.startswith("Show all matches..."):
            return
            
        self.last_typed_search = text
        q = text.strip().lower()
        if not q or len(q) < 2:
            completer_model.setStringList([])
            return
            
        prefix_matches = []
        seen = set()
        for search_key, display_name in self.all_search_names:
            if search_key.lower().startswith(q):
                if display_name not in seen:
                    seen.add(display_name)
                    prefix_matches.append(display_name)
                    if len(prefix_matches) >= 1000:
                        break
                        
        suggestions = list(prefix_matches)
        
        def levenshtein_distance(s1, s2):
            if len(s1) < len(s2):
                return levenshtein_distance(s2, s1)
            if len(s2) == 0:
                return len(s1)
            previous_row = range(len(s2) + 1)
            for i, c1 in enumerate(s1):
                current_row = [i + 1]
                for j, c2 in enumerate(s2):
                    insertions = previous_row[j + 1] + 1
                    deletions = current_row[j] + 1
                    substitutions = previous_row[j] + (c1 != c2)
                    current_row.append(min(insertions, deletions, substitutions))
                previous_row = current_row
            return previous_row[-1]
            
        if len(q) >= 3 and len(suggestions) < 15:
            first_char = q[0]
            candidates = [
                (sk, dn) for sk, dn in self.all_search_names
                if len(sk) > 0 and sk[0].lower() == first_char
                and abs(len(sk) - len(q)) <= 2
                and not sk.lower().startswith(q)
            ]
            
            lev_results = []
            for sk, dn in candidates:
                dist = levenshtein_distance(q, sk.lower())
                if dist <= 2:
                    lev_results.append((dist, dn))
                    
            lev_results.sort(key=lambda x: (x[0], x[1].lower()))
            for r in lev_results:
                if r[1] not in seen:
                    seen.add(r[1])
                    suggestions.append(r[1])
                    if len(suggestions) >= 1000:
                        break
                        
        self.current_unfiltered_suggestions = suggestions
        
        if len(suggestions) > 30:
            display_list = suggestions[:30] + [f"Show all matches... ({len(suggestions)} results)"]
        else:
            display_list = suggestions
            
        completer_model.setStringList(display_list)
        completer.setCompletionPrefix("")
        completer.complete()

    def open_all_matches_dialog_generic(self, search_box, is_start):
        query = getattr(self, "last_typed_search", "")
        search_box.setText(query)
        all_matches = getattr(self, "current_unfiltered_suggestions", [])
        if not all_matches:
            return
            
        dialog = AllMatchesDialog(query, all_matches, self)
        if dialog.exec() == QDialog.Accepted:
            selected = dialog.get_selected()
            if selected:
                search_box.setText(selected)
                if is_start:
                    self.on_search_a_activated(selected)
                else:
                    self.on_search_b_activated(selected)
  
    def update_coordinates(self, lat, lon):
        self.coord_label.setText(f"Lat: {lat:.5f}° N, Lon: {lon:.5f}° W")
  
    def zoom_in(self):
        self.map_widget.zoom_in()
   
    def zoom_out(self):
        self.map_widget.zoom_out()
  

    def convert_pbf_on_startup(self):
        pbf_path = self.db_path_pbf
        self.convert_pbf_to_db(pbf_path)
        
    def open_map_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Map File",
            ".",
            "Map Files (*.db *.osm.pbf);;SQLite Database (*.db);;OSM PBF File (*.osm.pbf)"
        )
        if not file_path:
            return
            
        if file_path.endswith(".db"):
            self.load_new_database(file_path)
        elif file_path.endswith(".osm.pbf"):
            self.convert_pbf_to_db(file_path)
            
    def convert_pbf_to_db(self, pbf_path):
        base, ext = os.path.splitext(pbf_path)
        if base.endswith(".osm"):
            base = base[:-4]
        suggested_db = base + ".db"
        
        db_path, _ = QFileDialog.getSaveFileName(
            self,
            "Select Output Database File",
            suggested_db,
            "SQLite Database (*.db)"
        )
        if not db_path:
            return
            
        if db_path == pbf_path:
            QMessageBox.warning(self, "Warning", constants.STR_WARNING_SAME_FILE)
            return
            
        self.btn_open_file.setEnabled(False)
        self.search_box_a.setEnabled(False)
        self.search_box_b.setEnabled(False)
        
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self.show_loading_status(constants.STR_CONVERT_STARTING)
        
        self.prep_worker = PreprocessorWorker(pbf_path, db_path)
        self.prep_worker.progress_update.connect(self.on_prep_progress)
        self.prep_worker.finished.connect(lambda: self.on_prep_finished(db_path))
        self.prep_worker.error.connect(self.on_prep_error)
        self.prep_worker.start()
        
    def on_prep_progress(self, pct, message):
        self.progress_bar.setValue(pct)
        self.show_loading_status(message)
        
    def on_prep_finished(self, db_path):
        self.btn_open_file.setEnabled(True)
        self.search_box_a.setEnabled(True)
        self.search_box_b.setEnabled(True)
        
        QMessageBox.information(self, "Success", constants.STR_CONVERT_SUCCESS)
        self.load_new_database(db_path)
        
    def on_prep_error(self, err_msg):
        self.btn_open_file.setEnabled(True)
        self.search_box_a.setEnabled(True)
        self.search_box_b.setEnabled(True)
        
        self.progress_bar.hide()
        self.show_loading_status(constants.STR_CONVERT_FAILED)
        QMessageBox.critical(self, "Error", constants.STR_CONVERT_ERROR.format(err_msg))
        
    def load_new_database(self, db_path):
        if hasattr(self, "db_check_timer") and self.db_check_timer.isActive():
            self.db_check_timer.stop()
            
        if hasattr(self, "loader") and self.loader and self.loader.isRunning():
            self.loader.terminate()
            self.loader.wait()
            
        self.map_widget.abort_rendering()
        
        self.map_widget.data_loaded = False
        self.map_widget.update()
        
        self.db_path = db_path
        
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        
        self.set_controls_enabled(False)
        self.loader = MapDataLoader(self.db_path, self.map_widget.zoom_details)
        self.loader.progress_message.connect(self.show_loading_status)
        self.loader.progress_pct.connect(self.update_load_progress)
        self.loader.data_loaded.connect(self.on_data_loaded)
        self.loader.error_occurred.connect(self.on_data_load_error)
        self.loader.start()
  
    def on_search_a_activated(self, text):
        res = self.find_coordinates_for_search(text)
        if res:
            pt, desc, scale = res
            self.map_widget.route_start = pt
            self.map_widget.current_route = None
            self.map_widget.route_distance = 0.0
            self.map_widget.route_duration = 0.0
            
            self.desc_a = desc
            self.update_search_inputs()
            
            self.map_widget.center_x = pt.x()
            self.map_widget.center_y = pt.y()
            self.map_widget.scale = scale
            self.map_widget.start_interaction()
            self.map_widget.constrain_view()
            self.map_widget.update()
            
            self.status_bar.showMessage(f"Start point snapped to: {self.search_box_a.text()}", 4000)
            
            if self.map_widget.route_end:
                self.status_bar.showMessage("Calculating route...", 4000)
                self.map_widget.calculate_route()
        else:
            QMessageBox.information(self, "Search", f"No matches found for '{text}'.")

    def on_search_b_activated(self, text):
        res = self.find_coordinates_for_search(text)
        if res:
            pt, desc, scale = res
            self.map_widget.route_end = pt
            self.map_widget.current_route = None
            self.map_widget.route_distance = 0.0
            self.map_widget.route_duration = 0.0
            
            self.desc_b = desc
            self.update_search_inputs()
            
            self.map_widget.center_x = pt.x()
            self.map_widget.center_y = pt.y()
            self.map_widget.scale = scale
            self.map_widget.start_interaction()
            self.map_widget.constrain_view()
            self.map_widget.update()
            
            self.status_bar.showMessage(f"Destination snapped to: {self.search_box_b.text()}", 4000)
            
            if self.map_widget.route_start:
                self.status_bar.showMessage("Calculating route...", 4000)
                self.map_widget.calculate_route()
        else:
            QMessageBox.information(self, "Search", f"No matches found for '{text}'.")

    def on_route_start_changed(self, pt, desc):
        self.desc_a = desc
        self.update_search_inputs()
        
    def on_route_end_changed(self, pt, desc):
        self.desc_b = desc
        self.update_search_inputs()

    def update_search_inputs(self):
        desc_a = getattr(self, "desc_a", "")
        desc_b = getattr(self, "desc_b", "")
        pt_a = self.map_widget.route_start
        pt_b = self.map_widget.route_end
        
        # Clean any existing coordinate brackets from description text
        import re
        desc_a = re.sub(r"\s*\(\d+\.\d+,\s*-\d+\.\d+\)$", "", desc_a)
        desc_b = re.sub(r"\s*\(\d+\.\d+,\s*-\d+\.\d+\)$", "", desc_b)
        
        if pt_a and pt_b and desc_a and desc_b and desc_a.strip().lower() == desc_b.strip().lower():
            lat_a, lon_a = inverse_mercator(pt_a.x(), pt_a.y())
            lat_b, lon_b = inverse_mercator(pt_b.x(), pt_b.y())
            display_a = f"{desc_a} ({lat_a:.5f}, {lon_a:.5f})"
            display_b = f"{desc_b} ({lat_b:.5f}, {lon_b:.5f})"
        else:
            display_a = desc_a
            display_b = desc_b
            
        self.search_box_a.setText(display_a)
        self.search_box_b.setText(display_b)

    def on_search_requested(self, click_pt):
        dialog = SearchDialog(self, self.all_search_names)
        if dialog.exec() == QDialog.Accepted:
            text = dialog.search_box.text().strip()
            if text:
                res = self.find_coordinates_for_search(text)
                if res:
                    pt, desc, scale = res
                    self.map_widget.center_x = pt.x()
                    self.map_widget.center_y = pt.y()
                    self.map_widget.scale = scale
                    self.map_widget.start_interaction()
                    self.map_widget.constrain_view()
                    self.map_widget.update()
                    self.status_bar.showMessage(f"Centered on: {desc}", 4000)
                else:
                    QMessageBox.information(self, "Search", f"No matches found for '{text}'.")

    def on_profile_index_changed(self, index):
        profile_name = self.combo_profile.currentText()
        profile_dict = self.map_widget.routing_profiles.get(profile_name, {})
        self.map_widget.active_profile_name = profile_name
        self.map_widget.active_profile = profile_dict
        if self.map_widget.route_start and self.map_widget.route_end:
            self.status_bar.showMessage("Recalculating route...", 4000)
            self.map_widget.calculate_route()

    def clear_route(self):
        """
        Clears all selected route markers and paths.
        """
        self.map_widget.route_start = None
        self.map_widget.route_end = None
        self.map_widget.current_route = None
        self.map_widget.route_distance = 0.0
        self.map_widget.route_duration = 0.0
        self.map_widget.current_route_directions = []
        self.desc_a = ""
        self.desc_b = ""
        self.update_search_inputs()
        self.status_bar.showMessage("Route cleared.")
        self.map_widget.update()

    def find_coordinates_for_search(self, search_text):
        """
        Resolves a search query (postcode, town, road) to Web Mercator coordinates.
        Returns (QPointF(x, y), display_name, zoom_scale) or None.
        """
        if not self.map_widget.data_loaded:
            return None
            
        search_text = search_text.strip()
        if not search_text:
            return None
            
        clean_q = search_text
        if clean_q.endswith(" (Postcode)"):
            clean_q = clean_q[:-11].strip()
            
        parts = [p.strip() for p in clean_q.split(",")]
        base_name = parts[0]
        
        # Check if query is targeting a station generically
        is_station_query = False
        base_name_lower = base_name.lower()
        if base_name_lower.endswith(" train station"):
            base_name = base_name[:-14].strip()
            is_station_query = True
        elif base_name_lower.endswith(" station"):
            base_name = base_name[:-8].strip()
            is_station_query = True
            
        norm_q = clean_q.replace(" ", "").upper()
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='postcodes'")
            if cursor.fetchone():
                cursor.execute("SELECT postcode, x, y FROM postcodes WHERE normalized_postcode = ?", (norm_q,))
                row = cursor.fetchone()
                if row:
                    pc, x, y = row
                    conn.close()
                    return QPointF(x, y), f"Eircode: {pc}", 0.1
                    
                fallback_q = norm_q
                is_full_eircode = len(norm_q) == 7 and norm_q[0].isalpha()
                if is_full_eircode:
                    fallback_q = norm_q[:3]
                    
                cursor.execute("SELECT postcode, x, y FROM postcodes WHERE normalized_postcode LIKE ?", (fallback_q + '%',))
                rows = cursor.fetchall()
                if rows:
                    avg_x = sum(r[1] for r in rows) / len(rows)
                    avg_y = sum(r[2] for r in rows) / len(rows)
                    conn.close()
                    desc = f"Area {fallback_q}" if is_full_eircode else f"Postcode Area {search_text}"
                    scale = 0.015 if len(rows) > 1 else 0.1
                    return QPointF(avg_x, avg_y), desc, scale
            conn.close()
        except Exception as e:
            print("Postcode search error:", e)
            
        target_place = None
        if len(parts) == 2 and parts[1].startswith("Co. "):
            county_name = parts[1][4:].strip().lower()
            matches = [
                p for p in self.map_widget.places 
                if p["name"].lower() == base_name.lower() 
                and (p["county"] or "").lower() == county_name
                and (not is_station_query or p["place_type"] == "station")
            ]
            if matches:
                target_place = matches[0]
        else:
            matches = [
                p for p in self.map_widget.places 
                if p["name"].lower() == base_name.lower()
                and (not is_station_query or p["place_type"] == "station")
            ]
            if not matches:
                matches = [
                    p for p in self.map_widget.places 
                    if base_name.lower() in p["name"].lower()
                    and (not is_station_query or p["place_type"] == "station")
                ]
            if matches:
                target_place = matches[0]
                
        if target_place:
            zoom_map = {'city': 0.05, 'town': 0.1, 'village': 0.25, 'station': 0.15}
            scale = zoom_map.get(target_place["place_type"], 0.1)
            county_str = f", Co. {target_place['county']}" if target_place.get('county') else ""
            
            # Format station name display dynamically
            display_name = target_place["name"]
            if target_place["place_type"] == "station":
                if not display_name.lower().endswith("station"):
                    display_name = f"{display_name} Train Station"
                    
            return QPointF(target_place["x"], target_place["y"]), f"{display_name}{county_str}", scale
            
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT min_x, min_y, max_x, max_y, name, sub_type 
                FROM ways 
                WHERE name LIKE ? 
                LIMIT 200
            """, (f"%{base_name}%",))
            ways_matches = cursor.fetchall()
            conn.close()
            
            if ways_matches:
                exact_matches = [w for w in ways_matches if w[4].lower() == base_name.lower()]
                candidates = exact_matches if exact_matches else ways_matches
                
                ref_x, ref_y = None, None
                if len(parts) >= 2:
                    town_part = parts[1]
                    town_matches = []
                    if len(parts) == 3 and parts[2].startswith("Co. "):
                        county_name = parts[2][4:].strip().lower()
                        town_matches = [
                            p for p in self.map_widget.places 
                            if p["name"].lower() == town_part.lower()
                            and (p["county"] or "").lower() == county_name
                        ]
                    if not town_matches:
                        town_matches = [p for p in self.map_widget.places if p["name"].lower() == town_part.lower()]
                    
                    if town_matches:
                        ref_x = town_matches[0]["x"]
                        ref_y = town_matches[0]["y"]
                
                if ref_x is None or ref_y is None:
                    ref_x = self.map_widget.center_x
                    ref_y = self.map_widget.center_y
                    
                def get_dist(w):
                    cx = (w[0] + w[2]) / 2.0
                    cy = (w[1] + w[3]) / 2.0
                    return (cx - ref_x)**2 + (cy - ref_y)**2
                    
                candidates.sort(key=get_dist)
                target_way = candidates[0]
                
                min_x, min_y, max_x, max_y, name, sub_type = target_way
                cx = (min_x + max_x) / 2.0
                cy = (min_y + max_y) / 2.0
                
                w_w = max_x - min_x
                w_h = max_y - min_y
                max_dim = max(w_w, w_h, 1.0)
                fit_scale = min(self.map_widget.width(), self.map_widget.height()) / max_dim
                scale = max(self.map_widget.min_scale, min(self.map_widget.max_scale, fit_scale))
                return QPointF(cx, cy), f"{name} ({sub_type})", scale
        except Exception as e:
            print("Feature search error:", e)
            
        return None
 
 
def main():
    """
    Main application entry point. Enables High DPI scaling, configures styles,
    and runs the event execution loop.
    """
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"
    app = QApplication(sys.argv)
    
    # Custom Modern CSS Style overrides for PyQt components
    app.setStyleSheet(constants.APP_STYLESHEET)
    
    db_path = None
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
        
    if not db_path or not os.path.exists(db_path):
        # Scan current directory for any database files ending in .db
        db_files = [f for f in os.listdir(".") if f.endswith(".db") and f != "config.json"]
        
        if len(db_files) == 1:
            db_path = os.path.abspath(db_files[0])
            print(f"Auto-detected database file: {db_path}")
        else:
            # Show file selection dialog supporting both DB and PBF files
            db_path, _ = QFileDialog.getOpenFileName(
                None, 
                "Select Map File (DB or OSM PBF)", 
                ".", 
                "Map Files (*.db *.osm.pbf);;SQLite Database (*.db);;OSM PBF File (*.osm.pbf)"
            )
            
    # If the user selects a file, we proceed, otherwise start in empty state (db_path=None)
    if db_path and not os.path.exists(db_path):
        db_path = None
        
    window = MainWindow(db_path)
    window.show()
    sys.exit(app.exec())
 
if __name__ == "__main__":
    main()
