# -*- coding: utf-8 -*-
import os, base64, json, urllib.request
XLSX = r'C:\Users\Administrator\Desktop\批量发布测试数据v2.xlsx'
MAIN_DIR = r'C:\Users\Administrator\Desktop\KGN1000330'
DETAIL_DIR = r'C:\Users\Administrator\Desktop\放置于详情内的通用通栏'
API = 'http://127.0.0.1:8020'

def to_b64(p):
    with open(p,'rb') as f: return base64.b64encode(f.read()).decode('ascii')

with open(XLSX,'rb') as f: xlsx_b64 = base64.b64encode(f.read()).decode('ascii')

main_files = [{'path':f'KGN1000330/{n}','name':n,'content_base64':to_b64(os.path.join(MAIN_DIR,n))}
              for n in os.listdir(MAIN_DIR) if n.lower().endswith(('.jpg','.jpeg','.png','.webp'))]
detail_files = [{'path':f'放置于详情内的通用通栏/{n}','name':n,'content_base64':to_b64(os.path.join(DETAIL_DIR,n))}
                for n in os.listdir(DETAIL_DIR) if n.lower().endswith(('.jpg','.jpeg','.png','.webp'))]

print(f'主图 {len(main_files)} 张, 详情 {len(detail_files)} 张')

payload = {'filename':'批量发布测试数据v2.xlsx','content_base64':xlsx_b64,
           'folder_files':main_files,'common_detail_files':detail_files}
req = urllib.request.Request(f'{API}/import', data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                              headers={'Content-Type':'application/json'}, method='POST')
resp = json.loads(urllib.request.urlopen(req, timeout=180).read().decode('utf-8'))
print(f'/import 返回: total={resp.get("total")} valid={resp.get("valid")} errors={resp.get("errors")}')
bid = resp['batch_id']
print(f'新 batch_id = {bid}')

# 直接读 DB 看 main_images
import shutil, sqlite3
shutil.copyfile('bulk_api/bulk_catalog.sqlite3','_tmp_e.db')
c = sqlite3.connect('_tmp_e.db'); c.row_factory = sqlite3.Row
r = c.execute("SELECT rows_json FROM batches WHERE id=?", (bid,)).fetchone()
c.close(); os.remove('_tmp_e.db')
rows = json.loads(r['rows_json'])
print(f'\n{"商品编码":<18} {"主图":>4} {"详情":>4}')
for row in rows:
    print(f"{row.get('product_code'):<18} {len(row.get('main_images') or []):>4} {len(row.get('detail_images') or []):>4}")
