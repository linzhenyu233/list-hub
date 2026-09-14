# -*- coding: utf-8 -*-
"""批量商品中台首期服务：Excel 导入、校验、批次查询与模板下载。

本服务刻意独立于微信/小红书服务。当前环境没有 MySQL 驱动和 multipart，
因此上传接口采用 JSON + Base64，存储使用 SQLite；后续可替换存储层而不改前端接口。
"""
import base64
import csv
import io
import hashlib
import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
import urllib.error
import uuid
import zipfile
import time
import sys
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime_config import load_project_env

load_project_env()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get("BULK_DB_FILE", os.path.join(BASE_DIR, "bulk_catalog.sqlite3"))
IMAGE_DIR = os.environ.get("BULK_IMAGE_DIR", os.path.join(BASE_DIR, "../images"))
BULK_API_HOST = os.environ.get("BULK_API_HOST", "0.0.0.0")
BULK_API_PORT = int(os.environ.get("BULK_API_PORT", "8020"))
os.makedirs(IMAGE_DIR, exist_ok=True)

# 图片根目录默认值(网络共享, 服务端可直读):图片直读扫描的默认根目录
DEFAULT_IMAGE_ROOT = r"\\192.168.10.250\电子商务部\网销部共享\SHINING HOUSE培育钻"
# 允许按引用读取图片的根目录白名单:
# 防止有人构造 disk:/local: 引用(或直接传 root 参数)去读服务器上的任意文件
ALLOWED_IMAGE_ROOTS = [DEFAULT_IMAGE_ROOT, IMAGE_DIR]


def _within_allowed_roots(path):
    """判断绝对路径是否落在 ALLOWED_IMAGE_ROOTS 内。

    用 commonpath 而不是 startswith:后者会让 '...\\images_evil\\x.jpg' 命中
    白名单根 '...\\images' 的前缀,导致越权读取。
    """
    target = os.path.abspath(str(path or ""))
    for root in ALLOWED_IMAGE_ROOTS:
        root_abs = os.path.abspath(root)
        try:
            if os.path.commonpath((root_abs, target)) == root_abs:
                return True
        except ValueError:
            # 不同盘符 / 本地盘与 UNC 混用时 commonpath 会报错,视为不在白名单内
            continue
    return False


app = FastAPI(title="商品批量发布中台", version="0.1.0")
_WECHAT_CATEGORY_CHAIN_CACHE = {}
_XHS_VAR_CANDIDATES_CACHE = {}

# 小红书 SKU 规格值别名:货盘英文 SKU 编码 → 平台中文 valueName(发品时映射用)
# 小红书「颜色分类」等规格的 valueName 是中文,货盘常用英文编码,直接发英文会显示英文
XHS_SPEC_VALUE_ALIASES = {
    '粉红色': ['SHINING PINK', '粉色', '粉红', 'PINK', 'Rose'],
    '浅蓝色': ['ICE BLUE', '浅蓝', 'LIGHT BLUE'],
    '香槟金色': ['CHAMPAGNE GOLD', '香槟金', 'CHAMPAGNE'],
    '玫瑰金色': ['ROSE GOLD', '玫瑰金'],
    '黄金色': ['YELLOW GOLD', '黄金', 'GOLD'],
    '18K金色': ['18K GOLD', '18K金'],
    '白色': ['WHITE'],
    '黑色': ['BLACK'],
    '无色': ['COLORLESS', '透明'],
    '玫红色': ['ROSE RED', '玫红'],
    '红色': ['RED'],
    '绿色': ['GREEN'],
    '黄色': ['YELLOW'],
    '蓝色': ['BLUE'],
    '紫色': ['PURPLE', 'VIOLET'],
    '灰色': ['GRAY', 'GREY'],
    '银色': ['SILVER'],
    # 其它颜色/规格值按需补充
}


