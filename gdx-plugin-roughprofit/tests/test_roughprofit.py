"""gdx-plugin-roughprofit — the monthly math, the pairing, and the access gate.

Runs in this repo's `contract` workflow with core shallow-cloned onto
PYTHONPATH and core's requirement set installed (what the plugin-host image
installs).

Schema comes from core's ORM metadata, never hand-written DDL — hand-written test DDL once hid a
feature that could not be inserted at all. Rows are seeded through core's own
models, so a column the plugin's raw SQL names wrongly fails here.

SQLite always; Postgres too when ROUGHPROFIT_TEST_PG_URL points at a
THROWAWAY database (the fixture creates and drops tables in it).
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_plugin_roughprofit import compute
from gdx_plugin_roughprofit.compute import BankLine, LaborRow, RuleRow, Statement

TODAY = date(2026, 9, 20)
LIVE_SYNC = datetime(2026, 9, 19, 12, tzinfo=UTC)


def _live(*accounts):
    """synced_through for pure-compute Data: each feed reaches yesterday."""
    return {a: TODAY - timedelta(days=1) for a in accounts}
CO = "co-1"
PREFIX = "/api/plugins/roughprofit"

# Postgres is always a parameter, so a run without the URL SAYS it skipped
# (`-rs`) rather than silently omitting the cases (code audit 2026-09-26).
_BACKENDS = ["sqlite", "postgres"]


# ── schema + session ──────────────────────────────────────────────────────────


def _core_metadata():
    """Core's whole ORM schema. Every table, not just the ones the plugin
    reads: core's flush listeners (the GL guard, invoice rules) read tables
    of their own the moment an invoice is inserted."""
    import gdx_dispatch.models.tenant_models  # noqa: F401 — registers the tables
    import gdx_dispatch.modules.bank_feeds.models  # noqa: F401
    import gdx_dispatch.modules.ledger.models  # noqa: F401
    import gdx_dispatch.modules.vendor_statements.models  # noqa: F401
    from gdx_dispatch.core.audit import TenantBase

    return TenantBase.metadata


def _reset_pg(engine):
    """Empty the throwaway database. DROP SCHEMA, because drop_all cannot
    order core's estimates <-> proposal_tiers cycle."""
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


@pytest.fixture(scope="module", params=_BACKENDS)
def engine(request):
    from gdx_plugin_roughprofit import models  # noqa: F401

    from gdx_dispatch.plugin_api.base import PluginBase

    if request.param == "sqlite":
        eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                            poolclass=StaticPool)
    else:
        if not os.getenv("ROUGHPROFIT_TEST_PG_URL"):
            pytest.skip("ROUGHPROFIT_TEST_PG_URL not set — Postgres arm not run")
        eng = create_engine(os.environ["ROUGHPROFIT_TEST_PG_URL"])
        _reset_pg(eng)
    _core_metadata().create_all(eng)
    PluginBase.metadata.create_all(eng)
    yield eng
    if request.param != "sqlite":
        _reset_pg(eng)
    eng.dispose()


@pytest.fixture
def db(engine):
    from gdx_dispatch.plugin_api.base import PluginBase

    # autoflush=False: exactly core/database.py's SessionLocal config.
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.rollback()
    session.close()
    tables = list(_core_metadata().tables) + list(PluginBase.metadata.tables)
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " CASCADE"))
        else:
            for t in tables:
                conn.execute(text(f'DELETE FROM "{t}"'))


@pytest.fixture
def client(db, monkeypatch):
    import importlib

    from gdx_dispatch.plugin_api.context import get_plugin_db

    # The package re-exports the APIRouter as `router`, shadowing the module.
    router_mod = importlib.import_module("gdx_plugin_roughprofit.router")
    monkeypatch.setattr(router_mod, "_today", lambda db: TODAY)
    app = FastAPI()
    app.include_router(router_mod.router, prefix=PREFIX)
    app.dependency_overrides[get_plugin_db] = lambda: db
    return TestClient(app)


def _h(role="owner", user_id=None):
    return {"X-GDX-Tenant-Id": "t1", "X-GDX-Role": role,
            "X-GDX-User-Id": str(user_id or uuid.uuid4())}


# ── seeding through core's models ─────────────────────────────────────────────


class Seed:
    def __init__(self, db):
        from gdx_dispatch.models.tenant_models import Customer

        self.db = db
        self.customer = Customer(id=uuid.uuid4(), company_id=CO, name="Test Customer")
        db.add(self.customer)
        db.flush()
        self._conn = self._connection()
        self._n = 0
        db.commit()

    def _connection(self):
        from gdx_dispatch.modules.bank_feeds.models import BannoConnection, BannoInstitution

        inst = BannoInstitution(id=uuid.uuid4(), fi_host="bank.example", display_label="Bank")
        self.db.add(inst)
        self.db.flush()  # no ORM relationship orders these two inserts
        conn = BannoConnection(id=uuid.uuid4(), institution_id=inst.id, fi_host="bank.example",
                               banno_user_id="u1")
        self.db.add(conn)
        self.db.flush()
        return conn

    def account(self, enabled=True, inactive=False, synced=LIVE_SYNC, backfill_done=True,
                backfill_through=None, provider="banno"):
        """A live feed by default: first backfill complete and synced the day
        before TODAY."""
        from gdx_dispatch.modules.bank_feeds.models import BankFeedAccount

        a = BankFeedAccount(id=uuid.uuid4(), connection_id=self._conn.id,
                            external_account_id=str(uuid.uuid4()), sync_enabled=enabled,
                            is_inactive=inactive, last_synced_at=synced,
                            initial_backfill_done=backfill_done,
                            backfill_synced_through=backfill_through, provider=provider)
        self.db.add(a)
        self.db.commit()
        return a.id

    def line(self, account, posted, cents, payee="", memo="", pending=False, deleted=False):
        from gdx_dispatch.modules.bank_feeds.models import BankFeedTransaction

        self._n += 1
        t = BankFeedTransaction(
            id=uuid.uuid4(), account_id=account, external_transaction_id=f"x{self._n}",
            amount_cents=cents, pending=pending, posted_date=posted, payee=payee, memo=memo,
            deleted_at=datetime.now(UTC) if deleted else None,
        )
        self.db.add(t)
        self.db.commit()
        return t.id

    def invoice(self, dated, total, balance=0, status="sent", deleted=False):
        from gdx_dispatch.models.tenant_models import Invoice

        self._n += 1
        inv = Invoice(id=uuid.uuid4(), customer_id=self.customer.id, company_id=CO,
                      invoice_number=f"INV-{self._n}", public_token=uuid.uuid4().hex,
                      status=status, invoice_date=dated,
                      total=Decimal(str(total)), balance_due=Decimal(str(balance)),
                      deleted_at=datetime.now(UTC) if deleted else None)
        self.db.add(inv)
        self.db.commit()
        return inv.id

    def adjustment(self, invoice_id, kind, amount, created):
        from gdx_dispatch.models.tenant_models import InvoiceAdjustment

        self.db.add(InvoiceAdjustment(
            id=uuid.uuid4(), invoice_id=invoice_id, kind=kind, amount=Decimal(str(amount)),
            company_id=CO, created_at=datetime(created.year, created.month, created.day, 12, tzinfo=UTC),
        ))
        self.db.commit()

    def statement(self, vendor, dated, balances):
        from gdx_dispatch.modules.vendor_statements.models import VendorStatement, VendorStatementLine

        s = VendorStatement(id=uuid.uuid4(), vendor_name=vendor, statement_date=dated, parser_name="t")
        self.db.add(s)
        self.db.flush()
        for i, b in enumerate(balances):
            self.db.add(VendorStatementLine(id=uuid.uuid4(), statement_id=s.id, line_no=i,
                                            vendor_invoice_no=f"V{i}", balance=Decimal(str(b)),
                                            amount=Decimal(str(b))))
        self.db.commit()

    def user(self, role, name="Some One"):
        from gdx_dispatch.models.tenant_models import User

        u = User(id=uuid.uuid4(), company_id=CO, role=role, name=name, active=True)
        self.db.add(u)
        self.db.commit()
        return u.id


