from __future__ import annotations


def test_alerts_render_profile_warning(awen_home):
    from awen_agent import alerts

    rows = alerts.check(limit=20)
    assert any(a["code"] == "profile.incomplete" for a in rows)
    text = alerts.render(rows)
    assert "awen Alerts" in text
    assert "profile.incomplete" in text


def test_alert_cli(awen_home, capsys):
    from awen_agent.cli import main

    assert main(["alert", "check"]) == 0
    out = capsys.readouterr().out
    assert "awen Alerts" in out


def test_alert_cli_notify_stdout(awen_home, capsys):
    from awen_agent.cli import main

    assert main(["alert", "check", "--notify", "--channel", "stdout", "--title", "Ops"]) == 0
    out = capsys.readouterr().out
    assert "awen Alerts" in out
    assert "Ops" in out


def test_schedule_set_list_run_due(awen_home):
    from awen_agent import schedule

    job = schedule.set_job("daily-alert", "alert", every_hours=24)
    assert job["name"] == "daily-alert"
    assert "daily-alert" in schedule.render_jobs()

    rows = schedule.run_due(now=10_000_000)
    assert rows and rows[0]["job"] == "daily-alert"
    assert "awen Alerts" in rows[0]["output"]
    assert not schedule.due_jobs(now=10_000_000 + 10)

    assert schedule.remove_job("daily-alert") is True
    assert "暂无计划任务" in schedule.render_jobs()


def test_schedule_alert_notify_stdout(awen_home):
    from awen_agent import schedule

    ok, text = schedule.run_task("alert", {"notify": True, "channel": "stdout", "title": "Ops"})
    assert ok is True
    assert "awen Alerts" in text


def test_schedule_knowledge_sync(awen_home, monkeypatch):
    from awen_agent import schedule

    monkeypatch.setattr(schedule.knowledge_sync, "sync", lambda **kwargs: {
        "ok": True,
        "summary": {"selected": 1, "unchanged": 1},
        "results": [{"id": "sp_api.llms_index", "status": "unchanged"}],
    })
    monkeypatch.setattr(schedule.knowledge_sync, "render_sync", lambda data: "knowledge sync unchanged")
    job = schedule.set_job("official-updates", "knowledge_sync", every_hours=6)
    assert job["task"] == "knowledge_sync"
    ok, text = schedule.run_task("knowledge_sync")
    assert ok is True
    assert text == "knowledge sync unchanged"


def test_schedule_cli(awen_home, capsys):
    from awen_agent.cli import main

    assert main(["schedule", "set", "daily-eval", "eval", "--every-hours", "24"]) == 0
    assert "daily-eval" in capsys.readouterr().out
    assert main(["schedule", "list"]) == 0
    assert "daily-eval" in capsys.readouterr().out
    assert main(["schedule", "run", "eval"]) == 0
    assert "awen Agent Eval" in capsys.readouterr().out
