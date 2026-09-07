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
import time    # 用来算 token 过期时间、测试时等待
import requests  # 发 HTTP 请求的库(微信接口都是 HTTP 接口)

# ------------------------------------------------------------------
# 配置区:把下面两个值换成你自己的
# (也可以不改这里,运行前设置环境变量 WX_APPID 和 WX_SECRET,更安全)
# ------------------------------------------------------------------
APPID = os.environ.get("WX_APPID", "替换成你的AppID")      # 小店 AppID, wx 开头
SECRET = os.environ.get("WX_SECRET", "替换成你的AppSecret")  # 小店 AppSecret, 一串字母数字


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

        img_url = data.get("img_url") or data.get("url")
        if not img_url:
            raise RuntimeError(f"[img_upload] 响应里没有 img_url: {data}")
        print(f"[img_upload] 上传成功 img_url={img_url}")
        return img_url

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
        #     "cats": [                               # 必填, 商品类目(旧三级类目树)
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
        # ⚠️ 注意: 类目用"cats"数组(旧三级类目树, 和 cat_finder 输出配套);
        #    也可以用"cats_v2"(新多级类目树), 二选一别都传。
        #    SKU 价格字段叫 sale_price, 不是 price; 单位都是"分"。
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
    def get_all_categories(self):
        """
        获取微信小店全部类目树(接口:获取所有类目)。
        返回: 嵌套的类目列表,每个类目有 cat_id / name / children
        注意: 类目树很大(几千个),调用一次即可缓存复用
        """
        data = self._post("/channels/ec/category/all")
        return data.get("data", {}).get("cat_list", [])

    def find_category(self, keyword):
        """
        按关键词搜索类目,打印"类目路径 + cat_id"。
        用法: client.find_category("珠宝")   # 找到所有含"珠宝"的类目
        返回: [(cat_id, "一级 > 二级 > 三级"), ...]
        """
        cats = self.get_all_categories()
        results = []

        # 递归遍历类目树,记录每一级的路径
        def walk(items, path):
            for c in items:
                name = c.get("name", "")
                children = c.get("children", [])
                cur_path = path + [name]
                if keyword in name:
                    # 第三个元素: True=叶子类目(能发商品), False=还有下级
                    results.append((c.get("cat_id"), " > ".join(cur_path), not children))
                walk(children, cur_path)

        walk(cats, [])
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
          status  = 上架状态(0=未上架 1=已上架 2=已下架,以官方文档为准)
          audit_info = 审核信息(审核中/通过/驳回及驳回原因)
        """
        data = self._post("/channels/ec/product/get", {"product_id": str(product_id)})
        product = data.get("data", {}).get("product", {})
        print(f"[getproduct] status={product.get('status')} "
              f"audit={product.get('audit_info')}")
        return data


# =====================================================================
# 演示流程:一键跑通"传图 → 发品 → 上架 → 查状态"
# =====================================================================
if __name__ == "__main__":
    # 安全检查:如果没填 AppID,直接提醒,不往下跑
    assert APPID.startswith("wx"), "请先设置 WX_APPID 环境变量,或直接改文件里的 APPID"

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
        "cats": [                                   # ⚠️ 类目(用cats,和cat_finder配套;三级都填真实数字ID)
            {"cat_id": '一级类目ID'},
            {"cat_id": '二级类目ID'},
            {"cat_id": '三级类目ID'},
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