"""The rough-profit computation (plan §5). Read-only.

`load()` is the only function that touches the database: raw `text()` reads of
core tables, never a core model import (ADR-013: importing core internals makes
them public API). Everything after it is pure, over plain rows, so each step
is unit-testable on its own.

Portability rules, both from the SQLite dashless-UUID trap:
- no `WHERE id = :uuid` in SQL; every id is canonicalized in Python;
- no date comparison in SQL (SQLite hands dates back as strings); rows are
  filtered in Python. The tables are small (hundreds of rows a year).

Money is integer cents throughout; formatting happens only in the router.
"""
from __future__ import annotations

import calendar
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# ── categories ────────────────────────────────────────────────────────────────

REVENUE = "revenue"
TRANSFER = "transfer"
OWNER_PAY = "owner_pay"
LOAN = "loan"
EQUIPMENT = "equipment"
IGNORE = "ignore"

COST_CATEGORIES = {
    "materials": "Materials",
    "payroll": "Payroll",
    "vehicle": "Vehicle & fuel",
    "insurance": "Insurance",
    "marketing": "Marketing",
    "overhead": "Overhead",
    "card_payment": "Credit card payment",
    "other_cost": "Other cost",
}

CATEGORY_LABELS = {
    REVENUE: "Revenue",
    **COST_CATEGORIES,
    TRANSFER: "Transfer (between our accounts)",
    OWNER_PAY: "Owner pay (draw)",
    LOAN: "Loan payment",
    EQUIPMENT: "Equipment purchase",
    IGNORE: "Ignore",
}

LOOK_IN = ("payee", "memo", "either")

# Case-insensitive; at least one side of a pair must carry one of these.
TRANSFER_WORDS = ("transfer", "trnsfr", "xfer")

# A supplier statement older than this at a month end is "unknown", not "no
# change" (plan §5, audit finding 3).
STALE_STATEMENT_DAYS = 35
# Billed below this share of Deposited flags the month (D6). A guess (§12).
INCOMPLETE_BILLING_RATIO = Decimal("0.5")
MAX_MONTHS = 12
# How stale a feed may be at a month's end before that month is flagged.
SYNC_GRACE_DAYS = 3
# How long a just-linked account with no lines yet is treated as downloading.
NEW_ACCOUNT_DAYS = 7


# ── plain rows ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BankLine:
    id: str
    account_id: str
    posted: date
    amount_cents: int  # positive = money in
    payee: str = ""
    memo: str = ""
    merchant: str = ""

    @property
    def payee_text(self) -> str:
        return f"{self.payee} {self.merchant}".lower()

    @property
    def memo_text(self) -> str:
        return self.memo.lower()

    @property
    def worded(self) -> bool:
        blob = f"{self.payee_text} {self.memo_text}"
        return any(w in blob for w in TRANSFER_WORDS)

    @property
    def label(self) -> str:
        return (self.payee or self.merchant or self.memo or "(no description)").strip()


@dataclass(frozen=True)
class InvoiceRow:
    id: str
    dated: date
    total_cents: int
    balance_cents: int


@dataclass(frozen=True)
class CreditMemo:
    invoice_id: str
    created: date
    amount_cents: int


@dataclass(frozen=True)
class Statement:
    vendor: str
    dated: date
    balance_cents: int


@dataclass(frozen=True)
class RuleRow:
    id: int
    match_text: str
    look_in: str
    category: str
    updated_at: datetime | None = None


@dataclass(frozen=True)
class LaborRow:
    effective_month: str  # YYYY-MM
    amount_cents: int
    set_at: datetime | None = None
    id: int = 0


@dataclass
class Data:
    lines: list[BankLine] = field(default_factory=list)
    invoices: list[InvoiceRow] = field(default_factory=list)
    credit_memos: list[CreditMemo] = field(default_factory=list)
    statements: list[Statement] = field(default_factory=list)
    # Included account id -> the date its feed is known to reach (None: not
    # known). With the last line, this says how far each feed reaches.
    synced_through: dict[str, date | None] = field(default_factory=dict)
    # Included account id -> the day it was linked (None: not known).
    linked: dict[str, date | None] = field(default_factory=dict)
    # Included accounts whose reach is only `balance_as_of`: core has not
    # confirmed their transactions are complete.
    unverified: set[str] = field(default_factory=set)


