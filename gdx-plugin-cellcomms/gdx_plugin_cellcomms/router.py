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

from datetime import datetime, timezone
import hashlib
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
from gdx_plugin_cellcomms.matching import match_customer, rematch_unlinked
from gdx_plugin_cellcomms.models import CellCall, CellMessage

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
    if q:
        q_filter = """
            AND other_number IN (
                SELECT DISTINCT other_number FROM plug_cellcomms_messages 
                WHERE company_id = :company_id AND other_number IS NOT NULL AND (
                    LOWER(other_number) LIKE :q_lower OR 
                    LOWER(customer_name) LIKE :q_lower OR 
                    LOWER(body) LIKE :q_lower
                )
            )
        """
        params["q_lower"] = f"%{q.lower()}%"

    sql = f"""
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
        {q_filter}
    )
    SELECT id, other_number, direction, body, media_url, sent_at, cust_id, cust_name, cnt
    FROM ranked
    WHERE rn = 1
    ORDER BY sent_at DESC
    LIMIT :limit
    """
    rows = db.execute(text(sql), params).all()
    res = []
    for r in rows:
        sent_at_val = r[5]
        if isinstance(sent_at_val, datetime):
            sent_at_str = sent_at_val.isoformat()
        else:
            sent_at_str = str(sent_at_val) if sent_at_val else None

        res.append({
            "thread_key": r[1],
            "number": r[1],
            "customer_name": r[7] or "",
            "customer_id": str(r[6]) if r[6] else None,
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
    from gdx_plugin_cellcomms.matching import normalize_e164
    e164 = normalize_e164(thread_key)
    numbers = {thread_key.strip()}
    if e164:
        numbers.add(e164)
        if e164.startswith("+1") and len(e164) == 12:
            numbers.add(e164[2:])
            numbers.add(e164[1:])

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
            "number": r.other_number,
        }
        for r in rows
    ]

@router.get("/messages")
def list_messages(
    q: str | None = Query(default=None, max_length=100),
    limit: int = DEFAULT_LIST_LIMIT,
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> list[dict]:
    query = db.query(CellMessage).filter(CellMessage.company_id == ctx.tenant_id)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            CellMessage.body.ilike(like),
            CellMessage.other_number.ilike(like),
            CellMessage.customer_name.ilike(like),
        ))
    rows = query.order_by(CellMessage.sent_at.desc().nullslast()).limit(limit).all()
    return [
        {
            "id": r.id,
            "when": _fmt_ts(r.sent_at),
            "direction": "→ out" if r.direction == "out" else "← in",
            "number": r.other_number,
            "customer": r.customer_name or "",
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
    if q:
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

    sections = {
        "Message Details": {
            "Direction": "Outgoing" if msg.direction == "out" else "Incoming",
            "Number": msg.other_number or "",
            "Customer": msg.customer_name or "Unlinked",
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

    from gdx_plugin_cellcomms.matching import normalize_e164
    clean_number = normalize_e164(payload.number) or payload.number.strip()

    now = datetime.now(timezone.utc)
    ts_str = str(int(now.timestamp() * 1000))
    dedupe_key = hashlib.sha256(f"out|{clean_number}|{ts_str}|{payload.body}".encode()).hexdigest()

    cid, cname = match_customer(db, clean_number)
    msg = CellMessage(
        company_id=ctx.tenant_id,
        direction="out",
        other_number=clean_number,
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