def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL 模式:读写互不阻塞,大幅降低 database is locked
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS batches (
        id TEXT PRIMARY KEY, filename TEXT, status TEXT, total INTEGER DEFAULT 0,
        valid INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, created_at TEXT,
        rows_json TEXT NOT NULL, mappings_json TEXT NOT NULL DEFAULT '{}'
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS category_aliases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        internal_category TEXT UNIQUE NOT NULL,
        wechat_json TEXT, xhs_json TEXT, updated_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS publish_jobs (
        id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, platforms_json TEXT NOT NULL,
        status TEXT NOT NULL, total INTEGER DEFAULT 0, processed INTEGER DEFAULT 0,
        success INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, created_at TEXT,
        updated_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS publish_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, batch_id TEXT NOT NULL,
        product_code TEXT NOT NULL, platform TEXT NOT NULL, status TEXT NOT NULL,
        platform_product_id TEXT, platform_sku_ids TEXT, error TEXT,
        started_at TEXT, finished_at TEXT,
        UNIQUE(job_id, product_code, platform)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_publish_items_batch_product_platform ON publish_items(batch_id, product_code, platform)")
    conn.execute("""CREATE TABLE IF NOT EXISTS image_upload_cache (
        platform TEXT NOT NULL, source_hash TEXT NOT NULL, source_url TEXT NOT NULL,
        platform_url TEXT NOT NULL, updated_at TEXT, PRIMARY KEY(platform, source_hash)
    )""")
    conn.commit()
    return conn


def _json_post(url, payload, timeout=180):
    request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except Exception:
            detail = None
        raise RuntimeError(str(detail or exc)[:1000]) from exc
    except Exception as exc:
        raise RuntimeError(str(exc)[:500]) from exc
    return data


def _wechat_payload(product, mapping):
    chain = _latest_wechat_chain(mapping.get("wechat_category_chain") or [])
    if not chain:
        raise RuntimeError("未配置微信类目")
    attrs = mapping.get("wechat_attrs") or product.get("attributes") or {}
    attr_list = [{"attr_key": str(k), "attr_value": str(v)} for k, v in attrs.items() if v not in (None, "", [])]
    freight_id = mapping.get("wechat_freight_template_id") or mapping.get("freight_template_id")
    if not freight_id:
        raise RuntimeError("未配置微信运费模板")
    if not product.get("main_images"):
        raise RuntimeError("微信商品至少需要一张主图")
    item = {
        "title": mapping.get("wechat_title") or product.get("title", ""),
        "out_product_id": product.get("product_code"),
        "head_imgs": product.get("main_images") or [],
        "cats_v2": [{"cat_id": n.get("cat_id") or n.get("id")} for n in chain],
        "brand_id": str(mapping.get("wechat_brand_id") or "2100000000"),
        "express_info": {"template_id": str(freight_id)},
        "attrs": attr_list,
        "deliver_method": 0,
        "extra_service": {"seven_day_return": 1, "freight_insurance": 0},
    }
    if product.get("description") or product.get("detail_images"):
        item["desc_info"] = {}
        if product.get("description"): item["desc_info"]["detail"] = product["description"]
        if product.get("detail_images"): item["desc_info"]["imgs"] = product["detail_images"]
    skus = []
    for sku in product.get("skus", []):
        attrs_sku = [{"attr_key": s.get("name", ""), "attr_value": s.get("value", "")} for s in sku.get("specs", []) if s.get("name") and s.get("value")]
        item_sku = {"out_sku_id": sku.get("sku_code"), "sale_price": round(float(sku.get("price") or 0) * 100),
                    "market_price": round(float(sku.get("original_price") or sku.get("price") or 0) * 100),
                    "stock_num": int(float(sku.get("stock") or 0)), "sku_attrs": attrs_sku}
        # SKU 缩略图: 没匹配到图就不传, 避免空串被判非法
        if sku.get("uploaded_sku_image"):
            item_sku["thumb_img"] = sku["uploaded_sku_image"]
        skus.append(item_sku)
    item["skus"] = skus
    return item


def _latest_wechat_chain(saved_chain):
    if not saved_chain:
        return []
    leaf = saved_chain[-1]
    leaf_id = str(leaf.get("cat_id") or leaf.get("id") or "")
    leaf_name = str(leaf.get("name") or "").strip()
    if not leaf_name:
        raise RuntimeError("微信类目映射缺少叶子类目名称，请重新确认类目")
    cache_key = (leaf_id, leaf_name)
    if cache_key in _WECHAT_CATEGORY_CHAIN_CACHE:
        return _WECHAT_CATEGORY_CHAIN_CACHE[cache_key]
    try:
        data = _http_get_json(f"{WECHAT_API_BASE}/categories?keyword={urllib.parse.quote(leaf_name)}", timeout=180)
    except Exception as exc:
        raise RuntimeError(f"无法校验最新微信 cats_v2 类目: {exc}") from exc
    candidates = [item for item in data.get("results", []) if item.get("leaf")]
    matched = [item for item in candidates if str(item.get("cat_id")) == leaf_id]
    if len(matched) != 1:
        saved_names = [str(node.get("name") or "").strip() for node in saved_chain]
        matched = [item for item in candidates if [str(node.get("name") or "").strip() for node in item.get("chain", [])] == saved_names]
    if len(matched) != 1:
        raise RuntimeError(f"微信叶子类目 {leaf_name} 无法在最新 cats_v2 中唯一确认，请重新选择类目")
    chain = matched[0].get("chain") or []
    _WECHAT_CATEGORY_CHAIN_CACHE[cache_key] = chain
    return chain


def _local_image(source):
    """Resolve an importer-owned local:// reference without allowing path traversal."""
    if not source.startswith("local://"):
        return None
    relative = source[len("local://"):].replace("\\", "/")
    parts = relative.split("/")
    if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{32}", parts[0]) or not re.fullmatch(r"[0-9a-f]{64}\.[a-z0-9]{2,5}", parts[1]):
        raise RuntimeError("Excel 图片引用无效")
    path = os.path.abspath(os.path.join(IMAGE_DIR, parts[0], parts[1]))
    root = os.path.abspath(IMAGE_DIR)
    if os.path.commonpath((root, path)) != root or not os.path.isfile(path):
        raise RuntimeError("Excel 内嵌图片文件不存在")
    with open(path, "rb") as image_file:
        content = image_file.read()
    if not content:
        raise RuntimeError("Excel 内嵌图片内容为空")
    return path, content


def _platform_image(platform, source_url):
    source_url = str(source_url or "").strip()
    if not source_url: return ""
    local_image = _local_image(source_url)
    if not local_image and is_share_image(source_url):
        # 共享盘图片懒拷贝: 只有真正发布到平台时才读字节(扫描阶段零磁盘)
        local_image = fetch_share_image(source_url)
    source_hash = hashlib.sha256(local_image[1] if local_image else source_url.encode("utf-8")).hexdigest()
    conn = db(); cached = conn.execute("SELECT platform_url FROM image_upload_cache WHERE platform=? AND source_hash=?", (platform, source_hash)).fetchone(); conn.close()
    if cached: return cached["platform_url"]
    if local_image:
        payload = {"filename": os.path.basename(local_image[0]),
                   "content_base64": base64.b64encode(local_image[1]).decode("ascii")}
        endpoint = "/images/upload-file" if platform == "wechat" else "/materials/upload-file"
        response = _json_post(f"{WECHAT_API_BASE if platform == 'wechat' else XHS_API_BASE}{endpoint}", payload)
    elif platform == "wechat":
        response = _json_post(f"{WECHAT_API_BASE}/images/upload", {"img_url": source_url})
    else:
        response = _json_post(f"{XHS_API_BASE}/materials/upload", {"url": source_url})
    if response.get("ok") is False: raise RuntimeError(str(response.get("detail") or response)[:500])
    result = response.get("result")
    if isinstance(result, str): platform_url = result
    else: platform_url = (result or {}).get("url") or (result or {}).get("materialUrl") or (result or {}).get("fileUrl") or (result or {}).get("img_url")
    if not platform_url: raise RuntimeError(f"{platform} 图片上传未返回地址")
    conn = db(); conn.execute("INSERT OR REPLACE INTO image_upload_cache(platform,source_hash,source_url,platform_url,updated_at) VALUES(?,?,?,?,?)", (platform, source_hash, source_url, platform_url, now())); conn.commit(); conn.close()
    return platform_url


def _upload_images_parallel(platform: str, urls: list, max_workers: int = 5) -> list:
    """并发上传图片并保序返回(替代原来的串行列表推导)。

    - 命中 image_upload_cache 的图片直接返回,不产生网络请求
    - 单张失败时抛出原始异常(与串行语义一致),发布项会标记 failed、可重试
    - max_workers 默认 5(平台限流风险可控)
    """
    from concurrent.futures import ThreadPoolExecutor
    targets = [str(u or "").strip() for u in (urls or [])]
    targets = [u for u in targets if u]
    if not targets:
        return []
    if len(targets) == 1:
        return [_platform_image(platform, targets[0])]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as pool:
        # map 保序;任一任务抛异常会在迭代到该位置时重新抛出
        return list(pool.map(lambda u: _platform_image(platform, u), targets))


def _upload_image_uncached(platform, source_url):
    """绕开 image_upload_cache 直接上传, 拿一个全新的平台素材地址。

    背景(实测): 小红书同一张图重复上传会返回**不同的**素材 URL, 而同一商品内多个 SKU
    共用同一个素材地址时, 小红书后台的「规格图」列显示为空; 图各不相同的商品则正常显示。
    所以只有"撞图"的 SKU 需要走这里补传一次, 没撞图的继续走缓存, 不做无谓重复上传。
    """
    local_image = _local_image(source_url)
    if not local_image and is_share_image(source_url):
        local_image = fetch_share_image(source_url)
    if not local_image:
        raise RuntimeError(f"无法读取图片: {str(source_url)[:120]}")
    payload = {"filename": os.path.basename(local_image[0]),
               "content_base64": base64.b64encode(local_image[1]).decode("ascii")}
    endpoint = "/images/upload-file" if platform == "wechat" else "/materials/upload-file"
    base = WECHAT_API_BASE if platform == "wechat" else XHS_API_BASE
    response = _json_post(f"{base}{endpoint}", payload)
    if response.get("ok") is False:
        raise RuntimeError(str(response.get("detail") or response)[:300])
    result = response.get("result")
    if isinstance(result, str):
        return result
    return ((result or {}).get("url") or (result or {}).get("materialUrl")
            or (result or {}).get("fileUrl") or (result or {}).get("img_url") or "")


def _upload_sku_images(platform, skus):
    """并发上传各 SKU 的规格图, 保序返回并保留空位。

    ⚠️ 不能直接用 _upload_images_parallel: 它会过滤空值导致返回列表变短,
       与 skus 按下标 zip 会串图(把 A 的图挂到 B 上)。
    ⚠️ 同一商品内多个 SKU 共用同一张图 → 同一个素材地址 → 小红书后台规格图列显示为空。
       所以发现撞图时, 给后面的 SKU 重新上传一次, 保证每个 SKU 地址唯一。
       (货盘图库每种颜色常只存一套图, 同色不同分数必然撞图, 这里就是处理这种情况。)
    """
    references = [str(sku.get("sku_image") or "").strip() for sku in skus]
    pending = [r for r in references if r]
    if not pending:
        return ["" for _ in references]
    queue = list(_upload_images_parallel(platform, pending))
    urls = [queue.pop(0) if r else "" for r in references]
    seen = {}
    for index, url in enumerate(urls):
        if not url:
            continue
        if url in seen:
            try:
                urls[index] = _upload_image_uncached(platform, references[index])
            except Exception as exc:
                print(f"[sku_image] 撞图重新上传失败, 保留原地址: {exc}")
        else:
            seen[url] = index
    return urls


def _prepare_platform_images(product, platform):
    prepared = dict(product)
    prepared["main_images"] = _upload_images_parallel(platform, product.get("main_images", []))
    prepared["detail_images"] = _upload_images_parallel(platform, product.get("detail_images", []))
    # SKU 规格图: 微信 → skus[].thumb_img, 小红书 → sku_list[].specImage
    skus = [dict(sku) for sku in product.get("skus", [])]
    for sku, url in zip(skus, _upload_sku_images(platform, skus)):
        if url:
            sku["uploaded_sku_image"] = url
    # 小红书开了「主规格图」后要求每个规格值都有图(否则报「启用规格大图需上传所有规格图」)。
    # 图库缺图的 SKU(如某颜色没拍图)用商品主图兜底, 保证每个颜色值都有图。
    fallback = (prepared.get("main_images") or [""])[0]
    if platform == "xhs" and fallback:
        for sku in skus:
            if not sku.get("uploaded_sku_image"):
                sku["uploaded_sku_image"] = fallback
    prepared["skus"] = skus
    return prepared


def _get_xhs_var_candidates(category_id, var_id):
    """拉小红书某个规格维度(如「颜色分类」)的候选值,带缓存(12h)。
    用于 SKU 规格值映射:货盘英文编码 → 平台中文 valueName + valueId。"""
    key = (str(category_id), str(var_id))
    hit = _XHS_VAR_CANDIDATES_CACHE.get(key)
    if hit and time.time() - hit["ts"] < 12 * 3600:
        return hit["data"]
    try:
        url = (f"{XHS_API_BASE}/attribute-values"
               f"?category_id={urllib.parse.quote(str(category_id))}"
               f"&attribute_id={urllib.parse.quote(str(var_id))}")
        data = _http_get_json(url, timeout=60)
        result = data.get("result") or {}
        cands = result.get("attributeValueV3s") or result.get("values") or []
        _XHS_VAR_CANDIDATES_CACHE[key] = {"ts": time.time(), "data": cands}
        return cands
    except Exception:
        return []


def _resolve_xhs_spec_value(source_value, var_id, category_id):
    """把货盘 SKU 规格值(可能英文)映射成小红书平台的中文 valueName + valueId。
    顺序:① 精确匹配候选值 ② 模糊匹配 ③ 别名表(英文→中文) ④ 原值兜底(不带 valueId)。"""
    sv = str(source_value).strip()
    cands = _get_xhs_var_candidates(category_id, var_id) or []
    hit = next((c for c in cands if c.get("valueName") == sv), None)
    if not hit:
        hit = next((c for c in cands if sv in (c.get("valueName") or '') or (c.get("valueName") or '') in sv), None)
    if hit:
        return hit.get("valueName", sv), hit.get("valueId", '')
    # 别名兜底:货盘英文 SKU 编码 → 平台中文 valueName
    for cn, aliases in XHS_SPEC_VALUE_ALIASES.items():
        if sv in aliases:
            vhit = next((c for c in cands if c.get("valueName") == cn), None)
            if vhit:
                return cn, vhit.get("valueId", '')
            return cn, ''
    return sv, ''


def _xhs_payload(product, mapping, group_config=None):
    category_id = mapping.get("xhs_category_id")
    if not category_id:
        raise RuntimeError("未配置小红书类目")
    if not mapping.get("xhs_brand_id"):
        raise RuntimeError("小红书品牌未唯一匹配")
    if not mapping.get("xhs_shipping_template_id"):
        raise RuntimeError("未选择小红书运费模板")
    if not mapping.get("xhs_logistics_plan_id"):
        raise RuntimeError("未选择小红书物流方案")
    if not product.get("main_images"):
        raise RuntimeError("小红书商品至少需要一张主图")
    item = {"name": mapping.get("xhs_title") or product.get("title", ""),
            "brandId": str(mapping.get("xhs_brand_id") or ""), "categoryId": str(category_id),
            "attributes": [], "shippingTemplateId": str(mapping.get("xhs_shipping_template_id") or ""),
            "shippingGrossWeight": int(float(product.get("weight") or 500)),
            "variantIds": [], "images": product.get("main_images") or [],
            "imageDescriptions": product.get("detail_images") or [],
            "description": product.get("description", ""), "deliveryMode": "0", "freeReturn": "1",
            "articleNo": (product.get("product_code") or "").split("-", 1)[0]}
    attrs = mapping.get("xhs_attrs") or {}
    for key, val in attrs.items():
        values = val if isinstance(val, list) else [val]
        for value_item in values:
            if isinstance(value_item, dict):
                item["attributes"].append(value_item)
            elif value_item not in (None, ""):
                item["attributes"].append({"propertyId": str(key), "valueId": str(value_item), "value": str(value_item)})
    xhs_config = (group_config or {}).get("xhs") or {}
    var_defs = xhs_config.get("var_defs") or []
    spec_map = xhs_config.get("spec_map") or {}
    candidates = xhs_config.get("candidates") or {}
    mapped_var_ids = [str(v.get("id")) for v in var_defs if spec_map.get(str(v.get("id"))) or spec_map.get(v.get("id"))]
    item["variantIds"] = mapped_var_ids
    # 「规格大图」开关:平台要求开启该开关的商品必须至少有一个主规格项(variantIds 非空),
    # 否则 createItemV2 直接报 -5000500「当前商品未包含主规格项,无法开启规格大图功能」。
    # 因此只在确实映射到规格维度时才带;没有维度(如 spec_map 为空)就不带,商品照常发布(只是没有规格图列)。
    # 注:该字段只能在创建时带,审核中的商品 updateItemV2 改不动它。
    if mapped_var_ids:
        item["enableMainSpecImage"] = True
    sku_list = []
    for sku in product.get("skus", []):
        variants = []
        specs_by_name = {s.get("name"): s.get("value") for s in sku.get("specs", [])}
        for var_def in var_defs:
            var_id = str(var_def.get("id"))
            source_name = spec_map.get(var_id) or spec_map.get(var_def.get("id"))
            source_value = specs_by_name.get(source_name)
            if not source_name or not source_value: continue
            # SKU 规格值:货盘英文编码 → 平台中文 valueName + valueId(小红书后台才能显示中文)
            value_name, value_id = _resolve_xhs_spec_value(source_value, var_id, category_id)
            variant = {"id": var_def.get("id"), "name": var_def.get("name"), "value": value_name}
            if value_id:
                variant["valueId"] = value_id
            variants.append(variant)
        item_sku = {"ipq": 1, "originalPrice": round(float(sku.get("original_price") or sku.get("price") or 0) * 100),
                    "price": round(float(sku.get("price") or 0) * 100), "stock": int(float(sku.get("stock") or 0)),
                    "logisticsPlanId": str(mapping.get("xhs_logistics_plan_id") or ""), "variants": variants,
                    "deliveryTime": {"time": "24", "type": "RELATIVE_TIME_NEW"}, "erpCode": sku.get("sku_code")}
        # SKU 规格图: 没匹配到图就不传
        if sku.get("uploaded_sku_image"):
            item_sku["specImage"] = sku["uploaded_sku_image"]
        sku_list.append(item_sku)
    return {"item": item, "sku_list": sku_list}


def _run_publish_item(row):
    # 用缓存版:避免每个发布项都重新 json.loads 全量 rows + mappings(800 商品时单次近百毫秒)
    batch = get_batch_cached(row["batch_id"])
    product = next((p for p in batch["products"] if p.get("product_code") == row["product_code"]), None)
    if not product: raise RuntimeError("商品不存在")
    mapping = batch["mappings"].get("products", {}).get(row["product_code"], {})
    group_config = batch["mappings"].get("attr_groups", {}).get(product.get("internal_category"), {})
    if row["platform"] == "wechat":
        product = _prepare_platform_images(product, "wechat")
        result = _json_post(f"{WECHAT_API_BASE}/products", _wechat_payload(product, mapping))
        if result.get("ok") is False: raise RuntimeError(str(result.get("detail") or result)[:500])
        value = result.get("result")
        return {"product_id": value} if not isinstance(value, dict) else value
    if row["platform"] == "xhs":
        product = _prepare_platform_images(product, "xhs")
        result = _json_post(f"{XHS_API_BASE}/items/and-sku", _xhs_payload(product, mapping, group_config))
        value = result.get("result") or {}
        if value.get("itemId") and (result.get("partial") or value.get("skuErrors")):
            value["partial_error"] = json.dumps(value.get("skuErrors") or [], ensure_ascii=False)
            return value
        if result.get("ok") is False: raise RuntimeError(str(result.get("detail") or result)[:500])
        return value
    raise RuntimeError(f"不支持的平台: {row['platform']}")


def recover_interrupted_items():
    conn = db()
    conn.execute("UPDATE publish_items SET status='queued' WHERE status='running'")
    conn.commit(); conn.close()


def _platform_ready(platform):
    base_url = WECHAT_API_BASE if platform == "wechat" else XHS_API_BASE
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def _worker_loop(platform=None):
    while True:
        conn = None
        try:
            if platform and not _platform_ready(platform):
                time.sleep(5)
                continue
            conn = db()
            conn.execute("BEGIN IMMEDIATE")
            if platform:
                item = conn.execute("SELECT * FROM publish_items WHERE status='queued' AND platform=? ORDER BY id LIMIT 1", (platform,)).fetchone()
            else:
                item = conn.execute("SELECT * FROM publish_items WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not item:
                conn.commit(); conn.close()
                time.sleep(1)
                continue
            conn.execute("UPDATE publish_items SET status='running', started_at=? WHERE id=?", (now(), item["id"])); conn.commit(); conn.close()
            try:
                result = _run_publish_item(item)
                product_id = result.get("product_id") or result.get("itemId") or result.get("id")
                sku_ids = result.get("skuIds") or []
                status = "partial" if result.get("partial_error") else "success"
                conn = db(); conn.execute("UPDATE publish_items SET status=?, platform_product_id=?, platform_sku_ids=?, error=?, finished_at=? WHERE id=?", (status, str(product_id or ""), json.dumps(sku_ids), result.get("partial_error"), now(), item["id"])); conn.commit(); conn.close()
            except Exception as exc:
                conn = db(); conn.execute("UPDATE publish_items SET status='failed', error=?, finished_at=? WHERE id=?", (str(exc)[:1000], now(), item["id"])); conn.commit(); conn.close()
            conn = db(); conn.execute("""UPDATE publish_jobs SET processed=(SELECT COUNT(*) FROM publish_items WHERE job_id=? AND status IN ('success','failed','partial')), success=(SELECT COUNT(*) FROM publish_items WHERE job_id=? AND status='success'), failed=(SELECT COUNT(*) FROM publish_items WHERE job_id=? AND status IN ('failed','partial')), status=CASE WHEN (SELECT COUNT(*) FROM publish_items WHERE job_id=? AND status IN ('queued','running'))=0 THEN CASE WHEN (SELECT COUNT(*) FROM publish_items WHERE job_id=? AND status IN ('failed','partial'))>0 THEN 'completed_with_errors' ELSE 'completed' END ELSE 'running' END, updated_at=? WHERE id=?""", (item["job_id"], item["job_id"], item["job_id"], item["job_id"], item["job_id"], now(), item["job_id"])); conn.commit(); conn.close()
        except Exception:
            # 异常时必须回滚并关闭连接,否则 BEGIN IMMEDIATE 持有的写锁会一直不释放,锁死整个库
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            time.sleep(2)


class ImportBody(BaseModel):
    filename: str = "products.xlsx"
    content_base64: str
    zip_base64: str | None = None
    folder_files: list[dict] | None = None
    common_detail_files: list[dict] | None = None


class HuopaiImportBody(BaseModel):
    """货盘直读导入:读服务器本地货盘表,后台转换后入库(不走文件上传)。

    路径收敛: 只接受「货盘目录(HUOPAI_DIR,默认取 HUOPAI_PATH 所在目录)」内的 .xlsx,
    目录由服务端配置决定,不接受客户端指定任意目录。
    """
    path: str | None = None        # 货盘目录内的 .xlsx 路径;不传则用默认货盘表


class ImageScanBody(BaseModel):
    """图片直读:扫描图片根目录, 把商品图/SKU 图挂到批次上(不走浏览器上传, 几百个商品也没压力)。"""
    root: str | None = None            # 必须是服务端白名单内的图片根目录;不传用默认路径
    months: list[str] | None = None    # 只扫指定月份目录(如 ["7-9月份"]);None = 全部
    dry_run: bool = False              # True=只匹配不拷贝(先看命中率)


class ItemsBody(BaseModel):
    items: list[dict]


class SkuImageBody(BaseModel):
    """补规格图: 运营在批次表里给缺图的 SKU 单张上传(JSON+Base64, 与本项目其它上传接口一致)。"""
    content_base64: str
    filename: str | None = None


class MappingsBody(BaseModel):
    mappings: dict


class PublishBody(BaseModel):
    platforms: list[str]


class RetryBody(BaseModel):
    item_ids: list[int] | None = None


class AliasBody(BaseModel):
    internal_category: str
    wechat: dict | None = None
    xhs: dict | None = None


class AliasImportBody(BaseModel):
    filename: str = "category-aliases.xlsx"
    content_base64: str


def now():
    return datetime.now(timezone.utc).isoformat()


def value(row, *names):
    for name in names:
        if name in row and row[name] not in (None, ""):
            return str(row[name]).strip()
    return ""


def _embedded_image_extension(image, content):
    image_format = str(getattr(image, "format", "") or "").lower()
    if image_format in ("jpeg", "jpg") or content.startswith(b"\xff\xd8"):
        return "jpg"
    if image_format == "png" or content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_format == "gif" or content.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if image_format == "webp" or (content.startswith(b"RIFF") and content[8:12] == b"WEBP"):
        return "webp"
    return "png"


def _extract_embedded_images(sheet, headers, batch_id):
    column_types = {}
    for index, header in enumerate(headers):
        if header in ("主图", "主图链接"):
            column_types[index] = "main_images"
        elif header in ("详情图", "详情图链接"):
            column_types[index] = "detail_images"
    extracted = {}
    batch_dir = os.path.join(IMAGE_DIR, batch_id)
    for image in getattr(sheet, "_images", []):
        anchor = getattr(image, "anchor", None)
        marker = getattr(anchor, "_from", None)
        if marker is None or marker.col not in column_types or marker.row < 1:
            continue
        try:
            content = image._data()
        except Exception as exc:
            raise HTTPException(400, f"Excel 第 {marker.row + 1} 行内嵌图片读取失败：{exc}")
        digest = hashlib.sha256(content).hexdigest()
        extension = _embedded_image_extension(image, content)
        os.makedirs(batch_dir, exist_ok=True)
        path = os.path.join(batch_dir, f"{digest}.{extension}")
        if not os.path.exists(path):
            with open(path, "wb") as image_file:
                image_file.write(content)
        reference = f"local://{batch_id}/{digest}.{extension}"
        values = extracted.setdefault(marker.row + 1, {}).setdefault(column_types[marker.col], [])
        if reference not in values:
            values.append(reference)
    return extracted


def parse_excel(raw: bytes, filename: str, batch_id=None):
    extension = os.path.splitext(filename or "")[1].lower()
    if extension == ".xls":
        raise HTTPException(400, "暂不支持旧版 .xls，请另存为 .xlsx 后再导入")
    if extension == ".csv":
        try:
            text = raw.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            return [normalize_row(row, index) for index, row in enumerate(reader, 2) if any(str(v or "").strip() for v in row.values())]
        except UnicodeDecodeError:
            raise HTTPException(400, "CSV 请保存为 UTF-8 编码后再导入")
        except Exception as exc:
            raise HTTPException(400, f"CSV 解析失败：{exc}")
    try:
        import openpyxl
        workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=False, data_only=True)
        sheet = workbook.active
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(x or "").strip() for x in rows[0]]
        embedded = _extract_embedded_images(sheet, headers, batch_id) if batch_id else {}
        result = []
        for index, cells in enumerate(rows[1:], 2):
            if not any(cell not in (None, "") for cell in cells):
                continue
            item = {headers[i]: cells[i] if i < len(cells) else "" for i in range(len(headers)) if headers[i]}
            normalized = normalize_row(item, index)
            for field, references in embedded.get(index, {}).items():
                normalized[field] = list(dict.fromkeys(normalized.get(field, []) + references))
            result.append(normalized)
        return result
    except Exception as exc:
        raise HTTPException(400, f"Excel 解析失败：{exc}")


def _parse_attributes(text):
    """解析商品属性列: "材质=18K金;钻石切工=Very good" → {"材质": "18K金", "钻石切工": "Very good"}"""
    attrs = {}
    if not text:
        return attrs
    for pair in re.split(r"[;；]", text):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip():
                attrs[k.strip()] = v.strip()
    return attrs


_CN_NUM = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}


