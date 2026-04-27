# MEMORY.md - 拓越后端项目长期记忆

## 项目基础信息
- 项目路径：`D:\tuoyue-backend-clean`
- 技术栈：Python 3.10.4 + FastAPI + slowapi + Redis + MySQL(Prisma)
- Git 远程：`git@github.com:Add-peng/tuoyue-backend.git`
- 前端路径：`C:\Users\Admin\tuoyue-saas-web`（Next.js App Router + shadcn/ui）
- 生产域名：API `https://api.tuoyue-tech.icu`，前端 `https://pofengzhetuoyue.vercel.app`

## 环境约束
- **本地网络受限**：PyPI 官方源和部分镜像存在 SSL 问题；阿里云镜像 `https://mirrors.aliyun.com/pypi/simple/` 可用
- Node.js：v22.16.0，npm 10.9.2（已安装）
- Python：3.10.4，pip 26.0.1

## 数据库配置
- MySQL 数据库：`tuoyue_db_new`，用户 `pofengzhe`，密码 `ZzZdLe6Jp56n78my`
- MySQL root 密码：`TuoyueABC1`（通过 SSH 隧道验证）
- 连接方式（本地开发）：`ssh -L 3306:127.0.0.1:3306 root@8.218.14.40 -N -f` 建立隧道后使用 `127.0.0.1:3306`
- `.env` DATABASE_URL：`mysql://pofengzhe:ZzZdLe6Jp56n78my@127.0.0.1:3306/tuoyue_db_new`
- **表结构已同步**：`prisma db push` 已成功执行，users / orders / agents 三张表已创建
- Redis：localhost:6379（Docker 运行）

## 已实现模块

### 认证与用户
- `user_store.py`：基于 Redis 的用户存储（手机号哈希 key、JWT 7天 token、PBKDF2 密码哈希）
- 接口：`POST /api/auth/send-code`、`/api/auth/register`、`/api/auth/login`、`GET /api/user/profile`

### 积分计费
- `billing_service.py`：Redis 原子积分操作（grant/deduct/get/has_sufficient）
- 计费规则：1积分/1K tokens；新用户奖励 100 积分
- 接口：`GET /api/user/credits`

### 支付宝支付
- `payment_service.py`：alipay-sdk-python 懒加载（无 SDK 返回 503）
- 套餐：pkg_10(10元/100积分)、pkg_50(50元/600积分)、pkg_100(100元/1500积分)
- 订单存 Redis `order:{order_id}`（pending TTL 2h，paid TTL 30d）
- 接口：`POST /api/order/create`、`POST /api/order/webhook`、`GET /api/order/status/{id}`
- .env 已配置：ALIPAY_APP_ID、ALIPAY_PRIVATE_KEY、ALIPAY_PUBLIC_KEY、ALIPAY_NOTIFY_URL、ALIPAY_RETURN_URL

### 管理后台
- `app/admin.py`：Mock 数据（待接 MySQL），路由前缀 `/api/admin`
- 接口：users 列表/详情/tier更新/reset-password、orders 列表、stats、grant-credits
- Admin Redis 注入：`admin.set_redis_client(redis_client)`

### 短信服务
- `sms_service.py`：阿里云短信，签名"破风者"，5min 验证码 TTL
- 降级：SDK 不可用时打印到控制台

### 敏感词过滤
- `app/middleware.py`：DFA 算法 `SensitiveWordMiddleware`

### Prisma ORM（MySQL）
- `schema.prisma`（项目根）：User / Order / Agent 三模型
- `lib/prisma.py`：全局单例 get_db()，startup/shutdown 生命周期
- prisma 0.15.0 已安装；`prisma generate` 成功生成 client.py
- **注意**：`prisma generate` 需先设置 `$env:PRISMA_GENERATOR_INVOCATION="1"` 才能正确生成
- **表结构已同步**：`prisma db push` 执行成功，三张表（users / orders / agents）已在 tuoyue_db_new 中创建
- 授权方式（本地开发）：通过 pymysql 用 root 授权给 pofengzhe（MySQL root 密码 TuoyueABC1）

## 用户偏好
- 沟通语言：中文
- 提交规范：feat/refactor/docs 前缀，指定 commit message
- 失败时：暂停等待指令；成功时：自动 git add/commit/push
- 设计风格：极简科技风（圆角、细边框、indigo 强调色）

## 关键技巧
- prisma generate 正确方式：`$env:PRISMA_GENERATOR_INVOCATION="1"; python -m prisma generate`
- pip 安装：使用阿里云镜像 `-i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com`
- PowerShell 不支持 `tail`，用 `Select-Object -Last N` 替代
