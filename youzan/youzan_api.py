# -*- coding: utf-8 -*-
"""
====================================================================
 有赞店铺 · FastAPI 服务（端口 8030）
====================================================================
把 youzan_store 包装成 HTTP 接口，路由与 xhs/xhs_api.py **一一对应**，
使前端与中台（bulk_api）无需为有赞写特例。

接口一览：
  ── 系统 / Token ────────────────────────────────────────────
    GET  /health                            健康检查（探活）
    GET  /token/info                        当前店铺 token 状态（剩余天数）
    POST /token/code                        换取/续期 token（自用型无 code，入参可空）
    POST /token/refresh                     一键续期（幂等，剩余>1h 不换发）
  ── 发品页 ──────────────────────────────────────────────────
    GET  /categories                        商品类目（叶子类目 = 发布用的 cid）
    GET  /delivery-templates                运费模板下拉
    GET  /shop                              店铺信息（连通性自检）
    POST /materials/upload                  传图 {"url":"..."} → image_id
    POST /materials/upload-file             传本地文件 {"content_base64":"..."} → image_id
    POST /products                          创建商品（含 SKU）
  ── 商品编辑 / 列表 ─────────────────────────────────────────
    GET  /products/custom?item_no=          按商品编码反查（ID 回填用）
    GET  /skus/custom?sku_no=               按规格编码反查
    GET  /items?state=onsale|inventory      商品列表（出售中 / 仓库中）
    GET  /products/{item_id}                商品详情
    PUT  /products/{item_id}                改商品
    PUT  /skus/{sku_id}                     改 SKU（含改价）
    POST /skus/{sku_id}/quantity            改库存（增量/全量，不改价）
    POST /products/{item_id}/listing        上下架 {"listing": 1|0}
  ── 订单（只读，数据面板用）────────────────────────────────
    GET  /orders?start=&end=                订单列表
    GET  /order-detail?order_id=            订单详情

⚠️ 有赞与微信/小红书的三个关键差异（详见 docs/有赞平台适配实施方案.md）：
  1. 凭证是三件套 client_id / client_secret / kdt_id；
  2. 没有 refresh_token，access_token 7 天过期后静默重取（官方：1 小时内重复
     请求返回同一个 token，所以"剩余<1 小时就重取"是安全的、无副作用的）；
  3. 价格单位是**元**（不是分），换算由中台的 platform_registry.price_unit 负责。

启动方式：
    python youzan_api.py
    # 或: uvicorn youzan_api:app --host 127.0.0.1 --port 8030
浏览器打开 http://127.0.0.1:8030/docs 可看接口文档并在线调试。
====================================================================
"""
import os
import sys
import json
import time
import base64

from fastapi import FastAPI, HTTPException, Body, Query, Request
from fastapi.middleware.cors import CORSMiddleware

# 复用客户端类（凭证按店铺从 shop_registry 取）
from youzan_store import YouzanStore, YouzanError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import runtime_config
from runtime_config import load_project_env
from app_version import APP_VERSION
import shop_registry
import api_auth
import mock_platform        # 三级模式（mock/sandbox/live）：网关出口统一走它

load_project_env()