def _parse_specs(row, max_specs=20):
    """动态读取 规格N名称/规格N值 列（N 从 1 递增），返回 [{"name":.., "value":..}]。
    表头支持阿拉伯数字与中文数字（规格1名称 / 规格一名称）；只收集填了名称或值的规格列，按列序排列。
    规格维度数量不写死，运营可自由增减 Excel 规格列。"""
    specs = []
    for n in range(1, max_specs + 1):
        cn = _CN_NUM.get(n, str(n))
        name = value(row, f"规格{n}名称", f"规格{cn}名称")
        val = value(row, f"规格{n}值", f"规格{cn}值")
        if name or val:
            specs.append({"name": name, "value": val})
    return specs


def normalize_row(row, line):
    sku_code = value(row, "SKU编码", "SKU 编码", "sku_code", "erpCode")
    product_code = value(row, "商品编码", "商品 编码", "product_code", "articleNo")
    price = value(row, "售价", "售价（元）", "price", "price_yuan")
    stock = value(row, "库存", "stock")
    errors = []
    if not product_code:
        errors.append("缺少商品编码")
    if not value(row, "标题", "商品标题", "name"):
        errors.append("缺少标题")
    if not sku_code:
        errors.append("缺少SKU编码")
    try:
        if float(price) <= 0:
            errors.append("售价必须大于0")
    except (TypeError, ValueError):
        errors.append("售价格式错误")
    try:
        if int(float(stock)) < 0:
            errors.append("库存不能小于0")
    except (TypeError, ValueError):
        errors.append("库存格式错误")
    return {
        "line": line, "product_code": product_code,
        "title": value(row, "标题", "商品标题", "name"),
        "wechat_title": value(row, "微信标题"), "xhs_title": value(row, "小红书标题"),
        "internal_category": value(row, "内部类目", "类目"),
        "brand": value(row, "品牌"), "description": value(row, "描述", "商品描述"),
        "attributes": _parse_attributes(value(row, "商品属性", "属性", "attributes")),
        "weight": value(row, "重量", "商品毛重"),
        "main_images": value(row, "主图", "主图链接").split(",") if value(row, "主图", "主图链接") else [],
        "detail_images": value(row, "详情图", "详情图链接").split(",") if value(row, "详情图", "详情图链接") else [],
        "sku_code": sku_code, "specs": _parse_specs(row),
        "original_price": value(row, "原价", "原价（元）"),
        "price": price, "stock": stock, "errors": errors,
    }


