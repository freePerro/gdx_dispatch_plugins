"""Rough-profit plugin API. Mounted by plugin-host under /api/plugins/roughprofit.

Reachable only through the core proxy. Every route passes the plugin's own
access gate (access.py) — the proxy's blanket `plugins.read` is not enough.

List endpoints return BARE ARRAYS (the host renderer assigns the response
straight to the DataTable). The host loads every list screen in one pass and
abandons the whole plugin on the first error, so a list GET must never 403 a
caller who can otherwise use the plugin: the owner-only access list answers
`view` and `edit` callers with an explanation row instead.

Amounts are formatted here, as strings, because the renderer shows raw values.
"""
from __future__ import annotations

import calendar
import re
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from gdx_dispatch.plugin_api.context import get_plugin_db
from gdx_plugin_roughprofit import compute
from gdx_plugin_roughprofit.access import (
    EDIT,
    OWNER,
    OWNER_ROLE,
    VIEW,
    canonical_user_id,
    require_edit,
    require_owner,
    require_view,
)
from gdx_plugin_roughprofit.models import Access, AccessChange, OwnerLabor, Rule, RuleChange

router = APIRouter()

REMOVE = "remove"
# Minus sign + U+2060 WORD JOINER, so a negative never wraps after the sign.
_MINUS = "\u2212\u2060"


def _now() -> datetime:
    return datetime.now(UTC)


def _today(db: Session) -> date:
    return compute.tenant_today(db)


def money(cents: int | None) -> str:
    """Whole dollars — these are rough numbers. None is a blank, not a zero."""
    if cents is None:
        return "—"
    dollars = int(round(abs(cents) / 100))
    return f"{_MINUS if cents < 0 else ''}${dollars:,}"


def month_label(key: str) -> str:
    """"2026-09" → "Sep 2026" (a hyphenated key wraps in a narrow cell)."""
    y, m = key.split("-")
    return f"{calendar.month_abbr[int(m)]} {y}"


def _stamp(dt, zone) -> str:
    """A stored UTC time, shown in the tenant's zone and labelled with it —
    the audit trail must not read hours off for the owner."""
    if dt is None:
        return ""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")


def _user_names(db: Session) -> dict[str, str]:
    """canonical user id → display name, for the "by" columns and the access
    list. Only plaintext columns are read (never an encrypted one)."""
    out = {}
    for r in db.execute(text(
        "SELECT id, name, full_name, username FROM users WHERE deleted_at IS NULL"
    )):
        out[canonical_user_id(r.id)] = (r.full_name or r.name or r.username or "").strip()
    return out


def _who(uid: str | None, names: dict[str, str]) -> str:
    if not uid:
        return ""
    return names.get(canonical_user_id(uid)) or uid


def _rules(db: Session) -> list[Rule]:
    return db.query(Rule).filter(Rule.deleted_at.is_(None)).all()


def _rule_rows(rules: list[Rule]) -> list[compute.RuleRow]:
    return [
        compute.RuleRow(id=r.id, match_text=r.match_text, look_in=r.look_in,
                        category=r.category, updated_at=r.updated_at)
        for r in rules
    ]


def _labor_rows(db: Session) -> list[compute.LaborRow]:
    return [
        compute.LaborRow(effective_month=r.effective_month, amount_cents=r.amount_cents,
                         set_at=r.set_at, id=r.id)
        for r in db.query(OwnerLabor).all()
    ]


def _result(db: Session) -> compute.Result:
    return compute.compute(compute.load(db), _rule_rows(_rules(db)), _labor_rows(db), _today(db))


# ── Monthly ───────────────────────────────────────────────────────────────────


@router.get("/monthly")
def monthly(gate=Depends(require_view), db: Session = Depends(get_plugin_db)) -> list[dict]:
    res = _result(db)
    if res.coverage is None:
        return [{"id": "none", "month": "—",
                 "flags": "No bank feed transactions yet. Connect a bank feed, then come back."}]
    rows = []
    if res.unverified:
        # One statement, not a flag on every row: the same doubt covers every
        # month, and per-row noise teaches the owner to ignore the column.
        # Short, because it renders in the narrow Flags column; Help explains.
        rows.append({"id": "feed-note", "month": "Note", "flags": (
            f"Bank feed unconfirmed for {res.unverified} of {res.accounts} accounts (see Help)")})
    for mo in res.months:
        rows.append({
            "id": mo.key,
            "month": month_label(mo.key),
            "deposited": money(mo.deposited),
            "billed": money(mo.billed),
            "still_unpaid": money(mo.still_unpaid),
            "costs": money(mo.costs),
            "supplier_delta": money(mo.supplier_delta),
            "profit_deposits": money(mo.profit_deposits),
            # When Δ is blank the term is dropped; the row's "supplier balance
            # unknown" flag says so.
            "profit_billed": money(mo.profit_billed),
            "owner_labor": money(mo.owner_labor),
            "true_profit": money(mo.true_profit),
            "owner_pay": money(mo.owner_pay),
            "loans_equipment": money(mo.loan + mo.equipment),
            "left_in_business": money(mo.left_in_business),
            "unsorted": money(mo.unsorted_abs),
            "flags": "; ".join(mo.flags),
        })
    return rows


