# 微信小店运营前端

基于 Vue 3、Vite 和 Element Plus 的微信小店运营系统，对接现有 FastAPI 服务。

## 功能

- 商品列表及状态筛选
- 游标翻页与商品详情
- 商品草稿创建
- 微信类目搜索与叶子类目选择
- 图片 URL 转存
- 多 SKU、价格和库存录入
- 商品上架、下架和删除
- 后端健康检查与微信凭证测试

## 启动

先启动后端：

```powershell
cd C:\Users\Administrator\PycharmProjects\PythonProject
$env:WX_APPID = "你的 AppID"
$env:WX_SECRET = "你的 AppSecret"
python fastapi_server.py
```

再启动前端：

```powershell
cd D:\codex
npm install
npm run dev
```

浏览器访问 `http://localhost:5173/`。Vite 会将 `/api` 转发到 `http://127.0.0.1:8000`。

小红书商品页需要单独启动小红书后端：

```powershell
cd C:\Users\Administrator\PycharmProjects\PythonProject\xhs
python xhs_api.py
```

小红书服务默认使用 `8010` 端口。前端通过 `/xhs-api` 访问它；微信小店仍通过 `/api` 访问 `8000`，两个平台的接口与数据不会混用。

生产构建：

```powershell
npm run build
```

构建产物位于 `D:\codex\dist`。

## 环境配置

开发环境默认使用 Vite 代理。部署前端和后端为不同域名时，可创建 `.env.production`：

```env
VITE_API_BASE_URL=https://api.example.com
```

后端的 `WX_APPID`、`WX_SECRET` 必须使用服务端环境变量，不能写入前端或提交到代码仓库。
