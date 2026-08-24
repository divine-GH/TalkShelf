# note-brain 在 macOS / Linux 下的便捷入口（Windows 请用 start.ps1）。
# 通过 PY 覆盖解释器：make run PY=python3.12
PY      ?= python3
VENV    := .venv
VENV_PY := $(abspath $(VENV)/bin/python)

.PHONY: venv run test lint format check eval clean

# 创建虚拟环境并安装依赖（requirements-dev.txt 包含 requirements.txt）
venv:
	$(PY) -m venv $(VENV)
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -r requirements-dev.txt

# 运行（必须单 worker：异步队列在进程内存，见 README / 设计文档 §5）
run: venv
	cd backend && $(VENV_PY) -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1

test: venv
	cd backend && $(VENV_PY) -m pytest -q

lint: venv
	$(VENV_PY) -m ruff check .

format: venv
	$(VENV_PY) -m ruff format .

check: venv
	$(VENV_PY) -m ruff check .
	$(VENV_PY) -m ruff format --check .

eval: venv
	$(VENV_PY) scripts/eval_retrieval.py

clean:
	rm -rf $(VENV) .ruff_cache backend/.pytest_cache
