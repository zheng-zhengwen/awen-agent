"""Read-only, unknown metadata, and absent approval channels must never write."""
import pytest

from awen_agent import agent_tools


@pytest.mark.parametrize("plan,execute,channel", [
    (True, False, False), (True, True, True), (False, False, False),
    (False, False, True), (False, True, False),
])
def test_readonly_or_missing_approval_channel_never_writes(
        awen_home, monkeypatch, plan, execute, channel):
    reached = []

    def bridge(ctx, path, payload, timeout=20):
        if path == "/tools":
            return {"ok": True, "protocol_version": 2, "tools": [
                {"name": "listing_create_project", "destructive": True}]}
        reached.append(path)
        return {"ok": True}

    monkeypatch.setattr(agent_tools, "_ops_bridge_request", bridge)
    ctx = agent_tools.ToolContext(plan_mode=plan, execute=execute)
    ctx.ops_bridge = {"base_url": "http://127.0.0.1/api", "token": "test", "protocol_version": 2}
    if channel:
        ctx.perm.prompt_fn = lambda *args: "approve"
    agent_tools._t_awen_ops_call_tool({"name": "listing_create_project"}, ctx)
    assert reached == [], "unauthorized write reached the bridge"


@pytest.mark.parametrize("metadata", [[], [{"name": "unknown"}], [{"name": "unknown", "destructive": "false"}]])
def test_unknown_or_invalid_metadata_fails_closed(awen_home, monkeypatch, metadata):
    calls = []

    def bridge(ctx, path, payload, timeout=20):
        if path == "/tools":
            return {"ok": True, "protocol_version": 2, "tools": metadata}
        calls.append(path)
        return {"ok": True}

    monkeypatch.setattr(agent_tools, "_ops_bridge_request", bridge)
    ctx = agent_tools.ToolContext(execute=True)
    ctx.perm.accept_edits = True
    agent_tools._t_awen_ops_call_tool({"name": "unknown"}, ctx)
    assert calls == []