# ------------------------------------------------------------------
# 配置：token 持久化（每店一个文件，重启不丢）
# ------------------------------------------------------------------
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_DIR = os.environ.get("YOUZAN_TOKEN_DIR", os.path.join(_BASE_DIR, "tokens"))
API_HOST = os.environ.get("YOUZAN_API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("YOUZAN_API_PORT", "8030"))
CORS_ORIGINS = [item.strip() for item in os.environ.get("CORS_ORIGINS", "*").split(",") if item.strip()]

# 提前重取 token 的阈值（秒）：官方说明 1 小时内重复请求返回同一个 token，
# 所以"剩不到 1 小时就重取"既不会浪费配额，也不会用到边界上的过期 token。
TOKEN_RENEW_BEFORE = 3600

app = FastAPI(title="有赞运营后台服务", version=APP_VERSION)

# 鉴权：X-API-Key（未配置 API_KEY 时只告警不拦截）。必须在 CORS 之前装。
api_auth.install(app, "youzan-api")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================================================================
# 多店铺：店铺解析 + 凭证
# ==================================================================
def shop_of(request: Request) -> dict:
    """解析当前店铺：读 X-Shop-Id 头；不传则用默认店（兼容旧调用）。"""
    shop_id = (request.headers.get("x-shop-id") or "").strip() if request is not None else ""
    try:
        return shop_registry.resolve(shop_id or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def youzan_conf_of(shop: dict) -> dict:
    """取店铺的有赞配置（client_id/client_secret/kdt_id/access_token 等）。

    ⚠️ mock 模式下**允许凭证为空**：这是"软著/应用审核通过前也能把
    导入→映射→发布→状态全链路跑通"的前提（见 docs/有赞平台适配实施方案.md §8）。
    非 mock 模式缺凭证则立刻报错，而不是发一个注定鉴权失败的请求出去。
    """
    conf = shop.get("youzan") or {}
    if not (conf.get("client_id") or ""):
        if mock_platform.resolve_mode("youzan", shop["shop_id"]) == mock_platform.MOCK:
            print(f"[youzan-api] WARN: 店铺 {shop['shop_id']} 未配有赞凭证，"
                  f"当前为 mock 模式，按占位处理（不会发出外网请求）", flush=True)
            return conf
        raise HTTPException(
            status_code=400,
            detail=f"店铺 {shop['shop_id']} 未配置有赞凭证（需要 client_id/client_secret/kdt_id）",
        )
    return conf


def _token_file(shop_id: str) -> str:
    return os.path.join(TOKEN_DIR, f"{shop_id}.json")


def _load_token(shop_id: str) -> dict:
    """从该店铺的 token 文件读；没有/损坏返回空 dict（并留痕）。"""
    path = _token_file(shop_id)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as exc:
            # 静默 return {} 会把"token 文件损坏"变成"从没授权过"，排查不到根因
            print(f"[youzan-api] WARN: 店铺 {shop_id} 的 token 文件不可读({path}): {exc}", flush=True)
    return {}


def _save_token(shop_id: str, tok: dict):
    os.makedirs(TOKEN_DIR, exist_ok=True)
    with open(_token_file(shop_id), "w", encoding="utf-8") as handle:
        json.dump(tok, handle, ensure_ascii=False, indent=2)


def _renew_token(sid: str, conf: dict) -> str:
    """静默换取/续期 access_token 并落盘（自用型应用无 refresh_token）。"""
    store = YouzanStore(conf.get("client_id"), conf.get("client_secret"),
                        conf.get("kdt_id"), shop_id=sid)
    data = store.request_token()
    tok = _load_token(sid)
    tok["accessToken"] = store.access_token
    tok["expireAt"] = _expire_at_from(data)
    tok["kdtId"] = conf.get("kdt_id") or tok.get("kdtId") or ""
    _save_token(sid, tok)
    return store.access_token


def _expire_at_from(data: dict) -> int:
    """把授权接口返回的过期信息统一成**秒级绝对时间戳**。

    真机返回的 `expires` 是绝对毫秒时间戳（如 1792649474000 = 7 天后）；
    文档/mock 里也见过 `expires_in`（距现在多少秒）。两种都要能吃下，
    否则会出现"刚拿到的 token 立刻被判过期，每次都重取"或"过期了还在用"。
    判据：>1e12 视为毫秒时间戳（秒级时间戳要到公元 33658 年才会那么大）。
    """
    raw = data.get("expires", data.get("expires_in", data.get("expiresIn")))
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        value = 0
    if value > 10 ** 12:
        return value // 1000
    if value > 0:
        return int(time.time()) + value
    return int(time.time()) + 7 * 24 * 3600     # 有赞自用型固定 7 天


def _get_access_token(sid: str, conf: dict) -> str:
    """取当前 access_token（文件优先，其次店铺配置），快到期时自动重取。"""
    tok = _load_token(sid)
    access = tok.get("accessToken") or conf.get("access_token")
    expire_at = int(tok.get("expireAt") or 0)
    if not access or (expire_at and time.time() > expire_at - TOKEN_RENEW_BEFORE):
        try:
            access = _renew_token(sid, conf)
        except Exception as exc:
            if not access:
                raise HTTPException(status_code=401, detail=f"有赞 token 获取失败: {exc}")
            # 重取失败但手上还有未过期的 token：继续用，但必须留痕
            print(f"[youzan-api] WARN: 店铺 {sid} 重新获取 token 失败（沿用旧 token）: {exc}", flush=True)
    return access


# ==================================================================
# 统一请求封装：按店铺取凭证 → 换 token → 归一化错误 → 401 自动重试一次
# ==================================================================
def _youzan_call(api=None, payload: dict = None, shop_id=None, *, retry_safe=None, invoke=None):
    """调有赞接口。成功返回 data；失败抛 HTTP 400（鉴权类抛 401）。

    - api/payload: 常规用法，直接指定接口与业务参数
    - invoke:      需要自己组装调用时用（如订单时间窗），入参是已建好的 store
    """
    shop = shop_registry.resolve(shop_id or None) if not isinstance(shop_id, dict) else shop_id
    sid = shop["shop_id"]
    conf = youzan_conf_of(shop)
    access = _get_access_token(sid, conf)

    def build(token):
        return YouzanStore(conf.get("client_id"), conf.get("client_secret"),
                           conf.get("kdt_id"), token, shop_id=sid)

    def run(store):
        if invoke is not None:
            return invoke(store)
        return store._call(api, payload or {}, retry_safe=retry_safe)

    try:
        return run(build(access))
    except YouzanError as exc:
        if exc.is_auth_expired:
            # token 失效：重取一次再试（自用型重取无副作用，不会换出第二个 token）
            try:
                access = _renew_token(sid, conf)
                return run(build(access))
            except YouzanError as retry_exc:
                raise HTTPException(status_code=401, detail=f"有赞授权已失效，自动续期后仍失败: {retry_exc}")
            except HTTPException:
                raise
            except Exception as retry_exc:
                raise HTTPException(status_code=401, detail=f"有赞授权已失效，自动续期失败: {retry_exc}")
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"服务内部错误: {exc}")


# ==================================================================
# 系统 / Token
# ==================================================================
@app.get("/health")
def health():
    return {"status": "ok", "service": "youzan-store-api", "platform": "youzan",
            "mode": mock_platform.resolve_mode("youzan")}


@app.get("/token/info")
def token_info(request: Request):
    """当前店铺 token 状态：是否已配置、过期时间（前端提醒运营用）。"""
    shop = shop_of(request)
    youzan_conf_of(shop)
    sid = shop["shop_id"]
    tok = _load_token(sid)
    expire_at = int(tok.get("expireAt") or 0)
    now = int(time.time())
    remain = max(0, expire_at - now) if expire_at else None
    return {
        "ok": True,
        "shop_id": sid,
        "configured": bool(tok.get("accessToken")),
        "kdt_id": tok.get("kdtId") or (shop.get("youzan") or {}).get("kdt_id") or "",
        "expire_at": expire_at,
        "remain_seconds": remain,
        "remain_days": round(remain / 86400, 2) if remain is not None else None,
        # 有赞自用型应用**没有** refresh_token，永远靠静默重取
        "has_refresh_token": False,
    }


@app.post("/token/code")
def token_by_code(request: Request, body: dict = Body(default=None,
                                                     example={"code": "（自用型应用可留空）"})):
    """换取 token。

    有赞自用型应用**没有授权 code**（grant_type=silent 直接换），
    保留这个路径是为了让前端/SOP 流程（POST /token/code）在小红书与有赞之间保持一致。
    """
    shop = shop_of(request)
    conf = youzan_conf_of(shop)
    sid = shop["shop_id"]
    access = _renew_token(sid, conf)
    tok = _load_token(sid)
    return {"ok": True, "shop_id": sid,
            "result": {"accessToken": access, "expireAt": tok.get("expireAt"),
                       "kdtId": tok.get("kdtId"),
                       "note": "有赞自用型应用为静默授权，无 refresh_token（过期自动重取）"}}


@app.post("/token/refresh")
def token_refresh(request: Request):
    """一键续期。有赞官方说明：1 小时内重复请求返回同一个 token（幂等）。"""
    shop = shop_of(request)
    conf = youzan_conf_of(shop)
    sid = shop["shop_id"]
    tok = _load_token(sid)
    remain = int(tok.get("expireAt") or 0) - int(time.time())
    if tok.get("accessToken") and remain > TOKEN_RENEW_BEFORE:
        # 剩余充足就不换：避免运营误以为"点一下才有效"
        return {"ok": True, "refreshed": False,
                "result": {"expireAt": tok.get("expireAt"),
                           "remain_days": round(remain / 86400, 2),
                           "message": f"token 仍有效（剩余 {round(remain / 86400, 2)} 天），未换发新 token"}}
    access = _renew_token(sid, conf)
    tok = _load_token(sid)
    return {"ok": True, "refreshed": True,
            "result": {"accessToken": access, "expireAt": tok.get("expireAt")}}


# ==================================================================
# 发品页：公共参数查询
# ==================================================================
def _extract_list(data, *keys) -> list:
    """从平台返回里取列表（★各接口结构待真机核对，这里对多种写法做兼容）。"""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _flatten_category_rows(rows, parent_cid="", level=1) -> list:
    """把类目树压成"带 parent_cid 的平铺列表"。

    ★有赞 ``youzan.itemcategories.get`` 官方称为"二维列表"，社区实现里见过两种形状：
    嵌套 ``sub_categories``，或单层 + 每行自带 ``parent_cid``。两种都归一成平铺，
    前端才能按 parent_cid 逐级下钻（否则第二级只能拿到跟第一级一样的全量）。
    """
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        cid = str(item.get("cid") or item.get("id") or "")
        # 每行同时给 cid 与 id、parent_cid 与 parent_id，前端读哪个字段都对
        item["cid"] = item["id"] = item.get("id") or item.get("cid") or cid
        item["parent_cid"] = str(item.get("parent_cid") or item.get("parent_id") or parent_cid or "")
        item["parent_id"] = item["parent_cid"]
        item["level"] = level
        item["name"] = item.get("name") or item.get("category_name") or ""
        raw_leaf = item.get("is_leaf", item.get("isLeaf", item.get("leaf")))
        children = row.get("sub_categories") or row.get("subCategories") or row.get("children") or []
        if raw_leaf is None:
            # ⚠️ 2026-10-15 真机（云测试店铺）：`is_leaf` 不存在，有赞给的是 **`is_parent`**。
            #    所以顺序是：is_leaf 系 → is_parent 取反 → 由有无子级推断。
            raw_parent = item.get("is_parent", item.get("isParent"))
            if raw_parent is None:
                leaf = not list(children)
            else:
                leaf = str(raw_parent).strip().lower() not in ("1", "true")
        else:
            leaf = str(raw_leaf).strip().lower() in ("1", "true")
        item["is_leaf"] = leaf
        # ⚠️ 2026-10-15 真机（云测试店铺）：平台把根节点**又当成自己的子节点**返回了一次
        #    （女人 1000000 的子级还是 女人 1000000）。留着有两个后果：
        #      · 一级下拉里同一个类目重复出现（前端第一级拿的是全量平铺列表）；
        #      · 按 parent_id 下钻永远返回自己 —— 死循环，永远走不到叶子（测试店铺"选不出类目"）。
        #    自引用行没有信息量，丢掉；但它的真子级仍要继续展开。
        if item["parent_cid"] and item["parent_cid"] == cid:
            if list(children):
                out.extend(_flatten_category_rows(children, cid, level + 1))
            continue
        out.append(item)
        if list(children):
            out.extend(_flatten_category_rows(children, cid, level + 1))
    return out


@app.get("/categories")
def categories(request: Request, parent_id: str = ""):
    """商品类目树（发布商品的 ``cid`` 取叶子类目）。

    ``parent_id`` 为空返回全量平铺列表；传上级 cid 只返回它的直接子级。
    """
    sid = shop_of(request)["shop_id"]
    data = _youzan_call(YouzanStore.API_ITEM_CATEGORIES, {}, shop_id=sid, retry_safe=True)
    rows = _extract_list(data, "categories", "item_categories", "category_list")
    flat = _flatten_category_rows(rows)
    if parent_id:
        flat = [row for row in flat if str(row.get("parent_cid")) == str(parent_id)]
    return {"ok": True, "total": len(flat), "result": flat}


@app.get("/delivery-templates")
def delivery_templates(request: Request):
    """运费模板列表（发布商品的 ``delivery_template_id``）。

    ✅ 2026-10-15 真机核对（云测试店铺）：
      - 接口名 `youzan.logistics.template.search` 3.0.0 **正确**（其他候选名一律 `4005 非法API`）；
      - 传空 body 就能拿到全部：`{"paginator": {"total_count": 1, ...}, "items": [...]}`；
      - 模板对象：`{"id": 3689803, "name": "测试", "is_free_delivery": 1, "use_count": 1, ...}`，
        id 键就是 **`id`**（与商品详情 `delivery_template_info.delivery_template_id` 对得上）。
      ★ 之前返回空是因为 `_extract_list` 的键里没有 `items` —— 列表在 items 下。
    """
    sid = shop_of(request)["shop_id"]
    data = _youzan_call(YouzanStore.API_DELIVERY_TEMPLATE, {}, shop_id=sid, retry_safe=True)
    rows = _extract_list(data, "items", "templates", "delivery_templates", "list")
    total = None
    if isinstance(data, dict):
        paginator = data.get("paginator")
        if isinstance(paginator, dict):
            total = paginator.get("total_count")
    return {"ok": True, "total": total if total is not None else len(rows), "result": rows}


@app.get("/shop")
def shop_info(request: Request):
    """店铺信息（连通性自检：凭证不对时这里最先报错）。★接口名待核对。"""
    sid = shop_of(request)["shop_id"]
    return {"ok": True, "result": _youzan_call(YouzanStore.API_SHOP_GET, {}, shop_id=sid,
                                               retry_safe=True)}


# ==================================================================
# 发品页：图片素材（必须先传素材拿 image_id，商品接口只收 image_ids）
# ==================================================================
@app.post("/materials/upload")
def upload_material(request: Request, body: dict = Body(..., example={"url": "https://你的图床/主图1.jpg"})):
    """传图：下载公网图片 → 上传有赞素材 → 返回 image_id（填进商品 image_ids）。"""
    sid = shop_of(request)["shop_id"]
    url = body.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="请求体需要 url 字段")
    try:
        # 下载外链不算"调平台网关"，可以直接用 requests（与 xhs_api 同做法）
        import requests
        content = requests.get(url, timeout=30).content
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"下载图片失败: {exc}")
    if not content:
        raise HTTPException(status_code=400, detail="图片内容为空")
    name = os.path.basename(str(url).split("?")[0])[:40] or "image.jpg"
    data = _youzan_call(invoke=lambda store: store.upload_image_bytes(content, name),
                        shop_id=sid)
    return {"ok": True, "result": data}


