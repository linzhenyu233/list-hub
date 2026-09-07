# -*- coding: utf-8 -*-
"""
====================================================================
 小红书店铺 · FastAPI 服务(运营后台版,19 个接口)
====================================================================
把 xhs_store 包装成 HTTP 接口,给前端运营后台用:
运营在网页点按钮 → 前端调这里 → 小红书店铺操作。

调用链:
    前端运营后台(Vue/React,只认 HTTP)
        │  按页面分组调 19 个接口
        ▼
    xhs_api.py(本服务,端口 8010)
        │  内部封装签名/请求/错误(含 error_msg)
        ▼
    小红书开放平台网关(ark.xiaohongshu.com)

启动方式:
    python xhs_api.py
    # 或: uvicorn xhs_api:app --host 0.0.0.0 --port 8010

浏览器打开 http://127.0.0.1:8010/docs 可看接口文档并在线调试。

接口一览(19 个,按前端页面分组):
  ── 系统/Token ────────────────────────────────
    GET  /health                       健康检查(探活)
    GET  /token/info                   当前 token 信息(剩几天,提醒运营续期)
    POST /token/code                   首次接入:用授权 code 换 token {"code":"..."}
    POST /token/refresh                一键续期(accessToken 7天,refreshToken 14天)
  ── 发品页 ────────────────────────────────────
    GET  /categories?category_id=&keyword=   选类目(传到叶子 isLeaf=true)
    GET  /brands?category_id=&keyword=       选品牌(联动,选完类目再查)
    GET  /shipping-templates                 运费模板下拉
    GET  /logistics-plans                    物流方案下拉(创建SKU用)
    GET  /category-attributes?category_id=   末级类目属性(珠宝attributes必查)
    GET  /category-variations?category_id=   末级类目规格(variantIds/variants)
    GET  /attribute-values?category_id=&attribute_id=  属性/规格候选值(valueId)
    POST /materials/upload                   传图 {"url":"..."} 拿素材URL
    POST /items/and-sku                      提交:创建商品+SKU {"item":{},"sku_list":[]}
  ── 商品列表页 ────────────────────────────────
    GET  /items?page_no=&page_size=          分页商品列表
    POST /items/status                       批量查审核状态 {"item_ids":[...]}
    POST /skus/{sku_id}/available            行内上下架 {"available":1上架 0下架}
  ── 商品编辑页 ────────────────────────────────
    GET  /items/{item_id}                    商品详情(含 skus[].buyable 审核状态)
    PUT  /items/{item_id}                    改商品 {"item":{},"updated_fields":[...]}
    PUT  /skus/{sku_id}                      改价格/库存 {"sku":{},"updated_fields":[...]}

审核状态说明(前端列表页"审核中/已上架"怎么来):
    小红书"先审后发",商品创建后要过审核(24h~1-3工作日)才能上架。
    用 GET /items/{id} 或 POST /items/status 查 skus[].buyable:
        buyable=false → 审核中/不可售(上架按钮置灰,上架会报 -5000300)
        buyable=true  → 审核通过/可售(可点上架)
    建议列表页每 5 分钟调一次 /items/status 刷新状态。
====================================================================
"""
import os
import json
import time
import base64
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Body, Query
from fastapi.middleware.cors import CORSMiddleware
import requests

# 复用 xhs_store.py 里的凭证配置(客户端类里的常量)
from xhs_store import XhsStore, APP_ID, APP_SECRET, ACCESS_TOKEN

