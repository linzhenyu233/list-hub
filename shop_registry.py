"""多店铺注册表（单实例运行时切换用）。

一个 shop = 一个运营单元（品牌/店铺组），同时包含微信 + 小红书两套配置。
运营在前端切换 shop，切换后批次/任务/图片缓存/类目映射全部按 shop 隔离。
每个平台的凭证与参数挂在 shop.wechat / shop.xhs 下，缺失则该平台不可用。

启动时由三个服务（wechat / xhs / bulk_api）分别读取 ``shops.json`` 并校验；
若根目录不存在 ``shops.json``，自动用现有 ``.env`` 中的 ``WX_*`` / ``XHS_*``
合成一个默认店铺（shop_id = ``default``），行为与改造前完全一致。

对外接口（全部幂等、线程安全）：
    - load_and_validate(force_reload=False)  加载并校验（首次或 force_reload 时读盘）
    - get_shop(shop_id)                     按 ID 取店铺，不存在返回 None
    - default_shop()                        全局默认店铺
    - all_shops(only_enabled=True)          店铺列表
    - resolve(shop_id)                      解析请求上下文：空值回退默认店
    - platform_creds(shop_id, platform)     取某 shop 某平台的凭证+参数
    - has_platform(shop_id, platform)       某 shop 是否配置了某平台
    - redacted_list(only_enabled=True)      脱敏店铺清单（供 GET /shops 使用）
    - all_image_roots(only_enabled=True)    所有启用店的 image_root 并集
    - default_operator()                    默认操作人

P0 交付：本文件 + shops.example.json + runtime_config 扩展 + .gitignore 更新。
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

# --- 常量 -------------------------------------------------------------------

_VALID_PLATFORMS = ("wechat", "xhs")
_SHOP_ID_RE = re.compile(r"^[a-z0-9_]+$")

# 单店回退时使用的默认 shop_id（数据库迁移历史数据回填必须与此一致）
DEFAULT_SHOP_ID = "default"
DEFAULT_SHOP_NAME = "默认店铺"

# 每个平台的必填凭证键（enabled=true 时校验非空才是完整可用状态，允许留空占位）
_REQUIRED_CREDENTIALS = {
    "wechat": ("appid", "secret"),
    "xhs": ("app_id", "app_secret"),
}

# 每个平台的参数键（均允许空字符串占位）
_PARAM_KEYS = {
    "wechat": ("brand_id", "freight_template_id", "after_sale_address_id"),
    "xhs": ("brand_id", "category_id", "shipping_template_id", "logistics_plan_id"),
}

# 从 .env 合成默认店时的字段映射
_ENV_CREDENTIALS_MAP = {
    "wechat": {"appid": "WX_APPID", "secret": "WX_SECRET"},
    "xhs": {
        "app_id": "XHS_APP_ID",
        "app_secret": "XHS_APP_SECRET",
        "access_token": "XHS_ACCESS_TOKEN",
        "refresh_token": "XHS_REFRESH_TOKEN",
    },
}
_ENV_PARAMS_MAP = {
    "wechat": {
        "brand_id": ("WX_BRAND_ID", "2100000000"),  # 微信默认无品牌占位
        "freight_template_id": ("WX_FREIGHT_TEMPLATE_ID", ""),
        "after_sale_address_id": ("WX_AFTER_SALE_ADDRESS_ID", ""),
    },
    "xhs": {
        "brand_id": ("XHS_BRAND_ID", ""),
        "category_id": ("XHS_CATEGORY_ID", ""),
        "shipping_template_id": ("XHS_SHIPPING_TEMPLATE_ID", ""),
        "logistics_plan_id": ("XHS_LOGISTICS_PLAN_ID", ""),
    },
}

# 凭证字段（redacted_list 会剔除这些键）
_SECRET_FIELDS = {
    "secret", "app_secret", "access_token", "refresh_token",
    "session_key", "token",
}

# --- 内部状态 ---------------------------------------------------------------

_lock = threading.RLock()
_loaded = False
_shops: list[dict] = []                   # 全部店铺（含 enabled=false）
_default_shop: dict | None = None          # 默认店铺
_index_by_id: dict[str, dict] = {}         # shop_id -> shop


# --- 公共 API ---------------------------------------------------------------

def load_and_validate(force_reload: bool = False) -> list[dict]:
    """加载 ``shops.json`` 并校验。

    幂等：重复调用直接返回已加载结果；传 ``force_reload=True`` 强制重新读盘。
    返回全部店铺列表（含 ``enabled=false``）。
    校验失败直接抛 ``ValueError`` —— 启动即失败（fail fast）。
    """
    global _loaded, _shops, _default_shop, _index_by_id
    with _lock:
        if _loaded and not force_reload:
            return list(_shops)

        config_path = _shops_json_path()
        if config_path and config_path.is_file():
            shops_raw = _load_from_file(config_path)
            source = "file"
        else:
            shops_raw = [_synthesize_default_from_env()]
            source = "env-fallback"

        _validate_shops(shops_raw)

        _shops = shops_raw
        _index_by_id = {s["shop_id"]: s for s in shops_raw}
        _default_shop = _compute_default(shops_raw)
        _loaded = True

        enabled = [s for s in shops_raw if s.get("enabled", True)]
        print(f"[shop_registry] 已加载 {len(shops_raw)} 家店铺 "
              f"(启用 {len(enabled)})，来源: {source}")
        if _default_shop:
            print(f"[shop_registry] 默认店铺: {_default_shop['shop_id']} ({_default_shop['name']})")

        return list(_shops)


def get_shop(shop_id: str) -> dict | None:
    """按 shop_id 取店铺，不存在返回 None。首次调用自动加载。"""
    load_and_validate()
    with _lock:
        shop = _index_by_id.get(shop_id)
        return _deep_copy(shop) if shop else None


def default_shop() -> dict:
    """取全局默认店铺。若无任何可用店铺，抛 ValueError。"""
    load_and_validate()
    with _lock:
        if not _default_shop:
            raise ValueError("无可用店铺")
        return _deep_copy(_default_shop)


def all_shops(only_enabled: bool = True) -> list[dict]:
    """全部店铺列表（默认店排最前，其余按 shop_id 排序）。"""
    load_and_validate()
    with _lock:
        src = [s for s in _shops if (s.get("enabled", True) if only_enabled else True)]
    src.sort(key=lambda s: (0 if s.get("is_default") else 1, s["shop_id"]))
    return [_deep_copy(s) for s in src]


def resolve(shop_id: str | None) -> dict:
    """解析请求上下文：shop_id 为空则回退默认店，未知/停用则报错。"""
    load_and_validate()
    if not shop_id:
        return default_shop()
    shop = get_shop(shop_id)
    if not shop:
        raise ValueError(f"未知店铺: {shop_id}")
    if not shop.get("enabled", True):
        raise ValueError(f"店铺已停用: {shop_id}")
    return shop


def has_platform(shop_id: str, platform: str) -> bool:
    """某店铺是否配置了指定平台（有凭证字段即视为配置）。"""
    if platform not in _VALID_PLATFORMS:
        raise ValueError(f"非法平台: {platform}")
    shop = resolve(shop_id) if isinstance(shop_id, str) and shop_id else default_shop()
    pconf = shop.get(platform) or {}
    # 有 appid/app_id 字段即视为配置了该平台（secret 可能为空占位）
    key = "appid" if platform == "wechat" else "app_id"
    return bool(pconf.get(key))


def platform_creds(shop_id: str | None, platform: str) -> dict:
    """取某店铺某平台的完整配置（credentials + params 打平）。

    返回结构示例（微信）：
        {"appid": "...", "secret": "...", "brand_id": "...",
         "freight_template_id": "...", "after_sale_address_id": "..."}

    该平台未配置时抛 ValueError。
    """
    if platform not in _VALID_PLATFORMS:
        raise ValueError(f"非法平台: {platform}")
    shop = resolve(shop_id)
    pconf = shop.get(platform) or {}
    key = "appid" if platform == "wechat" else "app_id"
    if not pconf.get(key):
        raise ValueError(f"店铺 {shop['shop_id']} 未配置 {platform} 平台")
    return _deep_copy(pconf)


def redacted_list(only_enabled: bool = True) -> list[dict]:
    """脱敏店铺清单（去掉 secret / token 等密钥字段），供 GET /shops 使用。"""
    shops = all_shops(only_enabled=only_enabled)
    for s in shops:
        _redact_dict(s)
    return shops


def all_image_roots(only_enabled: bool = True) -> list[str]:
    """所有启用店铺的 image_root 并集（去重、保序）。"""
    seen: set[str] = set()
    result: list[str] = []
    for s in all_shops(only_enabled=only_enabled):
        root = s.get("image_root") or ""
        if root and root not in seen:
            seen.add(root)
            result.append(root)
    return result


def image_root_for(shop_id: str) -> str:
    """某店铺的图片根目录，未配置则返回空串。"""
    shop = resolve(shop_id)
    return shop.get("image_root") or ""


def default_operator() -> str:
    """默认操作人，来源：shops.json 顶层 default_operator 或 环境变量 DEFAULT_OPERATOR。"""
    load_and_validate()
    return os.environ.get("DEFAULT_OPERATOR") or os.environ.get("OPERATOR") or "unknown"


# --- 内部：加载 -------------------------------------------------------------

def _shops_json_path() -> Path | None:
    """返回 shops.json 的路径。优先 SHOPS_FILE 环境变量。"""
    from_env = os.environ.get("SHOPS_FILE")
    if from_env:
        return Path(from_env).resolve()
    return Path(__file__).resolve().parent / "shops.json"


def _load_from_file(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"读取店铺配置失败: {path} ({e})") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"shops.json 格式错误: {e}") from e

    shops = data.get("shops")
    if not isinstance(shops, list):
        raise ValueError("shops.json 顶层必须含 shops 数组")

    default_op = data.get("default_operator")
    if isinstance(default_op, str) and default_op:
        os.environ.setdefault("DEFAULT_OPERATOR", default_op)

    # 解析 ${ENV_VAR} 占位符 —— 密钥留在环境变量里，shops.json 只描述结构。
    # 这样部署方式与改造前一致（继续管 .env / 系统环境变量），
    # 且 shops.json 可以安全地复制、传阅、甚至提交。
    try:
        import runtime_config
        runtime_config.ensure_env_loaded()   # 只加载环境变量，不会递归回店铺注册表
    except Exception:
        pass  # 独立使用（如单元测试）时允许未加载，插值会按空值处理并告警

    missing: list[str] = []
    shops = _interpolate(shops, "", missing)
    if missing:
        print("[shop_registry] WARN: shops.json 引用了未设置的环境变量（已按空值处理）:")
        for item in missing:
            print(f"    {item}")
        print("    请把 .env 补齐并重启服务，否则对应店铺不可用。")

    return shops


_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _interpolate(obj, path: str, missing: list):
    """递归把字符串里的 ``${VAR}`` / ``${VAR:-默认值}`` 替换成环境变量值。

    - ``${VAR}``：变量缺失时替换为空串，并登记到 missing（上层打印告警）。
      不直接抛异常，是为了让"某个店少配了一个密钥"不至于导致三个服务全部起不来。
    - ``${VAR:-默认值}``：变量缺失或为空时用默认值，不告警。
    """
    if isinstance(obj, str):
        def repl(match):
            name, default = match.group(1), match.group(2)
            value = os.environ.get(name)
            if value:
                return value
            if default is not None:
                return default
            missing.append(f"{path or '(root)'} -> ${{{name}}}")
            return ""
        return _PLACEHOLDER_RE.sub(repl, obj)
    if isinstance(obj, dict):
        return {k: _interpolate(v, f"{path}.{k}" if path else k, missing) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate(v, f"{path}[{i}]", missing) for i, v in enumerate(obj)]
    return obj


def _synthesize_default_from_env() -> dict:
    """无 shops.json 时，从 .env 合成默认店。与改造前行为完全等价。"""
    wechat_conf = {}
    for field, env_key in _ENV_CREDENTIALS_MAP["wechat"].items():
        wechat_conf[field] = os.environ.get(env_key, "") or ""
    for field, (env_key, default) in _ENV_PARAMS_MAP["wechat"].items():
        wechat_conf[field] = os.environ.get(env_key) or default

    xhs_conf = {}
    for field, env_key in _ENV_CREDENTIALS_MAP["xhs"].items():
        xhs_conf[field] = os.environ.get(env_key, "") or ""
    for field, (env_key, default) in _ENV_PARAMS_MAP["xhs"].items():
        xhs_conf[field] = os.environ.get(env_key) or default

    image_root = (
        os.environ.get("DEFAULT_IMAGE_ROOT")
        or r"\\192.168.10.250\电子商务部\网销部共享\SHINING HOUSE培育钻"
    )

    return {
        "shop_id": DEFAULT_SHOP_ID,
        "name": DEFAULT_SHOP_NAME,
        "enabled": True,
        "is_default": True,
        "wechat": wechat_conf,
        "xhs": xhs_conf,
        "image_root": image_root,
        "remark": "自动合成：从 .env WX_* / XHS_* 单店配置回退",
    }


# --- 内部：校验 -------------------------------------------------------------

def _validate_shops(shops: list[dict]):
    seen_ids: set[str] = set()
    default_count = 0

    for i, s in enumerate(shops):
        prefix = f"shops[{i}]"
        if not isinstance(s, dict):
            raise ValueError(f"{prefix} 必须是对象")

        # shop_id
        sid = s.get("shop_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError(f"{prefix}.shop_id 不能为空")
        if not _SHOP_ID_RE.match(sid):
            raise ValueError(
                f"{prefix}.shop_id='{sid}' 非法，仅允许小写字母/数字/下划线"
            )
        if sid in seen_ids:
            raise ValueError(f"{prefix}.shop_id='{sid}' 重复")
        seen_ids.add(sid)

        # name
        if not isinstance(s.get("name"), str) or not s["name"]:
            raise ValueError(f"{prefix}.name 不能为空")

        # 布尔字段
        for key in ("enabled", "is_default"):
            if key in s and not isinstance(s[key], bool):
                raise ValueError(f"{prefix}.{key} 必须是布尔值")
        if s.get("is_default"):
            default_count += 1

        # 平台配置：wechat / xhs 至少有一个
        has_any = False
        for platform in _VALID_PLATFORMS:
            pconf = s.get(platform)
            if pconf is None:
                # 允许缺失，表示该平台未配置
                continue
            if not isinstance(pconf, dict):
                raise ValueError(f"{prefix}.{platform} 必须是对象")
            key = "appid" if platform == "wechat" else "app_id"
            if pconf.get(key):
                has_any = True
        if not has_any and s.get("enabled", True):
            # 启用的店铺至少得有一个平台配置了 appid
            # 这里不强制报错（允许占位），但打印警告
            print(f"[shop_registry] WARN: {prefix} {sid} 启用但无任何平台凭证")

        # image_root 允许为空
        if "image_root" in s and not isinstance(s["image_root"], str):
            raise ValueError(f"{prefix}.image_root 必须是字符串")

    if default_count > 1:
        raise ValueError(f"存在 {default_count} 个 is_default=true 的店铺，只能有一个")


def _compute_default(shops: list[dict]) -> dict | None:
    """计算默认店铺。

    规则：
      1. 显式 is_default=true 且 enabled=true 的店铺。
      2. 没有的话取第一家 enabled=true 的店铺。
      3. 都没有返回 None。
    """
    enabled = [s for s in shops if s.get("enabled", True)]
    explicit = next((s for s in enabled if s.get("is_default")), None)
    if explicit:
        return explicit
    if enabled:
        return enabled[0]
    return None


# --- 内部：工具 -------------------------------------------------------------

def _deep_copy(obj):
    if isinstance(obj, dict):
        return {k: _deep_copy(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_copy(v) for v in obj]
    return obj


def _redact_dict(d: dict):
    """就地脱敏：把密钥字段替换为 '***'。递归处理嵌套 dict。"""
    for k in list(d.keys()):
        v = d[k]
        if isinstance(v, dict):
            _redact_dict(v)
        elif isinstance(v, str) and (
            k.lower() in _SECRET_FIELDS
            or "secret" in k.lower()
            or "token" in k.lower()
        ):
            if v:
                d[k] = "***"


if __name__ == "__main__":
    import sys
    try:
        shops = load_and_validate()
    except ValueError as e:
        print(f"校验失败: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"共 {len(shops)} 家店铺")
    for s in redacted_list(only_enabled=False):
        platforms = [p for p in _VALID_PLATFORMS if s.get(p, {}).get("appid" if p == "wechat" else "app_id")]
        print(f"  {s['shop_id']} - {s['name']} [{','.join(platforms) or '无平台'}]"
              f"{' (默认)' if s.get('is_default') else ''}"
              f"{' [已停用]' if not s.get('enabled', True) else ''}")
    try:
        d = default_shop()
        print(f"默认店: {d['shop_id']}")
    except ValueError:
        print("默认店: 无")
