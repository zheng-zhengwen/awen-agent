"""LingXing's v2 advertising operation-log adapter."""
from __future__ import annotations

import datetime as dt
import calendar
import hashlib
import json
import time
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import adjustment_scope, adjustments, lingxing_openapi, stores

ROUTE = "/pb/openapi/newad/apiLogStandard"
LOG_SOURCES = {"all", "erp", "amazon"}
SPONSORED_TYPES = {"sp", "sb", "sd"}
OPERATE_TYPES = {
    "campaigns", "adGroups", "productAds", "keywords", "negativeKeywords",
    "targets", "negativeTargets", "profiles",
}

_OBJECT_TYPES = {
    "campaigns": "campaign", "adGroups": "ad_group", "productAds": "product_ad",
    "keywords": "keyword", "negativeKeywords": "negative_keyword",
    "targets": "target", "negativeTargets": "negative_target", "profiles": "profile",
}


def _extract_rows(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
    top_total = payload.get("total") if isinstance(payload, dict) else None

    def parsed_total(candidate: Any) -> int | None:
        try:
            return int(candidate) if candidate is not None else None
        except (TypeError, ValueError):
            return None

    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)], parsed_total(top_total)
    if isinstance(data, dict):
        for key in ("list", "records", "rows", "items"):
            if isinstance(data.get(key), list):
                total = data.get("total")
                if total is None:
                    total = data.get("total_count")
                if total is None:
                    total = top_total
                total_int = parsed_total(total)
                return [row for row in data[key] if isinstance(row, dict)], total_int
    return [], None


def _values(rows: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if code:
            out[code] = item.get("value")
    return out


def _changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"field": key, "before": before.get(key), "after": after.get(key)}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    ]


def _action_type(row: dict[str, Any], changes: list[dict[str, Any]]) -> str:
    function = str(row.get("function_name") or "").lower()
    change = str(row.get("change_type") or "").strip().lower()
    change = {"更新": "update", "创建": "create", "新增": "create",
              "删除": "delete"}.get(change, change)
    operate_type = str(row.get("operate_type") or "")
    fields = {str(item.get("field") or "").lower() for item in changes}

    def changed_value(needle: str, side: str) -> Any:
        return next((item.get(side) for item in changes
                     if needle in str(item.get("field") or "").lower()), None)

    # Negative entities describe lifecycle changes, not bid changes.  Provider
    # rows may still carry inherited bid-like fields, so object semantics win.
    if operate_type in {"negativeKeywords", "negativeTargets"} or any(
            word in function for word in ("否定", "negative")):
        if change in {"create", "add", "insert"}:
            return "negative_add"
        if change in {"delete", "remove"}:
            return "negative_remove"
        return "negative_change"
    if any("bid" in field for field in fields) or "竞价" in function:
        old = changed_value("bid", "before")
        new = changed_value("bid", "after")
        try:
            if float(new) > float(old):
                return "bid_increase"
            if float(new) < float(old):
                return "bid_decrease"
            return "bid_change"
        except (TypeError, ValueError):
            return "bid_change"
    if any("budget" in field for field in fields) or "预算" in function:
        old = changed_value("budget", "before")
        new = changed_value("budget", "after")
        try:
            if float(new) > float(old):
                return "budget_increase"
            if float(new) < float(old):
                return "budget_decrease"
            return "budget_change"
        except (TypeError, ValueError):
            return "budget_change"
    state_after = str(changed_value("state", "after") or changed_value("status", "after") or "").lower()
    if state_after in {"paused", "disabled", "archived", "暂停", "禁用", "归档"}:
        return "pause"
    if state_after in {"enabled", "active", "启用"}:
        return "enable"
    if any(word in function for word in ("暂停", "pause", "disable")):
        return "pause"
    if any(word in function for word in ("启用", "enable")):
        return "enable"
    return change or "update"


def _timezone(name: str) -> tuple[dt.tzinfo, str, bool]:
    try:
        return ZoneInfo(name), name, False
    except (ZoneInfoNotFoundError, ValueError):
        return dt.timezone.utc, "UTC", True


