# 学术报告管理系统（计算机相关院系）

基于研究生平台 `xsbgglappustc` 应用的接口，自己实现的一个本地学术报告管理工具。
只覆盖**计算机相关院系**发布的学术报告，支持：

- 查看报告（未选报告列表 + 报告详情）
- 查看已选报告
- 选课（选报告）
- 退课（退报告）
- 自动选课（定时查找「状态可选且人数未满」的报告并自动选课）

零第三方依赖，只用 Python 标准库。

## 目录结构

```
report_system/
├── server.py                 # HTTP 服务 + 路由 + 缓存
├── ustc_api.py               # 研究生平台接口客户端
├── auto_enroll.py            # 自动选课：定时调度 + 选课策略 + 日志
├── session_watch.py          # 会话健康检查：定时探测 Cookie 是否失效
├── cookiecloud.py            # 从自建 CookieCloud 拉取并解密 Cookie
├── notify.py                 # Server酱 推送（可单独运行发送测试消息）
├── static/
│   ├── index.html            # 页面结构
│   ├── app.js                # 前端逻辑
│   └── style.css             # 样式
├── data/
│   ├── departments.json      # 院系代码表（静态数据）
│   ├── session.json          # 运行时生成：会话 Cookie
│   ├── cache.json            # 运行时生成：报告缓存
│   ├── auto_enroll.json      # 运行时生成：自动选课设置与日志
│   └── notify.json           # 运行时生成：推送设置与提醒状态（含 SendKey）
└── i18n/
    └── zh.json               # 界面文案 + 推送文案
```

## 启动

```bash
cd report_system
python3 server.py                 # 默认 http://127.0.0.1:8770
python3 server.py --port 9000     # 自定义端口
```

打开浏览器访问 `http://127.0.0.1:8770`。

## 连接会话

接口需要 CAS 登录后的会话 Cookie。输入框支持两种格式，程序会自动识别：

**格式一：浏览器 Cookie 请求头**

1. 浏览器登录 <https://yjs1.ustc.edu.cn/gsapp/sys/yjsemaphome/portal/index.do>
2. 打开开发者工具（F12）→ Network
3. 任选一个 `.do` 请求 → Request Headers → 复制 `Cookie:` 后面的整行
4. 粘贴到页面顶部输入框，点「连接」

**格式二：Netscape cookies.txt（curl / wget / 浏览器插件导出）**

```
# Netscape HTTP Cookie File
yjs1.ustc.edu.cn	FALSE	/gsapp/	FALSE	0	GS_DBLOGIN_TOKEN	5e566bf7...
yjs1.ustc.edu.cn	FALSE	/gsapp/	TRUE	0	_WEU	HleVrosWjjIcb...
yjs1.ustc.edu.cn	FALSE	/	FALSE	0	JSESSIONID	AxfZdtbEkLE9...
.ustc.edu.cn	TRUE	/	FALSE	1824171404	_ga	GA1.3.1052600105...
```

整份文件内容（含注释行）直接粘贴即可，无需删减。每行七列，制表符分隔：

| 列 | 含义 |
| --- | --- |
| 1 | domain，前导 `.` 表示对子域生效 |
| 2 | includeSubdomains，`TRUE` / `FALSE` |
| 3 | path |
| 4 | secure，`TRUE` / `FALSE` |
| 5 | 过期时间，`0` 表示会话 Cookie |
| 6 | name |
| 7 | value |

解析时会保留每行真实的 `domain` / `path` / `secure` / 过期时间，而不是把所有 Cookie
都挂到同一个域名下，因此 `_WEU`（`/gsapp/`、secure）与 `_ga`（`.ustc.edu.cn`）这类
作用域不同的 Cookie 都能被正确区分。以 `#` 开头的行会被跳过，但 `#HttpOnly_` 前缀是例外
——它标记的是一个真实的 Cookie 行，只是该 Cookie 带 HttpOnly 标志。

两种格式的判别不靠猜：程序会校验第 2、4 列是否为 `TRUE`/`FALSE`、第 5 列是否为整数，
所以形如 `a=1; b=2; c=3; ...` 的请求头不会被误判成 cookies.txt。

