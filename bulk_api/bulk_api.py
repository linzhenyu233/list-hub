# -*- coding: utf-8 -*-
"""批量商品中台首期服务：Excel 导入、校验、批次查询与模板下载。

本服务刻意独立于微信/小红书服务。当前环境没有 MySQL 驱动和 multipart，
因此上传接口采用 JSON + Base64，存储使用 SQLite；后续可替换存储层而不改前端接口。
"""
import base64
import csv
import io
import hashlib
import json
import os
import re
import shutil
import sqlite3
import urllib.parse
import urllib.request
import urllib.error
import uuid
import zipfile
import time
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import runtime_config
from runtime_config import load_project_env
from app_version import APP_VERSION
import platform_registry
import shop_registry
import api_auth
import mock_platform    # 三级模式标签(mock/sandbox/live)：只用于统计隔离，不参与发布行为

load_project_env()


# ---- 店铺上下文（请求级） ------------------------------------------------
# 路由层通过 _current_shop(request) 拿到当前店铺；未传 X-Shop-Id 则回退默认店。
# 操作人通过 X-Operator 头获取，空则取默认。

SHOP_HEADER = "x-shop-id"
OPERATOR_HEADER = "x-operator"


def _current_shop_id(request: Request) -> str:
    """从请求头取 shop_id，空则回退默认店。未知/停用店直接抛 400。"""
    sid = request.headers.get(SHOP_HEADER) or ""
    sid = sid.strip()
    if not sid:
        return shop_registry.default_shop()["shop_id"]
    shop = _verify_shop(sid)
    return shop["shop_id"]


def _current_operator(request: Request) -> str:
    """从请求头取操作人，空则取默认。

    前端会做 encodeURIComponent：HTTP 头只允许 ASCII，中文姓名直接放会报
    "Cannot convert argument to a ByteString"。这里 decode 回来；
    若调用方（脚本/curl）没编码，unquote 对纯 ASCII 是空操作，向后兼容。
    """
    op = request.headers.get(OPERATOR_HEADER) or ""
    return urllib.parse.unquote(op.strip()) or shop_registry.default_operator()


def _verify_shop(shop_id: str) -> dict:
    """校验店铺存在且启用，返回店铺配置。不存在/停用抛 HTTPException(400)。"""
    try:
        return shop_registry.resolve(shop_id)
    except ValueError as e:
        raise HTTPException(400, str(e))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get("BULK_DB_FILE", os.path.join(BASE_DIR, "bulk_catalog.sqlite3"))
IMAGE_DIR = os.environ.get("BULK_IMAGE_DIR", os.path.join(BASE_DIR, "../images"))
# 默认只监听本机：原先 0.0.0.0 + 无鉴权，局域网内任意机器都能直接调发布/删商品接口。
# 确需局域网直连时设 BULK_API_HOST=0.0.0.0，并务必配好 API_KEY。
BULK_API_HOST = os.environ.get("BULK_API_HOST", "127.0.0.1")
BULK_API_PORT = int(os.environ.get("BULK_API_PORT", "8020"))
# 共享盘图片"懒拷贝"的保留天数(0=不清理): 发品时把共享盘图片按内容哈希拷进 IMAGE_DIR/_share/,
# 内容随时能从共享盘重取, 所以可以安全按时间清; 不清的话这个目录只会越来越大(实测 10 天 147MB)。
IMAGE_TTL_DAYS = int(os.environ.get("BULK_IMAGE_TTL_DAYS", "30"))
# 批次数据保留天数: 批次的 rows_json + mappings_json 是库里最大的两块(实测占 160MB+),
# 而运营每次发品都是重新导入货盘表, "已发布/未发布"是按商品编码跨批次统计的(不依赖批次本身),
# 所以只留最近这些天, 更早的批次连行数据一起清。
# ⚠️ 发布记录 publish_items / publish_jobs 永久保留 —— 删了会让已发过的商品重新显示"未发布",
#    诱使运营重复发品(平台以"标题重复"拒)。
BATCH_TTL_DAYS = int(os.environ.get("BULK_BATCH_TTL_DAYS", "14"))
os.makedirs(IMAGE_DIR, exist_ok=True)


def prune_old_images(days=IMAGE_TTL_DAYS):
    """清理 N 天前的共享盘图片懒拷贝, 返回 (删除数, 释放字节数)。

    只动 IMAGE_DIR/_share/(内容哈希命名、可重取); 浏览器上传和 Excel 内嵌图落在
    IMAGE_DIR/<批次ID>/ 下 —— 那些源图删了就再也发不出去, 所以这里故意不碰。
    """
    share_dir = os.path.join(IMAGE_DIR, "_share")
    if days <= 0 or not os.path.isdir(share_dir):
        return 0, 0
    cutoff = time.time() - days * 86400
    removed = freed = 0
    for name in os.listdir(share_dir):
        target = os.path.join(share_dir, name)
        try:
            if not os.path.isfile(target) or os.path.getmtime(target) >= cutoff:
                continue
            size = os.path.getsize(target)
            os.remove(target)
            removed += 1
            freed += size
        except OSError:
            pass
    return removed, freed

# 图片根目录默认值(网络共享, 服务端可直读):图片直读扫描的默认根目录
DEFAULT_IMAGE_ROOT = r"\\192.168.10.250\电子商务部\网销部共享\SHINING HOUSE培育钻"
# 允许按引用读取图片的根目录白名单:
# 防止有人构造 disk:/local: 引用(或直接传 root 参数)去读服务器上的任意文件
ALLOWED_IMAGE_ROOTS = [DEFAULT_IMAGE_ROOT, IMAGE_DIR]


def _within_allowed_roots(path, roots=None):
    """判断绝对路径是否落在白名单根目录内。默认用全局 ALLOWED_IMAGE_ROOTS。

    用 commonpath 而不是 startswith:后者会让 '...\\images_evil\\x.jpg' 命中
    白名单根 '...\\images' 的前缀,导致越权读取。
    """
    if roots is None:
        roots = ALLOWED_IMAGE_ROOTS
    target = os.path.abspath(str(path or ""))
    for root in roots:
        root_abs = os.path.abspath(root)
        try:
            if os.path.commonpath((root_abs, target)) == root_abs:
                return True
        except ValueError:
            # 不同盘符 / 本地盘与 UNC 混用时 commonpath 会报错,视为不在白名单内
            continue
    return False


def _allowed_roots_for(shop_id=None):
    """当前店铺允许读图的根目录白名单：店铺 image_root + 全局兜底。

    统一入口 —— 原先 4 个校验点各写一遍，其中发布读图那处漏了店铺 root，
    导致「扫描时认可、发布时拒绝」：服务器 image_root 是 COS 挂载点 /mnt/cos/...，
    而 DEFAULT_IMAGE_ROOT 是为 Windows 共享盘写的 UNC 路径，两者永不相等。
    """
    shop_root = ""
    if shop_id:
        try:
            shop_root = shop_registry.image_root_for(shop_id)
        except ValueError:
            shop_root = ""  # 店铺已停用/不存在：退回全局兜底，由调用方给出明确报错
    return list(dict.fromkeys(([shop_root] if shop_root else []) + list(ALLOWED_IMAGE_ROOTS)))


app = FastAPI(title="商品批量发布中台", version=APP_VERSION)

# 鉴权：X-API-Key（未配置 API_KEY 时只告警不拦截，见 api_auth.py）。
# bulk_api 没有 CORS 中间件（前端经 vite 代理同源访问），顺序无额外要求。
api_auth.install(app, "bulk-api")

_WECHAT_CATEGORY_CHAIN_CACHE = {}
_XHS_VAR_CANDIDATES_CACHE = {}

# 图片上传缓存:内存 LRU 层(SQLite 之上),命中直接返回,省一次磁盘查询。
# 上限 2000 条(按单商品约 10 张图算,覆盖 200 件商品的并发上传窗口)。
_IMAGE_CACHE_MEM = {}
_IMAGE_CACHE_ORDER = []  # 插入顺序,用于 LRU 淘汰
_IMAGE_CACHE_MAX = 2000
_image_cache_hit_mem = 0  # 内存命中计数
_image_cache_hit_db = 0   # DB 命中计数
_image_cache_miss = 0     # 未命中计数

# 小红书 SKU 规格值别名:货盘英文 SKU 编码 → 平台中文 valueName(发品时映射用)
# 小红书「颜色分类」等规格的 valueName 是中文,货盘常用英文编码,直接发英文会显示英文
XHS_SPEC_VALUE_ALIASES = {
    '粉红色': ['SHINING PINK', '粉色', '粉红', 'PINK', 'Rose'],
    '浅蓝色': ['ICE BLUE', '浅蓝', 'LIGHT BLUE'],
    '香槟金色': ['CHAMPAGNE GOLD', '香槟金', 'CHAMPAGNE'],
    '玫瑰金色': ['ROSE GOLD', '玫瑰金'],
    '黄金色': ['YELLOW GOLD', '黄金', 'GOLD'],
    '18K金色': ['18K GOLD', '18K金'],
    '白色': ['WHITE'],
    '黑色': ['BLACK'],
    '无色': ['COLORLESS', '透明'],
    '玫红色': ['ROSE RED', '玫红'],
    '红色': ['RED'],
    '绿色': ['GREEN'],
    '黄色': ['YELLOW'],
    '蓝色': ['BLUE'],
    '紫色': ['PURPLE', 'VIOLET'],
    '灰色': ['GRAY', 'GREY'],
    '银色': ['SILVER'],
    # 其它颜色/规格值按需补充
}


_db_schema_ready = False
_db_schema_lock = threading.Lock()


