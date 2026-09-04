from __future__ import annotations

import datetime as dt


def test_windows_are_equal_and_exclude_action_day():
    from awen_agent.adjustment_review import review_windows

    window = review_windows(dt.date(2026, 8, 10), 7)
    assert window["baseline_dates"] == [
        "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06",
        "2026-08-07", "2026-08-08", "2026-08-09",
    ]
    assert window["evaluation_dates"] == [
        "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14",
        "2026-08-15", "2026-08-16", "2026-08-17",
    ]
    assert "2026-08-10" not in window["baseline_dates"] + window["evaluation_dates"]


def test_aggregate_recomputes_rates_from_sums():
    from awen_agent.adjustment_review import aggregate_metrics

    result = aggregate_metrics([
        {"impressions": 100, "clicks": 10, "spend": 20, "orders": 2, "sales": 100},
        {"impressions": 900, "clicks": 10, "spend": 80, "orders": 8, "sales": 400},
    ])
    assert result["ctr"] == 0.02
    assert result["cvr"] == 0.5
    assert result["cpc"] == 5.0
    assert result["acos"] == 0.2
    assert result["roas"] == 5.0


def test_parent_scope_keeps_advertised_and_unadvertised_children_separate():
    from awen_agent.adjustment_review import aggregate_parent_scope

    rows = [
        {"asin": "A1", "sales": 100, "orders": 2, "spend": 20},
        {"asin": "A2", "sales": 200, "orders": 4, "spend": 0},
        {"asin": "OUTSIDE", "sales": 900, "orders": 9, "spend": 90},
    ]
    result = aggregate_parent_scope(
        rows, parent_asin="P1", advertised_asins={"A1"}, child_asins={"A1", "A2"})
    assert result["advertised_children"]["sales"] == 100
    assert result["unadvertised_siblings"]["sales"] == 200
    assert result["parent_total"]["sales"] == 300
    assert result["parent_asin"] == "P1"


def test_parent_scope_includes_whole_link_sessions_units_and_conversion():
    from awen_agent.adjustment_review import aggregate_parent_scope

    result = aggregate_parent_scope([
        {"asin": "A1", "sessions": 100, "units": 10, "orders": 8, "sales": 200},
        {"asin": "A2", "sessions": 50, "units": 5, "orders": 4, "sales": 100},
    ], parent_asin="P1", advertised_asins={"A1"}, child_asins={"A1", "A2"})

    assert result["parent_total"]["sessions"] == 150
    assert result["parent_total"]["units"] == 15
    assert result["parent_total"]["unit_session_rate"] == 0.1


def test_evaluation_is_observational_and_confounders_win():
    from awen_agent.adjustment_review import evaluate_change

    result = evaluate_change(
        action_type="bid_decrease",
        before={"clicks": 30, "orders": 5, "spend": 100, "sales": 250},
        after={"clicks": 28, "orders": 5, "spend": 80, "sales": 260},
        confounders=["同窗口调整了售价"],
    )
    assert result["verdict"] == "confounded"
    assert result["causality_claimed"] is False
    assert "不能归因" in result["boundary"]


def test_evaluation_handles_maturity_gap_sample_and_direction():
    from awen_agent.adjustment_review import evaluate_change

    common = dict(
        action_type="bid_decrease",
        before={"clicks": 30, "orders": 5, "spend": 100, "sales": 250},
        after={"clicks": 28, "orders": 5, "spend": 80, "sales": 260},
    )
    assert evaluate_change(**common, mature=False)["verdict"] == "waiting_for_data"
    assert evaluate_change("bid_decrease", {}, {})["verdict"] == "data_gap"
    assert evaluate_change(
        "bid_decrease", {"clicks": 2, "orders": 0, "spend": 1, "sales": 0},
        {"clicks": 3, "orders": 0, "spend": 2, "sales": 0})["verdict"] == "insufficient_sample"
    assert evaluate_change(**common)["verdict"] == "positive_signal"
    assert evaluate_change(
        "bid_increase",
        {"clicks": 30, "orders": 5, "spend": 100, "sales": 250},
        {"clicks": 45, "orders": 5, "spend": 160, "sales": 240})["verdict"] == "negative_signal"


def test_removing_negative_is_evaluated_as_traffic_expansion():
    from awen_agent.adjustment_review import evaluate_change

    result = evaluate_change(
        "negative_remove",
        {"clicks": 30, "orders": 4, "spend": 60, "sales": 160},
        {"clicks": 50, "orders": 7, "spend": 90, "sales": 260},
    )
    assert result["verdict"] == "positive_signal"