# ------------------------------------------------------------------
# 配置:token 持久化文件(存 accessToken/refreshToken,系统重启不丢)
# ------------------------------------------------------------------
TOKEN_FILE = os.environ.get(
    "XHS_TOKEN_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "token_store.json"),
)
REFRESH_TOKEN = os.environ.get("XHS_REFRESH_TOKEN", "")   # 可环境变量指定初始 refreshToken
API_HOST = os.environ.get("XHS_API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("XHS_API_PORT", "8010"))
CORS_ORIGINS = [item.strip() for item in os.environ.get("CORS_ORIGINS", "*").split(",") if item.strip()]

# 创建 FastAPI 应用
app = FastAPI(title="小红书运营后台服务", version="0.2.0")

# CORS:允许前端跨域(开发期全放行,上线改成前端具体域名)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_CACHE = {}
_CACHE_LOCK = threading.RLock()


def _cached(key, ttl, loader, force=False):
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not force and entry and entry["expires_at"] > now:
            return entry["value"]
    value = loader()
    with _CACHE_LOCK:
        _CACHE[key] = {"value": value, "expires_at": time.monotonic() + ttl}
        if len(_CACHE) > 1000:
            expired = [cache_key for cache_key, item in _CACHE.items() if item["expires_at"] <= now]
            for cache_key in expired:
                _CACHE.pop(cache_key, None)
    return value


def _clear_cache(*prefixes):
    with _CACHE_LOCK:
        for key in list(_CACHE):
            if key and key[0] in prefixes:
                _CACHE.pop(key, None)


def _clear_product_cache():
    _clear_cache("item_list", "item_detail", "item_status")

# ==================================================================
# Token 存储 + 自动续期
# ==================================================================
def _load_token() -> dict:
    """从本地文件读 token;没有则返回空"""
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_token(tok: dict):
    """把 token 写回本地文件(换新 token 后必须调,否则重启丢失)"""
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(tok, f, ensure_ascii=False, indent=2)

def _refresh_access_token():
    tok = _load_token()
    refresh = tok.get("refreshToken") or REFRESH_TOKEN
    if not refresh:
        raise RuntimeError("没有保存 refreshToken，无法自动续期")
    data = _xhs_call("oauth.refreshAccessToken", {"refreshToken": refresh})
    access = data.get("accessToken")
    if not access:
        raise RuntimeError("刷新接口未返回 accessToken")
    tok["accessToken"] = access
    if data.get("refreshToken"):
        tok["refreshToken"] = data["refreshToken"]
    tok["expireAt"] = int(time.time()) + int(data.get("expiresIn", 7 * 24 * 3600))
    _save_token(tok)
    return access

def _get_access_token() -> str:
    """取当前 accessToken(文件优先,其次配置),并自动续期"""
    tok = _load_token()
    access = tok.get("accessToken") or ACCESS_TOKEN
    refresh = tok.get("refreshToken") or REFRESH_TOKEN
    expire_at = tok.get("expireAt", 0)

    # 快过期(剩<30分钟)且有 refreshToken → 自动续期
    if refresh and expire_at and time.time() > expire_at - 1800:
        try:
            access = _refresh_access_token()
        except Exception:
            pass  # 续期失败不阻塞本次调用,让业务接口自己报鉴权错
    return access


# ==================================================================
# 统一请求封装(签名 + 公共参数 + 完整错误信息含 error_msg)
# ==================================================================
def _xhs_call(method: str, payload: dict = None) -> dict:
    """调小红书网关。成功返回 data 字段;失败抛 HTTP 400 带完整错误信息"""
    access = None
    # oauth 换 token 的接口不需要 accessToken
    if method not in ("oauth.getAccessToken", "oauth.refreshAccessToken"):
        access = _get_access_token()

    store = XhsStore(APP_ID, APP_SECRET, access)
    timestamp = str(int(time.time()))
    body = {
        "timestamp": timestamp,
        "appId": APP_ID,
        "sign": store._sign(method, timestamp),
        "version": "2.0",
        "method": method,
        **(payload or {}),
    }
    if access:
        body["accessToken"] = access

    try:
        resp = requests.post(
            store.BASE,
            headers={"Content-Type": "application/json;charset=utf-8"},
            json=body,
            timeout=30,
        )
        data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"请求小红书网关失败: {e}")

    expired = data.get("error_code") == 401 or "accessToken expired" in str(data.get("error_msg") or data.get("message") or "")
    if expired and method not in ("oauth.getAccessToken", "oauth.refreshAccessToken"):
        try:
            access = _refresh_access_token()
            store = XhsStore(APP_ID, APP_SECRET, access)
            timestamp = str(int(time.time()))
            retry_body = {"timestamp": timestamp, "appId": APP_ID, "sign": store._sign(method, timestamp), "version": "2.0", "method": method, **(payload or {}), "accessToken": access}
            resp = requests.post(store.BASE, headers={"Content-Type": "application/json;charset=utf-8"}, json=retry_body, timeout=30)
            data = resp.json()
        except Exception as exc:
            raise HTTPException(status_code=401, detail=f"小红书授权已过期，自动刷新失败: {exc}")
    if data.get("error_code") != 0 or not data.get("success"):
        msg = data.get("error_msg") or data.get("message") or ""
        raise HTTPException(
            status_code=400,
            detail=f"[{method}] 调用失败: error_code={data.get('error_code')} {msg}",
        )
    return data.get("data")


