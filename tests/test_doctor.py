"""doctor command checks."""
from __future__ import annotations


def test_doctor_runs_and_renders(awen_home):
    from awen_agent import doctor

    checks = doctor.run_checks()
    names = {c.name for c in checks}
    assert "Python" in names
    assert "知识库" in names
    text = doctor.render(checks)
    assert "awen Agent Doctor" in text and "结果:" in text
