# -*- coding: utf-8 -*-
"""
====================================================================
 有赞云开放平台 · 商品 / 订单 API 客户端（Python）
====================================================================
给 youzan_api.py 用：把「网关调用 + 响应壳归一化 + 错误码识别」收口在这里，
业务路由只写 ``store.create_item(...)`` 这种一行调用。

调用链：
    前端运营台 / 批量中台(bulk_api)
        │  HTTP（X-Shop-Id 指定店铺）
        ▼
    youzan/youzan_api.py（本服务的 HTTP 层，端口 8030）
        │  按店铺取 client_id/client_secret/kdt_id + access_token
        ▼
    YouzanStore（本文件）
        │  统一走 mock_platform.gateway_call（三级模式 mock/sandbox/live 的唯一出口）
        ▼
    有赞云网关 https://open.youzanyun.com

与微信/小红书的三点关键差异（详见 docs/有赞平台适配实施方案.md）
--------------------------------------------------------------------
1. **凭证是三件套**：client_id + client_secret（应用）+ kdt_id（店铺 ID）。
   自用型应用可绑多个店铺，access_token 按 kdt_id 隔离，不能跨店复用。
2. **没有 refresh_token**：grant_type=silent 静默换 token，7 天过期重新获取；
   官方说明"1 小时内重复请求返回同一个 token"，所以"快到期就重取"是安全的。
3. **价格单位是「元」**（youzan.item.create 的 price 官方注明"单位:元"），
   与微信/小红书的「分」不同 —— 中台侧由 platform_registry.price_unit 负责换算。

⚠️ 带 ★ 的地方是**待真机核对**项（官方文档未给全或存在两种说法）：
   核对方式见 docs/有赞平台适配实施方案.md §9/§11，或 live 下开
   ``MOCK_RECORD=1`` 录一次真实响应校正。
====================================================================
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime_config import load_project_env
import mock_platform        # 三级模式（mock/sandbox/live）与 fixture 回放

load_project_env()


def format_day_time(value) -> str:
    """时间 → 有赞订单接口要的字符串格式 ``YYYY-MM-DD HH:MM:SS``。

    有赞订单接口的时间**不是时间戳**，是字符串；而中台内部传的是秒级时间戳，
    所以这里做一次归一化（同时兼容已经是字符串的调用方）。
    """
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if len(text) >= 19 and text[4] == "-":
        return text[:19]
    try:
        seconds = int(float(text))
    except (TypeError, ValueError):
        return text
    if seconds >= 10 ** 12:          # 毫秒时间戳
        seconds //= 1000
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))


_ERROR_HINTS = None


def error_hint(code) -> str:
    """错误码 → 中文解释（读 ``youzan/error_codes.json``）。

    只用于让「失败清单」里的原因对运营可读；限流/鉴权的**判定**不读它，
    而读 platform_registry 的 markers，避免两处口径打架。
    """
    global _ERROR_HINTS
    if _ERROR_HINTS is None:
        _ERROR_HINTS = {}
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error_codes.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                _ERROR_HINTS = {str(k): str(v)
                                for k, v in (json.load(handle).get("codes") or {}).items()}
        except Exception:
            _ERROR_HINTS = {}
    return _ERROR_HINTS.get(str(code), "")


class YouzanError(RuntimeError):
    """有赞网关返回的错误。

    带上 ``code`` 是为了让上层能区分"该刷 token"、"该退避重试"、"该进失败清单"，
    而不是把所有失败都糊成一句"调用失败"。
    """

    def __init__(self, method: str, code, message):
        self.method = method
        self.code = code
        self.message = str(message or "")
        hint = error_hint(code)
        super().__init__(f"[{method}] 调用失败: code={code} {self.message}"
                         + (f"（{hint}）" if hint else ""))

    def _hit(self, markers) -> bool:
        text = f"{self.code} {self.message}"
        return any(str(marker) in text for marker in (markers or ()))

    @property
    def is_auth_expired(self) -> bool:
        """凭证过期/无效 → 上层换 token 后重试一次（真机联调后校正标记）。"""
        import platform_registry
        return self._hit(platform_registry.adapter("youzan")["auth_expired_markers"])

    @property
    def is_rate_limited(self) -> bool:
        """限流（4101）→ 退避重试（gateway_call 已处理，这里供上层判断）。"""
        import platform_registry
        return self._hit(platform_registry.adapter("youzan")["rate_limit_markers"])


class YouzanStore:
    """有赞店铺操作类（商品域 + 订单只读）。

    用法::

        store = YouzanStore(client_id, client_secret, kdt_id, access_token, shop_id="xxx")
        store.request_token()                     # 换取/续期 token（静默授权）
        store.create_item({...})                  # 创建商品
        store.list_orders(start_ts, end_ts)       # 拉订单
    """

    # 有赞云网关（新域名；旧域名 open.youzan.com/api/oauthentry 是另一套，不要混用）
    GATEWAY = os.environ.get("YOUZAN_GATEWAY", "https://open.youzanyun.com").rstrip("/")
    # 取 token 不是 youzan.* 接口，路径独立
    AUTH_PATH = "/auth/token"
    # 所有业务接口：POST {GATEWAY}/api/{接口名}/{版本号}?access_token=xxx
    API_PREFIX = "/api"

    # --- 接口名与版本（集中在这里，联调时只改这一段）-----------------------------
    API_ITEM_CATEGORIES = ("youzan.itemcategories.get", "3.0.0")
    API_DELIVERY_TEMPLATE = ("youzan.logistics.template.search", "3.0.0")        # ★
    API_IMG_UPLOAD = ("youzan.materials.storage.platform.img.upload", "3.0.0")
    API_ITEM_CREATE = ("youzan.item.create", "3.0.0")
    API_ITEM_UPDATE = ("youzan.item.update", "3.0.0")
    API_ITEM_GET = ("youzan.item.get", "3.0.0")
    API_ITEM_BY_CODE = ("youzan.items.custom.get", "3.0.0")
    API_SKU_BY_CODE = ("youzan.skus.custom.get", "3.0.0")
    API_SKU_UPDATE = ("youzan.item.sku.update", "3.0.0")
    API_QUANTITY_UPDATE = ("youzan.item.quantity.update", "3.0.0")
    API_ITEMS_ONSALE = ("youzan.items.onsale.get", "3.0.0")
    API_ITEMS_INVENTORY = ("youzan.items.inventory.get", "3.0.0")
    API_ITEM_LISTING = ("youzan.item.update.listing", "3.0.0")                   # ★
    API_ITEM_DELISTING = ("youzan.item.update.delisting", "3.0.0")               # ★
    API_ITEM_DELETE = ("youzan.item.delete", "3.0.0")                            # ★
    API_SHOP_GET = ("youzan.shop.get", "3.0.0")                                  # ★
    API_TRADES_SOLD = ("youzan.trades.sold.get", "4.0.4")
    API_TRADE_GET = ("youzan.trade.get", "4.0.2")

    def __init__(self, client_id, client_secret, kdt_id="", access_token=None,
                 shop_id=None, timeout=30):
        """
        - client_id / client_secret: 有赞云应用凭证（控制台 → 应用中心 → 应用概况）
        - kdt_id:                    店铺 ID（授权信息页），多店铺必须各配各的
        - access_token:              店铺 token；不传则调用前先 request_token()
        - shop_id:                   本项目店铺标识，仅用于解析 mock/sandbox/live 模式
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.kdt_id = str(kdt_id or "")
        self.access_token = access_token
        self.shop_id = shop_id or ""
        self.timeout = timeout

    # =================================================================
    # 授权：自用型应用静默换 token（无 refresh_token，过期重取即可）
    # =================================================================
    def request_token(self) -> dict:
        """换取 access_token。

        ⚠️ 2026-10-15 真机结论（两条，都是实测踩出来的）：
          1. 必须用 **JSON** 提交。表单会报 code=1000：
             `Content type 'application/x-www-form-urlencoded;charset=UTF-8' not supported,
              Please use 'application/json;charset=UTF-8'`；
          2. 入参名是 **`authorize_type` + `grant_id`**，不是 `grant_type` + `kdt_id`。
             用旧名字的报错原文：`grantId 为空`（试过 kdt_id 与省略 grant_id 都是这句）。
        成功壳：`{"success": true, "code": 200, "data": {"access_token", "expires", "scope"}}`，
        其中 `expires` 是**绝对毫秒时间戳**（7 天后），不是 expires_in 秒数。
        换 token 属于"无副作用"操作，所以允许超时重试。
        """
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "authorize_type": "silent",
            "grant_id": self.kdt_id,
        }
        url = f"{self.GATEWAY}{self.AUTH_PATH}"
        try:
            resp = mock_platform.gateway_call(
                "youzan", url, method_key="auth.token", body=payload,
                timeout=self.timeout, shop_id=self.shop_id, retry_safe=True,
            )
        except Exception as exc:
            raise YouzanError("auth.token", None, f"请求有赞授权接口失败: {exc}")
        try:
            data = resp.json()
        except Exception:
            raise YouzanError("auth.token", resp.status_code,
                              f"授权接口返回非 JSON 内容: {str(resp.text)[:200]}")
        data = data if isinstance(data, dict) else {}
        envelope = data.get("gw_err_resp")
        if isinstance(envelope, dict):
            raise YouzanError("auth.token", envelope.get("err_code"), envelope.get("err_msg"))
        # 真机失败壳（第四种形态）：{"success": false, "code": 1000, "data": null, "message": "…"}
        # message 里是**人能看懂的原因**（Content type 不对 / grantId 为空 / 参数错误），
        # 直接透出它，别让运营对着一坨 dict 猜（★1 证据）。
        if data.get("success") is False:
            raise YouzanError("auth.token", data.get("code"),
                              data.get("message") or f"授权接口失败: {data}")
        # 真机成功壳把 token 包在 data 里；也兼容 token 直接在顶层的写法（mock/其他版本）
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        token = inner.get("access_token") or inner.get("accessToken") or data.get("access_token")
        if not token:
            raise YouzanError("auth.token", data.get("code"),
                              data.get("message") or f"授权接口未返回 access_token: {data}")
        self.access_token = token
        return inner

    # =================================================================
    # 统一请求壳：拼 URL + 归一化响应 + 抛可识别的错误
    # =================================================================
    def _call(self, api, payload=None, *, retry_safe=None, files=None) -> dict:
        """调一个业务接口，返回 **data**（已剥掉平台响应壳）。

        - api:   (接口名, 版本号)，取类常量
        - files: 传了就走 multipart（图片素材上传用，见 upload_image_bytes，★3）
        - 返回:  平台的 data 字段；接口直接返回业务对象时原样返回
        """
        method, version = api
        url = f"{self.GATEWAY}{self.API_PREFIX}/{method}/{version}"
        if self.access_token:
            # 有赞把 access_token 放在 URL query 上（不是 body、也不需要签名）
            url = f"{url}?access_token={self.access_token}"
        try:
            resp = mock_platform.gateway_call(
                "youzan", url, method_key=method, body=payload or {}, files=files,
                timeout=self.timeout, shop_id=self.shop_id, retry_safe=retry_safe,
            )
        except Exception as exc:
            raise YouzanError(method, None, f"请求有赞网关失败: {exc}")
        try:
            data = resp.json()
        except Exception:
            raise YouzanError(method, resp.status_code,
                              f"网关返回非预期内容(HTTP {resp.status_code}): {str(resp.text)[:200]}")
        return self._unwrap(method, data)

    @staticmethod
    def _unwrap(method: str, data):
        """剥响应壳 + 识别错误。

        ⚠️ 2026-10-15 真机结论：业务接口的成功壳是
        ``{"success": true, "code": 200, "message": "successful", "data": {...}}``
        ——**成功码是 200，不是 0**（mock 与部分文档里写 0）。原先只认 0，
        于是白名单一配好，第一个真机调用就被判成 ``code=200 successful`` 的"失败"。
        这里把 0 与 200 都当成功码，失败壳取 ``message``/``msg``（都见过）。
        另有网关错壳 ``gw_err_resp`` 与"直接返回业务对象"两种，一并兼容。
        """
        if not isinstance(data, dict):
            return data
        envelope = data.get("gw_err_resp")
        if isinstance(envelope, dict):
            raise YouzanError(method, envelope.get("err_code"), envelope.get("err_msg"))
        if data.get("success") is False:
            raise YouzanError(method, data.get("code"),
                              data.get("msg") or data.get("message"))
        code = data.get("code")
        if code not in (None, 0, "0", 200, "200"):
            raise YouzanError(method, code, data.get("msg") or data.get("message"))
        if "data" in data:
            return data.get("data")
        return data

    # =================================================================
    # 公共参数查询：类目 / 运费模板 / 店铺
    # =================================================================
    def get_categories(self):
        """商品类目（叶子类目 id 就是发布商品的 ``cid``）。

        ★待核对：返回结构（官方《商品场景》称之为"二维列表"，社区文章又称 cid 为
        "商品分组/标签 id"）—— 上层路由对多种结构做了兼容提取。
        """
        return self._call(self.API_ITEM_CATEGORIES, {}, retry_safe=True)

    def get_delivery_templates(self):
        """运费模板列表（发布商品的 ``delivery_template_id``）。★接口名待核对。"""
        return self._call(self.API_DELIVERY_TEMPLATE, {}, retry_safe=True)

    def get_shop(self):
        """店铺基本信息（连通性自检用）。★接口名待核对。"""
        return self._call(self.API_SHOP_GET, {}, retry_safe=True)

    # =================================================================
    # 图片素材：必须先传素材拿到 image_id，商品接口只收 image_ids
    # =================================================================
    def upload_image_bytes(self, content: bytes, filename="image.jpg"):
        """上传图片素材 → ``{"image_id": ..., "image_url": ...}``。

        ✅ 2026-10-15 真机核对（★3，云测试店铺）：正确写法是
        **multipart/form-data，字段名 `image`，值是原始字节**：

            POST /api/youzan.materials.storage.platform.img.upload/3.0.0?access_token=…
            Content-Type: multipart/form-data;  field: image = <bytes>

        试错记录（这些**都失败**，别再回头试）：
            JSON {"img": URL/base64} / {"file": …}  → code=160400100「file 参数错误」
            JSON {"image": base64/URL}              → gw_err_resp 5001「系统异常」
            multipart 字段名 img / file             → 160400100「file 参数错误」
        成功返回：``{"code":200,"data":{"image_id":9480524327,"image_url":"…","width":800,"height":800}}``
        """
        if not content:
            raise YouzanError("materials.upload", None, "图片内容为空")
        name = (filename or "image.jpg")[:60]
        lower = name.lower()
        mime = ("image/png" if lower.endswith(".png")
                else "image/webp" if lower.endswith(".webp")
                else "image/gif" if lower.endswith(".gif")
                else "image/jpeg")
        return self._call(self.API_IMG_UPLOAD, None, files={"image": (name, content, mime)})

    # =================================================================
    # 商品：创建 / 更新 / 查询 / 上下架
    # =================================================================
    def create_item(self, payload: dict):
        """创建商品（SKU 随商品一起创建）。

        ⚠️ payload 的字段名与结构（price 单位元、image_ids 逗号分隔、sku_stocks 等）
        属 `*` 待核对项，由中台 `_youzan_payload` 组装，联调时对着官方文档逐字核对。
        """
        return self._call(self.API_ITEM_CREATE, payload)

    def update_item(self, payload: dict):
        return self._call(self.API_ITEM_UPDATE, payload)

    def get_item(self, item_id):
        return self._call(self.API_ITEM_GET, {"item_id": str(item_id)}, retry_safe=True)

    def get_item_by_code(self, item_no):
        """按商家商品编码反查（ID 回填/重发去重靠它）。"""
        return self._call(self.API_ITEM_BY_CODE, {"item_no": str(item_no)}, retry_safe=True)

    def get_sku_by_code(self, sku_no):
        """按规格编码反查（最多返回 600 条 SKU）。"""
        return self._call(self.API_SKU_BY_CODE, {"sku_no": str(sku_no)}, retry_safe=True)

    def update_sku(self, payload: dict):
        """改 SKU（可改价）。"""
        return self._call(self.API_SKU_UPDATE, payload)

    def update_quantity(self, payload: dict):
        """改库存（全量/增量，**不能改价**）。"""
        return self._call(self.API_QUANTITY_UPDATE, payload)

    def list_items(self, state="onsale", page_no=1, page_size=20):
        """出售中 / 仓库中的商品列表。"""
        api = self.API_ITEMS_ONSALE if state == "onsale" else self.API_ITEMS_INVENTORY
        return self._call(api, {"page_no": int(page_no), "page_size": int(page_size)},
                          retry_safe=True)

    def set_listing(self, item_id, listing=True):
        """上架 / 下架。★接口名待核对（官方《商品场景》提到支持但未列独立接口名）。"""
        api = self.API_ITEM_LISTING if listing else self.API_ITEM_DELISTING
        return self._call(api, {"item_id": str(item_id)})

    def delete_item(self, item_id):
        """删除商品。★接口名待核对；适配器暂未声明 delete 能力，先不接入编排层。"""
        return self._call(self.API_ITEM_DELETE, {"item_id": str(item_id)})

    # =================================================================
    # 订单（只读）—— 中台「数据面板」用
    #   只返回算指标要用的字段；收件人姓名/电话/地址等隐私一律不透出。
    #   官方建议：交易后间隔 30 秒以上再查；列表为空可以重试。
    # =================================================================
    def list_orders(self, start, end, page_no=1, page_size=20, by="created"):
        """按时间窗拉订单列表。

        ★待核对：时间参数名与格式。这里按公开文档用 ``start_created``/``end_created``
        （值 "YYYY-MM-DD HH:MM:SS"）；按更新时间查时用 ``start_update``/``end_update``。
        """
        key = "created" if by == "created" else "update"
        payload = {
            f"start_{key}": format_day_time(start),
            f"end_{key}": format_day_time(end),
            "page_no": int(page_no),
            "page_size": int(page_size),
        }
        return self._call(self.API_TRADES_SOLD, payload, retry_safe=True)

    def get_order_detail(self, tid):
        """订单详情（金额/商品明细只能从详情取时用）。"""
        return self._call(self.API_TRADE_GET, {"tid": str(tid)}, retry_safe=True)

    @staticmethod
    def _pick(source, *keys):
        """按顺序取第一个非空字段（有赞同义字段在不同接口里命名不一致）。"""
        for key in keys:
            value = (source or {}).get(key)
            if value is not None:
                return value
        return None

    @classmethod
    def summarize_order(cls, order):
        """订单 → 只留算指标要用的字段（买家隐私全部丢弃）。

        ⚠️ 合规：``buyer_key`` 是唯一例外 —— 它**不落库、不透出**，
        只在同步当天内存里用于去重，算完只留个数（与小红书同口径）。
        ⚠️ 字段名与金额单位（元/分）是 `*` 待核对项：真机录一次再校正，
        单位由中台 ``ORDER_AMOUNT_UNIT`` 按平台配置。
        """
        order = order or {}
        return {
            "order_id": cls._pick(order, "tid", "order_no", "order_id"),
            "status": cls._pick(order, "status", "order_status"),
            # 有赞订单时间是字符串 "YYYY-MM-DD HH:MM:SS"（不是时间戳）
            "create_time": cls._pick(order, "created", "create_time", "created_time"),
            "pay_time": cls._pick(order, "pay_time", "payTime"),
            "finish_time": cls._pick(order, "success_time", "finish_time"),
            "cancel_time": cls._pick(order, "cancel_time", "cancelTime"),
            "update_time": cls._pick(order, "update_time", "updateTime"),
            "total_amount": cls._pick(order, "total_fee", "total_amount"),
            "actual_amount": cls._pick(order, "payment", "actual_amount", "pay_amount"),
            "buyer_key": cls._pick(order, "buyer_id", "buyerId", "yz_open_id"),
        }

    @classmethod
    def summarize_order_detail(cls, payload):
        """订单详情 → 指标字段（收件人/买家等隐私一律丢弃，buyer_key 例外见上）。"""
        payload = payload or {}
        order = payload.get("trade") or payload.get("full_order_info") or payload
        if isinstance(order, dict) and isinstance(order.get("order_info"), dict):
            order = order["order_info"]
        return {
            "order_id": cls._pick(order, "tid", "order_id"),
            "status": cls._pick(order, "status", "order_status"),
            "create_time": cls._pick(order, "created", "create_time"),
            "pay_time": cls._pick(order, "pay_time", "payTime"),
            "finish_time": cls._pick(order, "success_time", "finish_time"),
            "cancel_time": cls._pick(order, "cancel_time", "cancelTime"),
            "update_time": cls._pick(order, "update_time", "updateTime"),
            "total_amount": cls._pick(order, "total_fee", "total_amount"),
            "actual_amount": cls._pick(order, "payment", "actual_amount", "pay_amount"),
            "buyer_key": cls._pick(order, "buyer_id", "buyerId", "yz_open_id"),
            "items": cls._order_items(payload),
        }

    @classmethod
    def _order_items(cls, payload):
        """订单里的商品条目（数据面板按商品拆解用）。

        ★待核对：有赞订单详情的明细数组名与字段（这里兼容 order_items / orders /
        full_order_info.orders 三种常见结构，取 item_id/sku_id/num/price）。
        """
        payload = payload or {}
        rows = (payload.get("order_items") or payload.get("orders")
                or (payload.get("full_order_info") or {}).get("orders") or [])
        items = []
        for row in rows:
            row = row or {}
            items.append({
                "product_id": cls._pick(row, "item_id", "itemId"),
                "sku_id": cls._pick(row, "sku_id", "skuId"),
                "title": cls._pick(row, "title", "item_title", "sku_title"),
                "sku_code": cls._pick(row, "outer_sku_id", "outer_item_id", "sku_no"),
                "product_cnt": cls._pick(row, "num", "quantity") or 1,
                "paid_amount": cls._pick(row, "payment", "price", "total_fee"),
                "aftersale_status": cls._pick(row, "refund_status", "aftersale_status"),
                "image": cls._pick(row, "image", "pic_url", "image_url"),
            })
        return items


if __name__ == "__main__":
    # 自检：打印当前模式与接口表，不发任何外网请求（除非 mode=live 且真的调用了业务方法）
    print("YouzanStore 自检：")
    print(f"  网关      {YouzanStore.GATEWAY}")
    print(f"  当前模式  {mock_platform.describe('youzan')}")
    print("  接口表（★=待真机核对）：")
    for _name in dir(YouzanStore):
        if _name.startswith("API_"):
            _api = getattr(YouzanStore, _name)
            if isinstance(_api, tuple):
                print(f"    {_name:26s} {_api[0]}/{_api[1]}")
