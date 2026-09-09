# -*- coding: utf-8 -*-
"""货盘表 → 系统标准模板 转换器

映射规则（与运营对齐）：
- 商品编码 = 商家编码的「字母+数字」主干（ZSDZ388W-100 → ZSDZ388）
- SKU 编码 = 完整商家编码（ZSDZ388W-100）
- 规格维度 = 主钻分数 + 钻石颜色
- 售价(分) = ceil(零售标价(分) / 0.7)，划线价不填
- 库存：供定制/可定制 → 预售(不设库存)；售罄 → 0；数字 → 原样
- 图片：uploaded_images.json 按商家编码前缀匹配，排除 .psd
"""
import json
import os
import re
import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Font

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HUOPAI_PATH = r"C:\Users\Administrator\Desktop\共享-线上渠道货盘表（附库存）最新.xlsx"
UPLOADED_JSON = os.path.join(BASE_DIR, "uploaded_images.json")
OUTPUT_PATH = os.path.join(BASE_DIR, "货盘转换预览.xlsx")

HEADERS = ["商品编码", "标题", "微信标题", "小红书标题", "内部类目", "品牌", "描述", "商品属性",
           "重量", "主图", "详情图", "SKU编码", "规格1名称", "规格1值", "规格2名称", "规格2值",
           "规格3名称", "规格3值", "原价（元）", "售价（元）", "库存"]


def s(value):
    return "" if value is None else str(value).strip()


def split_product_code(shangjia):
    """商家编码 → 商品编码主干：去掉 - 后缀，再去掉末尾的单个颜色字母(W/Y/R/S)"""
    base = shangjia.split("-")[0]
    if base and base[-1] in "WYRS":
        base = base[:-1]
    return base


def clean_base(code):
    """去掉商家编码主干末尾多余的 X（源表不可改，转换时清洗）：'LGDND1460X-050' → 'LGDND1460-050'"""
    if not code:
        return code
    parts = code.split("-", 1)
    base = parts[0]
    if base and base[-1] == "X":
        base = base[:-1]
    return "-".join([base] + parts[1:])


def split_series(series):
    """系列字段拆成(主系列, 子系列中文名)：'Shining系列-PASSION·炽爱' → ('Shining系列', '炽爱')"""
    text = s(series)
    if "-" in text:
        main, sub = text.split("-", 1)
        sub_cn = sub.split("·")[-1].strip() if "·" in sub else sub.strip()
        return main.strip(), sub_cn
    return text, ""


# 切工值按平台适配：微信「切工级别」用简写(EX/VG/不分级)，小红书「钻石切工」用全拼
CUT_MAP = {
    "VG/很好": ("VG", "Very good/优良"),
    "EX/完美": ("EX", "Excellent/极优"),
    "未分级": ("不分级", "Poor/未分级"),
}


def map_cut(val):
    """切工 → (微信值, 小红书值)；未收录的原样返回。"""
    v = s(val)
    return CUT_MAP.get(v, (v, v))


# 天然钻形状 → 小红书「形状」候选值（平台候选为通用饰品形状，钻石形状需兜底映射）
XHS_SHAPE_MAP = {
    "八边形": "其它",
    "圆钻形": "圆形",
}


def normalize_fuzuan(val):
    """副钻分数归一化到小红书候选：全角括号→半角，100分以上→100分及以上"""
    v = s(val).replace("（", "(").replace("）", ")")
    return "100分及以上" if v == "100分以上" else v


def parse_main_ct(guige):
    """从规格列解析主钻/主石分数：'主钻约150分' → '150分'"""
    m = re.search(r'主[钻石]约[:：]?\s*([\d.]+)\s*分', s(guige))
    return (m.group(1) + "分") if m else ""


METALS = [
    "S925银", "S990银", "S999银", "S800银",
    "银800镀金", "银925镀金", "足银镀金", "铜合金镀金", "钛合金镀金", "锌合金镀金", "钛钢镀金", "不锈钢镀金", "铜镀金",
    "足金", "22K金", "18K金", "14K金", "9K金", "银800", "银925", "足银", "铂900", "铂950", "足铂", "钯500", "钯950", "足钯",
    "铜合金", "钛合金", "锌合金", "钛钢", "不锈钢", "铜",
]
_METAL_RE = re.compile("|".join(re.escape(m) for m in METALS))

_METAL_ALIAS = {
    "S925银": "银925",
    "S990银": "足银",
    "S999银": "足银",
    "S800银": "银800",
}


def parse_metal(guige):
    """从规格列提取镶嵌金属并归一化到微信候选值：'S925银镶嵌' → '银925'"""
    m = _METAL_RE.search(s(guige))
    if not m:
        return ""
    metal = m.group(0)
    return _METAL_ALIAS.get(metal, metal)


