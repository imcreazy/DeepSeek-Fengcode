"""SSH 远程主机工具：在远端执行命令、上传下载文件、查看系统状态。

依赖 paramiko（本机已安装）。主机列表来自配置 ``remote.hosts``，
也支持在工具调用里临时指定 host/user/password。
"""

from __future__ import annotations

import asyncio
import io
import os
import posixpath
import stat
import time
from pathlib import Path
from typing import Any

from ...utils import human_size, truncate_middle
from ..base import Tool, ToolContext, ToolResult


def _get_host(ctx: ToolContext, name: str) -> tuple[str, dict[str, Any]] | tuple[None, str]:
    cfg = getattr(ctx.config, "remote", None)
    hosts = getattr(cfg, "hosts", None) or []
    if not hosts:
        return None, "配置里没有远程主机（可在「设置 → 远程主机」添加）"
    if not name:
        h = hosts[0]
    else:
        h = None
        for cand in hosts:
            if name in (cand.name, cand.host):
                h = cand
                break
        if h is None:
            names = ", ".join(f"{x.name}({x.host})" for x in hosts)
            return None, f"没有找到主机 “{name}”。已配置：{names}"
    return h.host, {
        "hostname": h.host,
        "port": h.port,
        "username": h.user,
        "password": _resolve_password(ctx, h),
        "key_filename": h.key_file or None,
        "name": h.name,
        "workspace": h.workspace or "~",
    }


def _resolve_password(ctx: ToolContext, h: Any) -> str | None:
    if getattr(h, "password", None):
        return h.password
    env_key = getattr(h, "password_env", None)
    if env_key:
        val = os.environ.get(env_key)
        if val:
            return val
        if ctx.llm is not None:
            try:
                return ctx.llm.manager.env_get(env_key)
            except Exception:
                pass
    return None


def _connect(info: dict[str, Any], timeout: float = 20.0):
    import paramiko  # type: ignore

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=info["hostname"],
        port=int(info.get("port") or 22),
        username=info.get("username") or "root",
        password=info.get("password"),
        key_filename=info.get("key_filename"),
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        look_for_keys=not info.get("password"),
        allow_agent=not info.get("password"),
    )
    return client


