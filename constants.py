# constants.py

# Path to the developer/viewer settings file
SETTINGS_PATH = r"./dev_settings.json"

# Bounding box of the Web Mercator projection limits as a universal fallback
WORLD_BBOX = {
    "min_x": -20037508.34,
    "min_y": -20037508.34,
    "max_x": 20037508.34,
    "max_y": 20037508.34
}

# Scale keys definition for multi-resolution LOD indexes
SCALE_KEYS = ["0.0001", "0.0004", "0.001", "0.003", "0.01", "0.04"]

# Default modern Light Mode palette settings
DEFAULT_COLORS = {
    "ocean": "#D4E6F1",
    "land": "#FCFAF2",
    "land_border": "#D5D8DC",
    "forest": "#DCEBD6",
    "wetland": "#E3E5D5",
    "wetland_border": "#D2D5C5",
    "waterbody": "#D4E6F1",
    "waterbody_border": "#A9CCE3",
    "river": "#A9CCE3",
    "road_motorway": "#E05A47",
    "road_casing_motorway": "#932E22",
    "road_trunk": "#F5B041",
    "road_casing_trunk": "#D35400",
    "road_primary": "#F1C40F",
    "road_casing_primary": "#B7950B",
    "road_secondary": "#FFF5CC",
    "road_casing_secondary": "#BDC3C7",
    "road_tertiary": "#FFFFFF",
    "road_casing_tertiary": "#D5D8DC",
    "road_unclassified": "#EAECEE",
    "road_casing_unclassified": "#D5D8DC",
    "road_residential": "#FFFFFF",
    "road_casing_residential": "#F8F9FA",
    "road_service": "#FFFFFF",
    "road_casing_service": "#D5D8DC",
    "road_living_street": "#FFFFFF",
    "road_casing_living_street": "#D5D8DC",
    "road_pedestrian": "#FCF3CF",
    "road_casing_pedestrian": "#F9E79F",
    "road_track": "#A1887F",
    "road_casing_track": "#D7CCC8",
    "road_path": "#90A4AE",
    "road_casing_path": "#CFD8DC",
    "road_footway": "#E57373",
    "road_casing_footway": "#FFCDD2",
    "road_cycleway": "#81C784",
    "road_casing_cycleway": "#C8E6C9",
    "railway": "#566573",
    "railway_dash": "#FCFAF2",
    "boundary_country": "#8E44AD",
    "boundary_county": "#C39BD3"
}

# Default reference scale to zoom details mapping
DEFAULT_ZOOM_DETAILS = {
    "0.0001": {"roads": 1, "places": 50000, "simplification": 5000},
    "0.0004": {"roads": 2, "places": 15000, "simplification": 1250},
    "0.001":  {"roads": 3, "places": 3000,  "simplification": 500},
    "0.003":  {"roads": 5, "places": 500,   "simplification": 150},
    "0.01":   {"roads": 6, "places": 100,   "simplification": 50},
    "0.04":   {"roads": 14, "places": 0,     "simplification": 0}
}

# Premium stylesheet style to zoom panel overlay
ZOOM_PANEL_STYLESHEET = """
    #ZoomPanel {
        background-color: rgba(255, 255, 255, 200);
        border: 1px solid rgba(203, 213, 225, 180);
        border-radius: 6px;
    }
    QPushButton {
        background-color: transparent;
        border: none;
        color: #475569;
        font-size: 16px;
        font-weight: bold;
        min-width: 26px;
        max-width: 26px;
        min-height: 26px;
        max-height: 26px;
        padding: 0;
    }
    QPushButton:hover {
        background-color: rgba(226, 232, 240, 220);
        border-radius: 4px;
        color: #1e293b;
    }
    QPushButton:pressed {
        background-color: rgba(203, 213, 225, 240);
    }
    QSlider::groove:vertical {
        background: rgba(226, 232, 240, 240);
        width: 4px;
        border-radius: 2px;
    }
    QSlider::handle:vertical {
        background: #3B82F6;
        height: 12px;
        width: 12px;
        margin: 0 -4px;
        border-radius: 6px;
    }
    QSlider::handle:vertical:hover {
        background: #2563EB;
    }
"""

# Custom Modern CSS Style overrides for PyQt components
APP_STYLESHEET = """
    QMainWindow {
        background-color: #F8F9FA;
    }
    QToolBar {
        background-color: #FFFFFF;
        border-bottom: 1px solid #E2E8F0;
        spacing: 6px;
        padding: 5px;
    }
    QPushButton {
        background-color: #FFFFFF;
        border: 1px solid #CBD5E1;
        border-radius: 4px;
        padding: 5px 12px;
        color: #334155;
        font-family: 'Segoe UI';
        font-size: 11px;
        font-weight: 500;
    }
    QPushButton:hover {
        background-color: #F1F5F9;
        border-color: #94A3B8;
    }
    QPushButton:pressed {
        background-color: #E2E8F0;
    }
    QLineEdit {
        border: 1px solid #CBD5E1;
        border-radius: 4px;
        padding: 5px 8px;
        background-color: #FFFFFF;
        color: #334155;
        font-family: 'Segoe UI';
        font-size: 11px;
    }
    QLineEdit:focus {
        border-color: #3B82F6;
    }
    QStatusBar {
        background-color: #FFFFFF;
        border-top: 1px solid #E2E8F0;
        color: #64748B;
        font-family: 'Segoe UI';
        font-size: 11px;
    }
    QComboBox {
        border: 1px solid #CBD5E1;
        border-radius: 4px;
        padding: 2px 15px 2px 5px;
        min-width: 6em;
        background-color: #FFFFFF;
        font-family: 'Segoe UI';
        font-size: 10px;
    }
"""

# User-facing and internal UI text strings
STR_APP_TITLE_BASE = "Offline Map Viewer"
STR_READY = "Ready"
STR_NO_MAP_LOADED = "No map loaded. Please click 'Open Map File' to load a map."
STR_DB_READY_LOADING = "Database is ready. Loading maps..."
STR_LOADING_DATA = "Loading map data...\nPlease wait."
STR_NO_MAP_PROMPT = "No map loaded.\nPlease click 'Open Map File' in the toolbar to open a map (.db or .osm.pbf)."
STR_MAP_LOADED_SUCCESS = "Map data loaded successfully"
STR_SEARCH_NOT_FOUND = "Place '{}' not found in the map database."
STR_SEARCH_PLACEHOLDER = "Search city or town..."
STR_CONVERT_STARTING = "Starting conversion..."
STR_CONVERT_SUCCESS = "PBF conversion completed successfully. Loading map..."
STR_CONVERT_FAILED = "Conversion failed"
STR_CONVERT_ERROR = "PBF conversion failed:\n{}"
STR_DB_SELECT_ERROR = "No valid map database was selected. Exiting."
STR_WARNING_SAME_FILE = "Output database cannot be the same file as input PBF."
STR_CENTERED_ON = "Centered on {} ({})"
