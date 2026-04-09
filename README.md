# claude-max-proxy

将 Claude Max 订阅转换为标准 Anthropic API 接口的本地代理网关。

让你的第三方工具（如 Cursor、OpenClaw 等）通过 Claude Max 订阅额度调用 Claude API，无需额外付费购买 API credits。

> **请低调使用，不要大范围宣传。**

## 原理

```
第三方客户端 → localhost:5678 → [请求处理] → api.anthropic.com
                                    ↓
                              - OAuth 认证注入
                              - 工具名称映射 (双向)
                              - System Prompt 迁移
                              - CCH 签名计算
```

代理读取 Claude Code CLI 本地保存的 OAuth token，将请求伪装为 Claude Code CLI 发出，从而使用订阅额度而非 API credits。

## 前置条件

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 已安装并登录（`claude` 命令可用）
- Claude Max / Pro 有效订阅

## 快速开始

```bash
# 克隆项目
git clone https://github.com/zhangbinhui/claude-max-proxy.git
cd claude-max-proxy

# 创建虚拟环境并安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 启动代理
python3 proxy.py
```

启动后代理监听 `http://localhost:5678`，兼容标准 Anthropic Messages API。

## 配置

通过环境变量配置：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `5678` | 监听端口 |
| `DEBUG` | 空 | 设为 `1` 开启调试模式（请求 dump 到 `/tmp/`） |

示例：

```bash
PORT=8080 python3 proxy.py
DEBUG=1 python3 proxy.py
```

## 在第三方工具中使用

将 API Base URL 设置为 `http://localhost:5678`，API Key 随意填写（代理会忽略并使用本地 OAuth token）。

## 关于 Extra Usage

本项目的主要目的是让第三方工具用上 Claude Max/Pro 的订阅额度，因此建议在 [claude.ai/settings/usage](https://claude.ai/settings/usage) 中关闭 Extra Usage，避免产生额外费用。

如果遇到 `You're out of extra usage` 报错，说明该请求被 Anthropic 判定为第三方客户端，强制走 Extra Usage 计费。

## 工具名称映射

代理通过 `tool_name_mapping.json` 对工具名进行双向映射：

- **请求方向**：第三方客户端的工具名 → Claude Code 原生工具名（如 `exec` → `Bash`，`read` → `Read`）
- **响应方向**：Claude Code 返回的工具名 → 第三方客户端工具名（自动反向映射）
- **移除不支持的工具**：`_remove` 列表中的工具会被直接丢弃（如 `canvas`、`browser` 等 Claude Code 不存在的工具）

映射表分两类：
- `direct`：功能直接对应的工具（如 `read` ↔ `Read`）
- `borrowed`：借用 Claude Code 已有工具名的映射（如 `memory_search` → `Glob`），保留原始 schema 不变

如需自定义，编辑 `tool_name_mapping.json` 即可。

## System Prompt 处理

Anthropic 通过 `system` 参数检测第三方应用。代理将原始 system prompt 迁移到第一条 user message 中（包裹在 `<system_instructions>` 标签内），`system` 参数只保留标准 Claude Code 格式（billing header + identity），从而绕过检测。

## 参考文件

| 文件 | 说明 |
|------|------|
| `cc_tools_baseline.json` | Claude Code 原生工具定义快照，用于对照映射 |
| `cc_system_baseline.json` | Claude Code 原生 system prompt 快照，用于对照格式 |
| `tool_name_mapping.json` | 工具名称映射配置 |

## 注意事项

- 本项目仅供学习和研究用途
- Token 依赖 Claude Code CLI 的本地凭证，请勿泄露 `~/.claude/.credentials.json`
- Token 过期时代理会自动通过 `claude --print` 刷新
- CC 版本号从本地 `claude --version` 自动检测，build 号可通过 `.cc_build` 文件缓存
- 本项目由 Claude Opus 4.6 编写，遇到问题请咨询 AI

## License

[MIT](LICENSE)
