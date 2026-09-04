"""领星写入：请求体形状、幅度硬闸、operate 开关、候选转 intent、回滚。

全程 dry-run / monkeypatch，绝不对真实领星发写请求。
"""
from __future__ import annotations

import pytest


def _lw():
    from awen_agent import lingxing_write
    return lingxing_write


# ── build_body 黄金形状（逐字段对齐 awen-ops）──────────────────────────────
def test_build_body_negate(awen_home):
    lw = _lw()
    body = lw.build_body({"op_type": "negate_keyword", "sid": 1876, "campaign_id": "C1",
                          "keyword_text": "junk term", "match_type": "negativeExact"})
    assert body == {"sid": 1876, "negativeKeywords": [
        {"campaignId": "C1", "keyword": "junk term", "matchType": "negativeExact", "state": "ENABLED"}]}


def test_build_body_negate_adgroup(awen_home):
    lw = _lw()
    body = lw.build_body({"op_type": "negate_keyword", "sid": 1, "campaign_id": "C1",
                          "ad_group_id": "G9", "keyword_text": "x", "match_type": "negativePhrase"})
    assert body["negativeKeywords"][0]["adGroupId"] == "G9"


def test_build_body_keyword_bid(awen_home):
    lw = _lw()
    body = lw.build_body({"op_type": "keyword_bid", "sid": 1876, "target_id": "123",
                          "change": {"bid": 0.85}})
    assert body == {"sid": 1876, "keywords": [{"keywordId": 123, "isBaseValue": 0, "bid": 0.85}]}


def test_build_body_campaign_budget_nested(awen_home):
    lw = _lw()
    body = lw.build_body({"op_type": "campaign_budget", "sid": 1876, "target_id": "55",
                          "change": {"daily_budget": 12.0}})
    assert body == {"sid": 1876, "campaigns": [
        {"campaignId": 55, "isBaseValue": 0, "budget": {"budgetType": "DAILY", "budget": 12.0}}]}


# ── 幅度硬闸 ─────────────────────────────────────────────────────────────────
def test_magnitude_over_20pct_blocked(awen_home):
    lw = _lw()
    ok, why = lw.magnitude_ok({"op_type": "keyword_bid", "before": {"bid": 1.0}, "change": {"bid": 0.7}})
    assert not ok and "20%" in why


def test_magnitude_within_ok(awen_home):
    lw = _lw()
    ok, _ = lw.magnitude_ok({"op_type": "keyword_bid", "before": {"bid": 1.0}, "change": {"bid": 0.85}})
    assert ok


# ── operate 开关（默认关）──────────────────────────────────────────────────────
def test_operate_default_off(awen_home):
    lw = _lw()
    assert lw.operate_active() is False


def test_real_write_blocked_when_switch_off(awen_home, monkeypatch):
    lw = _lw()
    called = {"n": 0}
    monkeypatch.setattr(lw, "call", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"code": 0})
    r = lw.execute({"op_type": "negate_keyword", "sid": 1, "campaign_id": "C1",
                    "keyword_text": "x", "match_type": "negativeExact"}, dry_run=False)
    assert r["ok"] is False and "operate" in r["detail"]
    assert called["n"] == 0  # 绝不调用真实写接口


def test_dry_run_returns_body_no_call(awen_home, monkeypatch):
    lw = _lw()
    monkeypatch.setattr(lw, "call", lambda *a, **k: pytest.fail("dry-run 不应调用 call"))
    r = lw.execute({"op_type": "keyword_bid", "sid": 1, "target_id": "9",
                    "before": {"bid": 1.0}, "change": {"bid": 0.85}}, dry_run=True)
    assert r["ok"] and r["dry_run"] and r["body"]["keywords"][0]["bid"] == 0.85


# ── 候选 → intent ────────────────────────────────────────────────────────────
def test_candidate_to_intent_negate(awen_home):
    lw = _lw()
    cand = {"op_type": "negate_keyword", "sid": 1876, "campaign_id": "C1", "target_name": "junk"}
    intent = lw.candidate_to_intent(cand)
    assert intent["keyword_text"] == "junk" and intent["match_type"] == "negativeExact"


def test_candidate_to_intent_preserves_site_context(awen_home):
    lw = _lw()
    intent = lw.candidate_to_intent({
        "op_type": "keyword_bid", "sid": 1876, "target_id": "k1",
        "current": {"bid": 1.0}, "proposed": {"bid": 0.9},
        "marketplace_id": "ATVPDKIKX0DER", "profile_id": "p1",
        "timezone": "America/Los_Angeles",
    })
    assert intent["marketplace_id"] == "ATVPDKIKX0DER"
    assert intent["profile_id"] == "p1"
    assert intent["timezone"] == "America/Los_Angeles"


