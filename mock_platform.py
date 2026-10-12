# -*- coding: utf-8 -*-
"""三级模式（mock / sandbox / live）与 mock 数据回放 —— 平台服务侧的唯一开关。

背景
----
软著下来之前，三个平台服务（wechat / xhs，以及后续 douyin / jd / tmall）**拿不到正式
网关权限**，于是"把导入 → 映射 → 发布 → 状态 → 回填这条链路跑通"这件事被卡住：
没有真网关，就只能等；等的时候编排层（bulk_api）的改动完全无法验证。

本模块给出三级模式，让"没有真平台"不再是开发阻塞：

    mock     平台服务**不发出真实外网请求**，按 fixture 返回可预期的响应；
             所有写操作返回假的（但确定的）商品 ID。现在是默认开发模式。
    sandbox  打到沙箱网关（京东/天猫有，抖音可能没有 —— 用 `*` 标注，软著后核对）。
    live     打到正式网关（上线）。**默认值**，所以不配置时行为与改造前完全一致。

模式由**平台服务**读取生效（`wechat_store` / `xhs_store` 的出口都在本模块），
中台 bulk_api **不参与判断**，它只把模式当**标签**用于统计隔离（见 §mode 标签）。

四个必须守住的约束
------------------
1. **响应壳与真实一致**：mock 返回的是平台原样的响应体（小红书
   ``{"error_code":0,"success":true,"data":{...}}``、微信 ``{"errcode":0,...}``），
   **不是**中台自己的简化结构。否则"响应解析 / 错误归一化"这套代码要等真联调才第一次执行，
   等于把最容易出错的地方留到压力最大的时候才试。
2. **确定性**：同一 ``product_code``（或商品名）算出的假 ID 恒定，多次发布结果一致，
   这样回归脚本可以比对（``bulk_api/multishop_regression_test.py``）。
3. **可注入故障**：``MOCK_SCENARIO`` 支持 timeout / rate_limit / auth_expired / param_error，
   分别验证重试退避、token 刷新、进失败清单四条路径（见 §故障注入）。
4. **录制必脱敏**：``MOCK_RECORD=1`` 时把 live 的真实响应录成 fixture 复用，
   但 ``app_secret`` / ``access_token`` / 买家信息一律替换成 ``***``，
   否则 fixture 会成为新的密钥泄漏点。

相关开关一览
------------
    BULK_MODE_<PLATFORM>    把某平台临时切到 mock/sandbox/live（优先级最高）
    shops.json <平台>.mode   常规切换方式（按店按平台）
    MOCK_SCENARIO            注入故障：timeout / rate_limit / auth_expired / param_error
    MOCK_SCENARIO_FAIL_ALWAYS=1  故障不自动恢复（验证"进失败清单"而不是"重试后成功"）
    MOCK_RECORD=1            live 模式下把真实响应录成 fixture（自动脱敏）
    MOCK_FIXTURE_MAP         指定某接口回放哪个 fixture：``接口=文件名;接口2=文件名2``

模式解析优先级（单一事实来源，中台与平台服务共用同一个函数，不会两边打架）
------------------------------------------------------------------------
    1. 环境变量 ``BULK_MODE_<PLATFORM>``（如 ``BULK_MODE_XHS=mock``）—— 临时单店/单平台试
    2. ``shops.json`` 里该店该平台段的 ``"mode"`` —— 常规切换方式
    3. ``live`` —— 缺省。**不配置就与改造前完全一致**，这是本模块的兼容性底线。

⚠️ 三个服务都没有 ``--reload``，改 ``shops.json`` 或环境变量后必须重启**对应**的平台服务。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import requests

import platform_registry

# --- 模式常量 ----------------------------------------------------------------

MOCK = "mock"
SANDBOX = "sandbox"
LIVE = "live"
VALID_MODES = (MOCK, SANDBOX, LIVE)

BASE_DIR = Path(__file__).resolve().parent

# 故障注入场景（§故障注入）
SCENARIOS = ("timeout", "rate_limit", "auth_expired", "param_error")

# 写操作（非幂等）方法名特征 —— 决定超时后**能否**自动重试：
# 创建类接口超时是"不知道平台到底建没建"，重试可能建出两件商品（重复上架，运营要手工删），
# 所以超时只对查询类重试；调用方也可以显式传 retry_safe 覆盖本判断。
_WRITE_MARKERS = (
    "create", "add", "update", "delete", "upload", "listing", "delisting",
    "save", "submit", "modify", "remove", "initupload", "uploadpart", "finishupload",
)

_lock = threading.RLock()
# 故障注入已触发次数 {(platform, method_key): n} —— 进程内有效，reset_faults() 可清
_fault_state: dict[tuple[str, str], int] = {}


# --- 模式解析 ----------------------------------------------------------------

def env_mode_key(platform: str) -> str:
    """该平台的模式覆盖环境变量名（``BULK_MODE_XHS``）。"""
    return f"BULK_MODE_{str(platform or '').upper()}"


def resolve_mode(platform: str, shop_id: str | None = None) -> str:
    """解析当前生效的模式（mock / sandbox / live）。未知值按 ``live`` 处理并告警。

    非法值不抛异常是有意的：模式写错一个字（如 ``Mock ``）不应该让发布链路直接挂掉，
    但要能在日志里看见，所以打印 WARN。
    """
    platform = str(platform or "").strip()
    env_value = str(os.environ.get(env_mode_key(platform)) or "").strip().lower()
    if env_value:
        return _validated(platform, env_value, f"环境变量 {env_mode_key(platform)}")
    file_value = _mode_from_shops(platform, shop_id)
    if file_value:
        return _validated(platform, file_value, f"shops.json({shop_id or '默认店'}.{platform}.mode)")
    return LIVE


def _validated(platform: str, value: str, source: str) -> str:
    if value in VALID_MODES:
        return value
    print(f"[mock] WARN: {source} 的 mode='{value}' 非法"
          f"（合法值 {', '.join(VALID_MODES)}），按 live 处理", flush=True)
    return LIVE


def _mode_from_shops(platform: str, shop_id: str | None) -> str:
    """从 shops.json 读该店该平台段的 mode；读不到返回空串。"""
    try:
        import shop_registry
        shop = shop_registry.get_shop(shop_id) if shop_id else shop_registry.default_shop()
    except Exception:
        return ""
    conf = (shop or {}).get(platform) or {}
    return str(conf.get("mode") or "").strip().lower()


def is_mock(platform: str, shop_id: str | None = None) -> bool:
    """该平台该店当前是否 mock 模式。"""
    return resolve_mode(platform, shop_id) == MOCK


def describe(platform: str, shop_id: str | None = None) -> str:
    """给人看的一行描述（日志/接口回显用）。"""
    mode = resolve_mode(platform, shop_id)
    sc = scenario()
    return f"{platform}@{shop_id or '默认店'} mode={mode}" + (f" scenario={sc}" if sc else "")


# --- 故障注入 ----------------------------------------------------------------

def scenario() -> str:
    """当前注入的故障场景（``MOCK_SCENARIO``），未设置返回空串。"""
    return str(os.environ.get("MOCK_SCENARIO") or "").strip().lower()


def reset_faults():
    """清空故障注入计数（回归脚本用：每条用例都从"第一次调用"开始）。"""
    with _lock:
        _fault_state.clear()


def _fault_hit(platform: str, method_key: str) -> int:
    """该 (平台, 接口) 已经注入过几次故障。"""
    with _lock:
        key = (platform, method_key)
        n = _fault_state.get(key, 0)
        _fault_state[key] = n + 1
        return n


def _fault_response(platform: str, method_key: str, stage: str) -> "MockResponse | None":
    """按场景造一个"故障响应"；场景与阶段不匹配时返回 None（表示正常放行）。

    设计要点：**同一次进程内，(平台, 接口) 的第一次调用注入故障，第二次起恢复正常**。
    这样"重试后成功"才可复现 —— 只注入一次故障，剩下的交给被验证的重试/刷新逻辑去恢复；
    若需要"怎么重试都失败、必须进失败清单"，用 ``MOCK_SCENARIO_FAIL_ALWAYS=1``。
    """
    sc = scenario()
    if not sc or sc not in SCENARIOS:
        return None
    always = str(os.environ.get("MOCK_SCENARIO_FAIL_ALWAYS") or "") == "1"
    if not always and _fault_hit(platform, method_key) > 0:
        return None
    if sc == "timeout":
        # 网络超时不是"响应"，要抛异常才能走到重试/退避或失败分支
        if stage == "pre":
            raise requests.exceptions.Timeout(f"[mock] 注入故障: timeout ({platform} {method_key})")
        return None
    if sc == "rate_limit":
        return MockResponse(200, _error_envelope(platform, "rate_limit", method_key))
    if sc == "auth_expired":
        return MockResponse(200, _error_envelope(platform, "auth_expired", method_key))
    if sc == "param_error":
        return MockResponse(200, _error_envelope(platform, "param_error", method_key))
    return None


def _error_envelope(platform: str, kind: str, method_key: str) -> dict:
    """平台原样的错误响应壳。

    错误码取各平台文档化的值；新平台的码是 `*` 待核对项（软著后对着真实响应校正，
    录制回放会把真实错误壳沉淀成 fixture，所以这里的偏差不会影响流程，只影响提示文案）。
    """
    if platform == "xhs":
        code = {"rate_limit": 429, "auth_expired": 401, "param_error": 400}[kind]
        return {"error_code": code, "success": False,
                "error_msg": f"[mock] {kind} ({method_key})", "data": None}
    if platform == "wechat":
        code = {"rate_limit": 45009, "auth_expired": 40001, "param_error": 40002}[kind]
        return {"errcode": code, "errmsg": f"[mock] {kind} ({method_key})"}
    return {"error": f"[mock] {kind} ({method_key})"}


def is_write_method(platform: str, method_key: str) -> bool:
    """猜这个接口是不是写操作（非幂等）。用于决定超时能不能自动重试。"""
    text = str(method_key or "").lower()
    return any(marker in text for marker in _WRITE_MARKERS)


# --- 响应对象 ----------------------------------------------------------------

class MockResponse:
    """``requests.Response`` 的鸭子类型子集。

    只实现平台服务真正用到的那几项（``status_code`` / ``text`` / ``json()``），
    这样 mock 可以在**HTTP 出口处**替换真实响应，让签名之外的所有解析、错误处理、
    重试代码原样跑一遍 —— 而不是在业务函数里到处插 ``if mock``。
    """

    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return json.loads(self.text)

    def __repr__(self):  # 方便日志里直接看到内容
        return f"<MockResponse {self.status_code} {self.text[:200]}>"


# --- 网关出口（平台服务唯一出口） --------------------------------------------

def gateway_call(platform: str, url: str, *, method_key: str, body: dict | None = None,
                 params: dict | None = None, timeout: int = 15, shop_id: str | None = None,
                 http_get: bool = False, files: dict | None = None, form: bool = False,
                 retries: int = 2, backoff: float = 0.4,
                 retry_safe: bool | None = None):
    """平台服务发往平台网关的**唯一出口**：模式判定 → mock 回放/真实请求 → 重试退避 → 录制。

    参数：
        method_key  平台接口标识（小红书=method 名如 product.createItemAndSku；
                    微信=URL path 如 /channels/ec/product/add）。用于 fixture 命名与故障注入。
        retry_safe  超时后能否重试。None=按 is_write_method() 推断（写操作不重试）。
    返回：真实 ``requests.Response`` 或 ``MockResponse``（鸭子类型一致）。
    """
    mode = resolve_mode(platform, shop_id)
    recording = str(os.environ.get("MOCK_RECORD") or "") == "1"

    def call_once():
        """发一次请求：mock 模式回放/合成，其余模式走真实网关。"""
        if mode == MOCK:
            return _mock_call(platform, method_key, body, shop_id)
        resp = _real_call(platform, url, body=body, params=params, timeout=timeout,
                          http_get=http_get, files=files, form=form)
        if recording:
            _record(platform, method_key, body or params, resp)
        return resp

    if retry_safe is None:
        retry_safe = not is_write_method(platform, method_key)
    attempts = max(1, int(retries) + 1) if retry_safe else 1

    last_exc = None
    resp = None
    for attempt in range(attempts):
        try:
            resp = call_once()
        except requests.exceptions.RequestException as exc:
            # 超时是"不知道平台到底收没收到"：写操作不重试（可能建出两件商品），读操作退避重试
            last_exc = exc
            if attempt + 1 < attempts:
                _log_retry(platform, method_key, attempt, f"网络异常: {exc}")
                time.sleep(backoff * (2 ** attempt))
                continue
            raise
        # 平台明确回「请求太频繁」= 本次没有产生副作用，退避后重试是安全的
        if _is_rate_limited(platform, resp) and attempt + 1 < attempts:
            _log_retry(platform, method_key, attempt, "平台限流")
            time.sleep(backoff * (2 ** attempt))
            continue
        return resp
    if resp is not None:    # 重试用尽：把最后一次的响应交回调用方（错误处理在服务层）
        return resp
    if last_exc:
        raise last_exc
    raise RuntimeError(f"[mock] {platform} {method_key} 请求失败")


def _log_retry(platform: str, method_key: str, attempt: int, why: str):
    print(f"[mock] {platform} {method_key} 第 {attempt + 1} 次重试（{why}）", flush=True)


def _real_call(platform: str, url: str, *, body, params, timeout, http_get, files, form=False):
    headers = {"Content-Type": "application/json;charset=utf-8"}
    if http_get:
        return requests.get(url, params=params or {}, timeout=timeout)
    if files is not None:
        return requests.post(url, params=params or {}, files=files, timeout=timeout)
    if form:
        return requests.post(url, params=params or {}, data=body or {}, timeout=timeout)
    # ⚠️ JSON POST 也必须带上 params：微信小店的 access_token 放在 URL 查询串里，
    #    这一支漏掉 params 会让所有微信接口报 `41001 access_token missing`
    #    （mock 模式不校验 query，所以 mock 烟测发现不了，只有真实调用才暴露）。
    return requests.post(url, headers=headers, json=body or {}, params=params or {}, timeout=timeout)


def _mock_call(platform: str, method_key: str, body: dict | None,
               shop_id: str | None) -> MockResponse:
    """mock 模式下的响应：故障注入 > 录制回放 > 手写 fixture > 合成。"""
    injected = _fault_response(platform, method_key, "pre")   # timeout 会在这里抛异常
    if injected is not None:
        print(f"[mock] {platform} {method_key} 注入故障场景 {scenario()}", flush=True)
        return injected

    fixture = _load_fixture(platform, method_key)
    if fixture is not None:
        print(f"[mock] {platform} {method_key} 回放 fixture", flush=True)
        return MockResponse(fixture.get("__http_status__", 200), fixture)

    payload = _synthesize(platform, method_key, body or {})
    print(f"[mock] {platform} {method_key} 合成响应（无 fixture）", flush=True)
    return MockResponse(200, payload)


def _is_rate_limited(platform: str, resp) -> bool:
    """响应体里是否出现该平台的限流错误码。"""
    try:
        markers = platform_registry.adapter(platform)["rate_limit_markers"]
    except Exception:
        markers = ()
    if not markers:
        return False
    return any(marker in _response_text(resp) for marker in markers)


def _response_text(resp) -> str:
    try:
        return resp.text or ""
    except Exception:
        return ""


# --- fixture：加载 / 录制 ----------------------------------------------------

def _sanitize(method_key: str) -> str:
    """接口标识 → 文件名（``product.createItemAndSku`` → ``product_createItemAndSku``）。"""
    text = str(method_key or "unknown").strip()
    for ch in ("/", "\\", "?", ":", "*", '"', "<", ">", "|", "#", " "):
        text = text.replace(ch, "_")
    return text.lstrip("_") or "unknown"


def fixture_dir(platform: str) -> Path:
    """该平台服务的 fixture 目录：``<service_dir>/fixtures``。"""
    return BASE_DIR / platform_registry.adapter(platform)["service_dir"] / "fixtures"


def _index_map(platform: str) -> dict:
    """``fixtures/_index.json``：method_key → 文件名/说明。缺失或损坏时返回空表。"""
    path = fixture_dir(platform) / "_index.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("fixtures") or {}
    except Exception as exc:
        print(f"[mock] WARN: {path} 解析失败，按无索引处理: {exc}", flush=True)
        return {}


def _fixture_override(platform: str) -> dict:
    """``MOCK_FIXTURE_MAP`` 显式指定某接口回放哪个 fixture 文件。

    格式：``接口标识=文件名;接口2=文件名2``（分号/换行分隔）。
    用途：一个接口有多个"剧本"（成功 / 商品重名被拒 / 类目无权限），
    断言失败清单时用它切到负面剧本，而不用改代码或删文件。
    """
    raw = str(os.environ.get("MOCK_FIXTURE_MAP") or "")
    table = {}
    for item in raw.replace("\n", ";").split(";"):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key.strip() and value.strip():
            table[key.strip()] = value.strip()
    return table


def _load_fixture(platform: str, method_key: str):
    """按「显式指定 > 录制 > 索引 > 约定文件名」顺序找 fixture，返回**平台原样的响应体**。"""
    directory = fixture_dir(platform)
    name = _sanitize(method_key)

    override = _fixture_override(platform).get(method_key)
    if override:
        candidate = directory / override
        if candidate.is_file():
            return _unwrap_record(candidate)

    recorded = directory / "_recorded" / f"{name}.json"
    if recorded.is_file():
        return _unwrap_record(recorded)

    entry = _index_map(platform).get(method_key) or {}
    file_name = entry.get("file") if isinstance(entry, dict) else None
    candidate = directory / (file_name or f"{name}.json")
    if candidate.is_file():
        return _unwrap_record(candidate)
    return None


def _unwrap_record(path: Path):
    """读 fixture：录制文件是 ``{"request":...,"response":{...}}``，手写文件就是响应体本身。

    手写剧本里以 ``_`` 开头的键（``_comment`` / ``_enable`` 之类）是给人看的注释，这里剔掉，
    保证回放出去的仍然是与真实网关一致的纯响应壳。
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[mock] WARN: fixture {path} 解析失败: {exc}", flush=True)
        return None
    if isinstance(doc, dict) and "response" in doc and "recorded_at" in doc:
        return doc["response"]
    if isinstance(doc, dict):
        return {key: value for key, value in doc.items() if not str(key).startswith("_")}
    return doc


