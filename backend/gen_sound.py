"""
生成"齁齁齁"魔性笑声音效（无需 API Key）
纯 Python 合成：用正弦波 + 噪声模拟笑声波形，输出 mp3
"""
import math
import random
import struct
import wave
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
ASSETS_DIR = BASE_DIR / "desktop" / "assets"
OUTPUT = ASSETS_DIR / "houhouhou.mp3"

SR = 44100  # 采样率


def synth_laugh(duration=1.2):
    """合成一段魔性笑声：3 个'齁'音 + 尾音"""
    samples = []
    n = int(SR * duration)

    for i in range(n):
        t = i / SR
        # 三个"齁"音，每个约 0.3 秒，中间有停顿
        phase = (t % 0.45) / 0.45  # 每个音内部进度
        syllable = int(t / 0.45)  # 第几个音

        if syllable < 3:  # 前三个音
            # 音调先升后降，模拟笑声
            if phase < 0.5:
                freq = 300 + 200 * phase * 2  # 升到 500Hz
            else:
                freq = 500 - 150 * (phase - 0.5) * 2  # 降到 350Hz
            # 振幅包络：快速起音，缓慢衰减
            env = math.sin(math.pi * phase) ** 0.5
            # 加一点噪声让声音更"魔性"
            noise = random.uniform(-0.1, 0.1)
            sample = math.sin(2 * math.pi * freq * t) * env * 0.6 + noise * env
        else:
            # 尾音：快速衰减的颤音
            freq = 250 + 100 * math.sin(2 * math.pi * 8 * t)
            env = max(0, 1 - (t - 1.35) * 3)
            sample = math.sin(2 * math.pi * freq * t) * env * 0.4

        samples.append(sample)

    return samples


def write_wav(samples, path):
    """写 WAV 文件"""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        frames = b"".join(
            struct.pack("<h", int(max(-1, min(1, s)) * 32767)) for s in samples
        )
        w.writeframes(frames)


def write_mp3_fallback(samples, path):
    """
    没有 ffmpeg 时退化方案：把 WAV 内容直接存为 .wav 后缀
    Electron 的 Audio 支持 wav，所以直接生成 wav 即可
    """
    wav_path = path.with_suffix(".wav")
    write_wav(samples, wav_path)
    return wav_path


if __name__ == "__main__":
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    print("[TTS] 合成齁齁齁音效...")
    samples = synth_laugh()
    # 直接生成 wav（Electron Audio 原生支持）
    wav_path = write_mp3_fallback(samples, OUTPUT)
    print(f"[TTS] 音效已生成: {wav_path}")
