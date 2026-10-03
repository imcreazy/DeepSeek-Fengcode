"""Windows 与系统自动化工具：截图、窗口、输入模拟、剪贴板、通知、系统信息。

跨平台策略：
- Windows：用 ctypes 调 User32/GDI32（不依赖 pywin32，但有则优先用）
- Linux：xdotool / scrot（存在才用）
- macOS：screencapture / osascript

所有输入模拟类操作默认标记为危险，需要用户批准。
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ...utils import human_size, is_windows
from ..base import Tool, ToolContext, ToolResult


def _ss_dir(ctx: ToolContext) -> Path:
    cfg = getattr(ctx.config, "automation", None)
    custom = getattr(cfg, "screenshot_dir", "") if cfg else ""
    d = Path(custom) if custom else (ctx.workspace / "screenshots")
    d.mkdir(parents=True, exist_ok=True)
    return d


class ScreenshotTool(Tool):
    name = "screenshot"
    group = "自动化"
    read_only = True
    description = (
        "截取屏幕（或指定窗口）并保存为图片，同时把图片附到对话中供视觉模型分析。"
        "用于看界面状态、验证自动化结果、排查视觉问题。"
    )
    parameters = {
        "path": {"type": "string", "description": "保存路径，默认工作区 screenshots/ 下按时间命名"},
        "region": {"type": "string", "description": "截取区域 x,y,w,h"},
        "monitor": {"type": "integer", "description": "显示器序号（0 为主屏）"},
        "scale": {"type": "number", "description": "缩放比例 0.1-1.0，默认 1.0"},
    }
    required = []

    async def run(self, ctx: ToolContext, path: str = "", region: str = "", monitor: int = 0,
                  scale: float = 1.0, **_: Any) -> ToolResult:
        from ...security.paths import resolve

        target = (
            resolve(path, workspace=ctx.workspace)
            if path
            else _ss_dir(ctx) / f"screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png"
        )
        ctx.guard.check_write(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        box = _parse_region(region)

        ok, err = await asyncio.to_thread(_grab, target, box, int(monitor or 0), float(scale or 1.0))
        if not ok:
            return ToolResult.fail(err)
        size = target.stat().st_size
        info = f"已截图：{target}（{human_size(size)}）"
        if box:
            info += f"\n区域：{box}"
        return ToolResult(
            content=info + "\n（图片已附加到对话，可直接分析）",
            display=f"截图 {target.name}",
            files=[str(target)],
            data={"_attach": [{"kind": "image", "path": str(target), "mime": "image/png", "name": target.name}]},
        )


def _parse_region(region: str) -> tuple[int, int, int, int] | None:
    if not region:
        return None
    try:
        parts = [int(float(x.strip())) for x in str(region).replace("，", ",").split(",")]
        if len(parts) == 4:
            return (parts[0], parts[1], parts[2], parts[3])
    except ValueError:
        pass
    return None


def _grab(target: Path, box: tuple[int, int, int, int] | None, monitor: int, scale: float) -> tuple[bool, str]:
    if is_windows():
        return _grab_windows(target, box, monitor, scale)
    if sys.platform == "darwin":
        cmd = ["screencapture", "-x"]
        if box:
            cmd += ["-R", f"{box[0]},{box[1]},{box[2]},{box[3]}"]
        cmd.append(str(target))
        try:
            subprocess.run(cmd, check=True, timeout=30)
            return True, ""
        except Exception as e:
            return False, f"截图失败：{e}"
    # Linux
    for tool, args in (
        ("scrot", ["-o", str(target)]),
        ("gnome-screenshot", ["-f", str(target)]),
        ("import", ["-window", "root", str(target)]),
    ):
        exe = shutil.which(tool)
        if exe:
            try:
                subprocess.run([exe, *args], check=True, timeout=30)
                return True, ""
            except Exception as e:
                return False, f"截图失败（{tool}）：{e}"
    return False, "系统没有可用的截图工具（Linux 可安装 scrot 或 imagemagick）"


def _grab_windows(target: Path, box, monitor: int, scale: float) -> tuple[bool, str]:
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        shcore = getattr(ctypes.windll, "shcore", None)
        SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
        if monitor == -1:
            x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
            y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
            w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
            h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        elif box:
            x, y, w, h = box
        else:
            x = user32.GetSystemMetrics(76) if monitor else 0
            y = 0
            w = user32.GetSystemMetrics(0)
            h = user32.GetSystemMetrics(1)
        hdc = user32.GetDC(0)
        memdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(memdc, bmp)
        # SRCCOPY | CAPTUREBLT
        gdi32.BitBlt(memdc, 0, 0, w, h, hdc, x, y, 0x00CC0020 | 0x40000000)
        gdi32.SelectObject(memdc, bmp)
        user32.ReleaseDC(0, hdc)

        # 用 GDI+ 保存 PNG
        import base64
        import struct

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32),
            ]

        bih = BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bih.biWidth = w
        bih.biHeight = -h
        bih.biPlanes = 1
        bih.biBitCount = 32
        bih.biCompression = 0
        bufsize = w * h * 4
        buf = ctypes.create_string_buffer(bufsize)
        gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bih), 0)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(memdc)

        # BGRA → RGBA
        raw = bytearray(buf.raw)
        raw[2::4], raw[0::4] = raw[0::4], raw[2::4]
        raw[3::4] = b"\xff" * (w * h)
        try:
            from PIL import Image  # type: ignore

            img = Image.frombytes("RGBA", (w, h), bytes(raw))
            if scale and 0 < scale < 1:
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
            img.convert("RGB").save(str(target), "PNG")
        except ImportError:
            # 无 Pillow：退化为 BMP（Pillow 缺失时极少见）
            target = target.with_suffix(".bmp")
            with open(target, "wb") as f:
                row = w * 4
                filesize = 14 + 40 + bufsize
                f.write(b"BM" + struct.pack("<IHHI", filesize, 0, 0, 54))
                f.write(struct.pack("<IiiHHIIiiII", 40, w, h, 1, 32, 0, bufsize, 0, 0, 0, 0))
                f.write(bytes(raw))
        return True, ""
    except Exception as e:
        return False, f"Windows 截图失败：{type(e).__name__}: {e}"


class InputTool(Tool):
    name = "input_control"
    group = "自动化"
    dangerous = True
    description = (
        "模拟鼠标与键盘操作（Windows）或调用 xdotool（Linux）。"
        "action 可为：move（移动）、click（点击）、double_click、right_click、drag（拖拽）、"
        "type（输入文本）、key（按快捷键，如 ctrl+s）、scroll（滚轮）、pos（读取当前鼠标位置）。"
        "这是高危操作，请谨慎使用并先截图确认目标位置。"
    )
    parameters = {
        "action": {
            "type": "string",
            "enum": ["move", "click", "double_click", "right_click", "drag", "type", "key", "scroll", "pos"],
            "description": "操作类型",
        },
        "x": {"type": "integer", "description": "X 坐标"},
        "y": {"type": "integer", "description": "Y 坐标"},
        "x2": {"type": "integer", "description": "drag 终点 X"},
        "y2": {"type": "integer", "description": "drag 终点 Y"},
        "text": {"type": "string", "description": "type 时要输入的文本"},
        "keys": {"type": "string", "description": "key 时的按键组合，如 ctrl+shift+s"},
        "amount": {"type": "integer", "description": "scroll 的滚动量（正上负下）"},
        "duration": {"type": "number", "description": "drag 持续时间秒"},
    }
    required = ["action"]

    async def run(self, ctx: ToolContext, action: str = "", x: int = 0, y: int = 0, x2: int = 0,
                  y2: int = 0, text: str = "", keys: str = "", amount: int = 3,
                  duration: float = 0.4, **_: Any) -> ToolResult:
        cfg = getattr(ctx.config, "automation", None)
        if cfg is not None and not getattr(cfg, "allow_input_simulation", True):
            return ToolResult.fail("输入模拟已在设置中关闭（automation.allow_input_simulation=false）")
        act = (action or "").lower()
        if is_windows():
            ok, msg = await asyncio.to_thread(_win_input, act, x, y, x2, y2, text, keys, amount, duration)
        else:
            ok, msg = await asyncio.to_thread(_posix_input, act, x, y, x2, y2, text, keys, amount)
        if not ok:
            return ToolResult.fail(msg)
        return ToolResult.text(msg, display=f"输入模拟：{act}")


_KEYS = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "super": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


def _win_input(act, x, y, x2, y2, text, keys, amount, duration) -> tuple[bool, str]:
    try:
        u = ctypes.windll.user32
        MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
        MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
        MOUSEEVENTF_WHEEL = 0x0800
        KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004

        if act == "pos":
            pt = ctypes.wintypes.POINT() if hasattr(ctypes, "wintypes") else None
            import ctypes.wintypes as wt

            p = wt.POINT()
            u.GetCursorPos(ctypes.byref(p))
            return True, f"当前鼠标位置：({p.x}, {p.y})"

        if act in ("move", "click", "double_click", "right_click"):
            u.SetCursorPos(int(x), int(y))
            time.sleep(0.08)
            if act == "move":
                return True, f"鼠标已移动到 ({x}, {y})"
            if act == "click":
                u.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                u.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                return True, f"已在 ({x}, {y}) 单击"
            if act == "double_click":
                for _ in range(2):
                    u.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                    u.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                    time.sleep(0.06)
                return True, f"已在 ({x}, {y}) 双击"
            u.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            u.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
            return True, f"已在 ({x}, {y}) 右键点击"

        if act == "drag":
            u.SetCursorPos(int(x), int(y))
            time.sleep(0.1)
            u.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            steps = max(8, int(duration * 60))
            for i in range(1, steps + 1):
                nx = int(x + (x2 - x) * i / steps)
                ny = int(y + (y2 - y) * i / steps)
                u.SetCursorPos(nx, ny)
                time.sleep(duration / steps)
            u.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            return True, f"已从 ({x}, {y}) 拖拽到 ({x2}, {y2})"

        if act == "scroll":
            u.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, int(amount * 120), 0)
            return True, f"滚轮滚动 {amount} 格"

        if act == "type":
            for ch in text:
                code = ord(ch)
                u.keybd_event(0, code, KEYEVENTF_UNICODE, 0)
                u.keybd_event(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0)
                time.sleep(0.012)
            return True, f"已输入 {len(text)} 个字符"

        if act == "key":
            vks: list[int] = []
            for part in str(keys).lower().replace(" ", "").split("+"):
                if not part:
                    continue
                vk = _KEYS.get(part)
                if vk is None and len(part) == 1:
                    vk = u.VkKeyScanA(ord(part)) & 0xFF
                if vk:
                    vks.append(vk)
            if not vks:
                return False, f"无法识别的按键：{keys}"
            for vk in vks:
                u.keybd_event(vk, 0, 0, 0)
            for vk in reversed(vks):
                u.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            return True, f"已按下 {keys}"
        return False, f"不支持的操作：{act}"
    except Exception as e:
        return False, f"输入模拟失败：{type(e).__name__}: {e}"


def _posix_input(act, x, y, x2, y2, text, keys, amount) -> tuple[bool, str]:
    xdo = shutil.which("xdotool")
    if not xdo:
        return False, "Linux 下需要 xdotool（sudo apt install xdotool）"
    cmds = {
        "move": [xdo, "mousemove", str(x), str(y)],
        "click": [xdo, "mousemove", str(x), str(y), "click", "1"],
        "double_click": [xdo, "mousemove", str(x), str(y), "click", "--repeat", "2", "1"],
        "right_click": [xdo, "mousemove", str(x), str(y), "click", "3"],
        "drag": [xdo, "mousemove", str(x), str(y), "mousedown", "1", "mousemove", str(x2), str(y2), "mouseup", "1"],
        "type": [xdo, "type", "--delay", "12", text],
        "key": [xdo, "key", keys.replace("+", "+")],
        "scroll": [xdo, "click", "--repeat", str(abs(amount)), "4" if amount > 0 else "5"],
    }
    cmd = cmds.get(act)
    if not cmd:
        return False, f"不支持的操作：{act}"
    try:
        subprocess.run(cmd, check=True, timeout=30)
        return True, f"已执行 {act}"
    except Exception as e:
        return False, f"执行失败：{e}"


class WindowTool(Tool):
    name = "window"
    group = "自动化"
    dangerous = True
    description = (
        "窗口管理：list 列出所有可见窗口、active 查看当前活动窗口、"
        "focus 激活指定窗口、close 关闭窗口、title 读取窗口标题。"
    )
    parameters = {
        "action": {"type": "string", "enum": ["list", "active", "focus", "close", "title"], "description": "操作"},
        "title": {"type": "string", "description": "窗口标题关键字"},
        "pid": {"type": "integer", "description": "按进程号定位窗口"},
    }
    required = ["action"]

    async def run(self, ctx: ToolContext, action: str = "", title: str = "", pid: int = 0,
                  **_: Any) -> ToolResult:
        act = (action or "").lower()
        if not is_windows():
            return ToolResult.text(
                "（非 Windows 系统）窗口管理受限。可尝试：xdotool search --name 关键字 getwindowname"
            )
        return await asyncio.to_thread(_win_window, act, title, pid)


def _win_window(act: str, title: str, pid: int) -> ToolResult:
    try:
        import ctypes.wintypes as wt

        u = ctypes.windll.user32
        EnumWindows = u.EnumWindows
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        wins: list[dict[str, Any]] = []

        def cb(hwnd, _lparam):
            if not u.IsWindowVisible(hwnd):
                return True
            n = u.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            rect = wt.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(rect))
            wpid = wt.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            wins.append(
                {
                    "hwnd": int(hwnd),
                    "title": buf.value,
                    "pid": int(wpid.value),
                    "rect": (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top),
                }
            )
            return True

        EnumWindows(WNDENUMPROC(cb), 0)

        if act == "list":
            if not wins:
                return ToolResult.text("没有找到可见窗口。")
            lines = [f"共 {len(wins)} 个可见窗口："]
            for w in wins[:50]:
                lines.append(f"  [{w['pid']:>6}] {w['title'][:80]}  {w['rect'][2]}×{w['rect'][3]}")
            return ToolResult.text("\n".join(lines), data={"windows": wins[:100]})

        if act == "active":
            hwnd = u.GetForegroundWindow()
            n = u.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            return ToolResult.text(f"当前活动窗口：{buf.value}", data={"title": buf.value})

        if act == "title":
            hits = [w for w in wins if title.lower() in w["title"].lower()]
            if not hits:
                return ToolResult.text(f"没有标题含 “{title}” 的窗口。")
            return ToolResult.text(
                "\n".join(f"[{w['pid']}] {w['title']}" for w in hits), data={"windows": hits}
            )

        if act in ("focus", "close"):
            hits = [w for w in wins if (title.lower() in w["title"].lower() if title else True)]
            if pid:
                hits = [w for w in hits if w["pid"] == pid]
            if not hits:
                return ToolResult.fail(f"没有找到匹配的窗口（title={title!r}, pid={pid}）")
            w = hits[0]
            if act == "focus":
                u.SetForegroundWindow(w["hwnd"])
                u.ShowWindow(w["hwnd"], 9)  # SW_RESTORE
                return ToolResult.text(f"已激活窗口：{w['title']}")
            # 优雅关闭：发 WM_CLOSE
            u.PostMessageW(w["hwnd"], 0x0010, 0, 0)
            return ToolResult.text(f"已请求关闭窗口：{w['title']}")
        return ToolResult.fail(f"不支持的操作：{act}")
    except Exception as e:
        return ToolResult.fail(f"窗口操作失败：{type(e).__name__}: {e}")


class ClipboardTool(Tool):
    name = "clipboard"
    group = "自动化"
    dangerous = True
    description = "读写系统剪贴板。action=get 读取文本，action=set 写入文本。"
    parameters = {
        "action": {"type": "string", "enum": ["get", "set"], "description": "操作"},
        "text": {"type": "string", "description": "set 时的文本内容"},
    }
    required = ["action"]

    async def run(self, ctx: ToolContext, action: str = "", text: str = "", **_: Any) -> ToolResult:
        act = (action or "").lower()
        if act == "get":
            content = await asyncio.to_thread(_clip_get)
            if content is None:
                return ToolResult.fail("读取剪贴板失败（可能需要安装 pyperclip）")
            return ToolResult.text(f"剪贴板内容（{len(content)} 字符）：\n{content[:8000]}", data={"text": content})
        if act == "set":
            ok = await asyncio.to_thread(_clip_set, text)
            return ToolResult(
                ok=ok,
                content=f"已写入剪贴板（{len(text)} 字符）" if ok else "写入剪贴板失败",
                error=None if ok else "写入失败",
            )
        return ToolResult.fail(f"不支持的操作：{action}")


def _clip_get() -> str | None:
    try:
        import pyperclip  # type: ignore

        return pyperclip.paste()
    except Exception:
        pass
    if is_windows():
        try:
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            CF_UNICODETEXT = 13
            if not u.OpenClipboard(None):
                return None
            try:
                h = u.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return ""
                p = k.GlobalLock(h)
                if not p:
                    return ""
                try:
                    return ctypes.wstring_at(p)
                finally:
                    k.GlobalUnlock(h)
            finally:
                u.CloseClipboard()
        except Exception:
            return None
    for cmd in (["pbpaste"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "-b"]):
        exe = shutil.which(cmd[0])
        if exe:
            try:
                r = subprocess.run([exe, *cmd[1:]], capture_output=True, timeout=10)
                return r.stdout.decode("utf-8", "replace")
            except Exception:
                continue
    return None


def _clip_set(text: str) -> bool:
    try:
        import pyperclip  # type: ignore

        pyperclip.copy(text)
        return True
    except Exception:
        pass
    if is_windows():
        try:
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            CF_UNICODETEXT = 13
            GMEM_MOVEABLE = 0x0002
            data = text.encode("utf-16-le") + b"\x00\x00"
            if not u.OpenClipboard(None):
                return False
            try:
                u.EmptyClipboard()
                h = k.GlobalAlloc(GMEM_MOVEABLE, len(data))
                p = k.GlobalLock(h)
                ctypes.memmove(p, data, len(data))
                k.GlobalUnlock(h)
                u.SetClipboardData(CF_UNICODETEXT, h)
                return True
            finally:
                u.CloseClipboard()
        except Exception:
            return False
    for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"]):
        exe = shutil.which(cmd[0])
        if exe:
            try:
                subprocess.run([exe, *cmd[1:]], input=text.encode("utf-8"), timeout=10, check=True)
                return True
            except Exception:
                continue
    return False


class NotifyTool(Tool):
    name = "notify"
    group = "自动化"
    description = "发送系统通知（任务完成、需要用户确认时用）。Windows 用托盘气泡，Linux 用 notify-send。"
    parameters = {
        "title": {"type": "string", "description": "通知标题"},
        "message": {"type": "string", "description": "通知正文"},
    }
    required = ["message"]

    async def run(self, ctx: ToolContext, title: str = "", message: str = "", **_: Any) -> ToolResult:
        ctx.emit("notify", {"title": title or "Fengcode", "message": message})
        ok = await asyncio.to_thread(_notify, title or "Fengcode", message)
        return ToolResult(
            ok=True,
            content=f"已发送通知：{title or 'Fengcode'} - {message[:100]}",
            display="发送通知",
            data={"delivered": ok},
        )


def _notify(title: str, message: str) -> bool:
    if is_windows():
        try:
            ps = (
                "[reflection.assembly]::loadwithpartialname('System.Windows.Forms')|Out-Null;"
                "[reflection.assembly]::loadwithpartialname('System.Drawing')|Out-Null;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;"
                "$n.Visible=$true;"
                f"$n.ShowBalloonTip(6000,'{_ps_escape(title)}','{_ps_escape(message[:200])}',0);"
                "Start-Sleep -Seconds 6;$n.Dispose()"
            )
            exe = shutil.which("powershell") or shutil.which("pwsh")
            if not exe:
                return False
            subprocess.Popen(
                [exe, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False
    exe = shutil.which("notify-send")
    if exe:
        try:
            subprocess.run([exe, title, message[:400]], timeout=10, check=True)
            return True
        except Exception:
            return False
    exe = shutil.which("osascript")
    if exe and sys.platform == "darwin":
        try:
            subprocess.run(
                [exe, "-e", f'display notification "{message[:200]}" with title "{title}"'],
                timeout=10, check=True,
            )
            return True
        except Exception:
            return False
    return False


def _ps_escape(s: str) -> str:
    return s.replace("'", "''")


class SystemInfoTool(Tool):
    name = "system_info"
    group = "自动化"
    read_only = True
    description = "查看系统信息：操作系统、CPU、内存、磁盘、网络、Python 环境、已安装的关键工具。"
    parameters = {
        "detail": {"type": "string", "enum": ["basic", "full"], "description": "详细程度，默认 basic"},
    }
    required = []

    async def run(self, ctx: ToolContext, detail: str = "basic", **_: Any) -> ToolResult:
        lines = await asyncio.to_thread(_sysinfo, detail == "full")
        return ToolResult.text("\n".join(lines), display="系统信息")


def _sysinfo(full: bool) -> list[str]:
    import platform

    out = [
        f"操作系统：{platform.system()} {platform.release()}（{platform.version()[:60]}）",
        f"架构：{platform.machine()}",
        f"主机名：{platform.node()}",
        f"Python：{sys.version.split()[0]}（{sys.executable}）",
        f"工作目录：{os.getcwd()}",
    ]
    try:
        import psutil  # type: ignore

        out.append(f"CPU：{psutil.cpu_count(logical=False)} 物理核 / {psutil.cpu_count()} 逻辑核，"
                   f"使用率 {psutil.cpu_percent(interval=0.5):.1f}%")
        vm = psutil.virtual_memory()
        out.append(f"内存：{vm.total / 1073741824:.1f} GB，已用 {vm.percent:.1f}%")
        if full:
            for part in psutil.disk_partitions(all=False)[:6]:
                try:
                    u = psutil.disk_usage(part.mountpoint)
                    out.append(
                        f"磁盘 {part.mountpoint}：{u.total / 1073741824:.0f} GB，"
                        f"可用 {u.free / 1073741824:.0f} GB（{u.percent}% 已用）"
                    )
                except Exception:
                    continue
        net = psutil.net_if_addrs()
        if full and net:
            out.append(f"网络接口：{', '.join(list(net.keys())[:6])}")
    except ImportError:
        out.append("（未安装 psutil，内存/磁盘详情不可用：python -m pip install psutil）")
    out.append("")
    out.append("关键工具：")
    for name in ("python", "git", "node", "npm", "cargo", "go", "docker", "pwsh", "code", "rg", "ffmpeg"):
        found = shutil.which(name)
        out.append(f"  {'✓' if found else '✗'} {name}" + (f"  {found}" if found else ""))
    return out


TOOLS = [ScreenshotTool, InputTool, WindowTool, ClipboardTool, NotifyTool, SystemInfoTool]
