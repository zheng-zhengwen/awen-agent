"""Advertising adjustment event ledger.

The technical audit log answers whether a write call succeeded.  This ledger is
the business-facing, source-neutral record used to answer what changed, why it
changed, which parent/child ASINs were in scope, and what later review observed.

Actions are immutable.  Human notes and review revisions are appended alongside
the action so that a later explanation never rewrites the original fact.
"""
from __future__ import annotations

import datetime as dt
import base64
import binascii
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config, stores

DB_PATH = config.AWEN_DIR / "adjustments.db"

_SENSITIVE_KEY_PARTS = (
    "token", "password", "secret", "authorization", "credential", "apikey", "appkey",
    "signature", "cookie",
)
_SENSITIVE_TEXT_RE = re.compile(
    r"(?i)([\"']?(?:access[_-]?token|refresh[_-]?token|password|app[_-]?secret|"
    r"client[_-]?secret|api[_-]?key|app[_-]?key|authorization|signature|sign|cookie)"
    r"[\"']?\s*(?:=|:)\s*[\"']?)([^&,\s\"'}]+)"
)
_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
_VALID_ASIN_ROLES = {"parent", "child", "advertised_child", "unadvertised_sibling"}
_SCHEMA_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[str] = set()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _decode(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _sensitive_key(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).lower())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_text(value: str) -> str:
    redacted = _BEARER_RE.sub(r"\1[REDACTED]", value)
    return _SENSITIVE_TEXT_RE.sub(r"\1[REDACTED]", redacted)


def _clean_text(value: Any) -> str:
    return _redact_text(str(value or ""))


def scrub_sensitive(value: Any) -> Any:
    """Return a recursively sanitized copy safe for ledger or API diagnostics."""
    if isinstance(value, dict):
        return {
            str(k): scrub_sensitive(v)
            for k, v in value.items()
            if not _sensitive_key(k)
        }
    if isinstance(value, list):
        return [scrub_sensitive(v) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub_sensitive(v) for v in value)
    if isinstance(value, str):
        return _redact_text(value)
    return value


_scrub_sensitive = scrub_sensitive


def _scrub_changes(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or item.get("code") or "").lower()
        if _sensitive_key(field):
            continue
        cleaned = _scrub_sensitive(item)
        if isinstance(cleaned, dict):
            out.append(cleaned)
    return out


def _parse_timestamp(value: Any, timezone_name: str = "UTC") -> tuple[float, str]:
    if isinstance(value, (int, float)):
        stamp = float(value)
        if not math.isfinite(stamp):
            raise ValueError("operated_at 必须是有限时间戳")
        try:
            timezone_value = ZoneInfo(timezone_name or "UTC")
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"未知 IANA 时区: {timezone_name}") from exc
        return stamp, dt.datetime.fromtimestamp(stamp, timezone_value).isoformat()
    raw = str(value or "").strip()
    if not raw:
        now = time.time()
        return now, dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat()
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"operated_at 不是有效 ISO 时间: {raw}") from exc
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name or "UTC"))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"未知 IANA 时区: {timezone_name}") from exc
    return parsed.timestamp(), parsed.isoformat()


def _fingerprint(event: dict[str, Any]) -> str:
    material = {
        "sid": str(event.get("sid") or ""),
        "source": str(event.get("source") or ""),
        "sponsored_type": str(event.get("sponsored_type") or ""),
        "operate_type": str(event.get("operate_type") or ""),
        "object_id": str(event.get("object_id") or ""),
        "campaign_id": str(event.get("campaign_id") or ""),
        "ad_group_id": str(event.get("ad_group_id") or ""),
        "operated_at": str(event.get("operated_at") or event.get("operated_at_local") or ""),
        "before": event.get("before") or {},
        "after": event.get("after") or {},
        "operator_id": str(event.get("operator_id") or ""),
    }
    return hashlib.sha256(_json(_scrub_sensitive(material)).encode("utf-8")).hexdigest()


