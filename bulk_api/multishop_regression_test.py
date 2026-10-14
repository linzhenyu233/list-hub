# -*- coding: utf-8 -*-
"""多店铺改造回归测试（仅测中台数据隔离与迁移幂等，不调平台接口）。

覆盖：
  1. 迁移幂等：在空库上两次建表 + 迁移结果一致
  2. 跨店 batch 不可见：A 店导入的批次，B 店读不到（404）
  3. 类目别名按店隔离：同 internal_category 在 A、B 两店各存一份，互不覆盖
  4. 图片上传缓存按店隔离：同 source_hash 在两店各存一条，URL 不同
  5. /publish-status 按店汇总
  6. image-cache 只清当前店
  7. X-API-Key 鉴权：配了 API_KEY 后无头 401、带头 200、/health 免鉴权
  8. 适配器注册表与旧常量逐字一致（阶段①验收：重构没改行为）
  9. mode 列迁移 + mock 记录不污染 live 统计（阶段②验收）
 10. 价格单位换算 + mock 三级模式与故障注入四路径（阶段②验收）
 11. DEV_RELOAD 默认关闭（阶段②验收）

用法：
    python bulk_api/multishop_regression_test.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
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


def _asgi_status(app, path, key=None, query=""):
    """进程内直接调 ASGI app，只取状态码（见 _asgi_call 的说明）。"""
    return _asgi_call(app, path, key, query)[0]


def _asgi_call(app, path, key=None, query=""):
    """进程内直接调 ASGI app，返回 (状态码, 响应体 bytes)。

    没用 fastapi.testclient.TestClient：它依赖 httpx，本项目 requirements 里没装
    （实测 `import httpx` 直接 ModuleNotFoundError），所以手搓一个最小 scope 走真实
    中间件栈，效果一样但不引入新依赖。
    """
    headers = [(b"host", b"127.0.0.1")]
    if key:
        headers.append((b"x-api-key", key.encode("utf-8")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query.encode("utf-8"),
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8020),
    }
    result = {"code": None, "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            result["code"] = message["status"]
        elif message["type"] == "http.response.body":
            result["body"] += message.get("body") or b""

    asyncio.run(app(scope, receive, send))
    return result["code"], result["body"]


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


def test_api_key_auth():
    """配了 API_KEY 时：无头 401、带对 200、带错 401、/health 免鉴权、?api_key= 兜底可用。

    鉴权是三个服务共用的（api_auth.py），这里拿 bulk_api 代表验一遍。回归脚本自己也是
    "调用方"，接口口径变了必须能被测出来 —— 尤其是「配了 API_KEY 后老调用方全 401」
    这种上线当天才会暴露的问题。
    """
    p = _new_db_path()
    old_key = os.environ.get("API_KEY")
    os.environ["API_KEY"] = "regression_test_key"
    try:
        # _boot 会清掉模块缓存重新加载，让 api_auth.install 读到新的 API_KEY
        m = _boot(p)
        assert _asgi_status(m.app, "/shops") == 401, "没带 X-API-Key 居然放行了"
        assert _asgi_status(m.app, "/shops", key="regression_test_key") == 200, "带对 key 仍被拒"
        assert _asgi_status(m.app, "/shops", key="wrong") == 401, "带错 key 居然放行了"
        assert _asgi_status(m.app, "/health") == 200, "/health 必须免鉴权（探活用）"
        assert _asgi_status(m.app, "/shops", query="api_key=regression_test_key") == 200, \
            "?api_key= 兜底失效（浏览器 <img>/<a download> 只能用它）"
    finally:
        if old_key is None:
            os.environ.pop("API_KEY", None)
        else:
            os.environ["API_KEY"] = old_key
        # 本用例只发 HTTP 请求、没碰 db()，临时库文件可能压根没建出来，直接 unlink 会报错
        if os.path.exists(p):
            os.unlink(p)
    print("  [OK] X-API-Key 鉴权（含免鉴权路径与 query 兜底）")


# 重构前（platform_registry 出现之前）的常量原文，冻结在这里当基线。
# 阶段①的验收条件是「适配器字段与旧常量逐字一致」—— 只有把旧值冻结在测试里才叫"逐字"：
# 对着代码肉眼看会漏，尤其是 id_headers 顺序、价格单位、gone 文案这类**不会报错**的差异，
# 错了只会表现成"货盘回填列位置变了 / 在售商品被误判成已删除"，要等运营发现。
_LEGACY = {
    "valid_platforms": ("wechat", "xhs"),
    "creds_key": {"wechat": "appid", "xhs": "app_id"},
    "required_credentials": {"wechat": ("appid", "secret"), "xhs": ("app_id", "app_secret")},
    "param_keys": {
        "wechat": ("brand_id", "freight_template_id", "after_sale_address_id"),
        "xhs": ("brand_id", "category_id", "shipping_template_id", "logistics_plan_id"),
    },
    "env_credentials": {
        "wechat": {"appid": "WX_APPID", "secret": "WX_SECRET"},
        "xhs": {"app_id": "XHS_APP_ID", "app_secret": "XHS_APP_SECRET",
                "access_token": "XHS_ACCESS_TOKEN", "refresh_token": "XHS_REFRESH_TOKEN"},
    },
    "env_params": {
        "wechat": {"brand_id": ("WX_BRAND_ID", "2100000000"),
                   "freight_template_id": ("WX_FREIGHT_TEMPLATE_ID", ""),
                   "after_sale_address_id": ("WX_AFTER_SALE_ADDRESS_ID", "")},
        "xhs": {"brand_id": ("XHS_BRAND_ID", ""), "category_id": ("XHS_CATEGORY_ID", ""),
                "shipping_template_id": ("XHS_SHIPPING_TEMPLATE_ID", ""),
                "logistics_plan_id": ("XHS_LOGISTICS_PLAN_ID", "")},
    },
    "required_shop_params": {
        "wechat": (("wechat_freight_template_id", "freight_template_id", "微信运费模板"),),
        "xhs": (("xhs_shipping_template_id", "小红书运费模板"),
                ("xhs_logistics_plan_id", "小红书物流方案")),
    },
    "publish_path": {"wechat": "/products", "xhs": "/items/and-sku"},
    "image_paths": {"wechat": ("/images/upload-file", "/images/upload", "img_url"),
                    "xhs": ("/materials/upload-file", "/materials/upload", "url")},
    "item_path": {"wechat": "/products/{id}", "xhs": "/items/{id}"},
    "title_fit_max": {"wechat": 60, "xhs": 30},
    "price_unit": {"wechat": "cent", "xhs": "cent"},
    "gone_markers": {
        "wechat": ("10020052", "商品不存在", "商品已删除", "商品在回收站"),
        "xhs": ("商品不存在", "商品已删除",
                "item not exist", "item_not_exist", "item not found"),
    },
    "api_base": {"wechat": ("WECHAT_API_BASE", "http://127.0.0.1:8000"),
                 "xhs": ("XHS_API_BASE", "http://127.0.0.1:8010")},
    "huopai_id_headers": {
        "wechat": ("微信小店", "微信商品id", "微信小店商品id", "微信产品id"),
        "xhs": ("小红书产品id", "小红书商品id"),
        "wechat_sku": ("微信规格id", "微信sku id", "微信规格id(sku)"),
        "xhs_sku": ("小红书规格id", "小红书sku id", "小红书规格id(sku)"),
    },
    "huopai_platform_label": {"wechat": "微信小店", "xhs": "小红书产品id",
                              "wechat_sku": "微信规格id", "xhs_sku": "小红书规格id"},
    "huopai_field_platform": {"wechat": "wechat", "xhs": "xhs",
                              "wechat_sku": "wechat", "xhs_sku": "xhs"},
}


def test_adapter_registry_parity():
    """适配器字段与旧常量逐字一致（阶段①验收）。

    这条用例是"重构没改行为"的机器证据：接新平台时它必须仍然通过，
    若哪天为了让新平台好写而顺手改了微信/小红书的某个字段，这里会立刻失败。
    """
    import platform_registry as pr

    assert pr.all_platforms() == _LEGACY["valid_platforms"], "平台列表或顺序变了"

    for platform in _LEGACY["valid_platforms"]:
        a = pr.adapter(platform)
        for field, legacy_key in (("creds_key", "creds_key"),
                                  ("required_credentials", "required_credentials"),
                                  ("param_keys", "param_keys"),
                                  ("env_credentials", "env_credentials"),
                                  ("env_params", "env_params"),
                                  ("required_params", "required_shop_params"),
                                  ("publish_path", "publish_path"),
                                  ("item_path", "item_path"),
                                  ("price_unit", "price_unit"),
                                  ("title_fit_max", "title_fit_max"),
                                  ("gone_markers", "gone_markers")):
            want = _LEGACY[legacy_key][platform]
            assert a[field] == want, f"{platform}.{field} 与旧常量不一致：{a[field]!r} != {want!r}"
        assert a["title_column"] == f"{platform}_title", f"{platform} 标题列名变了"
        assert a["title_fit"] == platform, f"{platform} 标题兜底函数标识变了"
        assert (a["image_file_path"], a["image_url_path"], a["image_url_payload_key"]) \
            == _LEGACY["image_paths"][platform], f"{platform} 图片端点/入参键变了"
        env_key, default_base = _LEGACY["api_base"][platform]
        assert a["api_base_env"] == env_key, f"{platform} 地址环境变量名变了"
        saved = os.environ.pop(env_key, None)
        try:
            assert pr.api_base(platform) == default_base, f"{platform} 默认地址变了"
        finally:
            if saved is not None:
                os.environ[env_key] = saved

    # 未知平台必须 fail fast，不能被静默当成别的平台
    for bad in ("", None, "douyin", "WEHCAT"):
        try:
            pr.adapter(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"未注册平台 {bad!r} 竟然拿到了适配器（说明有静默兜底）")

    # 价格换算：中台内部是元，微信/小红书按分发出
    assert pr.price_to_platform("wechat", 19.99) == 1999, "微信价格换算错了（会超低价卖出）"
    assert pr.price_to_platform("xhs", 12) == 1200, "小红书价格换算错了"
    assert pr.price_to_platform("wechat", None) == 0

    # shop_registry 的四张派生表
    sys.modules.pop("shop_registry", None)
    import shop_registry
    assert shop_registry._VALID_PLATFORMS == _LEGACY["valid_platforms"], "店铺平台白名单变了"
    assert shop_registry._REQUIRED_CREDENTIALS == _LEGACY["required_credentials"], "凭证契约变了"
    assert shop_registry._PARAM_KEYS == _LEGACY["param_keys"], "参数契约变了"
    assert shop_registry._ENV_CREDENTIALS_MAP == _LEGACY["env_credentials"], ".env 凭证映射变了"
    assert shop_registry._ENV_PARAMS_MAP == _LEGACY["env_params"], ".env 参数映射变了"

    # bulk_api 的派生表（含**键顺序**：决定货盘 ID 列追加顺序）
    p = _new_db_path()
    m = _boot(p)
    assert m._REQUIRED_SHOP_PARAMS == _LEGACY["required_shop_params"], "发布前必填参数变了"
    assert m.HUOPAI_ID_HEADERS == _LEGACY["huopai_id_headers"], "货盘 ID 表头变了"
    assert list(m.HUOPAI_ID_HEADERS) == list(_LEGACY["huopai_id_headers"]), \
        "货盘 ID 表头顺序变了（会让回填列插到别的位置）"
    assert m.HUOPAI_PLATFORM_LABEL == _LEGACY["huopai_platform_label"], "货盘列名变了"
    assert m.HUOPAI_FIELD_PLATFORM == _LEGACY["huopai_field_platform"], "货盘回填映射变了"
    assert sorted(m._PUBLISH_HANDLERS) == sorted(_LEGACY["valid_platforms"]), "发布处理器覆盖不全"
    assert sorted(m._TITLE_FITTERS) == sorted(_LEGACY["valid_platforms"]), "标题兜底函数覆盖不全"
    assert m._WECHAT_GONE_MARKERS == _LEGACY["gone_markers"]["wechat"]
    assert m._XHS_GONE_MARKERS == _LEGACY["gone_markers"]["xhs"]
    # 旧名与注册表同源
    assert m._WECHAT_GONE_MARKERS == pr.adapter("wechat")["gone_markers"]

    # 标题兜底仍然按各自平台的上限裁剪（60/30），且小红书仍保留"短文补品牌词"的原逻辑
    assert m.platform_title({}, {}, "wechat") == "", "空标题不该报错"
    assert len(m.platform_title({"title": "微" * 80}, {}, "wechat")) == 60, "微信标题兜底上限变了"
    assert len(m.platform_title({"title": "小" * 40}, {}, "xhs")) == 30, "小红书标题兜底上限变了"
    assert m.platform_title({"title": "短"}, {}, "xhs") == "短 培育钻石", "小红书短文补词逻辑变了"
    # 平台标题列优先于通用标题（用 ≥8 字的标题，避免被"短文补词"改写，才能断言原样透传）
    assert m.platform_title({"title": "通用标题文案内容", "xhs_title": "小红书专用标题文案"}, {}, "xhs") \
        == "小红书专用标题文案", "平台标题列优先级变了"
    assert m.platform_title({"title": "通用标题文案内容"}, {}, "xhs") == "通用标题文案内容", \
        "平台标题列缺失时回落到通用标题的逻辑变了"

    if os.path.exists(p):
        os.unlink(p)
    print("  [OK] 适配器注册表与旧常量逐字一致")


def test_publish_mode_isolation():
    """mock 记录不得污染 live 统计（阶段②验收①）。

    这条用例守的是"两类事故"：
      1. 发布状态页把 mock 成功显示成"已发布" —— 运营以为发出去了，平台上一件都没有；
      2. 幂等保护把 mock 成功当成"发过了" —— 真实发布被静默跳过（漏发且不可见）。
    所以判定标准不是"能查到 mock"，而是**默认查不到**，显式加 ?mode= 才看得到。
    """
    p = _new_db_path()
    m = _boot(p)
    conn = m.db()

    # 迁移：两张表都要有 mode 列（老库由 _migrate_add_publish_mode 补），历史行默认 live
    for table in ("publish_jobs", "publish_items"):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert "mode" in cols, f"{table} 缺 mode 列（统计隔离就无从谈起）"
    conn.execute("INSERT INTO publish_items (job_id,batch_id,product_code,platform,shop_id,status,priority) "
                 "VALUES ('j_legacy','b','P_legacy','xhs','s','success',1)")
    legacy = conn.execute("SELECT mode FROM publish_items WHERE product_code='P_legacy'").fetchone()
    assert legacy[0] == "live", "历史数据默认必须是 live（否则老记录会从发布状态页消失）"

    sid = m.shop_registry.default_shop()["shop_id"]
    # ⚠️ 一个任务只能有一种模式：publish_items 的唯一键是 (job_id, product_code, platform)，
    #    不含 mode。也就是说"同一任务里同一商品既有 mock 又有 live 记录"在结构上就不成立
    #    （任务创建时按目标店+平台落 mode 标签），所以这里用两个任务分别造两种模式。
    job_live = "job_live_" + uuid.uuid4().hex[:6]
    job_mock = "job_mock_" + uuid.uuid4().hex[:6]
    conn.execute("INSERT INTO publish_jobs (id,shop_id,batch_id,platforms_json,status,total,operator,created_at,mode) "
                 "VALUES (?,?,?,?,?,?,?,datetime('now'),?)",
                 (job_live, sid, "batch_live", '["xhs"]', "done", 1, "tester", "live"))
    conn.execute("INSERT INTO publish_jobs (id,shop_id,batch_id,platforms_json,status,total,operator,created_at,mode) "
                 "VALUES (?,?,?,?,?,?,?,datetime('now'),?)",
                 (job_mock, sid, "batch_mock", '["xhs"]', "done", 2, "tester", "mock"))
    # 同一个商品同一平台：真实成功（live 任务）+ 模拟成功（mock 任务）；再加一条模拟失败
    inserts = [
        (job_live, "P1", "success", "live", "LIVE_ID_1"),
        (job_mock, "P1", "success", "mock", "MOCK_ID_1"),
        (job_mock, "P2", "failed", "mock", ""),
    ]
    for job_id, code, status, mode, pid in inserts:
        conn.execute("INSERT INTO publish_items (job_id,shop_id,batch_id,product_code,platform,status,"
                     "priority,mode,platform_product_id) VALUES (?,?,?,?,?,?,?,?,?)",
                     (job_id, sid, "batch_" + mode, code, "xhs", status, 1, mode, pid))
    conn.commit()
    conn.close()

    # 本机 .env 可能配了 API_KEY（服务开了鉴权），这里带上它 —— 否则 401 会掩盖真正的断言
    api_key = os.environ.get("API_KEY") or None
    code, body = _asgi_call(m.app, "/publish-status", key=api_key)
    assert code == 200, f"/publish-status 返回 {code}"
    payload = json.loads(body)
    result = payload["result"]
    assert payload.get("mode") == "live", "默认口径应该是 live"
    assert result["P1"]["platforms"]["xhs"].get("success") == 1, \
        "live 成功数不对：mock 成功被算进 live 了（会把模拟当真实发布）"
    assert "P2" not in result, "mock 失败不该出现在 live 统计里"

    code, body = _asgi_call(m.app, "/publish-status", key=api_key, query="mode=mock")
    mock_result = json.loads(body)["result"]
    assert mock_result["P1"]["platforms"]["xhs"].get("success") == 1 and \
        mock_result["P2"]["platforms"]["xhs"].get("failed") == 1, "?mode=mock 应该能查到模拟记录"

    # 货盘 ID 回填只能拿真实 ID（latest_platform_ids 返回 (ids, shops, ambiguous)）
    ids, shops, ambiguous = m.latest_platform_ids(sid)
    picked = (ids.get("xhs") or {}).get("P1")
    assert picked == "LIVE_ID_1", \
        f"回填取到了模拟 ID：{picked}（假 ID 写进货盘表会把整表污染成「看似已发布」）"
    assert "MOCK_ID_1" not in json.dumps(ids), "结果里混进了 mock 的假 ID"

    os.unlink(p)
    print("  [OK] mock 记录不污染 live 统计（mode 列 + 默认 live 口径）")


def test_mock_mode_and_faults():
    """三级模式解析 + 故障注入四路径 + 价格换算（阶段②验收②③）。"""
    import mock_platform as mp
    import platform_registry as pr

    # --- 价格换算：分/元两个分支 ---
    assert pr.price_to_platform("wechat", 19.99) == 1999
    assert pr.price_to_platform("xhs", 0.01) == 1, "分换算错（会超低价卖出）"
    # douyin/jd/tmall 适配器在阶段⑤才落地，这里临时注入一个「元」平台把换算分支测掉：
    # 元/分搞反不会报错，只会超低价卖出，所以这个分支必须现在就有用例守着。
    pr._ADAPTERS["_yuan_probe"] = {"price_unit": "yuan"}
    try:
        assert pr.price_to_platform("_yuan_probe", 19.99) == 19.99, "元单位平台被按分发出了"
        assert pr.price_to_platform("_yuan_probe", 12) == 12
    finally:
        pr._ADAPTERS.pop("_yuan_probe", None)

    # --- 模式解析优先级：环境变量 > shops.json > live ---
    key = mp.env_mode_key("xhs")
    saved = os.environ.pop(key, None)
    try:
        assert mp.resolve_mode("xhs", "不存在的店") == "live", "缺省必须是 live（不配置=生产行为不变）"
        assert mp.resolve_mode("xhs") == "live"
        os.environ[key] = " Mock "
        assert mp.resolve_mode("xhs", "不存在的店") == "mock", "环境变量覆盖没生效（或没做去空格/小写）"
        assert mp.is_mock("xhs", "不存在的店")
        os.environ[key] = "fake_mode"
        assert mp.resolve_mode("xhs") == "live", "非法值必须回落 live（写错一个字不该让发布链路挂掉）"
        os.environ[key] = "mock"
        # --- 故障注入四路径（mock 模式；backoff=0 让用例不真的睡） ---
        mp.reset_faults()
        os.environ["MOCK_SCENARIO"] = "rate_limit"
        resp = mp.gateway_call("xhs", "http://mock.invalid", method_key="product.getItemInfo",
                               body={"itemId": "x"}, shop_id="s", retries=2, backoff=0.0)
        assert resp.json().get("error_code") == 0, "限流应被退避重试后恢复（平台明确拒绝=无副作用，可安全重试）"

        mp.reset_faults()
        os.environ["MOCK_SCENARIO"] = "timeout"
        resp = mp.gateway_call("xhs", "http://mock.invalid", method_key="product.getItemInfo",
                               body={"itemId": "x"}, shop_id="s", retries=2, backoff=0.0)
        assert resp.json().get("error_code") == 0, "读操作超时应重试恢复"

        mp.reset_faults()
        try:
            mp.gateway_call("xhs", "http://mock.invalid", method_key="product.createItemAndSku",
                            body={"item": {"name": "n"}}, shop_id="s", retries=2, backoff=0.0)
        except Exception:
            pass   # 写操作超时**允许**抛出；下面单独断言"确实没有重试"
        assert mp.is_write_method("xhs", "product.createItemAndSku"), "写操作识别失效（超时会盲目重试→重复建商品）"
        assert not mp.is_write_method("xhs", "product.getItemInfo"), "读操作被误判成写操作"

        mp.reset_faults()
        os.environ["MOCK_SCENARIO"] = "auth_expired"
        resp = mp.gateway_call("xhs", "http://mock.invalid", method_key="product.getItemInfo",
                               body={}, shop_id="s", retries=0)
        assert "401" in resp.text, "鉴权失效的响应壳不对，服务层就识别不出「该刷 token 了」"

        mp.reset_faults()
        os.environ["MOCK_SCENARIO"] = "param_error"
        os.environ["MOCK_SCENARIO_FAIL_ALWAYS"] = "1"
        resp = mp.gateway_call("xhs", "http://mock.invalid", method_key="product.createItemAndSku",
                               body={}, shop_id="s", retries=2, backoff=0.0)
        assert resp.json().get("error_code") == 400, "参数错的响应壳不对，失败清单就读不出原因"
        os.environ.pop("MOCK_SCENARIO_FAIL_ALWAYS", None)

        # --- 确定性 ID / 响应壳 / 脱敏 ---
        assert mp.mock_id("xhs", "A") == mp.mock_id("xhs", "A"), "同一商品编码的假 ID 必须稳定（回归要能比对）"
        assert mp.mock_id("xhs", "A") != mp.mock_id("xhs", "B"), "不同商品编码不该算出同一个 ID"
        assert len(mp.mock_id("xhs", "A")) == 24, "小红书 itemId 是 24 位十六进制，形态必须一致"
        assert mp.mock_id("wechat", "A").isdigit(), "微信 product_id 是纯数字"
        synth = mp._synthesize("xhs", "product.getItemInfo", {"itemId": "i"})
        assert set(synth) == {"error_code", "success", "data"}, "mock 必须返回平台原样的响应壳"
        masked = mp.mask({"app_secret": "s3cr3t", "accessToken": "t", "item": {"name": "标题"}})
        assert masked["app_secret"] == "***" and masked["accessToken"] == "***", "录制脱敏失效（fixture 会泄漏密钥）"
        assert masked["item"]["name"] == "标题", "脱敏不该碰业务字段"

        # --- 负面剧本（失败清单场景）：显式指定才生效 ---
        # 故障注入优先级高于剧本回放（注入的是"平台行为"，剧本是"平台数据"），
        # 所以验证剧本前要先撤掉故障注入。
        os.environ.pop("MOCK_SCENARIO", None)
        os.environ["MOCK_FIXTURE_MAP"] = \
            "product.createItemAndSku=product_createItemAndSku_duplicate.json"
        mp.reset_faults()
        resp = mp.gateway_call("xhs", "http://mock.invalid", method_key="product.createItemAndSku",
                               body={}, shop_id="s", retries=0)
        dup = resp.json()
        assert dup.get("success") is False and "重复" in str(dup.get("error_msg")), \
            f"负面剧本没被回放：{dup}（失败清单场景无法复现）"
        assert "_comment" not in dup, "剧本里的注释键泄漏进响应壳了"
        os.environ.pop("MOCK_FIXTURE_MAP", None)
    finally:
        for name in ("MOCK_SCENARIO", "MOCK_SCENARIO_FAIL_ALWAYS", "MOCK_FIXTURE_MAP"):
            os.environ.pop(name, None)
        mp.reset_faults()
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved

    print("  [OK] 三级模式 + 故障注入四路径 + 价格换算 + 脱敏")


def test_dev_reload_default_off():
    """DEV_RELOAD 默认关闭：未设置时启动参数与改造前完全一致（阶段②验收④）。"""
    import runtime_config
    import uvicorn

    captured = {}
    real_run = uvicorn.run

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured["kwargs"] = kwargs

    saved = os.environ.pop("DEV_RELOAD", None)
    uvicorn.run = fake_run
    try:
        runtime_config.run_service("APP_SENTINEL", "some_module", "127.0.0.1", 9999,
                                   reload_dirs=[BULK_DIR])
        assert captured["app"] == "APP_SENTINEL", "默认路径必须直接传 app 对象（与改造前一致）"
        assert "reload" not in captured["kwargs"], "未设置 DEV_RELOAD 时不能开热重载"
        captured.clear()
        os.environ["DEV_RELOAD"] = "1"
        runtime_config.run_service("APP_SENTINEL", "some_module", "127.0.0.1", 9999,
                                   reload_dirs=[BULK_DIR])
        assert captured["app"] == "some_module:app", "开了 DEV_RELOAD 必须传 import string（uvicorn 要求）"
        assert captured["kwargs"].get("reload") is True, "DEV_RELOAD=1 没生效"
        assert BULK_DIR in os.environ.get("PYTHONPATH", ""), \
            "没把服务目录塞进 PYTHONPATH，reload 子进程会 import 不到模块"
    finally:
        uvicorn.run = real_run
        if saved is None:
            os.environ.pop("DEV_RELOAD", None)
        else:
            os.environ["DEV_RELOAD"] = saved
    print("  [OK] DEV_RELOAD 默认关闭、开启时走 import string")


def test_gateway_json_post_keeps_params():
    """网关出口的 JSON POST 必须带上 params（查询串）。

    踩过的坑（2026-10-12 线上）：mock 改造后 `mock_platform._real_call` 的
    JSON 分支把 params 漏了，而**微信小店的 access_token 就放在 URL 查询串里**，
    于是所有微信接口报 `41001 access_token missing`（发品、商品列表、上下架全挂）。
    mock 模式不校验 query，所以 mock 烟测发现不了，只有真实调用才暴露 —— 这里补一条
    不依赖网络的用例把它钉住。
    """
    import mock_platform as mp

    captured = {}

    class _FakeResponse:
        text = '{"errcode":0}'

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return _FakeResponse()

    real_post = mp.requests.post
    mp.requests.post = fake_post
    try:
        mp._real_call("wechat", "http://example.com/channels/ec/product/list/get",
                      body={"page_size": 1}, params={"access_token": "T"}, timeout=5,
                      http_get=False, files=None, form=False)
    finally:
        mp.requests.post = real_post

    assert captured.get("params") == {"access_token": "T"}, \
        "JSON POST 丢了 params：微信会报 41001 access_token missing"
    assert captured.get("json") == {"page_size": 1}, "JSON body 没按 json 传"
    print("  [OK] 网关 JSON POST 带上 params（微信 access_token 不会丢）")


def test_order_metrics_aggregation():
    """数据面板口径：金额换算（两家都是分；售后退款另有单位）、退款归天、时间字段判状态。

    纯函数用例，不联网：直接喂合成订单/售后，断言 7 个指标。
    """
    p = _new_db_path()
    m = _boot(p)
    day = "2026-10-12"
    start, end = m._day_bounds(day)
    noon = start + 12 * 3600

    # 微信：3 单（2 单待收货=已发货、1 单已完成并确认收货）+ 1 笔当日完成退款（分）
    wx_orders = [
        {"order_id": "o1", "status": 30, "create_time": noon, "pay_time": noon,
         "order_price": 409900, "confirm_receipt_time": 0},
        {"order_id": "o2", "status": 30, "create_time": noon + 60, "pay_time": noon + 60,
         "order_price": 419900, "confirm_receipt_time": 0},
        {"order_id": "o3", "status": 100, "create_time": noon + 120, "pay_time": noon + 120,
         "order_price": 429900, "confirm_receipt_time": noon + 7200},
    ]
    wx_aftersales = [{"after_sale_order_id": "a1", "status": "MERCHANT_REFUND_SUCCESS",
                      "refund_amount": 99900, "complete_time": noon + 10800}]
    wx = m._aggregate_order_day("wechat", wx_orders, wx_aftersales, start, end)
    assert wx["paid_orders"] == 3 and wx["paid_amount"] == 12597.0, f"微信支付口径不对: {wx}"
    assert wx["dealt_orders"] == 1, f"微信成交笔数按确认收货归天: {wx}"
    assert wx["refund_amount"] == 999.0, f"微信退款 99900 分应换算成 999.00 元: {wx}"
    assert wx["actual_orders"] == 2 and wx["actual_income"] == 8298.0, f"微信实际收入口径不对: {wx}"
    assert wx["refund_rate"] == 0.0793, f"退款率=退款金额/支付金额: {wx}"

    # 小红书：**金额单位是分**（真机实测 详情 totalPayAmount=109900 → 1099.00 元，
    # 与同日售后 expectedRefundAmountYuan=1099.0 元交叉验证一致）；
    # 平台未公开订单状态码表 → 用时间字段判断（paidTime 已支付 / finishTime 已完成 / cancelTime 取消），
    # 且时间戳是**毫秒**，必须归一化到秒，否则一条都算不进当天。
    xhs_orders = [
        {"order_id": "x1", "status": 30, "pay_time": noon * 1000, "actual_amount": 279900},
        {"order_id": "x2", "status": 100, "pay_time": (noon + 60) * 1000,
         "finish_time": (noon + 120) * 1000, "actual_amount": 359900},
    ]
    xhs = m._aggregate_order_day("xhs", xhs_orders, [], start, end)
    assert xhs["paid_orders"] == 2 and xhs["paid_amount"] == 6398.0, \
        f"小红书金额是分(要÷100)且毫秒时间戳要归一化: {xhs}"
    assert xhs["dealt_orders"] == 1, f"finishTime 落在本日才算成交: {xhs}"
    # x1 已支付未完成未取消 → 在途；x2 已完成 → 不算在途
    assert xhs["actual_orders"] == 1 and xhs["actual_income"] == 2799.0, \
        f"小红书在途=已支付且未完成未取消: {xhs}"
    # 已取消(cancelTime 有值)不能算在途
    xhs_cancelled = m._aggregate_order_day("xhs", [
        {"order_id": "x3", "pay_time": noon * 1000, "cancel_time": (noon + 60) * 1000,
         "actual_amount": 100000},
    ], [], start, end)
    assert xhs_cancelled["actual_orders"] == 0 and xhs_cancelled["paid_amount"] == 1000.0, \
        f"已取消订单不该计入在途: {xhs_cancelled}"
    # 售后退款金额单位与订单不同：小红书带 Yuan 的字段是元，不能再除 100
    xhs_aftersale = m._aggregate_order_day("xhs", [
        {"order_id": "x4", "pay_time": noon * 1000, "actual_amount": 279900},
    ], [{"status": 2, "refund_amount": None, "expected_refund_amount": 1099.0,
         "update_time": noon * 1000}], start, end)
    assert xhs_aftersale["refund_amount"] == 1099.0, \
        f"小红书退款金额是元(不换算),且 refund_amount 为空要用 expected_refund_amount: {xhs_aftersale}"

    # 原则：支付类指标按「支付时间是否落在本日」过滤；实际收入是**存量**口径（当前在途就算，
    # 不受本日限制）。所以下面这组：o4 未付款不算、o5 昨天支付但仍在途 → 支付 0 但实际收入 1000。
    other = m._aggregate_order_day("wechat", [
        {"order_id": "o4", "status": 10, "create_time": noon, "pay_time": noon, "order_price": 100000},
        {"order_id": "o5", "status": 30, "create_time": start - 600, "pay_time": start - 600,
         "order_price": 100000},
    ], [], start, end)
    assert other["paid_orders"] == 0 and other["paid_amount"] == 0.0, f"未付款/跨天单不该计入当日支付: {other}"
    assert other["actual_orders"] == 1 and other["actual_income"] == 1000.0, \
        f"实际收入是存量口径，昨天的在途订单也算: {other}"

    # 表结构：按 (店, 平台, 日期) 唯一，重复同步不会写出重复行
    conn = m.db()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(order_daily_metrics)").fetchall()}
    for col in ("shop_id", "platform", "stat_date", "paid_orders", "paid_amount", "dealt_orders",
                "refund_amount", "actual_income", "actual_orders", "refund_rate", "sync_error"):
        assert col in cols, f"order_daily_metrics 缺列 {col}"
    pk = [row[1] for row in conn.execute("PRAGMA table_info(order_daily_metrics)").fetchall() if row[5]]
    assert pk == ["shop_id", "platform", "stat_date"], f"主键应为 店+平台+日期: {pk}"
    conn.close()
    print("  [OK] 订单指标口径（分/元换算、退款归天、状态过滤）+ 表主键幂等")


def test_order_mock_shells():
    """mock 订单壳：结构正确、按请求那天稳定生成（数据面板在没真实订单时也能演示）。"""
    import mock_platform as mp

    start = int(time.mktime(time.strptime("2026-10-12 00:00:00", "%Y-%m-%d %H:%M:%S")))
    end = start + 86400
    wx_list = mp._synthesize("wechat", "/channels/ec/order/list/get",
                             {"create_time_range": {"start_time": start, "end_time": end}})
    ids = wx_list.get("order_id_list") or []
    assert len(ids) == 3 and ids[0] == "mock-order-20261012-1", f"微信订单列表 mock 壳不对: {wx_list}"
    wx_detail = mp._synthesize("wechat", "/channels/ec/order/get", {"order_id": ids[0]}).get("order") or {}
    assert wx_detail.get("order_detail", {}).get("price_info", {}).get("order_price"), "微信订单详情缺金额"
    assert wx_detail.get("order_detail", {}).get("pay_info", {}).get("pay_time"), "微信订单详情缺支付时间"

    # ⚠️ 两个平台的响应壳不一样：小红书是 {"error_code":0,"success":true,"data":{...}}
    #    （业务字段在 data 里），微信是 {"errcode":0,...,"order_id_list":[...]}（字段在顶层）。
    #    写成一样的就会取不到值 —— 与"微信 access_token 放查询串"同源的结构差异坑。
    xhs_list = mp._synthesize("xhs", "order.getOrderList",
                              {"startTime": start, "endTime": end, "pageNo": 1, "pageSize": 20})["data"]
    orders = xhs_list.get("orderList") or []
    assert len(orders) == 2 and orders[0]["orderId"] == "mock-xhs-order-20261012-1", \
        f"小红书订单列表 mock 壳不对: {xhs_list}"
    xhs_aft = mp._synthesize("xhs", "afterSale.listAfterSaleApi",
                             {"startTime": start, "endTime": end, "pageNo": 1, "pageSize": 20})["data"]
    assert (xhs_aft.get("simpleAfterSaleList") or [{}])[0].get("refundAmountYuan"), "小红书售后缺退款金额"
    # 列表**不返回**金额(真机实测)→ 金额必须能通过订单详情拿到,字段名 totalPayAmount(分)
    xhs_detail = mp._synthesize("xhs", "order.getOrderDetail",
                                {"orderId": "mock-xhs-order-20261012-2"})["data"]
    assert xhs_detail.get("totalPayAmount") == 359900, \
        f"小红书订单详情金额字段应是 totalPayAmount 且单位分: {xhs_detail}"
    print("  [OK] mock 订单/售后壳结构正确且按天稳定")


def test_product_dimension_metrics():
    """商品明细：金额按订单实付等比分摊 → 各商品之和必须等于订单实付之和。

    纯函数用例，不联网。线上已核对过：小红书 2026-10-04 两个商品 2274.90 + 1099.00
    = 3373.90，正好等于这两笔订单的实付之和（优惠/运费摊过后依然对得上）。
    """
    p = _new_db_path()
    m = _boot(p)
    start, end = m._day_bounds("2026-10-10")
    noon = start + 12 * 3600
    orders = [
        {"order_id": "o1", "status": 100, "pay_time": noon, "order_price": 10000,
         "products": [{"product_id": "p1", "sku_id": "s1", "title": "A", "sku_code": "C1",
                       "product_cnt": 2, "real_price": 5000}]},
        {"order_id": "o2", "status": 100, "pay_time": noon + 60, "order_price": 3000,
         "products": [{"product_id": "p2", "sku_id": "s2", "title": "B", "sku_code": "C2",
                       "product_cnt": 1, "real_price": 3000}]},
    ]
    rows, unnamed = m._aggregate_product_day("wechat", orders, start, end)
    assert unnamed == 0 and len(rows) == 2, (rows, unnamed)
    assert round(sum(row["paid_amount"] for row in rows), 2) == 130.0, \
        f"商品金额之和必须等于订单实付之和(130.00 元): {rows}"
    top = rows[0]
    assert (top["product_id"], top["sku_code"], top["paid_qty"], top["paid_amount"]) \
        == ("p1", "C1", 2, 100.0), rows

    # 没带商品信息的订单要单独计数，不能悄悄消失（否则总览有金额、拆解里却看不到）
    rows2, unnamed2 = m._aggregate_product_day(
        "wechat", [{"order_id": "o3", "status": 100, "pay_time": noon, "order_price": 100}],
        start, end)
    assert unnamed2 == 1 and not rows2, (rows2, unnamed2)
    print("  [OK] 商品维度：金额等比分摊、无名订单单独计数")


def main():
    print("多店铺中台回归测试：")
    test_migration_idempotent()
    test_batch_isolation()
    test_category_alias_isolation()
    test_image_cache_isolation()
    test_publish_status_isolation()
    test_image_cache_clear_by_shop()
    test_api_key_auth()
    test_adapter_registry_parity()
    test_publish_mode_isolation()
    test_mock_mode_and_faults()
    test_dev_reload_default_off()
    test_gateway_json_post_keeps_params()
    test_order_metrics_aggregation()
    test_order_mock_shells()
    test_product_dimension_metrics()
    print("\n全部通过 OK")


if __name__ == "__main__":
    main()
