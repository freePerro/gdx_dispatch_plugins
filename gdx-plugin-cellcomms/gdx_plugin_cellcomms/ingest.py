"""Nomad Gateway webhook payload → row dicts for plugin tables.

Contract with the cell-gateway forwarder:
  - Phone posts JSON to /api/cell-gateway/sms or /call (core, routers/cell_gateway.py).
  - Forwarder verifies the bearer token (SHARED_CELL_SECRET against
    the header, matching ADR-013 single-tenant boundary), then re-POSTs the
    parsed dict to /api/plugins/cellcomms/ingest with X-Company-Id stamped.
  - Ingest never trusts anything caller-supplied except the payload fields
    and the company header; dedupe_key is derived entirely from message content.

Fields per kind:
  sms:  kind="sms", from, text, sentStamp (or receivedStamp), sim
  call: kind="call", from, duration, timestamp, sim, contact
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import re

log = logging.getLogger(__name__)


def canonical_number(raw: object) -> str | None:
    """Best-effort canonical form of a phone number: E.164 if possible, else
    the stripped string. None on missing/empty. Never throws."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if not digits:
        return s
    if s.startswith("+"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return s


def parse_ts(raw: object) -> datetime | None:
    """Parse epoch millis or ISO-8601 string to timezone-aware UTC datetime."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.isdigit():
        try:
            val = int(s)
            # epoch seconds vs millis heuristic (10 digits vs 13 digits)
            if val < 100_000_000_000:
                val *= 1000
            return datetime.fromtimestamp(val / 1000.0, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    try:
        # ISO-8601 with or without Z
        clean = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def ts_token(dt: datetime | None, raw: object) -> str:
    """Stable token representing a timestamp for dedupe keys.

    Prefers epoch-seconds from parsed UTC datetime so different string
    representations of the same instant hash identically. Falls back to
    the stripped raw string if unparseable.
    """
    if dt is not None:
        return str(int(dt.timestamp()))
    return str(raw or "").strip()


def message_dedupe_key(direction: str, other_number: str | None, ts_tok: str, body: str) -> str:
    seed = f"{direction}|{other_number or ''}|{ts_tok}|{body}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def call_dedupe_key(direction: str, other_number: str | None, ts_tok: str) -> str:
    seed = f"{direction}|{other_number or ''}|{ts_tok}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def event_to_row(payload: dict) -> dict | None:
    """Nomad event → row dict for CellMessage/CellCall, or None if not one.

    Returns {"kind": "sms"|"call", **column values}. Raises nothing on odd
    payloads — an unrecognized kind is None (the router 422s it), missing
    fields degrade to NULL columns.
    """
    kind = str(payload.get("kind") or "").strip().lower()
    number = canonical_number(payload.get("from"))
    raw_json = json.dumps(payload, ensure_ascii=False)

    if kind == "sms":
        sent_at = parse_ts(payload.get("sentStamp")) or parse_ts(payload.get("receivedStamp"))
        body = str(payload.get("text") or "")
        token = ts_token(sent_at, payload.get("sentStamp") or payload.get("receivedStamp"))
        contact = str(payload.get("contact") or payload.get("contact_name") or "").strip() or None
        return {
            "kind": "sms",
            "direction": "in",
            "other_number": number,
            "contact_name": contact,
            "body": body,
            "sim": (str(payload.get("sim")) or None) if payload.get("sim") is not None else None,
            "sent_at": sent_at,
            "source": "webhook",
            "dedupe_key": message_dedupe_key("in", number, token, body),
            "raw_payload": raw_json,
        }

    if kind == "call":
        started_at = parse_ts(payload.get("timestamp"))
        try:
            duration = int(str(payload.get("duration")).strip())
        except (ValueError, TypeError, AttributeError):
            duration = None
        contact = str(payload.get("contact") or payload.get("contact_name") or "").strip() or None
        return {
            "kind": "call",
            "direction": "in",
            "call_type": "incoming",
            "other_number": number,
            "contact_name": contact,
            "started_at": started_at,
            "duration_s": duration,
            "source": "webhook",
            "dedupe_key": call_dedupe_key("in", number, ts_token(started_at, payload.get("timestamp"))),
            "raw_payload": raw_json,
        }

    return None
