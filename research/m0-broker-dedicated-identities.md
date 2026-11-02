# M0-05 专用身份、DAC 与实际服务上下文

日期：2026-11-02。状态：**专用账号、实际跨UID服务上下文与限定文件/IPC策略已开发完成并验证（VERIFIED）；M0-05仍IN_PROGRESS**。生产libvirt授权、专用UID真实VM和完整Supervisor集成尚未验收。

这一阶段补齐 [上一轮服务入口](m0-broker-service-identities.md)尚未证明的不同稳定 UID。
沿用生产 Resource Broker 与 IPC，不把开发账号下的进程测试算作专用身份验收。

账号创建独立于 libvirt 委派。NSS 中同名、数字 ID、成员及主组关系均需检查；新账号锁定密码、
使用 nologin 且不创建 home。创建命令可能部分生效，超时或查询不到账号不能证明未修改
shadow/gshadow 等数据库，因此效果未知时保留 intent 并拒绝自动重试。
[Ubuntu useradd 手册](https://manpages.ubuntu.com/manpages/noble/man8/useradd.8.html)

实际进程身份按 real/effective/saved/fs UID/GID、全部补充组、capabilities 与 NNP 核对。
补充组会影响文件访问，不能只看 unit 的 User/Group 文本。
[Linux credentials](https://man7.org/linux/man-pages/man7/credentials.7.html)

systemd 的只读文件系统不阻止连接 UNIX socket。身份资格同时使用显式不可达管理路径、
PrivateNetwork 和 AF_UNIX 限制，并直接运行连接负例；文件路径型 IPC 与 abstract socket
分别按网络 namespace 语义处理。DAC 与 mount 屏蔽分别实测，不把 EROFS 当作私有文件
读取拒绝或 IPC 目录本身的 DAC 证明。
[systemd 255](https://www.freedesktop.org/software/systemd/man/255/systemd.exec.html)、
[Linux UNIX socket](https://man7.org/linux/man-pages/man7/unix.7.html)

当前共享宿主已有业务 VM。细粒度 access driver 的启用与 daemon 重启是另一项管理员变更，
本阶段不修改这些设置；不能先给管理连接许可再猜测 per-API ACL 是否工作。
[libvirt access drivers](https://libvirt.org/acl.html#access-control-drivers)

交付与操作入口见 [身份部署说明](../deploy/runner/IDENTITIES.md)。资格配置与模板是 canary，
没有有效执行授权、VM 启动或生产 Supervisor 集成声明；完整 M0-05 仍需单独完成剩余条件。

## 实现、失败与恢复

[身份部署入口](../scripts/deploy/provision_broker_identities.py)与[实际资格入口](../scripts/qualification/qualify_broker_identities.py)均为正式可运行代码，使用Python标准库及目标系统工具。root调用从管理员控制的不可写发行目录使用`python3 -I -B`；运行中的Broker/listener/watch及peer均是专用普通UID。NSS创建有独立锁、原始计划摘要及持久步骤receipt，普通apply/check不接管未知身份。

实现审查确认并修正了未知native效果被NSS absence误判为可重试、首次目录同步失败后重入跳过父目录屏障，以及四操作中的renew缺口。新增反例在旧源码分别实际RED：真实子进程timeout/输出超限留下NSS不可见marker，真实文件系统目录重入，以及只返回supplementary groups的NSS夹具。旧源码和这些原始RED各自保留。日志采集失败仍须停止已核对的自有unit，总体保持FAILED；InvocationID变更时不停止外来执行。

[最终Linux组件](../docs/operations/broker-identities-components-2026-11-02.json)64项全部通过：37身份部署/恢复、27操作者，0失败/错误/跳过。组件NSS为受控夹具，文件系统及超时/输出子进程真实执行；不把这些数量当作管理员宿主资格。早期43/52/53通过各保留当时源码绑定，最终64另有唯一日志。一次私有SSH脚本路径被后续生成替换，造成组件实际通过后的外层shell收尾错误；修正为每次执行独立不可变脚本副本后重新确认，不把传输错误改写成成功。

第一次真实管理员apply已创建三组与Broker账号，后置检查将GNU `getent initgroups`只含补充组的实际输出误判为异常；工具按设计留下unconfirmed，没有重跑创建。只读核验分别确认passwd主组、OS实际组集、shadow锁定、nologin和无home。修复把已严格核对的passwd主GID加入initgroups结果，IPC仍必须存在且不允许额外组。

随后管理员通过显式`confirm-created`绑定原receipt SHA、计划和指定步骤，完整后置核对后先保留原字节备份与审计，再仅确认Broker步骤。确认前后四份本地账号数据库的字节摘要相同，未执行NSS写入；普通apply仅继续Supervisor新步骤，最终五步complete及完整check通过。原始FAILED、unconfirmed原件、审计、中间receipt摘要和最终成功分别保存，未用最终成功覆盖历史。[getent接口说明](https://man7.org/linux/man-pages/man1/getent.1.html)

## 实际跨UID服务验收

[独立原生核验](../docs/operations/broker-identities-native-2026-11-02.json)证明2个稳定UID/3组、10个唯一unit InvocationID、6份成功oneshot退出、9份实际进程身份快照和5个probe的172项显式检查全部通过；全部自有unit停止，错误和cleanup失败为空。实际UID/GID四元组、组集、零capabilities、NNP/seccomp、同host user namespace及不同mount/net namespace分别核对，不从配置文本推定已生效。

正式CLI初始化、独立watch和listener启动后，peer的observe/revoke到达真实空状态核心，坏签名ensure/renew被原核心拒绝；没有发出有效执行授权。错误资源、UID/主GID拒绝分别核对。单独DAC探针保持私有路径可见和IPC mount可写，以实际权限拒绝读取key/state/images及创建或替换IPC目录项；代码/配置写保护另核，受限探针再核对管理socket与别名、AF_INET及namespace限制。listener停止和新unit重开同一认证空journal时，原watch持续存活；这不替代运行中VM的恢复。

独立只读collector不导入producer，另验六份root发行源码、NSS/锁定状态/receipt/恢复审计、配置与路径、HMAC空journal/anchor/锁inode、原始probe、实际unit/进程快照及停止状态；三份既有domain XML、network/filter摘要与执行前基线一致。raw/seed位于项目images下，raw为8GiB稀疏且不可启动的canary，实际未启动VM；未新增镜像/工具链归档，manifest仍12项。新canary及私有状态保留用于审计，旧96GiB诊断层不在清理范围。

本阶段实际使用了授权管理员创建身份和自有临时unit；未改共享libvirt、polkit、sudoers、SSH或既有VM。仅证明所测服务上下文及列出的权限策略，UID在namespace外的全局授权、有效libvirt逐API权限、专用UID真实VM和生产Supervisor常驻集成继续PENDING；M0仍4/8。实际GitHub CI按下节分别登记。

## 实际 GitHub CI

源码`f7775b6d181d4aca7bfeeead8be816f78bc67adf`的[完整资格CI](https://github.com/StateKnot/Harniqo/actions/runs/41375990700)通过636项Python回归，0失败/错误/跳过，两组Node检查成功；[同源三平台契约CI](https://github.com/StateKnot/Harniqo/actions/runs/41375990757)各通过60定义、18路径、107样例和15生命周期。共4份实际artifact已下载，run/job/步骤、报告、源码、锁与SHA核对完成，见[CI索引](../docs/operations/broker-identities-ci-2026-11-02.json)。CI没有创建实际服务账号或启动本轮特权服务，不能从组件成功继承E70的宿主身份结果，也不是三端桌面构建。

最终证据增补E71，并将本轮生成的images/idq-*/canary目录纳入Git忽略规则；实际canary、key/state和原件均保留，不移动或重写已验证catalog。运行代码、测试、工作流与契约保持该受测提交的字节，不因文档重复执行VM或管理员部署。M0仍4/8、M0-05仍IN_PROGRESS。
