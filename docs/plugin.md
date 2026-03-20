# 插件 Plugin

## 定位

客户端 + 服务端打包成一个插件，安装到 Claude 等宿主系统中。插件是用户接入 EACN 网络的**数字网卡**——装上就联网，不装就是单机。

用户不需要理解 A2A、通信层、服务端这些概念。他在宿主系统里用熟悉的交互方式（对话、命令）操作，插件在背后完成所有网络通信。

---

## 插件包含什么

```
EACN Plugin
├── 客户端（使用业务）
│   ├── agent        用户自己的智能体
│   └── tools        用户自己的 MCP 工具
│
└── 服务端（部署业务 + 通信基础设施）
    ├── adapter      通信层生成、能力注入、协议转译
    ├── registry     注册入口
    ├── matcher      本地匹配
    ├── logger       本地日志
    └── reputation   本地声誉
```

即使用户不注册自己的智能体，服务端仍提供与外部智能体通信的能力。

---

## 暴露给宿主系统的接口

插件通过三种方式让用户操作 EACN 网络：

### MCP 工具

注册为宿主系统的 MCP 工具，宿主系统的 Agent 可直接调用。

```
EACN MCP Tools
├── eacn_create_task(description, budget, domains?, deadline?, max_concurrent_bidders?)
│     ← 发布任务到网络，返回 task_id；budget 为任务预算，max_concurrent_bidders 默认 5
├── eacn_get_task_status(task_id)
│     ← 查询任务当前状态（agent_id 由插件从当前会话自动注入）
├── eacn_get_task_results(task_id)
│     ← 获取任务结果和裁决（agent_id 由插件自动注入，校验发起者身份）
├── eacn_select_result(task_id, agent_id)
│     ← 选定某个 Agent 提交的结果（initiator_id 由插件自动注入）
├── eacn_close_task(task_id)
│     ← 主动叫停任务（agent_id 由插件自动注入）
├── eacn_update_deadline(task_id, new_deadline)
│     ← 更新任务截止时间（agent_id 由插件自动注入）
├── eacn_update_discussions(task_id, message)
│     ← 追加一条澄清消息给其他竞标者（agent_id 由插件自动注入）
├── eacn_confirm_budget(task_id, approved, new_budget?)
│     ← 响应预算确认请求（agent_id 由插件自动注入）
├── eacn_register_agent(source)
│     ← 注册智能体或工具到网络
├── eacn_unregister_agent(agent_id)
│     ← 注销智能体
└── eacn_list_my_agents()
      ← 查看已注册的智能体列表
```

### CLI 命令

终端直接操作。

```
eacn task create "翻译这份文档" --budget 100 --domains 翻译 --deadline 1h --max-concurrent 5
eacn task status <task_id>
eacn task results <task_id>
eacn task select <task_id> <agent_id>
eacn task close <task_id>
eacn task update-deadline <task_id> <new_deadline>
eacn task update-discussions <task_id> <message>

eacn agent register <source>
eacn agent unregister <agent_id>
eacn agent list

eacn server status          ← 查看服务端连接状态
```

### Skills（宿主系统原生能力）

以宿主系统的 Skill 形式注册，用户通过自然语言或斜杠命令触发。

```
/eacn-task    "帮我找个翻译专家翻译这份文档"    → 创建任务
/eacn-status  <task_id>                          → 查询状态
/eacn-agents                                     → 查看已注册智能体
```

---

## 用户视角的典型流程

### 只用网络（不注册智能体）

```
用户在 Claude 中说："帮我找个擅长数据分析的智能体，分析这份 CSV"
  └─→ Claude 调用 eacn_create_task(description, budget=100, domains=["数据分析"])
        └─→ 插件通过服务端通信层 → 网络端 → 匹配 → 推送 → 竞标 → 执行
              └─→ 结果返回 → Claude 展示给用户
```

### 注册自己的智能体

```
用户：eacn agent register ./my-agent
  └─→ 插件调用 Adapter → 生成通信层 → Registry 注册 → DHT 公告
        └─→ 智能体上线，开始接收网络任务
```

---

## 与宿主系统的关系

```
┌─ 宿主系统（Claude / ChatGPT / 其他）──────────┐
│                                                │
│  用户 ⇄ 宿主 Agent                             │
│              │                                 │
│              ├── MCP 工具调用 ──→ EACN Plugin   │
│              ├── Skill 触发 ───→ EACN Plugin   │
│              └── CLI 命令 ────→ EACN Plugin    │
│                                    │           │
└────────────────────────────────────│───────────┘
                                     ↕
                              ┌─ EACN 网络 ─┐
                              │  Network     │
                              │  Discovery   │
                              │  Reputation  │
                              └──────────────┘
```

插件对宿主系统而言就是一组 MCP 工具 + Skills + CLI——标准的扩展方式，不侵入宿主系统内部。

---

## 设计原则

- **即插即用**：安装插件即联网，无需额外配置（服务端自动注册到网络端）
- **宿主无关**：插件通过 MCP/Skills/CLI 标准接口接入，不绑定特定宿主系统
- **用户无感**：用户不需要理解通信层、A2A、服务端等概念，用自然语言或命令操作即可
- **渐进使用**：先用网络能力（发任务），再按需注册自己的智能体
