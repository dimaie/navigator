# tests/test_rules.py
"""
Tests for rules.py — road widths, predicates, place classification.
"""

import pytest
from rules import (
    ROAD_CATEGORIES,
    is_valid_highway,
    get_parent_road_type,
    get_road_width_for_scale,
    is_coastline,
    is_river_canal,
    is_wetland,
    is_forest,
    is_waterbody,
    is_valid_polygon,
    is_city_town_village,
    get_place_font_style,
    get_place_marker_style,
)


# ---------------------------------------------------------------------------
# get_road_width_for_scale
# ---------------------------------------------------------------------------

class TestGetRoadWidthForScale:
    """Tests for get_road_width_for_scale."""

    def test_unknown_road_type_returns_zero(self):
        """Non-existent road type 'cyclepath' always returns 0.0."""
        assert get_road_width_for_scale("cyclepath", 0.01, False, 14) == 0.0

    def test_interacting_residential_returns_zero(self):
        """During interaction, residential road is suppressed (returns 0.0)."""
        assert get_road_width_for_scale("residential", 0.01, True, 14) == 0.0

    def test_interacting_motorway_returns_1_5(self):
        """During interaction, motorway always returns 1.5."""
        assert get_road_width_for_scale("motorway", 0.01, True, 14) == 1.5

    def test_interacting_trunk_returns_1_0(self):
        """During interaction, trunk always returns 1.0."""
        assert get_road_width_for_scale("trunk", 0.01, True, 14) == 1.0

    def test_ignore_interaction_overrides_suppression(self):
        """ignore_interaction=True makes residential visible even while interacting."""
        result = get_road_width_for_scale("residential", 0.01, True, 14, ignore_interaction=True)
        assert result > 0.0

    def test_lod_threshold_hides_road_at_index(self):
        """tertiary (index=4), lod_threshold=4 → 0.0 (index >= threshold)."""
        idx = ROAD_CATEGORIES.index("tertiary")
        assert idx == 4
        result = get_road_width_for_scale("tertiary", 0.01, False, 4)
        assert result == 0.0

    def test_lod_threshold_shows_road_below_threshold(self):
        """tertiary (index=4), lod_threshold=5 → non-zero at a large scale."""
        result = get_road_width_for_scale("tertiary", 0.01, False, 5)
        assert result > 0.0

    @pytest.mark.parametrize("scale", [0.00010, 0.001, 0.01, 0.1])
    def test_motorway_has_largest_width_in_band(self, scale):
        """In every zoom band, motorway width is the largest of all road types."""
        motorway_w = get_road_width_for_scale("motorway", scale, False, len(ROAD_CATEGORIES))
        for road in ROAD_CATEGORIES[1:]:
            w = get_road_width_for_scale(road, scale, False, len(ROAD_CATEGORIES))
            assert motorway_w >= w, f"motorway({motorway_w}) < {road}({w}) at scale={scale}"

    @pytest.mark.parametrize("scale", [0.00010, 0.001, 0.01, 0.1])
    def test_primary_width_less_than_motorway_in_same_band(self, scale):
        """primary width < motorway width in every scale band."""
        motorway_w = get_road_width_for_scale("motorway", scale, False, len(ROAD_CATEGORIES))
        primary_w = get_road_width_for_scale("primary", scale, False, len(ROAD_CATEGORIES))
        assert primary_w <= motorway_w


# ---------------------------------------------------------------------------
# get_parent_road_type
# ---------------------------------------------------------------------------

class TestGetParentRoadType:
    """Tests for get_parent_road_type."""

    @pytest.mark.parametrize("sub_type,expected", [
        ("primary_link", "primary"),
        ("motorway_link", "motorway"),
        ("trunk_link", "trunk"),
        ("secondary_link", "secondary"),
        ("tertiary_link", "tertiary"),
        ("residential", "residential"),  # no change
        ("motorway", "motorway"),        # no change
        (None, "residential"),           # None → fallback
    ])
    def test_various_sub_types(self, sub_type, expected):
        assert get_parent_road_type(sub_type) == expected


# ---------------------------------------------------------------------------
# is_valid_highway
# ---------------------------------------------------------------------------

class TestIsValidHighway:
    """Tests for is_valid_highway."""

    @pytest.mark.parametrize("sub_type", ["motorway", "residential", "primary_link", "cycleway"])
    def test_valid_highway_types(self, sub_type):
        assert is_valid_highway(sub_type) is True

    @pytest.mark.parametrize("sub_type", ["motorway_junction", "construction", "", "undefined"])
    def test_invalid_highway_types(self, sub_type):
        assert is_valid_highway(sub_type) is False


