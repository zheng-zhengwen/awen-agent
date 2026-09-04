from __future__ import annotations

import datetime as dt
import json
import threading
import urllib.error
import urllib.request


def _event(**overrides):
    row = {
        "sid": "7", "source": "upstream", "source_event_id": "evt-1",
        "sponsored_type": "sp", "operate_type": "campaigns",
        "action_type": "budget_increase", "object_type": "campaign",
        "object_id": "c-7", "campaign_id": "c-7",
        "before": {"budget": 10}, "after": {"budget": 15},
        "operated_at": "2026-08-10T12:00:00+00:00",
        "raw": {"provider": "raw", "access_token": "must-not-persist"},
    }
    row.update(overrides)
    return row


def test_metric_contracts_and_verified_lingxing_support_exist():
    from awen_agent import metrics
    from awen_agent.datasources.lingxing_source import LingxingSource

    expected = {
        "catalog.variation_snapshot", "ads.ad_group_config", "ads.target_config",
        "ads.ad_group_report", "ads.target_report", "ads.placement_report",
        "ads.advertised_product_report", "ads.purchased_product_report",
        "business.asin_daily", "profit.parent_asin", "ranking.asin_keyword_daily",
    }
    assert expected <= set(metrics.REGISTRY)
    source = LingxingSource()
    assert source.supports("ads.ad_group_config")
    assert source.supports("ads.target_config")
    assert source.supports("ads.target_report")
    assert not source.supports("ads.purchased_product_report")


def test_service_projection_manifest_and_import_do_not_expose_raw_or_secrets(awen_home):
    from awen_agent import service

    imported = service.adjustment_import({"events": [_event()]})
    action_id = imported["action_ids"][0]
    detail = service.adjustment_detail(action_id)
    listing = service.adjustment_list({"sid": ["7"]})
    manifest = service.manifest()

    assert imported["ok"] is True
    assert "raw" not in detail["action"]
    assert "access_token" not in json.dumps(detail)
    from awen_agent import adjustments
    assert adjustments.get_sync_state("7", "push")["last_synced_at"] > 0
    assert listing["items"][0]["id"] == action_id
    for capability in ("ads_adjustment_history", "ads_adjustment_review",
                       "ads_parent_child_scope", "ads_adjustment_import"):
        assert manifest["capabilities"][capability] is True
    assert manifest["capabilities"]["write_execution"] is False
    assert any(item["path"] == "/v1/adjustments/{id}/evaluate" for item in manifest["endpoints"])


def test_invalid_import_source_mode_has_no_partial_side_effect(awen_home):
    import pytest
    from awen_agent import adjustments, service

    with pytest.raises(ValueError, match="source_mode"):
        service.adjustment_import({"events": [_event()], "source_mode": "guess"})
    assert adjustments.summary()["actions"] == 0


def test_invalid_sync_request_does_not_change_source_mode(awen_home):
    import pytest
    from awen_agent import adjustments, service

    with pytest.raises(ValueError, match="sid 必须是正整数"):
        service.adjustment_sync({
            "sid": "not-numeric", "start_date": "2026-08-01", "end_date": "2026-08-02",
            "source_mode": "push",
        })
    assert adjustments.get_source_mode("not-numeric") == "hybrid"

    with pytest.raises(ValueError, match="不能为空"):
        service.adjustment_sync({
            "sid": 1, "start_date": "2026-08-01", "end_date": "2026-08-02",
            "sponsored_types": [],
        })
    assert adjustments.get_source_mode(1) == "hybrid"


def test_service_manual_evaluation_appends_review(awen_home):
    from awen_agent import service

    action_id = service.adjustment_import({"events": [_event()]})["action_ids"][0]
    rows = []
    for day in range(3, 10):
        rows.append({"date": f"2026-08-{day:02d}", "clicks": 20, "orders": 3,
                     "spend": 50, "sales": 150, "impressions": 500})
    for day in range(11, 18):
        rows.append({"date": f"2026-08-{day:02d}", "clicks": 30, "orders": 5,
                     "spend": 75, "sales": 250, "impressions": 700})
    out = service.adjustment_evaluate(action_id, {
        "horizon_days": 7, "as_of": "2026-08-19", "rows": rows,
    })
    assert out["ok"] is True
    assert out["review"]["revision"] == 1
    assert out["review"]["window"]["t0_excluded"] is True
    assert out["review"]["provenance"]["source"] == "caller_supplied"
    assert out["review"]["cohorts"]["campaign"]["before"]["row_count"] == 7
    assert out["review"]["cohorts"]["campaign_provenance"]["source"] == "caller_supplied"
    assert service.adjustment_reviews(action_id)["items"][0]["verdict"] == "positive_signal"


