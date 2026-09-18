# -*- coding: utf-8 -*-
"""生成 masktool.ico：深色圆角底 + 紫渐变盾牌 + 白色锁形，简洁可辨识。"""
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "assets" / "masktool.ico"

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 圆角方形底：深海军蓝渐变（手绘分层近似）
for i in range(S):
    t = i / S
    r = int(25 + (21 - 25) * t)
    g = int(26 + (26 - 26) * t)
    b = int(46 + (51 - 46) * t)
    d.line([(0, i), (S, i)], fill=(r, g, b, 255), width=1)
# 圆角遮罩
mask = Image.new("L", (S, S), 0)
md = ImageDraw.Draw(mask)
md.rounded_rectangle([4, 4, S - 5, S - 5], radius=56, fill=255)
img.putalpha(mask)

# 盾牌（紫渐变近似：两层叠加）
sh_top = (91, 110, 232)
sh_bot = (118, 75, 162)
shield = Image.new("RGBA", (S, S), (0, 0, 0, 0))
sd = ImageDraw.Draw(shield)
pts = [128, 42, 196, 68, 196, 128, 128, 210, 60, 128, 60, 68]
sd.polygon(pts, fill=sh_top)
# 下半段覆盖偏深色
pts2 = [128, 128, 196, 128, 196, 128, 128, 210, 60, 128, 60, 128]
sd.polygon([128, 130, 196, 130, 128, 210, 60, 130], fill=sh_bot)
img.alpha_composite(shield)

# 锁（白色）
d = ImageDraw.Draw(img)
# 锁环
d.rounded_rectangle([106, 92, 150, 138], radius=16, outline=(255, 255, 255, 255), width=9)
# 锁体
d.rounded_rectangle([94, 118, 162, 172], radius=12, fill=(255, 255, 255, 255))
# 锁孔
d.ellipse([120, 134, 136, 150], fill=(37, 42, 66, 255))
d.rectangle([124, 144, 132, 162], fill=(37, 42, 66, 255))

OUT.parent.mkdir(parents=True, exist_ok=True)
img.save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("written:", OUT)
