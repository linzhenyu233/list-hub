# -*- coding: utf-8 -*-
"""图片根目录扫描:把「一个商品一个文件夹」的商品图片, 按编码匹配挂到 SKU 上。

运营实际目录(网络共享, 服务端可直读):
    SHINING HOUSE培育钻/
        1-3月/  4-6月份/  7-9月份/  培育裸钻/      ← 按月份分, 商品分散在不同月份
        APYE0112-Y-10/                             ← 单 SKU 商品: 文件夹名=SKU编码
            APYE0112-Y-10主图1.jpg / 主图2 / 主图3 / 主图PNG.png / 主图白底.jpg
        ZNJ1725W-50 三色/                          ← 多 SKU 商品: 本色图放根下
            ZNJ1725W-50主图1~3.jpg ...             ← 本色(W)的图
            ZNJ1725R-50/  ZNJ1725R-50主图1~3.jpg   ← 其他色在子文件夹, 文件夹名=SKU编码
            ZNJ1725Y-50/
        通用通栏/详情通栏_01.jpg                    ← 通用详情图(所有商品共用)
        750-1000/                                  ← 主图的 750x1000 尺寸版, 跳过

匹配规则:
    - 图片「编码段」= 文件名中「主图」之前的部分(去空格); 文件夹名再去掉「三色」等标记
      'ZNJ1725W-50主图2.jpg' -> 'ZNJ1725W-50';  'ZNJ1725W-50 三色' -> 'ZNJ1725W-50'
    - SKU 图     : 取该 SKU 的「主图2」(运营指定, 每个 SKU 固定用第 2 张)
    - 商品主图   : 商品文件夹根下的本色图, 按「商品编码」(去掉颜色字母的款号主干)归类
    - 通用详情图 : 通用通栏目录下的图
    - 同色不同分数(如 ZSTZ106SLW-20 与图片 ZSTZ106SLW-10)共用一套图: 去掉尾部 -数字 后匹配
    - 前缀匹配必须落在分隔符上: 'C065XAM00021R' 可匹配 'C065XAM00021R-30',
      但 'ZSED004W-10' 不能匹配 'ZSED004W-100-D1'(否则把 10 分的图给 100 分)
"""

import os
import re

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# 商家编码末尾的颜色字母: R=红色 W=白色 Y=黄色 S=银色(与 huopai_adapter 一致)
COLOR_LETTERS = "WYRS"

# 主图的尺寸版本目录: 内容与主图同款, 只是 750x1000, 不作为独立图片
SKIP_DIR_NAMES = {"750-1000"}

# 文件名含这些标记的是尺寸版本(如 主图750-1.jpg / 主图750-1000-1.jpg), 跳过
SKIP_FILENAME_MARKERS = ("750",)

# 商品文件夹名里可能带的中文标记, 提取编码时去掉
NAME_TAGS = ("三色", "二色", "双色", "四色", "五色")

# 通用详情图目录(不受月份过滤影响)
COMMON_DETAIL_NAMES = {"通用通栏", "放置于详情内的通用通栏"}

# 非商品目录
NON_PRODUCT_NAMES = {"文档", "__MACOSX"}

# 文件名里的主图标记, 编码段取它之前的部分
MAIN_MARK = "主图"

# 非数字主图角色排序权重
ROLE_ORDER = {"白底": 2, "PNG": 3}


def _clean(text):
    return re.sub(r"\s+", "", str(text or "")).strip()


def code_of_filename(filename):
    """文件名 -> 编码段。'ZNJ1725W-50主图2.jpg' -> 'ZNJ1725W-50'"""
    stem = os.path.splitext(filename)[0]
    index = stem.find(MAIN_MARK)
    code = stem[:index] if index > 0 else stem
    return _clean(code)


def code_of_dirname(dirname):
    """文件夹名 -> 编码段。'ZNJ1725W-50 三色' -> 'ZNJ1725W-50'"""
    name = _clean(dirname)
    for tag in NAME_TAGS:
        if name.endswith(tag):
            name = name[: -len(tag)]
            break
    return name


def product_code_of(code):
    """SKU 编码 -> 商品编码(去掉颜色字母的款号主干), 规则同 huopai_adapter.split_product_code。

    'ZNJ1725W-50' -> 'ZNJ1725';  'APYE0112-Y-10' -> 'APYE0112'
    图片文件夹名是 SKU 编码, 但商品级主图要按商品编码挂, 必须做这层转换。
    """
    base = _clean(code).split("-")[0]
    if base and base[-1] in COLOR_LETTERS:
        base = base[:-1]
    return base


def main_role(filename):
    """判断主图序号: '主图2.jpg' -> '2'; '主图PNG.png' -> 'PNG'; '主图白底.jpg' -> '白底'。"""
    stem = os.path.splitext(filename)[0]
    index = stem.find(MAIN_MARK)
    if index < 0:
        return ""
    tail = stem[index + len(MAIN_MARK):].strip()
    if not tail:
        return "1"          # 「主图.jpg」视为第 1 张
    match = re.match(r"(\d+)", tail)
    if match:
        return match.group(1)
    return tail


def sort_key(path):
    """根下商品图排序: 主图1 -> 主图2 -> 主图3 -> 白底 -> PNG -> 其它。"""
    role = main_role(os.path.basename(path))
    if role.isdigit():
        return (0, int(role), os.path.basename(path))
    return (1, ROLE_ORDER.get(role, 9), os.path.basename(path))


