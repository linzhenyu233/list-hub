# -*- coding: utf-8 -*-
"""
====================================================================
 微信小店 · FastAPI 服务(把脚本包装成 HTTP 接口)
====================================================================
之前 wechat_store_client.py 是"脚本",只能自己跑、自己看输出;
包装成 FastAPI 服务后,其他系统(中台/前端网页)可以通过 HTTP
调用它,实现:运营在网页点按钮 → 中台调这里 → 微信小店上架。

调用链:
    中台系统(Java/Go 都行,只认 HTTP)
        │  POST /products  {"product": {...}}
        ▼
    fastapi_server.py(本服务,Python)
        │  内部复用 wechat_store_client.WxStore
        ▼
    微信小店 API(api.weixin.qq.com)

启动方式:
    set WX_APPID=wx你的AppID
    set WX_SECRET=你的AppSecret
    python fastapi_server.py
    # 或: uvicorn fastapi_server:app --host 0.0.0.0 --port 8000

浏览器打开 http://127.0.0.1:8000/docs 可看接口文档并在线调试。

接口一览(全部返回 JSON):
    GET  /health                       健康检查(不用凭证)
    POST /token/test                   测试凭证是否有效
    GET  /categories?keyword=珠宝      查类目(返回每级的 cat_id)
    POST /images/upload                上传图片 {"img_url": "..."}
    POST /videos/upload-file           上传本地视频 {"filename": "...", "content_base64": "..."}
    POST /products                     发布商品(草稿) {"product": {...}}
    POST /products/{pid}/update        更新商品 {"product": {...}}
    POST /products/{pid}/listing       上架商品
    POST /products/{pid}/delisting     下架商品
    DELETE /products/{pid}             删除商品
    GET  /products/{pid}               查商品/审核状态
    GET  /category-detail?cat_id=      查叶子类目属性+规格定义

说明:
  - 类目权限只影响"发布/上架",其他接口(查类目/传图/查商品)
    现在就能用,所以服务可以先搭起来,等珠宝类目审核通过再发品。
====================================================================
"""
import base64
import os
import sys
import threading
import time
from fastapi import FastAPI, HTTPException, Body, Query, Request
from fastapi.middleware.cors import CORSMiddleware

# 复用之前写好的微信小店客户端(脚本里的类,直接拿来用)
from wechat_store_client import WxStore

# 多店铺：从项目根读店铺注册表（shops.json，缺失则回退 .env 单店）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime_config import load_project_env
import shop_registry
import api_auth

load_project_env()

