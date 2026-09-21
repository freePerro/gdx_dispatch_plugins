"""The storefront listing must match the plugin it actually installs.

`[tool.gdx.catalog]` is read statically so the app never has to import (run) a
plugin just to show its card — but that means the declared values could drift
from the real `PluginManifest`. Since one of those values is **the permission
list an owner approves before installing**, drift is not cosmetic: the card
would promise one thing and the code would ask for another.

This test closes that loop by importing each manifest and comparing. It needs
core's `plugin_api` (and FastAPI for router-bearing plugins) on the path, so it
runs in the contract job, not the packaging job.

Run: PYTHONPATH=<core>:<each plugin dir> python -m pytest tools/test_catalog_matches_manifest.py
"""
from __future__ import annotations

import importlib
import pathlib

import pytest

try:  # 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

REPO = pathlib.Path(__file__).resolve().parent.parent

#: (plugin directory, importable module). Only plugins published to the
#: storefront — the private one in this repo is never listed.
PUBLISHED = [
    ("gdx-plugin-example", "gdx_plugin_example"),
    ("gdx-plugin-hvac", "gdx_plugin_hvac"),
    ("gdx-plugin-n8n", "gdx_plugin_n8n"),
    ("gdx-plugin-cellcomms", "gdx_plugin_cellcomms"),
]


def _declared(plugin_dir: str) -> dict:
    data = tomllib.loads((REPO / plugin_dir / "pyproject.toml").read_text())
    table = data.get("tool", {}).get("gdx", {}).get("catalog")
    assert table, f"{plugin_dir} has no [tool.gdx.catalog] — it cannot be listed"
    return table


def _entry_point_key(plugin_dir: str) -> str:
    data = tomllib.loads((REPO / plugin_dir / "pyproject.toml").read_text())
    eps = data["project"]["entry-points"]["gdx.modules"]
    assert len(eps) == 1, f"{plugin_dir}: expected exactly one gdx.modules entry point"
    return next(iter(eps))


@pytest.mark.parametrize(("plugin_dir", "module"), PUBLISHED)
def test_declared_listing_matches_the_manifest(plugin_dir, module):
    manifest = importlib.import_module(module).manifest
    declared = _declared(plugin_dir)

    assert declared["name"] == manifest.name, (
        f"{plugin_dir}: catalog name {declared['name']!r} != manifest name {manifest.name!r}"
    )
    # `tier` is optional since core dropped plan tiers (2026-09-06); a listing
    # that still declares one must at least agree with its manifest.
    if "tier" in declared:
        assert declared["tier"] == manifest.tier, (
            f"{plugin_dir}: catalog tier {declared['tier']!r} != manifest tier {manifest.tier!r}"
        )
    # The host-version floor in the catalog must be the one discovery enforces.
    # (Nothing renders or gates on the catalog's `requires` today — the
    # plugin-host's compat gate reads the manifest's — so a mismatch here would
    # be a listing that documents a floor other than the one that applies.)
    assert declared.get("requires", "") == manifest.requires, (
        f"{plugin_dir}: catalog requires {declared.get('requires', '')!r} != manifest "
        f"requires {manifest.requires!r}"
    )
    # The one that matters most: an owner consents to this list before install.
    assert sorted(declared.get("permissions", [])) == sorted(manifest.permissions), (
        f"{plugin_dir}: catalog permissions {declared.get('permissions')} != manifest "
        f"permissions {list(manifest.permissions)} — the storefront would show the owner "
        "a different set than the plugin actually asks for"
    )


def test_permission_mirror_matches_core():
    """build_catalog.py cannot import core (the packaging job has none), so it
    carries a literal copy of KNOWN_PERMISSIONS. This job has core on the path:
    if the copy drifts, a listing gets refused for a permission it really has —
    or worse, accepted for one core no longer knows."""
    import sys

    sys.path.insert(0, str(REPO / "tools"))
    from build_catalog import VALID_PERMISSIONS

    from gdx_dispatch.plugin_api.manifest import KNOWN_PERMISSIONS

    assert VALID_PERMISSIONS == set(KNOWN_PERMISSIONS), (
        f"build_catalog.VALID_PERMISSIONS {sorted(VALID_PERMISSIONS)} != core "
        f"KNOWN_PERMISSIONS {sorted(KNOWN_PERMISSIONS)} — update the literal"
    )


@pytest.mark.parametrize(("plugin_dir", "module"), PUBLISHED)
def test_entry_point_name_is_the_manifest_key(plugin_dir, module):
    """The catalog derives `key` from the entry-point name; core keys off
    `manifest.key`. If those disagree, the app installs one plugin and looks for
    another."""
    manifest = importlib.import_module(module).manifest
    assert _entry_point_key(plugin_dir) == manifest.key
