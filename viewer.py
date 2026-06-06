import os
import sys
import math
import sqlite3
import json
import array
import struct
from PySide6.QtCore import Qt, QPointF, QRectF, QThread, Signal, QPoint, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QFontMetrics, QPolygonF
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QLineEdit, QPushButton, QLabel, QStatusBar, QToolBar, QProgressBar, 
                             QMessageBox, QCompleter, QSizePolicy, QFrame, QComboBox, QColorDialog,
                             QFileDialog, QSlider)
import constants
import renderer
import rules



# Settings file path
SETTINGS_PATH = r"./dev_settings.json"

from utils import inverse_mercator, simplify_path
from render_worker import MapRenderWorker


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
            
            # Initialize separate spatial grid indexes for different layer features.
            # Cell dimensions are tuned based on density (e.g. roads grid has more subdivisions).
            self.progress_pct.emit(10)
            self.progress_message.emit("Initializing spatial indexes...")
            coastlines_index = {sk: SpatialGridIndex(global_bbox, 16, 16) for sk in scale_keys}
            wetlands_index = {sk: SpatialGridIndex(global_bbox, 24, 24) for sk in scale_keys}
            forests_index = {sk: SpatialGridIndex(global_bbox, 24, 24) for sk in scale_keys}
            waterbodies_index = {sk: SpatialGridIndex(global_bbox, 24, 24) for sk in scale_keys}
            rivers_index = {sk: SpatialGridIndex(global_bbox, 24, 24) for sk in scale_keys}
            
            # Roads index mapping by highway type
            road_types = rules.ROAD_CATEGORIES
            roads_index = {sk: {rtype: SpatialGridIndex(global_bbox, 32, 32) for rtype in road_types} for sk in scale_keys}
            
            # 1. Load places (cities, towns, villages) for name search/labeling
            self.progress_pct.emit(15)
            self.progress_message.emit("Loading places...")
            cursor.execute("SELECT id, name, place_type, x, y, population FROM places ORDER BY population DESC, place_type ASC")
            places_rows = cursor.fetchall()
            places = []
            for r in places_rows:
                places.append({
                    "id": r[0],
                    "name": r[1],
                    "place_type": r[2],
                    "x": r[3],
                    "y": r[4],
                    "population": r[5]
                })
                
            # 2. Load coastline ways (closed loops for land mass drawing)
            self.progress_pct.emit(20)
            self.progress_message.emit("Loading coastlines...")
            cursor.execute("SELECT coords, min_x, min_y, max_x, max_y FROM ways WHERE feature_type='coastline'")
            coastline_rows = cursor.fetchall()
            for blob, min_x, min_y, max_x, max_y in coastline_rows:
                # Convert the binary blob back to a flat array of doubles (X, Y, X, Y, ...)
                coords = array.array('d', blob)
                points = [QPointF(coords[i], coords[i+1]) for i in range(0, len(coords), 2)]
                poly = QPolygonF(points)
                simplified_polys = make_simplified_polygons(points)
                item = MapPolygon(poly, simplified_polygons=simplified_polys, min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y)
                for sk in scale_keys:
                    coastlines_index[sk].add(item, min_x, min_y, max_x, max_y)
                
            # 3. Load wetland/peat bog boundaries
            self.progress_pct.emit(35)
            self.progress_message.emit("Loading wetlands (bogs)...")
            cursor.execute("SELECT coords, name, min_x, min_y, max_x, max_y FROM ways WHERE feature_type='wetland'")
            wetland_rows = cursor.fetchall()
            for blob, name, min_x, min_y, max_x, max_y in wetland_rows:
                coords = array.array('d', blob)
                points = [QPointF(coords[i], coords[i+1]) for i in range(0, len(coords), 2)]
                poly = QPolygonF(points)
                simplified_polys = make_simplified_polygons(points)
                item = MapPolygon(poly, simplified_polygons=simplified_polys, min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y, name=name)
                for sk in scale_keys:
                    tol = self.zoom_details.get(sk, {}).get("simplification", 0.0)
                    if item.area >= tol * tol:
                        wetlands_index[sk].add(item, min_x, min_y, max_x, max_y)
                
            # 4. Load forests
            self.progress_pct.emit(45)
            self.progress_message.emit("Loading forests...")
            cursor.execute("SELECT coords, name, min_x, min_y, max_x, max_y FROM ways WHERE feature_type='forest'")
            forest_rows = cursor.fetchall()
            for blob, name, min_x, min_y, max_x, max_y in forest_rows:
                coords = array.array('d', blob)
                points = [QPointF(coords[i], coords[i+1]) for i in range(0, len(coords), 2)]
                poly = QPolygonF(points)
                simplified_polys = make_simplified_polygons(points)
                item = MapPolygon(poly, simplified_polygons=simplified_polys, min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y, name=name)
                for sk in scale_keys:
                    tol = self.zoom_details.get(sk, {}).get("simplification", 0.0)
                    if item.area >= tol * tol:
                        forests_index[sk].add(item, min_x, min_y, max_x, max_y)
                
            # 5. Load water bodies (lakes, reservoirs, basins)
            self.progress_pct.emit(55)
            self.progress_message.emit("Loading water bodies...")
            cursor.execute("SELECT coords, sub_type, name, min_x, min_y, max_x, max_y FROM ways WHERE feature_type='waterbody'")
            water_rows = cursor.fetchall()
            for blob, sub_type, name, min_x, min_y, max_x, max_y in water_rows:
                coords = array.array('d', blob)
                points = [QPointF(coords[i], coords[i+1]) for i in range(0, len(coords), 2)]
                poly = QPolygonF(points)
                simplified_polys = make_simplified_polygons(points)
                item = MapPolygon(poly, sub_type, simplified_polys, min_x, min_y, max_x, max_y, name)
                for sk in scale_keys:
                    tol = self.zoom_details.get(sk, {}).get("simplification", 0.0)
                    if item.area >= tol * tol:
                        waterbodies_index[sk].add(item, min_x, min_y, max_x, max_y)
                
            # 6. Load linear rivers
            self.progress_pct.emit(70)
            self.progress_message.emit("Loading rivers...")
            cursor.execute("SELECT coords, name, min_x, min_y, max_x, max_y FROM ways WHERE feature_type='river'")
            river_rows = cursor.fetchall()
            for blob, name, min_x, min_y, max_x, max_y in river_rows:
                coords = array.array('d', blob)
                points = [QPointF(coords[i], coords[i+1]) for i in range(0, len(coords), 2)]
                poly = QPolygonF(points)
                simplified_polys = make_simplified_polygons(points)
                item = MapPolygon(poly, simplified_polygons=simplified_polys, min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y, name=name)
                for sk in scale_keys:
                    tol = self.zoom_details.get(sk, {}).get("simplification", 0.0)
                    if item.length >= tol:
                        rivers_index[sk].add(item, min_x, min_y, max_x, max_y)
                
            # 7. Load highways and map them into the custom categorized road indices
            self.progress_pct.emit(80)
            self.progress_message.emit("Loading roads...")
            cursor.execute("SELECT coords, sub_type, name, min_x, min_y, max_x, max_y FROM ways WHERE feature_type='highway'")
            road_rows = cursor.fetchall()
            for blob, sub_type, name, min_x, min_y, max_x, max_y in road_rows:
                coords = array.array('d', blob)
                points = [QPointF(coords[i], coords[i+1]) for i in range(0, len(coords), 2)]
                poly = QPolygonF(points)
                simplified_polys = make_simplified_polygons(points)
                item = MapPolygon(poly, sub_type, simplified_polys, min_x, min_y, max_x, max_y, name)
                parent_type = rules.get_parent_road_type(sub_type)
                for sk in scale_keys:
                    if parent_type in roads_index[sk]:
                        tol = self.zoom_details.get(sk, {}).get("simplification", 0.0)
                        if item.length >= tol:
                            roads_index[sk][parent_type].add(item, min_x, min_y, max_x, max_y)
                
            conn.close()
            
            # Send loaded dictionaries back to the GUI main thread
            self.progress_pct.emit(95)
            self.progress_message.emit("Finalizing maps...")
            data = {
                "bbox": global_bbox,
                "places": places,
                "coastlines_index": coastlines_index,
                "wetlands_index": wetlands_index,
                "forests_index": forests_index,
                "waterbodies_index": waterbodies_index,
                "rivers_index": rivers_index,
                "roads_index": roads_index
            }
            self.progress_pct.emit(100)
            self.data_loaded.emit(data)
            
        except Exception as e:
            self.progress_message.emit(f"Error loading map: {str(e)}")



