# -*- coding: utf-8 -*-
"""
====================================================================
 微信小店 API 客户端 (Python) —— 带详细注释教学版
====================================================================
这个脚本用来"自动操作"你的微信小店,包含 6 个核心接口:
  1. img_upload       上传图片(把图片地址交给微信,微信转存后返回 img_url)
  2. addproduct       添加商品(创建到"草稿"状态,不会立即上架)
  3. updateproduct    更新商品(修改草稿或线上商品的信息)
  4. listingproduct   上架商品(提交审核,审核通过后才正式开卖)
  5. delistingproduct 下架商品(把商品从店铺撤下,不再售卖)
  6. deleteproduct    删除商品(彻底删掉,不可恢复)

运行前提:
  - 已安装 requests:  pip install requests
  - 已拿到小店的 AppID 和 AppSecret(小店后台 → 服务市场 → 经营工具 → 自研)

怎么运行:
  1. 把下面的 APPID 和 SECRET 改成你自己的
  2. 把 __main__ 演示部分里的图片URL、类目ID 换成真实的
  3. 执行:  python wechat_store_client.py

重要概念(先理解再动手):
  - access_token: 微信发的"临时通行证",2小时有效,过期要重新换
  - img_url:      图片上传后微信返回的图片地址,发商品时 head_imgs 填它
  - product_id:   商品编号,上架/下架/删除/查询都靠它
  - 草稿 vs 上架:  addproduct 只创建草稿(后台可见,用户看不到,不审核)
                  listingproduct 才提交审核,审核通过才对外售卖
====================================================================
"""

import os      # 用来读环境变量(把密钥放环境变量比写死在代码里安全)
import sys
import struct
import time    # 用来算 token 过期时间、测试时等待
import requests  # 发 HTTP 请求的库(微信接口都是 HTTP 接口)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime_config import load_project_env

load_project_env()

# ------------------------------------------------------------------
# 配置区:把下面两个值换成你自己的
# (也可以不改这里,运行前设置环境变量 WX_APPID 和 WX_SECRET,更安全)
# ------------------------------------------------------------------
APPID = os.environ.get("WX_APPID", "")
SECRET = os.environ.get("WX_SECRET", "")

# ------------------------------------------------------------------
# 发品兜底默认值(可用环境变量覆盖)。取本店在售商品实测有效的真实值:
#  - 运费模板 ID: 官方要求填在 express_info.template_id(不是顶层 freight_template_id),
#    前端没选模板时用它兜底,避免 6600120「查询模板ID失败」。
#  - 售后/退货地址 ID: after_sale_info.after_sale_address_id 现为必填(官方 2025-05 起),
#    前端没有退货地址选择项时用它兜底,避免「售后地址id无效」。
# ------------------------------------------------------------------
DEFAULT_FREIGHT_TEMPLATE_ID = os.environ.get("WX_FREIGHT_TEMPLATE_ID", "")
DEFAULT_AFTER_SALE_ADDRESS_ID = os.environ.get("WX_AFTER_SALE_ADDRESS_ID", "")