# ── Unsorted ──────────────────────────────────────────────────────────────────


@router.get("/unsorted")
def unsorted(gate=Depends(require_view), db: Session = Depends(get_plugin_db)) -> list[dict]:
    """Lines no rule sorts, grouped by description and direction, largest
    first — the to-do list for writing rules."""
    res = _result(db)
    groups: dict[tuple[str, bool], dict] = {}
    for ln in res.unsorted:
        key = (ln.label, ln.amount_cents > 0)
        g = groups.setdefault(key, {"total": 0, "count": 0, "last": ln.posted,
                                    "memo": ln.memo.strip(), "possible": False})
        g["total"] += ln.amount_cents
        g["count"] += 1
        g["last"] = max(g["last"], ln.posted)
        g["possible"] = g["possible"] or ln.id in res.possible
    ordered = sorted(groups.items(), key=lambda kv: (-abs(kv[1]["total"]), kv[0][0]))
    return [
        {
            "id": f"{'in' if money_in else 'out'}:{label}",
            "description": label,
            "memo": g["memo"] if g["memo"] != label else "",
            "direction": "money in" if money_in else "money out",
            "total": money(g["total"]),
            "count": g["count"],
            "last_seen": g["last"].isoformat(),
            "note": "possible transfer" if g["possible"] else "",
        }
        for (label, money_in), g in ordered
    ]


# ── Rules ─────────────────────────────────────────────────────────────────────


@router.get("/rules")
def list_rules(gate=Depends(require_view), db: Session = Depends(get_plugin_db)) -> list[dict]:
    rules = _rules(db)
    hits = _result(db).rule_hits
    names = _user_names(db)
    zone = compute.tenant_zone(db)
    rules.sort(key=lambda r: (compute.CATEGORY_LABELS.get(r.category, r.category), r.match_text.lower()))
    return [
        {
            "id": r.id,
            "match_text": r.match_text,
            "look_in": r.look_in,
            "category": compute.CATEGORY_LABELS.get(r.category, r.category),
            "lines": hits.get(r.id, 0),
            "changed_by": _who(r.updated_by, names),
            "changed_at": _stamp(r.updated_at, zone),
        }
        for r in rules
    ]


# Request bodies take loose types and every check lives in the handler, with a
# one-sentence refusal. The host's create form turns any refused submit into
# an error page (core PluginScreen.onCreate has no catch), so a refusal should
# be rare and, when it happens, readable: never Pydantic's JSON error list.
class RuleIn(BaseModel):
    match_text: str | None = None
    look_in: str | None = None
    category: str | None = None


@router.post("/rules")
def save_rule(body: RuleIn, gate=Depends(require_edit), db: Session = Depends(get_plugin_db)) -> dict:
    """One form, three outcomes: an existing (match text, look in) with a new
    category is a change; with "Remove this rule" it is a soft delete;
    anything else is an add. Each writes exactly one rule_changes row."""
    ctx, _level = gate
    uid = canonical_user_id(ctx.user_id)
    match_text = " ".join((body.match_text or "").split())
    if not match_text:
        raise HTTPException(status_code=422, detail="Type the text to match first.")
    if len(match_text) > 200:
        raise HTTPException(status_code=422, detail="Match text is too long (200 characters at most).")
    look_in = body.look_in or "either"
    if look_in not in compute.LOOK_IN:
        raise HTTPException(status_code=422, detail="Look in must be payee, memo, or either.")
    if not body.category:
        raise HTTPException(status_code=422, detail="Pick a category.")
    if body.category != REMOVE and body.category not in compute.CATEGORY_LABELS:
        raise HTTPException(status_code=422, detail="Unknown category.")

    existing = (
        db.query(Rule)
        .filter(Rule.deleted_at.is_(None), func.lower(Rule.match_text) == match_text.lower(),
                Rule.look_in == look_in)
        .first()
    )
    now = _now()
    if body.category == REMOVE:
        if existing is None:
            raise HTTPException(status_code=404, detail=f"No rule for '{match_text}' in {look_in} to remove")
        existing.deleted_at = now
        existing.updated_by, existing.updated_at = uid, now
        change = RuleChange(rule_id=existing.id, action="removed", match_text=existing.match_text,
                            look_in=look_in, old_category=existing.category, new_category=None,
                            by_user=uid, at=now)
        rule, action = existing, "removed"
    elif existing is not None:
        if existing.category == body.category:
            return {"status": "unchanged", "id": existing.id}
        change = RuleChange(rule_id=existing.id, action="changed", match_text=existing.match_text,
                            look_in=look_in, old_category=existing.category,
                            new_category=body.category, by_user=uid, at=now)
        existing.category = body.category
        existing.updated_by, existing.updated_at = uid, now
        rule, action = existing, "changed"
    else:
        rule = Rule(match_text=match_text, look_in=look_in, category=body.category,
                    created_by=uid, created_at=now, updated_by=uid, updated_at=now)
        db.add(rule)
        db.flush()
        change = RuleChange(rule_id=rule.id, action="added", match_text=match_text, look_in=look_in,
                            old_category=None, new_category=body.category, by_user=uid, at=now)
        action = "added"
    db.add(change)
    db.commit()
    return {"status": action, "id": rule.id}


