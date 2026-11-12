# Keycloak 本机联调

`qualify-keycloak.sh` 使用 Docker 启动固定镜像的临时 Keycloak，仅在 `https://127.0.0.1:10055` 提供服务。需要 Docker、OpenSSL、curl 和 jq。第一次运行会从 Quay 拉取镜像。测试 CA、证书和随机密码保存在本机临时目录，子命令退出后清理容器和目录。[Keycloak 容器说明](https://www.keycloak.org/server/containers)

```sh
bash deploy/identity/qualify-keycloak.sh bash -c '
  curl --noproxy "*" --fail --silent --show-error --cacert "$HARNIQO_TEST_CA" \
    "$HARNIQO_TEST_ISSUER/.well-known/openid-configuration" | jq -r .issuer
'
```

子命令可通过 `HARNIQO_TEST_DISCOVERY_FILE` 获取 Harniqo 控制面需要发布的身份元数据，通过 `HARNIQO_TEST_CA` 为该进程建立 TLS 信任。测试账号名为 `HARNIQO_TEST_USERNAME`，密码保存在 `HARNIQO_TEST_PASSWORD_FILE` 指向的文件。回环服务器证书和私钥分别由 `HARNIQO_TEST_CERT` 和 `HARNIQO_TEST_KEY_FILE` 提供。

公开客户端 `harniqo-desktop` 要求 PKCE S256。Keycloak 将带临时端口的本机回调与注册的 `http://127.0.0.1/oauth/callback` 匹配。[Keycloak 回调 URI 说明](https://www.keycloak.org/securing-apps/oidc-layers#_redirect_uris) · [26.8.2 回调校验实现](https://github.com/keycloak/keycloak/blob/26.8.2/services/src/main/java/org/keycloak/protocol/oidc/utils/RedirectUtils.java#L119-L123)