def _record(platform: str, method_key: str, request_body, resp):
    """把 live 的真实响应录成 fixture（脱敏后）。任何异常都只告警，绝不打断发布。"""
    try:
        payload = resp.json()
    except Exception:
        return
    directory = fixture_dir(platform) / "_recorded"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        doc = {
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "method_key": method_key,
            "request": mask(request_body),
            "response": payload,
        }
        (directory / f"{_sanitize(method_key)}.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[mock] 已录制 {platform} {method_key} → {directory.name}/"
              f"{_sanitize(method_key)}.json（密钥已脱敏）", flush=True)
    except Exception as exc:
        print(f"[mock] WARN: 录制 {platform} {method_key} 失败: {exc}", flush=True)


# 脱敏：键名里带这些词的一律替换（大小写不敏感）
_SECRET_KEY_MARKERS = ("secret", "token", "sign", "password", "appkey", "sessionkey",
                       "buyer", "receiver", "phone", "mobile")
_SECRET_EXTRA_KEYS = {"code", "auth_code"}   # 授权码也是一次性凭证，别进 fixture


def mask(obj):
    """递归脱敏：密钥/令牌/签名/买家信息 → ``***``。"""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if isinstance(key, str) and (
                    any(marker in key.lower() for marker in _SECRET_KEY_MARKERS)
                    or key.lower() in _SECRET_EXTRA_KEYS):
                out[key] = "***" if value not in (None, "") else value
            else:
                out[key] = mask(value)
        return out
    if isinstance(obj, list):
        return [mask(item) for item in obj]
    return obj


