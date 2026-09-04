from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_image_audit import _png


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def no_rapidocr(monkeypatch):
    """强制 RapidOCR 不可用，把测试固定在 tesseract 分支。

    引擎探测是运行时做的且带模块级缓存，装没装 RapidOCR 的机器会走出两套行为；
    不钉死这一项，同一份测试在开发机（装了）和 CI（没装）上结论相反。
    """
    from awen_agent import ocr
    monkeypatch.setattr(ocr, "_RAPID_CACHE", None)
    monkeypatch.setattr(ocr, "_RAPID_FAILED", "forced-off")
    return ocr


def test_ocr_unavailable(tmp_path, monkeypatch, no_rapidocr):
    ocr = no_rapidocr

    img = tmp_path / "main.png"
    _png(img, 1200, 1200)
    monkeypatch.setattr("shutil.which", lambda name: None)
    res = ocr.run([str(tmp_path)])
    assert res["available"] is False
    assert res["images"]
    text = ocr.render(res)
    assert "OCR 不可用" in text


def test_ocr_available_and_agent_cli(tmp_path, monkeypatch, capsys, no_rapidocr):
    from awen_agent.agent_tools import TOOL_SCHEMAS, _DISPATCH, ToolContext
    from awen_agent.cli import main

    img = tmp_path / "feature.png"
    _png(img, 1200, 1200)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tesseract")

    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="tesseract 5.0\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="Waterproof karaoke machine\n")

    monkeypatch.setattr("subprocess.run", fake_run)
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert "run_image_ocr" in names
    out = _DISPATCH["run_image_ocr"]({"paths": [str(tmp_path)], "lang": "eng"}, ToolContext())
    assert "Waterproof karaoke machine" in out

    assert main(["image", "ocr", str(tmp_path), "--lang", "eng"]) == 0
    cli_out = capsys.readouterr().out
    assert "图片 OCR" in cli_out
    assert "Waterproof karaoke machine" in cli_out


def test_tesseract_rows_carry_empty_boxes(tmp_path, monkeypatch, no_rapidocr):
    """tesseract 分支必须给出空 boxes 而不是缺字段。

    local_vision 用 `row.get("boxes")` 判断能否推版面骨架；这里若不给字段，
    换引擎时下游要么 KeyError，要么拿 None 去迭代。
    """
    ocr = no_rapidocr
    _png(tmp_path / "a.png", 800, 800)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout="tesseract 5.0\n" if "--version" in cmd else "HELLO\n"))
    res = ocr.run([str(tmp_path)])
    assert res["available"] is True
    assert res["results"][0]["boxes"] == []
    assert res["results"][0]["text"] == "HELLO"


def test_rapidocr_branch_emits_boxes(tmp_path, monkeypatch):
    """RapidOCR 分支要把四点坐标压成 bbox，并带上原图尺寸供版面推断。"""
    from awen_agent import ocr

    _png(tmp_path / "b.png", 1000, 500)

    def fake_engine(path):
        return ([[[[10, 20], [110, 20], [110, 60], [10, 60]], "SALE 50%", 0.97]], 0.01)

    monkeypatch.setattr(ocr, "_RAPID_CACHE", fake_engine)
    monkeypatch.setattr(ocr, "_RAPID_FAILED", "")
    res = ocr.run([str(tmp_path)])
    assert res["available"] is True
    row = res["results"][0]
    assert row["ok"] is True
    assert row["text"] == "SALE 50%"
    assert row["boxes"][0]["bbox"] == [10.0, 20.0, 110.0, 60.0]
    assert row["image_width"] == 1000 and row["image_height"] == 500


def test_python_313_plus_uses_the_supported_rapidocr_distribution():
    """Python 3.13+ cannot resolve rapidocr-onnxruntime, so packaging must select v3."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'rapidocr-onnxruntime>=1.4; python_version < \'3.13\'' in pyproject
    assert 'rapidocr>=3.0,<4; python_version >= \'3.13\'' in pyproject


def test_modern_rapidocr_result_emits_boxes(tmp_path, monkeypatch):
    """RapidOCR v3 returns an object instead of the legacy ``(rows, elapsed)`` tuple."""
    from awen_agent import ocr

    _png(tmp_path / "modern.png", 640, 480)

    class ModernResult:
        boxes = [[[12, 24], [112, 24], [112, 64], [12, 64]]]
        txts = ("新品上市",)
        scores = (0.985,)

    monkeypatch.setattr(ocr, "_RAPID_CACHE", lambda path: ModernResult())
    monkeypatch.setattr(ocr, "_RAPID_FAILED", "")

    res = ocr.run([str(tmp_path)])

    assert res["available"] is True
    row = res["results"][0]
    assert row["ok"] is True
    assert row["text"] == "新品上市"
    assert row["boxes"] == [{
        "text": "新品上市",
        "score": 0.985,
        "bbox": [12.0, 24.0, 112.0, 64.0],
    }]


def test_loader_falls_back_to_the_modern_rapidocr_package(monkeypatch):
    """The v3 dependency is useful only if runtime probing imports its new module name."""
    from awen_agent import ocr

    engine = object()

    def fake_import(name):
        if name == "rapidocr_onnxruntime":
            raise ModuleNotFoundError(name)
        assert name == "rapidocr"
        return SimpleNamespace(RapidOCR=lambda: engine)

    monkeypatch.setattr(ocr, "_RAPID_CACHE", None)
    monkeypatch.setattr(ocr, "_RAPID_FAILED", "")
    monkeypatch.setattr(ocr, "_RAPID_DISTRIBUTION", "")
    monkeypatch.setattr(ocr.importlib, "import_module", fake_import)

    assert ocr._rapidocr() is engine
    assert ocr._RAPID_DISTRIBUTION == "rapidocr"
