# -*- coding: utf-8 -*-
"""
====================================================================
 管易云 C-ERP → 小红书上下架系统 · 同步中间层(独立脚本)
====================================================================
作用:把运营在管易云里维护的「商品主数据 + 库存」同步到你自己写的
     小红书上下架系统(xhs_api.py),由它再落到小红书店铺。

设计原则(三条,请务必保留):
    1. 不改动 xhs_api.py / xhs_store.py 等任何现有代码,本文件完全独立。
    2. 只通过 HTTP 调 xhs_api.py 已开放的端点,不 import 它,做到松耦合。
    3. 库存只做「管易云 → 小红书」单向,不回写,避免两边循环覆盖。

调用链:
    管易云开放 API(gy.erp.*)
        │  拉商品 / 库存(增量)
        ▼
    guanyi_sync.py(本文件:字段映射 + SKU 绑定)
        │
        ▼
    xhs_api.py(端口 8010,现有服务,需先启动)
        │
        ▼
    小红书店铺

前置条件:
    1. 管易云后台「控制面板 → 应用授权 → 云 ERP 授权」拿到 appkey/secret/sessionkey
    2. xhs_api.py 已在跑(另开一个终端:python xhs_api.py)
    3. SKU 绑定在首次创建商品时自动写入 guanyi_xhs_mapping.json,无需手工建

用法(建议第一次一定带 --dry-run 先看一遍):
    python guanyi_sync.py --mode items --dry-run     # 预演:看会创建哪些商品
    python guanyi_sync.py --mode items               # 同步商品(创建/更新)
    python guanyi_sync.py --mode stock               # 同步库存(单向)
    python guanyi_sync.py --mode available           # 按管易云停用状态同步上下架
    python guanyi_sync.py --mode all                 # 以上三步全跑

环境变量(密钥不要写死进仓库):
    GY_APPKEY / GY_SECRET / GY_SESSIONKEY            管易云三件套
    GY_API_URL                                      管易云接口地址(随登录域名变)
    XHS_API_BASE                                    上下架系统地址,默认 127.0.0.1:8010
    XHS_BRAND_ID / XHS_CATEGORY_ID                  小红书品牌、叶子类目
    XHS_SHIPPING_TEMPLATE_ID / XHS_LOGISTICS_PLAN_ID 运费模板、物流方案
====================================================================
"""
import os
import json
import time
import hashlib
import argparse
import sys
from datetime import datetime, timedelta

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime_config import load_project_env

load_project_env()

# ==================================================================
# 一、配置
# ==================================================================
# 管易云凭证:后台「控制面板 → 应用授权 → 云 ERP 授权」页面获取
GY_APPKEY = os.environ.get("GY_APPKEY", "")
GY_SECRET = os.environ.get("GY_SECRET", "")
GY_SESSIONKEY = os.environ.get("GY_SESSIONKEY", "")

# 管易云接口地址随你的登录域名变,常见两种:
#   www.guanyierp.com → http://api.guanyierp.com/rest/erp_open
#   v2.guanyierp.com  → http://v2.api.guanyierp.com/rest/erp_open
GY_API_URL = os.environ.get("GY_API_URL", "http://api.guanyierp.com/rest/erp_open")

# 现有上下架系统地址(xhs_api.py 默认端口 8010)
XHS_API_BASE = os.environ.get("XHS_API_BASE", "http://127.0.0.1:8010")

# 小红书侧固定参数:管易云里没有对应概念,必须按你的店铺配好。
# 下面填的是测试店铺实测可用的值,换真实店铺(钻石世家)请改这里或改环境变量。
XHS_BRAND_ID = os.environ.get("XHS_BRAND_ID", "")
XHS_CATEGORY_ID = os.environ.get("XHS_CATEGORY_ID", "")
XHS_SHIPPING_TEMPLATE_ID = os.environ.get("XHS_SHIPPING_TEMPLATE_ID", "")
XHS_LOGISTICS_PLAN_ID = os.environ.get("XHS_LOGISTICS_PLAN_ID", "")

DEFAULT_WEIGHT = 500        # 毛重兜底(克),管易云有 weight 时优先用它的
DEFAULT_IPQ = 1             # 每笔购买数量
PAGE_SIZE = 50              # 管易云分页大小(默认 QPS=50,分页间留间隔)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAPPING_FILE = os.path.join(BASE_DIR, "guanyi_xhs_mapping.json")    # SKU 绑定表
STATE_FILE = os.path.join(BASE_DIR, "guanyi_sync_state.json")       # 增量游标


