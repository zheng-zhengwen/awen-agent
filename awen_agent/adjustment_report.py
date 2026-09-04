"""Application service and text projections for adjustment reviews."""
from __future__ import annotations

import datetime as dt
import math
import statistics
from typing import Any, Iterable, Optional

from . import adjustment_review, adjustments, metrics


_METRIC_BY_OBJECT = {
    # metric, row identity field, action identity field.  The latter matters for
    # campaign/ad-group rows because action.object_id is not reliable in every source.
    "campaign": (metrics.ADS_CAMPAIGN_REPORT.key, "campaign_id", "campaign_id"),
    "ad_group": (metrics.ADS_AD_GROUP_REPORT.key, "ad_group_id", "ad_group_id"),
    "keyword": (metrics.ADS_KEYWORD_REPORT.key, "keyword_id", "object_id"),
    "target": (metrics.ADS_TARGET_REPORT.key, "target_id", "object_id"),
    "product_ad": (metrics.ADS_ADVERTISED_PRODUCT_REPORT.key, "ad_id", "object_id"),
}


def _metric_for_action(action: dict[str, Any]) -> tuple[str, str, str] | tuple[str, None, None] | None:
    object_type = str(action.get("object_type") or "")
    if object_type in {"negative_keyword", "negative_target"}:
        # Negative entities have no performance report of their own.  Their primary
        # observable scope is the affected ad group, falling back to its campaign.
        if action.get("ad_group_id"):
            return metrics.ADS_AD_GROUP_REPORT.key, "ad_group_id", "ad_group_id"
        if action.get("campaign_id"):
            return metrics.ADS_CAMPAIGN_REPORT.key, "campaign_id", "campaign_id"
        return None
    if object_type == "profile":
        return metrics.ADS_CAMPAIGN_REPORT.key, None, None
    return _METRIC_BY_OBJECT.get(object_type)


def _as_date(value: Any, default: Optional[dt.date] = None) -> dt.date:
    if isinstance(value, dt.date):
        return value
    raw = str(value or "").strip()
    if raw:
        return dt.date.fromisoformat(raw)
    return default or dt.date.today()


def _scope_groups(action: dict[str, Any]) -> dict[str, dict[str, set[str]]]:
    parents: set[str] = set()
    groups: dict[str, dict[str, set[str]]] = {}
    for row in action.get("scope_asins") or []:
        asin = str(row.get("asin") or "").upper()
        role = str(row.get("role") or "")
        if role == "parent":
            parents.add(asin)
            groups.setdefault(asin, {"advertised": set(), "children": set()})
    for row in action.get("scope_asins") or []:
        role = str(row.get("role") or "")
        if role == "parent":
            continue
        asin = str(row.get("asin") or "").upper()
        parent = str(row.get("parent_asin") or "").upper()
        if not parent and len(parents) == 1:
            parent = next(iter(parents))
        if not parent:
            continue
        group = groups.setdefault(parent, {"advertised": set(), "children": set()})
        group["children"].add(asin)
        if role == "advertised_child":
            group["advertised"].add(asin)
    direct = str(action.get("advertised_asin") or "").upper()
    if direct and len(groups) == 1:
        group = next(iter(groups.values()))
        group["advertised"].add(direct)
        group["children"].add(direct)
    return groups


def _window_metrics(rows: Iterable[dict[str, Any]], window: dict[str, Any]) -> dict[str, Any]:
    materialized = list(rows)
    before_dates = set(window["baseline_dates"])
    after_dates = set(window["evaluation_dates"])
    result = {
        "before": adjustment_review.aggregate_metrics(
            row for row in materialized if str(row.get("date")) in before_dates),
        "after": adjustment_review.aggregate_metrics(
            row for row in materialized if str(row.get("date")) in after_dates),
    }
    if not result["before"]["row_count"] or not result["after"]["row_count"]:
        result["status"] = "data_gap"
        result["warning"] = "前窗或后窗没有匹配行，不能把缺数解释为零表现"
    return result


def _mark_pair_gap(pair: dict[str, Any], *, row_path: tuple[str, ...] = ("row_count",)) -> dict[str, Any]:
    """Mark a contextual before/after pair when either side has no matching rows."""
    def count(side: str) -> int:
        value: Any = pair.get(side) or {}
        for key in row_path:
            value = value.get(key) if isinstance(value, dict) else None
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    if not count("before") or not count("after"):
        pair["status"] = "data_gap"
        pair["warning"] = "前窗或后窗没有匹配行，不能把缺数解释为零表现"
    return pair