@pytest.fixture
def seed(db):
    return Seed(db)


def _line(i, acct, d, cents, payee="", memo=""):
    return BankLine(id=f"l{i}", account_id=acct, posted=d, amount_cents=cents, payee=payee, memo=memo)


# ── manifest / contract ───────────────────────────────────────────────────────


def test_manifest_loads_and_every_list_has_an_endpoint():
    import gdx_plugin_roughprofit as mod

    from gdx_dispatch.plugin_api.discovery import is_compatible

    m = mod.manifest
    assert m.key == "roughprofit"
    assert m.permissions == ()
    assert m.ui["category"] == "accounting" and m.ui["icon"] == "pi pi-chart-bar"
    titles = [s.get("title") for s in m.ui["screens"]]
    assert titles == ["Monthly", "Unsorted", "Rules", "Owner labor", "Who can see it", "Help"]
    for s in m.ui["screens"]:
        if s["type"] == "list":
            assert s["endpoint"].startswith(f"{PREFIX}/")
        if s.get("create"):
            assert s["create"]["endpoint"].startswith(f"{PREFIX}/")
    assert is_compatible(m.requires, "1.125.0")
    assert not is_compatible(m.requires, "1.124.0")


def test_every_list_endpoint_returns_a_bare_array(client, seed):
    a = seed.account()
    seed.line(a, date(2026, 8, 3), 5000, payee="CUSTOMER")
    for path in ("/monthly", "/unsorted", "/rules", "/owner-labor", "/access", "/access/subjects"):
        r = client.get(PREFIX + path, headers=_h())
        assert r.status_code == 200, (path, r.text)
        assert isinstance(r.json(), list), path


# ── step 0: coverage ──────────────────────────────────────────────────────────


def test_months_before_any_bank_data_are_absent_and_incomplete_ones_flagged(client, seed):
    a, b = seed.account(), seed.account()
    seed.line(a, date(2026, 5, 15), 1000, payee="early")
    seed.line(b, date(2026, 6, 8), 1000, payee="later account")
    seed.invoice(date(2026, 3, 2), 900)  # billing long before the bank feed
    rows = client.get(PREFIX + "/monthly", headers=_h()).json()
    assert [r["id"] for r in rows] == ["2026-09", "2026-08", "2026-07", "2026-06", "2026-05"]
    assert rows[0]["month"] == "Sep 2026"
    flags = {r["id"]: r["flags"] for r in rows}
    assert "partial bank month" in flags["2026-05"]  # no account covers all of May
    assert "1 of 2 bank accounts cover this whole month" in flags["2026-06"]
    assert "bank accounts cover" not in flags["2026-07"] and "partial" not in flags["2026-07"]
    assert "month in progress" in flags["2026-09"]


def test_linking_a_new_account_does_not_collapse_the_history():
    """Code audit 2026-09-26: with coverage at the LATEST account start, a
    second account first seen on Sep 20 hid eleven months."""
    lines = [_line(1, "a", date(2025, 10, 3), 100), _line(2, "a", date(2026, 9, 1), 100),
             _line(3, "b", date(2026, 9, 20), 100)]
    months = compute.compute(compute.Data(lines=lines, synced_through=_live("a", "b")), [], [], TODAY).months
    assert len(months) == 12
    # Every month before b's first line is flagged: the plugin cannot tell a
    # new account from a late-connected one, so it says what it knows.
    assert all("1 of 2 bank accounts cover this whole month" in m.flags for m in months[:-1])
    assert "partial bank month" in months[-1].flags  # Oct 2025: a starts on the 3rd


def test_no_bank_data_says_so_rather_than_an_empty_table(client, seed):
    rows = client.get(PREFIX + "/monthly", headers=_h()).json()
    assert len(rows) == 1 and "No bank feed transactions yet" in rows[0]["flags"]


def test_window_is_capped_at_twelve_months():
    lines = [_line(1, "a", date(2024, 1, 5), 100)]
    res = compute.compute(compute.Data(lines=lines), [], [], TODAY)
    assert len(res.months) == 12
    assert res.months[0].key == "2026-09" and res.months[-1].key == "2025-10"


# ── step 1: transfer pairing ──────────────────────────────────────────────────


def test_simple_transfer_pair_drops_out():
    ls = [_line(1, "a", TODAY, -5000, memo="Online TRANSFER to chk"),
          _line(2, "b", TODAY, 5000, memo="deposit")]
    assert compute.pair_transfers(ls) == {"l1", "l2"}


def test_two_identical_transfers_on_one_day_consume_two_counterparts():
    ls = [_line(1, "a", TODAY, -5000, memo="xfer"), _line(2, "a", TODAY, -5000, memo="xfer"),
          _line(3, "b", TODAY, 5000), _line(4, "b", TODAY, 5000), _line(5, "b", TODAY, 5000)]
    paired = compute.pair_transfers(ls)
    assert len(paired) == 4
    assert len({"l3", "l4", "l5"} - paired) == 1  # the third deposit is not a transfer


def test_one_day_drift_pairs_and_two_days_does_not():
    d = date(2026, 9, 10)
    assert compute.pair_transfers([_line(1, "a", d, -700, memo="trnsfr"),
                                   _line(2, "b", d + timedelta(days=1), 700)]) == {"l1", "l2"}
    assert compute.pair_transfers([_line(1, "a", d, -700, memo="trnsfr"),
                                   _line(2, "b", d + timedelta(days=2), 700)]) == set()