def test_service_accepts_separate_campaign_and_ad_group_context_rows(awen_home):
    from awen_agent import service

    action_id = service.adjustment_import({"events": [_event(
        source_event_id="upper-contexts", operate_type="keywords",
        action_type="bid_increase", object_type="keyword", object_id="kw-7",
        ad_group_id="g-7",
    )]})["action_ids"][0]
    primary_rows = []
    campaign_rows = []
    ad_group_rows = []
    for day in (7, 8, 9, 11, 12, 13):
        date = f"2026-08-{day:02d}"
        primary_rows.append({
            "date": date, "keyword_id": "kw-7", "clicks": 20,
            "orders": 4, "spend": 50, "sales": 200,
        })
        campaign_rows.append({
            "date": date, "campaign_id": "c-7", "clicks": 80,
            "orders": 12, "spend": 180, "sales": 600,
        })
        ad_group_rows.append({
            "date": date, "ad_group_id": "g-7", "clicks": 40,
            "orders": 7, "spend": 90, "sales": 320,
        })

    review = service.adjustment_evaluate(action_id, {
        "horizon_days": 3, "as_of": "2026-08-15", "rows": primary_rows,
        "campaign_rows": campaign_rows, "ad_group_rows": ad_group_rows,
    })["review"]

    assert review["cohorts"]["campaign"]["before"]["sales"] == 1800
    assert review["cohorts"]["ad_group"]["before"]["sales"] == 960
    assert review["cohorts"]["campaign_provenance"]["source"] == "caller_supplied"
    assert review["cohorts"]["ad_group_provenance"]["source"] == "caller_supplied"


def test_explicit_empty_upper_contexts_are_data_gaps_not_zero_performance(awen_home):
    from awen_agent import adjustment_report, adjustments

    action_id = adjustments.import_events([_event(
        source_event_id="empty-upper-contexts", operate_type="keywords",
        action_type="bid_decrease", object_type="keyword", object_id="kw-7",
        ad_group_id="g-7",
    )])["action_ids"][0]
    rows = []
    for day in (7, 8, 9, 11, 12, 13):
        rows.append({
            "date": f"2026-08-{day:02d}", "keyword_id": "kw-7",
            "clicks": 20, "orders": 4, "spend": 50, "sales": 200,
        })

    review = adjustment_report.evaluate_action(
        action_id, 3, as_of="2026-08-15", rows=rows,
        campaign_rows=[], ad_group_rows=[])
    assert review["cohorts"]["campaign"]["status"] == "data_gap"
    assert review["cohorts"]["ad_group"]["status"] == "data_gap"
    assert review["cohorts"]["campaign_provenance"]["row_count"] == 0
    assert review["cohorts"]["ad_group_provenance"]["row_count"] == 0


def test_negative_keyword_review_uses_affected_ad_group_rows(awen_home):
    from awen_agent import adjustment_report, adjustments

    action_id = adjustments.import_events([_event(
        source_event_id="negative-1", operate_type="negativeKeywords",
        action_type="negative_add", object_type="negative_keyword",
        object_id="neg-1", campaign_id="c-7", ad_group_id="g-7",
    )])["action_ids"][0]
    rows = []
    for day in range(7, 10):
        rows.append({"date": f"2026-08-{day:02d}", "ad_group_id": "g-7",
                     "clicks": 30, "orders": 5, "spend": 100, "sales": 250})
    for day in range(11, 14):
        rows.append({"date": f"2026-08-{day:02d}", "ad_group_id": "g-7",
                     "clicks": 25, "orders": 5, "spend": 80, "sales": 260})

    review = adjustment_report.evaluate_action(
        action_id, 3, as_of="2026-08-15", rows=rows)
    assert review["verdict"] == "positive_signal"
    assert review["mapping_confidence"] == "high"