def test_scope_enrichment_fetches_once_per_store_and_freezes_decision_snapshot(
        awen_home, monkeypatch):
    lw = _lw()
    calls = []
    index = {
        "ad_to_asin": {},
        "campaign_to_asins": {"c1": {"A1", "A2"}},
        "ad_group_to_asins": {"g1": {"A1"}},
        "parent_by_child": {"A1": "P1", "A2": "P1"},
        "children_by_parent": {"P1": {"A1", "A2"}},
        "mapping_confidence": "high", "gaps": [],
    }
    monkeypatch.setattr(
        lw.adjustment_scope, "build_scope_index",
        lambda sid: calls.append(sid) or index,
    )
    intents = [
        {"sid": 1, "op_type": "keyword_bid", "target_id": "k1",
         "campaign_id": "c1", "ad_group_id": "g1", "evidence": {"orders": 4}},
        {"sid": 1, "op_type": "campaign_budget", "target_id": "c1",
         "campaign_id": "c1"},
    ]

    result = lw.enrich_intents_with_scope(intents)

    assert calls == [1]
    first_roles = {row["asin"]: row["role"] for row in result[0]["scope_asins"]}
    assert first_roles == {"P1": "parent", "A1": "advertised_child",
                           "A2": "unadvertised_sibling"}
    assert result[0]["evidence"]["orders"] == 4
    mapping = result[0]["evidence"]["scope_mapping"]
    assert mapping["basis"] == "decision_snapshot"
    assert mapping["action_time_exact"] is False


def test_scope_enrichment_failure_keeps_intent_and_redacts_error(awen_home, monkeypatch):
    lw = _lw()
    monkeypatch.setattr(
        lw.adjustment_scope, "build_scope_index",
        lambda sid: (_ for _ in ()).throw(RuntimeError("access_token=scope-secret")),
    )
    intent = {"sid": 1, "op_type": "keyword_bid", "target_id": "k1"}

    result = lw.enrich_intents_with_scope([intent])[0]

    assert "scope_asins" not in result
    mapping = result["evidence"]["scope_mapping"]
    assert mapping["confidence"] == "none"
    assert "scope-secret" not in str(mapping)


def test_candidate_harvest_not_writable(awen_home):
    lw = _lw()
    assert lw.candidate_to_intent({"op_type": "add_keyword", "sid": 1}) is None


# ── 执行 + 回滚（monkeypatch，开关开）──────────────────────────────────────────
def test_execute_and_rollback_bid(awen_home, monkeypatch):
    lw = _lw()
    lw.set_operate(True)
    sent = []
    monkeypatch.setattr(lw, "call", lambda route, body, **k: sent.append((route, body)) or {"code": 0, "data": {}})
    monkeypatch.setattr(lw, "_current_value", lambda intent: {"bid": 1.0, "state": "enabled"})
    r = lw.execute({"op_type": "keyword_bid", "sid": 1876, "target_id": "123",
                    "target_name": "kw", "before": {"bid": 1.0}, "change": {"bid": 0.85}}, dry_run=False)
    assert r["ok"] and not r["dry_run"] and r["audit_id"]
    assert sent[0][1]["keywords"][0]["bid"] == 0.85
    # 回滚 → 用 snapshot 的旧值 1.0
    rb = lw.rollback(r["audit_id"])
    assert rb["ok"], rb
    assert sent[-1][1]["keywords"][0]["bid"] == 1.0
    from awen_agent import adjustments
    rows = adjustments.list_actions(sid=1876)["items"]
    assert [row["action_type"] for row in rows] == ["rollback", "bid_decrease"]
    assert rows[0]["reversal_of"] == rows[1]["id"]


def test_successful_write_reports_ledger_sidecar_failure(awen_home, monkeypatch):
    lw = _lw()
    from awen_agent import adjustments

    lw.set_operate(True)
    monkeypatch.setattr(lw, "call", lambda *args, **kwargs: {"code": 0, "data": {}})
    monkeypatch.setattr(lw, "_current_value", lambda intent: {"bid": 1.0})
    monkeypatch.setattr(
        adjustments, "record_native_execution",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ledger unavailable")),
    )
    result = lw.execute({
        "op_type": "keyword_bid", "sid": 1, "target_id": "9",
        "before": {"bid": 1.0}, "change": {"bid": 0.9},
    }, dry_run=False)

    assert result["ok"] is True
    assert result["adjustment_ledger"]["ok"] is False
    assert "ledger unavailable" in result["adjustment_ledger"]["error"]


def test_ledger_sidecar_failure_redacts_credentials(awen_home, monkeypatch):
    lw = _lw()
    from awen_agent import adjustments

    lw.set_operate(True)
    monkeypatch.setattr(lw, "call", lambda *args, **kwargs: {"code": 0, "data": {}})
    monkeypatch.setattr(lw, "_current_value", lambda intent: {"bid": 1.0})
    monkeypatch.setattr(
        adjustments, "record_native_execution",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("access_token=sidecar-token-secret")),
    )

    result = lw.execute({
        "op_type": "keyword_bid", "sid": 1, "target_id": "9",
        "before": {"bid": 1.0}, "change": {"bid": 0.9},
    }, dry_run=False)

    assert result["ok"] is True
    assert "sidecar-token-secret" not in result["adjustment_ledger"]["error"]
    assert "[REDACTED]" in result["adjustment_ledger"]["error"]


def test_rollback_negate_uses_archive(awen_home, monkeypatch):
    lw = _lw()
    lw.set_operate(True)
    sent = []
    monkeypatch.setattr(lw, "call",
                        lambda route, body, **k: sent.append((route, body)) or {"code": 0, "data": {"success": [{"targetId": "T1"}]}})
    r = lw.execute({"op_type": "negate_keyword", "sid": 1876, "campaign_id": "C1",
                    "keyword_text": "junk", "match_type": "negativeExact"}, dry_run=False)
    assert r["ok"]
    rb = lw.rollback(r["audit_id"])
    assert rb["ok"]
    assert "archiveNegatives" in sent[-1][0] and sent[-1][1]["targetIds"] == ["T1"]
