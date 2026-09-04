from __future__ import annotations

import datetime as dt
import sqlite3


def _event(**overrides):
    row = {
        "sid": "1001",
        "marketplace_id": "ATVPDKIKX0DER",
        "profile_id": "p-1",
        "source": "lingxing",
        "sponsored_type": "sp",
        "operate_type": "keywords",
        "action_type": "bid_change",
        "object_type": "keyword",
        "object_id": "kw-1",
        "object_name": "water bottle",
        "campaign_id": "c-1",
        "ad_group_id": "g-1",
        "before": {"bid": 1.2},
        "after": {"bid": 0.9},
        "changes": [{"field": "bid", "before": 1.2, "after": 0.9}],
        "operated_at": "2026-08-01T10:00:00+08:00",
        "operated_at_local": "2026-08-01 10:00:00",
        "timezone": "Asia/Shanghai",
        "raw": {"provider_field": "kept"},
    }
    row.update(overrides)
    return row


def test_import_is_idempotent_per_source_and_store(awen_home):
    from awen_agent import adjustments

    first = adjustments.import_events([_event()])
    again = adjustments.import_events([_event()])
    other_store = adjustments.import_events([_event(sid="1002")])

    assert first["created"] == 1
    assert again["created"] == 0
    assert again["duplicates"] == 1
    assert other_store["created"] == 1
    assert adjustments.summary()["actions"] == 2


def test_connection_context_always_closes_handle(monkeypatch):
    from awen_agent import adjustments

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def close(self):
            self.closed = True

    fake = FakeConnection()
    monkeypatch.setattr(adjustments, "_connect", lambda _path=None: fake)

    with adjustments._connection() as connection:
        assert connection is fake

    assert fake.closed is True


def test_migration_adds_structured_confounders_to_existing_review_table():
    from awen_agent import adjustments

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE adjustment_reviews("
            "id TEXT PRIMARY KEY, action_id TEXT, horizon_days INTEGER, revision INTEGER)")
        adjustments._migrate(connection)
        columns = {
            str(row[1]) for row in connection.execute(
                "PRAGMA table_info(adjustment_reviews)")}
        assert "confounders_json" in columns
    finally:
        connection.close()


def test_ledger_releases_database_file_after_each_operation(awen_home):
    from awen_agent import adjustments

    adjustments.import_events([_event()])
    adjustments.list_actions()
    moved = adjustments.DB_PATH.with_suffix(".moved")

    adjustments.DB_PATH.replace(moved)
    moved.replace(adjustments.DB_PATH)


def test_source_event_id_takes_precedence_for_idempotency(awen_home):
    from awen_agent import adjustments

    a = _event(source_event_id="upstream-42", before={"bid": 1.2})
    b = _event(source_event_id="upstream-42", before={"bid": 1.3})
    first = adjustments.import_events([a])
    second = adjustments.import_events([b])

    assert first["action_ids"] == second["action_ids"]
    detail = adjustments.get_action(first["action_ids"][0], include_raw=True)
    assert detail["before"] == {"bid": 1.2}


def test_duplicate_can_backfill_missing_scope_without_rewriting_action(awen_home):
    from awen_agent import adjustments

    first = adjustments.import_events([_event(
        source_event_id="scope-late", reason="original",
        evidence={"decision": {"rule": "original"},
                  "scope_mapping": {"confidence": "none", "gaps": ["offline"]}},
        scope_asins=[{"asin": "A1", "role": "advertised_child", "parent_asin": ""}],
    )])
    second = adjustments.import_events([_event(
        source_event_id="scope-late", reason="changed-must-not-win",
        evidence={"decision": {"rule": "changed-must-not-win"},
                  "scope_mapping": {"confidence": "high", "gaps": []}},
        scope_asins=[
            {"asin": "P1", "role": "parent", "parent_asin": "P1"},
            {"asin": "A1", "role": "advertised_child", "parent_asin": "P1"},
            {"asin": "A2", "role": "unadvertised_sibling", "parent_asin": "P1"},
        ],
    )])
    detail = adjustments.get_action(first["action_ids"][0])

    assert second["created"] == 0 and second["duplicates"] == 1
    assert second["enriched"] == 1
    assert detail["reason"] == "original"
    assert {row["asin"] for row in detail["scope_asins"]} == {"P1", "A1", "A2"}
    assert detail["evidence"]["decision"] == {"rule": "original"}
    assert detail["evidence"]["scope_mapping"]["confidence"] == "high"


