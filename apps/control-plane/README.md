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