def test_opposite_amounts_without_transfer_wording_stay_unpaired_and_are_flagged():
    d = date(2026, 9, 10)
    ls = [_line(1, "a", d, -12000, payee="INSURANCE CO"), _line(2, "b", d, 12000, payee="CUSTOMER")]
    assert compute.pair_transfers(ls) == set()
    assert compute.possible_transfers(ls, set()) == {"l1", "l2"}


def test_pairing_is_a_maximum_matching_not_greedy():
    """Code audit 2026-09-26: nearest-first greedy pairs in-day-2 with
    out-day-2, stranding in-day-3 and out-day-1 (two days apart). A maximum
    matching pairs all four."""
    ls = [_line(1, "a", date(2026, 9, 2), 100, memo="transfer"),
          _line(2, "a", date(2026, 9, 3), 100, memo="transfer"),
          _line(3, "b", date(2026, 9, 2), -100, memo="transfer"),
          _line(4, "b", date(2026, 9, 1), -100, memo="transfer")]
    assert compute.pair_transfers(ls) == {"l1", "l2", "l3", "l4"}


def test_nearest_counterpart_still_wins_a_tie():
    d = date(2026, 9, 10)
    ls = [_line(1, "a", d, 100, memo="transfer"),
          _line(2, "b", d - timedelta(days=1), -100), _line(3, "b", d, -100)]
    assert compute.pair_transfers(ls) == {"l1", "l3"}


def test_today_is_the_tenants_date_not_the_containers(db, monkeypatch):
    """The plugin-host runs on UTC. At 01:30 UTC on Oct 1 it is still
    Sep 30 in Chicago, and the month must not roll over early."""
    from gdx_dispatch.models.tenant_models import AppSettings

    db.add(AppSettings(timezone="America/Chicago"))
    db.commit()

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 1, 1, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(compute, "datetime", Frozen)
    assert compute.tenant_today(db) == date(2026, 9, 30)
    db.query(AppSettings).delete()
    db.commit()
    assert compute.tenant_today(db) == date(2026, 9, 30)  # New York default, still Sep 30


def test_long_augmenting_chain_does_not_hit_the_recursion_limit():
    """Worst case for augmenting paths: every in-line's nearest out-line is
    the one the next in-line needs, so the last in flips a path through all
    earlier pairs. The recursive version raised RecursionError near 1,200."""
    n = 3000
    start = date(2020, 1, 1)
    ls = [_line(f"i{k}", "a", start + timedelta(days=k), 500, memo="transfer") for k in range(1, n + 1)]
    ls += [_line(f"o{k}", "b", start + timedelta(days=k), -500) for k in range(0, n)]
    assert len(compute.pair_transfers(ls)) == 2 * n


def test_data_starting_on_the_1st_is_not_a_partial_month():
    lines = [_line(1, "a", date(2026, 7, 1), 100), _line(2, "a", date(2026, 9, 19), 100)]
    months = {m.key: m for m in compute.compute(compute.Data(lines=lines, synced_through=_live("a")),
                                                [], [], TODAY).months}
    assert "partial bank month" not in months["2026-07"].flags
    lines[0] = _line(1, "a", date(2026, 7, 2), 100)
    months = {m.key: m for m in compute.compute(compute.Data(lines=lines, synced_through=_live("a")),
                                                [], [], TODAY).months}
    assert "partial bank month" in months["2026-07"].flags


def test_a_feed_that_stops_flags_every_later_month():
    """Code audit 2026-09-26: coverage looked only at where each account's
    data STARTS, so a stopped card feed silently raised later months' profit."""
    lines = [_line(1, "a", date(2026, 1, 2), 100), _line(2, "a", date(2026, 9, 19), 100),
             _line(3, "b", date(2026, 1, 2), -100), _line(4, "b", date(2026, 5, 20), -500)]
    months = {m.key: m for m in compute.compute(compute.Data(lines=lines), [], [], TODAY).months}
    for key in ("2026-06", "2026-07", "2026-08"):
        assert "1 of 2 bank accounts cover this whole month" in months[key].flags, key
    # September: a's last line is Sep 19, within the grace of TODAY (Sep 20).
    assert "1 of 2 bank accounts cover this whole month" in months["2026-09"].flags
    for key in ("2026-02", "2026-04"):
        assert not any("bank accounts cover" in f for f in months[key].flags), key
    assert "1 of 2 bank accounts cover this whole month" in months["2026-05"].flags  # stops May 20

    # The same account with no new lines but a recent sync is simply quiet.
    quiet = compute.Data(lines=lines, synced_through=_live("a", "b"))
    months = {m.key: m for m in compute.compute(quiet, [], [], TODAY).months}
    assert not any("bank accounts cover" in f for f in months["2026-08"].flags)


def test_last_synced_is_read_from_the_account_row(client, seed, db):
    from gdx_dispatch.modules.bank_feeds.models import BankFeedAccount

    a = seed.account()
    seed.line(a, date(2026, 7, 1), 100, payee="x")
    db.query(BankFeedAccount).filter(BankFeedAccount.id == a).update(
        {"last_synced_at": datetime(2026, 9, 19, 12, tzinfo=UTC)})
    db.commit()
    assert list(compute.load(db).synced_through.values()) == [date(2026, 9, 19)]
    flags = {r["id"]: r["flags"] for r in client.get(PREFIX + "/monthly", headers=_h()).json()}
    assert "bank accounts cover" not in flags["2026-08"] and "partial" not in flags["2026-08"]


def test_an_unfinished_first_backfill_is_not_read_as_current(client, seed, db):
    """Sixth audit pass: SimpleFIN stamps last_synced_at after every backfill
    window. A card whose first backfill broke off in March, synced 'yesterday',
    must not count as covering April onward."""
    live = seed.account()
    seed.line(live, date(2026, 1, 2), 100, payee="x")
    seed.line(live, date(2026, 9, 18), 100, payee="x")
    card = seed.account(backfill_done=False, backfill_through=date(2026, 3, 31))
    seed.line(card, date(2026, 1, 2), -9000, payee="CARD")
    seed.line(card, date(2026, 3, 20), -9000, payee="CARD")
    assert compute.load(db).synced_through[str(card)] == date(2026, 3, 31)
    flags = {r["id"]: r["flags"] for r in client.get(PREFIX + "/monthly", headers=_h()).json()}
    assert "bank accounts cover" not in flags["2026-02"]
    for key in ("2026-04", "2026-06", "2026-09"):
        assert "1 of 2 bank accounts cover this whole month" in flags[key], key


