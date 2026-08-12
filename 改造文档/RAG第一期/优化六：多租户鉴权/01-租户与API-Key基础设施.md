# 01 - 租户与 API Key 基础设施

rag-center 的数据层早就有 `tenant_id` 隔离，但 API 层面还靠调用方自己在请求体里填租户 ID——谁都可以伪造，算不上真正的多租户中台。

这一步先把鉴权的「地基」打好：**租户是谁、Key 怎么发、请求怎么验**。业务接口（建库、上传、检索）的改造放在下一篇，这篇不动它们。

## 要解决什么问题

现在的调用方式大概是这样：

```bash
curl -X POST /api/v1/rag/retrieve \
  -d '{ "tenant_id": "tenant_a", "kb_id": "...", "query": "..." }'
```

`tenant_id` 写在 body 里，服务端直接信。任何人只要猜到别的租户 ID，就能读写对方的数据。作为对外提供能力的 RAG 中台，这不可接受。

改造后的思路很简单：**每个租户发一把 API Key，请求头带上 Key，服务端自己解析出 tenant_id**，不再信任客户端传的租户字段。

## 数据怎么存

新增两张表，关系如下：

```
tenants（租户）
  id          → 就是 tenant_id，如 tenant_demo
  name        → 展示名
  status      → active / disabled
  created_at

api_keys（密钥）
  id          → UUID
  tenant_id   → 外键，指向 tenants.id
  key_hash    → 只存 hash，绝不存明文
  key_prefix  → 如 rk_a1b2，方便日志里识别是哪把 key
  name        → 备注，如「客服系统对接」
  status      → active / revoked
  expires_at  → 可选，过期时间
  created_at
```

Key 的明文格式建议：`rk_live_` 加 32 位随机字符。创建时终端打印一次，之后库里只有 hash。

## 鉴权怎么验

所有后续业务接口都会复用同一套逻辑，这篇先把核心组件写好：

1. 从请求头读取 `Authorization: Bearer rk_live_xxx`
2. 对 Key 做 hash，去 `api_keys` 表查
3. 校验：存在、status 为 active、未过期、对应 tenant 为 active
4. 通过后得到 `TenantContext`（含 `tenant_id`），供后续接口使用

本地开发需要能跳过鉴权，通过环境变量控制：

```env
AUTH_ENABLED=false          # 本地调试可关
```

生产环境 `AUTH_ENABLED=true`，不带 Key 或 Key 无效一律返回未授权。关鉴权时固定使用代码内置默认租户 `tenant_demo`（见 `app/core/auth.py`）。

## 这篇要新增什么

**只做后端，不动现有四个业务接口的请求体。**

| 做 | 不做 |
|----|------|
| `tenants` / `api_keys` 表 + Alembic 迁移 | 改 upload / retrieve 的 schema |
| Repository + Model | 前端 |
| `scripts/create_tenant.py`、`scripts/create_api_key.py` | Admin HTTP 接口 |
| `get_current_tenant` Dependency | Key 吊销的 Web UI |
| `GET /api/v1/auth/me` 验证 Key | |
| 新增 `UNAUTHORIZED` 错误码 | |

`/auth/me` 是给调用方「验 Key 是否有效、当前是哪个租户」用的，返回示例：

```json
{
  "code": 0,
  "data": {
    "tenant_id": "tenant_demo",
    "tenant_name": "演示租户",
    "key_prefix": "rk_a1b2",
    "key_name": "本地开发"
  }
}
```

## 给 AI 的提示词

把下面整段复制给 AI，一步完成开发：

