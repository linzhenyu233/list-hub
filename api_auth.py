# -*- coding: utf-8 -*-
"""三个服务（wechat / xhs / bulk_api）共用的 X-API-Key 鉴权。

背景
----
三个服务原先都监听 0.0.0.0 且没有任何鉴权：局域网内任意机器、甚至浏览器里
打开的任意网页，都能直接调「发布商品 / 删除商品 / 改标题」这类接口。这里做两件事：
  1) 默认只监听 127.0.0.1（各服务 *_API_HOST 默认值已改），挡住跨机访问；
  2) 请求必须带 X-API-Key（取自环境变量 API_KEY），挡住同机上的其他程序/网页。

用法
----
    import api_auth
    api_auth.install(app, "xhs-api")   # app 建好后、add_middleware(CORS) 之前调用

配置
----
    API_KEY=<一串随机字符串>           # 写在 .env 或系统环境变量
未配置 API_KEY 时不拦截、只在启动时打 WARN —— 保证上线这一步不会让现有部署直接瘫；
配好重启即生效（三个服务都无 --reload，必须重启进程）。

浏览器发起的请求（<img src>、<a download>）带不了自定义头，所以额外支持
?api_key= 查询参数兜底；开发环境由 vite 代理在服务端补头（frontend/vite.config.js），
生产环境请在 nginx 的 proxy_set_header 里补，密钥不下发到浏览器。
"""

import os

from fastapi.responses import JSONResponse

API_KEY_HEADER = "X-API-Key"
API_KEY_QUERY = "api_key"

# 免鉴权路径：/health 供探活（bulk_api 的 _platform_ready 也打这里）；
# /docs 系列是只读接口文档，方便本地排查，没有业务副作用。
EXEMPT_PATHS = {
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/docs/oauth2-redirect",
    "/favicon.ico",
}


def api_key() -> str:
    """当前进程配置的 API_KEY（空串 = 不校验）。"""
    return (os.environ.get("API_KEY") or "").strip()


def auth_headers(extra: dict = None) -> dict:
    """服务间/脚本调用时带上的鉴权头（未配置 API_KEY 时返回空 dict，向后兼容）。"""
    headers = {}
    key = api_key()
    if key:
        headers[API_KEY_HEADER] = key
    if extra:
        headers.update(extra)
    return headers


def _presented_key(request) -> str:
    return (request.headers.get(API_KEY_HEADER)
            or request.query_params.get(API_KEY_QUERY) or "").strip()


def install(app, service_name: str) -> None:
    """给 FastAPI app 装鉴权中间件；未配置 API_KEY 时只告警不拦截。

    必须在 app.add_middleware(CORSMiddleware, ...) 之前调用：Starlette 后加的
    中间件在外层，若反过来，401 响应不经过 CORS，前端只会看到跨域报错。
    """
    key = api_key()
    if not key:
        print(f"[api-auth] WARN: {service_name} 未配置 API_KEY，接口无鉴权；"
              f"建议在 .env 加 API_KEY=<随机串> 后重启（详见 api_auth.py 顶部注释）", flush=True)
        return

    @app.middleware("http")
    async def _guard(request, call_next):
        # OPTIONS 是浏览器预检，本身不带自定义头，放行交给 CORS 中间件处理
        if request.method == "OPTIONS" or request.url.path in EXEMPT_PATHS:
            return await call_next(request)
        if _presented_key(request) != key:
            return JSONResponse(
                {"detail": f"缺少或错误的 {API_KEY_HEADER}（{service_name} 已开启鉴权）"},
                status_code=401,
            )
        return await call_next(request)

    print(f"[api-auth] {service_name} 已开启 X-API-Key 鉴权"
          f"（免鉴权：{', '.join(sorted(EXEMPT_PATHS))}）", flush=True)
