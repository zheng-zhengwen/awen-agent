from __future__ import annotations

import pytest


def _row(**overrides):
    row = {
        "profile_id": "p1", "sponsored_type": "sp", "operate_type": "keywords",
        "campaign_id": "c1", "campaign_name": "Campaign",
        "ad_group_id": "g1", "ad_group_name": "Group",
        "object_id": "k1", "object_name": "bottle",
        "function_name": "修改关键词竞价", "change_type": "update",
        "operate_before": [{"code": "bid", "value": "1.20"}],
        "operate_after": [{"code": "bid", "value": "0.90"}],
        "user_id": "u1", "user_name": "wen", "operate_time": "2026-08-01 10:30:00",
    }
    row.update(overrides)
    return row


def test_normalize_retains_provider_fields_and_uses_site_timezone():
    from awen_agent.operation_sources.lingxing import normalize_row

    event = normalize_row(_row(extra="kept"), sid=1001, timezone_name="America/Los_Angeles")
    assert event["object_type"] == "keyword"
    assert event["before"] == {"bid": "1.20"}
    assert event["after"] == {"bid": "0.90"}
    assert event["changes"] == [{"field": "bid", "before": "1.20", "after": "0.90"}]
    assert event["operated_at"].endswith("-07:00")
    assert event["raw"]["extra"] == "kept"
    assert event.get("source_event_id") in (None, "")


def test_aware_provider_time_is_projected_to_iana_site_timezone():
    from awen_agent.operation_sources.lingxing import normalize_row

    event = normalize_row(
        _row(operate_time="2026-08-01T01:30:00+00:00"),
        sid=1001, timezone_name="America/Los_Angeles")
    assert event["operated_at"].startswith("2026-07-31T18:30:00")
    assert event["operated_at_local"].startswith("2026-07-31T18:30:00")
    assert event["timezone"] == "America/Los_Angeles"


def test_unknown_timezone_falls_back_with_explicit_low_confidence_warning():
    from awen_agent.operation_sources.lingxing import normalize_row

    event = normalize_row(_row(), sid=1001, timezone_name="")
    assert event["timezone"] == "UTC"
    mapping = event["evidence"]["time_mapping"]
    assert mapping["confidence"] == "low"
    assert mapping["requested_timezone"] == ""


def test_request_id_is_not_used_as_event_identity():
    from awen_agent.operation_sources.lingxing import normalize_row

    a = normalize_row(_row(), sid=1001, timezone_name="UTC", request_id="req-a")
    b = normalize_row(_row(), sid=1001, timezone_name="UTC", request_id="req-b")
    assert a["source_fingerprint"] == b["source_fingerprint"]
    assert a["sync_request_id"] == "req-a"


def test_fetch_uses_v2_header_and_offset_pagination(monkeypatch):
    from awen_agent.operation_sources import lingxing

    calls = []

    def fake_call(route, params, *, method, headers):
        calls.append((route, dict(params), method, dict(headers)))
        offset = params["offset"]
        rows = [_row(object_id=f"k{offset + i}") for i in range(2 if offset == 0 else 1)]
        return {"code": 0, "request_id": f"r{offset}", "data": {"list": rows, "total": 3}}

    monkeypatch.setattr(lingxing.lingxing_openapi, "call", fake_call)
    source = lingxing.LingxingOperationSource(page_size=2)
    rows = source.fetch_dimension(
        sid=1001, log_source="all", sponsored_type="sp", operate_type="keywords",
        start_date="2026-08-01", end_date="2026-08-02", timezone_name="UTC")

    assert len(rows) == 3
    assert [c[1]["offset"] for c in calls] == [0, 2]
    assert all(c[3] == {"X-API-VERSION": "2"} for c in calls)
    assert all(c[0] == "/pb/openapi/newad/apiLogStandard" for c in calls)


