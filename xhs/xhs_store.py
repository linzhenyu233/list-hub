# -*- coding: utf-8 -*-
"""
====================================================================
 小红书开放平台 · 商品 API 客户端 (Python) —— 带详细注释教学版
====================================================================
这个脚本用来"自动操作"你的小红书店铺(千帆),覆盖 9 个商品接口:

  Item(商品) 级:
  1. create_item          创建商品  product.createItemV2
  2. update_item          更新商品  product.updateItemV2
  3. create_item_and_sku  创建商品+SKU(product.createItemAndSku)
  4. update_item_and_sku  更新商品+SKU(product.updateItemAndSku)

  SKU(规格) 级:
  5. create_sku           创建SKU   product.createSkuV2
  6. update_sku           更新SKU   product.updateSkuV2
  7. delete_skus          删除SKU   product.deleteSkuV2

  状态/素材:
  8. set_sku_available    商品上下架 product.updateSkuAvailable
  9. update_item_image    修改主图/主图视频 product.updateItemImage

运行前提:
  - 已安装 requests: pip install requests
  - 已在 open.xiaohongshu.com 注册开发者并创建应用,拿到 appId + appSecret
  - 已完成店铺授权,拿到该店铺的 accessToken(见下方「授权流程」)

怎么运行:
  1. 把 APP_ID / APP_SECRET / ACCESS_TOKEN 换成自己的
  2. 把 __main__ 演示里的商品字段换成真实的
  3. 执行:  python xhs_store_client.py

====================================================================
 核心概念
====================================================================
  - accessToken: 店铺的"授权凭证",7 天有效;refreshToken 14 天有效。
    一个 token 对应一家店铺,多店铺要各自维护。
  - itemId / skuId: 商品 ID / SKU ID(小红书后台商品列表能看到,
    "商品id:" 后那串数字+小写字母就是 itemId)。
  - 签名(sign): 小红书所有业务接口都要求对公共参数做 MD5 签名,
    签错一律报鉴权失败,这是对接时最容易踩的坑(本脚本已封装好)。
  - 上下架: 小红书以 SKU 为粒度上下架(set_sku_available),
    不存在"整个商品一键上架"的接口,批量商品要遍历其 SKU 逐个操作。

====================================================================
 授权流程(自研应用,一次性操作,之后靠 refreshToken 续期)
====================================================================
  1. 生成授权链接(浏览器打开,用店铺主账号登录授权):
       https://ark.xiaohongshu.com/ark/authorization?appId=你的APPID&redirectUri=你的回调地址&state=随便填
  2. 授权成功后,code 会回调到你填的 redirectUri 上(形如
     https://你的回调/?code=xxxxx&state=随便填),code 10 分钟有效。
  3. 用 code 换 token:调 oauth.getAccessToken 方法(见本文件
     get_access_token 方法),拿到 accessToken + refreshToken 入库。
  4. 之后每次调业务接口,把 accessToken 塞进公共参数即可。
     accessToken 剩 <30 分钟时,用 refreshToken 换新的(见
     refresh_access_token 方法)。
====================================================================
"""

import os
import sys
import time          # 生成 timestamp
import hashlib       # 做 MD5 签名
import requests      # 发 HTTP 请求

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime_config import load_project_env

load_project_env()


# ------------------------------------------------------------------
# 配置区:换成自己的(也可以设环境变量)
# ------------------------------------------------------------------
APP_ID = os.environ.get("XHS_APP_ID", "")
APP_SECRET = os.environ.get("XHS_APP_SECRET", "")
ACCESS_TOKEN = os.environ.get("XHS_ACCESS_TOKEN", "")