def test_a_connected_account_with_no_lines_yet_is_counted():
    """Seventh audit pass: an account linked this morning, first lines not
    in yet, was left out of the count, so every month read fully covered."""
    lines = [_line(1, "live", date(2026, 6, 2), 100), _line(2, "live", date(2026, 9, 19), 100)]
    data = compute.Data(lines=lines, synced_through={"live": TODAY - timedelta(days=1), "card": None})
    months = {m.key: m for m in compute.compute(data, [], [], TODAY).months}
    for key in ("2026-07", "2026-08", "2026-09"):
        assert "1 of 2 bank accounts cover this whole month" in months[key].flags, key


def test_prods_feed_shape_does_not_flag_every_month():
    """Prod's included accounts on 2026-09-26 (dates only, ids generic): all
    SimpleFIN, backfill fields never recorded, balances current, one account
    dormant since it was linked. Eighth audit pass: the rule then flagged
    every month, which teaches the owner to ignore the column. This proves
    the flags stay quiet on that shape — NOT that the data is complete; that
    doubt is the Monthly note (test_an_unconfirmed_feed_is_stated_once...)."""
    today = date(2026, 9, 26)
    shape = {  # account: (first line, last line, lines?)
        "a": (date(2026, 5, 15), date(2026, 9, 24)), "b": (date(2026, 5, 21), date(2026, 9, 24)),
        "c": (date(2026, 5, 21), date(2026, 9, 24)), "d": (date(2026, 6, 8), date(2026, 9, 8)),
    }
    lines = []
    for acct, (lo, hi) in shape.items():
        lines += [_line(f"{acct}1", acct, lo, 100), _line(f"{acct}2", acct, hi, -100)]
    balance = {a: today for a in ("a", "b", "c", "d", "dormant")}
    linked = {a: date(2026, 8, 13) for a in balance}
    data = compute.Data(lines=lines, synced_through=balance, linked=linked)
    flags = {m.key: m.flags for m in compute.compute(data, [], [], today).months}
    assert "partial bank month" in flags["2026-05"]
    assert "4 of 5 bank accounts cover this whole month" in flags["2026-06"]  # d starts Jun 8
    for key in ("2026-07", "2026-08", "2026-09"):
        assert not any("bank accounts cover" in f or "partial" in f for f in flags[key]), (key, flags[key])


def test_no_progress_recorded_falls_back_to_the_live_balance(client, seed, db):
    """Where core records no backfill progress (prod), `balance_as_of` is the
    reach; where it does, progress wins over a newer balance."""
    from gdx_dispatch.modules.bank_feeds.models import BankFeedAccount

    bare = seed.account(synced=None, backfill_done=False)
    mid = seed.account(synced=LIVE_SYNC, backfill_done=False, backfill_through=date(2026, 3, 31))
    db.query(BankFeedAccount).update({"balance_as_of": datetime(2026, 9, 19, 9, tzinfo=UTC)})
    db.commit()
    reach = compute.load(db).synced_through
    assert reach[str(bare)] == date(2026, 9, 19)
    assert reach[str(mid)] == date(2026, 3, 31)


def test_an_unconfirmed_feed_is_stated_once_on_monthly(client, seed, db):
    """Ninth audit pass: with no backfill progress recorded, reach comes from
    `balance_as_of`, which core writes even for accounts whose transactions
    failed. A current balance over stale lines cannot be told from a quiet
    account, so Monthly says so once, at the top."""
    from gdx_dispatch.modules.bank_feeds.models import BankFeedAccount

    live = seed.account()
    seed.line(live, date(2026, 5, 2), 100, payee="x")
    seed.line(live, date(2026, 9, 18), 100, payee="x")
    card = seed.account(synced=None, backfill_done=False)
    seed.line(card, date(2026, 5, 2), -100, payee="CARD")
    seed.line(card, date(2026, 6, 30), -100, payee="CARD")  # stopped arriving
    db.query(BankFeedAccount).filter(BankFeedAccount.id == card).update(
        {"balance_as_of": datetime(2026, 9, 19, 9, tzinfo=UTC)})
    db.commit()
    rows = client.get(PREFIX + "/monthly", headers=_h()).json()
    assert rows[0]["id"] == "feed-note"
    assert rows[0]["flags"] == "Bank feed unconfirmed for 1 of 2 accounts (see Help)"
    assert [r["id"] for r in rows[1:]][:3] == ["2026-09", "2026-08", "2026-07"]


def test_simplefin_reach_is_its_watermark_even_when_done(client, seed, db):
    """Tenth audit pass: a connection-wide SimpleFIN backfill that dies after
    one window re-stamps last_synced_at on already-done accounts. For
    SimpleFIN the watermark is the reach in both modes.

    The flag asserted here can be a FALSE ALARM (eleventh pass): a sibling's
    backfill restarts from the horizon and sets a done account's watermark
    back, though its lines are complete. Only a quiet account shows it (an
    active one's last line outruns the watermark), and it heals when the
    backfill resumes. The plugin cannot tell that from missing data."""
    card = seed.account(provider="simplefin", backfill_done=True, backfill_through=date(2025, 12, 23))
    seed.line(card, date(2025, 11, 2), -100, payee="CARD")
    live = seed.account()
    seed.line(live, date(2025, 11, 2), 100, payee="x")
    seed.line(live, date(2026, 9, 18), 100, payee="x")
    assert compute.load(db).synced_through[str(card)] == date(2025, 12, 23)
    assert compute.load(db).unverified == set()
    flags = {r["id"]: r["flags"] for r in client.get(PREFIX + "/monthly", headers=_h()).json()}
    for key in ("2026-06", "2026-09"):
        assert "1 of 2 bank accounts cover this whole month" in flags[key], key
    # A SimpleFIN account with no watermark at all is unverified, done or not.
    seed.account(provider="simplefin", backfill_done=True)
    assert len(compute.load(db).unverified) == 1


def test_confirmed_feeds_add_no_note(client, seed):
    a = seed.account()
    seed.line(a, date(2026, 9, 1), 100, payee="x")
    assert client.get(PREFIX + "/monthly", headers=_h()).json()[0]["id"] == "2026-09"


def test_memo_and_undated_invoice_dates_follow_cores_utc_convention(seed, db):
    """Core's ledger posts a credit memo at `created_at.date()` (UTC) and its
    reports anchor an undated invoice at `created_at::date`. Billed must use
    the same dates or it cannot reconcile with core's revenue report — even
    though that puts a 7:30pm-Chicago Sep 30 memo in October."""
    from gdx_dispatch.models.tenant_models import AppSettings, Invoice, InvoiceAdjustment

    db.add(AppSettings(timezone="America/Chicago"))
    late = datetime(2026, 10, 1, 0, 30, tzinfo=UTC)
    inv = seed.invoice(date(2026, 9, 10), 1000)
    db.add(InvoiceAdjustment(id=uuid.uuid4(), invoice_id=inv, kind="credit_memo", amount=Decimal("50"),
                             company_id=CO, created_at=late))
    db.add(Invoice(id=uuid.uuid4(), customer_id=seed.customer.id, company_id=CO, invoice_number="INV-ND",
                   public_token=uuid.uuid4().hex, status="sent", invoice_date=None,
                   total=Decimal("300"), balance_due=Decimal("0"), created_at=late))
    db.commit()
    data = compute.load(db)
    assert [m.created for m in data.credit_memos] == [date(2026, 10, 1)]
    assert {i.dated for i in data.invoices} == {date(2026, 9, 10), date(2026, 10, 1)}