def test_review_keeps_multiple_parent_link_cohorts_separate(awen_home):
    from awen_agent import adjustment_report, adjustments

    action_id = adjustments.import_events([_event(
        source_event_id="multi-parent", scope_asins=[
            {"asin": "P1", "role": "parent", "parent_asin": "P1"},
            {"asin": "A1", "role": "advertised_child", "parent_asin": "P1"},
            {"asin": "A2", "role": "unadvertised_sibling", "parent_asin": "P1"},
            {"asin": "P2", "role": "parent", "parent_asin": "P2"},
            {"asin": "B1", "role": "advertised_child", "parent_asin": "P2"},
        ],
    )])["action_ids"][0]
    object_rows = []
    business_rows = []
    for day in (7, 8, 9, 11, 12, 13):
        date = f"2026-08-{day:02d}"
        object_rows.append({"date": date, "campaign_id": "c-7", "clicks": 20,
                            "orders": 4, "spend": 50, "sales": 200})
        business_rows.extend([
            {"date": date, "asin": "A1", "sessions": 100, "units": 10, "orders": 8, "sales": 200},
            {"date": date, "asin": "A2", "sessions": 50, "units": 5, "orders": 4, "sales": 100},
            {"date": date, "asin": "B1", "sessions": 40, "units": 4, "orders": 3, "sales": 80},
        ])

    review = adjustment_report.evaluate_action(
        action_id, 3, as_of="2026-08-15", rows=object_rows, business_rows=business_rows)
    groups = review["cohorts"]["parent"]["groups"]
    assert set(groups) == {"P1", "P2"}
    assert groups["P1"]["before"]["parent_total"]["sessions"] == 450
    assert groups["P2"]["before"]["parent_total"]["sessions"] == 120


def test_review_separates_purchases_of_advertised_children_and_sibling_halo(awen_home):
    from awen_agent import adjustment_report, adjustments

    action_id = adjustments.import_events([_event(
        source_event_id="purchase-halo", scope_asins=[
            {"asin": "P1", "role": "parent", "parent_asin": "P1"},
            {"asin": "A1", "role": "advertised_child", "parent_asin": "P1"},
            {"asin": "A2", "role": "unadvertised_sibling", "parent_asin": "P1"},
        ],
    )])["action_ids"][0]
    action_date = dt.date(2026, 8, 10)
    object_rows = []
    purchased_rows = []
    for offset in range(1, 4):
        before = (action_date - dt.timedelta(days=offset)).isoformat()
        after = (action_date + dt.timedelta(days=offset)).isoformat()
        object_rows.extend([
            {"date": before, "campaign_id": "c-7", "clicks": 20, "orders": 7, "spend": 50, "sales": 140},
            {"date": after, "campaign_id": "c-7", "clicks": 20, "orders": 8, "spend": 50, "sales": 170},
        ])
        purchased_rows.extend([
            {"date": before, "campaign_id": "c-7", "advertised_asin": "A1",
             "purchased_asin": "A1", "orders": 5, "sales": 100},
            {"date": before, "campaign_id": "c-7", "advertised_asin": "A1",
             "purchased_asin": "A2", "orders": 2, "sales": 40},
            {"date": after, "campaign_id": "c-7", "advertised_asin": "A1",
             "purchased_asin": "A1", "orders": 4, "sales": 90},
            {"date": after, "campaign_id": "c-7", "advertised_asin": "A1",
             "purchased_asin": "A2", "orders": 4, "sales": 80},
        ])

    review = adjustment_report.evaluate_action(
        action_id, 3, as_of="2026-08-15", rows=object_rows,
        purchased_rows=purchased_rows)
    parent = review["cohorts"]["purchased_products"]["groups"]["P1"]
    assert parent["before"]["unadvertised_sibling"]["orders"] == 6
    assert parent["after"]["unadvertised_sibling"]["orders"] == 12


def test_purchased_product_cohorts_bind_advertised_child_before_parent_halo(awen_home):
    from awen_agent import adjustment_report, adjustments

    action_id = adjustments.import_events([_event(
        source_event_id="cross-parent-purchase", scope_asins=[
            {"asin": "P1", "role": "parent", "parent_asin": "P1"},
            {"asin": "A1", "role": "advertised_child", "parent_asin": "P1"},
            {"asin": "A2", "role": "unadvertised_sibling", "parent_asin": "P1"},
            {"asin": "P2", "role": "parent", "parent_asin": "P2"},
            {"asin": "B1", "role": "advertised_child", "parent_asin": "P2"},
        ],
    )])["action_ids"][0]
    action_date = dt.date(2026, 8, 10)
    object_rows = []
    purchased_rows = []
    for offset in range(1, 4):
        for date in (
            (action_date - dt.timedelta(days=offset)).isoformat(),
            (action_date + dt.timedelta(days=offset)).isoformat(),
        ):
            object_rows.append({"date": date, "campaign_id": "c-7", "clicks": 20,
                                "orders": 4, "spend": 50, "sales": 150})
            purchased_rows.extend([
                {"date": date, "campaign_id": "c-7", "advertised_asin": "A1",
                 "purchased_asin": "A2", "orders": 2, "sales": 40},
                {"date": date, "campaign_id": "c-7", "advertised_asin": "B1",
                 "purchased_asin": "A2", "orders": 9, "sales": 180},
            ])

    review = adjustment_report.evaluate_action(
        action_id, 3, as_of="2026-08-15", rows=object_rows,
        purchased_rows=purchased_rows)
    parents = review["cohorts"]["purchased_products"]["groups"]
    assert parents["P1"]["before"]["unadvertised_sibling"]["orders"] == 6
    assert parents["P2"]["before"]["parent_attributed_total"]["orders"] == 0


