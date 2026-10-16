# -*- coding: utf-8 -*-
"""平台适配器注册表：平台差异的唯一事实来源。

背景
----
改造前，「平台」这个概念散落在编排层的每个角落：``shop_registry`` 的平台白名单与凭证字典、
``bulk_worker`` 的线程池平台列表、``bulk_api`` 的发布/标题/图片/回填/gone 判定分支，
全是 ``if platform == "wechat" else ...`` 的二元判断。接第三个平台时这些地方会同时崩塌
（改一处漏一处，且漏掉的地方不会报错，只会在线上表现成"某个平台发不出去/标题被按别家裁剪"）。

本模块把每个平台的差异收敛成一份**声明式的适配器描述**，编排层只通过 ``adapter(platform)``
读取字段，不再出现平台名。改造后的调用关系：

    shop_registry._VALID_PLATFORMS = platform_registry.all_platforms()
    bulk_worker._planned_slots()    用 all_platforms() 驱动线程池
    bulk_api 的发布/标题/图片/必填参数/货盘回填/gone 判定全部从适配器字段读

**新增一个平台 = 在 ``_ADAPTERS`` 里加一条 + 在 ``bulk_api`` 里加一个发布处理器**，
不需要再新增判断分支。

兼容性
------
``wechat`` / ``xhs`` 两条适配器的每个字段都与改造前的常量**逐字相同**，因此重构后行为不变。
回归方式：``python bulk_api/multishop_regression_test.py`` + 微信/小红书各真实试发一件。

线程安全
--------
``_ADAPTERS`` 是只读常量，``adapter()`` 返回**副本**，调用方改写不会污染注册表。
"""

from __future__ import annotations

import os
import urllib.parse

# --- 适配器字段字典 -----------------------------------------------------------
# key                  平台标识：拼配置键/批次列名/前端选项/publish_items.platform 都用它
# label                给运营看的中文名
# api_base_env         中台调用该平台服务所用地址的环境变量名
# default_base         该环境变量缺省时的地址（必须 127.0.0.1，不能 localhost：Windows 下
#                      localhost 优先解析成 IPv6，而平台服务只监听 IPv4，会先连超时才回退）
# service_dir          平台服务目录名
# creds_key            凭证主键：店铺配置里"配了这个键就算配了这个平台"的那个键
#                      （替代 ``"appid" if platform == "wechat" else "app_id"``）
# required_credentials 完整凭证键（与 param_keys 共同构成店铺配置契约）
# param_keys           店铺级参数键（均允许空字符串占位）
# env_credentials      从 .env 合成默认店时的凭证映射 {字段: 环境变量名}
# env_params           从 .env 合成默认店时的参数映射 {字段: (环境变量名, 缺省值)}
# title_column         商品/映射里的平台标题列名（运营在审核抽屉里改的就是这一列）
# title_fit            发布前长度兜底函数的标识（bulk_api 里映射到具体的 fit_* 函数）
# title_fit_max        该兜底的上限。⚠️ 这是**发布前裁剪**的上限；
#                      **生成期**上限在 huopai_adapter（WECHAT_TITLE_MAX/XHS_TITLE_MAX），是另一层，别混用
# price_unit           价格单位：cent=分 / yuan=元（中台内部统一用元，适配层负责换算）
# publish_path         商品创建接口在本平台服务里的路径
# image_file_path      按文件字节上传图片的端点
# image_url_path       按外链 URL 上传图片的端点
# image_url_payload_key 外链上传时该端点的入参键名（微信/小红书不同）
# item_path            商品详情路径模板，``{id}`` 占位
# required_params      发布前必须齐的店铺级参数。每项 = (映射键候选..., 给人看的名字)，
#                      **最后一项是名字、前面是候选键** —— 与 bulk_api._missing_shop_params 的取值口径一致
# id_headers           货盘表「款级商品ID」列的候选表头名（第 1 个同时用作追加新列时的列名）
# sku_id_headers       货盘表「行级规格ID」列的候选表头名
# gone_markers         平台「商品不存在/已删除」的文案或错误码（用于发布状态核对）
# sku_image_fallback   规格图缺图时是否用商品主图兜底（小红书要求每个规格值都有图）
# supports             能力声明（订单/数据/营销模块按能力建页，未开通的能力直接隐藏）
# rate_limit_markers   限流类错误码/文案（在响应原文里做子串匹配）。命中即认定"平台已拒绝、
#                      本次没有副作用"，可安全退避重试（见 mock_platform.gateway_call）
# auth_expired_markers 凭证过期类错误码/文案，用于识别"该刷新 token 了"的重试时机
#                      ⚠️ 新平台的这两组标记是 `*` 待核对项：先用 MOCK_RECORD 录一次真实
#                      报错响应再校正，否则限流/过期会被当成普通业务失败（文案不可读）