@app.post("/materials/upload-file")
def upload_material_file(request: Request, body: dict = Body(...)):
    """上传本地文件（Excel 内嵌图 / 前端选图，内容走 base64）→ image_id。"""
    sid = shop_of(request)["shop_id"]
    encoded = body.get("content_base64")
    if not encoded:
        raise HTTPException(status_code=400, detail="请求体需要 content_base64 字段")
    try:
        content = base64.b64decode(encoded, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="content_base64 编码无效")
    if not content:
        raise HTTPException(status_code=400, detail="文件内容为空")
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片超过 20MB")
    name = os.path.basename(str(body.get("filename") or "image.jpg"))[:40]
    data = _youzan_call(invoke=lambda store: store.upload_image_bytes(content, name),
                        shop_id=sid)
    return {"ok": True, "result": data}


# ==================================================================
# 商品：创建 / 编辑 / 编码反查
# ==================================================================
@app.post("/products")
def create_product(request: Request, body: dict = Body(..., examples=[{
    "title": "商品标题",
    "desc": "商品描述（必填，>5 个字符）",
    "image_ids": "img123456,img789012",       # 逗号分隔，≤15 张；先用 /materials/upload 拿
    "cid": "叶子类目ID",                       # /categories 查
    "delivery_template_id": "运费模板ID",       # /delivery-templates 查
    "price": 199.00,                           # ⚠️ 单位：元（不是分）
    "quantity": 100,
    "item_no": "货号/商品编码",                 # 用于 ID 回填与重发去重
    "sku_list": [{"price": 199.00, "quantity": 100, "sku_no": "规格编码"}],
}])):
    """创建商品（SKU 随商品一起创建）。

    ⚠️ body 的字段名与结构是待核对项（有赞 item.create 3.0.0），
    中台侧由 `_youzan_payload` 组装；这里只做透传，联调时对着官方文档逐字核对。
    """
    sid = shop_of(request)["shop_id"]
    if not body:
        raise HTTPException(status_code=400, detail="请求体不能为空")
    data = _youzan_call(YouzanStore.API_ITEM_CREATE, body, shop_id=sid)
    return {"ok": True, "result": data}