def ok_or_400(fn, *args, **kwargs):
    """执行 fn,成功返回 {"ok":true,"result":...};失败统一转 HTTP 错误"""
    try:
        return {"ok": True, "result": fn(*args, **kwargs)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"服务内部错误: {e}")


# ==================================================================
# 系统/Token
# ==================================================================
@app.get("/health")
def health():
    return {"status": "ok", "service": "xhs-store-api"}


@app.get("/token/info")
def token_info():
    """当前 token 状态:是否已配置、过期时间(前端顶部提醒运营用)"""
    tok = _load_token()
    expire_at = tok.get("expireAt", 0)
    now = int(time.time())
    remain = max(0, expire_at - now) if expire_at else None
    return {
        "ok": True,
        "configured": bool(tok.get("accessToken") or ACCESS_TOKEN),
        "has_refresh_token": bool(tok.get("refreshToken") or REFRESH_TOKEN),
        "expire_at": expire_at,
        "remain_seconds": remain,
        "remain_days": round(remain / 86400, 2) if remain is not None else None,
    }


@app.post("/token/code")
def token_by_code(body: dict = Body(..., example={"code": "code-xxx"})):
    """首次接入:用授权回调的 code 换 accessToken/refreshToken,并持久化。
    调用链:店铺主账号授权 → 浏览器地址栏拿 code → 调这里一次,以后自动续。"""
    code = body.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="请求体需要 code 字段")
    data = _xhs_call("oauth.getAccessToken", {"code": code})
    tok = _load_token()
    tok["accessToken"] = data.get("accessToken")
    if data.get("refreshToken"):
        tok["refreshToken"] = data["refreshToken"]
    tok["expireAt"] = int(time.time()) + int(data.get("expiresIn", 7 * 24 * 3600))
    _save_token(tok)
    return {"ok": True, "result": {"accessToken": data.get("accessToken"),
                                   "refreshToken": data.get("refreshToken"),
                                   "expireAt": tok["expireAt"]}}


@app.post("/token/refresh")
def token_refresh():
    """一键续期(accessToken 7天 / refreshToken 14天,14天内至少续一次)"""
    tok = _load_token()
    refresh = tok.get("refreshToken") or REFRESH_TOKEN
    if not refresh:
        raise HTTPException(status_code=400, detail="没有保存 refreshToken,无法续期")
    data = _xhs_call("oauth.refreshAccessToken", {"refreshToken": refresh})
    tok["accessToken"] = data.get("accessToken")
    if data.get("refreshToken"):
        tok["refreshToken"] = data["refreshToken"]
    tok["expireAt"] = int(time.time()) + int(data.get("expiresIn", 7 * 24 * 3600))
    _save_token(tok)
    return {"ok": True, "result": {"accessToken": data.get("accessToken"),
                                   "expireAt": tok["expireAt"]}}


# ==================================================================
# 发品页:公共参数查询
# ==================================================================
@app.get("/categories")
def categories(category_id: str = Query(None, description="父类目ID,不传查一级类目"),
               keyword: str = Query(None, description="关键词过滤(匹配类目名)")):
    """选类目。传 category_id 查子类目;不传查一级。创建商品要用叶子(isLeaf=true)。"""
    payload = {"categoryId": category_id} if category_id else {}
    data = _cached(
        ("categories", str(category_id or "")), 12 * 3600,
        lambda: _xhs_call("common.getCategories", payload),
    )
    cats = data.get("categoryV3s", []) if data else []
    if keyword:
        cats = [c for c in cats if keyword in (c.get("name") or "")]
    return {"ok": True, "total": len(cats), "result": cats}