def _purchased_scope_metrics(rows: Iterable[dict[str, Any]], *,
                             advertised_asins: set[str], child_asins: set[str]) -> dict[str, Any]:
    materialized = list(rows)
    advertised = {str(value).upper() for value in advertised_asins}
    children = {str(value).upper() for value in child_asins}

    # A campaign can advertise children from more than one parent.  First bind
    # each purchase back to the advertised child, then classify what was bought;
    # purchased_asin alone would mislabel cross-parent purchases as sibling halo.
    if any(str(row.get("advertised_asin") or "") for row in materialized):
        materialized = [
            row for row in materialized
            if str(row.get("advertised_asin") or "").upper() in advertised
        ]

    def purchased(row: dict[str, Any]) -> str:
        return str(row.get("purchased_asin") or "").upper()

    return {
        "advertised_child": adjustment_review.aggregate_metrics(
            row for row in materialized if purchased(row) in advertised),
        "unadvertised_sibling": adjustment_review.aggregate_metrics(
            row for row in materialized if purchased(row) in children - advertised),
        "parent_attributed_total": adjustment_review.aggregate_metrics(
            row for row in materialized if purchased(row) in children),
    }


def _finite_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _profit_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    materialized = list(rows)
    totals = {field: 0.0 for field in ("sales_amount", "ads_cost", "gross_profit")}
    for row in materialized:
        for field in totals:
            totals[field] += _finite_number(row.get(field)) or 0.0
    gross_rate = (totals["gross_profit"] / totals["sales_amount"]
                  if totals["sales_amount"] else None)
    return {
        **{key: round(value, 6) for key, value in totals.items()},
        "gross_rate": round(gross_rate, 6) if gross_rate is not None else None,
        "row_count": len(materialized),
    }


def _ranking_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    materialized = list(rows)

    def ranks(field: str) -> list[float]:
        values = [_finite_number(row.get(field)) for row in materialized]
        return [value for value in values if value is not None and value > 0]

    organic = ranks("organic_rank")
    advertising = ranks("ad_rank")
    return {
        "organic_rank_median": round(statistics.median(organic), 6) if organic else None,
        "ad_rank_median": round(statistics.median(advertising), 6) if advertising else None,
        "keyword_count": len({str(row.get("keyword") or "") for row in materialized
                              if str(row.get("keyword") or "")}),
        "row_count": len(materialized),
        "lower_rank_is_better": True,
    }


def _inventory_metrics(rows: Iterable[dict[str, Any]], child_asins: set[str]) -> dict[str, Any]:
    children = {str(value).upper() for value in child_asins}
    selected = [row for row in rows if str(row.get("asin") or "").upper() in children]
    quantities = ("fulfillable", "inbound_shipped", "inbound_working", "inbound_receiving",
                  "reserved", "unsellable")
    out = {field: round(sum(_finite_number(row.get(field)) or 0.0 for row in selected), 6)
           for field in quantities}
    days = [_finite_number(row.get("days_of_supply")) for row in selected]
    valid_days = [value for value in days if value is not None and value >= 0]
    fulfillable_by_asin: dict[str, list[float]] = {}
    for row in selected:
        asin = str(row.get("asin") or "").upper()
        fulfillable = _finite_number(row.get("fulfillable"))
        if fulfillable is not None:
            fulfillable_by_asin.setdefault(asin, []).append(fulfillable)
    out.update({
        "child_count": len({str(row.get("asin") or "").upper() for row in selected}),
        "stockout_children": sum(
            1 for values in fulfillable_by_asin.values() if sum(values) <= 0),
        "minimum_days_of_supply": min(valid_days) if valid_days else None,
        "row_count": len(selected),
    })
    return out


def _provenance(result: metrics.MetricResult) -> dict[str, Any]:
    if result.provenance:
        return {"metric": result.provenance.metric, "source": result.provenance.source,
                "source_label": result.provenance.source_label,
                "lag_seconds": result.provenance.lag_seconds,
                "row_count": result.provenance.row_count}
    return {"metric": result.metric, "gap": result.gap.describe() if result.gap else "无数据"}