class XhsStore:
    """
    小红书店铺操作类(商品域)。
    用法:
        client = XhsStore(app_id, app_secret, access_token)
        client.create_item({...})     # 创建商品
        client.set_sku_available(sku_id, 1)   # 上架某个 SKU
    """

    # 所有业务接口 + 换 token 接口,都走这一个网关(官方规定)
    BASE = "https://ark.xiaohongshu.com/ark/open_api/v3/common_controller"

    def __init__(self, app_id, app_secret, access_token=None):
        """
        - app_id:       开放平台应用 appId
        - app_secret:   应用 appSecret(参与签名)
        - access_token: 店铺授权凭证;不传则必须在调用前 get_access_token()
        """
        self.app_id = app_id
        self.app_secret = app_secret
        self.access_token = access_token
        # token 到期时间(秒级时间戳),refresh 时用来判断是否需要续
        self._token_expire_at = 0

    # =================================================================
    # 签名(小红书所有业务接口的前提,最容易出错的地方)
    # =================================================================
    def _sign(self, method, timestamp):
        """
        生成签名 sign,规则(官方文档「签名算法」一节):
          1. 只取 4 个系统参数拼成字符串:
                 method?appId=xxx&timestamp=xxx&version=2.0
          2. 把 appSecret 直接连在末尾
          3. 对整个字符串做 MD5(小写)
        注意: accessToken 不参与签名!别把它拼进去,否则必错。
        """
        raw = f"{method}?appId={self.app_id}&timestamp={timestamp}&version=2.0{self.app_secret}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    # =================================================================
    # 统一发 POST 请求(公共参数 + 业务参数一起塞进 body)
    # =================================================================
    def _post(self, method, payload=None):
        """
        向小红书网关发 POST 请求。
        - method:  接口名,如 product.createItemV2
        - payload: 业务参数(dict)
        返回: data 字段内容(已帮你剥掉 error_code/success 壳)
        失败: 抛 RuntimeError,带小红书错误码和信息
        """
        timestamp = str(int(time.time()))  # 当前 unix 时间戳(秒)
        body = {
            # ---- 公共参数(每个接口必须) ----
            "timestamp": timestamp,
            "appId": self.app_id,
            "sign": self._sign(method, timestamp),   # 自动算签名
            "version": "2.0",
            "method": method,
            # ---- 业务参数 ----
            **(payload or {}),
        }
        # 商家业务接口必须带 accessToken(oauth.getAccessToken 等授权接口除外)
        if self.access_token and method != "oauth.getAccessToken":
            body["accessToken"] = self.access_token

        resp = requests.post(
            self.BASE,
            headers={"Content-Type": "application/json;charset=utf-8"},
            json=body,
            timeout=15,
        )
        data = resp.json()

        # 返回约定: {"error_code": 0, "data": {...}, "success": true}
        if data.get("error_code") != 0 or not data.get("success"):
            raise RuntimeError(
                f"[{method}] 调用失败: error_code={data.get('error_code')} "
                f"data={data.get('data')} success={data.get('success')}"
            )
        return data.get("data")

    # =================================================================
    # 授权:用 code 换 accessToken / refreshToken(首次授权时调一次)
    # =================================================================
    def get_access_token(self, code):
        """
        用授权回调拿到的 code 换取 accessToken。
        - code: 回调地址 ?code= 后面的值(10 分钟有效)
        返回: {"accessToken":..., "refreshToken":..., "expiresIn":...}
        """
        data = self._post("oauth.getAccessToken", {"code": code})
        self.access_token = data.get("accessToken") or data.get("access_token")
        print(f"[oauth.getAccessToken] 获取成功 accessToken={self.access_token}")
        return data

    # =================================================================
    # 授权:用 refreshToken 续期(accessToken 剩 <30 分钟时调用)
    # =================================================================
    def refresh_access_token(self, refresh_token):
        """
        用 refreshToken 换新的 accessToken(14 天有效期)。
        - refresh_token: 首次换 token 时返回的 refreshToken
        返回: 新的 token 信息
        """
        data = self._post("oauth.refreshAccessToken", {"refreshToken": refresh_token})
        self.access_token = data.get("accessToken") or data.get("access_token")
        print(f"[oauth.refreshAccessToken] 刷新成功 accessToken={self.access_token}")
        return data

    # =================================================================
    # 接口 1:创建商品(product.createItemV2)
    # =================================================================
    def create_item(self, item: dict):
        """
        创建商品(Item)。返回 itemId(商品ID,后续更新/加SKU都靠它)。
        - item: 商品信息字典,必填字段(实测 V2 接口字段名,与旧文档不同):
            {
              "name": "商品标题",                          # 必填
              "brandId": "品牌ID",                        # 必填(用 common.brandSearch 查)
              "categoryId": "末级叶子类目ID",               # 必填(用 common.getCategories 查叶子)
              "attributes": [                             # 类目属性(用「由末级分类获取属性」查好)
                  {"propertyId": "属性ID", "name": "属性名",
                   "valueId": "属性值ID", "value": "属性值"}
              ],
              "shippingTemplateId": "运费模板ID",           # 必填(用 common.getCarriageTemplateList 查)
              "shippingGrossWeight": 500,                 # 物流重量(克)
              "variantIds": ["规格ID"],                    # 规格列表(由末级分类查规格)
              "images": ["https://...主图1.jpg"],          # 主图(必填!字段名是 images 不是 imageUrls)
              "videoUrl": "",                             # 主图视频(没有留空)
              "articleNo": "货号",                         # 商品货号
              "imageDescriptions": ["https://...详情图.jpg"],  # 图文描述(字段名是 imageDescriptions)
              "transparentImage": "",                     # 透明图(选填)
              "description": "商品描述",
              "faq": [{"question": "Q", "answer": "A"}],  # 选填
              "deliveryMode": "0",                        # 0=普通物流
              "freeReturn": "1",                          # 1=支持7天无理由
            }
        ⚠️ 实测关键点:
          - 主图字段是 images(不是文档写的 imageUrls)!
          - 详情图字段是 imageDescriptions(不是 imageDescUrls)!
          - 图片必须先 material.uploadMaterial 上传素材拿 URL
          - categoryId 必须是叶子类目(isLeaf=true),父类目报错
        返回: itemId
        """
        data = self._post("product.createItemV2", item)
        item_id = data.get("itemId") or data.get("id")
        print(f"[product.createItemV2] 创建成功 itemId={item_id}")
        return item_id

    # =================================================================
    # 接口 2:更新商品(product.updateItemV2)
    # =================================================================
    def update_item(self, item_id, item: dict, updated_fields=None):
        """
        更新商品。item 结构与 create_item 一致,额外要求:
        - item_id:        要更新的商品ID(必填)
        - updated_fields: 只更新指定字段列表,如 ["name","imageUrls"];
                          不传=全量更新(全量更新有风险,建议显式传)
        返回: 更新后的商品信息
        """
        payload = {"id": item_id, **item} # 更新的商品ID
        if updated_fields:
            payload["updatedFields"] = updated_fields # 只更新指定字段
        data = self._post("product.updateItemV2", payload) # 更新商品
        print(f"[product.updateItemV2] 更新成功 itemId={item_id}")
        return data

    # =================================================================
    # 接口 3:创建商品+SKU(product.createItemAndSku)
    # =================================================================
    def create_item_and_sku(self, item: dict, sku_list: list):
        """
        创建商品+它的多个 SKU。
        - item:     商品信息(结构同 create_item,不含 SKU)
        - sku_list: SKU 列表,每项结构见 create_sku 的 sku 参数
        返回: {"itemId":..., "skuIds":[...]}
        ⚠️ 实测:平台 product.createItemAndSku 接口有 bug(字段体系混乱,任何格式
        都报"struct format"或"主图/标题不能为空"错误),本方法改为分步:
        先 createItemV2 建商品,再 createSkuV2 逐个建 SKU(两者都实测可用)。
        """
        # ---- 分步创建(可靠) ----
        item_id = self.create_item(item)
        sku_ids = []
        for sku in sku_list:
            sku_ids.append(self.create_sku(item_id, sku))
        print(f"[create_item_and_sku] 分步创建完成 itemId={item_id} skuIds={sku_ids}")
        return {"itemId": item_id, "skuIds": sku_ids}

    # =================================================================
    # 接口 4:更新商品+SKU(product.updateItemAndSku)
    # =================================================================
    def update_item_and_sku(self, item_id, item: dict, sku_list: list):
        """
        一次调用同时更新商品信息和它的多个 SKU。
        - item_id:  商品ID
        - item:     要更新的商品字段(只传要改的即可)
        - sku_list: SKU 列表(每项要带 skuId 才能更新,新增的SKU不带id)
        返回: 更新结果
        """
        payload = {"id": item_id, **item, "skuList": sku_list}
        data = self._post("product.updateItemAndSku", payload)
        print(f"[product.updateItemAndSku] 更新成功 itemId={item_id}")
        return data

    # =================================================================
    # 接口 5:创建SKU(product.createSkuV2)
    # =================================================================
    def create_sku(self, item_id, sku: dict):
        """
        给已有商品追加一个 SKU。
        - item_id: 所属商品ID
        - sku: SKU 信息,常用字段(实测通过):
            {
              "ipq": 1,                    # 打包数,只允许 1
              "originalPrice": 8800,       # 市场价,单位"分"
              "price": 8000,               # 售价,单位"分"
              "stock": 100,                # 库存
              "logisticsPlanId": "物流方案ID",  # 必填!用 common.getLogisticsList 查
              "erpCode": "商家编码",
              "variants": [                # 规格(无规格留空数组)
                  {"id": "规格ID", "name": "尺码",
                   "value": "36", "valueId": "规格值ID"}
              ],
              "deliveryTime": {"time": "24", "type": "RELATIVE_TIME_NEW"},
                  # 必填!发货时间要符合类目履约规则,先用
                  # common.getDeliveryRule 查允许的 timeType/value,
                  # 然后 time=value, type 用 RELATIVE_TIME_NEW(小时)
              "specImage": "https://...规格图.jpg",
              "barcode": "条形码",          # 特定品类必填
            }
        ⚠️ 实测关键点:
          - logisticsPlanId 是物流方案ID(不是运费模板ID)!
            用 common.getLogisticsList 查,字段是 planInfoId
          - deliveryTime 必填,time 必须符合类目规则
            (getDeliveryRule 里 timeType=4 时 value=24/48 → time=24,48)
        返回: 新 SKU 的 id
        """
        payload = {"itemId": item_id, **sku}
        data = self._post("product.createSkuV2", payload)
        sku_id = data.get("id")
        print(f"[product.createSkuV2] 创建成功 skuId={sku_id}")
        return sku_id

    # =================================================================
    # 接口 6:更新SKU(product.updateSkuV2)
    # =================================================================
    def update_sku(self, sku_id, sku: dict, updated_fields=None):
        """
        更新某个 SKU(价格/库存/规格图等)。
        - sku_id:        要更新的 SKU ID
        - sku:           要改的字段(结构同 create_sku,但不用再带 itemId)
        - updated_fields: 只更新指定字段列表;不传=全量
        返回: 更新后的 SKU 信息
        """
        payload = {"id": sku_id, **sku}
        if updated_fields:
            payload["updatedFields"] = updated_fields
        data = self._post("product.updateSkuV2", payload)
        print(f"[product.updateSkuV2] 更新成功 skuId={sku_id}")
        return data

    # =================================================================
    # 接口 7:删除SKU(product.deleteSkuV2)
    # =================================================================
    def delete_skus(self, sku_ids):
        """
        批量删除 SKU(删除后不可恢复,谨慎使用)。
        - sku_ids: 待删除的 skuId 列表,如 ["sku1", "sku2"]
        """
        if isinstance(sku_ids, str):
            sku_ids = [sku_ids]
        data = self._post("product.deleteSkuV2", {"skuIds": sku_ids})
        print(f"[product.deleteSkuV2] 删除成功 skuIds={sku_ids}")
        return data

    # =================================================================
    # 接口 8:商品上下架(product.updateSkuAvailable)
    # =================================================================
    def set_sku_available(self, sku_id, available):
        """
        上下架一个 SKU。小红书按 SKU 粒度上下架,商品下的每个 SKU
        都要单独调一次。
        - sku_id:    SKU ID
        - available: 1=上架(买家可见可买); 0=下架
        返回: 结果
        """
        data = self._post("product.updateSkuAvailable", {
            "skuId": str(sku_id),
            "available": str(available),
        })
        print(f"[product.updateSkuAvailable] {'上架' if available else '下架'}成功 skuId={sku_id}")
        return data

    # =================================================================
    # 接口 9:修改商品主图、主图视频(product.updateItemImage)
    # =================================================================
    def update_item_image(self, item_id, material_urls, material_type=1):
        """
        修改商品主图或主图视频。
        - item_id:       商品ID
        - material_type: 素材类型 1=图片 2=视频
        - material_urls: 素材URL列表
                         图片=全量覆盖(传入的整组替换原主图,顺序即展示顺序);
                         视频=取列表第一个
        返回: 结果
        """
        data = self._post("product.updateItemImage", {
            "itemId": str(item_id),
            "materialType": str(material_type),
            "materialUrls": material_urls,
        })
        kind = "主图" if int(material_type) == 1 else "主图视频"
        print(f"[product.updateItemImage] 修改{kind}成功 itemId={item_id}")
        return data


