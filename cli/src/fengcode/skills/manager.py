"""技能系统：Markdown（SKILL.md 形态）+ Python 类技能双支持。

Markdown 技能
-------------
目录结构（兼容常见 Skills 约定）::

    <skills_dir>/<skill-name>/
        SKILL.md          # 必需：带 YAML frontmatter
        scripts/xxx.py    # 可选：脚本
        reference.md      # 可选：附加资料

``SKILL.md`` 格式::

    ---
    name: 技能名
    description: 一句话说明（用于自动匹配）
    when_to_use: 什么时候用
    version: 1.0.0
    tags: [标签1, 标签2]
    ---
    正文：给模型的操作指引……

Python 技能
-----------
继承 :class:`BaseSkill` 并实现 ``run``，通过插件或内置模块注册。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import paths
from ..utils import ensure_dir, read_text, truncate

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


@dataclass
class SkillInfo:
    """一个技能的元信息。"""

    name: str
    description: str = ""
    when_to_use: str = ""
    version: str = "0.0.0"
    tags: list[str] = field(default_factory=list)
    path: str = ""
    dir: str = ""
    source: str = "builtin"       # builtin / user / plugin
    enabled: bool = True
    body: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    runner: Callable | None = None   # Python 技能

    def to_dict(self, *, with_body: bool = False) -> dict[str, Any]:
        d = {
            "name": self.name,
            "description": self.description,
            "when_to_use": self.when_to_use,
            "version": self.version,
            "tags": list(self.tags),
            "path": self.path,
            "dir": self.dir,
            "source": self.source,
            "enabled": self.enabled,
            "kind": "python" if self.runner is not None else "markdown",
            "size": len(self.body or ""),
        }
        if with_body:
            d["body"] = self.body
        return d


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析 YAML frontmatter（不依赖 pyyaml 也能跑，有则更准）。"""
    m = _FRONTMATTER_RE.match(text or "")
    if not m:
        return {}, text or ""
    raw, body = m.group(1), m.group(2)
    meta: dict[str, Any] = {}
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(raw)
        if isinstance(loaded, dict):
            meta = {str(k): v for k, v in loaded.items()}
            return meta, body
    except Exception:
        pass
    # 简易解析
    for line in raw.split("\n"):
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if not k:
            continue
        if v.startswith("[") and v.endswith("]"):
            meta[k] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        elif v.lower() in ("true", "false"):
            meta[k] = v.lower() == "true"
        elif re.fullmatch(r"-?\d+", v):
            meta[k] = int(v)
        else:
            meta[k] = v.strip("'\"")
    return meta, body