class WxStore:
    """
    微信小店操作类。
    把"操作小店"封装成一个个方法,用的时候:
        client = WxStore(appid, secret)   # 1. 创建客户端
        client.listing("商品ID")           # 2. 调方法操作小店
    """

    BASE = "https://api.weixin.qq.com"  # 微信所有接口的公共服务器地址

    def __init__(self, appid, secret):
        """初始化:记住 appid/secret,准备一个空的 token 缓存"""
        self.appid = appid
        self.secret = secret
        self._token = None            # 缓存 access_token,避免每次都重新换
        self._token_expire_at = 0     # token 的过期时间戳(Unix时间)
        self._cat_cache = None        # 类目树缓存(发品归一化类目用,避免每次拉15000+节点)
        self._cat_cache_ts = 0.0

    # =================================================================
    # 第 1 步(所有接口的前提):获取 access_token
    # =================================================================
    def get_access_token(self, force=False):
        """
        获取接口调用凭证 access_token。
        - 第一次调用:向微信申请(拿 appid+secret 换)
        - 之后调用:直接用缓存里的(2小时内不用重复申请)
        - force=True: 强制重新申请(调试时用)
        返回: access_token 字符串
        """
        # 如果缓存里有 token,而且还没到过期时间(提前60秒当过期),就直接返回
        if not force and self._token and time.time() < self._token_expire_at - 60:
            return self._token

        # 向微信申请新 token:调 GET 接口,带上 appid 和 secret 两个参数
        resp = requests.get(
            f"{self.BASE}/cgi-bin/token",          # 接口地址
            params={                                # 查询参数
                "grant_type": "client_credential",  # 固定写法:"客户端凭证"模式
                "appid": self.appid,                # 你的应用ID
                "secret": self.secret,              # 你的应用密钥
            },
            timeout=10,                             # 10秒超时,防止卡死
        )
        data = resp.json()  # 把返回的 JSON 文本转成 Python 字典

        # 微信返回 {"access_token": "xxx", "expires_in": 7200}
        # 如果里面没有 access_token,说明出错了(比如 appid/secret 填错)
        if "access_token" not in data:
            raise RuntimeError(f"获取 access_token 失败: {data}")

        self._token = data["access_token"]                       # 存起来
        self._token_expire_at = time.time() + data["expires_in"] # 记录过期时间
        print(f"[token] 获取成功, 有效期 {data['expires_in']} 秒")
        return self._token

    # =================================================================
    # 公共方法:统一发 POST 请求并检查错误
    # (下面的具体功能方法都会调用它,不用每个接口都写一遍请求逻辑)
    # =================================================================
    @staticmethod
    def _format_ext_info(data):
        """把 ext_info.error_list 里的真实业务错误拼进异常信息。
        微信发品失败时外层 errmsg 只有笼统的「商品信息检查不通过」,
        真正的错误码/原因/出错字段(error_field)在 ext_info.error_list 里,
        不拼出来就无法定位到底是 cats 还是别的字段不对。"""
        errors = (data.get("ext_info") or {}).get("error_list") or []
        if not errors:
            return ""
        parts = []
        for e in errors:
            fields = ",".join(str(f) for f in (e.get("error_field") or []))
            parts.append(f"错误码:{e.get('error_code')} 原因:{e.get('error_msg')}"
                         + (f" 出错字段:[{fields}]" if fields else ""))
        return " | " + "；".join(parts)

    @staticmethod
    def _error_hint(data):
        """针对高频错误码追加「可操作排查建议」,把微信的天书错误翻译成人话。
        重点解释 6600016，并结合最新 cats_v2、开发者认证和类目资质给出排查方向。"""
        text = str(data.get("errmsg") or "")
        for e in (data.get("ext_info") or {}).get("error_list") or []:
            text += f" {e.get('error_code')} {e.get('error_msg')}"
        hints = []
        if "6600016" in text or "类目错误" in text:
            hints.append(
                "【6600016 类目错误】请确认请求只包含最新 cats_v2 完整链路，并检查"
                "开发者资质认证、店铺类目授权、保证金和 product_qua_infos 类目资质。"
                "若均已完成，请凭上方 rid 联系微信小店官方查询具体限制。"
            )
        if "10020083" in text or "10020048" in text or "10020070" in text or "保证金" in text:
            hints.append("【保证金不足】请前往微信小店网页端添加一次该类目商品,完成保证金补缴后再用 API 发品。")
        if "10020018" in text or "10020252" in text:
            hints.append("【类目资质/未申请】请在微信小店后台完成该类目资质提交与准入申请,审核通过后再发品。")
        if "6600120" in text or "查询模板ID失败" in text or "10020019" in text:
            hints.append("【运费模板】请确认 express_info.template_id 为有效模板ID,且 deliver_method=0(快递发货)。")
        if "售后地址" in text or "after_sale_address_id" in text:
            hints.append("【售后地址】after_sale_info.after_sale_address_id 为必填,请填写有效的退货地址ID。")
        return (" | 排查建议: " + " ".join(hints)) if hints else ""

    def _post(self, path, payload=None, is_json_body=True):
        """
        向微信小店接口发 POST 请求。
        - path:      接口路径,如 "/channels/ec/product/listing"
        - payload:   请求体(业务参数,Python 字典)
        - is_json_body: 是否用 JSON 格式传(微信小店接口基本都是 JSON)
        返回: 微信返回的完整 JSON(已转成 Python 字典)
        注意: 如果微信返回错误码(errno不为0),直接抛异常,不用手动判断
        """
        # 每个接口都要带 access_token,自动帮你加上
        params = {"access_token": self.get_access_token()}

        if is_json_body:
            # json= 会自动把 Python 字典转成 JSON 字符串,并设置 Content-Type
            resp = requests.post(f"{self.BASE}{path}", params=params,
                                 json=payload or {}, timeout=15)
        else:
            resp = requests.post(f"{self.BASE}{path}", params=params,
                                 data=payload or {}, timeout=15)

        data = resp.json()  # 解析响应

        # 微信小店接口约定:返回 {"errcode": 0, "errmsg": "ok"} 表示成功
        # errcode 不等于 0 就是失败(比如参数错、没权限、限流)
        if data.get("errcode", 0) != 0:
            raise RuntimeError(
                f"[{path}] 调用失败: errcode={data.get('errcode')} "
                f"errmsg={data.get('errmsg')}"
                f"{self._format_ext_info(data)}"
                f"{self._error_hint(data)}"
            )
        return data

    # =================================================================
    # 接口 1:上传图片(img_upload)
    # =================================================================
    def upload_image(self, img_url):
        """
        上传一张图片,返回微信转存后的图片URL(img_url)。
        原理:把你给的图片URL告诉微信,微信把图片"搬"到自己服务器,
              然后返回一个新的图片地址(img_url)。发商品时 head_imgs 填它。
        - img_url: 图片的公网地址(必须能直接访问,建议放你们自己的OSS)
        返回: img_url 字符串(注意:resp_type=1 返回的是图片URL,不是media_id)
        注意: 这个接口不走 _post,因为它的参数在 URL 上而不是 JSON 里
        """
        params = {
            "access_token": self.get_access_token(),  # 通行证
            "upload_type": 1,   # 1=传图片URL(推荐); 0=传二进制文件流
            "resp_type": 1,     # 1=返回图片URL(发商品head_imgs要填它); 0=返回media_id
        }
        # 请求体里放图片地址,微信去这个地址把图片拉过来转存
        resp = requests.post(
            f"{self.BASE}/shop/ec/basics/img/upload",
            params=params,
            json={"img_url": img_url},
            timeout=15,
        )
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"[img_upload] 失败: {data}")

        # 图片地址可能在顶层 img_url/url,也可能在 pic_file.img_url 里(实测是这里)
        img_url = (data.get("img_url") or data.get("url")
                   or (data.get("pic_file") or {}).get("img_url"))
        if not img_url:
            raise RuntimeError(f"[img_upload] 响应里没有 img_url: {data}")
        print(f"[img_upload] 上传成功 img_url={img_url}")
        return img_url

    @staticmethod
    def _image_size(content):
        if content[:8] == b"\x89PNG\r\n\x1a\n" and len(content) >= 24:
            return struct.unpack(">II", content[16:24])
        if content[:2] == b"\xff\xd8":
            index = 2
            while index + 9 < len(content):
                if content[index] != 0xFF:
                    index += 1
                    continue
                marker = content[index + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    height, width = struct.unpack(">HH", content[index + 5:index + 9])
                    return width, height
                if index + 4 > len(content):
                    break
                segment_length = struct.unpack(">H", content[index + 2:index + 4])[0]
                index += 2 + segment_length
        return 0, 0

    def upload_image_bytes(self, content, filename="image.jpg"):
        """使用微信二进制模式上传 Excel 内嵌图片。"""
        width, height = self._image_size(content)
        params = {
            "access_token": self.get_access_token(),
            "upload_type": 0,
            "resp_type": 1,
            "width": width,
            "height": height,
        }
        response = requests.post(
            f"{self.BASE}/shop/ec/basics/img/upload",
            params=params,
            files={"media": (os.path.basename(filename), content)},
            timeout=120,
        )
        data = response.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"[img_upload_file] 失败: {data}")
        picture = data.get("pic_file") or {}
        image_url = (data.get("img_url") or data.get("url")
                     or picture.get("img_url") or picture.get("temp_img_url"))
        if not image_url:
            raise RuntimeError(f"[img_upload_file] 响应中没有 img_url: {data}")
        return image_url

    def get_category_detail(self, cat_id):
        """获取叶子类目详情(属性定义 + 规格定义)。
        调用 /shop/ec/category/detail, 返回 attr 对象, 包含:
          - product_attr_list: 商品属性 [{name, type_v2, value, is_required, ...}]
          - sale_attr_list:    销售规格 [{name, type_v2, value, is_required, ...}]
        type_v2: string(文本) / select_one(单选) / select_many(多选)
                 / integer / decimal4 / integer_unit / decimal4_unit
        value:   候选值列表(select_one/select_many 时是选项, *_unit 时是单位)
        发品时 attrs 填 [{attr_key: name, attr_value: 用户选/填的值}],
        sku_attrs 填 [{attr_key: name, attr_value: 用户选的值}]。"""
        data = self._post("/shop/ec/category/detail", {"cat_id": int(cat_id)})
        return data.get("attr", {})

    def get_freight_template_detail(self, template_id):
        """查询单个运费模板详情(getfreighttemplatedetail)。
        返回 freight_template 对象:{template_id, name, valuation_type, send_time,
        address_info, delivery_type, shipping_method, is_default, ...}"""
        data = self._post("/channels/ec/merchant/getfreighttemplatedetail",
                          {"template_id": str(template_id)})
        return data.get("freight_template", {}) or {}

    def get_freight_templates(self, page_size=100, page_num=1):
        """获取微信小店运费模板列表(带模板名称,前端下拉框直接可用)。
        ⚠️ 官方 getfreighttemplatelist 只返回 template_id_list(纯 ID 数组),没有名称,
        前端按 templates/freight_templates/list 取值取不到 → 下拉框永远是空的。
        所以这里再逐个调 getfreighttemplatedetail 把名称补上,统一返回:
          {"templates": [{"template_id", "name", "is_default", ...}],
           "template_id_list": [原始ID数组], "total": 模板数}
        单个模板详情查询失败时降级为「名称=模板ID」,不影响整体返回。"""
        data = self._post("/channels/ec/merchant/getfreighttemplatelist", {
            "limit": page_size, "offset": max(page_num - 1, 0) * page_size,
        })
        ids = data.get("template_id_list") or []
        templates = []
        for tid in ids:
            item = {"template_id": str(tid), "name": str(tid), "is_default": False}
            try:
                ft = self.get_freight_template_detail(tid)
                if ft:
                    item.update({
                        "name": ft.get("name") or item["name"],
                        "is_default": bool(ft.get("is_default")),
                        "valuation_type": ft.get("valuation_type"),
                        "send_time": ft.get("send_time"),
                        "delivery_type": ft.get("delivery_type"),
                        "shipping_method": ft.get("shipping_method"),
                    })
            except RuntimeError as e:
                print(f"[freight_template] 模板 {tid} 详情查询失败,降级用ID当名称: {e}")
            templates.append(item)
        return {"templates": templates, "template_id_list": ids, "total": len(templates)}


    # =================================================================
    # 接口 2:添加商品(addproduct) —— 只创建草稿,不会上架
    # =================================================================
    def add_product(self, product: dict):
        """
        添加一个商品,返回 product_id。
        重要: 创建的是"草稿"!不会对外展示、不会审核。
              只有再调用 listing() 上架,才会提交审核、对外售卖。
        - product: 商品信息字典,最小结构见下方注释
        返回: product_id(商品编号,后面所有操作都靠它)
        """
        # 商品字典结构说明(字段以官方 addproduct 文档为准, 必填项):
        # {
        #     "title": "商品标题",                    # 必填, 最多60字符
        #     "short_title": "短标题",                # 选填, 最多20字符
        #     "out_product_id": "外部商品ID",          # 选填, 你系统的商品编码
        #     "head_imgs": ["img_url1", "img_url2", "img_url3"],
        #         ↑ 必填, 主图 至少3张最多9张, 填 upload_image 返回的 img_url
        #     "desc_info": {"imgs": ["img_url4"]},   # 选填, 详情图(1~50张)
        #     "cats_v2": [                            # 必填, 最新多级类目树
        #         {"cat_id": 一级类目ID},
        #         {"cat_id": 二级类目ID},
        #         {"cat_id": 三级类目ID},             # 最后一个是叶子类目
        #     ],
        #     "brand_id": "品牌ID",                   # 无品牌填 "2100000000"
        #     "deliver_method": 0,                    # 必填, 0=快递发货
        #     "extra_service": {                      # 必填, 额外服务
        #         "seven_day_return": 1,              # 七天无理由: 1=支持
        #         "freight_insurance": 0,             # 运费险: 0=不支持
        #     },
        #     "attrs": [{"attr_key": "材质", "attr_value": "18K金"}],
        #         ↑ 选填, 商品参数(按类目要求)
        #     "skus": [{                              # 必填, SKU列表(1~500个)
        #         "out_sku_id": "SKU-001",
        #         "sale_price": 1299900,              # 价格, 单位"分"!
        #         "stock_num": 100,                   # 库存
        #         "sku_attrs": [],                    # 规格(无规格留空数组)
        #     }],
        #     "listing": 0,   # 选填: 1=添加后立即上架(一步到位,免去再调listing)
        # }
        # ⚠️ 注意: 类目只提交 cats_v2。提交前会根据叶子 ID 从最新类目树重建完整链路，
        #    并移除旧 cats 字段，避免旧父级 ID 引发类目错误。
        #    SKU 价格字段叫 sale_price, 不是 price; 单位都是"分"。
        product = self._prepare_for_submit(product)  # 归一化类目 + 补运费(express_info)/售后(after_sale_info)兜底
        data = self._post("/channels/ec/product/add", {"product": product})
        pid = data["data"]["product_id"]
        print(f"[addproduct] 添加成功 product_id={pid}")
        return pid

    # =================================================================
    # 接口 3:更新商品(updateproduct)
    # =================================================================
    def update_product(self, product_id, product: dict):
        """
        更新商品信息(标题/价格/库存/图片等)。
        - product_id: 要更新的商品编号
        - product:    要修改的字段(字典,结构和 add_product 一样)
        注意: 更新消耗"提审限额",别频繁调用
        """
        product = self._prepare_for_submit(product)  # 同 add,统一整备(类目/运费/售后)
        data = self._post(
            "/channels/ec/product/update",
            {"product_id": product_id, "product": product},
        )
        print(f"[updateproduct] 更新成功 product_id={product_id}")
        return data

    # =================================================================
    # 接口 4:上架商品(listingproduct) —— 提交审核,审核通过才开卖
    # =================================================================
    def listing(self, product_id):
        """
        上架商品:把草稿/下架的商品提交审核。
        - 审核通过后:商品正式对外售卖
        - 审核中:重复提交会报错(先查状态再提交)
        - product_id: 商品编号
        """
        data = self._post(
            "/channels/ec/product/listing",
            {"product_id": str(product_id)},
        )
        print(f"[listingproduct] 已提交上架 product_id={product_id}")
        return data

    # =================================================================
    # 接口 5:下架商品(delistingproduct)
    # =================================================================
    def delisting(self, product_id):
        """
        下架商品:从店铺撤下,不再售卖(商品数据还在,随时能重新上架)。
        - product_id: 商品编号
        """
        data = self._post(
            "/channels/ec/product/delisting",
            {"product_id": str(product_id)},
        )
        print(f"[delistingproduct] 已下架 product_id={product_id}")
        return data

    # =================================================================
    # 接口 6:删除商品(deleteproduct) —— 彻底删除,不可恢复
    # =================================================================
    def delete_product(self, product_id):
        """
        删除商品:彻底删掉(不是下架!删除后不可恢复,评价/销量数据都没了)。
        日常建议用下架代替删除。
        - product_id: 商品编号
        """
        data = self._post(
            "/channels/ec/product/delete",
            {"product_id": str(product_id)},
        )
        print(f"[deleteproduct] 已删除 product_id={product_id}")
        return data

    # =================================================================
    # 附加:获取商品类目(查珠宝类目ID用)
    # =================================================================
    @staticmethod
    def _collect_cat_nodes(obj, nodes):
        """递归遍历返回结构,把所有的 "cat": {...} 节点收集成扁平列表。
        兼容真实返回: cats 顶层 + cat_and_qua 数组包装 + 任意嵌套。"""
        if isinstance(obj, dict):
            if "cat" in obj and isinstance(obj["cat"], dict):
                nodes.append(obj["cat"])
            for v in obj.values():
                WxStore._collect_cat_nodes(v, nodes)
        elif isinstance(obj, list):
            for v in obj:
                WxStore._collect_cat_nodes(v, nodes)

    def get_all_categories(self):
        """
        获取微信小店全部类目节点(接口:获取所有类目)。
        真实返回: {errcode:0, cats:[{cat_and_qua:[{cat:{cat_id,name,f_cat_id,leaf}}]}]}
        ⚠️ 注意: 不是 data.cat_list 嵌套树!所以这里用 GET 请求 +
        _collect_cat_nodes 递归收集所有 "cat" 节点,返回扁平列表。
        (旧实现取 data.cat_list 永远为空,是取不到类目的根源)
        返回: [{"cat_id":..., "name":..., "f_cat_id":..., "leaf":...}, ...]
        """
        token = self.get_access_token()
        for path in ("/channels/ec/category/all", "/shop/ec/category/all"):
            resp = requests.get(
                f"{self.BASE}{path}",
                params={"access_token": token},
                timeout=30,
            )
            data = resp.json()
            if data.get("errcode", 0) != 0:
                continue
            nodes = []
            self._collect_cat_nodes(data.get("cats_v2") or [], nodes)
            if nodes:
                seen, uniq = set(), []
                for n in nodes:
                    cid = n.get("cat_id")
                    if cid is not None and cid not in seen:
                        seen.add(cid)
                        uniq.append(n)
                return uniq
        raise RuntimeError("获取类目数据失败: /channels/ec/category/all 和 "
                           "/shop/ec/category/all 都返回错误")

    # -----------------------------------------------------------------
    # 发品类目归一化:根治 6600016 类目错误
    # 新旧类目树叶子 cat_id 一致,但一级/二级父 cat_id 完全不同且无映射。
    # 即使历史调用方仍传旧 cats，也只取末端叶子 ID，随后从新树重建 cats_v2；
    # 发往微信的最终请求永远不包含 cats。
    # -----------------------------------------------------------------
    def _categories_cached(self, ttl=6 * 3600):
        """带缓存的类目树(get_all_categories 已优先 cats_v2)。"""
        now = time.time()
        if self._cat_cache is None or now - self._cat_cache_ts > ttl:
            self._cat_cache = self.get_all_categories()
            self._cat_cache_ts = now
        return self._cat_cache

    @staticmethod
    def _as_cat_id(cid):
        """类目ID统一成 int(微信文档里 cat_id 是 number);非纯数字时原样返回字符串。"""
        s = str(cid).strip()
        return int(s) if s.isdigit() else s

    def _rebuild_cats_v2_chain(self, leaf_cat_id):
        """用叶子 cat_id 在新树 cats_v2 里回溯出完整链路 [一级..叶子];找不到返回 None。"""
        nodes = self._categories_cached()
        by_id = {str(n["cat_id"]): n for n in nodes if n.get("cat_id") is not None}
        cur = by_id.get(str(leaf_cat_id))
        if not cur:
            return None
        chain, seen = [], set()
        while cur and str(cur.get("cat_id")) not in seen:
            seen.add(str(cur.get("cat_id")))
            chain.append(cur.get("cat_id"))
            pid = cur.get("f_cat_id")
            cur = by_id.get(str(pid)) if pid is not None else None
        chain.reverse()
        return chain

    def normalize_category(self, product: dict):
        """发品前归一化类目字段为 cats_v2(新多级类目树)。
        取前端传来的 cats_v2/cats 链路末端的叶子 cat_id,在新树重建完整链路,
        统一用 cats_v2 发出并删掉 cats(二选一,传了 cats_v2 微信优先读它)。
        类目树拉取失败或叶子不在新树时直接阻止发布，避免旧类目请求进入平台。"""
        src = product.get("cats_v2") or product.get("cats") or []
        leaf = None
        for item in reversed(src):
            cid = item.get("cat_id") if isinstance(item, dict) else None
            if cid is not None and str(cid).strip() != "":
                leaf = cid
                break
        if leaf is None:
            raise RuntimeError("商品缺少 cats_v2 叶子类目")
        try:
            chain = self._rebuild_cats_v2_chain(leaf)
        except Exception as e:
            raise RuntimeError(f"无法读取最新 cats_v2 类目树: {e}") from e
        if chain:
            product.pop("cats", None)
            product["cats_v2"] = [{"cat_id": self._as_cat_id(cid)} for cid in chain]
            print(f"[addproduct] 类目已归一化为 cats_v2 链路: {chain}")
        else:
            raise RuntimeError(f"叶子类目 {leaf} 不存在于最新 cats_v2 类目树")
        return product

    def _prepare_for_submit(self, product: dict):
        """发品/更新前统一整备 payload,补齐官方要求但前端易漏的字段(不覆盖调用方已填值):
        1. 类目归一化为官方格式(cats_v2:[新树链路])并移除 cats;
        2. 运费:把顶层 freight_template_id 迁到 express_info.template_id(官方字段),
           两者都缺且为快递发货时用默认模板兜底,避免 6600120「查询模板ID失败」;
        3. 售后:after_sale_info.after_sale_address_id 现为必填,缺失时用默认退货地址兜底。
        仅在字段缺失时补默认值,调用方已显式提供的值一律保留,不改变既有行为。"""
        product = self.normalize_category(product)

        # 运费模板 → express_info.template_id(官方字段在 express_info 内,顶层字段会被忽略)
        top_tid = product.pop("freight_template_id", None)
        express = product.get("express_info")
        express = dict(express) if isinstance(express, dict) else {}
        if not express.get("template_id"):
            tid = top_tid or DEFAULT_FREIGHT_TEMPLATE_ID
            # deliver_method=1/3(无需快递)时官方不需要运费模板,不兜底
            if tid and product.get("deliver_method", 0) in (0, None):
                express["template_id"] = str(tid)
        if express:
            product["express_info"] = express

        # 售后/退货地址(官方已改为必填)兜底
        asi = product.get("after_sale_info")
        asi = dict(asi) if isinstance(asi, dict) else {}
        if not asi.get("after_sale_address_id") and DEFAULT_AFTER_SALE_ADDRESS_ID:
            asi["after_sale_address_id"] = str(DEFAULT_AFTER_SALE_ADDRESS_ID)
        if asi:
            product["after_sale_info"] = asi

        return product

    def find_category(self, keyword):
        """
        按关键词搜索类目,打印"类目路径 + cat_id"。
        用法: client.find_category("珠宝")   # 找到所有含"珠宝"的类目
        返回: [(cat_id, "一级 > 二级 > 三级", is_leaf), ...]
        原理: 节点靠 f_cat_id 指向父级,从命中的节点一路向上回溯拼出完整路径
        """
        nodes = self.get_all_categories()

        # 建立 cat_id -> 节点 的索引,方便回溯父级
        by_id = {}
        for n in nodes:
            if isinstance(n, dict) and n.get("cat_id") is not None:
                by_id[str(n["cat_id"])] = n

        results = []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            # 全字段匹配:不管名称字段叫什么,只要含关键词就命中
            hay = " ".join(str(v) for v in n.values() if v is not None)
            if keyword not in hay:
                continue
            # 从当前节点一路向上回溯 f_cat_id,拼出完整路径
            chain, cur, seen = [], n, set()
            while cur and str(cur.get("cat_id")) not in seen:
                seen.add(str(cur.get("cat_id")))
                chain.append((cur.get("cat_id"), cur.get("name", "")))
                pid = cur.get("f_cat_id")
                cur = by_id.get(str(pid)) if pid is not None else None
            chain.reverse()
            path_str = " > ".join(f"{nm}({cid})" for cid, nm in chain)
            results.append((n.get("cat_id"), path_str, bool(n.get("leaf"))))

        if not results:
            print(f"[find_category] 没找到包含「{keyword}」的类目")
        else:
            print(f"[find_category] 找到 {len(results)} 个包含「{keyword}」的类目:")
            for cid, path, is_leaf in results:
                mark = " ★叶子(可发商品)" if is_leaf else ""
                print(f"  {cid}  {path}{mark}")
        return results

    # =================================================================
    # 附加:查询商品(查审核状态)
    # =================================================================
    def get_product(self, product_id):
        """
        查询商品详情,重点是看审核状态。
        - product_id: 商品编号
        返回的数据里:
          status  = 上架状态(0=未上架 5=已上架 11=已下架,以官方文档为准)
          audit_info = 审核信息(审核中/通过/驳回及驳回原因)
        ⚠️ 注意: 微信返回的 product / audit_info 在顶层,不在 data 里
        """
        data = self._post("/channels/ec/product/get", {"product_id": str(product_id)})
        product = data.get("product", {})
        print(f"[getproduct] status={product.get('status')} "
              f"audit={data.get('audit_info')}")
        return data

    # =================================================================
    # 附加:查询商品列表(分页,可按状态过滤) —— 前端商品列表页用
    # =================================================================
    def list_products(self, status=None, page_size=10, next_key=None):
        """
        查询商品列表(分页,按状态过滤)。
        注意: 微信 list/get 接口返回的是 product_ids(ID列表),不是完整商品,
              所以这里再逐个查详情组装完整信息,前端可直接展示。
        - status:    0=未上架 1=已上架 2=已下架;None=全部
        - page_size: 每页条数(默认10,最大30)
        - next_key:  上一页返回的翻页游标;第一页不传
        返回: {"products": [完整商品对象...], "next_key": 下一页游标, "total": 总数}
        """
        payload = {"page_size": page_size}
        if status is not None:
            payload["status"] = status
        if next_key:
            payload["next_key"] = next_key
        data = self._post("/channels/ec/product/list/get", payload)
        d = data.get("data", data)  # 兼容 data 包装或顶层返回
        # 微信只返回商品ID列表,逐个查详情组装(单个失败不阻塞整页)
        products = []
        for pid in d.get("product_ids", []):
            try:
                p = self.get_product(pid)
                prod = p.get("product", {})
                # 审核信息在顶层,合并进商品对象供前端直接展示
                prod["audit_info"] = p.get("audit_info")
                products.append(prod)
            except RuntimeError as e:
                products.append({"product_id": pid, "title": f"(详情获取失败:{e})"})
        return {
            "products": products,
            "next_key": d.get("next_key"),
            "total": d.get("total_num", 0),
        }