def _parse_local(value: Any, timezone_name: str) -> tuple[str, str, str, bool]:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("领星操作日志缺少 operate_time")
    parsed: dt.datetime | None = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"无法解析领星 operate_time: {raw}") from exc
    timezone_value, resolved_name, fallback = _timezone(timezone_name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone_value)
    else:
        # 有偏移的 provider 时间代表一个精确时刻；仍要投影到站点时区，供 T0
        # 本地日期使用。固定偏移名（例如 UTC+08:00）不是 IANA key，不能直接入库。
        parsed = parsed.astimezone(timezone_value)
    return parsed.isoformat(), parsed.isoformat(), resolved_name, fallback


def normalize_row(row: dict[str, Any], *, sid: Any, timezone_name: str,
                  log_source: str = "all", request_id: str = "") -> dict[str, Any]:
    before = _values(row.get("operate_before"))
    after = _values(row.get("operate_after"))
    changes = _changes(before, after)
    operated_at, local, resolved_timezone, timezone_fallback = _parse_local(
        row.get("operate_time"), timezone_name)
    operate_type = str(row.get("operate_type") or "")
    material = {
        key: row.get(key) for key in (
            "profile_id", "sponsored_type", "operate_type", "campaign_id", "ad_group_id",
            "object_id", "function_name", "change_type", "operate_before", "operate_after",
            "user_id", "operate_time",
        )
    }
    fingerprint = hashlib.sha256(
        json.dumps(adjustments.scrub_sensitive(material), ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    event = {
        "sid": str(sid), "profile_id": str(row.get("profile_id") or ""), "source": "lingxing",
        "source_fingerprint": fingerprint, "sync_request_id": str(request_id or ""),
        "log_source": log_source, "sponsored_type": str(row.get("sponsored_type") or ""),
        "operate_type": operate_type, "action_type": _action_type(row, changes),
        "object_type": _OBJECT_TYPES.get(operate_type, operate_type or "unknown"),
        "object_id": str(row.get("object_id") or ""), "object_name": str(row.get("object_name") or ""),
        "campaign_id": str(row.get("campaign_id") or ""),
        "campaign_name": str(row.get("campaign_name") or ""),
        "ad_group_id": str(row.get("ad_group_id") or ""),
        "ad_group_name": str(row.get("ad_group_name") or ""),
        "advertised_asin": str(row.get("advertised_asin") or row.get("asin") or ""),
        "before": before, "after": after, "changes": changes,
        "operator_id": str(row.get("user_id") or ""),
        "operator_name": str(row.get("user_name") or ""),
        "operated_at": operated_at, "operated_at_local": local, "timezone": resolved_timezone,
        "timezone_fallback": timezone_fallback,
        "reason_status": "missing", "raw": dict(row),
    }
    if timezone_fallback:
        event["evidence"] = {"time_mapping": {
            "requested_timezone": timezone_name, "resolved_timezone": "UTC",
            "confidence": "low", "warning": "未知站点时区，按 UTC 解释 operate_time",
        }}
    return event


class LingxingOperationSource:
    name = "lingxing"

    def __init__(self, *, page_size: int = 100, max_pages: int = 100) -> None:
        self.page_size = max(1, min(int(page_size), 500))
        self.max_pages = max(1, int(max_pages))

    @staticmethod
    def _validate(log_source: str, sponsored_type: str, operate_type: str,
                  start_date: str, end_date: str) -> tuple[dt.date, dt.date]:
        if log_source not in LOG_SOURCES:
            raise ValueError(f"log_source 必须是 {sorted(LOG_SOURCES)}")
        if sponsored_type not in SPONSORED_TYPES:
            raise ValueError(f"sponsored_type 必须是 {sorted(SPONSORED_TYPES)}")
        if operate_type not in OPERATE_TYPES:
            raise ValueError(f"operate_type 必须是 {sorted(OPERATE_TYPES)}")
        start, end = dt.date.fromisoformat(start_date), dt.date.fromisoformat(end_date)
        if end < start:
            raise ValueError("结束日期不能早于开始日期")
        # 文档示例明确允许 10-01 到 11-01，因此按「下一自然月同日」而非固定 30 天判断。
        next_month = 1 if start.month == 12 else start.month + 1
        next_year = start.year + 1 if start.month == 12 else start.year
        max_day = min(start.day, calendar.monthrange(next_year, next_month)[1])
        max_end = dt.date(next_year, next_month, max_day)
        if end > max_end:
            raise ValueError("操作日志单次查询不能超过一个月")
        return start, end

    def validate_request(self, *, sid: Any, log_source: str,
                         sponsored_types: Iterable[str], operate_types: Iterable[str],
                         start_date: str, end_date: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        try:
            numeric_sid = int(str(sid))
        except (TypeError, ValueError) as exc:
            raise ValueError("sid 必须是正整数") from exc
        if numeric_sid <= 0:
            raise ValueError("sid 必须是正整数")
        if isinstance(sponsored_types, (str, bytes)) or isinstance(operate_types, (str, bytes)):
            raise ValueError("sponsored_types 与 operate_types 必须是字符串数组")
        try:
            sponsored = tuple(sponsored_types)
            operated = tuple(operate_types)
        except TypeError as exc:
            raise ValueError("sponsored_types 与 operate_types 必须是字符串数组") from exc
        if not sponsored or not operated:
            raise ValueError("sponsored_types 与 operate_types 不能为空")
        if not all(isinstance(value, str) for value in sponsored + operated):
            raise ValueError("sponsored_types 与 operate_types 必须是字符串数组")
        for sponsored_type in sponsored:
            for operate_type in operated:
                self._validate(log_source, sponsored_type, operate_type, start_date, end_date)
        return sponsored, operated

    def fetch_dimension(self, sid: Any, log_source: str, sponsored_type: str,
                        operate_type: str, start_date: str, end_date: str,
                        timezone_name: str) -> list[dict[str, Any]]:
        self._validate(log_source, sponsored_type, operate_type, start_date, end_date)
        out: list[dict[str, Any]] = []
        offset = 0
        for _ in range(self.max_pages):
            params = {
                "sid": int(sid), "log_source": log_source, "sponsored_type": sponsored_type,
                "operate_type": operate_type, "start_date": start_date, "end_date": end_date,
                "offset": offset, "length": self.page_size,
            }
            payload = lingxing_openapi.call(
                ROUTE, params, method="POST", headers={"X-API-VERSION": "2"})
            rows, total = _extract_rows(payload)
            request_id = str(payload.get("request_id") or "") if isinstance(payload, dict) else ""
            for row in rows:
                provider_row = dict(row)
                provider_row.setdefault("sponsored_type", sponsored_type)
                provider_row.setdefault("operate_type", operate_type)
                out.append(normalize_row(provider_row, sid=sid, timezone_name=timezone_name,
                                         log_source=log_source, request_id=request_id))
            offset += len(rows)
            if not rows or len(rows) < self.page_size or (total is not None and offset >= total):
                break
        else:
            raise RuntimeError(f"领星操作日志分页超过安全上限 {self.max_pages}")
        return out

    def sync(self, *, sid: Any, start_date: str, end_date: str, log_source: str = "all",
             sponsored_types: Iterable[str] = ("sp",),
             operate_types: Iterable[str] = (
                 "campaigns", "adGroups", "productAds", "keywords", "negativeKeywords",
                 "targets", "negativeTargets"),
             timezone_name: str = "", force: bool = False) -> dict[str, Any]:
        # 即使 push/hybrid 会跳过网络，也先拒绝坏请求，避免配置错误被伪装成成功。
        sponsored_types, operate_types = self.validate_request(
            sid=sid, log_source=log_source, sponsored_types=sponsored_types,
            operate_types=operate_types, start_date=start_date, end_date=end_date)
        mode = adjustments.get_source_mode(sid)
        push_state = adjustments.get_sync_state(sid, "push")
        push_fresh = (dt.datetime.now(dt.timezone.utc).timestamp()
                      - float(push_state.get("last_synced_at") or 0)) < 36 * 3600
        if not force and (mode == "push" or (mode == "hybrid" and push_fresh)):
            reason = ("店铺配置为 push 模式" if mode == "push"
                      else "hybrid 模式检测到 36 小时内的上游推送，领星兜底无需运行")
            return {"ok": True, "skipped": True, "reason": reason, "source_mode": mode,
                    "created": 0, "duplicates": 0, "enriched": 0,
                    "action_ids": [], "failures": []}
        store = stores.get(sid) or {}
        marketplace_timezone = stores.MARKETPLACE_TZ.get(
            str(store.get("marketplace_id") or ""), "")
        requested_timezone = timezone_name or marketplace_timezone
        _, resolved_timezone, timezone_fallback = _timezone(requested_timezone)
        marketplace_id = str(store.get("marketplace_id") or "")
        imported = 0
        duplicates = 0
        enriched = 0
        action_ids: list[str] = []
        failures: list[dict[str, str]] = []
        successful_dimensions: list[dict[str, str]] = []
        request_ids: set[str] = set()
        scope_index: dict[str, Any] | None = None
        scope_error = ""
        scope_captured_at = 0.0
        for sponsored_type in sponsored_types:
            for operate_type in operate_types:
                try:
                    events = self.fetch_dimension(
                        sid, log_source, sponsored_type, operate_type, start_date, end_date,
                        requested_timezone)
                    for event in events:
                        event["marketplace_id"] = marketplace_id
                    if events:
                        if scope_index is None:
                            scope_captured_at = time.time()
                            try:
                                scope_index = adjustment_scope.build_scope_index(sid)
                            except Exception as exc:  # noqa: BLE001 - facts survive mapping failure
                                scope_error = str(adjustments.scrub_sensitive(
                                    f"{type(exc).__name__}: {exc}"))
                                scope_index = {
                                    "ad_to_asin": {}, "campaign_to_asins": {},
                                    "ad_group_to_asins": {}, "parent_by_child": {},
                                    "children_by_parent": {}, "mapping_confidence": "none",
                                    "gaps": [scope_error],
                                }
                        for event in events:
                            scope = adjustment_scope.scope_for_event(event, scope_index)
                            if scope:
                                for item in scope:
                                    item["snapshot_at"] = scope_captured_at
                                event["scope_asins"] = scope
                            evidence = dict(event.get("evidence") or {})
                            mapping_gaps = list(scope_index["gaps"])
                            mapping_confidence = scope_index["mapping_confidence"]
                            if not scope:
                                mapping_confidence = "none"
                                mapping_gaps.append("未能把该动作映射到投放子 ASIN")
                            evidence["scope_mapping"] = {
                                "confidence": mapping_confidence,
                                "gaps": mapping_gaps,
                                "basis": "ingestion_snapshot",
                                "captured_at": scope_captured_at,
                                "action_time_exact": False,
                            }
                            event["evidence"] = evidence
                    request_ids.update(str(e.get("sync_request_id")) for e in events if e.get("sync_request_id"))
                    # Transport request ids are deliberately removed from persisted business identity.
                    for event in events:
                        event.pop("sync_request_id", None)
                    result = adjustments.import_events(events) if events else {
                        "created": 0, "duplicates": 0, "enriched": 0, "action_ids": []}
                    imported += int(result["created"])
                    duplicates += int(result["duplicates"])
                    enriched += int(result.get("enriched") or 0)
                    action_ids.extend(result["action_ids"])
                    successful_dimensions.append({
                        "sponsored_type": sponsored_type, "operate_type": operate_type})
                except Exception as exc:  # noqa: BLE001 - isolate every provider dimension
                    failures.append({"sponsored_type": sponsored_type, "operate_type": operate_type,
                                     "error": str(adjustments.scrub_sensitive(str(exc)))})
        timezone_warning = (
            "未从请求或店铺 marketplace 映射到有效 IANA 时区，日志裸时间按 UTC 降级"
            if timezone_fallback else "")
        detail = {"start_date": start_date, "end_date": end_date, "log_source": log_source,
                  "request_ids": sorted(request_ids), "failures": failures,
                  "successful_dimensions": successful_dimensions,
                  "created": imported, "duplicates": duplicates, "enriched": enriched,
                  "scope_mapping": ({"confidence": scope_index["mapping_confidence"],
                                     "gaps": scope_index["gaps"]} if scope_index else {}),
                  "scope_error": scope_error, "timezone": resolved_timezone,
                  "timezone_warning": timezone_warning}
        adjustments.set_sync_state(sid, self.name, cursor=end_date, detail=detail)
        return {"ok": not failures,
                "partial": bool(failures) and bool(successful_dimensions),
                "created": imported, "duplicates": duplicates, "enriched": enriched,
                "action_ids": action_ids,
                "failures": failures, "successful_dimensions": successful_dimensions,
                "timezone": resolved_timezone, "timezone_warning": timezone_warning,
                "source_mode": mode, "skipped": False}