@app.get("/products/custom")
def product_by_code(request: Request, item_no: str = Query(..., description="商家商品编码")):
    """按商品编码反查商品（ID 回填、重发去重靠它）。

    ⚠️ 注意路由顺序：本路由必须定义在 ``/products/{item_id}`` 之前，
    否则 FastAPI 会把 "custom" 当成 item_id 匹配掉。
    """
    sid = shop_of(request)["shop_id"]
    data = _youzan_call(YouzanStore.API_ITEM_BY_CODE, {"item_no": str(item_no)},
                        shop_id=sid, retry_safe=True)
    rows = _extract_list(data, "items", "item_list") or ([data] if data else [])
    return {"ok": True, "result": rows}


@app.get("/skus/custom")
def sku_by_code(request: Request, sku_no: str = Query(..., description="商家规格编码")):
    """按规格编码反查 SKU（最多返回 600 条）。"""
    sid = shop_of(request)["shop_id"]
    data = _youzan_call(YouzanStore.API_SKU_BY_CODE, {"sku_no": str(sku_no)},
                        shop_id=sid, retry_safe=True)
    rows = _extract_list(data, "skus", "sku_list") or ([data] if data else [])
    return {"ok": True, "result": rows}


@app.get("/items")
def list_items(request: Request,
               state: str = Query("onsale", description="onsale=出售中 inventory=仓库中"),
               page_no: int = Query(1, ge=1),
               page_size: int = Query(20, ge=1, le=100)):
    """商品列表（出售中 / 仓库中）。"""
    sid = shop_of(request)["shop_id"]
    api = YouzanStore.API_ITEMS_ONSALE if state == "onsale" else YouzanStore.API_ITEMS_INVENTORY
    data = _youzan_call(api, {"page_no": int(page_no), "page_size": int(page_size)},
                        shop_id=sid, retry_safe=True)
    return {"ok": True, "result": data}


