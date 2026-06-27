"""Contract test for the HVAC reference pack's manifest.

Migrated from gdx_dispatch core (was tests/test_catalog_packs.py::
test_reference_hvac_pack_manifest_shape, where it only ever ran via
importorskip). It lives here now that the pack does.

Importing gdx_plugin_hvac binds to gdx_dispatch.plugin_api, which is
stdlib-only (no DB/FastAPI) — so CI runs this with core source on PYTHONPATH
rather than a full core install. See .github/workflows/contract.yml.
"""
import gdx_plugin_hvac


def test_manifest_top_level_shape():
    m = gdx_plugin_hvac.manifest
    assert m.key == "hvac"
    assert m.name == "HVAC Catalog Pack"
    # data-only pack: contributes catalog types + pricing, no router
    assert len(m.catalog_types) == 1
    assert len(m.pricing_strategies) == 1


def test_catalog_type_shape():
    (hvac_type,) = gdx_plugin_hvac.manifest.catalog_types
    assert hvac_type["key"] == "hvac_unit"
    field_names = {f["name"] for f in hvac_type["field_schema"]}
    assert {"tonnage", "seer", "refrigerant", "energy_star"} <= field_names


def test_pricing_strategy_shape():
    (strategy,) = gdx_plugin_hvac.manifest.pricing_strategies
    assert strategy["kind"] == "markup"
    assert strategy["params"]["pct"] == 0.4
