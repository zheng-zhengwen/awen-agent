from __future__ import annotations


def test_product_evals_pass():
    from awen_agent import evals

    result = evals.run()
    assert result["ok"], evals.render(result)
    assert any(c["name"] == "skill.recall" for c in result["checks"])
    assert any(c["name"] == "skill.no_false_inject" for c in result["checks"])
    assert any(c["name"] == "knowledge.registration_official_recall" for c in result["checks"])
    assert any(c["name"] == "knowledge.listing_error_citation" for c in result["checks"])


def test_eval_cli(capsys):
    from awen_agent.cli import main

    assert main(["eval"]) == 0
    out = capsys.readouterr().out
    assert "awen Agent Eval" in out
    assert "result: PASS" in out