@app.get("/products/{item_id}")
def get_product(item_id: str, request: Request):
    """商品详情。"""
    sid = shop_of(request)["shop_id"]
    return {"ok": True, "result": _youzan_call(YouzanStore.API_ITEM_GET,
                                               {"item_id": str(item_id)},
                                               shop_id=sid, retry_safe=True)}


@app.put("/products/{item_id}")
def update_product(item_id: str, request: Request, body: dict = Body(...)):
    """改商品（透传，字段同 item.update）。"""
    sid = shop_of(request)["shop_id"]
    payload = {"item_id": str(item_id), **{k: v for k, v in (body or {}).items()
                                           if k not in ("item_id",)}}
    return {"ok": True, "result": _youzan_call(YouzanStore.API_ITEM_UPDATE, payload, shop_id=sid)}


@app.put("/skus/{sku_id}")
def update_sku(sku_id: str, request: Request, body: dict = Body(...)):
    """改 SKU（含改价）。item_id 由调用方在 body 里带上（有赞要求）。"""
    sid = shop_of(request)["shop_id"]
    payload = {"sku_id": str(sku_id), **{k: v for k, v in (body or {}).items()
                                         if k not in ("sku_id",)}}
    if not payload.get("item_id"):
        raise HTTPException(status_code=400, detail="有赞改 SKU 需要 item_id（放在 body 里）")
    return {"ok": True, "result": _youzan_call(YouzanStore.API_SKU_UPDATE, payload, shop_id=sid)}


