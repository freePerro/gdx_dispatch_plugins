# gdx_dispatch_plugins

Operator-installable plugins for [gdx_dispatch](https://github.com/freePerro/gdx_dispatch).

Each subdirectory is an independent, `pip install`-able Python package that
registers itself under the `gdx.modules` entry-point group. The gdx_dispatch
**plugin-host** discovers installed plugins via that group and mounts each one —
see core's `ADR-013` (third-party module plugins) for the architecture.

## Plugins

| Package | What it is |
| --- | --- |
| [`gdx-plugin-example`](gdx-plugin-example) | Reference plugin — exercises the full plugin contract end to end (router + models + UI). |
| [`gdx-plugin-hvac`](gdx-plugin-hvac) | Reference Catalog Pack (ADR-015) — contributes an HVAC catalog type + pricing strategy as data; no router. |
| [`gdx-plugin-n8n`](gdx-plugin-n8n) | In-app n8n Automations console — subscribes to every business event, mirrors them into its own table, and renders Activity / Available Events / Connect / Setup screens. The WordPress-model integration surface for the n8n flagship. |
| [`gdx-plugin-cellcomms`](gdx-plugin-cellcomms) | Texts and calls from the owner's personal Android cell, next to the Phone.com line — live incoming feed relayed by core's cell-gateway webhook, plus SMS Backup & Restore XML import for history and outgoing. Built in core PR #752, moved here 2026-09-20. |

> The proprietary `gdx-plugin-chi-pricing` plugin lives outside version control
> and is **not** part of this repo.

## The contract

Plugins depend on exactly one stable surface from core, `gdx_dispatch.plugin_api`:

- `plugin_api.PluginManifest` / `plugin_api.base.PluginBase`
- `plugin_api.context` — `PluginContext`, `get_plugin_db`, `require_module`
- `plugin_api.catalog` — Catalog Pack helpers

There is **no runtime dependency** on `gdx_dispatch` in any plugin's
`pyproject.toml`. In production the plugin-host image already ships `plugin_api`,
and core's `reconcile.py` pip-installs only the plugin itself into the `/plugins`
volume at host boot.

## Installation (production)

Operators don't clone this repo. A plugin is installed in-app (owner-only admin
UI) — from the storefront, or by uploading its built wheel; core records the
intent and the plugin-host materializes it on restart via `reconcile.py`. See
core's `gdx_dispatch/docs/plugin_file_install.md`.

## Getting listed in the in-app storefront

**This repository is the curation authority.** A plugin appears in the app's
plugin storefront if and only if it is merged here and published by the
`catalog` workflow — nothing else writes `catalog.json`.

To be listable, a plugin needs a `[tool.gdx.catalog]` table in its
`pyproject.toml`:

```toml
[tool.gdx.catalog]
name = "n8n Automations"          # the card title
tier = "starter"                  # optional — core ignores tiers since 2026-09-06
permissions = ["events"]          # shown to the owner BEFORE they install
description = "One sentence for the card."
author = "GDX Dispatch"
```

Declared statically on purpose: a plugin's manifest is executable Python, and
the storefront must not run third-party code just to show a card. `key`,
`distribution` and `version` are **not** declared — they are derived from the
`gdx.modules` entry-point name and the wheel's own metadata, so they cannot
drift from what actually installs. CI imports each manifest and fails the build
if the declared name or permissions (or a declared tier) disagree with it.

**Your wheel must declare no dependencies** (`Requires-Dist` empty). This is not
style: plugin-host has no network egress, and `pip install --target` sets
`ignore_installed`, so pip does not treat a package already present in the image
as satisfying a requirement — any declared dependency reaches for the index and
fails the install. Libraries vendored in the plugin-host image are what you may
**import**, never what you may **declare**. CI enforces this.

To build a distributable wheel locally:

```bash
python -m build gdx-plugin-example   # → gdx-plugin-example/dist/*.whl
```

## Developing a plugin

Building a wheel needs nothing but `build`. **Importing or running** a plugin
needs the core contract on your path, because `plugin_api.context` binds to
core's DB session. The simplest dev setup is to run against core's source:

```bash
# alongside this repo:
git clone https://github.com/freePerro/gdx_dispatch.git
pip install -r gdx_dispatch/gdx_dispatch/requirements.txt
export PYTHONPATH="$PWD/gdx_dispatch"   # makes `gdx_dispatch.plugin_api` importable
```

For a full runtime (DB, mounted routes, browser-driving plugins) use the
plugin-host image from core (`docker/Dockerfile.plugin-host`) rather than a bare
venv.

## CI

- `.github/workflows/build.yml` — builds and `twine check`s every plugin on each
  push/PR. Build-only and hermetic (no core deps), since packaging never imports
  the code.
- `.github/workflows/contract.yml` — imports a plugin and asserts its manifest
  shape against the real `gdx_dispatch.plugin_api`. That surface is stdlib-only,
  so it shallow-clones core onto `PYTHONPATH` rather than installing it. Covers
  `gdx-plugin-hvac` (manifest shape, stdlib-only) and `gdx-plugin-cellcomms`
  (its whole suite, router and the core shim relay included). A router-bearing
  plugin's job installs core's **requirement set** (`requirements.txt`, the
  same set the plugin-host image installs) and nothing else, so importing a
  library the image lacks fails in CI instead of silently dropping the plugin
  at host boot.
