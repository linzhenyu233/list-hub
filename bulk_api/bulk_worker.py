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
import ctypes
import threading

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


def _planned_slots():
    """返回 [(shop_id, platform), ...]，即需要起的 worker 组。

    只给「启用 + 该平台已配置 appid」的店铺起线程；某店没配小红书就不起小红书线程。
    """
    slots = []
    for shop in shop_registry.all_shops(only_enabled=True):
        shop_id = shop["shop_id"]
        for platform in ("wechat", "xhs"):
            if shop_registry.has_platform(shop_id, platform):
                slots.append((shop_id, platform))
    return slots


if __name__ == "__main__":
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
