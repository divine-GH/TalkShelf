"""输入框随机示例占位文案（examples 模块 + 记录页/问答页渲染）。

约定：示例来自 backend/app/data/examples_{kind}.txt（每行一条，# 注释行跳过），
页面每次渲染随机挑一条；文件缺失/为空时回退内置静态文案。
"""

import shutil
import time

from app import config, examples


def test_example_files_have_entries():
    """两个示例文件都至少有一条有效示例（保证随机有得挑）。"""
    for kind in ("record", "ask"):
        items = examples._load(kind)
        assert items, f"examples_{kind}.txt 应至少包含一条有效示例"


def test_random_example_comes_from_file(monkeypatch):
    """随机结果来自示例文件（固定 choice 取首条验证来源）。"""
    monkeypatch.setattr(examples.random, "choice", lambda items: items[0])
    assert examples.random_example("record") == examples._load("record")[0]
    assert examples.random_example("ask") == examples._load("ask")[0]


def test_random_example_fallback_when_files_missing(monkeypatch):
    """示例文件缺失时回退内置静态文案（页面 placeholder 不空）。"""
    monkeypatch.setattr(examples, "_EXAMPLES_DIR", examples._EXAMPLES_DIR / "no-such-dir")
    assert examples.random_example("record") == examples._FALLBACK["record"]
    assert examples.random_example("ask") == examples._FALLBACK["ask"]


def test_random_example_fallback_when_file_empty(monkeypatch):
    """示例文件存在但为空（或全是注释）时同样回退内置静态文案。

    临时目录按 conftest 约定自建（Path.mkdir()，不用 pytest 的 tmp_path——
    Windows 上 os.mkdir(mode=0o700) 会生成创建者都进不去的目录）。
    """
    d = config.BASE_DIR / f".nb-examples-test-{time.time_ns():x}"
    d.mkdir()
    try:
        (d / "examples_record.txt").write_text("# 只有注释\n\n", encoding="utf-8")
        (d / "examples_ask.txt").write_text("", encoding="utf-8")
        monkeypatch.setattr(examples, "_EXAMPLES_DIR", d)
        assert examples.random_example("record") == examples._FALLBACK["record"]
        assert examples.random_example("ask") == examples._FALLBACK["ask"]
    finally:
        shutil.rmtree(d)  # 不 ignore_errors：清理失败必须可见


def test_index_page_renders_random_placeholder(client, monkeypatch):
    """记录页 placeholder 来自示例文件（固定 choice 取首条后应渲染在页面里）。"""
    monkeypatch.setattr(examples.random, "choice", lambda items: items[0])
    resp = client.get("/")
    assert resp.status_code == 200
    assert f'placeholder="{examples._load("record")[0]}"' in resp.text


def test_ask_page_renders_random_placeholder(client, monkeypatch):
    """问答页 placeholder 来自示例文件（固定 choice 取首条后应渲染在页面里）。"""
    monkeypatch.setattr(examples.random, "choice", lambda items: items[0])
    resp = client.get("/ask")
    assert resp.status_code == 200
    assert f'placeholder="{examples._load("ask")[0]}"' in resp.text
