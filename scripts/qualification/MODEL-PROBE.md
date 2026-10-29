# 模型工具协议前置检查

`probe_model.py` 用标准库执行两个 OpenAI-compatible 非流式请求，检查强制工具调用、JSON参数、工具结果回传及模型是否使用真实随机回执。它属于 M0-03 的前置证据，**不等于 Harness 已准入或平台功能完成**。

## 配置与执行

配置文件可以是 endpoints 列表，也可以是下列对象。示例值仅为占位符，真实地址、凭据及结果仅保存在私有目录。

```json
{
  "endpoints": [
    {
      "id": "primary",
      "base_url": "https://api.example.invalid/v1",
      "model": "model-id",
      "api_key_file": "secrets/primary-api-key"
    }
  ]
}
```

`api_key_file` 的相对路径以**配置文件所在目录**为基准。例如配置在 `.private/model-endpoints.json`，上述密钥路径就是 `.private/secrets/primary-api-key`，不是工作区根目录的 `secrets/primary-api-key`。绝对文件路径也必须满足同样的安全检查。

也可把 `api_key_file` 替换为 `api_key_env`，其值是受控环境变量名；两者必须且只能配置一种，不支持把明文 `api_key` 放进配置字段。不要通过命令行参数传真实密钥。

配置和密钥文件须为 POSIX 常规文件，所有者为执行用户或 root，权限为0600/0400等无组/其他访问权限，且路径中不能有符号链接。报告必须位于 `.private/` 下，私有目录及子目录为当前执行用户所有且无组/其他权限，通常使用0700。已有报告和产物目录不会覆盖。环境管理者应核对自己创建目录的权限；探针不会为了通过而静默放宽检查。

```bash
python3 scripts/qualification/probe_model.py \
  --config .private/model-endpoints.json \
  --endpoint-id primary \
  --report .private/qualification/primary/run-001/report.json \
  --timeout 30 \
  --max-tokens 192
```

每次使用新的报告路径。`--max-tokens` 可取32–512，最多两个请求，不自动重试。`--timeout` 可取1–60秒；含义见下节，不是费用硬封顶。

## 时限与费用边界

`--timeout` 是 urllib 使用的 **socket inactivity timeout**，报告字段为 `socket_timeout_seconds`。它约束连接/读写无进展的等待，不是整个 HTTP 响应、两轮工具流程或探针进程的总时长上限；持续滴流等情况不能靠它取得整体硬截止。

本轮真实资格检查的外层执行器另加 **135秒进程硬截止**。这是外层运行策略，不是探针内置能力：直接运行上面的命令不会自动获得这项整体时限。外层执行器必须记录启动、截止、退出/强制终止及报告是否完整；超时或没有完整报告不能算通过。

终止客户端进程也不能证明模型供应商已停止在途请求或不会计费。探针只记录供应商报告的 token usage，不推算完整账单、不宣称成本硬封顶；`cost_cap_enforced` 固定为 false。真实调用只使用获准端点、模型与预算。

## 探针实际做了什么

1. 第一轮强制一个 `record_probe` 调用，要求正好一组 JSON 参数，nonce必须匹配，未知字段、重复JSON成员、任意其他工具或模型提供路径都拒绝。
2. 只有第一轮验证后才生成随机receipt。程序仅在报告旁新建的 `<report-stem>.artifacts/` 中写固定 `probe-result.json`，不执行shell或模型给出的文件操作。
3. 第二轮回传真实工具结果，并要求模型返回匹配nonce和receipt的JSON。猜测或臆造回执不能通过。

所有重定向均拒绝，防止授权头被带到另一地址；默认只接受HTTPS且校验证书。模型响应的名称必须与配置一致，不能为了通过而悄悄换模型；名称匹配也只证明响应metadata一致，不能独立证明供应商后台模型权重或路由。

报告保存请求ID、用量、模型metadata、阶段、工具参数验证结果和产物hash的脱敏摘要，不保存URL、密钥、原始请求/响应或上游错误body。上游错误只记录安全错误代码及HTTP状态。私有报告不应未经复核直接作为公共日志或截图发布。

## 真实与模拟证据

| 证据 | 能证明什么 | 不能证明什么 |
| --- | --- | --- |
| 获准HTTPS端点的真实运行 | 本次端点/模型响应下，受控非流式两轮工具协议和回执通过或失败 | Harness原生会话、真实工具权限、取消、断线恢复、SSE、VM隔离、批量评测均未被此探针验证 |
| `test_probe_model.py` 假HTTP响应 | 探针自身解析、安全拒绝、报告与文件行为；报告标记 `transport_fixture_only` | 没有真实模型推理，不得用测试通过宣称模型或Harness通过 |
| `--allow-http-loopback` | 只允许字面环回IP上的HTTP传输测试 | 不授权任意明文远程HTTP，也不把环回成功升级为真实模型证据 |

返回0只表示本次受限协议检查通过；返回1表示协议/上游/网络失败，返回2表示配置/权限/报告写入等失败，130表示操作者中断。最终判定还需检查报告scope、transport、evidence_kind及完整性；不能仅凭一个退出码关闭M0-03。

标准库安全测试命令如下，假服务只选择 `127.0.0.1:10080–10100` 的空闲监听端口，并在结束时关闭，不使用port0或占用既有SSH：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/qualification/test_probe_model.py -q
```

测试不需要真实密钥。编译/测试前检查实际可用空间；运行证据与报告保存到被Git忽略的私有目录。