# ------------------------------------------------------------------
# 配置（host/port/cors 仍是进程级；凭证改为按店铺取，见 store_for）
# ------------------------------------------------------------------
# 默认只监听本机：原先 0.0.0.0 + 无鉴权，局域网内任意机器都能直接调发布/删商品接口。
# 确需局域网直连时设 WX_API_HOST=0.0.0.0，并务必配好 API_KEY。
API_HOST = os.environ.get("WX_API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("WX_API_PORT", "8000"))
CORS_ORIGINS = [item.strip() for item in os.environ.get("CORS_ORIGINS", "*").split(",") if item.strip()]

# 创建 FastAPI 应用(标题/版本会在 /docs 文档页显示)
app = FastAPI(title="微信小店自动上链接服务", version="0.2.0")

# 鉴权：X-API-Key（未配置 API_KEY 时只告警不拦截，见 api_auth.py）。
# 必须在 add_middleware(CORS) 之前装，否则 401 响应不经过 CORS 中间件。
api_auth.install(app, "wechat-api")

# CORS:允许前端页面跨域调用(开发期全放行,上线换成前端具体域名)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
# 多店铺：每个店铺一个 WxStore 实例（access_token 缓存在实例内，互不干扰）
# ------------------------------------------------------------------
_STORES = {}
_STORES_LOCK = threading.RLock()


def shop_of(request: Request) -> dict:
    """解析当前店铺：读 X-Shop-Id 头；不传则用默认店（兼容旧调用）。"""
    shop_id = (request.headers.get("x-shop-id") or "").strip() if request is not None else ""
    try:
        return shop_registry.resolve(shop_id or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def store_for(shop: dict) -> "WxStore":
    """按店铺返回 WxStore 实例（首次构造并缓存，之后复用其 access_token）。"""
    shop_id = shop["shop_id"]
    with _STORES_LOCK:
        inst = _STORES.get(shop_id)
        if inst is None:
            conf = shop.get("wechat") or {}
            appid = conf.get("appid") or ""
            if not appid:
                raise HTTPException(status_code=400, detail=f"店铺 {shop_id} 未配置微信小店凭证")
            inst = WxStore(
                appid, conf.get("secret") or "",
                # 本店自己的兜底值（之前这两个参数写死在 .env 里，第二家店会串用第一家店的）
                after_sale_address_id=conf.get("after_sale_address_id") or "",
                freight_template_id=conf.get("freight_template_id") or "",
                # 只有默认店才允许回退 .env 的单店时代默认值
                legacy_defaults=(shop_id == shop_registry.default_shop()["shop_id"]),
            )
            _STORES[shop_id] = inst
        return inst

# ------------------------------------------------------------------
# 类目树缓存:微信全量类目有 15000+ 节点,每次现拉要几十秒,
# 批量映射时前端每个商品都触发一次会把流程卡死,所以缓存 6 小时。
# 多店铺：缓存 key 一律以 shop_id 打头，不同店铺的类目/商品数据不混。
# ------------------------------------------------------------------
_CAT_CACHE = {}          # shop_id -> {"nodes": [...], "ts": float}
_CAT_CACHE_TTL = 6 * 3600
_CACHE = {}
_CACHE_LOCK = threading.RLock()


def _cached(shop_id, key, ttl, loader):
    """按 (店铺, 业务key) 缓存。key 形如 ("product_list", status, ...)。"""
    full_key = (shop_id,) + tuple(key)
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(full_key)
        if entry and entry["expires_at"] > now:
            return entry["value"]
    value = loader()
    with _CACHE_LOCK:
        _CACHE[full_key] = {"value": value, "expires_at": time.monotonic() + ttl}
    return value


def _clear_cache(shop_id, *prefixes):
    """清掉某店铺下指定前缀的缓存（不影响其他店铺）。"""
    with _CACHE_LOCK:
        for key in list(_CACHE):
            if key and key[0] == shop_id and len(key) > 1 and key[1] in prefixes:
                _CACHE.pop(key, None)


def _clear_product_cache(shop_id):
    _clear_cache(shop_id, "product_list", "product_detail")


def get_cached_categories(store, shop_id):
    """类目树按店铺缓存（各店类目树可能不同）。"""
    entry = _CAT_CACHE.get(shop_id)
    now = time.time()
    if not entry or entry["nodes"] is None or now - entry["ts"] > _CAT_CACHE_TTL:
        entry = {"nodes": store.get_all_categories(), "ts": now}
        _CAT_CACHE[shop_id] = entry
    return entry["nodes"]


# ==================================================================
# 类目搜索工具(纯逻辑,不涉及网络请求)
# 类目节点获取已内聚到 WxStore.get_all_categories() 里(见
# wechat_store_client.py),这里只负责"搜 + 拼路径"。
# ==================================================================
def search_categories(nodes, keyword):
    """按关键词搜类目,通过 f_cat_id 回溯父级,输出每级都带 ID 的路径。
    命中非叶子类目时,自动把其子树下所有叶子类目一起带出
    (解决"合成钻石"这类父类目名搜得到、叶子名搜不到的问题)。
    返回: [{"cat_id":..., "path":"一级 > 二级 > 三级", "leaf":bool, "chain":[...]}]"""
    by_id = {}
    for n in nodes:
        if isinstance(n, dict) and n.get("cat_id") is not None:
            by_id[str(n["cat_id"])] = n

    # 父类目 -> 子类目 索引,用于往下找叶子
    children_of = {}
    for n in nodes:
        if isinstance(n, dict) and n.get("f_cat_id") is not None:
            children_of.setdefault(str(n["f_cat_id"]), []).append(n)

    def leaf_descendants(node):
        """收集某节点子树下所有叶子类目(递归)"""
        leaves = []
        for c in children_of.get(str(node.get("cat_id")), []):
            if c.get("leaf"):
                leaves.append(c)
            else:
                leaves.extend(leaf_descendants(c))
        return leaves

    def build_result(n):
        """回溯父级拼完整链路,生成接口结果对象"""
        chain, cur, seen = [], n, set()
        while cur and str(cur.get("cat_id")) not in seen:
            seen.add(str(cur.get("cat_id")))
            chain.append((cur.get("cat_id"), cur.get("name", "")))
            pid = cur.get("f_cat_id")
            cur = by_id.get(str(pid)) if pid is not None else None
        chain.reverse()
        return {
            "cat_id": n.get("cat_id"),
            "path": " > ".join(f"{nm}({cid})" for cid, nm in chain),
            "leaf": bool(n.get("leaf")),
            # 最新完整链路(一级→叶子),前端发品时直接转成 cats_v2 数组
            "chain": [{"cat_id": cid, "name": nm} for cid, nm in chain],
        }

    results, seen_ids = [], set()
    for n in nodes:
        if not isinstance(n, dict):
            continue
        # 全字段匹配:不管名称字段叫什么,只要有该关键词就命中
        hay = " ".join(str(v) for v in n.values() if v is not None)
        if keyword not in hay:
            continue
        cid = str(n.get("cat_id"))
        # 命中节点本身(按 cat_id 去重)
        if cid not in seen_ids:
            seen_ids.add(cid)
            results.append(build_result(n))
        # 非叶子节点:把其下所有叶子也带出来,方便直接选叶子发品
        if not n.get("leaf"):
            for leaf in leaf_descendants(n):
                leaf_id = str(leaf.get("cat_id"))
                if leaf_id not in seen_ids:
                    seen_ids.add(leaf_id)
                    results.append(build_result(leaf))
    return results


# ==================================================================
# 通用错误处理:把脚本里抛的 RuntimeError 转成 HTTP 400 返回
# ==================================================================
def ok_or_400(fn, *args, **kwargs):
    """执行 fn,成功返回 {"ok": true, ...},失败抛 HTTP 400(带微信错误信息)"""
    try:
        return {"ok": True, "result": fn(*args, **kwargs)}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"服务内部错误: {e}")


# ==================================================================
# 1. 健康检查(不用凭证,验证服务本身活着)
# ==================================================================
@app.get("/health")
def health():
    return {"status": "ok", "service": "wechat-store-api"}


# ==================================================================
# 2. 测试凭证(最轻量,先确认 AppID/Secret 能不能换到 token)
# ==================================================================
@app.post("/token/test")
def test_token(request: Request):
    shop = shop_of(request)
    store = store_for(shop)
    try:
        tok = store.get_access_token(force=True)   # force=True 强制重新申请
        return {"ok": True, "shop_id": shop["shop_id"],
                "token_prefix": tok[:20] + "...", "expires_in": 7200}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==================================================================
# 3. 查类目(传关键词,多个词用空格分开,如 ?keyword=钻石 戒指)
# ==================================================================
@app.get("/categories")
def categories(request: Request, keyword: str = Query("珠宝", description="关键词,多个词用空格分隔")):
    shop = shop_of(request)
    store = store_for(shop)
    try:
        # 类目节点获取走缓存(内聚在 WxStore.get_all_categories),首次调用才真正请求微信
        nodes = get_cached_categories(store, shop["shop_id"])
        results = []
        for kw in keyword.split():
            results.extend(search_categories(nodes, kw))
        return {"ok": True, "total": len(results), "results": results}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==================================================================
# 4. 上传图片(传图片URL,微信转存后返回 img_url,发品时填进 head_imgs)
# ==================================================================
@app.post("/images/upload")
def upload_image(request: Request, body: dict = Body(..., example={"img_url": "https://你的图床/主图1.jpg"})):
    shop = shop_of(request)
    store = store_for(shop)
    img_url = body.get("img_url")
    if not img_url:
        raise HTTPException(status_code=400, detail="请求体需要 img_url 字段")
    return ok_or_400(store.upload_image, img_url)


@app.post("/images/upload-file")
def upload_image_file(request: Request, body: dict = Body(...)):
    """上传 Excel 中提取出的内嵌图片。"""
    shop = shop_of(request)
    store = store_for(shop)
    encoded = body.get("content_base64")
    if not encoded:
        raise HTTPException(status_code=400, detail="请求体需要 content_base64 字段")
    try:
        content = base64.b64decode(encoded, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="content_base64 编码无效")
    if not content or len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片为空或超过 20MB")
    filename = os.path.basename(str(body.get("filename") or "image.jpg"))[:100]
    return ok_or_400(store.upload_image_bytes, content, filename)


@app.post("/videos/upload-file")
def upload_video_file(request: Request, body: dict = Body(...)):
    """上传本地视频(商品视频),返回微信给的视频临时 URL(发品时填 video_url)。

    微信视频是分块上传(申请→分块→完成→轮询取URL),比图片慢得多;
    轮询等转码最长约 5 分钟,所以前端调用要给足超时。
    """
    shop = shop_of(request)
    store = store_for(shop)
    encoded = body.get("content_base64")
    if not encoded:
        raise HTTPException(status_code=400, detail="请求体需要 content_base64 字段")
    try:
        content = base64.b64decode(encoded, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="content_base64 编码无效")
    if not content:
        raise HTTPException(status_code=400, detail="视频内容为空")
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="视频超过 50MB")
    filename = os.path.basename(str(body.get("filename") or "video.mp4"))[:100]
    return ok_or_400(store.upload_video_bytes, content, filename)