# ---------------------------------------------------------------------------
# is_coastline
# ---------------------------------------------------------------------------

class TestIsCoastline:
    def test_coastline_natural_returns_true(self):
        assert is_coastline("coastline") is True

    def test_non_coastline_returns_false(self):
        assert is_coastline("water") is False


# ---------------------------------------------------------------------------
# is_river_canal
# ---------------------------------------------------------------------------

class TestIsRiverCanal:
    def test_river_returns_true(self):
        assert is_river_canal("river") is True

    def test_canal_returns_true(self):
        assert is_river_canal("canal") is True

    def test_stream_returns_false(self):
        assert is_river_canal("stream") is False


# ---------------------------------------------------------------------------
# is_wetland
# ---------------------------------------------------------------------------

class TestIsWetland:
    def test_wetland_natural_returns_true(self):
        assert is_wetland("wetland") is True

    def test_wood_returns_false(self):
        assert is_wetland("wood") is False


# ---------------------------------------------------------------------------
# is_forest
# ---------------------------------------------------------------------------

class TestIsForest:
    def test_wood_natural_returns_true(self):
        assert is_forest("wood", None) is True

    def test_forest_landuse_returns_true(self):
        assert is_forest(None, "forest") is True

    def test_both_none_returns_false(self):
        assert is_forest(None, None) is False

    def test_unrelated_values_return_false(self):
        assert is_forest("grass", "meadow") is False


# ---------------------------------------------------------------------------
# is_waterbody
# ---------------------------------------------------------------------------

class TestIsWaterbody:
    def test_natural_water_returns_true(self):
        assert is_waterbody("water", None, None) is True

    def test_riverbank_waterway_returns_true(self):
        assert is_waterbody(None, "riverbank", None) is True

    def test_reservoir_landuse_returns_true(self):
        assert is_waterbody(None, None, "reservoir") is True

    def test_unrelated_values_return_false(self):
        assert is_waterbody("grass", "stream", "forest") is False


# ---------------------------------------------------------------------------
# is_valid_polygon
# ---------------------------------------------------------------------------

class TestIsValidPolygon:
    def test_wetland_with_3_nodes_is_invalid(self):
        assert is_valid_polygon("wetland", 3) is False

    def test_wetland_with_4_nodes_is_valid(self):
        assert is_valid_polygon("wetland", 4) is True

    def test_forest_with_3_nodes_is_invalid(self):
        assert is_valid_polygon("forest", 3) is False

    def test_forest_with_4_nodes_is_valid(self):
        assert is_valid_polygon("forest", 4) is True

    def test_primary_with_2_nodes_is_always_valid(self):
        """Non-wetland/forest types are always valid regardless of count."""
        assert is_valid_polygon("primary", 2) is True

    def test_waterbody_with_1_node_is_valid(self):
        assert is_valid_polygon("waterbody", 1) is True


# ---------------------------------------------------------------------------
# is_city_town_village
# ---------------------------------------------------------------------------

class TestIsCityTownVillage:
    def test_city_with_name_is_true(self):
        assert is_city_town_village("city", "Dublin") is True

    def test_city_without_name_is_false(self):
        assert is_city_town_village("city", "") is False

    def test_hamlet_is_false(self):
        assert is_city_town_village("hamlet", "Ballybofey") is False

    def test_county_with_name_is_true(self):
        assert is_city_town_village("county", "Wicklow") is True


# ---------------------------------------------------------------------------
# get_place_font_style
# ---------------------------------------------------------------------------

class TestGetPlaceFontStyle:
    @pytest.mark.parametrize("place_type,expected_size,expected_bold", [
        ("city",    11, True),
        ("town",     9, False),
        ("county",  10, True),
        ("village",  8, False),
        ("unknown",  8, False),
        ("hamlet",   8, False),
    ])
    def test_font_style(self, place_type, expected_size, expected_bold):
        size, bold = get_place_font_style(place_type)
        assert size == expected_size
        assert bold == expected_bold


# ---------------------------------------------------------------------------
# get_place_marker_style
# ---------------------------------------------------------------------------

class TestGetPlaceMarkerStyle:
    @pytest.mark.parametrize("place_type,expected_size,expected_color", [
        ("city",   4.0, "#E74C3C"),
        ("county", 0.0, ""),
        ("town",   2.5, "#2C3E50"),
        ("village",2.5, "#2C3E50"),  # falls through to default
    ])
    def test_marker_style(self, place_type, expected_size, expected_color):
        size, color = get_place_marker_style(place_type)
        assert size == expected_size
        assert color == expected_color
