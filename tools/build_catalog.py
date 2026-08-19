"""Generate `catalog.json` — the index the in-app plugin storefront browses.

Reads only STATIC sources: each built wheel's `METADATA` and its package's
`[tool.gdx.catalog]` table. It never imports a plugin, which matters twice
over: a plugin's manifest is executable Python (importing it to read metadata
would be running third-party code just to list it), and several plugins pull in
core's DB/web stack on import, so they cannot be imported in a packaging job at
all.

Two static facts do NOT need declaring, and are derived so they cannot drift:

* the plugin **key** is the `gdx.modules` entry-point name;
* the **distribution** and **version** come from the wheel's own metadata.

## The dependency rule

`Requires-Dist` must be EMPTY, and this script fails the build otherwise.

That is not tidiness, it is the only thing that installs. plugin-host has no
network egress, and `pip install --target` sets `ignore_installed=True` — so pip
does not count a package already present in the image as satisfying a
requirement. A wheel declaring *any* dependency therefore reaches for the index
and fails (the 2026-06-29 outage). The vendored libraries in the plugin-host
image are what a plugin may **import**, never what it may **declare**.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import zipfile

try:  # 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

SCHEMA_VERSION = 1
VALID_TIERS = {"starter", "professional", "business"}
#: Mirrors gdx_dispatch.plugin_api.manifest.PERMISSIONS. Kept as a literal so
#: this script stays importable without core on the path (CI has no core here).
VALID_PERMISSIONS = {"browser", "events", "schedules", "services"}


class CatalogError(Exception):
    """A plugin cannot be listed. Always names the plugin and the fix."""


def _wheel_metadata(wheel: pathlib.Path) -> dict[str, list[str]]:
    """Parse the wheel's METADATA into {field: [values]} without installing it."""
    with zipfile.ZipFile(wheel) as z:
        name = next((n for n in z.namelist()
                     if n.endswith(".dist-info/METADATA")), None)
        if name is None:
            raise CatalogError(f"{wheel.name}: no dist-info/METADATA in the wheel")
        raw = z.read(name).decode("utf-8", "replace")
    fields: dict[str, list[str]] = {}
    for line in raw.splitlines():
        if not line.strip():
            break  # headers end at the first blank line; the body is the README
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        fields.setdefault(k.strip(), []).append(v.strip())
    return fields


def _entry_point_key(pyproject: dict, plugin_dir: str) -> str:
    eps = (pyproject.get("project", {})
           .get("entry-points", {})
           .get("gdx.modules", {}))
    if len(eps) != 1:
        raise CatalogError(
            f"{plugin_dir}: expected exactly one gdx.modules entry point, found {len(eps)}. "
            "The entry-point name is the plugin key the app installs it under."
        )
    return next(iter(eps))


def _catalog_table(pyproject: dict, plugin_dir: str) -> dict:
    table = pyproject.get("tool", {}).get("gdx", {}).get("catalog")
    if not table:
        raise CatalogError(
            f"{plugin_dir}: missing [tool.gdx.catalog]. The storefront shows a plugin's "
            "display name, tier and permissions BEFORE install, so they must be readable "
            "without importing (and therefore running) the plugin."
        )
    return table


def build_entry(plugin_dir: pathlib.Path, wheel: pathlib.Path, base_url: str) -> dict:
    pyproject = tomllib.loads((plugin_dir / "pyproject.toml").read_text())
    name = plugin_dir.name
    table = _catalog_table(pyproject, name)
    meta = _wheel_metadata(wheel)

    requires_dist = meta.get("Requires-Dist", [])
    if requires_dist:
        raise CatalogError(
            f"{name}: declares Requires-Dist {requires_dist!r}, which cannot install on the "
            "plugin-host (no network egress, and pip --target ignores already-installed "
            "packages). Vendored libraries may be IMPORTED but never DECLARED — remove the "
            "dependency, or get it vendored into the plugin-host image first."
        )

    tier = table.get("tier")
    if tier not in VALID_TIERS:
        raise CatalogError(f"{name}: tier {tier!r} is not one of {sorted(VALID_TIERS)}")

    permissions = list(table.get("permissions", []))
    unknown = set(permissions) - VALID_PERMISSIONS
    if unknown:
        raise CatalogError(
            f"{name}: unknown permission(s) {sorted(unknown)}; valid are "
            f"{sorted(VALID_PERMISSIONS)}"
        )

    display_name = table.get("name")
    if not display_name:
        raise CatalogError(f"{name}: [tool.gdx.catalog] needs a `name` (shown on the card)")

    distribution = (meta.get("Name") or [None])[0]
    version = (meta.get("Version") or [None])[0]
    if not distribution or not version:
        raise CatalogError(f"{name}: wheel metadata has no Name/Version")

    return {
        # Derived, never declared — so they cannot drift from what installs.
        "key": _entry_point_key(pyproject, name),
        "distribution": distribution,
        "version": version,
        # Declared, because reading them from the manifest would mean importing
        # (running) the plugin just to list it in a storefront.
        "name": display_name,
        "description": table.get("description")
                       or (meta.get("Summary") or [""])[0],
        "author": table.get("author", ""),
        "tier": tier,
        "permissions": permissions,
        "requires": table.get("requires", ""),
        # v1 lists free plugins only; the field is reserved so a paid listing can
        # be added later without a schema break.
        "license": "free",
        "wheel_url": f"{base_url.rstrip('/')}/{wheel.name}",
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "size": wheel.stat().st_size,
    }


def build_catalog(repo: pathlib.Path, base_url: str, plugins: list[str]) -> dict:
    entries, errors = [], []
    for plugin in plugins:
        plugin_dir = repo / plugin
        wheels = sorted((plugin_dir / "dist").glob("*.whl"))
        if not wheels:
            errors.append(f"{plugin}: no built wheel in dist/ — run `python -m build {plugin}`")
            continue
        if len(wheels) > 1:
            errors.append(f"{plugin}: {len(wheels)} wheels in dist/; expected exactly one")
            continue
        try:
            entries.append(build_entry(plugin_dir, wheels[0], base_url))
        except CatalogError as exc:
            errors.append(str(exc))
    if errors:
        raise CatalogError("\n".join(errors))

    keys = [e["key"] for e in entries]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        raise CatalogError(f"duplicate plugin key(s) {sorted(dupes)} — keys must be unique")

    return {
        "schema_version": SCHEMA_VERSION,
        "plugins": sorted(entries, key=lambda e: e["key"]),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=".", type=pathlib.Path)
    ap.add_argument("--base-url", required=True,
                    help="URL prefix the wheels are served from (the release asset base)")
    ap.add_argument("--out", default="catalog.json", type=pathlib.Path)
    ap.add_argument("plugins", nargs="+", help="plugin directories to include")
    args = ap.parse_args(argv)

    try:
        catalog = build_catalog(args.repo, args.base_url, args.plugins)
    except CatalogError as exc:
        print(f"catalog build failed:\n{exc}", file=sys.stderr)
        return 1

    args.out.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.out} with {len(catalog['plugins'])} plugin(s): "
          + ", ".join(e["key"] for e in catalog["plugins"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
