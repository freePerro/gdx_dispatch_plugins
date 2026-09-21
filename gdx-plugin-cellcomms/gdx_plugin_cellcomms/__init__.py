"""Cell-comms plugin. Exports `manifest`, registered under the gdx.modules
entry-point group (pyproject.toml) for the plugin-host to discover.

Personal Android cell → GDX: a live incoming feed relayed by the core
cell-gateway webhook (routers/cell_gateway.py) into /ingest here, plus an
SMS Backup & Restore XML backfill for outgoing + history. (Design record:
the cell-comms nomad-gateway plan among the design records.)

Customer events trigger a rematch pass, re-linking rows whose number didn't
match a customer at ingest time — the "customer created the day after their
first text" hole. (Not a schedule: the manifest `schedules` field is
catalogued but no runner executes it today.)
"""
from gdx_dispatch.plugin_api import PluginManifest
from gdx_plugin_cellcomms import models  # noqa: F401 — registers tables on PluginBase
from gdx_plugin_cellcomms.handler import handle_customer_event
from gdx_plugin_cellcomms.router import router
from gdx_plugin_cellcomms.ui import UI

manifest = PluginManifest(
    key="cellcomms",
    name="Cell Texts & Calls",
    # Three of this plugin's four surfaces exist only from core 1.122.0 (#752):
    # the cell-gateway shim, the `upload` screen type and `secondary_action`.
    # On an older host the card would install an inert plugin — discovery's
    # compat gate skips it with a log line instead. (The host reads its own
    # version from APP_VERSION; a plugin-host without it counts as version 0.)
    requires="gdx>=1.122.0",
    router=router,
    ui=UI,
    permissions=("events",),
    events=("customer.created", "customer.updated"),
    event_handler=handle_customer_event,
)
