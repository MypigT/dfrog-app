"""奶龙桌宠启动器

用法：双击本 exe（放在项目根目录）。
功能：启动后端（实时代码）+ 桌宠（实时代码）。
以后改代码，重新双击即可生效，不用重新打包。
"""

import os
import logging
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("dfrog")

# 项目根目录 = 启动器 exe 所在目录（打包后 sys.executable 指向 exe 本身）
ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent

BACKEND_PY = ROOT / "backend" / "main.py"
ELECTRON = ROOT / "desktop" / "node_modules" / "electron" / "dist" / "electron.exe"


def find_python():
    """优先用项目虚拟环境里的 python（依赖齐全），没有就用系统 python"""
    venv = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv.exists():
        return str(venv)
    return "python"


def backend_running():
    try:
        with urllib.request.urlopen("http://localhost:8000/health", timeout=2):
            return True
    except Exception:
        return False


def kill_pet_windows():
    """关掉旧的桌宠窗口（Electron 多开：改代码重跑启动器时，旧窗口还占着画面，
    用户点到的可能是旧代码的窗口，看起来像"改了没生效"）"""
    try:
        ps = (
            "Get-CimInstance Win32_Process -Filter \"name='electron.exe'\" "
            "| Where-Object { $_.CommandLine -like '*dfrog*' } "
            "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=20,
        )
        logger.info("[启动器] 已关闭旧桌宠窗口")
    except Exception as e:
        logger.info(f"[启动器] 清理旧桌宠窗口失败（忽略）: {e}")


def clean_env():
    env = os.environ.copy()
    env.pop("ELECTRON_RUN_AS_NODE", None)  # 否则 Electron 会以 Node 模式运行
    return env


def kill_backend():
    """杀掉占用 8000 端口的后端进程（改代码后重跑启动器必须重启后端才生效）"""
    try:
        # 按字节解析 netstat 输出，避开中文 Windows 上编码不一致的问题
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, timeout=10,
        ).stdout
        pids = set()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(b":8000") and parts[3] == b"LISTENING":
                pids.add(parts[4].decode())
        for pid in pids:
            logger.info(f"[启动器] 关闭旧后端进程 PID={pid}")
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=10)
    except Exception as e:
        logger.info(f"[启动器] 清理旧后端失败（忽略）: {e}")


def main():
    # 1) 后端：先关掉端口上的旧后端（否则改代码重跑不生效），再启动最新代码
    kill_backend()
    for _ in range(10):  # 等端口释放（最多 5 秒）s
        if not backend_running():
            break
        time.sleep(0.5)

    backend_proc = None
    if BACKEND_PY.exists():
        py = find_python()
        logger.info(f"[启动器] 启动后端: {py} {BACKEND_PY}")
        backend_proc = subprocess.Popen(
            [py, str(BACKEND_PY)],
            cwd=str(BACKEND_PY.parent),
            env=clean_env(),
        )
        for _ in range(20):  # 最多等 10 秒
            time.sleep(0.5)
            if backend_running():
                break
        if not backend_running():
            logger.info("[启动器] 警告：后端 10 秒内没起来，桌宠会提示后端未开")
    else:
        logger.info("[启动器] 错误：找不到 backend/main.py")
        return

    # 2) 桌宠：先关掉旧窗口再跑最新代码（旧窗口占着画面时，用户点到的还是旧代码）
    kill_pet_windows()
    if not ELECTRON.exists():
        logger.info("[启动器] 错误：找不到 Electron，请先在 desktop/ 下 npm install")
        if backend_proc:
            backend_proc.terminate()
        return
    logger.info("[启动器] 启动桌宠...")
    pet = subprocess.Popen(
        [str(ELECTRON), "."],
        cwd=str(ROOT / "desktop"),
        env=clean_env(),
    )
    try:
        pet.wait()  # 桌宠关闭后启动器退出
    except KeyboardInterrupt:
        logger.info("[启动器] 收到停止信号，退出中…")
    logger.info("[启动器] 桌宠已退出")

    # 3) 清理后端（只清理本启动器启动的那个）
    if backend_proc:
        logger.info("[启动器] 关闭后端")
        backend_proc.terminate()


if __name__ == "__main__":
    main()
