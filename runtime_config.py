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
    # 多店铺
    "SHOPS_FILE", "DEFAULT_OPERATOR", "OPERATOR",
    "XHS_TOKEN_DIR",
}

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
        if key in PROJECT_VARIABLES and value:
            os.environ.setdefault(key, value)


def _load_windows_user_environment():
    if os.name != "nt":
        return
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            for name in PROJECT_VARIABLES:
                if os.environ.get(name):
                    continue
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                if value:
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