def test_detail_preserves_raw_evidence_and_frozen_asin_scope(awen_home):
    from awen_agent import adjustments

    event = _event(
        evidence={"rationale": "7 日 ACoS 高于目标", "strategy": "profit_guard"},
        scope_asins=[
            {"asin": "PARENT1", "role": "parent"},
            {"asin": "CHILD1", "role": "advertised_child"},
            {"asin": "CHILD2", "role": "unadvertised_sibling"},
        ],
    )
    result = adjustments.import_events([event])
    detail = adjustments.get_action(result["action_ids"][0], include_raw=True)

    assert detail["raw"] == {"provider_field": "kept"}
    assert detail["evidence"]["rationale"] == "7 日 ACoS 高于目标"
    assert {(r["asin"], r["role"]) for r in detail["scope_asins"]} == {
        ("PARENT1", "parent"),
        ("CHILD1", "advertised_child"),
        ("CHILD2", "unadvertised_sibling"),
    }


def test_scope_snapshot_uses_capture_time_not_action_time_when_supplied(awen_home):
    from awen_agent import adjustments

    captured_at = 1_788_220_800.0
    action_id = adjustments.import_events([_event(scope_asins=[{
        "asin": "P1", "role": "parent", "parent_asin": "P1",
        "snapshot_at": captured_at,
    }])])["action_ids"][0]

    assert adjustments.get_action(action_id)["scope_asins"][0]["snapshot_at"] == captured_at


def test_parent_filter_accepts_child_row_with_parent_relation(awen_home):
    from awen_agent import adjustments

    action_id = adjustments.import_events([_event(scope_asins=[{
        "asin": "A1", "role": "advertised_child", "parent_asin": "P1",
    }])])["action_ids"][0]

    assert adjustments.list_actions(parent_asin="p1")["items"][0]["id"] == action_id
    assert adjustments.summary(parent_asin="p1")["actions"] == 1


def test_annotation_does_not_mutate_original_reason(awen_home):
    from awen_agent import adjustments

    result = adjustments.import_events([_event(reason="自动策略原始理由")])
    action_id = result["action_ids"][0]
    adjustments.annotate(action_id, "运营补充：清库存", operator="wen")
    detail = adjustments.get_action(action_id)

    assert detail["reason"] == "自动策略原始理由"
    assert detail["reason_status"] == "provided"
    assert detail["annotations"][0]["text"] == "运营补充：清库存"


def test_reviews_append_revisions_and_filter_by_parent(awen_home):
    from awen_agent import adjustments

    result = adjustments.import_events([_event(scope_asins=[{"asin": "P1", "role": "parent"}])])
    action_id = result["action_ids"][0]
    one = adjustments.record_review(action_id, 7, {
        "verdict": "positive_signal", "confidence": "medium",
        "window": {"baseline": ["2026-07-25", "2026-07-31"],
                   "evaluation": ["2026-08-02", "2026-08-08"]},
    })
    two = adjustments.record_review(action_id, 7, {
        "verdict": "stable_positive", "confidence": "high",
        "window": one["window"],
    })

    assert (one["revision"], two["revision"]) == (1, 2)
    assert [r["revision"] for r in adjustments.list_reviews(action_id)] == [2, 1]
    assert adjustments.list_actions(parent_asin="P1")["items"][0]["id"] == action_id
    assert adjustments.list_actions(verdict="stable_positive")["items"][0]["id"] == action_id


def test_review_revision_persists_structured_confounders(awen_home):
    from awen_agent import adjustments

    action_id = adjustments.import_events([_event()])["action_ids"][0]
    adjustments.record_review(action_id, 7, {
        "verdict": "confounded", "confidence": "low",
        "confounders": ["同期改价", "促销开始"],
    })

    stored = adjustments.list_reviews(action_id, horizon_days=7)[0]
    assert stored["confounders"] == ["同期改价", "促销开始"]


def test_due_horizons_use_action_local_date_and_skip_completed(awen_home):
    from awen_agent import adjustments

    result = adjustments.import_events([_event(operated_at="2026-08-01T23:30:00-07:00")])
    action_id = result["action_ids"][0]
    due = adjustments.due_reviews(today=dt.date(2026, 8, 9), horizons=(3, 7, 14, 30))
    assert {(r["action_id"], r["horizon_days"]) for r in due} == {
        (action_id, 3), (action_id, 7)
    }

    adjustments.record_review(action_id, 3, {"verdict": "neutral", "confidence": "low"})
    due = adjustments.due_reviews(today=dt.date(2026, 8, 9), horizons=(3, 7, 14, 30))
    assert {(r["action_id"], r["horizon_days"]) for r in due} == {(action_id, 7)}


def test_due_reviews_can_exclude_stores_without_enumerating_store_catalog(awen_home):
    from awen_agent import adjustments

    first = adjustments.import_events([_event(
        sid="1", source_event_id="exclude-one")])["action_ids"][0]
    adjustments.import_events([_event(
        sid="2", source_event_id="exclude-two")])

    due = adjustments.due_reviews(
        today=dt.date(2026, 8, 9), horizons=(3,), exclude_sids=["2"])

    assert due == [{"action_id": first, "horizon_days": 3}]