class BaseSkill:
    """Python 技能的基类。"""

    name: str = ""
    description: str = ""
    when_to_use: str = ""
    version: str = "1.0.0"
    tags: list[str] = []
    # 依赖的工具名
    requires_tools: list[str] = []
    parameters: dict[str, Any] = {}
    required: list[str] = []

    async def run(self, ctx: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def to_info(self) -> SkillInfo:
        return SkillInfo(
            name=self.name,
            description=self.description,
            when_to_use=self.when_to_use,
            version=self.version,
            tags=list(self.tags),
            source="builtin",
            body=f"（Python 技能：{self.name}）",
            runner=self.run,
        )


class SkillManager:
    """技能的发现、加载与匹配。"""

    def __init__(self, *, config: Any = None, db: Any = None) -> None:
        self.config = config
        self.db = db
        self._skills: dict[str, SkillInfo] = {}
        self._lock = threading.RLock()
        self._loaded_dirs: set[str] = set()
        self.reload()

    # ---- 发现 ----------------------------------------------------------
    def search_paths(self) -> list[tuple[Path, str]]:
        out: list[tuple[Path, str]] = []
        # 内置技能（包内）
        builtin = Path(__file__).resolve().parent.parent / "skills" / "builtin"
        if builtin.is_dir():
            out.append((builtin, "builtin"))
        # 用户目录
        out.append((paths.skills_dir(), "user"))
        # 配置里指定的路径
        cfg = getattr(self.config, "skills", None)
        for p in (getattr(cfg, "paths", None) or []):
            pp = Path(os.path.expandvars(str(p))).expanduser()
            if pp.is_dir():
                out.append((pp, "user"))
        return out

    def reload(self) -> dict[str, Any]:
        """重新扫描全部技能目录。"""
        with self._lock:
            self._skills.clear()
            self._loaded_dirs.clear()
            found = 0
            for base, source in self.search_paths():
                found += self._scan_dir(base, source)
        return {"count": len(self._skills), "dirs": [str(p) for p, _ in self.search_paths()]}

    def _scan_dir(self, base: Path, source: str) -> int:
        key = str(base.resolve())
        if key in self._loaded_dirs:
            return 0
        self._loaded_dirs.add(key)
        n = 0
        try:
            entries = sorted(base.iterdir())
        except OSError:
            return 0
        for entry in entries:
            if entry.is_dir():
                md = entry / "SKILL.md"
                if not md.is_file():
                    for alt in ("skill.md", "README.md"):
                        if (entry / alt).is_file():
                            md = entry / alt
                            break
                if md.is_file():
                    info = self._load_markdown(md, source)
                    if info:
                        self._skills[info.name] = info
                        n += 1
            elif entry.suffix.lower() == ".md" and entry.name.lower() != "readme.md":
                info = self._load_markdown(entry, source)
                if info:
                    self._skills[info.name] = info
                    n += 1
        return n

    def _load_markdown(self, md: Path, source: str) -> SkillInfo | None:
        try:
            text = read_text(md)
        except OSError:
            return None
        if not text.strip():
            return None
        meta, body = parse_frontmatter(text)
        name = str(meta.get("name") or md.parent.name or md.stem).strip()
        if not name:
            return None
        tags = meta.get("tags") or meta.get("keywords") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in re.split(r"[,，、]", tags) if t.strip()]
        desc = str(meta.get("description") or "").strip()
        if not desc:
            # 从正文第一段提取
            for line in body.split("\n"):
                s = line.strip()
                if s and not s.startswith("#"):
                    desc = truncate(s, 140)
                    break
        return SkillInfo(
            name=name,
            description=desc,
            when_to_use=str(meta.get("when_to_use") or meta.get("when") or "").strip(),
            version=str(meta.get("version") or "0.0.0"),
            tags=[str(t) for t in tags] if isinstance(tags, list) else [],
            path=str(md),
            dir=str(md.parent),
            source=source,
            enabled=True,
            body=body.strip(),
            meta=meta,
        )

    # ---- 注册 ----------------------------------------------------------
    def register(self, skill: BaseSkill | SkillInfo, *, source: str = "plugin") -> SkillInfo:
        info = skill.to_info() if isinstance(skill, BaseSkill) else skill
        info.source = source if isinstance(skill, BaseSkill) else info.source
        with self._lock:
            self._skills[info.name] = info
        return info

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._skills.pop(name, None) is not None

    def load_python_file(self, path: Path, *, source: str = "plugin") -> list[str]:
        """从 .py 文件加载 Python 技能（模块内 BaseSkill 子类即注册）。"""
        try:
            spec = importlib.util.spec_from_file_location(f"fengcode_skill_{path.stem}", str(path))
            if spec is None or spec.loader is None:
                return []
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception:
            return []
        out: list[str] = []
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if isinstance(obj, type) and issubclass(obj, BaseSkill) and obj is not BaseSkill:
                try:
                    inst = obj()
                    if inst.name:
                        self.register(inst, source=source)
                        out.append(inst.name)
                except Exception:
                    continue
        return out

    # ---- 查询 ----------------------------------------------------------
    def _enabled_names(self) -> tuple[list[str], list[str]]:
        cfg = getattr(self.config, "skills", None)
        on = list(getattr(cfg, "enabled", None) or [])
        off = list(getattr(cfg, "disabled", None) or [])
        return on, off

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._skills.values())
        # 合并数据库里的启用状态
        states: dict[str, bool] = {}
        if self.db is not None:
            try:
                from ..storage.tasks import SkillStore

                states = {s["name"]: s["enabled"] for s in SkillStore(self.db).list()}
            except Exception:
                states = {}
        on, off = self._enabled_names()
        out = []
        for s in sorted(items, key=lambda x: (x.source, x.name)):
            enabled = s.enabled
            if s.name in states:
                enabled = states[s.name]
            if on and s.name not in on:
                enabled = False
            if s.name in off:
                enabled = False
            d = s.to_dict()
            d["enabled"] = enabled
            out.append(d)
        return out

    def get(self, name: str) -> SkillInfo | None:
        with self._lock:
            if name in self._skills:
                return self._skills[name]
            low = name.lower()
            for k, v in self._skills.items():
                if k.lower() == low:
                    return v
        return None

    def load(self, name: str) -> dict[str, Any] | None:
        s = self.get(name)
        if s is None:
            return None
        d = s.to_dict(with_body=True)
        d["dir"] = s.dir
        if s.runner is not None:
            d["body"] = (
                f"这是一个 Python 技能，可直接通过 skill 工具的参数调用：\n"
                f"参数：{json.dumps(s.meta.get('parameters') or {}, ensure_ascii=False)}"
            )
        return d

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """按相关性排序技能（关键词 + 中文单字匹配）。"""
        q = (query or "").strip().lower()
        if not q:
            return self.list_all()[:limit]
        toks = {t for t in re.split(r"[\s,，、/]+", q) if t}
        toks |= {c for c in q if "\u4e00" <= c <= "\u9fff"}
        scored: list[tuple[float, SkillInfo]] = []
        with self._lock:
            items = list(self._skills.values())
        for s in items:
            hay = f"{s.name} {s.description} {s.when_to_use} {' '.join(s.tags)}".lower()
            score = 0.0
            if q in s.name.lower():
                score += 5.0
            for t in toks:
                if t in s.name.lower():
                    score += 2.0
                elif t in hay:
                    score += 0.6
            if score > 0:
                scored.append((score, s))
        scored.sort(key=lambda x: -x[0])
        return [s.to_dict() for _, s in scored[:limit]]

    def mark_used(self, name: str) -> None:
        if self.db is None:
            return
        try:
            from ..storage.tasks import SkillStore

            SkillStore(self.db).mark_used(name)
        except Exception:
            pass

    def sync_state(self) -> int:
        """把发现的技能写入数据库（记录路径与来源）。"""
        if self.db is None:
            return 0
        try:
            from ..storage.tasks import SkillStore

            store = SkillStore(self.db)
        except Exception:
            return 0
        n = 0
        with self._lock:
            items = list(self._skills.values())
        for s in items:
            store.sync(s.name, s.path, source=s.source)
            n += 1
        return n

    # ---- 自动匹配（注入提示词用）----------------------------------------
    def catalog_block(self, query: str = "", *, max_items: int = 24, max_chars: int = 2400) -> str:
        """生成技能目录片段，供系统提示使用。"""
        cfg = getattr(self.config, "skills", None)
        if not getattr(cfg, "auto_match", True):
            return ""
        items = self.search(query, limit=max_items) if query else self.list_all()[:max_items]
        if not items:
            return ""
        lines = [
            "<skills>",
            "可用技能（当任务与某个技能匹配时，先用 skill(action='load', name=...) 读取全文再照做）：",
        ]
        total = 0
        for it in items:
            line = f"· {it['name']}：{truncate(it.get('description') or '', 120)}"
            if total + len(line) > max_chars:
                break
            total += len(line)
            lines.append(line)
        lines.append("</skills>")
        return "\n".join(lines) if len(lines) > 3 else ""


_skills: SkillManager | None = None
_skills_lock = threading.Lock()


def get_skills(config: Any = None, db: Any = None) -> SkillManager:
    global _skills
    with _skills_lock:
        if _skills is None:
            _skills = SkillManager(config=config, db=db)
        return _skills


def reset_skills() -> None:
    global _skills
    with _skills_lock:
        _skills = None


__all__ = [
    "SkillManager",
    "SkillInfo",
    "BaseSkill",
    "get_skills",
    "reset_skills",
    "parse_frontmatter",
]
