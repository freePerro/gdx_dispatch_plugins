"""End to end: core's cell-gateway shim relays a phone's POST into THIS plugin's
real router, and a plug_cellcomms_* row lands.

Core's gdx_dispatch/tests/test_cell_gateway.py keeps the shim's own contract
tests (secret policy, size cap, header stamping, audit, how each upstream
verdict maps to the phone's response) against a recording stub — from the core
PR that removed the in-tree plugin (#TBD). This file is the half that needs
the real plugin: it mounts the real router where the
plugin-host mounts it and drives the real shim — imported from core, which the
contract job puts on PYTHONPATH — over an in-process ASGI transport. No mock
captures: a mock proves which arguments were passed; this proves a phone's
POST becomes a row.

Run: JWT_SECRET=<any 32+ bytes> PYTHONPATH=<core>:<this plugin dir> \\
     python -m pytest gdx-plugin-cellcomms/tests
(core's auth module refuses to import without a signing key; the shim
pulls it in.)
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.tenant import get_company_id
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.plugin_api import context as plugin_context
from gdx_dispatch.routers import cell_gateway
from gdx_plugin_cellcomms.models import CellCall, CellMessage
from gdx_plugin_cellcomms.router import router as plugin_router

TENANT = "t-cell-gw"

SMS_EVENT = {
    "kind": "sms",
    "from": "+13205550134",
    "text": "hello from the phone",
    "sentStamp": "1758100000000",
}


@pytest.fixture
def stack(monkeypatch):
    """Core app (shim only) + plugin app (real cellcomms router), one DB."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for table in (Customer.__table__, CellMessage.__table__, CellCall.__table__):
        table.create(engine, checkfirst=True)
    # autoflush=False: EXACTLY core/database.py's SessionLocal config — the
    # 2026-09-18 re-audit caught autoflush=True fixtures masking a prod-only
    # IntegrityError path in the backfill.
    TS = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _db():
        db = TS()
        try:
            yield db
        finally:
            db.close()

    # The real plugin app, mounted exactly where plugin-host mounts it.
    plugin_app = FastAPI()
    plugin_app.include_router(plugin_router, prefix="/api/plugins/cellcomms")
    plugin_app.dependency_overrides[plugin_context.get_plugin_db] = _db
    # get_plugin_context stays REAL — it must parse the headers the shim sends.

    # Core app carrying only the shim.
    core_app = FastAPI()
    core_app.include_router(cell_gateway.public_router)
    core_app.dependency_overrides[get_company_id] = lambda: TENANT
    core_app.dependency_overrides[get_db] = _db

    # Point the shim's outbound client at the plugin app, in-process.
    transport = httpx.ASGITransport(app=plugin_app)
    monkeypatch.setattr(
        cell_gateway, "_client",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://plugin-host"),
    )
    monkeypatch.setattr(cell_gateway, "_plugin_host_url", lambda: "http://plugin-host")

    monkeypatch.setenv("GDX_ENV", "production")
    monkeypatch.setenv(cell_gateway.SECRET_ENV, "s3cret")
    return TestClient(core_app), TS


def _post(client, payload):
    return client.post(
        "/api/cell-gateway/webhook", json=payload,
        headers={cell_gateway.SECRET_HEADER: "s3cret"},
    )


def test_relay_lands_row_in_plugin_table_and_audits(stack):
    client, TS = stack

    r = _post(client, SMS_EVENT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["kind"] == "sms"

    db = TS()
    try:
        row = db.execute(select(CellMessage)).scalars().one()
        assert row.company_id == TENANT          # stamped by the shim's header, not the phone
        assert row.body == "hello from the phone"
        audit = db.execute(select(AuditLog)).scalars().all()
        acted = next(a for a in audit if a.action == "cell_event_received")
        assert acted.user_id == "cell-gateway"
        assert acted.entity_id == str(row.id)    # the audit row points at the stored event
        # No PII in the audit trail: kind + source only.
        assert "+1320" not in str(acted.details)
        assert "hello" not in str(acted.details)
    finally:
        db.close()

    # Redelivery (nomad retries at-least-once) → 200 duplicate, still one row.
    r2 = _post(client, SMS_EVENT)
    assert r2.status_code == 200 and r2.json()["status"] == "duplicate"
    db = TS()
    try:
        assert len(db.execute(select(CellMessage)).scalars().all()) == 1
    finally:
        db.close()


def test_bad_template_is_422_not_stored(stack):
    """A misconfigured forwarding rule on the phone gets the plugin's 422 back
    verbatim, and nothing is written."""
    client, TS = stack
    r = _post(client, {"kind": "pigeon"})
    assert r.status_code == 422
    db = TS()
    try:
        assert db.execute(select(CellMessage)).scalars().all() == []
        assert db.execute(select(CellCall)).scalars().all() == []
    finally:
        db.close()
