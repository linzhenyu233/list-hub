# -*- coding: utf-8 -*-
"""
小红书测试店铺 · 查询创建商品所需真实参数
已实测确认的公共接口 method 名:
  common.getCategories            获取分类列表(categoryId 空=一级分类)
  common.brandSearch              品牌搜索(必填 categoryId 末级类目ID)
  common.getCarriageTemplateList  运费模板列表(→ templateId)
  common.getLogisticsList         物流方案列表(→ planInfoId) ★用于SKU的 logisticsPlanId
  common.getDeliveryRule          发货时间规则(logisticsPlanId+categoryId)
  common.getVariations            由末级分类获取规格
  common.getAttributeLists        由末级分类获取属性
  material.uploadMaterial         上传素材(materialContent 直接传 base64 字符串,不是数组!)
"""
import os
import time
import hashlib
import json
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
                      json=body, timeout=15)
    d = r.json()
    if d.get('error_code') != 0:
        raise RuntimeError(f'{method}: {json.dumps(d, ensure_ascii=False)}')
    return d.get('data')


def find_category_chain(keyword, max_depth=4):
    """BFS 遍历分类树,找含关键词的叶子类目,返回 {id: 完整路径}"""
    results = {}

    def walk(cat_id, path, depth):
        if depth > max_depth:
            return
        try:
            data = call('common.getCategories', {'categoryId': cat_id})
        except Exception:
            return
        for c in (data or {}).get('categoryV3s', []):
            name = c.get('name', '')
            cid = c.get('id', '')
            new_path = f"{path} > {name}" if path else name
            if keyword in name:
                results[cid] = new_path
            if not c.get('isLeaf'):
                walk(cid, new_path, depth + 1)

    walk('', '', 1)
    return results


if __name__ == '__main__':
    # 1. 珠宝类目
    print('=' * 50)
    print('1. 珠宝类目(递归遍历分类树)')
    found = find_category_chain('珠宝')
    for cid, path in list(found.items())[:20]:
        print(f'  {cid}  {path}')
    if not found:
        print('  (未找到珠宝类目,试试其他关键词)')

    # 2. 品牌(搜钻石世家)
    print('=' * 50)
    print('2. 品牌搜索(钻石世家)')
    try:
        data = call('common.brandSearch', {'categoryId': '', 'keyword': '钻石世家',
                                           'pageNo': 1, 'pageSize': 10})
        for b in (data or {}).get('brands', [])[:10]:
            print(f"  {b.get('id')}  {b.get('name')}")
    except Exception as e:
        print(f'  {e}')

    # 3. 运费模板
    print('=' * 50)
    print('3. 运费模板列表')
    for m in ('common.getShippingTemplateList', 'common.getFreightTemplateList',
              'common.shippingTemplateList', 'common.freightTemplateList',
              'common.getShippingTemplates'):
        try:
            data = call(m, {})
            print(f'  [{m}] 返回: {json.dumps(data, ensure_ascii=False)[:300]}')
            break
        except Exception as e:
            print(f'  [{m}] 失败')

    # 4. 物流方案(common.getLogisticsList)
    print('=' * 50)
    print('4. 物流方案列表(common.getLogisticsList)')
    try:
        data = call('common.getLogisticsList')
        for p in (data or {}).get('logisticsPlans', [])[:10]:
            print(f"  {p.get('planInfoId')}  {p.get('planInfoName')}  valid={p.get('isValid')}")
    except Exception as e:
        print(f'  {e}')

    # 5. 发货时间规则(common.getDeliveryRule)
    print('=' * 50)
    print('5. 发货时间规则(common.getDeliveryRule)')
    try:
        data = call('common.getDeliveryRule', {
            'getDeliveryRuleRequests': [{
                'logisticsPlanId': '68f9ea252ef39300158a9e80',
                'categoryId': '65f9975f3e946300016b345d',
            }]
        })
        for r in (data or {}).get('deliveryRuleList', []):
            for e in r.get('existing', []):
                print(f"  timeType={e.get('timeType')} value={e.get('value')} {e.get('desc', '')[:50]}")
    except Exception as e:
        print(f'  {e}')