def parse_fen_str(text):
    """'50分' → 50.0（解析已提取的主钻分数字符串）"""
    m = re.search(r'([\d.]+)\s*分', s(text))
    return float(m.group(1)) if m else None


def fen_to_carat(fen):
    """分 → 克拉字符串：100.0 → '1.00克拉'"""
    if fen is None:
        return ""
    return f"{fen / 100:.2f}克拉"


def append_attr(attr_str, key, val):
    """把 key=val 追加到商品属性字符串末尾；val 为空则不追加。"""
    v = s(val)
    if not v:
        return attr_str
    return f"{attr_str};{key}={v}" if attr_str else f"{key}={v}"


def parse_tone_from_attrs(attr_str):
    """从商品属性字符串取色调：'色调=粉钻;形状=...' → '粉钻'"""
    for pair in str(attr_str or "").replace("；", ";").split(";"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip() == "色调":
                return v.strip()
    return ""


def enrich_split_tone(rows):
    """同一(商品编码+系列)下若色调不同，按色调再拆一次，保证同商品标题一致。"""
    by_code = {}
    for row in rows:
        by_code.setdefault(row["商品编码"], []).append(row)
    for code, grp in by_code.items():
        tones = {parse_tone_from_attrs(r["商品属性"]) for r in grp}
        tones.discard("")
        if len(tones) > 1:
            for r in grp:
                tone = parse_tone_from_attrs(r["商品属性"])
                if tone:
                    r["商品编码"] = f"{code}-{tone}"
            print(f"  按色调拆分: {code!r} -> {sorted(tones)}")


def enrich_carat(rows):
    """按商品编码取主钻分数最大值，把「主钻克拉数」追加到每行商品属性。"""
    max_fen = {}
    for row in rows:
        fen = parse_fen_str(row.get("规格1值", ""))
        if fen is not None:
            code = row["商品编码"]
            max_fen[code] = max(max_fen.get(code, 0.0), fen)
    for row in rows:
        fen = max_fen.get(row["商品编码"])
        carat = fen_to_carat(fen) if fen is not None else "1.00克拉"
        row["商品属性"] = append_attr(row["商品属性"], "主钻克拉数", carat)


def parse_stock(val):
    """库存规则：售罄→0；预售/定制/样板/空 → 1（统一1）；数字→原样"""
    v = s(val)
    if not v:
        return "1"
    if "售罄" in v:
        return "0"
    if "定制" in v or v == "样板":
        return "1"
    m = re.search(r'\d+', v)
    return m.group(0) if m else "1"


def load_images():
    if not os.path.exists(UPLOADED_JSON):
        return []
    with open(UPLOADED_JSON, encoding="utf-8") as f:
        return json.load(f)


def match_images(sku_code, images):
    """按商家编码匹配主图：文件名去扩展名后以 sku_code 开头，排除 .psd"""
    urls = []
    for img in images:
        fname = img.get("file", "")
        if fname.lower().endswith(".psd"):
            continue
        stem = os.path.splitext(fname)[0]
        if stem == sku_code or stem.startswith(sku_code):
            wx = img.get("wx_url")
            if wx:
                urls.append(wx)
    return urls


def ceil_div07(price_fen):
    if price_fen is None:
        return None
    return (int(price_fen) * 10 + 6) // 7


def join_attrs(pairs):
    return ";".join(f"{k}={v}" for k, v in pairs if k and s(v))


def build_title(series, tone, category):
    """标题 = 主系列 + 子系列 + 色调 + 类目（子系列保留，使同系列商品标题一致、不同系列可区分）"""
    main, sub = split_series(series)
    series_cn = main or s(series)
    category_cn = s(category).split("(")[0].strip() or s(category)
    return " ".join(p for p in [series_cn, sub, tone, category_cn] if p)


def make_row(product_code, sku_code, title, category, attrs, weight, imgs,
             spec1_name, spec1_val, spec2_name, spec2_val, price_yuan, stock):
    return {
        "商品编码": product_code, "标题": title, "微信标题": title, "小红书标题": title,
        "内部类目": category, "品牌": "", "描述": "", "商品属性": attrs, "重量": weight,
        "主图": ",".join(imgs), "详情图": "", "SKU编码": sku_code,
        "规格1名称": spec1_name, "规格1值": spec1_val,
        "规格2名称": spec2_name, "规格2值": spec2_val,
        "规格3名称": "", "规格3值": "",
        "原价（元）": "", "售价（元）": price_yuan, "库存": stock,
    }


def parse_peiyuzuan(sheet, images):
    """培育钻：列结构与表头一致"""
    rows = list(sheet.iter_rows(values_only=True))
    data = rows[2:]
    result = []
    for r in data:
        shangjia = clean_base(s(r[9]))  # 商家编码
        if not shangjia:
            continue
        product_code = split_product_code(shangjia)
        # 同一商品编码下若混了多个系列（炽爱/真我等），按子系列拆成不同商品
        _, sub_series = split_series(r[19])
        if sub_series:
            product_code = f"{product_code}-{sub_series}"
        retail = r[37]  # 零售标价
        price_fen = ceil_div07(retail)
        price_yuan = (price_fen / 100) if price_fen is not None else ""
        title = build_title(r[19], r[24], r[16])
        cut_wx, cut_xhs = map_cut(r[30])
        cat = s(r[17]) or s(r[16])
        attr_pairs = [
            ("色调", r[24]), ("形状", r[27]), ("钻石颜色", r[28]),
            ("钻石净度", r[29]), ("切工级别", cut_wx), ("钻石切工", cut_xhs),
            ("镶嵌方式", r[21]), ("鉴定证书", r[26]), ("副钻分数", normalize_fuzuan(r[32])),
            ("圈号", "详情联系客服"), ("镶嵌", parse_metal(r[25])),
            ("合成方法", "高温高压法(HPHT)"), ("主体材质", "合成钻石"),
        ]
        # 吊坠类目(微信叶子 548288)比其它款式多一个必填「赠链材质」
        if "单坠" in cat or "吊坠" in cat:
            attr_pairs.append(("赠链材质", "银925链"))
        attrs = join_attrs(attr_pairs)
        main_ct = parse_main_ct(r[25])
        color = s(r[28])
        imgs = match_images(shangjia, images)
        result.append(make_row(
            product_code, shangjia, title, s(r[17]) or s(r[16]), attrs, s(r[22]),
            imgs, "主钻分数", main_ct, "钻石颜色", color, price_yuan, parse_stock(r[11]),
        ))
    return result


def parse_tianranzuan(sheet, images):
    """天然钻：列结构不同（商家编码=4，SKU码=5，规格=14，颜色=17，零售价=26）"""
    rows = list(sheet.iter_rows(values_only=True))
    data = rows[1:]
    result = []
    for r in data:
        shangjia = clean_base(s(r[4]))
        if not shangjia:
            continue
        product_code = split_product_code(shangjia)
        sku_code = clean_base(s(r[5])) or shangjia
        retail = r[26]
        price_fen = ceil_div07(retail)
        price_yuan = (price_fen / 100) if price_fen is not None else ""
        title = build_title(r[9], r[13], r[7])
        cut_wx, cut_xhs = map_cut(r[19])
        attrs = join_attrs([
            ("色调", r[13]), ("形状", XHS_SHAPE_MAP.get(s(r[16]), s(r[16]))), ("钻石颜色", r[17]),
            ("钻石净度", r[18]), ("切工级别", cut_wx), ("钻石切工", cut_xhs),
            ("副钻分数", normalize_fuzuan(r[20])), ("鉴定证书", r[15]),
            ("圈号", "详情联系客服"), ("镶嵌材质", "未镶嵌"),
            ("主体材质", "天然钻石"),
        ])
        main_ct = parse_main_ct(r[14])
        color = s(r[17])
        imgs = match_images(shangjia, images)
        result.append(make_row(
            product_code, sku_code, title, s(r[7]), attrs, s(r[11]),
            imgs, "主钻分数", main_ct, "钻石颜色", color, price_yuan, "1",
        ))
    return result


def main():
    images = load_images()
    print(f"uploaded_images.json 图片数: {len(images)}")
    wb = openpyxl.load_workbook(HUOPAI_PATH, read_only=True, data_only=True)
    print(f"货盘表 sheets: {wb.sheetnames}")

    rows = []
    if "培育钻" in wb.sheetnames:
        r1 = parse_peiyuzuan(wb["培育钻"], images)
        rows.extend(r1)
        print(f"培育钻: {len(r1)} 个 SKU")
    if "天然钻" in wb.sheetnames:
        r2 = parse_tianranzuan(wb["天然钻"], images)
        rows.extend(r2)
        print(f"天然钻: {len(r2)} 个 SKU")
    wb.close()

    enrich_carat(rows)
    enrich_split_tone(rows)

    out = Workbook()
    ws = out.active
    ws.title = "货盘转换预览"
    ws.append(HEADERS)
    for c in ws[1]:
        c.font = Font(bold=True)
    for row in rows:
        ws.append([row.get(h, "") for h in HEADERS])
    ws.freeze_panes = "A2"
    out.save(OUTPUT_PATH)

    product_codes = set(r["商品编码"] for r in rows if r["商品编码"])
    no_img = sum(1 for r in rows if not r["主图"])
    print(f"\n转换完成：{len(rows)} 个 SKU，{len(product_codes)} 个商品")
    print(f"无图片匹配的 SKU: {no_img} 个")
    print(f"输出文件: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
