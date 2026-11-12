# 配置 Harniqo 身份服务

平台管理员在服务器上配置 OIDC 身份提供方。每个 Harniqo 部署发布一个 `issuer` 和一个桌面端 `client_id`。当前桌面「测试连接」会显示平台是否可达，以及平台发布的身份配置。系统浏览器登录属于 M1-03。[开发清单](../planning/BACKLOG.md)

## 选择身份提供方

- **独立部署。** 通过 HTTPS 部署 Keycloak，在管理控制台创建 realm、管理本地用户，并在该 realm 下为 Harniqo 注册桌面 OIDC 客户端。同一个 Keycloak realm 也可以为其他系统分别注册客户端，各自使用自己的客户端 ID 和回调配置。[Keycloak 生产部署](https://www.keycloak.org/server/configuration-production) · [管理控制台](https://www.keycloak.org/docs/latest/server_admin/)
- **接入现有系统。** 在该系统使用的 OIDC 提供方中为 Harniqo 注册独立的公开桌面客户端，取得 HTTPS `issuer` 和注册返回的 `client_id`。身份提供方继续管理账号和登录策略。[原生应用注册规范](https://datatracker.ietf.org/doc/html/rfc8252#section-8.4)

桌面客户端采用公开客户端的授权码流程和 PKCE。在 Keycloak 中将 `Client authentication` 设为 `Off`，并启用 `Standard Flow`。在提供方启用 PKCE S256，按桌面登录版本公布的回调地址配置重定向。发现文件里的 `client_id` 是公开标识，账号凭据由身份提供方管理。[Keycloak 客户端设置](https://www.keycloak.org/docs/latest/server_admin/#_oidc-clients) · [原生应用授权流程](https://datatracker.ietf.org/doc/html/rfc8252#section-6)

## 在控制面发布发现配置

从用户设备和控制面可访问的网络检查提供方的 HTTPS 发现文档。将示例地址替换为提供方给出的 issuer。返回的 `issuer` 应逐字匹配配置值，`authorization_endpoint`、`token_endpoint` 和 `jwks_uri` 应为可访问的 HTTPS 地址。客户端注册信息在身份提供方的管理端核对。[OIDC Discovery 规范](https://openid.net/specs/openid-connect-discovery-1_0.html#ProviderConfig)

```sh
oidc_issuer='https://identity.example.invalid/realms/harniqo'
curl --fail --silent --show-error "${oidc_issuer%/}/.well-known/openid-configuration"
```

将以下字段写入服务器配置文件，例如 `/etc/harniqo/discovery.json`。文件路径由部署者选择，运行控制面的账号需要能够读取该文件。将示例值换成已核对的 issuer 和桌面客户端 ID。

```json
{
  "issuer": "https://identity.example.invalid/realms/harniqo",
  "client_id": "harniqo-desktop",
  "required_scopes": ["openid", "profile"],
  "authorization_code_pkce_required": true
}
```

在控制面的运行环境设置 `HARNIQO_DISCOVERY_FILE` 为该文件的绝对路径。systemd 部署可通过对应服务的 drop-in 设置环境变量，再重启控制面。

```ini
[Service]
Environment=HARNIQO_DISCOVERY_FILE=/etc/harniqo/discovery.json
```

从客户端访问控制面 `GET /v1/auth/discovery`，核对 HTTP 200 和返回的 `issuer`、`client_id`。这个结果验证控制面已发布配置。提供方发现、客户端注册和真实账号登录分别验证。HTTP 控制面地址可用于公开探测，后续系统浏览器登录从客户端信任的 HTTPS 平台地址发起。[控制面启动说明](../../apps/control-plane/README.md) · [原生应用浏览器登录规范](https://datatracker.ietf.org/doc/html/rfc8252#section-4.1)