def test_zero_denominators_are_none_not_infinity():
    from awen_agent.adjustment_review import aggregate_metrics

    result = aggregate_metrics([{"impressions": 0, "clicks": 0, "spend": 10,
                                 "orders": 0, "sales": 0}])
    assert result["ctr"] is None
    assert result["cvr"] is None
    assert result["cpc"] is None
    assert result["acos"] is None
    assert result["roas"] == 0.0


def test_formal_trend_uses_non_overlapping_action_specific_weights():
    from awen_agent.adjustment_review import weighted_segment_metrics

    rows = []
    for day in range(1, 31):
        rows.append({"date": f"2026-09-{day:02d}", "impressions": 100, "clicks": 10,
                     "spend": day, "orders": 1, "sales": 10})
    bid = weighted_segment_metrics(rows, action_date="2026-08-31",
                                   action_type="bid_decrease", horizon_days=30)
    negative = weighted_segment_metrics(rows, action_date="2026-08-31",
                                        action_type="negative_add", horizon_days=30)
    assert [row["label"] for row in bid["segments"]] == [
        "D1-D3", "D4-D7", "D8-D14", "D15-D30"]
    assert [row["weight"] for row in bid["segments"]] == [0.35, 0.3, 0.2, 0.15]
    assert [row["weight"] for row in negative["segments"]] == [0.1, 0.2, 0.3, 0.4]
    assert bid["weighted_daily"]["acos"] < negative["weighted_daily"]["acos"]


def test_budget_decrease_and_unknown_budget_change_do_not_use_scale_up_weights():
    from awen_agent.adjustment_review import weighted_segment_metrics

    rows = [{"date": f"2026-09-{day:02d}", "spend": day, "sales": 10}
            for day in range(1, 31)]
    budget_down = weighted_segment_metrics(
        rows, action_date="2026-08-31", action_type="budget_decrease", horizon_days=30)
    budget_unknown = weighted_segment_metrics(
        rows, action_date="2026-08-31", action_type="budget_change", horizon_days=30)
    budget_up = weighted_segment_metrics(
        rows, action_date="2026-08-31", action_type="budget_increase", horizon_days=30)

    assert [item["weight"] for item in budget_down["segments"]] == [0.35, 0.3, 0.2, 0.15]
    assert [item["weight"] for item in budget_unknown["segments"]] == [0.35, 0.3, 0.2, 0.15]
    assert [item["weight"] for item in budget_up["segments"]] == [0.2, 0.35, 0.3, 0.15]


def test_negative_remove_uses_expansion_weights_not_negative_add_weights():
    from awen_agent.adjustment_review import weighted_segment_metrics

    rows = [{"date": f"2026-09-{day:02d}", "spend": day, "sales": 10}
            for day in range(1, 31)]
    removed = weighted_segment_metrics(
        rows, action_date="2026-08-31", action_type="negative_remove", horizon_days=30)
    added = weighted_segment_metrics(
        rows, action_date="2026-08-31", action_type="negative_add", horizon_days=30)

    assert [item["weight"] for item in removed["segments"]] == [0.2, 0.35, 0.3, 0.15]
    assert [item["weight"] for item in added["segments"]] == [0.1, 0.2, 0.3, 0.4]


def test_formal_trend_skips_malformed_dates_without_losing_diagnostics():
    from awen_agent.adjustment_review import weighted_segment_metrics

    result = weighted_segment_metrics(
        [
            {"date": "not-a-date", "spend": 999, "sales": 1},
            {"date": None, "spend": 999, "sales": 1},
            {"date": "2026-09-01", "spend": 10, "sales": 100},
        ],
        action_date="2026-08-31",
        action_type="bid_decrease",
        horizon_days=3,
    )

    assert result["invalid_date_rows"] == 2
    assert result["segments"][0]["metrics"]["spend"] == 10


def test_sparse_window_is_a_data_gap_even_when_sample_totals_are_large():
    from awen_agent.adjustment_review import evaluate_rows

    result = evaluate_rows(
        "bid_increase",
        [
            {"date": "2026-08-09", "clicks": 100, "orders": 20, "spend": 100, "sales": 500},
            {"date": "2026-08-11", "clicks": 200, "orders": 40, "spend": 200, "sales": 1000},
        ],
        action_date="2026-08-10", horizon_days=7, as_of=dt.date(2026, 8, 19),
    )

    assert result["verdict"] == "data_gap"
    assert result["data_confidence"] == "none"
    assert result["metrics"]["coverage"]["baseline_days"] == 1
    assert any("覆盖" in warning for warning in result["warnings"])
