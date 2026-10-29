# Hermes 真实验收的启动与回收

`run_hermes_qualification.py` 为一次真实原生用例准备独立 home、workspace、API token 和模型配置，启动固定 image，执行相应探针，再核对容器停止与移除。它是可信验收操作者，不能作为生产 Runner、租约或 SaaS 隔离的实现。

在指定 Linux 服务器的仓库根目录运行；先读取实际可用空间，至少保留10GiB。模型配置沿用 [Provider 探针](MODEL-PROBE.md) 的 `schema_version:1` 格式，使用已有配置中的明确 endpoint ID。脚本不会发现后自动选用其他模型，也不会修改原配置。相对 `api_key_file` 基于配置文件目录解析；密钥通过原文件只读挂载，文件内容不进入 Docker ENV、命令参数或报告。

```bash
df -h .
python3 scripts/qualification/run_hermes_qualification.py \
  --root "$PWD" \
  --config "$PWD/.private/model-endpoints.json" \
  --endpoint-id APPROVED_ENDPOINT_ID \
  --provenance "$PWD/images/containers/hermes/git-a382af23f4c0/sha256-IMAGE_ID.docker.tar.provenance.json" \
  --case "$PWD/.private/qualification/hermes-cases/UNIQUE_CASE" \
  --probe tools
```

`--probe tools` 使用 [canonical 原生工具探针](HERMES-PROBE.md)；`model-lock` 和 `lifecycle` 分别调用原生模型锁与审批/取消探针。每种模式单独使用全新 case。缺少对应探针时拒绝执行，不降级或假定通过。配置、密钥及 `.private/` 下的祖先目录必须私有且没有符号链接；已有 case 永不覆盖。

运行器固定连接本机 `/var/run/docker.sock`。API 仅映射 `127.0.0.1:10010`，可通过 `--port` 选择10000–10100范围内其他空闲端口，排除10023；同端口的协作进程用文件锁串行执行。端口检查与 Docker 实际发布仍分别核对，启动竞争导致失败时进入回收，不接管他人的监听。

容器以操作者 UID/GID、只读根文件系统、无 capabilities、`no-new-privileges`、128 PID、4GiB内存、2CPU 和256MiB临时目录运行；仅独立 home/workspace 可持久写，三个私有配置/凭据文件只读挂载。日志使用本机驱动并限制到2×10MiB；只对已确认归属的容器，在停止前后分别保存最多4MiB的0600私有日志，记录 hash 和完整性，不打印内容。超限或归档失败不能当作完整留证。此容器仍有出网能力，只适用于可信合成任务，不代表生产网络隔离或费用硬封顶。

每次启动具有独立的 attempt label。回收前必须同时匹配 image、label 及已观察的 container ID，避免误停同名替代容器。先停止，再实际观察 `running:false`、宿主 PID=0，之后才移除。容器消失、Docker 查询失败、停止失败和端口未释放均不能当作停止确认；已观察执行无法确认停止时，operator 结果失败并保留诊断字段。Docker 不可达或外部管理者改动资源时，需要依据报告继续核对，不能盲目重启第二个写者。

`operator.json` 逐阶段原子落盘，`probe.json` 保留探针本身的独立结论。父进程给探针360秒截止，处理 SIGTERM/SIGHUP/键盘中断并进入回收；本地进程组终止后仍核对容器。Linux HTTP worker 设置内核 parent-death signal 并检查父进程身份，避免探针父进程退出后留下脱离会话的 worker。探针成功与宿主回收分别计证，停止容器不算原生运行中取消通过。SIGKILL、断电或 Docker 控制面故障仍需用记录的 label/container ID 对账；正式 Runner 的外部监督与持久租约由 M2 实现。

本工具不自动删除私有状态、证据、镜像或密钥。测试结束后 case 中的 home/workspace 及报告保留，供历史与恢复核对；未来重试必须新建 case。若凭据被撤销，未完成的真实调用会失败并留下相应阶段，不能用 mock 解除真实准入门槛。

无 Docker/网络/模型的错误路径测试：

```bash
df -h .
PYTHONDONTWRITEBYTECODE=1 python3 scripts/qualification/test_run_hermes_qualification.py -v
```

这些测试验证资源归属、真实停止字段、凭据路径与挂载构造的逻辑，不代替原生容器或模型执行。
