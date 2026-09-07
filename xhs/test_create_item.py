# -*- coding: utf-8 -*-
"""
小红书测试店铺 · 全链路安全测试
流程: 下载图片 → 上传素材 → 创建商品 → 查询验证(不上下架,买家不可见)
"""
import os
import time
import hashlib
import json
import base64
import requests

APP_ID = os.environ.get("XHS_APP_ID", "")
APP_SECRET = os.environ.get("XHS_APP_SECRET", "")
ACCESS_TOKEN = os.environ.get("XHS_ACCESS_TOKEN", "")
BASE = 'https://ark.xiaohongshu.com/ark/open_api/v3/common_controller'


def call(method, payload=None):
    ts = str(int(time.time()))
    raw = f'{method}?appId={APP_ID}&timestamp={ts}&version=2.0{APP_SECRET}'
    sign = hashlib.md5(raw.encode('utf-8')).hexdigest()
    body = {'timestamp': ts, 'appId': APP_ID, 'sign': sign,
            'version': '2.0', 'method': method, 'accessToken': ACCESS_TOKEN,
            **(payload or {})}
    r = requests.post(BASE, headers={'Content-Type': 'application/json;charset=utf-8'},
                      json=body, timeout=30)
    d = r.json()
    if d.get('error_code') != 0:
        raise RuntimeError(f'[{method}] {json.dumps(d, ensure_ascii=False)}')
    return d.get('data')


def upload_image_from_url(url, name='test.jpg'):
    """下载公网图片 → base64 → 上传小红书素材,返回素材 URL"""
    img = requests.get(url, timeout=20).content
    b64 = base64.b64encode(img).decode('utf-8')
    # materialContent: 文档写 array[string],但实测传数组报 -8103
    # 尝试传单个字符串 / 传对象包装两种格式
    for payload in [
        {'name': name, 'type': 'IMAGE', 'materialContent': [b64]},
        {'name': name, 'type': 'IMAGE', 'materialContent': b64},
        {'name': name, 'type': 'IMAGE', 'materialContent': [{'content': b64}]},
    ]:
        try:
            data = call('material.uploadMaterial', payload)
            print(f'  素材上传成功(格式{list(payload.keys())}) url={data.get("url")}')
            return data.get('url')
        except RuntimeError as e:
            print(f'  格式尝试失败: {str(e)[:100]}')
    raise RuntimeError('素材上传 3 种格式全部失败')


if __name__ == '__main__':
    # 1. 上传主图(用一张公网图)
    print('1. 上传素材(主图)')
    img_url = upload_image_from_url('https://picsum.photos/seed/ring/800/800')

    # 2. 创建商品(实测正确字段: images/imageDescriptions,叶子类目)
    print('2. 创建商品')
    item = {
        'name': '测试-18K金钻石戒指',
        'brandId': '428982',
        'categoryId': '65f9975f3e946300016b345d',
        'attributes': [],
        'shippingTemplateId': '68c91cdc1daa970001689e13',
        'shippingGrossWeight': 500,
        'variantIds': [],
        'images': [img_url],
        'videoUrl': '',
        'articleNo': 'TEST-001',
        'imageDescriptions': [img_url],
        'description': '测试商品,勿拍',
        'deliveryMode': '0',
        'freeReturn': '1',
    }
    d = call('product.createItemV2', item)
    print(f'  创建成功: {json.dumps(d, ensure_ascii=False)[:300]}')
    item_id = d.get('itemId') or d.get('id')
    print(f'  itemId = {item_id}')

    # 3. 创建 SKU(物流方案+发货时间必填)
    print('3. 创建 SKU')
    sku = {
        'itemId': item_id,
        'ipq': 1,
        'originalPrice': 100,      # 测试店铺会强制改价 0.1 元
        'price': 100,
        'stock': 100,
        'logisticsPlanId': '68f9ea252ef39300158a9e80',
        'variants': [],
        'deliveryTime': {'time': '24', 'type': 'RELATIVE_TIME_NEW'},
        'erpCode': 'TEST-SKU-001',
    }
    d = call('product.createSkuV2', sku)
    print(f'  SKU 创建成功: {json.dumps(d, ensure_ascii=False)[:300]}')
    sku_id = d.get('id') or d.get('itemId')
    print(f'  skuId = {sku_id}')

    # 4. 查询验证(获取商品详情,确认创建成功)
    print('4. 查询商品验证')
    try:
        d = call('product.getDetailItem', {'itemId': item_id})
        print(f'  查询成功: {json.dumps(d, ensure_ascii=False)[:400]}')
    except Exception as e:
        print(f'  查询接口: {e}')

    print()
    print('=' * 50)
    print('✅ 测试完成!商品已创建在测试店铺后台(ark.xiaohongshu.com)')
    print(f'   itemId={item_id}  skuId={sku_id}')
    print('   未调用上下架接口,买家不可见;可去测试店铺后台查看')
