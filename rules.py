# rules.py

# Whitelisted road categories (ordered from major to minor)
ROAD_CATEGORIES = [
    'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 
    'unclassified', 'residential', 'living_street', 'service', 'pedestrian'
]

# All valid highways tags (including link variations)
VALID_HIGHWAYS = {
    'motorway', 'trunk', 'primary', 'secondary', 'tertiary',
    'unclassified', 'residential', 'living_street', 'service', 'pedestrian',
    'motorway_link', 'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link'
}

def is_valid_highway(sub_type):
    """
    Checks if a highway sub-type is whitelisted for rendering.
    """
    return sub_type in VALID_HIGHWAYS

def get_parent_road_type(sub_type):
    """
    Normalizes a road sub-type (e.g., primary_link) to its parent classification (e.g., primary).
    """
    if sub_type is None:
        return 'residential'
    return sub_type.replace('_link', '')

def get_road_width_for_scale(road_type, scale, is_interacting, lod_roads_threshold, ignore_interaction=False):
    """
    Computes responsive road drawing width (in screen pixels) depending on zoom scale and interaction states.
    """
    if road_type not in ROAD_CATEGORIES:
        return 0.0
        
    # Progressive LOD optimization during active pan/zoom
    if is_interacting and not ignore_interaction:
        if road_type not in ('motorway', 'trunk'):
            return 0.0
        return 1.5 if road_type == 'motorway' else 1.0
        
    # Filter visibility by LOD threshold
    road_idx = ROAD_CATEGORIES.index(road_type)
    if road_idx >= lod_roads_threshold:
        return 0.0
        
    # Dynamic width sizing mapped to zoom bands
    if scale < 0.00015:
        base_widths = {
            'motorway': 1.5, 'trunk': 1.0, 'primary': 0.0, 'secondary': 0.0, 'tertiary': 0.0, 
            'unclassified': 0.0, 'residential': 0.0, 'living_street': 0.0, 'service': 0.0, 'pedestrian': 0.0
        }
    elif scale < 0.0004:
        base_widths = {
            'motorway': 2.0, 'trunk': 1.5, 'primary': 1.0, 'secondary': 0.0, 'tertiary': 0.0, 
            'unclassified': 0.0, 'residential': 0.0, 'living_street': 0.0, 'service': 0.0, 'pedestrian': 0.0
        }
    elif scale < 0.0012:
        base_widths = {
            'motorway': 2.5, 'trunk': 2.0, 'primary': 1.5, 'secondary': 1.0, 'tertiary': 0.8, 
            'unclassified': 0.0, 'residential': 0.0, 'living_street': 0.0, 'service': 0.0, 'pedestrian': 0.0
        }
    elif scale < 0.004:
        base_widths = {
            'motorway': 3.5, 'trunk': 3.0, 'primary': 2.5, 'secondary': 1.8, 'tertiary': 1.2, 
            'unclassified': 1.0, 'residential': 0.0, 'living_street': 0.0, 'service': 0.0, 'pedestrian': 0.0
        }
    elif scale < 0.015:
        base_widths = {
            'motorway': 5.0, 'trunk': 4.5, 'primary': 3.5, 'secondary': 2.5, 'tertiary': 2.0, 
            'unclassified': 1.5, 'residential': 1.0, 'living_street': 1.0, 'service': 0.8, 'pedestrian': 0.8
        }
    else:
        base_widths = {
            'motorway': 7.0, 'trunk': 6.0, 'primary': 5.0, 'secondary': 4.0, 'tertiary': 3.0, 
            'unclassified': 2.5, 'residential': 1.5, 'living_street': 1.5, 'service': 1.2, 'pedestrian': 1.2
        }
        
    return base_widths.get(road_type, 1.0)

# Geographical features rules
def is_coastline(natural):
    return natural == "coastline"

def is_river_canal(waterway):
    return waterway in ("river", "canal")

def is_wetland(natural):
    return natural == "wetland"

def is_forest(natural, landuse):
    return natural == "wood" or landuse == "forest"

def is_waterbody(natural, waterway, landuse):
    return (
        natural == "water" or 
        waterway in ("riverbank", "dock", "basin") or 
        landuse == "reservoir"
    )

def is_valid_polygon(feature_type, node_count):
    if feature_type in ("wetland", "forest"):
        return node_count >= 4
    return True

# Place classification rules
def is_city_town_village(place_type, name):
    return place_type in ("city", "town", "village") and bool(name)

def get_place_font_style(place_type):
    """
    Returns (font_size, bold_flag) depending on place category.
    """
    if place_type == "city":
        return 11, True
    elif place_type == "town":
        return 9, False
    return 8, False

def get_place_marker_style(place_type):
    """
    Returns (dot_size, dot_color_hex) depending on place category.
    """
    if place_type == "city":
        return 4.0, "#E74C3C"
    return 2.5, "#2C3E50"