# =====================================================================
# 演示流程(把下面字段换成真实的再跑)
# =====================================================================
if __name__ == "__main__":
    # 安全检查:没填 appId 就直接提醒,不往下跑
    assert APP_ID and APP_SECRET and ACCESS_TOKEN, (
        "请先设置 XHS_APP_ID / XHS_APP_SECRET / XHS_ACCESS_TOKEN 环境变量"
    )

    # 1. 创建客户端(记住凭证;access_token 已配好)
    client = XhsStore(APP_ID, APP_SECRET, ACCESS_TOKEN)

    # 2. 创建商品(用测试店铺实测通过的真实参数)
    item_id = client.create_item({
        "name": "测试-50分钻石戒指",
        "brandId": "428982",                  # 测试品牌(common.brandSearch 查)
        "categoryId": "65f9975f3e946300016b345d",  # 珠宝>定制玉石>定制其他玉石(叶子)
        "attributes": [],                    # 用「由末级分类获取属性」查好再填
        "shippingTemplateId": "68c91cdc1daa970001689e13",  # 系统默认运费模板
        "shippingGrossWeight": 500,
        "variantIds": [],                    # 用「由末级分类获取规格」查
        "images": ["https://qimg.xiaohongshu.com/material_space/0a498e74-2093-4705-a290-4f0de9b9f6c6"],
        "videoUrl": "",
        "articleNo": "JEWEL-001",
        "imageDescriptions": ["https://qimg.xiaohongshu.com/material_space/0a498e74-2093-4705-a290-4f0de9b9f6c6"],
        "description": "测试商品",
        "deliveryMode": "0",
        "freeReturn": "1",
    })

    # 3. 给商品加一个 SKU
    sku_id = client.create_sku(item_id, {
        "originalPrice": 100,                # 单位"分",测试店铺会被强制改 0.1 元
        "price": 100,
        "stock": 200,
        "logisticsPlanId": "68f9ea252ef39300158a9e80",   # 物流方案ID(common.getLogisticsList 查)
        "variants": [],
        "deliveryTime": {"time": "24", "type": "RELATIVE_TIME_NEW"},  # 24小时发货
    })

    # 4. ⚠️ 测试环境不要上架!注释掉下面这行,避免影响店铺
    #client.set_sku_available(sku_id, 1)

    # 5. 改主图(换图后全量覆盖)
    # client.update_item_image(item_id, ["https://你的图床/新主图.jpg"], material_type=1)

    print("演示完成")