# --- 合成响应（没有 fixture 时） ---------------------------------------------

def mock_id(platform: str, seed: str) -> str:
    """确定性的假 ID：同一 seed 永远得到同一个 ID（回归可比对）。

    - 小红书 itemId 是 24 位十六进制（与真实形态一致，`platform_item_state` 的解析不会走岔）
    - 微信 product_id 是纯数字（微信侧 ID 为数值字符串）
    """
    digest = hashlib.md5(f"{platform}:{seed}".encode("utf-8")).hexdigest()
    if platform == "xhs":
        return digest[:24]
    return str(int(digest[:15], 16))


def _seed_of(body: dict) -> str:
    """从请求体里挑一个稳定的种子：优先商品编码/货号，其次标题，最后整个请求体。"""
    for key in ("out_product_id", "outer_product_id", "productCode", "product_code", "erpCode",
                "product_id", "itemId", "item_id", "skuId", "sku_id"):
        value = body.get(key)
        if value:
            return str(value)
    item = body.get("item") if isinstance(body.get("item"), dict) else body
    for key in ("name", "title", "productName"):
        value = item.get(key)
        if value:
            return str(value)
    return json.dumps(body, ensure_ascii=False, sort_keys=True)[:200]


def _mock_date_of_request(body: dict, *keys):
    """从请求体里取时间区间起点 → 'YYYYMMDD'（mock 订单/售后按天稳定生成）。

    支持微信的嵌套区间（create_time_range.start_time）与小红书的扁平字段（startTime）。
    毫秒时间戳（>=1e12）自动折算成秒。
    """
    for key in keys:
        value = body.get(key)
        if isinstance(value, dict):
            value = value.get("start_time") if value.get("start_time") is not None else value.get("startTime")
        if value is None:
            continue
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            continue
        if seconds >= 10 ** 12:
            seconds //= 1000
        return time.strftime("%Y%m%d", time.localtime(seconds))
    return "19700101"


