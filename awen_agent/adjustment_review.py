"""Deterministic, read-only review math for advertising adjustments."""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Iterable, Optional

HORIZONS = (3, 7, 14, 30)
CONFIDENCE_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def review_windows(action_date: dt.date | str, horizon_days: int) -> dict[str, Any]:
    action = dt.date.fromisoformat(action_date) if isinstance(action_date, str) else action_date
    horizon = int(horizon_days)
    if horizon <= 0:
        raise ValueError("horizon_days 必须大于 0")
    baseline = [action - dt.timedelta(days=offset) for offset in range(horizon, 0, -1)]
    evaluation = [action + dt.timedelta(days=offset) for offset in range(1, horizon + 1)]
    return {
        "action_date": action.isoformat(),
        "baseline_dates": [day.isoformat() for day in baseline],
        "evaluation_dates": [day.isoformat() for day in evaluation],
        "baseline": [baseline[0].isoformat(), baseline[-1].isoformat()],
        "evaluation": [evaluation[0].isoformat(), evaluation[-1].isoformat()],
        "t0_excluded": True,
        "horizon_days": horizon,
    }


def _number(value: Any) -> float:
    try:
        number = float(value or 0)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _ratio(numerator: float, denominator: float) -> Optional[float]:
    return round(numerator / denominator, 6) if denominator else None


def aggregate_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    totals = {key: 0.0 for key in (
        "impressions", "clicks", "spend", "orders", "sales", "sessions", "units")}
    count = 0
    for row in rows:
        count += 1
        for key in totals:
            totals[key] += _number(row.get(key))
    result: dict[str, Any] = {key: round(value, 6) for key, value in totals.items()}
    result.update({
        "ctr": _ratio(totals["clicks"], totals["impressions"]),
        "cvr": _ratio(totals["orders"], totals["clicks"]),
        "cpc": _ratio(totals["spend"], totals["clicks"]),
        "acos": _ratio(totals["spend"], totals["sales"]),
        "roas": _ratio(totals["sales"], totals["spend"]),
        "unit_session_rate": _ratio(totals["units"], totals["sessions"]),
        "order_session_rate": _ratio(totals["orders"], totals["sessions"]),
        "row_count": count,
    })
    return result


def aggregate_parent_scope(rows: Iterable[dict[str, Any]], *, parent_asin: str,
                           advertised_asins: set[str], child_asins: set[str]) -> dict[str, Any]:
    advertised = {str(v).upper() for v in advertised_asins}
    children = {str(v).upper() for v in child_asins}
    included = [row for row in rows if str(row.get("asin") or "").upper() in children]
    advertised_rows = [row for row in included if str(row.get("asin") or "").upper() in advertised]
    sibling_rows = [row for row in included if str(row.get("asin") or "").upper() not in advertised]
    return {
        "parent_asin": str(parent_asin).upper(),
        "advertised_asins": sorted(advertised & children),
        "unadvertised_asins": sorted(children - advertised),
        "advertised_children": aggregate_metrics(advertised_rows),
        "unadvertised_siblings": aggregate_metrics(sibling_rows),
        "parent_total": aggregate_metrics(included),
    }


def sample_confidence(before: dict[str, Any], after: dict[str, Any], *,
                      zero_order_clicks: int = 15, target_cpa: Optional[float] = None) -> str:
    orders = min(_number(before.get("orders")), _number(after.get("orders")))
    if orders >= 10:
        return "high"
    if orders >= 4:
        return "medium"
    if orders >= 2:
        return "low"
    clicks = min(_number(before.get("clicks")), _number(after.get("clicks")))
    spend = min(_number(before.get("spend")), _number(after.get("spend")))
    if orders == 0 and (clicks >= zero_order_clicks or (target_cpa and spend >= target_cpa)):
        return "low"
    return "none"


def _delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (
        "impressions", "clicks", "spend", "orders", "sales", "sessions", "units",
        "ctr", "cvr", "cpc", "acos", "roas", "unit_session_rate", "order_session_rate",
    ):
        left, right = before.get(key), after.get(key)
        if left is None or right is None:
            out[key] = {"before": left, "after": right, "absolute": None, "rate": None}
            continue
        absolute = _number(right) - _number(left)
        rate = absolute / abs(_number(left)) if _number(left) else None
        out[key] = {"before": left, "after": right, "absolute": round(absolute, 6),
                    "rate": round(rate, 6) if rate is not None else None}
    return out


