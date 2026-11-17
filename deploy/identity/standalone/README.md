# 独立身份服务

在部署机运行 Keycloak、PostgreSQL 和 Nginx。Nginx 在回环端口提供 Harniqo realm 的 HTTPS 登录接口。数据库和 Keycloak 管理接口位于容器内部网络。局域网访问由部署机的入口转发到回环端口。

在本目录保存或通过 `--env-file` 指定部署环境文件。选择一个稳定的项目名，例如 `harniqo-identity`，升级时沿用该名称和数据库卷。

| 变量 | 配置 |
| --- | --- |
| `HARNIQO_IDENTITY_PUBLIC_ORIGIN` | 完整 HTTPS 地址，包含非默认端口，例如 `https://identity.example.internal:10055` |
| `HARNIQO_IDENTITY_PUBLIC_HOST` | 该地址的主机名或 IPv4 地址 |
| `HARNIQO_IDENTITY_PUBLIC_PORT` | 该地址的端口 |
| `HARNIQO_IDENTITY_LOCAL_PORT` | 部署机回环端口，默认 `10055` |
| `HARNIQO_IDENTITY_POSTGRES_IMAGE` | 与现有数据兼容的 PostgreSQL 17 镜像摘要 |
| `HARNIQO_IDENTITY_PROXY_IMAGE` | Nginx 官方镜像摘要 |
| `HARNIQO_IDENTITY_DB_PASSWORD_FILE` | 数据库密码文件的绝对路径，权限设为 `0600` 并让容器 UID 1000 可读 |
| `HARNIQO_IDENTITY_TLS_CERT_FILE`、`HARNIQO_IDENTITY_TLS_KEY_FILE` | 桌面与浏览器信任的 TLS 证书链和私钥的绝对路径 |
| `HARNIQO_IDENTITY_REALM_FILE` | 初始 realm 文件的绝对路径，并让容器 UID 1000 可读 |

将 [realm 模板](harniqo-realm.json) 写入运行时配置目录。首次启动时 Keycloak 创建 realm 并注册 `harniqo-desktop` 公开客户端。后续账号和配置保存在数据库中，由管理员维护。

```bash
docker compose -p harniqo-identity --env-file /absolute/path/to/identity.env up -d --wait --wait-timeout 150
```

从用户设备验证 `/realms/harniqo/.well-known/openid-configuration`，再将发现文档的 `issuer` 和客户端 ID 发布到 [Harniqo 控制面](../../../docs/operations/IDENTITY.md)。

需要建立或恢复临时管理员时，先停止本部署的 Keycloak。下面的命令从权限为 `0600`、容器 UID 1000 可读的文件读取管理员密码。将临时管理员用于创建正式账号，再删除临时账号。

```bash
docker compose -p harniqo-identity --env-file /absolute/path/to/identity.env stop proxy keycloak
docker compose -p harniqo-identity --env-file /absolute/path/to/identity.env up -d --wait database
docker compose -p harniqo-identity --env-file /absolute/path/to/identity.env run --rm --no-deps \
  --volume /absolute/path/to/bootstrap-password:/run/secrets/bootstrap_password:ro \
  keycloak 'export KC_DB_PASSWORD="$(cat /run/secrets/db_password)"
            export HARNIQO_BOOTSTRAP_PASSWORD="$(cat /run/secrets/bootstrap_password)"
            exec /opt/keycloak/bin/kc.sh bootstrap-admin user --username harniqo-bootstrap \
              --password:env HARNIQO_BOOTSTRAP_PASSWORD --no-prompt'
docker compose -p harniqo-identity --env-file /absolute/path/to/identity.env up -d --wait --wait-timeout 150
```

从部署机进入 Keycloak 容器，使用 `/opt/keycloak/bin/kcadm.sh` 连接容器内的 `http://127.0.0.1:8080` 管理账号。回滚时停止此 Compose 项目并保留命名数据库卷，再恢复控制面的上一版身份配置。
