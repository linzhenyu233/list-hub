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
import threading
import time
from fastapi import FastAPI, HTTPException, Body, Query
from fastapi.middleware.cors import CORSMiddleware

# 复用之前写好的微信小店客户端(脚本里的类,直接拿来用)
from wechat_store_client import WxStore

# ------------------------------------------------------------------
# 配置:凭证从环境变量读(别写死在代码里,密钥泄露=店铺钥匙被拿走)
# ------------------------------------------------------------------
APPID = os.environ.get("WX_APPID", "")
SECRET = os.environ.get("WX_SECRET", "")
API_HOST = os.environ.get("WX_API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("WX_API_PORT", "8000"))
CORS_ORIGINS = [item.strip() for item in os.environ.get("CORS_ORIGINS", "*").split(",") if item.strip()]

# 创建 FastAPI 应用(标题/版本会在 /docs 文档页显示)
app = FastAPI(title="微信小店自动上链接服务", version="0.1.0")

# CORS:允许前端页面跨域调用(开发期全放行,上线换成前端具体域名)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局唯一的客户端实例:所有请求共用它,access_token 缓存就不会反复失效
store = WxStore(APPID, SECRET)

# ------------------------------------------------------------------
# 类目树缓存:微信全量类目有 15000+ 节点,每次现拉要几十秒,
# 批量映射时前端每个商品都触发一次会把流程卡死,所以缓存 6 小时。
# ------------------------------------------------------------------
_CAT_CACHE = {"nodes": None, "ts": 0.0}
_CAT_CACHE_TTL = 6 * 3600
_CACHE = {}
_CACHE_LOCK = threading.RLock()


def _cached(key, ttl, loader):
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and entry["expires_at"] > now:
            return entry["value"]
    value = loader()
    with _CACHE_LOCK:
        _CACHE[key] = {"value": value, "expires_at": time.monotonic() + ttl}
    return value


def _clear_cache(*prefixes):
    with _CACHE_LOCK:
        for key in list(_CACHE):
            if key and key[0] in prefixes:
                _CACHE.pop(key, None)


def _clear_product_cache():
    _clear_cache("product_list", "product_detail")


def get_cached_categories():
    now = time.time()
    if _CAT_CACHE["nodes"] is None or now - _CAT_CACHE["ts"] > _CAT_CACHE_TTL:
        _CAT_CACHE["nodes"] = store.get_all_categories()
        _CAT_CACHE["ts"] = now
    return _CAT_CACHE["nodes"]


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
def test_token():
    if not APPID.startswith("wx"):
        raise HTTPException(status_code=400,
                            detail="未设置 WX_APPID/WX_SECRET 环境变量,请先设置再启动服务")
    try:
        tok = store.get_access_token(force=True)   # force=True 强制重新申请
        return {"ok": True, "token_prefix": tok[:20] + "...", "expires_in": 7200}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==================================================================
# 3. 查类目(传关键词,多个词用空格分开,如 ?keyword=钻石 戒指)
# ==================================================================
@app.get("/categories")
def categories(keyword: str = Query("珠宝", description="关键词,多个词用空格分隔")):
    try:
        # 类目节点获取走缓存(内聚在 WxStore.get_all_categories),首次调用才真正请求微信
        nodes = get_cached_categories()
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
def upload_image(body: dict = Body(..., example={"img_url": "https://你的图床/主图1.jpg"})):
    img_url = body.get("img_url")
    if not img_url:
        raise HTTPException(status_code=400, detail="请求体需要 img_url 字段")
    return ok_or_400(store.upload_image, img_url)


@app.post("/images/upload-file")
def upload_image_file(body: dict = Body(...)):
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
    filename = os.path.basename(str(body.get("filename") or "image.jpg"))[:100]
    return ok_or_400(store.upload_image_bytes, content, filename)


@app.get("/freight-templates")
def freight_templates(page_size: int = Query(100, ge=1, le=100), page_num: int = Query(1, ge=1)):
    """读取微信小店运费模板(已逐个查详情补齐模板名称)。
    返回 {"templates": [{"template_id","name","is_default",...}],
           "template_id_list": [...], "total": n}，前端下拉框直接读 templates。"""
    return ok_or_400(
        lambda: _cached(
            ("freight_templates", page_size, page_num), 10 * 60,
            lambda: store.get_freight_templates(page_size, page_num),
        )
    )


@app.get("/category-detail")
def category_detail(cat_id: str = Query(..., description="叶子类目 ID")):
    """查叶子类目的属性(product_attr_list)和规格(sale_attr_list)定义。
    前端选完类目后调用,据其渲染属性录入框和规格维度。
    每个属性含 type_v2(select_one/select_many/string/integer/...)、
    value(候选值列表)、is_required(是否必填)。"""
    return ok_or_400(
        lambda: _cached(
            ("category_detail", str(cat_id)), 12 * 3600,
            lambda: store.get_category_detail(cat_id),
        )
    )


# ==================================================================
# 5. 发布商品(只创建草稿!不提交审核、不展示。
#    等类目审核通过后,body 里填真实类目ID就能用)
# ==================================================================
@app.post("/products")
def create_product(product: dict = Body(..., examples=[{
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
    result = ok_or_400(store.add_product, product)
    _clear_product_cache()
    return result


# ==================================================================
# 6. 更新商品
# ==================================================================
@app.post("/products/{pid}/update")
def update_product(pid: str, product: dict = Body(...)):
    result = ok_or_400(store.update_product, pid, product)
    _clear_product_cache()
    return result


# ==================================================================
# 7. 上架商品(提交审核,审核通过才开卖)
# ==================================================================
@app.post("/products/{pid}/listing")
def listing_product(pid: str):
    result = ok_or_400(store.listing, pid)
    _clear_product_cache()
    return result


# ==================================================================
# 8. 下架商品
# ==================================================================
@app.post("/products/{pid}/delisting")
def delisting_product(pid: str):
    result = ok_or_400(store.delisting, pid)
    _clear_product_cache()
    return result


# ==================================================================
# 9. 删除商品(彻底删除,不可恢复;日常建议用下架代替)
# ==================================================================
@app.delete("/products/{pid}")
def delete_product(pid: str):
    result = ok_or_400(store.delete_product, pid)
    _clear_product_cache()
    return result


# ==================================================================
# 10. 查询商品/审核状态
# ==================================================================
@app.get("/products/{pid}")
def get_product(pid: str):
    return ok_or_400(
        lambda: _cached(
            ("product_detail", str(pid)), 60,
            lambda: store.get_product(pid),
        )
    )


# ==================================================================
# 11. 查询商品列表(按状态过滤,next_key 游标翻页) —— 前端商品列表页用
# ==================================================================
@app.get("/products")
def list_products(status: int = Query(None, description="0=未上架 1=已上架 2=已下架,不传查全部"),
                  page_size: int = Query(10, ge=1, le=30, description="每页条数,最大30"),
                  next_key: str = Query(None, description="上一页返回的翻页游标,第一页不传")):
    """商品列表(游标翻页)。返回 {"products": [...], "next_key": 下一页游标, "total": 总数}"""
    return ok_or_400(
        lambda: _cached(
            ("product_list", status, page_size, str(next_key or "")), 30,
            lambda: store.list_products(status, page_size, next_key),
        )
    )


# ==================================================================
# 启动入口:python fastapi_server.py 直接跑
# ==================================================================
if __name__ == "__main__":
    import uvicorn
    # host=0.0.0.0 允许局域网/其他机器访问;port 默认 8000,可改
    uvicorn.run(app, host=API_HOST, port=API_PORT)
