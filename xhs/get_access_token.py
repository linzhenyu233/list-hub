import json
import os
import time
from pathlib import Path

from xhs_store import XhsStore


client = XhsStore(
    os.environ.get("XHS_APP_ID", ""),
    os.environ.get("XHS_APP_SECRET", ""),
)
code = os.environ.get("XHS_AUTH_CODE", "")
if not code:
    raise RuntimeError("请先设置 XHS_AUTH_CODE 环境变量")

data = client.get_access_token(code)
token_file = Path(os.environ.get(
    "XHS_TOKEN_FILE",
    str(Path(__file__).resolve().with_name("token_store.json")),
))
token_file.parent.mkdir(parents=True, exist_ok=True)
token = {
    "accessToken": data.get("accessToken", ""),
    "refreshToken": data.get("refreshToken", ""),
    "expireAt": int(time.time()) + int(data.get("expiresIn", 7 * 24 * 3600)),
}
token_file.write_text(json.dumps(token, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"授权成功，Token 已保存到: {token_file}")
print(f"accessToken saved: {bool(token['accessToken'])}")
print(f"refreshToken saved: {bool(token['refreshToken'])}")
