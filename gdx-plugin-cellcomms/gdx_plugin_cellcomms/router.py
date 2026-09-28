"""Cell-comms plugin API. Mounted by plugin-host under /api/plugins/cellcomms.

Reachable two ways, both of which stamp the forwarded tenant headers:
  - the core proxy (authenticated users: the screens, the backfill upload,
    the rematch button);
  - the core cell-gateway webhook shim (routers/cell_gateway.py), which is the
    ONLY unauthenticated door and does its own shared-secret check before
    relaying the phone's POST to /ingest here.

List endpoints return BARE ARRAYS (host renderer assigns the response straight
to the DataTable — an {"items": [...]} envelope renders zero rows).
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
from io import StringIO
import logging
import mimetypes
import os
from pathlib import Path
import xml.etree.ElementTree as ET

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from gdx_dispatch.plugin_api.context import PluginContext, get_plugin_context, get_plugin_db
from gdx_plugin_cellcomms.backfill import ingest_backup_xml
from gdx_plugin_cellcomms.ingest import event_to_row
from gdx_plugin_cellcomms.matching import match_customer, normalize_e164, rematch_unlinked
from gdx_plugin_cellcomms.models import CellCall, CellContact, CellMessage

log = logging.getLogger(__name__)

router = APIRouter()

MAX_BACKUP_BYTES = 100 * 1024 * 1024  # a decade of texts is tens of MB
DEFAULT_LIST_LIMIT = 5000
MAX_LIST_LIMIT = 20000

MEDIA_DIR = Path(os.getenv("CELL_MEDIA_DIR", "/plugins/_media"))
try:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

class SendPayload(BaseModel):
    number: str
    body: str

class ContactPayload(BaseModel):
    name: str


@router.post("/ingest")
def ingest_event(
    payload: dict,
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> dict:
    row = event_to_row(payload)
    if row is None:
        raise HTTPException(status_code=422, detail="unrecognized event: expected kind 'sms' or 'call'")

    kind = row.pop("kind")
    model = CellMessage if kind == "sms" else CellCall
    existing = db.query(model.id).filter(model.dedupe_key == row["dedupe_key"]).first()
    if existing is not None:
        # nomad retries with backoff — a redelivery must be a cheap no-op.
        return {"status": "duplicate", "kind": kind, "id": existing[0]}

    cid, cname = match_customer(db, row["other_number"])
    # If not a GDX customer, check known CellContact
    if not cname and row.get("other_number"):
        try:
            contact = db.query(CellContact.name).filter(
                CellContact.company_id == ctx.tenant_id,
                CellContact.phone_number == row["other_number"],
            ).first()
            if contact and contact[0]:
                row["contact_name"] = contact[0]
        except Exception:
            pass

    rec = model(company_id=ctx.tenant_id, customer_id=cid, customer_name=cname, **row)
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return {"status": "ok", "kind": kind, "id": rec.id}


@router.post("/backfill")
async def backfill_upload(
    file: UploadFile,
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> dict:
    data = await file.read()
    if len(data) > MAX_BACKUP_BYTES:
        raise HTTPException(status_code=413, detail="backup file exceeds 100 MB")
    if not data.strip():
        raise HTTPException(status_code=422, detail="empty file")
    try:
        result = ingest_backup_xml(db, ctx.tenant_id, data)
    except ET.ParseError as exc:
        db.rollback()
        # Nothing from a malformed file lands — half an import is worse than none.
        raise HTTPException(status_code=422, detail=f"not a readable SMS Backup & Restore XML file: {exc}") from exc
    # New history often belongs to customers who already exist — link it now
    # rather than waiting for the nightly pass.
    rematch = rematch_unlinked(db, ctx.tenant_id)
    return {"status": "ok", "file": file.filename, **result.as_dict(), "rematch": rematch}


@router.post("/rematch")
def rematch(
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> dict:
    return {"status": "ok", **rematch_unlinked(db, ctx.tenant_id)}


def _fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    m, s = divmod(max(int(seconds), 0), 60)
    return f"{m}:{s:02d}"


def _fmt_ts(dt) -> str | None:
    return dt.isoformat() if dt else None


@router.get("/threads")
def list_threads(
    q: str | None = Query(default=None, max_length=100),
    limit: int = 500,
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> list[dict]:
    q_filter = ""
    params: dict[str, object] = {"company_id": ctx.tenant_id, "limit": limit}
    if q and isinstance(q, str):
        q_filter = """
            AND m.other_number IN (
                SELECT DISTINCT m2.other_number FROM plug_cellcomms_messages m2
                LEFT JOIN plug_cellcomms_contacts c2 
                  ON c2.company_id = m2.company_id AND c2.phone_number = m2.other_number
                LEFT JOIN plug_cellcomms_calls cl2 
                  ON cl2.company_id = m2.company_id AND cl2.other_number = m2.other_number
                WHERE m2.company_id = :company_id AND m2.other_number IS NOT NULL AND (
                    LOWER(m2.other_number) LIKE :q_lower OR 
                    LOWER(COALESCE(m2.customer_name, '')) LIKE :q_lower OR 
                    LOWER(COALESCE(c2.name, '')) LIKE :q_lower OR 
                    LOWER(COALESCE(cl2.contact_name, '')) LIKE :q_lower OR 
                    LOWER(COALESCE(m2.body, '')) LIKE :q_lower
                )
            )
        """
        params["q_lower"] = f"%{q.lower()}%"

    # SQLite and Postgres compatibility: query ranked messages and join contacts
    sql = f"""
    WITH ranked AS (
      SELECT 
        m.id, m.other_number, m.direction, m.body, m.media_url, m.sent_at,
        MAX(m.customer_id) OVER (PARTITION BY m.other_number) as cust_id,
        MAX(m.customer_name) OVER (PARTITION BY m.other_number) as cust_name,
        ROW_NUMBER() OVER (PARTITION BY m.other_number ORDER BY m.sent_at DESC, m.id DESC) as rn,
        COUNT(*) OVER (PARTITION BY m.other_number) as cnt
      FROM plug_cellcomms_messages m
      WHERE m.company_id = :company_id
        AND m.other_number IS NOT NULL AND m.other_number != ''
        {q_filter}
    )
    SELECT 
      r.id, r.other_number, r.direction, r.body, r.media_url, r.sent_at, 
      r.cust_id, r.cust_name, r.cnt,
      COALESCE(c.name, call_contact.contact_name, '') as resolved_contact_name
    FROM ranked r
    LEFT JOIN plug_cellcomms_contacts c 
      ON c.company_id = :company_id AND c.phone_number = r.other_number
    LEFT JOIN (
      SELECT other_number, contact_name,
             ROW_NUMBER() OVER (PARTITION BY other_number ORDER BY started_at DESC) as crn
      FROM plug_cellcomms_calls
      WHERE company_id = :company_id AND contact_name IS NOT NULL AND contact_name != ''
    ) call_contact ON call_contact.other_number = r.other_number AND call_contact.crn = 1
    WHERE r.rn = 1
    ORDER BY r.sent_at DESC
    LIMIT :limit
    """
    try:
        rows = db.execute(text(sql), params).all()
    except Exception:
        # Fallback if plug_cellcomms_contacts table has not been created yet in local test sqlite
        fallback_sql = f"""
        WITH ranked AS (
          SELECT 
            id, other_number, direction, body, media_url, sent_at,
            MAX(customer_id) OVER (PARTITION BY other_number) as cust_id,
            MAX(customer_name) OVER (PARTITION BY other_number) as cust_name,
            ROW_NUMBER() OVER (PARTITION BY other_number ORDER BY sent_at DESC, id DESC) as rn,
            COUNT(*) OVER (PARTITION BY other_number) as cnt
          FROM plug_cellcomms_messages
          WHERE company_id = :company_id
            AND other_number IS NOT NULL AND other_number != ''
        )
        SELECT id, other_number, direction, body, media_url, sent_at, cust_id, cust_name, cnt, '' as resolved_contact_name
        FROM ranked
        WHERE rn = 1
        ORDER BY sent_at DESC
        LIMIT :limit
        """
        rows = db.execute(text(fallback_sql), {"company_id": ctx.tenant_id, "limit": limit}).all()

    res = []
    for r in rows:
        sent_at_val = r[5]
        if isinstance(sent_at_val, datetime):
            sent_at_str = sent_at_val.isoformat()
        else:
            sent_at_str = str(sent_at_val) if sent_at_val else None

        cust_name = (r[7] or "").strip()
        contact_name = (r[9] or "").strip() if len(r) > 9 and r[9] else ""
        resolved_display = cust_name or contact_name

        res.append({
            "thread_key": r[1],
            "number": r[1],
            "customer_name": cust_name,
            "customer_id": str(r[6]) if r[6] else None,
            "contact_name": contact_name,
            "display_name": resolved_display,
            "message_count": int(r[8]),
            "last_message_at": sent_at_str,
            "last_message_body": r[3] or ("[Photo]" if r[4] else ""),
            "last_message_direction": r[2],
            "last_message_has_media": bool(r[4]),
        })
    return res


@router.get("/threads/{thread_key}/messages")
def get_thread_messages(
    thread_key: str,
    limit: int = 1000,
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> list[dict]:
    e164 = normalize_e164(thread_key)
    numbers = {thread_key.strip()}
    if e164:
        numbers.add(e164)
        if e164.startswith("+1") and len(e164) == 12:
            numbers.add(e164[2:])
            numbers.add(e164[1:])

    # Resolve contact name
    contact_name = ""
    try:
        c_row = (
            db.query(CellContact.name)
            .filter(
                CellContact.company_id == ctx.tenant_id,
                CellContact.phone_number.in_(list(numbers)),
            )
            .first()
        )
        if c_row and c_row[0]:
            contact_name = c_row[0]
        else:
            call_row = (
                db.query(CellCall.contact_name)
                .filter(
                    CellCall.company_id == ctx.tenant_id,
                    CellCall.other_number.in_(list(numbers)),
                    CellCall.contact_name.isnot(None),
                    CellCall.contact_name != "",
                )
                .order_by(CellCall.started_at.desc().nullslast())
                .first()
            )
            if call_row and call_row[0]:
                contact_name = call_row[0]
    except Exception:
        pass

    rows = (
        db.query(CellMessage)
        .filter(
            CellMessage.company_id == ctx.tenant_id,
            CellMessage.other_number.in_(list(numbers)),
        )
        .order_by(CellMessage.sent_at.asc().nullsfirst(), CellMessage.id.asc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "direction": r.direction,
            "body": r.body or "",
            "media_url": r.media_url,
            "media_type": r.media_type,
            "when": _fmt_ts(r.sent_at),
            "sent_at": _fmt_ts(r.sent_at),
            "customer_id": r.customer_id,
            "customer_name": r.customer_name or "",
            "contact_name": getattr(r, "contact_name", None) or contact_name or "",
            "display_name": (r.customer_name or getattr(r, "contact_name", None) or contact_name or "").strip(),
            "number": r.other_number,
        }
        for r in rows
    ]


@router.post("/threads/{thread_key}/contact")
def update_thread_contact(
    thread_key: str,
    payload: ContactPayload,
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> dict:
    clean_number = normalize_e164(thread_key) or thread_key.strip()
    name = payload.name.strip()

    # Upsert CellContact
    try:
        existing = db.query(CellContact).filter(
            CellContact.company_id == ctx.tenant_id,
            CellContact.phone_number == clean_number,
        ).first()
        if existing:
            existing.name = name
        else:
            db.add(CellContact(company_id=ctx.tenant_id, phone_number=clean_number, name=name))
    except Exception as exc:
        log.warning("cellcomms.upsert_contact_error: %s", exc)

    # Also update any messages and calls matching this number
    try:
        db.query(CellMessage).filter(
            CellMessage.company_id == ctx.tenant_id,
            CellMessage.other_number == clean_number,
        ).update({"contact_name": name}, synchronize_session=False)
    except Exception:
        pass

    try:
        db.query(CellCall).filter(
            CellCall.company_id == ctx.tenant_id,
            CellCall.other_number == clean_number,
        ).update({"contact_name": name}, synchronize_session=False)
    except Exception:
        pass

    db.commit()
    return {"status": "ok", "number": clean_number, "name": name}


def _parse_contacts_file(content: str) -> list[tuple[str, str]]:
    """Parse vCard (.vcf) or CSV for (phone, name)."""
    contacts = []
    lines = content.splitlines()
    if "BEGIN:VCARD" in content:
        name = ""
        for line in lines:
            line = line.strip()
            if line.startswith("FN:") or line.startswith("FN;"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("TEL") and ":" in line:
                raw_tel = line.split(":", 1)[1].strip()
                if raw_tel:
                    contacts.append((raw_tel, name))
            elif line == "END:VCARD":
                name = ""
    else:
        # CSV parsing
        reader = csv.reader(StringIO(content))
        header = None
        for row in reader:
            if not row or not any(row):
                continue
            if header is None:
                header = [h.strip().lower() for h in row]
                continue
            name = row[0].strip() if len(row) > 0 else ""
            phone = row[1].strip() if len(row) > 1 else ""
            if name and phone:
                contacts.append((phone, name))
    return contacts


@router.post("/contacts/import")
async def import_contacts(
    file: UploadFile,
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> dict:
    data = await file.read()
    try:
        text_content = data.decode("utf-8", errors="replace")
    except Exception:
        raise HTTPException(status_code=422, detail="Unable to read contacts file")
    pairs = _parse_contacts_file(text_content)
    if not pairs:
        raise HTTPException(status_code=422, detail="No contacts found in file")

    count = 0
    for raw_phone, name in pairs:
        if not raw_phone or not name:
            continue
        clean_num = normalize_e164(raw_phone) or raw_phone.strip()
        existing = db.query(CellContact).filter(
            CellContact.company_id == ctx.tenant_id,
            CellContact.phone_number == clean_num,
        ).first()
        if existing:
            existing.name = name
        else:
            db.add(CellContact(company_id=ctx.tenant_id, phone_number=clean_num, name=name))
        count += 1
    db.commit()
    rematch_unlinked(db, ctx.tenant_id)
    return {"status": "ok", "contacts_imported": count}


@router.get("/messages")
def list_messages(
    q: str | None = Query(default=None, max_length=100),
    limit: int = DEFAULT_LIST_LIMIT,
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> list[dict]:
    # Build contact lookup
    contact_map: dict[str, str] = {}
    try:
        for c in db.query(CellContact).filter(CellContact.company_id == ctx.tenant_id):
            if c.phone_number and c.name:
                contact_map[c.phone_number] = c.name
    except Exception:
        pass

    query = db.query(CellMessage).filter(CellMessage.company_id == ctx.tenant_id)
    if q and isinstance(q, str):
        like = f"%{q}%"
        matched_numbers = [num for num, name in contact_map.items() if q.lower() in name.lower()]
        conditions = [
            CellMessage.body.ilike(like),
            CellMessage.other_number.ilike(like),
            CellMessage.customer_name.ilike(like),
        ]
        if hasattr(CellMessage, "contact_name"):
            conditions.append(CellMessage.contact_name.ilike(like))
        if matched_numbers:
            conditions.append(CellMessage.other_number.in_(matched_numbers))
        query = query.filter(or_(*conditions))

    rows = query.order_by(CellMessage.sent_at.desc().nullslast()).limit(limit).all()
    return [
        {
            "id": r.id,
            "when": _fmt_ts(r.sent_at),
            "direction": "→ out" if r.direction == "out" else "← in",
            "number": r.other_number,
            "customer": r.customer_name or getattr(r, "contact_name", None) or contact_map.get(r.other_number or "", ""),
            "body": r.body,
            "media_url": r.media_url,
        }
        for r in rows
    ]


@router.get("/calls")
def list_calls(
    q: str | None = Query(default=None, max_length=100),
    limit: int = DEFAULT_LIST_LIMIT,
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> list[dict]:
    query = db.query(CellCall).filter(CellCall.company_id == ctx.tenant_id)
    if q and isinstance(q, str):
        like = f"%{q}%"
        query = query.filter(or_(
            CellCall.other_number.ilike(like),
            CellCall.contact_name.ilike(like),
            CellCall.customer_name.ilike(like),
        ))
    rows = query.order_by(CellCall.started_at.desc().nullslast()).limit(limit).all()
    return [
        {
            "when": _fmt_ts(r.started_at),
            "type": r.call_type or ("outgoing" if r.direction == "out" else "incoming"),
            "number": r.other_number,
            "customer": r.customer_name or r.contact_name or "",
            "duration": _fmt_duration(r.duration_s),
        }
        for r in rows
    ]


@router.get("/messages/{message_id}")
def get_message(
    message_id: int,
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> dict:
    msg = db.query(CellMessage).filter(
        CellMessage.id == message_id,
        CellMessage.company_id == ctx.tenant_id,
    ).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    contact_name = getattr(msg, "contact_name", None)
    if not contact_name and msg.other_number:
        c = db.query(CellContact.name).filter(
            CellContact.company_id == ctx.tenant_id,
            CellContact.phone_number == msg.other_number,
        ).first()
        if c:
            contact_name = c[0]

    sections = {
        "Message Details": {
            "Direction": "Outgoing" if msg.direction == "out" else "Incoming",
            "Number": msg.other_number or "",
            "Customer": msg.customer_name or contact_name or "Unlinked",
            "Date / Time": _fmt_ts(msg.sent_at) or "",
            "Source": msg.source,
        },
        "Message Content": msg.body or "(No text body)",
    }
    if msg.media_url:
        sections["Attached Photo"] = msg.media_url
    return sections


@router.api_route("/media/{filename}", methods=["GET", "HEAD"])
def get_media_file(filename: str):
    safe_name = os.path.basename(filename)
    path = MEDIA_DIR / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    media_type, _ = mimetypes.guess_type(str(path))
    return FileResponse(str(path), media_type=media_type or "image/jpeg")


@router.post("/send")
def send_message(
    payload: SendPayload,
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> dict:
    if not payload.number or not payload.number.strip():
        raise HTTPException(status_code=422, detail="Phone number is required")
    if not payload.body or not payload.body.strip():
        raise HTTPException(status_code=422, detail="Message body is required")

    clean_number = normalize_e164(payload.number) or payload.number.strip()

    now = datetime.now(timezone.utc)
    ts_str = str(int(now.timestamp() * 1000))
    dedupe_key = hashlib.sha256(f"out|{clean_number}|{ts_str}|{payload.body}".encode()).hexdigest()

    cid, cname = match_customer(db, clean_number)
    contact_name = None
    if not cname:
        try:
            c = db.query(CellContact.name).filter(
                CellContact.company_id == ctx.tenant_id,
                CellContact.phone_number == clean_number,
            ).first()
            if c:
                contact_name = c[0]
        except Exception:
            pass

    msg = CellMessage(
        company_id=ctx.tenant_id,
        direction="out",
        other_number=clean_number,
        contact_name=contact_name,
        body=payload.body.strip(),
        sent_at=now,
        customer_id=cid,
        customer_name=cname,
        source="app",
        dedupe_key=dedupe_key,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return {"status": "ok", "id": msg.id, "to": clean_number}