def test_retryable_data_gap_is_due_again_after_cooldown(awen_home):
    import time
    from awen_agent import adjustments

    action_id = adjustments.import_events([_event()])["action_ids"][0]
    adjustments.record_review(action_id, 3, {"verdict": "data_gap", "confidence": "none"})
    now = time.time()
    assert not any(item["horizon_days"] == 3 for item in adjustments.due_reviews(
        today=dt.date(2026, 8, 9), horizons=(3,), now=now + 60))
    due = adjustments.due_reviews(
        today=dt.date(2026, 8, 9), horizons=(3,), now=now + 25 * 3600)
    assert due == [{"action_id": action_id, "horizon_days": 3, "retry": True}]


def test_credentials_are_removed_before_persistence(awen_home):
    from awen_agent import adjustments

    event = _event(
        raw={
            "access_token": "secret", "Authorization": "Bearer bearer-secret",
            "X-Amz-Access-Token": "amz-secret", "apiKey": "api-secret",
            "nested": {"password": "secret", "safe": 1},
            "error": "failed https://example.test?a=1&access_token=url-secret&sign=signed-secret",
        },
        evidence={"refresh_token": "secret", "basis": "7d"},
        before={"bid": 1.2, "app_key": "secret"},
        reason="Bearer reason-secret",
        strategy="api_key=strategy-secret",
        object_name="authorization: object-secret",
        campaign_name="refresh_token=campaign-secret",
        ad_group_name="password=group-secret",
        operator_id="client_secret=operator-id-secret",
        operator_name="cookie=operator-name-secret",
        changes=[{"field": "access_token", "before": "a", "after": "b"},
                 {"field": "bid", "before": 1.2, "after": 0.9}],
    )
    action_id = adjustments.import_events([event])["action_ids"][0]
    adjustments.annotate(
        action_id, "refresh_token=annotation-secret",
        operator="Bearer annotation-operator-secret",
        strategy="signature=annotation-strategy-secret",
    )
    detail = adjustments.get_action(action_id, include_raw=True)
    serialized = __import__("json").dumps(detail, ensure_ascii=False)
    assert detail["raw"]["nested"] == {"safe": 1}
    assert "bearer-secret" not in serialized
    assert "amz-secret" not in serialized
    assert "api-secret" not in serialized
    assert "url-secret" not in serialized
    assert "signed-secret" not in serialized
    for secret in (
        "reason-secret", "strategy-secret", "object-secret", "campaign-secret",
        "group-secret", "operator-id-secret", "operator-name-secret",
        "annotation-secret", "annotation-operator-secret", "annotation-strategy-secret",
    ):
        assert secret not in serialized
    assert detail["evidence"] == {"basis": "7d"}
    assert detail["before"] == {"bid": 1.2}
    assert detail["changes"] == [{"field": "bid", "before": 1.2, "after": 0.9}]


def test_annotation_satisfies_missing_reason_without_rewriting_fact(awen_home):
    from awen_agent import adjustments

    action_id = adjustments.import_events([_event(reason="")])["action_ids"][0]
    assert adjustments.summary()["missing_reason"] == 1
    adjustments.annotate(action_id, "人工补充理由")
    detail = adjustments.get_action(action_id)
    assert detail["reason"] == ""
    assert detail["effective_reason"] == "人工补充理由"
    assert detail["effective_reason_status"] == "annotated"
    assert adjustments.summary()["missing_reason"] == 0


def test_annotation_length_limit_is_enforced_in_core(awen_home):
    import pytest
    from awen_agent import adjustments

    action_id = adjustments.import_events([_event(reason="")])["action_ids"][0]
    with pytest.raises(ValueError, match="4000"):
        adjustments.annotate(action_id, "x" * 4001)
    assert adjustments.get_action(action_id)["annotations"] == []


def test_verdict_filter_only_uses_latest_revision_per_horizon(awen_home):
    from awen_agent import adjustments

    action_id = adjustments.import_events([_event()])["action_ids"][0]
    adjustments.record_review(action_id, 7, {"verdict": "positive_signal", "confidence": "low"})
    adjustments.record_review(action_id, 7, {"verdict": "negative_signal", "confidence": "medium"})
    assert adjustments.list_actions(verdict="positive_signal")["items"] == []
    assert adjustments.list_actions(verdict="negative_signal")["items"][0]["id"] == action_id


def test_concurrent_duplicate_import_creates_one_action(awen_home):
    from concurrent.futures import ThreadPoolExecutor
    from awen_agent import adjustments

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: adjustments.import_events([_event()]), range(12)))
    assert sum(result["created"] for result in results) == 1
    assert adjustments.summary()["actions"] == 1