# --- 平台注册表 ---------------------------------------------------------------
# ⚠️ 顺序即 ``all_platforms()`` 的返回顺序，会被用于：
#    - 货盘表 ID 列的**追加顺序**（bulk_api._huopai_id_columns 按 HUOPAI_ID_HEADERS 的键顺序建列）
#    - worker 线程池的启动顺序
#    因此「新增平台」应追加到末尾，不要插队。
_PLATFORMS: tuple[str, ...] = ("wechat", "xhs", "youzan")

_ADAPTERS: dict[str, dict] = {
    "wechat": {
        "key": "wechat",
        "label": "微信小店",
        "api_base_env": "WECHAT_API_BASE",
        "default_base": "http://127.0.0.1:8000",
        "service_dir": "wechat",
        "creds_key": "appid",
        "required_credentials": ("appid", "secret"),
        "param_keys": ("brand_id", "freight_template_id", "after_sale_address_id"),
        "env_credentials": {"appid": "WX_APPID", "secret": "WX_SECRET"},
        "env_params": {
            "brand_id": ("WX_BRAND_ID", "2100000000"),  # 微信默认无品牌占位
            "freight_template_id": ("WX_FREIGHT_TEMPLATE_ID", ""),
            "after_sale_address_id": ("WX_AFTER_SALE_ADDRESS_ID", ""),
        },
        "title_column": "wechat_title",
        "title_fit": "wechat",
        "title_fit_max": 60,
        "price_unit": "cent",
        "publish_path": "/products",
        "image_file_path": "/images/upload-file",
        "image_url_path": "/images/upload",
        "image_url_payload_key": "img_url",
        "item_path": "/products/{id}",
        "required_params": (("wechat_freight_template_id", "freight_template_id", "微信运费模板"),),
        "id_headers": ("微信小店", "微信商品id", "微信小店商品id", "微信产品id"),
        "sku_id_headers": ("微信规格id", "微信sku id", "微信规格id(sku)"),
        "gone_markers": ("10020052", "商品不存在", "商品已删除", "商品在回收站"),
        "rate_limit_markers": ("45009", "45011"),   # 45009 频次超限 / 45011 分钟配额
        "auth_expired_markers": ("40001", "40014", "42001", "42007"),
        "sku_image_fallback": False,
        # orders = 只读订单/售后查询能力（数据面板按 orders 能力建页，权限没批下来就不出页）
        # stats  = 平台自带的数据概览接口（微信罗盘：成交金额/成交人数/商品点击人数…）
        "supports": frozenset({"publish", "listing", "delete", "image", "orders", "stats"}),
    },
    "xhs": {
        "key": "xhs",
        "label": "小红书",
        "api_base_env": "XHS_API_BASE",
        "default_base": "http://127.0.0.1:8010",
        "service_dir": "xhs",
        "creds_key": "app_id",
        "required_credentials": ("app_id", "app_secret"),
        "param_keys": ("brand_id", "category_id", "shipping_template_id", "logistics_plan_id"),
        "env_credentials": {
            "app_id": "XHS_APP_ID",
            "app_secret": "XHS_APP_SECRET",
            "access_token": "XHS_ACCESS_TOKEN",
            "refresh_token": "XHS_REFRESH_TOKEN",
        },
        "env_params": {
            "brand_id": ("XHS_BRAND_ID", ""),
            "category_id": ("XHS_CATEGORY_ID", ""),
            "shipping_template_id": ("XHS_SHIPPING_TEMPLATE_ID", ""),
            "logistics_plan_id": ("XHS_LOGISTICS_PLAN_ID", ""),
        },
        "title_column": "xhs_title",
        "title_fit": "xhs",
        "title_fit_max": 30,
        "price_unit": "cent",
        "publish_path": "/items/and-sku",
        "image_file_path": "/materials/upload-file",
        "image_url_path": "/materials/upload",
        "image_url_payload_key": "url",
        "item_path": "/items/{id}",
        "required_params": (("xhs_shipping_template_id", "小红书运费模板"),
                            ("xhs_logistics_plan_id", "小红书物流方案")),
        "id_headers": ("小红书产品id", "小红书商品id"),
        "sku_id_headers": ("小红书规格id", "小红书sku id", "小红书规格id(sku)"),
        "gone_markers": ("商品不存在", "商品已删除",
                         "item not exist", "item_not_exist", "item not found"),
        "rate_limit_markers": ("429", "too many request", "frequency limit"),
        "auth_expired_markers": ("401", "accessToken expired", "授权已过期"),
        "sku_image_fallback": True,
        "supports": frozenset({"publish", "listing", "delete", "image", "orders"}),
    },
    "youzan": {
        # 有赞云开放平台（自用型无容器应用）：见 docs/有赞平台适配实施方案.md
        "key": "youzan",
        "label": "有赞",
        "api_base_env": "YOUZAN_API_BASE",
        "default_base": "http://127.0.0.1:8030",
        "service_dir": "youzan",
        # 有赞把"应用身份 + 店铺"分开：client_id/client_secret 是应用凭证，kdt_id 是店铺 ID；
        # 自用型应用可绑多个店铺，access_token 按 kdt_id 隔离，所以三件套缺一不可。
        "creds_key": "client_id",
        "required_credentials": ("client_id", "client_secret", "kdt_id"),
        "param_keys": ("delivery_template_id",),
        "env_credentials": {
            "client_id": "YOUZAN_CLIENT_ID",
            "client_secret": "YOUZAN_CLIENT_SECRET",
            "kdt_id": "YOUZAN_KDT_ID",
            "access_token": "YOUZAN_ACCESS_TOKEN",
        },
        "env_params": {
            "delivery_template_id": ("YOUZAN_DELIVERY_TEMPLATE_ID", ""),
        },
        "title_column": "youzan_title",
        "title_fit": "youzan",
        # ★待核对：有赞商品标题上限。
        #   线索：有赞**后台发布页**商品名称允许 100 字（2026-10-15 用户截图），但后台页
        #   一般比开放接口宽松（后台会替我们兜住），不能据此认定 API 也给 100。
        #   现按 60 与微信对齐（保守）：多截 40 字只会让标题变短，不会让平台拒。
        #   真机联调时用一条 61~100 字的标题单独发一次，验出上限后再调这里 + fit_youzan_title。
        "title_fit_max": 60,
        # ★★ 2026-10-15 真机纠正：有赞商品接口的 price 单位是**分**，不是元！
        #    原先按"官方文档注明单位:元"写成 yuan，导致真机发布出来的商品价格是预期的 1/100
        #    （货盘上架价 699000 元 → 平台存成 699000 分 = 6990 元，会超低价卖出）。
        #    双重证据：
        #      ① 真机：用户在后台手工建的商品填 28000 元 → GET 回来 price=2800000（= 分）；
        #      ② 官方《有赞云开放接口商品场景对接说明》明确写 price「单位：分」。
        #    ⚠️ 与微信/小红书同为 cent，不要再改回 yuan。
        "price_unit": "cent",
        "publish_path": "/products",
        "image_file_path": "/materials/upload-file",
        # ⚠️ price_unit=cent 只适用于**创建**（item.create 的 price 与 sku_stocks[].price 收分）。
        # 2026-10-15 真机实测两条反直觉口径：
        #   ① **改价接口 youzan.item.sku.update 的 price 收「元」**（与创建方向相反）——
        #      按分传 20128600 会被读成 2012 万元，报 code=123001065 SKU价格超过最大限制；
        #      传 201287.5（元）成功，平台里存的是 20128750 分。
        #   ② youzan.item.quantity.update 的 type **只认 1=增量**（2/3 报 123000104
        #      不支持的库存更新类型）—— 平台没有"全量覆盖"，要设绝对库存必须先回读现值算差值。
        "image_url_path": "/materials/upload",
        # ★待核对：图片上传入参名（官方文档/SDK 示例有 img / image 两种写法）
        "image_url_payload_key": "img",
        "item_path": "/products/{id}",
        "required_params": (("youzan_delivery_template_id", "有赞运费模板"),),
        "id_headers": ("有赞商品id", "有赞商品ID", "有赞产品id"),
        "sku_id_headers": ("有赞规格id", "有赞sku id", "有赞规格id(sku)"),
        # ✅ 2026-10-15 真机核对：商品已删除时 youzan.item.get 报
        #    `code=122001001 商品不存在!`（HTTP 400）→ 错误码与文案都记下来，双保险
        "gone_markers": ("122001001", "商品不存在", "商品已删除", "item not exist", "item_not_exist"),
        # 4101 = 请求太频繁（公开文档确认）
        "rate_limit_markers": ("4101", "请求太频繁", "too many request"),
        # ★待核对：access_token 失效类错误码
        "auth_expired_markers": ("access_token 无效", "无效的access_token", "4201"),
        # 有赞商品主图与规格图是分开的素材，规格缺图时用主图兜底更稳
        "sku_image_fallback": True,
        # listing = 上下架；delete = 删除商品。
        # ✅ 2026-10-15 真机核对：youzan.item.update.listing / .delisting 与 youzan.item.delete
        #    都可用（均返回 {"item_id":…, "is_success": true}）。
        #    但 **delete 仍未在代码里开放**（youzan_api 没有 /products/{id} 的 DELETE 路由、
        #    前端也没有删除按钮）—— 声明了却没人实现，前端会冒出点不动的删除按钮，
        #    所以这里先不加；真要开放删除时：youzan_api 补 DELETE 路由 + 这里加 "delete"。
        "supports": frozenset({"publish", "image", "orders", "listing"}),
    },
    # --- 待接入（软著后按接口清单 §5.1 逐一补字段，标签均为 `*` 需核对）-------------
    # "douyin": { ... },   抖音：creds_key="access_token"、price_unit="cent"、
    #                      id_headers=("抖音产品id",)、sku_id_headers=("抖音规格id",)
    # "jd":     { ... },   京东：creds_key="access_token"、price_unit="yuan"（分/元是最高危的坑）
    # "tmall":  { ... },   天猫：creds_key="session"（字段名与其他三家都不同）、price_unit="yuan"
}