Cookie 只保存在本机 `data/session.json`（权限 600），且以 cookies.txt 格式回写，
保证重启后作用域信息不丢失，不会发往任何第三方。
连接成功后会把报告列表抓下来缓存，默认 30 分钟内复用缓存，可随时点「刷新数据」强制更新。

## Cookie 过期提醒（Server酱）

Cookie 是有时限的，失效后所有接口都会返回 401。程序会在两个时机发现失效并推送提醒：

- **被动**：任何一次请求（页面操作或自动选课）被上游拒绝时立即提醒；
- **主动**：`session_watch.py` 后台每 30 分钟做一次轻量探测（调 `getCjNum.do`），
  这样即使你几天不打开页面、自动选课也没开，Cookie 失效一样会被发现。

提醒通过 [Server酱](https://sct.ftqq.com/) 发送，**每次失效只推送一条**：推送成功后置位，
直到你重新连接成功才会复位。若推送本身失败（网络问题或 SendKey 失效），不会置位，
下一轮探测会重试，因此不会因为一次偶发失败而漏掉提醒。

配置写在 `data/notify.json`（首次运行自动生成，权限 600）：

| 字段 | 说明 |
| --- | --- |
| `sendkey` | Server酱 SendKey，形如 `SCT...`；也可用环境变量 `SERVERCHAN_SENDKEY` 覆盖 |
| `enabled` | 是否发送推送，默认 `true` |
| `checkIntervalMinutes` | 后台探测间隔（分钟），默认 `30`，取值 5–1440 |
| `siteUrl` | 推送正文里附上的地址，默认 `https://report.annya.work/` |

该文件已加入 `.gitignore`，SendKey 不会被提交到仓库。推送文案（标题与正文模板）放在
`i18n/zh.json` 的 `notify.*` 键下，正文支持 Markdown，可用 `{time}`、`{reason}`、`{url}`
三个占位符。

手动发一条测试消息：

```bash
python3 notify.py                      # 用 data/notify.json 里的 SendKey
python3 notify.py --message "自定义正文"
```

查看当前状态（是否配置、上次提醒时间、上次错误）：

```bash
curl -s http://127.0.0.1:8770/api/notify
```

页面上也会同步显示：Cookie 失效后顶部状态点变黄并显示「Cookie 已失效」，同时自动展开
Cookie 输入框，方便直接粘贴新的 Cookie。

如果配置了 CookieCloud（见下节），失效时会**先尝试自动恢复**：探测到 401 后立刻从
CookieCloud 拉一份新 Cookie 重连，只有恢复失败才推送提醒，所以浏览器插件一直在同步的话
通常不会收到打扰。

## 从 CookieCloud 自动获取 Cookie

[CookieCloud](https://github.com/easychen/CookieCloud) 是一个自建的 Cookie 同步服务，
浏览器插件会把加密后的 Cookie 定时上传到你的服务器。配置好之后，本工具可以**不再手动粘贴
Cookie**，并在会话失效时自动恢复。

配置只通过环境变量注入，**不会写进代码或仓库**：

| 环境变量 | 说明 |
| --- | --- |
| `COOKIECLOUD_HOST` | 服务地址，如 `https://cookie.annya.work`（可省略协议，默认补 `https://`） |
| `COOKIECLOUD_UUID` | 浏览器插件里设置的同步 UUID |
| `COOKIECLOUD_PASSWORD` | 端到端加密密码 |

三个变量缺任意一个，功能即视为未启用，程序照常运行，只是启动日志会提示未配置。

开发时直接导出即可：

```bash
export COOKIECLOUD_HOST=https://cookie.annya.work
export COOKIECLOUD_UUID=annya
export COOKIECLOUD_PASSWORD=你的密码
python3 server.py
```

生产环境用 systemd 的 `EnvironmentFile`，避免密钥出现在命令行或 shell 历史里：

```ini
# /etc/systemd/system/ustc-report.service.d/env.conf
[Service]
EnvironmentFile=/etc/ustc-report/cookiecloud.env
```

```ini
# /etc/ustc-report/cookiecloud.env   （权限 600）
COOKIECLOUD_HOST=https://cookie.annya.work
COOKIECLOUD_UUID=annya
COOKIECLOUD_PASSWORD=你的密码
```

改完执行 `systemctl daemon-reload && systemctl restart ustc-report`。

工作方式：

- 启动时会调用一次 `ensure_session()`：当前会话可用就直接复用，失效或为空则从 CookieCloud
  拉取并重连，因此**重启即自愈**；
- 后台监控线程发现 401 时，会先尝试恢复，成功则不计入「失效提醒」；
- 页面上多了一个「从 CookieCloud 同步」按钮，可以随时手动拉一次；
- 未配置时按钮为禁用状态，旁边会提示未配置。

解密沿用 CookieCloud 的规则：AES 密钥是 `md5(uuid-password)` 的前 16 个十六进制字符，
密文可能是 CryptoJS 信封格式（`Salted__` + 盐值，走 EVP_BytesToKey 派生）或固定零 IV 的
AES-128-CBC。解密交给系统自带的 `openssl` 命令行完成，因此**依旧零第三方 Python 依赖**。
拉下来的快照会按域名过滤，只保留研究生平台用得上的 Cookie，其余站点不会写入
`data/session.json`。

排查时可以单独运行该模块，打印快照里有哪些 Cookie：

```bash
python3 cookiecloud.py yjs1.ustc.edu.cn
```

## 覆盖的院系

| 代码 | 院系 |
| --- | --- |
| 006 | 电子工程与信息科学系 |
| 010 | 自动化系 |
| 011 | 计算机科学与技术学院 |
| 023 | 电子科学与技术系 |
| 210 | 信息科学技术学院 |
| 218 | 先进技术研究院 |
| 219 | 微电子学院 |
| 221 | 网络空间安全学院 |
| 225 | 软件学院 |
| 229 | 人工智能与数据科学学院 |
| 999 | 未来技术学院 |
| A13 | 软件学院合肥 |
| A14 | 软件学院苏州 |

需要调整范围时，改 `ustc_api.py` 里的 `COMPUTER_DEPT_CODES` 即可。

## HTTP 接口（本地服务）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/i18n` | 界面文案 |
| GET | `/api/status` | 会话状态、学分、报告数量 |
| GET | `/api/departments` | 计算机相关院系及各院系未选报告数 |
| GET | `/api/reports` | 报告列表，支持 `scope` / `keyword` / `depts` / `onlyOpen` / `sort` / `page` / `pageSize` |
| GET | `/api/report?bgbm=` | 单个报告详情 |
| POST | `/api/connect` | `{"cookies": "..."}` 建立会话并抓取数据 |
| POST | `/api/disconnect` | 清除会话与缓存 |
| POST | `/api/cookiecloud/sync` | 从 CookieCloud 拉取 Cookie 并重连（未配置返回 502） |
| POST | `/api/refresh` | 强制重新抓取 |
| POST | `/api/enroll` | `{"bgbm": "..."}` 选课 |
| POST | `/api/drop` | `{"bgbm": "..."}` 退课 |
| GET | `/api/auto` | 自动选课状态（开关、上限、间隔、下次执行、日志） |
| POST | `/api/auto` | `{"enabled": true, "target": 10, "intervalMinutes": 60}` 调整设置 |
| POST | `/api/auto/run` | 立即执行一轮，返回本轮结果 |
| POST | `/api/auto/log/clear` | 清空执行日志 |

## 自动选课

页面上的「自动选课」面板可以开启一个后台定时任务，每隔一段时间自动替你选报告。
开启后第一次执行在**一个完整间隔之后**（默认 60 分钟），给你留出反悔时间；
也可以随时点「立即执行一轮」马上跑一次。

每轮执行的筛选条件是**同时满足**：

1. 报告由计算机相关院系发布（`YXDM` 在 `COMPUTER_DEPT_CODES` 内）；
2. 平台标记为「可选」（`SFKXK = 1`）；
3. 还有余量（`YXRS < KXRS`）；若平台没给容量数字，则只依据可选标记判断；
4. 你尚未选过这门报告。

符合条件时按**选课截止时间由近到远**排序，依次选课，直到达到「选课上限」为止，
每选一门间隔 1.5 秒，避免请求过密。

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| 选课上限 | 10 | 已选报告达到该数量后不再选课 |
| 执行间隔 | 60 | 单位分钟，取值 1–1440 |

日志里每行是一条记录，类型含义：

| 类型 | 含义 |
| --- | --- |
| 信息 | 开启 / 暂停 / 间隔变更 |
| 已选课 | 本轮成功选中的报告 |
| 被拒绝 | 平台拒绝的选课（附原因） |
| 空闲 | 本轮没有可选的报告，或已达上限 |
| 跳过 | 没有有效会话，本轮不做事 |
| 错误 | 会话失效、接口异常等 |
| 汇总 | 本轮候选数 / 成功数 / 失败数 / 当前持有数 |

安全约束：

- **会话依赖**：自动选课依赖当前登录会话。会话失效时任务会自动暂停并记录原因，
  重新连接后可再次开启；点「断开连接」也会同时暂停自动选课。
- **不跨重启续跑**：进程退出后重新启动，自动选课默认处于关闭状态，需要重新手动开启，
  避免在无人看管的情况下继续写学校系统。
- **真实写入**：选课会真实提交到学校系统，开启前有二次确认，日志可随时清空。
- 设置与日志保存在 `data/auto_enroll.json`（已加入 `.gitignore`）。

## 上游接口说明

所有上游接口均为 `application/x-www-form-urlencoded` 的 POST，返回 JSON。

| 用途 | 路径 |
| --- | --- |
| 未选报告 | `/gsapp/sys/xsbgglappustc/modules/xsbgxk/wxbgbgdz.do` |
| 已选报告 | `/gsapp/sys/xsbgglappustc/modules/xsbgxk/yxbgbgdz.do` |
| 已选报告（硕博连读） | `/gsapp/sys/xsbgglappustc/modules/xsbgxk/szbyxbgbgdz.do` |
| 报告详情 | `/gsapp/sys/xsbgglappustc/modules/xsbgfb/xsbgfbbddz.do` |
| 选课 | `/gsapp/sys/xsbgglappustc/xsbgxkController/selectBg.do` |
| 退课 | `/gsapp/sys/xsbgglappustc/xsbgxkController/cancelBg.do` |
| 学分 | `/gsapp/sys/xsbgglappustc/xsbgxkController/getCjNum.do` |
| 院系列表 | `/gsapp/code/edfd3c2a-dbc4-481b-97ce-b11832a50b78.do` |

列表接口参数：`pageNumber`、`pageSize`、`*order`（如 `-BGSJ` 按报告时间倒序）、
`querySetting`（URL 编码的 JSON 条件数组）。选课/退课只接受一个参数 `BGBM`。

`xsbgxk` 是 EMAP 的分页模型，实际地址由 `模块路径 + 动作名` 拼出，
例如 `/modules/xsbgxk.do` 配合动作 `wxbgbgdz` 得到 `/modules/xsbgxk/wxbgbgdz.do`。

### 关于院系筛选

上游的高级查询条件格式对多选字段并不稳定，因此本工具的做法是：
一次性把未选报告全部拉取下来（自动选取服务端允许的最大分页大小），
再在本地按 `YXDM` 精确过滤出计算机相关院系。这样结果稳定、可缓存，
也便于在本地做关键字、排序、容量等二次筛选。

「未选报告」只展示计算机相关院系；「已选报告」不做院系过滤，
你自己选过的报告一律可见，即使它由其他院系发布。
只有当你手动勾选了某个院系后，已选列表才会跟着收窄。

## 注意

- 服务只监听 `127.0.0.1`，不对外暴露。
- 选课/退课会真实写入学校系统，界面上都有二次确认；自动选课同样真实写入，开启前有二次确认。
- 数据仅作本地查询与管理使用。