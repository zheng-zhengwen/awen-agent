"""Minimal stdio MCP server exposing awenAgent read-only capabilities.

This deliberately avoids the official MCP SDK so the package can keep Python
3.9 support and a small install footprint. The server is intended for local
clients such as awenOps, Claude Desktop, Codex-like shells, or other agents.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable, TextIO

from . import __version__, knowledge, retrieval, service, skills, task_runner


JsonDict = dict[str, Any]


def _schema(properties: JsonDict | None = None, required: list[str] | None = None) -> JsonDict:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


TOOL_DEFS: dict[str, dict[str, Any]] = {
    "awen_health": {
        "description": "Return awenAgent version, model, knowledge and retrieval status.",
        "inputSchema": _schema(),
    },
    "awen_manifest": {
        "description": "Return the awenAgent local integration manifest.",
        "inputSchema": _schema(),
    },
    "awen_knowledge_search": {
        "description": "Search bundled and user Amazon operations knowledge cards.",
        "inputSchema": _schema({
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        }, ["query"]),
    },
    "awen_knowledge_audit": {
        "description": "Audit knowledge card source quality, freshness, license and conflicts.",
        "inputSchema": _schema(),
    },
    "awen_retrieval_search": {
        "description": "Search local knowledge, memory and persistent retrieval index.",
        "inputSchema": _schema({
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "sources": {"type": "array", "items": {"type": "string", "enum": ["knowledge", "memory"]}},
        }, ["query"]),
    },
    "awen_skill_search": {
        "description": "Search active awen built-in and user skills.",
        "inputSchema": _schema({
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        }, ["query"]),
    },
    "awen_system_doctor": {
        "description": "Run install/runtime doctor checks without exposing secrets.",
        "inputSchema": _schema(),
    },
    "awen_task_list": {
        "description": "List local long-running agent tasks and their current status.",
        "inputSchema": _schema({
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "status": {
                "type": "string",
                "enum": ["", "pending", "in_progress", "blocked", "completed", "cancelled"],
            },
        }),
    },
    "awen_task_detail": {
        "description": "Load one local agent task with steps and recent events.",
        "inputSchema": _schema({
            "id": {"type": "string"},
        }, ["id"]),
    },
    "awen_task_resume": {
        "description": "Return the read-only resume prompt/next step for one local agent task.",
        "inputSchema": _schema({
            "id": {"type": "string"},
        }, ["id"]),
    },
    "awen_trace_list": {
        "description": "List recent local agent timeline events and tool calls.",
        "inputSchema": _schema({
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            "session_id": {"type": "string"},
        }),
    },
    "awen_trace_stats": {
        "description": "Summarize recent local agent timeline/tool-call statistics.",
        "inputSchema": _schema({
            "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
        }),
    },
    "awen_adjustment_list": {
        "description": "List sanitized advertising adjustment events and latest review verdicts.",
        "inputSchema": _schema({
            "sid": {"type": "string"},
            "parent_asin": {"type": "string"},
            "child_asin": {"type": "string"},
            "campaign_id": {"type": "string"},
            "ad_group_id": {"type": "string"},
            "object_id": {"type": "string"},
            "object_type": {"type": "string"},
            "verdict": {"type": "string"},
            "source": {"type": "string"},
            "date_from": {"type": "string"},
            "date_to": {"type": "string"},
            "cursor": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }),
    },
    "awen_adjustment_detail": {
        "description": "Load one sanitized advertising adjustment and its append-only reviews.",
        "inputSchema": _schema({"id": {"type": "string"}}, ["id"]),
    },
    "awen_workspace_search": {
        "description": "Read-only project search over indexed files and symbols.",
        "inputSchema": _schema({
            "root": {"type": "string"},
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 80},
        }, ["query"]),
    },
    "awen_workspace_inspect": {
        "description": "Read-only project map, entrypoints, tests and risk summary.",
        "inputSchema": _schema({
            "root": {"type": "string"},
        }),
    },
    "awen_code_plan": {
        "description": "Build a deterministic read-only code task plan.",
        "inputSchema": _schema({
            "root": {"type": "string"},
            "goal": {"type": "string"},
        }, ["goal"]),
    },
    "awen_code_context": {
        "description": "Collect compact read-only code context for a task.",
        "inputSchema": _schema({
            "root": {"type": "string"},
            "goal": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        }, ["goal"]),
    },
    "awen_code_bundle": {
        "description": "Build a read-only multi-round code task bundle.",
        "inputSchema": _schema({
            "root": {"type": "string"},
            "goal": {"type": "string"},
            "test_output": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        }, ["goal"]),
    },
    "awen_code_repair": {
        "description": "Parse test output and generate a read-only repair plan.",
        "inputSchema": _schema({
            "root": {"type": "string"},
            "output": {"type": "string"},
        }, ["output"]),
    },
}


def list_tools() -> list[JsonDict]:
    return [{"name": name, **spec} for name, spec in TOOL_DEFS.items()]


def call_tool(name: str, arguments: JsonDict | None = None) -> JsonDict:
    args = arguments or {}
    dispatch: dict[str, Callable[[JsonDict], JsonDict]] = {
        "awen_health": lambda _: service.health(),
        "awen_manifest": lambda _: service.manifest(),
        "awen_knowledge_search": lambda p: {
            "ok": True,
            "results": knowledge.search(str(p.get("query") or ""), limit=_int(p.get("limit"), 5)),
        },
        "awen_knowledge_audit": lambda _: service.knowledge_audit(),
        "awen_retrieval_search": lambda p: {
            "ok": True,
            **retrieval.search(
                str(p.get("query") or ""),
                limit=_int(p.get("limit"), 8),
                sources=p.get("sources") if isinstance(p.get("sources"), list) else None,
            ),
        },
        "awen_skill_search": lambda p: service.skill_search(str(p.get("query") or ""), limit=_int(p.get("limit"), 8)),
        "awen_system_doctor": lambda _: service.system_doctor(),
        "awen_task_list": lambda p: service.task_list(
            limit=_int(p.get("limit"), 20),
            status=str(p.get("status") or ""),
        ),
        "awen_task_detail": lambda p: service.task_detail(str(p.get("id") or "")),
        "awen_task_resume": lambda p: _task_resume(str(p.get("id") or "")),
        "awen_trace_list": lambda p: service.trace_list(
            limit=_int(p.get("limit"), 50),
            session_id=str(p.get("session_id") or ""),
        ),
        "awen_trace_stats": lambda p: service.trace_stats(limit=_int(p.get("limit"), 1000)),
        "awen_adjustment_list": lambda p: service.adjustment_list({
            key: [str(value)] for key, value in p.items() if value not in (None, "")}),
        "awen_adjustment_detail": lambda p: service.adjustment_detail(str(p.get("id") or "")),
        "awen_workspace_search": service.workspace_search,
        "awen_workspace_inspect": service.workspace_inspect,
        "awen_code_plan": service.code_plan,
        "awen_code_context": service.code_context,
        "awen_code_bundle": service.code_bundle,
        "awen_code_repair": service.code_repair,
    }
    fn = dispatch.get(name)
    if not fn:
        return _tool_result({"ok": False, "error": f"unknown tool: {name}"}, is_error=True)
    try:
        data = fn(args)
    except Exception as exc:  # noqa: BLE001 - MCP tool calls must report errors as data.
        data = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return _tool_result(data, is_error=True)
    return _tool_result(data, is_error=not bool(data.get("ok", True)))


_KNOWLEDGE_URI = "awen-knowledge://"


def list_resources() -> list[JsonDict]:
    """Amazon 运营知识卡作为 MCP resources。"""
    out: list[JsonDict] = []
    for card in knowledge.list_cards():
        tags = card.get("tags") or []
        desc = card.get("category") or ""
        if tags:
            desc = (desc + " · " if desc else "") + ", ".join(str(t) for t in tags)
        out.append({
            "uri": f"{_KNOWLEDGE_URI}{card['id']}",
            "name": card.get("title") or card["id"],
            "description": desc,
            "mimeType": "text/markdown",
        })
    return out


def read_resource(uri: str) -> JsonDict:
    card_id = uri[len(_KNOWLEDGE_URI):] if uri.startswith(_KNOWLEDGE_URI) else uri
    card = knowledge.get_card(card_id)
    if not card:
        raise ValueError(f"unknown resource: {uri}")
    return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": card.get("body") or ""}]}


def list_prompts() -> list[JsonDict]:
    """awen Skills 作为 MCP prompt 模板。"""
    return [{
        "name": sk.id,
        "description": (f"{sk.title} — {sk.description}")[:200],
        "arguments": [],
    } for sk in skills.list_skills()]


def get_prompt(name: str, arguments: JsonDict | None = None) -> JsonDict:
    sk = skills.get_skill(name)
    if not sk:
        raise ValueError(f"unknown prompt: {name}")
    return {
        "description": sk.title,
        "messages": [{"role": "user", "content": {"type": "text", "text": skills.render_skill(sk)}}],
    }


def self_config() -> JsonDict:
    return {
        "transport": "stdio",
        "command": "awen",
        "args": ["mcp", "serve"],
        "note": "Read-only awenAgent MCP server. Write operations are not exposed.",
    }


def _task_resume(task_id: str) -> JsonDict:
    task = task_runner.load(task_id)
    return {
        "ok": True,
        "task_id": task_id,
        "resume": task_runner.render_resume(task),
        "progress": task_runner.progress(task),
        "next_step": task_runner.next_step(task) or {},
    }


def handle_message(message: JsonDict) -> JsonDict | None:
    msg_id = message.get("id")
    method = message.get("method")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if msg_id is None:
        return None
    try:
        if method == "initialize":
            return _response(msg_id, {
                "protocolVersion": "2025-06-18",
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {"name": "awen-agent", "version": __version__},
            })
        if method == "ping":
            return _response(msg_id, {})
        if method == "tools/list":
            return _response(msg_id, {"tools": list_tools()})
        if method == "tools/call":
            return _response(msg_id, call_tool(str(params.get("name") or ""), params.get("arguments") or {}))
        if method == "resources/list":
            return _response(msg_id, {"resources": list_resources()})
        if method == "resources/read":
            return _response(msg_id, read_resource(str(params.get("uri") or "")))
        if method == "prompts/list":
            return _response(msg_id, {"prompts": list_prompts()})
        if method == "prompts/get":
            return _response(msg_id, get_prompt(str(params.get("name") or ""), params.get("arguments") or {}))
        return _error(msg_id, -32601, f"Method not found: {method}")
    except Exception as exc:  # noqa: BLE001
        return _error(msg_id, -32603, f"{type(exc).__name__}: {exc}")


def serve_stdio(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for raw in stdin:
        if not raw.strip():
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        response = handle_message(message)
        if response is None:
            continue
        stdout.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
        stdout.flush()
    return 0


def _tool_result(data: JsonDict, *, is_error: bool = False) -> JsonDict:
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) > 12_000:
        text = text[:11_900] + "\n...truncated"
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": data,
        "isError": is_error,
    }


def _response(msg_id: Any, result: JsonDict) -> JsonDict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> JsonDict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
