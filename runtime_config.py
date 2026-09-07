"""Load project configuration without storing credentials in source code."""

import os
from pathlib import Path


PROJECT_VARIABLES = {
    "WX_APPID", "WX_SECRET", "WX_FREIGHT_TEMPLATE_ID",
    "WX_AFTER_SALE_ADDRESS_ID", "WX_API_HOST", "WX_API_PORT",
    "XHS_APP_ID", "XHS_APP_SECRET", "XHS_ACCESS_TOKEN",
    "XHS_REFRESH_TOKEN", "XHS_AUTH_CODE", "XHS_TOKEN_FILE",
    "XHS_API_HOST", "XHS_API_PORT", "GY_APPKEY", "GY_SECRET",
    "GY_SESSIONKEY", "GY_API_URL", "XHS_BRAND_ID", "XHS_CATEGORY_ID",
    "XHS_SHIPPING_TEMPLATE_ID", "XHS_LOGISTICS_PLAN_ID",
    "BULK_API_HOST", "BULK_API_PORT", "BULK_DB_FILE", "BULK_IMAGE_DIR",
    "BULK_WORKERS_PER_PLATFORM", "WECHAT_API_BASE", "XHS_API_BASE",
    "CORS_ORIGINS",
}


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


def load_project_env():
    _load_dotenv(Path(__file__).resolve().parent / ".env")
    _load_windows_user_environment()
