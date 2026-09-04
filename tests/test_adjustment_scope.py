from __future__ import annotations


def test_campaign_scope_freezes_each_parent_and_child_cohort():
    from awen_agent.adjustment_scope import scope_for_event

    index = {
        "ad_to_asin": {"ad1": "A1", "ad2": "B1"},
        "campaign_to_asins": {"c1": {"A1", "B1"}},
        "ad_group_to_asins": {"g1": {"A1"}, "g2": {"B1"}},
        "parent_by_child": {"A1": "PA", "A2": "PA", "B1": "PB", "B2": "PB"},
        "children_by_parent": {"PA": {"A1", "A2"}, "PB": {"B1", "B2"}},
    }
    rows = scope_for_event({"object_type": "campaign", "campaign_id": "c1"}, index)
    shaped = {(r["asin"], r["role"], r["parent_asin"]) for r in rows}
    assert shaped == {
        ("PA", "parent", "PA"), ("A1", "advertised_child", "PA"),
        ("A2", "unadvertised_sibling", "PA"),
        ("PB", "parent", "PB"), ("B1", "advertised_child", "PB"),
        ("B2", "unadvertised_sibling", "PB"),
    }


def test_keyword_scope_uses_ad_group_not_whole_campaign():
    from awen_agent.adjustment_scope import scope_for_event

    index = {
        "ad_to_asin": {}, "campaign_to_asins": {"c1": {"A1", "B1"}},
        "ad_group_to_asins": {"g1": {"A1"}},
        "parent_by_child": {"A1": "PA", "A2": "PA", "B1": "PB"},
        "children_by_parent": {"PA": {"A1", "A2"}, "PB": {"B1"}},
    }
    rows = scope_for_event({"object_type": "keyword", "campaign_id": "c1", "ad_group_id": "g1"}, index)
    assert not any(row["asin"] in {"PB", "B1"} for row in rows)
    assert any(row["asin"] == "A2" and row["role"] == "unadvertised_sibling" for row in rows)


def test_unknown_parent_still_retains_advertised_asin():
    from awen_agent.adjustment_scope import scope_for_event

    rows = scope_for_event(
        {"object_type": "product_ad", "object_id": "ad1"},
        {"ad_to_asin": {"ad1": "A1"}, "campaign_to_asins": {},
         "ad_group_to_asins": {}, "parent_by_child": {}, "children_by_parent": {}})
    assert rows == [{"asin": "A1", "role": "advertised_child", "parent_asin": ""}]


def test_scope_index_prefers_canonical_variations_and_keeps_paused_ad_identity(monkeypatch):
    from awen_agent import adjustment_scope, metrics

    calls = []

    def result(metric, rows):
        return metrics.MetricResult(metric=metric, rows=rows)

    def fake_get(metric, scope, window=None):
        calls.append(metric)
        if metric == metrics.ADS_PRODUCT_AD_CONFIG.key:
            return result(metric, [{
                "ad_id": "ad-paused", "asin": "A1", "campaign_id": "c1",
                "ad_group_id": "g1", "state": "paused",
            }])
        if metric == metrics.CATALOG_VARIATION_SNAPSHOT.key:
            return result(metric, [{
                "parent_asin": "P1", "child_asin": "A1", "status": "active",
            }, {
                "parent_asin": "P1", "child_asin": "A2", "status": "active",
            }])
        raise AssertionError(f"unexpected fallback metric {metric}")

    monkeypatch.setattr(metrics, "get_metric", fake_get)
    monkeypatch.setattr("awen_agent.datasources.install_defaults", lambda: None)
    index = adjustment_scope.build_scope_index(1)
    rows = adjustment_scope.scope_for_event(
        {"object_type": "product_ad", "object_id": "ad-paused"}, index)

    assert calls == [metrics.ADS_PRODUCT_AD_CONFIG.key, metrics.CATALOG_VARIATION_SNAPSHOT.key]
    assert index["mapping_confidence"] == "high"
    assert {row["asin"] for row in rows} == {"P1", "A1", "A2"}
    assert "A1" not in index["campaign_to_asins"].get("c1", set())


def test_generic_scope_enrichment_is_marked_as_ingestion_snapshot(monkeypatch):
    from awen_agent import adjustment_scope

    monkeypatch.setattr(adjustment_scope, "build_scope_index", lambda _sid: {
        "ad_to_asin": {}, "campaign_to_asins": {"c1": {"A1"}},
        "ad_group_to_asins": {}, "parent_by_child": {"A1": "P1"},
        "children_by_parent": {"P1": {"A1", "A2"}},
        "mapping_confidence": "high", "gaps": [],
    })
    events = [{"campaign_id": "c1", "object_type": "campaign"}]

    result = adjustment_scope.enrich_events(events, 1)

    mapping = result["events"][0]["evidence"]["scope_mapping"]
    scope = result["events"][0]["scope_asins"]
    assert mapping["basis"] == "ingestion_snapshot"
    assert mapping["action_time_exact"] is False
    assert all(row["snapshot_at"] == mapping["captured_at"] for row in scope)
