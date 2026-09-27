"""Rough-profit plugin. Exports `manifest`, registered under the gdx.modules
entry-point group (pyproject.toml) for the plugin-host to discover.

"Do we look profitable or not?" — one row per month from the bank feed,
invoices and supplier statements, sorted by owner-written payee rules. Reads
core tables, writes only its own. Design record: core's
docs/design/rough-profit-plugin-plan.md.
"""
from gdx_dispatch.plugin_api import PluginManifest
from gdx_plugin_roughprofit import models  # noqa: F401 — registers tables on PluginBase
from gdx_plugin_roughprofit.router import router
from gdx_plugin_roughprofit.ui import UI

manifest = PluginManifest(
    key="roughprofit",
    name="Rough Profit",
    # The release this was built and tested against. Nothing here needs a
    # newer host surface than list/create/help screens, but an untested floor
    # is a guess.
    requires="gdx>=1.125.0",
    router=router,
    ui=UI,
)