def test_multi_parent_purchase_rows_without_advertised_asin_are_not_misattributed(awen_home):
    from awen_agent import adjustment_report, adjustments

    action_id = adjustments.import_events([_event(
        source_event_id="ambiguous-purchase", scope_asins=[
            {"asin": "P1", "role": "parent", "parent_asin": "P1"},
            {"asin": "A1", "role": "advertised_child", "parent_asin": "P1"},
            {"asin": "P2", "role": "parent", "parent_asin": "P2"},
            {"asin": "B1", "role": "advertised_child", "parent_asin": "P2"},
        ],
    )])["action_ids"][0]
    rows = []
    purchases = []
    for day in (7, 8, 9, 11, 12, 13):
        date = f"2026-08-{day:02d}"
        rows.append({"date": date, "campaign_id": "c-7", "clicks": 20,
                     "orders": 4, "spend": 50, "sales": 150})
        purchases.append({"date": date, "campaign_id": "c-7",
                          "purchased_asin": "A1", "orders": 9, "sales": 180})

    review = adjustment_report.evaluate_action(
        action_id, 3, as_of="2026-08-15", rows=rows, purchased_rows=purchases)
    cohort = review["cohorts"]["purchased_products"]
    assert cohort["status"] == "data_gap"
    assert cohort["attribution_binding"] == "missing_advertised_asin"
    assert "groups" not in cohort


def test_single_parent_purchase_rows_without_advertised_asin_are_low_confidence(awen_home):
    from awen_agent import adjustment_report, adjustments

    action_id = adjustments.import_events([_event(
        source_event_id="single-parent-purchase", scope_asins=[
            {"asin": "P1", "role": "parent", "parent_asin": "P1"},
            {"asin": "A1", "role": "advertised_child", "parent_asin": "P1"},
            {"asin": "A2", "role": "unadvertised_sibling", "parent_asin": "P1"},
        ],
    )])["action_ids"][0]
    rows = []
    purchases = []
    for day in (7, 8, 9, 11, 12, 13):
        date = f"2026-08-{day:02d}"
        rows.append({"date": date, "campaign_id": "c-7", "clicks": 20,
                     "orders": 4, "spend": 50, "sales": 150})
        purchases.append({"date": date, "campaign_id": "c-7",
                          "purchased_asin": "A2", "orders": 2, "sales": 40})

    review = adjustment_report.evaluate_action(
        action_id, 3, as_of="2026-08-15", rows=rows, purchased_rows=purchases)
    cohort = review["cohorts"]["purchased_products"]
    assert cohort["binding_confidence"] == "low"
    assert cohort["attribution_binding"] == "single_parent_scope_fallback"
    assert cohort["groups"]["P1"]["before"]["unadvertised_sibling"]["orders"] == 6


