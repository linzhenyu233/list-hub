# -*- coding: utf-8 -*-
"""
微信小店 · 类目查询小工具(按真实返回结构重写)

官方 getallcategory 真实返回(已抓取确认):
    { "errcode":0, "errmsg":"ok", "cats":[ {"cat_and_qua":[ {"cat":{...}, ...}, ...]}, ... ] }
    - cats 在顶层, 没有 data 包装
    - 类目是"扁平列表", 每个节点: cat_id / name / f_cat_id(父级) / level / leaf
    - 没有 children, 靠 f_cat_id 关联; leaf=True 表示叶子(能发商品)

用法:
    python cat_finder.py 珠宝
    python cat_finder.py 钻石 戒指
    python cat_finder.py
"""
import os
import sys
import requests

# ================== 配置区 ==================
APPID = os.environ.get("WX_APPID", "")
SECRET = os.environ.get("WX_SECRET", "")
# ===========================================

BASE = "https://api.weixin.qq.com"


def get_token():
    r = requests.get(f"{BASE}/cgi-bin/token", params={
        "grant_type": "client_credential",
        "appid": APPID,
        "secret": SECRET,
    }, timeout=10)
    d = r.json()
    if "access_token" not in d:
        raise SystemExit(f"[错误] 获取 token 失败: {d}")
    return d["access_token"]


def collect_cat_nodes(obj, nodes):
    """
    递归遍历返回结构,把所有 "cat": {...} 节点收集成扁平列表。
    兼容: cat_and_qua 是数组、cats 是数组、任意嵌套。
    """
    if isinstance(obj, dict):
        if "cat" in obj and isinstance(obj["cat"], dict):
            nodes.append(obj["cat"])
        for v in obj.values():
            collect_cat_nodes(v, nodes)
    elif isinstance(obj, list):
        for v in obj:
            collect_cat_nodes(v, nodes)


def get_all_categories(token):
    """获取全部类目,返回扁平节点列表 [{cat_id, name, f_cat_id, level, leaf, ...}]"""
    for path in ("/channels/ec/category/all", "/shop/ec/category/all"):
        r = requests.get(f"{BASE}{path}", params={"access_token": token}, timeout=30)
        d = r.json()
        if d.get("errcode", 0) != 0:
            print(f"[调试] {path} 报错: {d}")
            continue
        cats = d.get("cats_v2") or d.get("cats") or []   # 新类目树优先(店铺已全切cats_v2,发品用cats_v2字段)
        nodes = []
        collect_cat_nodes(cats, nodes)
        if nodes:
            print(f"[调试] 接口 {path} 成功,共收集 {len(nodes)} 个类目节点")
            return nodes
        print(f"[调试] {path} 通了但没解析到类目, 原始前400字: {str(d)[:400]}")
    raise SystemExit("[错误] 没拿到类目数据, 请把上面的输出发我")


def search(nodes, keyword):
    """按关键词搜索: 匹配名称, 通过 f_cat_id 回溯父级得到完整路径, 每级都带 cat_id"""
    by_id = {}
    for n in nodes:
        if isinstance(n, dict) and n.get("cat_id") is not None:
            by_id[str(n["cat_id"])] = n
    results = []

    for n in nodes:
        if not isinstance(n, dict):
            continue
        name = n.get("name", "")
        if keyword not in name:
            continue
        # 向上回溯, 记录每一级的 (cat_id, name)
        chain, cur, seen = [], n, set()
        while cur and str(cur.get("cat_id")) not in seen:
            seen.add(str(cur.get("cat_id")))
            chain.append((cur.get("cat_id"), cur.get("name", "")))
            pid = cur.get("f_cat_id")
            cur = by_id.get(str(pid)) if pid is not None else None
        chain.reverse()
        # 输出: 一级名(一级ID) > 二级名(二级ID) > 三级名(三级ID)
        path_str = " > ".join(f"{nm}({cid})" for cid, nm in chain)
        results.append((n.get("cat_id"), path_str, bool(n.get("leaf"))))

    return results


if __name__ == "__main__":
    assert APPID.startswith("wx"), "请先设置 WX_APPID/WX_SECRET"

    keywords = sys.argv[1:] or ["项链"]
    print("正在获取类目树(数据较大,可能需要几秒)...")
    token = get_token()
    nodes = get_all_categories(token)
    print(f"获取成功,开始搜索 {keywords}\n")

    for kw in keywords:
        rs = search(nodes, kw)
        print(f"===== 关键词「{kw}」: 共 {len(rs)} 条 =====")
        for cid, path, is_leaf in rs:
            mark = "  ★叶子(可发商品)" if is_leaf else ""
            print(f"  {cid}  {path}{mark}")
        print()
