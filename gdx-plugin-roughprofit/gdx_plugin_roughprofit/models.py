"""Rough-profit plugin tables — namespaced plug_roughprofit_* per ADR-013.

Everything the owner types lives here; the plugin writes to no core table.
Core's `log_audit_event` is not part of plugin_api, so every change to a rule,
the owner-labor value or the access list lands in an append-only trail table
of its own: who changed it, what, and when (invariant #1, inside a plugin).

User ids are plain strings holding the canonical dashed UUID, never a
ForeignKey: plugin tables stay decoupled from core metadata, and SQLite stores
a core `Uuid` as 32 dashless hex, so any comparison happens in Python after
canonicalizing both sides.
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, Index, Integer, String, func

from gdx_dispatch.plugin_api.base import PluginBase


class Rule(PluginBase):
    """`match_text` found in a bank line's payee and/or memo → a category.
    Applied at read time; nothing is stored per transaction."""

    __tablename__ = "plug_roughprofit_rules"

    id = Column(Integer, primary_key=True)
    match_text = Column(String(200), nullable=False)
    look_in = Column(String(10), nullable=False)  # payee / memo / either
    category = Column(String(30), nullable=False)
    created_by = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_by = Column(String(64), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "uq_plug_roughprofit_rules_live",
            func.lower(match_text), look_in,
            unique=True,
            postgresql_where=deleted_at.is_(None),
            sqlite_where=deleted_at.is_(None),
        ),
    )


class RuleChange(PluginBase):
    """Append-only history of every rule add, change and removal."""

    __tablename__ = "plug_roughprofit_rule_changes"

    id = Column(Integer, primary_key=True)
    rule_id = Column(Integer, nullable=False, index=True)
    action = Column(String(10), nullable=False)  # added / changed / removed
    match_text = Column(String(200), nullable=False)
    look_in = Column(String(10), nullable=False)
    old_category = Column(String(30), nullable=True)
    new_category = Column(String(30), nullable=True)
    by_user = Column(String(64), nullable=True)
    at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class OwnerLabor(PluginBase):
    """What the owner's labor is worth per month (D5). Append-only: the value
    in force for a month is the latest row with effective_month <= that month,
    so setting a new value next year does not rewrite this year. The rows are
    their own trail."""

    __tablename__ = "plug_roughprofit_owner_labor"

    id = Column(Integer, primary_key=True)
    effective_month = Column(String(7), nullable=False, index=True)  # YYYY-MM
    amount_cents = Column(Integer, nullable=False)
    set_by = Column(String(64), nullable=True)
    set_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Access(PluginBase):
    """Who besides the owner may see (or edit) this plugin (D7). The owner
    role is hard-coded in access.py, never a row, so it cannot be removed."""

    __tablename__ = "plug_roughprofit_access"

    id = Column(Integer, primary_key=True)
    kind = Column(String(10), nullable=False)  # role / user
    subject = Column(String(64), nullable=False)  # role name, or canonical user id
    level = Column(String(10), nullable=False)  # view / edit
    granted_by = Column(String(64), nullable=True)
    granted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "uq_plug_roughprofit_access_live",
            kind, subject,
            unique=True,
            postgresql_where=deleted_at.is_(None),
            sqlite_where=deleted_at.is_(None),
        ),
    )


class AccessChange(PluginBase):
    """Append-only trail of every grant, level change and removal."""

    __tablename__ = "plug_roughprofit_access_changes"

    id = Column(Integer, primary_key=True)
    access_id = Column(Integer, nullable=False, index=True)
    action = Column(String(10), nullable=False)  # granted / changed / removed
    kind = Column(String(10), nullable=False)
    subject = Column(String(64), nullable=False)
    old_level = Column(String(10), nullable=True)
    new_level = Column(String(10), nullable=True)
    by_user = Column(String(64), nullable=True)
    at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
