# Swarm 路线图

## 目标

把当前这套“可观测、可控、带工具链的多 Role 串行系统”，逐步演进成一个轻量 swarm：

- 后端仍然掌握权限、工具和真实执行
- LLM 负责路由、推理、专门分析和最终组织
- 支持多个同类 role 实例
- 支持并行分支
- 支持结果聚合与回退
- UI 始终能看清每个 role 在做什么

这不是要做一个完全自治的黑盒 Agent 系统，而是要做一个可实战、可调试、可维护的多 Role runtime。

## 术语

- `role`: 系统里的所有执行单元总称
- `agent`: 由 LLM 驱动的 role
- `processor`: 不依赖 LLM 的 role
- `Router`: `agent + processor`
- `Coordinator`: `processor`
- `Tool`: 后端执行能力，不属于 role

## 当前状态

当前实现已经不是旧文档描述的“阶段 1 到阶段 2 之前”。

当前主链路已经具备：

- `Router`
- `Coordinator`
- `Planner`
- `Worker`
- `Researcher / FileReader / Summarizer / Fixer`
- `Conflict Detector`
- `Reviewer`
- `Revision`
- `Structurer`

当前 UI 也已经具备：

- 调试流
- Role 视图
- role / agent / processor 术语
- 当前 active role 的状态展示

因此，当前真实状态应视为：

- 阶段 1：已完成
- 阶段 2：已完成
- 阶段 3：MVP 已完成
- 阶段 3.5：已完成
- 阶段 4：试点进行中
- 下一步重点：扩大多实例覆盖面，并为阶段 5/6 准备分支与聚合

## 当前架构

当前架构仍然是一个由后端编排的多 Role 串行流水线：

1. `Router` 决定任务类型和最小可行链路
2. `Coordinator` 持有运行时状态并编排执行
3. `Planner` 在需要时提炼目标与约束
4. `Specialist` 在需要时生成专门简报
5. `Worker` 执行主任务与工具循环
6. `Conflict Detector / Reviewer / Revision / Structurer` 按路由配置参与后处理

这个系统已经具备较强的可观测性和一定的动态分流能力，但本质上仍然是：

- 主执行仍以单实例 `Worker` 为中心
- 顺序仍主要由 Python 编排代码决定
- 还没有真正的一等公民“子任务实例”和“并行分支”
- 聚合逻辑仍然分散在主流程里，而不是独立 runtime 层

## 为什么现在还不算完整 Swarm

- 还没有通用的 `RoleInstance / TaskNode` 模型
- 还没有父子任务关系
- 还没有同类 role 的多实例调度
- 还没有正式的并行执行图
- 还没有独立的结果聚合层
- 主流程仍然高度集中在单个 orchestrator 实现中

## 路线原则

### 1. 不新开 repository

继续在当前仓库演进。

原因：

- 当前仓库已经沉淀了大量真实问题修复
- UI、调试链、工具链和后端编排已经深度耦合
- 新开仓库会切断历史、重复踩坑、引入双线漂移

### 2. 不做“大爆炸式全量类化”

不在进入阶段 4 前，把所有 role 一次性改成 `XxxAgent` 类。

原因：

- 阶段 4/5/6 的核心难点不在目录结构
- 真正的复杂度在运行时状态、子任务调度、并行与聚合
- 如果只重命名和拆文件，后续大问题不会变少

### 3. 先做 runtime 抽象，再逐步类化

先把“系统如何运行多个 role 实例”抽出来，再决定哪些 role 适合实例化成类。

这是进入阶段 4/5/6 前最重要的技术准备。

## 阶段 1：打稳基础流水线

状态：已完成

目标：

- 固定主链路稳定
- 工具调用轨迹清晰
- 调试流可解释
- 失败时可回退

完成标志：

- 团队能够解释调试面板上的每一步
- `Worker` 工具调用可以归因
- `Reviewer / Revision` 行为基本可追踪

## 阶段 2：加入路由 / 分诊

状态：已完成

已实现能力：

- 规则 Router
- 可选 LLM Router
- 基于任务类型切换链路
- 控制 `Planner / Reviewer / Revision / Specialist / Structurer` 是否启用

当前结论：

- `Router` 已经是系统正式入口之一
- `Coordinator` 已经是运行时状态机，而不是未来概念

## 阶段 3：专门角色

状态：MVP 已完成

当前已具备的专门角色：

- `Researcher`
- `FileReader`
- `Summarizer`
- `Fixer`

当前形态：

- 不是运行时动态发明新的 Python 类
- 而是预定义好的角色模板和 prompt
- 系统按请求动态选择是否启用

阶段 3 的剩余工作：

- 明确 specialist 的输入输出协议
- 收敛各 specialist 的职责边界
- 避免 specialist 与 `Worker` 功能漂移重叠

## 阶段 3.5：Runtime 抽象

状态：已完成（进入阶段 4 的前置条件已达成）

这是进入阶段 4/5/6 前的基础工程，不是可选优化。

当前进展（最小版已落地）：