def _walk_images(folder, skip_dir_names):
    """遍历一个商品文件夹下的所有图片(跳过尺寸目录/尺寸文件/非图片)。"""
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d not in skip_dir_names]
        for filename in filenames:
            if os.path.splitext(filename)[1].lower() not in IMAGE_EXTENSIONS:
                continue
            if any(marker in filename for marker in SKIP_FILENAME_MARKERS):
                continue
            yield dirpath, filename


def scan_image_root(root, months=None, skip_dir_names=None):
    """扫描图片根目录。

    - root:    图片根目录(本地路径或 UNC 网络路径)
    - months:  只扫这些月份目录(如 ["7-9月份"]);None = 全部
    返回 {
      "sku_images":    {SKU编码: {"main1": 路径, "main2": 路径, "all": [路径...]}},
      "root_images":   {商品编码: [根下图片...]},      ← 已按商品编码(去颜色字母)归类
      "common_images": [通用详情图...],
      "unmatched":     [无法识别编码的图片...],
      "scanned":       扫描到的图片总数,
      "folders":       商品文件夹数,
    }
    """
    skip_dir_names = set(skip_dir_names or SKIP_DIR_NAMES)
    result = {"sku_images": {}, "root_images": {}, "common_images": [],
              "unmatched": [], "scanned": 0, "folders": 0}

    for month in sorted(os.scandir(root), key=lambda e: e.name):
        if not month.is_dir() or month.name in NON_PRODUCT_NAMES:
            continue
        # 通用详情图目录: 不受月份过滤影响, 所有商品共用
        if month.name in COMMON_DETAIL_NAMES:
            for dirpath, filename in _walk_images(month.path, skip_dir_names):
                result["common_images"].append(os.path.join(dirpath, filename))
            continue
        if months and month.name not in months:
            continue

        for product_dir in sorted(os.scandir(month.path), key=lambda e: e.name):
            if not product_dir.is_dir() or product_dir.name in NON_PRODUCT_NAMES:
                continue
            result["folders"] += 1
            folder_code = code_of_dirname(product_dir.name)
            for dirpath, filename in _walk_images(product_dir.path, skip_dir_names):
                full = os.path.join(dirpath, filename)
                result["scanned"] += 1
                at_root = os.path.normcase(dirpath) == os.path.normcase(product_dir.path)
                if at_root:
                    # 按商品编码归类: 文件夹名是 SKU 编码(含颜色字母/分数), 商品级主图要用款号主干查
                    key = product_code_of(folder_code) or folder_code
                    result["root_images"].setdefault(key, []).append(full)
                code = code_of_filename(filename) or code_of_dirname(os.path.basename(dirpath))
                if not code:
                    result["unmatched"].append(full)
                    continue
                entry = result["sku_images"].setdefault(code, {"all": []})
                entry["all"].append(full)
                role = main_role(filename)
                if role == "1" and not entry.get("main1"):
                    entry["main1"] = full
                elif role == "2" and not entry.get("main2"):
                    entry["main2"] = full

    for paths in result["root_images"].values():
        paths.sort(key=sort_key)
    return result


def _boundary_match(longer, shorter):
    """shorter 是 longer 的前缀且落在边界上(结尾或后面紧跟 '-'), 防止 10 误配 100。"""
    if not longer.startswith(shorter):
        return False
    return len(longer) == len(shorter) or longer[len(shorter)] == "-"


def match_sku_code(sku_code, sku_images):
    """把一个 SKU 编码匹配到扫描到的图片编码。

    顺序:
      ① 精确相同
      ② 自己去掉尾部 -数字 后命中(如 C065XAM00021R-30 -> 文件夹 C065XAM00021R)
      ③ 图库里有同色不同分数的图(如 SKU ZSTZ106SLW-20, 图库只有 ZSTZ106SLW-10)
      ④ 前缀匹配, 但必须落在 '-' 边界上
    返回命中的图片编码, 没有则 None。
    """
    sku = _clean(sku_code)
    if not sku:
        return None
    if sku in sku_images:
        return sku
    base = re.sub(r"-\d+$", "", sku)
    if base and base in sku_images:
        return base
    # 同色不同分数共用一套图: 图库常只拍其中一个分数, 其余分数复用同一套
    if base:
        pattern = re.compile(re.escape(base) + r"-\d+$")
        for code in sku_images:
            if code and pattern.fullmatch(code):
                return code
    best = None
    for code in sku_images:
        if not code:
            continue
        if _boundary_match(sku, code) or _boundary_match(code, sku):
            if best is None or len(code) > len(best):
                best = code
    return best


def build_report(scan_result):
    """输出可读的扫描统计, 便于运营核对。"""
    lines = []
    lines.append("商品文件夹 %d 个, 扫描图片 %d 张" % (scan_result["folders"], scan_result["scanned"]))
    lines.append("识别出 SKU 编码 %d 个, 其中有「主图2」的 %d 个" % (
        len(scan_result["sku_images"]),
        sum(1 for v in scan_result["sku_images"].values() if v.get("main2"))))
    lines.append("商品主图(按商品编码): %d 个商品" % len(scan_result["root_images"]))
    lines.append("通用详情图: %d 张" % len(scan_result["common_images"]))
    lines.append("未识别编码的图片: %d 张" % len(scan_result["unmatched"]))
    return "\n".join(lines)
