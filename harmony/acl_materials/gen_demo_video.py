# -*- coding: utf-8 -*-
"""合成 READ_IMAGEVIDEO 权限功能 demo 录屏（mp4）
流程：首页 -> ChatPage -> 点击图片按钮 -> 相册选择器（安全访问图库）
每帧加字幕横幅说明当前步骤，ffmpeg 合成 4fps 幻灯片视频。
"""
import os
import subprocess
from PIL import Image, ImageDraw, ImageFont

FRAMES_DIR = r"C:\Agent-LuoTianyi\harmony\acl_materials\frames"
OUT_DIR = r"C:\Agent-LuoTianyi\harmony\acl_materials"
OUT_VIDEO = os.path.join(OUT_DIR, "READ_IMAGEVIDEO_功能demo录屏.mp4")
TMP_FRAMES = os.path.join(OUT_DIR, "frames_captioned")
os.makedirs(TMP_FRAMES, exist_ok=True)

# 步骤定义：(源图, 字幕, 显示秒数)
STEPS = [
    ("demo1.jpeg", "① 首页：点击「进入聊天」", 3),
    ("demo2.jpeg", "② 聊天页（ChatPage），输入栏有「图片」按钮", 3),
    ("demo3.jpeg", "③ 点击「图片」按钮，系统弹出相册选择器（白屏过渡=加载中）", 2),
    ("demo4.jpeg", "④ 相册选择器：系统「安全访问图库」模式，应用仅可访问用户选定的图片和视频（READ_IMAGEVIDEO 权限使用场景）", 5),
]

W, H = 1320, 2856
TITLE_H = 180  # 顶部字幕横幅高度


def find_font():
    for p in [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyh.ttf",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ]:
        if os.path.exists(p):
            return p
    return None


FONT_PATH = find_font()
print("字体:", FONT_PATH)


def add_caption(src, text, out):
    img = Image.open(src).convert("RGB")
    canvas = Image.new("RGB", (W, H + TITLE_H), (255, 255, 255))
    canvas.paste(img, (0, TITLE_H))
    draw = ImageDraw.Draw(canvas)
    # 顶部标题区
    draw.rectangle([0, 0, W, TITLE_H], fill=(255, 255, 255))
    draw.line([0, TITLE_H - 2, W, TITLE_H - 2], fill=(220, 220, 220), width=2)
    # 标题文字（自适应换行）
    try:
        font = ImageFont.truetype(FONT_PATH, 44)
    except Exception:
        font = ImageFont.load_default()
    lines = []
    cur = ""
    for ch in text:
        cur += ch
        w = draw.textlength(cur, font=font)
        if w > W - 80:
            lines.append(cur[:-1])
            cur = ch
    lines.append(cur)
    y = 20
    for line in lines[:2]:
        draw.text((40, y), line, fill=(20, 20, 20), font=font)
        y += 62
    canvas.save(out)


print("生成带字幕帧...")
frame_files = []
for src_name, caption, dur in STEPS:
    src = os.path.join(FRAMES_DIR, src_name)
    if not os.path.exists(src):
        print("!! 缺少源帧:", src)
        continue
    out = os.path.join(TMP_FRAMES, os.path.splitext(src_name)[0] + "_cap.png")
    add_caption(src, caption, out)
    frame_files.append((out, dur))
    print("  +", os.path.basename(out))

# 构建逐帧清单（4fps，每帧 dur*4 张重复 + 淡入淡出由 ffmpeg xfade 处理太复杂，用 concat demuxer）
with open(os.path.join(TMP_FRAMES, "list.txt"), "w", encoding="utf-8") as f:
    for out, dur in frame_files:
        for _ in range(int(dur * 4)):
            f.write(f"file '{os.path.basename(out)}'\n")

print("ffmpeg 合成 mp4（image2 序列变体）...")
ffmpeg = r"C:\ProgramData\anaconda3\envs\agent\Library\bin\ffmpeg.exe"

# 构建帧序列目录（每帧 dur*4 份，名称 0001.png...）
import shutil
SEQ_DIR = os.path.join(TMP_FRAMES, "seq")
os.makedirs(SEQ_DIR, exist_ok=True)
for f in os.listdir(SEQ_DIR):
    os.remove(os.path.join(SEQ_DIR, f))
idx = 1
for out, dur in frame_files:
    for _ in range(int(dur * 4)):
        shutil.copy(out, os.path.join(SEQ_DIR, f"{idx:04d}.png"))
        idx += 1
print("帧总数:", idx - 1)

cmd = [
    ffmpeg, "-y",
    "-framerate", "4",
    "-i", os.path.join(SEQ_DIR, "%04d.png"),
    "-vf", "scale=660:1518,format=yuv420p",
    "-c:v", "libopenh264", "-b:v", "800k",
    "-movflags", "+faststart",
    OUT_VIDEO,
]
r = subprocess.run(cmd, capture_output=True, text=True)
if r.returncode != 0:
    print("ffmpeg 失败:", r.stderr[-800:])
else:
    print("完成:", OUT_VIDEO, os.path.getsize(OUT_VIDEO), "bytes")