def test_invalid_scope_rejects_whole_batch(awen_home):
    import pytest
    from awen_agent import adjustments

    with pytest.raises(ValueError, match="scope role"):
        adjustments.import_events([_event(scope_asins=[{"asin": "A1", "role": "advertising-ish"}])])
    assert adjustments.summary()["actions"] == 0


def test_naive_upstream_time_requires_and_uses_iana_timezone(awen_home):
    import pytest
    from awen_agent import adjustments

    action_id = adjustments.import_events([
        _event(operated_at="2026-08-01 10:00:00", timezone="Asia/Tokyo")
    ])["action_ids"][0]
    assert adjustments.get_action(action_id)["operated_at_iso"].endswith("+09:00")
    with pytest.raises(ValueError, match="未知 IANA 时区"):
        adjustments.import_events([
            _event(object_id="kw-2", operated_at="2026-08-01 10:00:00", timezone="Mars/Olympus")
        ])


def test_aware_timestamp_without_local_text_uses_site_date(awen_home):
    from awen_agent import adjustments

    action_id = adjustments.import_events([_event(
        operated_at="2026-08-01T01:00:00+00:00",
        operated_at_local="",
        timezone="America/Los_Angeles",
    )])["action_ids"][0]
    action = adjustments.get_action(action_id)
    assert action["operated_at_local"].startswith("2026-07-31T18:00:00")
    assert action["action_date_local"] == "2026-07-31"


def test_aware_timestamp_still_rejects_invalid_site_timezone(awen_home):
    import pytest
    from awen_agent import adjustments

    with pytest.raises(ValueError, match="未知 IANA 时区"):
        adjustments.import_events([_event(
            operated_at="2026-08-01T01:00:00+00:00",
            operated_at_local="",
            timezone="Mars/Olympus",
        )])


def test_native_execution_uses_cached_store_timezone_without_network(awen_home, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from awen_agent import adjustments

    monkeypatch.setattr(adjustments.stores, "cached_get", lambda sid: {
        "sid": sid, "marketplace_id": "ATVPDKIKX0DER"})
    monkeypatch.setattr(
        adjustments.stores, "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不得联网解析店铺")),
    )
    action_id = adjustments.record_native_execution({
        "op_type": "keyword_bid", "sid": 9, "target_id": "kw-9",
        "before": {"bid": 1.0}, "change": {"bid": 0.9},
    }, {"ok": True, "audit_id": "audit-9"})["action_ids"][0]

    action = adjustments.get_action(action_id)
    expected_date = datetime.fromtimestamp(
        action["operated_at"], ZoneInfo("America/Los_Angeles")).date().isoformat()
    assert action["marketplace_id"] == "ATVPDKIKX0DER"
    assert action["timezone"] == "America/Los_Angeles"
    assert action["action_date_local"] == expected_date
    assert action["evidence"]["time_mapping"]["local_date_confidence"] == "high"


def test_native_execution_marks_utc_date_fallback_low_confidence(awen_home, monkeypatch):
    from awen_agent import adjustments

    monkeypatch.setattr(adjustments.stores, "cached_get", lambda sid: None)
    action_id = adjustments.record_native_execution({
        "op_type": "keyword_bid", "sid": 10, "target_id": "kw-10",
        "before": {"bid": 1.0}, "change": {"bid": 0.9},
    }, {"ok": True, "audit_id": "audit-10"})["action_ids"][0]

    mapping = adjustments.get_action(action_id)["evidence"]["time_mapping"]
    assert mapping["resolved_timezone"] == "UTC"
    assert mapping["action_time_exact"] is True
    assert mapping["local_date_confidence"] == "low"
    assert "T0" in mapping["warning"]


def test_cursor_pagination_does_not_skip_actions_with_same_timestamp(awen_home):
    from awen_agent import adjustments

    for index in range(3):
        adjustments.import_events([_event(
            id=f"adj-{index}", source_event_id=f"event-{index}", object_id=f"kw-{index}")])

    first = adjustments.list_actions(limit=2)
    second = adjustments.list_actions(limit=2, cursor=first["next_cursor"])
    ids = [row["id"] for row in first["items"] + second["items"]]
    assert len(ids) == 3
    assert len(set(ids)) == 3

    import pytest
    with pytest.raises(ValueError, match="cursor"):
        adjustments.list_actions(cursor="not-a-valid-cursor")


def test_invalid_explicit_action_date_rejects_whole_batch(awen_home):
    import pytest
    from awen_agent import adjustments

    with pytest.raises(ValueError, match="action_date_local"):
        adjustments.import_events([_event(action_date_local="not-a-date")])
    assert adjustments.summary()["actions"] == 0
