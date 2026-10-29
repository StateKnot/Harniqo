---
status: accepted
---

# Runner 管理身份与 VM 内执行身份分离

平台托管执行使用 KVM/libvirt，交互工作区与批量 Trial 按授权范围持有独立 VM 写入层。宿主 Runner Supervisor/受限 Resource Broker 管资源；VM 内 Execution Agent 加载 Harness Adapter。Deployment 管版本和执行目标，RuntimeInstance 管实际资源与工作区归属。

共享宿主容器中的目录/用户隔离不足以承担本项目不互信 SaaS 执行边界；直接把原生 CLI 放在具有 libvirt 或平台管理身份的 Runner 中，也会扩大越界影响。VM 带来启动/资源成本，因此同一获准交互工作区可保留状态并保持单写者，而不同 Trial/候选不能共享写入层。

取消/接管需要真实停止证据；租约过期、fencing 或 guest ACK 均不充分。不能证明停止时保持 reconciling，不启动第二写者。KVM、网络、镜像权限、恢复和宿主资源限制仍需 M0/M2 真实验收，不能静默换后端后沿用支持声明。
