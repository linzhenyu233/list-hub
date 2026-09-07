# -*- coding: utf-8 -*-
"""Run persistent publishing workers: two workers for each platform."""

import os
import ctypes
import threading

from bulk_api import _worker_loop, recover_interrupted_items


def acquire_single_instance():
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, True, "Global\\EcommerceBulkPublishWorker")
    if not handle or kernel32.GetLastError() == 183:
        return None
    return handle


if __name__ == "__main__":
    instance_handle = acquire_single_instance()
    if not instance_handle:
        raise SystemExit("批量发布 Worker 已经在运行，本次不重复启动")
    recover_interrupted_items()
    workers = []
    worker_count = max(1, int(os.environ.get("BULK_WORKERS_PER_PLATFORM", "2")))
    for platform in ("wechat", "xhs"):
        for index in range(worker_count):
            worker = threading.Thread(target=_worker_loop, args=(platform,),
                                      name=f"{platform}-worker-{index + 1}", daemon=False)
            worker.start()
            workers.append(worker)
    for worker in workers:
        worker.join()
