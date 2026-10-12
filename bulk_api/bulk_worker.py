# -*- coding: utf-8 -*-
"""Run persistent publishing workers: per (shop, platform) worker pools.

多店铺版：
  - 启动时读取 shops.json，对每个「启用」店铺的每个「已配置」平台，
    各起 BULK_WORKERS_PER_SHOP 个线程（默认 6）。
  - 每个线程只从 publish_items 里领本店本平台的任务（WHERE shop_id=? AND platform=?），
    因此不会出现 A 店的任务用 B 店凭证发布的情况。
  - 单店回退（无 shops.json）时，行为与改造前完全一致：1 店 × 2 平台 × 6 线程。
"""

import os
import sys
import ctypes
import threading

# 与 bulk_api.py / fastapi_server.py 保持一致，把项目根加入 sys.path：
# 直接跑 `python bulk_api/bulk_worker.py` 时 sys.path[0] 是 bulk_api/ 目录，
# 项目根的 shop_registry 会导入失败（此前只能靠外部 PYTHONPATH 兜着）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import platform_registry
import shop_registry
from bulk_api import _worker_loop, recover_interrupted_items


def acquire_single_instance():
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, True, "Global\\EcommerceBulkPublishWorker")
    if not handle or kernel32.GetLastError() == 183:
        return None
    return handle


def current_session_id():
    """当前进程所在的 Windows 会话号（非 Windows 或取不到时返回 None）。

    会话 0 = 服务会话（SYSTEM / 被服务方式拉起）。它不是登录用户会话，
    **读不到登录用户凭据管理器里的共享盘凭据**，发品读图必然 WinError 1326；
    更麻烦的是它会抢到下面那把「全局单实例互斥锁」，让身份正确的 Worker 永远起不来
    （线上现象：自启日志每次写「已经在运行」，实际干活的是没凭据的那个）。
    """
    if os.name != "nt":
        return None
    try:
        session = ctypes.c_ulong()
        pid = ctypes.windll.kernel32.GetCurrentProcessId()
        if ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(session)):
            return int(session.value)
    except Exception:
        return None
    return None


def check_share_roots():
    """启动自检：各启用店铺的图片根目录能不能读。返回 (可读的店, 读不到的店)。

    共享盘不通的典型表现是「整批商品发布失败」，事后翻日志很费劲，
    所以启动时先探一次并直接打在控制台/日志里。
    """
    readable, broken = [], []
    for shop in shop_registry.all_shops(only_enabled=True):
        root = str(shop.get("image_root") or "").strip()
        if not root:
            continue
        try:
            os.stat(root)
            readable.append(shop["shop_id"])
        except OSError as exc:
            broken.append((shop["shop_id"], root, exc))
    return readable, broken


def _planned_slots():
    """返回 [(shop_id, platform), ...]，即需要起的 worker 组。

    只给「启用 + 该平台已配置凭证主键」的店铺起线程；某店没配小红书就不起小红书线程。
    ⚠️ 平台列表来自 ``platform_registry.all_platforms()``，接新平台时本函数无需改动
    （但线程总数要重新核算：店铺数 × 平台数 × BULK_WORKERS_PER_SHOP）。
    """
    slots = []
    for shop in shop_registry.all_shops(only_enabled=True):
        shop_id = shop["shop_id"]
        for platform in platform_registry.all_platforms():
            if shop_registry.has_platform(shop_id, platform):
                slots.append((shop_id, platform))
    return slots


if __name__ == "__main__":
    # 会话 0（SYSTEM / 服务会话）读不到登录用户的共享盘凭据，必须先拦掉：
    # 否则它抢到单实例锁后，正确身份的 Worker 就再也起不来了。
    session_id = current_session_id()
    if session_id == 0 and os.environ.get("BULK_ALLOW_SESSION0_WORKER") != "1":
        raise SystemExit(
            "拒绝启动：当前进程在 Windows 会话 0（SYSTEM / 服务会话），读不到登录用户的共享盘凭据，"
            "发布商品时会报 WinError 1326。\n"
            "请改用有共享盘权限的登录用户启动（bulk_api\\start_worker.ps1，"
            "或计划任务 BulkWorkerAutostart）；确需在服务会话里跑，"
            "请先设 BULK_ALLOW_SESSION0_WORKER=1 并自行保证该账户能访问共享盘。"
        )

    instance_handle = acquire_single_instance()
    if not instance_handle:
        raise SystemExit("批量发布 Worker 已经在运行，本次不重复启动")

    # 每个 (店铺, 平台) 的线程数：新键 BULK_WORKERS_PER_SHOP 优先，
    # 未设置则回退旧键 BULK_WORKERS_PER_PLATFORM（默认 6），保证单店行为不变。
    worker_count = max(1, int(os.environ.get(
        "BULK_WORKERS_PER_SHOP",
        os.environ.get("BULK_WORKERS_PER_PLATFORM", "6"),
    )))

    slots = _planned_slots()
    if not slots:
        raise SystemExit("没有任何可用店铺（检查 shops.json 或 .env 配置）")

    # 启动自检：共享盘读不了就别等发品时才整批失败
    readable_shops, broken_shops = check_share_roots()
    if broken_shops:
        print("[bulk_worker] 警告：以下店铺的图片根目录当前读不到，这些店的商品发布会失败：")
        for shop_id, root, exc in broken_shops:
            print(f"  - {shop_id}: {root}（{exc}）")
        if not readable_shops:
            raise SystemExit(
                "[bulk_worker] 所有店铺的图片目录都读不到（共享盘未连接，或运行账户没有共享盘凭据）。"
                "现在启动只会整批发失败，请先修好共享盘后再启动。"
            )

    print(f"[bulk_worker] 启动 {len(slots)} 组 worker，每组 {worker_count} 线程：")
    for shop_id, platform in slots:
        print(f"  - {shop_id} / {platform}")

    # 恢复中断任务：按店铺逐个恢复，避免跨店 UPDATE
    for shop_id, _platform in slots:
        recover_interrupted_items(shop_id)

    workers = []
    for shop_id, platform in slots:
        for index in range(worker_count):
            worker = threading.Thread(
                target=_worker_loop,
                args=(platform, shop_id),
                name=f"{shop_id}-{platform}-worker-{index + 1}",
                daemon=False,
            )
            worker.start()
            workers.append(worker)
    for worker in workers:
        worker.join()
