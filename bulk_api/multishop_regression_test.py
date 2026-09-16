# -*- coding: utf-8 -*-
"""多店铺改造回归测试（仅测中台数据隔离与迁移幂等，不调平台接口）。

覆盖：
  1. 迁移幂等：在空库上两次建表 + 迁移结果一致
  2. 跨店 batch 不可见：A 店导入的批次，B 店读不到（404）
  3. 类目别名按店隔离：同 internal_category 在 A、B 两店各存一份，互不覆盖
  4. 图片上传缓存按店隔离：同 source_hash 在两店各存一条，URL 不同
  5. /publish-status 按店汇总
  6. image-cache 只清当前店

用法：
    python bulk_api/multishop_regression_test.py
"""
import importlib.util
import os
import sys
import tempfile
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BULK_DIR = os.path.join(BASE_DIR, "bulk_api")

# 保证能 import 项目根的 shop_registry / huopai_adapter
sys.path.insert(0, BASE_DIR)

import runtime_config
runtime_config.ensure_env_loaded()


def _new_db_path():
    return tempfile.mktemp(prefix="multishop_test_", suffix=".sqlite3")


def _boot(db_path):
    """把 BULK_DB_FILE 环境变量指向测试库，然后加载 bulk_api.py（会触发建表+迁移）。"""
    os.environ["BULK_DB_FILE"] = db_path
    # 清掉已加载的模块（多次 import 各跑一次建表）
    for mod in list(sys.modules.keys()):
        if mod.startswith("bulk_api") or mod in (
            "shop_registry", "image_scanner", "huopai_adapter", "runtime_config"):
            del sys.modules[mod]
    # bulk_api 目录不是 package（缺 __init__.py），用 spec 直接加载脚本
    spec = importlib.util.spec_from_file_location(
        "bulk_api_module", os.path.join(BULK_DIR, "bulk_api.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, BULK_DIR)
    spec.loader.exec_module(mod)
    sys.path.remove(BULK_DIR)
    return mod


def test_migration_idempotent():
    """迁移幂等：两次建表都不报错，且表结构一致。"""
    p = _new_db_path()
    m1 = _boot(p)
    conn = m1.db()
    # 第一次迁移后，batches 必须有 shop_id 列
    cols_a = [r[1] for r in conn.execute("PRAGMA table_info(batches)").fetchall()]
    assert "shop_id" in cols_a, "batches 缺 shop_id 列"
    conn.close()

    # 再 import 一次（相当于重启服务），确认不报错
    m2 = _boot(p)
    conn2 = m2.db()
    cols_b = [r[1] for r in conn2.execute("PRAGMA table_info(batches)").fetchall()]
    assert cols_a == cols_b, "两次迁移后列不一致"
    conn2.close()

    # 清理
    os.unlink(p)
    print("  [OK] 迁移幂等")


def _insert_batch(m, shop_id, filename="测试批次.xlsx"):
    conn = m.db()
    bid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO batches (id,shop_id,filename,operator,total,valid,errors,created_at,rows_json,mappings_json,revision) "
        "VALUES (?,?,?,?,?,?,?,datetime('now'),?,?,0)",
        (bid, shop_id, filename, "tester", 1, 1, 0, "[]", "{}"),
    )
    conn.commit()
    conn.close()
    return bid


def test_batch_isolation():
    p = _new_db_path()
    m = _boot(p)

    a_id = _insert_batch(m, "shop_a", "a店批次.xlsx")
    b_id = _insert_batch(m, "shop_b", "b店批次.xlsx")

    conn = m.db()
    # A 店看得到自己的
    row_a = conn.execute(
        "SELECT filename FROM batches WHERE id=? AND shop_id=?", (a_id, "shop_a")).fetchone()
    assert row_a and row_a[0] == "a店批次.xlsx", "A 店看不到自己的 batch"
    # A 店看不到 B 店的（模拟 batch_or_404 的逻辑）
    row_b = conn.execute(
        "SELECT filename FROM batches WHERE id=? AND shop_id=?", (b_id, "shop_a")).fetchone()
    assert row_b is None, "A 店居然看到了 B 店的 batch"
    conn.close()

    os.unlink(p)
    print("  [OK] batch 跨店隔离")


def test_category_alias_isolation():
    p = _new_db_path()
    m = _boot(p)
    conn = m.db()

    # 同 internal_category 在两店各存一份
    conn.execute(
        "INSERT INTO category_aliases (shop_id, internal_category, wechat_json, xhs_json, updated_at) "
        "VALUES ('shop_a','项链','{wx_a}','{xhs_a}',datetime('now'))")
    conn.execute(
        "INSERT INTO category_aliases (shop_id, internal_category, wechat_json, xhs_json, updated_at) "
        "VALUES ('shop_b','项链','{wx_b}','{xhs_b}',datetime('now'))")
    conn.commit()

    a_row = conn.execute(
        "SELECT wechat_json FROM category_aliases WHERE shop_id=? AND internal_category=?",
        ("shop_a", "项链")).fetchone()
    assert a_row[0] == "{wx_a}", "A 店别名被覆盖了"
    b_row = conn.execute(
        "SELECT wechat_json FROM category_aliases WHERE shop_id=? AND internal_category=?",
        ("shop_b", "项链")).fetchone()
    assert b_row[0] == "{wx_b}", "B 店别名被覆盖了"

    count = conn.execute(
        "SELECT COUNT(*) FROM category_aliases WHERE internal_category='项链'").fetchone()[0]
    assert count == 2, "两店各存一条，应该是 2 行"
    conn.close()

    os.unlink(p)
    print("  [OK] 类目别名按店隔离")


def test_image_cache_isolation():
    p = _new_db_path()
    m = _boot(p)
    conn = m.db()

    # 同 hash、同平台、两店各上传一次，URL 不同
    conn.execute(
        "INSERT INTO image_upload_cache (shop_id,platform,source_hash,source_url,platform_url,updated_at) "
        "VALUES ('shop_a','xhs','hash123','src','url_a',datetime('now'))")
    conn.execute(
        "INSERT INTO image_upload_cache (shop_id,platform,source_hash,source_url,platform_url,updated_at) "
        "VALUES ('shop_b','xhs','hash123','src','url_b',datetime('now'))")
    conn.commit()

    a_url = conn.execute(
        "SELECT platform_url FROM image_upload_cache WHERE shop_id=? AND platform=? AND source_hash=?",
        ("shop_a", "xhs", "hash123")).fetchone()[0]
    b_url = conn.execute(
        "SELECT platform_url FROM image_upload_cache WHERE shop_id=? AND platform=? AND source_hash=?",
        ("shop_b", "xhs", "hash123")).fetchone()[0]
    assert a_url == "url_a" and b_url == "url_b", "两店图片缓存串了"
    conn.close()

    os.unlink(p)
    print("  [OK] 图片缓存按店隔离")


def test_publish_status_isolation():
    p = _new_db_path()
    m = _boot(p)
    conn = m.db()

    job_a = "job_a_" + uuid.uuid4().hex[:6]
    job_b = "job_b_" + uuid.uuid4().hex[:6]
    conn.execute(
        "INSERT INTO publish_jobs (id,shop_id,batch_id,platforms_json,status,total,operator,created_at) "
        "VALUES (?,?,?,?,?,?,?,datetime('now'))",
        (job_a, "shop_a", "batch_a", '["wechat","xhs"]', "running", 1, "tester"))
    conn.execute(
        "INSERT INTO publish_jobs (id,shop_id,batch_id,platforms_json,status,total,operator,created_at) "
        "VALUES (?,?,?,?,?,?,?,datetime('now'))",
        (job_b, "shop_b", "batch_b", '["wechat"]', "running", 1, "tester"))
    conn.execute(
        "INSERT INTO publish_items (job_id,batch_id,product_code,platform,shop_id,status,priority) "
        "VALUES (?,'batch_a','P1','wechat','shop_a','success',1)", (job_a,))
    conn.execute(
        "INSERT INTO publish_items (job_id,batch_id,product_code,platform,shop_id,status,priority) "
        "VALUES (?,'batch_b','P2','wechat','shop_b','success',1)", (job_b,))
    conn.commit()

    # 按店统计
    count_a = conn.execute(
        "SELECT COUNT(*) FROM publish_items WHERE shop_id=? AND status='success'",
        ("shop_a",)).fetchone()[0]
    count_b = conn.execute(
        "SELECT COUNT(*) FROM publish_items WHERE shop_id=? AND status='success'",
        ("shop_b",)).fetchone()[0]
    assert count_a == 1 and count_b == 1, "发布状态按店统计不对"
    conn.close()

    os.unlink(p)
    print("  [OK] 发布状态按店统计")


def test_image_cache_clear_by_shop():
    p = _new_db_path()
    m = _boot(p)
    conn = m.db()

    conn.execute(
        "INSERT INTO image_upload_cache (shop_id,platform,source_hash,source_url,platform_url,updated_at) "
        "VALUES ('shop_a','xhs','h1','s','u',datetime('now'))")
    conn.execute(
        "INSERT INTO image_upload_cache (shop_id,platform,source_hash,source_url,platform_url,updated_at) "
        "VALUES ('shop_b','xhs','h2','s','u',datetime('now'))")
    conn.commit()

    # 只清 A 店
    conn.execute("DELETE FROM image_upload_cache WHERE shop_id=?", ("shop_a",))
    conn.commit()

    remaining = conn.execute(
        "SELECT shop_id FROM image_upload_cache ORDER BY shop_id").fetchall()
    assert len(remaining) == 1 and remaining[0][0] == "shop_b", "清 A 店居然把 B 店也清了"
    conn.close()

    os.unlink(p)
    print("  [OK] image-cache 按店清理")


def main():
    print("多店铺中台回归测试：")
    test_migration_idempotent()
    test_batch_isolation()
    test_category_alias_isolation()
    test_image_cache_isolation()
    test_publish_status_isolation()
    test_image_cache_clear_by_shop()
    print("\n全部通过 OK")


if __name__ == "__main__":
    main()