def test_trail_times_are_shown_in_the_tenants_zone():
    from zoneinfo import ZoneInfo

    from gdx_plugin_roughprofit.router import _stamp

    chicago = ZoneInfo("America/Chicago")
    assert _stamp(datetime(2026, 9, 27, 1, 30, tzinfo=UTC), chicago) == "2026-09-26 20:30 CDT"
    assert _stamp(datetime(2026, 9, 27, 1, 30), chicago) == "2026-09-26 20:30 CDT"  # SQLite: naive UTC
    assert _stamp(None, chicago) == ""


def test_route_trail_times_carry_the_tenants_zone(client, seed, db):
    """Route-level, so dropping the zone from a list route turns this red —
    not just the helper."""
    from gdx_dispatch.models.tenant_models import AppSettings

    db.add(AppSettings(timezone="America/Chicago"))
    db.commit()
    client.post(PREFIX + "/rules", headers=_h(), json={"match_text": "x", "look_in": "payee", "category": "materials"})
    client.post(PREFIX + "/owner-labor", headers=_h(), json={"effective_month": "2026-09", "amount": 1})
    client.post(PREFIX + "/access", headers=_h(), json={"subject": "role:admin", "level": "view"})
    zoned = re.compile(r" C[DS]T$")
    assert zoned.search(client.get(PREFIX + "/rules", headers=_h()).json()[0]["changed_at"])
    assert zoned.search(client.get(PREFIX + "/owner-labor", headers=_h()).json()[1]["set_at"])
    assert zoned.search(client.get(PREFIX + "/access", headers=_h()).json()[1]["granted_at"])


def test_router_today_is_wired_to_the_tenant_zone(db, monkeypatch):
    import importlib

    from gdx_dispatch.models.tenant_models import AppSettings

    router_mod = importlib.import_module("gdx_plugin_roughprofit.router")
    db.add(AppSettings(timezone="America/Chicago"))
    db.commit()

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 1, 1, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(compute, "datetime", Frozen)
    assert router_mod._today(db) == date(2026, 9, 30)


def test_same_account_never_pairs():
    ls = [_line(1, "a", TODAY, -500, memo="transfer"), _line(2, "a", TODAY, 500)]
    assert compute.pair_transfers(ls) == set()


def test_worded_line_without_counterpart_is_left_to_the_rules(client, seed):
    """A card-processor payout carries transfer wording in its memo and is
    revenue: with no counterpart on our accounts it must not drop."""
    a = seed.account()
    seed.line(a, date(2026, 9, 2), 25000, payee="PROCESSOR", memo="payout transfer")
    rows = client.get(PREFIX + "/unsorted", headers=_h()).json()
    assert [r["description"] for r in rows] == ["PROCESSOR"]
    client.post(PREFIX + "/rules", headers=_h(),
                json={"match_text": "processor", "look_in": "payee", "category": "revenue"})
    sep = client.get(PREFIX + "/monthly", headers=_h()).json()[0]
    assert sep["deposited"] == "$250" and sep["unsorted"] == "$0"


def test_lines_on_disabled_or_inactive_accounts_are_ignored_entirely(client, seed):
    live, off, gone = seed.account(), seed.account(enabled=False), seed.account(inactive=True)
    seed.line(live, date(2026, 9, 1), 10000, payee="CUSTOMER")
    seed.line(off, date(2026, 9, 1), -10000, memo="transfer")  # would pair if included
    seed.line(gone, date(2025, 1, 1), 99900, payee="OLD")  # would widen coverage if included
    seed.line(live, date(2026, 9, 3), 777, payee="PENDING", pending=True)
    seed.line(live, date(2026, 9, 3), 888, payee="DELETED", deleted=True)
    rows = client.get(PREFIX + "/monthly", headers=_h()).json()
    assert [r["id"] for r in rows] == ["2026-09"]
    assert rows[0]["deposited"] == "$100"


def test_pairing_falsifier_no_pair_lacks_wording_on_both_sides():
    """Plan §8: every paired line or its partner carries transfer wording.
    Checked over a mixed set by construction."""
    d = date(2026, 9, 10)
    ls = [_line(1, "a", d, -100, memo="transfer"), _line(2, "b", d, 100),
          _line(3, "a", d, -200, payee="RENT"), _line(4, "b", d, 200, payee="CUSTOMER"),
          _line(5, "b", d, -300, payee="XFER out"), _line(6, "a", d + timedelta(days=1), 300)]
    paired = compute.pair_transfers(ls)
    by_id = {ln.id: ln for ln in ls}
    assert paired == {"l1", "l2", "l5", "l6"}
    unworded = [i for i in paired if not by_id[i].worded]
    assert set(unworded) == {"l2", "l6"}  # each has a worded partner


# ── step 2: rules ─────────────────────────────────────────────────────────────


def test_longest_match_wins_and_ties_go_to_the_most_recent():
    ln = _line(1, "a", TODAY, -4000, payee="ACME FUEL STOP 123")
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    rules = [RuleRow(1, "acme", "payee", "materials", t0),
             RuleRow(2, "acme fuel", "payee", "vehicle", t0)]
    assert compute.classify(ln, rules).category == "vehicle"
    rules = [RuleRow(1, "acme", "payee", "materials", t0),
             RuleRow(2, "ACME", "either", "overhead", t0 + timedelta(days=1))]
    assert compute.classify(ln, rules).category == "overhead"


def test_look_in_limits_where_the_text_is_searched():
    ln = _line(1, "a", TODAY, -4000, payee="SHOP", memo="ref gas")
    assert compute.classify(ln, [RuleRow(1, "gas", "payee", "vehicle")]) is None
    assert compute.classify(ln, [RuleRow(1, "gas", "memo", "vehicle")]).category == "vehicle"
    assert compute.classify(ln, [RuleRow(1, "gas", "either", "vehicle")]).category == "vehicle"