- `RoleSpec / RoleContext / RoleResult` 已在主流程使用
- 新增 `RoleInstance / TaskNode / RunState` 数据模型
- 主链路已开始记录 role 实例级运行状态与关键事件（先保持行为不变）
- 已新增 `role_registry`，把当前可独立执行的 agent 统一注册
- 已新增 `runtime_controller`，把串行链路中的 Planner / Specialist / Conflict Detector / Reviewer / Revision / Structurer 包进统一 runtime 执行接口
- `Router / Coordinator / Worker` 已纳入 managed runtime shell，主行为保持原样但运行态已统一登记
- `Role-Agent Lab` 已能暴露 registry 快照、stage 4 readiness 和最近一轮 run state
- 当前 stage 4 readiness 已达到：
  - `registered_roles = 12`
  - `controller_backed_role_count = 12`
  - `controller_gaps = []`
  - `full_controller_coverage = true`

### 目标

把当前大流程里隐含的运行时概念，抽成明确的数据模型和调度接口。

### 需要抽出的核心对象

- `RoleSpec`
  - 描述一个 role 的静态能力
  - 包括 kind、工具权限、输入要求、输出类型

- `RoleContext`
  - 描述某个 role 本轮实际拿到的输入
  - 包括 user message、history summary、attachments、tool events、planner brief 等

- `RoleResult`
  - 描述某个 role 一次执行后的标准产物
  - 包括 summary、bullets、raw output、usage、next actions、handoff

- `RoleInstance`
  - 某个 role 的一次具体运行实例
  - 例如 `worker#1`、`researcher#2`

- `TaskNode`
  - 可调度节点
  - 用于承载父子关系、状态、结果、重试与超时

- `RunState` 或 `ExecutionGraph`
  - 承载整轮请求的执行图
  - 支持串行、并行、回流和聚合

### 第一阶段边界

只做 runtime 抽象，不大改现有行为。

具体做法：

- 保留现有 `Router / Worker / Reviewer / Revision` 行为
- 先把它们包进统一的 runtime 接口
- 让当前单实例串行流程先在新抽象上跑通
- `Worker` 主循环仍保持原样，但 runtime 记录、实例管理和 readiness 判定已纳入同一套控制面

### 这一阶段不做的事

- 不追求一次性改成完整 OO 结构
- 不强行把 `Coordinator + Worker` 主循环拆碎
- 不为了目录好看而牺牲可回归性

## 阶段 4：多实例执行

状态：试点进行中

目标：

允许同一种 role 同时启动多个实例。

示例：

- `worker#1` 读取附件 A
- `worker#2` 读取附件 B
- `researcher#1` 搜索来源 A
- `researcher#2` 搜索来源 B

进入条件：

- 阶段 3.5 的 `RoleInstance / TaskNode / RunState` 已落地

关键能力：

- 子任务 ID
- 父子任务关系
- 每个实例独立上下文
- 局部失败与局部重试
- UI 中可见实例级状态

当前试点：

- `runtime_controller.execute_batch()` 已能调度同类 role 的多实例执行
- `_debug_role_lab_multi_instance_batch()` 已验证 `researcher#1 / researcher#2` 双实例批处理
- 主链路已加入真实试点：当 `Role-Agent Lab` 处理多附件且命中 `file_reader` 场景时，会为多个附件并行生成子简报，再合并回主流程
- `/api/health` 与 `/api/role-lab/runtime` 已能看到 controller 覆盖率、stage 4 readiness、最近一轮节点 / 实例快照

阶段 4 剩余工作：

- 把 `Worker` 的子任务拆分也纳入真正的多实例分发
- 为并行分支补更清晰的 branch / join UI
- 明确局部失败后的重试与回退策略

## 阶段 5：并行分支

状态：未开始

目标：

允许独立子任务并行执行。

示例：

- 同时读取多个附件
- 同时读取附件和搜索网页
- 同时做代码分析和测试分析

要求：

- 并发数可控
- 成本和时延可控
- 合并规则明确
- UI 能展示 branch 和 join

## 阶段 6：聚合层

状态：未开始

目标：

加入正式的聚合逻辑或聚合 role。

职责：

- 收集多个子实例结果
- 解决结论冲突
- 统一组织最终答案
- 决定是否再触发验证或修订

说明：

- `Coordinator` 仍然是 runtime 权限与调度核心
- 聚合层不应绕开后端权限控制

## 推荐实施顺序

1. 先完成路线图与术语更新
2. 落地阶段 3.5 的 runtime 抽象
3. 让当前串行单实例链路迁移到新 runtime 壳上
4. 只选一个场景试点阶段 4
5. 扩大阶段 4 的多实例覆盖面
6. 再逐步扩展到阶段 5 的并行分支
7. 最后补阶段 6 的聚合层

## 当前非目标

- 开新仓库重写一套系统
- 完全自治、开放式 swarm
- 无限制动态生成 role
- 绕过后端权限与工具护栏
- 在没有 runtime 模型前直接上大规模并行
