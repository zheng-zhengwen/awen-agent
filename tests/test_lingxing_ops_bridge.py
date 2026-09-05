"""Embedded Lingxing patrols must reuse awenOps credentials through the bridge."""
from __future__ import annotations

import json

from awen_agent import agent_tools


def test_embedded_lingxing_patrol_uses_ops_bridge_not_local_credentials(monkeypatch):
    calls = []

    def fake_bridge(ctx, path, payload, timeout=80.0):
        calls.append((path, payload, timeout))
        name = payload["name"]
        if name == "lingxing_dashboard":
            return {"ok": True, "result": {"summary": {"spend": 12.5, "orders": 2}}}
        if name == "lingxing_optimizer":
            return {"ok": True, "result": {"count": 1, "candidates": [{"lever": "降bid"}]}}
        raise AssertionError(f"unexpected bridge tool: {name}")

    monkeypatch.setattr(agent_tools, "_ops_bridge_request", fake_bridge)

    # The embedded path must never consult the standalone ~/.awen credential store.
    from awen_agent import lingxing_openapi
    monkeypatch.setattr(
        lingxing_openapi,
        "is_configured",
        lambda: (_ for _ in ()).throw(AssertionError("standalone credentials were checked")),
    )

    ctx = agent_tools.ToolContext(
        ops_bridge={"base_url": "http://127.0.0.1:8001/api/awen-agent-bridge", "token": "bridge"}
    )
    output = agent_tools._t_run_patrol(
        {"from_lingxing": True, "sid": 113, "site": "US", "days": 3}, ctx
    )

    payload = json.loads(output)
    assert payload["ok"] is True
    assert payload["source"] == "awenOps_lingxing_bridge"
    assert payload["sid"] == 113 and payload["site"] == "US" and payload["days"] == 3
    assert payload["dashboard"]["summary"]["spend"] == 12.5
    assert payload["optimizer"]["count"] == 1
    assert [call[1]["name"] for call in calls] == ["lingxing_dashboard", "lingxing_optimizer"]
    assert calls[0][1]["arguments"] == {"sids": "113", "days": 3}
    assert calls[1][1]["arguments"] == {"sid": 113, "days": 3}
    assert ctx.asin == "sid:113"
    # Ops candidates must not be handed to the standalone direct-write executor.
    assert ctx.lingxing_result == {}


def test_embedded_lingxing_patrol_requires_sid_before_calling_bridge(monkeypatch):
    monkeypatch.setattr(
        agent_tools,
        "_ops_bridge_request",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("bridge should not be called")),
    )
    ctx = agent_tools.ToolContext(
        ops_bridge={"base_url": "http://127.0.0.1:8001/api/awen-agent-bridge", "token": "bridge"}
    )

    output = agent_tools._t_run_patrol({"from_lingxing": True, "days": 7}, ctx)

    assert "需要 sid" in output


def test_embedded_lingxing_patrol_reports_partial_bridge_failure(monkeypatch):
    def fake_bridge(_ctx, _path, payload, timeout=80.0):
        if payload["name"] == "lingxing_dashboard":
            return {"ok": True, "result": {"totals": {"spend": 9.0}}}
        return {"ok": False, "error": "optimizer_unavailable", "detail": "temporary failure"}

    monkeypatch.setattr(agent_tools, "_ops_bridge_request", fake_bridge)
    ctx = agent_tools.ToolContext(
        ops_bridge={"base_url": "http://127.0.0.1:8001/api/awen-agent-bridge", "token": "bridge"}
    )

    payload = json.loads(agent_tools._t_run_patrol(
        {"from_lingxing": True, "sid": 113, "days": 7}, ctx
    ))

    assert payload["ok"] is False
    assert payload["dashboard"]["totals"]["spend"] == 9.0
    assert payload["optimizer"]["error"] == "optimizer_unavailable"


def test_embedded_lingxing_patrol_rejects_extreme_window(monkeypatch):
    monkeypatch.setattr(
        agent_tools,
        "_ops_bridge_request",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("bridge should not be called")),
    )
    ctx = agent_tools.ToolContext(
        ops_bridge={"base_url": "http://127.0.0.1:8001/api/awen-agent-bridge", "token": "bridge"}
    )

    output = agent_tools._t_run_patrol(
        {"from_lingxing": True, "sid": 113, "days": 10_000}, ctx
    )

    assert "1 到 60" in output


def test_embedded_lingxing_patrol_keeps_large_result_valid_json(monkeypatch):
    large_campaigns = [{"name": "campaign-" + ("x" * 900), "spend": i} for i in range(20)]

    def fake_bridge(_ctx, _path, payload, timeout=80.0):
        if payload["name"] == "lingxing_dashboard":
            return {"ok": True, "result": {"by_campaign": large_campaigns}}
        return {"ok": True, "result": {
            "count": 1,
            "candidates": [{"lever": "否词", "target_name": "must-stay-visible"}],
        }}

    monkeypatch.setattr(agent_tools, "_ops_bridge_request", fake_bridge)
    ctx = agent_tools.ToolContext(
        ops_bridge={"base_url": "http://127.0.0.1:8001/api/awen-agent-bridge", "token": "bridge"}
    )

    output = agent_tools._t_run_patrol(
        {"from_lingxing": True, "sid": 113, "days": 14}, ctx
    )
    payload = json.loads(output)

    assert payload["optimizer"]["candidates"][0]["target_name"] == "must-stay-visible"
