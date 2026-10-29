# Hermes 原生 API 准入探针

`probe_hermes.py` 验证固定源码 `a382af23f4c067d23463b5781958684c2cda82bc` 加受控工具 profile 的**一次真实原生执行**。它不替代完整 M0-03、通用工具能力、取消确认、恢复或生产隔离验收。构建和启动见 [运行包说明](../../deploy/qualification/hermes/README.md)。

探针不会启动容器、配置模型、读取模型 API key 或访问模型 URL。操作者先为每个获准模型准备独立全新 home/workspace，并按 provenance 的准确 image ID 启动容器。API token 仅从当前用户拥有的0600常规文件读取，在 HTTP 头内使用，不进入命令行、报告或原始日志。文件路径不得包含符号链接；工作区及报告目录必须在 `.private/` 下，逐级为当前用户所有且0700。

```bash
df -h .
python3 scripts/qualification/probe_hermes.py \
  --root "$PWD" \
  --container harniqo-hermes-a382-qualification \
  --provenance images/containers/hermes/git-a382af23f4c0/sha256-IMAGE_ID.docker.tar.provenance.json \
  --token-file .private/qualification/hermes/api-token \
  --workspace .private/qualification/hermes/workspace \
  --expected-model APPROVED_MODEL_ID \
  --report .private/qualification/hermes-native/run-001/report.json
```

每次使用新报告路径和没有 `qualification-result.json` 的新 workspace。端口默认10010，可选10000–10100内的端口，排除SSH占用的10023。HTTP地址始终为字面 `127.0.0.1`，禁代理及所有重定向；探针不提供远程明文HTTP或公网监听选项。

实际检查顺序：

1. 核对 provenance 的固定源码、Docker image ID及工具 profile SHA；只读取实际容器的ID、image、running和端口字段，确认仍是该image并仅将8642映射到本机环回指定端口。此检查不验证宿主内核隔离。
2. 对同一真实原生接口发缺失token、错误token请求，必须获得401/403。
3. 创建唯一原生 Session，通过 `POST /v1/runs` 提交一次有幂等键的任务；网络错误不自动重试或再建Run。
4. 随机nonce要求 Agent 先写受控文件，再读受控文件，最终复述工具随机生成的receipt。路径和工具内容由受控代码固定，模型不提供shell或任意路径。
5. 在第一条真实SSE事件后关闭连接；以该整数 `Last-Event-ID` 重连，验证后续序号连续、无重复、run ID一致，并核对两种工具的started/completed事件及最终 `run.completed`。它只证明本次进程保留backlog的补取，不声称跨服务重启持久补取。
6. 查询原生终态、runtime model和用量并先保存观察结果。canonical SessionAuthority 的轮询投影省略 `completed/partial/interrupted`，这三项由最终SSE事件严格证明；轮询若显式返回矛盾值仍拒绝。名称一致只是原生metadata证据，不能证明供应商后端权重。
7. 从原生Session历史核对两个工具调用的ID、参数和对应真实结果，与宿主获准workspace中的固定产物、receipt、SHA及最终回答逐项对应。重新检查容器ID/image未变化。

终态、已报告的非负整数token、模型观察和工具证据分别落盘；工具/历史/产物/最终回答与容器一致性通过后记录 `stage:native_tools_verified`，随后才执行严格模型/用量准入门槛。runtime.model缺失或空值记 `model_evidence.status:not_reported`、`reported_model:null`，失败代码为 `native_effective_model_not_reported`；非空但不匹配则记 `mismatch`及该值hash，失败代码为 `native_effective_model_mismatch`，避免把异常自由文本写入报告。只有实际报告值匹配才记 `matched`。请求model回显和配置期望值都不能填充缺失的actual metadata。任一门槛失败仍 `native_api_case_passed:false`；先前独立验证通过的工具、用量和终态证据保留，不据此宣称整体验收通过。

父进程对HTTP worker实施默认150秒进程截止（可设30–300秒）。超时会终止本地worker并将最近阶段留在报告，**不能证明远端Run、模型请求或计费停止**。无论探针失败、超时或中断，操作者仍须查Run/执行进程并按运行包流程确认停止、保存证据和清理；脚本不会为了清理未经确认的任务而任意删除容器或用户工作区。

单个JSON响应限制2MiB，SSE限制8MiB/连接、64KiB/行、256KiB/帧和8192事件/连接；网络socket inactivity限制15秒，整体截止由父进程执行。HTTP错误不读取body。私有报告仅保留阶段、身份、计数、类型化失败、工具ID和hash，不保存原始prompt、模型URL、token、原始模型消息、HTTP body或SSE文本。报告逐阶段原子落盘，失败保留已知Session/Run ID供后续核对。

返回0只代表报告中 `native_api_case_passed:true` 的受限用例通过，`milestone_complete` 与 `saas_isolation_passed`始终为false。失败返回1、截止返回124、中断130、配置拒绝2。第二模型、原生续聊、服务重启恢复、取消的真实执行确认、人工审批拒绝和生产VM边界始终在单次报告的 `unverified` 中，须由各自独立用例补证；不能只靠本探针关闭M0。

组件测试只使用内存SSE输入和真实临时文件，mock仅用于导入上游工具registry。不会启动HTTP服务、Docker、VM或调用模型，结果不是原生Harness证据：

```bash
df -h .
PYTHONDONTWRITEBYTECODE=1 python3 scripts/qualification/test_hermes_tools.py -v
```

接口形状来自固定 primary 源码：[API资源/消息](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/platforms/api_server.py)、[Run状态与SSE](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/platforms/api_server_runs.py)。实际兼容性仍以运行结果为准，源码存在接口不能替代运行证据。