def _mock_day_seconds(day: str, hour: int) -> int:
    """'YYYYMMDD' + 小时 → 该天该小时的秒级时间戳（取不到就退回固定值）。"""
    try:
        stamp = time.mktime(time.strptime(day + f" {hour:02d}:00:00", "%Y%m%d %H:%M:%S"))
        return int(stamp)
    except (ValueError, OverflowError):
        return 1700000000 + hour * 3600


def _synthesize(platform: str, method_key: str, body: dict):
    """按平台响应壳合成一个"结构正确、值确定"的响应。

    ⚠️ 字段名按接口清单里的公开文档写；带 `*` 的（订单、库存、状态枚举）软著后要用
    `MOCK_RECORD=1` 录一次真实响应校正 —— 合成值的用途是"让流程跑通"，不是"替代文档"。
    """
    key = str(method_key or "").lower()
    seed = _seed_of(body)

    if platform == "xhs":
        data = _xhs_data(key, seed, body)
        return {"error_code": 0, "success": True, "data": data}
    if platform == "wechat":
        return _wechat_envelope(key, seed, body)
    # 新平台（阶段⑤）：先给一个统一的最小壳，接入时按接口清单替换本函数
    return {"errcode": 0, "errmsg": "ok", "data": {}}


def _xhs_data(key: str, seed: str, body: dict):
    # --- 订单 / 售后（数据面板用；字段名与单位按小红书公开文档写，软著后录真机校正）---
    if "order.getorderlist" in key:
        day = _mock_date_of_request(body, "startTime", "start_time")
        base_ts = _mock_day_seconds(day, 12)
        orders = []
        for index in (1, 2):
            orders.append({
                "orderId": f"mock-xhs-order-{day}-{index}",
                "orderNo": f"MOCK{day}{index}",
                "orderStatus": 30 if index == 1 else 100,   # 一条在途、一条已完成
                "orderType": 1,
                # 真机字段名是 createdTime/paidTime/finishTime/cancelTime,且是**毫秒**
                "createdTime": (base_ts + index * 60) * 1000,
                "paidTime": (base_ts + index * 60) * 1000,
                "finishTime": (base_ts + index * 120) * 1000 if index == 2 else 0,
                "cancelTime": 0,
                "updateTime": (base_ts + index * 90) * 1000,
                # 真实列表接口**不返回金额**(实测 26 个字段里没有金额) →
                # mock 也刻意不给,让"逐单补详情"这条路径在 mock 下也走到
                "orderAfterSalesStatus": 1,
            })
        return {"total": len(orders), "pageNo": int(body.get("pageNo") or 1),
                "pageSize": int(body.get("pageSize") or 20), "maxPageNo": 1, "orderList": orders}
    if "order.getorderdetail" in key:
        # 订单详情:付款金额 totalPayAmount(**单位分**),与真机一致(109900 分 = 1099.00 元)。
        # 这里刻意只回金额不回时间:时间由列表接口负责,避免两处时间打架。
        order_id = str(body.get("orderId") or "mock-xhs-order-19700101-1")
        index = 2 if order_id.endswith("-2") else 1
        amount = (1999 + index * 800) * 100
        return {"orderId": order_id, "orderStatus": 30 if index == 1 else 100,
                "totalPayAmount": amount, "merchantActualReceiveAmount": amount}
    if "after" in key and "sale" in key and "list" in key:
        day = _mock_date_of_request(body, "startTime", "start_time")
        base_ts = _mock_day_seconds(day, 15)
        rows = [{"afterSaleId": f"mock-xhs-aftersale-{day}-1",
                 "orderId": f"mock-xhs-order-{day}-1",
                 "refundStatus": "REFUND_SUCCESS",
                 "refundAmountYuan": 999.0,          # 单位疑似「元」
                 "expectedRefundAmountYuan": 999.0,
                 "createTime": base_ts, "updateTime": base_ts + 300, "refundTime": base_ts + 600}]
        return {"total": len(rows), "pageNo": int(body.get("pageNo") or 1),
                "pageSize": int(body.get("pageSize") or 20), "maxPageNo": 1, "simpleAfterSaleList": rows}
    if "oauth" in key:
        return {"accessToken": f"mock-access-{seed[:8]}", "refreshToken": f"mock-refresh-{seed[:8]}",
                "expiresIn": 7 * 24 * 3600}
    if "getiteminfo" in key or "itemdetail" in key or ("item" in key and "info" in key):
        item_id = str(body.get("itemId") or body.get("item_id") or mock_id("xhs", seed))
        # 结构对齐公开文档：商品信息在 itemInfo，规格在 skuInfos。
        # 中台核对发布状态时读 result.itemInfo / skuInfos，壳不对就会把在售商品误判成"查不到"。
        return {"itemInfo": {"itemId": item_id,
                             "name": body.get("name") or f"mock 商品 {seed[:12]}",
                             "status": "on_shelf"},
                "skuInfos": [{"id": mock_id("xhs", "sku:" + seed), "erpCode": seed, "buyable": True}]}
    if "itemlist" in key or "search" in key or "itemstatus" in key:
        return {"list": [], "total": 0, "itemList": []}
    if "brand" in key:
        return {"brands": [{"id": "mock_brand_1", "name": "mock 品牌"}], "total": 1}
    if "categor" in key:
        return {"categories": [{"id": "mock_cat_1", "name": "mock 类目", "isLeaf": True}], "total": 1}
    if "carriage" in key or "template" in key:
        return {"templates": [{"templateId": "mock_tpl_1", "name": "mock 运费模板"}]}
    if "logistics" in key:
        return {"planInfoList": [{"planInfoId": "mock_plan_1", "name": "mock 物流方案"}]}
    if "attribute" in key or "variation" in key:
        return {"attributes": [], "variations": [], "values": []}
    if "upload" in key or "material" in key:
        return {"url": f"https://mock.local/material/{seed[:8]}.jpg", "fileId": seed[:8]}
    if "createsku" in key or "addsku" in key:
        return {"skuId": mock_id("xhs", "sku:" + seed)}
    if "createitem" in key or "additem" in key:
        return {"itemId": mock_id("xhs", seed)}
    return {}