def test_rule_add_change_remove_each_write_exactly_one_trail_row(client, seed, db):
    from gdx_plugin_roughprofit.models import Rule, RuleChange

    a = seed.account()
    seed.line(a, date(2026, 9, 4), -3000, payee="PARTS HOUSE")
    uid = uuid.uuid4()
    h = _h(user_id=uid)

    def post(cat, text="Parts House"):
        return client.post(PREFIX + "/rules", headers=h,
                           json={"match_text": text, "look_in": "payee", "category": cat})

    assert post("materials").json()["status"] == "added"
    assert db.query(RuleChange).count() == 1
    assert post("materials", "parts   HOUSE").json()["status"] == "unchanged"  # same rule, case/space
    assert db.query(RuleChange).count() == 1
    assert post("overhead").json()["status"] == "changed"
    assert db.query(RuleChange).count() == 2
    rules = client.get(PREFIX + "/rules", headers=h).json()
    assert rules[0]["category"] == "Overhead" and rules[0]["lines"] == 1
    assert post("remove").json()["status"] == "removed"
    assert db.query(RuleChange).count() == 3
    assert [c.action for c in db.query(RuleChange).order_by(RuleChange.id)] == ["added", "changed", "removed"]
    assert all(c.by_user == str(uid) for c in db.query(RuleChange))
    # A removed rule stops applying: the line is unsorted again.
    assert client.get(PREFIX + "/rules", headers=h).json() == []
    assert [r["description"] for r in client.get(PREFIX + "/unsorted", headers=h).json()] == ["PARTS HOUSE"]
    assert db.query(Rule).filter(Rule.deleted_at.isnot(None)).count() == 1  # soft, not hard
    # Re-adding after removal is a fresh add, not blocked by the partial unique index.
    assert post("materials").json()["status"] == "added"


def test_refusals_are_one_readable_sentence(client, seed):
    """The host turns a refused create into an error page and shows `detail`
    raw, so every refusal must be a plain string, never Pydantic's list."""
    bad = [
        ("/rules", {"match_text": "x", "look_in": "payee", "category": "nonsense"}),
        ("/rules", {"match_text": "x", "look_in": "everywhere", "category": "materials"}),
        ("/rules", {"match_text": "   ", "look_in": "payee", "category": "materials"}),
        ("/rules", {"match_text": "x", "look_in": None, "category": None}),  # cleared selects
        ("/rules", {}),
        ("/owner-labor", {"effective_month": "Sept 2026x", "amount": 5}),
        ("/owner-labor", {"effective_month": "2026-09", "amount": None}),
        ("/access", {"subject": None, "level": "view"}),
        ("/access", {"subject": "role:admin", "level": None}),
    ]
    for path, body in bad:
        r = client.post(PREFIX + path, headers=_h(), json=body)
        assert r.status_code == 422, (path, body)
        assert isinstance(r.json()["detail"], str) and r.json()["detail"].endswith("."), (path, body, r.json())
    # A cleared "Look in" select falls back to either, rather than refusing.
    r = client.post(PREFIX + "/rules", headers=_h(), json={"match_text": "x", "look_in": None, "category": "materials"})
    assert r.json()["status"] == "added"


def test_month_is_read_the_way_people_type_it():
    from gdx_plugin_roughprofit.router import parse_month

    for raw in ("2026-09", "2026-9", "9/2026", "09/2026", "Sep 2026", "sept 2026", "September, 2026", " 2026/09 "):
        assert parse_month(raw) == "2026-09", raw
    for raw in ("2026-13", "13/2026", "Sepp 2026", "", None, "2026", "1899-01"):
        assert parse_month(raw) is None, raw


def test_rule_remove_of_a_missing_rule(client, seed):
    r = client.post(PREFIX + "/rules", headers=_h(),
                    json={"match_text": "never", "look_in": "payee", "category": "remove"})
    assert r.status_code == 404


# ── billing ───────────────────────────────────────────────────────────────────


def test_credit_memo_reduces_billed_in_its_own_month_and_credit_applied_does_not(client, seed):
    a = seed.account()
    seed.line(a, date(2026, 7, 1), 100, payee="x")
    inv = seed.invoice(date(2026, 7, 10), 1000, balance=400)
    seed.invoice(date(2026, 8, 5), 2000, balance=0)
    seed.adjustment(inv, "credit_memo", 150, date(2026, 8, 20))
    seed.adjustment(inv, "credit_applied", 300, date(2026, 8, 21))
    seed.adjustment(inv, "refund", 50, date(2026, 8, 22))
    rows = {r["id"]: r for r in client.get(PREFIX + "/monthly", headers=_h()).json()}
    assert rows["2026-07"]["billed"] == "$1,000"
    assert rows["2026-07"]["still_unpaid"] == "$400"
    assert rows["2026-08"]["billed"] == "$1,850"


def test_void_draft_and_deleted_invoices_are_not_billed(client, seed):
    a = seed.account()
    seed.line(a, date(2026, 9, 1), 100, payee="x")
    seed.invoice(date(2026, 9, 2), 500)
    seed.invoice(date(2026, 9, 2), 700, status="void")
    v = seed.invoice(date(2026, 9, 2), 800, status="draft")
    seed.invoice(date(2026, 9, 2), 900, deleted=True)
    seed.adjustment(v, "credit_memo", 100, date(2026, 9, 3))  # on an excluded invoice
    assert client.get(PREFIX + "/monthly", headers=_h()).json()[0]["billed"] == "$500"


# ── supplier Δ ────────────────────────────────────────────────────────────────


def test_supplier_owed_blank_when_missing_or_stale():
    s = [Statement("acme", date(2026, 7, 31), 10000)]
    assert compute.supplier_owed(s, date(2026, 6, 30)) is None  # none yet
    assert compute.supplier_owed(s, date(2026, 8, 31)) == 10000  # 31 days: fresh
    assert compute.supplier_owed(s, date(2026, 9, 30)) is None  # 61 days: stale
    # A settled supplier with no later statement is known to be zero.
    settled = [Statement("old", date(2026, 3, 31), 0)]
    assert compute.supplier_owed(settled, date(2026, 9, 30)) == 0
    # ...but a zero followed by a later statement means one went missing.
    gap = settled + [Statement("old", date(2026, 10, 31), 500)]
    assert compute.supplier_owed(gap, date(2026, 9, 30)) is None


def test_stale_statement_blanks_delta_flags_the_month_and_drops_the_term(client, seed):
    a = seed.account()
    seed.line(a, date(2026, 7, 1), 100000, payee="CUSTOMER")
    seed.statement("Supplier A", date(2026, 6, 30), [100, 200])
    seed.statement("Supplier A", date(2026, 7, 31), [500])
    seed.invoice(date(2026, 7, 5), 2000)
    seed.invoice(date(2026, 8, 5), 2000)
    rows = {r["id"]: r for r in client.get(PREFIX + "/monthly", headers=_h()).json()}
    jul, aug, sep = rows["2026-07"], rows["2026-08"], rows["2026-09"]
    assert jul["supplier_delta"] == "$200"  # 300 → 500
    assert jul["profit_billed"] == "$1,800"  # 2000 − 0 costs − 200
    assert aug["supplier_delta"] == "$0"  # Jul 31 is 31 days before Aug 31
    assert sep["supplier_delta"] == "—" and "supplier balance unknown" in sep["flags"]
    assert sep["profit_billed"] == "$0"  # no invoices, no costs, Δ term dropped