# ── Owner labor ───────────────────────────────────────────────────────────────

_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
_MONTHS["sept"] = 9


def parse_month(raw: str | None) -> str | None:
    """"2026-09", "2026-9", "9/2026", "09/2026", "Sep 2026", "September 2026"
    → "2026-09". None when it is not a month."""
    s = " ".join((raw or "").replace(",", " ").split()).lower()
    m = re.fullmatch(r"(\d{4})[-/ ](\d{1,2})", s) or None
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
    elif m := re.fullmatch(r"(\d{1,2})[-/ ](\d{4})", s):
        mo, y = int(m.group(1)), int(m.group(2))
    elif m := re.fullmatch(r"([a-z]+)\.? (\d{4})", s):
        mo, y = _MONTHS.get(m.group(1), 0), int(m.group(2))
    else:
        return None
    if not (1 <= mo <= 12 and 2000 <= y <= 2100):
        return None
    return f"{y:04d}-{mo:02d}"


@router.get("/owner-labor")
def list_owner_labor(gate=Depends(require_view), db: Session = Depends(get_plugin_db)) -> list[dict]:
    names = _user_names(db)
    zone = compute.tenant_zone(db)
    rows = db.query(OwnerLabor).order_by(OwnerLabor.effective_month.desc(), OwnerLabor.id.desc()).all()
    in_force = compute.labor_for(compute.month_key(compute.month_of(_today(db))), _labor_rows(db))
    out = [{"id": "now", "effective_month": "In force this month", "amount": money(in_force),
            "set_by": "", "set_at": ""}]
    out += [
        {"id": r.id, "effective_month": r.effective_month, "amount": money(r.amount_cents),
         "set_by": _who(r.set_by, names), "set_at": _stamp(r.set_at, zone)}
        for r in rows
    ]
    return out


class LaborIn(BaseModel):
    effective_month: str | None = None
    amount: float | None = None


@router.post("/owner-labor")
def set_owner_labor(body: LaborIn, gate=Depends(require_edit), db: Session = Depends(get_plugin_db)) -> dict:
    ctx, _level = gate
    month = parse_month(body.effective_month)
    if month is None:
        raise HTTPException(status_code=422, detail="Type the month like 2026-09 or Sep 2026.")
    if body.amount is None or not (0 <= body.amount <= 10_000_000):
        raise HTTPException(status_code=422, detail="Type a dollar amount per month, 0 or more.")
    row = OwnerLabor(effective_month=month, amount_cents=compute.to_cents(body.amount),
                     set_by=canonical_user_id(ctx.user_id), set_at=_now())
    db.add(row)
    db.commit()
    return {"status": "set", "id": row.id}


# ── Who can see it ────────────────────────────────────────────────────────────

LEVEL_LABELS = {VIEW: "View", EDIT: "View + edit"}


def _roles(db: Session) -> list[str]:
    return sorted({
        (r.role or "").strip()
        for r in db.execute(text("SELECT role FROM users WHERE deleted_at IS NULL"))
        if (r.role or "").strip() and (r.role or "").strip() != OWNER_ROLE
    })


