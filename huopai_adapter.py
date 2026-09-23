# -*- coding: utf-8 -*-
"""货盘表 → 系统标准模板 转换器

映射规则（与运营对齐）：
- 商品编码 = 商家编码的「字母+数字」主干（ZSDZ388W-100 → ZSDZ388）
- SKU 编码 = 完整商家编码（ZSDZ388W-100）
- 规格维度 = 主钻分数 + 钻石颜色
- 售价(元) = ceil(零售标价(元) / 0.7)，划线价不填
  注: 货盘「零售标价」这一列的单位是「元」不是「分」, 除以 0.7 的结果正好等于货盘自己的
      「上架价」列(如 489300/0.7=699000, 货盘该列就是 699000)。此前误当成"分"又除了 100,
      导致上架价比货盘价小 100 倍。
- 库存：供定制/可定制 → 预售(不设库存)；售罄 → 0；数字 → 原样
- 图片：上传结果文件(uploaded_images[_<shop_id>].json)按商家编码前缀匹配，排除 .psd
- 标题：所有拆分跑完后按商品编码生成微信/小红书两套标题（见 enrich_titles）
  标题里不放款号/货号、也不放克拉；同系列同形状的不同设计款在货盘里没有区分字段，
  标题必然重复（平台会拒），处理方案见 enrich_titles 上方的「待办」
"""
import collections
import json
import os
import re

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Font

from runtime_config import load_project_env

load_project_env()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 货盘表默认位置:可用 HUOPAI_PATH 环境变量覆盖。
# /import-huopai 只允许读取该文件所在目录(或 HUOPAI_DIR)内的 .xlsx。
HUOPAI_PATH = os.environ.get(
    "HUOPAI_PATH",
    r"C:\Users\zssj\Desktop\共享-线上渠道货盘表（附库存）最新.xlsx",
)
UPLOADED_JSON = os.path.join(BASE_DIR, "uploaded_images.json")


def uploaded_json_for(shop_id=None):
    """按店铺解析图片上传结果文件：优先 uploaded_images_<shop_id>.json，回退旧文件。

    旧文件是单店时代 upload_images.py 的输出（历史数据沿用）；指定 --shop-id
    重跑上传后会生成按店命名的新文件。多店铺下两店素材空间不同，
    转换发品前必须用对应店铺的上传结果，否则 A 店发的是 B 店的图。
    """
    if shop_id:
        per_shop = os.path.join(BASE_DIR, f"uploaded_images_{shop_id}.json")
        if os.path.exists(per_shop):
            return per_shop
    return UPLOADED_JSON


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


# 商家编码末尾字母 = 戒托颜色：R=红色 W=白色 Y=黄色 S=银色
SETTING_COLOR = {"R": "红色", "W": "白色", "Y": "黄色", "S": "银色"}


def extract_setting_color(shangjia):
    """从商家编码提取「戒托颜色」。支持两种编码格式：
      ① 颜色字母在第一段末尾：ZSTZ106JR-15 → 红色;ZSTZ105SLW-A-10 → 白色
      ② 颜色字母是独立一段：APYE0139-W-30-D1 → 白色
    同款不同戒托色在原表里除编码字母外字段完全相同,必须靠它作为规格值区分 SKU,
    否则多个 SKU 规格值重复,平台 SKU 选择器只能显示一个。"""
    parts = s(shangjia).split("-")
    base = parts[0] if parts else ""
    if base and base[-1] in SETTING_COLOR:
        return SETTING_COLOR[base[-1]]
    if len(parts) > 1 and parts[1] and parts[1][-1] in SETTING_COLOR:
        return SETTING_COLOR[parts[1][-1]]
    return ""


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