def test_review_projects_profit_ranking_and_inventory_as_noncausal_context(awen_home):
    from awen_agent import adjustment_report, adjustments

    action_id = adjustments.import_events([_event(
        source_event_id="contexts", scope_asins=[
            {"asin": "P1", "role": "parent", "parent_asin": "P1"},
            {"asin": "A1", "role": "advertised_child", "parent_asin": "P1"},
            {"asin": "A2", "role": "unadvertised_sibling", "parent_asin": "P1"},
        ],
    )])["action_ids"][0]
    action_date = dt.date(2026, 8, 10)
    object_rows = []
    profit_rows = []
    ranking_rows = []
    for offset in range(1, 4):
        before = (action_date - dt.timedelta(days=offset)).isoformat()
        after = (action_date + dt.timedelta(days=offset)).isoformat()
        object_rows.extend([
            {"date": before, "campaign_id": "c-7", "clicks": 20, "orders": 4, "spend": 50, "sales": 200},
            {"date": after, "campaign_id": "c-7", "clicks": 25, "orders": 5, "spend": 55, "sales": 250},
        ])
        profit_rows.extend([
            {"date": before, "parent_asin": "P1", "sales_amount": 300,
             "ads_cost": 50, "gross_profit": 90},
            {"date": after, "parent_asin": "P1", "sales_amount": 360,
             "ads_cost": 55, "gross_profit": 126},
        ])
        ranking_rows.extend([
            {"date": before, "parent_asin": "P1", "asin": "A1", "keyword": "bottle",
             "organic_rank": 20, "ad_rank": 5},
            {"date": after, "parent_asin": "P1", "asin": "A1", "keyword": "bottle",
             "organic_rank": 12, "ad_rank": 4},
        ])

    review = adjustment_report.evaluate_action(
        action_id, 3, as_of="2026-08-15", rows=object_rows,
        profit_rows=profit_rows, ranking_rows=ranking_rows,
        inventory_rows=[
            {"asin": "A1", "fulfillable": 20, "days_of_supply": 10},
            {"asin": "A2", "fulfillable": 0, "days_of_supply": 0},
            {"asin": "A2", "fulfillable": None, "days_of_supply": None},
        ])
    contexts = review["cohorts"]["contexts"]
    assert contexts["profit"]["groups"]["P1"]["after"]["gross_rate"] == 0.35
    assert contexts["ranking"]["groups"]["P1"]["after"]["organic_rank_median"] == 12
    assert contexts["inventory"]["groups"]["P1"]["current"]["stockout_children"] == 1
    assert contexts["inventory"]["snapshot_only"] is True
    assert review["causality_claimed"] is False


def test_explicit_empty_purchase_and_inventory_inputs_are_data_gaps(awen_home):
    from awen_agent import adjustment_report, adjustments

    action_id = adjustments.import_events([_event(
        source_event_id="empty-contexts", scope_asins=[
            {"asin": "P1", "role": "parent", "parent_asin": "P1"},
            {"asin": "A1", "role": "advertised_child", "parent_asin": "P1"},
        ],
    )])["action_ids"][0]
    rows = []
    action_date = dt.date(2026, 8, 10)
    for offset in range(1, 4):
        rows.extend([
            {"date": (action_date - dt.timedelta(days=offset)).isoformat(),
             "campaign_id": "c-7", "clicks": 20, "orders": 3,
             "spend": 50, "sales": 150},
            {"date": (action_date + dt.timedelta(days=offset)).isoformat(),
             "campaign_id": "c-7", "clicks": 20, "orders": 3,
             "spend": 50, "sales": 150},
        ])

    review = adjustment_report.evaluate_action(
        action_id, 3, as_of="2026-08-15", rows=rows,
        business_rows=[], store_rows=[], purchased_rows=[], inventory_rows=[])

    cohorts = review["cohorts"]
    assert cohorts["purchased_products"]["status"] == "data_gap"
    assert cohorts["purchased_products_provenance"]["source"] == "caller_supplied"
    assert cohorts["purchased_products_provenance"]["row_count"] == 0
    assert cohorts["parent"]["status"] == "data_gap"
    assert cohorts["parent_provenance"]["row_count"] == 0
    assert cohorts["store"]["status"] == "data_gap"
    assert cohorts["store_provenance"]["row_count"] == 0
    assert cohorts["contexts"]["inventory"]["status"] == "data_gap"
    assert cohorts["contexts"]["inventory"]["snapshot_only"] is True
    assert cohorts["contexts"]["inventory_provenance"]["row_count"] == 0