@app.get("/brands")
def brands(category_id: str = Query(..., description="末级类目ID(必填,选完类目再查)"),
           keyword: str = Query("", description="品牌关键词")):
    """搜索品牌(创建商品 brandId 必填,从这里查)"""
    data = _cached(
        ("brands", str(category_id), str(keyword)), 6 * 3600,
        lambda: _xhs_call("common.brandSearch", {
            "categoryId": category_id, "keyword": keyword, "pageNo": 1, "pageSize": 50}),
    )
    return {"ok": True, "result": data}


@app.get("/shipping-templates")
def shipping_templates():
    """运费模板列表(创建商品 shippingTemplateId 必填)"""
    data = _cached(
        ("shipping_templates",), 10 * 60,
        lambda: _xhs_call("common.getCarriageTemplateList", {"pageIndex": 1, "pageSize": 50}),
    )
    return {"ok": True, "result": data}


@app.get("/logistics-plans")
def logistics_plans():
    """物流方案列表(创建 SKU logisticsPlanId 必填,注意不是运费模板ID!)"""
    data = _cached(
        ("logistics_plans",), 10 * 60,
        lambda: _xhs_call("common.getLogisticsList"),
    )
    return {"ok": True, "result": data}


@app.get("/category-attributes")
def category_attributes(category_id: str = Query(..., description="末级叶子类目ID(必填)")):
    """由末级类目查“商品属性”定义(common.getAttributeLists)。
    珠宝等类目发品必填:创建商品 attributes 每一项要 propertyId + valueId。
    前端据此渲染属性下拉,把运营选的“材质=18K金”翻译成平台 propertyId/valueId。
    ⚠️ 首次联调在 /docs 看真实返回:属性数组键名、每个属性候选值 valueId 结构以平台为准。"""
    data = _cached(
        ("category_attributes", str(category_id)), 12 * 3600,
        lambda: _xhs_call("common.getAttributeLists", {"categoryId": category_id}),
    )
    return {"ok": True, "result": data}


@app.get("/category-variations")
def category_variations(category_id: str = Query(..., description="末级叶子类目ID(必填)")):
    """由末级类目查“规格”定义(common.getVariations)。
    对应创建商品的 variantIds(规格维度)和每个 SKU 的 variants(规格值)。
    后台“颜色分类/重量/尺寸”这些规格维度就来自这里。
    ⚠️ 首次联调看真实返回的规格维度 id 与其规格值(valueId)结构。"""
    data = _cached(
        ("category_variations", str(category_id)), 12 * 3600,
        lambda: _xhs_call("common.getVariations", {"categoryId": category_id}),
    )
    return {"ok": True, "result": data}


@app.get("/attribute-values")
def attribute_values(
    category_id: str = Query(..., description="末级叶子类目ID(必填)"),
    attribute_id: str = Query(..., description="属性或规格的 id(必填)"),
):
    """查某个属性或规格维度的候选值(common.getAttributeValues)。
    创建商品的 attributes 要 propertyId+valueId,SKU 的 variants 要 valueId。
    attributeId 既可传属性 id(如“钻石切工”),也可传规格 id(如“颜色分类”)。
    返回 attributeValueV3s:[{valueId,valueName}]。数值型规格(如“重量/克拉”)返回空数组。"""
    data = _cached(
        ("attribute_values", str(category_id), str(attribute_id)), 12 * 3600,
        lambda: _xhs_call("common.getAttributeValues", {
            "categoryId": category_id, "attributeId": attribute_id}),
    )
    return {"ok": True, "result": data}


# ==================================================================
# 发品页:素材上传
# ==================================================================
@app.post("/materials/upload")
def upload_material(body: dict = Body(..., example={"url": "https://你的图床/主图1.jpg"})):
    """传图:下载公网图片 → 上传小红书素材,返回素材URL(填进 images 字段)"""
    url = body.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="请求体需要 url 字段")
    try:
        img = requests.get(url, timeout=30).content
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"下载图片失败: {e}")
    b64 = base64.b64encode(img).decode("utf-8")
    name = url.split("/")[-1][:40] or "material.jpg"
    data = _xhs_call("material.uploadMaterial", {
        "name": name, "type": "IMAGE", "materialContent": b64})
    return {"ok": True, "result": data}