def parse_attrs(attr_str):
    """商品属性字符串 → dict：'色调=粉钻;形状=公主方' → {'色调': '粉钻', '形状': '公主方'}"""
    attrs = {}
    for pair in str(attr_str or "").replace("；", ";").split(";"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            if key.strip():
                attrs[key.strip()] = value.strip()
    return attrs


def parse_tone_from_attrs(attr_str):
    """从商品属性字符串取色调：'色调=粉钻;形状=...' → '粉钻'"""
    return parse_attrs(attr_str).get("色调", "")


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


def extract_chain_tag(sku_code):
    """手链 SKU 编码 → 链型/绳色标签。规则(与运营确认):
      - 末段 -R / -B  = 绳色(红绳 / 黑绳)
      - 中间段 -A / -B = 链型(半链 / 全绳);无该段 = 全链
      - 末段 -LR/-LB/-SR/-SB = 尺寸 + 绳色(取绳色)
    例：ZSTZ105SLW-A-10→半链;GLLP6912W-R→红绳;ZSSL396W-LB→黑绳。"""
    parts = s(sku_code).split("-")
    rest = parts[1:]
    for idx, seg in enumerate(rest):
        is_last = (idx == len(rest) - 1)
        if seg in ("A", "B"):
            if is_last:
                return {"B": "黑绳"}.get(seg, "")
            return {"A": "半链", "B": "全绳"}.get(seg, "")
        if seg == "R":
            return "红绳"
        if len(seg) == 2 and seg[0] in ("L", "S") and seg[1] in ("R", "B"):
            return {"R": "红绳", "B": "黑绳"}.get(seg[1], "")
    return ""


def enrich_split_chain(rows):
    """手链按链型/绳色拆分商品。
    小红书只支持 2 个规格维度(主钻分数 + 戒托颜色),链型(全链/半链/全绳)、
    绳色(红绳/黑绳)放不进规格,只能拆成不同商品;商品编码加 -标签 便于系统内区分,
    标签(自然语言,不加连字符)记在 _款标签 上,由 enrich_titles 补到标题末尾。"""
    by_code = {}
    for row in rows:
        by_code.setdefault(row["商品编码"], []).append(row)
    for code, grp in by_code.items():
        tags = {extract_chain_tag(r.get("SKU编码", "")) for r in grp}
        tags.discard("")
        if not tags:
            continue
        for r in grp:
            tag = extract_chain_tag(r.get("SKU编码", ""))
            if not tag:
                continue
            r["商品编码"] = f"{code}-{tag}"
            r.setdefault("_款标签", []).append(tag)
        print(f"  按链型/绳色拆分: {code!r} -> {sorted(tags)}")


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


# ==================================================================
# 标题生成（替代原先逐行拼「系列+色调+类目」的做法）
# ------------------------------------------------------------------
# 结构照搬两个平台**已上架**商品的写法（2026-09 拉线上标题逐条核对）：
#   微信  : [培育]SHINING HOUSE/钻石世家 {系列}{材质}镶嵌{色调}{形状}{品名}{克拉}{附证书}
#   小红书: 【培育】SHINING HOUSE{子系列}{材质}镶嵌{克拉}{色调}{形状}{品名}
# 差异都是线上实测出来的：
#   - 培育标记：微信用半角 [培育]、小红书用全角 【培育】；天然钻两款都不加
#   - 品牌写法：微信用「SHINING HOUSE/钻石世家」，小红书只写「SHINING HOUSE」
#   - 品名：微信规范要求用通用名（项链/戒指/耳环），取内部类目括号里的细类；
#           小红书线上 151 条在售都沿用内部词（颈饰/手饰/耳饰/单坠），保持一致
#   - 长度：微信线上最长 ~57 字、小红书 ~35 字（代码里「名称 8-30 字」那条注释与线上不符）
# 卖点词（轻奢/通勤/百搭）是运营手写的，没有数据依据，不自动生成；
# 唯一自动加的尾缀是「附XX证书」，来源是商品属性「鉴定证书」。
# ==================================================================
# 品牌写法：默认保持原值；某店品牌写法不同时用 .env 覆盖
# （HUOPAI_WECHAT_BRAND / HUOPAI_XHS_BRAND）。enrich_titles 的拼接顺序与字段
# 一律不动（运营硬约束），只允许替换这两个前缀常量。
WECHAT_BRAND = os.environ.get("HUOPAI_WECHAT_BRAND") or "SHINING HOUSE/钻石世家"
XHS_BRAND = os.environ.get("HUOPAI_XHS_BRAND") or "SHINING HOUSE"
# 微信：官方《添加商品》文档 title 最多 60 字符，且「中文/字母/数字各算 1 个字符」
# （线上 57 字的中文标题能过，也印证了中文按 1 算）。取 58 留 2 个余量。
WECHAT_TITLE_MAX = 58
# 小红书：平台报错原文「标题长度需要在[8]-[30]个字或[16]-[60]个字符」——
#   「字」每个字符算 1；「字符」中文/全角算 2、字母数字算 1；满足任一区间即可。
# 这里按最严的那档卡「≤30 个字」（≤30 字时加权必然 ≤60 字符，两个口径都满足），
# 中文英文混排也不会踩线（线上实测最长 42 字/51 字符的那种是后台手工建的，接口更严）。
XHS_TITLE_MAX = 30

# 内部类目括号里的细类 → 微信规范品名
_NOUN_BY_INNER = {
    "项链": "项链", "手链": "手链", "手镯": "手镯", "吊坠": "吊坠", "脚链": "脚链",
    "戒指": "戒指", "女戒": "戒指", "男戒": "戒指", "对戒": "戒指",
    "耳钉": "耳钉", "耳环": "耳环",
}
# 括号里只是数量说明（对/单只）时，回头用括号前的内部词兜底成通用名
_NOUN_BY_OUTER = {"颈饰": "项链", "手饰": "手链", "耳饰": "耳环", "单坠": "吊坠", "戒指": "戒指"}


def resolve_nouns(category):
    """内部类目 → (微信规范品名, 小红书沿用品名)。
    '粉钻培育钻-颈饰(项链)' → ('项链', '颈饰')
    '粉钻培育钻-手饰(女戒)' → ('戒指', '手饰')
    '粉钻培育钻-耳饰(单只)' → ('耳环（单只）', '耳饰')
    """
    text = s(category)
    match = re.search(r"[（(]([^）)]*)[）)]", text)
    inner = match.group(1).strip() if match else ""
    outer = re.sub(r"[（(][^）)]*[）)]", "", text).rsplit("-", 1)[-1].strip()
    noun = _NOUN_BY_INNER.get(inner, "")
    if not noun and inner in ("对", "一对", "对装"):
        noun = "耳环（一对）"
    if not noun and ("单只" in inner or "一只" in inner):
        noun = "耳环（单只）"
    if not noun:
        noun = _NOUN_BY_OUTER.get(outer, "") or outer
    return noun, (outer or inner)


def fit_title(pieces, names, limit, drop_order=(), suffix=""):
    """拼标题；超长时按 drop_order 依次丢掉可选片段，返回 (标题, 被丢掉的片段名)。

    两个平台都用「字数」衡量（微信 60 字符、小红书 30 字，中文/字母/数字各算 1）。
    suffix（拆分标签，如"半链/全绳"）无条件保留，实在放不下时只截正文。
    """
    parts = list(pieces)
    dropped = []
    for index in drop_order:
        if len("".join(parts)) + len(suffix) <= limit:
            break
        if parts[index]:
            dropped.append(names[index])
        parts[index] = ""
    body = "".join(parts)
    if len(body) + len(suffix) > limit:
        body = body[:max(0, limit - len(suffix))]
    return body + suffix, dropped


# ==================================================================
# 待办：标题重名的兜底手段（运营说先不改，先把方案和数据记在这儿）
# ------------------------------------------------------------------
# 现状：同「系列+色调+形状+品名」的不同设计款，货盘里没有能写进标题的区分字段
# （实测最多 30 个款共用一组字段，其中若干连镶嵌方式/副钻分数/证书都一样，
#  只有"重量"不同，而重量不能写进标题），所以这些款标题必然重复，平台会拒。
# 实测"把某些字段放进标题"能压掉多少重复（250 个商品里会失败的数量）：
#     现在的字段          微信 151 / 小红书 162
#     +克拉               微信  88 / 小红书  95
#     +净度+克拉           微信  67 / 小红书  73
#     +净度+镶嵌方式+克拉    微信  54 / 小红书  58
# 三个可选做法（都还没做）：
#   ① 标题里加回「克拉 + 钻石净度 + 镶嵌方式」：全自动，失败从 151 降到 54；
#   ② 加「款式名」人工区分：填在转换预览的第二张表里（不用动货盘表结构），
#      之前实现过一版（load_styles + enrich_titles(rows, styles) + 预览第二张表），
#      因为不想人工填先删了，需要时按 git 历史重加；
#   ③ 先发，只修被平台拒掉的那些：发布进度里能看到失败清单，
#      在「审核编辑」抽屉里直接改微信/小红书标题再重试。
# ==================================================================
def enrich_titles(rows):
    """按商品编码生成 标题/微信标题/小红书标题（必须在 enrich_carat、enrich_split_* 之后调用）。

    为什么不逐行拼标题：
      1) 平台标题是 SPU 级，同一商品各 SKU 行必须完全一致；
      2) 必须等商品拆完再生成，否则拆出来的几个商品标题又会互相撞；
      3) 标题里只能放商品级信息（款内各 SKU 不同的净度/分数不能进标题）。
    """
    by_code = {}
    for row in rows:
        by_code.setdefault(row["商品编码"], []).append(row)
    items = []
    for code, group in by_code.items():
        first = group[0]
        attrs = parse_attrs(first.get("商品属性", ""))
        # 只取主系列（不带子系列，如 '永恒之环-FLY·自在' → '永恒之环'）：运营要求标题里不加小系列
        series_raw = s(first.get("_系列原文")) or s(code).split("-")[0]
        series = split_series(series_raw)[0] or series_raw
        # 形状/色调里货盘常写成 '圆形+水滴形'『白钻+粉钻』，标题里不用 + 号
        tone = attrs.get("色调", "").replace("+", "")
        shape = attrs.get("形状", "").replace("+", "")
        metal = f"{attrs['镶嵌']}镶嵌" if attrs.get("镶嵌") else ""
        wechat_noun, xhs_noun = resolve_nouns(first.get("内部类目", ""))
        tags = " ".join(first.get("_款标签") or [])
        is_lab_grown = "合成" in str(attrs.get("主体材质", ""))
        # 证书名货盘写成 'IGI+NGTC'，标题里也统一不用 + 号
        cert = re.sub(r"[（(][^）)]*[）)]", "", str(attrs.get("鉴定证书", ""))).replace("+", "、").strip()
        # 标题是给买家看的，不放款号/货号（运营要求）。
        # 同系列同形状的不同设计款在货盘里没有能写进标题的区分字段，标题会重复——
        # 这里照实生成并标记出来，不编造（可选处理方案见上面「待办」）。
        suffix = f" {tags}" if tags else ""
        # 微信：官方框架「品牌 + 基本属性 + 商品品名 + 规格参数」
        # 品牌和系列拆成两段：否则长度不够时会把「品牌+系列」整块丢掉，标题只剩"耳环一对"
        # 可丢顺序：证书 → 形状 → 材质 → 色调 → 系列 → 品牌 → 培育标记（品名永不丢）
        wx_pieces = [
            "[培育]" if is_lab_grown else "", WECHAT_BRAND, series,
            metal, tone, shape, wechat_noun, f"附{cert}证书" if cert else "",
        ]
        wx_names = ["培育标记", "品牌", "系列", "材质", "色调", "形状", "品名", "证书"]
        # 小红书：照线上写法（不带子系列）；可丢顺序：材质 → 色调 → 形状 → 品牌（品名永不丢）
        xhs_pieces = [
            f"{'【培育】' if is_lab_grown else ''}{XHS_BRAND}", metal, tone, shape, xhs_noun,
        ]
        xhs_names = ["品牌", "材质", "色调", "形状", "品名"]
        wx_title, wx_dropped = fit_title(wx_pieces, wx_names, WECHAT_TITLE_MAX, (7, 5, 3, 4, 2, 1, 0), suffix)
        # 小红书按「字数」卡 30（见 XHS_TITLE_MAX 的说明），和微信用同一套算法
        xhs_title, xhs_dropped = fit_title(xhs_pieces, xhs_names, XHS_TITLE_MAX, (1, 2, 3, 0), suffix)
        items.append({
            "group": group, "wx": wx_title, "xhs": xhs_title,
            # 证书尾缀是被挤掉的第一个字段（本来就可有可无），不进备注，免得备注全是噪音
            "notes": [n for n in (
                f"微信丢弃{'/'.join(d for d in wx_dropped if d != '证书')}"
                if any(d != "证书" for d in wx_dropped) else "",
                f"小红书丢弃{'/'.join(xhs_dropped)}" if xhs_dropped else "",
            ) if n],
        })
    # 查重：两个平台都按标题判重，重复的那些发不出去，先在预览里标出来
    for key, label in (("wx", "微信"), ("xhs", "小红书")):
        counts = collections.Counter(item[key] for item in items)
        for item in items:
            shared = counts[item[key]]
            if shared > 1:
                item["notes"].append(f"{label}标题与另 {shared - 1} 个款重复")

    compressed = duplicated = 0
    for item in items:
        if any("丢弃" in note for note in item["notes"]):
            compressed += 1
        if any("重复" in note for note in item["notes"]):
            duplicated += 1
        for row in item["group"]:
            row["标题"] = item["wx"]
            row["微信标题"] = item["wx"]
            row["小红书标题"] = item["xhs"]
            row["标题备注"] = "；".join(item["notes"])
    print(f"  标题生成: {len(items)} 个商品, {duplicated} 个标题重名(平台会拒), "
          f"{compressed} 个因长度丢了字段")


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


def load_images(shop_id=None):
    """读图片上传结果；传 shop_id 时按店取文件（无按店文件则回退旧文件）。"""
    path = uploaded_json_for(shop_id)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_BOUNDARY_BLOCK = re.compile(r"[A-Za-z0-9]")


def _boundary_match(stem, code):
    """stem 以 code 开头、且紧跟的字符落在边界上（结尾 / 分隔符 / 中文标记）。

    不能直接 startswith：商家编码尾部就是分数（-10 / -100），
    'ZSED004W-10' 会命中 'ZSED004W-100-D1' 的图，把 10 分的图发到 100 分商品上。
    规则与 bulk_api/image_scanner.py 的 _boundary_match 一致（那边只认 '-'，
    这里放宽到「非字母数字」以兼容 'XXX-50主图2.jpg' 这类带中文标记的命名）。
    """
    if not code or not stem.startswith(code):
        return False
    rest = stem[len(code):]
    if not rest:
        return True
    return not _BOUNDARY_BLOCK.match(rest)


def match_images(sku_code, images):
    """按商家编码匹配主图：文件名去扩展名后等于 sku_code，或以 sku_code+边界 开头，排除 .psd"""
    if not sku_code:
        # 编码为空时 startswith("") 恒真，会把整套图挂到每个商品上
        return []
    urls = []
    for img in images:
        fname = img.get("file", "")
        if fname.lower().endswith(".psd"):
            continue
        stem = os.path.splitext(fname)[0]
        if _boundary_match(stem, sku_code):
            wx = img.get("wx_url")
            if wx:
                urls.append(wx)
    return urls


def parse_price_yuan(value):
    """源表零售价 → 数值（元），解析不出返回 None。

    这一列实测有 1280 / 1280.0 / '1,280' / '￥1280' / '1280元' / 空 等多种写法，
    原来直接 int() 遇到带千分位或带单位的写法会抛 ValueError，
    把整批转换直接打断（一行脏数据毁掉整次转换），所以统一先清洗再取数。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = s(value)
    if not text:
        return None                      # 空单元格：正常情况，不告警
    cleaned = (text.replace(",", "").replace("，", "")
                   .replace("￥", "").replace("¥", "").replace("元", ""))
    match = re.search(r"\d+(?:\.\d+)?", cleaned)
    if not match:
        print(f"[货盘转换] WARN: 零售价无法解析({text!r})，该行售价留空", flush=True)
        return None
    price = float(match.group(0))
    if price <= 0:
        print(f"[货盘转换] WARN: 零售价非正数({text!r})，该行售价留空", flush=True)
        return None
    return price


def ceil_div07(retail):
    """售价(元) = 零售价 / 0.7，向上取整（与货盘「上架价」列同口径）。

    沿用原整数算法（先 ×10 再按 7 向上取整），但先四舍五入到「角」，
    避免 100.1/0.7 = 143.00000000000003 这类浮点误差把结果多算 1 元。
    """
    price = parse_price_yuan(retail)
    if price is None:
        return ""
    return (round(price * 10) + 6) // 7


def join_attrs(pairs):
    return ";".join(f"{k}={v}" for k, v in pairs if k and s(v))


def make_row(product_code, sku_code, category, attrs, weight, imgs,
             spec1_name, spec1_val, spec2_name, spec2_val, price_yuan, stock):
    # 标题三列这里留空: 标题是 SPU 级、且要用款级聚合值(款内最大克拉), 统一由 enrich_titles
    # 在所有拆分完成后按商品编码生成。逐行拼会在 enrich_split_* 拆完商品后重新撞车。
    return {
        "商品编码": product_code, "标题": "", "微信标题": "", "小红书标题": "",
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
    for row_index, r in enumerate(data, start=3):   # 1-based 行号（表头占 2 行）
        shangjia = clean_base(s(r[9]))  # 商家编码
        if not shangjia:
            continue
        product_code = split_product_code(shangjia)
        # 同一商品编码下若混了多个系列（炽爱/真我等），按子系列拆成不同商品
        _, sub_series = split_series(r[19])
        if sub_series:
            product_code = f"{product_code}-{sub_series}"
        retail = r[37]  # 零售标价(单位: 元, 不是分)
        # 售价(元) = 零售标价/0.7, 与货盘「上架价」列一致；解析不出时 ceil_div07 返回 ""
        price_yuan = ceil_div07(retail)
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
        # 规格维度2 = 戒托颜色(从商家编码末尾字母 R/W/Y 提取);
        # 原「钻石颜色」(r[28])同款全部是"无色",多个 SKU 规格值重复无法区分
        setting_color = extract_setting_color(shangjia)
        imgs = match_images(shangjia, images)
        row = make_row(
            product_code, shangjia, s(r[17]) or s(r[16]), attrs, s(r[22]),
            imgs, "主钻分数", main_ct, "戒托颜色", setting_color, price_yuan, parse_stock(r[11]),
        )
        # 原始「系列」字段(如 'Shining系列-EASE·真我')供标题生成用:
        # 标准列里没有它, 而 split_series 只留 '·' 后的中文, 会把 'EASE' 丢掉
        row["_系列原文"] = s(r[19])
        # 来源定位：发布完要把平台商品ID回填进货盘表原文件, 必须记住这一行来自哪张表哪一行,
        # 以及当时的商家编码(回填前用它核对, 防止货盘表被改过/重排序导致写错行)
        row["_源表"] = sheet.title
        row["_源行"] = row_index
        row["_源码"] = s(r[9])   # 原表里的原始商家编码(未清洗), 回填前用它核对行没被改动
        result.append(row)
    return result


def parse_tianranzuan(sheet, images):
    """天然钻：列结构不同（商家编码=4，SKU码=5，规格=14，颜色=17，零售价=26）"""
    rows = list(sheet.iter_rows(values_only=True))
    data = rows[1:]
    result = []
    for row_index, r in enumerate(data, start=2):   # 1-based 行号（表头占 1 行）
        shangjia = clean_base(s(r[4]))
        if not shangjia:
            continue
        product_code = split_product_code(shangjia)
        sku_code = clean_base(s(r[5])) or shangjia
        retail = r[26]  # 零售价(单位: 元, 不是分)
        # 售价(元) = 零售价/0.7, 与货盘「上架价」列一致；解析不出时 ceil_div07 返回 ""
        price_yuan = ceil_div07(retail)
        cut_wx, cut_xhs = map_cut(r[19])
        attrs = join_attrs([
            ("色调", r[13]), ("形状", XHS_SHAPE_MAP.get(s(r[16]), s(r[16]))), ("钻石颜色", r[17]),
            ("钻石净度", r[18]), ("切工级别", cut_wx), ("钻石切工", cut_xhs),
            ("副钻分数", normalize_fuzuan(r[20])), ("鉴定证书", r[15]),
            ("圈号", "详情联系客服"), ("镶嵌材质", "未镶嵌"),
            ("主体材质", "天然钻石"),
        ])
        main_ct = parse_main_ct(r[14])
        # 规格维度2 = 戒托颜色(从商家编码末尾字母提取)
        setting_color = extract_setting_color(shangjia)
        imgs = match_images(shangjia, images)
        row = make_row(
            product_code, sku_code, s(r[7]), attrs, s(r[11]),
            imgs, "主钻分数", main_ct, "戒托颜色", setting_color, price_yuan, "1",
        )
        row["_系列原文"] = s(r[9])
        # 来源定位(同培育钻): 发布完要把平台商品ID回填进货盘表原文件
        row["_源表"] = sheet.title
        row["_源行"] = row_index
        row["_源码"] = s(r[4])
        result.append(row)
    return result


def write_preview(rows, path=OUTPUT_PATH):
    """把转换结果写成「货盘转换预览.xlsx」，给运营发布前过一遍。

    「标题备注」是预览专用列：标注标题重复（这些发不出去）、以及因长度丢了哪些字段。
    它不在标准模板 HEADERS 里，导入时按表头名取值、多出来的列会被忽略。
    """
    out = Workbook()
    ws = out.active
    ws.title = "货盘转换预览"
    headers = HEADERS + ["标题备注"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append([row.get(header, "") for header in headers])
    ws.freeze_panes = "A2"
    out.save(path)


def main():
    images = load_images()
    print(f"图片上传结果: {uploaded_json_for()} ({len(images)} 张)")
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
    enrich_split_chain(rows)
    # 标题必须在所有拆分之后生成（拆出来的商品编码变了，标题才能跟着区分开）
    enrich_titles(rows)

    write_preview(rows)

    product_codes = set(r["商品编码"] for r in rows if r["商品编码"])
    no_img = sum(1 for r in rows if not r["主图"])
    print(f"\n转换完成：{len(rows)} 个 SKU，{len(product_codes)} 个商品")
    print(f"无图片匹配的 SKU: {no_img} 个")
    print(f"输出文件: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