def test_fetch_rejects_invalid_dimensions_and_overlong_window():
    from awen_agent.operation_sources.lingxing import LingxingOperationSource

    source = LingxingOperationSource()
    with pytest.raises(ValueError, match="log_source"):
        source.fetch_dimension(1, "bad", "sp", "keywords", "2026-08-01", "2026-08-02", "UTC")
    with pytest.raises(ValueError, match="一个月"):
        source.fetch_dimension(1, "all", "sp", "keywords", "2026-08-01", "2026-09-02", "UTC")
    with pytest.raises(ValueError, match="结束日期"):
        source.fetch_dimension(1, "all", "sp", "keywords", "2026-08-02", "2026-08-01", "UTC")


def test_documented_one_calendar_month_boundary_is_allowed(monkeypatch):
    from awen_agent.operation_sources import lingxing

    monkeypatch.setattr(
        lingxing.lingxing_openapi, "call",
        lambda *args, **kwargs: {"code": 0, "total": 0, "data": []},
    )
    rows = lingxing.LingxingOperationSource().fetch_dimension(
        1, "all", "sp", "campaigns", "2026-08-01", "2026-09-01", "UTC")
    assert rows == []


def test_unknown_before_after_codes_are_retained():
    from awen_agent.operation_sources.lingxing import normalize_row

    event = normalize_row(_row(
        operate_before=[{"code": "future_provider_code", "value": "x"}],
        operate_after=[{"code": "future_provider_code", "value": "y"}],
    ), sid=1, timezone_name="UTC")
    assert event["changes"][0] == {"field": "future_provider_code", "before": "x", "after": "y"}


def test_documented_budget_amount_code_is_classified_by_direction():
    """领星文档的 function_name 是 WebPage，动作语义必须从 code 判断。"""
    from awen_agent.operation_sources.lingxing import normalize_row

    event = normalize_row(_row(
        operate_type="campaigns",
        function_name="WebPage",
        change_type="更新",
        operate_before=[{"code": "BUDGET_AMOUNT", "value": "1.01"}],
        operate_after=[{"code": "BUDGET_AMOUNT", "value": "1.02"}],
    ), sid=1, timezone_name="UTC")
    assert event["action_type"] == "budget_increase"


def test_negative_delete_is_classified_as_removal():
    from awen_agent.operation_sources.lingxing import normalize_row

    event = normalize_row(_row(
        operate_type="negativeKeywords", function_name="WebPage", change_type="删除",
    ), sid=1, timezone_name="UTC")
    assert event["action_type"] == "negative_remove"


def test_extract_rows_uses_documented_top_level_total():
    from awen_agent.operation_sources.lingxing import _extract_rows

    rows, total = _extract_rows({"code": 0, "total": 8, "data": [_row()]})
    assert len(rows) == 1
    assert total == 8


def test_hybrid_mode_skips_lingxing_when_push_is_fresh(awen_home, monkeypatch):
    from awen_agent import adjustments
    from awen_agent.operation_sources import lingxing

    adjustments.set_source_mode(1, "hybrid")
    adjustments.set_sync_state(1, "push", detail={"received": 1})
    called = []
    monkeypatch.setattr(lingxing.lingxing_openapi, "call", lambda *a, **k: called.append(1))
    result = lingxing.LingxingOperationSource().sync(
        sid=1, start_date="2026-08-01", end_date="2026-08-02", timezone_name="UTC")
    assert result["ok"] is True and result["skipped"] is True
    assert called == []


def test_sync_validates_dimensions_even_when_push_mode_would_skip(awen_home):
    from awen_agent import adjustments
    from awen_agent.operation_sources.lingxing import LingxingOperationSource

    adjustments.set_source_mode(1, "push")
    with pytest.raises(ValueError, match="operate_type"):
        LingxingOperationSource().sync(
            sid=1, start_date="2026-08-01", end_date="2026-08-02",
            sponsored_types=["sp"], operate_types=["not-a-dimension"])


