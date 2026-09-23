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

上下架与审核状态说明(前端列表页"售卖中/未在售"怎么来):
    ⚠️ 2026-09-17 实测纠正: 曾经把 skus[].buyable 当作"审核是否通过"的依据并据此拦截上架,
       结果运营在小红书后台手动上架成功后, 接口返回的 buyable 仍然是 false(三个商品、所有规格),
       而商品确实已经可售 —— 说明 buyable 不能用来判断"能否上架"。
       现行做法: 前端不做拦截, 直接调 product.updateSkuAvailable 让平台裁决;
       失败时把平台错误原文透出(审核未通过时平台会报 -5000300 这类明确错误)。
       buyable 仅用于列表页粗略标注, 文案统一说"未在售", 真实状态以平台后台为准。
    小红书是"先审后发", 商品创建后要过审核(24h~1-3 工作日)才能上架。
    建议列表页每 5 分钟调一次 /items/status 刷新标注。
====================================================================
"""
import os
import sys
import json
import time
import base64
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Body, Query, Request
from fastapi.middleware.cors import CORSMiddleware
import requests

# 复用 xhs_store.py 里的客户端类(凭证改为按店铺从 shop_registry 取)
from xhs_store import XhsStore

# 多店铺：从项目根读店铺注册表（shops.json，缺失则回退 .env 单店）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime_config import load_project_env
import shop_registry
import api_auth

load_project_env()

# ------------------------------------------------------------------
# 配置:token 持久化(每个店铺一个文件,存 accessToken/refreshToken,重启不丢)
# ------------------------------------------------------------------
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 单店时代的 token 文件位置（用于首次自动迁移，避免重新授权）
LEGACY_TOKEN_FILE = os.environ.get(
    "XHS_TOKEN_FILE",
    os.path.join(_BASE_DIR, "token_store.json"),
)
# 多店铺:token 目录，每店一个 <shop_id>.json
TOKEN_DIR = os.environ.get("XHS_TOKEN_DIR", os.path.join(_BASE_DIR, "tokens"))
# 默认只监听本机：原先 0.0.0.0 + 无鉴权，局域网内任意机器都能直接调发布/删商品接口。
# 确需局域网直连时设 XHS_API_HOST=0.0.0.0，并务必配好 API_KEY。
API_HOST = os.environ.get("XHS_API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("XHS_API_PORT", "8010"))
CORS_ORIGINS = [item.strip() for item in os.environ.get("CORS_ORIGINS", "*").split(",") if item.strip()]


# 创建 FastAPI 应用
app = FastAPI(title="小红书运营后台服务", version="0.2.0")

# 鉴权：X-API-Key（未配置 API_KEY 时只告警不拦截，见 api_auth.py）。
# 必须在 add_middleware(CORS) 之前装，否则 401 响应不经过 CORS 中间件。
api_auth.install(app, "xhs-api")

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


def _cached(shop_id, key, ttl, loader, force=False):
    """按 (店铺, 业务key) 缓存。key 形如 ("categories", "123")。"""
    full_key = (shop_id,) + tuple(key)
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(full_key)
        if not force and entry and entry["expires_at"] > now:
            return entry["value"]
    value = loader()
    with _CACHE_LOCK:
        _CACHE[full_key] = {"value": value, "expires_at": time.monotonic() + ttl}
        if len(_CACHE) > 1000:
            expired = [cache_key for cache_key, item in _CACHE.items() if item["expires_at"] <= now]
            for cache_key in expired:
                _CACHE.pop(cache_key, None)
    return value


def _clear_cache(shop_id, *prefixes):
    """清掉某店铺下指定前缀的缓存（不影响其他店铺）。"""
    with _CACHE_LOCK:
        for key in list(_CACHE):
            if key and key[0] == shop_id and len(key) > 1 and key[1] in prefixes:
                _CACHE.pop(key, None)


def _clear_product_cache(shop_id):
    _clear_cache(shop_id, "item_list", "item_detail", "item_status")


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


def xhs_conf_of(shop: dict) -> dict:
    """取店铺的小红书配置（app_id/app_secret/access_token/refresh_token 等）。"""
    conf = shop.get("xhs") or {}
    if not (conf.get("app_id") or ""):
        raise HTTPException(status_code=400, detail=f"店铺 {shop['shop_id']} 未配置小红书凭证")
    return conf


def _token_file(shop_id: str) -> str:
    """某店铺的 token 文件路径。"""
    return os.path.join(TOKEN_DIR, f"{shop_id}.json")


def _migrate_legacy_token():
    """把单店时代的 token_store.json 复制给默认店铺（免重新授权）。

    仅当：老文件存在、默认店的新文件不存在 时执行一次。
    """
    if not os.path.exists(LEGACY_TOKEN_FILE):
        return
    try:
        default_sid = shop_registry.default_shop()["shop_id"]
    except ValueError:
        return
    target = _token_file(default_sid)
    if os.path.exists(target):
        return
    try:
        os.makedirs(TOKEN_DIR, exist_ok=True)
        shutil.copy2(LEGACY_TOKEN_FILE, target)
        print(f"[xhs-token] 已将 {LEGACY_TOKEN_FILE} 复制为店铺 {default_sid} 的 token 文件")
    except Exception as exc:
        print(f"[xhs-token] WARN: 迁移旧 token 失败: {exc}")


_migrate_legacy_token()

# ==================================================================
# Token 存储 + 自动续期（按店铺）
# ==================================================================
def _load_token(shop_id: str) -> dict:
    """从该店铺的 token 文件读;没有则返回空"""
    path = _token_file(shop_id)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            # 静默 return {} 会把"token 文件损坏"变成"从没授权过"，
            # 上层只报鉴权失败，排查不到根因，必须留痕。
            print(f"[xhs-api] WARN: 店铺 {shop_id} 的 token 文件不可读({path}): {exc}", flush=True)
            return {}
    return {}

def _save_token(shop_id: str, tok: dict):
    """把 token 写回该店铺的文件(换新 token 后必须调,否则重启丢失)"""
    os.makedirs(TOKEN_DIR, exist_ok=True)
    path = _token_file(shop_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tok, f, ensure_ascii=False, indent=2)

def _refresh_access_token(shop_id: str, conf: dict):
    tok = _load_token(shop_id)
    refresh = tok.get("refreshToken") or conf.get("refresh_token")
    if not refresh:
        raise RuntimeError("没有保存 refreshToken，无法自动续期")
    data = _xhs_call("oauth.refreshToken", {"refreshToken": refresh}, shop_id=shop_id)
    # 小红书 OAuth 响应用蛇形命名(access_token/refresh_token/expires_in),兼容驼峰写法
    access = data.get("accessToken") or data.get("access_token")
    if not access:
        raise RuntimeError("刷新接口未返回 accessToken")
    tok["accessToken"] = access
    rk = data.get("refreshToken") or data.get("refresh_token")
    if rk:
        tok["refreshToken"] = rk
    exp = data.get("expiresIn") or data.get("expires_in") or 7 * 24 * 3600
    tok["expireAt"] = int(time.time()) + int(exp)
    _save_token(shop_id, tok)
    return access

def _get_access_token(shop_id: str, conf: dict) -> str:
    """取该店铺当前 accessToken(文件优先,其次店铺配置),并自动续期"""
    tok = _load_token(shop_id)
    access = tok.get("accessToken") or conf.get("access_token")
    refresh = tok.get("refreshToken") or conf.get("refresh_token")
    expire_at = tok.get("expireAt", 0)

    # 快过期(剩<30分钟)且有 refreshToken → 自动续期
    if refresh and expire_at and time.time() > expire_at - 1800:
        try:
            access = _refresh_access_token(shop_id, conf)
        except Exception as exc:
            # 续期失败不阻塞本次调用,让业务接口自己报鉴权错；但必须留痕，
            # 否则线上只看到"鉴权失败"，分不清是 refreshToken 过期还是网络问题。
            print(f"[xhs-api] WARN: 店铺 {shop_id} refreshToken 自动续期失败: {exc}", flush=True)
    return access


# ==================================================================
# 统一请求封装(签名 + 公共参数 + 完整错误信息含 error_msg)
# ==================================================================
def _xhs_call(method: str, payload: dict = None, shop_id: str = None) -> dict:
    """调小红书网关。成功返回 data 字段;失败抛 HTTP 400 带完整错误信息。

    多店铺：shop_id 决定用哪家店的 appId/appSecret/accessToken。
    未传时回退默认店铺（兼容旧调用）。
    """
    shop = shop_registry.resolve(shop_id or None) if not isinstance(shop_id, dict) else shop_id
    sid = shop["shop_id"]
    conf = xhs_conf_of(shop)
    app_id = conf["app_id"]
    app_secret = conf.get("app_secret") or ""

    access = None
    # oauth 换 token 的接口不需要 accessToken
    if method not in ("oauth.getAccessToken", "oauth.refreshToken"):
        access = _get_access_token(sid, conf)

    store = XhsStore(app_id, app_secret, access)
    timestamp = str(int(time.time()))
    body = {
        "timestamp": timestamp,
        "appId": app_id,
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
            timeout=120,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"请求小红书网关失败: {e}")
    # 网关出错时会返回 nginx 的 HTML(最常见的是 413 请求体过大),此时 resp.json() 会抛出
    # "Expecting value: line 1 column 1" 这种看不出原因的错,这里换成能定位的提示。
    if resp.status_code == 413:
        raise HTTPException(
            status_code=400,
            detail="小红书网关拒绝:请求体过大(HTTP 413)。素材经 base64 后体积约放大 1/3,"
                   "超过网关约 30MB 的上限——请把图片/视频压小后再传。",
        )
    try:
        data = resp.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail=f"小红书网关返回了非预期内容(HTTP {resp.status_code}): {resp.text[:200]}",
        )

    expired = data.get("error_code") == 401 or "accessToken expired" in str(data.get("error_msg") or data.get("message") or "")
    if expired and method not in ("oauth.getAccessToken", "oauth.refreshToken"):
        try:
            access = _refresh_access_token(sid, conf)
            store = XhsStore(app_id, app_secret, access)
            timestamp = str(int(time.time()))
            retry_body = {"timestamp": timestamp, "appId": app_id, "sign": store._sign(method, timestamp), "version": "2.0", "method": method, **(payload or {}), "accessToken": access}
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
def token_info(request: Request):
    """当前店铺 token 状态:是否已配置、过期时间(前端顶部提醒运营用)"""
    shop = shop_of(request)
    conf = xhs_conf_of(shop)
    sid = shop["shop_id"]
    tok = _load_token(sid)
    expire_at = tok.get("expireAt", 0)
    now = int(time.time())
    remain = max(0, expire_at - now) if expire_at else None
    return {
        "ok": True,
        "shop_id": sid,
        "configured": bool(tok.get("accessToken") or conf.get("access_token")),
        "has_refresh_token": bool(tok.get("refreshToken") or conf.get("refresh_token")),
        "expire_at": expire_at,
        "remain_seconds": remain,
        "remain_days": round(remain / 86400, 2) if remain is not None else None,
    }


@app.post("/token/code")
def token_by_code(request: Request, body: dict = Body(..., example={"code": "code-xxx"})):
    """首次接入:用授权回调的 code 换 accessToken/refreshToken,并持久化。
    调用链:店铺主账号授权 → 浏览器地址栏拿 code → 调这里一次,以后自动续。"""
    shop = shop_of(request)
    xhs_conf_of(shop)
    sid = shop["shop_id"]
    code = body.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="请求体需要 code 字段")
    data = _xhs_call("oauth.getAccessToken", {"code": code}, shop_id=sid)
    tok = _load_token(sid)
    # 小红书 OAuth 响应用蛇形命名,兼容驼峰写法
    ak = data.get("accessToken") or data.get("access_token")
    rk = data.get("refreshToken") or data.get("refresh_token")
    exp = data.get("expiresIn") or data.get("expires_in") or 7 * 24 * 3600
    if ak:
        tok["accessToken"] = ak
    if rk:
        tok["refreshToken"] = rk
    tok["expireAt"] = int(time.time()) + int(exp)
    _save_token(sid, tok)
    return {"ok": True, "shop_id": sid, "result": {"accessToken": ak,
                                                   "refreshToken": rk,
                                                   "expireAt": tok["expireAt"]}}


@app.post("/token/refresh")
def token_refresh(request: Request):
    """一键续期(accessToken 7天 / refreshToken 14天,14天内至少续一次)
    小红书官方规则:accessToken 未过期且剩余有效期 > 30分钟 时,刷新不会换发新 token"""
    shop = shop_of(request)
    conf = xhs_conf_of(shop)
    sid = shop["shop_id"]
    tok = _load_token(sid)
    refresh = tok.get("refreshToken") or conf.get("refresh_token")
    if not refresh:
        raise HTTPException(status_code=400, detail="没有保存 refreshToken,无法续期")
    remain = int(tok.get("expireAt", 0)) - int(time.time())
    if remain > 1800:
        # 官方规则:剩余>30分钟刷新不会换发新 token,直接返回当前状态,避免前端误报"已续期"
        return {"ok": True, "refreshed": False,
                "result": {"expireAt": tok.get("expireAt"),
                           "remain_days": round(remain / 86400, 2),
                           "message": f"accessToken 仍有效（剩余 {round(remain / 86400, 2)} 天），按小红书规则剩余>30分钟不会换发新 token"}}
    data = _xhs_call("oauth.refreshToken", {"refreshToken": refresh}, shop_id=sid)
    # 小红书 OAuth 响应用蛇形命名,兼容驼峰写法
    ak = data.get("accessToken") or data.get("access_token")
    rk = data.get("refreshToken") or data.get("refresh_token")
    # 官方返回 accessTokenExpiresAt(毫秒时间戳),没有 expiresIn 字段
    exp_at = data.get("accessTokenExpiresAt") or data.get("accessToken_expires_at")
    if exp_at:
        tok["expireAt"] = int(exp_at) // 1000
    else:
        exp = data.get("expiresIn") or data.get("expires_in") or 7 * 24 * 3600
        tok["expireAt"] = int(time.time()) + int(exp)
    if ak:
        tok["accessToken"] = ak
    if rk:
        tok["refreshToken"] = rk
    _save_token(sid, tok)
    return {"ok": True, "refreshed": True, "result": {"accessToken": ak,
                                                      "expireAt": tok["expireAt"]}}


# ==================================================================
# 发品页:公共参数查询
# ==================================================================
@app.get("/categories")
def categories(request: Request,
               category_id: str = Query(None, description="父类目ID,不传查一级类目"),
               keyword: str = Query(None, description="关键词过滤(匹配类目名)")):
    """选类目。传 category_id 查子类目;不传查一级。创建商品要用叶子(isLeaf=true)。"""
    sid = shop_of(request)["shop_id"]
    payload = {"categoryId": category_id} if category_id else {}
    data = _cached(
        sid, ("categories", str(category_id or "")), 12 * 3600,
        lambda: _xhs_call("common.getCategories", payload, shop_id=sid),
    )
    cats = data.get("categoryV3s", []) if data else []
    if keyword:
        cats = [c for c in cats if keyword in (c.get("name") or "")]
    return {"ok": True, "total": len(cats), "result": cats}


# 平台品牌搜索每页最多给 20 个(实测传 pageSize=500 也只返回 20),而珠宝类目品牌上百个。
# 编辑页回显一个排序靠后的品牌时, 只取第一页会让前端下拉匹配不到该选项、直接把数字 ID 显示给运营,
# 所以传了 brand_id 就继续翻页把它捞回来。
_BRAND_PAGE_MAX = 16      # 最多翻 16 页(约 300 个品牌), 兜底防止无限翻页
_BRAND_PAGE_SIZE = 50     # 平台按自己的上限截断, 这里给大值以免平台放宽后取少了


def _brand_page(sid: str, category_id: str, keyword: str, page: int) -> list:
    """取品牌搜索的某一页(带缓存)。每页究竟多少条由平台决定,这里不做假设。"""
    data = _cached(
        sid, ("brands", str(category_id), str(keyword), page), 6 * 3600,
        lambda: _xhs_call("common.brandSearch", {
            "categoryId": category_id, "keyword": keyword,
            "pageNo": page, "pageSize": _BRAND_PAGE_SIZE}, shop_id=sid),
    )
    return list((data or {}).get("brands") or (data or {}).get("brandList") or [])


@app.get("/brands")
def brands(request: Request,
           category_id: str = Query(..., description="末级类目ID(必填,选完类目再查)"),
           keyword: str = Query("", description="品牌关键词"),
           brand_id: str = Query("", description="编辑页回显用：需要一并返回的品牌ID(可能不在第一页)")):
    """搜索品牌(创建商品 brandId 必填,从这里查)

    brand_id 非空且不在第一页时, 并发翻页把该品牌找出来补进结果 —— 否则前端下拉没有这个选项,
    编辑页会把原始品牌ID(如 241795)直接展示给运营, 运营看不懂。
    页结果有 6 小时缓存, 同一类目的第二次编辑不会再产生平台请求。
    """
    sid = shop_of(request)["shop_id"]
    first_rows = _brand_page(sid, category_id, keyword, 1)
    if not brand_id or any(str(b.get("id")) == str(brand_id) for b in first_rows):
        return {"ok": True, "result": {"brands": first_rows}}

    rows = list(first_rows)
    seen = {str(b.get("id")) for b in rows}
    pages = list(range(2, _BRAND_PAGE_MAX + 1))
    with ThreadPoolExecutor(max_workers=min(8, len(pages))) as pool:
        for chunk in pool.map(lambda page: _brand_page(sid, category_id, keyword, page), pages):
            for brand in chunk:
                key = str(brand.get("id"))
                if key not in seen:
                    seen.add(key)
                    rows.append(brand)
            if str(brand_id) in seen:
                break
    return {"ok": True, "result": {"brands": rows}}


@app.get("/shipping-templates")
def shipping_templates(request: Request):
    """运费模板列表(创建商品 shippingTemplateId 必填)"""
    sid = shop_of(request)["shop_id"]
    data = _cached(
        sid, ("shipping_templates",), 10 * 60,
        lambda: _xhs_call("common.getCarriageTemplateList", {"pageIndex": 1, "pageSize": 50}, shop_id=sid),
    )
    return {"ok": True, "result": data}


@app.get("/logistics-plans")
def logistics_plans(request: Request):
    """物流方案列表(创建 SKU logisticsPlanId 必填,注意不是运费模板ID!)"""
    sid = shop_of(request)["shop_id"]
    data = _cached(
        sid, ("logistics_plans",), 10 * 60,
        lambda: _xhs_call("common.getLogisticsList", shop_id=sid),
    )
    return {"ok": True, "result": data}


@app.get("/category-attributes")
def category_attributes(request: Request, category_id: str = Query(..., description="末级叶子类目ID(必填)")):
    """由末级类目查“商品属性”定义(common.getAttributeLists)。
    珠宝等类目发品必填:创建商品 attributes 每一项要 propertyId + valueId。
    前端据此渲染属性下拉,把运营选的“材质=18K金”翻译成平台 propertyId/valueId。
    ⚠️ 首次联调在 /docs 看真实返回:属性数组键名、每个属性候选值 valueId 结构以平台为准。"""
    sid = shop_of(request)["shop_id"]
    data = _cached(
        sid, ("category_attributes", str(category_id)), 12 * 3600,
        lambda: _xhs_call("common.getAttributeLists", {"categoryId": category_id}, shop_id=sid),
    )
    return {"ok": True, "result": data}


@app.get("/category-variations")
def category_variations(request: Request, category_id: str = Query(..., description="末级叶子类目ID(必填)")):
    """由末级类目查“规格”定义(common.getVariations)。
    对应创建商品的 variantIds(规格维度)和每个 SKU 的 variants(规格值)。
    后台“颜色分类/重量/尺寸”这些规格维度就来自这里。
    ⚠️ 首次联调看真实返回的规格维度 id 与其规格值(valueId)结构。"""
    sid = shop_of(request)["shop_id"]
    data = _cached(
        sid, ("category_variations", str(category_id)), 12 * 3600,
        lambda: _xhs_call("common.getVariations", {"categoryId": category_id}, shop_id=sid),
    )
    return {"ok": True, "result": data}


@app.get("/attribute-values")
def attribute_values(
    request: Request,
    category_id: str = Query(..., description="末级叶子类目ID(必填)"),
    attribute_id: str = Query(..., description="属性或规格的 id(必填)"),
):
    """查某个属性或规格维度的候选值(common.getAttributeValues)。
    创建商品的 attributes 要 propertyId+valueId,SKU 的 variants 要 valueId。
    attributeId 既可传属性 id(如“钻石切工”),也可传规格 id(如“颜色分类”)。
    返回 attributeValueV3s:[{valueId,valueName}]。数值型规格(如“重量/克拉”)返回空数组。"""
    sid = shop_of(request)["shop_id"]
    data = _cached(
        sid, ("attribute_values", str(category_id), str(attribute_id)), 12 * 3600,
        lambda: _xhs_call("common.getAttributeValues", {
            "categoryId": category_id, "attributeId": attribute_id}, shop_id=sid),
    )
    return {"ok": True, "result": data}


# ==================================================================
# 发品页:素材上传
# ==================================================================
# 视频素材是异步转码的:material.uploadMaterial 返回时 url 还是 null(status=2),
# 要轮询 material.queryMaterial 等转码完成(status=1)才拿到可用地址。
VIDEO_TRANSCODE_TIMEOUT = 120    # 秒:等待转码上限
VIDEO_TRANSCODE_INTERVAL = 3     # 秒:轮询间隔


def _material_url_ready(url: str) -> bool:
    """素材 CDN 地址是否真的能取到。

    刚上传时接口就可能给出 url,但 CDN 上还没同步(取到的是 404),这种地址交给前端也播不了。
    """
    try:
        resp = requests.head(url, timeout=10, allow_redirects=True)
        return resp.status_code in (200, 206)
    except Exception as exc:
        # 探测失败按"未就绪"处理，但留痕：否则视频上传超时看不出是网络问题
        print(f"[xhs-api] WARN: 素材地址探测失败(按未就绪处理) {url}: {exc}", flush=True)
        return False


def _wait_video_material(sid: str, data: dict) -> dict:
    """等视频转码完成、且素材地址真的能取到,再返回(超时给明确错误)。"""
    material_id = (data or {}).get("materialId")
    if not material_id:
        raise HTTPException(status_code=400, detail="视频上传成功但未返回 materialId")
    deadline = time.time() + VIDEO_TRANSCODE_TIMEOUT
    while True:
        try:
            details = _xhs_call("material.queryMaterial", {"materialId": material_id}, shop_id=sid) or {}
        except HTTPException as exc:
            # 转码中平台可能报错，这里继续轮询；留痕是为了"2 分钟没就绪"时能定位原因
            print(f"[xhs-api] WARN: 查询视频素材 {material_id} 失败(继续等待): "
                  f"{getattr(exc, 'detail', exc)}", flush=True)
            details = {}
        hit = next((d for d in (details.get("materialDetailList") or [])
                    if d.get("materialId") == material_id), None)
        if hit and hit.get("url") and _material_url_ready(hit["url"]):
            return {**data, **hit}
        if time.time() >= deadline:
            raise HTTPException(
                status_code=504,
                detail="视频素材已上传,但约 2 分钟仍未就绪(平台还在转码,或素材已失效),请稍后重试",
            )
        time.sleep(VIDEO_TRANSCODE_INTERVAL)


@app.post("/materials/upload")
def upload_material(request: Request, body: dict = Body(..., example={"url": "https://你的图床/主图1.jpg"})):
    """传图:下载公网图片 → 上传小红书素材,返回素材URL(填进 images 字段)"""
    sid = shop_of(request)["shop_id"]
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
        "name": name, "type": "IMAGE", "materialContent": b64}, shop_id=sid)
    return {"ok": True, "result": data}


@app.post("/materials/upload-file")
def upload_material_file(request: Request, body: dict = Body(...)):
    """上传本地文件到小红书素材库,返回素材 URL。

    type=IMAGE(默认): Excel 内嵌图 / 前端选图,会放大到 ≥1200 长边;
    type=VIDEO: 主图视频,不做图片处理,直接按视频素材上传。
    """
    sid = shop_of(request)["shop_id"]
    encoded = body.get("content_base64")
    if not encoded:
        raise HTTPException(status_code=400, detail="请求体需要 content_base64 字段")
    material_type = str(body.get("type") or "IMAGE").upper()
    if material_type not in ("IMAGE", "VIDEO"):
        raise HTTPException(status_code=400, detail=f"不支持的素材类型: {material_type}")
    try:
        content = base64.b64decode(encoded, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="content_base64 编码无效")
    if not content:
        raise HTTPException(status_code=400, detail="文件内容为空")
    if material_type == "IMAGE":
        if len(content) > 20 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="图片超过 20MB")
        # 放大到至少 1200 长边,避免小红书 createItemV2 报「图片像素不低于800x800」
        # (平台对 800x800 会再压缩到 <800,传 1200x1200 压完仍 ≥ 800)
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(content))
            w, h = img.size
            if w < 1200 or h < 1200:
                scale = max(1200.0 / w, 1200.0 / h)
                new_size = (max(int(round(w * scale)), 1200), max(int(round(h * scale)), 1200))
                img = img.resize(new_size, Image.LANCZOS)
                buf = io.BytesIO()
                fmt = (img.format or "JPEG").upper()
                if fmt in ("JPG", "JPEG"):
                    img.save(buf, format="JPEG", quality=95)
                else:
                    img.save(buf, format=fmt if fmt in ("PNG", "WEBP") else "PNG")
                content = buf.getvalue()
                encoded = base64.b64encode(content).decode("ascii")
        except Exception as exc:
            # 放大失败就传原图,不阻塞；留痕便于判断平台是否因分辨率拒图
            print(f"[xhs-api] WARN: 素材图放大失败,改传原图: {exc}", flush=True)
    elif len(content) > 20 * 1024 * 1024:
        # 小红书不像微信支持分块:视频要 base64 后一次 POST,而网关请求体约 30MB 封顶,
        # base64 放大 1/3 → 视频实际上限约 22MB,这里取 20MB 留余量(前端也用同一数值)。
        raise HTTPException(status_code=400, detail="视频超过 20MB(小红书网关请求体上限约 30MB)")
    default_name = "material.mp4" if material_type == "VIDEO" else "material.jpg"
    name = os.path.basename(str(body.get("filename") or default_name))[:40]
    data = _xhs_call("material.uploadMaterial", {
        "name": name, "type": material_type, "materialContent": encoded}, shop_id=sid)
    if material_type == "VIDEO" and not (data or {}).get("url"):
        # 上传即刻返回的 url 为空(转码中),等转码完成再拿真实地址
        data = _wait_video_material(sid, data)
    return {"ok": True, "result": data}


# ==================================================================
# 发品页:创建商品+SKU(一次搞定)
# ==================================================================
@app.post("/items/and-sku")
def create_item_and_sku(request: Request, body: dict = Body(..., examples=[{
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
    sid = shop_of(request)["shop_id"]
    item = body.get("item")
    sku_list = body.get("sku_list") or []
    if not item:
        raise HTTPException(status_code=400, detail="请求体需要 item 字段")
    if not sku_list:
        raise HTTPException(status_code=400, detail="请求体需要 sku_list 列表")

    # ---- 分步创建(可靠方案) ----
    # 第 1 步:创建商品
    item_data = _xhs_call("product.createItemV2", item, shop_id=sid)
    item_id = item_data.get("id") or item_data.get("itemId")
    if not item_id:
        raise HTTPException(status_code=500, detail="创建商品成功但未返回 itemId")

    # 第 2 步:逐个创建 SKU(单个失败不阻塞,已建的保留,前端可用 itemId 再补)
    sku_ids, errors = [], []
    for sku in sku_list:
        try:
            sku_data = _xhs_call("product.createSkuV2", {"itemId": item_id, **sku}, shop_id=sid)
            sku_id = (sku_data or {}).get("id") or (sku_data or {}).get("skuId")
            if sku_id:
                sku_ids.append(sku_id)
            else:
                errors.append({"sku": sku.get("erpCode") or sku.get("price"),
                               "error": "SKU 创建接口未返回 skuId"})
        except HTTPException as e:
            errors.append({"sku": sku.get("erpCode") or sku.get("price"), "error": str(e.detail)})

    _clear_product_cache(sid)
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
def list_items(request: Request,
               page_no: int = Query(1, ge=1, description="页码,从1开始"),
               page_size: int = Query(20, ge=1, le=100, description="每页条数,最大100")):
    """商品列表(分页)。列表不含审核状态,要显示"审核中/已上架"再调 /items/status。"""
    sid = shop_of(request)["shop_id"]
    data = _cached(
        sid, ("item_list", page_no, page_size), 30,
        lambda: _xhs_call("product.searchItemList", {"pageNo": page_no, "pageSize": page_size}, shop_id=sid),
    )
    return {"ok": True, "result": data}


# 平台单次查询上限（原代码是直接 [:20] 截断）
MAX_STATUS_IDS = 20


@app.post("/items/status")
def items_status(request: Request, body: dict = Body(..., example={"item_ids": ["itemId1", "itemId2"]})):
    """批量查审核状态(前端列表页每5分钟刷一次)。
    返回 [{itemId, name, skus:[{skuId, buyable}]}],buyable=true=审核通过可上架。"""
    shop = shop_of(request)
    sid = shop["shop_id"]
    conf = xhs_conf_of(shop)
    item_ids = body.get("item_ids") or []
    if not item_ids:
        raise HTTPException(status_code=400, detail="请求体需要 item_ids 列表")
    # 超出的部分原先被静默丢掉，前端以为全查过了，表现是"有些商品一直没有审核状态"，
    # 所以这里记录截断情况并在响应里显式回传（见下方 truncated/skipped/message）。
    requested = len(item_ids)
    item_ids = item_ids[:MAX_STATUS_IDS]
    truncated = requested > len(item_ids)
    _get_access_token(sid, conf)  # 并发查询前先完成一次 token 检查或刷新

    def fetch_status(item_id):
        try:
            data = _cached(
                sid, ("item_status", str(item_id)), 10,
                lambda: _xhs_call("product.getItemInfo", {"itemId": item_id}, shop_id=sid),
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
        except HTTPException as exc:
            detail = str(getattr(exc, "detail", exc))[:300]
            # 单条失败不影响其余商品，但要留痕：否则前端只显示"查询失败"，
            # 分不清是限流、token 失效还是商品已被删。
            print(f"[xhs-api] WARN: 查询商品 {item_id} 状态失败: {detail}", flush=True)
            return {"itemId": item_id, "name": None, "skus": [], "error": "查询失败",
                    "error_detail": detail}

    # 商品详情互不依赖，限制为最多 5 个并发，兼顾速度与平台限流。
    with ThreadPoolExecutor(max_workers=min(5, len(item_ids))) as pool:
        results = list(pool.map(fetch_status, item_ids))
    return {
        "ok": True,
        "total": len(results),
        "requested": requested,
        "truncated": truncated,
        "skipped": requested - len(item_ids),
        "message": (f"一次最多查询 {MAX_STATUS_IDS} 个商品，本次只查了前 {MAX_STATUS_IDS} 个，"
                    f"其余 {requested - len(item_ids)} 个未查询，请分批重试") if truncated else "",
        "result": results,
    }


@app.post("/skus/{sku_id}/available")
def set_sku_available(sku_id: str, request: Request, body: dict = Body(..., example={"available": 1})):
    """行内上下架。available: 1=上架(买家可见) 0=下架。
    ⚠️ 不要用 buyable 预判能否上架(2026-09-17 实测: 平台后台已上架成功的商品该字段仍返回 false)。
       审核未通过时平台会直接报错(如 -5000300), 把错误原样透出给运营即可。
    小红书按 SKU 粒度上下架,商品多个 SKU 要逐个调。"""
    sid = shop_of(request)["shop_id"]
    available = body.get("available")
    if available not in (0, 1):
        raise HTTPException(status_code=400, detail="available 必须是 0 或 1")
    data = _xhs_call("product.updateSkuAvailable", {
        "skuId": str(sku_id), "available": str(available)}, shop_id=sid)
    _clear_product_cache(sid)
    return {"ok": True, "result": data}


# ==================================================================
# 商品编辑页
# ==================================================================
@app.get("/items/{item_id}")
def get_item(item_id: str, request: Request):
    """商品详情:完整信息 + skus[].buyable 审核状态(编辑页回填用)"""
    sid = shop_of(request)["shop_id"]
    data = _cached(
        sid, ("item_detail", str(item_id)), 60,
        lambda: _xhs_call("product.getItemInfo", {"itemId": item_id}, shop_id=sid),
    )
    return {"ok": True, "result": data}


@app.delete("/items/{item_id}")
def delete_item(item_id: str, request: Request):
    """删除商品(彻底删除,不可恢复;日常建议用下架代替)。

    对应平台方法 product.deleteItemV2 —— 2026-09-17 实测确认:
      · product.deleteItemV2 可用,返回「删除成功」;
      · 旧名 product.deleteItem 已被平台废弃,报 error_code=401「请使用商品3.0新接口」。
    ⚠️ 平台对不存在的 itemId 也返回「删除成功」(幂等),不能靠返回值判断是否真的删掉过。
    """
    sid = shop_of(request)["shop_id"]
    result = ok_or_400(lambda: _xhs_call("product.deleteItemV2", {"itemId": str(item_id)}, shop_id=sid))
    _clear_product_cache(sid)
    return result


@app.put("/items/{item_id}")
def update_item(item_id: str, request: Request, body: dict = Body(..., example={
    "item": {"name": "新标题"}, "updated_fields": ["name"]})):
    """改商品。建议传 updated_fields 只更新指定字段,不传=全量更新(有风险)"""
    sid = shop_of(request)["shop_id"]
    item = body.get("item")
    if not item:
        raise HTTPException(status_code=400, detail="请求体需要 item 字段")
    payload = {"id": item_id, **item}
    if body.get("updated_fields"):
        payload["updatedFields"] = body["updated_fields"]
    result = ok_or_400(lambda: _xhs_call("product.updateItemV2", payload, shop_id=sid))
    _clear_product_cache(sid)
    return result


@app.put("/skus/{sku_id}")
def update_sku(sku_id: str, request: Request, body: dict = Body(..., example={
    "sku": {"price": 9000, "stock": 50}, "updated_fields": ["price", "stock"]})):
    """改 SKU(价格/库存/规格图等)。updated_fields 可选。

    ⚠️ 小红书 product.updateSkuV2 必须带 itemId, 否则报「入参itemId不能为空」。
       用 body.item_id 或在 sku 里带 itemId 传进来(二选一)。
    """
    sid = shop_of(request)["shop_id"]
    sku = body.get("sku")
    if not sku:
        raise HTTPException(status_code=400, detail="请求体需要 sku 字段")
    payload = {"id": sku_id, **sku}
    if body.get("item_id") and not payload.get("itemId"):
        payload["itemId"] = str(body["item_id"])
    if body.get("updated_fields"):
        payload["updatedFields"] = body["updated_fields"]
    result = ok_or_400(lambda: _xhs_call("product.updateSkuV2", payload, shop_id=sid))
    _clear_product_cache(sid)
    return result


# ==================================================================
# 启动入口:python xhs_api.py 直接跑(端口 8010,避开微信小店 8000)
# ==================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=API_HOST, port=API_PORT)