def test_money_and_month_formatting():
    from gdx_plugin_roughprofit.router import money, month_label

    assert money(None) == "—" and money(0) == "$0" and money(123_456_78) == "$123,457"
    assert money(-1500) == "\u2212\u2060$15"  # the minus is joined to the amount
    assert month_label("2026-09") == "Sep 2026"


# ── the monthly math, hand-computed ───────────────────────────────────────────


def test_every_monthly_column_against_a_hand_computed_month():
    d = date(2026, 8, 10)
    lines = [
        _line(1, "a", date(2026, 7, 1), 1),  # coverage starts in July
        _line(2, "a", d, 1_000_00, payee="CUSTOMER PAY"),  # revenue
        _line(3, "a", d, 200_00, payee="MYSTERY IN"),  # unsorted in
        _line(4, "a", d, -300_00, payee="PARTS CO"),  # materials
        _line(5, "a", d, 20_00, payee="PARTS CO"),  # supplier refund → -cost
        _line(6, "a", d, -50_00, payee="MYSTERY OUT"),  # unsorted out
        _line(7, "a", d, -400_00, payee="OWNER DRAW"),  # owner pay
        _line(8, "a", d, -100_00, payee="LOAN SVC"),  # loan
        _line(9, "a", d, -60_00, payee="TOOL DEPOT"),  # equipment
        _line(10, "a", d, -70_00, payee="SKIP ME"),  # ignore
        _line(11, "a", d, -500_00, memo="transfer out"),  # pairs with 12
        _line(12, "b", d, 500_00, memo="from savings"),
        _line(13, "b", date(2026, 7, 2), 1),
    ]
    rules = [RuleRow(1, "customer", "payee", "revenue"), RuleRow(2, "parts co", "payee", "materials"),
             RuleRow(3, "owner draw", "payee", "owner_pay"), RuleRow(4, "loan", "payee", "loan"),
             RuleRow(5, "tool depot", "payee", "equipment"), RuleRow(6, "skip", "payee", "ignore")]
    data = compute.Data(
        lines=lines,
        synced_through=_live("a", "b"),
        invoices=[compute.InvoiceRow("i1", date(2026, 8, 3), 900_00, 100_00)],
        statements=[Statement("s", date(2026, 7, 31), 100_00), Statement("s", date(2026, 8, 31), 130_00)],
    )
    labor = [LaborRow("2026-01", 150_00, id=1), LaborRow("2026-08", 250_00, id=2),
             LaborRow("2026-10", 999_00, id=3)]
    aug = {m.key: m for m in compute.compute(data, rules, labor, TODAY).months}["2026-08"]

    assert aug.deposited == 1_000_00 + 200_00
    assert aug.costs == 300_00 - 20_00 + 50_00
    assert aug.billed == 900_00 and aug.still_unpaid == 100_00
    assert aug.supplier_delta == 30_00
    assert aug.profit_deposits == 1_200_00 - 330_00
    assert aug.profit_billed == 900_00 - 330_00 - 30_00
    assert aug.owner_labor == 250_00
    assert aug.true_profit == 870_00 - 250_00
    assert aug.owner_pay == 400_00
    assert aug.loan + aug.equipment == 160_00
    assert aug.left_in_business == 870_00 - 400_00 - 100_00 - 60_00
    assert aug.unsorted_abs == 200_00 + 50_00
    assert aug.flags == []  # 900 billed ≥ 50% of 1,200 deposited


def test_incomplete_billing_flag():
    lines = [_line(1, "a", date(2026, 9, 1), 1000_00)]
    none = compute.compute(compute.Data(lines=lines), [], [], TODAY).months[0]
    assert "billing looks incomplete" in none.flags  # zero invoices
    low = compute.compute(compute.Data(lines=lines, invoices=[
        compute.InvoiceRow("i", date(2026, 9, 2), 400_00, 0)]), [], [], TODAY).months[0]
    assert "billing looks incomplete" in low.flags  # 400 < 50% of 1,000
    ok = compute.compute(compute.Data(lines=lines, invoices=[
        compute.InvoiceRow("i", date(2026, 9, 2), 500_00, 0)]), [], [], TODAY).months[0]
    assert "billing looks incomplete" not in ok.flags


def test_query_falsifier_included_lines_sum_to_the_net_change(client, seed, db):
    """Plan §8: Σ of every included line equals Deposited − Costs − the
    below-profit lines, for any rule set — pairing drops +x and −x together,
    so this proves only the query and the bucketing."""
    a, b = seed.account(), seed.account()
    amounts = [(a, 5000, "CUST"), (a, -1200, "PARTS"), (b, 3000, "CUST"), (a, -800, "DRAW"),
               (a, -2500, "transfer"), (b, 2500, "x"), (b, -100, "IGNORED")]
    for acct, cents, payee in amounts:
        seed.line(acct, date(2026, 9, 5), cents, payee=payee)
    for text_, cat in (("cust", "revenue"), ("parts", "materials"), ("draw", "owner_pay"),
                       ("ignored", "ignore")):
        client.post(PREFIX + "/rules", headers=_h(), json={"match_text": text_, "look_in": "payee",
                                                           "category": cat})
    from gdx_plugin_roughprofit.router import _result

    mo = _result(db).months[0]
    ignored = -100
    net = sum(c for _, c, _ in amounts)
    assert mo.deposited - mo.costs - mo.owner_pay - mo.loan - mo.equipment == net - ignored


def test_owner_labor_effective_month_lookup():
    rows = [LaborRow("2026-01", 100, id=1), LaborRow("2026-06", 200, id=2),
            LaborRow("2026-06", 300, datetime(2026, 6, 2, tzinfo=UTC), id=3)]
    assert compute.labor_for("2025-12", rows) == 0
    assert compute.labor_for("2026-05", rows) == 100
    assert compute.labor_for("2026-06", rows) == 300  # latest set wins within a month
    assert compute.labor_for("2027-01", rows) == 300