def _encode_cursor(operated_at: float, action_id: str) -> str:
    raw = _json({"operated_at": float(operated_at), "id": str(action_id)}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        raw = str(cursor or "")
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        operated_at = float(payload["operated_at"])
        action_id = str(payload["id"])
        if not math.isfinite(operated_at) or not action_id:
            raise ValueError
        return operated_at, action_id
    except (binascii.Error, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("cursor 无效") from exc


def _connect(path: Optional[Path] = None) -> sqlite3.Connection:
    target = path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    existed_before_connect = target.exists()
    conn = sqlite3.connect(str(target), timeout=10.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        path_key = str(target.resolve())
        with _SCHEMA_LOCK:
            if path_key not in _INITIALIZED_PATHS or not existed_before_connect:
                conn.execute("PRAGMA journal_mode=WAL")
                _migrate(conn)
                _INITIALIZED_PATHS.add(path_key)
        return conn
    except Exception:
        conn.close()
        raise


@contextmanager
def _connection(path: Optional[Path] = None) -> Generator[sqlite3.Connection, None, None]:
    """Commit/rollback like sqlite's context manager and always release the handle."""
    conn = _connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS adjustment_batches (
            id TEXT PRIMARY KEY,
            sid TEXT NOT NULL,
            source TEXT NOT NULL,
            source_batch_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS adjustment_actions (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES adjustment_batches(id),
            sid TEXT NOT NULL,
            marketplace_id TEXT NOT NULL DEFAULT '',
            profile_id TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL,
            source_event_id TEXT,
            source_fingerprint TEXT NOT NULL,
            sponsored_type TEXT NOT NULL DEFAULT '',
            operate_type TEXT NOT NULL DEFAULT '',
            action_type TEXT NOT NULL DEFAULT '',
            object_type TEXT NOT NULL DEFAULT '',
            object_id TEXT NOT NULL DEFAULT '',
            object_name TEXT NOT NULL DEFAULT '',
            campaign_id TEXT NOT NULL DEFAULT '',
            campaign_name TEXT NOT NULL DEFAULT '',
            ad_group_id TEXT NOT NULL DEFAULT '',
            ad_group_name TEXT NOT NULL DEFAULT '',
            advertised_asin TEXT NOT NULL DEFAULT '',
            purchased_asin TEXT NOT NULL DEFAULT '',
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            changes_json TEXT NOT NULL DEFAULT '[]',
            reason TEXT NOT NULL DEFAULT '',
            reason_status TEXT NOT NULL DEFAULT 'missing',
            strategy TEXT NOT NULL DEFAULT '',
            operator_id TEXT NOT NULL DEFAULT '',
            operator_name TEXT NOT NULL DEFAULT '',
            operated_at REAL NOT NULL,
            operated_at_iso TEXT NOT NULL,
            operated_at_local TEXT NOT NULL DEFAULT '',
            timezone TEXT NOT NULL DEFAULT 'UTC',
            action_date_local TEXT NOT NULL,
            reversal_of TEXT,
            annotations_json TEXT NOT NULL DEFAULT '[]',
            raw_json TEXT NOT NULL DEFAULT '{}',
            ingested_at REAL NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_adjustment_source_event
        ON adjustment_actions(sid, source, source_event_id)
        WHERE source_event_id IS NOT NULL AND source_event_id <> '';

        CREATE UNIQUE INDEX IF NOT EXISTS uq_adjustment_source_fingerprint
        ON adjustment_actions(sid, source, source_fingerprint)
        WHERE source_event_id IS NULL OR source_event_id = '';

        CREATE INDEX IF NOT EXISTS ix_adjustment_time
        ON adjustment_actions(operated_at DESC, id);
        CREATE INDEX IF NOT EXISTS ix_adjustment_object
        ON adjustment_actions(sid, object_type, object_id);
        CREATE INDEX IF NOT EXISTS ix_adjustment_campaign
        ON adjustment_actions(sid, campaign_id, ad_group_id);

        CREATE TABLE IF NOT EXISTS adjustment_scope_asins (
            action_id TEXT NOT NULL REFERENCES adjustment_actions(id) ON DELETE CASCADE,
            asin TEXT NOT NULL,
            role TEXT NOT NULL,
            parent_asin TEXT NOT NULL DEFAULT '',
            snapshot_at REAL NOT NULL,
            PRIMARY KEY(action_id, asin, role)
        );
        CREATE INDEX IF NOT EXISTS ix_adjustment_scope_asin
        ON adjustment_scope_asins(asin, role, action_id);

        CREATE TABLE IF NOT EXISTS adjustment_evidence (
            action_id TEXT PRIMARY KEY REFERENCES adjustment_actions(id) ON DELETE CASCADE,
            evidence_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS adjustment_reviews (
            id TEXT PRIMARY KEY,
            action_id TEXT NOT NULL REFERENCES adjustment_actions(id) ON DELETE CASCADE,
            horizon_days INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            verdict TEXT NOT NULL,
            confidence TEXT NOT NULL,
            sample_confidence TEXT NOT NULL DEFAULT '',
            data_confidence TEXT NOT NULL DEFAULT '',
            mapping_confidence TEXT NOT NULL DEFAULT '',
            comparability_confidence TEXT NOT NULL DEFAULT '',
            window_json TEXT NOT NULL DEFAULT '{}',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            cohorts_json TEXT NOT NULL DEFAULT '{}',
            warnings_json TEXT NOT NULL DEFAULT '[]',
            confounders_json TEXT NOT NULL DEFAULT '[]',
            boundary TEXT NOT NULL DEFAULT '',
            provenance_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            UNIQUE(action_id, horizon_days, revision)
        );
        CREATE INDEX IF NOT EXISTS ix_adjustment_review_action
        ON adjustment_reviews(action_id, horizon_days, revision DESC);

        CREATE TABLE IF NOT EXISTS adjustment_sync_state (
            sid TEXT NOT NULL,
            source TEXT NOT NULL,
            cursor TEXT NOT NULL DEFAULT '',
            last_synced_at REAL NOT NULL,
            detail_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(sid, source)
        );
        """
    )
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(adjustment_scope_asins)")}
    if "parent_asin" not in columns:
        conn.execute("ALTER TABLE adjustment_scope_asins ADD COLUMN parent_asin TEXT NOT NULL DEFAULT ''")
    review_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(adjustment_reviews)")}
    if "confounders_json" not in review_columns:
        conn.execute(
            "ALTER TABLE adjustment_reviews "
            "ADD COLUMN confounders_json TEXT NOT NULL DEFAULT '[]'")


def _require_event(event: dict[str, Any]) -> None:
    missing = [key for key in ("sid", "source", "operated_at") if event.get(key) in (None, "")]
    if missing:
        raise ValueError(f"调整事件缺少必填字段: {', '.join(missing)}")
    scope = event.get("scope_asins")
    if scope is not None and not isinstance(scope, list):
        raise ValueError("scope_asins 必须是数组")
    for item in scope or []:
        if not isinstance(item, dict) or not str(item.get("asin") or "").strip():
            raise ValueError("scope_asins 每项必须包含 asin")
        if str(item.get("role") or "child") not in _VALID_ASIN_ROLES:
            raise ValueError(f"未知 ASIN scope role: {item.get('role')}")


def _insert_scope_rows(conn: sqlite3.Connection, action_id: str, event: dict[str, Any],
                       action_stamp: float) -> int:
    inserted = 0
    for scope in event.get("scope_asins") or []:
        asin = str(scope.get("asin") or "").strip().upper()
        role = str(scope.get("role") or "child")
        if not asin or role not in _VALID_ASIN_ROLES:
            continue
        scope_snapshot_at = action_stamp
        if scope.get("snapshot_at") not in (None, ""):
            scope_snapshot_at, _ = _parse_timestamp(
                scope.get("snapshot_at"), str(event.get("timezone") or "UTC"))
        parent_asin = str(scope.get("parent_asin") or "").upper()
        existing = conn.execute(
            "SELECT parent_asin FROM adjustment_scope_asins "
            "WHERE action_id=? AND asin=? AND role=?", (action_id, asin, role),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO adjustment_scope_asins(action_id,asin,role,parent_asin,snapshot_at)"
                " VALUES(?,?,?,?,?)", (action_id, asin, role, parent_asin, scope_snapshot_at),
            )
            inserted += 1
        elif not str(existing["parent_asin"] or "") and parent_asin:
            conn.execute(
                "UPDATE adjustment_scope_asins SET parent_asin=?,snapshot_at=? "
                "WHERE action_id=? AND asin=? AND role=?",
                (parent_asin, scope_snapshot_at, action_id, asin, role),
            )
            inserted += 1
    return inserted


def _store_evidence(conn: sqlite3.Connection, action_id: str, evidence: Any, *,
                    created_at: float, merge: bool = False) -> None:
    if not isinstance(evidence, dict) or not evidence:
        return
    cleaned = _scrub_sensitive(evidence)
    existing = conn.execute(
        "SELECT evidence_json FROM adjustment_evidence WHERE action_id=?", (action_id,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO adjustment_evidence(action_id,evidence_json,created_at) VALUES(?,?,?)",
            (action_id, _json(cleaned), created_at),
        )
    elif merge:
        merged = _decode(existing["evidence_json"], {})
        if isinstance(merged, dict):
            for key, value in cleaned.items():
                if key not in merged or merged[key] in (None, "", [], {}):
                    merged[key] = value
                    continue
                if key != "scope_mapping" or not isinstance(value, dict):
                    continue
                previous = merged[key] if isinstance(merged[key], dict) else {}
                confidence_rank = {"none": 0, "low": 1, "medium": 2, "high": 3}
                previous_score = (
                    confidence_rank.get(str(previous.get("confidence") or "none"), 0),
                    bool(previous.get("action_time_exact")),
                    -len(previous.get("gaps") or []),
                )
                incoming_score = (
                    confidence_rank.get(str(value.get("confidence") or "none"), 0),
                    bool(value.get("action_time_exact")),
                    -len(value.get("gaps") or []),
                )
                if incoming_score > previous_score:
                    merged[key] = value
            conn.execute(
                "UPDATE adjustment_evidence SET evidence_json=? WHERE action_id=?",
                (_json(merged), action_id),
            )


def import_events(events: Iterable[dict[str, Any]], *, batch: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Import canonical events transactionally and return created/duplicate counts."""
    rows = [dict(row) for row in events]
    if not rows:
        return {"batch_id": "", "created": 0, "duplicates": 0, "enriched": 0,
                "action_ids": []}
    for row in rows:
        _require_event(row)

    batch_data = dict(batch or {})
    sid_values = {str(row["sid"]) for row in rows}
    source_values = {str(row["source"]) for row in rows}
    if len(sid_values) != 1 or len(source_values) != 1:
        raise ValueError("一个导入批次只能包含同一 sid 和 source")
    batch_id = str(batch_data.get("id") or f"adb-{uuid.uuid4().hex}")
    now = time.time()
    created_ids: list[str] = []
    all_ids: list[str] = []
    enriched = 0

    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO adjustment_batches"
            "(id,sid,source,source_batch_id,metadata_json,created_at) VALUES(?,?,?,?,?,?)",
            (batch_id, next(iter(sid_values)), next(iter(source_values)),
             str(batch_data.get("source_batch_id") or "") or None,
             _json(_scrub_sensitive(batch_data.get("metadata") or {})), now),
        )
        stored_batch = conn.execute(
            "SELECT sid,source FROM adjustment_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if stored_batch and (stored_batch["sid"] != next(iter(sid_values))
                             or stored_batch["source"] != next(iter(source_values))):
            raise ValueError("batch id 已属于其他 sid/source")
        for event in rows:
            action_id = str(event.get("id") or f"adj-{uuid.uuid4().hex}")
            source_event_id = str(event.get("source_event_id") or "") or None
            fingerprint = str(event.get("source_fingerprint") or _fingerprint(event))
            timezone_name = str(event.get("timezone") or "UTC")
            stamp, operated_iso = _parse_timestamp(event.get("operated_at"), timezone_name)
            try:
                timezone_value = ZoneInfo(timezone_name)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                # aware ISO 时间本身能解析，但站点日期仍依赖 timezone；不能把坏时区
                # 静默存进去，否则后续 3/7/14/30 天窗口会从错误的 T0 起算。
                raise ValueError(f"未知 IANA 时区: {timezone_name}") from exc
            local_text = str(event.get("operated_at_local") or
                             dt.datetime.fromtimestamp(stamp, timezone_value).isoformat())
            action_date_local = str(event.get("action_date_local") or local_text[:10])
            try:
                dt.date.fromisoformat(action_date_local)
            except ValueError as exc:
                raise ValueError(f"action_date_local 不是有效日期: {action_date_local}") from exc
            reason = _clean_text(event.get("reason"))
            reason_status = str(event.get("reason_status") or ("provided" if reason else "missing"))
            values = (
                action_id, batch_id, str(event["sid"]), str(event.get("marketplace_id") or ""),
                str(event.get("profile_id") or ""), str(event["source"]), source_event_id,
                fingerprint, str(event.get("sponsored_type") or ""),
                str(event.get("operate_type") or ""), str(event.get("action_type") or ""),
                str(event.get("object_type") or ""), str(event.get("object_id") or ""),
                _clean_text(event.get("object_name")), str(event.get("campaign_id") or ""),
                _clean_text(event.get("campaign_name")), str(event.get("ad_group_id") or ""),
                _clean_text(event.get("ad_group_name")), str(event.get("advertised_asin") or ""),
                str(event.get("purchased_asin") or ""),
                _json(_scrub_sensitive(event.get("before") or {})),
                _json(_scrub_sensitive(event.get("after") or {})),
                _json(_scrub_changes(event.get("changes") or [])),
                reason, reason_status, _clean_text(event.get("strategy")),
                _clean_text(event.get("operator_id")), _clean_text(event.get("operator_name")),
                stamp, operated_iso, local_text, timezone_name,
                action_date_local, str(event.get("reversal_of") or "") or None,
                _json([]), _json(_scrub_sensitive(event.get("raw") or {})), now,
            )
            try:
                conn.execute(
                    """INSERT INTO adjustment_actions(
                    id,batch_id,sid,marketplace_id,profile_id,source,source_event_id,
                    source_fingerprint,sponsored_type,operate_type,action_type,object_type,
                    object_id,object_name,campaign_id,campaign_name,ad_group_id,ad_group_name,
                    advertised_asin,purchased_asin,before_json,after_json,changes_json,reason,
                    reason_status,strategy,operator_id,operator_name,operated_at,operated_at_iso,
                    operated_at_local,timezone,action_date_local,reversal_of,annotations_json,
                    raw_json,ingested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    values,
                )
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    """SELECT id FROM adjustment_actions WHERE sid=? AND source=? AND
                    ((? IS NOT NULL AND source_event_id=?) OR
                     (? IS NULL AND (source_event_id IS NULL OR source_event_id='') AND source_fingerprint=?))""",
                    (str(event["sid"]), str(event["source"]), source_event_id, source_event_id,
                     source_event_id, fingerprint),
                ).fetchone()
                if existing is None:
                    raise
                existing_id = str(existing["id"])
                all_ids.append(existing_id)
                scope_state = conn.execute(
                    "SELECT COUNT(*) n,SUM(CASE WHEN role='parent' THEN 1 ELSE 0 END) parents "
                    "FROM adjustment_scope_asins WHERE action_id=?",
                    (existing_id,),
                ).fetchone()
                has_scope = bool(scope_state and int(scope_state["n"] or 0))
                has_parent = bool(scope_state and int(scope_state["parents"] or 0))
                incoming_has_parent = any(
                    str(scope.get("role") or "") == "parent"
                    for scope in (event.get("scope_asins") or []))
                if event.get("scope_asins") and (not has_scope or (not has_parent and incoming_has_parent)):
                    inserted_scope = _insert_scope_rows(conn, existing_id, event, stamp)
                    if inserted_scope:
                        _store_evidence(
                            conn, existing_id, event.get("evidence"), created_at=now, merge=True)
                        enriched += 1
                continue

            created_ids.append(action_id)
            all_ids.append(action_id)
            _insert_scope_rows(conn, action_id, event, stamp)
            _store_evidence(conn, action_id, event.get("evidence"), created_at=now)

        if not created_ids:
            conn.execute(
                "DELETE FROM adjustment_batches WHERE id=? AND NOT EXISTS "
                "(SELECT 1 FROM adjustment_actions WHERE batch_id=?)", (batch_id, batch_id))

    return {
        "batch_id": batch_id if created_ids else "",
        "created": len(created_ids),
        "duplicates": len(rows) - len(created_ids),
        "enriched": enriched,
        "action_ids": all_ids,
    }


def _latest_reviews(conn: sqlite3.Connection, action_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT r.* FROM adjustment_reviews r
        JOIN (SELECT horizon_days, MAX(revision) revision FROM adjustment_reviews
              WHERE action_id=? GROUP BY horizon_days) latest
          ON latest.horizon_days=r.horizon_days AND latest.revision=r.revision
        WHERE r.action_id=? ORDER BY r.horizon_days""", (action_id, action_id),
    ).fetchall()
    return [_review_dict(row) for row in rows]


def _action_dict(conn: sqlite3.Connection, row: sqlite3.Row, *, include_raw: bool = False) -> dict[str, Any]:
    result = dict(row)
    for field, default in (("before_json", {}), ("after_json", {}), ("changes_json", []),
                           ("annotations_json", []), ("raw_json", {})):
        result[field[:-5]] = _decode(result.pop(field), default)
    if not include_raw:
        result.pop("raw", None)
    evidence = conn.execute(
        "SELECT evidence_json FROM adjustment_evidence WHERE action_id=?", (result["id"],)
    ).fetchone()
    result["evidence"] = _decode(evidence["evidence_json"], {}) if evidence else {}
    result["scope_asins"] = [dict(r) for r in conn.execute(
        "SELECT asin,role,parent_asin,snapshot_at FROM adjustment_scope_asins WHERE action_id=? ORDER BY role,asin",
        (result["id"],),
    ).fetchall()]
    result["reviews"] = _latest_reviews(conn, result["id"])
    if result["reason"]:
        result["effective_reason"] = result["reason"]
        result["effective_reason_status"] = result["reason_status"]
    elif result["annotations"]:
        result["effective_reason"] = result["annotations"][-1].get("text", "")
        result["effective_reason_status"] = "annotated"
    else:
        result["effective_reason"] = ""
        result["effective_reason_status"] = "missing"
    return result


def get_action(action_id: str, *, include_raw: bool = False) -> Optional[dict[str, Any]]:
    with _connection() as conn:
        row = conn.execute("SELECT * FROM adjustment_actions WHERE id=?", (action_id,)).fetchone()
        return _action_dict(conn, row, include_raw=include_raw) if row else None


def find_by_source_event(sid: Any, source: str, source_event_id: str) -> Optional[dict[str, Any]]:
    with _connection() as conn:
        row = conn.execute(
            "SELECT * FROM adjustment_actions WHERE sid=? AND source=? AND source_event_id=?",
            (str(sid), str(source), str(source_event_id)),
        ).fetchone()
        return _action_dict(conn, row, include_raw=False) if row else None


def list_actions(*, sid: Any = None, parent_asin: str = "", child_asin: str = "",
                 campaign_id: str = "", object_id: str = "", verdict: str = "",
                 source: str = "", limit: int = 50, before: Optional[float] = None,
                 cursor: str = "", date_from: str = "", date_to: str = "",
                 ad_group_id: str = "", object_type: str = "") -> dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    where: list[str] = []
    values: list[Any] = []
    if sid not in (None, ""):
        where.append("a.sid=?")
        values.append(str(sid))
    for column, value in (
        ("a.campaign_id", campaign_id), ("a.ad_group_id", ad_group_id),
        ("a.object_id", object_id), ("a.object_type", object_type), ("a.source", source),
    ):
        if value:
            where.append(f"{column}=?")
            values.append(str(value))
    for column, value, label in (
        ("a.action_date_local>=?", date_from, "date_from"),
        ("a.action_date_local<=?", date_to, "date_to"),
    ):
        if value:
            try:
                dt.date.fromisoformat(str(value))
            except ValueError as exc:
                raise ValueError(f"{label} 不是有效日期") from exc
            where.append(column)
            values.append(str(value))
    if date_from and date_to and str(date_to) < str(date_from):
        raise ValueError("date_to 不能早于 date_from")
    if cursor and before is not None:
        raise ValueError("cursor 与 before 不能同时使用")
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        # 排序是 operated_at DESC, id ASC；同秒记录必须从最后一个 id 继续。
        where.append("(a.operated_at<? OR (a.operated_at=? AND a.id>?))")
        values.extend((cursor_time, cursor_time, cursor_id))
    elif before is not None:
        if not math.isfinite(float(before)):
            raise ValueError("before 必须是有限数字")
        where.append("a.operated_at<?")
        values.append(float(before))
    if parent_asin:
        where.append(
            "EXISTS(SELECT 1 FROM adjustment_scope_asins s WHERE s.action_id=a.id "
            "AND ((s.asin=? AND s.role='parent') OR s.parent_asin=?))")
        values.extend((parent_asin.upper(), parent_asin.upper()))
    if child_asin:
        where.append("EXISTS(SELECT 1 FROM adjustment_scope_asins s WHERE s.action_id=a.id AND s.asin=? AND s.role<>'parent')")
        values.append(child_asin.upper())
    if verdict:
        where.append("""EXISTS(SELECT 1 FROM adjustment_reviews r WHERE r.action_id=a.id
                     AND r.verdict=? AND r.revision=(SELECT MAX(r2.revision)
                     FROM adjustment_reviews r2 WHERE r2.action_id=r.action_id
                     AND r2.horizon_days=r.horizon_days))""")
        values.append(verdict)
    clause = " WHERE " + " AND ".join(where) if where else ""
    query = f"SELECT a.* FROM adjustment_actions a{clause} ORDER BY a.operated_at DESC,a.id LIMIT ?"
    values.append(limit + 1)
    with _connection() as conn:
        rows = conn.execute(query, values).fetchall()
        has_more = len(rows) > limit
        items = [_action_dict(conn, row, include_raw=False) for row in rows[:limit]]
    return {
        "items": items,
        "has_more": has_more,
        "next_before": items[-1]["operated_at"] if has_more and items else None,
        "next_cursor": (_encode_cursor(items[-1]["operated_at"], items[-1]["id"])
                        if has_more and items else None),
    }


def annotate(action_id: str, text: str, *, operator: str = "", strategy: str = "") -> dict[str, Any]:
    note = _clean_text(text).strip()
    if not note:
        raise ValueError("annotation 不能为空")
    if len(note) > 4000:
        raise ValueError("annotation 最多 4000 字")
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT annotations_json FROM adjustment_actions WHERE id=?", (action_id,)
        ).fetchone()
        if row is None:
            raise KeyError(action_id)
        notes = _decode(row["annotations_json"], [])
        entry = {"text": note, "operator": _clean_text(operator),
                 "strategy": _clean_text(strategy),
                 "created_at": time.time()}
        notes.append(entry)
        conn.execute("UPDATE adjustment_actions SET annotations_json=? WHERE id=?", (_json(notes), action_id))
    return entry


def _review_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for field, default in (("window_json", {}), ("metrics_json", {}), ("cohorts_json", {}),
                           ("warnings_json", []), ("confounders_json", []),
                           ("provenance_json", {})):
        result[field[:-5]] = _decode(result.pop(field), default)
    result["causality_claimed"] = False
    return result


def record_review(action_id: str, horizon_days: int, review: dict[str, Any]) -> dict[str, Any]:
    horizon = int(horizon_days)
    if horizon <= 0:
        raise ValueError("horizon_days 必须大于 0")
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM adjustment_actions WHERE id=?", (action_id,)).fetchone() is None:
            raise KeyError(action_id)
        revision = int(conn.execute(
            "SELECT COALESCE(MAX(revision),0)+1 n FROM adjustment_reviews WHERE action_id=? AND horizon_days=?",
            (action_id, horizon),
        ).fetchone()["n"])
        review_id = f"adr-{uuid.uuid4().hex}"
        conn.execute(
            """INSERT INTO adjustment_reviews(
            id,action_id,horizon_days,revision,verdict,confidence,sample_confidence,
            data_confidence,mapping_confidence,comparability_confidence,window_json,
            metrics_json,cohorts_json,warnings_json,confounders_json,boundary,
            provenance_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (review_id, action_id, horizon, revision, str(review.get("verdict") or "neutral"),
             str(review.get("confidence") or "low"), str(review.get("sample_confidence") or ""),
             str(review.get("data_confidence") or ""), str(review.get("mapping_confidence") or ""),
             str(review.get("comparability_confidence") or ""),
             _json(_scrub_sensitive(review.get("window") or {})),
             _json(_scrub_sensitive(review.get("metrics") or {})),
             _json(_scrub_sensitive(review.get("cohorts") or {})),
             _json(_scrub_sensitive(review.get("warnings") or [])),
             _json(_scrub_sensitive(review.get("confounders") or [])),
             str(_scrub_sensitive(str(review.get("boundary") or ""))),
             _json(_scrub_sensitive(review.get("provenance") or {})), time.time()),
        )
        row = conn.execute("SELECT * FROM adjustment_reviews WHERE id=?", (review_id,)).fetchone()
    return _review_dict(row)


def list_reviews(action_id: str, *, horizon_days: Optional[int] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM adjustment_reviews WHERE action_id=?"
    values: list[Any] = [action_id]
    if horizon_days is not None:
        sql += " AND horizon_days=?"
        values.append(int(horizon_days))
    sql += " ORDER BY horizon_days DESC,revision DESC"
    with _connection() as conn:
        return [_review_dict(row) for row in conn.execute(sql, values).fetchall()]


def due_reviews(*, today: Optional[dt.date] = None,
                horizons: tuple[int, ...] = (3, 7, 14, 30), sid: Any = None,
                exclude_sids: Iterable[Any] = (),
                now: Optional[float] = None, retry_cooldown_hours: float = 24.0,
                max_retry_revisions: int = 5) -> list[dict[str, Any]]:
    current = today or dt.date.today()
    current_time = time.time() if now is None else float(now)
    excluded_values = ([exclude_sids] if isinstance(exclude_sids, (str, bytes))
                       else list(exclude_sids or ()))
    excluded = tuple(dict.fromkeys(str(value) for value in excluded_values))
    conditions: list[str] = []
    values: list[Any] = []
    if sid not in (None, ""):
        conditions.append("sid=?")
        values.append(str(sid))
    if excluded:
        conditions.append(f"sid NOT IN ({','.join('?' for _ in excluded)})")
        values.extend(excluded)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    out: list[dict[str, Any]] = []
    with _connection() as conn:
        actions = conn.execute(
            f"SELECT id,action_date_local FROM adjustment_actions{where}", values
        ).fetchall()
        for action in actions:
            try:
                action_date = dt.date.fromisoformat(str(action["action_date_local"]))
            except ValueError:
                continue
            latest: dict[int, sqlite3.Row] = {}
            for review in conn.execute(
                    "SELECT horizon_days,revision,verdict,created_at FROM adjustment_reviews "
                    "WHERE action_id=? ORDER BY horizon_days,revision DESC", (action["id"],)).fetchall():
                latest.setdefault(int(review["horizon_days"]), review)
            for horizon in horizons:
                mature = current >= action_date + dt.timedelta(days=horizon + 1)
                previous = latest.get(int(horizon))
                if previous is None and mature:
                    out.append({"action_id": str(action["id"]), "horizon_days": horizon})
                elif (mature and previous is not None
                      and str(previous["verdict"]) in {
                          "waiting_for_data", "data_gap", "insufficient_sample"}
                      and int(previous["revision"]) < max(1, int(max_retry_revisions))
                      and current_time - float(previous["created_at"])
                      >= max(1.0, float(retry_cooldown_hours)) * 3600):
                    out.append({"action_id": str(action["id"]),
                                "horizon_days": horizon, "retry": True})
    return out


def summary(*, sid: Any = None, parent_asin: str = "", days: Optional[int] = None) -> dict[str, Any]:
    filters: list[str] = []
    values: list[Any] = []
    if sid not in (None, ""):
        filters.append("a.sid=?")
        values.append(str(sid))
    if parent_asin:
        filters.append(
            "EXISTS(SELECT 1 FROM adjustment_scope_asins s WHERE s.action_id=a.id "
            "AND ((s.asin=? AND s.role='parent') OR s.parent_asin=?))")
        values.extend((parent_asin.upper(), parent_asin.upper()))
    if days is not None:
        filters.append("a.operated_at>=?")
        values.append(time.time() - max(0, int(days)) * 86400)
    clause = " WHERE " + " AND ".join(filters) if filters else ""
    with _connection() as conn:
        actions = int(conn.execute(f"SELECT COUNT(*) n FROM adjustment_actions a{clause}", values).fetchone()["n"])
        missing = int(conn.execute(
            f"SELECT COUNT(*) n FROM adjustment_actions a{clause + (' AND ' if clause else ' WHERE ')}"
            "a.reason_status='missing' AND a.annotations_json='[]'",
            values,
        ).fetchone()["n"])
        # Latest revision of every action/horizon only; restatements must not inflate counts.
        review_rows = conn.execute(
            f"""SELECT r.verdict,COUNT(*) n FROM adjustment_reviews r
            JOIN adjustment_actions a ON a.id=r.action_id
            JOIN (SELECT action_id,horizon_days,MAX(revision) revision FROM adjustment_reviews
                  GROUP BY action_id,horizon_days) latest
              ON latest.action_id=r.action_id AND latest.horizon_days=r.horizon_days
             AND latest.revision=r.revision{clause} GROUP BY r.verdict""", values,
        ).fetchall()
    return {"actions": actions, "missing_reason": missing,
            "verdicts": {str(r["verdict"]): int(r["n"]) for r in review_rows}}


def get_sync_state(sid: Any, source: str) -> dict[str, Any]:
    with _connection() as conn:
        row = conn.execute(
            "SELECT * FROM adjustment_sync_state WHERE sid=? AND source=?", (str(sid), str(source))
        ).fetchone()
    if row is None:
        return {"sid": str(sid), "source": str(source), "cursor": "", "last_synced_at": 0.0, "detail": {}}
    result = dict(row)
    result["detail"] = _decode(result.pop("detail_json"), {})
    return result


def set_sync_state(sid: Any, source: str, *, cursor: str = "", detail: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    stamp = time.time()
    with _connection() as conn:
        conn.execute(
            """INSERT INTO adjustment_sync_state(sid,source,cursor,last_synced_at,detail_json)
            VALUES(?,?,?,?,?) ON CONFLICT(sid,source) DO UPDATE SET cursor=excluded.cursor,
            last_synced_at=excluded.last_synced_at,detail_json=excluded.detail_json""",
            (str(sid), str(source), str(cursor), stamp, _json(_scrub_sensitive(detail or {}))),
        )
    return get_sync_state(sid, source)


def get_source_mode(sid: Any) -> str:
    mode = str(get_sync_state(sid, "_mode").get("detail", {}).get("mode") or "hybrid")
    return mode if mode in {"push", "lingxing", "hybrid"} else "hybrid"


def set_source_mode(sid: Any, mode: str) -> str:
    value = str(mode or "").lower()
    if value not in {"push", "lingxing", "hybrid"}:
        raise ValueError("source_mode 必须是 push、lingxing 或 hybrid")
    set_sync_state(sid, "_mode", cursor=value, detail={"mode": value})
    return value


def _native_time_context(intent: dict[str, Any]) -> dict[str, Any]:
    """Resolve a native write's site clock without doing post-write network I/O."""
    marketplace_id = str(intent.get("marketplace_id") or "")
    requested_timezone = str(intent.get("timezone") or "")
    metadata_source = "intent" if requested_timezone else ""
    store = stores.cached_get(intent.get("sid")) or {}
    if not marketplace_id:
        marketplace_id = str(store.get("marketplace_id") or "")
    timezone_name = requested_timezone
    if not timezone_name and marketplace_id:
        timezone_name = stores.MARKETPLACE_TZ.get(marketplace_id, "")
        if timezone_name:
            metadata_source = "local_store_metadata"
    fallback = False
    if not timezone_name:
        timezone_name, metadata_source, fallback = "UTC", "fallback_utc", True
    try:
        timezone_value = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        timezone_name, timezone_value, metadata_source, fallback = (
            "UTC", dt.timezone.utc, "fallback_utc", True)
    now_utc = dt.datetime.now(dt.timezone.utc)
    mapping = {
        "resolved_timezone": timezone_name,
        "metadata_source": metadata_source,
        "action_time_exact": True,
        "local_date_confidence": "low" if fallback else "high",
    }
    if requested_timezone and fallback:
        mapping["requested_timezone"] = requested_timezone
    if fallback:
        mapping["warning"] = "未从 intent 或本地店铺元数据解析到有效站点时区，T0 日期按 UTC 降级"
    return {
        "marketplace_id": marketplace_id,
        "timezone": timezone_name,
        "operated_at": now_utc.isoformat(),
        "operated_at_local": now_utc.astimezone(timezone_value).isoformat(),
        "evidence": mapping,
    }


def record_native_execution(intent: dict[str, Any], result: dict[str, Any], *,
                            evidence: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Best-effort helper used only after an awen write has succeeded."""
    if not result.get("ok") or result.get("dry_run"):
        return {"created": 0, "duplicates": 0, "enriched": 0, "action_ids": []}
    clock = _native_time_context(intent)
    target = intent.get("target") if isinstance(intent.get("target"), dict) else {}
    before = intent.get("before") if isinstance(intent.get("before"), dict) else {}
    after = (intent.get("after") if isinstance(intent.get("after"), dict)
             else intent.get("change") if isinstance(intent.get("change"), dict) else {})
    op_type = str(intent.get("op_type") or intent.get("operate_type") or "")
    object_type = str(target.get("type") or {
        "campaign_budget": "campaign", "keyword_bid": "keyword",
        "negate_keyword": "negative_keyword",
    }.get(op_type, ""))
    operate_type = {
        "campaign_budget": "campaigns", "keyword_bid": "keywords",
        "negate_keyword": "negativeKeywords",
    }.get(op_type, op_type)
    object_id = (target.get("id") or intent.get("object_id") or intent.get("target_id")
                 or (intent.get("keyword_text") if op_type == "negate_keyword" else ""))
    action_type = op_type
    field_hint = "budget" if op_type == "campaign_budget" else "bid"
    if op_type in {"campaign_budget", "keyword_bid"}:
        old = next((value for key, value in before.items() if field_hint in str(key).lower()), None)
        new = next((value for key, value in after.items() if field_hint in str(key).lower()), None)
        prefix = "budget" if op_type == "campaign_budget" else "bid"
        try:
            old_number, new_number = float(old), float(new)
            if new_number > old_number:
                action_type = f"{prefix}_increase"
            elif new_number < old_number:
                action_type = f"{prefix}_decrease"
            else:
                action_type = f"{prefix}_change"
        except (TypeError, ValueError):
            action_type = f"{prefix}_change"
    elif op_type == "negate_keyword":
        action_type = "negative_add"
    changes = intent.get("changes") or [
        {"field": key, "before": before.get(key), "after": after.get(key)}
        for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)
    ]
    supplied_evidence = evidence or intent.get("evidence") or {}
    event_evidence = dict(supplied_evidence) if isinstance(supplied_evidence, dict) else {
        "decision_evidence": supplied_evidence}
    event_evidence["time_mapping"] = clock["evidence"]
    event = {
        "sid": intent.get("sid"), "marketplace_id": clock["marketplace_id"],
        "profile_id": intent.get("profile_id") or "", "source": "awen",
        "source_event_id": result.get("audit_id") or result.get("request_id"),
        "sponsored_type": intent.get("sponsored_type") or "sp",
        "operate_type": operate_type,
        "action_type": action_type, "object_type": object_type,
        "object_id": object_id,
        "object_name": target.get("name") or intent.get("target_name") or "",
        "campaign_id": (intent.get("campaign_id") or
                        (object_id if op_type == "campaign_budget" else "")),
        "ad_group_id": intent.get("ad_group_id") or "", "advertised_asin": intent.get("asin") or "",
        "before": before, "after": after, "changes": changes,
        "reason": intent.get("reason") or intent.get("rationale") or "",
        "strategy": intent.get("strategy") or intent.get("rule") or "",
        "operator_name": "awen", "operated_at": clock["operated_at"],
        "operated_at_local": clock["operated_at_local"], "timezone": clock["timezone"],
        "evidence": event_evidence,
        "scope_asins": intent.get("scope_asins") or [], "raw": {"result": result},
    }
    return import_events([event])


def record_native_reversal(intent: dict[str, Any], result: dict[str, Any], *,
                           original_audit_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Record an explicit local rollback via its exact audit relation, never fuzzy matching."""
    if not result.get("ok"):
        return {"created": 0, "duplicates": 0, "enriched": 0, "action_ids": []}
    original = find_by_source_event(intent.get("sid"), "awen", original_audit_id)
    clock_intent = dict(intent)
    if original:
        if not clock_intent.get("marketplace_id"):
            clock_intent["marketplace_id"] = original.get("marketplace_id") or ""
        if not clock_intent.get("timezone"):
            clock_intent["timezone"] = original.get("timezone") or ""
    clock = _native_time_context(clock_intent)
    current = (intent.get("change") if isinstance(intent.get("change"), dict) else {})
    event = {
        "sid": intent.get("sid"), "marketplace_id": clock["marketplace_id"],
        "profile_id": original.get("profile_id", "") if original else "", "source": "awen",
        "source_event_id": result.get("audit_id"), "sponsored_type": "sp",
        "operate_type": "rollback", "action_type": "rollback",
        "object_type": original.get("object_type", "") if original else "",
        "object_id": original.get("object_id", "") if original else intent.get("target_id") or "",
        "object_name": original.get("object_name", "") if original else intent.get("target_name") or "",
        "campaign_id": intent.get("campaign_id") or (original.get("campaign_id", "") if original else ""),
        "ad_group_id": intent.get("ad_group_id") or (original.get("ad_group_id", "") if original else ""),
        "advertised_asin": original.get("advertised_asin", "") if original else intent.get("asin") or "",
        "before": current, "after": snapshot,
        "changes": [{"field": key, "before": current.get(key), "after": snapshot.get(key)}
                    for key in sorted(set(current) | set(snapshot))
                    if current.get(key) != snapshot.get(key)],
        "reason": f"显式回滚审计 {original_audit_id}", "strategy": "explicit_rollback",
        "operator_name": "awen", "operated_at": clock["operated_at"],
        "operated_at_local": clock["operated_at_local"], "timezone": clock["timezone"],
        "reversal_of": original.get("id") if original else None,
        "scope_asins": original.get("scope_asins", []) if original else [],
        "evidence": {"rollback_of_audit_id": original_audit_id, "exact_relation": bool(original),
                     "time_mapping": clock["evidence"]},
        "raw": {"result": result},
    }
    return import_events([event])
