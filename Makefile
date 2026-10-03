# Fengcode 常用操作
#
# 用法：make <目标>
# Windows 上如果没有 make，直接照抄对应命令在 PowerShell 里跑即可。

PY ?= python
NPM ?= npm

.PHONY: help install test check serve chat build-cli build-desktop dist clean

help:
	@echo "Fengcode 常用操作："
	@echo "  make install         安装命令行端依赖（含开发依赖）"
	@echo "  make serve           启动本地服务并打开网页界面"
	@echo "  make chat            进入命令行对话"
	@echo "  make test            跑单元测试"
	@echo "  make check           跑全部静态检查（语法/引用/字段一致性）"
	@echo "  make build-cli       打包命令行端 exe"
	@echo "  make build-desktop   打包桌面端安装包"
	@echo "  make dist            整理交付目录（预演，不写入）"
	@echo "  make dist-apply      整理交付目录（实际写入）"
	@echo "  make clean           清理构建产物"

install:
	cd cli && $(PY) -m pip install -e ".[dev]"

serve:
	cd cli && $(PY) -m fengcode.cli.main serve --open

chat:
	cd cli && $(PY) -m fengcode.cli.main chat

test:
	cd cli && $(PY) -m pytest tests/ -q

check:
	cd cli && $(PY) scripts/check_js_syntax.py
	cd cli && $(PY) scripts/check_refs.py
	cd cli && $(PY) scripts/check_frontend_refs.py
	cd cli && $(PY) scripts/check_config_fields.py

build-cli:
	cd cli && $(PY) -m PyInstaller fengcode.spec --noconfirm --clean

build-desktop:
	cd desktop && $(NPM) run dist

dist:
	cd cli && $(PY) scripts/deliver.py

dist-apply:
	cd cli && $(PY) scripts/deliver.py --apply

clean:
	cd cli && $(PY) -c "import shutil,pathlib;[shutil.rmtree(p,ignore_errors=True) for p in ['build','dist','.pytest_cache']]"
	cd desktop && $(PY) -c "import shutil;shutil.rmtree('dist',ignore_errors=True)"
