# API Key 安全设计

rag-center 是一个面向多租户的 RAG 服务平台。每个接入方都是一个独立租户，通过 API 调用检索、问答等能力。第一道门是鉴权。

鉴权方案的选型不是随意的。常见方案有三种：

1. **Session 登录态**：依赖服务端存储登录状态，不适合无状态的 API 网关场景。
2. **JWT Token**：可以做无状态鉴权，但吊销成本高，一旦签发就很难在过期前强制失效。
3. **API Key**：无需服务端维护登录态，也能精确控制生命周期，对服务与服务之间的调用场景天然友好。

rag-center 的消费方是后端服务，不是浏览器用户，API Key 是最合适的选择。

## Key 的格式与生成

Key 采用固定前缀加随机串的格式：

```
rk_live_<32位随机十六进制>
```

前缀 `rk_live_` 有两个作用：一是在日志或错误信息里能一眼辨认这是 rag-center 的 Key，而不是其他系统的凭证；二是方便 secret scanning 工具（比如 GitHub 内置的检测）识别泄露的 Key。固定前缀是业界常见做法，Stripe、OpenAI 的 Key 都有类似设计。

32 位十六进制的随机部分由密码学安全的随机数生成器产生，熵值足够高，不存在碰撞或穷举的实际威胁。生成逻辑如下：

```python
# rag-center/app/utils/api_key_crypto.py

import hashlib
import secrets

# 明文 Key 的固定前缀，便于识别与 secret scanning
KEY_LIVE_PREFIX = "rk_live_"


# 鉴权时对请求头里的 Bearer Token 做 SHA-256，再与库中 key_hash 比对
def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


# 创建 Key 时调用，返回 (明文, 哈希, 日志用前缀)
def generate_api_key_material() -> tuple[str, str, str]:
    secret = secrets.token_hex(16)        # 32 位十六进制随机串
    raw_key = f"{KEY_LIVE_PREFIX}{secret}"
    key_prefix = f"rk_{secret[:4]}"       # 短前缀，形如 rk_abcd，可安全写入日志
    return raw_key, hash_api_key(raw_key), key_prefix
```

Key 生成后只返回一次明文，后续入库存储的是哈希值和前缀，明文永远不落地。

## 明文不入库

这是整个鉴权设计里最核心的一条原则。

Key 被创建出来之后，系统**只打印一次明文**，告知用户保存好。此后，数据库里永远不会存储这个明文串，存的只是它的 SHA-256 哈希值。

鉴权时，系统从请求头里拿到 Bearer Token，先做一次 SHA-256，再拿计算结果去数据库里查：

```
请求头: Authorization: Bearer rk_live_REDACTED...
                          │
                     SHA-256 哈希
                          │
                    查数据库比对
                          │
               校验: active=true & 未过期 & 租户未禁用
                          │
                    注入 TenantContext
```

这和密码存储的思路一样，区别是 API Key 不需要加盐——Key 本身的随机性已经足够高，不存在彩虹表攻击的空间。即使数据库被完整拖走，攻击者拿到的也只是哈希值，无法反推出原始 Key，无法冒用任何租户的身份。

## key_prefix 的工程价值

完整 Key 只有用户自己知道，但服务侧需要一种方法，在不暴露 Key 的前提下，在日志里标记"这次请求用的是哪把 Key"。

解决方法是 **key_prefix**。创建 Key 时，系统取随机段的前 4 位拼成短前缀存入数据库（如 `rk_abcd`），日志和 `/auth/me` 接口返回的也是 prefix，而不是完整 Key：

```python
# rag-center/scripts/create_api_key.py

# 生成三元组：明文只在此处打印一次，入库仅存 hash 与 prefix
raw_key, key_hash, key_prefix = generate_api_key_material()
# ...
print(f"api key created: tenant_id={tenant_id} key_prefix={key_prefix} name={name}")
print(f"plain key (save now, shown once): {raw_key}")
```

这样运维人员看到日志里的 prefix，就能和数据库里的 Key 记录对应上，方便排查是哪个租户的哪把 Key 在请求，也方便快速定位和吊销。短 prefix 暴露的信息量极小，对 Key 的安全性几乎没有影响。

## AUTH_ENABLED 开关

生产环境需要强鉴权，但本地开发阶段频繁生成 Key、配置环境会拖慢节奏。rag-center 提供了环境变量 `AUTH_ENABLED`（对应配置项 `settings.auth_enabled`），设为 `false` 时跳过 Key 校验，直接注入固定的开发租户：

```python
# rag-center/app/core/auth.py

# 从 Authorization 头解析租户身份，供所有受保护路由复用
def resolve_tenant_context(db: Session, authorization: str | None) -> TenantContext:
    # AUTH_ENABLED=false 时跳过 Key 校验，注入固定开发租户
    if not settings.auth_enabled:
        tenant_repository = TenantRepository(db)
        tenant = tenant_repository.get_by_id(DEFAULT_DEV_TENANT_ID)
        tenant_name = tenant.name if tenant is not None else DEFAULT_DEV_TENANT_ID
        return TenantContext(
            tenant_id=DEFAULT_DEV_TENANT_ID,  # "tenant_demo"
            tenant_name=tenant_name,
            key_id=None,
            key_prefix=None,
            key_name=None,
        )

    raw_key = parse_bearer_token(authorization)
    if raw_key is None:
        raise AppError(ErrorCode.UNAUTHORIZED)

    api_key_repository = ApiKeyRepository(db)
    # 哈希后查库，同时校验 status / 过期时间 / 租户是否禁用
    api_key = api_key_repository.get_active_by_hash(hash_api_key(raw_key))
    if api_key is None or api_key.tenant is None:
        raise AppError(ErrorCode.UNAUTHORIZED)

    return TenantContext(
        tenant_id=api_key.tenant_id,
        tenant_name=api_key.tenant.name,
        key_id=api_key.id,
        key_prefix=api_key.key_prefix,
        key_name=api_key.name,
    )
```

鉴权通过 FastAPI 依赖注入接入，而不是单独的鉴权中间件：

```python
# rag-center/app/api/v1/deps.py

# 路由层 Depends 此函数，自动完成 Bearer Token → TenantContext
def get_current_tenant(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> TenantContext:
    return resolve_tenant_context(db, authorization)
```

设计思路是：**安全策略和开发体验可以分开配置**，而不是用一套硬编码逻辑同时服务两种场景。生产部署时环境变量默认开启鉴权，CI 和本地开发可以关掉以减少摩擦。这类问题的防线不在代码里，而在部署流程和环境变量管理上——只要生产环境的配置管理规范，`AUTH_ENABLED=false` 就不会出现在生产机器上。

## 鉴权之后

鉴权成功后，路由层通过 `Depends(get_current_tenant)` 拿到 **TenantContext**，后续业务逻辑只传 `tenant_id`，不再重复解析请求头：

```python
# rag-center/app/api/v1/routes/rag.py

@router.post("/retrieve")
async def retrieve(
    request: RetrieveRequest,
    db: Session = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant),  # 鉴权在此完成
) -> dict:
    # 业务层只接收 tenant_id，不再接触原始 Token
    data = await RagService(db).retrieve(tenant.tenant_id, request)
    return success_response(data.model_dump(mode="json"))
```

这是一个典型的依赖注入模式：鉴权层负责"你是谁"，业务层负责"你能做什么"，职责清晰，互不耦合。

知道"谁在访问"只是第一步。下一个问题是：多个租户共用同一套数据库，如何确保他们彼此看不到对方的数据？这就是数据隔离要解决的问题。