def test_nonempty_but_wrong_scope_context_rows_are_data_gaps(awen_home):
    from awen_agent import adjustment_report, adjustments

    action_id = adjustments.import_events([_event(
        source_event_id="wrong-context-scope", operate_type="keywords",
        action_type="bid_decrease", object_type="keyword", object_id="kw-7",
        ad_group_id="g-7", scope_asins=[
            {"asin": "P1", "role": "parent", "parent_asin": "P1"},
            {"asin": "A1", "role": "advertised_child", "parent_asin": "P1"},
        ],
    )])["action_ids"][0]
    rows = []
    for day in (7, 8, 9, 11, 12, 13):
        rows.append({"date": f"2026-08-{day:02d}", "keyword_id": "kw-7",
                     "clicks": 20, "orders": 4, "spend": 50, "sales": 200})
    wrong_date = "2026-08-07"
    review = adjustment_report.evaluate_action(
        action_id, 3, as_of="2026-08-15", rows=rows,
        campaign_rows=[{"date": wrong_date, "campaign_id": "other", "sales": 999}],
        ad_group_rows=[{"date": wrong_date, "ad_group_id": "other", "sales": 999}],
        business_rows=[{"date": wrong_date, "asin": "OUTSIDE", "sales": 999}],
        purchased_rows=[{"date": wrong_date, "campaign_id": "other",
                         "advertised_asin": "A1", "purchased_asin": "A1", "orders": 9}],
        profit_rows=[{"date": wrong_date, "parent_asin": "P2", "gross_profit": 999}],
        ranking_rows=[{"date": wrong_date, "parent_asin": "P2", "organic_rank": 1}],
        inventory_rows=[{"asin": "OUTSIDE", "fulfillable": 999}],
    )

    cohorts = review["cohorts"]
    assert cohorts["campaign"]["status"] == "data_gap"
    assert cohorts["ad_group"]["status"] == "data_gap"
    assert cohorts["parent"]["groups"]["P1"]["status"] == "data_gap"
    assert cohorts["purchased_products"]["status"] == "data_gap"
    assert cohorts["contexts"]["profit"]["groups"]["P1"]["status"] == "data_gap"
    assert cohorts["contexts"]["ranking"]["groups"]["P1"]["status"] == "data_gap"
    assert cohorts["contexts"]["inventory"]["groups"]["P1"]["status"] == "data_gap"


def test_automatic_review_uses_canonical_metric_layer_end_to_end(awen_home, monkeypatch):
    from awen_agent import adjustment_report, adjustments, metrics

    action_id = adjustments.import_events([_event(
        source_event_id="automatic", action_type="bid_increase", object_type="keyword",
        object_id="kw-7", campaign_id="c-7", ad_group_id="g-7",
    )])["action_ids"][0]
    action_date = dt.date(2026, 8, 10)
    keyword_rows = []
    for offset in range(1, 4):
        keyword_rows.extend([
            {"date": (action_date - dt.timedelta(days=offset)).isoformat(),
             "keyword_id": "kw-7", "clicks": 20, "orders": 3, "spend": 50, "sales": 150},
            {"date": (action_date + dt.timedelta(days=offset)).isoformat(),
             "keyword_id": "kw-7", "clicks": 30, "orders": 6, "spend": 70, "sales": 300},
        ])
    calls = []

    def fake_metric(metric, scope, window=None):
        calls.append(metric)
        if metric == metrics.ADS_KEYWORD_REPORT.key:
            return metrics.MetricResult(metric, keyword_rows)
        return metrics.MetricResult(
            metric, [], gap=metrics.DataGap(metric, "fixture has no source"))

    monkeypatch.setattr(metrics, "get_metric", fake_metric)
    monkeypatch.setattr("awen_agent.datasources.install_defaults", lambda: None)
    review = adjustment_report.evaluate_action(action_id, 3, as_of="2026-08-15")

    assert review["verdict"] == "positive_signal"
    assert calls.count(metrics.ADS_KEYWORD_REPORT.key) == 1
    assert metrics.ADS_CAMPAIGN_REPORT.key in calls
    assert metrics.ADS_AD_GROUP_REPORT.key in calls


def test_agent_and_reverse_mcp_tools_are_read_only(awen_home):
    from awen_agent import agent_tools, mcp_server

    expected = {
        "list_ad_adjustments", "get_ad_adjustment", "get_ad_adjustment_review",
        "get_parent_asin_adjustment_summary",
    }
    names = {row["function"]["name"] for row in agent_tools.TOOL_SCHEMAS}
    assert expected <= names
    assert expected <= agent_tools.READONLY_TOOLS
    assert expected <= set(agent_tools._DISPATCH)
    list_schema = next(row["function"]["parameters"]["properties"]
                       for row in agent_tools.TOOL_SCHEMAS
                       if row["function"]["name"] == "list_ad_adjustments")
    assert "source" in list_schema
    assert {"awen_adjustment_list", "awen_adjustment_detail"} <= set(mcp_server.TOOL_DEFS)
    assert "source" in mcp_server.TOOL_DEFS[
        "awen_adjustment_list"]["inputSchema"]["properties"]

    from awen_agent import adjustments
    action_id = adjustments.import_events([_event()])["action_ids"][0]
    result = mcp_server.call_tool("awen_adjustment_detail", {"id": action_id})
    assert result["isError"] is False
    assert result["structuredContent"]["action"]["id"] == action_id
    assert "raw" not in result["structuredContent"]["action"]


