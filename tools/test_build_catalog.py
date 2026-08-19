"""Tests for the storefront catalog builder.

The catalog is what an owner reads BEFORE installing — including the
permissions a plugin will ask for. Wrong data here is worse than no catalog, so
the builder refuses to emit a listing it cannot stand behind.

Run: `python -m pytest tools/test_build_catalog.py`
"""
from __future__ import annotations

import base64
import hashlib
import json
import zipfile

import pytest

from build_catalog import CatalogError, build_catalog, build_entry

BASE = "https://example.invalid/dl"


def _wheel(plugin_dir, dist="demo-plugin", version="1.0.0", summary="A demo.",
           requires=()):
    """A real wheel (zip + dist-info METADATA), not a stub."""
    info = f"{dist.replace('-', '_')}-{version}.dist-info"
    meta = f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\nSummary: {summary}\n"
    for r in requires:
        meta += f"Requires-Dist: {r}\n"
    meta += "\nLong description body, which must not be parsed as headers.\n"
    files = {f"{info}/METADATA": meta,
             f"{info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    recs = []
    for n, b in files.items():
        raw = b.encode()
        d = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
        recs.append(f"{n},sha256={d},{len(raw)}")
    files[f"{info}/RECORD"] = "\n".join(recs) + f"\n{info}/RECORD,,\n"

    dist_dir = plugin_dir / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    whl = dist_dir / f"{dist.replace('-', '_')}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        for n, b in files.items():
            z.writestr(n, b)
    return whl


def _plugin(tmp_path, name="demo-plugin", key="demo", table=True, **overrides):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    toml = f'''[project]
name = "{name}"
version = "1.0.0"

[project.entry-points."gdx.modules"]
{key} = "gdx_plugin_demo:manifest"
'''
    if table:
        fields = {"name": "Demo Plugin", "tier": "starter", "permissions": []}
        fields.update(overrides)
        toml += "\n[tool.gdx.catalog]\n"
        for k, v in fields.items():
            toml += f"{k} = {json.dumps(v)}\n"
    (d / "pyproject.toml").write_text(toml)
    return d


def test_key_and_version_are_derived_not_declared(tmp_path):
    """Deriving them is what stops the catalog drifting from what installs."""
    d = _plugin(tmp_path, key="demo")
    whl = _wheel(d, dist="demo-plugin", version="2.3.4")

    entry = build_entry(d, whl, BASE)

    assert entry["key"] == "demo"                 # the entry-point name
    assert entry["distribution"] == "demo-plugin"  # the wheel's own metadata
    assert entry["version"] == "2.3.4"
    assert entry["name"] == "Demo Plugin"          # declared, for the card
    assert entry["wheel_url"] == f"{BASE}/{whl.name}"
    assert entry["sha256"] == hashlib.sha256(whl.read_bytes()).hexdigest()
    assert entry["license"] == "free"


def test_a_declared_dependency_is_refused(tmp_path):
    """The rule that actually keeps installs working on an egress-less host."""
    d = _plugin(tmp_path)
    whl = _wheel(d, requires=("playwright>=1.40",))

    with pytest.raises(CatalogError) as e:
        build_entry(d, whl, BASE)

    assert "Requires-Dist" in str(e.value)
    assert "playwright" in str(e.value)
    # The message must say what to do, not merely that it failed.
    assert "vendored" in str(e.value).lower()


def test_permissions_travel_into_the_catalog(tmp_path):
    """Doug's requirement: the owner sees the permissions before installing."""
    d = _plugin(tmp_path, permissions=["events"])
    whl = _wheel(d)
    assert build_entry(d, whl, BASE)["permissions"] == ["events"]


def test_an_unknown_permission_is_refused(tmp_path):
    d = _plugin(tmp_path, permissions=["events", "root"])
    whl = _wheel(d)
    with pytest.raises(CatalogError, match="unknown permission"):
        build_entry(d, whl, BASE)


def test_a_bad_tier_is_refused(tmp_path):
    d = _plugin(tmp_path, tier="enterprise")
    whl = _wheel(d)
    with pytest.raises(CatalogError, match="tier"):
        build_entry(d, whl, BASE)


def test_a_plugin_with_no_catalog_table_is_refused(tmp_path):
    d = _plugin(tmp_path, table=False)
    whl = _wheel(d)
    with pytest.raises(CatalogError, match=r"\[tool\.gdx\.catalog\]"):
        build_entry(d, whl, BASE)


def test_the_metadata_body_is_not_parsed_as_headers(tmp_path):
    """METADATA headers end at the first blank line; the README follows."""
    d = _plugin(tmp_path)
    whl = _wheel(d, summary="Real summary.")
    assert build_entry(d, whl, BASE)["description"] == "Real summary."


def test_declared_description_wins_over_the_wheel_summary(tmp_path):
    d = _plugin(tmp_path, description="Card copy.")
    whl = _wheel(d, summary="Packaging summary.")
    assert build_entry(d, whl, BASE)["description"] == "Card copy."


def test_duplicate_keys_are_refused(tmp_path):
    a = _plugin(tmp_path, name="plugin-a", key="same")
    b = _plugin(tmp_path, name="plugin-b", key="same")
    _wheel(a, dist="plugin-a")
    _wheel(b, dist="plugin-b")
    with pytest.raises(CatalogError, match="duplicate plugin key"):
        build_catalog(tmp_path, BASE, ["plugin-a", "plugin-b"])


def test_a_missing_wheel_is_reported_not_skipped(tmp_path):
    """Silently omitting a plugin would read as 'not published yet'."""
    _plugin(tmp_path, name="plugin-a", key="a")
    with pytest.raises(CatalogError, match="no built wheel"):
        build_catalog(tmp_path, BASE, ["plugin-a"])


def test_all_failures_are_reported_at_once(tmp_path):
    """One run should tell an author everything to fix, not the first thing."""
    a = _plugin(tmp_path, name="plugin-a", key="a", tier="bogus")
    b = _plugin(tmp_path, name="plugin-b", key="b")
    _wheel(a, dist="plugin-a")
    _wheel(b, dist="plugin-b", requires=("requests",))

    with pytest.raises(CatalogError) as e:
        build_catalog(tmp_path, BASE, ["plugin-a", "plugin-b"])

    assert "plugin-a" in str(e.value) and "plugin-b" in str(e.value)


def test_catalog_shape_is_stable_and_sorted(tmp_path):
    a = _plugin(tmp_path, name="plugin-z", key="zeta")
    b = _plugin(tmp_path, name="plugin-a", key="alpha")
    _wheel(a, dist="plugin-z")
    _wheel(b, dist="plugin-a")

    catalog = build_catalog(tmp_path, BASE, ["plugin-z", "plugin-a"])

    assert catalog["schema_version"] == 1
    assert [p["key"] for p in catalog["plugins"]] == ["alpha", "zeta"]