# 「图片还在共享盘/本地磁盘上」的引用前缀: 扫描阶段只记路径, 真正上传平台时才读字节落盘(懒拷贝)
SHARE_PREFIX = "disk:"


def is_share_image(source):
    """是否是共享盘图片引用(懒加载), 形如 'disk:\\\\192.168.10.250\\...\\a.jpg'。"""
    return str(source or "").startswith(SHARE_PREFIX)


def fetch_share_image(source):
    """读共享盘上的图片并落盘缓存, 返回 (本地路径, 内容)。

    只在真正发布时调用: 扫描 233 个商品只记路径(秒级、零磁盘),
    发到哪个商品才读哪个商品的图, 不发布的商品完全不占磁盘。
    落盘按内容 sha256 命名, 同一张图(通用详情图/同款多 SKU 共用图)只存一份。

    安全: disk: 引用会存进批次数据, 而批次数据可由客户端通过 PUT /import/{id}/items
    覆写, 因此这里必须再校验一次路径白名单, 否则可被用来读服务器任意文件并上传平台。
    """
    path = str(source)[len(SHARE_PREFIX):]
    if not path or not _within_allowed_roots(path) or not os.path.isfile(path):
        raise RuntimeError(f"共享盘图片不可用(不存在或不在允许的图片目录内)：{path}")
    with open(path, "rb") as image_file:
        content = image_file.read()
    if not content:
        raise RuntimeError(f"共享盘图片内容为空：{path}")
    digest = hashlib.sha256(content).hexdigest()
    extension = os.path.splitext(path)[1].lower() or ".jpg"
    target_dir = os.path.join(IMAGE_DIR, "_share")
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, f"{digest}{extension}")
    if not os.path.exists(target):
        with open(target, "wb") as image_file:
            image_file.write(content)
    return target, content


