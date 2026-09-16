# -*- coding: utf-8 -*-
"""用授权 code 换取小红书 accessToken，按店铺写入 xhs/tokens/<shop_id>.json。

用法（二选一）：

    # 推荐：code 走环境变量，避免留在命令行历史里
    set XHS_AUTH_CODE=code-xxxxxxxx
    python get_access_token.py --shop zuanshishijia

    # 或直接传参
    python get_access_token.py --shop zuanshishijia --code "code-xxxxxxxx"

不带 --shop 时使用店铺注册表里的默认店。

⚠️ 本文件是入库的，**绝不能写死 app_secret / code**。
   凭证一律从 shops.json（其密钥位是 ${XHS_APP_ID} / ${XHS_APP_SECRET} 占位）
   解析到项目根 .env / 系统环境变量。本脚本会自动加载 .env，
   所以直接在命令行跑就能拿到值，不需要任何硬编码默认值。

⚠️ token 必须落在 xhs/tokens/<shop_id>.json —— 服务端就是按这个路径读的。
   不要再往项目根写 token_store.json：那个位置会被 xhs_api 的
   _migrate_legacy_token() 当成"单店时代遗留"，复制给**默认店**，造成串店。
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent
# 从项目根加载 .env + Windows 用户环境变量
# （这一步过去缺失，正是有人被迫把 app_secret 写死在本文件里的原因）
sys.path.insert(0, str(_BASE_DIR.parent))
from runtime_config import load_project_env  # noqa: E402

load_project_env()

import shop_registry  # noqa: E402
from xhs_store import XhsStore  # noqa: E402

# 必须与 xhs_api.py 的 TOKEN_DIR / _token_file 保持一致，否则服务端读不到
TOKEN_DIR = os.environ.get("XHS_TOKEN_DIR") or str(_BASE_DIR / "tokens")
LEGACY_TOKEN_FILE = _BASE_DIR / "token_store.json"


def token_file(shop_id: str) -> Path:
    return Path(TOKEN_DIR) / f"{shop_id}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="小红书授权：用 code 换取按店铺保存的 token")
    parser.add_argument("--shop", default="", help="店铺 shop_id；不传则用默认店")
    parser.add_argument("--code", default="", help="授权回调里的 code；不传则读环境变量 XHS_AUTH_CODE")
    args = parser.parse_args()

    # ---- 解析店铺 ----
    # 用 get_shop 而不是 resolve：新店上线前 enabled=false，也要能先完成授权。
    if args.shop:
        shop = shop_registry.get_shop(args.shop)
        if not shop:
            print(f"错误：未知店铺 {args.shop}", file=sys.stderr)
            return 1
    else:
        shop = shop_registry.default_shop()

    shop_id = shop["shop_id"]
    conf = shop.get("xhs") or {}
    app_id = conf.get("app_id") or ""
    app_secret = conf.get("app_secret") or ""
    if not app_id or not app_secret:
        print(f"错误：店铺 {shop_id} 的 xhs.app_id / app_secret 为空。"
              f"请确认 .env 里的 XHS_APP_ID / XHS_APP_SECRET 已配置"
              f"（shops.json 里这两项是 ${{XHS_APP_ID}} / ${{XHS_APP_SECRET}} 占位）。",
              file=sys.stderr)
        return 1

    code = args.code or os.environ.get("XHS_AUTH_CODE", "") or ""
    if not code:
        print("错误：没有拿到授权 code。请用 --code 传入，或设置环境变量 XHS_AUTH_CODE。", file=sys.stderr)
        return 1

    if not shop.get("enabled", True):
        print(f"提示：店铺 {shop_id} 当前 enabled=false（还没上线），本次只做授权、不影响发布。")

    # ---- 换 token ----
    client = XhsStore(app_id, app_secret)
    try:
        data = client.get_access_token(code)
    except Exception as exc:
        print(f"错误：换取 token 失败：{exc}", file=sys.stderr)
        print("     常见原因：code 已过期或已被使用（code 是一次性的，几分钟内有效）。", file=sys.stderr)
        return 1

    access = data.get("accessToken") or data.get("access_token") or ""
    refresh = data.get("refreshToken") or data.get("refresh_token") or ""
    if not access:
        print("错误：授权接口没有返回 accessToken。code 可能已过期/已被使用。", file=sys.stderr)
        return 1

    expire_at = int(time.time()) + int(data.get("expiresIn") or data.get("expires_in") or 7 * 24 * 3600)
    target = token_file(shop_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"accessToken": access, "refreshToken": refresh, "expireAt": expire_at},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"授权成功。店铺 {shop_id}（{shop['name']}）的 token 已写入：")
    print(f"  {target}")
    print(f"  accessToken:  已保存（长度 {len(access)}）")
    print(f"  refreshToken: {'已保存' if refresh else '未返回'}")
    print(f"  有效期至:     {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expire_at))}")

    # ---- 安全提醒：不要让遗留文件重新出现 ----
    if LEGACY_TOKEN_FILE.exists():
        print()
        print("⚠️ 检测到遗留文件 xhs/token_store.json。")
        print("   它会被服务端当作『单店时代遗留』复制给**默认店**，可能造成 token 串店。")
        print(f"   请删除：{LEGACY_TOKEN_FILE}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
