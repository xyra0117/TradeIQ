# TradeIQ iPhone 私人只读版

## 使用方式

手机读取电脑上已经保存的数据。八个模块为核心指数、阶段涨幅榜、涨停拆解、涨停合集、主力净流入、选股推荐、持仓、热门板块。

- 底部「行情 / 选股 / 持仓 / 更多」切换模块；每个模块默认选择自己最新的已保存日期，不用今天假冒数据日期。
- 日期、名称/代码筛选、数值排序只影响查看。刷新不采集、不补数据、不调用电脑端同步接口。
- 点击指数或股票打开日 K 与成交额，支持近 30/90 日、全部历史和自定义日期；缺失 OHLC 不填造蜡烛。均线沿用全历史计算，成交额统一显示亿元。
- 指数、个股分时在原项目中没有持久存储，手机版明确提示不可用，不发起外部行情请求。
- 持仓是**当前持仓数量**配合截至所选日期的最后已存收盘价，展示每行价格日期和缺价数量，不是历史持仓快照。估算浮盈沿用电脑持仓成本及预计卖出费用口径，价差比例不扣费。
- 东财主力净流入与同花顺总净流入分开标注。数据库的资金金额是元，显示时换算万/亿；价格与涨幅优先读取同日 `stock_daily`，不使用资金表里的旧价格。
- 条件/组合选股读取保存的候选快照，再按电脑默认的 3 亿元门槛过滤，可选 0/1/3/5/10 亿；不重算选股、不自动修补 T+N。页面显示快照保存时间；未持久保存的「条件选股新」结果不在本版重算。
- 板块热度按所选日期截至的近 7 个自然日统计，支持查看历史成员及当前自选板块。
- 手机持久缓存只包含页面、图标、JS、CSS 和本地图表库；接口和账户数据不进入 Cache Storage、localStorage 或 IndexedDB。断线后可打开应用外壳，但不提供离线行情。

## 本地启动

在项目根目录运行（只安装到已有项目虚拟环境）：

```sh
.venv/bin/python -m pip install -r backend/requirements-mobile.txt
.venv/bin/python backend/mobile_app.py
```

电脑浏览器打开 <http://127.0.0.1:5556>，可调整窗口宽度预览手机布局。此地址只用于电脑本机，不能直接填进 iPhone。

手机服务使用 Waitress，绑定 `127.0.0.1:5556`，无 debug/reloader。它不会导入 `app.py`、初始化/迁移数据库、启动定时任务或访问行情供应商。电脑端原服务继续负责数据更新，不要额外再启动第二套采集任务。

数据库路径固定相对 `backend/mobile_app.py` 解析为 `backend/market_data.db`，与启动目录无关；文件缺失返回 503，不创建空库。每次请求开启一个 SQLite 只读事务，使用 `mode=ro` 和 `query_only`。

关闭此手机服务终端会停止服务。常驻、登录启动和电脑防休眠属于后续系统配置，需获得用户许可后设置。

## 私人远程连接（需要设备授权后执行）

1. 用户确认安装后，在 Mac 安装 Tailscale 官方客户端，在 iPhone 安装 Tailscale 并允许 VPN 配置。两台设备登录同一个个人账号，不分享电脑节点给其他人。
2. 在 Tailscale 管理页核对两台设备的拥有者、设备名和 Tailscale IP。启用设备审批，确认只有自己的设备得到批准。
3. 先检查已有访问策略。为新建的个人网络配置「这台 iPhone → 这台电脑 TCP 443」的最小授权；同时移除覆盖这台电脑的默认全放行规则。规则是叠加的，只增加一条窄规则不会覆盖旧的全放行。不要覆盖已有网络的其他业务规则。

下面是**新建、仅供本项目使用的个人网络**策略模板，IP 为占位示例，保存前须替换为已核验的设备 Tailscale IPv4 地址：

```json
{
  "hosts": {
    "tradeiq-phone": "100.64.0.10",
    "tradeiq-mac": "100.64.0.11"
  },
  "grants": [
    {"src": ["tradeiq-phone"], "dst": ["tradeiq-mac"], "ip": ["tcp:443"]}
  ]
}
```

不向手机授权电脑的 5555 或 5556 端口，不发布子网路由，不启用公网 Funnel。旧的 `acls` 和新的 `grants` 都需检查是否存在宽泛放行；以管理页策略测试和实际连通结果为准。

4. 获取电脑完整的 Tailscale DNS 名（例如 `tradeiq.tailXXXX.ts.net`），不包含 `https://` 或尾部句点。重启**手机服务**，传入这个精确主机名：

```sh
.venv/bin/python backend/mobile_app.py --hostname tradeiq.tailXXXX.ts.net
```