def _direction_verdict(action_type: str, before: dict[str, Any], after: dict[str, Any]) -> str:
    kind = action_type.lower()
    b_orders, a_orders = _number(before.get("orders")), _number(after.get("orders"))
    b_sales, a_sales = _number(before.get("sales")), _number(after.get("sales"))
    b_spend, a_spend = _number(before.get("spend")), _number(after.get("spend"))
    b_acos, a_acos = before.get("acos"), after.get("acos")
    efficiency_better = ((b_acos is not None and a_acos is not None and a_acos < b_acos)
                         or (a_spend < b_spend and a_orders >= b_orders))
    efficiency_worse = ((b_acos is not None and a_acos is not None and a_acos > b_acos)
                        or (a_spend > b_spend and a_orders <= b_orders and a_sales <= b_sales))

    # 删除否定词会扩大流量，语义与新增否定词相反；必须先判这个特例，不能被
    # 字符串里的 ``negative`` 误归成控量动作。
    removes_negative = any(value in kind for value in (
        "negative_remove", "remove_negative", "negative_archive", "archive_negative"))
    adds_negative = any(value in kind for value in (
        "negative_add", "add_negative", "negate_keyword"))
    down = adds_negative or any(word in kind for word in (
        "decrease", "down", "pause", "disable", "reduce"))
    up = removes_negative or any(word in kind for word in (
        "increase", "up", "enable", "raise"))
    if down:
        if efficiency_better and a_orders >= b_orders:
            return "positive_signal"
        if a_orders < b_orders and a_sales < b_sales:
            return "negative_signal"
    elif up:
        if a_orders > b_orders and a_sales > b_sales and not efficiency_worse:
            return "positive_signal"
        if efficiency_worse:
            return "negative_signal"
    return "neutral"


def evaluate_change(action_type: str, before: dict[str, Any], after: dict[str, Any], *,
                    mature: bool = True, confounders: Optional[list[str]] = None,
                    data_confidence: str = "high", mapping_confidence: str = "high",
                    comparability_confidence: str = "high", target_cpa: Optional[float] = None,
                    changed_factors: Optional[list[str]] = None) -> dict[str, Any]:
    """Classify an observed before/after signal without making a causal claim."""
    warnings: list[str] = []
    factors = list(changed_factors or [])
    confounds = [str(v) for v in (confounders or []) if str(v).strip()]
    if len(factors) > 1:
        confounds.append("同一窗口存在多个调整变量")
    boundary = "这是时间窗相关性观察，不能归因为单次广告调整的因果结果。"
    sample = sample_confidence(before, after, target_cpa=target_cpa)

    if not mature:
        verdict = "waiting_for_data"
        warnings.append("评估窗尚未成熟，最近 1–2 天可能仍在归因回补")
    elif (not before or not after or (before.get("row_count") == 0)
          or (after.get("row_count") == 0) or data_confidence == "none"):
        verdict = "data_gap"
        data_confidence = "none"
        warnings.append("前窗、后窗缺少数据或日期覆盖不足")
    elif confounds:
        verdict = "confounded"
        comparability_confidence = "low"
        warnings.extend(confounds)
    elif sample == "none":
        verdict = "insufficient_sample"
        warnings.append("订单、点击和花费样本不足")
    else:
        verdict = _direction_verdict(action_type, before, after)

    parts = [sample, data_confidence, mapping_confidence, comparability_confidence]
    confidence = min(parts, key=lambda value: CONFIDENCE_ORDER.get(value, 0))
    if verdict in {"waiting_for_data", "data_gap", "insufficient_sample", "confounded"}:
        confidence = min(confidence, "low", key=lambda value: CONFIDENCE_ORDER.get(value, 0))
    return {
        "verdict": verdict,
        "confidence": confidence,
        "sample_confidence": sample,
        "data_confidence": data_confidence,
        "mapping_confidence": mapping_confidence,
        "comparability_confidence": comparability_confidence,
        "metrics": {"before": before, "after": after, "delta": _delta(before, after)},
        "warnings": warnings,
        "confounders": confounds,
        "boundary": boundary,
        "causality_claimed": False,
    }


def evaluate_rows(action_type: str, rows: Iterable[dict[str, Any]], *, action_date: dt.date | str,
                  horizon_days: int, as_of: Optional[dt.date] = None,
                  confounders: Optional[list[str]] = None,
                  mapping_confidence: str = "high") -> dict[str, Any]:
    window = review_windows(action_date, horizon_days)
    rows_list = list(rows)
    baseline_set = set(window["baseline_dates"])
    evaluation_set = set(window["evaluation_dates"])
    before = aggregate_metrics(row for row in rows_list if str(row.get("date")) in baseline_set)
    after = aggregate_metrics(row for row in rows_list if str(row.get("date")) in evaluation_set)
    baseline_days = len({str(row.get("date")) for row in rows_list
                         if str(row.get("date")) in baseline_set})
    evaluation_days = len({str(row.get("date")) for row in rows_list
                           if str(row.get("date")) in evaluation_set})
    coverage_ratio = min(baseline_days, evaluation_days) / max(1, int(horizon_days))
    if coverage_ratio >= 1:
        data_confidence = "high"
    elif coverage_ratio >= 0.8:
        data_confidence = "medium"
    elif coverage_ratio >= 0.5:
        data_confidence = "low"
    else:
        data_confidence = "none"
    end = dt.date.fromisoformat(window["evaluation"][1])
    current = as_of or dt.date.today()
    # One full day after the evaluation window protects the usual T+1 attribution lag.
    mature = current >= end + dt.timedelta(days=1)
    result = evaluate_change(action_type, before, after, mature=mature,
                             confounders=confounders, data_confidence=data_confidence,
                             mapping_confidence=mapping_confidence)
    result["window"] = window
    result["metrics"]["coverage"] = {
        "expected_days_each_window": int(horizon_days),
        "baseline_days": baseline_days,
        "evaluation_days": evaluation_days,
        "minimum_ratio": round(coverage_ratio, 6),
    }
    if coverage_ratio < 1:
        result["warnings"].append(
            f"日期覆盖不足：前窗 {baseline_days}/{horizon_days} 天，"
            f"后窗 {evaluation_days}/{horizon_days} 天")
    result["metrics"]["non_overlapping_trend"] = weighted_segment_metrics(
        rows_list, action_date=action_date, action_type=action_type, horizon_days=horizon_days)
    return result


