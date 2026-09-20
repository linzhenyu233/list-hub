# 多平台商品发布中台

一个「3 后端服务 + 1 前端」的 monorepo，用于把商品批量/单品发布到**微信小店**和**小红书**。
服务之间通过 HTTP 松耦合，需同时在线才能跑通完整链路。

## 架构与调用链

```
                ┌─────────────────────────┐
   浏览器 ─────▶ │  前端运营台 (Vite:5173)  │
                └───────────┬─────────────┘
        /api (proxy)        │        /xhs-api          /bulk-api
             ┌──────────────┼───────────────┬──────────────────┐
             ▼              ▼                ▼                  ▼
   ┌──────────────┐  ┌──────────────┐  ┌───────────────────────────┐
   │ 微信服务:8000 │  │ 小红书服务:8010│  │  批量发布中台:8020         │
   │ fastapi_server│  │  xhs_api.py  │  │  bulk_api.py + bulk_worker │
   └──────┬───────┘  └──────┬───────┘  └─────────────┬─────────────┘
          ▼                 ▼                         │ HTTP 调用 8000/8010
   微信小店 API        小红书开放平台  ◀────────────────┘
```

- **中台 (`bulk_api`)** 负责 Excel 导入、类目别名映射、发布任务编排，实际发品时通过
  `WECHAT_API_BASE` / `XHS_API_BASE` 调用上面两个服务。
- **发布是异步的**：`bulk_api.py` 只提供 HTTP 接口，真正执行发布的是常驻线程
  `bulk_worker.py`，两者都要运行。

## 目录结构

```
PythonProject/
├── wechat/                 # 微信小店服务 (:8000)
│   ├── fastapi_server.py   #   HTTP 接口入口
│   ├── wechat_store_client.py  # WxStore 客户端（发品/类目/图片）
│   ├── get_token.py, cat_finder.py  # 辅助脚本
│   └── diagnostics/        #   一次性调试脚本归档（_diag_*.py，非运行必需）
├── xhs/                    # 小红书服务 (:8010)
│   ├── xhs_api.py          #   HTTP 接口入口
│   ├── xhs_store.py        #   XhsStore 客户端（签名/发品/上下架）
│   └── get_access_token.py, guanyi_sync.py, query_params.py  # 辅助脚本
├── bulk_api/               # 批量发布中台 (:8020)
│   ├── bulk_api.py         #   HTTP 接口入口（导入/映射/任务）
│   ├── bulk_worker.py      #   常驻发布 worker（必须与 bulk_api 同时运行）
│   └── *.py                #   Excel 生成/校验、类目迁移脚本
├── frontend/               # 前端运营台 (Vite:5173)，原 wechat-store-ops
├── images/                 # 批量导入的图片缓存（运行时数据，已 gitignore）
├── upload_images.py        # 独立的双平台图片上传 CLI 工具
├── .env.example            # 环境变量模板（复制为 .env 填真值）
└── README.md
```

## 环境准备

- Python 3.11+，Node.js 18+
- 各服务依赖独立：`pip install -r <服务目录>/requirements.txt`

### 配置密钥

```powershell
Copy-Item .env.example .env    # 然后编辑 .env 填入真实 AppID/Secret/Token
```

> ⚠️ 目前 `wechat_store_client.py` 与 `xhs/xhs_store.py` 仍把凭证写死为默认值，
> 未全部改为读取环境变量；`.env` 中已标注哪些变量「已接入 / 未接入」。

## 启动服务

分别在**各自目录**下启动（PowerShell 设临时环境变量示例）：

```powershell
# 1) 微信小店服务 :8000
cd wechat
$env:WX_APPID="wx你的AppID"; $env:WX_SECRET="你的AppSecret"
python fastapi_server.py

# 2) 小红书服务 :8010
cd xhs
python xhs_api.py

# 3) 批量中台接口 :8020
cd bulk_api
python bulk_api.py

# 4) 批量中台发布 worker（另开一个终端，与第 3 步同时运行）
cd bulk_api
python bulk_worker.py
```

接口文档：服务启动后访问 `http://127.0.0.1:<端口>/docs`。

## 启动前端

```powershell
cd frontend        # 若尚未改名，则为 cd wechat-store-ops
npm install
npm run dev        # http://localhost:5173，已配置 /api、/xhs-api、/bulk-api 代理
```

## 说明

- 运行时数据（`*.sqlite3`、`images/`、`uploaded_images.*`）与密钥（`.env`）均已在
  `.gitignore` 中忽略，不会进入版本库。
- `wechat/diagnostics/` 内为历史一次性调试脚本，归档保留，不属于服务运行链路。

## 运行时数据的自动清理（保留策略）

没有独立的定时任务：**每次导入商品时顺手清一遍，数据库维护每天最多一次**，
避免批次数据、图片缓存、货盘表备份无限累积。各项都可按需调整：

| 数据 | 默认保留 | 环境变量 | 说明 |
| --- | --- | --- | --- |
| 数据库里的批次（`batches.rows_json/mappings_json`，库里最大的一块） | 14 天 | `BULK_BATCH_TTL_DAYS` | 只删"没有排队/执行中发布项"的旧批次；**发布记录 `publish_items` 永久保留**（"已发布/未发布"按商品编码跨批次统计，删了会导致重复发品） |
| 数据库空间回收（`VACUUM`） | 每天最多一次 | — | 删过批次或空闲页 >8MB 才执行；拿不到锁就跳过 |
| 货盘表备份 `bulk_api/_sources/huopai_backups/`（每份 60MB+） | 1 份 / 14 天 | `BULK_MAX_HUOPAI_BACKUPS`、`BULK_HUOPAI_BACKUP_TTL_DAYS` | 只兜"刚写错一次"，够用 |
| 批次来源记录 `bulk_api/_sources/*.json` | 14 天 | `BULK_SOURCE_INFO_TTL_DAYS` | 与批次对齐：批次删了它也没用（回填ID需要批次里的源行信息） |
| 共享盘图片懒拷贝 `images/_share/` | 30 天 | `BULK_IMAGE_TTL_DAYS`（0=关闭） | 按内容哈希命名，删了会在下次发布时自动从共享盘重取；**浏览器上传/Excel 内嵌图不自动清**（删了那批就发不出去） |

> 清得太早的影响：更早的批次在界面上打不开（404）、也不能再回填商品ID/规格ID；
> 需要时重新导入货盘表即可，不影响已发布到平台的商品和发布记录。