def _ensure_db_schema():
    """建表 / 补列 / 重建表 / 迁移归属 / 建索引，一个进程只做一次。

    原先这一整套挂在 db() 里，每开一个连接就重跑一遍 DDL：API 进程、worker 进程、
    以及每个请求的短连接都在重复执行，多进程同时启动时会互相抢 sqlite_master 的写锁，
    表现就是启动瞬间 database is locked / 建表超时。改成本进程内一次性执行。
    """
    global _db_schema_ready
    if _db_schema_ready:
        return
    with _db_schema_lock:
        if _db_schema_ready:      # 双检：等锁期间别的线程可能已经建好了
            return
        conn = sqlite3.connect(DB_FILE, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")

            # 顺序很重要：先建表（新库直接是新结构）→ 补列 → 重建带单店约束的老表
            # → 迁移历史数据归属 → 最后建索引。
            # 如果索引先建，老库还没有 shop_id 列，CREATE INDEX 会直接报 no such column。
            _create_tables(conn)
            _migrate_add_shop_id(conn)
            _migrate_add_job_params(conn)
            _migrate_add_publish_mode(conn)
            _migrate_add_order_stats_columns(conn)
            _migrate_add_order_buyer_columns(conn)
            _migrate_add_product_image_column(conn)
            _migrate_add_product_stats_columns(conn)
            _migrate_add_category_platform_columns(conn)
            _rebuild_multishop_tables(conn)
            _reassign_legacy_shop(conn)
            _create_indexes(conn)

            conn.commit()
        finally:
            conn.close()
        _db_schema_ready = True


def db():
    _ensure_db_schema()
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL 模式:读写互不阻塞,大幅降低 database is locked
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _create_tables(conn):
    """建表（仅 CREATE TABLE IF NOT EXISTS，不含索引）。"""
    conn.execute("CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("""CREATE TABLE IF NOT EXISTS batches (
        id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, filename TEXT, status TEXT,
        total INTEGER DEFAULT 0, valid INTEGER DEFAULT 0, errors INTEGER DEFAULT 0,
        created_at TEXT, operator TEXT,
        -- 每次 rows_json / mappings_json 被改写就 +1: 供跨进程批次缓存判新旧
        revision INTEGER NOT NULL DEFAULT 0,
        rows_json TEXT NOT NULL, mappings_json TEXT NOT NULL DEFAULT '{}'
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS category_aliases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id TEXT NOT NULL,
        internal_category TEXT NOT NULL,
        wechat_json TEXT, xhs_json TEXT, youzan_json TEXT, updated_at TEXT,
        UNIQUE(shop_id, internal_category)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS publish_jobs (
        id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, batch_id TEXT NOT NULL,
        platforms_json TEXT NOT NULL, status TEXT NOT NULL, total INTEGER DEFAULT 0,
        processed INTEGER DEFAULT 0, success INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
        operator TEXT, created_at TEXT, updated_at TEXT,
        -- 跨店发布：目标店铺的私有参数快照(运费模板/物流方案/品牌)。
        -- shop_id 即目标店；本字段让 worker 执行时覆盖批次里的来源店参数。空对象=同店发布。
        params_json TEXT NOT NULL DEFAULT '{}',
        -- 三级模式标签：mock/sandbox/live（多平台混合任务 = mixed）。
        -- ⚠️ 这是**标签**，行为不在这里 —— 是否真的 mock 由平台服务自己按 shops.json 判定
        --    （见 mock_platform.resolve_mode）。统计默认只算 live：mock 成功绝不能
        --    被当成"这个商品已经发过了"（否则会漏发真实商品）。
        mode TEXT NOT NULL DEFAULT 'live'
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS publish_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, batch_id TEXT NOT NULL,
        shop_id TEXT NOT NULL, product_code TEXT NOT NULL, platform TEXT NOT NULL,
        status TEXT NOT NULL, platform_product_id TEXT, platform_sku_ids TEXT, error TEXT,
        started_at TEXT, finished_at TEXT, priority INTEGER NOT NULL DEFAULT 0,
        mode TEXT NOT NULL DEFAULT 'live',
        UNIQUE(job_id, product_code, platform)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS image_upload_cache (
        shop_id TEXT NOT NULL, platform TEXT NOT NULL, source_hash TEXT NOT NULL,
        source_url TEXT NOT NULL, platform_url TEXT NOT NULL, updated_at TEXT,
        PRIMARY KEY(shop_id, platform, source_hash)
    )""")
    # 数据面板:订单日报指标（只存聚合值，不存订单明细/买家信息）。
    # 金额单位统一为「元」；日期为自然日 YYYY-MM-DD（本地时区）。
    # pay_gmv/pay_uv/... 是**平台自带数据概览**（微信罗盘）的官方口径原值，单位分；
    # 平台不提供这些接口时留空（stats_source 为空），由订单聚合值兜底。
    conn.execute("""CREATE TABLE IF NOT EXISTS order_daily_metrics (
        shop_id TEXT NOT NULL, platform TEXT NOT NULL, stat_date TEXT NOT NULL,
        paid_orders INTEGER NOT NULL DEFAULT 0, paid_amount REAL NOT NULL DEFAULT 0,
        dealt_orders INTEGER NOT NULL DEFAULT 0, refund_amount REAL NOT NULL DEFAULT 0,
        actual_income REAL NOT NULL DEFAULT 0, actual_orders INTEGER NOT NULL DEFAULT 0,
        refund_rate REAL NOT NULL DEFAULT 0,
        order_count INTEGER NOT NULL DEFAULT 0, aftersale_count INTEGER NOT NULL DEFAULT 0,
        status_map_ready INTEGER NOT NULL DEFAULT 0, notes TEXT,
        pay_gmv INTEGER NOT NULL DEFAULT 0, pay_uv INTEGER NOT NULL DEFAULT 0,
        pay_order_cnt INTEGER NOT NULL DEFAULT 0, pay_refund_gmv INTEGER NOT NULL DEFAULT 0,
        product_click_uv INTEGER NOT NULL DEFAULT 0, stats_source TEXT,
        -- 自算的「人数」口径：按订单里的买家标识当天去重计数。
        -- 平台没有官方人数接口时（小红书）就用它；微信优先用罗盘 pay_uv。
        -- ⚠️ 只落**个数**，买家标识本身不落库、不返回（合规边界同"不存订单明细"）。
        paid_buyers INTEGER NOT NULL DEFAULT 0, created_buyers INTEGER NOT NULL DEFAULT 0,
        synced_at TEXT, sync_error TEXT,
        PRIMARY KEY(shop_id, platform, stat_date)
    )""")
    # 商品级日报（数据面板下方的「商品明细」用）：
    # 只存**商品级聚合**，不含任何买家/收件人信息（与 order_daily_metrics 同一合规口径）。
    # 金额单位统一「元」。refund_* 暂为 0：平台没给"哪个商品退了多少"，
    # 要拆退款必须再调售后详情（接口能拿到，属下一步），口径会在页面/导出里注明。
    # product_click_* / product_pay_* / product_create_uv / product_refund_amount 是
    # **平台商品级接口**（微信罗盘）的原值口径，只有 stats_source 有值时才可信；
    # 平台没这能力时留 0，前端显示「—」。
    # product_create_uv = 罗盘「下单人数」：用来补「点击 → 下单 → 支付」漏斗中间那一环。
    #   之所以有它而没有「加购」：已核对罗盘商品级接口返回的全部 31 个字段，无 cart/add_cart
    #   任何形态的加购指标，订单接口也拿不到（加购发生在成单之前），故加购列直接删掉。
    conn.execute("""CREATE TABLE IF NOT EXISTS product_daily_metrics (
        shop_id TEXT NOT NULL, platform TEXT NOT NULL, stat_date TEXT NOT NULL,
        product_id TEXT NOT NULL DEFAULT '', sku_id TEXT NOT NULL DEFAULT '',
        product_name TEXT, sku_code TEXT,
        paid_qty INTEGER NOT NULL DEFAULT 0, paid_amount REAL NOT NULL DEFAULT 0,
        refund_qty INTEGER NOT NULL DEFAULT 0, refund_amount REAL NOT NULL DEFAULT 0,
        order_count INTEGER NOT NULL DEFAULT 0, image_url TEXT,
        product_click_uv INTEGER NOT NULL DEFAULT 0, product_click_cnt INTEGER NOT NULL DEFAULT 0,
        product_pay_uv INTEGER NOT NULL DEFAULT 0, product_pay_cnt INTEGER NOT NULL DEFAULT 0,
        product_create_uv INTEGER NOT NULL DEFAULT 0,
        product_refund_amount REAL NOT NULL DEFAULT 0, stats_source TEXT,
        notes TEXT, synced_at TEXT,
        PRIMARY KEY(shop_id, platform, stat_date, product_id, sku_id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_product_metrics_range"
                 " ON product_daily_metrics(shop_id, platform, stat_date)")
    # 订单级明细（导出的「交易清单」与页面上的「商品明细」面板都靠它）。
    # 为什么以前不落、现在落：以前导出交易清单只能**实时**打平台接口逐笔拉，
    # 小红书光补详情一个店一天就要十几秒，导出动辄半分钟起步；落库后导出/页面都是本地读，
    # **秒出**。而且这些数据同步时本来就拉过（sync_order_metrics 里），只是之前用完就丢，
    # 所以落库**不增加任何平台调用**。
    # ⚠️ 合规：与前两张表同一口径——**不落任何买家标识/收件人信息**，只留交易维度。
    conn.execute("""CREATE TABLE IF NOT EXISTS order_items (
        shop_id TEXT NOT NULL, platform TEXT NOT NULL, stat_date TEXT NOT NULL,
        order_no TEXT NOT NULL DEFAULT '', order_id TEXT NOT NULL DEFAULT '',
        create_ts INTEGER NOT NULL DEFAULT 0, pay_ts INTEGER NOT NULL DEFAULT 0,
        paid_amount REAL NOT NULL DEFAULT 0, status_code INTEGER, status_text TEXT,
        confirm_amount REAL, item_qty INTEGER NOT NULL DEFAULT 0, item_count INTEGER NOT NULL DEFAULT 0,
        item_title TEXT, shop_name TEXT, synced_at TEXT,
        PRIMARY KEY(shop_id, platform, stat_date, order_no)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_order_items_range"
                 " ON order_items(shop_id, platform, stat_date)")


# 订单明细保留天数：导出与页面只需要近期和历史对比，超期就清，避免库无限变大
ORDER_ITEMS_KEEP_DAYS = 180


def _migrate_add_order_stats_columns(conn):
    """给 order_daily_metrics 补「平台自带数据概览」的列（老库升级用）。

    单独一个函数、并显式挂进 _ensure_db_schema：老库走到这里时表已存在，
    塞进已有迁移函数会因为它们提前 return 而补不上列（同 _migrate_add_publish_mode 的教训）。
    """
    for col_def in (
        "pay_gmv INTEGER NOT NULL DEFAULT 0",
        "pay_uv INTEGER NOT NULL DEFAULT 0",
        "pay_order_cnt INTEGER NOT NULL DEFAULT 0",
        "pay_refund_gmv INTEGER NOT NULL DEFAULT 0",
        "product_click_uv INTEGER NOT NULL DEFAULT 0",
        "stats_source TEXT",
    ):
        _safe_add_column(conn, "order_daily_metrics", col_def)


def _migrate_add_order_buyer_columns(conn):
    """给 order_daily_metrics 补「自算人数」两列（老库升级用）。

    单独一个函数并显式挂进 _ensure_db_schema，理由同 _migrate_add_order_stats_columns：
    老库走到这里表已存在，塞进别的迁移函数会因提前 return 而补不上列。
    """
    for col_def in (
        "paid_buyers INTEGER NOT NULL DEFAULT 0",
        "created_buyers INTEGER NOT NULL DEFAULT 0",
    ):
        _safe_add_column(conn, "order_daily_metrics", col_def)


def _migrate_add_product_image_column(conn):
    """给 product_daily_metrics 补 image_url 列（商品/SKU 主图）。

    单独一个函数并显式挂进确保流程：老库走到这里时表已存在。
    """
    _safe_add_column(conn, "product_daily_metrics", "image_url TEXT")


def _migrate_add_product_stats_columns(conn):
    """给 product_daily_metrics 补「平台商品级数据」列（老库升级用）。

    单独一个函数并显式挂进 _ensure_db_schema，理由同 _migrate_add_order_stats_columns：
    老库走到这里表已存在，塞进别的迁移函数会因提前 return 而补不上列。
    """
    for col_def in (
        "product_click_uv INTEGER NOT NULL DEFAULT 0",
        "product_click_cnt INTEGER NOT NULL DEFAULT 0",
        "product_pay_uv INTEGER NOT NULL DEFAULT 0",
        "product_pay_cnt INTEGER NOT NULL DEFAULT 0",
        "product_create_uv INTEGER NOT NULL DEFAULT 0",
        "product_refund_amount REAL NOT NULL DEFAULT 0",
        "stats_source TEXT",
    ):
        _safe_add_column(conn, "product_daily_metrics", col_def)


def _create_indexes(conn):
    """建索引。必须在补列 + 重建表之后调用（老库此时才有 shop_id 列、表约束才是新的）。"""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_batches_shop ON batches(shop_id, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_category_aliases_shop ON category_aliases(shop_id, updated_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_publish_jobs_shop ON publish_jobs(shop_id, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_publish_items_batch_product_platform ON publish_items(batch_id, product_code, platform)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_publish_items_pick ON publish_items(shop_id, platform, status, priority DESC, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_publish_items_product ON publish_items(shop_id, product_code, platform, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_image_cache_shop ON image_upload_cache(shop_id, platform, updated_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_order_metrics_shop_date ON order_daily_metrics(shop_id, stat_date DESC)")
    # 唯一索引（老库靠这两条兜底保证含 shop_id 的唯一性；新库表定义里已内置同名约束）
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_category_aliases_shop_internal "
        "ON category_aliases(shop_id, internal_category)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_image_cache_shop_platform_hash "
        "ON image_upload_cache(shop_id, platform, source_hash)"
    )


def _migrate_add_shop_id(conn):
    """把老库升级到多店铺版本（幂等）。

    本函数只负责「补列 + 回填」；表级唯一约束的重建在 _rebuild_multishop_tables。

    迁移内容：
      1. priority 列（更早版本遗留）—— 必须在建索引前补齐。
      2. batches.revision 列 —— 供跨进程批次缓存判新旧（见 get_batch_cached）。
      3. 5 张表的 shop_id / operator 列，历史数据回填默认 shop_id。

    默认 shop_id 从 shop_registry.default_shop() 取：
      - 已有 shops.json → 取其默认店铺的 shop_id（推荐：先建好 shops.json 再首次启动）
      - 无 shops.json    → 回退 "default"
    """
    # --- 1. 更早版本遗留的 priority 列（独立于 shop_id，先补） ---
    if "priority" not in {row["name"] for row in conn.execute("PRAGMA table_info(publish_items)")}:
        conn.execute("ALTER TABLE publish_items ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")

    # --- 2. batches.revision：跨进程批次缓存判新旧（见 get_batch_cached）。
    #     必须在下面「已是新版就 return」之前判断，否则已有库永远补不上这一列。 ---
    if "revision" not in {row["name"] for row in conn.execute("PRAGMA table_info(batches)")}:
        conn.execute("ALTER TABLE batches ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")

    # --- 3. shop_id 相关列 ---
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(batches)")}
    if "shop_id" in cols:
        return  # 已是新版

    try:
        default_sid = shop_registry.default_shop()["shop_id"]
    except ValueError:
        default_sid = "default"

    print(f"[db-migrate] 检测到老库，历史数据回填 shop_id = {default_sid}")

    _safe_add_column(conn, "batches", "shop_id TEXT NOT NULL DEFAULT '" + default_sid + "'")
    _safe_add_column(conn, "batches", "operator TEXT")
    _safe_add_column(conn, "category_aliases", "shop_id TEXT NOT NULL DEFAULT '" + default_sid + "'")
    _safe_add_column(conn, "publish_jobs", "shop_id TEXT NOT NULL DEFAULT '" + default_sid + "'")
    _safe_add_column(conn, "publish_jobs", "operator TEXT")
    _safe_add_column(conn, "publish_items", "shop_id TEXT NOT NULL DEFAULT '" + default_sid + "'")
    _safe_add_column(conn, "image_upload_cache", "shop_id TEXT NOT NULL DEFAULT '" + default_sid + "'")

    # 记住这次回填用了哪个 shop_id，供 _reassign_legacy_shop 在配置补齐后自动改名
    _meta_set(conn, "legacy_shop_id", default_sid)

    print("[db-migrate] 补列完成（唯一约束由 _rebuild_multishop_tables 处理）")


def _table_sql(conn, table):
    """取建表 DDL，表不存在返回 None。"""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["sql"] if row and row["sql"] else None


def _migrate_add_category_platform_columns(conn):
    """category_aliases 补「新平台」的映射列（接平台时逐个加，老库需要）。

    ⚠️ 必须在 `_rebuild_multishop_tables` **之前**执行：
       重建时要从 legacy 表 SELECT 这些列，列不存在会直接报 no such column。

    加列是纯追加（TEXT，可为空），对既有微信/小红书映射零影响。
    """
    _safe_add_column(conn, "category_aliases", "youzan_json TEXT")


def _rebuild_multishop_tables(conn):
    """重建仍带「单店时代唯一约束」的表。

    单店版本的这两张表有全局唯一约束，ALTER TABLE 加 shop_id 列改不掉它们，
    会直接破坏多店铺语义，必须整表重建（SQLite 不支持 DROP CONSTRAINT）：

      - category_aliases: 旧 UNIQUE(internal_category)
          → 不重建：B 店保存与 A 店同名的内部类目会报
            "UNIQUE constraint failed: category_aliases.internal_category"
            （因为 ON CONFLICT(shop_id, internal_category) 匹配不上旧约束）
          → 新：UNIQUE(shop_id, internal_category)

      - image_upload_cache: 旧 PRIMARY KEY(platform, source_hash)
          → 不重建：B 店上传同一张图会 REPLACE 掉 A 店的缓存行，
            之后 B 店会拿到 A 店素材空间里的 URL（跨店串素材，发错店铺）
          → 新：PRIMARY KEY(shop_id, platform, source_hash)

    幂等：按 DDL 文本判断，已是新结构则跳过；新库由 _create_tables 直接建成新结构。
    """
    # --- category_aliases ---
    sql = _table_sql(conn, "category_aliases")
    if sql:
        normalized = "".join(sql.split()).lower()
        if "unique(shop_id,internal_category)" not in normalized:
            count = conn.execute("SELECT COUNT(*) FROM category_aliases").fetchone()[0]
            print(f"[db-migrate] 重建 category_aliases（{count} 行）以移除单店唯一约束")
            conn.execute("ALTER TABLE category_aliases RENAME TO category_aliases_legacy")
            conn.execute("""CREATE TABLE category_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id TEXT NOT NULL,
                internal_category TEXT NOT NULL,
                wechat_json TEXT, xhs_json TEXT, youzan_json TEXT, updated_at TEXT,
                UNIQUE(shop_id, internal_category)
            )""")
            conn.execute("""INSERT INTO category_aliases(id,shop_id,internal_category,wechat_json,xhs_json,youzan_json,updated_at)
                SELECT id,shop_id,internal_category,wechat_json,xhs_json,youzan_json,updated_at
                FROM category_aliases_legacy""")
            conn.execute("DROP TABLE category_aliases_legacy")

    # --- image_upload_cache ---
    sql = _table_sql(conn, "image_upload_cache")
    if sql:
        normalized = "".join(sql.split()).lower()
        if "primarykey(shop_id,platform,source_hash)" not in normalized:
            count = conn.execute("SELECT COUNT(*) FROM image_upload_cache").fetchone()[0]
            print(f"[db-migrate] 重建 image_upload_cache（{count} 行）以移除跨店主键冲突")
            conn.execute("ALTER TABLE image_upload_cache RENAME TO image_upload_cache_legacy")
            conn.execute("""CREATE TABLE image_upload_cache (
                shop_id TEXT NOT NULL, platform TEXT NOT NULL, source_hash TEXT NOT NULL,
                source_url TEXT NOT NULL, platform_url TEXT NOT NULL, updated_at TEXT,
                PRIMARY KEY(shop_id, platform, source_hash)
            )""")
            conn.execute("""INSERT OR REPLACE INTO image_upload_cache(shop_id,platform,source_hash,source_url,platform_url,updated_at)
                SELECT shop_id,platform,source_hash,source_url,platform_url,updated_at
                FROM image_upload_cache_legacy""")
            conn.execute("DROP TABLE image_upload_cache_legacy")


def _safe_add_column(conn, table, col_def):
    """执行 ALTER TABLE ADD COLUMN，忽略已存在的列错误。"""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e).lower():
            return
        raise


def _migrate_add_job_params(conn):
    """给 publish_jobs 补 params_json 列（跨店发布的目标店参数快照）。幂等。

    ⚠️ 为什么不写进 _migrate_add_shop_id：那个函数在「表里已有 shop_id」时直接 return，
       而所有现存库都早已是多店版，新列塞进去会永远补不上（老库启动即报 no such column）。
    ⚠️ 已有任务行回填 '{}'：表示「与批次同店发布」，即改造前的语义，行为完全不变。
    """
    _safe_add_column(conn, "publish_jobs", "params_json TEXT NOT NULL DEFAULT '{}'")


def _migrate_add_publish_mode(conn):
    """给 publish_jobs / publish_items 补 mode 列（mock/sandbox/live）。幂等。

    ⚠️ 与 params_json 同理，必须独立成函数并在 _ensure_db_schema 里显式调用：
       _migrate_add_shop_id 对现存库会提前 return，新列塞进去永远补不上。
    ⚠️ 历史数据一律回填 'live'：改造前没有 mock 概念，所有历史发布记录都是真实的，
       回填 live 才与"统计默认只算 live"的口径一致（否则老记录会从发布状态页消失）。
    """
    _safe_add_column(conn, "publish_jobs", "mode TEXT NOT NULL DEFAULT 'live'")
    _safe_add_column(conn, "publish_items", "mode TEXT NOT NULL DEFAULT 'live'")


def _mode_scope(platforms, shop_id: str):
    """一个任务覆盖多平台时，把各平台的模式压成一个标签。

    - 全 mock  → "mock"
    - 全 live  → "live"
    - 其余     → "mixed"（部分 mock/部分 live，或含 sandbox）
    真值以 **publish_items.mode**（逐条）为准，本标签只给任务列表用。
    """
    modes = {mock_platform.resolve_mode(platform, shop_id) for platform in platforms}
    if len(modes) == 1:
        return modes.pop()
    return "mixed"


def _mode_where(request: Request) -> tuple[str, tuple]:
    """发布状态类查询的 mode 过滤（默认只算 live）。

    为什么默认只看 live：mock 任务是"假成功"——它没有在平台上产生任何商品。
    如果混进统计，会出现两种事故：
      1. 发布状态页显示"已发布"，运营以为发出去了，实际平台上一件都没有；
      2. 幂等保护把 mock 成功当成"发过了"，真正的发布被静默跳过（漏发，且不可见）。
    需要看 mock 记录时显式加 ``?mode=mock`` / ``?mode=all``。
    """
    raw = ""
    try:
        raw = str(request.query_params.get("mode") or "").strip().lower()
    except Exception:
        raw = ""
    if raw in ("mock", "sandbox", "mixed"):
        return " AND mode=?", (raw,)
    if raw == "all":
        return "", ()
    return " AND mode='live'", ()


def _meta_get(conn, key, default=None):
    try:
        row = conn.execute("SELECT value FROM _meta WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return row["value"] if row else default


def _meta_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO _meta(key,value) VALUES(?,?)", (key, value))


def _reassign_legacy_shop(conn):
    """老库回填用的 shop_id 后来在 shops.json 里改了名时，自动把历史数据迁到新默认店。

    场景：运营先启动了新版本（此时还没有 shops.json，历史数据回填成 "default"），
    之后才按模板创建 shops.json（默认店叫 diamond_01）。若不处理，276 个批次会变成孤儿。

    只在安全条件下自动迁移：
      - 老 shop_id 已不再出现在 shops.json 里；
      - 目标（默认店）名下一行数据都没有（避免两个店的数据混在一起）。
    不满足条件时只打印警告，由人工确认，绝不擅自合并数据。
    """
    legacy = _meta_get(conn, "legacy_shop_id")
    if not legacy:
        return
    try:
        target = shop_registry.default_shop()["shop_id"]
    except ValueError:
        return
    if legacy == target:
        return
    if shop_registry.get_shop(legacy):
        return  # 老店仍在配置里，说明命名没变，不动

    tables = ("batches", "category_aliases", "publish_jobs", "publish_items", "image_upload_cache")
    occupied = conn.execute("SELECT COUNT(*) FROM batches WHERE shop_id=?", (target,)).fetchone()[0]
    if occupied:
        print(f"[db-migrate] WARN: 历史数据在 '{legacy}' 下，但默认店 '{target}' 已有数据，"
              f"未自动迁移。请人工确认后用 SQL 手动改名（5 张表的 shop_id 列）。")
        return

    moved = 0
    for table in tables:
        cur = conn.execute(f"UPDATE {table} SET shop_id=? WHERE shop_id=?", (target, legacy))
        moved += cur.rowcount or 0
    _meta_set(conn, "legacy_shop_id", target)
    print(f"[db-migrate] 历史数据已从 '{legacy}' 迁移到默认店 '{target}'（共 {moved} 行）")


def _json_post(url, payload, timeout=180, shop_id=None, extra_headers=None):
    headers = {"Content-Type": "application/json"}
    if shop_id:
        headers[SHOP_HEADER] = shop_id
    if extra_headers:
        headers.update(extra_headers)
    # 调 wechat/xhs 服务：它们开启鉴权后必须带 X-API-Key，否则一律 401
    headers.update(api_auth.auth_headers())
    request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except Exception:
            detail = None
        raise RuntimeError(str(detail or exc)[:1000]) from exc
    except Exception as exc:
        raise RuntimeError(str(exc)[:500]) from exc
    return data


def fit_wechat_title(title):
    """微信标题上限 60 个字符（官方文档：中文/字母/数字各算 1 个字符）。"""
    return str(title or "")[:60]


def fit_xhs_title(title):
    """小红书标题压到合法长度。

    平台报错原文：标题长度需要在[8]-[30]个字或[16]-[60]个字符
      「字」= 每个字符算 1；「字符」= 中文/全角算 2、字母数字算 1；满足任一区间即可。
      取「≤30 个字」这一档最稳（≤30 字时加权必然 ≤60 字符）。
    """
    text = str(title or "")
    if len(text) > 30:
        return text[:30]
    if len(text) < 8:
        return (text + " 培育钻石")[:30]
    return text


def fit_youzan_title(title):
    """有赞标题压到合法长度。

    ★待核对：有赞未给出"标题多少字"的确定上限（docs §11-★5），这里先按 60 个字符
    与微信对齐。有赞对过短标题没有硬要求，所以不做"短文补词"（小红书的补词逻辑是平台规则）。
    真机报"标题过长/过短"时，只改这个函数与适配器的 title_fit_max。
    """
    return str(title or "")[:60]


# 平台 → 发布前的标题长度兜底函数。新平台先补 fit_* 再在这里登记，否则 platform_title 会 fail fast。
_TITLE_FITTERS = {
    "wechat": fit_wechat_title,
    "xhs": fit_xhs_title,
    "youzan": fit_youzan_title,
}


def platform_title(product, mapping, platform):
    """取要发给平台的标题，并做长度兜底。

    优先顺序：本平台标题列（运营在审核抽屉里改过的）→ 商品行里本平台的标题 → 通用标题。
    踩过的坑：只写 mapping.get("xhs_title") or product.get("title")，映射里没有小红书标题时
    就回落到「标题」= 微信那版（带品牌/系列，54 字/75 字符），触发 -5000500 标题长度错误，
    实测一个批次 250 个商品全因此失败。
    """
    entry = platform_registry.adapter(platform)
    raw = (mapping.get(entry["title_column"])
           or product.get(entry["title_column"])
           or product.get("title", ""))
    fit = _TITLE_FITTERS.get(entry["title_fit"])
    if fit is None:
        # 不静默用别家平台的规则裁剪（那会把标题裁成运营没预期的样子）
        raise RuntimeError(f"平台 {platform} 尚未实现标题长度兜底（缺少 {entry['title_fit']} 的 fit_* 函数）")
    fixed = fit(raw)
    if fixed != str(raw or ""):
        print(f"[{platform}] 标题长度不合规，已裁剪为 {fixed!r}（原 {str(raw)!r}）")
    return fixed


def _wechat_payload(product, mapping, shop_id=None):
    chain = _latest_wechat_chain(mapping.get("wechat_category_chain") or [], shop_id)
    if not chain:
        raise RuntimeError("未配置微信类目")
    attrs = mapping.get("wechat_attrs") or product.get("attributes") or {}
    attr_list = [{"attr_key": str(k), "attr_value": str(v)} for k, v in attrs.items() if v not in (None, "", [])]
    freight_id = mapping.get("wechat_freight_template_id") or mapping.get("freight_template_id")
    if not freight_id:
        raise RuntimeError("未配置微信运费模板")
    if not product.get("main_images"):
        raise RuntimeError("微信商品至少需要一张主图")
    item = {
        "title": platform_title(product, mapping, "wechat"),
        "out_product_id": product.get("product_code"),
        "head_imgs": product.get("main_images") or [],
        "cats_v2": [{"cat_id": n.get("cat_id") or n.get("id")} for n in chain],
        "brand_id": str(mapping.get("wechat_brand_id") or "2100000000"),
        "express_info": {"template_id": str(freight_id)},
        "attrs": attr_list,
        "deliver_method": 0,
        "extra_service": {"seven_day_return": 1, "freight_insurance": 0},
    }
    if product.get("description") or product.get("detail_images"):
        item["desc_info"] = {}
        if product.get("description"): item["desc_info"]["detail"] = product["description"]
        if product.get("detail_images"): item["desc_info"]["imgs"] = product["detail_images"]
    skus = []
    for sku in product.get("skus", []):
        attrs_sku = [{"attr_key": s.get("name", ""), "attr_value": s.get("value", "")} for s in sku.get("specs", []) if s.get("name") and s.get("value")]
        item_sku = {"out_sku_id": sku.get("sku_code"), "sale_price": round(float(sku.get("price") or 0) * 100),
                    "market_price": round(float(sku.get("original_price") or sku.get("price") or 0) * 100),
                    "stock_num": int(float(sku.get("stock") or 0)), "sku_attrs": attrs_sku}
        # SKU 缩略图: 没匹配到图就不传, 避免空串被判非法
        if sku.get("uploaded_sku_image"):
            item_sku["thumb_img"] = sku["uploaded_sku_image"]
        skus.append(item_sku)
    item["skus"] = skus
    return item


def _latest_wechat_chain(saved_chain, shop_id=None):
    """用已存映射的叶子类目名, 回微信服务取最新 cats_v2 完整链路。

    ⚠️ 必须带 shop_id: 微信服务的类目树/凭证按店铺隔离, 不带头会落到默认店,
    发出品用的链路与该店实际权限可能对不上(默认店凭证异常时还会直接 400)。
    """
    if not saved_chain:
        return []
    leaf = saved_chain[-1]
    leaf_id = str(leaf.get("cat_id") or leaf.get("id") or "")
    leaf_name = str(leaf.get("name") or "").strip()
    if not leaf_name:
        raise RuntimeError("微信类目映射缺少叶子类目名称，请重新确认类目")
    cache_key = (shop_id, leaf_id, leaf_name)
    if cache_key in _WECHAT_CATEGORY_CHAIN_CACHE:
        return _WECHAT_CATEGORY_CHAIN_CACHE[cache_key]
    try:
        data = _http_get_json(f"{WECHAT_API_BASE}/categories?keyword={urllib.parse.quote(leaf_name)}", timeout=180, shop_id=shop_id)
    except Exception as exc:
        # 原来只带一句 "HTTP Error 400: Bad Request", 既看不出是哪个店也看不出原因;
        # 两个平台服务都是 FastAPI, 真实原因在响应体的 detail 里。
        detail = _http_error_detail(exc) if isinstance(exc, urllib.error.HTTPError) else str(exc)
        raise RuntimeError(f"无法校验最新微信 cats_v2 类目（店铺 {shop_id or '默认店'}）：{detail}") from exc
    candidates = [item for item in data.get("results", []) if item.get("leaf")]
    matched = [item for item in candidates if str(item.get("cat_id")) == leaf_id]
    if len(matched) != 1:
        saved_names = [str(node.get("name") or "").strip() for node in saved_chain]
        matched = [item for item in candidates if [str(node.get("name") or "").strip() for node in item.get("chain", [])] == saved_names]
    if len(matched) != 1:
        raise RuntimeError(f"微信叶子类目 {leaf_name} 无法在最新 cats_v2 中唯一确认，请重新选择类目")
    chain = matched[0].get("chain") or []
    _WECHAT_CATEGORY_CHAIN_CACHE[cache_key] = chain
    return chain


def _local_image(source):
    """Resolve an importer-owned local:// reference without allowing path traversal."""
    if not source.startswith("local://"):
        return None
    relative = source[len("local://"):].replace("\\", "/")
    parts = relative.split("/")
    if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{32}", parts[0]) or not re.fullmatch(r"[0-9a-f]{64}\.[a-z0-9]{2,5}", parts[1]):
        raise RuntimeError("Excel 图片引用无效")
    path = os.path.abspath(os.path.join(IMAGE_DIR, parts[0], parts[1]))
    root = os.path.abspath(IMAGE_DIR)
    if os.path.commonpath((root, path)) != root or not os.path.isfile(path):
        raise RuntimeError("Excel 内嵌图片文件不存在")
    with open(path, "rb") as image_file:
        content = image_file.read()
    if not content:
        raise RuntimeError("Excel 内嵌图片内容为空")
    return path, content


def _platform_image(platform, source_url, shop_id=None):
    global _image_cache_hit_mem, _image_cache_hit_db, _image_cache_miss
    # shop_id 缺失时回退默认店：新库 image_upload_cache.shop_id 是 NOT NULL，
    # 不兜底的话写入会直接失败。正常调用方(worker)始终显式传入。
    if not shop_id:
        try:
            shop_id = shop_registry.default_shop()["shop_id"]
        except ValueError:
            shop_id = "default"  # 极端情况：完全没有可用店铺时也要能写入
    source_url = str(source_url or "").strip()
    if not source_url: return ""
    local_image = _local_image(source_url)
    if not local_image and is_share_image(source_url):
        # 共享盘图片懒拷贝: 只有真正发布到平台时才读字节(扫描阶段零磁盘)
        local_image = fetch_share_image(source_url, shop_id)
    source_hash = hashlib.sha256(local_image[1] if local_image else source_url.encode("utf-8")).hexdigest()
    cache_key = (shop_id or "", platform, source_hash)
    # 1. 内存 LRU 层
    if cache_key in _IMAGE_CACHE_MEM:
        _image_cache_hit_mem += 1
        # 移到末尾表示最近使用
        _IMAGE_CACHE_ORDER.remove(cache_key)
        _IMAGE_CACHE_ORDER.append(cache_key)
        return _IMAGE_CACHE_MEM[cache_key]
    # 2. SQLite 持久层
    conn = db()
    cached = conn.execute("SELECT platform_url FROM image_upload_cache WHERE shop_id=? AND platform=? AND source_hash=?", (shop_id, platform, source_hash)).fetchone()
    conn.close()
    if cached:
        _image_cache_hit_db += 1
        _image_cache_mem_put(cache_key, cached["platform_url"])
        return cached["platform_url"]
    # 3. 未命中: 真正上传
    _image_cache_miss += 1
    # 未知平台在这里就 fail fast，不会发出请求（也避免把图传到别家平台的服务上）
    entry = platform_registry.adapter(platform)
    base = platform_registry.api_base(platform)
    if local_image:
        payload = {"filename": os.path.basename(local_image[0]),
                   "content_base64": base64.b64encode(local_image[1]).decode("ascii")}
        response = _json_post(f"{base}{entry['image_file_path']}", payload, shop_id=shop_id)
    else:
        # 外链上传：端点与入参键名两个平台都不同（img_url / url），均由适配器给出
        response = _json_post(f"{base}{entry['image_url_path']}",
                              {entry["image_url_payload_key"]: source_url}, shop_id=shop_id)
    if response.get("ok") is False: raise RuntimeError(str(response.get("detail") or response)[:500])
    result = response.get("result")
    if isinstance(result, str): platform_url = result
    else: platform_url = ((result or {}).get("url") or (result or {}).get("materialUrl")
                          or (result or {}).get("fileUrl") or (result or {}).get("img_url")
                          # 有赞：商品接口只收素材 ID（image_ids），所以这里缓存的是 image_id，
                          # 不是可访问 URL（平台侧素材 ID 本来就不可外链，能拼进 payload 即可）。
                          or (result or {}).get("image_id"))
    if not platform_url: raise RuntimeError(f"{platform} 图片上传未返回地址")
    conn = db()
    conn.execute("INSERT OR REPLACE INTO image_upload_cache(shop_id,platform,source_hash,source_url,platform_url,updated_at) VALUES(?,?,?,?,?,?)",
                 (shop_id, platform, source_hash, source_url, platform_url, now()))
    conn.commit(); conn.close()
    _image_cache_mem_put(cache_key, platform_url)
    return platform_url


def _image_cache_mem_put(cache_key, platform_url):
    """把一条缓存写入内存层,超出上限时淘汰最久未用的。"""
    if cache_key in _IMAGE_CACHE_MEM:
        _IMAGE_CACHE_ORDER.remove(cache_key)
    _IMAGE_CACHE_MEM[cache_key] = platform_url
    _IMAGE_CACHE_ORDER.append(cache_key)
    while len(_IMAGE_CACHE_ORDER) > _IMAGE_CACHE_MAX:
        evicted = _IMAGE_CACHE_ORDER.pop(0)
        _IMAGE_CACHE_MEM.pop(evicted, None)


def _upload_images_parallel(platform: str, urls: list, max_workers: int = 10, shop_id=None) -> list:
    """并发上传图片并保序返回(替代原来的串行列表推导)。

    - 命中 image_upload_cache 的图片直接返回,不产生网络请求
    - 单张失败时抛出原始异常(与串行语义一致),发布项会标记 failed、可重试
    - max_workers 默认 10(平台限流风险可控,且缓存命中后实际请求远少于总数)
    """
    from concurrent.futures import ThreadPoolExecutor
    targets = [str(u or "").strip() for u in (urls or [])]
    targets = [u for u in targets if u]
    if not targets:
        return []
    if len(targets) == 1:
        return [_platform_image(platform, targets[0], shop_id=shop_id)]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as pool:
        # map 保序;任一任务抛异常会在迭代到该位置时重新抛出
        return list(pool.map(lambda u: _platform_image(platform, u, shop_id=shop_id), targets))


def _upload_image_uncached(platform, source_url, shop_id=None):
    """绕开 image_upload_cache 直接上传, 拿一个全新的平台素材地址。

    背景(实测): 小红书同一张图重复上传会返回**不同的**素材 URL, 而同一商品内多个 SKU
    共用同一个素材地址时, 小红书后台的「规格图」列显示为空; 图各不相同的商品则正常显示。
    所以只有"撞图"的 SKU 需要走这里补传一次, 没撞图的继续走缓存, 不做无谓重复上传。
    """
    local_image = _local_image(source_url)
    if not local_image and is_share_image(source_url):
        local_image = fetch_share_image(source_url, shop_id)
    if not local_image:
        raise RuntimeError(f"无法读取图片: {str(source_url)[:120]}")
    payload = {"filename": os.path.basename(local_image[0]),
               "content_base64": base64.b64encode(local_image[1]).decode("ascii")}
    entry = platform_registry.adapter(platform)
    base = platform_registry.api_base(platform)
    response = _json_post(f"{base}{entry['image_file_path']}", payload, shop_id=shop_id)
    if response.get("ok") is False:
        raise RuntimeError(str(response.get("detail") or response)[:300])
    result = response.get("result")
    if isinstance(result, str):
        return result
    return ((result or {}).get("url") or (result or {}).get("materialUrl")
            or (result or {}).get("fileUrl") or (result or {}).get("img_url")
            or (result or {}).get("image_id") or "")


def _upload_sku_images(platform, skus, shop_id=None):
    """并发上传各 SKU 的规格图, 保序返回并保留空位。

    ⚠️ 不能直接用 _upload_images_parallel: 它会过滤空值导致返回列表变短,
       与 skus 按下标 zip 会串图(把 A 的图挂到 B 上)。
    ⚠️ 同一商品内多个 SKU 共用同一张图 → 同一个素材地址 → 小红书后台规格图列显示为空。
       所以发现撞图时, 给后面的 SKU 重新上传一次, 保证每个 SKU 地址唯一。
       (货盘图库每种颜色常只存一套图, 同色不同分数必然撞图, 这里就是处理这种情况。)
    """
    references = [str(sku.get("sku_image") or "").strip() for sku in skus]
    pending = [r for r in references if r]
    if not pending:
        return ["" for _ in references]
    queue = list(_upload_images_parallel(platform, pending, shop_id=shop_id))
    urls = [queue.pop(0) if r else "" for r in references]
    seen = {}
    for index, url in enumerate(urls):
        if not url:
            continue
        if url in seen:
            try:
                urls[index] = _upload_image_uncached(platform, references[index], shop_id=shop_id)
            except Exception as exc:
                print(f"[sku_image] 撞图重新上传失败, 保留原地址: {exc}")
        else:
            seen[url] = index
    return urls


def _prepare_platform_images(product, platform, shop_id=None):
    prepared = dict(product)
    prepared["main_images"] = _upload_images_parallel(platform, product.get("main_images", []), shop_id=shop_id)
    prepared["detail_images"] = _upload_images_parallel(platform, product.get("detail_images", []), shop_id=shop_id)
    # SKU 规格图: 微信 → skus[].thumb_img, 小红书 → sku_list[].specImage
    skus = [dict(sku) for sku in product.get("skus", [])]
    for sku, url in zip(skus, _upload_sku_images(platform, skus, shop_id=shop_id)):
        if url:
            sku["uploaded_sku_image"] = url
    # 小红书开了「主规格图」后要求每个规格值都有图(否则报「启用规格大图需上传所有规格图」)。
    # 图库缺图的 SKU(如某颜色没拍图)用商品主图兜底, 保证每个颜色值都有图。
    # 该行为由适配器 sku_image_fallback 声明（微信不需要，其余平台按各自规则加字段即可）。
    fallback = (prepared.get("main_images") or [""])[0]
    if fallback and platform_registry.adapter(platform)["sku_image_fallback"]:
        for sku in skus:
            if not sku.get("uploaded_sku_image"):
                sku["uploaded_sku_image"] = fallback
    prepared["skus"] = skus
    return prepared


def _get_xhs_var_candidates(category_id, var_id, shop_id=None):
    """拉小红书某个规格维度(如「颜色分类」)的候选值,带缓存(12h)。
    用于 SKU 规格值映射:货盘英文编码 → 平台中文 valueName + valueId。"""
    key = (shop_id, str(category_id), str(var_id))
    hit = _XHS_VAR_CANDIDATES_CACHE.get(key)
    if hit and time.time() - hit["ts"] < 12 * 3600:
        return hit["data"]
    try:
        url = (f"{XHS_API_BASE}/attribute-values"
               f"?category_id={urllib.parse.quote(str(category_id))}"
               f"&attribute_id={urllib.parse.quote(str(var_id))}")
        data = _http_get_json(url, timeout=60, shop_id=shop_id)
        result = data.get("result") or {}
        cands = result.get("attributeValueV3s") or result.get("values") or []
        _XHS_VAR_CANDIDATES_CACHE[key] = {"ts": time.time(), "data": cands}
        return cands
    except Exception:
        return []


def _resolve_xhs_spec_value(source_value, var_id, category_id, shop_id=None):
    """把货盘 SKU 规格值(可能英文)映射成小红书平台的中文 valueName + valueId。
    顺序:① 精确匹配候选值 ② 模糊匹配 ③ 别名表(英文→中文) ④ 原值兜底(不带 valueId)。"""
    sv = str(source_value).strip()
    cands = _get_xhs_var_candidates(category_id, var_id, shop_id) or []
    hit = next((c for c in cands if c.get("valueName") == sv), None)
    if not hit:
        hit = next((c for c in cands if sv in (c.get("valueName") or '') or (c.get("valueName") or '') in sv), None)
    if hit:
        return hit.get("valueName", sv), hit.get("valueId", '')
    # 别名兜底:货盘英文 SKU 编码 → 平台中文 valueName
    for cn, aliases in XHS_SPEC_VALUE_ALIASES.items():
        if sv in aliases:
            vhit = next((c for c in cands if c.get("valueName") == cn), None)
            if vhit:
                return cn, vhit.get("valueId", '')
            return cn, ''
    return sv, ''


def _xhs_payload(product, mapping, group_config=None, shop_id=None):
    category_id = mapping.get("xhs_category_id")
    if not category_id:
        raise RuntimeError("未配置小红书类目")
    if not mapping.get("xhs_brand_id"):
        raise RuntimeError("小红书品牌未唯一匹配")
    if not mapping.get("xhs_shipping_template_id"):
        raise RuntimeError("未选择小红书运费模板")
    if not mapping.get("xhs_logistics_plan_id"):
        raise RuntimeError("未选择小红书物流方案")
    if not product.get("main_images"):
        raise RuntimeError("小红书商品至少需要一张主图")
    item = {"name": platform_title(product, mapping, "xhs"),
            "brandId": str(mapping.get("xhs_brand_id") or ""), "categoryId": str(category_id),
            "attributes": [], "shippingTemplateId": str(mapping.get("xhs_shipping_template_id") or ""),
            "shippingGrossWeight": int(float(product.get("weight") or 500)),
            "variantIds": [], "images": product.get("main_images") or [],
            "imageDescriptions": product.get("detail_images") or [],
            "description": product.get("description", ""), "deliveryMode": "0", "freeReturn": "1",
            "articleNo": (product.get("product_code") or "").split("-", 1)[0]}
    attrs = mapping.get("xhs_attrs") or {}
    for key, val in attrs.items():
        values = val if isinstance(val, list) else [val]
        for value_item in values:
            if isinstance(value_item, dict):
                item["attributes"].append(value_item)
            elif value_item not in (None, ""):
                item["attributes"].append({"propertyId": str(key), "valueId": str(value_item), "value": str(value_item)})
    xhs_config = (group_config or {}).get("xhs") or {}
    var_defs = xhs_config.get("var_defs") or []
    spec_map = xhs_config.get("spec_map") or {}
    candidates = xhs_config.get("candidates") or {}
    mapped_var_ids = [str(v.get("id")) for v in var_defs if spec_map.get(str(v.get("id"))) or spec_map.get(v.get("id"))]
    item["variantIds"] = mapped_var_ids
    # 「规格大图」开关:平台要求开启该开关的商品必须至少有一个主规格项(variantIds 非空),
    # 否则 createItemV2 直接报 -5000500「当前商品未包含主规格项,无法开启规格大图功能」。
    # 因此只在确实映射到规格维度时才带;没有维度(如 spec_map 为空)就不带,商品照常发布(只是没有规格图列)。
    # 注:该字段只能在创建时带,审核中的商品 updateItemV2 改不动它。
    if mapped_var_ids:
        item["enableMainSpecImage"] = True
    sku_list = []
    for sku in product.get("skus", []):
        variants = []
        specs_by_name = {s.get("name"): s.get("value") for s in sku.get("specs", [])}
        for var_def in var_defs:
            var_id = str(var_def.get("id"))
            source_name = spec_map.get(var_id) or spec_map.get(var_def.get("id"))
            source_value = specs_by_name.get(source_name)
            if not source_name or not source_value: continue
            # SKU 规格值:货盘英文编码 → 平台中文 valueName + valueId(小红书后台才能显示中文)
            value_name, value_id = _resolve_xhs_spec_value(source_value, var_id, category_id, shop_id)
            variant = {"id": var_def.get("id"), "name": var_def.get("name"), "value": value_name}
            if value_id:
                variant["valueId"] = value_id
            variants.append(variant)
        item_sku = {"ipq": 1, "originalPrice": round(float(sku.get("original_price") or sku.get("price") or 0) * 100),
                    "price": round(float(sku.get("price") or 0) * 100), "stock": int(float(sku.get("stock") or 0)),
                    "logisticsPlanId": str(mapping.get("xhs_logistics_plan_id") or ""), "variants": variants,
                    "deliveryTime": {"time": "24", "type": "RELATIVE_TIME_NEW"}, "erpCode": sku.get("sku_code")}
        # SKU 规格图: 没匹配到图就不传
        if sku.get("uploaded_sku_image"):
            item_sku["specImage"] = sku["uploaded_sku_image"]
        sku_list.append(item_sku)
    return {"item": item, "sku_list": sku_list}


def _youzan_price(amount_yuan):
    """中台价格（元）→ 有赞价格。

    ⚠️ 有赞 item.create 的 price 官方注明"单位:元"（与微信/小红书的"分"不同）。
    换算走适配器的 price_unit，不在业务代码里写死 —— 否则平台改口径时两处会打架。
    """
    value = platform_registry.price_to_platform("youzan", amount_yuan)
    return round(float(value or 0), 2)


def _youzan_payload(product, mapping, group_config=None, shop_id=None):
    """组装有赞 item.create 的请求体。

    ⚠️ 字段名与结构属 `*` 待核对项（docs/有赞平台适配实施方案.md §11-★2）：
    **本函数是有赞发布口径的唯一改动点**，联调时对着官方文档逐字校正即可。

    与微信/小红书的三点不同：
      1. 主图只收素材 ID（``image_ids``，逗号分隔 ≤15 张）——
         URL 已在 _platform_image 阶段换成了 image_id；
      2. ``price`` / ``quantity`` 是**商品级**必填（多规格商品另有 sku_list）；
      3. ``desc`` 必填且要求 >5 个字符，货盘通常没有描述 → 用标题合成，不写回货盘。
    """
    cid = mapping.get("youzan_cid") or mapping.get("youzan_category_id")
    if not cid:
        raise RuntimeError("未配置有赞类目")
    template_id = mapping.get("youzan_delivery_template_id") or mapping.get("delivery_template_id")
    if not template_id:
        raise RuntimeError("未选择有赞运费模板")
    images = [str(u).strip() for u in (product.get("main_images") or []) if str(u or "").strip()]
    if not images:
        raise RuntimeError("有赞商品至少需要一张主图")

    title = platform_title(product, mapping, "youzan")
    description = str(product.get("description") or "").strip()
    if len(description) < 6:
        description = f"{title}｜商品详情以实物与平台规格为准"

    item = {
        "title": title,
        "desc": description,
        "image_ids": ",".join(images[:15]),
        "cid": str(cid),
        "delivery_template_id": str(template_id),
        # 商家编码：ID 回填与"重发去重"都靠它（对应有赞 items.custom.get 的查询键）
        "item_no": product.get("product_code"),
    }

    skus = []
    for sku in product.get("skus", []):
        item_sku = {
            "price": _youzan_price(sku.get("price")),
            "quantity": int(float(sku.get("stock") or 0)),
            "sku_no": sku.get("sku_code"),
        }
        if sku.get("uploaded_sku_image"):
            item_sku["image_id"] = sku["uploaded_sku_image"]      # ★字段名待核对
        skus.append(item_sku)
    if skus:
        item["sku_list"] = skus
        # 商品级 price/quantity 同样是必填：取首个 SKU 价 + 库存合计兜底
        # （多规格时平台以 sku_list 为准，这两个值只影响后台列表展示）
        item["price"] = skus[0]["price"]
        item["quantity"] = sum(one["quantity"] for one in skus)
    else:
        item["price"] = _youzan_price(product.get("price"))
        item["quantity"] = int(float(product.get("stock") or 0))

    # 规格维度（"颜色/重量"这类）：货盘 specs → 有赞 sku_properties（★结构待核对）
    properties = {}
    for sku in product.get("skus", []):
        for spec in sku.get("specs") or []:
            name = str(spec.get("name") or "").strip()
            value = str(spec.get("value") or "").strip()
            if name and value:
                properties.setdefault(name, [])
                if value not in properties[name]:
                    properties[name].append(value)
    if properties:
        item["sku_properties"] = [{"name": name, "values": [{"name": v} for v in values]}
                                  for name, values in properties.items()]
    return item


# 跨店发布：任务(job)里存了目标店的店铺私有参数，执行时必须覆盖批次里的来源店参数，
# 否则会把来源店的运费模板/物流方案/品牌发给目标店（平台会拒：模板不属于该店）。
# 只覆盖这 4 个「按店不同」的键；类目/属性/SKU规格是平台级的，跨店通用，绝不能动。
_SHOP_SCOPED_MAPPING_KEYS = (
    "wechat_freight_template_id",   # 微信运费模板
    "xhs_shipping_template_id",     # 小红书运费模板
    "xhs_logistics_plan_id",        # 小红书物流方案
    "xhs_brand_id",                 # 小红书品牌
    "youzan_delivery_template_id",  # 有赞运费模板（每店不同）
)


def _job_params_of(row):
    """取任务所属 job 的店铺参数快照。

    row 由 worker 的 LEFT JOIN 查询产出；老库或同店发布时该字段可能是 NULL/空串，一律当空处理。
    """
    try:
        raw = row["job_params_json"]
    except (IndexError, KeyError):
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# 发布前必须齐的「店铺级参数」: 缺了平台会整批拒。
# 以前是建完任务、worker 逐个商品报"未配置微信运费模板", 运营看到满屏失败、回头补选还得重发一轮;
# 现在建任务前直接拦住(见 publish 接口), 一个任务项都不建。
# 每项 = (该平台映射里可能用的键..., 给人看的名字)；条目来自 platform_registry 的 required_params，
# 接新平台只改注册表（口径与 _wechat_payload/_xhs_payload 的必填判断保持一致）。
_REQUIRED_SHOP_PARAMS = {
    p: platform_registry.adapter(p)["required_params"] for p in platform_registry.all_platforms()
}


def _missing_shop_params(mappings, products, platforms, job_params=None):
    """列出这批商品发布前还缺哪些店铺级必填参数(空列表=齐全)。

    与 _wechat_payload/_xhs_payload 的必填判断保持同一套键: 跨店发布时先套用目标店参数快照,
    和 worker 真正发品时的取值口径一致, 避免"这里放行、那边照样失败"。
    """
    product_mappings = (mappings or {}).get("products", {}) or {}
    problems = []
    for platform in platforms:
        counts = {}
        for product in products:
            mapping = _apply_job_params(product_mappings.get(product.get("product_code")) or {}, job_params)
            for keys in _REQUIRED_SHOP_PARAMS.get(platform, ()):
                if not any(str(mapping.get(key) or "").strip() for key in keys[:-1]):
                    counts[keys[-1]] = counts.get(keys[-1], 0) + 1
        problems.extend(f"{label}（{count} 件商品未选）" for label, count in counts.items())
    return problems


def _apply_job_params(mapping, job_params):
    """把目标店的店铺私有参数覆盖到映射上。

    ⚠️ 必须返回副本：mapping 取自批次缓存(get_batch_cached)，就地改写会污染缓存，
       导致之后同一批次的「同店发布」也用上目标店的模板。
    """
    if not job_params:
        return mapping
    merged = dict(mapping)
    for key in _SHOP_SCOPED_MAPPING_KEYS:
        value = job_params.get(key)
        if value:
            merged[key] = value
    return merged


def _publish_wechat(product, mapping, group_config, shop_id):
    """微信发布：一个接口创建商品（含 SKU）。返回 {product_id: ...} 或商品对象。

    ⚠️ 与改造前人肉 diff 过：图片顺序、payload 组装、返回结构完全一致。
    """
    product = _prepare_platform_images(product, "wechat", shop_id=shop_id)
    path = platform_registry.adapter("wechat")["publish_path"]
    result = _json_post(f"{WECHAT_API_BASE}{path}", _wechat_payload(product, mapping, shop_id), shop_id=shop_id)
    if result.get("ok") is False: raise RuntimeError(str(result.get("detail") or result)[:500])
    value = result.get("result")
    return {"product_id": value} if not isinstance(value, dict) else value


def _publish_xhs(product, mapping, group_config, shop_id):
    """小红书发布：一个接口创建商品（含 SKU）。部分成功时不抛错，返回带 partial_error 的结果供前端展示。"""
    product = _prepare_platform_images(product, "xhs", shop_id=shop_id)
    path = platform_registry.adapter("xhs")["publish_path"]
    result = _json_post(f"{XHS_API_BASE}{path}", _xhs_payload(product, mapping, group_config, shop_id), shop_id=shop_id)
    value = result.get("result") or {}
    if value.get("itemId") and (result.get("partial") or value.get("skuErrors")):
        value["partial_error"] = json.dumps(value.get("skuErrors") or [], ensure_ascii=False)
        return value
    if result.get("ok") is False: raise RuntimeError(str(result.get("detail") or result)[:500])
    return value


def _publish_youzan(product, mapping, group_config, shop_id):
    """有赞发布：一个接口创建商品（含 SKU）。

    返回体归一：有赞返回 item_id / sku_ids（★字段名待核对），这里补一个中台通用的
    ``product_id`` 键，让 publish_items.platform_product_id 的写入与别家平台一致。
    """
    product = _prepare_platform_images(product, "youzan", shop_id=shop_id)
    path = platform_registry.adapter("youzan")["publish_path"]
    result = _json_post(f"{YOUZAN_API_BASE}{path}",
                        _youzan_payload(product, mapping, group_config, shop_id), shop_id=shop_id)
    if result.get("ok") is False:
        raise RuntimeError(str(result.get("detail") or result)[:500])
    value = result.get("result")
    if isinstance(value, dict):
        if value.get("item_id") and not value.get("product_id"):
            value["product_id"] = value["item_id"]
        return value
    return {"product_id": value}


# 平台 → 发布处理器。接新平台 = 在 platform_registry 加一条适配器 + 在这里加一个 _publish_<平台>，
# 不需要在编排层再加 if 分支（分支一旦漏掉就会走到"当成别家平台发布"）。
_PUBLISH_HANDLERS = {
    "wechat": _publish_wechat,
    "xhs": _publish_xhs,
    "youzan": _publish_youzan,
}


def _run_publish_item(row):
    shop_id = row["shop_id"]
    platform = row["platform"]
    handler = _PUBLISH_HANDLERS.get(platform)
    if handler is None: raise RuntimeError(f"不支持的平台: {platform}")
    # 用缓存版:避免每个发布项都重新 json.loads 全量 rows + mappings(800 商品时单次近百毫秒)
    batch = get_batch_cached(row["batch_id"])
    product = next((p for p in batch["products"] if p.get("product_code") == row["product_code"]), None)
    if not product: raise RuntimeError("商品不存在")
    # 批次映射(来源店) + 任务参数(目标店) → 真正用于发布的映射
    mapping = _apply_job_params(
        batch["mappings"].get("products", {}).get(row["product_code"], {}),
        _job_params_of(row),
    )
    group_config = batch["mappings"].get("attr_groups", {}).get(product.get("internal_category"), {})
    return handler(product, mapping, group_config, shop_id)


def recover_interrupted_items(shop_id=None):
    """启动时把中断在 running 状态的项改回 queued。

    多店铺版：可选按 shop_id 恢复；不传则恢复所有店的中断项。
    """
    conn = db()
    if shop_id:
        conn.execute("UPDATE publish_items SET status='queued' WHERE status='running' AND shop_id=?", (shop_id,))
    else:
        conn.execute("UPDATE publish_items SET status='queued' WHERE status='running'")
    conn.commit(); conn.close()


def _platform_ready(platform, shop_id=None):
    try:
        # 未知平台会抛 ValueError → 与"连不上"同样处理成 False（fail closed，不会误判为可用）
        url = f"{platform_registry.api_base(platform)}/health"
        if shop_id:
            req = urllib.request.Request(url, headers={SHOP_HEADER: shop_id})
        else:
            req = url
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def _worker_loop(platform=None, shop_id=None):
    """发布 worker 主循环。

    多店铺模式：
      - shop_id 非空：只处理该店铺的任务（按平台过滤，platform=None 则全平台）。
      - shop_id 为空：兼容旧行为，取默认店铺。
    """
    if not shop_id:
        try:
            shop_id = shop_registry.default_shop()["shop_id"]
        except ValueError:
            shop_id = "default"
    while True:
        conn = None
        try:
            if platform and not _platform_ready(platform, shop_id=shop_id):
                time.sleep(5)
                continue
            conn = db()
            conn.execute("BEGIN IMMEDIATE")
            # priority DESC: 分批发布(只选中几件)的项插到前面先跑, 再按 id 先到先服务
            # JOIN 出 job 的店铺参数快照：跨店发布时用它覆盖批次里的来源店参数
            if platform:
                item = conn.execute(
                    "SELECT i.*, j.params_json AS job_params_json FROM publish_items i "
                    "LEFT JOIN publish_jobs j ON j.id = i.job_id "
                    "WHERE i.shop_id=? AND i.status='queued' AND i.platform=? "
                    "ORDER BY i.priority DESC, i.id LIMIT 1",
                    (shop_id, platform)
                ).fetchone()
            else:
                item = conn.execute(
                    "SELECT i.*, j.params_json AS job_params_json FROM publish_items i "
                    "LEFT JOIN publish_jobs j ON j.id = i.job_id "
                    "WHERE i.shop_id=? AND i.status='queued' "
                    "ORDER BY i.priority DESC, i.id LIMIT 1",
                    (shop_id,)
                ).fetchone()
            if not item:
                conn.commit(); conn.close()
                # 数据面板：空闲时顺手做一次「每天最多一次」的订单指标同步（date-guard，不新增调度器）。
                # 放在空闲分支是为了不阻塞发品；失败只打日志，绝不影响发布链路。
                try:
                    maybe_sync_order_metrics(shop_id, platform)
                except Exception as exc:  # noqa: BLE001
                    print(f"[orders] 自动同步跳过（不影响发布）：{exc}", flush=True)
                time.sleep(1)
                continue
            conn.execute("UPDATE publish_items SET status='running', started_at=? WHERE id=? AND shop_id=?",
                         (now(), item["id"], shop_id)); conn.commit(); conn.close()
            try:
                result = _run_publish_item(item)
                product_id = result.get("product_id") or result.get("itemId") or result.get("id")
                sku_ids = result.get("skuIds") or []
                status = "partial" if result.get("partial_error") else "success"
                conn = db(); conn.execute(
                    "UPDATE publish_items SET status=?, platform_product_id=?, platform_sku_ids=?, error=?, finished_at=? "
                    "WHERE id=? AND shop_id=?",
                    (status, str(product_id or ""), json.dumps(sku_ids), result.get("partial_error"),
                     now(), item["id"], shop_id)); conn.commit(); conn.close()
            except Exception as exc:
                conn = db(); conn.execute(
                    "UPDATE publish_items SET status='failed', error=?, finished_at=? WHERE id=? AND shop_id=?",
                    (str(exc)[:1000], now(), item["id"], shop_id)); conn.commit(); conn.close()
            conn = db(); conn.execute("""UPDATE publish_jobs SET processed=(SELECT COUNT(*) FROM publish_items WHERE shop_id=? AND job_id=? AND status IN ('success','failed','partial')), success=(SELECT COUNT(*) FROM publish_items WHERE shop_id=? AND job_id=? AND status='success'), failed=(SELECT COUNT(*) FROM publish_items WHERE shop_id=? AND job_id=? AND status IN ('failed','partial')), status=CASE WHEN (SELECT COUNT(*) FROM publish_items WHERE shop_id=? AND job_id=? AND status IN ('queued','running'))=0 THEN CASE WHEN (SELECT COUNT(*) FROM publish_items WHERE shop_id=? AND job_id=? AND status IN ('failed','partial'))>0 THEN 'completed_with_errors' ELSE 'completed' END ELSE 'running' END, updated_at=? WHERE id=? AND shop_id=?""",
                (shop_id, item["job_id"], shop_id, item["job_id"], shop_id, item["job_id"],
                 shop_id, item["job_id"], shop_id, item["job_id"], now(), item["job_id"], shop_id))
            conn.commit(); conn.close()
        except Exception:
            # 异常时必须回滚并关闭连接,否则 BEGIN IMMEDIATE 持有的写锁会一直不释放,锁死整个库
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            time.sleep(2)


class ImportBody(BaseModel):
    filename: str = "products.xlsx"
    content_base64: str
    zip_base64: str | None = None
    folder_files: list[dict] | None = None
    common_detail_files: list[dict] | None = None


class HuopaiImportBody(BaseModel):
    """货盘直读导入:读服务器本地货盘表,后台转换后入库(不走文件上传)。

    路径收敛: 只接受「货盘目录(HUOPAI_DIR,默认取 HUOPAI_PATH 所在目录)」内的 .xlsx,
    目录由服务端配置决定,不接受客户端指定任意目录。
    """
    path: str | None = None        # 货盘目录内的 .xlsx 路径;不传则用默认货盘表


class ImageScanBody(BaseModel):
    """图片直读:扫描图片根目录, 把商品图/SKU 图挂到批次上(不走浏览器上传, 几百个商品也没压力)。"""
    root: str | None = None            # 必须是服务端白名单内的图片根目录;不传用默认路径
    months: list[str] | None = None    # 只扫指定月份目录(如 ["7-9月份"]);None = 全部
    dry_run: bool = False              # True=只匹配不拷贝(先看命中率)


class ItemsBody(BaseModel):
    items: list[dict]


class SkuImageBody(BaseModel):
    """补规格图: 运营在批次表里给缺图的 SKU 单张上传(JSON+Base64, 与本项目其它上传接口一致)。"""
    content_base64: str
    filename: str | None = None


class MappingsBody(BaseModel):
    mappings: dict


class PublishBody(BaseModel):
    platforms: list[str]
    product_codes: list[str] | None = None  # 不传=全量发布,传了=只发布指定商品(分批发布)
    # 跨店发布：目标店铺（不传=当前店铺，即改造前的同店发布行为）。
    # 任务写到目标店名下(worker 用该店凭证发布)，params 是该店的私有参数
    # （微信运费模板 / 小红书运费模板+物流方案+品牌），执行时覆盖批次里的来源店参数。
    target_shop_id: str | None = None
    params: dict | None = None


class RetryBody(BaseModel):
    item_ids: list[int] | None = None


class AliasBody(BaseModel):
    internal_category: str
    wechat: dict | None = None
    xhs: dict | None = None
    youzan: dict | None = None


class AliasImportBody(BaseModel):
    filename: str = "category-aliases.xlsx"
    content_base64: str


def now():
    return datetime.now(timezone.utc).isoformat()


def value(row, *names):
    for name in names:
        if name in row and row[name] not in (None, ""):
            return str(row[name]).strip()
    return ""


def _embedded_image_extension(image, content):
    image_format = str(getattr(image, "format", "") or "").lower()
    if image_format in ("jpeg", "jpg") or content.startswith(b"\xff\xd8"):
        return "jpg"
    if image_format == "png" or content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_format == "gif" or content.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if image_format == "webp" or (content.startswith(b"RIFF") and content[8:12] == b"WEBP"):
        return "webp"
    return "png"


def _extract_embedded_images(sheet, headers, batch_id):
    column_types = {}
    for index, header in enumerate(headers):
        if header in ("主图", "主图链接"):
            column_types[index] = "main_images"
        elif header in ("详情图", "详情图链接"):
            column_types[index] = "detail_images"
    extracted = {}
    batch_dir = os.path.join(IMAGE_DIR, batch_id)
    for image in getattr(sheet, "_images", []):
        anchor = getattr(image, "anchor", None)
        marker = getattr(anchor, "_from", None)
        if marker is None or marker.col not in column_types or marker.row < 1:
            continue
        try:
            content = image._data()
        except Exception as exc:
            raise HTTPException(400, f"Excel 第 {marker.row + 1} 行内嵌图片读取失败：{exc}")
        digest = hashlib.sha256(content).hexdigest()
        extension = _embedded_image_extension(image, content)
        os.makedirs(batch_dir, exist_ok=True)
        path = os.path.join(batch_dir, f"{digest}.{extension}")
        if not os.path.exists(path):
            with open(path, "wb") as image_file:
                image_file.write(content)
        reference = f"local://{batch_id}/{digest}.{extension}"
        values = extracted.setdefault(marker.row + 1, {}).setdefault(column_types[marker.col], [])
        if reference not in values:
            values.append(reference)
    return extracted


def parse_excel(raw: bytes, filename: str, batch_id=None):
    extension = os.path.splitext(filename or "")[1].lower()
    if extension == ".xls":
        raise HTTPException(400, "暂不支持旧版 .xls，请另存为 .xlsx 后再导入")
    if extension == ".csv":
        try:
            text = raw.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            return [normalize_row(row, index) for index, row in enumerate(reader, 2) if any(str(v or "").strip() for v in row.values())]
        except UnicodeDecodeError:
            raise HTTPException(400, "CSV 请保存为 UTF-8 编码后再导入")
        except Exception as exc:
            raise HTTPException(400, f"CSV 解析失败：{exc}")
    try:
        import openpyxl
        workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=False, data_only=True)
        sheet = workbook.active
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(x or "").strip() for x in rows[0]]
        embedded = _extract_embedded_images(sheet, headers, batch_id) if batch_id else {}
        result = []
        for index, cells in enumerate(rows[1:], 2):
            if not any(cell not in (None, "") for cell in cells):
                continue
            item = {headers[i]: cells[i] if i < len(cells) else "" for i in range(len(headers)) if headers[i]}
            normalized = normalize_row(item, index)
            for field, references in embedded.get(index, {}).items():
                normalized[field] = list(dict.fromkeys(normalized.get(field, []) + references))
            result.append(normalized)
        return result
    except Exception as exc:
        raise HTTPException(400, f"Excel 解析失败：{exc}")


def _parse_attributes(text):
    """解析商品属性列: "材质=18K金;钻石切工=Very good" → {"材质": "18K金", "钻石切工": "Very good"}"""
    attrs = {}
    if not text:
        return attrs
    for pair in re.split(r"[;；]", text):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip():
                attrs[k.strip()] = v.strip()
    return attrs


_CN_NUM = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}


def _parse_specs(row, max_specs=20):
    """动态读取 规格N名称/规格N值 列（N 从 1 递增），返回 [{"name":.., "value":..}]。
    表头支持阿拉伯数字与中文数字（规格1名称 / 规格一名称）；只收集填了名称或值的规格列，按列序排列。
    规格维度数量不写死，运营可自由增减 Excel 规格列。"""
    specs = []
    for n in range(1, max_specs + 1):
        cn = _CN_NUM.get(n, str(n))
        name = value(row, f"规格{n}名称", f"规格{cn}名称")
        val = value(row, f"规格{n}值", f"规格{cn}值")
        if name or val:
            specs.append({"name": name, "value": val})
    return specs


def normalize_row(row, line):
    sku_code = value(row, "SKU编码", "SKU 编码", "sku_code", "erpCode")
    product_code = value(row, "商品编码", "商品 编码", "product_code", "articleNo")
    price = value(row, "售价", "售价（元）", "price", "price_yuan")
    stock = value(row, "库存", "stock")
    errors = []
    if not product_code:
        errors.append("缺少商品编码")
    if not value(row, "标题", "商品标题", "name"):
        errors.append("缺少标题")
    if not sku_code:
        errors.append("缺少SKU编码")
    try:
        if float(price) <= 0:
            errors.append("售价必须大于0")
    except (TypeError, ValueError):
        errors.append("售价格式错误")
    try:
        if int(float(stock)) < 0:
            errors.append("库存不能小于0")
    except (TypeError, ValueError):
        errors.append("库存格式错误")
    normalized = {
        "line": line, "product_code": product_code,
        "title": value(row, "标题", "商品标题", "name"),
        "wechat_title": value(row, "微信标题"), "xhs_title": value(row, "小红书标题"),
        "internal_category": value(row, "内部类目", "类目"),
        "brand": value(row, "品牌"), "description": value(row, "描述", "商品描述"),
        "attributes": _parse_attributes(value(row, "商品属性", "属性", "attributes")),
        "weight": value(row, "重量", "商品毛重"),
        "main_images": value(row, "主图", "主图链接").split(",") if value(row, "主图", "主图链接") else [],
        "detail_images": value(row, "详情图", "详情图链接").split(",") if value(row, "详情图", "详情图链接") else [],
        "sku_code": sku_code, "specs": _parse_specs(row),
        "original_price": value(row, "原价", "原价（元）"),
        "price": price, "stock": stock, "errors": errors,
    }
    # 货盘直读的来源定位(哪张表哪一行/原始商家编码)要原样带下去:
    # 发布完「回填平台商品ID到货盘表」靠它定位原文件的单元格。
    for key in ("_源表", "_源行", "_源码", "_系列原文"):
        if key in row:
            normalized[key] = row[key]
    return normalized


# 「图片还在共享盘/本地磁盘上」的引用前缀: 扫描阶段只记路径, 真正上传平台时才读字节落盘(懒拷贝)
SHARE_PREFIX = "disk:"


def is_share_image(source):
    """是否是共享盘图片引用(懒加载), 形如 'disk:\\\\192.168.10.250\\...\\a.jpg'。"""
    return str(source or "").startswith(SHARE_PREFIX)


# 共享盘读取失败的分级处理（线上实测：Worker 以 SYSTEM 跑时整批报 WinError 1326）：
#   53 找不到网络路径 / 64 网络名不再可用 / 1219 凭据冲突 / 1231 网络不可达
#     → 网络或 SMB 会话抖动，等一会儿重试通常就恢复，自动重试；
#   1326 用户名或密码不正确
#     → 运行 Worker 的账户根本没有该共享的凭据（典型：SYSTEM / 服务会话），
#       重试只会把 NAS 账号试到锁定，所以不重试，直接给可执行的排查提示，
#       并在冷却期内快速失败（一批几百张图不会反复去试错密码）。
_SHARE_TRANSIENT_WINERRORS = {53, 64, 1219, 1231}
_SHARE_AUTH_WINERROR = 1326
_SHARE_AUTH_COOLDOWN = 60.0
_SHARE_READ_ATTEMPTS = 3
_share_auth_failed_until = 0.0


def _winerror_code(exc):
    """取 OSError 的 Windows 错误码（拿不到返回 None）。"""
    code = getattr(exc, "winerror", None)
    if code:
        return code
    if exc.args and isinstance(exc.args[0], int):
        return exc.args[0]
    return None


def _share_error_text(path, exc, code):
    if code == _SHARE_AUTH_WINERROR:
        return (f"共享盘不可访问：{path}（{exc}）\n"
                "原因：运行发布 Worker 的账户没有该共享盘的登录凭据"
                "（常见于 Worker 被 SYSTEM / 服务方式启动——共享盘凭据只保存在登录用户的凭据管理器里）。\n"
                "处理：用有共享盘权限的登录用户启动 Worker（bulk_api\\start_worker.ps1）；"
                "并确认计划任务 BulkWorkerAutostart 的身份是当前登录用户，不是 SYSTEM。")
    if code in _SHARE_TRANSIENT_WINERRORS:
        return (f"共享盘暂时不可达：{path}（{exc}）"
                f"——已自动重试 {_SHARE_READ_ATTEMPTS} 次仍失败，稍后重试该商品即可")
    return f"共享盘不可访问：{path}（{exc}）"


def fetch_share_image(source, shop_id=None):
    """读共享盘上的图片并落盘缓存, 返回 (本地路径, 内容)。

    只在真正发布时调用: 扫描 233 个商品只记路径(秒级、零磁盘),
    发到哪个商品才读哪个商品的图, 不发布的商品完全不占磁盘。
    落盘按内容 sha256 命名, 同一张图(通用详情图/同款多 SKU 共用图)只存一份。

    安全: disk: 引用会存进批次数据, 而批次数据可由客户端通过 PUT /import/{id}/items
    覆写, 因此这里必须再校验一次路径白名单, 否则可被用来读服务器任意文件并上传平台。
    白名单必须按店铺取(见 _allowed_roots_for)，否则服务器上挂载点路径会被误拒。

    容错: 见上面 _SHARE_TRANSIENT_WINERRORS / _SHARE_AUTH_WINERROR 的说明。
    """
    global _share_auth_failed_until
    path = str(source)[len(SHARE_PREFIX):]
    if not path or not _within_allowed_roots(path, _allowed_roots_for(shop_id)):
        raise RuntimeError(f"共享盘图片路径不在允许的图片目录内：{path}")
    if time.monotonic() < _share_auth_failed_until:
        raise RuntimeError(_share_error_text(
            path, "Worker 账户没有共享盘凭据，冷却期内不重复尝试", _SHARE_AUTH_WINERROR))
    # 用 os.stat 而不是 os.path.isfile: 后者把"无权限 / 网络不可达"也一并吞成 False,
    # 让权限问题伪装成"文件不存在"(线上踩过: worker 以 SYSTEM 跑时整批报"不存在")。
    for attempt in range(1, _SHARE_READ_ATTEMPTS + 1):
        try:
            os.stat(path)
            break
        except FileNotFoundError:
            raise RuntimeError(f"共享盘图片不存在(可能已被移动或删除)：{path}")
        except PermissionError:
            raise RuntimeError(f"共享盘图片无权读取(Worker 运行账户对该共享没有权限)：{path}")
        except OSError as exc:
            code = _winerror_code(exc)
            if code == _SHARE_AUTH_WINERROR:
                _share_auth_failed_until = time.monotonic() + _SHARE_AUTH_COOLDOWN
                raise RuntimeError(_share_error_text(path, exc, code))
            if code not in _SHARE_TRANSIENT_WINERRORS or attempt == _SHARE_READ_ATTEMPTS:
                raise RuntimeError(_share_error_text(path, exc, code))
            time.sleep(0.5 * attempt)
    if not os.path.isfile(path):
        raise RuntimeError(f"共享盘路径不是图片文件：{path}")
    with open(path, "rb") as image_file:
        content = image_file.read()
    if not content:
        raise RuntimeError(f"共享盘图片内容为空：{path}")
    digest = hashlib.sha256(content).hexdigest()
    extension = os.path.splitext(path)[1].lower() or ".jpg"
    target_dir = os.path.join(IMAGE_DIR, "_share")
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, f"{digest}{extension}")
    if not os.path.exists(target):
        with open(target, "wb") as image_file:
            image_file.write(content)
    return target, content


def _store_folder_images(files, batch_id, rows, common_detail_files=None):
    """Save browser-selected folder files and resolve Excel image filenames."""
    batch_dir = os.path.join(IMAGE_DIR, batch_id)
    os.makedirs(batch_dir, exist_ok=True)
    references = {}
    file_records = []
    common_details = []
    for item in [*(files or []), *(common_detail_files or [])]:
        relative = str(item.get("path") or item.get("name") or "").replace("\\", "/").strip()
        encoded = item.get("content_base64") or ""
        if not relative or not encoded:
            continue
        parts = [part for part in relative.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            continue
        name = os.path.basename(relative)
        try:
            content = base64.b64decode(encoded, validate=True)
        except Exception:
            continue
        if not name or not content or len(content) > 20 * 1024 * 1024:
            continue
        digest = hashlib.sha256(content).hexdigest()
        extension = os.path.splitext(name)[1].lower() or ".jpg"
        target = os.path.join(batch_dir, f"{digest}{extension}")
        if not os.path.exists(target):
            with open(target, "wb") as image_file:
                image_file.write(content)
        reference = f"local://{batch_id}/{digest}{extension}"
        references[relative.lower()] = reference
        references.setdefault(name.lower(), reference)
        file_records.append((name, reference))
        if item in (common_detail_files or []):
            common_details.append(reference)
    for row in rows:
        for field in ("main_images", "detail_images"):
            row[field] = [
                references.get(str(source or "").strip().replace("\\", "/").lower(),
                               references.get(os.path.basename(str(source or "").strip()).lower(), source))
                for source in row.get(field, [])
            ]
        code = str(row.get("product_code") or "").strip().lower()
        # 取基础商品编码(去掉 -系列 后缀)用于匹配文件名,这样 KGN1000330主图1.jpg 能匹配 KGN1000330-永恒
        base_code = code.split("-", 1)[0] if code else ""
        if base_code:
            matches = [(name, ref) for name, ref in file_records if os.path.splitext(name)[0].lower().startswith(base_code)]
            if not row.get("main_images"):
                main_candidates = [ref for name, ref in sorted(matches) if "主图" in name or "main" in name.lower()]
                if not main_candidates and matches:
                    # 没有「主图/main」字样时,取匹配到的第一张当主图
                    main_candidates = [sorted(matches)[0][1]]
                row["main_images"] = main_candidates
            if not row.get("detail_images"):
                detail_candidates = [ref for name, ref in sorted(matches) if "详情" in name or "detail" in name.lower()]
                if not detail_candidates and len(matches) > 1:
                    # 没有「详情/detail」字样且多张时,除主图外都当详情图
                    detail_candidates = [ref for name, ref in sorted(matches)[1:]]
                row["detail_images"] = detail_candidates
            for reference in common_details:
                if reference not in row["detail_images"]:
                    row["detail_images"].append(reference)
            # 兜底:商品没匹配到主图时,优先用本次批次里 1:1 且 ≥800 的图(KGN 这类),其次才用通用详情
            # 1:1 + ≥800 才能过小红书 createItemV2 的「图片像素不低于800x800 (1:1)」
            if not row.get("main_images"):
                fallback = None
                # 先从文件夹图片里挑 1:1 且 ≥800 的
                for _n, _r in file_records:
                    try:
                        from PIL import Image as _Img
                        import io as _io
                        _p = _local_image(_r)
                        if not _p: continue
                        with _Img.open(_io.BytesIO(_p[1])) as _im:
                            _w, _h = _im.size
                            if _w >= 800 and _h == _w:
                                fallback = _r
                                break
                    except Exception:
                        continue
                # 再从通用详情里挑 1:1 且 ≥800 的
                if not fallback:
                    for _r in common_details:
                        try:
                            _p = _local_image(_r)
                            if not _p: continue
                            with _Img.open(_io.BytesIO(_p[1])) as _im:
                                _w, _h = _im.size
                                if _w >= 800 and _h == _w:
                                    fallback = _r
                                    break
                        except Exception:
                            continue
                # 实在没有 1:1 的,才用第一张通用详情(大概率会被小红书拒,生产应避免)
                if not fallback and common_details:
                    fallback = common_details[0]
                if fallback:
                    row["main_images"] = [fallback]


def group_products(rows):
    """将标准模板的 SKU 行合并为商品，供预览和后续平台发布使用。"""
    grouped = {}
    for row in rows:
        code = row.get("product_code") or f"__line_{row.get('line')}"
        product = grouped.setdefault(code, {
            "product_code": code, "title": row.get("title", ""),
            "wechat_title": row.get("wechat_title", ""), "xhs_title": row.get("xhs_title", ""),
            "internal_category": row.get("internal_category", ""), "brand": row.get("brand", ""),
            "description": row.get("description", ""), "attributes": row.get("attributes", {}),
            "weight": row.get("weight", ""),
            "main_images": row.get("main_images", []), "detail_images": row.get("detail_images", []),
            "skus": [], "errors": [], "warnings": [], "lines": [],
        })
        product["lines"].append(row.get("line"))
        product["errors"].extend(row.get("errors", []))
        for image_field in ("main_images", "detail_images"):
            for image_source in row.get(image_field, []):
                if image_source and image_source not in product[image_field]:
                    product[image_field].append(image_source)
        # 逐行留存商品属性，用于后续一致性检测（商品属性是 SPU 级，只采用第一行）
        product.setdefault("_attr_variants", []).append(row.get("attributes") or {})
        sku_key = row.get("sku_code")
        if any(sku.get("sku_code") == sku_key for sku in product["skus"]):
            product["errors"].append("同一商品内 SKU 编码重复")
        # 兼容旧批次 rows_json（早期为 spec1_name/spec1_value/spec2_*），统一转成 specs 数组
        row_specs = row.get("specs")
        if row_specs is None:
            row_specs = []
            for i in (1, 2):
                nm = row.get(f"spec{i}_name", "")
                vv = row.get(f"spec{i}_value", "")
                if nm or vv:
                    row_specs.append({"name": nm, "value": vv})
        product["skus"].append({
            "sku_code": sku_key,
            "specs": [{"name": sp.get("name", ""), "value": sp.get("value", "")} for sp in row_specs],
            "original_price": row.get("original_price", ""),
            "price": row.get("price", ""), "stock": row.get("stock", ""),
            # SKU 规格图(图片直读扫描写入, 固定为该 SKU 的「主图2」)
            "sku_image": row.get("sku_image", ""),
        })
    products = list(grouped.values())
    for product in products:
        product["errors"] = list(dict.fromkeys(product["errors"]))
        # 商品属性在同一商品的各 SKU 行间不一致时告警：
        # 平台模型只支持 SPU 级属性（微信 attrs / 小红书 attributes），
        # 实际仅第一行的值生效，其余行静默丢弃，必须提醒运营。
        variants = product.pop("_attr_variants", [])
        if len(variants) > 1:
            keys = set()
            for variant in variants:
                keys.update(variant.keys())
            for key in sorted(keys):
                values = [str(variant.get(key, "")).strip() for variant in variants]
                distinct = list(dict.fromkeys([v for v in values if v]))
                if len(distinct) > 1:
                    product["warnings"].append(
                        f"商品属性「{key}」在 SKU 间不一致（{' / '.join(distinct)}），"
                        f"当前仅采用第一行的值；若该差异影响选购或定价，建议提升为规格维度"
                    )
        product["warnings"] = list(dict.fromkeys(product.get("warnings", [])))
        # 汇总规格维度（按名称去重、保持列序），并把每个 SKU 的 specs 对齐到统一维度，
        # 避免各 SKU 规格列数量不同导致前端按下标渲染错位。
        dimensions = []
        for sku in product["skus"]:
            for sp in sku.get("specs", []):
                if sp.get("name") and sp["name"] not in dimensions:
                    dimensions.append(sp["name"])
        for sku in product["skus"]:
            by_name = {sp.get("name"): sp.get("value", "") for sp in sku.get("specs", []) if sp.get("name")}
            sku["specs"] = [{"name": d, "value": by_name.get(d, "")} for d in dimensions]
        product["spec_dimensions"] = dimensions
        if len(product["skus"]) > 100:
            product["errors"].append("单商品最多支持100个 SKU")
        titles = {rows[line - 2].get("title") for line in product["lines"] if isinstance(line, int) and 1 < line <= len(rows) + 1}
        if len(titles) > 1:
            product["errors"].append("同一商品标题不一致")
        prices = [float(s["price"]) for s in product["skus"] if str(s.get("price", "")).replace(".", "", 1).isdigit()]
        stocks = [int(float(s["stock"])) for s in product["skus"] if str(s.get("stock", "")).replace(".", "", 1).isdigit()]
        product["sku_count"] = len(product["skus"])
        product["price_min"] = min(prices) if prices else None
        product["price_max"] = max(prices) if prices else None
        product["stock_total"] = sum(stocks) if stocks else None
    return products


@app.get("/health")
def health():
    return {"status": "ok", "service": "bulk-catalog-api"}


@app.get("/shops")
def list_shops():
    """返回脱敏后的店铺列表，供前端店铺选择器使用。"""
    return {"ok": True, "result": shop_registry.redacted_list(only_enabled=True)}


@app.get("/template")
def template():
    headers = ["商品编码", "标题", "微信标题", "小红书标题", "内部类目", "品牌", "描述", "商品属性", "重量", "主图", "详情图", "SKU编码", "规格1名称", "规格1值", "规格2名称", "规格2值", "规格3名称", "规格3值", "原价（元）", "售价（元）", "库存"]
    try:
        import openpyxl
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "商品SKU"
        sheet.append(headers)
        sheet.append(["SPU-001", "示例商品标题", "", "", "珠宝", "", "", "材质=18K金;颜色=白色", 500, "", "", "SKU-001", "颜色", "红色", "尺码", "均码", "净度", "VS1", 99.9, 88.0, 100])
        for cell in sheet[1]:
            cell.font = openpyxl.styles.Font(bold=True)
        sheet.freeze_panes = "A2"
        output = io.BytesIO()
        workbook.save(output)
        output.seek(0)
        return StreamingResponse(iter([output.read()]), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=bulk-product-template.xlsx"})
    except Exception as exc:
        raise HTTPException(500, f"生成 Excel 模板失败：{exc}")


@app.post("/import")
def import_batch(body: ImportBody, request: Request):
    shop_id = _current_shop_id(request)
    _verify_shop(shop_id)
    operator = _current_operator(request)
    try:
        raw = base64.b64decode(body.content_base64)
    except Exception:
        raise HTTPException(400, "Excel 文件编码无效")
    batch_id = uuid.uuid4().hex
    rows = parse_excel(raw, body.filename, batch_id)
    if len(rows) > 5000:
        raise HTTPException(400, "单批最多导入5000行SKU")
    if body.zip_base64:
        try:
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(body.zip_base64))) as archive:
                for info in archive.infolist():
                    if info.is_dir() or info.filename.startswith(("/", "\\")) or ".." in info.filename:
                        continue
                    target = os.path.join(IMAGE_DIR, f"{uuid.uuid4().hex}_{os.path.basename(info.filename)}")
                    with open(target, "wb") as output:
                        output.write(archive.read(info))
        except Exception as exc:
            raise HTTPException(400, f"图片ZIP处理失败：{exc}")
    if body.folder_files or body.common_detail_files:
        _store_folder_images(body.folder_files, batch_id, rows, body.common_detail_files)
    errors = sum(bool(row["errors"]) for row in rows)
    conn = db()
    conn.execute("INSERT INTO batches(id,shop_id,filename,status,total,valid,errors,created_at,operator,rows_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (batch_id, shop_id, body.filename, "待校验" if errors else "待发布", len(rows), len(rows) - errors, errors, now(), operator, json.dumps(rows, ensure_ascii=False)))
    conn.commit(); conn.close()
    # 浏览器上传的 Excel 在服务器上没有原文件, 记一条空路径(回填ID到货盘表时会提示不支持)
    after_import_housekeeping(batch_id, "", body.filename)
    products = group_products(rows)
    product_errors = sum(bool(product["errors"]) for product in products)
    return {"ok": True, "batch_id": batch_id, "total": len(rows), "product_count": len(products), "valid": len(rows) - errors, "errors": errors, "product_errors": product_errors}


def _huopai_module():
    """货盘转换模块(局部导入:只有货盘相关接口需要 openpyxl 与转换规则)。"""
    try:
        import huopai_adapter as module
    except Exception as exc:
        raise HTTPException(500, f"货盘转换模块不可用：{exc}")
    return module


def _huopai_dir():
    """货盘表目录白名单根: HUOPAI_DIR 优先, 否则取默认货盘表所在目录。"""
    directory = os.environ.get("HUOPAI_DIR") or os.path.dirname(_huopai_module().HUOPAI_PATH)
    return os.path.abspath(directory)


def _resolve_huopai_file(path):
    """把请求里的货盘表路径收敛到白名单目录内的 .xlsx。

    防止 /import-huopai 被用来读取(并解析)服务器上任意文件: 之前 path 完全来自请求体,
    传 C:\\Windows\\... 也会被当成 Excel 打开。现在只允许货盘目录内的 .xlsx。
    """
    candidate = os.path.abspath(str(path or ""))
    root = _huopai_dir()
    if os.path.splitext(candidate)[1].lower() != ".xlsx":
        raise HTTPException(400, "货盘表只支持 .xlsx 文件")
    try:
        inside = os.path.commonpath((root, candidate)) == root
    except ValueError:
        inside = False
    if not inside:
        raise HTTPException(400, f"货盘表路径不在允许的货盘目录内：{root}")
    if not os.path.isfile(candidate):
        raise HTTPException(400, f"货盘表不存在：{candidate}")
    return candidate


@app.get("/huopai-files")
def list_huopai_files():
    """【货盘文件列表】列出货盘目录下可导入的 .xlsx,供前端下拉选择。

    目录由服务端配置(HUOPAI_DIR / HUOPAI_PATH)决定,接口不接受客户端传目录;
    因此运营侧的货盘表只要放进该目录即可,不再需要把绝对路径写进前端。
    """
    directory = _huopai_dir()
    if not os.path.isdir(directory):
        raise HTTPException(400, f"货盘目录不可访问：{directory}(请配置 HUOPAI_DIR / HUOPAI_PATH)")
    with os.scandir(directory) as scanner:
        entries = sorted(scanner, key=lambda entry: entry.name)
    files = []
    for entry in entries:
        if not entry.is_file() or os.path.splitext(entry.name)[1].lower() != ".xlsx":
            continue
        stat = entry.stat()
        files.append({"name": entry.name, "path": entry.path, "size": stat.st_size,
                      "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()})
    default_path = os.path.abspath(_huopai_module().HUOPAI_PATH)
    return {"ok": True, "dir": directory,
            "default": default_path if os.path.isfile(default_path) else "",
            "files": files}


@app.post("/import-huopai")
def import_huopai(body: HuopaiImportBody | None = None, request: Request = None):
    """【直读货盘】从服务器本地货盘目录读货盘表,后台跑「货盘→标准模板」转换后入库。
    与大平台一致:数据源直连,不经过浏览器上传,规避 64MB 大文件 Base64 传输。
    安全: 只允许读取货盘目录白名单内的 .xlsx(见 _resolve_huopai_file)。
    转换规则与 huopai_adapter.py 完全一致(编码/价格/库存/属性/图片/系列拆分)。"""
    shop_id = _current_shop_id(request) if request else shop_registry.default_shop()["shop_id"]
    _verify_shop(shop_id)
    operator = _current_operator(request) if request else shop_registry.default_operator()
    import openpyxl  # 局部导入:仅本接口需要
    H = _huopai_module()
    path = _resolve_huopai_file((body.path if body and body.path else None) or H.HUOPAI_PATH)
    # ① 转换:货盘 → 标准列 dict(复用 huopai_adapter 的业务规则)
    try:
        images = H.load_images(shop_id)
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            raw_rows = []
            if "培育钻" in wb.sheetnames:
                raw_rows.extend(H.parse_peiyuzuan(wb["培育钻"], images))
            if "天然钻" in wb.sheetnames:
                raw_rows.extend(H.parse_tianranzuan(wb["天然钻"], images))
        finally:
            wb.close()
        H.enrich_carat(raw_rows)
        H.enrich_split_tone(raw_rows)
        H.enrich_split_chain(raw_rows)
        # 标题按商品编码生成, 必须在所有拆分之后(拆出来的商品标题要靠它区分开)
        H.enrich_titles(raw_rows)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"货盘转换失败：{exc}")
    if not raw_rows:
        raise HTTPException(400, "货盘表没解析到商品行(检查 sheet 名是否含「培育钻」「天然钻」)")
    # ② 复用标准导入的归一化 + 批次入库
    batch_id = uuid.uuid4().hex
    rows = [normalize_row(r, i) for i, r in enumerate(raw_rows, 2)
            if any(str(v or "").strip() for v in r.values())]
    if len(rows) > 5000:
        raise HTTPException(400, "单批最多导入5000行SKU")
    errors = sum(bool(row["errors"]) for row in rows)
    filename = os.path.basename(path)
    conn = db()
    conn.execute("INSERT INTO batches(id,shop_id,filename,status,total,valid,errors,created_at,operator,rows_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (batch_id, shop_id, filename, "待校验" if errors else "待发布", len(rows), len(rows) - errors, errors, now(), operator, json.dumps(rows, ensure_ascii=False)))
    conn.commit(); conn.close()
    # 记住货盘表原文件位置: 发布完「回填商品ID」直接改这个文件
    after_import_housekeeping(batch_id, path, filename)
    products = group_products(rows)
    return {"ok": True, "batch_id": batch_id, "filename": filename, "total": len(rows),
            "product_count": len(products), "valid": len(rows) - errors, "errors": errors,
            "product_errors": sum(bool(product["errors"]) for product in products)}


# ==================================================================
# 回填平台商品ID到货盘表原文件
# ==================================================================
# 发布完成后运营要把平台生成的商品ID登记回货盘表(培育钻的「微信小店」「小红书产品id」列),
# 原先只能照着导出的 CSV 一行行抄。这里直接改写货盘表原文件:
#   - 只改需要的单元格: 在 xlsx 内部做字节级替换, 不重新保存整本工作簿 ——
#     货盘表里 WPS 的内嵌图片(DISPIMG 主图)用 openpyxl 重存会丢;
#   - 写前自动备份原文件, 写完自检(重新读回单元格)通过才替换, 失败则原文件不动;
#   - 只填空白单元格: 已有值(人工填过)的行跳过并回报, 不覆盖;
#   - 逐行用导入时记下的商家编码核对, 货盘表被改过/重排过的行不写(宁可不写, 不写错行)。
SOURCE_INFO_DIR = os.path.join(BASE_DIR, "_sources")
BACKUP_DIR = os.path.join(SOURCE_INFO_DIR, "huopai_backups")
# 货盘表备份保留份数(每份 60MB+): 默认 1 份(只兜"刚写错一次"这种场景), 可用环境变量调大
MAX_BACKUPS = int(os.environ.get("BULK_MAX_HUOPAI_BACKUPS", "1"))
# 备份保留天数: 超过就删(份数之外的兜底, 免得旧的 60MB 文件一直躺在磁盘上)
BACKUP_TTL_DAYS = int(os.environ.get("BULK_HUOPAI_BACKUP_TTL_DAYS", "14"))
# 批次来源记录保留天数: 每导入一次记一条, 不清会无限累积(每条约 180 字节, 量小但没必要永久留)。
# 与批次数据(下面 BATCH_TTL_DAYS)对齐: 批次被清掉后来源记录也没用了(回填ID需要批次里的源行信息)。
SOURCE_INFO_TTL_DAYS = int(os.environ.get("BULK_SOURCE_INFO_TTL_DAYS", "14"))

# 货盘表各 sheet 的表头所在行；ID列可能叫的名字（都不匹配就追加到表尾）
HUOPAI_SHEETS = {"培育钻": 2, "天然钻": 1}
# 两种ID各归各位(运营核对时在后台都能直接找到):
#   「小红书产品id」「微信小店」   = 款级商品ID(后台商品列表里的"商品ID", 同款各规格一样, 可搜索)
#   「小红书规格id」「微信规格id」 = 行级规格ID(后台商品规格列表里的"规格ID", 一行一个)
# 以前只填一种: 只填商品ID则每行都一样、定位不到具体规格; 只填规格ID则跟后台商品ID对不上,
# 运营拿这个值在后台搜不到商品。所以两种都填, 缺哪列就追加哪列。
# 三张表都从 platform_registry 派生。
# ⚠️ 键顺序 = _huopai_id_columns 追加新列的顺序，必须保持「款级在前、规格级在后」；
#    所以这里分两轮构建（先所有平台的款级键，再所有平台的规格级键），不要合并成一轮。
HUOPAI_ID_HEADERS = {
    p: tuple(platform_registry.adapter(p)["id_headers"])
    for p in platform_registry.all_platforms()
}
HUOPAI_ID_HEADERS.update({
    f"{p}_sku": tuple(platform_registry.adapter(p)["sku_id_headers"])
    for p in platform_registry.all_platforms()
})
HUOPAI_PLATFORM_LABEL = {key: names[0] for key, names in HUOPAI_ID_HEADERS.items()}
HUOPAI_FIELD_PLATFORM = {key: key[:-4] if key.endswith("_sku") else key for key in HUOPAI_ID_HEADERS}
# 写入的ID列统一拉到这个宽度: 原表这些列宽只有 6 个字符, 24 位ID会溢出显示到相邻列,
# 看着像"占用了别的平台ID的位置"
HUOPAI_ID_COLUMN_WIDTH = 24


def _source_info_path(batch_id):
    return os.path.join(SOURCE_INFO_DIR, f"{batch_id}.json")


def prune_source_info(days=SOURCE_INFO_TTL_DAYS):
    """清掉过期的批次来源记录, 避免 _sources 目录无限累积。

    只删 .json(不动 huopai_backups 里的备份, 备份由 _backup_source 按 MAX_BACKUPS 控制);
    删早了也只是那批不能再回填ID, 发布数据/批次本身都不受影响。
    """
    if not os.path.isdir(SOURCE_INFO_DIR):
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for name in os.listdir(SOURCE_INFO_DIR):
        if not name.endswith(".json"):
            continue
        target = os.path.join(SOURCE_INFO_DIR, name)
        try:
            if os.path.getmtime(target) < cutoff:
                os.remove(target)
                removed += 1
        except OSError:
            pass
    return removed


def save_source_info(batch_id, path, filename):
    """记下批次的来源文件(只有货盘直读才有 path), 供「回填ID到货盘表」定位原文件。"""
    try:
        os.makedirs(SOURCE_INFO_DIR, exist_ok=True)
        with open(_source_info_path(batch_id), "w", encoding="utf-8") as handle:
            json.dump({"path": os.path.abspath(path) if path else "", "filename": filename or ""},
                      handle, ensure_ascii=False)
    except Exception as exc:      # 记不下来不影响导入
        print(f"[source-info] 记录来源失败: {exc}")


def prune_old_batches(days=BATCH_TTL_DAYS):
    """删掉 N 天前的批次(含它们最大的 rows/mappings), 返回 (批次数, 释放字节数)。

    安全性:
      - 还有 queued/running 发布项的批次绝不删(worker 正在读它的行数据发品);
      - 发布记录(publish_items/publish_jobs)不动 —— 它是"哪些商品已经发过"的依据;
      - 影响: 更早的批次在界面上打不开(404)、也不能再回填ID, 需要时重新导入货盘表即可。
    """
    if days <= 0:
        return 0, 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = db()
    try:
        rows = conn.execute(
            "SELECT id, COALESCE(LENGTH(rows_json),0)+COALESCE(LENGTH(mappings_json),0) size FROM batches "
            "WHERE created_at < ? AND id NOT IN "
            "(SELECT DISTINCT batch_id FROM publish_items WHERE status IN ('queued','running'))",
            (cutoff,)).fetchall()
        if not rows:
            return 0, 0
        freed = sum(row["size"] for row in rows)
        conn.executemany("DELETE FROM batches WHERE id=?", [(row["id"],) for row in rows])
        conn.commit()
    finally:
        conn.close()
    for row in rows:                      # 批次没了, 来源记录也一起收掉
        try:
            os.remove(_source_info_path(row["id"]))
        except OSError:
            pass
    return len(rows), freed


def prune_old_backups(days=BACKUP_TTL_DAYS):
    """删掉超过保留期(或超出份数)的货盘表备份, 返回 (删除数, 释放字节数)。"""
    if not os.path.isdir(BACKUP_DIR):
        return 0, 0
    cutoff = time.time() - days * 86400
    entries = []
    for name in os.listdir(BACKUP_DIR):
        target = os.path.join(BACKUP_DIR, name)
        if os.path.isfile(target):
            entries.append((os.path.getmtime(target), target))
    entries.sort(reverse=True)
    removed = freed = 0
    for index, (stamp, target) in enumerate(entries):
        if index < MAX_BACKUPS and stamp >= cutoff:
            continue                       # 保留: 份数以内且没过期
        try:
            size = os.path.getsize(target)
            os.remove(target)
            removed += 1
            freed += size
        except OSError:
            pass
    return removed, freed


def maintain_database():
    """每天做一次库维护: 清过期批次 + 回收空间(VACUUM)。

    没有定时任务, 就用"导入时顺手跑、每天最多一次"的方式:
      - VACUUM 会重写整个库文件, 必须没有并发写入; 中台/worker 在跑时可能拿不到锁,
        拿不到就跳过(下次再说), 绝不让它影响导入本身。
    """
    conn = db()
    try:
        last = _meta_get(conn, "last_db_maintenance", "")
        today = datetime.now(timezone.utc).date().isoformat()
        if last == today:
            return
    finally:
        conn.close()
    removed = 0
    try:
        removed, freed = prune_old_batches()
        if removed:
            print(f"[db] 已清理 {removed} 个过期批次（>{BATCH_TTL_DAYS} 天，约 {freed / 1024 / 1024:.1f} MB）")
    except Exception as exc:
        print(f"[db] 清理过期批次失败: {exc}")
    # 订单明细只保留近期：导出与页面用它，但一年前的逐笔订单没人会再翻，
    # 不清的话这张表会随着时间一直涨（每天几笔就是一年十几万行）。
    try:
        conn = db()
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=ORDER_ITEMS_KEEP_DAYS)).date().isoformat()
            dropped = conn.execute("DELETE FROM order_items WHERE stat_date < ?", (cutoff,)).rowcount
            conn.commit()
        finally:
            conn.close()
        if dropped and dropped > 0:
            print(f"[db] 已清理 {dropped} 行过期订单明细（>{ORDER_ITEMS_KEEP_DAYS} 天）")
    except Exception as exc:
        print(f"[db] 清理过期订单明细失败: {exc}")
    conn = db()
    try:
        free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        _meta_set(conn, "last_db_maintenance", datetime.now(timezone.utc).date().isoformat())
        conn.commit()
        # 刚删过批次(产生了空闲页) 或空闲超过 8MB 才重写整个库 —— 白跑一次要几秒 + 临时翻倍磁盘
        if removed or free_pages * page_size > 8 * 1024 * 1024:
            before = os.path.getsize(DB_FILE)
            conn.execute("VACUUM")
            print(f"[db] VACUUM 完成: {before / 1024 / 1024:.1f} MB → {os.path.getsize(DB_FILE) / 1024 / 1024:.1f} MB")
    except sqlite3.OperationalError as exc:               # 有人在写库, 这次不做
        print(f"[db] VACUUM 跳过（库正被占用）: {exc}")
    except Exception as exc:
        print(f"[db] VACUUM 失败: {exc}")
    finally:
        conn.close()


def after_import_housekeeping(batch_id, path, filename):
    """导入收尾: 记来源 + 清理过期运行时文件。

    没有定时任务, 就用"每次导入顺手清一遍"的方式, 避免这些目录/数据无限累积:
      - _sources/<批次ID>.json  批次来源记录(默认留 14 天, 与批次数据对齐)
      - _sources/huopai_backups 货盘表备份(默认只留 1 份, 且最多 14 天)
      - images/_share/*         共享盘图片懒拷贝(默认留 30 天, 可重取)
      - 数据库里过期批次的 rows/mappings(默认留 14 天) + 每天一次 VACUUM
    """
    save_source_info(batch_id, path, filename)
    try:
        removed = prune_source_info()
        if removed:
            print(f"[source-info] 已清理 {removed} 条过期批次来源记录（>{SOURCE_INFO_TTL_DAYS} 天）")
    except Exception as exc:
        print(f"[source-info] 清理过期记录失败: {exc}")
    try:
        removed, freed = prune_old_images()
        if removed:
            print(f"[image-cache] 已清理 {removed} 个过期图片懒拷贝（{freed / 1024 / 1024:.1f} MB，>{IMAGE_TTL_DAYS} 天）")
    except Exception as exc:
        print(f"[image-cache] 清理过期图片失败: {exc}")
    try:
        removed, freed = prune_old_backups()
        if removed:
            print(f"[backup] 已清理 {removed} 个过期货盘表备份（{freed / 1024 / 1024:.1f} MB）")
    except Exception as exc:
        print(f"[backup] 清理过期备份失败: {exc}")
    try:
        maintain_database()
    except Exception as exc:
        print(f"[db] 维护失败: {exc}")


def load_source_info(batch_id):
    path = _source_info_path(batch_id)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle) or {}
    except Exception:
        return {}


def _col_index(letter):
    index = 0
    for char in str(letter).upper():
        if "A" <= char <= "Z":
            index = index * 26 + (ord(char) - 64)
    return index


def _col_letter(index):
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _xml_text(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _patch_row_cells(row_xml, row_number, cells):
    """在一行的 XML 里设置若干单元格: 已有就换内容(保留原样式), 没有就按列序插进去。"""
    for letter in sorted(cells, key=_col_index):
        target = f"{letter}{row_number}"
        cell_re = re.compile(r'<c\b[^>]*\br="%s"[^>]*(?:/>|>.*?</c>)' % re.escape(target), re.S)
        found = cell_re.search(row_xml)
        style = ""
        if found:
            style_match = re.search(r'\bs="[^"]*"', found.group(0))
            style = f" {style_match.group(0)}" if style_match else ""
        new_cell = f'<c r="{target}"{style} t="inlineStr"><is><t>{_xml_text(cells[letter])}</t></is></c>'
        if found:
            row_xml = row_xml[:found.start()] + new_cell + row_xml[found.end():]
            continue
        insert_at = row_xml.rindex("</row>") if "</row>" in row_xml else len(row_xml)
        for cell in re.finditer(r'<c\b[^>]*\br="([A-Z]+)\d+"', row_xml):
            if _col_index(cell.group(1)) > _col_index(letter):
                insert_at = cell.start()
                break
        row_xml = row_xml[:insert_at] + new_cell + row_xml[insert_at:]
    # 追加列后把行的 spans(如 spans="1:35") 一起放宽: 有些阅读器(含 openpyxl 只读模式)
    # 会按 spans/dimension 截断, 不改就会出现"写进去了但读出来是空"
    widest = max(_col_index(letter) for letter in cells)
    spans = re.search(r'spans="(\d+):(\d+)"', row_xml)
    if spans and widest > int(spans.group(2)):
        row_xml = row_xml[:spans.start()] + f'spans="{spans.group(1)}:{widest}"' + row_xml[spans.end():]
    return row_xml


def _unhide_columns(sheet_xml, letters, width=HUOPAI_ID_COLUMN_WIDTH):
    """把指定列由隐藏改成显示, 并把列宽拉到放得下 24 位ID。

    货盘表里 D~H 是整段隐藏的(小红书产品id/京东/抖音/微信小店都是隐藏列), 而且宽度只有 6 个字符:
    运营即使取消隐藏, ID 也会因居中溢出显示到相邻列上, 看着像"占用了别的平台ID的位置"。
    所以写入的列统一放开 + 加宽, 其余列(含同一段里其它隐藏列)原样不动。
    <col min="4" max="8" hidden="1"/> 这种区间会被拆开, 只有目标列改属性。
    """
    for letter in sorted(set(letters), key=_col_index):
        index = _col_index(letter)
        target = None
        for match in list(re.finditer(r"<col\b[^>]*/>", sheet_xml)):
            low = re.search(r'min="(\d+)"', match.group(0))
            high = re.search(r'max="(\d+)"', match.group(0))
            if low and high and int(low.group(1)) <= index <= int(high.group(1)):
                target = match
                break
        if target is None:
            # 该列没有列定义(追加在表尾的新列就是这样): 补一条, 保持 min 升序
            definition = f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
            if "</cols>" in sheet_xml:
                following = [(int(m.group(1)), m.start()) for m in re.finditer(r'<col\b[^>]*min="(\d+)"', sheet_xml)]
                later = [position for value, position in following if value > index]
                position = min(later) if later else sheet_xml.rindex("</cols>")
                sheet_xml = sheet_xml[:position] + definition + sheet_xml[position:]
            elif "<sheetData" in sheet_xml:
                sheet_xml = sheet_xml.replace("<sheetData", f"<cols>{definition}</cols><sheetData", 1)
            continue
        attributes = target.group(0)
        low = re.search(r'min="(\d+)"', attributes)
        high = re.search(r'max="(\d+)"', attributes)
        first, last = int(low.group(1)), int(high.group(1))
        body = attributes[len("<col"):-2]
        shown = re.sub(r'\s*(min="\d+"|max="\d+"|hidden="1"|width="[^"]*"|customWidth="[^"]*")', "", body)
        shown += f' width="{width}" customWidth="1"'
        still_hidden = re.sub(r'\s*(min="\d+"|max="\d+")', "", body)      # 其余列: 属性照旧
        pieces = []
        if first < index:
            pieces.append(f'<col min="{first}" max="{index - 1}"{still_hidden}/>')
        pieces.append(f'<col min="{index}" max="{index}"{shown}/>')
        if last > index:
            pieces.append(f'<col min="{index + 1}" max="{last}"{still_hidden}/>')
        sheet_xml = sheet_xml[:target.start()] + "".join(pieces) + sheet_xml[target.end():]
    return sheet_xml


def _patch_sheet_xml(sheet_xml, updates):
    """updates = {行号: {列字母: 值}}；只替换这些行, 其它内容(图片、样式)原样保留。"""
    for row_number in sorted(updates):
        row_re = re.compile(r'<row\b[^>]*\br="%d"[^>]*>.*?</row>' % row_number, re.S)
        match = row_re.search(sheet_xml)
        if not match:
            continue
        patched = _patch_row_cells(match.group(0), row_number, updates[row_number])
        sheet_xml = sheet_xml[:match.start()] + patched + sheet_xml[match.end():]
    # 写入的列如果原本是隐藏的, 一并显示出来, 否则运营看不到回填结果
    sheet_xml = _unhide_columns(sheet_xml, (letter for cells in updates.values() for letter in cells))
    # 新加的列要同步进 <dimension>, 否则阅读器按旧范围截断
    widest = max(_col_index(letter) for cells in updates.values() for letter in cells)
    dimension = re.search(r'<dimension ref="([^"]*)"\s*/>', sheet_xml)
    if dimension:
        parts = dimension.group(1).split(":")
        end = parts[-1]
        end_column = re.match(r"([A-Z]+)(\d*)", end)
        if end_column and _col_index(end_column.group(1)) < widest:
            end = f"{_col_letter(widest)}{end_column.group(2)}"
            ref = f"{parts[0]}:{end}" if len(parts) > 1 else end
            sheet_xml = sheet_xml[:dimension.start()] + f'<dimension ref="{ref}"/>' + sheet_xml[dimension.end():]
    return sheet_xml


def _sheet_xml_targets(archive):
    """sheet 名 → zip 内的 XML 路径（经 workbook.xml + rels 解析, 不靠顺序猜）。"""
    workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
    rels_xml = archive.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    rels = dict(re.findall(r'Id="([^"]+)"[^>]*?Target="([^"]+)"', rels_xml))
    targets = {}
    for attributes in re.findall(r'<sheet\b([^>]*?)/?>', workbook_xml):
        name = re.search(r'name="([^"]*)"', attributes)
        rid = re.search(r'r:id="([^"]*)"', attributes)
        if not name or not rid:
            continue
        target = rels.get(rid.group(1), "")
        if not target:
            continue
        target = target[1:] if target.startswith("/") else target
        targets[name.group(1)] = target if target.startswith("xl/") else f"xl/{target}"
    return targets


def _write_workbook(source_path, target_path, updates_by_sheet):
    """把各 sheet 的单元格更新写进 xlsx: 逐条拷贝 zip 内容, 只替换目标 sheet 的 XML。

    ⚠️ 不能用 openpyxl 重新保存整本工作簿 —— 货盘表里的 WPS 内嵌图片(DISPIMG)会被丢掉。
    """
    with zipfile.ZipFile(source_path) as archive:
        targets = _sheet_xml_targets(archive)
        xml_paths = {targets[name]: updates for name, updates in updates_by_sheet.items() if name in targets}
        with zipfile.ZipFile(target_path, "w", zipfile.ZIP_DEFLATED) as output:
            for item in archive.infolist():
                data = archive.read(item.filename)
                if item.filename in xml_paths:
                    data = _patch_sheet_xml(data.decode("utf-8"), xml_paths[item.filename]).encode("utf-8")
                output.writestr(item, data)


def _backup_source(path):
    """备份原货盘表, 只保留最近几份(货盘表 60MB+, 不能无限留)。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem, extension = os.path.splitext(os.path.basename(path))
    backup = os.path.join(BACKUP_DIR, f"{stem}-{stamp}{extension}")
    shutil.copy2(path, backup)
    backups = sorted(
        (os.path.join(BACKUP_DIR, name) for name in os.listdir(BACKUP_DIR) if name.endswith(extension)),
        key=os.path.getmtime, reverse=True,
    )
    for stale in backups[MAX_BACKUPS:]:
        try:
            os.remove(stale)
        except OSError:
            pass
    return backup


def _verify_written(path, sheet_name, updates):
    """重新读回写入的单元格, 确认真的写进去了(避免写坏 60MB 的货盘表还替换原文件)。"""
    import openpyxl
    lines = sorted(line for line in updates if line > 1)
    if not lines:
        return ""
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook[sheet_name]
        for index, values in enumerate(sheet.iter_rows(min_row=1, max_row=lines[-1], values_only=True), start=1):
            if index not in updates:
                continue
            for letter, value in updates[index].items():
                position = _col_index(letter) - 1
                actual = str(values[position] or "").strip() if position < len(values) else ""
                if actual != str(value).strip():
                    return f"{sheet_name}!{letter}{index} 期望 {value} 实际 {actual}"
    finally:
        workbook.close()
    return ""


def _huopai_id_columns(sheet_name, sheet):
    """(表头行, 表头内容, {字段: [列字母...]}, 需要追加的表头{字段: 名称})。

    字段 4 个: wechat/xhs = 款级商品ID列, wechat_sku/xhs_sku = 行级规格ID列。
    同一字段命中多列就都写(避免运营看的那一列还是旧值); 一列都没命中才追加新列。
    """
    header_row = HUOPAI_SHEETS[sheet_name]
    headers = []
    for row in sheet.iter_rows(min_row=header_row, max_row=header_row, values_only=True):
        headers = [str(cell or "").strip() for cell in row]
    normalized = [re.sub(r"[\s()（）]+", "", name).lower() for name in headers]
    next_index = max(sheet.max_column or 0, len(headers))
    columns, appended = {}, {}
    for field, names in HUOPAI_ID_HEADERS.items():
        wanted = {re.sub(r"[\s()（）]+", "", name).lower() for name in names}
        indexes = [i for i, name in enumerate(normalized) if name in wanted]
        if not indexes:
            next_index += 1
            indexes = [next_index - 1]
            appended[field] = HUOPAI_PLATFORM_LABEL[field]
        columns[field] = [_col_letter(i + 1) for i in indexes]
    return header_row, headers, columns, appended


def _sku_key(value):
    return re.sub(r"\s+", "", str(value or "")).upper()


def _looks_like_platform_id(value):
    """判断单元格里是不是平台ID(而不是人工写的备注): 纯数字/字母数字且够长。"""
    text = str(value or "").strip()
    return len(text) >= 10 and re.fullmatch(r"[A-Za-z0-9_-]+", text) is not None


def build_huopai_updates(source_path, rows, ids, sku_ids=None):
    """算出要写进货盘表的单元格: ({sheet: {行: {列: 值}}}, 统计)。

    - 款级「产品id」(ids, 来自发布记录): 写到「小红书产品id」「微信小店」列 —— 同款各规格一样,
      但它是后台商品列表里的"商品ID", 直接能搜到商品;
    - 行级「规格id」(sku_ids, 现查平台商品详情): 写到「小红书规格id」「微信规格id」列 —— 一行一个,
      对应后台商品规格列表里的"规格ID"。

    单元格取值规则:
      - 空 → 写新值；
      - 已有值等于新值 → 不动, 计入 existing；
      - 已有值是别的平台ID(之前填错的产品id/规格id) → 覆盖, 计入 overwritten；
      - 已有值是人工写的备注(中文等, 不像ID) → 不动, 计入 existing。
    导入后货盘表被改过(商家编码对不上)的行直接跳过, 宁可不写也不写错行。
    """
    import openpyxl
    sku_ids = sku_ids or {}
    updates, stats = {}, {"filled": 0, "overwritten": 0, "existing": 0, "mismatch": 0, "sku_missing": 0,
                          "filled_product": 0, "filled_sku": 0, "sheets": {}}
    workbook = openpyxl.load_workbook(source_path, read_only=True, data_only=True)
    try:
        for sheet_name in HUOPAI_SHEETS:
            if sheet_name not in workbook.sheetnames:
                continue
            sheet = workbook[sheet_name]
            header_row, headers, columns, appended = _huopai_id_columns(sheet_name, sheet)
            code_index = next((i for i, name in enumerate(headers) if "商家编码" in name), None)
            wanted = {}
            for row in rows:
                if str(row.get("_源表") or "") != sheet_name:
                    continue
                try:
                    line = int(row.get("_源行") or 0)
                except (TypeError, ValueError):
                    continue
                if line <= header_row:
                    continue
                cells = wanted.setdefault(line, {})
                for field, letters in columns.items():
                    platform = HUOPAI_FIELD_PLATFORM[field]
                    if field.endswith("_sku"):
                        # 规格id按"这一行的商家编码"取: 优先用发品时真正提交的 SKU编码,
                        # 再退回原表里的原始编码(清洗过的编码靠它兜底)
                        variants = sku_ids.get(platform, {}).get(row.get("product_code"), {})
                        value = None
                        for key in (row.get("sku_code"), row.get("_源码")):
                            value = variants.get(_sku_key(key))
                            if value:
                                break
                        if not value:
                            if (ids.get(platform) or {}).get(row.get("product_code")):
                                stats["sku_missing"] += 1
                            continue
                    else:
                        value = (ids.get(platform) or {}).get(row.get("product_code"))
                        if not value:
                            continue
                    for letter in letters:
                        cells[letter] = str(value)
                        cells.setdefault("__kind", {})[letter] = field
                if cells:
                    cells["__code"] = str(row.get("_源码") or "")
            if appended:
                header_cells = wanted.setdefault(header_row, {})
                for field, label in appended.items():
                    header_cells[columns[field][0]] = label
            if not wanted:
                continue
            last_line = max(wanted)
            sheet_updates = {}
            for index, values in enumerate(sheet.iter_rows(min_row=1, max_row=last_line, values_only=True), start=1):
                cells = wanted.get(index)
                if not cells:
                    continue
                kind = cells.pop("__kind", {})
                if index == header_row:
                    sheet_updates[index] = dict(cells)
                    continue
                record = str(cells.pop("__code", "") or "")
                actual_code = str(values[code_index] or "").strip() if code_index is not None and code_index < len(values) else ""
                if record and actual_code != record:
                    stats["mismatch"] += 1
                    continue
                for letter, value in list(cells.items()):
                    position = _col_index(letter) - 1
                    current = str(values[position] or "").strip() if position < len(values) else ""
                    if current == str(value).strip():
                        stats["existing"] += 1
                        cells.pop(letter)
                        continue
                    if current and not _looks_like_platform_id(current):
                        stats["existing"] += 1        # 人工写的备注, 绝不动
                        cells.pop(letter)
                        continue
                    stats["overwritten" if current else "filled"] += 1
                    if kind.get(letter, "").endswith("_sku"):
                        stats["filled_sku"] += 1
                    else:
                        stats["filled_product"] += 1
                if cells:
                    sheet_updates[index] = cells
            if sheet_updates:
                updates[sheet_name] = sheet_updates
                stats["sheets"][sheet_name] = {
                    "rows": len([line for line in sheet_updates if line != header_row]),
                    "columns": {field: "、".join(letters) for field, letters in columns.items()},
                    "appended": appended,
                }
    finally:
        workbook.close()
    return updates, stats


def latest_platform_ids(shop_id=None, mode="live"):
    """商品编码 → {平台: 平台商品ID}，取最近一次发布成功的记录（默认**只算 live**）。

    为什么不只看"当前任务": 运营可能分几次发(分批发布/换批次重新导入/重试),
    货盘表里要登记的是"这个商品在平台上是几号", 所以按商品取最近一次成功记录 ——
    与「发布状态」页「已发布/未发布」的口径一致(它也是按商品而不是按批次汇总)。
    同一商品同一平台存在多个不同ID(重发过、或跨店发布到两家店)时: 优先当前店, 仍不唯一则
    记进 ambiguous, 由前端提示运营确认(仍写入最近一次, 避免整列空着)。
    """
    conn = db()
    # 回填货盘 ID 只能用真实发布的 ID：mock 的假 ID 写进货盘表会把整个表污染成"看似已发布"
    where_mode, args = ("", ()) if str(mode) == "all" else (" AND mode=?", (mode,))
    rows = conn.execute(
        "SELECT product_code, platform, platform_product_id, shop_id, finished_at FROM publish_items "
        "WHERE platform_product_id IS NOT NULL AND platform_product_id<>'' "
        "AND status IN ('success','partial')" + where_mode + " ORDER BY finished_at", args
    ).fetchall()
    conn.close()
    candidates = {}
    for row in rows:
        key = (row["platform"], row["product_code"])
        bucket = candidates.setdefault(key, [])
        if not any(item["id"] == str(row["platform_product_id"]) for item in bucket):
            bucket.append({"id": str(row["platform_product_id"]), "shop": row["shop_id"], "at": row["finished_at"] or ""})
    ids, shops, ambiguous = {}, {}, {}
    for (platform, code), bucket in candidates.items():
        same_shop = [item for item in bucket if item["shop"] == shop_id]
        pool = same_shop or bucket
        chosen = sorted(pool, key=lambda item: item["at"])[-1]
        ids.setdefault(platform, {})[code] = chosen["id"]
        # 记下这个ID是哪家店发的: 查规格ID必须用同一家店(跨店发布时 A 店查不到 B 店的商品)
        shops.setdefault(platform, {})[code] = chosen["shop"]
        if len({item["id"] for item in pool}) > 1:
            ambiguous.setdefault(platform, []).append(code)
    return ids, shops, ambiguous


def platform_sku_ids(platform, product_id, shop_id=None):
    """查平台商品详情, 拿「商家编码 → 规格(SKU)ID」对应关系。

    货盘表一行 = 一个 SKU(商家编码), 但平台给的商品ID是"款"级的、同款各规格完全一样,
    所以行级要填的规格ID只能回平台查:
      微信   GET /products/{pid}   → product.skus[].{sku_id, sku_code}
      小红书 GET /items/{item_id}  → result.skuInfos[].{id, erpCode}
    路径来自 platform_registry.item_url；**响应的解析必须按平台分别写**（字段名各家不同），
    新平台不补分支就直接报错 —— 不做"默认按某家解析"的兜底，那会把别家字段当成自己的。
    查不到就抛异常(由调用方记进"查不到"清单, 不影响产品ID的回填)。
    """
    if not platform_registry.is_platform(platform):
        raise RuntimeError(f"不支持的平台：{platform}")
    data = _http_get_json(platform_registry.item_url(platform, product_id), timeout=60, shop_id=shop_id)
    mapping = {}
    if platform == "wechat":
        product = (data.get("result") or {}).get("product") or {}
        for sku in product.get("skus") or []:
            sku_id = str(sku.get("sku_id") or "").strip()
            for key in ("out_sku_id", "sku_code"):
                code = _sku_key(sku.get(key))
                if code and sku_id:
                    mapping.setdefault(code, sku_id)
    elif platform == "xhs":
        if data.get("ok") is False:
            raise RuntimeError(str(data.get("detail") or data)[:200])
        for sku in (data.get("result") or {}).get("skuInfos") or []:
            code = _sku_key(sku.get("erpCode"))
            sku_id = str(sku.get("id") or sku.get("skuId") or "").strip()
            if code and sku_id:
                mapping[code] = sku_id
    else:
        raise RuntimeError(f"平台 {platform} 的规格ID解析尚未实现（按接口清单确认响应字段后补分支）")
    return mapping


class WritebackBody(BaseModel):
    dry_run: bool = False


@app.post("/jobs/{job_id}/writeback-huopai")
def writeback_huopai(job_id: str, body: WritebackBody | None = None, request: Request = None):
    """【回填ID】把平台商品ID/规格ID写进货盘表原文件, 两种各归各位。

    - 「小红书产品id」「微信小店」= 款级商品ID(后台商品列表里的"商品ID", 直接能搜到商品);
    - 「小红书规格id」「微信规格id」= 行级规格ID(后台商品规格列表里的"规格ID", 一行一个);
    缺列会自动追加。以前只填一种: 只填商品ID则每行都一样、定位不到具体规格; 只填规格ID则
    在后台商品列表里搜不到、跟"商品ID"对不上。两种都填, 核对时两边都能对上。
    dry_run=true 只统计将要写哪些行(不落盘), 前端据此弹确认框；
    正式写入前备份原文件, 写完自检通过才替换, 失败则原文件保持不动。
    """
    shop_id = _current_shop_id(request) if request else shop_registry.default_shop()["shop_id"]
    _verify_shop(shop_id)
    dry_run = bool(body.dry_run) if body else False
    conn = db()
    job_row = conn.execute("SELECT * FROM publish_jobs WHERE id=? AND shop_id=?", (job_id, shop_id)).fetchone()
    if not job_row:
        conn.close(); raise HTTPException(404, "发布任务不存在")
    batch = conn.execute("SELECT id, filename, rows_json FROM batches WHERE id=? AND shop_id=?",
                         (job_row["batch_id"], shop_id)).fetchone()
    conn.close()
    if not batch:
        raise HTTPException(404, "批次不存在")
    source = load_source_info(batch["id"])
    source_path = source.get("path") or ""
    if not source_path or not os.path.isfile(source_path):
        raise HTTPException(400, "这批不是从货盘表直读导入的（或原货盘表已被移动），无法回填；请用「导出结果」")
    ids, id_shops, ambiguous = latest_platform_ids(shop_id)
    if not ids:
        raise HTTPException(400, "还没有成功发布过的商品ID，发布成功后再回填")
    try:
        rows = json.loads(batch["rows_json"] or "[]")
    except Exception:
        rows = []
    if not any(row.get("_源表") for row in rows):
        raise HTTPException(400, "这批数据是旧版本导入的（没记录源表行号），请重新导入货盘表后再发布")
    # 规格ID(行级)要现查平台: 只查"这批数据里真的用到、且已发布成功"的商品, 控制请求数
    used_codes = {str(row.get("product_code") or "") for row in rows}
    sku_ids, sku_failed, gone = {}, [], {}
    for platform, mapping in ids.items():
        gone_codes = set()
        for code, product_id in list(mapping.items()):
            if code not in used_codes:
                continue
            try:
                # 必须用"发这个ID的那家店"去查, 否则跨店发布时另一家店查不到该商品(400)
                sku_ids.setdefault(platform, {})[code] = platform_sku_ids(
                    platform, product_id, (id_shops.get(platform) or {}).get(code) or shop_id)
            except urllib.error.HTTPError as exc:
                detail = _http_error_detail(exc)
                markers = platform_registry.adapter(platform)["gone_markers"]
                if any(marker.lower() in detail.lower() for marker in markers):
                    # 平台明确说这个商品不存在了(后台删过): 死ID不写进货盘表, 免得运营照着找
                    gone_codes.add(code)
                    gone.setdefault(platform, []).append(code)
                else:
                    sku_failed.append(f"{code}: {detail[:120]}")
            except Exception as exc:
                sku_failed.append(f"{code}: {str(exc)[:120]}")
        for code in gone_codes:
            mapping.pop(code, None)
            (id_shops.get(platform) or {}).pop(code, None)
    try:
        updates, stats = build_huopai_updates(source_path, rows, ids, sku_ids)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"解析货盘表失败：{exc}")
    result = {
        "path": source_path, "filename": source.get("filename") or batch["filename"],
        "filled": stats["filled"], "existing": stats["existing"], "mismatch": stats["mismatch"],
        "overwritten": stats["overwritten"], "sku_missing": stats["sku_missing"],
        "filled_product": stats["filled_product"], "filled_sku": stats["filled_sku"],
        "sheets": stats["sheets"],
        "platform_ids": {platform: len(mapping) for platform, mapping in ids.items()},
        "ambiguous": {platform: list(codes)[:20] for platform, codes in ambiguous.items()},
        "sku_failed": sku_failed[:20],
        "gone": {platform: list(codes)[:20] for platform, codes in gone.items()},
        "dry_run": dry_run,
    }
    if dry_run:
        return {"ok": True, "result": result}
    if not updates:
        result["message"] = "没有需要写入的单元格（可能都已有值，或商品编码在货盘表里对不上）"
        return {"ok": True, "result": result}
    try:
        backup = _backup_source(source_path)
    except Exception as exc:
        raise HTTPException(500, f"备份货盘表失败（原文件未改动）：{exc}")
    # ⚠️ 临时文件必须留 .xlsx 后缀: 下面自检要用 openpyxl 读它, openpyxl 不认 .tmp 会直接抛异常。
    # 放在原文件同目录(同盘符才能 os.replace 原子替换), 失败路径统一删除。
    stem, extension = os.path.splitext(source_path)
    temp_path = f"{stem}.idbackup-{os.getpid()}{extension or '.xlsx'}"
    try:
        try:
            _write_workbook(source_path, temp_path, updates)
        except Exception as exc:
            raise HTTPException(500, f"写入货盘表失败（原文件未改动）：{exc}")
        problem = ""
        for name, cells in updates.items():
            problem = _verify_written(temp_path, name, cells)
            if problem:
                break
        if problem:
            raise HTTPException(500, f"写入后自检不通过（原文件未改动）：{problem}；备份在 {backup}")
        try:
            os.replace(temp_path, source_path)
        except PermissionError:
            raise HTTPException(409, "货盘表正被占用（可能有人用 Excel/WPS 打开着），请关闭后重试")
        except OSError as exc:
            raise HTTPException(500, f"替换货盘表失败（原文件未改动，新内容在 {temp_path}）：{exc}")
    finally:
        if os.path.exists(temp_path):     # 成功时已被 os.replace 移走, 失败时清掉, 不留垃圾文件
            try:
                os.remove(temp_path)
            except OSError:
                pass
    result["backup"] = backup
    result["message"] = f"已回填 {stats['filled']} 个单元格到 {os.path.basename(source_path)}"
    return {"ok": True, "result": result}


@app.get("/image-roots")
def list_image_roots(request: Request = None):
    """【图片根目录】返回当前店铺允许扫描的图片根目录,供前端下拉选择。

    优先返回店铺配置的 image_root；全局 ALLOWED_IMAGE_ROOTS 兜底。
    """
    shop_id = _current_shop_id(request) if request else shop_registry.default_shop()["shop_id"]
    shop_root = shop_registry.image_root_for(shop_id)
    roots = _allowed_roots_for(shop_id)
    default = shop_root or DEFAULT_IMAGE_ROOT
    return {"ok": True, "default": os.path.abspath(default),
            "roots": [{"path": os.path.abspath(root), "available": os.path.isdir(root)}
                      for root in roots]}


@app.post("/batches/{batch_id}/images/scan")
def scan_batch_images(batch_id: str, body: ImageScanBody | None = None, request: Request = None):
    """【直读图片】扫描图片根目录, 按编码把商品图/SKU 图挂到批次上。
    多店铺: 图片根目录从当前店铺的 image_root 取。

    解决两个实际问题:
      ① 多 SKU 商品的图分散在子文件夹(文件夹名就是 SKU 编码), 浏览器一次只能选一个文件夹,
         结果只读到本色那几张图, 其他规格的图全漏;
      ② 一次要发几百个商品, 走浏览器 Base64 上传要几 GB, 根本不现实。
    服务端直接读网络共享, 233 个商品 4589 张图约 4 秒扫完。

    匹配规则见 image_scanner.py:
      - 商品主图   = 商品文件夹根下的本色图(按 主图1 -> 主图2 -> 白底 -> PNG 排序)
      - SKU 图     = 该 SKU 的「主图2」(运营指定), 落到 row["sku_image"]
      - 通用详情图 = 通用通栏目录, 追加到每个商品的详情图
    扫描阶段只记录共享盘路径(懒拷贝), 不占磁盘; 真正发布到某个商品时,
    才读它的图片字节落盘缓存(内容 sha256 去重), 再上传平台拿素材 URL。

    传 dry_run=true 可只匹配不写库, 先看命中率。
    """
    shop_id = _current_shop_id(request) if request else shop_registry.default_shop()["shop_id"]
    _verify_shop(shop_id)
    try:
        import image_scanner
    except Exception as exc:
        raise HTTPException(500, f"图片扫描模块不可用：{exc}")

    batch = batch_or_404(batch_id, shop_id)
    rows = batch["items"]
    shop_image_root = shop_registry.image_root_for(shop_id)
    root = (body.root if body and body.root else None) or shop_image_root or DEFAULT_IMAGE_ROOT
    months = body.months if body else None
    dry_run = bool(body.dry_run) if body else False
    # 路径收敛: root 只能取白名单内的图片根目录(见 GET /image-roots),
    # 否则该接口可被用来枚举服务器任意文件夹的商品图。
    allowed_roots = _allowed_roots_for(shop_id)
    if not _within_allowed_roots(root, allowed_roots) or not os.path.isdir(root):
        raise HTTPException(400, f"图片根目录不可访问或不在允许的图片目录内：{root}"
                                 f"(允许：{'、'.join(allowed_roots)})")

    try:
        scan = image_scanner.scan_image_root(root, months=months)
    except Exception as exc:
        raise HTTPException(500, f"扫描图片失败：{exc}")

    product_main = {}      # 商品编码 -> [共享盘图片引用]    (多 SKU 行共用, 只解析一次)
    sku_image = {}         # SKU 编码  -> 共享盘图片引用
    errors = []
    unmatched_skus = []
    unmatched_products = []   # 连商品级主图都没挂上的商品编码: 发布时会被平台以"至少需要一张主图"拒

    def localize(path):
        # 懒拷贝: 只记共享盘路径, 真正上传平台时才读字节落盘。
        # 扫描 233 个商品秒级完成、不占磁盘; 只发一部分商品就只读一部分图。
        return f"{SHARE_PREFIX}{path}"

    common_refs = []
    for path in scan["common_images"]:
        try:
            common_refs.append(localize(path))
        except Exception as exc:
            errors.append(f"通用详情图 {os.path.basename(path)}: {exc}")

    products_matched = 0
    skus_matched = 0
    for row in rows:
        code = str(row.get("product_code") or "")
        if code not in product_main:
            base = code.split("-", 1)[0]
            # ① 精确 → ② 去系列标签(如 ZSTZ106XL-星悦 → ZSTZ106XL)
            paths = scan["root_images"].get(code) or scan["root_images"].get(base)
            if not paths:
                # ③ 图库文件夹名可能带中文后缀(如 HZL253福运钻 vs 商品编码 HZL253-如愿),
                #    取最长前缀兜底; 仍匹配不到 = 图库里确实没有这个款
                best_key = ""
                for key in scan["root_images"]:
                    if key and (key.startswith(base) or base.startswith(key)) and len(key) > len(best_key):
                        best_key = key
                paths = scan["root_images"].get(best_key) if best_key else None
            references = []
            for path in (paths or [])[:5]:      # 平台主图最多 5 张
                try:
                    references.append(localize(path))
                except Exception as exc:
                    errors.append(f"{os.path.basename(path)}: {exc}")
            product_main[code] = references
            if references:
                products_matched += 1
            elif code:
                unmatched_products.append(code)
        if product_main[code]:
            row["main_images"] = product_main[code]
        if common_refs:
            row["detail_images"] = list(dict.fromkeys((row.get("detail_images") or []) + common_refs))

        sku_code = str(row.get("sku_code") or "")
        if sku_code not in sku_image:
            hit = image_scanner.match_sku_code(sku_code, scan["sku_images"])
            main2 = ((scan["sku_images"].get(hit) or {}).get("main2") if hit else None)
            if main2:
                try:
                    sku_image[sku_code] = localize(main2)
                except Exception as exc:
                    sku_image[sku_code] = ""
                    errors.append(f"SKU图 {os.path.basename(main2)}: {exc}")
            else:
                sku_image[sku_code] = ""
        if sku_image[sku_code]:
            row["sku_image"] = sku_image[sku_code]
            skus_matched += 1
        else:
            unmatched_skus.append(sku_code or f"line_{row.get('line')}")

    if not dry_run:
        conn = db()
        conn.execute("UPDATE batches SET rows_json=?, revision=revision+1 WHERE id=? AND shop_id=?",
                     (json.dumps(rows, ensure_ascii=False), batch_id, shop_id))
        conn.commit()
        conn.close()

    return {
        "ok": True, "batch_id": batch_id, "dry_run": dry_run,
        "root": root, "months": months,
        "folders": scan["folders"], "scanned_images": scan["scanned"],
        "common_detail_images": len(scan["common_images"]),
        "products_matched": products_matched,
        "products_total": len({r.get("product_code") for r in rows}),
        "skus_matched": skus_matched, "skus_total": len(rows),
        "unmatched_sku_count": len(unmatched_skus),
        "unmatched_skus": unmatched_skus[:200],
        "unmatched_product_count": len(unmatched_products),
        "unmatched_products": unmatched_products[:200],
        "lazy_load": True, "errors": errors[:50],
    }


@app.post("/batches/{batch_id}/sku-image")
def upload_sku_image(batch_id: str, body: SkuImageBody, request: Request):
    """给批次里某个 SKU 补规格图(浏览器上传单张图)。

    落盘到 IMAGE_DIR/{批次id}/{sha256}.{ext}, 返回 local:// 引用;
    前端拿到引用后写回该行 sku_image, 再 PUT /import/{id}/items 持久化。
    引用格式必须与 _local_image 的校验一致, 否则预览/上传素材会失败。
    """
    shop_id = _current_shop_id(request)
    batch_or_404(batch_id, shop_id)
    try:
        content = base64.b64decode(body.content_base64 or "")
    except Exception as exc:
        raise HTTPException(400, f"图片内容解析失败：{exc}")
    if not content:
        raise HTTPException(400, "图片内容为空")
    extension = os.path.splitext(body.filename or "")[1].lower().lstrip(".")
    if extension not in {"jpg", "jpeg", "png", "webp", "gif", "bmp"}:
        extension = "jpg"
    digest = hashlib.sha256(content).hexdigest()
    batch_dir = os.path.join(IMAGE_DIR, batch_id)
    os.makedirs(batch_dir, exist_ok=True)
    path = os.path.join(batch_dir, f"{digest}.{extension}")
    if not os.path.exists(path):
        with open(path, "wb") as image_file:
            image_file.write(content)
    return {"ok": True, "result": {"ref": f"local://{batch_id}/{digest}.{extension}"}}


@app.get("/images/preview")
def preview_image(ref: str, shop_id: str = None, request: Request = None):
    """预览批次里的图片(供浏览器显示纯文本路径无法渲染的图)。

    支持两种引用:
      - local://{批次id}/{hash}.{ext}   浏览器上传或懒拷贝落盘后的本地图
      - disk:{共享盘绝对路径}            图片直读扫描写入的引用
    出于安全只允许读取白名单(店铺 image_root + ALLOWED_IMAGE_ROOTS)下的文件, 否则参数可读任意文件。

    多店铺: 这个接口常被 <img src> 直接调用, 而浏览器自己发的请求**带不了**
    X-Shop-Id 自定义头, 所以这里额外接受 ?shop_id= 查询参数(header 仍可用, 且优先用参数)。
    """
    shop_id = ((shop_id or "").strip()
               or (_current_shop_id(request) if request else shop_registry.default_shop()["shop_id"]))
    allowed_roots = _allowed_roots_for(shop_id)
    source = str(ref or "").strip()
    if source.startswith("local://"):
        found = _local_image(source)
        if not found:
            raise HTTPException(404, "图片不存在")
        content = found[1]
    elif is_share_image(source):
        path = os.path.abspath(source[len(SHARE_PREFIX):])
        # 用 commonpath 判断而非 startswith: 否则 '...\images_evil\x.jpg' 会命中 '...\images'
        if not _within_allowed_roots(path, allowed_roots) or not os.path.isfile(path):
            raise HTTPException(404, "图片不存在或不在允许的图片目录内")
        with open(path, "rb") as image_file:
            content = image_file.read()
    else:
        raise HTTPException(400, "不支持的图片引用")
    if not content:
        raise HTTPException(404, "图片内容为空")
    media = "image/png" if source.lower().endswith(".png") else "image/jpeg"
    return StreamingResponse(io.BytesIO(content), media_type=media)


def batch_or_404(batch_id, shop_id=None):
    """按 ID 取批次。若传了 shop_id，则校验归属；未传则不校验（兼容旧调用）。"""
    conn = db()
    if shop_id:
        row = conn.execute("SELECT * FROM batches WHERE id=? AND shop_id=?", (batch_id, shop_id)).fetchone()
    else:
        row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    conn.close()
    if not row: raise HTTPException(404, "批次不存在")
    data = dict(row); data["items"] = json.loads(data.pop("rows_json")); data["products"] = group_products(data["items"]); data["mappings"] = json.loads(data.pop("mappings_json")); return data


# ------------------------------------------------------------------
# 批次解析缓存:worker 每发一个商品都要拿批次的 rows/mappings,
# 全量 json.loads + group_products 在 800 商品时单次近百毫秒,
# 2N 次(商品×平台)调用就是 O(N²)。这里按 batch_id 缓存解析结果。
#
# 跨进程失效: _invalidate_batch_cache 只清本进程的缓存, 而"改映射"发生在 API
# 进程、"发品"发生在 worker 进程, 只靠它 worker 会一直用旧 mappings
# (实测: 改完运费模板立刻重试, 平台仍报旧模板 id 不存在)。
# 因此缓存里额外记住 batches.revision, 命中后再比一次(单列查询, 亚毫秒),
# revision 变了就回源重解析。TTL 仅作兜底(防止将来某条写路径漏 bump revision)。
# ------------------------------------------------------------------
_BATCH_CACHE: dict = {}
_BATCH_CACHE_TTL = 60.0


def _invalidate_batch_cache(batch_id: str = None) -> None:
    """写接口改完 batches 后调用(只影响本进程);batch_id 为空则全清。"""
    if batch_id:
        _BATCH_CACHE.pop(batch_id, None)
    else:
        _BATCH_CACHE.clear()


def _batch_revision(batch_id: str):
    """读该批次当前 revision(单列查询, 不做 JSON 解析);批次不存在返回 None。"""
    conn = db()
    try:
        row = conn.execute("SELECT revision FROM batches WHERE id=?", (batch_id,)).fetchone()
    finally:
        conn.close()
    return row["revision"] if row else None


def get_batch_cached(batch_id: str) -> dict:
    """命中、未过期、且 revision 没变时复用解析结果,否则回源 batch_or_404 并写缓存。"""
    hit = _BATCH_CACHE.get(batch_id)
    if hit and (time.time() - hit["ts"]) < _BATCH_CACHE_TTL:
        revision = _batch_revision(batch_id)
        if revision is not None and revision == hit["revision"]:
            return hit["data"]
    data = batch_or_404(batch_id)
    _BATCH_CACHE[batch_id] = {"ts": time.time(), "revision": _batch_revision(batch_id), "data": data}
    return data


@app.get("/import/{batch_id}")
def get_batch(batch_id: str, request: Request):
    shop_id = _current_shop_id(request)
    return {"ok": True, "result": batch_or_404(batch_id, shop_id)}


@app.put("/import/{batch_id}/items")
def update_items(batch_id: str, body: ItemsBody, request: Request):
    shop_id = _current_shop_id(request)
    batch = batch_or_404(batch_id, shop_id); rows = body.items
    errors = sum(bool(row.get("errors")) for row in rows)
    conn = db(); conn.execute("UPDATE batches SET rows_json=?,status=?,valid=?,errors=?, revision=revision+1 WHERE id=? AND shop_id=?", (json.dumps(rows, ensure_ascii=False), "待校验" if errors else "待发布", len(rows)-errors, errors, batch_id, shop_id)); conn.commit(); conn.close()
    _invalidate_batch_cache(batch_id)
    products = group_products(rows)
    return {"ok": True, "result": {"total": len(rows), "product_count": len(products), "valid": len(rows)-errors, "errors": errors, "product_errors": sum(bool(product["errors"]) for product in products)}}


@app.put("/import/{batch_id}/mappings")
def update_mappings(batch_id: str, body: MappingsBody, request: Request):
    shop_id = _current_shop_id(request)
    batch_or_404(batch_id, shop_id); conn = db(); conn.execute("UPDATE batches SET mappings_json=?, revision=revision+1 WHERE id=? AND shop_id=?", (json.dumps(body.mappings, ensure_ascii=False), batch_id, shop_id)); conn.commit(); conn.close(); _invalidate_batch_cache(batch_id); return {"ok": True, "result": body.mappings}


@app.post("/import/{batch_id}/validate")
def validate(batch_id: str, request: Request):
    shop_id = _current_shop_id(request)
    batch = batch_or_404(batch_id, shop_id); rows = batch["items"]; seen = set()
    for row in rows:
        errors = [e for e in row.get("errors", []) if e not in ("商品编码重复", "SKU编码重复")]
        key = row.get("product_code", "")
        sku = row.get("sku_code", "")
        if key and sku and (key, sku) in seen: errors.append("SKU编码重复")
        seen.add((key, sku)); row["errors"] = errors
    # 复用 update_items 落库; 必须把 request 透传过去, 否则它会拿不到店铺上下文(P1 加 shop_id 后新增的参数)
    return update_items(batch_id, ItemsBody(items=rows), request)


@app.post("/import/{batch_id}/publish")
def publish(batch_id: str, body: PublishBody, request: Request):
    # 这里有两个身份，别混淆：
    #   来源店 = 批次所属店（由 X-Shop-Id 决定），提供平台级映射(类目/属性/SKU规格)；
    #   目标店 = 任务写到哪家店（由 body.target_shop_id 决定），决定用谁的凭证 + 店铺参数发布。
    # 不传 target_shop_id 时两者相同，即改造前的同店发布。
    shop_id = _current_shop_id(request)
    _verify_shop(shop_id)
    operator = _current_operator(request)
    batch = batch_or_404(batch_id, shop_id)
    target_shop_id = (body.target_shop_id or "").strip() or shop_id
    _verify_shop(target_shop_id)
    cross_shop = target_shop_id != shop_id
    if batch["errors"]: raise HTTPException(400, "仍有校验错误，不能发布")
    # 校验所选平台在**目标店**下都配置了（凭证与店铺参数都按目标店取）
    # 白名单来自注册表：新平台注册后这里自动放行，不需要再改这一行
    platforms = [p for p in body.platforms if platform_registry.is_platform(p)]
    if not platforms: raise HTTPException(400, "至少选择一个发布平台")
    for p in platforms:
        if not shop_registry.has_platform(target_shop_id, p):
            raise HTTPException(400, f"目标店铺未配置 {p} 平台")
        # 跨店复用来源店的平台级映射，所以来源店必须也是同一平台：
        # 微信批次的类目/属性发不到小红书，硬发必被平台拒。
        if cross_shop and not shop_registry.has_platform(shop_id, p):
            raise HTTPException(400, f"来源店铺未配置 {p} 平台，不能跨店发布该平台")
    # 按 product_codes 过滤(分批发布);不传则全量
    products = batch["products"]
    if body.product_codes:
        code_set = set(body.product_codes)
        products = [p for p in products if p["product_code"] in code_set]
        if not products: raise HTTPException(400, "所选商品均不存在")
    # ⚠️ 店铺必填参数(运费模板/物流方案)没选 → 直接拦在这里, 不建任务:
    #    否则任务建出来、worker 逐个商品报"未配置运费模板", 运营只能看着满屏失败项再重发一轮。
    missing_params = _missing_shop_params(batch["mappings"], products, platforms, body.params or {})
    if missing_params:
        where = "目标店铺参数（跨店发布）" if cross_shop else "「平台映射」这一步"
        raise HTTPException(400, "还没选：" + "；".join(missing_params)
                            + f"。请先在{where}选好，再回来点发布（不会创建任务，也不会产生失败记录）")
    # Idempotency guard: do not create another platform item for a combination
    # that already has a publish record in this batch.
    check_conn = db()
    check_conn.execute("BEGIN IMMEDIATE")
    prior = []
    pending = []
    for product in products:
        for platform in platforms:
            # 幂等按**目标店 + 该平台当前模式**判断：发给 A 店成功的记录不该阻止把同一批货发给 B 店；
            # 更关键的是 mock 成功（假记录）绝不能拦住真实发布 —— 否则运营"试跑一遍"之后
            # 正式发布会被静默跳过，平台上什么都没有而界面显示"已发布"。
            row = check_conn.execute("SELECT * FROM publish_items WHERE shop_id=? AND batch_id=? AND product_code=? AND platform=? AND mode=? ORDER BY id DESC LIMIT 1", (target_shop_id, batch_id, product["product_code"], platform, mock_platform.resolve_mode(platform, target_shop_id))).fetchone()
            # deleted=核对后确认平台商品已被删除, cancelled=人工停掉: 都不算"发过", 必须放行重新发,
            # 否则运营在平台后台删完商品后, 这一批永远卡在"已有发布任务，未重复创建"。
            if row and row["status"] not in ("deleted", "cancelled"):
                prior.append(row)
            else:
                pending.append((product["product_code"], platform))
    if prior and not pending:
        check_conn.commit()
        reusable = next((row for row in reversed(prior) if row["status"] in ("queued", "running")), prior[-1])
        check_conn.close()
        status = reusable["status"]
        message = {"success": "所选商品已发布，未重复创建", "failed": "所选商品已有失败项，请使用重试失败项", "partial": "所选商品存在部分成功项，请勿重复创建"}.get(status, "所选商品已有发布任务，未重复创建")
        return {"ok": True, "job_id": reusable["job_id"], "batch_id": batch_id, "platforms": platforms, "existing": True, "created_count": 0, "skipped_count": len(prior), "target_shop_id": target_shop_id, "cross_shop": cross_shop, "message": message}
    # Keep the write transaction open through job/item insertion.
    conn = check_conn
    job_id = uuid.uuid4().hex
    total = len(pending) if pending else len(products) * len(platforms)
    # 分批发布(带了 product_codes) = 运营明确只要发这几件, 标记优先, 插到队列前面走
    partial = bool(body.product_codes)
    priority = 1 if partial else 0
    # 任务与任务项写到**目标店**名下（worker 据此用目标店凭证发布，图片素材也传目标店）；
    # params_json 存该店私有参数，执行时覆盖批次里的来源店参数。
    # 批次本身仍属来源店：状态更新按来源店，绝不改批次的 shop_id。
    # 模式标签：逐条按「目标店 + 平台」解析（与平台服务内部用的是同一个函数，
    # 所以标签不会和实际行为打架）。mock 记录默认不进发布状态/失败统计，见 _LIVE_ONLY。
    job_mode = _mode_scope(platforms, target_shop_id)
    conn.execute("INSERT INTO publish_jobs(id,shop_id,batch_id,platforms_json,status,total,operator,created_at,updated_at,params_json,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (job_id, target_shop_id, batch_id, json.dumps(platforms), "queued", total, operator, now(), now(), json.dumps(body.params or {}), job_mode))
    for product_code, platform in (pending or [(product["product_code"], platform) for product in products for platform in platforms]):
        conn.execute("INSERT INTO publish_items(job_id,shop_id,batch_id,product_code,platform,status,priority,mode) VALUES(?,?,?,?,?,?,?,?)", (job_id, target_shop_id, batch_id, product_code, platform, "queued", priority, mock_platform.resolve_mode(platform, target_shop_id)))
    conn.execute("UPDATE batches SET status=? WHERE id=? AND shop_id=?", ("发布任务已创建", batch_id, shop_id)); conn.commit(); conn.close(); _invalidate_batch_cache(batch_id)
    message = f"已创建发布任务({len(products)} 件商品 × {len(platforms)} 平台)" if partial else "发布任务已入队，后台将持续执行"
    if cross_shop:
        message += f"（目标店铺：{target_shop_id}）"
    return {"ok": True, "job_id": job_id, "batch_id": batch_id, "platforms": platforms, "partial": partial, "product_count": len(products), "target_shop_id": target_shop_id, "cross_shop": cross_shop, "message": message}


# ==================================================================
# 数据面板：订单日报指标（天 × 店铺 × 平台）
# ------------------------------------------------------------------
# 来源：平台服务的只读接口（微信 /orders /aftersales，小红书 /orders /aftersales）。
# 中台只落**聚合值**，不存订单明细、不落地任何买家信息（合规）。
# ⚠️ 口径默认值集中在本节常量里；运营确认后只改常量与页面文案，不动表结构。
# ==================================================================
# 订单状态分组（口径）：
#   微信枚举见官方文档「获取订单详情」；
#   小红书 orderStatus 官方未公开码表（SDK 里只是 int），先留空 →
#   该平台的"实际收入/实际笔数"暂记 0，页面会标注「码表待核对」，绝不猜。
ORDER_STATUS_GROUPS = {
    "wechat": {
        "unpaid": (10, 12, 13, 17, 250),      # 待付款/礼物待收下/待成团/待核销/已取消
        "pending_ship": (20,),                # 待发货
        "shipped": (21, 30),                  # 部分发货 / 待收货（已发货）
        "finished": (100,),                   # 已完成
    },
    "xhs": {"unpaid": (), "pending_ship": (), "shipped": (), "finished": ()},
    # 有赞：status 是**字符串枚举**（不是数字码），★枚举值待真机核对（docs §11-★8）。
    # 未列出的状态会被当成"既未付款也未完成"，不会算进任何一个口径 —— 宁可少算不猜。
    "youzan": {
        "unpaid": ("WAIT_BUYER_PAY", "TRADE_CLOSED"),        # 待付款 / 交易关闭
        "pending_ship": ("WAIT_SELLER_SEND_GOODS",),         # 待发货
        "shipped": ("WAIT_BUYER_CONFIRM_GOODS",),            # 待收货（已发货）
        "finished": ("TRADE_SUCCESS",),                      # 交易成功
    },
}
# 退款「已完成」的状态值（微信：官方枚举；小红书：字段名带 SUCCESS 语义，待真机核对）
REFUND_DONE_STATUS = {
    "wechat": ("MERCHANT_REFUND_SUCCESS", "MERCHANT_RETURN_SUCCESS"),
}
# 金额单位：微信=分（官方文档），小红书=元（字段名 refundAmountYuan 暗示「元」，待真机核对）
# 订单「支付金额」单位：实测小红书订单详情 totalPayAmount 也是**分**（109900 = 1099.00 元）
# 有赞：★待真机对账一次（item.create 的 price 确定是「元」，订单金额字段须实测确认；
#       写错不会报错，只会让面板金额放大/缩小 100 倍）。
ORDER_AMOUNT_UNIT = {"wechat": "cent", "xhs": "cent", "youzan": "yuan"}
# 售后退款金额单位与订单不同：小红书字段名 refundAmountYuan/expectedRefundAmountYuan 带 Yuan → 元
REFUND_AMOUNT_UNIT = {"wechat": "cent", "xhs": "yuan", "youzan": "yuan"}
# 售后（退款）只读接口已接入的平台。
# ⚠️ 有赞的售后接口名仍是待核对项（docs §11-★9）：在确认之前**不去打**它的服务 ——
#    否则要么 404 让整个订单同步失败，要么静默把退款金额记成 0 而页面看不出原因。
#    核对完成后把它加进来即可（同时要在 youzan_api.py 补 /aftersales 路由）。
_AFTERSALE_PLATFORMS = ("wechat", "xhs")
ORDER_SYNC_MAX_PAGES = 20          # 单日单平台最多翻 20 页，防止异常时无限翻页
# 单日单店最多补几条订单详情（小红书列表缺金额/下单时间，只能逐单补；防止把平台限流打满）。
# 定 200 的原因：实测一天可能有几十单，30 会把大半天数漏成"金额不全"（面板数字偏小且不显眼），
# 200 足够覆盖日常、又不会在异常时无限调用（超出会在 notes 里点名还剩多少单没补）。
ORDER_DETAIL_LOOKUP_MAX = 200
# 小红书取图：拉商品列表建映射时最多翻多少页（100 页/页，店内商品实测 155 条，2 页就够）
XHS_ITEM_IMAGE_MAX_PAGES = 20
# 商品图取图失败（平台没返回头图 / 上游服务当时没起来）后多久才允许重试：
# 失败不能像成功那样永久缓存 —— 否则一次"上游没启动"会被固化，面板长期无图。
IMAGE_FAILURE_RETRY_SECONDS = 600
# 商品级数据（微信罗盘）单日单店最多查几个商品：接口**只支持一个商品+一个单日**，
# 没批量，所以是"商品数 × 2 天"次请求。定 300 是覆盖日常有销量的商品，又避免异常时把限流打满。
PRODUCT_STATS_MAX_ITEMS = 300
# 并发查商品级数据的线程数：接口是纯读、单次很轻，给 4 个并发把 N 次串行的等待摊掉。
PRODUCT_STATS_WORKERS = 4
ORDER_SYNC_DEFAULT_DAYS = 2        # 手动/自动同步默认覆盖「昨天 + 今天」（T+1 补拉）
# 自动同步的进程内占位（key=last_order_sync:<店>:<平台>）：多线程 worker 去重用，见 maybe_sync_order_metrics
_ORDER_SYNC_LOCK = threading.Lock()
_ORDER_SYNC_INFLIGHT = set()


def _to_yuan(platform, value, unit_map=None):
    """平台金额 → 元（保留 2 位）。unit_map 用于"同一平台不同字段单位不同"的场合。"""
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if (unit_map or ORDER_AMOUNT_UNIT).get(platform) == "cent":
        amount /= 100
    return round(amount, 2)


def _to_epoch_seconds(value):
    """平台时间 → 秒（拿不到返回 0）。三种形态都要吃下：

    - **秒级时间戳**（微信）
    - **毫秒时间戳**（小红书：如 1791152601107）
    - **"YYYY-MM-DD HH:MM:SS" 字符串**（有赞订单接口的时间字段是字符串，不是时间戳）

    不归一化就会拿"秒级区间"去比另一种刻度，结果永远不在区间内 ——
    线上表现是"订单拉到了、指标全是 0"（小红书踩过一次，有赞会以字符串形式再踩一次）。
    """
    if isinstance(value, str) and value.strip() and not value.strip().isdigit():
        text = value.strip().replace("T", " ")[:19]
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return int(time.mktime(time.strptime(text, fmt)))
            except (ValueError, OverflowError):
                continue
    try:
        ts = int(float(value or 0))
    except (TypeError, ValueError):
        return 0
    if ts > 10 ** 11:      # 毫秒（10^11 秒 ≈ 公元 5138 年，秒级不可能超过）
        ts //= 1000
    return ts


def _day_bounds(stat_date):
    """'YYYY-MM-DD' → (当日 00:00 的秒级时间戳, 次日 00:00)，本地时区。"""
    day = datetime.strptime(stat_date, "%Y-%m-%d")
    start = int(day.timestamp())
    return start, start + 86400


# 平台 → 订单只读服务地址。
# ⚠️ 这里原来是 `WECHAT_API_BASE if platform == "wechat" else XHS_API_BASE` 的二元判断：
#    接第三个平台时会把有赞的订单请求**静默打到小红书服务**（404 或拿错数据都不报错），
#    所以改成查表 + 适配器兜底。
def _order_api_base(platform):
    return {"wechat": WECHAT_API_BASE, "xhs": XHS_API_BASE,
            "youzan": YOUZAN_API_BASE}.get(platform) or platform_registry.api_base(platform)


# —— 「商品明细」面板的订单级数据 ————————————————————————————————
# 中台**不落订单明细表**（order_daily_metrics/product_daily_metrics 都只存聚合值，
# 见建表注释），所以明细只能实时调平台订单接口现取现用。
# 代价是慢：小红书列表不返回金额，还得逐单补详情（并发 4，上限 ORDER_DETAIL_LOOKUP_MAX）。
# 因此加一层**短 TTL 内存缓存**：翻页、换关键词、切走再切回来都命中缓存，
# 不重复打平台（平台有限流，重复打有风险）。
ORDER_DETAIL_CACHE_TTL = 300          # 秒。当天订单基本不变，但也不能一直用旧数据
ORDER_DETAIL_MAX_ORDERS = 300         # 单店单平台单次最多列多少笔（超出会明说，不静默截断）
_ORDER_DETAIL_CACHE_LOCK = threading.Lock()
_ORDER_DETAIL_CACHE: dict = {}        # '店|平台|日期' → (取回时间戳, 行列表, 备注)


def _order_detail_cache_get(key):
    with _ORDER_DETAIL_CACHE_LOCK:
        hit = _ORDER_DETAIL_CACHE.get(key)
    if not hit:
        return None
    fetched_at, rows, notes = hit
    if time.time() - fetched_at > ORDER_DETAIL_CACHE_TTL:
        return None
    return rows, notes


def _order_detail_cache_put(key, rows, notes):
    with _ORDER_DETAIL_CACHE_LOCK:
        _ORDER_DETAIL_CACHE[key] = (time.time(), rows, notes)
        # 只留最近的若干个键，避免长期运行把内存吃满
        if len(_ORDER_DETAIL_CACHE) > 200:
            oldest = sorted(_ORDER_DETAIL_CACHE, key=lambda k: _ORDER_DETAIL_CACHE[k][0])
            for stale in oldest[:80]:
                _ORDER_DETAIL_CACHE.pop(stale, None)


# 微信官方有订单状态码表；小红书未公开（SDK 里只是 int），所以那边按时间字段判断，
# 与 _aggregate_order_day 的口径保持一致（实测比猜状态码可靠）
WECHAT_ORDER_STATUS_TEXT = {
    10: "待付款", 20: "待发货", 21: "部分发货", 30: "待收货",
    100: "已完成", 200: "售后取消", 250: "未付款取消",
}


def _order_status_text(platform, order):
    """订单状态 → 中文文案（导出的「交易清单」用；不认识的码原样标出来，不编）。"""
    status = order.get("status")
    if platform == "wechat":
        if status in WECHAT_ORDER_STATUS_TEXT:
            return WECHAT_ORDER_STATUS_TEXT[status]
        try:
            return WECHAT_ORDER_STATUS_TEXT.get(int(status), f"状态码 {status}")
        except (TypeError, ValueError):
            return "—"
    if _to_epoch_seconds(order.get("finish_time")):
        return "已完成"
    if _to_epoch_seconds(order.get("pay_time")):
        return "已支付"
    if _to_epoch_seconds(order.get("cancel_time")):
        return "已取消"
    return "待付款"


def _order_detail_row(platform, shop_id, order):
    """一条订单摘要 → 交易明细的一行。

    金额统一「元」（两平台订单接口都是分，必须过 _to_yuan）；时间统一成**本地时区**
    的秒级时间戳，前端直接 new Date() 就能按本地时间显示（库里 synced_at 那类是 UTC，
    这里刻意不存字符串、避免又出现差 8 小时的问题）。
    """
    raw_amount = (order.get("order_price") if platform == "wechat"
                  else order.get("actual_amount") or order.get("total_amount"))
    paid_amount = _to_yuan(platform, raw_amount)
    create_ts = _to_epoch_seconds(order.get("create_time"))
    pay_ts = _to_epoch_seconds(order.get("pay_time"))
    # 「确认收货」判定：微信有状态码 100 和确认收货时间；小红书码表未公开，按 finishTime
    confirm_ts = _to_epoch_seconds(order.get("confirm_receipt_time") or order.get("finish_time"))
    confirmed = bool(confirm_ts) or (platform == "wechat" and order.get("status") == 100)
    items = order.get("items") if isinstance(order.get("items"), list) else []
    if not items and isinstance(order.get("product_infos"), list):
        items = order.get("product_infos")
    quantity = 0
    for item in items:
        try:
            quantity += int((item or {}).get("product_cnt") or (item or {}).get("quantity") or 0)
        except (TypeError, ValueError):
            quantity += 1
    return {
        # 订单编号：小红书有独立的 order_no（人看的单号），微信只有 order_id
        "order_no": str(order.get("order_no") or order.get("order_id") or ""),
        "order_id": str(order.get("order_id") or ""),
        "platform": platform, "shop_id": shop_id,
        "shop_name": _shop_display_name(shop_id),
        "create_ts": create_ts, "pay_ts": pay_ts,
        "paid": bool(pay_ts), "paid_amount": paid_amount,
        # 确认收货金额：平台没有这个字段，用「已完成订单的实付额」近似，未完成给 None
        "confirm_amount": paid_amount if confirmed else None,
        "status": order.get("status"),
        "status_text": _order_status_text(platform, order),
        "item_count": len(items), "item_qty": quantity or len(items),
        "item_title": (str((items[0] or {}).get("title") or "")[:40]
                       if items else ""),
    }


def _write_order_items(conn, shop_id, platform, stat_date, orders):
    """把当天的订单级明细落库（导出的交易清单 / 页面商品明细都读它）。

    只写交易维度，**不写买家标识**：买家信息同步时只用于内存去重，不落库（见 xhs_store 白名单）。
    同一次同步里订单可能被重复处理（幂等重跑），所以先删当天这个店这个平台的旧行再写。
    """
    conn.execute("DELETE FROM order_items WHERE shop_id=? AND platform=? AND stat_date=?",
                 (shop_id, platform, stat_date))
    payload = []
    for order in orders:
        row = _order_detail_row(platform, shop_id, order)
        payload.append((shop_id, platform, stat_date, row["order_no"], row["order_id"],
                        row["create_ts"] or 0, row["pay_ts"] or 0, row["paid_amount"],
                        row["status"], row["status_text"], row["confirm_amount"],
                        row["item_qty"], row["item_count"], row["item_title"],
                        row["shop_name"], now()))
    if payload:
        conn.executemany(
            "INSERT OR REPLACE INTO order_items (shop_id, platform, stat_date, order_no, order_id,"
            " create_ts, pay_ts, paid_amount, status_code, status_text, confirm_amount,"
            " item_qty, item_count, item_title, shop_name, synced_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", payload)
    return len(payload)


def _local_order_rows(shop_id, platform, stat_date):
    """读本地已落的订单明细；没有就返回空列表（调用方决定要不要实时去拉）。"""
    conn = db()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM order_items WHERE shop_id=? AND platform=? AND stat_date=?"
            " ORDER BY COALESCE(pay_ts, create_ts) DESC", (shop_id, platform, stat_date))]
    finally:
        conn.close()


def _order_detail_rows(platform, shop_id, stat_date):
    """某店某平台某天的订单明细行 + 备注。

    **本地优先**：同步时已经把明细落库了，所以这里直接读 SQLite（毫秒级）。
    只有"还没同步过的日子"才实时打平台接口（带短 TTL 缓存）——这正是导出变慢的根源。
    """
    local = _local_order_rows(shop_id, platform, stat_date)
    if local:
        return local, []
    key = f"{shop_id}|{platform}|{stat_date}"
    cached = _order_detail_cache_get(key)
    if cached:
        return cached
    start, end = _day_bounds(stat_date)
    notes = []
    try:
        orders = _fetch_orders(platform, shop_id, start, end)
        detail_note = _enrich_orders_with_detail(platform, shop_id, orders)
        if detail_note:
            notes.append(detail_note)
    except Exception as exc:  # noqa: BLE001
        return [], [f"{_shop_display_name(shop_id)}/{platform}：订单拉取失败"
                    f"（{_http_error_message(exc)[:120]}）"]
    if len(orders) > ORDER_DETAIL_MAX_ORDERS:
        total = len(orders)
        orders = orders[:ORDER_DETAIL_MAX_ORDERS]
        notes.append(f"当天订单 {total} 笔，超过单次 {ORDER_DETAIL_MAX_ORDERS} 笔上限，"
                     f"明细只列最近 {ORDER_DETAIL_MAX_ORDERS} 笔（完整数据看上方「数据概览」）")
    rows = [_order_detail_row(platform, shop_id, order) for order in orders]
    # 按支付时间倒序；没付款的排在最后但仍按创建时间排
    rows.sort(key=lambda row: (row["pay_ts"] or row["create_ts"] or 0), reverse=True)
    _order_detail_cache_put(key, rows, notes)
    return rows, notes


def _fetch_orders(platform, shop_id, start, end):
    """拉某平台某天的订单摘要（平台侧已剔除买家隐私）。"""
    base = _order_api_base(platform)
    orders = []
    if platform == "wechat":
        next_key = ""
        for _ in range(ORDER_SYNC_MAX_PAGES):
            query = {"start": start, "end": end, "page_size": 100}
            if next_key:
                query["next_key"] = next_key
            page = (_http_get_json(f"{base}/orders?{urllib.parse.urlencode(query)}", shop_id=shop_id)
                    .get("result") or {})
            orders.extend(page.get("orders") or [])
            next_key = page.get("next_key") or ""
            if not page.get("has_more") or not next_key:
                break
        return orders
    page_no = 1
    for _ in range(ORDER_SYNC_MAX_PAGES):
        query = {"start": start, "end": end, "page_no": page_no, "page_size": 100}
        page = (_http_get_json(f"{base}/orders?{urllib.parse.urlencode(query)}", shop_id=shop_id)
                .get("result") or {})
        orders.extend(page.get("orders") or [])
        max_page = int(page.get("max_page_no") or 0)
        page_no += 1
        if page_no > max_page:
            break
    return orders


def _fetch_aftersales(platform, shop_id, start, end):
    """拉某平台某天的售后（退款）摘要。

    ⚠️ 只对 `_AFTERSALE_PLATFORMS` 里的平台发请求：未接入售后的平台（当前是有赞）
    直接返回空，由 sync_order_metrics 在 notes 里写明"退款按 0 计"，
    **不静默当成"这天没有退款"**，也不去赌一个还没核对的接口名。
    """
    if platform not in _AFTERSALE_PLATFORMS:
        return []
    base = _order_api_base(platform)
    aftersales = []
    if platform == "wechat":
        next_key = ""
        for _ in range(ORDER_SYNC_MAX_PAGES):
            query = {"begin": start, "end": end, "page_size": 100}
            if next_key:
                query["next_key"] = next_key
            page = (_http_get_json(f"{base}/aftersales?{urllib.parse.urlencode(query)}", shop_id=shop_id)
                    .get("result") or {})
            aftersales.extend(page.get("aftersales") or [])
            next_key = page.get("next_key") or ""
            if not page.get("has_more") or not next_key:
                break
        return aftersales
    page_no = 1
    for _ in range(ORDER_SYNC_MAX_PAGES):
        query = {"start": start, "end": end, "page_no": page_no, "page_size": 100}
        page = (_http_get_json(f"{base}/aftersales?{urllib.parse.urlencode(query)}", shop_id=shop_id)
                .get("result") or {})
        aftersales.extend(page.get("aftersales") or [])
        max_page = int(page.get("max_page_no") or 0)
        page_no += 1
        if page_no > max_page:
            break
    return aftersales


def _enrich_orders_with_detail(platform, shop_id, orders):
    """列表里缺「下单时间 / 金额」的订单，逐单补订单详情（目前只有小红书需要）。

    实测：小红书 `order.getOrderList` 的 `createTime` / `actualAmount` / `totalAmount`
    **全是 null**（只有毫秒级 `updateTime`），而这两个字段是算指标必需的。
    不补详情 → 该天订单数 > 0 但金额与笔数全 0 —— 这就是线上"小红书没有数据"的真身。
    返回一行说明写进 notes；不需要补或没补到则返回空串。
    """
    if platform != "xhs" or not orders:
        return ""
    missing = [order for order in orders
               if not (order.get("actual_amount") or order.get("total_amount"))
               or not _to_epoch_seconds(order.get("pay_time") or order.get("create_time"))]
    if not missing:
        return ""
    base = _order_api_base(platform)
    todo = missing[:ORDER_DETAIL_LOOKUP_MAX]
    filled = 0
    failed = 0

    def fetch(order):
        order_id = order.get("order_id")
        if not order_id:
            return None
        url = f"{base}/order-detail?order_id={urllib.parse.quote(str(order_id))}"
        try:
            return order, (_http_get_json(url, shop_id=shop_id).get("result") or {})
        except Exception:  # noqa: BLE001
            return order, None

    # 逐单详情互不依赖，限并发避免把平台限流打满（平台侧有频率限制）
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(4, len(todo))) as pool:
        for order, detail in pool.map(fetch, todo):
            if detail is None:
                failed += 1
                continue
            for key in ("create_time", "pay_time", "finish_time", "cancel_time", "status",
                        "update_time", "actual_amount", "total_amount", "items", "buyer_key"):
                if detail.get(key) not in (None, ""):
                    order[key] = detail[key]
            filled += 1
    note = f"订单列表缺金额/下单时间，已逐单补详情 {filled} 单"
    if failed:
        note += f"（{failed} 单详情取失败）"
    if len(missing) > ORDER_DETAIL_LOOKUP_MAX:
        note += f"；另有 {len(missing) - ORDER_DETAIL_LOOKUP_MAX} 单未补（超过单日上限 {ORDER_DETAIL_LOOKUP_MAX}）"
    return note


def _aggregate_order_day(platform, orders, aftersales, start, end):
    """把一天的订单/售后聚合成本面板的指标（金额单位：元）。

    人数口径：按订单里的买家标识(buyer_key)**当天去重**计数，只落个数、不落标识。
    平台没有官方人数接口时（小红书）就用这个自算值；微信优先用罗盘 pay_uv。
    """
    groups = ORDER_STATUS_GROUPS.get(platform) or {}
    unpaid, pending_ship, shipped = (set(groups.get(k) or ()) for k in ("unpaid", "pending_ship", "shipped"))
    finished = set(groups.get("finished") or ())
    paid_orders = dealt_orders = actual_orders = 0
    paid_amount = actual_income = 0.0
    paid_buyer_keys, created_buyer_keys = set(), set()

    for order in orders:
        status = order.get("status")
        buyer_key = str(order.get("buyer_key") or "").strip()
        # 金额字段名各平台不同：微信 order_price(分)；小红书详情 totalPayAmount(分)
        raw_amount = (order.get("order_price") if platform == "wechat"
                      else order.get("actual_amount") or order.get("total_amount"))
        amount = _to_yuan(platform, raw_amount)
        # 下单时间：两平台都是"订单创建时间"，用来算下单人数（含未支付/已取消）
        create_ts = _to_epoch_seconds(order.get("create_time"))
        if buyer_key and create_ts and start <= create_ts < end:
            created_buyer_keys.add(buyer_key)
        # ⚠️ 支付件数/金额**只认 pay_time**：原来的 `pay_time or create_time` 兜底会把
        #    没付款的订单也算成已支付（金额为 0，所以金额看着对、件数虚高）；
        #    微信那边"未付款取消"的单还会把挂牌价算进支付金额。
        if platform == "xhs":
            # 小红书**没有公开的状态码表**,所以一律用时间字段判断(实测字段:paidTime/finishTime/
            # cancelTime,毫秒)——比猜状态码可靠:有 paidTime 就是已支付,有 finishTime 就是已完成。
            pay_ts = _to_epoch_seconds(order.get("pay_time"))
            finish_ts = _to_epoch_seconds(order.get("finish_time"))
            cancel_ts = _to_epoch_seconds(order.get("cancel_time"))
            if pay_ts and start <= pay_ts < end:
                paid_orders += 1
                paid_amount += amount
                if buyer_key:
                    paid_buyer_keys.add(buyer_key)
            if finish_ts and start <= finish_ts < end:
                dealt_orders += 1
            # 在途=已支付且未完成未取消(存量口径,与微信一致)
            if pay_ts and not finish_ts and not cancel_ts:
                actual_orders += 1
                actual_income += amount
            continue
        # 支付时间：微信 pay_time（秒）
        pay_ts = _to_epoch_seconds(order.get("pay_time"))
        if status not in unpaid and pay_ts and start <= pay_ts < end:
            paid_orders += 1
            paid_amount += amount
            if buyer_key:
                paid_buyer_keys.add(buyer_key)
        if status in finished:
            # 微信用 confirm_receipt_time（确认收货时间），有赞用 success_time（已归一成 finish_time）
            done_ts = _to_epoch_seconds(order.get("confirm_receipt_time") or order.get("finish_time"))
            if start <= done_ts < end:
                dealt_orders += 1
        if status in pending_ship or status in shipped:
            actual_orders += 1
            actual_income += amount

    done_states = REFUND_DONE_STATUS.get(platform)
    refund_amount = 0.0
    unverified_refunds = 0
    for aftersale in aftersales:
        status = str(aftersale.get("status") or "")
        if done_states:
            if status not in done_states:
                continue
            done_ts = _to_epoch_seconds(aftersale.get("complete_time") or aftersale.get("refund_time")
                                        or aftersale.get("update_time"))
            if not (start <= done_ts < end):
                continue
        else:
            # 平台没公开售后状态码表(小红书):**不猜码值**,有金额就计入,并把"状态未校验"计数回报;
            # 而且**不再用完结时间二次过滤**:售后是按「创建时间窗」查回来的,若拿完结时间再筛,
            # "10-04 创建、10-05 完结"的退款会两天都落空(实测踩到过,退款金额凭空少一笔)。
            unverified_refunds += 1
        # 小红书实际退款字段常为空、只回"预计退款金额"(实测),这里做字段兜底;
        # 退款金额单位与订单不同(小红书 refundAmountYuan 是元、订单 totalPayAmount 是分)
        refund_amount += _to_yuan(platform, aftersale.get("refund_amount")
                                  or aftersale.get("expected_refund_amount"),
                                  REFUND_AMOUNT_UNIT)

    refund_rate = round(refund_amount / paid_amount, 4) if paid_amount else 0.0
    return {
        "paid_orders": paid_orders, "paid_amount": round(paid_amount, 2),
        "dealt_orders": dealt_orders, "refund_amount": round(refund_amount, 2),
        "actual_income": round(actual_income, 2), "actual_orders": actual_orders,
        "refund_rate": refund_rate, "unverified_refunds": unverified_refunds,
        # 自算人数：当天去重的买家个数。0 表示这天没取到买家标识（平台没这字段），
        # 调用方要靠"是 0 还是没有"区分，别把它当成"0 个买家"。
        "paid_buyers": len(paid_buyer_keys), "created_buyers": len(created_buyer_keys),
    }


def _aggregate_product_day(platform, orders, start, end):
    """把一天的订单按「商品」聚合（金额单位：元）。

    金额**按订单实付等比分摊**到各商品条目：平台给的"商品单价 × 数量"之和常常不等于
    订单实付（优惠、运费、跨店满减都在里面），直接相加会和总览的支付金额对不上。
    这里用比例分摊，保证 Σ商品金额 === Σ订单实付，页面/导出都能自洽。
    返回 (rows, unnamed)：无名商品的订单数（平台没给商品名时无法归类）。
    """
    groups = ORDER_STATUS_GROUPS.get(platform) or {}
    unpaid = set(groups.get("unpaid") or ())
    buckets = {}
    unnamed = 0
    for order in orders:
        # 商品条目：微信服务的键是 product_infos，小红书是 items（都兼容，别写死）
        items = order.get("product_infos") or order.get("products") or order.get("items") or []
        status = order.get("status")
        pay_ts = _to_epoch_seconds(order.get("pay_time") or order.get("create_time"))
        if not items or status in unpaid or not pay_ts or not (start <= pay_ts < end):
            if not items:
                unnamed += 1
            continue
        raw_amount = (order.get("order_price") if platform == "wechat"
                      else order.get("actual_amount") or order.get("total_amount"))
        order_amount = _to_yuan(platform, raw_amount)
        weights = []
        for item in items:
            try:
                qty = int(item.get("product_cnt") or 0) or 1
            except (TypeError, ValueError):
                qty = 1
            unit = item.get("real_price") or item.get("sale_price") or item.get("paid_amount") or 0
            try:
                unit_value = float(unit)
            except (TypeError, ValueError):
                unit_value = 0.0
            weights.append((qty, max(unit_value, 0.0) * qty))
        total_weight = sum(weight for _qty, weight in weights)
        for item, (qty, weight) in zip(items, weights):
            key = (str(item.get("product_id") or ""), str(item.get("sku_id") or ""))
            bucket = buckets.setdefault(key, {
                "product_id": key[0], "sku_id": key[1],
                "product_name": item.get("title") or "",
                "image_url": item.get("image") or "",
                "sku_code": item.get("sku_code") or "",
                "paid_qty": 0, "paid_amount": 0.0, "order_count": 0,
            })
            bucket["paid_qty"] += qty
            bucket["order_count"] += 1
            bucket["paid_amount"] += (order_amount if total_weight <= 0
                                      else order_amount * weight / total_weight)
            if not bucket["product_name"] and item.get("title"):
                bucket["product_name"] = item["title"]
            if not bucket["image_url"] and item.get("image"):
                bucket["image_url"] = item["image"]
            if not bucket["sku_code"] and item.get("sku_code"):
                bucket["sku_code"] = item["sku_code"]
    rows = []
    for bucket in buckets.values():
        bucket["paid_amount"] = round(bucket["paid_amount"], 2)
        rows.append(bucket)
    rows.sort(key=lambda row: row["paid_amount"], reverse=True)
    return rows, unnamed


def _write_product_metrics(conn, shop_id, platform, stat_date, rows):
    """整天的商品行先删再写：商品集合会变（改名/下架），只 UPSERT 会留下已经不存在的旧行。"""
    conn.execute("DELETE FROM product_daily_metrics WHERE shop_id=? AND platform=? AND stat_date=?",
                 (shop_id, platform, stat_date))
    if not rows:
        return 0
    note = "退款仍按订单口径，未用商品级退款金额（平台商品级接口只回成交退款，口径不同）"
    conn.executemany(
        "INSERT INTO product_daily_metrics (shop_id, platform, stat_date, product_id, sku_id,"
        " product_name, sku_code, paid_qty, paid_amount, order_count, image_url,"
        " product_click_uv, product_click_cnt, product_pay_uv, product_pay_cnt,"
        " product_create_uv, product_refund_amount, stats_source, notes, synced_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(shop_id, platform, stat_date, row["product_id"], row["sku_id"], row["product_name"],
          row["sku_code"], row["paid_qty"], row["paid_amount"], row["order_count"],
          row.get("image_url") or "",
          int(row.get("product_click_uv") or 0), int(row.get("product_click_cnt") or 0),
          int(row.get("product_pay_uv") or 0), int(row.get("product_pay_cnt") or 0),
          int(row.get("product_create_uv") or 0),
          float(row.get("product_refund_amount") or 0.0),
          row.get("stats_source") or None, note, now())
         for row in rows])
    return len(rows)


def _supports_stats(platform):
    """该平台是否提供了「自带数据概览」能力（微信=罗盘；小红书没有）。"""
    try:
        return "stats" in (platform_registry.adapter(platform).get("supports") or ())
    except Exception:  # noqa: BLE001
        return False


def _fetch_shop_overview(platform, shop_id, stat_date):
    """拉平台自带的当日数据概览（目前只有微信罗盘有）。没有该能力的平台返回空 dict。"""
    if not _supports_stats(platform):
        return {}
    base = _order_api_base(platform)
    data = _http_get_json(f"{base}/shop-overview?date={stat_date}", shop_id=shop_id)
    return (data or {}).get("result") or {}


def _fetch_product_overview(platform, shop_id, stat_date, product_id):
    """拉平台自带的**商品级**单日数据（目前只有微信罗盘有）。

    罗盘商品接口限制（实测）：
      · 一次只支持**一个商品 + 一个单日**，没有批量/区间 —— 覆盖 N 商品 × D 天就是 N×D 次请求；
      · 官方标注「仅历史数据，暂无当日数据」，所以调用方要保证 stat_date 不是今天；
      · 商品不在该店/已删除时返回不了数据 → 调用方按"没取到"处理，不要写 0 冒充。
    返回归一化后的 dict（金额单位：分），拿不到返回空 dict。
    """
    if not product_id or not _supports_stats(platform):
        return {}
    base = _order_api_base(platform)
    query = urllib.parse.urlencode({"date": stat_date, "product_id": product_id})
    try:
        data = _http_get_json(f"{base}/product-overview?{query}", shop_id=shop_id, timeout=20)
    except Exception:  # noqa: BLE001
        # 商品已删除/不在本店时平台报错 → 上层把这一行当"没有数据"（显示「—」），
        # 不要因为个别商品查不到就把整天的同步打断。
        return {}
    return (data or {}).get("result") or {}


def _attach_product_stats(platform, shop_id, stat_date, rows):
    """给当天的商品行补「平台商品级」流量/成交数据（原地写入 row）。返回一条 notes 说明。

    只对支持 stats 的平台做（目前 = 微信）；小红书整列保持 0 且不写 stats_source，
    前端据此显示「—」（用 0 冒充"没有访客"是错的：分不清"真的 0 人"和"平台没这数据"）。

    ⚠️ 只把数据挂在该商品的**一行**上（该商品多 SKU 时选第一行）：
    罗盘口径是「商品级」，同商品的每个 SKU 行都挂一遍，下游 merge 求和会成倍虚高。
    单商品失败不影响整体：跳过该商品即可，最后在 notes 里点名取到几个。
    """
    if not rows or not _supports_stats(platform):
        return ""
    if stat_date >= datetime.now().strftime("%Y-%m-%d"):
        # 罗盘商品级接口官方标注"仅历史数据，暂无当日数据"，今天拉只会拿到空/未就绪值
        return "商品级流量/成交为平台历史口径：当天数据要 T+1 才有（今天的行暂不显示）"

    # 每个商品只挑一行承载数据（也是唯一的请求对象，避免同商品重复调用）
    carriers = {}
    for row in rows:
        pid = row.get("product_id")
        if pid and pid not in carriers:
            carriers[pid] = row
    targets = list(carriers.items())[:PRODUCT_STATS_MAX_ITEMS]

    def fetch(item):
        pid, row = item
        try:
            return row, _fetch_product_overview(platform, shop_id, stat_date, pid)
        except Exception:  # noqa: BLE001
            return row, {}

    got = 0
    with ThreadPoolExecutor(max_workers=PRODUCT_STATS_WORKERS) as pool:
        for row, stats in pool.map(fetch, targets):
            if not stats:
                continue
            row["product_click_uv"] = int(stats.get("product_click_uv") or 0)
            row["product_click_cnt"] = int(stats.get("product_click_cnt") or 0)
            # 罗盘「下单人数/下单件数」：点击到支付之间那一环（下单未支付也计）
            row["product_create_uv"] = int(stats.get("create_uv") or 0)
            row["product_pay_uv"] = int(stats.get("pay_uv") or 0)
            row["product_pay_cnt"] = int(stats.get("pay_cnt") or 0)
            # 罗盘退款金额单位是分，库里商品表统一「元」
            row["product_refund_amount"] = round(int(stats.get("pay_refund_gmv") or 0) / 100, 2)
            row["stats_source"] = "平台商品数据"
            got += 1
    if not got:
        return "平台商品级数据未取到（商品可能已下架或不在本店），流量列按「无数据」显示"
    note = f"商品级流量/成交取自平台商品数据（{got}/{len(targets)} 个商品有数据）"
    if len(carriers) > PRODUCT_STATS_MAX_ITEMS:
        note += f"；超过单日上限 {PRODUCT_STATS_MAX_ITEMS}，其余商品未取"
    return note



def sync_order_metrics(shop_id, platform, stat_date):
    """同步某店某平台某一天的指标（UPSERT，幂等）。

    同时取两份数据：
      ① 订单/售后接口聚合（自算口径，两家平台都有）；
      ② 平台自带数据概览（微信罗盘，官方口径：成交金额/成交人数/商品点击人数…）。
    前端按"官方口径优先、没有就用自算口径"展示，并在卡片上标明来源。
    """
    start, end = _day_bounds(stat_date)
    groups = ORDER_STATUS_GROUPS.get(platform) or {}
    status_map_ready = bool(groups.get("shipped"))
    note_parts = [] if status_map_ready else ["订单状态码表待核对：实际收入/实际笔数暂记 0"]
    if platform == "xhs":
        # 小红书未公开订单状态码表 → 改用时间字段判断(paidTime/finishTime/cancelTime),
        # 口径可靠且可核对,所以这里不再挂"码表待核对"。金额取订单详情 totalPayAmount(分)。
        status_map_ready = True
        note_parts = ["小红书按 paidTime/finishTime 判断状态（平台未公开状态码表），"
                      "支付金额取订单详情 totalPayAmount(分)"]
    if platform not in _AFTERSALE_PLATFORMS:
        # 有赞：售后接口名待核对（docs §11-★9）。明说"退款按 0 计"，别让页面看着像"没有退款"。
        note_parts.append("该平台售后接口未接入：退款金额按 0 计（接口名待核对后补齐）")
    try:
        orders = _fetch_orders(platform, shop_id, start, end)
        aftersales = _fetch_aftersales(platform, shop_id, start, end)
        detail_note = _enrich_orders_with_detail(platform, shop_id, orders)
        if detail_note:
            note_parts.append(detail_note)
        metrics = _aggregate_order_day(platform, orders, aftersales, start, end)
        if metrics.get("unverified_refunds"):
            note_parts.append("平台未公开售后状态码表：退款金额按售后单金额计入（未校验状态）")
        error = None
    except Exception as exc:  # noqa: BLE001
        # 同步失败也要留痕（页面能看到"哪天的哪家店没拉成功"），不能静默成 0
        orders, aftersales, metrics = [], [], {
            "paid_orders": 0, "paid_amount": 0.0, "dealt_orders": 0, "refund_amount": 0.0,
            "actual_income": 0.0, "actual_orders": 0, "refund_rate": 0.0,
            "paid_buyers": 0, "created_buyers": 0,
        }
        error = _http_error_message(exc)[:300]
    stats, stats_source = {}, None
    try:
        stats = _fetch_shop_overview(platform, shop_id, stat_date)
        if stats:
            stats_source = "平台数据概览"
    except Exception as exc:  # noqa: BLE001
        # 官方口径拉不到不影响自算口径：只是这一行少了个更权威的来源，要说清楚
        note_parts.append(f"平台数据概览未取到（{str(exc)[:80]}），本行用订单接口聚合口径")
    conn = db()
    conn.execute(
        "INSERT INTO order_daily_metrics (shop_id, platform, stat_date, paid_orders, paid_amount,"
        " dealt_orders, refund_amount, actual_income, actual_orders, refund_rate, order_count,"
        " aftersale_count, status_map_ready, notes, pay_gmv, pay_uv, pay_order_cnt, pay_refund_gmv,"
        " product_click_uv, stats_source, paid_buyers, created_buyers, synced_at, sync_error)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(shop_id, platform, stat_date) DO UPDATE SET"
        " paid_orders=excluded.paid_orders, paid_amount=excluded.paid_amount,"
        " dealt_orders=excluded.dealt_orders, refund_amount=excluded.refund_amount,"
        " actual_income=excluded.actual_income, actual_orders=excluded.actual_orders,"
        " refund_rate=excluded.refund_rate, order_count=excluded.order_count,"
        " aftersale_count=excluded.aftersale_count, status_map_ready=excluded.status_map_ready,"
        " notes=excluded.notes, pay_gmv=excluded.pay_gmv, pay_uv=excluded.pay_uv,"
        " pay_order_cnt=excluded.pay_order_cnt, pay_refund_gmv=excluded.pay_refund_gmv,"
        " product_click_uv=excluded.product_click_uv, stats_source=excluded.stats_source,"
        " paid_buyers=excluded.paid_buyers, created_buyers=excluded.created_buyers,"
        " synced_at=excluded.synced_at, sync_error=excluded.sync_error",
        (shop_id, platform, stat_date, metrics["paid_orders"], metrics["paid_amount"],
         metrics["dealt_orders"], metrics["refund_amount"], metrics["actual_income"],
         metrics["actual_orders"], metrics["refund_rate"], len(orders), len(aftersales),
         1 if status_map_ready else 0, "；".join(note_parts),
         int(stats.get("pay_gmv") or 0), int(stats.get("pay_uv") or 0),
         int(stats.get("pay_order_cnt") or 0), int(stats.get("pay_refund_gmv") or 0),
         int(stats.get("product_click_uv") or 0), stats_source,
         int(metrics.get("paid_buyers") or 0), int(metrics.get("created_buyers") or 0),
         now(), error),
    )
    # 商品级聚合（数据面板下方「商品明细」的数据源）
    product_rows, unnamed_products = _aggregate_product_day(platform, orders, start, end)
    # 再补平台自带的商品级流量/成交（微信罗盘；小红书没有 → 整列留 0 让前端显示「—」）
    product_stats_note = _attach_product_stats(platform, shop_id, stat_date, product_rows)
    _write_product_metrics(conn, shop_id, platform, stat_date, product_rows)
    # 订单级明细一并落库：这些订单上面刚从平台拉过，直接存下来，
    # 导出交易清单/页面商品明细就能本地读，不必再逐笔去打平台接口（导出从半分钟变秒出）
    item_count = _write_order_items(conn, shop_id, platform, stat_date, orders)
    conn.commit()
    conn.close()
    return {"shop_id": shop_id, "platform": platform, "stat_date": stat_date, **metrics,
            "order_count": len(orders), "item_rows": item_count, "aftersale_count": len(aftersales),
            "product_count": len(product_rows), "unnamed_product_orders": unnamed_products,
            "status_map_ready": status_map_ready, "notes": "；".join(note_parts),
            "product_stats_note": product_stats_note,
            "stats_source": stats_source, "stats": stats, "sync_error": error}


def order_platforms(shop_id):
    """该店声明了 orders 能力、且确实配了凭证的平台。"""
    result = []
    for platform in platform_registry.adapters_with("orders"):
        try:
            if shop_registry.has_platform(shop_id, platform):
                result.append(platform)
        except Exception:  # noqa: BLE001
            continue
    return result


def _detail_targets(shop_id, platform=""):
    """明细查询该打哪些平台。

    ``platform`` 为空 = 该店已配的全部平台（走 order_platforms，天然安全）；
    显式指定时**必须该店确实配了这个平台**才打 —— 否则会给"只配了小红书"的店
    打微信订单接口，微信服务直接回 400（detail:「店铺 xx 未配置微信小店凭证」），
    明细表底下就多出一串「订单拉取失败（HTTP Error 400）」的噪音。
    ⚠️ 导出路径（_export_order_rows）早就修过同一个坑，区间/单日明细这里当时漏了。
    """
    configured = order_platforms(shop_id)
    if not platform:
        return configured
    return [platform] if platform in configured else []


def sync_orders_range(shop_id, start_date, end_date, platforms=None, on_progress=None):
    """同步日期区间内的订单指标（逐天 × 逐平台）。返回逐项结果。

    on_progress(shop_id, platform, stat_date, result)：每同步完一天回调一次，
    给后台同步任务汇报进度用（不传就按老样子只返回最终结果）。
    """
    targets = platforms or order_platforms(shop_id)
    days = []
    cursor = datetime.strptime(start_date, "%Y-%m-%d").date()
    last = datetime.strptime(end_date, "%Y-%m-%d").date()
    while cursor <= last:
        days.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    results = []
    for platform in targets:
        for day in days:
            item = sync_order_metrics(shop_id, platform, day)
            results.append(item)
            if on_progress:
                try:
                    on_progress(shop_id, platform, day, item)
                except Exception:  # noqa: BLE001  汇报进度出问题不该影响同步本身
                    pass
    return results


def maybe_sync_order_metrics(shop_id, platform=None, days=ORDER_SYNC_DEFAULT_DAYS):
    """每天最多同步一次（复用 _meta + 当天日期做守卫，不新增调度器）。

    由发布 worker 的空闲分支调用：不阻塞发品，失败只打日志。
    ⚠️ 同一店同一平台的多个 worker 线程会几乎同时进入本函数，只靠 _meta 判断会出现
       "6 个线程同时发现今天没同步、于是各跑一遍"的竞态（实测日志里重复 3~6 次，
       既是白拉平台接口、也可能触发平台限流）。所以这里先用**进程内占位**去重：
       抢到占位的线程负责同步，其余线程直接跳过。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    for target in ([platform] if platform else order_platforms(shop_id)):
        if target not in order_platforms(shop_id):
            continue
        key = f"last_order_sync:{shop_id}:{target}"
        with _ORDER_SYNC_LOCK:
            if key in _ORDER_SYNC_INFLIGHT:
                continue                      # 别的线程正在同步，跳过
            conn = db()
            last = _meta_get(conn, key)
            conn.close()
            if last == today:
                continue
            _ORDER_SYNC_INFLIGHT.add(key)     # 先占位再干活，避免重复拉取
        start = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        try:
            results = sync_orders_range(shop_id, start, today, platforms=[target])
            failed = [r for r in results if r.get("sync_error")]
            print(f"[orders] {shop_id}/{target} 同步 {start}~{today}：{len(results) - len(failed)} 天成功"
                  f"{f'，{len(failed)} 天失败：{failed[0]['sync_error'][:120]}' if failed else ''}", flush=True)
            conn = db()
            conn.execute("INSERT INTO _meta(key, value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, today))
            conn.commit()
            conn.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[orders] {shop_id}/{target} 同步异常（不影响发布）：{exc}", flush=True)
        finally:
            with _ORDER_SYNC_LOCK:
                _ORDER_SYNC_INFLIGHT.discard(key)


class OrderSyncBody(BaseModel):
    start: str = ""
    end: str = ""
    platforms: list[str] = []
    shop: str = ""      # 数据面板的店铺范围：'all' = 逐店同步（耗时成倍增加）；空 = 跟随当前店铺


# —— 手动同步任务注册表 ————————————————————————————————————————————
# 同步要逐天 × 逐平台打平台接口（还要逐单拉订单详情），一次几十秒到几分钟。
# 原来 POST /orders/sync 同步阻塞：请求一直挂着，前端按钮只能干转圈，看不到卡在哪天，
# 于是改成「POST 只登记任务 → 后台线程干活 → GET /orders/sync/status 读进度」。
# 任务只存内存（不建表）：服务重启后旧任务自然消失，前端拿到 idle 按"已结束"处理。
_ORDER_SYNC_JOBS_LOCK = threading.Lock()
_ORDER_SYNC_JOBS: dict = {}          # job_id → 任务字典（字段见 _sync_job_create）
_ORDER_SYNC_JOBS_SEQ: list = []      # 按创建顺序（取"最近一个任务"、淘汰老任务用）
_ORDER_SYNC_JOBS_KEEP = 20           # 只留最近这么多个，避免内存无限涨
_ORDER_SYNC_JOB_ERRORS = 5           # 任务详情里最多带几条失败明细（够前端提示就行）


def _sync_job_days(start, end):
    """区间天数（含首尾）。"""
    cursor = datetime.strptime(start, "%Y-%m-%d").date()
    last = datetime.strptime(end, "%Y-%m-%d").date()
    return max((last - cursor).days + 1, 0)


def _sync_job_public(job):
    """任务字典 → 接口返回（只给前端要看的字段）。"""
    return {
        "job_id": job["job_id"], "status": job["status"],
        "range": job["range"], "shops": job["shops"], "platforms": job["platforms"],
        "total": job["total"], "done": job["done"], "current": job["current"],
        "percent": round(job["done"] * 100 / job["total"], 1) if job["total"] else 0.0,
        "synced": job["synced"], "failed": job["failed"],
        "errors": job["errors"], "message": job["message"],
        # 因为"该店没配这个平台"被排除掉的组合（如选了微信、但某店只有小红书），
        # 让面板能说清楚它们为什么没进这次同步，而不是让人以为漏同步了。
        "skipped": job.get("skipped") or [],
        "started_at": job["started_at"], "finished_at": job["finished_at"],
    }


def _sync_job_create(start, end, shop_ids, targets_by_shop, skipped=None):
    """登记一个手动同步任务（status=running）。"""
    job_id = uuid.uuid4().hex[:12]
    platforms = sorted({p for targets in targets_by_shop.values() for p in targets})
    job = {
        "job_id": job_id, "status": "running",
        "range": {"start": start, "end": end},
        "shops": list(shop_ids), "platforms": platforms,
        "skipped": list(skipped or []),
        # 总步数 = Σ(每家平台数 × 天数)，前端拿它算百分比
        "total": sum(len(targets) * _sync_job_days(start, end) for targets in targets_by_shop.values()),
        "done": 0, "current": "", "synced": 0, "failed": 0, "errors": [],
        "message": "", "started_at": now(), "finished_at": "",
    }
    with _ORDER_SYNC_JOBS_LOCK:
        _ORDER_SYNC_JOBS[job_id] = job
        _ORDER_SYNC_JOBS_SEQ.append(job_id)
        for stale in _ORDER_SYNC_JOBS_SEQ[:-_ORDER_SYNC_JOBS_KEEP]:
            _ORDER_SYNC_JOBS.pop(stale, None)
        del _ORDER_SYNC_JOBS_SEQ[:-_ORDER_SYNC_JOBS_KEEP]
    return job


def _run_sync_job(job_id, targets_by_shop, start, end):
    """后台线程：逐店逐平台逐天同步，进度写回任务字典。"""
    with _ORDER_SYNC_JOBS_LOCK:
        job = _ORDER_SYNC_JOBS.get(job_id)
    if not job:
        return

    def on_progress(shop_id, platform, stat_date, item):
        with _ORDER_SYNC_JOBS_LOCK:
            job["done"] += 1
            job["current"] = f"{_shop_display_name(shop_id)}/{platform}/{stat_date}"
            if item.get("sync_error"):
                job["failed"] += 1
                if len(job["errors"]) < _ORDER_SYNC_JOB_ERRORS:
                    job["errors"].append({
                        "shop_id": shop_id, "platform": platform, "stat_date": stat_date,
                        "error": str(item.get("sync_error") or "")[:300],
                    })
            else:
                job["synced"] += 1

    try:
        for shop_id, targets in targets_by_shop.items():
            sync_orders_range(shop_id, start, end, platforms=targets, on_progress=on_progress)
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001  任务级失败也要落状态，别让前端一直转
        job["status"] = "failed"
        job["message"] = str(exc)[:300]
    finally:
        job["finished_at"] = now()
        job["current"] = ""
        print(f"[orders] 手动同步任务 {job_id} 结束：{job['status']}"
              f"（{job['range']['start']}~{job['range']['end']}，成功 {job['synced']} 天、"
              f"失败 {job['failed']} 天）", flush=True)


@app.post("/orders/sync")
def orders_sync(body: OrderSyncBody, request: Request):
    """手动同步订单指标：登记后台任务后**立即返回**，进度用 GET /orders/sync/status 读。

    默认「昨天 + 今天」；可指定日期区间、平台与店铺范围。
    shop='all' 时逐店同步（面板看全部店铺汇总时用），耗时会随店铺数成倍增加。
    同一时刻只跑一个手动同步：重复点击直接复用正在跑的那个任务。
    """
    shop_ids = _metric_shop_scope(request, body.shop)
    today = datetime.now().strftime("%Y-%m-%d")
    start = body.start or (datetime.now() - timedelta(days=ORDER_SYNC_DEFAULT_DAYS - 1)).strftime("%Y-%m-%d")
    end = body.end or today
    for value in (start, end):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, f"日期格式应为 YYYY-MM-DD：{value}")
    if start > end:
        raise HTTPException(400, "开始日期不能晚于结束日期")
    # 逐店解析目标平台：某家店没配订单平台就跳过，不因为其中一家没配就整批失败。
    # ⚠️ 显式 platforms 也要按"该店实际配了没"过滤（_detail_targets）：否则选了微信 +
    # 全部店铺，会给只配了小红书的两家店打微信订单接口 → 平台 400，同步结果里凭空多出
    # 一堆"订单拉取失败（HTTP Error 400）"（实测踩过）。
    targets_by_shop = {}
    skipped = []
    for shop_id in shop_ids:
        targets = [p for p in (body.platforms or order_platforms(shop_id))
                   if platform_registry.is_platform(p) and _detail_targets(shop_id, p)]
        if targets:
            targets_by_shop[shop_id] = targets
        for platform in (body.platforms or ()):
            if platform_registry.is_platform(platform) and not _detail_targets(shop_id, platform):
                skipped.append({"shop_id": shop_id, "shop": _shop_display_name(shop_id),
                                "platform": platform})
    if not targets_by_shop:
        raise HTTPException(400, "所选店铺都没有可同步订单的平台（未配置凭证，或平台未声明 orders 能力）")
    with _ORDER_SYNC_JOBS_LOCK:
        running = [job for job in _ORDER_SYNC_JOBS.values() if job["status"] == "running"]
    if running:
        return {"ok": True, "result": {**_sync_job_public(running[0]), "reused": True}}
    job = _sync_job_create(start, end, shop_ids, targets_by_shop, skipped=skipped)
    threading.Thread(target=_run_sync_job, args=(job["job_id"], targets_by_shop, start, end),
                     name=f"order-sync-{job['job_id']}", daemon=True).start()
    print(f"[orders] 手动同步任务 {job['job_id']} 启动：{start}~{end}，"
          f"店铺 {len(targets_by_shop)} 家，总步数 {job['total']}", flush=True)
    return {"ok": True, "result": _sync_job_public(job)}


@app.get("/orders/sync/status")
def orders_sync_status(job_id: str = Query("", description="不传 = 最近一个任务")):
    """读手动同步任务的进度（前端轮询用）。

    不传 job_id 时返回最近一个任务：同步过程中刷新页面，前端接着轮询就能继续看进度。
    任务只在内存里，服务重启后这里没有任务 → status=idle，前端按"已结束"处理。
    """
    with _ORDER_SYNC_JOBS_LOCK:
        job = (_ORDER_SYNC_JOBS.get(job_id) if job_id
               else (_ORDER_SYNC_JOBS.get(_ORDER_SYNC_JOBS_SEQ[-1]) if _ORDER_SYNC_JOBS_SEQ else None))
        public = _sync_job_public(job) if job else None
    return {"ok": True, "result": public or {"status": "idle", "done": 0, "total": 0,
                                             "synced": 0, "failed": 0, "errors": []}}


def _metric_view(row):
    """一行原始记录 → 面板口径（**平台自带数据概览优先**，没有就用订单接口聚合兜底）。

    这样前端只认一套字段名，不会出现"微信一行叫 pay_gmv、小红书一行叫 paid_amount"的混乱；
    每行都带 source，卡片上会写清这个数字是哪来的。
    """
    row = dict(row)
    stats_source = str(row.get("stats_source") or "").strip()
    stats_ready = bool(stats_source) and any(
        row.get(key) for key in ("pay_gmv", "pay_uv", "pay_order_cnt", "product_click_uv"))
    if stats_ready:
        paid_amount = round((row.get("pay_gmv") or 0) / 100, 2)
        refund_amount = round((row.get("pay_refund_gmv") or 0) / 100, 2)
        paid_orders = int(row.get("pay_order_cnt") or 0)
        buyers = int(row.get("pay_uv") or 0)
        clicks = int(row.get("product_click_uv") or 0)
        source = stats_source
    else:
        paid_amount = round(row.get("paid_amount") or 0, 2)
        refund_amount = round(row.get("refund_amount") or 0, 2)
        paid_orders = int(row.get("paid_orders") or 0)
        # 平台没给官方人数时用**自算的去重买家数**（小红书就是这样：订单里有买家标识，
        # 但平台没开放人数接口）。真的取不到（0）才显示"—"，别把"没数据"显示成 0。
        buyers = int(row.get("paid_buyers") or 0) or None
        clicks = None                     # 访客/点击任何平台都没有可自算的来源 → 不编数
        source = "订单接口聚合"
    # 下单人数：店铺级官方口径两个平台都拿不到（微信罗盘店铺概览只回 7 个字段，
    # 下单人数 create_uv 只在**商品级**接口里），所以统一用自算的去重下单人数。
    created_buyers = int(row.get("created_buyers") or 0) or None
    return {
        "stat_date": row.get("stat_date"), "platform": row.get("platform"),
        # 带上店铺：数据面板可以看"全部店铺汇总"，汇总时必须能分清每行/每天属于哪家店
        "shop_id": row.get("shop_id"), "shop_name": _shop_display_name(row.get("shop_id")),
        "paid_amount": paid_amount,
        "net_paid_amount": round(paid_amount - refund_amount, 2),
        "refund_amount": refund_amount,
        "paid_orders": paid_orders,
        "buyers": buyers, "clicks": clicks,
        "conversion_rate": round(buyers / clicks, 4) if (buyers is not None and clicks) else None,
        "created_buyers": created_buyers,
        # 下单后转化率 = 支付买家数 ÷ 下单人数。这是**下单之后**的转化，
        # 和上面那个"支付 ÷ 点击"（下单之前）不是一回事，面板上分开列、分开写口径。
        "created_conversion_rate": (round(buyers / created_buyers, 4)
                                    if (buyers is not None and created_buyers) else None),
        "avg_order_value": round(paid_amount / paid_orders, 2) if paid_orders else 0.0,
        "dealt_orders": int(row.get("dealt_orders") or 0),
        "actual_income": round(row.get("actual_income") or 0, 2),
        "actual_orders": int(row.get("actual_orders") or 0),
        "refund_rate": round(refund_amount / paid_amount, 4) if paid_amount else 0.0,
        "source": source, "stats_ready": stats_ready,
        "status_map_ready": bool(row.get("status_map_ready")),
        "notes": row.get("notes") or "", "synced_at": row.get("synced_at"),
        "sync_error": row.get("sync_error"),
    }


def _bucket_key(stat_date, granularity):
    """日期 → (分组键, 显示名)。支持 日 / 周 / 月。"""
    if granularity == "week":
        year, week, _ = datetime.strptime(stat_date, "%Y-%m-%d").isocalendar()
        return f"{year}-W{week:02d}", f"{year} 年第 {week} 周"
    if granularity == "month":
        return stat_date[:7], stat_date[:7]
    return stat_date, stat_date


def _aggregate_views(views, granularity):
    """按粒度归并（金额与笔数相加；比率一律用合计值重算，不平均百分比）。"""
    buckets = {}
    for view in views:
        key, label = _bucket_key(view["stat_date"], granularity)
        item = buckets.setdefault(key, {
            "bucket": key, "label": label, "days": 0, "platforms": [],
            "paid_amount": 0.0, "net_paid_amount": 0.0, "refund_amount": 0.0, "actual_income": 0.0,
            "paid_orders": 0, "dealt_orders": 0, "actual_orders": 0,
            "buyers": 0, "clicks": 0, "buyers_available": False, "stats_ready": False,
            # 下单人数（自算去重）单独一套可用性标记：微信拿不到 → 只有小红书那几行有数
            "created_buyers": 0, "created_buyers_available": False,
            # 归并后也要把口径说明/同步失败带上来，否则页面无法提示"这天的金额没取全"
            "notes": [], "sync_error": "", "sources": [], "shops": [],
        })
        item["days"] += 1
        if view.get("notes"):
            item["notes"].append(view["notes"])
        if view.get("sync_error"):
            item["sync_error"] = view["sync_error"]
        if view.get("source"):
            item["sources"].append(view["source"])
        shop_name = view.get("shop_name") or view.get("shop_id")
        if shop_name and shop_name not in item["shops"]:
            item["shops"].append(shop_name)
        if view["platform"] not in item["platforms"]:
            item["platforms"].append(view["platform"])
        for money in ("paid_amount", "net_paid_amount", "refund_amount", "actual_income"):
            item[money] = round(item[money] + view[money], 2)
        for counter in ("paid_orders", "dealt_orders", "actual_orders"):
            item[counter] += view[counter]
        if view["buyers"] is not None:
            item["buyers"] += view["buyers"]
            item["clicks"] += view["clicks"] or 0
            item["buyers_available"] = True
        if view.get("created_buyers") is not None:
            item["created_buyers"] += view["created_buyers"]
            item["created_buyers_available"] = True
        item["stats_ready"] = item["stats_ready"] or view["stats_ready"]
    for item in buckets.values():
        item["avg_order_value"] = round(item["paid_amount"] / item["paid_orders"], 2) if item["paid_orders"] else 0.0
        item["refund_rate"] = round(item["refund_amount"] / item["paid_amount"], 4) if item["paid_amount"] else 0.0
        item["conversion_rate"] = (round(item["buyers"] / item["clicks"], 4)
                                  if item["buyers_available"] and item["clicks"] else None)
        if not item["buyers_available"]:
            item["buyers"] = None
            item["clicks"] = None
        # 下单后转化率（支付买家 ÷ 下单人数）；拿不到下单人数就显示 —，别拿 0 充数
        item["created_conversion_rate"] = (
            round(item["buyers"] / item["created_buyers"], 4)
            if (item["buyers_available"] and item["created_buyers_available"]
                and item["created_buyers"]) else None)
        if not item["created_buyers_available"]:
            item["created_buyers"] = None
        # 同一周期里多天/多平台会带来大量重复说明，**按分句去重**再拼。
        # 只按整串去重不够：各天的说明常常是"前半句相同、后半句不同"，
        # 结果拼出来是一长串 run-on 文字（页面上实测就是这样）。
        segments = []
        for text in item["notes"]:
            for segment in str(text).split("；"):
                segment = segment.strip()
                if segment and segment not in segments:
                    segments.append(segment)
        item["notes"] = "；".join(segments)
        item["source"] = "、".join(dict.fromkeys(item["sources"]))
        # platforms 保持数组（接口契约不变），另给一个给 Excel/文案用的拼接串
        item["platform_text"] = "、".join(item["platforms"])
        item["shop_text"] = "、".join(item["shops"])
    return sorted(buckets.values(), key=lambda bucket: bucket["bucket"], reverse=True)


def _sum_views(views):
    """区间合计（用于顶部大数字与"上一周期"对比）。"""
    total = {key: 0 for key in ("paid_orders", "dealt_orders", "actual_orders")}
    for money in ("paid_amount", "net_paid_amount", "refund_amount", "actual_income"):
        total[money] = 0.0
    buyers = clicks = 0
    buyers_available = False
    created_buyers = 0
    created_buyers_available = False
    for view in views:
        for counter in ("paid_orders", "dealt_orders", "actual_orders"):
            total[counter] += view[counter]
        for money in ("paid_amount", "net_paid_amount", "refund_amount", "actual_income"):
            total[money] = round(total[money] + view[money], 2)
        if view["buyers"] is not None:
            buyers += view["buyers"]
            clicks += view["clicks"] or 0
            buyers_available = True
        if view.get("created_buyers") is not None:
            created_buyers += view["created_buyers"]
            created_buyers_available = True
    total["buyers"] = buyers if buyers_available else None
    total["clicks"] = clicks if buyers_available else None
    total["created_buyers"] = created_buyers if created_buyers_available else None
    total["avg_order_value"] = round(total["paid_amount"] / total["paid_orders"], 2) if total["paid_orders"] else 0.0
    total["refund_rate"] = round(total["refund_amount"] / total["paid_amount"], 4) if total["paid_amount"] else 0.0
    total["conversion_rate"] = round(buyers / clicks, 4) if (buyers_available and clicks) else None
    total["created_conversion_rate"] = (
        round(buyers / created_buyers, 4)
        if (buyers_available and created_buyers_available and created_buyers) else None)
    return total


def _resolve_metric_range(start, end, granularity):
    """校验日期/粒度，并算出「本周期」与「上一周期」（紧邻在前、天数相同）。

    返回 (start, end, prev_start, prev_end, span, granularity)。
    抽出来共用：页面取数与 Excel 导出必须是同一套区间口径，否则导出来的和屏幕上对不上。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    start = start or (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    end = end or today
    # 「自定义」只是界面上的"自选区间"，粒度仍然按天：归一一下，
    # 免得前端多传一个值就整页报 400（线上已发生：granularity=custom）。
    if granularity == "custom":
        granularity = "day"
    if granularity not in ("day", "week", "month"):
        raise HTTPException(400, f"granularity 只能是 day/week/month（自定义区间请用 day）：{granularity}")
    for value in (start, end):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, f"日期格式应为 YYYY-MM-DD：{value}")
    if start > end:
        raise HTTPException(400, "开始日期不能晚于结束日期")
    span = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days + 1
    prev_end = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    prev_start = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=span)).strftime("%Y-%m-%d")
    return start, end, prev_start, prev_end, span, granularity


def _shop_display_name(shop_id):
    """店铺显示名（店铺被停用/删除时退回 shop_id，不让面板因此报错）。"""
    try:
        return shop_registry.resolve(shop_id).get("name") or shop_id
    except Exception:  # noqa: BLE001
        return shop_id or ""


# 数据面板的"店铺范围"取值：全部启用店铺汇总
METRIC_SHOP_ALL = "all"


def _metric_shop_scope(request, shop=""):
    """解析数据面板的店铺范围，返回 shop_id 列表。

    · 'all'      → 全部启用店铺（面板的"全部店铺汇总"）
    · 指定 shop  → 单店（会校验店铺存在且启用）
    · 空         → 跟随请求头 X-Shop-Id（兼容旧调用）

    注意：这是**面板自己的筛选维度**，与发布/商品模块的"当前店铺"解耦——
    看全店汇总不该顺手把中台的当前店铺也切掉。
    """
    value = str(shop or "").strip()
    if not value:
        return [_current_shop_id(request)]
    if value.lower() == METRIC_SHOP_ALL:
        return [entity["shop_id"] for entity in shop_registry.all_shops(only_enabled=True)]
    return [_verify_shop(value)["shop_id"]]


def _load_order_views(shop_ids, start, end, platform=""):
    """按区间读订单日报指标（支持多店汇总），转成面板口径。

    注意用显式 close：sqlite3 连接做 with 只会提交/回滚，不会关闭连接。
    """
    shop_ids = [sid for sid in (shop_ids or []) if sid]
    if not shop_ids:
        return []
    placeholders = ",".join("?" for _ in shop_ids)
    sql = (f"SELECT * FROM order_daily_metrics WHERE shop_id IN ({placeholders})"
           " AND stat_date>=? AND stat_date<=?")
    args = [*shop_ids, start, end]
    if platform:
        sql += " AND platform=?"
        args.append(platform)
    conn = db()
    try:
        return [_metric_view(row) for row in conn.execute(sql, args)]
    finally:
        conn.close()


# —— 导出 Excel 的列定义 ————————————————————————————————————————
# 结构对齐运营给的《日销售数据统计表》：① 汇总页（周期×平台）② 每个平台一张日销售
# ③ 每个平台一张交易清单（逐笔订单）④ 口径说明。
# 三个键：列键、列名、数字格式（money/int/rate/date/datetime；None=原样写出）。
PLATFORM_DISPLAY = {"wechat": "微信小店", "xhs": "小红书"}
EXPORT_SUMMARY_COLUMNS = [
    ("seq", "序号", None), ("period", "日期", None), ("week_seg", "周段", None),
    ("platform", "平台", None), ("visits", "访客流量", "int"),
    ("create_amount", "下单金额", "money"), ("paid_amount", "支付金额", "money"),
    ("created_buyers", "下单买家数", "int"), ("paid_orders", "支付笔数", "int"),
    ("sum_paid", "汇总支付金额", "money"), ("sum_dealt", "汇总成交笔数", "int"),
    ("refund_amount", "退款金额", "money"),
    ("actual_income", "实际收入（已发货+待发货）", "money"),
    ("actual_orders", "实际笔数", "int"), ("refund_rate", "退款率", "rate"),
    ("analysis", "经营情况分析", None),
]
EXPORT_DAILY_COLUMNS = [
    ("stat_date", "日期", "date"), ("visits", "访客数", "int"),
    ("paid_amount", "支付金额", "money"), ("paid_orders", "支付订单数", "int"),
    ("refund_amount", "退款金额", "money"), ("conversion_rate", "支付转化率", "rate"),
    ("ad_cost", "推广费", "money"), ("actual_income", "实际收入", "money"),
    ("actual_orders", "实际笔数", "int"),
]
EXPORT_ORDER_COLUMNS = [
    ("seq", "序号", None), ("month_seg", "月段", None), ("week_seg", "周段", None),
    ("create_time", "订单创建时间", "datetime"), ("order_no", "订单编号", None),
    ("paid_amount", "订单金额", "money"), ("item_qty", "购买数量", "int"),
    ("item_title", "商品", None), ("status_text", "订单状态", None),
    ("refund_reason", "退款原因", None), ("remark", "备注", None),
]
EXPORT_COLUMN_WIDTHS = {
    "seq": 6, "period": 18, "week_seg": 10, "platform": 12, "visits": 12,
    "create_amount": 12, "paid_amount": 14, "created_buyers": 12, "paid_orders": 12,
    "sum_paid": 14, "sum_dealt": 14, "refund_amount": 12, "actual_income": 20,
    "actual_orders": 12, "refund_rate": 10, "analysis": 28, "stat_date": 12,
    "conversion_rate": 12, "ad_cost": 10, "month_seg": 8, "create_time": 20,
    "order_no": 24, "item_qty": 10, "item_title": 40, "status_text": 12,
    "refund_reason": 14, "remark": 16,
}
# 没有数据源的列：**保留列名但不填**（运营表里留着位置，接上数据源后自己就能补）
EXPORT_EMPTY_COLUMNS = {
    "create_amount": "下单金额：平台未开放「下单金额」口径，且订单接口只给已付金额 → 留空",
    "ad_cost": "推广费：未接广告投放数据（各平台广告后台单独导出）→ 留空",
    "analysis": "经营情况分析：留给人填写，中台不自动生成结论",
    "refund_reason": "退款原因：售后接口未返回原因字段 → 留空",
    "remark": "备注：留给人填写",
}
# 导出的「口径说明」页：每条只对应**导出里真实存在的列**，避免留下一堆没有的列名
#让人以为漏了数（之前混进了净支付金额/客单价/下单后转化率等已经不导出的指标）。
EXPORT_METRIC_NOTES = [
    ("支付金额", "微信：平台数据概览（罗盘「成交金额」）；小红书：订单接口聚合（订单详情 totalPayAmount，分→元）"),
    ("退款金额", "微信：罗盘「成交退款金额」；小红书：售后单金额（平台未公开状态码，按金额计入）"),
    ("支付笔数", "微信：罗盘「成交订单数」；小红书：当日**已支付**（有支付时间）的订单数"),
    ("访客流量 / 访客数", "仅微信罗盘「商品点击人数」；小红书没有流量接口 → 显示「—」，不编数"),
    ("下单买家数", "按订单创建时间去重买家自算（两个平台都没有店铺级的官方人数接口）"),
    ("支付买家数", "微信取罗盘「成交人数」；小红书用订单里的买家标识当天去重自算（不落库）"),
    ("支付转化率", "支付买家数 ÷ 商品点击人数（**下单之前**的转化，近似口径；小红书分母缺失 → 「—」）"),
    ("汇总支付金额 / 汇总成交笔数", "同一周期内**各平台合计**（表内已纵向合并）；"
                    "汇总成交笔数与支付笔数同口径，取各平台支付笔数相加"),
    ("实际收入 / 实际笔数", "存量口径：该日创建、**截至最近一次同步**仍未完成的订单金额与笔数。"
                    "所以只在当天/次日同步时抓得到；事后补同步会显示 0（订单早已完成或退款）"),
    ("退款率", "退款金额 ÷ 支付金额"),
    ("订单状态", "微信按官方状态码（待发货/部分发货/待收货/已完成/售后取消）；"
             "小红书未公开码表，按支付/完成/取消时间判定"),
    ("商品 / 购买数量", "取该订单的第一个商品名称与总件数（订单里含多个商品时只显示第一个）"),
    ("月段 / 周段", "月段=订单创建月份；周段按几号分旬（1~7 第一周、8~14 第二周…），仅便于分档查看"),
    ("人数去重口径", "按天去重后相加：同一个人在不同天各算一次；买家标识只用于当天内存去重，不落库、不导出"),
] + [(key, note) for key, note in EXPORT_EMPTY_COLUMNS.items()]


def _excel_date(value):
    """'YYYY-MM-DD' → datetime.date（Excel 里显示成真日期，不是文本）。"""
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return value


def _excel_datetime(value):
    """'YYYY-MM-DD HH:MM:SS' → datetime.datetime。"""
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return value


def _month_week_seg(day):
    """参考表里的「周段」：按**几号**分旬（1~7 第一周、8~14 第二周…），跨月各自从第一周起算。"""
    if not day:
        return None
    return "第%s周" % "一二三四五六"[(min(day.day, 28) - 1) // 7]


def _export_summary_rows(views, granularity):
    """汇总页的数据行：周期 × 平台一行，外加「汇总支付金额/汇总成交笔数」的合并区间。

    返回 (rows, [(列键, 起始行号, 结束行号)])；行号是**从表头下一行**开始数的相对行号，
    调用方写完表头后按 first_data_row 平移。
    """
    groups = {}
    for view in views:
        key = (_bucket_key(view["stat_date"], granularity)[0], view["platform"])
        groups.setdefault(key, []).append(view)
    buckets = sorted({bucket for bucket, _p in groups})
    rows, merge_ranges = [], []
    for index, bucket in enumerate(buckets, start=1):
        period_views = [v for (b, p), items in groups.items() if b == bucket for v in items]
        first_label = min(v["stat_date"] for v in period_views)
        last_label = max(v["stat_date"] for v in period_views)
        if granularity == "week":
            period = "%d月%d日 - %d月%d日" % (
                int(first_label[5:7]), int(first_label[8:10]),
                int(last_label[5:7]), int(last_label[8:10]))
            # 周段 = 这是导出区间里的第几个周（第一周、第二周…）
            week_seg = "第%s周" % ("一二三四五六"[index - 1] if index <= 6 else str(index))
        elif granularity == "month":
            period = first_label[:7]
            week_seg = None          # 按月导出时谈"第几周"没有意义，留空
        else:
            period = "%d月%d日" % (int(first_label[5:7]), int(first_label[8:10]))
            week_seg = None          # 同理：按天导出不能把每一天叫成"第一周"（实测踩过）
        bucket_platforms = sorted(p for (b, p) in groups if b == bucket)
        bucket_start = len(rows) + 1
        for offset, platform in enumerate(bucket_platforms):
            items = groups[(bucket, platform)]
            totals = _sum_views(items)
            visits_available = any(item["clicks"] is not None for item in items)
            rows.append({
                # 同一周期的这几列**只在第一行写值**，其余留空由调用方合并单元格
                # （参考表就是这么做的：序号/日期/周段/汇总各占一个合并格，不逐行重复）
                "seq": (len(rows) + 1 if offset == 0 else None),
                "period": (period if offset == 0 else None),
                "week_seg": (week_seg if offset == 0 else None),
                "platform": PLATFORM_DISPLAY.get(platform, platform),
                "visits": (sum(item["clicks"] or 0 for item in items) if visits_available else None),
                "create_amount": None,             # 未开放该口径 → 留空
                "paid_amount": totals["paid_amount"],
                "created_buyers": totals["created_buyers"],
                "paid_orders": totals["paid_orders"],
                "refund_amount": totals["refund_amount"],
                "actual_income": totals["actual_income"],
                "actual_orders": totals["actual_orders"],
                "refund_rate": totals["refund_rate"],
                "analysis": None,                  # 留给人填
            })
        # 跨平台合计：只在每组第一行写值，其余留空，由调用方合并单元格
        bucket_totals = _sum_views(period_views)
        rows[bucket_start - 1]["sum_paid"] = bucket_totals["paid_amount"]
        # 汇总成交笔数 = 该周期各平台**支付笔数**合计：运营确认"成交笔数"与"支付笔数"
        # 是同一回事；原先取罗盘「确认收货成交」，小红书没有这个口径恒为 0，整列全 0
        rows[bucket_start - 1]["sum_dealt"] = bucket_totals["paid_orders"]
        if len(bucket_platforms) > 1:
            for key in ("seq", "period", "week_seg", "sum_paid", "sum_dealt", "analysis"):
                merge_ranges.append((key, bucket_start, len(rows)))
    return rows, merge_ranges


# 交易清单最多拉多少个「店 × 天」：小红书补一次详情约 15 秒，不设上限的话
# 一个月的区间能把导出拖到好几分钟（前端也会先超时）。
EXPORT_ORDER_DETAIL_MAX = 20
# 缺本地明细时，实时去拉的**并发数与总超时**：导出不能被平台接口拖死，
# 超时就跳过这些天并在表里写明（宁可清单少几天，也不要让人等十分钟）。
EXPORT_DETAIL_FETCH_WORKERS = 6
EXPORT_DETAIL_FETCH_TIMEOUT = 45
# 明细面板「交易明细」的区间查询一次最多看多少天：防误选一年把请求拖爆。
# 同步过的天都是本地读（不慢）；慢的是没同步过的天，那部分走上面的并发+总超时保护。
DETAIL_RANGE_MAX_DAYS = 62


def _export_order_rows(shop_ids, platform, start, end):
    """导出用：某平台逐笔订单行。

    优先读本地已落库的明细（同步时写的，毫秒级）；只有"从没同步过的店×天"才实时
    去平台拉 —— 这类天**并发**拉、且有总超时，绝不让导出无限期等下去。
    """
    notes = []
    # 只保留**配了该平台**的店铺：否则会给没配凭证的店打接口，平台直接 400，
    # 导出里就会多出一堆"订单拉取失败"的噪音（实测踩过）。
    targets = [sid for sid in shop_ids if platform in (order_platforms(sid) or [])]
    if not targets:
        return [], []
    placeholders = ",".join("?" for _ in targets)
    conn = db()
    try:
        days = [row[0] for row in conn.execute(
            f"SELECT DISTINCT stat_date FROM order_daily_metrics WHERE shop_id IN ({placeholders})"
            " AND platform=? AND stat_date>=? AND stat_date<=? AND paid_orders>0"
            " ORDER BY stat_date", [*targets, platform, start, end])]
    finally:
        conn.close()
    pairs = [(shop_id, day) for shop_id in targets for day in days]
    if len(pairs) > EXPORT_ORDER_DETAIL_MAX:
        pairs = pairs[:EXPORT_ORDER_DETAIL_MAX]
        notes.append(f"交易清单：只取了前 {EXPORT_ORDER_DETAIL_MAX} 个「店×天」（上限），"
                     f"其余请缩小日期区间或分批导出")
    rows, notes_all, live_pairs = [], [], []
    for shop_id, day in pairs:
        local = _local_order_rows(shop_id, platform, day)
        if local:
            rows.extend(local)
        else:
            live_pairs.append((shop_id, day))
    if live_pairs:
        notes.append(f"交易清单：有 {len(live_pairs)} 个「店×天」没有本地明细（这些天没点过同步），"
                     f"正在实时向平台拉取；先点一次「同步数据」再导出即可秒出")
        deadline = time.time() + EXPORT_DETAIL_FETCH_TIMEOUT
        with ThreadPoolExecutor(max_workers=EXPORT_DETAIL_FETCH_WORKERS) as pool:
            futures = {pool.submit(_order_detail_rows, platform, shop_id, day): (shop_id, day)
                       for shop_id, day in live_pairs}
            for future in as_completed(futures, timeout=max(1, deadline - time.time())):
                shop_id, day = futures[future]
                try:
                    day_rows, day_notes = future.result()
                except Exception as exc:  # noqa: BLE001  单天失败不影响整张表
                    day_rows, day_notes = [], [f"{_shop_display_name(shop_id)}/{day} 明细拉取失败（{str(exc)[:60]}）"]
                rows.extend(day_rows)
                notes_all.extend(day_notes)
        # 超时未返回的 future 直接忽略（线程池退出时会等它们，但导出不再空等）
        if any(not future.done() for future in futures):
            notes.append(f"交易清单：{sum(1 for f in futures if not f.done())} 个「店×天」拉取超时"
                         f"（超过 {EXPORT_DETAIL_FETCH_TIMEOUT} 秒）已跳过，完整数据请点「同步数据」后重导")
    notes.extend(notes_all)
    return sorted(rows, key=lambda row: row.get("pay_ts") or row.get("create_ts") or 0,
                  reverse=True), notes


@app.get("/orders/metrics/export")
def orders_metrics_export(request: Request, start: str = Query(""), end: str = Query(""),
                          platform: str = Query(""), granularity: str = Query("day"),
                          shop: str = Query("", description="all=全部店铺汇总")):
    """导出 Excel：结构对齐运营给的《日销售数据统计表》。

    四类页：① 汇总页（周期 × 平台，含"汇总支付金额"这类跨平台合计列）
    ② 每个平台一张日销售（逐日一行）③ 每个平台一张交易清单（逐笔订单，**不带商品图**）
    ④ 口径说明。

    汇总/日销售的口径与 GET /orders/metrics 完全一致（共用 _resolve_metric_range 与
    _load_order_views），店铺范围也一样（shop=all 导出全部店铺汇总）。
    ⚠️ 交易清单要**逐笔**订单，只能实时打平台接口（带 5 分钟缓存），所以：
       只拉「当天有订单」的那些天，且总店·天数有上限，截断了会在表里写明。
    """
    shop_ids = _metric_shop_scope(request, shop)
    start, end, prev_start, prev_end, span, granularity = _resolve_metric_range(start, end, granularity)
    views = _load_order_views(shop_ids, start, end, platform)
    totals = _sum_views(views)
    scope_name = "全部店铺汇总" if len(shop_ids) > 1 else _shop_display_name(shop_ids[0])
    export_notes = []
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font
        from openpyxl.utils import get_column_letter
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"服务器缺少 openpyxl，无法导出 Excel：{exc}")

    def write_header(sheet, columns, freeze=True):
        sheet.append([column[1] for column in columns])
        header_row = sheet.max_row      # 标题行可能占了几行，表头不一定在第 1 行
        for cell in sheet[header_row]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        if freeze:
            sheet.freeze_panes = f"A{header_row + 1}"

    def write_row(sheet, columns, row, bold=False):
        # 先 append 完整一行再设格式：append([]) 这种"空行"不会推进行号，
        # 用它算 max_row 会把数据写回表头那一行（实测踩到：整张表只剩最后一行）。
        # 没有数据源的列**留空**（不写"—"）：表里干净，原因在「口径说明」页写明。
        sheet.append([row.get(key) for key, _title, _kind in columns])
        row_index = sheet.max_row
        for offset, (key, _title, kind) in enumerate(columns, start=1):
            cell = sheet.cell(row=row_index, column=offset)
            if bold:
                cell.font = Font(bold=True)
            if kind == "money":
                cell.number_format = "#,##0.00"
            elif kind == "int":
                cell.number_format = "#,##0"
            elif kind == "rate" and isinstance(cell.value, (int, float)):
                cell.number_format = "0.00%"
            elif kind == "date" and isinstance(cell.value, str):
                cell.value = _excel_date(cell.value)
                cell.number_format = "yyyy-mm-dd"
            elif kind == "datetime" and isinstance(cell.value, str):
                cell.value = _excel_datetime(cell.value)
                cell.number_format = "yyyy-mm-dd hh:mm:ss"

    def apply_widths(target_sheet, columns):
        for index, (key, _title, _kind) in enumerate(columns, start=1):
            target_sheet.column_dimensions[get_column_letter(index)].width = EXPORT_COLUMN_WIDTHS.get(key, 14)

    platforms = sorted({view["platform"] for view in views})
    workbook = openpyxl.Workbook()

    # ——————————————— ① 汇总页（周期 × 平台）—————————————————————
    # 参考表是「每周」口径；面板也能按天/按月导出，所以表名跟着粒度走，别叫"每周"却按天。
    sheet = workbook.active
    sheet.title = "每周数据统计" if granularity == "week" else "数据汇总"
    # 不加"销售数据统计 · 什么范围 · 导出时间"这类大标题：参考表没有，
    # 区间/范围/导出时间在**文件名**里已经有了（数据概览_范围_起_止_粒度.xlsx）。
    # 列定义跟着粒度走：按周导出保留「周段」；按天/按月导出时这一列没意义（实测踩过：
    # 按天导出曾把每一天标成"第一周、第二周…"），整列去掉，"日期"列也只写一个日期。
    if granularity == "week":
        summary_columns = list(EXPORT_SUMMARY_COLUMNS)
    else:
        summary_columns = [column for column in EXPORT_SUMMARY_COLUMNS if column[0] != "week_seg"]
    period_title = {"month": "月份", "day": "日期"}.get(granularity, "日期")
    summary_columns = [(key, (period_title if key == "period" else title), kind)
                       for key, title, kind in summary_columns]
    write_header(sheet, summary_columns)
    summary_rows, merge_ranges = _export_summary_rows(views, granularity)
    first_data_row = sheet.max_row + 1
    for row in summary_rows:
        write_row(sheet, summary_columns, row)
    # 同一周期的「序号 / 日期 / 周段 / 汇总金额 / 汇总笔数 / 经营情况分析」按参考表那样
    # 纵向合并成一个格（不逐行重复写"第一周"），并垂直居中
    column_index = {column[0]: index for index, column in enumerate(summary_columns, start=1)}
    merge_keys = ("seq", "period", "week_seg", "sum_paid", "sum_dealt", "analysis")
    merge_ranges = [item for item in merge_ranges if item[0] in column_index]
    for col_key, first_row, last_row in merge_ranges:
        if last_row <= first_row:
            continue
        # ⚠️ merge_ranges 里的行号是**相对第一行数据**的（1 起算），必须加上表头占的行数，
        #    否则会合并到表头上（实测踩过：表头"序号"和第一行数据一起被吞掉）。
        start_row = first_data_row + first_row - 1
        end_row = first_data_row + last_row - 1
        letter = get_column_letter(column_index[col_key])
        sheet.merge_cells(f"{letter}{start_row}:{letter}{end_row}")
        sheet[f"{letter}{start_row}"].alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True)
    # 合计行
    total_row = {"seq": None, "period": "合计", "week_seg": "", "platform": "、".join(
        PLATFORM_DISPLAY.get(p, p) for p in platforms) or "—",
        "visits": (sum(row["visits"] for row in summary_rows
                       if isinstance(row["visits"], (int, float))) or None),
        "create_amount": None, "paid_amount": totals["paid_amount"],
        "created_buyers": (sum(row["created_buyers"] for row in summary_rows
                               if isinstance(row["created_buyers"], (int, float))) or None),
        "paid_orders": totals["paid_orders"], "sum_paid": totals["paid_amount"],
        "sum_dealt": totals["paid_orders"], "refund_amount": totals["refund_amount"],
        "actual_income": totals["actual_income"], "actual_orders": totals["actual_orders"],
        "refund_rate": totals["refund_rate"], "analysis": None}
    write_row(sheet, summary_columns, total_row, bold=True)
    apply_widths(sheet, summary_columns)

    # ——————————————— ② 每个平台一张日销售 ————————————————————————
    for target in platforms:
        daily_sheet = workbook.create_sheet(f"{PLATFORM_DISPLAY.get(target, target)}-日销售")
        write_header(daily_sheet, EXPORT_DAILY_COLUMNS)
        by_day = {view["stat_date"]: view for view in views if view["platform"] == target}
        day = datetime.strptime(start, "%Y-%m-%d").date()
        last = datetime.strptime(end, "%Y-%m-%d").date()
        while day <= last:
            view = by_day.get(day.strftime("%Y-%m-%d"))
            write_row(daily_sheet, EXPORT_DAILY_COLUMNS, {
                "stat_date": day.strftime("%Y-%m-%d"),
                "visits": (view or {}).get("clicks"),
                "paid_amount": (view or {}).get("paid_amount", 0.0),
                "paid_orders": (view or {}).get("paid_orders", 0),
                "refund_amount": (view or {}).get("refund_amount", 0.0),
                "conversion_rate": (view or {}).get("conversion_rate"),
                "ad_cost": None,                       # 未接广告数据 → 留空
                "actual_income": (view or {}).get("actual_income", 0.0),
                "actual_orders": (view or {}).get("actual_orders", 0),
            })
            day += timedelta(days=1)
        apply_widths(daily_sheet, EXPORT_DAILY_COLUMNS)

    # ——————————————— ③ 每个平台一张交易清单（逐笔，不带图）——————————————
    detail_platforms = [platform] if platform else (
        [p for p in platforms] if platforms else
        sorted({p for sid in shop_ids for p in order_platforms(sid)}))
    for target in detail_platforms:
        order_sheet = workbook.create_sheet(f"{PLATFORM_DISPLAY.get(target, target)}交易清单")
        # 只留参考表那样的一行说明（原来还带了范围/区间，交给文件名和口径说明页）
        order_sheet.append(["仅统计真实成交且已支付的订单"])
        order_sheet["A1"].font = Font(bold=True, size=12)
        order_sheet.merge_cells(start_row=1, start_column=1, end_row=1,
                                end_column=len(EXPORT_ORDER_COLUMNS))
        orders, order_notes = _export_order_rows(shop_ids, target, start, end)
        export_notes.extend(order_notes)
        write_header(order_sheet, EXPORT_ORDER_COLUMNS)
        order_first_data_row = order_sheet.max_row + 1   # 数据从表头下一行开始（标题行占第 1 行）
        amount_sum = 0.0
        qty_sum = 0
        for index, row in enumerate(orders, start=1):
            created = datetime.fromtimestamp(row["create_ts"]) if row.get("create_ts") else None
            amount_sum += row.get("paid_amount") or 0.0
            qty_sum += int(row.get("item_qty") or 0)
            write_row(order_sheet, EXPORT_ORDER_COLUMNS, {
                "seq": index,
                "month_seg": f"{created.month}月" if created else None,
                "week_seg": _month_week_seg(created),
                "create_time": created.strftime("%Y-%m-%d %H:%M:%S") if created else None,
                "order_no": row.get("order_no") or None,
                "paid_amount": row.get("paid_amount") or 0.0,
                "item_qty": row.get("item_qty") or 0,
                "item_title": row.get("item_title") or None,
                "status_text": row.get("status_text") or None,
                "refund_reason": None,              # 售后接口没返回原因 → 留空
                "remark": row.get("shop_name") or None,
            })
        write_row(order_sheet, EXPORT_ORDER_COLUMNS, {
            "seq": None, "month_seg": None, "week_seg": None, "create_time": None,
            "order_no": f"合计（{len(orders)} 笔）", "paid_amount": round(amount_sum, 2),
            "item_qty": qty_sum, "item_title": None, "status_text": None,
            "refund_reason": None, "remark": None}, bold=True)
        # 月段/周段：相邻同值的单元格纵向合并成一格（对齐参考表的读法）。清单按时间
        # 倒序写，同月同周的订单必然相邻，只看相邻即可。周段要带上月份一起分组：
        # 周段跨月会重新从第一周起算，不同月的两个"第一周"不是同一段，不能并成一格。
        # 合计行、没有创建时间的行（值为 None）不参与合并。
        seg_keys = []
        for row in orders:
            created = datetime.fromtimestamp(row["create_ts"]) if row.get("create_ts") else None
            seg_keys.append((f"{created.month}月" if created else None,
                             _month_week_seg(created)))
        order_column_index = {col[0]: i for i, col in enumerate(EXPORT_ORDER_COLUMNS, start=1)}

        def merge_equal_runs(letter, position):
            """把 seg_keys 第 position 位连续相同的行合并成一个单元格。"""
            run_start = 0
            for i in range(1, len(seg_keys) + 1):
                head = seg_keys[run_start][position]
                same = (i < len(seg_keys) and seg_keys[i][position] == head
                        and (position == 0 or seg_keys[i][0] == seg_keys[run_start][0]))
                if same:
                    continue
                if head is not None and i - run_start > 1:
                    top = order_first_data_row + run_start
                    order_sheet.merge_cells(
                        f"{letter}{top}:{letter}{order_first_data_row + i - 1}")
                    order_sheet[f"{letter}{top}"].alignment = Alignment(
                        horizontal="center", vertical="center")
                run_start = i

        merge_equal_runs(get_column_letter(order_column_index["month_seg"]), 0)
        merge_equal_runs(get_column_letter(order_column_index["week_seg"]), 1)
        apply_widths(order_sheet, EXPORT_ORDER_COLUMNS)

    # ——————————————— ④ 口径说明 ————————————————————————
    note_sheet = workbook.create_sheet("口径说明")
    write_header(note_sheet, [("metric", "指标 / 列", None), ("note", "口径 / 来源", None)], freeze=False)
    for metric, note in EXPORT_METRIC_NOTES:
        note_sheet.append([metric, note])
    for extra in export_notes:
        note_sheet.append(["导出说明", extra])
    note_sheet.column_dimensions["A"].width = 24
    note_sheet.column_dimensions["B"].width = 90

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    filename = f"dashboard-{start}_{end}-{granularity}.xlsx"
    return StreamingResponse(iter([output.read()]),
                             media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename={filename}"})


def _xhs_item_image_map(shop_id):
    """小红书商品 ID -> 主图。

    为什么不能单查：小红书**没有**“按 itemId 查单品”的接口——
    实测 `GET /items/{item_id}` 会**忽略路径参数**、直接把商品列表 `itemDetailV3s` 整包返回，
    所以单查必然拿不到图（就是之前小红书全“无图”的真因）。
    可商品列表的每条里就带 `id`(=itemId) 与 `images`(主图数组)，
    拉一次全量建映射即可（店内商品实测 155 条），结果按天落本地缓存。
    """
    cache_path = os.path.join(BASE_DIR, "_cache_xhs_item_images.json")
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(cache_path, encoding="utf-8") as fp:
            stored = json.load(fp) or {}
    except Exception:  # noqa: BLE001
        stored = {}
    cached = stored.get(shop_id) or {}
    if cached.get("day") == today and isinstance(cached.get("map"), dict) and cached["map"]:
        return cached["map"], ""
    base = _order_api_base("xhs")
    page_size = 100
    mapping = {}
    for page in range(1, XHS_ITEM_IMAGE_MAX_PAGES + 1):
        result = (_http_get_json(f"{base}/items?page_no={page}&page_size={page_size}",
                                 shop_id=shop_id).get("result") or {})
        batch = result.get("itemDetailV3s") or result.get("itemList") or []
        for item in batch:
            item_id = str(item.get("id") or item.get("itemId") or "")
            images = item.get("images") or item.get("imageList") or []
            first = images[0] if images else ""
            url = str(first.get("url") if isinstance(first, dict) else first or "")
            if item_id and url:
                mapping[item_id] = url
        total = int(result.get("total") or 0)
        if len(batch) < page_size or (total and page * page_size >= total):
            break
    stored[shop_id] = {"day": today, "count": len(mapping), "map": mapping}
    try:
        with open(cache_path, "w", encoding="utf-8") as fp:
            json.dump(stored, fp, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass
    return mapping, "" if mapping else "小红书商品列表为空，未能建立商品图映射"


@app.get("/orders/product-image")
def order_product_image(request: Request, platform: str = Query(..., description="wechat / xhs"),
                        product_id: str = Query(..., description="平台商品ID"),
                        shop: str = Query("", description="店铺范围"),
                        shop_id: str = Query("", description="该商品所属店铺（最准）")):
    """商品头图（数据面板「商品明细」的商品图列用）。

    为什么走平台接口而不是我们自己的图片缓存：商品维度的 product_id 是**平台商品ID**，
    与货盘里的本地图片路径没有可靠对应关系（同一商品可能由不同批次/不同店发布）。
    结果落本地缓存文件 —— 同一个商品只调一次平台接口，之后直接读缓存。
    取不到时返回空 url（前端显示占位图），不报错、不阻塞面板。
    """
    if not platform_registry.is_platform(platform):
        raise HTTPException(400, f"未知平台：{platform}")
    product_id = str(product_id or "").strip()
    if not product_id:
        return {"ok": True, "result": {"url": "", "source": "无商品ID"}}
    # 取图必须用**这件商品所属的店**的凭证：同一平台下多家店的商品互不通用，
    # 用别的店去查会拿不到图（实测：取图回退到“商品列表里没有这个商品ID”，
    # 因为被选中的是另一家店，它的商品列表里没有这个 item）。
    # 优先用前端传来的行内 shop_id，其次才是“范围内第一个搭了该平台的店”。
    explicit_shop = str(shop_id or "").strip()
    candidates = [sid for sid in _metric_shop_scope(request, shop)
                  if shop_registry.has_platform(sid, platform)]
    shop_id = (explicit_shop or (candidates[0] if candidates else "")
               or _current_shop_id(request))
    cache_path = os.path.join(BASE_DIR, "_cache_product_images.json")
    cache = {}
    try:
        with open(cache_path, encoding="utf-8") as fp:
            cache = json.load(fp)
    except Exception:  # noqa: BLE001
        cache = {}
    key = f"{shop_id}|{platform}|{product_id}"
    cached = cache.get(key)
    if isinstance(cached, dict) and cached.get("url"):
        return {"ok": True, "result": cached | {"cached": True}}
    # 只有真正取到图才算缓存命中。失败条目仅短期抑制重复请求，过期即重试：
    # 老逻辑把失败也当永久缓存，于是"取图那一刻上游服务恰好在重启"会被永久固化，面板一直无图。
    if isinstance(cached, dict) and (
            time.time() - float(cached.get("ts") or 0) < IMAGE_FAILURE_RETRY_SECONDS):
        return {"ok": True, "result": cached | {"cached": True}}
    url, note = "", ""
    try:
        base = _order_api_base(platform)
        if platform == "wechat":
            detail = (_http_get_json(f"{base}/products/{urllib.parse.quote(product_id)}",
                                     shop_id=shop_id).get("result") or {})
            images = (detail.get("product") or {}).get("head_imgs") or []
            url = str(images[0]) if images else ""
            note = "" if url else "平台未返回头图"
        else:
            mapping, map_note = _xhs_item_image_map(shop_id)
            url = mapping.get(product_id, "")
            note = map_note or ("" if url else
                    "商品列表里没有这个商品ID（订单里的 ID 可能是 SKU id）")
    except Exception as exc:  # noqa: BLE001
        note = f"取图失败：{str(exc)[:80]}"
    entry = {"url": url, "note": note, "product_id": product_id, "platform": platform}
    # 失败的条目带 ts 落盘（短期不再打接口），成功的条目不带 —— 命中判定见函数开头
    cache[key] = entry if url else {**entry, "ts": time.time()}
    try:
        with open(cache_path, "w", encoding="utf-8") as fp:
            json.dump(cache, fp, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "result": entry}


def _product_stats_note(rows):
    """给「商品明细」面板写一句口径说明（按平台分别说清访客/转化从哪来）。"""
    platforms = []
    for row in rows:
        if row["platform"] not in platforms:
            platforms.append(row["platform"])
    parts = []
    for platform in platforms:
        try:
            label = platform_registry.platform_label(platform)
        except Exception:  # noqa: BLE001
            label = platform
        if _supports_stats(platform):
            parts.append(f"{label}：访客/下单/转化取自平台商品数据（官方标注仅历史数据，当天要 T+1）")
        else:
            parts.append(f"{label}：平台未开放商品级流量接口，访客/下单/转化显示「—」")
    if not parts:
        parts.append("访客/下单/转化来自平台商品数据接口（仅历史数据，当天要 T+1）")
    return "；".join(parts)


@app.get("/orders/details")
def orders_details(request: Request, date: str = Query("", description="YYYY-MM-DD；单日模式"),
                   start: str = Query("", description="YYYY-MM-DD；区间模式开始（与 end 至少给一个，优先于 date）"),
                   end: str = Query("", description="YYYY-MM-DD；区间模式结束"),
                   platform: str = Query("", description="wechat / xhs；不传=该店已配的全部平台"),
                   shop: str = Query("", description="店铺范围：all=全部店铺汇总；不传=跟随当前店铺"),
                   keyword: str = Query("", description="按订单编号/订单号模糊搜"),
                   limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0)):
    """【商品明细】订单级明细（面板下方那张表的数据源）：支持单日（date）或区间（start/end）。

    和 /orders/metrics 的区别：那个是按天聚合的日报，这个是逐笔订单。
    **本地优先**：同步过的天直接读 SQLite（毫秒级）；没同步过的天才实时打平台接口
    （带 5 分钟内存缓存，区间模式下并发拉 + 总超时，超时的天跳过并在 notes 写明）。
    区间最长 62 天，超出自动截断并在 notes 里说明。
    店铺范围沿用面板自己的 shop 参数（'all'=全部店铺汇总），与顶栏当前店铺解耦。
    """
    stat_date = date or datetime.now().strftime("%Y-%m-%d")
    try:
        datetime.strptime(stat_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"日期格式应为 YYYY-MM-DD：{stat_date}")
    if platform and not platform_registry.is_platform(platform):
        raise HTTPException(400, f"未知平台：{platform}")
    shop_ids = _metric_shop_scope(request, shop)
    rows, notes = [], []
    # 显式筛了平台、但某家店压根没配这个平台 → 跳过该店（下面写进 notes 说明原因）
    skipped_shops = []
    # 区间模式（start/end 任传一个即生效；只传一个就当单日查）
    range_mode = bool(start or end)
    range_first = range_last = stat_date
    if range_mode:
        first = start or end
        last = end or start
        try:
            first_day = datetime.strptime(first, "%Y-%m-%d").date()
            last_day = datetime.strptime(last, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, f"日期格式应为 YYYY-MM-DD：{first} ~ {last}")
        if first_day > last_day:
            first_day, last_day = last_day, first_day
        if (last_day - first_day).days + 1 > DETAIL_RANGE_MAX_DAYS:
            last_day = first_day + timedelta(days=DETAIL_RANGE_MAX_DAYS - 1)
            notes.append(f"区间最长 {DETAIL_RANGE_MAX_DAYS} 天，已截断到 {last_day.strftime('%Y-%m-%d')}")
        range_first = first_day.strftime("%Y-%m-%d")
        range_last = last_day.strftime("%Y-%m-%d")
        days = [(first_day + timedelta(days=off)).strftime("%Y-%m-%d")
                for off in range((last_day - first_day).days + 1)]
        live = []
        for shop_id in shop_ids:
            targets = _detail_targets(shop_id, platform)
            if platform and not targets:
                skipped_shops.append(_shop_display_name(shop_id))
                continue
            for target in targets:
                for day in days:
                    local = _local_order_rows(shop_id, target, day)
                    if local:
                        rows.extend(local)
                    else:
                        live.append((target, shop_id, day))
        if live:
            notes.append(f"区间里有 {len(live)} 个「店×平台×天」没同步过明细，正在实时向平台拉取；"
                         f"先点「同步数据」再查可秒出")
            deadline = time.time() + EXPORT_DETAIL_FETCH_TIMEOUT
            with ThreadPoolExecutor(max_workers=EXPORT_DETAIL_FETCH_WORKERS) as pool:
                futures = {pool.submit(_order_detail_rows, target, shop_id, day): (target, shop_id, day)
                           for target, shop_id, day in live}
                for future in as_completed(futures, timeout=max(1, deadline - time.time())):
                    try:
                        day_rows, day_notes = future.result()
                    except Exception as exc:  # noqa: BLE001  单天失败不影响整张表
                        target, shop_id, day = futures[future]
                        day_rows, day_notes = [], [f"{_shop_display_name(shop_id)}/{target}/{day}"
                                                   f" 拉取失败（{_http_error_message(exc)[:80]}）"]
                    rows.extend(day_rows)
                    notes.extend(day_notes)
            # 超时未返回的天直接跳过（线程池退出时会等它们，但查询不再空等）
            pending = sum(1 for f in futures if not f.done())
            if pending:
                notes.append(f"有 {pending} 个「店×平台×天」实时拉取超时已跳过，"
                             f"点「同步数据」后重查可补全")
        # 各天各自倒序，拼起来不保证全局有序：统一按支付/创建时间倒序
        rows.sort(key=lambda row: row.get("pay_ts") or row.get("create_ts") or 0, reverse=True)
    else:
        for shop_id in shop_ids:
            targets = _detail_targets(shop_id, platform)
            if platform and not targets:
                skipped_shops.append(_shop_display_name(shop_id))
                continue
            for target in targets:
                day_rows, day_notes = _order_detail_rows(target, shop_id, stat_date)
                rows.extend(day_rows)
                notes.extend(day_notes)
    if skipped_shops:
        notes.append(f"{'、'.join(skipped_shops)} 未配置{platform_registry.platform_label(platform)}凭证，"
                     f"已跳过（在「店铺管理」补好凭证后直接可查，不必先同步）")
    keyword = str(keyword or "").strip()
    if keyword:
        low = keyword.lower()
        rows = [row for row in rows
                if low in row["order_no"].lower() or low in row["order_id"].lower()
                or low in str(row["shop_name"]).lower()]
    total = len(rows)
    page = rows[offset:offset + limit]
    return {"ok": True, "result": {
        "date": stat_date, "start": range_first, "end": range_last,
        "rows": page, "total": total,
        "limit": limit, "offset": offset,
        "platforms": sorted({row["platform"] for row in rows}),
        "shops": sorted({row["shop_id"] for row in rows}),
        "notes": list(dict.fromkeys(notes)),
        "amount_unit": ORDER_AMOUNT_UNIT,
        # 实付金额合计只统计当前这一页之外的**全量**，前端用来对一下总额对不对
        "sum_paid_amount": round(sum(row["paid_amount"] for row in rows), 2),
    }}


@app.get("/orders/product-metrics")
def orders_product_metrics(request: Request, start: str = Query(""), end: str = Query(""),
                           platform: str = Query(""), shop: str = Query(""),
                           keyword: str = Query(""), sort: str = Query("amount"),
                           limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    """商品明细：把总览的支付金额/件数按**单个商品**拆开（数据面板下方那块）。

    · 金额按订单实付等比分摊到商品（见 _aggregate_product_day），因此各商品之和 = 总览支付金额；
    · 访客/转化/商品级退款取自平台商品数据接口（微信罗盘；小红书无此能力 → 整列显示「—」）；
      「商品访客数」用的是罗盘的**商品点击人数**（平台没有"详情页访客数"这个口径）；
    · 支持按名称/编码搜索、按金额/件数/订单数排序，返回占比与上一周期对比。
    """
    shop_ids = _metric_shop_scope(request, shop)
    start, end, prev_start, prev_end, span, _gran = _resolve_metric_range(start, end, "day")

    def load(begin, finish):
        placeholders = ",".join("?" for _ in shop_ids)
        sql = (f"SELECT MIN(shop_id) AS shop_id, platform, product_id, sku_id,"
               f" product_name, sku_code,"
               f" MAX(image_url) AS image_url,"
               f" SUM(paid_qty) AS paid_qty, ROUND(SUM(paid_amount), 2) AS paid_amount,"
               f" SUM(order_count) AS order_count,"
               f" SUM(product_click_uv) AS product_click_uv,"
               f" SUM(product_click_cnt) AS product_click_cnt,"
               f" SUM(product_create_uv) AS product_create_uv,"
               f" SUM(product_pay_uv) AS product_pay_uv,"
               f" SUM(product_pay_cnt) AS product_pay_cnt,"
               f" ROUND(SUM(product_refund_amount), 2) AS product_refund_amount,"
               f" MAX(stats_source) AS stats_source FROM product_daily_metrics"
               f" WHERE shop_id IN ({placeholders}) AND stat_date>=? AND stat_date<=?")
        args = [*shop_ids, begin, finish]
        if platform:
            sql += " AND platform=?"
            args.append(platform)
        # 必须 GROUP BY：否则 SQLite 会把区间内所有商品聚成一行（实测踩过：2 个商品显示成 1 个）
        sql += " GROUP BY platform, product_id, sku_id, product_name, sku_code"
        conn = db()
        try:
            return [dict(row) for row in conn.execute(sql, args)]
        finally:
            conn.close()

    def merge(raw_rows):
        """按「平台 + 商品」合并（同商品多 SKU 合并成一行；无 product_id 时退回用 sku_id 当键）。"""
        merged = {}
        for row in raw_rows:
            key = (row["platform"], row.get("product_id") or row.get("sku_id") or "")
            item = merged.get(key)
            if item is None:
                item = merged[key] = {
                    "shop_id": row.get("shop_id") or "",
                    "platform": row["platform"], "product_id": row.get("product_id") or "",
                    "product_name": "", "sku_code": "", "sku_count": 0,
                    "image_url": row.get("image_url") or "",
                    "paid_qty": 0, "paid_amount": 0.0, "order_count": 0,
                    "product_click_uv": 0, "product_click_cnt": 0,
                    "product_create_uv": 0,
                    "product_pay_uv": 0, "product_pay_cnt": 0,
                    "product_refund_amount": 0.0, "stats_source": "",
                }
            item["paid_qty"] += int(row.get("paid_qty") or 0)
            item["paid_amount"] = round(item["paid_amount"] + float(row.get("paid_amount") or 0), 2)
            item["order_count"] += int(row.get("order_count") or 0)
            # 平台商品级数据：写库时只挂在同商品的一行上（见 _attach_product_stats），
            # 这里求和不会重复计数；SKU 行若也带了值，求和的语义就是"该商品各口径之和"。
            for stat_key in ("product_click_uv", "product_click_cnt", "product_create_uv",
                             "product_pay_uv", "product_pay_cnt"):
                item[stat_key] += int(row.get(stat_key) or 0)
            item["product_refund_amount"] = round(
                item["product_refund_amount"] + float(row.get("product_refund_amount") or 0), 2)
            if not item["stats_source"] and row.get("stats_source"):
                item["stats_source"] = row["stats_source"]
            if row.get("product_id"):
                item["sku_count"] += 1
            if not item["product_name"] and row.get("product_name"):
                item["product_name"] = row["product_name"]
            if not item["sku_code"] and row.get("sku_code"):
                item["sku_code"] = row["sku_code"]
        return list(merged.values())

    rows = merge(load(start, end))
    prev_index = {(row["platform"], row["product_id"] or ""): row for row in merge(load(prev_start, prev_end))}
    total_amount = round(sum(row["paid_amount"] for row in rows), 2)
    total_qty = sum(row["paid_qty"] for row in rows)

    keyword = str(keyword or "").strip().lower()
    if keyword:
        rows = [row for row in rows
                if keyword in str(row["product_name"]).lower()
                or keyword in str(row["sku_code"]).lower()
                or keyword in str(row["product_id"]).lower()]
    sorters = {
        "amount": lambda row: -row["paid_amount"],
        "qty": lambda row: -row["paid_qty"],
        "orders": lambda row: -row["order_count"],
        "name": lambda row: str(row["product_name"]),
    }
    rows.sort(key=sorters.get(sort) or sorters["amount"])

    for row in rows:
        prev = prev_index.get((row["platform"], row["product_id"] or "")) or {}
        row["share"] = round(row["paid_amount"] / total_amount, 4) if total_amount else 0.0
        row["prev_amount"] = prev.get("paid_amount", 0.0)
        row["prev_qty"] = prev.get("paid_qty", 0)
        prev_amount = row["prev_amount"]
        row["delta"] = (round((row["paid_amount"] - prev_amount) / prev_amount, 4)
                        if prev_amount else None)
        # 平台商品级流量/转化：只有统计接口有返回(stats_source 非空)时才算数，
        # 否则是 0 —— 前端必须靠 stats_source 判断"显示 —"还是"显示 0"。
        row["visits_available"] = bool(row.get("stats_source"))
        row["visits"] = int(row.get("product_click_uv") or 0)
        row["clicks"] = row["visits"]
        # 下单人数：点击之后、支付之前那一环（罗盘「下单人数」，下单未支付也算）
        row["create_uv"] = int(row.get("product_create_uv") or 0)
        row["buyers"] = int(row.get("product_pay_uv") or 0)
        row["conversion_rate"] = (round(row["buyers"] / row["visits"], 4)
                                  if row["visits"] else None)
        row["refund_amount_stats"] = round(float(row.get("product_refund_amount") or 0), 2)
    stats_note = _product_stats_note(rows)
    return {"ok": True, "result": {
        "range": {"start": start, "end": end, "days": span},
        "prev_range": {"start": prev_start, "end": prev_end, "days": span},
        "rows": rows[offset:offset + limit],
        "product_total": len(rows),
        "stats_note": stats_note,
        "totals": {"paid_amount": total_amount, "paid_qty": total_qty,
                   "order_count": sum(row["order_count"] for row in rows),
                   "products": len(rows),
                   "visits": sum(row["visits"] for row in rows if row["visits_available"]),
                   "create_uv": sum(row["create_uv"] for row in rows if row["visits_available"]),
                   "buyers": sum(row["buyers"] for row in rows if row["visits_available"])},
        "prev_totals": {"paid_amount": round(sum(row["prev_amount"] for row in rows), 2),
                        "paid_qty": sum(row["prev_qty"] for row in rows)},
        "sort": sort if sort in sorters else "amount",
        "amount_unit": ORDER_AMOUNT_UNIT,
        "refund_split": False,
    }}


@app.get("/orders/metrics")
def orders_metrics(request: Request, start: str = Query(""), end: str = Query(""),
                   platform: str = Query(""), granularity: str = Query("day"),
                   shop: str = Query("", description="all=全部店铺汇总；不传=跟随当前店铺")):
    """读数据面板指标（日期区间 / 粒度 / 平台 / 店铺范围）。纯本地表读，不打平台。

    · 口径：平台自带数据概览（微信罗盘）优先，缺失时用订单接口聚合兜底，每行都带 source；
    · 同时返回「上一周期」（紧邻的等长区间）合计，供前端算涨跌幅；
    · granularity 支持 day / week / month；
    · shop 是**面板自己的**店铺范围（all=全部店铺汇总），与中台"当前店铺"解耦。
    """
    shop_ids = _metric_shop_scope(request, shop)
    start, end, prev_start, prev_end, span, granularity = _resolve_metric_range(start, end, granularity)
    views = _load_order_views(shop_ids, start, end, platform)
    prev_views = _load_order_views(shop_ids, prev_start, prev_end, platform)
    placeholders = ",".join("?" for _ in shop_ids)
    conn = db()
    try:
        last_row = conn.execute(
            "SELECT MAX(synced_at) AS last_synced_at, COUNT(DISTINCT stat_date) AS days "
            f"FROM order_daily_metrics WHERE shop_id IN ({placeholders})", tuple(shop_ids)).fetchone()
    finally:
        conn.close()
    platforms = sorted({p for sid in shop_ids for p in order_platforms(sid)})
    stats_platforms = sorted({
        p for p in platform_registry.adapters_with("stats")
        if any(shop_registry.has_platform(sid, p) for sid in shop_ids)
    })
    return {"ok": True, "result": {
        "granularity": granularity,
        "range": {"start": start, "end": end, "days": span},
        "prev_range": {"start": prev_start, "end": prev_end, "days": span},
        # 面板的店铺范围（前端下拉用它回显，避免只靠请求参数猜）
        "scope": {"all": len(shop_ids) > 1,
                  "shops": [{"shop_id": sid, "name": _shop_display_name(sid)} for sid in shop_ids]},
        "rows": _aggregate_views(views, granularity),
        "totals": _sum_views(views),
        "prev_totals": _sum_views(prev_views),
        "daily": sorted(views, key=lambda view: (view["stat_date"], view["shop_id"], view["platform"])),
        # 上一周期的逐日值：趋势图要画"本周期 vs 上一周期"两条线，没有它第二条线只能画成 0
        "prev_daily": sorted(prev_views, key=lambda view: (view["stat_date"], view["shop_id"], view["platform"])),
        "platforms": platforms,
        "stats_platforms": stats_platforms,
        "configured": {p: any(shop_registry.has_platform(sid, p) for sid in shop_ids)
                       for p in platform_registry.all_platforms()},
        "amount_unit": ORDER_AMOUNT_UNIT,
        "last_synced_at": (last_row["last_synced_at"] if last_row else None),
        "synced_days": (last_row["days"] if last_row else 0),
    }}


@app.get("/publish-status")
def publish_status(request: Request):
    """【发布状态】按商品编码汇总**当前店铺所有批次**的发布记录。

    为什么按商品、而不是按批次:
      运营的常见做法是"重新导入一次货盘表再发", 而每次导入都会生成一个新批次。
      如果按 batch_id 统计, 重新导入后"已经发过"的记录全部消失, 又会显示成 250 件
      未发布, 于是再重复发一遍 —— 平台对重复标题直接拒绝(-5000500 标题不能与现有商品标题重复)。
      商品编码跨批次稳定, 所以这里全局汇总, 换批次也不会丢。

    只统计"有意义的终态": success/partial 表示确实发出去过; queued/running 表示正在发;
    failed 表示发过但失败; cancelled 是被手动停掉的, 不算发过(前端按 none 处理);
    deleted 是核对后发现平台商品已被删除的, 同样按 none 处理(可以重新发)。

    **模式隔离**：默认只统计 ``mode='live'`` 的记录（mock 是假成功，不能当已发布用），
    要看模拟记录加 ``?mode=mock``，全都要加 ``?mode=all``。

    返回: {商品编码: {"platforms": {平台: {状态: 条数}}, "last_success_at": ISO时间}}
    """
    shop_id = _current_shop_id(request)
    mode_where, mode_args = _mode_where(request)
    conn = db()
    rows = conn.execute("SELECT product_code, platform, status, COUNT(*) n, MAX(finished_at) last_at "
                        "FROM publish_items WHERE shop_id=?" + mode_where +
                        " GROUP BY product_code, platform, status",
                        (shop_id, *mode_args)).fetchall()
    conn.close()
    result = {}
    for row in rows:
        entry = result.setdefault(row["product_code"], {"platforms": {}, "last_success_at": None})
        entry["platforms"].setdefault(row["platform"], {})[row["status"]] = row["n"]
        if row["status"] == "success" and row["last_at"]:
            if not entry["last_success_at"] or row["last_at"] > entry["last_success_at"]:
                entry["last_success_at"] = row["last_at"]
    # mode 回显：前端要据此标注"这批是模拟记录"，避免把 mock 当真实发布
    return {"ok": True, "result": result, "mode": mode_args[0] if mode_args else "live"}


# ==================================================================
# 核对发布状态: 本地"已发布"记录 vs 平台真实商品
# ==================================================================
ITEM_EXISTS = "exists"    # 平台还有该商品
ITEM_GONE = "gone"        # 平台明确表示不存在/已删除
ITEM_UNKNOWN = "unknown"  # 查不动(网络/鉴权/其它), 不能据此改状态

# "商品不存在"的判定特征(已对官方文档):
#   微信 获取商品: errcode 10020052 = 商品不存在; product.status=6 = 回收站(后台删除后落到这里)
#   小红书 2026-09-16 实测: 查已删除商品返回
#       {"detail":"[product.getItemInfo] 调用失败: error_code=-5000500 item not exist"}
#     即错误码 -5000500 + 文案 "item not exist"。
#     ⚠️ 这里仍然**只按文案**匹配、不认错误码: -5000500 在微信侧是"标题重复"，
#        疑似是跨平台的通用码；只认数字码可能把"标题重复"误判成"商品已删除"。
#        文案 "item not exist" 已经能精确命中这个场景，更安全。
# 注意别用过于宽泛的词(如裸的"不存在"): 微信的"运费模板不存在""类目不存在"等也会命中,
# 会把在售商品误判成已删除, 所以微信只认自己的错误码和明确提到商品的文案。
# ⚠️ 小红书同理, 而且更要收紧: "not found" 正是 Starlette/FastAPI 默认 404 的原文({"detail":"Not Found"}),
#   一旦把它当删除信号, 权限/参数/路径类报错都会命中 →
#   批量把 success 改 deleted → 前端变"未发布" → 幂等保护放行 → 运营重发 → 平台上出现重复商品。
#   所以这里只认"明确指向商品"的文案; 拿不准一律 unknown(状态不变)。
# 实际取值已收敛到 platform_registry 的 gone_markers（各平台一轮核对后只需改注册表）；
# 下面两个名字保留为旧引用/文档锚点。
_WECHAT_GONE_MARKERS = platform_registry.adapter("wechat")["gone_markers"]
_XHS_GONE_MARKERS = platform_registry.adapter("xhs")["gone_markers"]


def _http_error_detail(exc):
    """取 HTTPError 响应体里的 detail —— 两个平台服务都用 FastAPI, 真实错误码在 detail 里。"""
    try:
        raw = exc.read().decode("utf-8")
        return str(json.loads(raw).get("detail") or raw)[:500]
    except Exception:
        return str(exc)[:500]


def _http_error_message(exc):
    """异常 → 能给运营看的文案。

    ⚠️ urllib 的 HTTPError 直接 ``str()`` 只有 "HTTP Error 400: Bad Request"，
    真实原因（"店铺 xx 未配置微信小店凭证"之类）在响应体的 detail 里。
    直接把 str(exc) 写进面板，看的人只知道是个 400，完全不知道错在哪（实测踩过）。
    """
    if isinstance(exc, urllib.error.HTTPError):
        return f"平台服务返回 {exc.code}：{_http_error_detail(exc)}"
    return str(exc)


def platform_item_state(platform, product_id, shop_id=None):
    """核对该商品在平台上还在不在, 返回 (state, 说明)。

    只有平台**明确**说商品不存在/已删除时才返回 gone; 网络超时、连接失败、鉴权过期
    一律返回 unknown 并保留原状态 —— 否则一次网络抖动就会把在售商品标成"未发布",
    诱导运营重复发布(平台会以「标题重复」拒绝), 反而更麻烦。

    详情路径来自适配器；**响应体判定按平台分别写**（各平台"已删除"的表示方式不同），
    未实现分支的平台返回 unknown（不下结论），不会误判成"已删除"。
    """
    if not platform_registry.is_platform(platform):
        return ITEM_UNKNOWN, f"不支持的平台：{platform}"
    url = platform_registry.item_url(platform, product_id)
    try:
        data = _http_get_json(url, timeout=30, shop_id=shop_id)
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        markers = platform_registry.adapter(platform)["gone_markers"]
        lowered = str(detail).lower()
        if any(marker.lower() in lowered for marker in markers):
            return ITEM_GONE, detail
        return ITEM_UNKNOWN, detail
    except Exception as exc:
        return ITEM_UNKNOWN, f"查询失败：{exc}"
    if platform == "wechat":
        product = (data.get("result") or {}).get("product") or {}
        if not product.get("product_id"):
            return ITEM_GONE, "平台未返回商品数据"
        if str(product.get("status")) == "6":
            return ITEM_GONE, "商品在平台回收站（后台已删除）"
        return ITEM_EXISTS, ""
    if platform == "xhs":
        # 小红书: 200 也要看返回体。商品被后台删掉后, 网关很可能回 200 + 空 result 而不是报错,
        # 只看"接口有没有报错"会把已删商品一直当成"还在", 核对等于没做(小红书又是失败/删除的重灾区)。
        # ⚠️ 该判定尚未实测: 请拿一个确实已在后台删掉的小红书 itemId 调一次 GET /items/{id},
        #    把真实返回(是报错? 空 result? 还是别的结构)补记到本注释, 再确认这条判定可靠。
        result = data.get("result")
        if data.get("ok") is False:
            # 200 但业务失败 → 不下结论(保留原状态)
            return ITEM_UNKNOWN, f"平台返回业务失败：{str(data.get('detail') or data.get('message') or data)[:200]}"
        if not result:
            # 空 result: 平台确实没给商品数据 → 判定已删除(把原始返回带进 error, 便于事后复盘)
            return ITEM_GONE, f"平台未返回商品数据：{str(data)[:200]}"
        if not isinstance(result, dict):
            return ITEM_UNKNOWN, f"返回结构无法识别：{str(result)[:200]}"
        if not (result.get("itemInfo") or result.get("itemId") or result.get("item_id") or result.get("id")):
            # 有返回但结构不认识 → 不结论(宁可漏判, 也不要把在售商品误标成未发布)
            return ITEM_UNKNOWN, f"返回结构无法识别：{str(result)[:200]}"
        return ITEM_EXISTS, ""
    # 未实现判定分支的平台: 不下结论(返回 unknown 比误判"已删除"安全得多)
    return ITEM_UNKNOWN, f"平台 {platform} 的发布状态判定尚未实现"


class VerifyPublishStatusBody(BaseModel):
    product_codes: list[str] | None = None  # 不传=核对全部有成功记录的商品
    mode: str = "verify"                    # verify=按平台核对; reset=人工确认已删除, 直接改回未发布


@app.post("/publish-status/verify")
def verify_publish_status(body: VerifyPublishStatusBody, request: Request):
    """【核对发布状态】把本地"已发布"记录拿去平台核一遍, 已删掉的改回"未发布"。

    为什么需要: 运营会在平台后台把商品删掉, 但本地 publish_items 还留着 success,
    step3 就一直显示"已发布" —— 既看着不对, 又会撞上发布接口的幂等保护(同一批次
    同商品已有记录就不再创建), 导致删完后想重发却发不出去。

    处理方式: 平台确认不存在的记录状态改成 deleted(前端按"未发布"处理, 且幂等保护放行);
    查不动的保持原状(unknown), 并在返回里提示, 由运营用 mode=reset 人工兜底。
    """
    shop_id = _current_shop_id(request)
    conn = db()
    # ⚠️ 固定只看 live：mock 记录里的 platform_product_id 是本地算出来的假 ID，
    #    拿它去真实网关核对必然"查不到"，会被判成 gone 并把记录改成 deleted ——
    #    一个纯本地动作就能把模拟记录"核销"掉，之后再也分不清哪些是模拟的。
    sql = ("SELECT id, product_code, platform, platform_product_id, finished_at FROM publish_items "
           "WHERE shop_id=? AND mode='live' AND status IN ('success','partial') "
           "AND COALESCE(platform_product_id,'')<>''")
    params = [shop_id]
    if body.product_codes:
        codes = list({str(code) for code in body.product_codes})
        sql += f" AND product_code IN ({','.join('?' for _ in codes)})"
        params.extend(codes)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    # 同一商品+平台可能有多条历史成功记录: 只拿最新那条的 platform_product_id 去平台核对
    latest = {}
    for row in rows:
        key = (row["product_code"], row["platform"])
        if key not in latest or str(row["finished_at"] or "") > str(latest[key]["finished_at"] or ""):
            latest[key] = row
    targets = list(latest.values())
    if not targets:
        return {"ok": True, "result": {"checked": 0, "deleted": 0, "exists": 0, "unknown": 0, "products": [], "unknown_items": []}}
    states = {}
    if body.mode == "reset":
        # 人工兜底: 运营自己确认这些商品在后台删了, 不再请求平台(也用于小红书查不出的情况)
        for row in targets:
            states[(row["product_code"], row["platform"])] = (ITEM_GONE, "人工标记为已在平台删除")
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=min(6, len(targets))) as pool:
            futures = {pool.submit(platform_item_state, row["platform"], row["platform_product_id"], shop_id): row for row in targets}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    states[(row["product_code"], row["platform"])] = future.result()
                except Exception as exc:
                    states[(row["product_code"], row["platform"])] = (ITEM_UNKNOWN, str(exc)[:200])
    deleted_products, unknown_items, exists_count = set(), [], 0
    conn = db()
    for row in targets:
        state, note = states.get((row["product_code"], row["platform"]), (ITEM_UNKNOWN, ""))
        if state == ITEM_GONE:
            # 只改「核对过的那条身份」: WHERE 必须带上 platform_product_id。
            # 判定用的 platform_product_id 是上面 SELECT 时读到的, 而这里原本按"商品+平台"宽条件更新;
            # 若两步之间运营正好重发成功(新 success 行落库), 这条新行会被一起改成 deleted →
            # 刚发成功的商品显示"未发布" → 再发一次 → 平台上出现重复商品(只能人工删)。
            # 带上 platform_product_id 后, 并发新增的记录平台 ID 必然不同, 不会被误伤;
            # 同一身份的其它历史记录仍一起改, 保证汇总接口按状态计数时不会漏成"已发布"。
            conn.execute("UPDATE publish_items SET status='deleted', error=? "
                         "WHERE shop_id=? AND product_code=? AND platform=? AND platform_product_id=? "
                         "AND status IN ('success','partial')",
                         (f"平台已删除（{now()} 核对，平台ID={row['platform_product_id']}）：{note}"[:500],
                          shop_id, row["product_code"], row["platform"], row["platform_product_id"]))
            deleted_products.add(row["product_code"])
        elif state == ITEM_EXISTS:
            exists_count += 1
        else:
            unknown_items.append({"product_code": row["product_code"], "platform": row["platform"], "reason": note})
    conn.commit()
    conn.close()
    return {"ok": True, "result": {
        "checked": len(targets), "deleted": len(deleted_products), "exists": exists_count,
        "unknown": len(unknown_items), "products": sorted(deleted_products), "unknown_items": unknown_items[:50],
    }}


@app.get("/jobs/{job_id}")
def job(job_id: str, request: Request):
    shop_id = _current_shop_id(request)
    conn = db(); j = conn.execute("SELECT * FROM publish_jobs WHERE id=? AND shop_id=?", (job_id, shop_id)).fetchone()
    if not j: conn.close(); raise HTTPException(404, "发布任务不存在")
    items = [dict(r) for r in conn.execute("SELECT * FROM publish_items WHERE job_id=? AND shop_id=? ORDER BY id", (job_id, shop_id)).fetchall()]
    conn.close()
    # mode 随任务/条目一起回传：前端按它标注"模拟"，也用于区分同一任务里的混合模式
    return {"ok": True, "result": {"job_id": job_id, "batch_id": j["batch_id"], "status": j["status"], "total": j["total"], "processed": j["processed"], "success": j["success"], "failed": j["failed"], "mode": j["mode"], "items": items}}


@app.get("/image-cache/stats")
def image_cache_stats(request: Request):
    """图片上传缓存统计:DB 条目数、内存命中、DB 命中、未命中次数。"""
    shop_id = _current_shop_id(request)
    conn = db()
    total = conn.execute("SELECT COUNT(*) as c FROM image_upload_cache WHERE shop_id=?", (shop_id,)).fetchone()["c"]
    wechat_count = conn.execute("SELECT COUNT(*) as c FROM image_upload_cache WHERE shop_id=? AND platform='wechat'", (shop_id,)).fetchone()["c"]
    xhs_count = conn.execute("SELECT COUNT(*) as c FROM image_upload_cache WHERE shop_id=? AND platform='xhs'", (shop_id,)).fetchone()["c"]
    conn.close()
    total_lookups = _image_cache_hit_mem + _image_cache_hit_db + _image_cache_miss
    hit_rate = round((_image_cache_hit_mem + _image_cache_hit_db) / total_lookups * 100, 1) if total_lookups else 0
    return {"ok": True, "result": {
        "db_total": total, "db_wechat": wechat_count, "db_xhs": xhs_count,
        "mem_size": len(_IMAGE_CACHE_MEM), "mem_max": _IMAGE_CACHE_MAX,
        "hit_mem": _image_cache_hit_mem, "hit_db": _image_cache_hit_db,
        "miss": _image_cache_miss, "total_lookups": total_lookups, "hit_rate_pct": hit_rate,
    }}


@app.delete("/image-cache")
def clear_image_cache(request: Request):
    """清空当前店铺的图片上传缓存(内存 + SQLite)。用于平台素材被删、强制重传等场景。"""
    shop_id = _current_shop_id(request)
    global _image_cache_hit_mem, _image_cache_hit_db, _image_cache_miss
    _IMAGE_CACHE_MEM.clear()
    _IMAGE_CACHE_ORDER.clear()
    _image_cache_hit_mem = _image_cache_hit_db = _image_cache_miss = 0
    conn = db(); conn.execute("DELETE FROM image_upload_cache WHERE shop_id=?", (shop_id,)); conn.commit(); conn.close()
    return {"ok": True, "message": "图片上传缓存已清空"}


# ==================================================================
# 类目别名映射表:内部类目 -> 微信/小红书平台类目。
# 运营人工确认一次类目组后存入此表,后续批次自动命中,不再重复选。
# ==================================================================
@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, body: RetryBody, request: Request):
    """仅重新排队失败项；部分成功项不自动重试，避免重复创建平台商品。"""
    shop_id = _current_shop_id(request)
    conn = db()
    if not conn.execute("SELECT id FROM publish_jobs WHERE id=? AND shop_id=?", (job_id, shop_id)).fetchone():
        conn.close()
        raise HTTPException(404, "发布任务不存在")
    requested = set(body.item_ids or [])
    if requested:
        placeholders = ",".join("?" for _ in requested)
        rows = conn.execute(f"SELECT * FROM publish_items WHERE shop_id=? AND job_id=? AND id IN ({placeholders})", [shop_id, job_id, *requested]).fetchall()
        if requested - {row["id"] for row in rows}:
            conn.close()
            raise HTTPException(400, "只能重试当前任务中的商品")
    else:
        rows = conn.execute("SELECT * FROM publish_items WHERE shop_id=? AND job_id=? AND status='failed'", (shop_id, job_id)).fetchall()
    conn.execute("BEGIN IMMEDIATE")
    retried = skipped = 0
    for row in rows:
        if row["status"] == "failed":
            conn.execute("UPDATE publish_items SET status='queued', error=NULL, started_at=NULL, finished_at=NULL WHERE id=? AND shop_id=?", (row["id"], shop_id))
            retried += 1
        elif row["status"] == "partial":
            skipped += 1
    if retried:
        conn.execute("UPDATE publish_jobs SET status='queued', processed=(SELECT COUNT(*) FROM publish_items WHERE shop_id=? AND job_id=? AND status IN ('success','partial')), success=(SELECT COUNT(*) FROM publish_items WHERE shop_id=? AND job_id=? AND status='success'), failed=(SELECT COUNT(*) FROM publish_items WHERE shop_id=? AND job_id=? AND status='partial'), updated_at=? WHERE id=? AND shop_id=?", (shop_id, job_id, shop_id, job_id, shop_id, job_id, now(), job_id, shop_id))
    conn.commit(); conn.close()
    message = f"已将 {retried} 个失败商品重新排队" if retried else ("部分成功项不会自动重试，以免重复创建平台商品" if skipped else "当前没有可重试的失败商品")
    return {"ok": True, "job_id": job_id, "retried": retried, "skipped": skipped, "message": message}


@app.get("/category-aliases")
def list_aliases(request: Request):
    """类目别名映射表（跨店铺共享读取）。

    ⚠️ 这里刻意返回**所有店铺**的别名，而不是只看当前店：
       平台类目树是全局的（同一平台下所有店铺共用同一棵树），别名表里也只存
       类目链 / 属性默认值 / 规格映射，不含任何店铺私有参数（运费模板、物流方案、
       品牌都不在这张表里）。原来按 shop_id 过滤，会让"4 家店卖同类珠宝"变成
       同一套映射要配 4 遍，而实际上配一次就该全平台通用。

    同内部类目可能分散在不同店铺的行里（A 店配了微信侧、B 店只配了小红书侧），
    所以两侧各自取最近一次有值的，拼成一份完整映射返回。
    """
    conn = db()
    rows = conn.execute("SELECT * FROM category_aliases ORDER BY updated_at DESC").fetchall()
    conn.close()
    merged: dict = {}
    for r in rows:
        key = r["internal_category"]
        entry = merged.get(key)
        if entry is None:
            entry = merged[key] = {
                "id": r["id"], "internal_category": key, "shop_id": r["shop_id"],
                "wechat": None, "xhs": None, "youzan": None, "updated_at": r["updated_at"],
                "shop_ids": [],
            }
        entry["shop_ids"].append(r["shop_id"])
        if entry["wechat"] is None and r["wechat_json"]:
            entry["wechat"] = json.loads(r["wechat_json"])
        if entry["xhs"] is None and r["xhs_json"]:
            entry["xhs"] = json.loads(r["xhs_json"])
        if entry["youzan"] is None and r["youzan_json"]:
            entry["youzan"] = json.loads(r["youzan_json"])
    return {"ok": True, "result": list(merged.values())}


@app.post("/category-aliases")
def save_alias(body: AliasBody, request: Request):
    shop_id = _current_shop_id(request)
    key = (body.internal_category or "").strip()
    if not key:
        raise HTTPException(400, "internal_category 不能为空")
    conn = db()
    existing = conn.execute("SELECT wechat_json, xhs_json, youzan_json FROM category_aliases WHERE shop_id=? AND internal_category=?", (shop_id, key)).fetchone()
    # 合并语义:本次未传的一侧保留旧值(微信和小红书经常分两次确认)
    wechat = body.wechat if body.wechat is not None else (json.loads(existing["wechat_json"]) if existing and existing["wechat_json"] else None)
    xhs = body.xhs if body.xhs is not None else (json.loads(existing["xhs_json"]) if existing and existing["xhs_json"] else None)
    youzan = body.youzan if body.youzan is not None else (json.loads(existing["youzan_json"]) if existing and existing["youzan_json"] else None)
    if not wechat and not xhs and not youzan:
        conn.close()
        raise HTTPException(400, "wechat / xhs / youzan 至少提供一个")
    conn.execute(
        """INSERT INTO category_aliases(shop_id, internal_category, wechat_json, xhs_json, youzan_json, updated_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(shop_id, internal_category) DO UPDATE SET
             wechat_json=excluded.wechat_json, xhs_json=excluded.xhs_json,
             youzan_json=excluded.youzan_json, updated_at=excluded.updated_at""",
        (shop_id, key, json.dumps(wechat, ensure_ascii=False) if wechat else None,
         json.dumps(xhs, ensure_ascii=False) if xhs else None,
         json.dumps(youzan, ensure_ascii=False) if youzan else None, now()))
    conn.commit(); conn.close()
    return {"ok": True, "result": {"internal_category": key, "wechat": wechat, "xhs": xhs,
                                   "youzan": youzan}}


@app.delete("/category-aliases/{alias_id}")
def delete_alias(alias_id: int, request: Request):
    """删除一条类目映射。

    ⚠️ 列表是跨店合并展示的，删除也必须按「内部类目」把所有店铺的同名行一起删掉，
       否则会出现"删了它、切到另一个店又冒出来"的诡异现象。
    """
    conn = db()
    row = conn.execute("SELECT internal_category FROM category_aliases WHERE id=?", (alias_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "类目映射不存在")
    conn.execute("DELETE FROM category_aliases WHERE internal_category=?", (row["internal_category"],))
    conn.commit(); conn.close()
    return {"ok": True}


# ------------------------------------------------------------------
# 映射表批量导入:运营提前整理"内部类目 -> 平台类目路径"Excel,
# 导入时按官方类目名逐级反查平台类目ID并校验,成功行存入映射表。
# ------------------------------------------------------------------
# ⚠️ 默认值必须是 127.0.0.1 而不是 localhost：Windows 下 localhost 优先解析成 IPv6 [::1]，
#    而两个平台服务只监听 IPv4，导致每次调用都要先把 ::1 连超时才回退 IPv4
#    （实测 worker 进程里堆着一排 SYN_SENT 到 [::1]:8000/8010，明显拖慢任务领取与图片上传）。
#    127.0.0.1 是纯 IPv4，行为确定。
WECHAT_API_BASE = platform_registry.api_base("wechat")
XHS_API_BASE = platform_registry.api_base("xhs")
YOUZAN_API_BASE = platform_registry.api_base("youzan")


def _http_get_json(url, timeout=60, shop_id=None, extra_headers=None):
    # 调 wechat/xhs 服务：它们开启鉴权后必须带 X-API-Key，否则一律 401
    headers = api_auth.auth_headers()
    if shop_id:
        headers[SHOP_HEADER] = shop_id
    if extra_headers:
        headers.update(extra_headers)
    if headers:
        req = urllib.request.Request(url, headers=headers)
    else:
        req = url
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def split_category_path(text):
    return [part.strip() for part in re.split(r"[>＞]", str(text or "")) if part.strip()]


def parse_mapping_sheet(raw, filename):
    """解析映射表文件,返回 [{内部类目/微信类目路径/小红书类目路径...}] 原始行。"""
    extension = os.path.splitext(filename or "")[1].lower()
    if extension == ".xls":
        raise HTTPException(400, "暂不支持旧版 .xls，请另存为 .xlsx 后再导入")
    if extension == ".csv":
        try:
            return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        except UnicodeDecodeError:
            raise HTTPException(400, "CSV 请保存为 UTF-8 编码后再导入")
        except Exception as exc:
            raise HTTPException(400, f"CSV 解析失败：{exc}")
    try:
        import openpyxl
        workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        rows = list(workbook.active.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(x or "").strip() for x in rows[0]]
        result = []
        for cells in rows[1:]:
            if not any(cell not in (None, "") for cell in cells):
                continue
            result.append({headers[i]: (cells[i] if i < len(cells) else "") for i in range(len(headers)) if headers[i]})
        return result
    except Exception as exc:
        raise HTTPException(400, f"Excel 解析失败：{exc}")


def resolve_wechat_path(parts, cache, shop_id=None):
    """按官方名称路径整链反查微信类目,返回 (结果, 错误原因)。"""
    keyword = parts[-1]
    if keyword not in cache:
        try:
            data = _http_get_json(f"{WECHAT_API_BASE}/categories?keyword={urllib.parse.quote(keyword)}", timeout=180, shop_id=shop_id)
        except Exception as exc:
            raise HTTPException(502, f"微信类目服务({WECHAT_API_BASE})不可用："
                                     f"{_http_error_detail(exc) if isinstance(exc, urllib.error.HTTPError) else exc}")
        cache[keyword] = [r for r in data.get("results", []) if r.get("leaf")]
    leaves = cache[keyword]
    matched = [r for r in leaves if [str(n.get("name", "")).strip() for n in r.get("chain", [])] == parts]
    if len(matched) == 1:
        return {"category": matched[0]["path"], "chain": matched[0].get("chain", [])}, None
    candidates = "；".join(r.get("path", "") for r in leaves[:5]) or "无"
    return None, f"未匹配到唯一微信类目（含“{keyword}”的候选：{candidates}）"


def resolve_xhs_path(parts, cache, shop_id=None):
    """按官方名称路径逐级反查小红书类目,末级必须是叶子,返回 (结果, 错误原因)。"""
    parent_id = None
    chain = []
    last = None
    for index, part in enumerate(parts):
        key = str(parent_id or "")
        if key not in cache:
            url = f"{XHS_API_BASE}/categories" + (f"?category_id={urllib.parse.quote(key)}" if parent_id else "")
            try:
                data = _http_get_json(url, timeout=60, shop_id=shop_id)
            except Exception as exc:
                raise HTTPException(502, f"小红书类目服务({XHS_API_BASE})不可用："
                                         f"{_http_error_detail(exc) if isinstance(exc, urllib.error.HTTPError) else exc}")
            result = data.get("result") or []
            cache[key] = result if isinstance(result, list) else []
        options = cache[key]
        matches = [c for c in options if str(c.get("name", "")).strip() == part]
        if not matches:
            names = "、".join(str(c.get("name", "")) for c in options[:10]) or "空"
            return None, f"第{index + 1}级没有“{part}”，该级可选：{names}"
        last = matches[0]
        parent_id = str(last.get("id") or last.get("categoryId") or "")
        chain.append({"id": parent_id, "name": str(last.get("name", "")).strip()})
    if not (last.get("isLeaf") or last.get("leaf")):
        return None, f"“{parts[-1]}”不是叶子类目，请写到平台最后一级"
    return {"category": " > ".join(c["name"] for c in chain), "chain": chain, "category_id": chain[-1]["id"]}, None


@app.get("/category-aliases/template")
def alias_template():
    headers = ["内部类目", "微信类目路径", "小红书类目路径"]
    try:
        import openpyxl
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "类目映射"
        sheet.append(headers)
        sheet.append(["钻石 > 钻石首饰 > 项链", "珠宝首饰 > 天然钻石 > 项链", "黄金/彩宝/钻石/珍珠 > 钻石首饰 > 颈饰/挂件/吊坠"])
        for cell in sheet[1]:
            cell.font = openpyxl.styles.Font(bold=True)
        sheet.freeze_panes = "A2"
        note = workbook.create_sheet("填写说明")
        for line in [
            "1. 内部类目必须与商品导入表里的内部类目完全一致，同名行会覆盖更新已有映射。",
            "2. 平台类目路径必须使用平台官方类目名，各级之间用 > 分隔，写到叶子级为止。",
            "3. 只填一侧也可以，未填写的一侧保留映射表里已有的旧值。",
            "4. 导入时系统自动把名称反查成平台类目ID并逐级校验，失败行会给出原因，修正后重新上传即可。",
        ]:
            note.append([line])
        output = io.BytesIO()
        workbook.save(output)
        output.seek(0)
        return StreamingResponse(iter([output.read()]), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=category-alias-template.xlsx"})
    except Exception as exc:
        raise HTTPException(500, f"生成映射表模板失败：{exc}")


@app.post("/category-aliases/import")
def import_aliases(body: AliasImportBody, request: Request):
    shop_id = _current_shop_id(request)
    try:
        raw = base64.b64decode(body.content_base64)
    except Exception:
        raise HTTPException(400, "文件编码无效")
    rows = parse_mapping_sheet(raw, body.filename)
    wechat_cache, xhs_cache, report, resolved = {}, {}, [], []
    imported = failed = skipped = 0
    for index, row in enumerate(rows, 2):
        internal = value(row, "内部类目", "类目")
        wechat_path = value(row, "微信类目路径", "微信类目", "微信")
        xhs_path = value(row, "小红书类目路径", "小红书类目", "小红书")
        if not internal:
            skipped += 1
            report.append({"line": index, "internal_category": "", "wechat": "跳过：缺少内部类目", "xhs": ""})
            continue
        if not wechat_path and not xhs_path:
            skipped += 1
            report.append({"line": index, "internal_category": internal, "wechat": "", "xhs": "跳过：两侧类目都为空"})
            continue
        wechat = xhs = None
        wechat_msg = xhs_msg = "未填写"
        if wechat_path:
            wechat, err = resolve_wechat_path(split_category_path(wechat_path), wechat_cache, shop_id)
            wechat_msg = "成功" if wechat else err
        if xhs_path:
            xhs, err = resolve_xhs_path(split_category_path(xhs_path), xhs_cache, shop_id)
            xhs_msg = "成功" if xhs else err
        if wechat or xhs:
            imported += 1
            resolved.append((internal, wechat, xhs))
        else:
            failed += 1
        report.append({"line": index, "internal_category": internal, "wechat": wechat_msg, "xhs": xhs_msg})
    if resolved:
        conn = db()
        for internal, wechat, xhs in resolved:
            # 合并语义与单条保存一致:本次未解析出的一侧保留旧值
            existing = conn.execute("SELECT wechat_json, xhs_json, youzan_json FROM category_aliases WHERE shop_id=? AND internal_category=?", (shop_id, internal)).fetchone()
            merged_wechat = wechat or (json.loads(existing["wechat_json"]) if existing and existing["wechat_json"] else None)
            merged_xhs = xhs or (json.loads(existing["xhs_json"]) if existing and existing["xhs_json"] else None)
            # 批量导入的 Excel 当前只有「微信路径 / 小红书路径」两列，有赞列留待前端改造（Y3）后补；
            # 这里保留旧值，避免"导一次 Excel 把已配好的有赞映射清空"。
            merged_youzan = (json.loads(existing["youzan_json"])
                             if existing and existing["youzan_json"] else None)
            conn.execute(
                """INSERT INTO category_aliases(shop_id, internal_category, wechat_json, xhs_json, youzan_json, updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(shop_id, internal_category) DO UPDATE SET
                     wechat_json=excluded.wechat_json, xhs_json=excluded.xhs_json,
                     youzan_json=excluded.youzan_json, updated_at=excluded.updated_at""",
                (shop_id, internal, json.dumps(merged_wechat, ensure_ascii=False) if merged_wechat else None,
                 json.dumps(merged_xhs, ensure_ascii=False) if merged_xhs else None,
                 json.dumps(merged_youzan, ensure_ascii=False) if merged_youzan else None, now()))
        conn.commit(); conn.close()
    return {"ok": True, "result": {"imported": imported, "failed": failed, "skipped": skipped, "report": report}}


if __name__ == "__main__":
    # 启动时一次性建表/迁移：避免第一个请求（或与 worker 进程并发）同时抢 DDL 写锁
    _ensure_db_schema()
    # DEV_RELOAD=1 时开热重载（开发机用）；未设置时行为与改造前完全一致
    runtime_config.run_service(app, "bulk_api", BULK_API_HOST, BULK_API_PORT,
                               reload_dirs=[os.path.dirname(os.path.abspath(__file__))])
