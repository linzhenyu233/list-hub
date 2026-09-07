# -*- coding: utf-8 -*-
"""
微信小店 · Token 连通性测试(最小脚本)

只做一件事:验证 AppID + AppSecret 能不能换到 access_token。
不碰商品、不上传图片、不影响店铺任何数据。

用法:
    1. 设置下面的 APPID / SECRET(或环境变量 WX_APPID / WX_SECRET)
    2. 运行:  python test_token.py
    3. 看输出: 成功 → 打印 access_token;失败 → 打印微信返回的错误原因
"""
import os
import requests

APPID = os.environ.get("WX_APPID", "")
SECRET = os.environ.get("WX_SECRET", "")
# ===================================================

BASE = "https://api.weixin.qq.com"


def main():
    # 1. 检查有没有填
    assert APPID.startswith("wx"), "[错误] AppID 还没填(或不是 wx 开头)"
    assert SECRET and "替换" not in SECRET, "[错误] AppSecret 还没填"

    # 2. 调微信的 token 接口
    print(f"正在用 AppID: {APPID} 换取 access_token ...")
    resp = requests.get(
        f"{BASE}/cgi-bin/token",
        params={
            "grant_type": "client_credential",   # 固定写法:客户端凭证模式
            "appid": APPID,
            "secret": SECRET,
        },
        timeout=10,
    )

    # 3. 解析返回结果
    data = resp.json()
    print("微信返回:", data)
    print("-" * 50)

    # 成功:返回里有 access_token
    if "access_token" in data:
        token = data["access_token"]
        expires = data.get("expires_in", 7200)
        print("✅ TOKEN 测试通过!")
        print(f"   access_token: {token[:30]}...")   # 只显示前30位,别全暴露
        print(f"   有效期: {expires} 秒 ({expires/3600:.1f} 小时)")
        print(f"   结论: AppID/AppSecret 正确, 凭证可用")
    else:
        # 失败:看 errcode / errmsg 判断原因
        code = data.get("errcode")
        msg = data.get("errmsg", "未知错误")
        print("❌ TOKEN 测试失败")
        print(f"   errcode: {code}")
        print(f"   errmsg:  {msg}")
        print()
        # 常见错误对照
        tips = {
            40001: "AppSecret 错误,或 AppSecret 被重置过(去小店后台重新生成)",
            40013: "AppID 格式不对,检查是不是复制错了",
            40125: "AppSecret 无效(可能被重置,后台重新生成)",
            -1:    "系统繁忙,稍后重试",
        }
        # 根据 errcode 打印对应原因
        if code in tips:
            print(f"   💡 原因: {tips[code]}")
        # 其他错误码
        else:
            print("   💡 检查: AppID/AppSecret 是否复制完整、有没有多余空格")


if __name__ == "__main__":
    main()
