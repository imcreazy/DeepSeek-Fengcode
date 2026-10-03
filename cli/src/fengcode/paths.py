"""路径解析：源码目录、数据目录、各类子目录。

设计原则
--------
1. 所有运行期数据集中在一处，便于备份与清理。
2. 优先使用环境变量 ``FENGCODE_HOME``；未设置时：
   - 若运行的是源码目录（上层有 ``pyproject.toml``），用 ``<仓库根>/.fengcode``，
     这样用户能直接在 ``D:\\Fengcode`` 里看到自己的数据；
   - 否则（已安装为第三方包）退回 ``~/.fengcode``。
3. 所有目录按需创建，且是幂等的。
4. Windows 下路径可能超过 260 字符，提供 ``long_path()`` 加 ``\\\\?\\`` 前缀。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ENV_HOME = "FENGCODE_HOME"


def _is_source_tree(root: Path) -> bool:
    return (root / "pyproject.toml").is_file() and (root / "src" / "fengcode").is_dir()


def package_root() -> Path:
    """返回 ``src/fengcode`` 所在目录（即包目录本身）。"""
    return Path(__file__).resolve().parent


def repo_root() -> Path:
    """返回源码仓库根目录；已安装为包时返回包目录。"""
    pkg = package_root()
    candidate = pkg.parent.parent
    if _is_source_tree(candidate):
        return candidate
    return pkg


def home() -> Path:
    """返回 Fengcode 数据根目录。"""
    env = os.environ.get(_ENV_HOME)
    if env:
        return Path(env).expanduser().resolve()
    root = repo_root()
    if _is_source_tree(root):
        return root / ".fengcode"
    return Path.home() / ".fengcode"


def _sub(name: str) -> Path:
    p = home() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---- 数据子目录 ---------------------------------------------------------

def config_dir() -> Path:
    return _sub("config")


def data_dir() -> Path:
    return _sub("data")


def logs_dir() -> Path:
    return _sub("logs")


def audit_dir() -> Path:
    return _sub("audit")


def workspace_dir() -> Path:
    """默认工作区：Agent 读写文件的默认根目录。"""
    return _sub("workspace")


def cache_dir() -> Path:
    return _sub("cache")


def uploads_dir() -> Path:
    return _sub("uploads")


def skills_dir() -> Path:
    """用户自定义技能目录。"""
    return _sub("skills")


def plugins_dir() -> Path:
    """用户安装的插件目录。"""
    return _sub("plugins")


def agents_dir() -> Path:
    """用户自定义子智能体定义目录。"""
    return _sub("agents")


def workflows_dir() -> Path:
    return _sub("workflows")


def sessions_dir() -> Path:
    return _sub("sessions")


# ---- 关键文件 -----------------------------------------------------------

def config_file() -> Path:
    return config_dir() / "config.toml"


def db_file() -> Path:
    return data_dir() / "fengcode.db"


def env_file() -> Path:
    return config_dir() / ".env"


def pid_file() -> Path:
    return data_dir() / "server.pid"


def ensure_all() -> None:
    """一次性创建全部目录。"""
    for fn in (
        config_dir,
        data_dir,
        logs_dir,
        audit_dir,
        workspace_dir,
        cache_dir,
        uploads_dir,
        skills_dir,
        plugins_dir,
        agents_dir,
        workflows_dir,
        sessions_dir,
    ):
        fn()


def long_path(path: os.PathLike | str, *, threshold: int = 240) -> str:
    """Windows 上为超长路径加 ``\\\\?\\`` 前缀，规避 260 字符限制。"""
    s = str(path)
    if sys.platform != "win32":
        return s
    if s.startswith("\\\\?\\"):
        return s
    if len(s) < threshold:
        return s
    p = Path(s)
    if not p.is_absolute():
        return s
    s = str(p.resolve()) if p.exists() else s
    if s.startswith("\\\\"):  # UNC
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def display_path(path: os.PathLike | str) -> str:
    """尽量把绝对路径显示成相对数据目录/当前目录的形式，便于阅读。"""
    p = Path(str(path))
    try:
        if p.is_relative_to(workspace_dir()):
            return "~/" + str(p.relative_to(workspace_dir())).replace("\\", "/")
        if p.is_relative_to(home()):
            return "$FENGCODE/" + str(p.relative_to(home())).replace("\\", "/")
    except (ValueError, OSError):
        pass
    try:
        return str(p.relative_to(Path.cwd()))
    except (ValueError, OSError):
        return str(p)


__all__ = [
    "package_root",
    "repo_root",
    "home",
    "config_dir",
    "data_dir",
    "logs_dir",
    "audit_dir",
    "workspace_dir",
    "cache_dir",
    "uploads_dir",
    "skills_dir",
    "plugins_dir",
    "agents_dir",
    "workflows_dir",
    "sessions_dir",
    "config_file",
    "db_file",
    "env_file",
    "pid_file",
    "ensure_all",
    "long_path",
    "display_path",
]