def test_sync_rejects_invalid_sid_and_empty_dimensions_before_work(awen_home):
    from awen_agent.operation_sources.lingxing import LingxingOperationSource

    source = LingxingOperationSource()
    with pytest.raises(ValueError, match="sid 必须是正整数"):
        source.sync(sid="store-name", start_date="2026-08-01", end_date="2026-08-02")
    with pytest.raises(ValueError, match="不能为空"):
        source.sync(sid=1, start_date="2026-08-01", end_date="2026-08-02",
                    sponsored_types=[], operate_types=["keywords"])
    with pytest.raises(ValueError, match="必须是字符串数组"):
        source.sync(sid=1, start_date="2026-08-01", end_date="2026-08-02",
                    sponsored_types=None, operate_types=["keywords"])


def test_empty_successful_dimension_still_makes_sync_partial(awen_home, monkeypatch):
    from awen_agent.operation_sources import lingxing

    source = lingxing.LingxingOperationSource()

    def fake_fetch(_sid, _log_source, _sponsored_type, operate_type, *_rest):
        if operate_type == "targets":
            raise RuntimeError("one dimension failed")
        return []

    monkeypatch.setattr(source, "fetch_dimension", fake_fetch)
    result = source.sync(
        sid=1, start_date="2026-08-01", end_date="2026-08-02",
        sponsored_types=["sp"], operate_types=["keywords", "targets"], force=True)

    assert result["ok"] is False
    assert result["partial"] is True
    assert result["successful_dimensions"] == [
        {"sponsored_type": "sp", "operate_type": "keywords"}]


def test_sync_exposes_unknown_store_timezone_fallback(awen_home, monkeypatch):
    from awen_agent.operation_sources import lingxing

    source = lingxing.LingxingOperationSource()
    monkeypatch.setattr(lingxing.stores, "get", lambda sid: {})
    monkeypatch.setattr(source, "fetch_dimension", lambda *args, **kwargs: [])
    result = source.sync(
        sid=1, start_date="2026-08-01", end_date="2026-08-02",
        sponsored_types=["sp"], operate_types=["keywords"], force=True)

    assert result["timezone"] == "UTC"
    assert "UTC" in result["timezone_warning"]


def test_scope_failure_keeps_event_and_records_mapping_gap(awen_home, monkeypatch):
    from awen_agent import adjustment_scope, adjustments
    from awen_agent.operation_sources import lingxing

    event = lingxing.normalize_row(_row(), sid=1, timezone_name="UTC")
    source = lingxing.LingxingOperationSource()
    monkeypatch.setattr(source, "fetch_dimension", lambda *args, **kwargs: [dict(event)])
    monkeypatch.setattr(lingxing.stores, "get", lambda sid: {"marketplace_id": "ATVPDKIKX0DER"})
    monkeypatch.setattr(
        adjustment_scope, "build_scope_index",
        lambda sid: (_ for _ in ()).throw(RuntimeError("scope unavailable")),
    )

    result = source.sync(
        sid=1, start_date="2026-08-01", end_date="2026-08-02",
        sponsored_types=["sp"], operate_types=["keywords"], force=True)
    detail = adjustments.get_action(result["action_ids"][0])
    assert result["created"] == 1
    assert detail["evidence"]["scope_mapping"]["confidence"] == "none"
    assert detail["evidence"]["scope_mapping"]["basis"] == "ingestion_snapshot"
    assert detail["evidence"]["scope_mapping"]["action_time_exact"] is False
    assert "scope unavailable" in " ".join(detail["evidence"]["scope_mapping"]["gaps"])


def test_sync_redacts_credentials_from_failure_response_and_state(awen_home, monkeypatch):
    import json
    from awen_agent import adjustments
    from awen_agent.operation_sources import lingxing

    source = lingxing.LingxingOperationSource()
    monkeypatch.setattr(lingxing.stores, "get", lambda sid: {})
    monkeypatch.setattr(
        source, "fetch_dimension",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("https://api.test?access_token=token-secret&sign=signature-secret")),
    )
    result = source.sync(
        sid=1, start_date="2026-08-01", end_date="2026-08-02",
        sponsored_types=["sp"], operate_types=["keywords"], force=True)
    serialized = json.dumps({
        "result": result,
        "state": adjustments.get_sync_state(1, "lingxing"),
    })
    assert "token-secret" not in serialized
    assert "signature-secret" not in serialized
    assert "[REDACTED]" in serialized