def _store_folder_images(files, batch_id, rows, common_detail_files=None):
    """Save browser-selected folder files and resolve Excel image filenames."""
    batch_dir = os.path.join(IMAGE_DIR, batch_id)
    os.makedirs(batch_dir, exist_ok=True)
    references = {}
    file_records = []
    common_details = []
    for item in [*(files or []), *(common_detail_files or [])]:
        relative = str(item.get("path") or item.get("name") or "").replace("\\", "/").strip()
        encoded = item.get("content_base64") or ""
        if not relative or not encoded:
            continue
        parts = [part for part in relative.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            continue
        name = os.path.basename(relative)
        try:
            content = base64.b64decode(encoded, validate=True)
        except Exception:
            continue
        if not name or not content or len(content) > 20 * 1024 * 1024:
            continue
        digest = hashlib.sha256(content).hexdigest()
        extension = os.path.splitext(name)[1].lower() or ".jpg"
        target = os.path.join(batch_dir, f"{digest}{extension}")
        if not os.path.exists(target):
            with open(target, "wb") as image_file:
                image_file.write(content)
        reference = f"local://{batch_id}/{digest}{extension}"
        references[relative.lower()] = reference
        references.setdefault(name.lower(), reference)
        file_records.append((name, reference))
        if item in (common_detail_files or []):
            common_details.append(reference)
    for row in rows:
        for field in ("main_images", "detail_images"):
            row[field] = [
                references.get(str(source or "").strip().replace("\\", "/").lower(),
                               references.get(os.path.basename(str(source or "").strip()).lower(), source))
                for source in row.get(field, [])
            ]
        code = str(row.get("product_code") or "").strip().lower()
        # 取基础商品编码(去掉 -系列 后缀)用于匹配文件名,这样 KGN1000330主图1.jpg 能匹配 KGN1000330-永恒
        base_code = code.split("-", 1)[0] if code else ""
        if base_code:
            matches = [(name, ref) for name, ref in file_records if os.path.splitext(name)[0].lower().startswith(base_code)]
            if not row.get("main_images"):
                main_candidates = [ref for name, ref in sorted(matches) if "主图" in name or "main" in name.lower()]
                if not main_candidates and matches:
                    # 没有「主图/main」字样时,取匹配到的第一张当主图
                    main_candidates = [sorted(matches)[0][1]]
                row["main_images"] = main_candidates
            if not row.get("detail_images"):
                detail_candidates = [ref for name, ref in sorted(matches) if "详情" in name or "detail" in name.lower()]
                if not detail_candidates and len(matches) > 1:
                    # 没有「详情/detail」字样且多张时,除主图外都当详情图
                    detail_candidates = [ref for name, ref in sorted(matches)[1:]]
                row["detail_images"] = detail_candidates
            for reference in common_details:
                if reference not in row["detail_images"]:
                    row["detail_images"].append(reference)
            # 兜底:商品没匹配到主图时,优先用本次批次里 1:1 且 ≥800 的图(KGN 这类),其次才用通用详情
            # 1:1 + ≥800 才能过小红书 createItemV2 的「图片像素不低于800x800 (1:1)」
            if not row.get("main_images"):
                fallback = None
                # 先从文件夹图片里挑 1:1 且 ≥800 的
                for _n, _r in file_records:
                    try:
                        from PIL import Image as _Img
                        import io as _io
                        _p = _local_image(_r)
                        if not _p: continue
                        with _Img.open(_io.BytesIO(_p[1])) as _im:
                            _w, _h = _im.size
                            if _w >= 800 and _h == _w:
                                fallback = _r
                                break
                    except Exception:
                        continue
                # 再从通用详情里挑 1:1 且 ≥800 的
                if not fallback:
                    for _r in common_details:
                        try:
                            _p = _local_image(_r)
                            if not _p: continue
                            with _Img.open(_io.BytesIO(_p[1])) as _im:
                                _w, _h = _im.size
                                if _w >= 800 and _h == _w:
                                    fallback = _r
                                    break
                        except Exception:
                            continue
                # 实在没有 1:1 的,才用第一张通用详情(大概率会被小红书拒,生产应避免)
                if not fallback and common_details:
                    fallback = common_details[0]
                if fallback:
                    row["main_images"] = [fallback]


def group_products(rows):
    """将标准模板的 SKU 行合并为商品，供预览和后续平台发布使用。"""
    grouped = {}
    for row in rows:
        code = row.get("product_code") or f"__line_{row.get('line')}"
        product = grouped.setdefault(code, {
            "product_code": code, "title": row.get("title", ""),
            "wechat_title": row.get("wechat_title", ""), "xhs_title": row.get("xhs_title", ""),
            "internal_category": row.get("internal_category", ""), "brand": row.get("brand", ""),
            "description": row.get("description", ""), "attributes": row.get("attributes", {}),
            "weight": row.get("weight", ""),
            "main_images": row.get("main_images", []), "detail_images": row.get("detail_images", []),
            "skus": [], "errors": [], "warnings": [], "lines": [],
        })
        product["lines"].append(row.get("line"))
        product["errors"].extend(row.get("errors", []))
        for image_field in ("main_images", "detail_images"):
            for image_source in row.get(image_field, []):
                if image_source and image_source not in product[image_field]:
                    product[image_field].append(image_source)
        # 逐行留存商品属性，用于后续一致性检测（商品属性是 SPU 级，只采用第一行）
        product.setdefault("_attr_variants", []).append(row.get("attributes") or {})
        sku_key = row.get("sku_code")
        if any(sku.get("sku_code") == sku_key for sku in product["skus"]):
            product["errors"].append("同一商品内 SKU 编码重复")
        # 兼容旧批次 rows_json（早期为 spec1_name/spec1_value/spec2_*），统一转成 specs 数组
        row_specs = row.get("specs")
        if row_specs is None:
            row_specs = []
            for i in (1, 2):
                nm = row.get(f"spec{i}_name", "")
                vv = row.get(f"spec{i}_value", "")
                if nm or vv:
                    row_specs.append({"name": nm, "value": vv})
        product["skus"].append({
            "sku_code": sku_key,
            "specs": [{"name": sp.get("name", ""), "value": sp.get("value", "")} for sp in row_specs],
            "original_price": row.get("original_price", ""),
            "price": row.get("price", ""), "stock": row.get("stock", ""),
            # SKU 规格图(图片直读扫描写入, 固定为该 SKU 的「主图2」)
            "sku_image": row.get("sku_image", ""),
        })
    products = list(grouped.values())
    for product in products:
        product["errors"] = list(dict.fromkeys(product["errors"]))
        # 商品属性在同一商品的各 SKU 行间不一致时告警：
        # 平台模型只支持 SPU 级属性（微信 attrs / 小红书 attributes），
        # 实际仅第一行的值生效，其余行静默丢弃，必须提醒运营。
        variants = product.pop("_attr_variants", [])
        if len(variants) > 1:
            keys = set()
            for variant in variants:
                keys.update(variant.keys())
            for key in sorted(keys):
                values = [str(variant.get(key, "")).strip() for variant in variants]
                distinct = list(dict.fromkeys([v for v in values if v]))
                if len(distinct) > 1:
                    product["warnings"].append(
                        f"商品属性「{key}」在 SKU 间不一致（{' / '.join(distinct)}），"
                        f"当前仅采用第一行的值；若该差异影响选购或定价，建议提升为规格维度"
                    )
        product["warnings"] = list(dict.fromkeys(product.get("warnings", [])))
        # 汇总规格维度（按名称去重、保持列序），并把每个 SKU 的 specs 对齐到统一维度，
        # 避免各 SKU 规格列数量不同导致前端按下标渲染错位。
        dimensions = []
        for sku in product["skus"]:
            for sp in sku.get("specs", []):
                if sp.get("name") and sp["name"] not in dimensions:
                    dimensions.append(sp["name"])
        for sku in product["skus"]:
            by_name = {sp.get("name"): sp.get("value", "") for sp in sku.get("specs", []) if sp.get("name")}
            sku["specs"] = [{"name": d, "value": by_name.get(d, "")} for d in dimensions]
        product["spec_dimensions"] = dimensions
        if len(product["skus"]) > 100:
            product["errors"].append("单商品最多支持100个 SKU")
        titles = {rows[line - 2].get("title") for line in product["lines"] if isinstance(line, int) and 1 < line <= len(rows) + 1}
        if len(titles) > 1:
            product["errors"].append("同一商品标题不一致")
        prices = [float(s["price"]) for s in product["skus"] if str(s.get("price", "")).replace(".", "", 1).isdigit()]
        stocks = [int(float(s["stock"])) for s in product["skus"] if str(s.get("stock", "")).replace(".", "", 1).isdigit()]
        product["sku_count"] = len(product["skus"])
        product["price_min"] = min(prices) if prices else None
        product["price_max"] = max(prices) if prices else None
        product["stock_total"] = sum(stocks) if stocks else None
    return products


@app.get("/health")
def health():
    return {"status": "ok", "service": "bulk-catalog-api"}


@app.get("/template")
def template():
    headers = ["商品编码", "标题", "微信标题", "小红书标题", "内部类目", "品牌", "描述", "商品属性", "重量", "主图", "详情图", "SKU编码", "规格1名称", "规格1值", "规格2名称", "规格2值", "规格3名称", "规格3值", "原价（元）", "售价（元）", "库存"]
    try:
        import openpyxl
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "商品SKU"
        sheet.append(headers)
        sheet.append(["SPU-001", "示例商品标题", "", "", "珠宝", "", "", "材质=18K金;颜色=白色", 500, "", "", "SKU-001", "颜色", "红色", "尺码", "均码", "净度", "VS1", 99.9, 88.0, 100])
        for cell in sheet[1]:
            cell.font = openpyxl.styles.Font(bold=True)
        sheet.freeze_panes = "A2"
        output = io.BytesIO()
        workbook.save(output)
        output.seek(0)
        return StreamingResponse(iter([output.read()]), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=bulk-product-template.xlsx"})
    except Exception as exc:
        raise HTTPException(500, f"生成 Excel 模板失败：{exc}")


@app.post("/import")
def import_batch(body: ImportBody):
    try:
        raw = base64.b64decode(body.content_base64)
    except Exception:
        raise HTTPException(400, "Excel 文件编码无效")
    batch_id = uuid.uuid4().hex
    rows = parse_excel(raw, body.filename, batch_id)
    if len(rows) > 5000:
        raise HTTPException(400, "单批最多导入5000行SKU")
    if body.zip_base64:
        try:
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(body.zip_base64))) as archive:
                for info in archive.infolist():
                    if info.is_dir() or info.filename.startswith(("/", "\\")) or ".." in info.filename:
                        continue
                    target = os.path.join(IMAGE_DIR, f"{uuid.uuid4().hex}_{os.path.basename(info.filename)}")
                    with open(target, "wb") as output:
                        output.write(archive.read(info))
        except Exception as exc:
            raise HTTPException(400, f"图片ZIP处理失败：{exc}")
    if body.folder_files or body.common_detail_files:
        _store_folder_images(body.folder_files, batch_id, rows, body.common_detail_files)
    errors = sum(bool(row["errors"]) for row in rows)
    conn = db()
    conn.execute("INSERT INTO batches(id,filename,status,total,valid,errors,created_at,rows_json) VALUES(?,?,?,?,?,?,?,?)", (batch_id, body.filename, "待校验" if errors else "待发布", len(rows), len(rows) - errors, errors, now(), json.dumps(rows, ensure_ascii=False)))
    conn.commit(); conn.close()
    products = group_products(rows)
    product_errors = sum(bool(product["errors"]) for product in products)
    return {"ok": True, "batch_id": batch_id, "total": len(rows), "product_count": len(products), "valid": len(rows) - errors, "errors": errors, "product_errors": product_errors}


def _huopai_module():
    """货盘转换模块(局部导入:只有货盘相关接口需要 openpyxl 与转换规则)。"""
    try:
        import huopai_adapter as module
    except Exception as exc:
        raise HTTPException(500, f"货盘转换模块不可用：{exc}")
    return module


def _huopai_dir():
    """货盘表目录白名单根: HUOPAI_DIR 优先, 否则取默认货盘表所在目录。"""
    directory = os.environ.get("HUOPAI_DIR") or os.path.dirname(_huopai_module().HUOPAI_PATH)
    return os.path.abspath(directory)


def _resolve_huopai_file(path):
    """把请求里的货盘表路径收敛到白名单目录内的 .xlsx。

    防止 /import-huopai 被用来读取(并解析)服务器上任意文件: 之前 path 完全来自请求体,
    传 C:\\Windows\\... 也会被当成 Excel 打开。现在只允许货盘目录内的 .xlsx。
    """
    candidate = os.path.abspath(str(path or ""))
    root = _huopai_dir()
    if os.path.splitext(candidate)[1].lower() != ".xlsx":
        raise HTTPException(400, "货盘表只支持 .xlsx 文件")
    try:
        inside = os.path.commonpath((root, candidate)) == root
    except ValueError:
        inside = False
    if not inside:
        raise HTTPException(400, f"货盘表路径不在允许的货盘目录内：{root}")
    if not os.path.isfile(candidate):
        raise HTTPException(400, f"货盘表不存在：{candidate}")
    return candidate