# ==================================================================
# 二、管易云客户端(签名 + 通用调用)
# ==================================================================
def gy_sign(params: dict, secret: str) -> str:
    """签名:MD5(secret + 标准JSON(不含 sign) + secret),32 位大写"""
    raw = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    return hashlib.md5((secret + raw + secret).encode("utf-8")).hexdigest().upper()


def gy_call(method: str, biz: dict = None, timeout: int = 30) -> dict:
    """调管易云接口。失败抛异常并带上官方错误码"""
    if not (GY_APPKEY and GY_SECRET and GY_SESSIONKEY):
        raise RuntimeError("缺少管易云凭证,请先设置 GY_APPKEY / GY_SECRET / GY_SESSIONKEY")

    params = {"appkey": GY_APPKEY, "sessionkey": GY_SESSIONKEY, "method": method, **(biz or {})}
    params["sign"] = gy_sign(params, GY_SECRET)

    resp = requests.post(GY_API_URL, json=params, timeout=timeout)
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(
            f"[{method}] 管易云调用失败: errorCode={data.get('errorCode')} "
            f"{data.get('errorDesc') or data.get('subErrorDesc') or ''}"
        )
    return data


def gy_fetch_items(start_time: str, end_time: str, page_size: int = PAGE_SIZE) -> list:
    """按修改时间增量拉管易云商品。返回商品列表"""
    items, page_no = [], 1
    while True:
        data = gy_call("gy.erp.items.get", {
            "start_date": start_time,
            "end_date": end_time,
            "page_no": page_no,
            "page_size": page_size,
        })
        batch = data.get("items") or []
        items.extend(batch)
        if len(batch) < page_size:
            break
        page_no += 1
        time.sleep(0.2)     # 管易云默认 QPS=50,分页之间留余量
    return items


def gy_fetch_stock(page_size: int = PAGE_SIZE) -> list:
    """拉管易云库存。

    ⚠️ 注意:gy.erp.new.stock 的返回字段名各版本略有差异(常见为
    item_code / sku_code / qty / warehouse_code)。第一次跑时建议先打印一条
    原始返回确认字段名,再回来校准 sync_stock() 里的取值逻辑。
    """
    stocks, page_no = [], 1
    while True:
        data = gy_call("gy.erp.new.stock", {"page_no": page_no, "page_size": page_size})
        batch = data.get("stocks") or data.get("items") or []
        stocks.extend(batch)
        if len(batch) < page_size:
            break
        page_no += 1
        time.sleep(0.2)
    return stocks


# ==================================================================
# 三、调现有上下架系统(xhs_api.py),不 import,只走 HTTP
# ==================================================================
def xhs_call(method: str, path: str, json_body: dict = None, timeout: int = 60):
    """调 xhs_api.py。HTTP 4xx/5xx 直接抛异常带 detail;
    ok=False 但 partial=True(部分 SKU 失败)时不抛,留给上层处理。"""
    url = f"{XHS_API_BASE.rstrip('/')}{path}"
    resp = requests.request(method, url, json=json_body, timeout=timeout)
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code >= 400:
        raise RuntimeError(
            f"[{method} {path}] 上下架系统返回 {resp.status_code}: {data.get('detail') or data}")
    if data.get("ok") is False and data.get("partial") is not True:
        raise RuntimeError(f"[{method} {path}] 调用未成功: {data}")
    return data.get("result")


def xhs_upload_image(url: str) -> str:
    """把管易云图片地址转存到小红书素材,返回素材 URL(填进 images 字段)"""
    result = xhs_call("POST", "/materials/upload", {"url": url})
    return result.get("url") or result.get("materialUrl") or ""


# ==================================================================
# 四、持久化:映射表 + 增量游标
# ==================================================================
def load_mapping() -> dict:
    """映射表结构:{items:{管易云商品code: 小红书itemId}, skus:{管易云sku码: 小红书skuId}}"""
    if os.path.exists(MAPPING_FILE):
        with open(MAPPING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"items": {}, "skus": {}}