@app.get("/freight-templates")
def freight_templates(request: Request, page_size: int = Query(100, ge=1, le=100), page_num: int = Query(1, ge=1)):
    """读取微信小店运费模板(已逐个查详情补齐模板名称)。
    返回 {"templates": [{"template_id","name","is_default",...}],
          "template_id_list": [...], "total": n}，前端下拉框直接读 templates。"""
    shop = shop_of(request)
    store = store_for(shop)
    shop_id = shop["shop_id"]
    return ok_or_400(
        lambda: _cached(
            shop_id, ("freight_templates", page_size, page_num), 10 * 60,
            lambda: store.get_freight_templates(page_size, page_num),
        )
    )


@app.get("/brand")
def brand_detail(request: Request, brand_id: str = Query("", description="微信品牌 ID")):
    """按品牌 ID 查品牌名（编辑商品页把 10002926 显示成「钻石世家」用）。

    一次请求就能拿到名称，不需要翻品牌库（`/brands` 那个列表接口因品牌库上万条已弃用）。
    无品牌占位值(2100000000)直接返回空，由前端显示"无品牌"。
    """
    if not brand_id or brand_id == "2100000000":
        return {"ok": True, "result": {}}
    shop = shop_of(request)
    store = store_for(shop)
    shop_id = shop["shop_id"]
    return ok_or_400(
        lambda: _cached(
            shop_id, ("brand", str(brand_id)), 12 * 3600,
            lambda: store.get_brand(brand_id),
        )
    )


