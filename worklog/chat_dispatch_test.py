"""统一聊天调度测试：chat / reminder / note / task(待目录→补目录)"""
import os, shutil, subprocess, sys, time, json, httpx

BASE = "http://localhost:8000"
ROOT_FILE = r"C:\Twork\dfrog-app\backend\data\agent_root.txt"
ROOT_BAK = ROOT_FILE + ".bak"

def chat(msg, timeout=300):
    t0 = time.time()
    out = ""
    with httpx.Client(timeout=timeout) as c:
        with c.stream("POST", BASE + "/chat", json={"message": msg}) as r:
            for chunk in r.iter_bytes():
                out += chunk.decode("utf-8", "replace")
    return out, time.time() - t0

def main():
    print("== 1) 闲聊 ==")
    text, dt = chat("今天天气怎么样？")
    print(repr(text[:200]), f"({dt:.1f}s)\n")

    print("== 2) 自然语言提醒 ==")
    text, dt = chat("十分钟后提醒我喝水")
    print(repr(text[:200]), f"({dt:.1f}s)\n")

    print("== 3) 自然语言速记 ==")
    text, dt = chat("记住：明天下午两点去面试，准备一份自我介绍")
    print(repr(text[:200]), f"({dt:.1f}s)\n")

    print("== 4) 无目录派活 → 问目录 → 答路径开工 ==")
    if os.path.exists(ROOT_FILE):
        shutil.move(ROOT_FILE, ROOT_BAK)
    try:
        text, dt = chat("统计一下这个文件夹里有哪些文本文件")
        print(repr(text[:200]), f"({dt:.1f}s)\n")
        text, dt = chat("C:\\Twork\\dfrog-app\\worklog")
        print(repr(text[:400]), f"({dt:.1f}s)\n")
    finally:
        if os.path.exists(ROOT_BAK):
            shutil.move(ROOT_BAK, ROOT_FILE)

    print("== 5) 提醒已入库 / 速记已入库 ==")
    print(httpx.get(BASE + "/reminders", timeout=10).text[:300])
    print(httpx.get(BASE + "/notes", timeout=10).text[:300])

if __name__ == "__main__":
    sys.exit(main())
