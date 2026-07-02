#!/usr/bin/env python3
"""生成 4 份合成中文测试样本：截图、扫描PDF、表格、公式文档（需求 11）。

用 PIL 生成，避免中文字体在 cv2 下的渲染问题。
依赖 Pillow（已随 paddleocr 安装）。
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "samples"
OUT.mkdir(parents=True, exist_ok=True)

# 尝试常见中文字体
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for f in FONT_CANDIDATES:
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def draw_text(img: Image.Image, xy, text, font, fill=(20, 20, 20)):
    d = ImageDraw.Draw(img)
    d.text(xy, text, font=font, fill=fill)
    return d


def make_screenshot():
    """1. 中文截图（模拟微信聊天界面）"""
    img = Image.new("RGB", (720, 900), (245, 245, 245))
    f = load_font(26)
    fSmall = load_font(20)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 720, 70], fill=(127, 126, 131))
    draw_text(img, (20, 22), "张律师", fSmall, fill=(255, 255, 255))
    lines = [
        ("李总", "你好，关于上周那份合同有几个问题想确认", (225, 240, 255), (60, 60, 60)),
        ("李总", "第三条的违约金比例是否可以再谈", (225, 240, 255), (60, 60, 60)),
        ("我", "好的，违约金我建议按日万分之五计算", (160, 235, 130), (30, 30, 30)),
        ("我", "交货期可以延到 8 月 31 日", (160, 235, 130), (30, 30, 30)),
        ("李总", "那付款方式呢，能不能分三期", (225, 240, 255), (60, 60, 60)),
        ("我", "可以，签约付 30%，到货付 50%，验收后付 20%", (160, 235, 130), (30, 30, 30)),
        ("李总", "好的，我让法务按这个改一版", (225, 240, 255), (60, 60, 60)),
        ("我", "麻烦尽快，我们这周要定下来", (160, 235, 130), (30, 30, 30)),
        ("李总", "收到，明天给你", (225, 240, 255), (60, 60, 60)),
    ]
    y = 110
    for who, txt, bg, fg in lines:
        w = len(txt) * 26 + 30
        if who == "我":
            d.rounded_rectangle([720 - w - 20, y, 700, y + 48], radius=8, fill=bg)
            draw_text(img, (720 - w - 5, y + 8), txt, f, fill=fg)
        else:
            d.rounded_rectangle([20, y, 20 + w, y + 48], radius=8, fill=bg)
            draw_text(img, (35, y + 8), txt, f, fill=fg)
        y += 70
    img.save(OUT / "sample_chat_screenshot.png")
    print("saved sample_chat_screenshot.png")


def make_scan_pdf():
    """2. 扫描风 PDF（多页中文段落，带轻微倾斜模拟扫描）"""
    import pypdfium2 as pdfium
    pages = []
    paras1 = [
        "中华人民共和国民法典合同编相关条款节选",
        "",
        "第四百六十五条 依法成立的合同，受法律保护。",
        "依法成立的合同，仅对当事人具有法律约束力，但是法律另有规定的除外。",
        "",
        "第四百七十条 合同的内容由当事人约定，一般包括下列条款：",
        "（一）当事人的姓名或者名称和住所；",
        "（二）标的；",
        "（三）数量；",
        "（四）质量；",
        "（五）价款或者报酬；",
        "（六）履行期限、地点和方式；",
        "（七）违约责任；",
        "（八）解决争议的方法。",
    ]
    paras2 = [
        "第五百条 当事人在订立合同过程中有下列情形之一，",
        "造成对方损失的，应当承担赔偿责任：",
        "（一）假借订立合同，恶意进行磋商；",
        "（二）故意隐瞒与订立合同有关的重要事实或者提供虚假情况；",
        "（三）有其他违背诚信原则的行为。",
        "",
        "第五百七十七条 当事人一方不履行合同义务或者履行合同",
        "义务不符合约定的，应当承担继续履行、采取补救措施或者",
        "赔偿损失等违约责任。",
    ]
    for paras in [paras1, paras2]:
        img = Image.new("RGB", (800, 1100), (252, 250, 246))
        f = load_font(30)
        y = 80
        for line in paras:
            draw_text(img, (60, y), line, f, fill=(30, 30, 30))
            y += 52
        img = img.rotate(-1.5, expand=False, fillcolor=(252, 250, 246))
        pages.append(img.convert("RGB"))
    pages[0].save(str(OUT / "sample_scan.pdf"), save_all=True, append_images=pages[1:])
    print("saved sample_scan.pdf")


def make_table():
    """3. 表格文档（中英文混排表格）"""
    img = Image.new("RGB", (900, 600), (255, 255, 255))
    f = load_font(24)
    fHead = load_font(28)
    d = ImageDraw.Draw(img)
    draw_text(img, (40, 30), "采购订单明细表 PO-2026-0712", fHead, fill=(20, 20, 20))
    headers = ["序号", "商品名称", "规格型号", "数量", "单价(元)", "金额(元)"]
    rows = [
        ["1", "工业级路由器", "RJ-4500", "20", "1,280.00", "25,600.00"],
        ["2", "光纤收发器", "GF-1000S", "50", "320.00", "16,000.00"],
        ["3", "网络交换机", "SW-2400", "8", "4,500.00", "36,000.00"],
        ["4", "服务器机柜", "CAB-42U", "3", "6,800.00", "20,400.00"],
        ["5", "不间断电源", "UPS-3000", "5", "2,900.00", "14,500.00"],
    ]
    x0, y0, rh, cw = 40, 90, 50, [70, 220, 150, 80, 130, 150]
    xs = [x0]
    for w in cw:
        xs.append(xs[-1] + w)
    d.rectangle([x0, y0, xs[-1], y0 + rh], fill=(80, 130, 200))
    for i, h in enumerate(headers):
        draw_text(img, (xs[i] + 8, y0 + 12), h, f, fill=(255, 255, 255))
    for ri, row in enumerate(rows):
        yy = y0 + (ri + 1) * rh
        d.rectangle([x0, yy, xs[-1], yy + rh], outline=(180, 180, 180))
        if ri % 2 == 0:
            d.rectangle([x0, yy, xs[-1], yy + rh], fill=(240, 245, 255))
        for i, v in enumerate(row):
            draw_text(img, (xs[i] + 8, yy + 12), v, f, fill=(30, 30, 30))
    for i in range(len(headers)):
        d.line([xs[i], y0, xs[i], y0 + (len(rows) + 1) * rh], fill=(180, 180, 180))
    d.line([x0, y0, xs[-1], y0], fill=(80, 130, 200))
    total_y = y0 + (len(rows) + 1) * rh + 20
    draw_text(img, (x0 + 480, total_y), "合计金额：112,500.00 元", f, fill=(200, 30, 30))
    img.save(OUT / "sample_table.png")
    print("saved sample_table.png")


def make_formula():
    """4. 公式文档（含数学公式的中文说明）"""
    img = Image.new("RGB", (900, 700), (255, 255, 255))
    f = load_font(26)
    fF = load_font(24)
    d = ImageDraw.Draw(img)
    draw_text(img, (40, 30), "机器学习常用损失函数", f, fill=(20, 20, 20))
    lines = [
        "一、均方误差（MSE）用于回归任务：",
        "    MSE = (1/n) * Σ(yi - ŷi)²",
        "其中 yi 为真实值，ŷi 为预测值，n 为样本数。",
        "",
        "二、交叉熵损失用于分类任务：",
        "    L = -Σ ci * log(pi)",
        "其中 ci 为真实分布，pi 为预测概率分布。",
        "",
        "三、正则化项 L2 范数：",
        "    L2 = λ * Σ wi²",
        "λ 为正则化系数，wi 为模型权重。",
        "",
        "四、Softmax 函数将 logits 转为概率：",
        "    softmax(zi) = exp(zi) / Σ exp(zj)",
        "zi 为第 i 类的 logit，分母对所有类别求和。",
    ]
    y = 90
    for line in lines:
        draw_text(img, (40, y), line, fF, fill=(30, 30, 30))
        y += 36
    img.save(OUT / "sample_formula.png")
    print("saved sample_formula.png")


def main():
    make_screenshot()
    make_scan_pdf()
    make_table()
    make_formula()
    print("\n全部样本已生成到:", OUT)


if __name__ == "__main__":
    main()