def test_cli_parser_and_schedule_task_contract():
    from awen_agent import cli, schedule

    args = cli.build_parser().parse_args(["adjustment", "list", "--sid", "7", "--json"])
    assert args.command == "adjustment"
    assert args.action == "list"
    assert "adjustment_review" in schedule.ALLOWED_TASKS


def test_scheduled_review_is_read_only_and_isolates_actions(awen_home, monkeypatch):
    from awen_agent import adjustment_report, adjustments, approvals, schedule

    one = adjustments.import_events([_event(source_event_id="one")])["action_ids"][0]
    two = adjustments.import_events([_event(source_event_id="two", object_id="c-8")])["action_ids"][0]
    monkeypatch.setattr(adjustments, "due_reviews", lambda **_: [
        {"action_id": one, "horizon_days": 3}, {"action_id": two, "horizon_days": 3}])

    def fake(action_id, horizon_days, **kwargs):
        if action_id == one:
            raise RuntimeError("one failed access_token=review-token-secret")
        return {"id": "review-two", "action_id": action_id, "horizon_days": horizon_days,
                "verdict": "neutral"}

    monkeypatch.setattr(adjustment_report, "evaluate_action", fake)
    before = approvals.summary()
    ok, text = schedule.run_task("adjustment_review", {"sid": "7"})
    after = approvals.summary()
    assert ok is False
    assert "成功 1" in text and "失败 1" in text
    assert "review-token-secret" not in text
    assert before == after


def test_scheduled_review_respects_explicit_multi_store_scope(awen_home, monkeypatch):
    from awen_agent import adjustment_report, schedule

    seen = []

    def fake_run_due(*, sid=None, today=None):
        seen.append(sid)
        return {"ok": True, "due": 0, "created": 0, "reviews": [], "failures": []}

    monkeypatch.setattr(adjustment_report, "run_due", fake_run_due)
    ok, _ = schedule.run_task(
        "adjustment_review", {"sids": ["1", "2", "3"], "exclude_sids": ["2"]})
    assert ok is True
    assert seen == ["1", "3"]


def test_scheduled_review_all_stores_passes_exclusions_to_ledger(awen_home, monkeypatch):
    from awen_agent import adjustment_report, schedule

    seen = []

    def fake_run_due(**kwargs):
        seen.append(kwargs)
        return {"ok": True, "due": 0, "created": 0, "reviews": [], "failures": []}

    monkeypatch.setattr(adjustment_report, "run_due", fake_run_due)
    ok, _ = schedule.run_task(
        "adjustment_review", {"sids": "all", "exclude_sids": ["2", "3"]})

    assert ok is True
    assert seen == [{"sid": None, "today": None, "exclude_sids": {"2", "3"}}]


def test_scheduled_sync_skips_store_without_ads_and_isolates_failures(awen_home, monkeypatch):
    from awen_agent import schedule, stores
    from awen_agent.operation_sources import lingxing

    monkeypatch.setattr(stores, "resolve_targets", lambda args: [
        {"sid": "1", "has_ads": False}, {"sid": "2", "has_ads": True},
        {"sid": "3", "has_ads": True},
    ])
    seen = []

    def fake_sync(self, **kwargs):
        seen.append(str(kwargs["sid"]))
        if str(kwargs["sid"]) == "2":
            raise RuntimeError("provider down access_token=schedule-token-secret")
        return {"ok": True, "created": 1, "duplicates": 0, "enriched": 0,
                "failures": [], "skipped": False}

    monkeypatch.setattr(lingxing.LingxingOperationSource, "sync", fake_sync)
    ok, text = schedule.run_task(
        "adjustment_sync", {"sids": "all", "start_date": "2026-08-01",
                            "end_date": "2026-08-02"})
    assert ok is False
    assert seen == ["2", "3"]
    assert "未配置广告" in text and "provider down" in text and "新增 1" in text
    assert "schedule-token-secret" not in text


def test_scheduled_sync_does_not_turn_explicit_empty_dimensions_into_defaults(
        awen_home, monkeypatch):
    from awen_agent import schedule, stores

    monkeypatch.setattr(stores, "resolve_targets", lambda args: [
        {"sid": "1", "has_ads": True},
    ])
    ok, text = schedule.run_task(
        "adjustment_sync", {
            "sid": "1", "start_date": "2026-08-01", "end_date": "2026-08-02",
            "sponsored_types": [], "operate_types": ["keywords"],
        })
    assert ok is False
    assert "不能为空" in text