def _filter_object_rows(action: dict[str, Any], rows: Iterable[dict[str, Any]],
                        id_field: Optional[str], action_field: Optional[str], *,
                        allow_prescoped: bool = False) -> list[dict[str, Any]]:
    materialized = list(rows)
    if not id_field:
        return materialized
    object_id = str(action.get(action_field or "object_id") or action.get("object_id") or "")
    if not object_id:
        return materialized if allow_prescoped else []
    # An upstream caller may submit a dataset already scoped to the action.  If
    # the identity column is absent from every row, filtering it would turn valid
    # evidence into a false data gap.  Mixed presence is still filtered strictly.
    if not any(id_field in row for row in materialized):
        return materialized if allow_prescoped else []
    return [row for row in materialized if str(row.get(id_field) or "") == object_id]


def _overlapping_action_confounders(action: dict[str, Any], window: dict[str, Any]) -> list[str]:
    listed = adjustments.list_actions(
        sid=action.get("sid"), campaign_id=str(action.get("campaign_id") or ""),
        date_from=str(window["baseline"][0]), date_to=str(window["evaluation"][1]), limit=200)
    start = dt.date.fromisoformat(window["baseline"][0])
    end = dt.date.fromisoformat(window["evaluation"][1])
    out: list[str] = []
    for other in listed["items"]:
        if other["id"] == action["id"]:
            continue
        try:
            day = dt.date.fromisoformat(str(other.get("action_date_local") or ""))
        except ValueError:
            continue
        same_object = (str(other.get("object_id") or "") == str(action.get("object_id") or ""))
        same_campaign = (action.get("campaign_id") and
                         str(other.get("campaign_id")) == str(action.get("campaign_id")))
        if start <= day <= end and (same_object or same_campaign):
            out.append(f"复盘窗口内还有调整 {other['id']}（{other.get('action_type') or 'update'}）")
    return out


