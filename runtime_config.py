"""Load project configuration without storing credentials in source code."""

import os
from pathlib import Path


PROJECT_VARIABLES = {
    "WX_APPID", "WX_SECRET", "WX_FREIGHT_TEMPLATE_ID",
    "WX_AFTER_SALE_ADDRESS_ID", "WX_API_HOST", "WX_API_PORT",
    "WX_BRAND_ID",
    "XHS_APP_ID", "XHS_APP_SECRET", "XHS_ACCESS_TOKEN",
    "XHS_REFRESH_TOKEN", "XHS_AUTH_CODE", "XHS_TOKEN_FILE",
    "XHS_API_HOST", "XHS_API_PORT", "GY_APPKEY", "GY_SECRET",
    "GY_SESSIONKEY", "GY_API_URL", "XHS_BRAND_ID", "XHS_CATEGORY_ID",
    "XHS_SHIPPING_TEMPLATE_ID", "XHS_LOGISTICS_PLAN_ID",
    "BULK_API_HOST", "BULK_API_PORT", "BULK_DB_FILE", "BULK_IMAGE_DIR",
    "BULK_WORKERS_PER_PLATFORM", "BULK_WORKERS_PER_SHOP",
    "WECHAT_API_BASE", "XHS_API_BASE",
    "CORS_ORIGINS", "HUOPAI_PATH", "HUOPAI_DIR",
    # 鉴权：三个服务共用的 X-API-Key（见 api_auth.py；未配置则不校验）
    "API_KEY",
    # 以下键代码里在 os.environ.get，但白名单漏了 —— 在 .env 里配了也读不到：
    # 货盘标题品牌（huopai_adapter.py）
    "HUOPAI_WECHAT_BRAND", "HUOPAI_XHS_BRAND",
    # 图片/批次/货盘备份/溯源信息的保留天数（bulk_api.py）
    "BULK_IMAGE_TTL_DAYS", "BULK_BATCH_TTL_DAYS",
    "BULK_MAX_HUOPAI_BACKUPS", "BULK_HUOPAI_BACKUP_TTL_DAYS",
    "BULK_SOURCE_INFO_TTL_DAYS",
    # 多店铺
    "SHOPS_FILE", "DEFAULT_OPERATOR", "OPERATOR",
    "XHS_TOKEN_DIR",
    # 各店图片根目录（shop_registry.py 读）
    "DEFAULT_IMAGE_ROOT",
}

# 各店自己的平台凭证：键名约定 = 原键名 + "_" + SHOP_ID 大写，
# 例：WX_APPID_XIANLUODINGZHI / WX_SECRET_XIANLUODINGZHI
#     （shops.json 里照常写 ${WX_APPID_XIANLUODINGZHI} 占位）
# 这类键不进 PROJECT_VARIABLES，而是按前缀放行 —— 否则每新增一家店都要改本文件，
# 忘改就会静默读不到（.env 与用户环境变量都会被过滤），排查成本很高。
_PER_SHOP_PREFIXES = (
    "WX_APPID_", "WX_SECRET_", "WX_BRAND_ID_",
    "WX_FREIGHT_TEMPLATE_ID_", "WX_AFTER_SALE_ADDRESS_ID_",
    "XHS_APP_ID_", "XHS_APP_SECRET_", "XHS_BRAND_ID_", "XHS_CATEGORY_ID_",
    "XHS_SHIPPING_TEMPLATE_ID_", "XHS_LOGISTICS_PLAN_ID_",
)


def _is_allowed_variable(name: str) -> bool:
    """白名单：显式列出的项目变量，或"某店自己的平台凭证"（见 _PER_SHOP_PREFIXES）。"""
    return name in PROJECT_VARIABLES or name.startswith(_PER_SHOP_PREFIXES)

# ensure_env_loaded() 的幂等标记
_ENV_LOADED = False


def _load_dotenv(path):
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        if _is_allowed_variable(key) and value:
            os.environ.setdefault(key, value)


def _load_windows_user_environment():
    if os.name != "nt":
        return
    try:
        import winreg

        # 枚举用户环境变量后按白名单过滤：不能只遍历 PROJECT_VARIABLES，
        # 否则各店自己的凭证（如 WX_APPID_XIANLUODINGZHI）读不到。
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            index = 0
            while True:
                try:
                    name, value, _type = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if not value or os.environ.get(name):
                    continue
                if _is_allowed_variable(name):
                    os.environ[name] = str(value)
    except OSError:
        pass


def load_project_env(load_shops: bool = True):
    """加载项目环境变量，可选地初始化店铺注册表。

    ``load_shops=False`` 用于只加载 env 不初始化店铺的场景（如纯工具脚本）。
    三个服务（wechat / xhs / bulk_api）按默认值调用，启动即完成店铺注册表加载。
    """
    ensure_env_loaded()
    if load_shops:
        # 延迟 import，避免循环依赖（shop_registry 内部会 import runtime_config 的部分常量）
        import shop_registry
        shop_registry.load_and_validate()


def ensure_env_loaded():
    """幂等地加载 .env + Windows 用户环境变量（只真正加载一次）。

    单独抽出来是给 shop_registry 用的：它需要在解析 shops.json 里的
    ``${ENV_VAR}`` 占位符之前，确保环境变量已就位。若直接在 load_project_env
    里做插值会形成 ``load_project_env -> load_and_validate -> load_project_env``
    的循环，所以这里只做"加载环境变量"这一件事，不碰店铺注册表。
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _load_dotenv(Path(__file__).resolve().parent / ".env")
    _load_windows_user_environment()
    _ENV_LOADED = True