@app.post("/materials/upload-file")
def upload_material_file(body: dict = Body(...)):
    """上传 Excel 中提取出的内嵌图片。"""
    encoded = body.get("content_base64")
    if not encoded:
        raise HTTPException(status_code=400, detail="请求体需要 content_base64 字段")
    try:
        content = base64.b64decode(encoded, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="content_base64 编码无效")
    if not content or len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片为空或超过 20MB")
    name = os.path.basename(str(body.get("filename") or "material.jpg"))[:40]
    data = _xhs_call("material.uploadMaterial", {
        "name": name, "type": "IMAGE", "materialContent": encoded})
    return {"ok": True, "result": data}


# ==================================================================
# 发品页:创建商品+SKU(一次搞定)
# ==================================================================
@app.post("/items/and-sku")
def create_item_and_sku(body: dict = Body(..., examples=[{
    "item": {
        "name": "18K金钻石戒指",                 # 8-30字,同店不可重复
        "brandId": "品牌ID",                     # /brands 查
        "categoryId": "叶子类目ID",              # /categories 查叶子
        "attributes": [],
        "shippingTemplateId": "运费模板ID",      # /shipping-templates 查
        "shippingGrossWeight": 500,
        "variantIds": [],
        "images": ["https://.../主图1.jpg"],     # /materials/upload 拿URL
        "videoUrl": "",
        "articleNo": "货号",
        "imageDescriptions": ["https://.../详情图.jpg"],
        "description": "商品描述",
        "deliveryMode": "0",
        "freeReturn": "1",
    },
    "sku_list": [{
        "ipq": 1, "originalPrice": 8800, "price": 8000, "stock": 100,
        "logisticsPlanId": "物流方案ID",         # /logistics-plans 查
        "variants": [],
        "deliveryTime": {"time": "24", "type": "RELATIVE_TIME_NEW"},
        "erpCode": "商家编码",
    }],
}])):
    """提交发品:一次创建商品+多个SKU。
    ⚠️ 实现说明:平台 product.createItemAndSku 接口实测有 bug(字段体系混乱,
    任何格式都报错),这里改为分步:createItemV2 建商品 → createSkuV2 逐个建 SKU,
    两者都已实测可用。对前端来说效果一样:一次请求返回 itemId + skuIds。
    创建成功后商品进入"审核中",等审核通过(buyable=true)才能上架。"""
    item = body.get("item")
    sku_list = body.get("sku_list") or []
    if not item:
        raise HTTPException(status_code=400, detail="请求体需要 item 字段")
    if not sku_list:
        raise HTTPException(status_code=400, detail="请求体需要 sku_list 列表")

    # ---- 分步创建(可靠方案) ----
    # 第 1 步:创建商品
    item_data = _xhs_call("product.createItemV2", item)
    item_id = item_data.get("id") or item_data.get("itemId")
    if not item_id:
        raise HTTPException(status_code=500, detail="创建商品成功但未返回 itemId")

    # 第 2 步:逐个创建 SKU(单个失败不阻塞,已建的保留,前端可用 itemId 再补)
    sku_ids, errors = [], []
    for sku in sku_list:
        try:
            sku_data = _xhs_call("product.createSkuV2", {"itemId": item_id, **sku})
            sku_id = (sku_data or {}).get("id") or (sku_data or {}).get("skuId")
            if sku_id:
                sku_ids.append(sku_id)
            else:
                errors.append({"sku": sku.get("erpCode") or sku.get("price"),
                               "error": "SKU 创建接口未返回 skuId"})
        except HTTPException as e:
            errors.append({"sku": sku.get("erpCode") or sku.get("price"), "error": str(e.detail)})

    _clear_product_cache()
    return {"ok": not errors, "partial": bool(errors) and bool(sku_ids), "result": {
        "itemId": item_id,
        "skuIds": sku_ids,
        "skuRequestedCount": len(sku_list),
        "skuCount": len(sku_ids),
        "skuErrors": errors,          # 部分 SKU 失败时的明细,空=全部成功
    }}


# ==================================================================
# 商品列表页
# ==================================================================
@app.get("/items")
def list_items(page_no: int = Query(1, ge=1, description="页码,从1开始"),
               page_size: int = Query(20, ge=1, le=100, description="每页条数,最大100")):
    """商品列表(分页)。列表不含审核状态,要显示"审核中/已上架"再调 /items/status。"""
    data = _cached(
        ("item_list", page_no, page_size), 30,
        lambda: _xhs_call("product.searchItemList", {"pageNo": page_no, "pageSize": page_size}),
    )
    return {"ok": True, "result": data}