def _wechat_envelope(key: str, seed: str, body: dict):
    base = {"errcode": 0, "errmsg": "ok"}
    # --- 订单 / 售后（数据面板用）---
    # ⚠️ 字段名按微信官方文档写（金额单位：分；时间：秒级），软著后要用 MOCK_RECORD=1 录真机校正。
    if "order/list/get" in key:
        # 微信只给订单ID，且必须带时间区间；mock 按"请求里的那天"稳定生成 3 单。
        day = _mock_date_of_request(body, "create_time_range", "update_time_range")
        return {**base, "order_id_list": [f"mock-order-{day}-{i}" for i in (1, 2, 3)],
                "has_more": False, "next_key": ""}
    if "order/get" in key:
        order_id = str(body.get("order_id") or "mock-order-19700101-1")
        parts = order_id.rsplit("-", 2)
        day = parts[-2] if len(parts) >= 3 and len(parts[-2]) == 8 else "19700101"
        index = int(parts[-1]) if parts[-1].isdigit() else 1
        pay_ts = _mock_day_seconds(day, 10) + index * 300
        amount = 399900 + index * 10000                     # 分
        return {**base, "order": {
            "order_id": order_id,
            "status": 100 if index == 3 else 30,            # 3 号单已完成，便于演示"成交"口径
            "create_time": pay_ts - 120, "update_time": pay_ts + 60,
            "order_detail": {
                "price_info": {"order_price": amount, "product_price": amount, "freight": 0},
                "pay_info": {"pay_time": pay_ts},
                "delivery_info": {"ship_done_time": 0 if index == 2 else pay_ts + 3600},
                # 完成时间必须落在**同一天**，否则"汇总成交笔数"永远为 0（演示时看不出效果）
                "ext_info": {"confirm_receipt_time": pay_ts + 7200 if index == 3 else 0},
                "refund_info": {"refund_freight": 0},
                "product_infos": [{"product_id": mock_id("wechat", seed),
                                   "sku_id": mock_id("wechat", "sku:" + seed),
                                   "product_cnt": 1, "real_price": amount,
                                   "on_aftersale_sku_cnt": 0, "finish_aftersale_sku_cnt": 0}],
            },
            "aftersale_detail": {"on_aftersale_order_cnt": 0},
        }}
    if "aftersale/getaftersalelist" in key:
        day = _mock_date_of_request(body, "begin_create_time", "begin_update_time")
        return {**base, "after_sale_order_id_list": [f"mock-aftersale-{day}-1"],
                "has_more": False, "next_key": ""}
    if "aftersale/getaftersaleorder" in key:
        aftersale_id = str(body.get("after_sale_order_id") or "mock-aftersale-19700101-1")
        parts = aftersale_id.rsplit("-", 2)
        day = parts[-2] if len(parts) >= 3 and len(parts[-2]) == 8 else "19700101"
        complete_ts = _mock_day_seconds(day, 15)
        return {**base, "after_sale_order": {
            "after_sale_order_id": aftersale_id,
            "order_id": f"mock-order-{day}-1",
            "status": "MERCHANT_REFUND_SUCCESS", "type": "REFUND", "sub_type": "DEFAULT",
            "refund_info": {"amount": 99900},               # 分
            "create_time": complete_ts - 3600, "update_time": complete_ts, "complete_time": complete_ts,
            "product_info": {"product_id": mock_id("wechat", seed),
                             "sku_id": mock_id("wechat", "sku:" + seed), "count": 1},
        }}
    if "funds/getfundsflowlist" in key:
        return {**base, "flow_ids": [], "has_more": False, "next_key": ""}
    if "compass/shop/overall/get" in key:
        # 罗盘数据概览:按 ds 稳定生成(金额单位分),让数据面板在 mock 下也有"官方口径"数字
        day = str(body.get("ds") or "19700101")
        variant = sum(ord(ch) for ch in day) % 3
        pay_gmv = 373100 if variant == 0 else 0
        return {**base, "data": {
            "pay_gmv": str(pay_gmv),
            "pay_uv": "1" if pay_gmv else "0",
            "pay_order_cnt": "1" if pay_gmv else "0",
            "pay_refund_gmv": str(pay_gmv),
            "live_pay_gmv": str(pay_gmv), "feed_pay_gmv": "0",
            "product_click_uv": str(4 + variant * 6),
        }}
    if "token" in key:
        return {**base, "access_token": f"mock-access-{seed[:8]}", "expires_in": 7200}
    if "img/upload" in key or "video" in key:
        return {**base, "pic_file": {"img_url": f"https://mock.local/wechat/{seed[:8]}.jpg"}}
    if "product/add" in key:
        return {**base, "data": {"product_id": mock_id("wechat", seed)}}
    if "product/get" in key:
        # ⚠️ 商品对象在**顶层** product（不是 data.product）：中台核对发布状态时读
        #    result.product.status，status=6 表示回收站（后台已删除）。
        return {**base, "product": {
            "product_id": str(body.get("product_id") or mock_id("wechat", seed)),
            "status": 5, "title": body.get("title") or seed}}
    if "product/listing" in key or "product/delisting" in key or "product/update" in key \
            or "product/delete" in key:
        return {**base, "data": {"product_id": mock_id("wechat", seed)}}
    if "category" in key:
        # 类目树：客户端是"递归找所有 cat 节点"（_collect_cat_nodes），
        # 所以这里给一个最小可用的三层树，mock 下发品才能选到叶子类目。
        return {**base, "cats_v2": [{"cat": {"cat_id": "600001", "name": "mock 一级类目"},
                                     "children_v2": [{"cat": {"cat_id": "600002", "name": "mock 二级类目"},
                                                      "children_v2": [{"cat": {"cat_id": "600003",
                                                                               "name": "mock 叶子类目",
                                                                               "leaf": True}}]}]}],
                "product_qua_list": []}
    return {**base, "data": {}}


if __name__ == "__main__":
    # 自检：打印当前各平台解析到的模式 + 一次合成响应，便于确认开关是否生效
    print("mock_platform 自检：")
    for _p in platform_registry.all_platforms():
        _entry = platform_registry.adapter(_p)
        print(f"  {_p:8s} {_entry['label']:6s} {describe(_p)}"
              f"  fixture 目录={fixture_dir(_p)}")
    print("\n合成响应示例（不会发外网请求）：")
    print("  小红书 product.createItemAndSku ->",
          json.dumps(_synthesize("xhs", "product.createItemAndSku",
                                 {"item": {"name": "示例商品名"}}), ensure_ascii=False))
    print("  微信 /channels/ec/product/add ->",
          json.dumps(_synthesize("wechat", "/channels/ec/product/add",
                                 {"title": "示例商品名"}), ensure_ascii=False))
    print("\n脱敏示例：", json.dumps(mask({"app_secret": "s3cr3t", "accessToken": "t0ken",
                                          "item": {"name": "标题"}}), ensure_ascii=False))
