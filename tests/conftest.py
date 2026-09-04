"""测试隔离：把 AWEN_HOME 指向临时目录，绝不触碰真实 ~/.awen。

写测试的约定（踩过的坑，勿再犯）：
- **跨平台路径**：断言路径时别硬编码正斜杠 `"a/b/c"`——Windows 上工具输出可能是反斜杠。
  工具的路径输出统一走 `Path.as_posix()`（正斜杠），断言也用同款或 `os.sep`-无关写法；
  CI 跑 ubuntu/macos/**windows** 三平台，本地只在一个平台过 ≠ 全绿。
  （历史：`t_grep` 曾用原生分隔符输出，Windows CI 上 `sub\\c.ts` 让断言 `sub/c.ts` 失败。）
- **隔离**：碰 ~/.awen 的用例用下面的 `awen_home` fixture；碰 git 的用例在临时目录里 init，
  绝不在真实仓库跑 git 写操作。
"""
from __future__ import annotations

import importlib
import tempfile

import pytest


@pytest.fixture()
def awen_home(monkeypatch):
    """每个用例一个干净的临时 ~/.awen。返回该目录 Path。"""
    import sys
    d = tempfile.mkdtemp(prefix="awen_test_")
    monkeypatch.setenv("AWEN_HOME", d)
    # config 在 import 时按 AWEN_HOME 定目录；依赖它路径的模块也要重载
    from awen_agent import config
    importlib.reload(config)
    policy_file = config.AWEN_DIR / "policy.json"
    if policy_file.exists():
        policy_file.unlink()
    for mod in ("awen_agent.memory", "awen_agent.memory_core", "awen_agent.memory_store",
                "awen_agent.lingxing_openapi",
                "awen_agent.lingxing_cache", "awen_agent.pricing",
                "awen_agent.sessions", "awen_agent.audit", "awen_agent.shadow",
                "awen_agent.action_queue", "awen_agent.doctor", "awen_agent.profiles",
                "awen_agent.snapshots", "awen_agent.intraday", "awen_agent.approvals",
                "awen_agent.feishu_client", "awen_agent.reliability",
                "awen_agent.alert_state", "awen_agent.amazon_auth",
                "awen_agent.serve_workers", "awen_agent.evidence_ledger",
                "awen_agent.adjustments",
                # 下面这些同样在模块级绑定 config.AWEN_DIR；不重载会跨用例泄漏，
                # 甚至写到真实 ~/.awen（用 grep "= config.AWEN_DIR /" 可复查）
                "awen_agent.log", "awen_agent.schedule", "awen_agent.workspace",
                "awen_agent.stores",
                "awen_agent.self_manage", "awen_agent.task_runner",
                "awen_agent.code_agent", "awen_agent.tools_general",
                "awen_agent.traces", "awen_agent.policy"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    yield config.AWEN_DIR