@app.post("/items/status")
def items_status(body: dict = Body(..., example={"item_ids": ["itemId1", "itemId2"]})):
    """批量查审核状态(前端列表页每5分钟刷一次)。
    返回 [{itemId, name, skus:[{skuId, buyable}]}],buyable=true=审核通过可上架。"""
    item_ids = body.get("item_ids") or []
    if not item_ids:
        raise HTTPException(status_code=400, detail="请求体需要 item_ids 列表")
    item_ids = item_ids[:20]
    _get_access_token()  # 并发查询前先完成一次 token 检查或刷新

    def fetch_status(item_id):
        try:
            data = _cached(
                ("item_status", str(item_id)), 10,
                lambda: _xhs_call("product.getItemInfo", {"itemId": item_id}),
            )
            skus = [{
                "skuId": s.get("id"),
                "buyable": bool(s.get("buyable")),
                "price": s.get("price"),
                "originalPrice": s.get("originalPrice"),
                "stock": s.get("stock"),
            } for s in (data.get("skuInfos") or [])]
            return {"itemId": item_id,
                    "name": (data.get("itemInfo") or {}).get("name"),
                    "skus": skus}
        except HTTPException:
            return {"itemId": item_id, "name": None, "skus": [], "error": "查询失败"}

    # 商品详情互不依赖，限制为最多 5 个并发，兼顾速度与平台限流。
    with ThreadPoolExecutor(max_workers=min(5, len(item_ids))) as pool:
        results = list(pool.map(fetch_status, item_ids))
    return {"ok": True, "total": len(results), "result": results}


@app.post("/skus/{sku_id}/available")
def set_sku_available(sku_id: str, body: dict = Body(..., example={"available": 1})):
    """行内上下架。available: 1=上架(买家可见) 0=下架。
    ⚠️ 上架前必须审核通过(buyable=true),否则报 -5000300。
    小红书按 SKU 粒度上下架,商品多个 SKU 要逐个调。"""
    available = body.get("available")
    if available not in (0, 1):
        raise HTTPException(status_code=400, detail="available 必须是 0 或 1")
    data = _xhs_call("product.updateSkuAvailable", {
        "skuId": str(sku_id), "available": str(available)})
    _clear_product_cache()
    return {"ok": True, "result": data}


# ==================================================================
# 商品编辑页
# ==================================================================
@app.get("/items/{item_id}")
def get_item(item_id: str):
    """商品详情:完整信息 + skus[].buyable 审核状态(编辑页回填用)"""
    data = _cached(
        ("item_detail", str(item_id)), 60,
        lambda: _xhs_call("product.getItemInfo", {"itemId": item_id}),
    )
    return {"ok": True, "result": data}


@app.put("/items/{item_id}")
def update_item(item_id: str, body: dict = Body(..., example={
    "item": {"name": "新标题"}, "updated_fields": ["name"]})):
    """改商品。建议传 updated_fields 只更新指定字段,不传=全量更新(有风险)"""
    item = body.get("item")
    if not item:
        raise HTTPException(status_code=400, detail="请求体需要 item 字段")
    payload = {"id": item_id, **item}
    if body.get("updated_fields"):
        payload["updatedFields"] = body["updated_fields"]
    result = ok_or_400(lambda: _xhs_call("product.updateItemV2", payload))
    _clear_product_cache()
    return result


@app.put("/skus/{sku_id}")
def update_sku(sku_id: str, body: dict = Body(..., example={
    "sku": {"price": 9000, "stock": 50}, "updated_fields": ["price", "stock"]})):
    """改 SKU(价格/库存等)。updated_fields 可选。"""
    sku = body.get("sku")
    if not sku:
        raise HTTPException(status_code=400, detail="请求体需要 sku 字段")
    payload = {"id": sku_id, **sku}
    if body.get("updated_fields"):
        payload["updatedFields"] = body["updated_fields"]
    result = ok_or_400(lambda: _xhs_call("product.updateSkuV2", payload))
    _clear_product_cache()
    return result


# ==================================================================
# 启动入口:python xhs_api.py 直接跑(端口 8010,避开微信小店 8000)
# ==================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=API_HOST, port=API_PORT)