def evaluate_action(action_id: str, horizon_days: int, *, as_of: Any = None,
                    rows: Optional[list[dict[str, Any]]] = None,
                    campaign_rows: Optional[list[dict[str, Any]]] = None,
                    ad_group_rows: Optional[list[dict[str, Any]]] = None,
                    business_rows: Optional[list[dict[str, Any]]] = None,
                    store_rows: Optional[list[dict[str, Any]]] = None,
                    purchased_rows: Optional[list[dict[str, Any]]] = None,
                    profit_rows: Optional[list[dict[str, Any]]] = None,
                    ranking_rows: Optional[list[dict[str, Any]]] = None,
                    inventory_rows: Optional[list[dict[str, Any]]] = None,
                    confounders: Optional[list[str]] = None) -> dict[str, Any]:
    """Evaluate one action and append a review revision.

    Callers may pass rows (upstream push/testing).  Without rows, the canonical
    metrics layer is queried, retaining the existing official→LingXing priority.
    """
    action = adjustments.get_action(action_id)
    if action is None:
        raise KeyError(action_id)
    horizon = int(horizon_days)
    if horizon not in adjustment_review.HORIZONS:
        raise ValueError("horizon_days 必须是 3、7、14 或 30")
    action_date = _as_date(action.get("action_date_local"))
    window = adjustment_review.review_windows(action_date, horizon)
    metric_info = _metric_for_action(action)
    provenance: dict[str, Any] = {}
    mapping_confidence = "high" if metric_info else "none"
    scope_mapping = (action.get("evidence") or {}).get("scope_mapping") or {}
    scope_confidence = str(scope_mapping.get("confidence") or "")
    if scope_confidence:
        mapping_confidence = min(
            mapping_confidence, scope_confidence,
            key=lambda value: adjustment_review.CONFIDENCE_ORDER.get(value, 0))
    supplied_rows = rows is not None
    supplied_campaign_rows = campaign_rows is not None
    supplied_ad_group_rows = ad_group_rows is not None
    supplied_business_rows = business_rows is not None
    supplied_purchased_rows = purchased_rows is not None
    supplied_profit_rows = profit_rows is not None
    supplied_ranking_rows = ranking_rows is not None
    supplied_inventory_rows = inventory_rows is not None
    object_source_rows: list[dict[str, Any]] = list(rows or [])
    campaign_rows = list(campaign_rows or [])
    ad_group_rows = list(ad_group_rows or [])

    if supplied_rows:
        provenance = {
            "metric": metric_info[0] if metric_info else "unmapped",
            "source": "caller_supplied",
            "source_label": "awenOps / API 调用方",
            "row_count": len(object_source_rows),
        }

    if rows is None:
        if metric_info is None:
            rows = []
            provenance = {"gap": f"对象类型 {action.get('object_type') or '(空)'} 尚无复盘指标映射"}
        else:
            from . import datasources
            datasources.install_defaults()
            metric_key = metric_info[0]
            dates = tuple(window["baseline_dates"] + window["evaluation_dates"])
            result = metrics.get_metric(metric_key, {"sid": action["sid"]}, metrics.Window(dates))
            rows = result.rows
            object_source_rows = result.rows
            provenance = _provenance(result)

    object_rows = (_filter_object_rows(
        action, rows or [], metric_info[1], metric_info[2], allow_prescoped=supplied_rows)
                   if metric_info else [])
    auto_confounds = _overlapping_action_confounders(action, window)
    result = adjustment_review.evaluate_rows(
        str(action.get("action_type") or "update"), object_rows,
        action_date=action_date, horizon_days=horizon, as_of=_as_date(as_of),
        confounders=list(confounders or []) + auto_confounds,
        mapping_confidence=mapping_confidence,
    )

    cohorts: dict[str, Any] = {
        "object": result["metrics"],
        "ad_group": {"status": "not_available"},
        "campaign": {"status": "not_available"},
        "parent": {"status": "not_mapped"},
        "purchased_products": {"status": "not_available"},
        "contexts": {
            "profit": {"status": "not_available"},
            "ranking": {"status": "not_available"},
            "inventory": {"status": "not_requested_automatically", "snapshot_only": True},
        },
        "store": {"status": "not_available"},
    }
    dates = tuple(window["baseline_dates"] + window["evaluation_dates"])
    campaign_id = str(action.get("campaign_id") or "")
    ad_group_id = str(action.get("ad_group_id") or "")
    if not supplied_campaign_rows:
        if metric_info and metric_info[0] == metrics.ADS_CAMPAIGN_REPORT.key:
            campaign_rows = object_source_rows
            cohorts["campaign_provenance"] = dict(provenance)
        elif not supplied_rows and (campaign_id or (metric_info and metric_info[1] is None)):
            campaign_result = metrics.get_metric(
                metrics.ADS_CAMPAIGN_REPORT.key, {"sid": action["sid"]}, metrics.Window(dates))
            campaign_rows = campaign_result.rows
            cohorts["campaign_provenance"] = _provenance(campaign_result)
    else:
        cohorts["campaign_provenance"] = {
            "metric": metrics.ADS_CAMPAIGN_REPORT.key,
            "source": "caller_supplied", "source_label": "awenOps / API 调用方",
            "row_count": len(campaign_rows),
        }
    if not supplied_ad_group_rows:
        if metric_info and metric_info[0] == metrics.ADS_AD_GROUP_REPORT.key:
            ad_group_rows = object_source_rows
            cohorts["ad_group_provenance"] = dict(provenance)
        elif not supplied_rows and ad_group_id:
            ad_group_result = metrics.get_metric(
                metrics.ADS_AD_GROUP_REPORT.key, {"sid": action["sid"]}, metrics.Window(dates))
            ad_group_rows = ad_group_result.rows
            cohorts["ad_group_provenance"] = _provenance(ad_group_result)
    else:
        cohorts["ad_group_provenance"] = {
            "metric": metrics.ADS_AD_GROUP_REPORT.key,
            "source": "caller_supplied", "source_label": "awenOps / API 调用方",
            "row_count": len(ad_group_rows),
        }

    if campaign_id:
        selected_campaign_rows = _filter_object_rows(
            action, campaign_rows, "campaign_id", "campaign_id",
            allow_prescoped=(supplied_campaign_rows or (
                supplied_rows and bool(metric_info)
                and metric_info[0] == metrics.ADS_CAMPAIGN_REPORT.key)),
        )
        cohorts["campaign"] = (_window_metrics(selected_campaign_rows, window)
                               if selected_campaign_rows else
                               {"status": "data_gap", "campaign_id": campaign_id})
    if ad_group_id:
        selected_ad_group_rows = _filter_object_rows(
            action, ad_group_rows, "ad_group_id", "ad_group_id",
            allow_prescoped=(supplied_ad_group_rows or (
                supplied_rows and bool(metric_info)
                and metric_info[0] == metrics.ADS_AD_GROUP_REPORT.key)),
        )
        cohorts["ad_group"] = (_window_metrics(selected_ad_group_rows, window)
                               if selected_ad_group_rows else
                               {"status": "data_gap", "ad_group_id": ad_group_id})

    groups = _scope_groups(action)
    scope_basis = str(scope_mapping.get("basis") or ("event_supplied" if groups else ""))
    scope_exact_value = scope_mapping.get("action_time_exact")
    scope_exact = bool(groups) if scope_exact_value is None else bool(scope_exact_value)
    if groups and business_rows is None and not supplied_rows:
        business_result = metrics.get_metric(
            metrics.BUSINESS_ASIN_DAILY.key, {"sid": action["sid"]}, metrics.Window(dates))
        business_rows = business_result.rows
        cohorts["parent_provenance"] = _provenance(business_result)
    if groups and business_rows:
        baseline_dates = set(window["baseline_dates"])
        evaluation_dates = set(window["evaluation_dates"])
        parent_groups: dict[str, Any] = {}
        for parent, group in groups.items():
            parent_groups[parent] = _mark_pair_gap({
                "before": adjustment_review.aggregate_parent_scope(
                    (r for r in business_rows if str(r.get("date")) in baseline_dates),
                    parent_asin=parent, advertised_asins=group["advertised"],
                    child_asins=group["children"]),
                "after": adjustment_review.aggregate_parent_scope(
                    (r for r in business_rows if str(r.get("date")) in evaluation_dates),
                    parent_asin=parent, advertised_asins=group["advertised"],
                    child_asins=group["children"]),
            }, row_path=("parent_total", "row_count"))
        cohorts["parent"] = {
            "groups": parent_groups, "scope_frozen_at_action": scope_exact,
            "scope_snapshot_basis": scope_basis,
        }
    elif groups:
        cohorts["parent"] = {"status": "data_gap", "scope_frozen_at_action": scope_exact,
                             "scope_snapshot_basis": scope_basis,
                             "groups": {parent: {
                                 "advertised_asins": sorted(group["advertised"]),
                                 "child_asins": sorted(group["children"]),
                             } for parent, group in groups.items()}}
    if supplied_business_rows:
        cohorts["parent_provenance"] = {
            "metric": metrics.BUSINESS_ASIN_DAILY.key,
            "source": "caller_supplied", "source_label": "awenOps / API 调用方",
            "row_count": len(business_rows or []),
        }

    if groups and purchased_rows is None and not supplied_rows:
        purchased_result = metrics.get_metric(
            metrics.ADS_PURCHASED_PRODUCT_REPORT.key,
            {"sid": action["sid"]}, metrics.Window(dates))
        purchased_rows = purchased_result.rows
        cohorts["purchased_products_provenance"] = _provenance(purchased_result)
    if groups and purchased_rows:
        scoped_purchase_rows = list(purchased_rows)
        for identity_field in ("campaign_id", "ad_group_id"):
            identity = str(action.get(identity_field) or "")
            if identity and any(identity_field in row for row in scoped_purchase_rows):
                scoped_purchase_rows = [
                    row for row in scoped_purchase_rows
                    if str(row.get(identity_field) or "") == identity]
        bound_rows = sum(bool(str(row.get("advertised_asin") or ""))
                         for row in scoped_purchase_rows)
        if not scoped_purchase_rows:
            cohorts["purchased_products"] = {
                "status": "data_gap",
                "warning": "购买商品输入中没有与本动作活动/广告组匹配的行",
            }
        elif len(groups) > 1 and not bound_rows:
            # 同一活动/广告组覆盖多个父体时，仅看 purchased_asin 无法知道购买是由
            # 哪个投放子体触发；硬分组会制造假的兄弟体 halo。
            cohorts["purchased_products"] = {
                "status": "data_gap", "attribution_binding": "missing_advertised_asin",
                "warning": "多父体投放缺少 advertised_asin，未进行跨父体购买归属",
            }
        else:
            baseline_dates = set(window["baseline_dates"])
            evaluation_dates = set(window["evaluation_dates"])
            purchase_groups: dict[str, Any] = {}
            for parent, group in groups.items():
                purchase_groups[parent] = _mark_pair_gap({
                    "before": _purchased_scope_metrics(
                        (row for row in scoped_purchase_rows
                         if str(row.get("date")) in baseline_dates),
                        advertised_asins=group["advertised"], child_asins=group["children"]),
                    "after": _purchased_scope_metrics(
                        (row for row in scoped_purchase_rows
                         if str(row.get("date")) in evaluation_dates),
                        advertised_asins=group["advertised"], child_asins=group["children"]),
                }, row_path=("parent_attributed_total", "row_count"))
            unbound_rows = len(scoped_purchase_rows) - bound_rows
            binding = ("advertised_asin" if bound_rows
                       else "single_parent_scope_fallback")
            binding_confidence = ("high" if bound_rows == len(scoped_purchase_rows)
                                  else "medium" if bound_rows else "low")
            cohorts["purchased_products"] = {
                "groups": purchase_groups,
                "advertised_and_purchased_asin_kept_separate": True,
                "attribution_binding": binding,
                "binding_confidence": binding_confidence,
                "unbound_rows_excluded": unbound_rows if bound_rows else 0,
            }
            if not bound_rows:
                cohorts["purchased_products"]["warning"] = (
                    "购买商品行缺少 advertised_asin，仅因当前范围只有一个父体而降级归组")
            elif unbound_rows:
                cohorts["purchased_products"]["warning"] = (
                    f"有 {unbound_rows} 行缺少 advertised_asin，已从购买归属统计中排除")
    elif groups:
        cohorts["purchased_products"] = {"status": "data_gap"}
    if supplied_purchased_rows:
        cohorts["purchased_products_provenance"] = {
            "metric": metrics.ADS_PURCHASED_PRODUCT_REPORT.key,
            "source": "caller_supplied", "source_label": "awenOps / API 调用方",
            "row_count": len(purchased_rows or []),
        }

    contexts = cohorts["contexts"]
    if groups and profit_rows is None and not supplied_rows:
        profit_result = metrics.get_metric(
            metrics.PROFIT_PARENT_ASIN.key,
            {"sid": action["sid"]}, metrics.Window(dates))
        profit_rows = profit_result.rows
        contexts["profit_provenance"] = _provenance(profit_result)
    if groups and ranking_rows is None and not supplied_rows:
        ranking_result = metrics.get_metric(
            metrics.RANKING_ASIN_KEYWORD_DAILY.key,
            {"sid": action["sid"]}, metrics.Window(dates))
        ranking_rows = ranking_result.rows
        contexts["ranking_provenance"] = _provenance(ranking_result)

    baseline_dates = set(window["baseline_dates"])
    evaluation_dates = set(window["evaluation_dates"])
    if groups and profit_rows:
        contexts["profit"] = {"groups": {
            parent: _mark_pair_gap({
                "before": _profit_metrics(
                    row for row in profit_rows
                    if str(row.get("parent_asin") or "").upper() == parent
                    and str(row.get("date")) in baseline_dates),
                "after": _profit_metrics(
                    row for row in profit_rows
                    if str(row.get("parent_asin") or "").upper() == parent
                    and str(row.get("date")) in evaluation_dates),
            }) for parent in groups
        }}
    elif groups:
        contexts["profit"] = {"status": "data_gap"}
    if supplied_profit_rows:
        contexts["profit_provenance"] = {
            "metric": metrics.PROFIT_PARENT_ASIN.key, "source": "caller_supplied",
            "source_label": "awenOps / API 调用方", "row_count": len(profit_rows or []),
        }

    if groups and ranking_rows:
        contexts["ranking"] = {"groups": {
            parent: _mark_pair_gap({
                "before": _ranking_metrics(
                    row for row in ranking_rows
                    if (str(row.get("parent_asin") or "").upper() == parent
                        or str(row.get("asin") or "").upper() in group["children"])
                    and str(row.get("date")) in baseline_dates),
                "after": _ranking_metrics(
                    row for row in ranking_rows
                    if (str(row.get("parent_asin") or "").upper() == parent
                        or str(row.get("asin") or "").upper() in group["children"])
                    and str(row.get("date")) in evaluation_dates),
            }) for parent, group in groups.items()
        }}
    elif groups:
        contexts["ranking"] = {"status": "data_gap"}
    if supplied_ranking_rows:
        contexts["ranking_provenance"] = {
            "metric": metrics.RANKING_ASIN_KEYWORD_DAILY.key, "source": "caller_supplied",
            "source_label": "awenOps / API 调用方", "row_count": len(ranking_rows or []),
        }

    if groups and inventory_rows:
        inventory_groups: dict[str, Any] = {}
        for parent, group in groups.items():
            current = _inventory_metrics(inventory_rows, group["children"])
            inventory_groups[parent] = ({"current": current} if current["row_count"] else {
                "status": "data_gap", "current": current,
                "warning": "库存输入中没有该父体子 ASIN 的匹配行",
            })
        contexts["inventory"] = {
            "groups": inventory_groups,
            "snapshot_only": True,
            "boundary": "库存是复盘时快照，不代表动作前后库存变化。",
        }
    elif groups and supplied_inventory_rows:
        contexts["inventory"] = {
            "status": "data_gap",
            "snapshot_only": True,
            "boundary": "调用方已提供库存快照输入，但没有可用数据；不能按零库存解释。",
        }
    if supplied_inventory_rows:
        contexts["inventory_provenance"] = {
            "metric": metrics.INVENTORY_FBA.key, "source": "caller_supplied",
            "source_label": "awenOps / API 调用方", "row_count": len(inventory_rows or []),
        }
    if store_rows:
        cohorts["store"] = _window_metrics(store_rows, window)
    elif store_rows is not None:
        cohorts["store"] = {"status": "data_gap"}
    if store_rows is not None:
        cohorts["store_provenance"] = {
            "source": "caller_supplied", "source_label": "awenOps / API 调用方",
            "row_count": len(store_rows),
        }
    elif campaign_rows:
        cohorts["store"] = _window_metrics(campaign_rows, window)

    payload = {**result, "cohorts": cohorts, "provenance": provenance,
               "segments": adjustment_review.non_overlapping_segments(action_date)}
    if payload["verdict"] == "positive_signal" and horizon >= 14:
        # get_action() already projects only the latest revision of each earlier
        # horizon.  Superseded positives must never promote a later result.
        earlier = action.get("reviews") or []
        if any(row["horizon_days"] < horizon and row["verdict"] in
               {"positive_signal", "stable_positive"} for row in earlier):
            payload["verdict"] = "stable_positive"
    return adjustments.record_review(action_id, horizon, payload)