# ── conversions ───────────────────────────────────────────────────────────────


def to_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def to_cents(v) -> int:
    if v is None:
        return 0
    return int((Decimal(str(v)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def canon_id(v) -> str:
    s = str(v or "").strip()
    try:
        return str(uuid.UUID(s))
    except ValueError:
        return s


def tenant_zone(db: Session) -> ZoneInfo:
    """The tenant's timezone (`app_settings.timezone`), for what the OWNER
    sees: which month is "now", and the trail times on screen. The
    plugin-host runs on UTC, so from early evening on a month's last day its
    own date is already next month.

    Data dates follow core instead, so Billed reconciles with core's reports:
    bank lines carry `posted_date` (core already dated it in this zone), and
    credit memos and undated invoices take their UTC `created_at` date, which
    is what the ledger (`modules/ledger/rules.py` credit_memo posting) and
    `routers/reports.py` use.

    Mirrors core's `modules/bank_feeds/service.py::tenant_zoneinfo`. Raw SQL
    rather than importing core (ADR-013)."""
    name = "America/New_York"  # the column's model default, as in core
    try:
        row = db.execute(text("SELECT timezone FROM app_settings")).first()
        if row and row[0]:
            name = str(row[0])
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("roughprofit_tenant_timezone_read_failed — falling back to %s", name)
    try:
        zone = ZoneInfo(name)
    except Exception:  # noqa: BLE001
        log.warning("roughprofit_tenant_timezone_invalid tz=%r — falling back to America/New_York", name)
        zone = ZoneInfo("America/New_York")
    return zone


def tenant_today(db: Session) -> date:
    return datetime.now(tenant_zone(db)).date()


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "t", "true", "yes")
    return bool(v)


# ── the one database read ─────────────────────────────────────────────────────


def load(db: Session) -> Data:
    accounts = db.execute(
        text("SELECT id, provider, sync_enabled, is_inactive, last_synced_at, initial_backfill_done, "
             "backfill_synced_through, balance_as_of, created_at FROM bank_feed_accounts")
    ).all()
    # "Our" accounts. Lines on any other account are excluded entirely — from
    # the totals AND from pairing (prod has disabled legacy accounts holding
    # one-off duplicates of the live feed).
    # How far each feed's data reaches — the best signal core keeps:
    # - backfill done: `last_synced_at`;
    # - backfill in progress: `backfill_synced_through`. SimpleFIN stamps
    #   `last_synced_at` after every window, reached today or not, so a
    #   backfill that broke off would read as current (sixth audit pass);
    # - no progress recorded at all: `balance_as_of`. Prod's SimpleFIN rows
    #   never advance their watermarks (all NULL on 2026-09-26, feeds live),
    #   and without this every month there read as a stopped feed (eighth
    #   pass). A live balance shows the connection works, not that every
    #   line arrived — the weakest of the three, used only when it is all.
    def reach(a) -> date | None:
        # SimpleFIN advances `backfill_synced_through` on EVERY successful
        # window, incremental runs included (simplefin_service.py, the
        # watermark loop), and re-stamps `last_synced_at` on already-done
        # accounts when a sibling restarts a connection-wide backfill — so for
        # SimpleFIN the watermark is the reach in both modes (tenth pass).
        if (a.provider or "") == "simplefin" and a.backfill_synced_through is not None:
            return to_date(a.backfill_synced_through)
        if _truthy(a.initial_backfill_done):
            return to_date(a.last_synced_at)
        if a.backfill_synced_through is not None:
            return to_date(a.backfill_synced_through)
        return to_date(a.balance_as_of)

    live = [a for a in accounts if _truthy(a.sync_enabled) and not _truthy(a.is_inactive)]
    synced_through = {canon_id(a.id): reach(a) for a in live}
    linked = {canon_id(a.id): to_date(a.created_at) for a in live}
    # Ninth pass: on prod the watermarks stay empty because every sync run
    # ends in error, and `balance_as_of` is written even for accounts that
    # failed. So a reach taken from it is unverified — said once, on the
    # Monthly screen, rather than trusted silently.
    unverified = {
        canon_id(a.id) for a in live
        if a.backfill_synced_through is None
        and ((a.provider or "") == "simplefin" or not _truthy(a.initial_backfill_done))
    }
    included = set(synced_through)

    lines = []
    for r in db.execute(text(
        "SELECT id, account_id, amount_cents, pending, posted_date, payee, memo, merchant_name "
        "FROM bank_feed_transactions WHERE deleted_at IS NULL"
    )):
        acct = canon_id(r.account_id)
        posted = to_date(r.posted_date)
        if acct not in included or _truthy(r.pending) or posted is None or r.amount_cents is None:
            continue
        lines.append(BankLine(
            id=canon_id(r.id), account_id=acct, posted=posted, amount_cents=int(r.amount_cents),
            payee=r.payee or "", memo=r.memo or "", merchant=r.merchant_name or "",
        ))

    invoices = []
    for r in db.execute(text(
        "SELECT id, status, invoice_date, created_at, total, balance_due "
        "FROM invoices WHERE deleted_at IS NULL"
    )):
        if (r.status or "") in ("void", "draft"):
            continue
        # Core's anchor: coalesce(invoice_date, created_at::date), UTC.
        dated = to_date(r.invoice_date) or to_date(r.created_at)
        if dated is None:
            continue
        invoices.append(InvoiceRow(
            id=canon_id(r.id), dated=dated,
            total_cents=to_cents(r.total), balance_cents=to_cents(r.balance_due),
        ))

    # Only credit memos lower billing. `credit_applied` spends a credit the
    # customer already holds (routers/invoices.py, apply-credit route checks
    # `customer_credit_balance_cents` first) — it is not new revenue, so it is
    # not subtracted. Memos on excluded invoices (void/draft/deleted) are
    # dropped with their invoice, or the memo would be subtracted twice.
    live = {i.id for i in invoices}
    memos = []
    for r in db.execute(text(
        "SELECT invoice_id, amount, created_at FROM invoice_adjustments "
        "WHERE kind = 'credit_memo'"
    )):
        inv = canon_id(r.invoice_id)
        created = to_date(r.created_at)  # UTC date, as the ledger posts it
        if inv in live and created is not None:
            memos.append(CreditMemo(invoice_id=inv, created=created, amount_cents=to_cents(r.amount)))

    statements = []
    for r in db.execute(text(
        "SELECT s.id, s.vendor_name, s.statement_date, COALESCE(SUM(l.balance), 0) AS owed "
        "FROM vendor_statements s "
        "LEFT JOIN vendor_statement_lines l ON l.statement_id = s.id "
        "WHERE s.deleted_at IS NULL "
        "GROUP BY s.id, s.vendor_name, s.statement_date"
    )):
        dated = to_date(r.statement_date)
        vendor = (r.vendor_name or "").strip().lower()
        if dated is None or not vendor:
            continue
        statements.append(Statement(vendor=vendor, dated=dated, balance_cents=to_cents(r.owed)))

    return Data(lines=lines, invoices=invoices, credit_memos=memos, statements=statements,
                synced_through=synced_through, linked=linked, unverified=unverified)


# ── step 0: the window ────────────────────────────────────────────────────────


def first_lines(lines: list[BankLine]) -> dict[str, date]:
    """Each included account's earliest line."""
    first: dict[str, date] = {}
    for ln in lines:
        if ln.account_id not in first or ln.posted < first[ln.account_id]:
            first[ln.account_id] = ln.posted
    return first


def last_seen(lines: list[BankLine], synced_through: dict[str, date | None]) -> dict[str, date]:
    """How far each account's feed reaches: the later of its last line and
    the date its sync is known to reach. A quiet account that keeps syncing
    still counts; a feed that stopped (lapsed re-auth, expired token, a first
    backfill that broke off) does not."""
    seen: dict[str, date] = {}
    for ln in lines:
        if ln.account_id not in seen or ln.posted > seen[ln.account_id]:
            seen[ln.account_id] = ln.posted
    for acct, synced in synced_through.items():
        if synced and (acct not in seen or synced > seen[acct]):
            seen[acct] = synced
    return seen


def coverage_start(lines: list[BankLine]) -> date | None:
    """The EARLIEST account's first line. Before it there is no bank data at
    all, so billing would read as pure profit (plan audit finding 1).

    Not the latest start, as the plan first said: then linking one new
    account (or one whose feed only backfills 90 days) collapses the whole
    view to the current month (code audit 2026-09-26). A month that some
    account does not fully cover is flagged instead — that account's money is
    missing from it."""
    first = first_lines(lines)
    return min(first.values()) if first else None


def month_of(d: date) -> tuple[int, int]:
    return (d.year, d.month)


def month_end(m: tuple[int, int]) -> date:
    return date(m[0], m[1], calendar.monthrange(m[0], m[1])[1])


def month_key(m: tuple[int, int]) -> str:
    return f"{m[0]:04d}-{m[1]:02d}"


def months_between(start: date, today: date) -> list[tuple[int, int]]:
    """Calendar months from start's month to today's, oldest first, capped at
    the most recent MAX_MONTHS."""
    out = []
    y, m = start.year, start.month
    while (y, m) <= month_of(today):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out[-MAX_MONTHS:]


# ── step 1: transfers between our own accounts ────────────────────────────────


def pair_transfers(lines: list[BankLine]) -> set[str]:
    """Ids of lines that pair with a counterpart on another included account.

    A ONE-TO-ONE assignment, not an existence test: two identical same-day
    transfers consume two counterparts (prod has four such). A pair needs
    equal absolute amounts, dates within ±1 day, and transfer wording on at
    least one side — amount + date alone over-pairs recurring payments
    (audit 2026-09-26: 8 of 28 amount matches had no transfer text).

    A MAXIMUM matching (augmenting paths), not a greedy nearest-first pass:
    greedy can strand a pair (in on day 2 takes the out on day 2, leaving an
    in on day 3 and an out on day 1 two days apart — code audit 2026-09-26).
    Candidates are tried nearest date first, so ties still go to the closest
    counterpart."""
    ins = sorted((ln for ln in lines if ln.amount_cents > 0), key=lambda x: (x.posted, x.id))
    outs_by_amount: dict[int, list[BankLine]] = defaultdict(list)
    for o in sorted((ln for ln in lines if ln.amount_cents < 0), key=lambda x: (x.posted, x.id)):
        outs_by_amount[-o.amount_cents].append(o)

    candidates: dict[str, list[str]] = {}
    for i in ins:
        opts = [
            o for o in outs_by_amount.get(i.amount_cents, ())
            if o.account_id != i.account_id
            and abs((o.posted - i.posted).days) <= 1
            and (i.worded or o.worded)
        ]
        opts.sort(key=lambda o: (abs((o.posted - i.posted).days), o.posted, o.id))
        candidates[i.id] = [o.id for o in opts]

    owner_of: dict[str, str] = {}  # out id -> in id

    def augment(root: str) -> None:
        """One augmenting-path search, iterative: a recursive one hits
        Python's recursion limit on ~1,200 chained same-amount transfers, and
        a 500 here blanks every screen of the plugin."""
        seen: set[str] = set()
        stack: list[list] = [[root, 0, None]]  # [in id, next candidate, chosen out]
        while stack:
            level = stack[-1]
            cands = candidates[level[0]]
            if level[1] >= len(cands):
                stack.pop()
                continue
            out_id = cands[level[1]]
            level[1] += 1
            if out_id in seen:
                continue
            seen.add(out_id)
            level[2] = out_id
            if out_id in owner_of:
                stack.append([owner_of[out_id], 0, None])
                continue
            for u, _next, v in stack:  # flip the path: each in takes its chosen out
                owner_of[v] = u
            return

    for i in ins:
        if candidates[i.id]:
            augment(i.id)
    return set(owner_of) | set(owner_of.values())


def possible_transfers(lines: list[BankLine], exclude: set[str]) -> set[str]:
    """Unpaired lines with an opposite-amount line on another account within
    ±1 day. Shown on Unsorted as a hint; never dropped automatically."""
    rest = [ln for ln in lines if ln.id not in exclude]
    by_amount: dict[int, list[BankLine]] = defaultdict(list)
    for ln in rest:
        by_amount[ln.amount_cents].append(ln)
    hits: set[str] = set()
    for ln in rest:
        for o in by_amount.get(-ln.amount_cents, ()):
            if o.account_id != ln.account_id and abs((o.posted - ln.posted).days) <= 1:
                hits.add(ln.id)
                break
    return hits


# ── step 2: rules ─────────────────────────────────────────────────────────────


def _stamp(dt) -> float:
    """Sortable time. SQLite hands back naive datetimes and Postgres aware
    ones, and the two cannot be compared directly."""
    return dt.timestamp() if isinstance(dt, datetime) else 0.0


def _rule_order(r: RuleRow):
    return (len(r.match_text), _stamp(r.updated_at), r.id)


def classify(line: BankLine, rules: list[RuleRow]) -> RuleRow | None:
    """The winning rule: the longest match_text, so the most specific rule
    decides; ties go to the most recently changed rule."""
    best = None
    for r in rules:
        needle = r.match_text.strip().lower()
        if not needle:
            continue
        if r.look_in == "payee":
            hit = needle in line.payee_text
        elif r.look_in == "memo":
            hit = needle in line.memo_text
        else:
            hit = needle in line.payee_text or needle in line.memo_text
        if hit and (best is None or _rule_order(r) > _rule_order(best)):
            best = r
    return best


# ── supplier balances ─────────────────────────────────────────────────────────


def supplier_owed(statements: list[Statement], at: date) -> int | None:
    """Total owed to suppliers at `at`, or None when any supplier's balance at
    that date is unknown.

    Per supplier, the latest statement dated on or before `at` counts. It is
    unknown when there is none yet, or when it is more than 35 days old —
    unless it showed nothing owed and no later statement exists (a supplier
    we stopped buying from would otherwise blank every month after)."""
    by_vendor: dict[str, list[Statement]] = defaultdict(list)
    for s in statements:
        by_vendor[s.vendor].append(s)
    total = 0
    for rows in by_vendor.values():
        before = [s for s in rows if s.dated <= at]
        if not before:
            return None
        latest = max(before, key=lambda s: s.dated)
        if (at - latest.dated).days > STALE_STATEMENT_DAYS:
            later = any(s.dated > at for s in rows)
            if latest.balance_cents != 0 or later:
                return None
        total += latest.balance_cents
    return total


# ── step 4: the monthly rows ──────────────────────────────────────────────────


@dataclass
class Month:
    key: str
    deposited: int = 0
    revenue: int = 0
    unsorted_in: int = 0
    billed: int = 0
    invoice_count: int = 0
    still_unpaid: int = 0
    costs: int = 0
    unsorted_out: int = 0
    supplier_delta: int | None = None
    owner_pay: int = 0
    loan: int = 0
    equipment: int = 0
    owner_labor: int = 0
    unsorted_abs: int = 0
    flags: list[str] = field(default_factory=list)

    @property
    def profit_deposits(self) -> int:
        return self.deposited - self.costs

    @property
    def profit_billed(self) -> int:
        return self.billed - self.costs - (self.supplier_delta or 0)

    @property
    def true_profit(self) -> int:
        return self.profit_deposits - self.owner_labor

    @property
    def left_in_business(self) -> int:
        return self.profit_deposits - self.owner_pay - self.loan - self.equipment


def labor_for(month: str, labor: list[LaborRow]) -> int:
    live = [r for r in labor if r.effective_month <= month]
    if not live:
        return 0
    return max(live, key=lambda r: (r.effective_month, _stamp(r.set_at), r.id)).amount_cents


@dataclass
class Result:
    coverage: date | None
    months: list[Month]
    unsorted: list[BankLine]
    possible: set[str]
    rule_hits: dict[int, int]
    unverified: int = 0
    accounts: int = 0


def compute(data: Data, rules: list[RuleRow], labor: list[LaborRow], today: date) -> Result:
    cov = coverage_start(data.lines)
    first = first_lines(data.lines)
    seen = last_seen(data.lines, data.synced_through)
    accounts = len(set(first) | set(data.synced_through))
    # An account with no lines at all: in its first days its history may still
    # be downloading, so it covers nothing (seventh pass). Linked longer ago,
    # a 365-day backfill found nothing — a dormant account, which covers every
    # month rather than flagging all of them forever (eighth pass).
    for acct in data.synced_through:
        when = data.linked.get(acct)
        if acct not in first and when is not None and (today - when).days > NEW_ACCOUNT_DAYS:
            first[acct] = date.min
    if cov is None:
        return Result(coverage=None, months=[], unsorted=[], possible=set(), rule_hits={})
    shown = months_between(cov, today)
    first_day = date(shown[0][0], shown[0][1], 1)
    months = {m: Month(key=month_key(m)) for m in shown}

    # Pair over a one-day margin so a transfer straddling the window's first
    # day still finds its partner; only in-window lines are counted.
    candidates = [ln for ln in data.lines if first_day - timedelta(days=1) <= ln.posted <= today]
    paired = pair_transfers(candidates)
    window = [ln for ln in candidates if ln.posted >= first_day]

    unsorted: list[BankLine] = []
    rule_hits: dict[int, int] = defaultdict(int)
    for ln in window:
        if ln.id in paired:
            continue
        mo = months[month_of(ln.posted)]
        rule = classify(ln, rules)
        cat = rule.category if rule else None
        if rule:
            rule_hits[rule.id] += 1
        amt = ln.amount_cents
        if cat is None:
            unsorted.append(ln)
            mo.unsorted_abs += abs(amt)
            if amt > 0:
                mo.unsorted_in += amt
            else:
                mo.unsorted_out += -amt
        elif cat == REVENUE:
            mo.revenue += amt  # a money-out line here is a customer refund
        elif cat in COST_CATEGORIES:
            mo.costs += -amt  # a money-in line here is a supplier refund
        elif cat == OWNER_PAY:
            mo.owner_pay += -amt
        elif cat == LOAN:
            mo.loan += -amt
        elif cat == EQUIPMENT:
            mo.equipment += -amt
        # TRANSFER and IGNORE land nowhere.

    for mo in months.values():
        mo.deposited = mo.revenue + mo.unsorted_in
        mo.costs += mo.unsorted_out

    for inv in data.invoices:
        m = month_of(inv.dated)
        if m in months:
            months[m].billed += inv.total_cents
            months[m].invoice_count += 1
            months[m].still_unpaid += inv.balance_cents
    for cm in data.credit_memos:
        m = month_of(cm.created)
        if m in months:
            months[m].billed -= cm.amount_cents

    for m, mo in months.items():
        prior_end = date(m[0], m[1], 1) - timedelta(days=1)
        end = min(month_end(m), today)
        a, b = supplier_owed(data.statements, prior_end), supplier_owed(data.statements, end)
        mo.supplier_delta = None if a is None or b is None else b - a
        mo.owner_labor = labor_for(mo.key, labor)

        # An account covers the month when its data starts by the 1st AND its
        # feed reaches the month's end (today, less a few days' sync lag, for
        # the current month). Without the end check a stopped card feed
        # silently raised every later month's profit (code audit 2026-09-26).
        reach = min(month_end(m), today) - timedelta(days=SYNC_GRACE_DAYS)
        covering = sum(
            1 for acct, d in first.items()
            if d <= date(m[0], m[1], 1) and seen.get(acct, d) >= reach
        )
        # Every included account counts, lines or not: one connected this
        # morning covers nothing yet (seventh audit pass).
        if m == month_of(cov) and cov > date(m[0], m[1], 1):
            mo.flags.append("partial bank month")
        elif covering < accounts:
            mo.flags.append(f"{covering} of {accounts} bank accounts cover this whole month")
        if m == month_of(today):
            mo.flags.append("month in progress")
        if mo.invoice_count == 0 or (
            mo.deposited > 0 and Decimal(mo.billed) < INCOMPLETE_BILLING_RATIO * mo.deposited
        ):
            mo.flags.append("billing looks incomplete")
        if mo.supplier_delta is None:
            mo.flags.append("supplier balance unknown")

    ordered = [months[m] for m in reversed(shown)]  # newest first
    possible = possible_transfers(window, paired)
    return Result(coverage=cov, months=ordered, unsorted=unsorted, possible=possible,
                  rule_hits=dict(rule_hits), unverified=len(data.unverified), accounts=accounts)
