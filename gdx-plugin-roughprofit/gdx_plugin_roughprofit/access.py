"""Who may use this plugin — checked by the plugin itself on every route.

The core proxy lets a call through on `plugin.roughprofit.*` OR the blanket
`plugins.read`/`plugins.write` (core/plugin_permissions.py `may_use_plugin`),
and admins hold the blanket keys. The proxy gate alone would show company
profit to every admin, so this module is the real gate (plan §6, D7):

- the owner role always has full access; it is hard-coded, not a row, so
  nobody can lock the owner out;
- anyone else needs a live grant on their role or on their own user id, and
  the higher level wins;
- only the owner changes the access list; an `edit` grant never extends to
  "who can see this".

Nothing is cached: a removed grant is refused on the very next request.
"""
from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from gdx_dispatch.plugin_api.context import PluginContext, get_plugin_context, get_plugin_db
from gdx_plugin_roughprofit.models import Access

OWNER_ROLE = "owner"

VIEW = "view"
EDIT = "edit"
OWNER = "owner"
_RANK = {VIEW: 1, EDIT: 2, OWNER: 3}

NO_ACCESS = (
    "Rough profit is owner-only. Ask the owner to grant you access on its "
    "'Who can see it' tab."
)
VIEW_ONLY = "You have view access to Rough profit. Ask the owner for edit access to change this."
OWNER_ONLY = "Only the owner can change who sees Rough profit."


def canonical_user_id(value) -> str:
    """Dashed lowercase UUID, whatever shape it arrived in (SQLite hands back
    32 dashless hex for a core `Uuid` column). Non-UUID values pass through
    stripped, so they can never accidentally equal a real user's id."""
    s = str(value or "").strip()
    try:
        return str(uuid.UUID(s))
    except ValueError:
        return s


def level_for(db: Session, ctx: PluginContext) -> str | None:
    if ctx.role == OWNER_ROLE:
        return OWNER
    uid = canonical_user_id(ctx.user_id)
    best: str | None = None
    rows = db.query(Access).filter(Access.deleted_at.is_(None)).all()
    for r in rows:
        hit = (r.kind == "role" and ctx.role and r.subject == ctx.role) or (
            r.kind == "user" and uid and r.subject == uid
        )
        if hit and _RANK.get(r.level, 0) > _RANK.get(best, 0):
            best = r.level
    return best


def _require(minimum: str, detail: str):
    def _dep(
        ctx: PluginContext = Depends(get_plugin_context),
        db: Session = Depends(get_plugin_db),
    ) -> tuple[PluginContext, str]:
        level = level_for(db, ctx)
        if level is None:
            raise HTTPException(status_code=403, detail=NO_ACCESS)
        if _RANK[level] < _RANK[minimum]:
            raise HTTPException(status_code=403, detail=detail)
        return ctx, level

    return _dep


require_view = _require(VIEW, NO_ACCESS)
require_edit = _require(EDIT, VIEW_ONLY)
require_owner = _require(OWNER, OWNER_ONLY)