@app.post("/skus/{sku_id}/quantity")
def update_quantity(sku_id: str, request: Request, body: dict = Body(..., example={
    "item_id": "商品ID", "quantity": 100, "type": 1})):
    """改库存（type: 1=全量 2=增量；该接口**不能改价**）。"""
    sid = shop_of(request)["shop_id"]
    payload = {"sku_id": str(sku_id), **{k: v for k, v in (body or {}).items()
                                         if k not in ("sku_id",)}}
    return {"ok": True, "result": _youzan_call(YouzanStore.API_QUANTITY_UPDATE, payload, shop_id=sid)}


@app.post("/products/{item_id}/listing")
def set_listing(item_id: str, request: Request, body: dict = Body(..., example={"listing": 1})):
    """上下架。listing: 1=上架 0=下架（★接口名待核对）。"""
    sid = shop_of(request)["shop_id"]
    listing = body.get("listing")
    if listing not in (0, 1, "0", "1", True, False):
        raise HTTPException(status_code=400, detail="listing 必须是 1（上架）或 0（下架）")
    api = YouzanStore.API_ITEM_LISTING if str(listing) in ("1", "True") else YouzanStore.API_ITEM_DELISTING
    return {"ok": True, "result": _youzan_call(api, {"item_id": str(item_id)}, shop_id=sid)}


