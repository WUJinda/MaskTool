# -*- coding: utf-8 -*-
"""生成 mask-tool 应用图标位图资产（v2 扁平设计：文档 + 遮蔽条）。

与 assets/icon/masktool-icon.svg 同构图；小尺寸（<=32px）自动切换简化构图
（纸占比加大、遮蔽条加粗、省略折角细节），保证任务栏 16px 依然可辨。

输出：
  assets/icon/png/masktool-icon-{16,24,32,48,64,128,256}.png   图标各尺寸
  assets/icon/png/masktool-logo-horizontal-{48h,96h}.png        横向 logo（图标+文字）
  assets/masktool.ico                                           多尺寸 ico（替换旧版）

旧版 ico 首次运行时备份为 assets/icon/legacy-masktool.ico。
用法：.venv/Scripts/python.exe scripts/make_icon_v2.py
"""
import io
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT_PNG = ROOT / "assets" / "icon" / "png"
ICO_OUT = ROOT / "assets" / "masktool.ico"
BACKUP = ROOT / "assets" / "icon" / "legacy-masktool.ico"

# 扁平纯色（与 SVG 源一致）
BG = (30, 36, 64, 255)        # #1E2440 深蓝紫底
PAPER = (255, 255, 255, 255)  # 文档
BAR = (91, 110, 232, 255)     # #5B6EE8 遮蔽条（品牌紫）
LINE = (201, 206, 220, 255)   # #C9CEDC 普通文本行
FOLD = (217, 222, 235, 255)   # #D9DEEB 折角翻页

SS = 4  # 超采样倍数（抗锯齿）

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeuib.ttf",   # Segoe UI Bold
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
]


def _rounded_icon(size: int) -> Image.Image:
    """绘制 size×size 应用图标（4x 超采样后 LANCZOS 缩回）。"""
    S = size * SS
    s = S / 256  # 以 256 主构图为基准的缩放系数
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角方底（透明背景）
    rx = int(S * 0.18) if size <= 16 else int(56 * s)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=rx, fill=BG)

    if size >= 48:
        # 完整构图：纸 + 折角 + 两条遮蔽条 + 一条文本行
        d.rounded_rectangle([66 * s, 50 * s, 190 * s, 206 * s], radius=10 * s, fill=PAPER)
        d.polygon([(158 * s, 50 * s), (190 * s, 50 * s), (190 * s, 82 * s)], fill=BG)
        d.polygon([(158 * s, 50 * s), (190 * s, 82 * s), (158 * s, 82 * s)], fill=FOLD)
        d.rounded_rectangle([84 * s, 92 * s, 172 * s, 112 * s], radius=5 * s, fill=BAR)
        d.rounded_rectangle([84 * s, 124 * s, 148 * s, 144 * s], radius=5 * s, fill=BAR)
        d.rounded_rectangle([84 * s, 162 * s, 172 * s, 170 * s], radius=4 * s, fill=LINE)
    elif size > 16:
        # 简化构图（24/32px）：纸加大、条加粗、保留折角
        d.rounded_rectangle([58 * s, 44 * s, 198 * s, 212 * s], radius=9 * s, fill=PAPER)
        d.polygon([(164 * s, 44 * s), (198 * s, 44 * s), (198 * s, 78 * s)], fill=BG)
        d.polygon([(164 * s, 44 * s), (198 * s, 78 * s), (164 * s, 78 * s)], fill=FOLD)
        d.rounded_rectangle([76 * s, 90 * s, 180 * s, 116 * s], radius=6 * s, fill=BAR)
        d.rounded_rectangle([76 * s, 132 * s, 152 * s, 158 * s], radius=6 * s, fill=BAR)
    else:
        # 极简构图（16px）：纸几乎撑满 + 两条粗遮蔽条
        d.rounded_rectangle([52 * s, 40 * s, 204 * s, 216 * s], radius=8 * s, fill=PAPER)
        d.rounded_rectangle([72 * s, 84 * s, 184 * s, 112 * s], radius=6 * s, fill=BAR)
        d.rounded_rectangle([72 * s, 136 * s, 148 * s, 164 * s], radius=6 * s, fill=BAR)

    return img.resize((size, size), Image.LANCZOS)


def _load_font(px: int) -> ImageFont.FreeTypeFont:
    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            return ImageFont.truetype(p, px)
    return ImageFont.load_default()


def _logo_horizontal(height: int) -> Image.Image:
    """横向 logo 位图版（图标 + "mask-tool" 文字，透明背景，深色界面用）。"""
    icon = _rounded_icon(height)
    gap = max(6, int(height * 0.22))
    font = _load_font(int(height * 0.44))

    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    text_w = int(probe.textlength("mask-tool", font=font))
    bbox = probe.textbbox((0, 0), "mask-tool", font=font)
    text_h = bbox[3] - bbox[1]

    W = height + gap + text_w + 4
    img = Image.new("RGBA", (W, height), (0, 0, 0, 0))
    img.paste(icon, (0, 0), icon)
    d = ImageDraw.Draw(img)
    # 垂直居中（按字形实际 bbox 修正）
    ty = (height - text_h) // 2 - bbox[1]
    d.text((height + gap, ty), "mask-tool", font=font, fill=(255, 255, 255, 255))
    return img


def _save_ico(path: Path, sized: "list[tuple[int, Image.Image]]") -> None:
    """手写 ICO 容器：每尺寸内嵌 PNG（Vista+ 均支持），避免统一缩放糊小图。"""
    header = struct.pack("<HHH", 0, 1, len(sized))
    offset = 6 + 16 * len(sized)
    entries = b""
    blobs = b""
    for size, im in sized:
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        data = buf.getvalue()
        entries += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset
        )
        blobs += data
        offset += len(data)
    path.write_bytes(header + entries + blobs)


def main() -> None:
    OUT_PNG.mkdir(parents=True, exist_ok=True)

    # 1) 图标 PNG 各尺寸
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs: dict[int, Image.Image] = {}
    for sz in sizes:
        im = _rounded_icon(sz)
        imgs[sz] = im
        out = OUT_PNG / f"masktool-icon-{sz}.png"
        im.save(out)
        print(f"written: {out}")

    # 2) 多尺寸 ico（替换前备份旧版一次）
    if ICO_OUT.exists() and not BACKUP.exists():
        BACKUP.write_bytes(ICO_OUT.read_bytes())
        print(f"backup : {BACKUP}")
    _save_ico(ICO_OUT, [(sz, imgs[sz]) for sz in sizes])
    print(f"written: {ICO_OUT}")

    # 3) 横向 logo PNG（48h/@1x、96h/@2x）
    for h in (48, 96):
        out = OUT_PNG / f"masktool-logo-horizontal-{h}h.png"
        _logo_horizontal(h).save(out)
        print(f"written: {out}")


if __name__ == "__main__":
    main()
