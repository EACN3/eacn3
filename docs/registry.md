# 注册中心 Registry

## 职责

Registry 是**服务端与网络端之间的桥梁**，只管三件事：

1. **校验**：接收 Adapter 产出的 Agent Card，校验合法性
2. **持久化**：通过校验后存储
3. **公告**：向网络端（DHT）公告域标签，纳入发现体系

Registry 不调用 Adapter，不参与能力提取和通信层生成。Adapter 是入口，Registry 是出口。

```
接入者 → Adapter（转化+通信层生成） → Registry（校验+持久化+DHT 公告）
```

---

## 接口

```
Registry
├── register(agent_card) → Agent
│     ← 接收 Adapter 产出的 Agent Card，校验 + 持久化 + DHT 公告
├── unregister(agent_id) → void
├── discover(domains?) → Agent[]
│     ← 按域过滤，不传则返回全部
└── update(agent_card) → Agent
      ← 能力变更时，Adapter 重新提取后提交更新
```

---

## 注册校验

**Agent Card 必须满足：**
- `domains` 非空
- `skills` 至少一条
- `url` 可达
校验不通过则拒绝注册，不持久化。

---

## 设计原则

- **只做校验和存储**：不参与能力提取、通信层生成，这些是 Adapter 的事
- **校验在入口**：注册时一次性校验，已持久化的数据保证合法
- **Adapter 是入口，Registry 是出口**：单向流转，不互相调用