# ==================================================================
# 订单（只读）—— 中台「数据面板」用
#   ⚠️ 有赞官方建议：交易后间隔 30 秒以上再查；列表返回为空可以重试。
#   ⚠️ 收件人姓名/电话/地址、买家 ID 等隐私一律不透出（buyer_key 只在同步当天内存去重）。
# ==================================================================
@app.get("/orders")
def list_orders(request: Request,
                start: str = Query(..., description="起始时间（秒级时间戳或 'YYYY-MM-DD HH:MM:SS'）"),
                end: str = Query(..., description="结束时间（同上）"),
                by: str = Query("created", description="created=按下单时间 update=按更新时间"),
                page_no: int = Query(1, ge=1),
                page_size: int = Query(20, ge=1, le=100)):
    """订单列表（只读）。金额单位待真机复核。"""
    sid = shop_of(request)["shop_id"]
    data = _youzan_call(
        shop_id=sid,
        invoke=lambda store: store.list_orders(start, end, page_no=page_no,
                                               page_size=page_size, by=by),
    )
    rows = _extract_list(data, "orders", "full_order_info_list", "list")
    return {"ok": True, "result": {
        "orders": [YouzanStore.summarize_order(row) for row in rows],
        "total": (data or {}).get("total_results") if isinstance(data, dict) else len(rows),
        "page_no": page_no,
        "page_size": page_size,
        "note": "金额单位与时间格式待真机核对（见 youzan_store.summarize_order）",
    }}


@app.get("/order-detail")
def order_detail(request: Request, order_id: str = Query(..., description="订单号 tid")):
    """订单详情（只读）。"""
    sid = shop_of(request)["shop_id"]
    data = _youzan_call(YouzanStore.API_TRADE_GET, {"tid": str(order_id)},
                        shop_id=sid, retry_safe=True)
    return {"ok": True, "result": YouzanStore.summarize_order_detail(data)}


# ==================================================================
# 启动入口：python youzan_api.py（端口 8030，避开微信 8000 / 小红书 8010 / 中台 8020）
# ==================================================================
if __name__ == "__main__":
    # DEV_RELOAD=1 时开热重载（开发机用）；未设置时行为与生产一致
    runtime_config.run_service(app, "youzan_api", API_HOST, API_PORT,
                               reload_dirs=[os.path.dirname(os.path.abspath(__file__))])