@router.get("/access")
def list_access(gate=Depends(require_view), db: Session = Depends(get_plugin_db)) -> list[dict]:
    _ctx, level = gate
    if level != OWNER:
        # Not a 403: the host would abandon every other screen with it.
        return [{"id": "owner-only", "who": "Only the owner can see or change who has access.",
                 "kind": "", "level": "", "granted_by": "", "granted_at": ""}]
    names = _user_names(db)
    zone = compute.tenant_zone(db)
    out = [{"id": "owner", "who": "Owner role (always)", "kind": "role", "level": "View + edit + access",
            "granted_by": "", "granted_at": ""}]
    for r in db.query(Access).filter(Access.deleted_at.is_(None)).order_by(Access.kind, Access.subject).all():
        who = f"Role: {r.subject}" if r.kind == "role" else _who(r.subject, names)
        out.append({"id": r.id, "who": who, "kind": r.kind, "level": LEVEL_LABELS.get(r.level, r.level),
                    "granted_by": _who(r.granted_by, names), "granted_at": _stamp(r.granted_at, zone)})
    # The core proxy gates before this plugin does, and the plugin cannot
    # change core's role grants, so say so where the owner grants access.
    out.append({"id": "note", "who": "Also needed: the role's \"Use Rough Profit\" permission "
                "(and \"Change data in Rough Profit\" for edit) in Roles & Permissions. "
                "Admins have it already.", "kind": "", "level": "", "granted_by": "", "granted_at": ""})
    return out


@router.get("/access/subjects")
def access_subjects(gate=Depends(require_view), db: Session = Depends(get_plugin_db)) -> list[dict]:
    """Options for the "Grant to" select: every role in use, then each active
    user by name. Empty for non-owners (the form refuses them anyway)."""
    _ctx, level = gate
    if level != OWNER:
        return []
    opts = [{"label": f"Role: {role}", "value": f"role:{role}"} for role in _roles(db)]
    users = []
    for r in db.execute(text(
        "SELECT id, name, full_name, username, role, active FROM users WHERE deleted_at IS NULL"
    )):
        if r.active is not None and not compute._truthy(r.active):
            continue
        if (r.role or "") == OWNER_ROLE:
            continue  # the owner role already has everything
        name = (r.full_name or r.name or r.username or "").strip() or canonical_user_id(r.id)
        users.append({"label": f"{name} ({r.role or 'no role'})", "value": f"user:{canonical_user_id(r.id)}"})
    return opts + sorted(users, key=lambda o: o["label"].lower())


class AccessIn(BaseModel):
    subject: str | None = None  # "role:<name>" or "user:<uuid>"
    level: str | None = None


@router.post("/access")
def save_access(body: AccessIn, gate=Depends(require_owner), db: Session = Depends(get_plugin_db)) -> dict:
    """Grant, change or remove, on the same convention as Rules. Each writes
    exactly one access_changes row."""
    ctx, _level = gate
    uid = canonical_user_id(ctx.user_id)
    kind, _, subject = (body.subject or "").partition(":")
    subject = subject.strip()
    if kind not in ("role", "user") or not subject:
        raise HTTPException(status_code=422, detail="Choose a role or a person to grant.")
    if kind == "role" and subject == OWNER_ROLE:
        raise HTTPException(status_code=422, detail="The owner role always has access and cannot be changed")
    if kind == "user":
        subject = canonical_user_id(subject)
        known = {canonical_user_id(r.id): r.role for r in db.execute(
            text("SELECT id, role FROM users WHERE deleted_at IS NULL"))}
        if subject not in known:
            raise HTTPException(status_code=422, detail="No such user")
    if body.level not in (VIEW, EDIT, REMOVE):
        raise HTTPException(status_code=422, detail="Pick a level: View, View + edit, or Remove access.")

    existing = (
        db.query(Access)
        .filter(Access.deleted_at.is_(None), Access.kind == kind, Access.subject == subject)
        .first()
    )
    now = _now()
    if body.level == REMOVE:
        if existing is None:
            raise HTTPException(status_code=404, detail="That grant does not exist")
        existing.deleted_at = now
        change = AccessChange(access_id=existing.id, action="removed", kind=kind, subject=subject,
                              old_level=existing.level, new_level=None, by_user=uid, at=now)
        row, action = existing, "removed"
    elif existing is not None:
        if existing.level == body.level:
            return {"status": "unchanged", "id": existing.id}
        change = AccessChange(access_id=existing.id, action="changed", kind=kind, subject=subject,
                              old_level=existing.level, new_level=body.level, by_user=uid, at=now)
        existing.level = body.level
        row, action = existing, "changed"
    else:
        row = Access(kind=kind, subject=subject, level=body.level, granted_by=uid, granted_at=now)
        db.add(row)
        db.flush()
        change = AccessChange(access_id=row.id, action="granted", kind=kind, subject=subject,
                              old_level=None, new_level=body.level, by_user=uid, at=now)
        action = "granted"
    db.add(change)
    db.commit()
    return {"status": action, "id": row.id}