此参数只增加一个允许的 Host，不修改系统或 `.env`。手机服务仍然只监听回环地址。

5. 核对 `tailscale serve status` 和 `tailscale funnel status`，确认没有已有端口冲突或公网映射。然后配置 Serve：

```sh
tailscale serve --bg --https=443 http://127.0.0.1:5556
tailscale serve status
```

按客户端提示启用 MagicDNS / HTTPS。部分 macOS 客户端的 CLI 未加入 PATH，需要使用该客户端提供的终端集成或可执行文件路径；不要另起第二个 Tailscale 守护进程。

使用 Serve 输出的 `https://...ts.net` 地址。若 400，核对 `--hostname` 与实际完整域名一致；若 502，检查手机服务是否运行。只能映射 **5556**，不要将电脑端 **5555** 映射给手机。

公网可信证书会将设备域名写入证书透明度日志，页面和数据库不会因此公开。设备名建议用不含姓名等信息的普通名称。

6. iPhone 连接 Tailscale，Safari 打开私人 HTTPS 地址，在分享菜单选择「添加到主屏幕」，名称设为 TradeIQ。以后从图标打开；外出仍需电脑在线、手机 Tailscale 已连接。

## 验证

自动验证使用临时测试数据库与模拟行情，不初始化真实数据库：

```sh
.venv/bin/python -m unittest discover -s backend -p 'test_mobile.py'
.venv/bin/python -m unittest discover -s backend -p 'test_index_charts.py'
.venv/bin/python -m unittest discover -s backend -p 'test_conditional_picks_new.py'
node dashboard/test-mobile.cjs
node dashboard/test-index-charts.cjs
node dashboard/test-conditional-picks-new.cjs
node dashboard/test-stock-flow-click.cjs
node dashboard/test-stock-flow-tooltip.cjs
node --check dashboard/mobile/app.js
node --check dashboard/mobile/sw.js
```

可选浏览器回归（使用项目现有 Playwright 与 Chromium；服务和数据库均为临时测试实例）：

```sh
RUN_MOBILE_BROWSER_TESTS=1 .venv/bin/python -m unittest discover -s backend -p 'test_mobile_browser.py'
```

覆盖 320/375/390/430 像素竖屏、844 像素横屏、八模块导航、日期/筛选、指数和个股日 K、弹窗返回/关闭、离线重载及重连、缓存中不出现任何 API。示例截图放 `/private/tmp/tradeiq-mobile-*.png`，全部使用虚构数据。

真实设备验收（尚需连接设备后完成）：

- iPhone Safari 和主屏幕独立窗口都能打开；安全区域、触摸提示、日期输入及横竖屏正常。
- 关闭 Wi-Fi，通过手机流量仍能读取数据；切换历史日期核对电脑数据库对应日期。
- 手机查询/刷新前后无同步任务产生；POST/PUT/PATCH/DELETE 返回 405。
- 断开 Tailscale 后新请求失败；应用显示无法连接，而不是静默呈现缓存行情。
- 未加入网络的设备以及网络内未授权设备均无法访问私人地址；策略测试拒绝其到 TCP 443 的连接。
- iPhone 不能直接访问电脑 Tailscale IP 的 5555/5556；`tailscale funnel status` 不存在本项目公网映射。

更新静态资源时递增 `sw.js` 的缓存版本。回退代码后重启手机服务即可，不涉及数据库回滚；若停用远程访问，在检查 Serve 状态后只关闭本项目的 HTTPS 443 映射，不重置其他服务。

## 实现约定与来源

- `/api/mobile/dates?module=...&kind=...&source=...`：当前模块可用日期。
- `/api/mobile/modules/<module>?date=YYYYMMDD`：只读列表、实际日期、缺数据说明；选股额外支持 `kind` / `threshold_yi`，资金支持 `source`，榜单支持 `period`。
- `/api/mobile/chart?kind=index|stock&ts_code=...&date=...&start=...`：保存的日线和单位。
- `/api/mobile/sectors/detail?name=...&date=...`、`/api/mobile/user-sectors`：板块成员。
- `/api/mobile/health`：数据库可打开且服务可用。
- 静态路由仅放行明确资源名单。没有通用反向代理、跨域放行、写入路由或数据库下载路径。不会以客户端 User-Agent / 请求参数决定写权限。
- Apache ECharts 5.4.3 本地分发，许可证与版权声明在 `dashboard/mobile/vendor/LICENSE-echarts.txt` 和 `NOTICE-echarts.txt`。

参考：[Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve)、[访问规则语法](https://tailscale.com/docs/reference/syntax/grants)、[HTTPS 与证书透明度](https://tailscale.com/docs/how-to/set-up-https-certificates)、[Waitress](https://flask.palletsprojects.com/en/stable/deploying/waitress/)。
