# 二手交易平台：本地开发

## 当前阶段

已实现注册、统一登录、退出、个人账号页、管理员工作台入口，以及首个管理员的账号授权设置。
基础设施包括应用工厂、数据库会话、CSRF、公共模板和健康检查。

已加入商品草稿、图片、提交/撤回审核、管理员审核、组合查询、下架和重新审核。
已加入预约、确认、取消/拒绝、完成与个人预约历史，含持久请求去重与唯一有效预约约束。
当前业务表包括 users、products、reservations、operation_requests、operation_logs；login_sessions 支持可撤销登录。
已加入管理员违规屏蔽/恢复、按北京时间查询交易统计，以及全平台只读操作日志查询。
屏蔽联动取消有效预约，商品、预约及日志原子提交；已完成交易不能屏蔽。
商品规则见 [商品管理实现说明](docs/products.md)，预约规则与并发核验见 [预约与交易说明](docs/reservations.md)。
管理员规则及核验见 [管理员功能说明](docs/admin.md)。
最新 [整体联调与程序验收记录](docs/acceptance.md)：22 项需求核对、缺陷修复、回归及提交待办。
数据库初始化创建缺少的表、保留首个管理员编号，并事务升级本项目已知的旧日志表结构；不清空业务数据。

## 环境要求

- Windows PowerShell
- Python 3.12（数据库配置使用 Python 3.12 的 sqlite3 事务接口）
- 首次安装依赖需要访问 Python 包源

以下命令均在本文件所在的 code 目录执行。直接调用虚拟环境解释器，不需要修改 PowerShell 执行策略或激活环境。

## 首次安装

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock.txt
```

本次验证的完整版本保存在 requirements-dev.lock.txt，上面使用的就是锁定版本。
若只运行程序而不执行测试和静态检查，可安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

如果没有 py 启动器，先确认 `python --version` 为 3.12，再将 `py -3.12` 改为 `python`。
仅运行程序可安装 requirements.lock.txt；开发和验证使用 requirements-dev.lock.txt。
requirements.txt 与 requirements-dev.txt 用于表达直接依赖的兼容范围。

## 本地配置

提交副本不含 .env、数据库或任何账号。首次运行时，在 code 目录且尚无 .env 的情况下执行：

```powershell
.\.venv\Scripts\python.exe -c "from pathlib import Path; import secrets; p=Path('.env'); assert not p.exists(), '.env already exists'; p.write_text('SECRET_KEY=' + secrets.token_hex(32) + '\n', encoding='utf-8')"
```

`.env.example` 是配置项说明，SECRET_KEY 留空是有意的，直接复制后仍需生成密钥。
上面的命令不会显示密钥，也不会覆盖已有 .env；再次运行不需要重新生成。

可参照 .env.example 追加 DATABASE_URL；未设置时使用 instance/secondhand.sqlite3。
系统环境变量优先于 .env。密钥和运行数据不加入版本管理。

## 初始化与启动

```powershell
.\.venv\Scripts\python.exe -m flask --app app init-db
.\.venv\Scripts\python.exe -m flask --app app init-admin --username admin
.\.venv\Scripts\python.exe -m flask --app app run --host 127.0.0.1 --port 5000
```

访问：

- 商品大厅/首页：http://127.0.0.1:5000/
- 我的商品：http://127.0.0.1:5000/my/products
- 发布商品：http://127.0.0.1:5000/products/new
- 商品审核与管理：http://127.0.0.1:5000/admin/products
- 注册：http://127.0.0.1:5000/auth/register
- 统一登录：http://127.0.0.1:5000/auth/login
- 我的账号：http://127.0.0.1:5000/account
- 买入预约：http://127.0.0.1:5000/reservations?side=buying
- 卖出预约：http://127.0.0.1:5000/reservations?side=selling
- 管理员工作台：http://127.0.0.1:5000/admin
- 交易统计：http://127.0.0.1:5000/admin/statistics
- 操作日志：http://127.0.0.1:5000/admin/logs
- 管理员账号：http://127.0.0.1:5000/admin/accounts（仅首个管理员）
- 健康检查：http://127.0.0.1:5000/health

在启动窗口按 Ctrl+C 停止。需要断点与自动重载时可显式加 --debug。
该启动命令用于本地开发。数据库按项目绝对路径定位，不依赖启动时临时目录。
/health 会实际执行 SELECT 1；数据库不可用时返回 HTTP 503。

## 首次演示与常见问题

先使用上面的 init-admin 在本机设置管理员，再从注册页创建两个普通账号（买家和卖家）。
管理员与普通用户统一登录，没有随提交目录附带的默认用户名/密码。
推荐顺序：卖家提交商品 → 管理员审核 → 买家预约 → 卖家确认 → 买家确认完成；
随后由管理员在统计和日志页核对结果。屏蔽、恢复等可另用测试商品操作。

- 提示 SECRET_KEY 缺失：确认当前在 code 目录，已执行生成 .env 的命令，且没有空的同名环境变量覆盖它。
- 管理员无法登录：先执行 init-admin，使用自己设置的用户名和密码；普通注册不能获取管理员权限。
- 5000 端口已占用：将启动命令中的 `--port 5000` 改成 `--port 5001`，浏览器地址同步改端口。
- Python/依赖不匹配：确认虚拟环境解释器为 Python 3.12，按锁文件安装，并执行 pip check。
- 初次页面没有商品：提交副本使用空数据库，需创建商品并审核后才会出现在商品大厅。

## 账号与权限

init-admin 会在本地终端隐藏输入密码并要求确认，不接受命令行密码参数，也没有默认密码。
用户名可自行指定，默认 admin。重复执行不会覆盖账号或重置密码。
尚未预置首个管理员时，执行上面的 init-admin 命令并在本地设置密码。已有管理员可直接登录，无需重复初始化。

INITIAL_ADMIN_ID 在 app/config.py 中固定配置为 1，不读取表单或 Cookie 的同名字段。
init-db 为该编号保留 SQLite 自增序号，因此管理员尚未设置时，普通注册仍可使用且不会占用编号 1。
后续管理员必须由首个管理员登录后，在“管理员账号 → 批准并设置账号”中创建。
申请在线下核对，本阶段不扩展为独立的线上申请、审核队列。

| 身份 | 个人账号 | 管理工作台入口 | 管理员列表与新增 |
|---|---|---|---|
| 游客 | 要求登录 | 要求登录 | 要求登录 |
| 普通用户 | 本人 | 拒绝 | 拒绝 |
| 后续管理员 | 本人 | 允许 | 拒绝 |
| 首个管理员 | 本人 | 允许 | 允许 |

普通注册始终创建 MEMBER，同一账号可作为买家和卖家；注册成功后进入统一登录页。
创建管理员始终创建新 ADMIN，已占用的用户名不能用于升级普通账号或覆盖旧密码。
后端每次检查登录身份、角色及需要的对象归属，菜单可见性不是授权依据。
商品编辑和状态操作已接入所属卖家检查；预约也已按买家、所属卖家及操作类型检查。

输入约束：用户名去除首尾空白，长度 1–40，不接受控制字符，按大小写区分且数据库唯一。
密码长度 8–128，不能全部为空白，不去除首尾空白；创建账号需要确认密码。
这些是本阶段补充的实现约束，不改变实验二的角色与授权流程。
密码通过 Werkzeug 的 scrypt 加随机盐生成哈希，只保存完整哈希，不在响应或业务日志输出密码。

登录后浏览器只保存签名保护的随机会话标识，不保存密码或权威角色。
数据库仅保存该标识的 SHA256，登录有效期为 8 小时，访问时重新读取账号角色。
退出会删除服务端会话；已复制的旧 Cookie 也不能恢复登录。服务重启后，未过期会话仍可使用。
本阶段新增 login_sessions 是实现会话撤销的技术支持表，属于对 UML 的实现细化。
注册、登录、退出和管理员创建均有 CSRF 保护；退出仅接受 POST。
账号页只展示会话对应账号，查询参数 user_id、表单 actor 或 Cookie role 均不能更换身份。

## 开发检查

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q app tests tools
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pylint app tests tools
```

