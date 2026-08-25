# TalkShelf 部署手册（M4：Cloudflare Tunnel 方案）

> 2026-08-26 用户拍板：部署走 **Cloudflare Tunnel**（设计文档 §10 方案 B），替代原计划的 frp + Caddy。
> 好处：免 VPS、免备案、免证书/域名解析管理；家 PC 无需公网 IP / 端口映射（cloudflared 主动出站）。
> 域名：**example.com**（示例，替换为你的域名）。
> 当前约定：**不设置自启**，统一用本目录三个脚本手动启停（将来要自启再演进）。

## 架构

```
浏览器 → https://note.example.com（HTTPS，CF 边缘自动证书）
              │
        Cloudflare 边缘（TLS 终止 + CNAME → <tunnel>.cfargotunnel.com）
              │  出站长连接（QUIC/HTTP2，cloudflared 主动连 CF，重启/IP 变化零操作）
              ▼
        家 PC  cloudflared.exe（tunnel run --token ...）
              ▼
        http://127.0.0.1:8000  uvicorn（--workers 1，只绑回环，不暴露局域网）
        SQLite data/talkshelf.db
```

## 前置条件

- 家 PC 可访问 `7844`/`443` 端口出站（普通宽带即可，无需公网 IP、无需路由器设置）。
- TalkShelf 本地可跑（`start.ps1` 或手动 uvicorn，见仓库 README「运行」）。
- 域名 `example.com` 注册审核通过。

## 一次性配置步骤（域名生效后执行）

### 1. 域名接入 Cloudflare（浏览器操作）

1. 登录 [Cloudflare 控制台](https://dash.cloudflare.com) → 左侧 **Domains** → **Onboard a domain**。
2. 输入 `example.com`，选 **Free 计划**。
3. 记下分配的 **2 个 NS 地址**（如 `xxx.ns.cloudflare.com`）。
4. 去域名注册商（阿里云/腾讯云等）控制台 → 域名 DNS 管理 → 把 NS 改成上面 2 个（是改「DNS 服务器/NS」，不是加解析记录）。
5. 等待状态 Pending → **Active**（几分钟到 24 小时）。

### 2. 创建隧道并取 token（浏览器操作）

1. 左侧 **Networking → Tunnels**（或 https://dash.cloudflare.com/?to=/:account/tunnels ）→ **Create a tunnel**。
2. 命名（如 `talkshelf`）→ 选 **Windows**。
3. 页面给出安装命令（形如 `cloudflared.exe service install eyJ...`）——**只取 `eyJ...` 那一段 token**。
4. 把 token 粘贴到本目录 `cloudflared\tunnel-token.txt`（已 gitignore，机密不入库）。
   - ⚠️ `service install` 是注册 Windows 服务（自启），当前**不采用**；token 给手动脚本用 `tunnel run --token` 即可。

### 3. 手动启停（本目录脚本，无自启）

```powershell
.\deploy\start.ps1    # 后台启动 uvicorn(127.0.0.1:8000) + cloudflared 隧道；再后台跑 status 可看结果
.\deploy\status.ps1   # 进程状态 + 本地探活 + 隧道连接日志摘要
.\deploy\stop.ps1     # 停止隧道与 uvicorn
```

脚本约定：进程后台运行（关终端不掉）；PID 与日志在 `deploy/logs/`（gitignore）；
日志：`uvicorn.out.log / uvicorn.err.log / tunnel.out.log / tunnel.err.log`。
**重启电脑后需手动再跑 `start.ps1`**（未设自启；家 PC 请设「永不休眠」，否则隧道会断）。

### 4. 配置公网主机名（浏览器操作）

隧道显示 Connected 后：

1. 隧道详情页 → **Routes** → Add route → **Published application**。
2. Subdomain：`note`；Domain：`example.com`。
3. **Service URL：`http://127.0.0.1:8000`**（源站无需 HTTPS；边缘到源站由隧道加密）。
4. Save —— CF 自动创建 CNAME 与 HTTPS 证书，无需任何证书操作。

### 5. 应用侧部署配置

`.env`（部署机，不改则按默认）：

| 配置 | 值 | 说明 |
|---|---|---|
| `AUTH_PASSWORD` | 强密码 | **部署必须设置**（设置即启用登录，未设则公网裸奔） |
| `AUTH_COOKIE_SECURE` | `1` | HTTPS 下必须；本地 http 保持 `0` |
| `PUBLIC_URL` | `https://note.example.com` | 公网访问地址（start.ps1 启动提示用；示例，替换为你的域名） |

验证：浏览器访问 `https://note.example.com` 应看到登录页；
`GET /api/version`（免登录）返回 `{"name": "TalkShelf", "version": "0.10.2"}`（部署后确认线上版本用）。

### 6. 应用侧安全加固（v0.10.2 起自带，无需额外配置）

| 加固项 | 说明 |
|---|---|
| `/docs`、`/openapi.json` 关闭 | 公网不暴露 API 形状；本地要查接口文档时把 `main.py` 的 `docs_url` 临时改回 `"/docs"` |
| 安全响应头 | `X-Content-Type-Options` / `X-Frame-Options` / CSP（含 `frame-ancestors 'none'`，防 iframe 套壳）/ `Referrer-Policy` 全响应下发；HSTS 仅在 HTTPS 场景（CF 边缘透传 `X-Forwarded-Proto: https`）下发 |
| 改密码吊销其他会话 | 修改密码后除当前会话外的全部会话立即失效（其他设备自动登出） |

> 边缘侧（浏览器操作，与上面互补）：CF 面板 SSL/TLS 开启 **Always Use HTTPS + HSTS**；
> Security → WAF 建一条 `/api/login` 的 rate limiting 规则（按真实 IP 限速，应用侧限速是全局的）。

## 运维须知

- **家 PC 离线/重启/换 IP**：无需任何操作——cloudflared 自动重连（前提：设永不休眠 + 重启后跑 `start.ps1`）。
- **离线期间访问**：CF 返回错误页，恢复后自动正常。
- **安全边界**：公网只有 CF 边缘；家 PC 不暴露任何端口（uvicorn 只绑 127.0.0.1，隧道端口是出站）。
- **备份**：数据在 `data/talkshelf.db`（恢复演练与定时备份排期在 M4 后续，见设计文档 §12/§9）。
- **改主机名/端口**：改隧道 Routes 或脚本端口参数后重跑 `start.ps1`（先 `stop.ps1`）。
- 未来要**自启**：两条路——`cloudflared.exe service install <token>`（服务）+ 任务计划开机运行 `deploy\start.ps1`（uvicorn 部分需后台化，当前脚本已是后台），本次不做。

## 排期备注

- `example.com` 审核通过 → 用户执行第 1 步 → 告知后继续第 2~4 步（我协助）。
- 第 5 步前 TalkShelf 本地功能验收（161 pytest）已完成（v0.10.2）。
