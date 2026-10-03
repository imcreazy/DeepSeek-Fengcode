"""exe 入口：打包后的启动逻辑。

命令端 Fengcode.exe 的行为：
  1. 检测端口是否被占用，冲突则自动换一个可用端口
  2. 拉起内置的 HTTP 服务（含网页 UI）
  3. 在控制台打印访问地址与退出方式

⚠️ 命令端**不自动打开浏览器**：双击后它只在本机起服务并在窗口里显示地址，
   由用户自己决定用哪个浏览器访问。真正的桌面应用是「桌面端」（Electron）。

也支持命令行参数：
    Fengcode.exe serve --port 9000      启动服务
    Fengcode.exe serve --open           启动服务并打开浏览器（显式要求才开）
    Fengcode.exe chat                   进入命令行对话
    Fengcode.exe run "任务"             执行一次性任务
    Fengcode.exe doctor                 环境自检
    Fengcode.exe mcp serve              作为 MCP 服务运行
不带参数等价于 ``serve``（不开浏览器）。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path


def _setup_paths() -> None:
    """打包环境下，把数据目录固定到 exe 同级，便于用户找到自己的数据。"""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if not os.environ.get("FENGCODE_HOME"):
            os.environ["FENGCODE_HOME"] = str(exe_dir / "fengcode-data")
        os.makedirs(os.environ["FENGCODE_HOME"], exist_ok=True)


def _setup_encoding() -> None:
    """Windows 控制台中文编码。"""
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    try:
        if os.name == "nt":
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _free_port(preferred: int, host: str = "127.0.0.1") -> int:
    """返回可用端口：优先用 preferred，被占用则往后找。"""

    def in_use(p: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            return s.connect_ex((host, p)) == 0

    if not in_use(preferred):
        return preferred
    for p in range(preferred + 1, preferred + 40):
        if not in_use(p):
            return p
    return 0  # 交给系统分配


def _open_browser_later(url: str, delay: float = 1.5) -> None:
    """延迟打开浏览器（只在用户显式传 --open 时调用）。"""

    def _go() -> None:
        time.sleep(delay)
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_go, daemon=True).start()


def _banner(url: str, data_dir: str) -> None:
    line = "=" * 58
    print()
    print("  " + line)
    print("    Fengcode  -  中文 AI Agent")
    print("  " + line)
    print()
    print("    访问地址： " + url)
    print("    数据目录： " + data_dir)
    print()
    print("    本窗口是命令端服务，不会自动打开浏览器。")
    print("    要用图形界面：复制上面的地址到浏览器，或改用「桌面端」。")
    print("    按 Ctrl+C 停止服务。")
    print()
    print("  " + line)
    print()


def main() -> int:
    _setup_paths()
    _setup_encoding()

    from fengcode.version import __version__

    args = sys.argv[1:]

    # 不带参数 → 启动服务（不开浏览器，由用户自己决定用什么访问）
    if not args:
        args = ["serve"]

    # serve 时做端口探测，避免冲突导致启动失败
    if args and args[0] == "serve":
        from fengcode.config import get_config

        cfg = get_config()
        host = cfg.server.host or "127.0.0.1"
        want = int(cfg.server.port or 7845)

        # 解析用户显式指定的端口
        if "--port" in args:
            i = args.index("--port")
            if i + 1 < len(args):
                try:
                    want = int(args[i + 1])
                except ValueError:
                    pass
        if "--host" in args:
            i = args.index("--host")
            if i + 1 < len(args):
                host = args[i + 1]

        port = _free_port(want, "127.0.0.1" if host in ("0.0.0.0", "::") else host)
        if port == 0:
            print("  [!] 找不到可用端口，将交给系统分配")
        elif port != want:
            print(f"  [!] 端口 {want} 被占用，已自动改用 {port}")

        # 重写参数里的端口，保证一致
        if "--port" in args:
            i = args.index("--port")
            args[i + 1] = str(port)
        else:
            args += ["--port", str(port)]
        if "--host" not in args:
            args += ["--host", host]

        shown_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        url = f"http://{shown_host}:{port}"
        data_dir = os.environ.get("FENGCODE_HOME", "")
        if "--open" in args:
            _open_browser_later(url)
        _banner(url, data_dir)

    # 交给真正的 CLI
    from fengcode.cli.main import app

    sys.argv = ["fengcode"] + args
    try:
        app()
    except KeyboardInterrupt:
        print("\n  已停止。")
        return 0
    except SystemExit as e:
        return int(e.code or 0)
    except Exception as e:
        print(f"\n  [X] 运行出错：{type(e).__name__}: {e}")
        print()
        print("  排查建议：")
        print("    1. 双击 Fengcode.exe doctor 做环境自检")
        print("    2. 查看数据目录下的 logs/")
        print("    3. 或改用源码方式运行：python -m fengcode.cli.main serve")
        print()
        try:
            input("  按回车退出…")
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
