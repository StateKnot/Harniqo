# Octop Harness → OpenCode stdio ACP 资格组合

最后已通过的范围是 `octop-harness-acp-delegated-patched-outer512`：固定Octop Harness 1.0.5 加一个来源固定MIT文本拼接补丁，委派固定OpenCode 1.19.5，外层输出512/内层1024。原版仍FAILED；完整Octop服务、SDK非委派能力及生产隔离分别验收。[研究记录](../../../research/octop-acp-qualification.md)包含三个真实case、失败保留、源码/镜像/报告hash与限制。归档、原始日志及配置均不进Git。

1. 将固定Harness官方uv.lock放入`images/sources/octop/acp/harness-upstream-uv.lock`。`prepare_octop_acp.py --root <服务器根>`核已有image ID、官方extras完整hash与release metadata，禁网构建原版组合；归档在images。
2. `prepare_octop_acp_patched.py --root <根>`以原组合固定image建立独立修补版。`acp-final-text.patch`仅保留工具摘要并追加未输出final assistant，不改Agent loop/模型/权限/ACP wire。应用器核原源码/patch/结果三重SHA，构建重载验证、带MIT LICENSE。固定构建输入`acp-patch-profile.json`不承载运行资格；原版、patched128及patched512 runtime profiles分别登记。
3. `run_octop_acp.py --root <根> --variant patched512 --image <profile中的完整image ID>`先运行无模型预检：禁网、无真实凭据，真实initialize/new/close、断线后require_existing无重建，加载source SHA及512/1024预算快照一致，observer发送日志为空。
4. 只有明确任务额度且本次预检通过时，追加`--mode model --bootstrap-report <成功operator-report.json>`运行。outer/inner各最多4 SDK发送，输出512/1024，整体硬上限180秒。`patched512-model-reservation.json`独占创建后才dispatch；原版`model-reservation.json`和`patched-model-reservation.json`独立保留。不得删除reservation绕过额度或重放未知提交。本轮三次真实任务已完成，不再追加。

原版/首个修补版outer128各FAILED：原版native工具丢失32字receipt；补丁修复后ToolMessage正确，但outer128中的114 token用于reasoning，最终length截断为11字。最后仅提高outer到512，原exact receipt/stop/inner idle判据不变；实际outer2/inner2 HTTP200后PASS。三次分别保存原结果，没有将补证或后续成功覆盖先前失败。

`run_octop_acp_replay.py`在禁网/无凭据容器中回放真实保存事件；`--variant patched`会验证实际安装的patched模块。内部服务seam只是记录fixture，无ACP peer/子进程/模型，不能替代真实任务。16链路断言与8格式fixture通过。`summarize_octop_acp.py`只读追加脱敏链路/token分析，保留原报告hash。24项本机/服务器纯组件见`test_octop_acp.py`；复用OpenCode observer的21组件是独立既有证据。

配置/密钥只从服务器既有私有文件读取并readonly挂载；路径链拒symlink，私有目录当前owner且无组/其他权限，已有错误权限直接拒绝。固定Docker本地daemon，只按自己的label/image/immutable ID清理，保留全日志并确认PID0。所有native调用/事件、两层history、额外usage、发送审计和源码快照保存在`.private/qualification/octop-acp/`。

OpenCode官方ACP CLI内部启动loopback HTTP供自身使用。主委派始终stdio ACP；任务结束后native history/status各一次HTTP只读查询用于补证。外thread与内ses_*分别保存；外protocol clientInfo版本不能替代SDK版本，内层额外模型请求不能漏算。共享observer文件只读复用`deploy/qualification/opencode/transport_{plugin,audit}.mjs`，SHA冻结在runtime profile。

最终资格不覆盖完整服务ACP、租户隔离、委派写审批、执行中取消、durable事件重放或重启续聊。内层provider响应模型metadata仍unknown。close时原生owner teardown的child返回-9如实保留，Docker PID0/remove已确认；不把kill等同优雅退出或供应商计费停止。