@app.get("/brands")
def brands(request: Request):
    """微信品牌列表(前端品牌下拉用:运营看到的应该是品牌名,而不是 10002926 这种数字 ID)。

    ⚠️ 微信接口是游标分页、每页 10 条,全量可能上千,所以这里给了长缓存(12 小时):
       首次调用会慢几秒(前端点开下拉时懒加载),之后都命中缓存秒回。
    """
    shop = shop_of(request)
    store = store_for(shop)
    shop_id = shop["shop_id"]
    return ok_or_400(
        lambda: _cached(
            shop_id, ("brands",), 12 * 3600,
            lambda: store.get_all_brands(),
        )
    )


@app.get("/category-detail")
def category_detail(request: Request, cat_id: str = Query(..., description="叶子类目 ID")):
    """查叶子类目的属性(product_attr_list)和规格(sale_attr_list)定义。
    前端选完类目后调用,据其渲染属性录入框和规格维度。
    每个属性含 type_v2(select_one/select_many/string/integer/...)、
    value(候选值列表)、is_required(是否必填)。"""
    shop = shop_of(request)
    store = store_for(shop)
    shop_id = shop["shop_id"]
    return ok_or_400(
        lambda: _cached(
            shop_id, ("category_detail", str(cat_id)), 12 * 3600,
            lambda: store.get_category_detail(cat_id),
        )
    )


