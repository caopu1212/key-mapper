"""生成程序图标 icon.ico：蓝色圆角底 + 白色键帽 + 双向箭头（表示"映射"）。需要 Pillow，只在打包前运行。"""
import os

from PIL import Image, ImageDraw

S = 1024  # 大尺寸画好再缩小，边缘更平滑
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")


def draw():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 底：蓝色圆角方块（上浅下深）
    for y in range(S):
        t = y / S
        c = (int(52 + (24 - 52) * t), int(132 + (88 - 132) * t), int(244 + (200 - 244) * t), 255)
        d.line([(0, y), (S, y)], fill=c)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([24, 24, S - 24, S - 24], radius=210, fill=255)
    img.putalpha(mask)
    d = ImageDraw.Draw(img)
    # 键帽：深一点的底边 + 白色顶面
    d.rounded_rectangle([190, 230, 834, 850], radius=110, fill=(205, 215, 230, 255))
    d.rounded_rectangle([190, 190, 834, 790], radius=110, fill=(255, 255, 255, 255))
    # 双向箭头
    blue = (30, 100, 220, 255)
    w = 58

    def arrow(y, right):
        x0, x1 = 300, 724
        d.rounded_rectangle([x0 + (0 if right else 90), y - w // 2, x1 - (90 if right else 0), y + w // 2],
                            radius=w // 2, fill=blue)
        tip = x1 if right else x0
        back = tip - 150 if right else tip + 150
        d.polygon([(tip, y), (back, y - 115), (back, y + 115)], fill=blue)

    arrow(395, True)
    arrow(585, False)
    return img


if __name__ == "__main__":
    big = draw()
    big.resize((256, 256), Image.LANCZOS).save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                                           (128, 128), (256, 256)])
    print("saved", OUT)