@app.get("/huopai-files")
def list_huopai_files():
    """【货盘文件列表】列出货盘目录下可导入的 .xlsx,供前端下拉选择。

    目录由服务端配置(HUOPAI_DIR / HUOPAI_PATH)决定,接口不接受客户端传目录;
    因此运营侧的货盘表只要放进该目录即可,不再需要把绝对路径写进前端。
    """
    directory = _huopai_dir()
    if not os.path.isdir(directory):
        raise HTTPException(400, f"货盘目录不可访问：{directory}(请配置 HUOPAI_DIR / HUOPAI_PATH)")
    with os.scandir(directory) as scanner:
        entries = sorted(scanner, key=lambda entry: entry.name)
    files = []
    for entry in entries:
        if not entry.is_file() or os.path.splitext(entry.name)[1].lower() != ".xlsx":
            continue
        stat = entry.stat()
        files.append({"name": entry.name, "path": entry.path, "size": stat.st_size,
                      "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()})
    default_path = os.path.abspath(_huopai_module().HUOPAI_PATH)
    return {"ok": True, "dir": directory,
            "default": default_path if os.path.isfile(default_path) else "",
            "files": files}


@app.post("/import-huopai")
def import_huopai(body: HuopaiImportBody | None = None):
    """【直读货盘】从服务器本地货盘目录读货盘表,后台跑「货盘→标准模板」转换后入库。
    与大平台一致:数据源直连,不经过浏览器上传,规避 64MB 大文件 Base64 传输。
    安全: 只允许读取货盘目录白名单内的 .xlsx(见 _resolve_huopai_file)。
    转换规则与 huopai_adapter.py 完全一致(编码/价格/库存/属性/图片/系列拆分)。"""
    import openpyxl  # 局部导入:仅本接口需要
    H = _huopai_module()
    path = _resolve_huopai_file((body.path if body and body.path else None) or H.HUOPAI_PATH)
    # ① 转换:货盘 → 标准列 dict(复用 huopai_adapter 的业务规则)
    try:
        images = H.load_images()
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            raw_rows = []
            if "培育钻" in wb.sheetnames:
                raw_rows.extend(H.parse_peiyuzuan(wb["培育钻"], images))
            if "天然钻" in wb.sheetnames:
                raw_rows.extend(H.parse_tianranzuan(wb["天然钻"], images))
        finally:
            wb.close()
        H.enrich_carat(raw_rows)
        H.enrich_split_tone(raw_rows)
        H.enrich_split_chain(raw_rows)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"货盘转换失败：{exc}")
    if not raw_rows:
        raise HTTPException(400, "货盘表没解析到商品行(检查 sheet 名是否含「培育钻」「天然钻」)")
    # ② 复用标准导入的归一化 + 批次入库
    batch_id = uuid.uuid4().hex
    rows = [normalize_row(r, i) for i, r in enumerate(raw_rows, 2)
            if any(str(v or "").strip() for v in r.values())]
    if len(rows) > 5000:
        raise HTTPException(400, "单批最多导入5000行SKU")
    errors = sum(bool(row["errors"]) for row in rows)
    filename = os.path.basename(path)
    conn = db()
    conn.execute("INSERT INTO batches(id,filename,status,total,valid,errors,created_at,rows_json) VALUES(?,?,?,?,?,?,?,?)",
                 (batch_id, filename, "待校验" if errors else "待发布", len(rows), len(rows) - errors, errors, now(), json.dumps(rows, ensure_ascii=False)))
    conn.commit(); conn.close()
    products = group_products(rows)
    return {"ok": True, "batch_id": batch_id, "filename": filename, "total": len(rows),
            "product_count": len(products), "valid": len(rows) - errors, "errors": errors,
            "product_errors": sum(bool(product["errors"]) for product in products)}


@app.get("/image-roots")
def list_image_roots():
    """【图片根目录】返回服务端允许扫描的图片根目录白名单,供前端下拉选择。

    之前 root 完全来自请求体,传入任意目录即可枚举该目录下的商品文件夹与图片;
    现在只暴露白名单(ALLOWED_IMAGE_ROOTS),扫描接口也只接受白名单内的 root。
    """
    return {"ok": True, "default": os.path.abspath(DEFAULT_IMAGE_ROOT),
            "roots": [{"path": os.path.abspath(root), "available": os.path.isdir(root)}
                      for root in ALLOWED_IMAGE_ROOTS]}


@app.post("/batches/{batch_id}/images/scan")
def scan_batch_images(batch_id: str, body: ImageScanBody | None = None):
    """【直读图片】扫描图片根目录, 按编码把商品图/SKU 图挂到批次上。

    解决两个实际问题:
      ① 多 SKU 商品的图分散在子文件夹(文件夹名就是 SKU 编码), 浏览器一次只能选一个文件夹,
         结果只读到本色那几张图, 其他规格的图全漏;
      ② 一次要发几百个商品, 走浏览器 Base64 上传要几 GB, 根本不现实。
    服务端直接读网络共享, 233 个商品 4589 张图约 4 秒扫完。

    匹配规则见 image_scanner.py:
      - 商品主图   = 商品文件夹根下的本色图(按 主图1 -> 主图2 -> 白底 -> PNG 排序)
      - SKU 图     = 该 SKU 的「主图2」(运营指定), 落到 row["sku_image"]
      - 通用详情图 = 通用通栏目录, 追加到每个商品的详情图
    扫描阶段只记录共享盘路径(懒拷贝), 不占磁盘; 真正发布到某个商品时,
    才读它的图片字节落盘缓存(内容 sha256 去重), 再上传平台拿素材 URL。

    传 dry_run=true 可只匹配不写库, 先看命中率。
    """
    try:
        import image_scanner
    except Exception as exc:
        raise HTTPException(500, f"图片扫描模块不可用：{exc}")

    batch = batch_or_404(batch_id)
    rows = batch["items"]
    root = (body.root if body and body.root else None) or DEFAULT_IMAGE_ROOT
    months = body.months if body else None
    dry_run = bool(body.dry_run) if body else False
    # 路径收敛: root 只能取白名单内的图片根目录(见 GET /image-roots),
    # 否则该接口可被用来枚举服务器任意文件夹的商品图。
    if not _within_allowed_roots(root) or not os.path.isdir(root):
        raise HTTPException(400, f"图片根目录不可访问或不在允许的图片目录内：{root}"
                                 f"(允许：{'、'.join(ALLOWED_IMAGE_ROOTS)})")

    try:
        scan = image_scanner.scan_image_root(root, months=months)
    except Exception as exc:
        raise HTTPException(500, f"扫描图片失败：{exc}")

    product_main = {}      # 商品编码 -> [共享盘图片引用]    (多 SKU 行共用, 只解析一次)
    sku_image = {}         # SKU 编码  -> 共享盘图片引用
    errors = []
    unmatched_skus = []

    def localize(path):
        # 懒拷贝: 只记共享盘路径, 真正上传平台时才读字节落盘。
        # 扫描 233 个商品秒级完成、不占磁盘; 只发一部分商品就只读一部分图。
        return f"{SHARE_PREFIX}{path}"

    common_refs = []
    for path in scan["common_images"]:
        try:
            common_refs.append(localize(path))
        except Exception as exc:
            errors.append(f"通用详情图 {os.path.basename(path)}: {exc}")

    products_matched = 0
    skus_matched = 0
    for row in rows:
        code = str(row.get("product_code") or "")
        if code not in product_main:
            base = code.split("-", 1)[0]
            # ① 精确 → ② 去系列标签(如 ZSTZ106XL-星悦 → ZSTZ106XL)
            paths = scan["root_images"].get(code) or scan["root_images"].get(base)
            if not paths:
                # ③ 图库文件夹名可能带中文后缀(如 HZL253福运钻 vs 商品编码 HZL253-如愿),
                #    取最长前缀兜底; 仍匹配不到 = 图库里确实没有这个款
                best_key = ""
                for key in scan["root_images"]:
                    if key and (key.startswith(base) or base.startswith(key)) and len(key) > len(best_key):
                        best_key = key
                paths = scan["root_images"].get(best_key) if best_key else None
            references = []
            for path in (paths or [])[:5]:      # 平台主图最多 5 张
                try:
                    references.append(localize(path))
                except Exception as exc:
                    errors.append(f"{os.path.basename(path)}: {exc}")
            product_main[code] = references
            if references:
                products_matched += 1
        if product_main[code]:
            row["main_images"] = product_main[code]
        if common_refs:
            row["detail_images"] = list(dict.fromkeys((row.get("detail_images") or []) + common_refs))

        sku_code = str(row.get("sku_code") or "")
        if sku_code not in sku_image:
            hit = image_scanner.match_sku_code(sku_code, scan["sku_images"])
            main2 = ((scan["sku_images"].get(hit) or {}).get("main2") if hit else None)
            if main2:
                try:
                    sku_image[sku_code] = localize(main2)
                except Exception as exc:
                    sku_image[sku_code] = ""
                    errors.append(f"SKU图 {os.path.basename(main2)}: {exc}")
            else:
                sku_image[sku_code] = ""
        if sku_image[sku_code]:
            row["sku_image"] = sku_image[sku_code]
            skus_matched += 1
        else:
            unmatched_skus.append(sku_code or f"line_{row.get('line')}")

    if not dry_run:
        conn = db()
        conn.execute("UPDATE batches SET rows_json=? WHERE id=?",
                     (json.dumps(rows, ensure_ascii=False), batch_id))
        conn.commit()
        conn.close()

    return {
        "ok": True, "batch_id": batch_id, "dry_run": dry_run,
        "root": root, "months": months,
        "folders": scan["folders"], "scanned_images": scan["scanned"],
        "common_detail_images": len(scan["common_images"]),
        "products_matched": products_matched,
        "products_total": len({r.get("product_code") for r in rows}),
        "skus_matched": skus_matched, "skus_total": len(rows),
        "unmatched_sku_count": len(unmatched_skus),
        "unmatched_skus": unmatched_skus[:200],
        "lazy_load": True, "errors": errors[:50],
    }


@app.post("/batches/{batch_id}/sku-image")
def upload_sku_image(batch_id: str, body: SkuImageBody):
    """给批次里某个 SKU 补规格图(浏览器上传单张图)。

    落盘到 IMAGE_DIR/{批次id}/{sha256}.{ext}, 返回 local:// 引用;
    前端拿到引用后写回该行 sku_image, 再 PUT /import/{id}/items 持久化。
    引用格式必须与 _local_image 的校验一致, 否则预览/上传素材会失败。
    """
    batch_or_404(batch_id)
    try:
        content = base64.b64decode(body.content_base64 or "")
    except Exception as exc:
        raise HTTPException(400, f"图片内容解析失败：{exc}")
    if not content:
        raise HTTPException(400, "图片内容为空")
    extension = os.path.splitext(body.filename or "")[1].lower().lstrip(".")
    if extension not in {"jpg", "jpeg", "png", "webp", "gif", "bmp"}:
        extension = "jpg"
    digest = hashlib.sha256(content).hexdigest()
    batch_dir = os.path.join(IMAGE_DIR, batch_id)
    os.makedirs(batch_dir, exist_ok=True)
    path = os.path.join(batch_dir, f"{digest}.{extension}")
    if not os.path.exists(path):
        with open(path, "wb") as image_file:
            image_file.write(content)
    return {"ok": True, "result": {"ref": f"local://{batch_id}/{digest}.{extension}"}}


@app.get("/images/preview")
def preview_image(ref: str):
    """预览批次里的图片(供浏览器显示纯文本路径无法渲染的图)。

    支持两种引用:
      - local://{批次id}/{hash}.{ext}   浏览器上传或懒拷贝落盘后的本地图
      - disk:{共享盘绝对路径}            图片直读扫描写入的引用
    出于安全只允许读取 ALLOWED_IMAGE_ROOTS 下的文件, 否则参数可读任意文件。
    """
    source = str(ref or "").strip()
    if source.startswith("local://"):
        found = _local_image(source)
        if not found:
            raise HTTPException(404, "图片不存在")
        content = found[1]
    elif is_share_image(source):
        path = os.path.abspath(source[len(SHARE_PREFIX):])
        # 用 commonpath 判断而非 startswith: 否则 '...\images_evil\x.jpg' 会命中 '...\images'
        if not _within_allowed_roots(path) or not os.path.isfile(path):
            raise HTTPException(404, "图片不存在或不在允许的图片目录内")
        with open(path, "rb") as image_file:
            content = image_file.read()
    else:
        raise HTTPException(400, "不支持的图片引用")
    if not content:
        raise HTTPException(404, "图片内容为空")
    media = "image/png" if source.lower().endswith(".png") else "image/jpeg"
    return StreamingResponse(io.BytesIO(content), media_type=media)


def batch_or_404(batch_id):
    conn = db(); row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone(); conn.close()
    if not row: raise HTTPException(404, "批次不存在")
    data = dict(row); data["items"] = json.loads(data.pop("rows_json")); data["products"] = group_products(data["items"]); data["mappings"] = json.loads(data.pop("mappings_json")); return data


# ------------------------------------------------------------------
# 批次解析缓存:worker 每发一个商品都要拿批次的 rows/mappings,
# 全量 json.loads + group_products 在 800 商品时单次近百毫秒,
# 2N 次(商品×平台)调用就是 O(N²)。这里按 batch_id 缓存解析结果,
# 写接口(items/mappings/status)主动失效,避免 worker 用到旧数据。
# ------------------------------------------------------------------
_BATCH_CACHE: dict = {}
_BATCH_CACHE_TTL = 60.0


def _invalidate_batch_cache(batch_id: str = None) -> None:
    """写接口改完 batches 后调用;batch_id 为空则全清。"""
    if batch_id:
        _BATCH_CACHE.pop(batch_id, None)
    else:
        _BATCH_CACHE.clear()


def get_batch_cached(batch_id: str) -> dict:
    """命中且未过期则复用解析结果,否则回源 batch_or_404 并写缓存。"""
    hit = _BATCH_CACHE.get(batch_id)
    if hit and (time.time() - hit["ts"]) < _BATCH_CACHE_TTL:
        return hit["data"]
    data = batch_or_404(batch_id)
    _BATCH_CACHE[batch_id] = {"ts": time.time(), "data": data}
    return data


@app.get("/import/{batch_id}")
def get_batch(batch_id: str):
    return {"ok": True, "result": batch_or_404(batch_id)}


@app.put("/import/{batch_id}/items")
def update_items(batch_id: str, body: ItemsBody):
    batch = batch_or_404(batch_id); rows = body.items
    errors = sum(bool(row.get("errors")) for row in rows)
    conn = db(); conn.execute("UPDATE batches SET rows_json=?,status=?,valid=?,errors=? WHERE id=?", (json.dumps(rows, ensure_ascii=False), "待校验" if errors else "待发布", len(rows)-errors, errors, batch_id)); conn.commit(); conn.close()
    _invalidate_batch_cache(batch_id)
    products = group_products(rows)
    return {"ok": True, "result": {"total": len(rows), "product_count": len(products), "valid": len(rows)-errors, "errors": errors, "product_errors": sum(bool(product["errors"]) for product in products)}}


@app.put("/import/{batch_id}/mappings")
def update_mappings(batch_id: str, body: MappingsBody):
    batch_or_404(batch_id); conn = db(); conn.execute("UPDATE batches SET mappings_json=? WHERE id=?", (json.dumps(body.mappings, ensure_ascii=False), batch_id)); conn.commit(); conn.close(); _invalidate_batch_cache(batch_id); return {"ok": True, "result": body.mappings}


@app.post("/import/{batch_id}/validate")
def validate(batch_id: str):
    batch = batch_or_404(batch_id); rows = batch["items"]; seen = set()
    for row in rows:
        errors = [e for e in row.get("errors", []) if e not in ("商品编码重复", "SKU编码重复")]
        key = row.get("product_code", "")
        sku = row.get("sku_code", "")
        if key and sku and (key, sku) in seen: errors.append("SKU编码重复")
        seen.add((key, sku)); row["errors"] = errors
    return update_items(batch_id, ItemsBody(items=rows))


@app.post("/import/{batch_id}/publish")
def publish(batch_id: str, body: PublishBody):
    batch = batch_or_404(batch_id)
    if batch["errors"]: raise HTTPException(400, "仍有校验错误，不能发布")
    platforms = [p for p in body.platforms if p in ("wechat", "xhs")]
    if not platforms: raise HTTPException(400, "至少选择一个发布平台")
    # Idempotency guard: do not create another platform item for a combination
    # that already has a publish record in this batch.
    products = batch["products"]
    check_conn = db()
    check_conn.execute("BEGIN IMMEDIATE")
    prior = []
    pending = []
    for product in products:
        for platform in platforms:
            row = check_conn.execute("SELECT * FROM publish_items WHERE batch_id=? AND product_code=? AND platform=? ORDER BY id DESC LIMIT 1", (batch_id, product["product_code"], platform)).fetchone()
            if row:
                prior.append(row)
            else:
                pending.append((product["product_code"], platform))
    if prior and not pending:
        check_conn.commit()
        reusable = next((row for row in reversed(prior) if row["status"] in ("queued", "running")), prior[-1])
        check_conn.close()
        status = reusable["status"]
        message = {"success": "该批次所选商品已发布，未重复创建", "failed": "该批次已有失败项，请使用重试失败项", "partial": "该批次存在部分成功项，请勿重复创建"}.get(status, "该批次已有发布任务，未重复创建")
        return {"ok": True, "job_id": reusable["job_id"], "batch_id": batch_id, "platforms": platforms, "existing": True, "created_count": 0, "skipped_count": len(prior), "message": message}
    # Keep the write transaction open through job/item insertion.
    conn = check_conn
    job_id = uuid.uuid4().hex
    products = batch["products"]
    conn.execute("INSERT INTO publish_jobs(id,batch_id,platforms_json,status,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (job_id, batch_id, json.dumps(platforms), "queued", len(pending) if pending else len(products) * len(platforms), now(), now()))
    for product_code, platform in (pending or [(product["product_code"], platform) for product in products for platform in platforms]):
        conn.execute("INSERT INTO publish_items(job_id,batch_id,product_code,platform,status) VALUES(?,?,?,?,?)", (job_id, batch_id, product_code, platform, "queued"))
    conn.execute("UPDATE batches SET status=? WHERE id=?", ("发布任务已创建", batch_id)); conn.commit(); conn.close(); _invalidate_batch_cache(batch_id)
    return {"ok": True, "job_id": job_id, "batch_id": batch_id, "platforms": platforms, "message": "发布任务已入队，后台将持续执行"}


@app.get("/jobs/{job_id}")
def job(job_id: str):
    conn = db(); j = conn.execute("SELECT * FROM publish_jobs WHERE id=?", (job_id,)).fetchone()
    if not j: conn.close(); raise HTTPException(404, "发布任务不存在")
    items = [dict(r) for r in conn.execute("SELECT * FROM publish_items WHERE job_id=? ORDER BY id", (job_id,)).fetchall()]
    conn.close()
    return {"ok": True, "result": {"job_id": job_id, "batch_id": j["batch_id"], "status": j["status"], "total": j["total"], "processed": j["processed"], "success": j["success"], "failed": j["failed"], "items": items}}


# ==================================================================
# 类目别名映射表:内部类目 -> 微信/小红书平台类目。
# 运营人工确认一次类目组后存入此表,后续批次自动命中,不再重复选。
# ==================================================================
@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, body: RetryBody):
    """仅重新排队失败项；部分成功项不自动重试，避免重复创建平台商品。"""
    conn = db()
    if not conn.execute("SELECT id FROM publish_jobs WHERE id=?", (job_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "发布任务不存在")
    requested = set(body.item_ids or [])
    if requested:
        placeholders = ",".join("?" for _ in requested)
        rows = conn.execute(f"SELECT * FROM publish_items WHERE job_id=? AND id IN ({placeholders})", [job_id, *requested]).fetchall()
        if requested - {row["id"] for row in rows}:
            conn.close()
            raise HTTPException(400, "只能重试当前任务中的商品")
    else:
        rows = conn.execute("SELECT * FROM publish_items WHERE job_id=? AND status='failed'", (job_id,)).fetchall()
    conn.execute("BEGIN IMMEDIATE")
    retried = skipped = 0
    for row in rows:
        if row["status"] == "failed":
            conn.execute("UPDATE publish_items SET status='queued', error=NULL, started_at=NULL, finished_at=NULL WHERE id=?", (row["id"],))
            retried += 1
        elif row["status"] == "partial":
            skipped += 1
    if retried:
        conn.execute("UPDATE publish_jobs SET status='queued', processed=(SELECT COUNT(*) FROM publish_items WHERE job_id=? AND status IN ('success','partial')), success=(SELECT COUNT(*) FROM publish_items WHERE job_id=? AND status='success'), failed=(SELECT COUNT(*) FROM publish_items WHERE job_id=? AND status='partial'), updated_at=? WHERE id=?", (job_id, job_id, job_id, now(), job_id))
    conn.commit(); conn.close()
    message = f"已将 {retried} 个失败商品重新排队" if retried else ("部分成功项不会自动重试，以免重复创建平台商品" if skipped else "当前没有可重试的失败商品")
    return {"ok": True, "job_id": job_id, "retried": retried, "skipped": skipped, "message": message}


@app.get("/category-aliases")
def list_aliases():
    conn = db()
    rows = conn.execute("SELECT * FROM category_aliases ORDER BY updated_at DESC").fetchall()
    conn.close()
    return {"ok": True, "result": [{
        "id": r["id"], "internal_category": r["internal_category"],
        "wechat": json.loads(r["wechat_json"]) if r["wechat_json"] else None,
        "xhs": json.loads(r["xhs_json"]) if r["xhs_json"] else None,
        "updated_at": r["updated_at"],
    } for r in rows]}


@app.post("/category-aliases")
def save_alias(body: AliasBody):
    key = (body.internal_category or "").strip()
    if not key:
        raise HTTPException(400, "internal_category 不能为空")
    conn = db()
    existing = conn.execute("SELECT wechat_json, xhs_json FROM category_aliases WHERE internal_category=?", (key,)).fetchone()
    # 合并语义:本次未传的一侧保留旧值(微信和小红书经常分两次确认)
    wechat = body.wechat if body.wechat is not None else (json.loads(existing["wechat_json"]) if existing and existing["wechat_json"] else None)
    xhs = body.xhs if body.xhs is not None else (json.loads(existing["xhs_json"]) if existing and existing["xhs_json"] else None)
    if not wechat and not xhs:
        conn.close()
        raise HTTPException(400, "wechat 和 xhs 至少提供一个")
    conn.execute(
        """INSERT INTO category_aliases(internal_category, wechat_json, xhs_json, updated_at)
           VALUES(?,?,?,?)
           ON CONFLICT(internal_category) DO UPDATE SET
             wechat_json=excluded.wechat_json, xhs_json=excluded.xhs_json, updated_at=excluded.updated_at""",
        (key, json.dumps(wechat, ensure_ascii=False) if wechat else None,
         json.dumps(xhs, ensure_ascii=False) if xhs else None, now()))
    conn.commit(); conn.close()
    return {"ok": True, "result": {"internal_category": key, "wechat": wechat, "xhs": xhs}}


@app.delete("/category-aliases/{alias_id}")
def delete_alias(alias_id: int):
    conn = db(); conn.execute("DELETE FROM category_aliases WHERE id=?", (alias_id,)); conn.commit(); conn.close()
    return {"ok": True}


# ------------------------------------------------------------------
# 映射表批量导入:运营提前整理"内部类目 -> 平台类目路径"Excel,
# 导入时按官方类目名逐级反查平台类目ID并校验,成功行存入映射表。
# ------------------------------------------------------------------
WECHAT_API_BASE = os.environ.get("WECHAT_API_BASE", "http://localhost:8000")
XHS_API_BASE = os.environ.get("XHS_API_BASE", "http://localhost:8010")


def _http_get_json(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def split_category_path(text):
    return [part.strip() for part in re.split(r"[>＞]", str(text or "")) if part.strip()]


def parse_mapping_sheet(raw, filename):
    """解析映射表文件,返回 [{内部类目/微信类目路径/小红书类目路径...}] 原始行。"""
    extension = os.path.splitext(filename or "")[1].lower()
    if extension == ".xls":
        raise HTTPException(400, "暂不支持旧版 .xls，请另存为 .xlsx 后再导入")
    if extension == ".csv":
        try:
            return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        except UnicodeDecodeError:
            raise HTTPException(400, "CSV 请保存为 UTF-8 编码后再导入")
        except Exception as exc:
            raise HTTPException(400, f"CSV 解析失败：{exc}")
    try:
        import openpyxl
        workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        rows = list(workbook.active.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(x or "").strip() for x in rows[0]]
        result = []
        for cells in rows[1:]:
            if not any(cell not in (None, "") for cell in cells):
                continue
            result.append({headers[i]: (cells[i] if i < len(cells) else "") for i in range(len(headers)) if headers[i]})
        return result
    except Exception as exc:
        raise HTTPException(400, f"Excel 解析失败：{exc}")


def resolve_wechat_path(parts, cache):
    """按官方名称路径整链反查微信类目,返回 (结果, 错误原因)。"""
    keyword = parts[-1]
    if keyword not in cache:
        try:
            data = _http_get_json(f"{WECHAT_API_BASE}/categories?keyword={urllib.parse.quote(keyword)}", timeout=180)
        except Exception as exc:
            raise HTTPException(502, f"微信类目服务({WECHAT_API_BASE})不可用：{exc}")
        cache[keyword] = [r for r in data.get("results", []) if r.get("leaf")]
    leaves = cache[keyword]
    matched = [r for r in leaves if [str(n.get("name", "")).strip() for n in r.get("chain", [])] == parts]
    if len(matched) == 1:
        return {"category": matched[0]["path"], "chain": matched[0].get("chain", [])}, None
    candidates = "；".join(r.get("path", "") for r in leaves[:5]) or "无"
    return None, f"未匹配到唯一微信类目（含“{keyword}”的候选：{candidates}）"


def resolve_xhs_path(parts, cache):
    """按官方名称路径逐级反查小红书类目,末级必须是叶子,返回 (结果, 错误原因)。"""
    parent_id = None
    chain = []
    last = None
    for index, part in enumerate(parts):
        key = str(parent_id or "")
        if key not in cache:
            url = f"{XHS_API_BASE}/categories" + (f"?category_id={urllib.parse.quote(key)}" if parent_id else "")
            try:
                data = _http_get_json(url, timeout=60)
            except Exception as exc:
                raise HTTPException(502, f"小红书类目服务({XHS_API_BASE})不可用：{exc}")
            result = data.get("result") or []
            cache[key] = result if isinstance(result, list) else []
        options = cache[key]
        matches = [c for c in options if str(c.get("name", "")).strip() == part]
        if not matches:
            names = "、".join(str(c.get("name", "")) for c in options[:10]) or "空"
            return None, f"第{index + 1}级没有“{part}”，该级可选：{names}"
        last = matches[0]
        parent_id = str(last.get("id") or last.get("categoryId") or "")
        chain.append({"id": parent_id, "name": str(last.get("name", "")).strip()})
    if not (last.get("isLeaf") or last.get("leaf")):
        return None, f"“{parts[-1]}”不是叶子类目，请写到平台最后一级"
    return {"category": " > ".join(c["name"] for c in chain), "chain": chain, "category_id": chain[-1]["id"]}, None


@app.get("/category-aliases/template")
def alias_template():
    headers = ["内部类目", "微信类目路径", "小红书类目路径"]
    try:
        import openpyxl
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "类目映射"
        sheet.append(headers)
        sheet.append(["钻石 > 钻石首饰 > 项链", "珠宝首饰 > 天然钻石 > 项链", "黄金/彩宝/钻石/珍珠 > 钻石首饰 > 颈饰/挂件/吊坠"])
        for cell in sheet[1]:
            cell.font = openpyxl.styles.Font(bold=True)
        sheet.freeze_panes = "A2"
        note = workbook.create_sheet("填写说明")
        for line in [
            "1. 内部类目必须与商品导入表里的内部类目完全一致，同名行会覆盖更新已有映射。",
            "2. 平台类目路径必须使用平台官方类目名，各级之间用 > 分隔，写到叶子级为止。",
            "3. 只填一侧也可以，未填写的一侧保留映射表里已有的旧值。",
            "4. 导入时系统自动把名称反查成平台类目ID并逐级校验，失败行会给出原因，修正后重新上传即可。",
        ]:
            note.append([line])
        output = io.BytesIO()
        workbook.save(output)
        output.seek(0)
        return StreamingResponse(iter([output.read()]), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=category-alias-template.xlsx"})
    except Exception as exc:
        raise HTTPException(500, f"生成映射表模板失败：{exc}")


@app.post("/category-aliases/import")
def import_aliases(body: AliasImportBody):
    try:
        raw = base64.b64decode(body.content_base64)
    except Exception:
        raise HTTPException(400, "文件编码无效")
    rows = parse_mapping_sheet(raw, body.filename)
    wechat_cache, xhs_cache, report, resolved = {}, {}, [], []
    imported = failed = skipped = 0
    for index, row in enumerate(rows, 2):
        internal = value(row, "内部类目", "类目")
        wechat_path = value(row, "微信类目路径", "微信类目", "微信")
        xhs_path = value(row, "小红书类目路径", "小红书类目", "小红书")
        if not internal:
            skipped += 1
            report.append({"line": index, "internal_category": "", "wechat": "跳过：缺少内部类目", "xhs": ""})
            continue
        if not wechat_path and not xhs_path:
            skipped += 1
            report.append({"line": index, "internal_category": internal, "wechat": "", "xhs": "跳过：两侧类目都为空"})
            continue
        wechat = xhs = None
        wechat_msg = xhs_msg = "未填写"
        if wechat_path:
            wechat, err = resolve_wechat_path(split_category_path(wechat_path), wechat_cache)
            wechat_msg = "成功" if wechat else err
        if xhs_path:
            xhs, err = resolve_xhs_path(split_category_path(xhs_path), xhs_cache)
            xhs_msg = "成功" if xhs else err
        if wechat or xhs:
            imported += 1
            resolved.append((internal, wechat, xhs))
        else:
            failed += 1
        report.append({"line": index, "internal_category": internal, "wechat": wechat_msg, "xhs": xhs_msg})
    if resolved:
        conn = db()
        for internal, wechat, xhs in resolved:
            # 合并语义与单条保存一致:本次未解析出的一侧保留旧值
            existing = conn.execute("SELECT wechat_json, xhs_json FROM category_aliases WHERE internal_category=?", (internal,)).fetchone()
            merged_wechat = wechat or (json.loads(existing["wechat_json"]) if existing and existing["wechat_json"] else None)
            merged_xhs = xhs or (json.loads(existing["xhs_json"]) if existing and existing["xhs_json"] else None)
            conn.execute(
                """INSERT INTO category_aliases(internal_category, wechat_json, xhs_json, updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(internal_category) DO UPDATE SET
                     wechat_json=excluded.wechat_json, xhs_json=excluded.xhs_json, updated_at=excluded.updated_at""",
                (internal, json.dumps(merged_wechat, ensure_ascii=False) if merged_wechat else None,
                 json.dumps(merged_xhs, ensure_ascii=False) if merged_xhs else None, now()))
        conn.commit(); conn.close()
    return {"ok": True, "result": {"imported": imported, "failed": failed, "skipped": skipped, "report": report}}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=BULK_API_HOST, port=BULK_API_PORT)