def test_native_execution_helper_only_records_real_success(awen_home):
    from awen_agent import adjustments

    intent = {"sid": 7, "op_type": "campaign_budget", "campaign_id": "c7",
              "target": {"type": "campaign", "id": "c7"}, "before": {"budget": 10},
              "after": {"budget": 12}, "reason": "放量"}
    assert adjustments.record_native_execution(intent, {"ok": True, "dry_run": True})["created"] == 0
    assert adjustments.record_native_execution(intent, {"ok": False})["created"] == 0
    result = adjustments.record_native_execution(
        intent, {"ok": True, "dry_run": False, "audit_id": "audit-1"})
    assert result["created"] == 1
    action = adjustments.get_action(result["action_ids"][0])
    assert action["reason"] == "放量"
    assert action["action_type"] == "budget_increase"
    assert action["campaign_id"] == "c7"


def test_report_rejects_nonstandard_horizon(awen_home):
    import pytest
    from awen_agent import adjustment_report, adjustments

    action_id = adjustments.import_events([_event()])["action_ids"][0]
    with pytest.raises(ValueError, match="3、7、14 或 30"):
        adjustment_report.evaluate_action(action_id, 5, rows=[])


def test_stable_positive_uses_only_latest_earlier_revision(awen_home):
    from awen_agent import adjustment_report, adjustments

    action_id = adjustments.import_events([_event(source_event_id="revision-history")])["action_ids"][0]
    adjustments.record_review(action_id, 7, {"verdict": "positive_signal", "confidence": "medium"})
    adjustments.record_review(action_id, 7, {"verdict": "negative_signal", "confidence": "medium"})
    rows = []
    action_date = dt.date(2026, 8, 10)
    for offset in range(1, 15):
        rows.append({"date": (action_date - dt.timedelta(days=offset)).isoformat(),
                     "campaign_id": "c-7",
                     "clicks": 20, "orders": 3, "spend": 50, "sales": 150})
        rows.append({"date": (action_date + dt.timedelta(days=offset)).isoformat(),
                     "campaign_id": "c-7",
                     "clicks": 30, "orders": 6, "spend": 70, "sales": 300})

    review = adjustment_report.evaluate_action(
        action_id, 14, as_of="2026-08-26", rows=rows)
    assert review["verdict"] == "positive_signal"


def test_schedule_cli_keeps_adjustment_sync_store_scope(awen_home, monkeypatch):
    from awen_agent import cli, schedule

    captured = {}

    def fake_set_job(name, task, **kwargs):
        captured.update({"name": name, "task": task, **kwargs})
        return {"name": name, "task": task, "every_hours": kwargs["every_hours"]}

    monkeypatch.setattr(schedule, "set_job", fake_set_job)
    args = cli.build_parser().parse_args([
        "schedule", "set", "adjustment-sync", "adjustment_sync", "--sid", "7",
    ])
    assert args.func(args) == 0
    assert captured["args"]["sid"] == "7"


def test_http_adjustment_routes_and_bearer_auth(awen_home, monkeypatch):
    from awen_agent import service

    server = service.make_server("127.0.0.1", 0, api_token="test-secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def request(path, *, method="GET", body=None, authorized=True):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = "Bearer test-secret"
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        status, unauthorized = request("/v1/adjustments", authorized=False)
        assert status == 401 and unauthorized["error"] == "unauthorized"
        status, imported = request("/v1/adjustments/import", method="POST", body={"events": [_event()]})
        assert status == 200 and imported["created"] == 1
        action_id = imported["action_ids"][0]
        status, detail = request(f"/v1/adjustments/{action_id}")
        assert status == 200 and detail["action"]["id"] == action_id
        status, listing = request("/v1/adjustments?sid=7")
        assert status == 200 and listing["items"][0]["id"] == action_id
        status, bad = request(f"/v1/adjustments/{action_id}/unknown")
        assert status == 404 and bad["ok"] is False

        monkeypatch.setattr(service, "adjustment_sync", lambda _body: {
            "ok": False, "partial": True, "failures": [{"error": "one dimension failed"}],
        })
        status, partial = request("/v1/adjustments/sync", method="POST", body={})
        assert status == 207 and partial["partial"] is True

        monkeypatch.setattr(service, "adjustment_sync", lambda _body: {
            "ok": False, "partial": False, "failures": [{"error": "provider unavailable"}],
        })
        status, failed = request("/v1/adjustments/sync", method="POST", body={})
        assert status == 502 and failed["partial"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