def test_owner_labor_route_is_append_only_and_validated(client, seed, db):
    from gdx_plugin_roughprofit.models import OwnerLabor

    h = _h()
    assert client.post(PREFIX + "/owner-labor", headers=h,
                       json={"effective_month": "2026-13", "amount": 5}).status_code == 422
    assert client.post(PREFIX + "/owner-labor", headers=h,
                       json={"effective_month": "2026-01", "amount": -5}).status_code == 422
    client.post(PREFIX + "/owner-labor", headers=h, json={"effective_month": "2026-01", "amount": 4000})
    client.post(PREFIX + "/owner-labor", headers=h, json={"effective_month": "2026-09", "amount": 5000})
    assert db.query(OwnerLabor).count() == 2
    rows = client.get(PREFIX + "/owner-labor", headers=h).json()
    assert rows[0]["amount"] == "$5,000" and rows[0]["effective_month"] == "In force this month"


# ── access gate ───────────────────────────────────────────────────────────────

_READS = ("/monthly", "/unsorted", "/rules", "/owner-labor", "/access", "/access/subjects")
_RULE = {"match_text": "abc", "look_in": "payee", "category": "materials"}
_LABOR = {"effective_month": "2026-09", "amount": 1}


def test_empty_list_admin_is_refused_everywhere_and_owner_is_let_in(client, seed):
    a = seed.account()
    seed.line(a, date(2026, 9, 1), 100, payee="x")
    admin = _h("admin")
    for path in _READS:
        assert client.get(PREFIX + path, headers=admin).status_code == 403, path
    assert client.post(PREFIX + "/rules", headers=admin, json=_RULE).status_code == 403
    assert client.post(PREFIX + "/owner-labor", headers=admin, json=_LABOR).status_code == 403
    assert client.post(PREFIX + "/access", headers=admin,
                       json={"subject": "role:admin", "level": "view"}).status_code == 403
    r = client.get(PREFIX + "/monthly", headers=admin)
    assert "owner-only" in r.json()["detail"]
    for path in _READS:
        assert client.get(PREFIX + path, headers=_h()).status_code == 200, path


def test_forged_headers_without_a_tenant_are_refused(client):
    assert client.get(PREFIX + "/monthly", headers={"X-GDX-Role": "owner"}).status_code == 400


@pytest.mark.parametrize("by", ["role", "user"])
def test_view_grant_opens_reads_but_not_writes(client, seed, by):
    uid = seed.user("office", "Office Person")
    subject = "role:office" if by == "role" else f"user:{uid}"
    assert client.post(PREFIX + "/access", headers=_h(),
                       json={"subject": subject, "level": "view"}).json()["status"] == "granted"
    h = _h("office", uid)
    for path in _READS:
        assert client.get(PREFIX + path, headers=h).status_code == 200, path
    # The access list answers a non-owner with an explanation, not the grants.
    assert client.get(PREFIX + "/access", headers=h).json()[0]["id"] == "owner-only"
    assert client.get(PREFIX + "/access/subjects", headers=h).json() == []
    r = client.post(PREFIX + "/rules", headers=h, json=_RULE)
    assert r.status_code == 403 and "view access" in r.json()["detail"]
    assert client.post(PREFIX + "/owner-labor", headers=h, json=_LABOR).status_code == 403


def test_user_grant_matches_a_dashless_user_id(client, seed):
    """SQLite stores core ids as 32 dashless hex; the gate canonicalizes."""
    uid = seed.user("tech")
    client.post(PREFIX + "/access", headers=_h(), json={"subject": f"user:{uid.hex}", "level": "view"})
    assert client.get(PREFIX + "/monthly", headers=_h("tech", uid)).status_code == 200
    assert client.get(PREFIX + "/monthly", headers=_h("tech")).status_code == 403  # another tech


def test_edit_grant_opens_writes_but_never_the_access_list(client, seed):
    client.post(PREFIX + "/access", headers=_h(), json={"subject": "role:admin", "level": "edit"})
    h = _h("admin")
    assert client.post(PREFIX + "/rules", headers=h, json=_RULE).status_code == 200
    assert client.post(PREFIX + "/owner-labor", headers=h, json=_LABOR).status_code == 200
    r = client.post(PREFIX + "/access", headers=h, json={"subject": "role:tech", "level": "view"})
    assert r.status_code == 403 and "Only the owner" in r.json()["detail"]


def test_higher_level_wins_across_role_and_user_grants(client, seed):
    uid = seed.user("office")
    client.post(PREFIX + "/access", headers=_h(), json={"subject": "role:office", "level": "view"})
    client.post(PREFIX + "/access", headers=_h(), json={"subject": f"user:{uid}", "level": "edit"})
    assert client.post(PREFIX + "/rules", headers=_h("office", uid), json=_RULE).status_code == 200
    assert client.post(PREFIX + "/rules", headers=_h("office"), json=_RULE).status_code == 403


def test_removed_grant_is_refused_on_the_next_request(client, seed):
    client.post(PREFIX + "/access", headers=_h(), json={"subject": "role:admin", "level": "view"})
    assert client.get(PREFIX + "/monthly", headers=_h("admin")).status_code == 200
    client.post(PREFIX + "/access", headers=_h(), json={"subject": "role:admin", "level": "remove"})
    assert client.get(PREFIX + "/monthly", headers=_h("admin")).status_code == 403


def test_owner_cannot_be_removed_or_changed(client, seed):
    for level in ("remove", "view"):
        r = client.post(PREFIX + "/access", headers=_h(), json={"subject": "role:owner", "level": level})
        assert r.status_code == 422
    assert client.get(PREFIX + "/monthly", headers=_h()).status_code == 200


def test_every_access_change_writes_exactly_one_trail_row(client, seed, db):
    from gdx_plugin_roughprofit.models import AccessChange

    owner = uuid.uuid4()
    h = _h(user_id=owner)
    steps = [("view", "granted"), ("view", "unchanged"), ("edit", "changed"), ("remove", "removed")]
    expected = 0
    for level, status in steps:
        r = client.post(PREFIX + "/access", headers=h, json={"subject": "role:admin", "level": level})
        assert r.json()["status"] == status
        expected += status != "unchanged"
        assert db.query(AccessChange).count() == expected
    trail = db.query(AccessChange).order_by(AccessChange.id).all()
    assert [(c.action, c.old_level, c.new_level) for c in trail] == [
        ("granted", None, "view"), ("changed", "view", "edit"), ("removed", "edit", None)]
    assert all(c.by_user == str(owner) for c in trail)


def test_grant_to_an_unknown_user_is_refused(client, seed):
    r = client.post(PREFIX + "/access", headers=_h(),
                    json={"subject": f"user:{uuid.uuid4()}", "level": "view"})
    assert r.status_code == 422


def test_subjects_list_roles_and_active_users_but_not_the_owner(client, seed):
    seed.user("owner", "The Owner")
    tech = seed.user("tech", "Tech One")
    opts = client.get(PREFIX + "/access/subjects", headers=_h()).json()
    values = [o["value"] for o in opts]
    assert "role:tech" in values and f"user:{tech}" in values
    assert "role:owner" not in values
    assert not any("The Owner" in o["label"] for o in opts)