# =====================================================================
# 演示流程:一键跑通"传图 → 发品 → 上架 → 查状态"
# =====================================================================
if __name__ == "__main__":
    # 安全检查:如果没填 AppID,直接提醒,不往下跑
    assert APPID.startswith("wx"), "请先设置 WX_APPID 环境变量"

    # 1. 创建客户端(记住凭证)
    client = WxStore(APPID, SECRET)

    # 2. 上传主图(把下面的地址换成你们真实的商品图地址,图片必须公网能访问)
    img_url = client.upload_image("https://你的OSS地址/主图1.jpg")

    # 3. 添加商品(先进草稿,不会影响店铺)
    pid = client.add_product({
        "title": "测试-18K金钻石戒指",              # 标题
        "short_title": "18K金钻戒",                # 短标题
        "out_product_id": "TEST-001",              # 外部商品ID(你自己的编码)
        "head_imgs": [img_url, img_url, img_url],  # 主图(至少3张,先用同一张测试)
        "desc_info": {"imgs": [img_url]},          # 详情图
        "cats_v2": [                                # 最新类目树完整链路
            {"cat_id": 一级类目ID},
            {"cat_id": 二级类目ID},
            {"cat_id": 三级类目ID},
        ],
        "brand_id": "2100000000",                  # 无品牌用这个值,有品牌填品牌ID
        "deliver_method": 0,                       # 0=快递发货
        "extra_service": {                         # 额外服务(必填)
            "seven_day_return": 1,                 # 支持七天无理由
            "freight_insurance": 0,                # 暂不支持运费险
        },
        "skus": [{                                  # SKU 列表
            "out_sku_id": "SKU-001",
            "sale_price": 1299900,                  # 价格单位是"分"!12999元=1299900
            "stock_num": 100,                       # 库存
            "sku_attrs": [],                        # 无规格就留空
        }],
        # 想一步上架?把下面这行取消注释,就不需要再调 listing 了:
        # "listing": 1,
    })

    # 4. 上架(提交审核) —— 到这一步才开始审核,审核通过才对外
    #    (如果只是测试,可以注释掉这行,只保留草稿)
    client.listing(pid)

    # 5. 查状态:每隔30秒查一次审核结果,最多查10次(约5分钟)
    for i in range(10):
        time.sleep(30)
        try:
            client.get_product(pid)
        except RuntimeError as e:
            print("查询失败:", e)

    print("演示完成")