def save_mapping(mapping: dict):
    with open(MAPPING_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_items_sync": ""}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ==================================================================
# 五、字段映射:管易云商品 → 小红书创建请求体
# ==================================================================
def build_xhs_payload(gy_item: dict) -> dict:
    """把管易云商品转成 xhs_api /items/and-sku 要的 {item, sku_list}。

    管易云侧字段:code / name / simple_name / pic_url / weight / note / skus[].code|name|sales_price
    小红书侧要求:name 8-30 字且同店唯一、images 必须是小红书素材 URL、
                brandId / categoryId / shippingTemplateId / logisticsPlanId 必填。
    """
    code = gy_item.get("code") or ""
    name = (gy_item.get("name") or gy_item.get("simple_name") or "").strip()
    if not name:
        raise ValueError(f"商品缺少名称,跳过: {code}")
    if len(name) < 8:
        name = f"{name} 定制款"          # 小红书标题下限 8 字,补后缀兜底
    name = name[:30]

    # 图片:管易云 pic_url 是外链,必须上传成小红书素材才能用
    images = []
    for raw_url in str(gy_item.get("pic_url") or "").split(";"):
        url = raw_url.strip()
        if not url:
            continue
        try:
            material_url = xhs_upload_image(url)
            if material_url:
                images.append(material_url)
        except Exception as e:
            print(f"    ! 主图上传失败,跳过该图: {url} ({e})")
    if not images:
        raise ValueError(f"商品无可用主图,跳过: {code}")

    item = {
        "name": name,
        "brandId": XHS_BRAND_ID,
        "categoryId": XHS_CATEGORY_ID,
        "attributes": [],
        "shippingTemplateId": XHS_SHIPPING_TEMPLATE_ID,
        "shippingGrossWeight": int(gy_item.get("weight") or DEFAULT_WEIGHT),
        "variantIds": [],
        "images": images,
        "videoUrl": "",
        "articleNo": code,
        "imageDescriptions": images[:5],
        "description": gy_item.get("note") or name,
        "deliveryMode": "0",
        "freeReturn": "1",
    }

    sku_list = []
    for sku in (gy_item.get("skus") or []):
        try:
            price = int(float(sku.get("sales_price") or 0))
        except (TypeError, ValueError):
            price = 0
        if price <= 0:
            continue
        sku_list.append({
            "ipq": DEFAULT_IPQ,
            "originalPrice": price,
            "price": price,
            # 管易云商品接口不返回库存,先置 0,随后由 --mode stock 补真实值
            "stock": 0,
            "logisticsPlanId": XHS_LOGISTICS_PLAN_ID,
            "variants": [],
            "deliveryTime": {"time": "24", "type": "RELATIVE_TIME_NEW"},
            "erpCode": sku.get("code") or "",     # 关键:用管易云 SKU 编码做两边绑定
        })
    if not sku_list:
        raise ValueError(f"商品无有效 SKU(价格缺失),跳过: {code}")

    return {"item": item, "sku_list": sku_list}


# ==================================================================
# 六、三种同步动作
# ==================================================================
def sync_items(since: str = None, dry_run: bool = False):
    """同步商品主数据:未绑定的创建,已绑定的只更新描述(不动标题,避免重新审核)"""
    state = load_state()
    mapping = load_mapping()
    end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start_time = since or state.get("last_items_sync") or (
        datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

    print(f"[商品同步] 增量区间: {start_time} ~ {end_time}")
    items = gy_fetch_items(start_time, end_time)
    print(f"[商品同步] 管易云返回 {len(items)} 个商品")

    created = updated = skipped = 0
    for gy_item in items:
        code = gy_item.get("code") or ""
        try:
            payload = build_xhs_payload(gy_item)
        except ValueError as e:
            print(f"  - 跳过: {e}")
            skipped += 1
            continue

        if dry_run:
            print(f"  [预演] {code} → 标题「{payload['item']['name']}」, "
                  f"SKU {len(payload['sku_list'])} 个")
            continue

        xhs_item_id = mapping["items"].get(code)
        if xhs_item_id:
            # 已绑定 → 只改描述,改标题会触发小红书重新审核
            xhs_call("PUT", f"/items/{xhs_item_id}", {
                "item": {"description": payload["item"]["description"]},
                "updated_fields": ["description"],
            })
            updated += 1
        else:
            result = xhs_call("POST", "/items/and-sku", payload)
            xhs_item_id = result.get("itemId")
            mapping["items"][code] = xhs_item_id
            # 用 erpCode 反查,把小红书 skuId 绑到管易云 SKU 编码上
            for sku_id, sku in zip(result.get("skuIds") or [], payload["sku_list"]):
                if sku.get("erpCode"):
                    mapping["skus"][sku["erpCode"]] = sku_id
            created += 1
            print(f"  + 创建: {code} → itemId={xhs_item_id}, skuIds={result.get('skuIds')}")
            for err in (result.get("skuErrors") or []):
                print(f"    ! SKU 创建失败明细: {err}")

    if not dry_run:
        save_mapping(mapping)
        state["last_items_sync"] = end_time
        save_state(state)
    print(f"[商品同步] 完成: 新增 {created}, 更新 {updated}, 跳过 {skipped}")


def sync_stock(dry_run: bool = False):
    """同步库存:管易云 → 小红书,单向不回写"""
    mapping = load_mapping()
    if not mapping["skus"]:
        print("[库存同步] 映射表为空,请先跑 --mode items 建立 SKU 绑定")
        return

    print("[库存同步] 拉取管易云库存...")
    stocks = gy_fetch_stock()
    print(f"[库存同步] 管易云返回 {len(stocks)} 条")

    updated = skipped = 0
    for row in stocks:
        # 字段名按管易云实际返回取值,见 gy_fetch_stock 的注释
        sku_code = row.get("sku_code") or row.get("item_code") or ""
        qty = row.get("qty", row.get("stock", row.get("quantity")))
        if not sku_code or qty is None:
            skipped += 1
            continue

        xhs_sku_id = mapping["skus"].get(sku_code)
        if not xhs_sku_id:
            skipped += 1
            continue

        if dry_run:
            print(f"  [预演] SKU {sku_code}({xhs_sku_id}) → stock={int(qty)}")
            continue

        xhs_call("PUT", f"/skus/{xhs_sku_id}", {
            "sku": {"stock": int(qty)},
            "updated_fields": ["stock"],
        })
        updated += 1

    print(f"[库存同步] 完成: 更新 {updated}, 跳过 {skipped}(未绑定或字段缺失)")


def sync_available(dry_run: bool = False):
    """按管易云「是否停用(del)」同步小红书 SKU 上下架。

    ⚠️ 两条规则必须和运营确认后再开:
       1. 管易云 del=1(停用)是否就等于小红书下架,别想当然。
       2. 小红书上架前商品必须审核通过(buyable=true),否则报 -5000300,
          这里会把失败单独打出来,不影响其他 SKU。
    """
    mapping = load_mapping()
    if not mapping["skus"]:
        print("[上下架同步] 映射表为空,请先跑 --mode items")
        return

    end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start_time = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    items = gy_fetch_items(start_time, end_time)
    print(f"[上下架同步] 扫描 {len(items)} 个商品")

    on = off = failed = 0
    for gy_item in items:
        available = 0 if str(gy_item.get("del") or "0") == "1" else 1
        for sku in (gy_item.get("skus") or []):
            sku_code = sku.get("code") or ""
            xhs_sku_id = mapping["skus"].get(sku_code)
            if not xhs_sku_id:
                continue

            if dry_run:
                print(f"  [预演] SKU {sku_code} → available={available}")
                continue

            try:
                xhs_call("POST", f"/skus/{xhs_sku_id}/available", {"available": available})
                if available:
                    on += 1
                else:
                    off += 1
            except Exception as e:
                print(f"    ! SKU {sku_code} 上下架失败(多半是未过审): {e}")
                failed += 1

    print(f"[上下架同步] 完成: 上架 {on}, 下架 {off}, 失败 {failed}")


# ==================================================================
# 七、入口
# ==================================================================
def main():
    parser = argparse.ArgumentParser(description="管易云 → 小红书上下架系统 同步中间层")
    parser.add_argument("--mode", choices=["items", "stock", "available", "all"],
                        default="items", help="同步内容,默认 items")
    parser.add_argument("--since", default=None,
                        help="商品增量起始时间,格式 'YYYY-MM-DD HH:MM:SS'")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要执行的操作,不真正写数据")
    args = parser.parse_args()

    if args.mode in ("items", "all"):
        sync_items(since=args.since, dry_run=args.dry_run)
    if args.mode in ("stock", "all"):
        sync_stock(dry_run=args.dry_run)
    if args.mode in ("available", "all"):
        sync_available(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