# ==================================================================
# 5. 发布商品(只创建草稿!不提交审核、不展示。
#    等类目审核通过后,body 里填真实类目ID就能用)
# ==================================================================
@app.post("/products")
def create_product(request: Request, product: dict = Body(..., examples=[{
    "title": "18K金钻石戒指",
    "short_title": "18K金钻戒",
    "out_product_id": "TEST-001",
    "head_imgs": ["https://你的图床/主图1.jpg"] * 3,
    "cats_v2": [{"cat_id": "新类目树一级ID"}, {"cat_id": "新类目树二级ID"},
                {"cat_id": "叶子类目ID"}],   # 新多级类目树(店铺已全切新树),cat_id 从 /categories 搜索结果的 chain 取
    "brand_id": "2100000000",
    "deliver_method": 0,
    "extra_service": {"seven_day_return": 1, "freight_insurance": 0},
    "skus": [{"out_sku_id": "SKU-001", "sale_price": 1299900,
              "stock_num": 100, "sku_attrs": []}],
}])):
    """发布商品,返回 product_id。注意:只是草稿,需再调 /listing 上架。"""
    shop = shop_of(request)
    store = store_for(shop)
    result = ok_or_400(store.add_product, product)
    _clear_product_cache(shop["shop_id"])
    return result


# ==================================================================
# 6. 更新商品
# ==================================================================
@app.post("/products/{pid}/update")
def update_product(pid: str, request: Request, product: dict = Body(...)):
    shop = shop_of(request)
    store = store_for(shop)
    result = ok_or_400(store.update_product, pid, product)
    _clear_product_cache(shop["shop_id"])
    return result


# ==================================================================
# 7. 上架商品(提交审核,审核通过才开卖)
# ==================================================================
@app.post("/products/{pid}/listing")
def listing_product(pid: str, request: Request):
    shop = shop_of(request)
    store = store_for(shop)
    result = ok_or_400(store.listing, pid)
    _clear_product_cache(shop["shop_id"])
    return result


# ==================================================================
# 8. 下架商品
# ==================================================================
@app.post("/products/{pid}/delisting")
def delisting_product(pid: str, request: Request):
    shop = shop_of(request)
    store = store_for(shop)
    result = ok_or_400(store.delisting, pid)
    _clear_product_cache(shop["shop_id"])
    return result


# ==================================================================
# 9. 删除商品(彻底删除,不可恢复;日常建议用下架代替)
# ==================================================================
@app.delete("/products/{pid}")
def delete_product(pid: str, request: Request):
    shop = shop_of(request)
    store = store_for(shop)
    result = ok_or_400(store.delete_product, pid)
    _clear_product_cache(shop["shop_id"])
    return result


# ==================================================================
# 10. 查询商品/审核状态
# ==================================================================
@app.get("/products/{pid}")
def get_product(pid: str, request: Request):
    shop = shop_of(request)
    store = store_for(shop)
    shop_id = shop["shop_id"]
    return ok_or_400(
        lambda: _cached(
            shop_id, ("product_detail", str(pid)), 60,
            lambda: store.get_product(pid),
        )
    )


# ==================================================================
# 11. 查询商品列表(按状态过滤,next_key 游标翻页) —— 前端商品列表页用
# ==================================================================
@app.get("/products")
def list_products(request: Request,
                  status: int = Query(None, description="0=未上架 1=已上架 2=已下架,不传查全部"),
                  page_size: int = Query(10, ge=1, le=30, description="每页条数,最大30"),
                  next_key: str = Query(None, description="上一页返回的翻页游标,第一页不传")):
    """商品列表(游标翻页)。返回 {"products": [...], "next_key": 下一页游标, "total": 总数}"""
    shop = shop_of(request)
    store = store_for(shop)
    shop_id = shop["shop_id"]
    return ok_or_400(
        lambda: _cached(
            shop_id, ("product_list", status, page_size, str(next_key or "")), 30,
            lambda: store.list_products(status, page_size, next_key),
        )
    )


# ==================================================================
# 启动入口:python fastapi_server.py 直接跑
# ==================================================================
if __name__ == "__main__":
    import uvicorn
    # host 默认 127.0.0.1（仅本机/前端代理访问）；确需局域网直连时设 WX_API_HOST=0.0.0.0
    # 并配好 API_KEY。port 默认 8000,可改。
    uvicorn.run(app, host=API_HOST, port=API_PORT)
