# M0-05 受限 HTTPS 上游传输

日期：2026-10-31。状态：固定受限 HTTPS 上游 transport 子项 `VERIFIED`。**不代表完整 Egress、执行权限、预算或内核出口策略已通过**；M0-05 保持 `IN_PROGRESS`。

本轮交付 Model Egress 内部的上游传输 Module，采用既定 Node.js24/TypeScript 与成熟 HTTPS parser。调用方是受信宿主代码；本 Module 不直接开放 Guest HTTP 入口，也不提供任意 URL、CONNECT、Unix socket、代理地址或调用方自选 TLS 策略。未来对外 Egress 仍须逐请求核对执行身份、当前授权、代次、租约与预算，再调用传输 Module。范围沿用[技术架构](../docs/architecture/ARCHITECTURE.md)和[执行网络评审](m0-execution-network-review.md)，不能用本轮受控传输实验替代控制面权威状态与真实模型/Harness/VM 准入。

## 冻结路由与地址策略

可信配置将 `route_id` 绑定到确定 HTTPS origin、允许的方法与精确路径、请求/响应限制及必要的信任配置。origin 使用 WHATWG URL 规范化后冻结，拒绝 userinfo、fragment、query、非根 pathname、尾点和 zone ID 等不支持表示；IDNA 采用运行时的 ASCII hostname 表示。调用方不能通过路径改变 origin。Node 的 `options.path` 会将绝对 URL 按 absolute-form 发送，上游若是代理可按该目标转发，因此允许路径必须是明确的 origin-form，不能把外部路径交给 URL resolve。[URL 行为](https://raw.githubusercontent.com/nodejs/node/v24.22.1/doc/api/url.md)、[HTTP request-target](https://raw.githubusercontent.com/nodejs/node/v24.22.1/doc/api/http.md)。

默认拒绝非公共地址，不能只检查 RFC1918。实现依据 IANA 特殊用途表，保守拒绝特殊用途前缀、组播及保留空间；这包括 CGN、loopback、link-local、documentation、benchmark、IPv4-mapped IPv6 和已知转换前缀。保守策略可以同时拒绝表中少数标为 globally reachable 的特殊服务地址，不将其错误描述为普通公网全部可用。IPv6 公共候选还限定在 `2000::/3` 内，再排除其中的特殊用途段。CIDR 比较优先使用 `net.BlockList` 等运行时设施并区分地址 family。[IPv4 特殊用途表](https://www.iana.org/assignments/iana-ipv4-special-registry/)、[IPv6 特殊用途表](https://www.iana.org/assignments/iana-ipv6-special-registry/)、[IPv6 全局空间](https://www.iana.org/assignments/ipv6-unicast-address-assignments/)。

私有上游确有需要时，例外必须明确绑定该 route 的 origin、端口与精确 IP，不放开整个私有地址段；部署登记的管理地址/网段继续受拒绝策略约束。一次解析返回公共与禁止地址的混合集合时应整体拒绝，不能只挑一个看起来合法的地址绕过检查。每次新请求重新检查实际返回集，连接仅使用本次已经核验的地址。

应用层地址判断存在明确边界：RFC6052 除 well-known prefix 外还允许网络自有 NAT64 前缀，单靠某个 IPv6 地址值无法普适识别所有这类映射。部署必须登记相关转换前缀和管理网，或由独立内核出口策略约束；不能将拦截已知转换前缀称为任意 NAT64/路由转换旁路均已阻断。[RFC6052](https://www.rfc-editor.org/rfc/rfc6052)。

## 固定拨号与 TLS

`dns.lookup({all:true, family:0})` 获取的是操作系统解析设施返回的地址集合，不代表已经枚举所有权威 DNS 记录。检查该完整集合之后，将已选地址通过专用 lookup 交给连接，保留原 hostname，并显式保留原名称的 TLS servername 与证书检查。固定单地址时显式设置已验证的 family 和关闭 `autoSelectFamily`，避免底层自动选择路径要求另一种 lookup 回调或进行未审查的回退。TLS 保持可信 CA 和 `rejectUnauthorized`，不通过关闭验证让私有 fixture 或 Harness 继续运行。[DNS lookup](https://nodejs.org/docs/latest-v24.x/api/dns.html#dnslookuphostname-options-callback)、[固定连接实现](https://raw.githubusercontent.com/nodejs/node/v24.22.1/lib/net.js)、[TLS 名称与证书校验](https://raw.githubusercontent.com/nodejs/node/v24.22.1/doc/api/tls.md)。

不能依赖全局 agent 或环境代理决定实际目标。Node24 已支持 `NODE_USE_ENV_PROXY`、`--use-env-proxy` 和动态全局代理配置；传输实现应使用显式独立、无代理、无跨请求复用的连接策略，并在结束或失败时销毁自己持有的 agent/socket。TLS session cache 也不能跨本轮请求/授权作用域隐式复用。具体 Node24.22.1 源码显示，普通自建 Agent 的代理配置来自其显式 `proxyEnv`，全局 Agent 另有环境初始化路径。[HTTP agent 实现](https://raw.githubusercontent.com/nodejs/node/v24.22.1/lib/_http_agent.js)、[HTTPS agent 实现](https://raw.githubusercontent.com/nodejs/node/v24.22.1/lib/https.js)、[HTTPS 配置](https://raw.githubusercontent.com/nodejs/node/v24.22.1/doc/api/https.md)。

## 有界请求、响应与资源回收

上游认证和路由头来自受信配置；不继承外部 Authorization、Cookie、Proxy-Authorization、Host、Content-Length 或 hop-by-hop 控制。Host 与长度应由固定目标及实际 body 重建。采用允许的原生头集合时，仍需处理 Connection 点名的连接级字段；不把响应的连接级控制转给后续调用方。默认严格 HTTP parser，显式限制响应 header 大小；拒绝协议升级，不自动跟随重定向。原生非重定向错误响应，例如 HTTP4xx，应按本轮声明保留其状态与有界 body，而不是伪造模型成功。[RFC9110 连接级字段](https://www.rfc-editor.org/rfc/rfc9110.html#section-7.6.1)、[Node HTTP](https://raw.githubusercontent.com/nodejs/node/v24.22.1/doc/api/http.md)。

请求和响应均有独立字节上限；流式转发保持原字节，不隐式 JSON 改写、SSE 合并或解压。背压不能仅靠累积 Buffer，`highWaterMark` 也只是阈值而非硬内存上限。使用成熟 stream 机制并明确等待 sink 完成；sink 错误、提前关闭、上游中断或 HTTP message 不完整均不能记录成功。中止时关闭本次 request、response、pipeline 和独立连接，并清理计时器/监听器；不承诺已经停止供应商侧计费。[Stream 背压与取消](https://raw.githubusercontent.com/nodejs/node/v24.22.1/doc/api/stream.md)、[HTTP message.complete](https://nodejs.org/docs/latest-v24.x/api/http.html#messagecomplete)。

总 deadline 从解析前开始，覆盖 DNS、连接、TLS、响应和下游 drain；idle timeout 另行限制无进展窗口。`request.setTimeout` 或 timeout option 只产生事件，不会自行终止请求，必须主动 abort/destroy。[Node timeout 行为](https://raw.githubusercontent.com/nodejs/node/v24.22.1/doc/api/http.md)。

OS `dns.lookup` 在 libuv 线程池执行同步 getaddrinfo，公开接口没有取消该后台工作的 AbortSignal。由此得出的实现约束是：对外超时或取消后，尚未结算的解析仍占用内部并发 permit；迟到结果绝不能继续拨号。不能因 Promise.race 已返回便无限接受新的后台解析。独立 `dns.Resolver` 有不同的取消机制，但不采用相同 hosts/NSS 语义；替换前需另行验证，不能混为一谈。[DNS 实现区别](https://nodejs.org/docs/latest-v24.x/api/dns.html#implementation-considerations)。

## TypeScript 与运行验证

Node24.22.1 的 type stripping 已稳定，可直接执行 erasable-only TypeScript；它不进行类型检查，也不读取 tsconfig。使用明确模块格式、显式导入扩展名和 `import type`；避免依赖 enum、参数属性等运行时转换。`node` 执行通过只能证明运行行为，完整 TypeScript 类型验收须独立运行锁定工具链的 typecheck，不能把 strip-types 冒充 `tsc`。[Node TypeScript](https://raw.githubusercontent.com/nodejs/node/v24.22.1/doc/api/typescript.md)。

## 真实与合成验收分别计量

本轮已在指定开发环境通过正式 Module 运行独立真实 HTTPS fixture，不调用模型。fixture 只在获准端口10020/10021的 loopback 监听；客户端使用 OS 对 localhost 的真实解析，配置仅限 fixture origin 与精确 `127.0.0.1`/`::1` 的例外。该例外不进入普通公网路由默认策略，不新增公网转发或修改全局 trust store。测试 CA、私钥、真实路由配置及原始日志留在受控存储，公开报告仅保留结果与摘要。

| 证据类别 | 已验证的检查及范围 | 不能据此推出 |
| --- | --- | --- |
| 真实 TLS | 正确 CA/SAN、错误 CA、错 SAN、过期证书；错误证书下 HTTP 请求零命中 | 错误证书可能已有 TLS 连接，不能将 HTTP 零命中写成零连接；不证明生产证书轮换已实现 |
| 真实 HTTP/stream | 流式字节与背压、原生4xx、3xx/upgrade拒绝、body/header限制 | 不代表原生 Harness 或生产模型代理已完成 |
| 真实时限/取消 | idle/total、主动 abort、并发饱和、关闭/清理 | 不证明供应商费用即时停止，也不代替执行租约撤销 |
| 合成 DNS/策略 | 可控 resolver 的公共/禁止混合返回、重绑定、地址表示及精确例外 | 只证明所测解析与固定拨号逻辑，不与真实 TLS 数量混记，不证明任意网络转换安全 |

重绑定用例应核对解析调用次数和实际 target 命中：第一次解析返回获准 fixture 地址，若发生第二次解析则改为禁止地址；只有固定已审地址的路径可继续。各成功、拒绝与故障用例均须保留正式源码版本、实际目标计数和资源回收状态，不以单次超时或安装成功代替安全拒绝。

## 实测结果

最终同源组 `a24cab89bf23` 于 `2026-10-31T04:37:05.713Z` 至 `2026-10-31T04:37:08.593Z` 完成指定服务器验收，30个必需门槛全部通过，`errors=0`、`cleanup_errors=0`、`real_model_calls=0`。[原生脱敏报告](../docs/operations/egress-transport-native-2026-10-31.json)记录原始私有报告SHA-256 `990a1f5f74a73bff9a8b58aae38be0f579cda0cfa191beb4299d413b6b4a8667`、配置摘要、源码摘要及分项receipt。实际客户端为Node.js24.22.1、其内嵌OpenSSL3.5.9；固定正式运行时另行通过严格TypeScript6.0.3检查及14项Module回归，零失败/错误/跳过，JUnit摘要为`52d56694d918839a0953fdddd2c1dc69cd50475efb01e9116dc4749aad2f5e37`。本机Node26仅用于开发验证，不混计为Node24正式结果。

30项是资格入口的门槛数，不是30次真实模型或网络请求。`zero_port_rejected`属于配置拒绝；`pinned_dns_dial`、`mixed_dns_denied`、`dns_total_deadline`、`dns_permit_retained`、`late_dns_cannot_dial`使用受控resolver或异步依赖，单列其证据范围。其中固定拨号的TLS连接是真实的，但它不代表已测试完整互联网DNS链路或所有重绑定方式。其余TLS/HTTP/流式用例、目标命中计数和最终端口回收按实际fixture分别留证。

实际fixture记录主目标HTTP14次、均匹配其本轮认证；独立peer正向命中1次、禁止路径命中0次，错误证书的TLS client error计数3。错CA/SAN/过期证书下HTTP未到达目标，不把这些失败写成“没有发生TLS连接”。在明确启用环境代理的资格进程中，正式Module仍使用自己的无代理Agent、固定目标与原SNI；没有将测试CA安装到全局信任库。流式payload为262,144 bytes，32次下游write、观察到的Writable队列峰值16,378 bytes，字节SHA-256 `dd3dde87623d9a6b354c68c943d189c89c63652d945e7bbdf0986cae91a49521`；这只说明本次字节保持和背压行为，不是硬进程RSS上限。

空闲/总期限、调用方取消、并发拒绝、请求/响应/header限制、截断响应、原生HTTP429、3xx及upgrade拒绝均通过。HTTP错误响应保留其原生状态和有界body；中断receipt仍保留`may_have_been_sent`的保守归因，不能据此证明供应商停止计费。超时后不可取消DNS工作保留permit直到结算，迟到结果不能发起连接；不将公开Promise已拒绝当作后台工作已经消失。

前两次记录保持原样：`ef745a7c5158`为FAILED，资格清理路径曾对`net.Server`调用HTTPS专用关闭方法，保留2个cleanup errors；`ccc8406f3326`按当时29门槛PASSED，未包含随后发现的端口0配置缺口。独立修正组增加constructor端口`1..65535`检查与端口0拒绝门槛后得到上述30门槛结果，原失败/原PASSED均未改写。测试客户端、server/socket/timer以及10020/10021监听已按本轮报告回收；CA/私钥、原始日志、配置和此前记录仍在私有证据目录。

最终生产transport SHA-256为`ad4f85f3604debf89da5694cc683224c06e0c76f982f8731e93877cf7fa423f9`，完整7份源码/配置摘要见公开报告。14项Module回归与真实资格入口分别计量；实际GitHub源码`ba836f2039cacf9f7bf1d7c8ac22c1e209c3d57c`的[传输CI](https://github.com/StateKnot/Harniqo/actions/runs/41170364383)已通过锁定Node24.22.1/TS6.0.3严格检查、14项Module与11项失败留证回归；下载artifact后核对source、step outcomes、完整锁及JUnit摘要，见[独立CI摘要](../docs/operations/egress-transport-ci-2026-10-31.json)。同源262项既有资格组件与三平台契约也通过，见[各自范围报告](../docs/operations/qualification-ci-egress-2026-10-31.json)。CI不启动VM或调用模型，也不替代上述真实TLS结果与完整Egress准入。

## 工具链与依赖来源

Node官方Linux x64 archive为32,026,916 bytes，SHA-256`6c172a9ab6718f7b801fb5a7bd486af2ee5e32758086a04b84fcce2aa77095e6`，校验方式`official_https_manifest_sha256`，**`release_signature_verified=false`**。它位于受控`images/toolchains/node/v24.22.1/`；解释器解包在项目`.state/toolchains/`，未覆盖系统Node。实际完整LICENSE摘要为`5888dbb9a1d2b18f2c3e6c5f6af1b39de658372b402a0577b002777f14c62ace`。私有清单保留原11项并增为12项，本次新增是运行工具链archive，不是VM/容器镜像。[工具链脱敏来源](../docs/operations/node-toolchain-2026-10-31.json)、[目录与获取约定](../images/README.md)。同站点HTTPS checksum不是独立发布签名验证，也不证明可复现构建或完整发行SBOM。

Module的完整npm锁SHA-256为`ab45312ad661eb8b38b23e8f63b8fd67983f6a5526626838560a99eba366a4b5`，精确固定运行依赖ipaddr.js2.5.0（MIT），开发依赖TypeScript6.0.3（Apache-2.0）、@types/node24.19.2（MIT）及传递undici-types7.24.6（MIT），各tarball的官方registry URL与完整SRI在锁中保留。[完整锁](../apps/model-egress/package-lock.json)、[分项许可记录](../docs/compliance/DEPENDENCIES.md)。安装/类型检查、运行资格与未来二进制分发核验不是同一个结果。

当前仍没有完整Egress listener、Guest到模型的生产链路、逐请求执行身份/当前授权/租约/预算、OpenBao、专用运行UID、内核出口策略、真实模型/Harness集成或生产证书签发轮换通过声明。对网络自有NAT64、管理网与公开/NAT宿主地址仍需部署显式拒绝配置和独立内核策略。M0-05整体保持`IN_PROGRESS`；本子项不扩大M0已验收任务计数。
