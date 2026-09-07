# -*- coding: utf-8 -*-
"""
====================================================================
 双平台图片上传脚本:微信小店 + 小红书
====================================================================
把本地图片分别上传到微信小店和小红书开放平台,
拿到两个平台返回的图片链接,保存到本地文件。

用法:
    python upload_images.py                      # 上传 images/ 目录下所有图片
    python upload_images.py a.jpg b.png          # 指定文件
    python upload_images.py D:/图片目录           # 指定目录

输出:
    uploaded_images.json    结构化结果(文件名 + 两平台URL)
    uploaded_images.txt     每行: 文件名 | 微信URL | 小红书URL
====================================================================
"""
import base64
import hashlib
import json
import os
import struct
import sys
import time

import requests

from runtime_config import load_project_env

load_project_env()

# ------------------------------------------------------------------
# 配置区(换成你自己的凭证)
# ------------------------------------------------------------------
# 微信小店
WX_APPID = os.environ.get("WX_APPID", "")
WX_SECRET = os.environ.get("WX_SECRET", "")
# 小红书开放平台
XHS_APP_ID = os.environ.get("XHS_APP_ID", "")
XHS_APP_SECRET = os.environ.get("XHS_APP_SECRET", "")
XHS_ACCESS_TOKEN = os.environ.get("XHS_ACCESS_TOKEN", "")

# 默认输入目录 / 输出文件
DEFAULT_INPUT_DIR = "images"
OUTPUT_JSON = "uploaded_images.json"
OUTPUT_TXT = "uploaded_images.txt"

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


# ==================================================================
# 工具:读取图片宽高(免装 PIL,支持 PNG / JPEG)
# ==================================================================
def image_size(path):
    """返回 (width, height);解析失败返回 (0, 0)"""
    with open(path, "rb") as f:
        data = f.read()
    # PNG:签名后第 16~23 字节是宽高(大端)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    # JPEG:扫描段标记,在 SOF 段读取宽高
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seg_len
    return 0, 0


def collect_images(args):
    """从命令行参数收集待上传图片;无参数时默认取 images/ 目录"""
    files = []
    if not args:
        if not os.path.isdir(DEFAULT_INPUT_DIR):
            print(f"默认目录 {DEFAULT_INPUT_DIR}/ 不存在,请传图片文件或目录")
            sys.exit(1)
        args = [DEFAULT_INPUT_DIR]
    for arg in args:
        if os.path.isdir(arg):
            for name in sorted(os.listdir(arg)):
                if os.path.splitext(name)[1].lower() in IMG_EXTS:
                    files.append(os.path.join(arg, name))
        elif os.path.isfile(arg) and os.path.splitext(arg)[1].lower() in IMG_EXTS:
            files.append(arg)
        else:
            print(f"跳过(非图片或不存在): {arg}")
    return files


# ==================================================================
# 微信小店:获取 access_token(带缓存)
# ==================================================================
_wx_token = None


def wx_get_token():
    global _wx_token
    if _wx_token:
        return _wx_token
    r = requests.get("https://api.weixin.qq.com/cgi-bin/token",
                     params={"grant_type": "client_credential",
                             "appid": WX_APPID, "secret": WX_SECRET}, timeout=15)
    d = r.json()
    if "access_token" not in d:
        raise RuntimeError(f"微信 token 获取失败: {d}")
    _wx_token = d["access_token"]
    return _wx_token


def upload_wx(path):
    """微信小店:二进制流上传,返回图片永久链接"""
    token = wx_get_token()
    w, h = image_size(path)
    with open(path, "rb") as f:
        files = {"media": (os.path.basename(path), f)}
        resp = requests.post(
            "https://api.weixin.qq.com/shop/ec/basics/img/upload",
            params={"access_token": token, "upload_type": 0,
                    "resp_type": 1, "width": w, "height": h},
            files=files, timeout=120,
        )
    d = resp.json()
    if d.get("errcode", 0) != 0:
        raise RuntimeError(f"微信上传失败: {d}")
    # 兼容多种返回结构(顶层 img_url/url,或 pic_file 里的 img_url/temp_img_url)
    pic = d.get("pic_file") or {}
    url = (d.get("img_url") or d.get("url")
           or pic.get("img_url") or pic.get("temp_img_url"))
    if not url:
        raise RuntimeError(f"微信上传成功但没拿到链接: {d}")
    return url


# ==================================================================
# 小红书:签名 + 上传素材(material.uploadMaterial)
# ==================================================================
def xhs_sign(method, timestamp):
    """小红书签名: method?appId=..&timestamp=..&version=2.0 + appSecret, MD5"""
    raw = f"{method}?appId={XHS_APP_ID}&timestamp={timestamp}&version=2.0{XHS_APP_SECRET}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def upload_xhs(path):
    """小红书:图片 base64 上传素材,返回素材 URL"""
    timestamp = str(int(time.time()))
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    body = {
        "timestamp": timestamp,
        "appId": XHS_APP_ID,
        "sign": xhs_sign("material.uploadMaterial", timestamp),
        "version": "2.0",
        "method": "material.uploadMaterial",
        "accessToken": XHS_ACCESS_TOKEN,
        "name": os.path.basename(path)[:40],
        "type": "IMAGE",
        "materialContent": b64,
    }
    resp = requests.post(
        "https://ark.xiaohongshu.com/ark/open_api/v3/common_controller",
        json=body, timeout=120,
    )
    d = resp.json()
    if d.get("error_code") != 0 or not d.get("success"):
        raise RuntimeError(f"小红书上传失败: {d}")
    url = (d.get("data") or {}).get("url")
    if not url:
        raise RuntimeError(f"小红书上传成功但没拿到 url: {d}")
    return url


# ==================================================================
# 主流程:逐张上传,收集结果,保存到本地
# ==================================================================
def main():
    files = collect_images(sys.argv[1:])
    if not files:
        print("没有找到可上传的图片")
        sys.exit(1)

    print(f"共 {len(files)} 张图片,开始上传...")
    results = []
    for path in files:
        name = os.path.basename(path)
        row = {"file": name, "wx_url": None, "xhs_url": None, "error": None}
        print(f"\n[上传中] {name}")
        # 微信小店
        try:
            row["wx_url"] = upload_wx(path)
            print(f"  微信小店: OK -> {row['wx_url'][:80]}...")
        except Exception as e:
            row["error"] = f"微信:{e}"
            print(f"  微信小店: 失败 -> {e}")
        # 小红书
        try:
            row["xhs_url"] = upload_xhs(path)
            print(f"  小红书:   OK -> {row['xhs_url'][:80]}...")
        except Exception as e:
            row["error"] = (row["error"] + ";" if row["error"] else "") + f"小红书:{e}"
            print(f"  小红书:   失败 -> {e}")
        results.append(row)

    # 保存结果
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    with open(OUTPUT_TXT, "w", encoding="utf-8") as f:
        for r in results:
            f.write(f"{r['file']} | {r['wx_url'] or '-'} | {r['xhs_url'] or '-'}\n")

    ok = sum(1 for r in results if r["wx_url"] and r["xhs_url"])
    print(f"\n完成: {len(results)} 张,双平台都成功 {ok} 张")
    print(f"结果已保存: {OUTPUT_JSON} / {OUTPUT_TXT}")


if __name__ == "__main__":
    main()