```
在 rag-center 项目完成「多租户鉴权 - 01 基础设施」。

本篇只搭鉴权地基，不改建库/上传/检索四个业务接口（02 再做）。

【1. 数据库：租户和 Key 存哪】
- 新增 Alembic 迁移 0002（在现有 0001_initial 之后），建两张表
- tenants：一个租户一行
  - id：即 tenant_id，如 tenant_demo
  - name：给人看的名字
  - status：active / disabled，禁用后该租户下所有 Key 失效
  - created_at
- api_keys：一把 Key 一行，绑在一个 tenant 上
  - id：UUID
  - tenant_id：属于哪个租户
  - key_hash：只存 hash，不存明文（泄露库也不会直接暴露 Key）
  - key_prefix：如 rk_a1b2，日志里用来辨认是哪把 Key，不打完整 Key
  - name：备注，如「客服系统对接」
  - status：active / revoked
  - expires_at：可选过期时间
  - created_at
- 写法、命名风格对齐 migrations/versions/0001_initial.py

【2. Model / Repository：代码里怎么读写表】
- app/models/tenant.py、app/models/api_key.py
- app/repositories/tenant_repository.py、app/repositories/api_key_repository.py
- 生成 Key 时：明文格式 rk_live_<32位hex>，SHA-256 后入库；明文只在创建时打印一次

【3. 配置：本地能不能跳过鉴权】
- app/core/config.py 增加：
  - AUTH_ENABLED：默认 true；本地调试可设 false 跳过验 Key

【4. 鉴权核心：请求头里的 Key 怎么变成 tenant_id】
- app/core/auth.py + app/api/v1/deps.py
- TenantContext：验通过后携带 tenant_id、tenant_name、key_id、key_prefix
- get_current_tenant（FastAPI Depends）：
  - 读 Authorization: Bearer rk_live_xxx
  - hash 后查 api_keys，校验 active、未过期、tenant 未禁用
  - AUTH_ENABLED=false 时：不验 Key，固定使用内置默认租户 tenant_demo（tenant 不存在也可占位通过）
  - 失败抛 AppError UNAUTHORIZED（exceptions.py 新增，如 20010）

【5. 运维脚本：怎么创建租户和 Key（两个独立文件）】
- scripts/create_tenant.py：注册租户（先有这个，才能发 Key）
  - 用法：python scripts/create_tenant.py --id tenant_demo --name "演示租户"
  - 内部复用 app 的 config、db session、TenantRepository
- scripts/create_api_key.py：给已有租户发 Key（一个租户可有多把 Key）
  - 用法：python scripts/create_api_key.py --tenant-id tenant_demo --name "本地开发"
  - 生成明文 rk_live_<32位hex>，终端打印一次；库里只存 SHA-256 hash
- 两个脚本均在项目根目录执行； argparse 解析参数；不要合并成 cli.py / manage.py

【6. 验证接口：调用方检查 Key 是否有效】
- GET /api/v1/auth/me
- 走 get_current_tenant，返回 tenant_id、tenant_name、key_prefix、key_name
- 挂到 api/v1/router.py；02 之前这是唯一需要鉴权的接口

【边界】
- 现有四个业务接口（create/tree/upload/retrieve）本次不改
- 不做 Admin HTTP API、不做前端

【验收】
- scripts/create_tenant.py + create_api_key.py 建 tenant 和 key → curl /auth/me 带 Bearer 成功
- 不带 Key / Key 错误 → code 非 0（UNAUTHORIZED）
- AUTH_ENABLED=false → 不带 Key 也能调 /auth/me
- 现有四个业务接口行为与改前一致（本篇未挂鉴权）
```

## 做完怎么验

前置：`AUTH_ENABLED=true`（默认），后端已启动。

### 1. 跑迁移，确认表已建

```bash
alembic upgrade head
```

预期：`tenants`、`api_keys` 两张表创建成功。

### 2. 创建租户

```bash
python scripts/create_tenant.py --id tenant_demo --name "演示租户"
```

预期：终端提示创建成功。重复执行相同 `--id` 应报错（租户已存在）。

### 3. 创建 API Key

```bash
python scripts/create_api_key.py --tenant-id tenant_demo --name "本地开发"
```

预期：终端打印明文 Key（如 `rk_live_abc123...`），**只出现这一次**，请立即复制。若 tenant 不存在应报错。

### 4. 带正确 Key 调 /auth/me（应成功）

```bash
curl -s http://127.0.0.1:8000/api/v1/auth/me \
  -H "Authorization: Bearer rk_live_你的key"
```

预期：`code` 为 0，`data` 含 `tenant_id: "tenant_demo"`、`tenant_name`、`key_prefix`、`key_name`：

```json
{"code":0,"msg":"success","data":{"tenant_id":"tenant_demo","tenant_name":"演示租户","key_prefix":"rk_a1b2","key_name":"本地开发"}}
```

### 5. 不带 Key（应鉴权失败）

```bash
curl -s http://127.0.0.1:8000/api/v1/auth/me
```

预期：`code` 为 20010（UNAUTHORIZED）。

### 6. 带错误 Key（应鉴权失败）

```bash
curl -s http://127.0.0.1:8000/api/v1/auth/me \
  -H "Authorization: Bearer rk_live_invalid_key_12345"
```

预期：同上，`code` 为 20010。

### 7. 关闭鉴权（本地调试）

`.env` 改为 `AUTH_ENABLED=false`，重启后端后：

```bash
curl -s http://127.0.0.1:8000/api/v1/auth/me
```

预期：不带 Key 也能成功，`tenant_id` 固定为 `tenant_demo`。

### 8. 确认业务接口未被改动

本篇未给业务接口挂鉴权，以下调用应与改前行为一致：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/knowledge-bases/create \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"tenant_demo","name":"测试库","description":""}'
```

预期：`code` 为 0，行为与 01 之前相同。02 完成后此处才需要改为 Header 带 Key。