# --- 公共 API ----------------------------------------------------------------

def all_platforms() -> tuple[str, ...]:
    """所有已注册平台标识（驱动 worker 线程池、前端平台选项、店铺校验白名单）。"""
    return _PLATFORMS


def is_platform(platform) -> bool:
    """是否已注册平台。用于「未知平台」需要走旧错误分支（而非抛 ValueError）的地方。"""
    return str(platform or "").strip() in _ADAPTERS


def adapter(platform: str) -> dict:
    """取平台适配器描述（返回副本）。

    未知平台直接抛 ``ValueError`` —— fail fast，不静默降级成别的平台：
    改造前未知平台会落到 ``else`` 分支被当成小红书处理（标题按小红书裁剪、图片传到小红书服务），
    这种"静默当成别家平台"比报错危险得多。
    """
    entry = _ADAPTERS.get(str(platform or "").strip())
    if entry is None:
        raise ValueError(f"未知平台: {platform}（已注册: {', '.join(_PLATFORMS)}）")
    return dict(entry)


def adapters_with(capability: str) -> list[str]:
    """按能力筛选平台（如订单模块只给 ``supports`` 里声明了 ``orders`` 的平台建页）。"""
    return [p for p in _PLATFORMS if capability in _ADAPTERS[p]["supports"]]