def run_due(*, sid: Any = None, today: Any = None,
            exclude_sids: Iterable[Any] = ()) -> dict[str, Any]:
    current = _as_date(today) if today else dt.date.today()
    due = adjustments.due_reviews(
        today=current, sid=sid, exclude_sids=exclude_sids)
    reviews: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for item in due:
        try:
            reviews.append(evaluate_action(
                item["action_id"], item["horizon_days"], as_of=current))
        except Exception as exc:  # noqa: BLE001 - one action must not stop the batch
            failures.append({
                **item,
                "error": str(adjustments.scrub_sensitive(f"{type(exc).__name__}: {exc}")),
            })
    return {"ok": not failures, "due": len(due), "created": len(reviews),
            "reviews": reviews, "failures": failures}


def render_list(items: Iterable[dict[str, Any]]) -> str:
    rows = list(items)
    if not rows:
        return "（暂无广告调整记录）\n"
    lines = []
    for row in rows:
        latest = (row.get("reviews") or [])[-1:] or [{}]
        verdict = latest[0].get("verdict") or "待复盘"
        target = row.get("object_name") or row.get("object_id") or "(未知对象)"
        lines.append(
            f"{row['id']}  {row.get('action_date_local')}  sid={row.get('sid')}  "
            f"{row.get('action_type') or row.get('operate_type')}  {target}  [{verdict}]"
        )
    return "\n".join(lines) + "\n"


def render_summary(data: dict[str, Any]) -> str:
    verdicts = " ".join(f"{key}={value}" for key, value in sorted((data.get("verdicts") or {}).items()))
    return (f"广告调整 {data.get('actions', 0)} 条；缺少调整理由 {data.get('missing_reason', 0)} 条；"
            f"复盘：{verdicts or '暂无'}\n")