class MapWidget(QWidget):
    """
    Custom QWidget designed to render vector geometries onto the screen.
    
    This component handles pan/zoom events, progressive rendering states, view 
    constraining, place-label collision calculations, and double-click feature detection.
    """
    coordinate_hover = Signal(float, float)  # Emitted on mouse-move with hovered Lat/Lon
    color_updated_signal = Signal(str, str)  # Emitted when a layer gets recolored
    
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
        self.coastlines_index = None
        self.wetlands_index = None
        self.forests_index = None
        self.waterbodies_index = None
        self.rivers_index = None
        self.roads_index = None
        self.places = []
        
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
        
        if os.path.exists(constants.SETTINGS_PATH):
            try:
                with open(constants.SETTINGS_PATH, 'r') as f:
                    data = json.load(f)
                    if "colors" in data:
                        self.colors.update(data["colors"])
                    if "zoom_details" in data:
                        self.zoom_details = data["zoom_details"]
            except Exception as e:
                print("Error loading settings:", e)
                
        self.apply_theme_colors()
 
    def save_settings(self):
        """
        Saves the current developer modifications (custom colors, LOD mappings) and viewport info to JSON.
        """
        try:
            viewports = {}
            # Load existing viewports to prevent overwriting other map states
            if os.path.exists(constants.SETTINGS_PATH):
                try:
                    with open(constants.SETTINGS_PATH, 'r') as f:
                        old_data = json.load(f)
                        viewports = old_data.get("viewports", {})
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
                "viewports": viewports
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
            'residential': QColor(self.colors["road_residential"])
        }
        
        # Pen colors for high-contrast road casing borders
        self.road_casing_colors = {
            'motorway': QColor(self.colors["road_casing_motorway"]),
            'trunk': QColor(self.colors["road_casing_trunk"]),
            'primary': QColor(self.colors["road_casing_primary"]),
            'secondary': QColor(self.colors["road_casing_secondary"]),
            'tertiary': QColor(self.colors["road_casing_tertiary"]),
            'unclassified': QColor(self.colors["road_casing_unclassified"]),
            'residential': QColor(self.colors["road_casing_residential"])
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
        self.coastlines_index = data["coastlines_index"]
        self.wetlands_index = data["wetlands_index"]
        self.forests_index = data["forests_index"]
        self.waterbodies_index = data["waterbodies_index"]
        self.rivers_index = data["rivers_index"]
        self.roads_index = data["roads_index"]
        
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
        if self.is_rendering_detailed:
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
        if self.is_rendering_detailed:
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
            "places": self.places,
            "coastlines_index": self.coastlines_index,
            "wetlands_index": self.wetlands_index,
            "forests_index": self.forests_index,
            "waterbodies_index": self.waterbodies_index,
            "rivers_index": self.rivers_index,
            "roads_index": self.roads_index
        }
        
        self.render_worker = MapRenderWorker(
            self.width(), self.height(),
            self.center_x, self.center_y, self.scale,
            map_data, self.zoom_details, self.colors,
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
        if hasattr(self, "is_rendering_detailed") and self.is_rendering_detailed:
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            self.dragging = True
            self.last_mouse_pos = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseReleaseEvent(self, event):
        """
        Terminates active panning.
        """
        if hasattr(self, "is_rendering_detailed") and self.is_rendering_detailed:
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            self.dragging = False
            self.setCursor(Qt.ArrowCursor)

    def mouseMoveEvent(self, event):
        """
        Pans the map view relative to movement delta and converts pointer to coordinates.
        """
        if hasattr(self, "is_rendering_detailed") and self.is_rendering_detailed:
            event.accept()
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
        if hasattr(self, "is_rendering_detailed") and self.is_rendering_detailed:
            event.accept()
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

    def mouseDoubleClickEvent(self, event):
        """
        Performs vector hit-detection on the map to trigger a custom color picker.
        """
        if hasattr(self, "is_rendering_detailed") and self.is_rendering_detailed:
            event.accept()
            return
        if event.button() == Qt.LeftButton and self.data_loaded:
            px = event.position().x()
            py = event.position().y()
            mx, my = self.to_mercator(px, py)
            click_point = QPointF(mx, my)
            
            # Create a viewport bounding rect to limit spatial queries
            vx1, vy2 = self.to_mercator(0, 0)
            vx2, vy1 = self.to_mercator(self.width(), self.height())
            viewport_rect = QRectF(vx1, vy1, vx2 - vx1, vy2 - vy1)
            
            # Resolve simplification scale key once
            sim_key = None
            for k in sorted(self.zoom_details.keys(), key=float):
                if float(k) <= self.scale:
                    sim_key = k
                else:
                    break
            if sim_key is None:
                sim_key = "0.0001"
            
            matched_type = None
            
            # Hit-test layers in drawing order (top to bottom)
            # 1. Check water bodies
            visible_water = self.waterbodies_index[sim_key].query(viewport_rect)
            for item in visible_water:
                if item.polygon.containsPoint(click_point, Qt.OddEvenFill):
                    matched_type = "waterbody"
                    break
            
            # 2. Check wetlands (bogs)
            if not matched_type:
                visible_wetlands = self.wetlands_index[sim_key].query(viewport_rect)
                for item in visible_wetlands:
                    if item.polygon.containsPoint(click_point, Qt.OddEvenFill):
                        matched_type = "wetland"
                        break
                        
            # 3. Check forest areas
            if not matched_type:
                visible_forests = self.forests_index[sim_key].query(viewport_rect)
                for item in visible_forests:
                    if item.polygon.containsPoint(click_point, Qt.OddEvenFill):
                        matched_type = "forest"
                        break
                        
            # 4. Check main landmass coastlines
            if not matched_type:
                visible_coastlines = self.coastlines_index[sim_key].query(viewport_rect)
                for item in visible_coastlines:
                    if item.polygon.containsPoint(click_point, Qt.OddEvenFill):
                        matched_type = "land"
                        break
                        
            # 5. Fallback to ocean if within global coordinates
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
        
        # Toolbar controls setup
        self.toolbar = QToolBar("Controls")
        self.toolbar.setIconSize(self.toolbar.iconSize() * 0.8)
        self.addToolBar(self.toolbar)
        
        self.btn_open_file = QPushButton("📂 Open Map File")
        self.btn_open_file.clicked.connect(self.open_map_file)
        self.toolbar.addWidget(self.btn_open_file)
        self.toolbar.addSeparator()
        
        self.btn_zoom_in = QPushButton("➕ Zoom In")
        self.btn_zoom_in.clicked.connect(self.zoom_in)
        self.toolbar.addWidget(self.btn_zoom_in)
        
        self.btn_zoom_out = QPushButton("➖ Zoom Out")
        self.btn_zoom_out.clicked.connect(self.zoom_out)
        self.toolbar.addWidget(self.btn_zoom_out)
        
        self.btn_fit = QPushButton("🗺 Fit Map")
        self.btn_fit.clicked.connect(self.fit_country)
        self.toolbar.addWidget(self.btn_fit)
        
        self.toolbar.addSeparator()
        
        # Auto-complete search bar setup
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(constants.STR_SEARCH_PLACEHOLDER)
        self.search_box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.search_box.setMinimumWidth(220)
        self.search_box.returnPressed.connect(self.search_place)
        self.toolbar.addWidget(self.search_box)
        
        self.btn_search = QPushButton("🔍 Search")
        self.btn_search.clicked.connect(self.search_place)
        self.toolbar.addWidget(self.btn_search)
        
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
                QTimer.singleShot(100, self.convert_pbf_on_startup)
            else:
                self.loader = MapDataLoader(self.db_path, self.map_widget.zoom_details)
                self.loader.progress_message.connect(self.show_loading_status)
                self.loader.progress_pct.connect(self.update_load_progress)
                self.loader.data_loaded.connect(self.on_data_loaded)
                
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
  
    def on_data_loaded(self, data):
        """
        Executes on data loaded signal. Setup auto-completer and updates map.
        
        Passes the active database filename to the MapWidget to enable per-map viewport saving.
        """
        self.progress_bar.hide()
        self.status_bar.showMessage(constants.STR_MAP_LOADED_SUCCESS, 5000)
        self.map_widget.set_map_data(data, os.path.basename(self.db_path))
        
        # Update window title dynamically based on the database name
        map_name = os.path.splitext(os.path.basename(self.db_path))[0]
        map_name = map_name.replace('_', ' ').replace('-', ' ').title()
        self.setWindowTitle(f"{constants.STR_APP_TITLE_BASE} - {map_name}")
        
        # Map place names to Search Completer
        names = [p["name"] for p in data["places"]]
        completer = QCompleter(names, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self.search_box.setCompleter(completer)
  
    def update_coordinates(self, lat, lon):
        self.coord_label.setText(f"Lat: {lat:.5f}° N, Lon: {lon:.5f}° W")
  
    def zoom_in(self):
        self.map_widget.zoom_in()
   
    def zoom_out(self):
        self.map_widget.zoom_out()
  
    def fit_country(self):
        if self.map_widget.is_rendering_detailed:
            return
        if self.map_widget.data_loaded:
            self.map_widget.fit_to_bbox(self.map_widget.global_bbox)
            
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
        self.btn_zoom_in.setEnabled(False)
        self.btn_zoom_out.setEnabled(False)
        self.btn_fit.setEnabled(False)
        self.search_box.setEnabled(False)
        self.btn_search.setEnabled(False)
        
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
        self.btn_zoom_in.setEnabled(True)
        self.btn_zoom_out.setEnabled(True)
        self.btn_fit.setEnabled(True)
        self.search_box.setEnabled(True)
        self.btn_search.setEnabled(True)
        
        QMessageBox.information(self, "Success", constants.STR_CONVERT_SUCCESS)
        self.load_new_database(db_path)
        
    def on_prep_error(self, err_msg):
        self.btn_open_file.setEnabled(True)
        self.btn_zoom_in.setEnabled(True)
        self.btn_zoom_out.setEnabled(True)
        self.btn_fit.setEnabled(True)
        self.search_box.setEnabled(True)
        self.btn_search.setEnabled(True)
        
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
        
        self.loader = MapDataLoader(self.db_path, self.map_widget.zoom_details)
        self.loader.progress_message.connect(self.show_loading_status)
        self.loader.progress_pct.connect(self.update_load_progress)
        self.loader.data_loaded.connect(self.on_data_loaded)
        self.loader.start()
  
    def search_place(self):
        """
        Searches places by name and centers camera focal point around search target.
        """
        if self.map_widget.is_rendering_detailed:
            return
        if not self.map_widget.data_loaded:
            return
            
        search_text = self.search_box.text().strip()
        if not search_text:
            return
            
        # Match exact name first, then partial contents
        matches = [p for p in self.map_widget.places if p["name"].lower() == search_text.lower()]
        if not matches:
            matches = [p for p in self.map_widget.places if search_text.lower() in p["name"].lower()]
            
        if matches:
            target = matches[0]
            self.map_widget.center_x = target["x"]
            self.map_widget.center_y = target["y"]
            
            # Dynamic zoom scale adjustments depending on feature types
            zoom_map = {'city': 0.05, 'town': 0.1, 'village': 0.25}
            self.map_widget.scale = zoom_map.get(target["place_type"], 0.1)
            
            self.map_widget.start_interaction()
            self.map_widget.constrain_view()
            self.map_widget.update()
            
            self.status_bar.showMessage(f"Centered on {target['name']} ({target['place_type'].capitalize()})", 3000)
            self.map_widget.setFocus()
        else:
            QMessageBox.information(self, "Search", constants.STR_SEARCH_NOT_FOUND.format(search_text))
 
    # (Developer Config methods and Reset settings handler removed)
 
 
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
        db_files = [f for f in os.listdir(".") if f.endswith(".db") and f != "dev_settings.json"]
        
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