class SSHTool(Tool):
    name = "ssh"
    group = "远程"
    dangerous = True
    description = (
        "在远程主机上执行操作。action 可为："
        "exec（执行命令）、upload（上传文件）、download（下载文件）、"
        "list（列目录）、read（读远端文件）、write（写远端文件）、"
        "hosts（列出已配置主机）、info（远端系统信息）、check（连通性测试）、"
        "tail（看末尾日志）。"
        "host 可用配置里的主机名；未提供则用第一个配置主机。"
    )
    parameters = {
        "action": {
            "type": "string",
            "enum": ["exec", "upload", "download", "list", "read", "write", "hosts", "info", "check", "tail"],
            "description": "操作类型",
        },
        "host": {"type": "string", "description": "主机名或 IP（留空用配置中的第一个）"},
        "command": {"type": "string", "description": "exec 时要执行的命令"},
        "remote_path": {"type": "string", "description": "远端路径"},
        "local_path": {"type": "string", "description": "本地路径"},
        "content": {"type": "string", "description": "write 时的内容"},
        "timeout": {"type": "number", "description": "超时秒数，默认 120"},
        "lines": {"type": "integer", "description": "tail 时看多少行，默认 100"},
    }
    required = ["action"]

    async def run(self, ctx: ToolContext, action: str = "", host: str = "", command: str = "",
                  remote_path: str = "", local_path: str = "", content: str = "",
                  timeout: float = 120, lines: int = 100, **_: Any) -> ToolResult:
        act = (action or "").lower()

        if act == "hosts":
            cfg = getattr(ctx.config, "remote", None)
            hosts = getattr(cfg, "hosts", None) or []
            if not hosts:
                return ToolResult.text("还没有配置远程主机。在「设置 → 远程主机」里添加，或编辑 config.toml 的 [remote]。")
            out = [f"已配置 {len(hosts)} 台主机："]
            for h in hosts:
                has_pw = bool(_resolve_password(ctx, h))
                out.append(
                    f"· {h.name}　{h.user or 'root'}@{h.host}:{h.port}　"
                    f"{'有密钥' if h.key_file else ('有密码' if has_pw else '⚠ 无认证信息')}"
                )
            return ToolResult.text("\n".join(out), data={"hosts": [h.model_dump() for h in hosts]})

        ip, info = _get_host(ctx, host)
        if ip is None:
            return ToolResult.fail(str(info))
        assert isinstance(info, dict)

        if act == "check":
            t0 = time.time()
            try:
                client = await asyncio.to_thread(_connect, info)
                await asyncio.to_thread(client.close)
                return ToolResult.text(
                    f"✓ 连接成功：{info['hostname']}（耗时 {time.time() - t0:.2f}s）"
                )
            except Exception as e:
                return ToolResult.fail(f"连接失败：{type(e).__name__}: {e}")

        if act == "exec":
            if not command:
                return ToolResult.fail("exec 需要 command")
            try:
                rc, out, err = await asyncio.to_thread(_exec, info, command, float(timeout or 120))
            except Exception as e:
                return ToolResult.fail(f"远程执行失败：{type(e).__name__}: {e}")
            text = out
            if err.strip():
                text = (text + "\n【stderr】\n" + err).strip()
            return ToolResult(
                ok=rc == 0,
                content=f"远端 {info['hostname']} 执行：{truncate_middle(command, 100)}\n退出码：{rc}\n\n"
                        + truncate_middle(text or "（无输出）", 20000),
                error=None if rc == 0 else f"退出码 {rc}",
                display=f"远程执行（{info['name']}，退出码 {rc}）",
                data={"returncode": rc, "stdout": out[-8000:], "stderr": err[-4000:]},
            )

        if act == "info":
            cmd = (
                "echo '=== 系统 ==='; uname -a 2>/dev/null || ver; "
                "echo '=== 发行版 ==='; cat /etc/os-release 2>/dev/null | head -3; "
                "echo '=== 负载 ==='; uptime; "
                "echo '=== 内存 ==='; free -h 2>/dev/null | head -2; "
                "echo '=== 磁盘 ==='; df -h 2>/dev/null | head -6; "
                "echo '=== CPU ==='; nproc 2>/dev/null; "
                "echo '=== 进程 TOP5 ==='; ps aux --sort=-%mem 2>/dev/null | head -6; "
                "echo '=== Docker ==='; docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null | head -10"
            )
            try:
                rc, out, err = await asyncio.to_thread(_exec, info, cmd, 60.0)
            except Exception as e:
                return ToolResult.fail(f"获取信息失败：{e}")
            return ToolResult.text(f"远端 {info['hostname']} 概况：\n\n{out}", data={"raw": out})

        if act == "list":
            path = remote_path or info.get("workspace") or "~"
            rc, out, err = await asyncio.to_thread(_exec, info, f"ls -lah --color=never {path} 2>&1 | head -100", 60.0)
            return ToolResult(ok=rc == 0, content=out or err, display=f"列远端目录 {path}")

        if act == "read":
            if not remote_path:
                return ToolResult.fail("read 需要 remote_path")
            rc, out, err = await asyncio.to_thread(
                _exec, info, f"cat -- {remote_path} 2>&1 | head -c 200000", 60.0
            )
            if rc != 0:
                return ToolResult.fail(f"读取失败：{err or out}")
            return ToolResult(
                content=truncate_middle(out, 20000),
                display=f"读远端文件 {posixpath.basename(remote_path)}",
            )

        if act == "write":
            if not remote_path:
                return ToolResult.fail("write 需要 remote_path 与 content")
            try:
                await asyncio.to_thread(_write_remote, info, remote_path, content)
            except Exception as e:
                return ToolResult.fail(f"写入失败：{type(e).__name__}: {e}")
            return ToolResult.text(f"已写入远端文件：{remote_path}（{len(content)} 字符）")

        if act in ("upload", "download"):
            from ...security.paths import resolve

            if not remote_path or not local_path:
                return ToolResult.fail(f"{act} 需要 local_path 与 remote_path")
            local = resolve(local_path, workspace=ctx.workspace)
            try:
                if act == "upload":
                    ctx.guard.check_read(local)
                    size = await asyncio.to_thread(_sftp_put, info, local, remote_path)
                    return ToolResult.text(f"已上传 {local} → {info['hostname']}:{remote_path}（{human_size(size)}）")
                ctx.guard.check_write(local)
                size = await asyncio.to_thread(_sftp_get, info, remote_path, local)
                return ToolResult.text(
                    f"已下载 {info['hostname']}:{remote_path} → {local}（{human_size(size)}）",
                    files=[str(local)],
                )
            except Exception as e:
                return ToolResult.fail(f"{act} 失败：{type(e).__name__}: {e}")

        if act == "tail":
            if not remote_path:
                return ToolResult.fail("tail 需要 remote_path")
            rc, out, err = await asyncio.to_thread(
                _exec, info, f"tail -n {int(lines or 100)} -- {remote_path} 2>&1", 60.0
            )
            return ToolResult(ok=rc == 0, content=truncate_middle(out or err, 20000),
                              display=f"查看远端日志尾部（{lines} 行）")

        return ToolResult.fail(f"不支持的操作：{action}")


def _exec(info: dict[str, Any], command: str, timeout: float) -> tuple[int, str, str]:
    client = _connect(info)
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=False)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err
    finally:
        try:
            client.close()
        except Exception:
            pass


def _write_remote(info: dict[str, Any], remote: str, content: str) -> None:
    client = _connect(info)
    try:
        sftp = client.open_sftp()
        try:
            parent = posixpath.dirname(remote)
            if parent:
                try:
                    sftp.stat(parent)
                except OSError:
                    client.exec_command(f"mkdir -p {parent}")
            with sftp.open(remote, "w") as f:
                f.write(content)
        finally:
            sftp.close()
    finally:
        client.close()


def _sftp_put(info: dict[str, Any], local: Path, remote: str) -> int:
    client = _connect(info)
    try:
        sftp = client.open_sftp()
        try:
            sftp.put(str(local), remote)
            return int(sftp.stat(remote).st_size)
        finally:
            sftp.close()
    finally:
        client.close()


def _sftp_get(info: dict[str, Any], remote: str, local: Path) -> int:
    client = _connect(info)
    try:
        sftp = client.open_sftp()
        try:
            local.parent.mkdir(parents=True, exist_ok=True)
            sftp.get(remote, str(local))
            return int(local.stat().st_size)
        finally:
            sftp.close()
    finally:
        client.close()


TOOLS = [SSHTool]
