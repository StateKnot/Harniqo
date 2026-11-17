# 控制面身份发现

M1-06 的公开接口 `GET /v1/auth/discovery` 返回部署配置的 OIDC issuer、桌面公开 client ID、scope 和 PKCE 要求。控制面和 Node 客户端使用同一份 draft 2020-12 契约校验这些字段。调用方可使用 `@harniqo/client` 的 `discoverPlatform` 从指定 HTTPS 或 HTTP origin 读取配置。独立部署或接入现有 OIDC 提供方的步骤见[身份服务配置指南](../../docs/operations/IDENTITY.md)。

从仓库根目录执行。使用已获准的 issuer 和公开 client ID。配置文件格式如下。

```json
{
  "issuer": "https://identity.example.invalid/realms/harniqo",
  "client_id": "harniqo-desktop",
  "required_scopes": ["openid", "profile"],
  "authorization_code_pkce_required": true
}
```

启动时将 TLS 证书与私钥通过文件路径交给进程。默认绑定 `127.0.0.1:10050`，实际部署由受信配置指定地址与端口。客户端以受信 CA 校验证书和主机名。

```sh
pnpm install --frozen-lockfile
export HARNIQO_DISCOVERY_FILE=/absolute/path/to/discovery.json
export HARNIQO_TLS_CERT_FILE=/absolute/path/to/tls.crt
export HARNIQO_TLS_KEY_FILE=/absolute/path/to/tls.key
pnpm --filter @harniqo/control-plane start
```

使用客户端可访问的 HTTPS origin 检查接口。在私有 CA 环境中为验证命令指定 CA 文件。

```sh
curl --cacert /absolute/path/to/ca.pem https://localhost:10050/v1/auth/discovery
pnpm --filter '@harniqo/control-plane...' -r typecheck
pnpm --filter @harniqo/control-plane test
```

局域网联调可显式设置 `HARNIQO_CONTROL_PROTOCOL=http`。HTTP 模式直接读取同一份发现配置，无需 TLS 文件。默认只监听本机，局域网客户端需使用通过 `HARNIQO_CONTROL_HOST` 配置的可达地址。

```sh
HARNIQO_CONTROL_PROTOCOL=http pnpm --filter @harniqo/control-plane start
curl http://127.0.0.1:10050/v1/auth/discovery
```

尚未配置身份提供方时，可在 HTTP 模式省略 `HARNIQO_DISCOVERY_FILE`。`GET /v1/auth/discovery` 此时返回 HTTP 503 和 `{"error":"identity_not_configured"}`。配置有效的发现文件后，重启服务即可获取身份元数据。HTTPS 模式启动时必须提供发现文件和 TLS 文件。

HTTP 仅用于无凭据的公开发现。OIDC issuer 仍要求 HTTPS，后续登录和受保护的 API 使用 HTTPS。

## Hermes 工作台业务接口

已配置身份和 PostgreSQL 的 HTTPS 控制面可加载工作台。公开发现仍可通过独立 HTTP 监听提供。HTTP 登录表单中的业务接口地址填管理员提供的 HTTPS 控制面 origin，桌面主进程经该 origin 调用版本握手、部署查询、指令提交、回执查询和 Run 状态/事件查询。

设置 `HARNIQO_WORKBENCH_FILE` 和 `HARNIQO_WORKBENCH_DATABASE_URL_FILE`。后者指向运行用户所有、权限为 `0600` 的独立文件，内容是 `harniqo_control` 登录角色的 `postgresql://` 连接字符串。远程数据库配置 `sslmode=verify-full`，使用进程受信任的 CA 或绝对路径的 `sslrootcert`。配置示例中的 UUID、主机和版本应换成部署的实际值。

```json
{
  "access": {
    "organizationId": "00000000-0000-4000-8000-000000000001",
    "issuer": "https://identity.example.internal/realms/harniqo",
    "jwksUri": "https://identity.example.internal/realms/harniqo/protocol/openid-connect/certs",
    "audience": "harniqo-workbench",
    "desktopClientId": "harniqo-desktop",
    "accessTokenProfile": "keycloak",
    "algorithm": "RS256"
  },
  "platform": {
    "platform_id": "00000000-0000-4000-8000-000000000002",
    "platform_epoch": "00000000-0000-4000-8000-000000000003",
    "build_id": "deployment",
    "software_version": "0.1.0",
    "selected_contract_version": "0.1.0",
    "supported_contract_versions": ["0.1.0"],
    "minimum_component_versions": {"desktop": "0.1.0", "runner": "0.1.0", "execution_agent": "0.1.0"},
    "feature_flags": []
  }
}
```

用独立迁移账号应用工作台存储 001、身份映射 002、[预算及队列 003](workbench-queue/README.md)、执行核对 004。运行账号与数据库迁移账号分离。身份配置中的 issuer 和 desktop client ID 必须与公开发现一致。Keycloak 新建 realm 的模板会给桌面 Access Token 添加业务 audience，已有 realm 需要管理员增加同名 audience 协议映射。外部提供方可发 RFC 9068 Access Token，并使用 `rfc9068`。需要分别配置身份映射、项目成员关系和运行预算。[独立身份服务](../../deploy/identity/standalone/README.md)

只有具备当前授权和有效执行就绪期限的目标出现在部署列表。受理时在数据库事务中再次校验目标和预算。实际执行还需要已登记的隔离运行环境及能验证当前授权和已服务模型的执行端。未接上该执行端的环境应保持目标停用，独立验证接口与队列。

接口集成验证使用一次性 PostgreSQL（数据库名 `harniqo_workbench_route_test`）及临时 HTTPS 测试身份提供方。通过私有环境变量 `HARNIQO_TEST_POSTGRES_URL` 提供本机回环数据库 URL 后运行 `pnpm --filter @harniqo/control-plane test:workbench-routes`。
