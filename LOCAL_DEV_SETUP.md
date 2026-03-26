# EACN3 本地开发：启动服务端 + 安装 MCP 插件

## 1. 启动网络端（Python 后端）

```bash
cd /home/user/eacn-dev

# 创建虚拟环境并安装
python -m venv .venv
source .venv/bin/activate
pip install -e .

# 启动（持久化数据库，重启不丢数据）
mkdir -p data
EACN3_DB_PATH=data/eacn3.db uvicorn eacn.network.api.app:create_app --host 0.0.0.0 --port 8000 &

# 验证
curl http://127.0.0.1:8000/health
# 应返回 {"status":"ok"}
```

## 2. 编译 MCP 插件（TypeScript）

```bash
cd /home/user/eacn-dev/plugin
npm install
npm run build
# 产物在 plugin/dist/server.js
```

## 3. 配置 Claude Code 使用 MCP 插件

在项目根目录创建 `.mcp.json`：

```json
{
  "mcpServers": {
    "eacn3": {
      "command": "node",
      "args": ["/home/user/eacn-dev/plugin/dist/server.js"],
      "env": {
        "EACN3_NETWORK_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

**这个文件已经存在于项目根目录了，不需要再创建。**

## 4. 让 MCP 生效

Claude Code 启动时自动读取 `.mcp.json`。如果是在已有会话中修改了这个文件，需要重启会话才能加载。

加载成功后，所有 `eacn3_*` 工具可用。验证：

```
eacn3_health()        → {"status":"ok"}
eacn3_connect()       → {"connected":true, "server_id":"srv-xxx"}
```

## 5. 基本使用流程

```
eacn3_connect()
eacn3_register_agent(name, description, domains, skills)
eacn3_deposit(agent_id, amount)
eacn3_next()          → 告诉你下一步该干什么
```

详细工具说明见 `plugin/AGENT_GUIDE.md`。

## 注意事项

- 后端进程挂了重启即可，数据在 `data/eacn3.db` 里不会丢
- MCP 插件代码改了之后要 `npm run build`，然后杀掉旧的 node 进程让 Claude Code 重新拉起
- 杀 MCP 进程：`pgrep -f "dist/server.js" | xargs kill`，Claude Code 会自动重启它