测试使用临时目录内的独立数据库，不修改日常业务数据。
整体验收、并发及性能复现命令见 [验收记录](docs/acceptance.md)，测试中的固定密码仅用于临时虚构账号。
检查覆盖基础启动和数据库，以及注册校验、带盐哈希、重复与并发重名注册、两种身份登录、
初始管理员预置、管理员审批、重复用户名保护、游客与角色权限矩阵、归属校验、
CSRF、重定向约束、角色实时读取、会话过期、退出后 Cookie 重放和应用重启。

## 结构与后续扩展

- app/models：用户、登录会话、商品、预约、成功请求记录、状态枚举与操作日志。
- app/services：业务逻辑，对应五个业务模块的服务类。
- app/views：HTTP 页面入口，只接收参数、调用服务和返回结果。
- app/templates、app/static：公共模板、页面与样式资源。
- instance：本地数据库及后续上传图片，自动创建并被 Git 忽略。
- tests：用户、商品、预约、管理员及整体验收边界集成验证。
- tools：真实 HTTP 流程、进程重启、并发一致性与查询性能验证。

后续业务写操作需显式使用事务；会话关闭时回滚未提交的工作，不在请求结束时自动提交。
商品、预约、成功请求记录及操作日志已在同一事务保存；重复成功请求返回原预约当前状态。
已启用全局 CSRF 保护；现有表单已携带 csrf_token，后续 AJAX 请求应传 X-CSRFToken。

本阶段不初始化或推送远程 Git 仓库。后续建立 lab3 时应承接 lab2 的提交历史。

## 官方参考

- Flask 应用工厂：https://flask.palletsprojects.com/en/stable/patterns/appfactories/
- Flask 测试：https://flask.palletsprojects.com/en/stable/testing/
- SQLAlchemy SQLite 事务与外键：https://docs.sqlalchemy.org/en/20/dialects/sqlite.html
- Flask-WTF CSRF：https://flask-wtf.readthedocs.io/en/1.2.x/csrf/
- Werkzeug 密码哈希：https://werkzeug.palletsprojects.com/en/stable/utils/#werkzeug.security.generate_password_hash
- Flask 安全说明：https://flask.palletsprojects.com/en/stable/web-security/
