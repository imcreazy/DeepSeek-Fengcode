# -*- coding: utf-8 -*-
"""手机远程访问（局域网）。

设计要点
--------
- **真的开一个监听器**：在本机 127.0.0.1 之外另起一个 uvicorn，监听 ``0.0.0.0``
  与随机端口，复用**同一个** ``create_app()`` —— app 里的 ``STATE`` 是模块级单例，
  所以手机看到的就是电脑上同一批会话、同一批审批，不是另起一套。
- **必须带配对令牌才放行**：监听 ``0.0.0.0`` 意味着同一局域网里任何设备都能连。
  开启时自动生成一个随机令牌，扫码链接里带上它；不带头部的请求会被拒。
- 关闭即销毁：``should_exit`` 让 uvicorn 自己退出，端口随之释放。

★ 为什么不把主服务的监听地址直接改成 0.0.0.0：那会要求重启主进程（当前连接全断、
  桌面端会以为后端挂了），而「手机上临时看一下」不该有这种代价。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any

# 运行期状态（进程内单例）
LAN: dict[str, Any] = {
    "enabled": False,
    "token": "",
    "port": 0,
    "host": "",
    "url": "",
    "since": 0.0,
    "clients": 0,
    "server": None,
    "task": None,
    "error": "",
}


def local_ips() -> list[dict[str, str]]:
    """列出本机可用于局域网访问的 IPv4 地址（带网卡名，便于用户分辨）。"""
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(ip: str, name: str) -> None:
        if not ip or ip in seen:
            return
        seen.add(ip)
        out.append({"ip": ip, "name": name})

    # 主用出口地址（连一个外部地址但不真发包，只为让系统选出默认网卡）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            add(s.getsockname()[0], "以太网")
        finally:
            s.close()
    except OSError:
        pass

    # 所有网卡上的地址（Windows 上 getaddrinfo 拿不到全部网卡，靠 gethostbyname_ex）
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        for a in addrs:
            add(a, "本机网卡")
    except OSError:
        pass

    # 过滤掉回环、链路本地与 APIPA
    clean: list[dict[str, str]] = []
    for item in out:
        try:
            ip = ipaddress.ip_address(item["ip"])
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            continue
        clean.append(item)
    return clean


def pick_port(preferred: int = 0) -> int:
    """挑一个可用端口：给了偏好值就用它（占用则报错），否则由系统分配。"""
    if preferred:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", int(preferred)))
            return int(preferred)
        finally:
            s.close()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def new_token() -> str:
    import secrets

    return secrets.token_urlsafe(24)


def qr_svg(text: str) -> str:
    """生成二维码 SVG；没有二维码库时返回空串（前端会退回「复制链接」）。

    ★ 不引硬依赖：qrcode 在打包环境里不一定被收进来，缺了不该让整个功能失败。
    """
    try:
        import qrcode  # type: ignore
        import qrcode.image.svg as svg_mod  # type: ignore

        img = qrcode.make(text, image_factory=svg_mod.SvgPathImage, box_size=8, border=2)
        import io

        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8", "replace")
    except Exception:
        return ""


async def start(app: Any, *, port: int = 0, host: str = "") -> dict[str, Any]:
    """开启局域网访问。``app`` 传主服务的 Starlette 应用（复用同一套状态）。"""
    import time

    if LAN["enabled"]:
        return status()

    token = LAN["token"] or new_token()
    try:
        actual_port = pick_port(port)
    except OSError as e:
        LAN["error"] = f"端口 {port} 已被占用：{e}"
        return status()

    try:
        import uvicorn
    except ImportError:
        LAN["error"] = "缺少 uvicorn，无法开启局域网访问"
        return status()

    ip = host or (local_ips()[0]["ip"] if local_ips() else "127.0.0.1")
    cfg = uvicorn.Config(app, host="0.0.0.0", port=actual_port,
                         log_level="warning", access_log=False)
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())

    LAN.update({
        "enabled": True, "token": token, "port": actual_port, "host": ip,
        "url": f"http://{ip}:{actual_port}/?pair={token}",
        "since": time.time(), "server": server, "task": task, "error": "",
    })
    # 等它真的起来（最多 3 秒），起不来就如实报错，别显示成「已开启」
    for _ in range(30):
        if getattr(server, "started", False):
            break
        if task.done():
            exc = task.exception()
            LAN["error"] = f"监听失败：{exc}" if exc else "监听失败"
            LAN.update({"enabled": False, "server": None, "task": None})
            break
        await asyncio.sleep(0.1)
    return status()


async def stop() -> dict[str, Any]:
    """关闭局域网访问。"""
    server = LAN.get("server")
    if server is not None:
        try:
            server.should_exit = True
        except Exception:
            pass
    task = LAN.get("task")
    if task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except Exception:
            try:
                task.cancel()
            except Exception:
                pass
    LAN.update({"enabled": False, "server": None, "task": None, "port": 0,
                "url": "", "host": "", "clients": 0, "error": ""})
    return status()


def status() -> dict[str, Any]:
    return {
        "enabled": bool(LAN["enabled"]),
        "token": LAN["token"] if LAN["enabled"] else "",
        "port": int(LAN["port"] or 0),
        "host": LAN["host"] or "",
        "url": LAN["url"] if LAN["enabled"] else "",
        "since": float(LAN["since"] or 0),
        "clients": int(LAN["clients"] or 0),
        "error": LAN["error"],
        "ips": local_ips(),
    }


def token_ok(got: str) -> bool:
    """配对令牌校验（供 _auth_ok 调用）。"""
    tok = LAN.get("token") or ""
    return bool(tok) and bool(got) and got == tok
