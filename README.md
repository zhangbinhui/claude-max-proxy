# claude-max-proxy

将 Claude Max 订阅转换为标准 Anthropic API 接口的本地代理网关。

让你的第三方工具（如 Cursor、Cline 等）通过 Claude Max 订阅额度调用 Claude API，无需额外付费购买 API credits。

## 原理

```
第三方客户端 → localhost:5678 → [请求处理] → api.anthropic.com
                                    ↓
                              - OAuth 认证注入
                              - 工具列表过滤
                              - 系统提示注入
                              - CCH 签名计算
                              - 关键词替换
```

代理读取 Claude Code CLI 本地保存的 OAuth token，将请求伪装为 Claude Code CLI 发出，从而使用订阅额度而非 API credits。

## 前置条件

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 已安装并登录（`claude` 命令可用）
- Claude Max / Pro 有效订阅

## 快速开始

```bash
# 克隆项目
git clone https://github.com/你的用户名/claude-max-proxy.git
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

## 工具过滤

代理默认只保留 13 个核心工具，避免因工具数量过多被 Anthropic 识别为第三方客户端。可在 `proxy.py` 中修改 `KEEP_TOOLS` 集合，但请谨慎测试——工具数量/组合是 Anthropic 检测第三方应用的维度之一。

## 注意事项

- 本项目仅供学习和研究用途
- Token 依赖 Claude Code CLI 的本地凭证，请勿泄露 `~/.claude/.credentials.json`
- Token 过期时代理会自动通过 `claude --print` 刷新
- 本项目由 Claude Opus 4.6 编写，遇到问题请咨询 AI

## License

[MIT](LICENSE)
