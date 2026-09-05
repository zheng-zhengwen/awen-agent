"""UTF-8 must not depend on how the parent process was started."""
import subprocess
import sys

from awen_agent.subproc_env import build_env


def test_minimal_child_environment_is_utf8(monkeypatch):
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    env = build_env()
    assert env.get("PYTHONUTF8") == "1"
    assert env.get("PYTHONIOENCODING") == "utf-8"
    result = subprocess.run(
        [sys.executable, "-c", "print(chr(20013)+chr(25991)+chr(128640))"],
        env=env, capture_output=True, timeout=10, check=True)
    assert result.stdout.decode("utf-8").strip() == "中文🚀"
