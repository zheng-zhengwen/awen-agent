"""Freeze parent/child ASIN scope for an adjustment using canonical snapshots."""
from __future__ import annotations

import time
from typing import Any

from . import metrics


def build_scope_index(sid: Any) -> dict[str, Any]:
    """Fetch product-ad and variation snapshots once for a synchronization run."""
    from . import datasources
    datasources.install_defaults()

    product_result = metrics.get_metric(metrics.ADS_PRODUCT_AD_CONFIG.key, {"sid": sid})
    variation_result = metrics.get_metric(metrics.CATALOG_VARIATION_SNAPSHOT.key, {"sid": sid})
    relation_rows: list[dict[str, Any]] = []
    relation_source = ""
    listing_result = None
    if variation_result.ok and variation_result.rows:
        relation_rows = variation_result.rows
        relation_source = metrics.CATALOG_VARIATION_SNAPSHOT.key
    else:
        # 当前领星 MCP 已核实能提供 listing.snapshot；保留它作为 canonical
        # variation source 接通前的兼容回退，不把供应商字段带到上层。
        listing_result = metrics.get_metric(metrics.LISTING_SNAPSHOT.key, {"sid": sid})
        if listing_result.ok and listing_result.rows:
            relation_rows = listing_result.rows
            relation_source = metrics.LISTING_SNAPSHOT.key
    ad_to_asin: dict[str, str] = {}
    campaign_to_asins: dict[str, set[str]] = {}
    ad_group_to_asins: dict[str, set[str]] = {}
    for row in product_result.rows if product_result.ok else []:
        state = str(row.get("state") or "").lower()
        asin = str(row.get("asin") or "").strip().upper()
        if not asin:
            continue
        ad_id = str(row.get("ad_id") or "")
        campaign_id = str(row.get("campaign_id") or "")
        ad_group_id = str(row.get("ad_group_id") or "")
        if ad_id:
            # 归档/暂停动作仍需靠 ad_id 找回自己的 ASIN。
            ad_to_asin[ad_id] = asin
        if state and state not in {"enabled", "active"}:
            continue
        if campaign_id:
            campaign_to_asins.setdefault(campaign_id, set()).add(asin)
        if ad_group_id:
            ad_group_to_asins.setdefault(ad_group_id, set()).add(asin)

    parent_by_child: dict[str, str] = {}
    children_by_parent: dict[str, set[str]] = {}
    for row in relation_rows:
        status = str(row.get("status") or row.get("status_text") or "").lower()
        if status in {"inactive", "deleted", "closed", "停售", "已删除"}:
            continue
        child = str(row.get("child_asin") or row.get("asin") or "").strip().upper()
        parent = str(row.get("parent_asin") or "").strip().upper()
        if child and parent:
            parent_by_child[child] = parent
            children_by_parent.setdefault(parent, set()).add(child)

    gaps: list[str] = []
    if not product_result.ok:
        gaps.append(product_result.gap.describe())
    elif not product_result.rows:
        gaps.append("投放商品快照为空")
    if not relation_rows:
        variation_gap = variation_result.gap.describe() if variation_result.gap else "父子变体快照为空"
        listing_gap = (listing_result.gap.describe() if listing_result and listing_result.gap
                       else "Listing 快照为空")
        gaps.extend((variation_gap, listing_gap))
    if ad_to_asin and parent_by_child and not gaps:
        confidence = "high"
    elif ad_to_asin:
        confidence = "medium" if parent_by_child else "low"
    else:
        confidence = "none"
    return {
        "ad_to_asin": ad_to_asin,
        "campaign_to_asins": campaign_to_asins,
        "ad_group_to_asins": ad_group_to_asins,
        "parent_by_child": parent_by_child,
        "children_by_parent": children_by_parent,
        "mapping_confidence": confidence,
        "gaps": gaps,
        "relation_source": relation_source,
    }


def scope_for_event(event: dict[str, Any], index: dict[str, Any]) -> list[dict[str, str]]:
    """Map an event to advertised children, siblings and one or more parents."""
    direct = str(event.get("advertised_asin") or "").strip().upper()
    object_type = str(event.get("object_type") or "")
    object_id = str(event.get("object_id") or "")
    campaign_id = str(event.get("campaign_id") or "")
    ad_group_id = str(event.get("ad_group_id") or "")

    advertised: set[str] = {direct} if direct else set()
    if object_type == "product_ad" and object_id:
        asin = (index.get("ad_to_asin") or {}).get(object_id)
        if asin:
            advertised.add(str(asin).upper())
    if object_type in {"keyword", "negative_keyword", "target", "negative_target", "ad_group"} \
            and ad_group_id:
        advertised.update(str(v).upper() for v in
                          (index.get("ad_group_to_asins") or {}).get(ad_group_id, set()))
    elif campaign_id:
        advertised.update(str(v).upper() for v in
                          (index.get("campaign_to_asins") or {}).get(campaign_id, set()))

    parent_by_child = index.get("parent_by_child") or {}
    children_by_parent = index.get("children_by_parent") or {}
    grouped: dict[str, set[str]] = {}
    unknown: set[str] = set()
    for asin in advertised:
        parent = str(parent_by_child.get(asin) or "").upper()
        if parent:
            grouped.setdefault(parent, set()).add(asin)
        else:
            unknown.add(asin)

    rows: list[dict[str, str]] = []
    for parent in sorted(grouped):
        advertised_for_parent = grouped[parent]
        children = {str(v).upper() for v in children_by_parent.get(parent, set())}
        children.update(advertised_for_parent)
        rows.append({"asin": parent, "role": "parent", "parent_asin": parent})
        rows.extend({"asin": child,
                     "role": "advertised_child" if child in advertised_for_parent else "unadvertised_sibling",
                     "parent_asin": parent} for child in sorted(children))
    rows.extend({"asin": asin, "role": "advertised_child", "parent_asin": ""}
                for asin in sorted(unknown))
    return rows


def enrich_events(events: list[dict[str, Any]], sid: Any) -> dict[str, Any]:
    """Add scope only when a source did not already provide its frozen snapshot."""
    index = build_scope_index(sid)
    enriched = 0
    captured_at = time.time()
    for event in events:
        if event.get("scope_asins"):
            continue
        scope = scope_for_event(event, index)
        if scope:
            for item in scope:
                item["snapshot_at"] = captured_at
            event["scope_asins"] = scope
            event["scope_mapping"] = {
                "confidence": index["mapping_confidence"], "gaps": index["gaps"],
                "basis": "ingestion_snapshot", "captured_at": captured_at,
                "action_time_exact": False,
            }
            evidence = dict(event.get("evidence") or {})
            evidence["scope_mapping"] = event["scope_mapping"]
            event["evidence"] = evidence
            enriched += 1
    return {"events": events, "enriched": enriched,
            "mapping_confidence": index["mapping_confidence"], "gaps": index["gaps"]}