def non_overlapping_segments(action_date: dt.date | str) -> list[dict[str, Any]]:
    """Return D1–D3, D4–D7, D8–D14 and D15–D30 score segments."""
    action = dt.date.fromisoformat(action_date) if isinstance(action_date, str) else action_date
    out = []
    for start, end in ((1, 3), (4, 7), (8, 14), (15, 30)):
        out.append({
            "label": f"D{start}-D{end}",
            "start": (action + dt.timedelta(days=start)).isoformat(),
            "end": (action + dt.timedelta(days=end)).isoformat(),
            "days": end - start + 1,
        })
    return out


def _segment_weights(action_type: str) -> tuple[float, float, float, float]:
    kind = str(action_type or "").lower()
    removes_negative = any(value in kind for value in (
        "negative_remove", "remove_negative", "negative_archive", "archive_negative"))
    adds_negative = any(value in kind for value in (
        "negative_add", "add_negative", "negate_keyword"))
    if adds_negative or any(word in kind for word in ("pause", "disable")):
        return (0.10, 0.20, 0.30, 0.40)
    if removes_negative or any(word in kind for word in (
            "increase", "scale", "enable", "raise")):
        return (0.20, 0.35, 0.30, 0.15)
    return (0.35, 0.30, 0.20, 0.15)


def weighted_segment_metrics(rows: Iterable[dict[str, Any]], *, action_date: dt.date | str,
                             action_type: str, horizon_days: int = 30) -> dict[str, Any]:
    """Score only disjoint post-action segments; never re-weight overlapping 3/7/14/30 totals."""
    materialized = list(rows)
    dated_rows: list[tuple[dict[str, Any], dt.date]] = []
    invalid_date_rows = 0
    for row in materialized:
        raw_date = row.get("date")
        try:
            if isinstance(raw_date, dt.datetime):
                parsed_date = raw_date.date()
            elif isinstance(raw_date, dt.date):
                parsed_date = raw_date
            else:
                parsed_date = dt.date.fromisoformat(str(raw_date))
        except (TypeError, ValueError):
            invalid_date_rows += 1
            continue
        dated_rows.append((row, parsed_date))
    segments = non_overlapping_segments(action_date)
    nominal = _segment_weights(action_type)
    segment_ends = {"D1-D3": 3, "D4-D7": 7, "D8-D14": 14, "D15-D30": 30}
    eligible = [(segment, weight) for segment, weight in zip(segments, nominal)
                if segment_ends[segment["label"]] <= int(horizon_days)]
    total_weight = sum(weight for _, weight in eligible) or 1.0
    weighted = {key: 0.0 for key in ("impressions", "clicks", "spend", "orders", "sales")}
    output_segments: list[dict[str, Any]] = []
    for segment, raw_weight in eligible:
        start, end = dt.date.fromisoformat(segment["start"]), dt.date.fromisoformat(segment["end"])
        selected = [row for row, row_date in dated_rows if start <= row_date <= end]
        metrics_for_segment = aggregate_metrics(selected)
        weight = raw_weight / total_weight
        for key in weighted:
            # Normalize each interval to a day before weighting, otherwise the
            # 16-day final segment wins merely because it contains more days.
            weighted[key] += _number(metrics_for_segment[key]) / segment["days"] * weight
        output_segments.append({**segment, "weight": round(weight, 6),
                                "coverage_days": len({str(r.get('date')) for r in selected}),
                                "metrics": metrics_for_segment})
    weighted_metrics = aggregate_metrics([weighted])
    weighted_metrics["row_count"] = len(output_segments)
    return {"segments": output_segments, "weighted_daily": weighted_metrics,
            "invalid_date_rows": invalid_date_rows,
            "method": "non_overlapping_weighted_daily_numerators"}
