"""系统监控插件：资源快照、进程排行、压力检测，并统计工具耗时。

这个插件同时演示了 Fengcode 插件系统的几种能力：
- 注册自定义工具（``register_tool``）
- 注册钩子（``after_tool`` 统计耗时、``register_prompt`` 注入提示）
- 通过 ``add_prompt`` 往系统提示里加内容
"""

from __future__ import annotations

import shutil
import time
from collections import defaultdict

# 工具调用耗时统计（插件自己维护的运行时状态）
_TOOL_STATS: dict[str, dict[str, float]] = defaultdict(
    lambda: {"calls": 0.0, "total": 0.0, "max": 0.0, "fails": 0.0}
)


def _psutil():
    try:
        import psutil  # type: ignore

        return psutil
    except ImportError:
        return None


def setup(api):  # noqa: ANN001
    """插件入口。"""

    # ---------------- 工具 ----------------

    def snapshot(detail: str = "basic"):
        """采集当前系统资源快照。"""
        ps = _psutil()
        if ps is None:
            return {
                "ok": False,
                "hint": "未安装 psutil，无法采集资源信息",
                "install": "python -m pip install psutil",
            }
        out: dict = {}
        cpu = ps.cpu_percent(interval=0.4)
        out["cpu"] = {
            "percent": cpu,
            "logical_cores": ps.cpu_count(),
            "physical_cores": ps.cpu_count(logical=False),
            "load_avg": [round(x, 2) for x in (ps.getloadavg() if hasattr(ps, "getloadavg") else (0, 0, 0))],
        }
        vm = ps.virtual_memory()
        out["memory"] = {
            "total_gb": round(vm.total / 1073741824, 2),
            "available_gb": round(vm.available / 1073741824, 2),
            "used_percent": vm.percent,
        }
        sw = ps.swap_memory()
        out["swap"] = {"total_gb": round(sw.total / 1073741824, 2), "used_percent": sw.percent}
        if detail == "full":
            disks = []
            for part in ps.disk_partitions(all=False):
                try:
                    u = ps.disk_usage(part.mountpoint)
                    disks.append(
                        {
                            "mount": part.mountpoint,
                            "fs": part.fstype,
                            "total_gb": round(u.total / 1073741824, 1),
                            "free_gb": round(u.free / 1073741824, 1),
                            "used_percent": u.percent,
                        }
                    )
                except (OSError, PermissionError):
                    continue
            out["disks"] = disks
            try:
                net = ps.net_io_counters()
                out["network"] = {
                    "sent_mb": round(net.bytes_sent / 1048576, 1),
                    "recv_mb": round(net.bytes_recv / 1048576, 1),
                }
            except Exception:
                pass
            out["boot_time"] = ps.boot_time()
            out["uptime_hours"] = round((time.time() - ps.boot_time()) / 3600, 1)
        out["ok"] = True
        return out

    def top_processes(by: str = "memory", count: int = 10):
        """列出资源占用最高的进程。"""
        ps = _psutil()
        if ps is None:
            return {"ok": False, "hint": "需要 psutil：python -m pip install psutil"}
        by = (by or "memory").lower()
        key = "cpu_percent" if by == "cpu" else "memory_info"
        rows = []
        for p in ps.process_iter(["pid", "name", "cpu_percent", "memory_info", "username"]):
            try:
                info = p.info
                mem = info.get("memory_info")
                rows.append(
                    {
                        "pid": info.get("pid"),
                        "name": info.get("name"),
                        "cpu": round(info.get("cpu_percent") or 0.0, 1),
                        "mem_mb": round((mem.rss if mem else 0) / 1048576, 1),
                        "user": (info.get("username") or "")[:28],
                    }
                )
            except Exception:
                continue
        rows.sort(key=lambda r: -(r["mem_mb"] if by != "cpu" else r["cpu"]))
        return {"ok": True, "by": by, "processes": rows[: max(1, int(count or 10))]}

    def check_pressure():
        """检查是否有资源告急。"""
        ps = _psutil()
        if ps is None:
            return {"ok": False, "hint": "需要 psutil"}
        alerts = []
        vm = ps.virtual_memory()
        if vm.percent >= 90:
            alerts.append(
                {
                    "level": "high",
                    "what": "内存",
                    "detail": f"内存已用 {vm.percent}%，仅剩 {round(vm.available / 1073741824, 1)} GB",
                    "advice": "关闭占用大的进程，或减少并行任务数",
                }
            )
        elif vm.percent >= 80:
            alerts.append(
                {"level": "medium", "what": "内存", "detail": f"内存已用 {vm.percent}%",
                 "advice": "留意后续操作的内存占用"}
            )
        for part in ps.disk_partitions(all=False):
            try:
                u = ps.disk_usage(part.mountpoint)
            except (OSError, PermissionError):
                continue
            if u.percent >= 95:
                alerts.append(
                    {
                        "level": "high",
                        "what": f"磁盘 {part.mountpoint}",
                        "detail": f"已用 {u.percent}%，剩余 {round(u.free / 1073741824, 1)} GB",
                        "advice": "清理临时文件或旧日志",
                    }
                )
            elif u.percent >= 90:
                alerts.append(
                    {"level": "medium", "what": f"磁盘 {part.mountpoint}",
                     "detail": f"已用 {u.percent}%"}
                )
        cpu = ps.cpu_percent(interval=0.3)
        if cpu >= 95:
            alerts.append(
                {"level": "high", "what": "CPU", "detail": f"CPU 使用率 {cpu}%",
                 "advice": "建议用 top_processes 查看是哪个进程"}
            )
        return {
            "ok": True,
            "healthy": not alerts,
            "cpu_percent": cpu,
            "memory_percent": vm.percent,
            "alerts": alerts,
        }

    def tool_stats():
        """查看各工具的调用次数与耗时统计（由本插件钩子收集）。"""
        rows = []
        for name, s in sorted(_TOOL_STATS.items(), key=lambda kv: -kv[1]["total"]):
            calls = int(s["calls"])
            rows.append(
                {
                    "tool": name,
                    "calls": calls,
                    "total_seconds": round(s["total"], 2),
                    "avg_seconds": round(s["total"] / calls, 3) if calls else 0,
                    "max_seconds": round(s["max"], 2),
                    "failures": int(s["fails"]),
                }
            )
        return {"ok": True, "stats": rows}

    # 注册工具（名字会自动加 `system-monitor__` 前缀避免冲突）
    api.register_tool(
        "snapshot",
        snapshot,
        description="采集当前系统资源快照（CPU/内存/磁盘/网络/负载）",
        parameters={"detail": {"type": "string", "description": "basic 或 full"}},
    )
    api.register_tool(
        "top_processes",
        top_processes,
        description="按 CPU 或内存列出占用最高的进程",
        parameters={
            "by": {"type": "string", "description": "cpu 或 memory"},
            "count": {"type": "integer", "description": "返回条数，默认 10"},
        },
    )
    api.register_tool(
        "check_pressure",
        check_pressure,
        description="检查是否有资源告急（内存/磁盘/CPU）",
        parameters={},
    )
    api.register_tool(
        "tool_stats",
        tool_stats,
        description="查看各工具调用次数与耗时统计",
        parameters={},
    )

    # ---------------- 钩子 ----------------

    def on_after_tool(payload):  # noqa: ANN001
        """统计每次工具调用的耗时（用于发现慢工具）。"""
        name = str(payload.get("tool") or "")
        if not name:
            return {}
        s = _TOOL_STATS[name]
        dur = float(payload.get("duration") or 0.0)
        s["calls"] += 1
        s["total"] += dur
        s["max"] = max(s["max"], dur)
        if not payload.get("ok", True):
            s["fails"] += 1
        # 慢工具提醒（超过 20 秒）
        if dur > 20:
            api.log_info(f"工具 {name} 耗时 {dur:.1f}s，偏慢")
        return {}

    def on_register_prompt(_payload):  # noqa: ANN001
        """往系统提示里注入一句可用能力说明。"""
        return {}

    api.on("after_tool", on_after_tool)
    api.on("register_prompt", on_register_prompt)

    # 往系统提示追加说明（让模型知道有这些工具可主动用）
    api.add_prompt(
        "本机装了「系统监控」插件，可用 system-monitor__check_pressure 检查资源是否告急；"
        "怀疑性能问题时用 system-monitor__top_processes 看占用最高的进程。"
    )

    api.log_info("system-monitor 插件加载完成")
