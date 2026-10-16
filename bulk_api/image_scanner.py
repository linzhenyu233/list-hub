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
    - 商品详情图 : 文件名带「详情页/详情图/长图」的图(如 HZE118详情页.jpg / images/HZE118详情页_01..05.jpg),
                   按「商品编码」归类; 整图与分片同时在时只用分片(内容重复, 见 pick_detail_images)
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

# ⚠️ 通用通栏里混着「整页拼接长图」，与同目录的分段图内容重复（2026-10-15 实测+用户反馈）：
#   通用通栏\详情通栏_02.jpg 是 790x3741 的长图，缩略图逐段比对下来就是
#   1包装 / 2项链 / 2戒指 / 详情通栏_01 / 3品牌介绍---通栏_01 … 这些分段拼起来的，
#   两个一起发 = 详情里同一段内容连着出现两遍。
# 口径：**保留分段图、跳过拼接长图**（分段图手机端加载快、能点开看大图，长图要点很久）。
COMMON_DETAIL_SKIP_STEMS = ("详情通栏_02",)
# 兜底：文件名不在上面这张表里、但"高 ≥ 宽 × 该倍数"的一律当拼接长图跳过，
# 免得运营以后又拼一张新的长图进来，我们又发了两遍内容。
COMMON_DETAIL_LONG_ASPECT = 3.0

# 非商品目录
NON_PRODUCT_NAMES = {"文档", "__MACOSX"}

# 文件名里的主图标记, 编码段取它之前的部分
MAIN_MARK = "主图"

# 商品自有详情图的文件名标记(编码段同样取它之前的部分):
#   HZE118详情页.jpg            整张长图
#   images/HZE118详情页_01.jpg  长图切成的分片(实测多数商品是"整图 + 5 片"同时在)
# 2026-10-15 补: 以前只收集「主图 / SKU 图 / 通用通栏」, 商品自己的详情页完全没进
# 详情图列表 —— 表现就是各平台详情里只有通用通栏, 没有该商品自己的图。
DETAIL_MARKS = ("详情页", "详情图", "详情长图", "详情", "长图")
# 分片文件名的序号(用于排序 + 判断"这是分片而不是整图")
_DETAIL_INDEX = re.compile(r"_(\d+)\s*$")

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
    # 「主图-2.jpg」这种多一个连字符的写法也要认出来(实测有款图集这么命名)
    match = re.match(r"[^\d]*(\d+)", tail)
    if match:
        return match.group(1)
    return tail


def sort_key(path):
    """根下商品图排序: 主图1 -> 主图2 -> 主图3 -> 白底 -> PNG -> 其它。"""
    role = main_role(os.path.basename(path))
    if role.isdigit():
        return (0, int(role), os.path.basename(path))
    return (1, ROLE_ORDER.get(role, 9), os.path.basename(path))


def detail_mark_of(filename):
    """文件名里的详情图标记('详情页'/'详情图'/'详情'/'长图'), 没有返回 ''。"""
    stem = os.path.splitext(filename)[0]
    for mark in DETAIL_MARKS:
        index = stem.find(mark)
        if index > 0:                      # 必须在编码之后出现(纯"详情.jpg"没有编码, 不认)
            return mark
    return ""


def detail_code_of_filename(filename):
    """详情图文件名 -> 编码段。'HZE118详情页_01.jpg' -> 'HZE118'; 'ZSTZ106JR-15详情页.jpg' -> 'ZSTZ106JR-15'"""
    mark = detail_mark_of(filename)
    if not mark:
        return ""
    return _clean(os.path.splitext(filename)[0].split(mark)[0])


def detail_slice_index(filename):
    """分片序号: 'HZE118详情页_02.jpg' -> 2; 整图 'HZE118详情页.jpg' -> None。"""
    stem = os.path.splitext(filename)[0]
    match = _DETAIL_INDEX.search(stem)
    return int(match.group(1)) if match else None