def platform_label(platform: str) -> str:
    """平台中文名，用于给运营看的报错/日志。"""
    return adapter(platform)["label"]


def creds_key(platform: str) -> str:
    """该平台「配了就算配了」的凭证主键（wechat=appid / xhs=app_id / 天猫=session）。"""
    return adapter(platform)["creds_key"]


def api_base(platform: str) -> str:
    """该平台服务的地址。

    ⚠️ 必须**调用时**读环境变量，不能在 import 期读：三个服务的入口都是
    ``import 模块`` → ``load_project_env()`` 的顺序（bulk_api.py:31-36），
    import 期读到的 .env 还是空的，会把 ``WECHAT_API_BASE`` 之类的配置吃掉。
    """
    entry = adapter(platform)
    return os.environ.get(entry["api_base_env"]) or entry["default_base"]


def item_url(platform: str, product_id) -> str:
    """商品详情 URL（状态核对、规格ID回查都用它）。product_id 做 URL 编码。"""
    entry = adapter(platform)
    return f"{api_base(platform)}{entry['item_path'].format(id=urllib.parse.quote(str(product_id)))}"


def title_fit_max(platform: str) -> int:
    """该平台发布前的标题长度兜底上限。"""
    return adapter(platform)["title_fit_max"]


def price_to_platform(platform: str, amount_yuan):
    """中台内部价格（元）→ 平台价格单位。

    微信/小红书/抖店用「分」，京东/天猫用「元」。
    换算错误**不会报错，只会超低价卖出**，所以这条路径必须有单测（见回归脚本）。
    """
    entry = adapter(platform)
    value = float(amount_yuan or 0)
    if entry["price_unit"] == "cent":
        return round(value * 100)
    return value


if __name__ == "__main__":
    # 自检：打印注册表，便于接新平台时对照字段是否漏填
    print(f"已注册 {len(_PLATFORMS)} 个平台：")
    for _p in _PLATFORMS:
        _a = adapter(_p)
        print(f"  {_p:8s} {_a['label']:6s} {api_base(_p):24s} "
              f"cea={_a['creds_key']:12s} 价格单位={_a['price_unit']:4s} "
              f"标题上限={_a['title_fit_max']:3d} 能力={','.join(sorted(_a['supports']))}")
        print(f"           价格换算: 19.99 元 → {price_to_platform(_p, 19.99)}")
