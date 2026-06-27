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
UI) or by uploading its built wheel; core records the intent and the plugin-host
materializes it on restart via `reconcile.py`. See core's
`docs/plugin_file_install.md`.

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
  `gdx-plugin-hvac` today; data-only packs fit here. Plugins whose import pulls
  in core's DB/web stack (e.g. ones with a router) are verified against the
  plugin-host in core instead.