def is_stitched_long_image(path):
    """是否是「整页拼接长图」（内容与同目录分段图重复，发出去详情里会出现两遍）。

    两种判据任一命中即算：
      ① 文件名在 ``COMMON_DETAIL_SKIP_STEMS`` 里（已确认的那张：详情通栏_02.jpg）；
      ② 高 ≥ 宽 × ``COMMON_DETAIL_LONG_ASPECT``（靠图片头判断尺寸，不解码整张）。
    读不到尺寸时按"不是长图"处理 —— 宁可多发一张，也不要误删运营的图。
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    if stem in COMMON_DETAIL_SKIP_STEMS:
        return True
    try:
        from PIL import Image
        with Image.open(path) as image:
            width, height = image.size
    except Exception:  # noqa: BLE001
        return False
    return width > 0 and height >= width * COMMON_DETAIL_LONG_ASPECT


def pick_detail_images(paths):
    """从同一商品的详情图候选里挑出"一套"。

    实测命名是「整张长图 + 切成若干分片」同时在(HZE118详情页.jpg 与 images/HZE118详情页_01..05.jpg),
    两者内容重复: 直接用会把同一段详情放两遍。规则 = 有分片就用分片(按序号排序), 否则用整图。
    多个不同前缀(可能按颜色各一套)时各出一套, 顺序按文件名稳定排序。
    """
    groups = {}
    for path in paths or []:
        name = os.path.basename(path)
        stem = os.path.splitext(name)[0]
        match = _DETAIL_INDEX.search(stem)
        base = stem[: match.start()] if match else stem
        groups.setdefault(base, []).append((detail_slice_index(name), name, path))
    picked = []
    for base in sorted(groups):
        items = sorted(groups[base], key=lambda one: (one[0] is None, one[0] or 0, one[1]))
        slices = [one for one in items if one[0] is not None]
        picked.extend(one[2] for one in (slices if len(slices) >= 2 else items))
    return picked


def _walk_images(folder, skip_dir_names, nested_product_dirs=None):
    """遍历一个商品文件夹下的所有图片(跳过尺寸目录/尺寸文件/非图片)。

    nested_product_dirs: 本文件夹内部「自身也是商品文件夹」的子目录(规范化路径)。
    这些子目录的图由它们自己那一轮收集, 这里不再下钻, 否则同一张图会被统计两遍。
    """
    nested = nested_product_dirs or frozenset()
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [
            d for d in dirnames
            if d not in skip_dir_names
            and os.path.normcase(os.path.join(dirpath, d)) not in nested
        ]
        for filename in filenames:
            if os.path.splitext(filename)[1].lower() not in IMAGE_EXTENSIONS:
                continue
            if any(marker in filename for marker in SKIP_FILENAME_MARKERS):
                continue
            yield dirpath, filename


def _direct_images(folder, skip_dir_names):
    """该目录下「直接」放着的图片(不递归子目录), 只用来判断"这个目录本身是不是商品文件夹"。"""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return False
    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        if os.path.splitext(entry.name)[1].lower() not in IMAGE_EXTENSIONS:
            continue
        if any(marker in entry.name for marker in SKIP_FILENAME_MARKERS):
            continue
        return True
    return False


def _child_dirs(folder, skip_dir_names):
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name)
    except OSError:
        return []
    dirs = []
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if entry.name in skip_dir_names or entry.name in NON_PRODUCT_NAMES:
            continue
        dirs.append(entry.path)
    return dirs


def _iter_product_folders(month_path, skip_dir_names):
    """在月份目录下找出所有「商品文件夹」, 支持中间夹任意层分组目录。

    运营的目录并不总是「月份/商品文件夹」两层, 常见还夹着系列/批次/补充目录:
        1-3月/！永恒之环/FOREVER·永恒系列/KGN1000330/
        1-3月/！永恒之环/TND0163/
        1-3月/第四批/6款耳钉/ZSED238 椭/
        4-6月份/6月补充/LGPD0930/
    旧实现固定只取月份目录的直接子目录当商品文件夹, 于是上面这些把「！永恒之环」
    当成了商品(编码也是垃圾值), 真正的商品图因为不在那一层, 永远挂不上商品主图。

    判定规则: 递归所有目录, 凡「直接含图片」的目录都算商品文件夹, 只有一种例外 ——
    「与最近一个含图祖先算出同一个商品编码」的子目录, 那是多颜色商品的规格子夹
    (本色图放父目录, 其余色放子目录且子目录名=SKU 编码, 如 ZNJ1725W-50 三色/ZNJ1725R-50),
    它只能作 SKU 图来源, 不能把别的颜色的图混进商品主图。

    注意不能"遇到含图目录就停止下钻": 分组目录自己也常散落着图
    (实测: FOREVER·永恒系列/系列详情图1.jpg、6月补充/ZSSL305SLR...jpg、
     过火IP款/宣传主图1.jpg、RDX2067 情侣款各三色/RDX2067主图1.jpg),
    一停就会漏掉它们下面的真正商品文件夹。分组目录算出来的编码是垃圾值,
    匹配不到任何商品, 留着无害。
    """
    # 栈元素: (目录, 最近一个含图祖先的商品编码)
    stack = [(month_path, None)]
    while stack:
        current, ancestor_key = stack.pop()
        key = product_code_of(code_of_dirname(os.path.basename(current))) or code_of_dirname(os.path.basename(current))
        if _direct_images(current, skip_dir_names) and key and key != ancestor_key:
            yield current
            ancestor_key = key
        for child in _child_dirs(current, skip_dir_names):
            stack.append((child, ancestor_key))


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
    result = {"sku_images": {}, "root_images": {}, "detail_images": {}, "common_images": [],
              "common_skipped_long": [], "unmatched": [], "scanned": 0, "folders": 0}

    for month in sorted(os.scandir(root), key=lambda e: e.name):
        if not month.is_dir() or month.name in NON_PRODUCT_NAMES:
            continue
        # 通用详情图目录: 不受月份过滤影响, 所有商品共用
        if month.name in COMMON_DETAIL_NAMES:
            for dirpath, filename in _walk_images(month.path, skip_dir_names):
                full = os.path.join(dirpath, filename)
                # 拼接长图与分段图内容重复, 跳过(见 is_stitched_long_image 的实测说明)
                if is_stitched_long_image(full):
                    result["common_skipped_long"].append(full)
                    continue
                result["common_images"].append(full)
            continue
        if months and month.name not in months:
            continue

        product_folders = list(_iter_product_folders(month.path, skip_dir_names))
        nested_product_dirs = frozenset(os.path.normcase(p) for p in product_folders)
        for product_dir in product_folders:
            result["folders"] += 1
            folder_code = code_of_dirname(os.path.basename(product_dir))
            for dirpath, filename in _walk_images(product_dir, skip_dir_names, nested_product_dirs):
                full = os.path.join(dirpath, filename)
                result["scanned"] += 1
                # 商品自有详情图(详情页/详情图/长图): 单独归类, 直接跳到下一张 ——
                # 它们既不是主图也不是 SKU 图, 若混进去会顶掉主图(主图不足 5 张时尤其明显),
                # 还会把 SKU 图匹配引到一张没有「主图2」的候选上。
                detail_code = detail_code_of_filename(filename)
                if detail_code:
                    key = product_code_of(detail_code) or detail_code
                    result["detail_images"].setdefault(key, []).append(full)
                    continue
                at_root = os.path.normcase(dirpath) == os.path.normcase(product_dir)
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


# 编码里只保留 ASCII 字母数字与连字符: 中文后缀(粉钻/圆钻/爱心钻/女款/葫芦…)不参与比较
_CODE_CHARS = re.compile(r"[A-Za-z0-9\-]*")


def _split_code(code):
    """拆解编码 -> (款号主干, 附加段, 分数)。

    'ZSSL307SLY-B-10'  -> ('ZSSL307SLY', ('B',), '10')     B=链型附加段
    'ZSTZ106EW-10-D1'  -> ('ZSTZ106EW', ('D1',), '10')     D1=尾部附加段
    'ZDR01122W-150粉钻' -> ('ZDR01122W', (), '150')         中文后缀丢掉
    'C065XAM00021R'    -> ('C065XAM00021R', (), '')         无分数
    """
    text = _CODE_CHARS.match(_clean(code)).group(0).rstrip("-")
    parts = [part for part in text.split("-") if part]
    if not parts:
        return "", (), ""
    head, middles, score = parts[0], [], ""
    for part in parts[1:]:
        if part.isdigit() and not score:
            score = part
        else:
            middles.append(part)
    return head, tuple(middles), score


def _head_distance(head, other):
    """款号主干比较: 0=相同; 1=只差一个字母(颜色字母写法不一致); None=不是同一款。

    只容许"一方比另一方多一个字母"(如 SKU ZDR01122-150 的图叫 ZDR01122W-150粉钻),
    不容许"等长但末位字母不同" —— 那是不同颜色(如 ZSTZ106XLR vs ZSTZ106XLW),
    图各不一样, 混用会把别的颜色的图挂上去。
    """
    if head == other:
        return 0
    if len(head) == len(other) + 1 and head.startswith(other):
        return 1
    if len(other) == len(head) + 1 and other.startswith(head):
        return 1
    return None


def _color_segments(middles):
    """附加段里的颜色段: 单个 W/Y/R/S 字母。

    颜色不同 = 图不一样, 必须完全一致 —— 否则会把别的颜色的图挂上去
    (实测: SKU APYE0139-Y-30-D1 曾被匹到 APYE0139-W-30-D1)。
    注意链型/绳色之类的非颜色附加段(-A/-B/-D1)不在此列, 允许写法差异。
    """
    return tuple(part for part in middles if len(part) == 1 and part in COLOR_LETTERS)


def match_sku_code(sku_code, sku_images):
    """把一个 SKU 编码匹配到扫描到的图片编码。

    硬性前置: 颜色段(W/Y/R/S)必须与 SKU 完全一致, 绝不用别的颜色的图充数。
    因为每个 SKU 都要求用它自己的「主图2」(运营约定), 所以:
      - 「有主图2」的候选优先(精确命中但只有主图PNG/白底时, 会继续往后找);
      - 同档位下优先款号主干更精确的候选。

    档位由高到低(sku_images 的 value 是 {"all":[...], "main1":..., "main2":...}):
      6 完全相同
      5 主干/附加段/分数都相同, 只是中文后缀等写法不同
        (ZSSL308SLR-A-10 ↔ ZSSL308SLR-A-10爱心钻; ED260301R-010-D1 ↔ ED260301R-010-D)
      4 主干/附加段相同, 图库那条没写分数(原规则②: 去掉尾部 -数字 后命中)
        (C065XAM00021R-30 ↔ C065XAM00021R; ZSTZ105SLY-A-10 ↔ ZSTZ105SLY-A)
      3 主干/附加段相同, 只有分数不同 —— 同色不同分数共用一套图
        (ZSTZ106EW-10-D1 ↔ ZSTZ106EW-15; 图库常只拍其中一个分数)
      2 主干相同、分数相同, 只有附加段写法不同
        (ZSSL307SLY-B-10 ↔ ZSSL307SLY-A-10; ZSSL309SLR-A-50 ↔ ZSSL309SLR-50葫芦)
      1 主干只差一个字母(颜色字母写法不一致), 分数相同
        (ZDR01122-150 ↔ ZDR01122W-150粉钻; FWR0085-100 ↔ FWR0085R-100)
      0 其它主干相同/相近的写法
     -1 前缀落在 '-' 边界上(兜底, 防止 10 误配 100)
    返回命中的图片编码, 没有则 None。
    """
    sku = _clean(sku_code)
    if not sku:
        return None
    head, middles, score = _split_code(sku)
    if not head:
        return None
    colors = _color_segments(middles)

    best = None      # (排序键, 编码)

    def consider(code, tier):
        nonlocal best
        if not code:
            return
        has_main2 = 1 if (sku_images.get(code) or {}).get("main2") else 0
        # 同档位下取更长的编码; 并列时先遇到的优先(与原实现保持稳定)
        key = (has_main2, tier, -len(code))
        if best is None or key > best[0]:
            best = (key, code)

    for code in sku_images:
        if not code:
            continue
        if code == sku:
            consider(code, 6)
            continue
        other_head, other_middles, other_score = _split_code(code)
        distance = _head_distance(head, other_head)
        if distance is None:
            continue
        if _color_segments(other_middles) != colors:
            continue                       # 颜色段不一致: 这不是同一个颜色的图, 宁缺勿滥
        if distance == 0 and other_middles == middles:
            if other_score == score:
                consider(code, 5)
            elif not other_score:
                consider(code, 4)
            else:
                consider(code, 3)
        elif other_score == score:
            consider(code, 2 if distance == 0 else 1)
        else:
            consider(code, 0)

    # 兜底: 原前缀边界匹配(处理拆解不出来的写法)
    for code in sku_images:
        if code and (_boundary_match(sku, code) or _boundary_match(code, sku)):
            consider(code, -1)

    return best[1] if best else None


def build_report(scan_result):
    """输出可读的扫描统计, 便于运营核对。"""
    lines = []
    lines.append("商品文件夹 %d 个, 扫描图片 %d 张" % (scan_result["folders"], scan_result["scanned"]))
    lines.append("识别出 SKU 编码 %d 个, 其中有「主图2」的 %d 个" % (
        len(scan_result["sku_images"]),
        sum(1 for v in scan_result["sku_images"].values() if v.get("main2"))))
    lines.append("商品主图(按商品编码): %d 个商品" % len(scan_result["root_images"]))
    own_detail = scan_result.get("detail_images") or {}
    lines.append("商品自有详情图: %d 张 / %d 个商品" % (sum(len(v) for v in own_detail.values()), len(own_detail)))
    lines.append("通用详情图: %d 张" % len(scan_result["common_images"]))
    skipped_long = scan_result.get("common_skipped_long") or []
    if skipped_long:
        lines.append("跳过拼接长图(与分段重复): %d 张 %s" % (
            len(skipped_long), [os.path.basename(one) for one in skipped_long]))
    lines.append("未识别编码的图片: %d 张" % len(scan_result["unmatched"]))
    return "\n".join(lines)
