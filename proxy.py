#!/usr/bin/env python3
"""
Claude Max → Standard API 代理网关 v3
基于 Neo (linux.do) 的逆向方案 + mitmproxy 抓包验证

关键发现：
1. 认证用 Authorization: Bearer <oauth-token>
2. 必须带 anthropic-beta: oauth-2025-04-20
3. Anthropic 通过扫描请求 body 中的 "OpenClaw" 关键词检测第三方应用
4. 替换该关键词即可绕过检测，使用订阅额度
"""

import json
import os
import sys
import time
import uuid

import xxhash
import requests
from flask import Flask, request, Response, stream_with_context

app = Flask(__name__)

# ============================================================
# 配置
# ============================================================

DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
PORT = int(os.environ.get("PORT", "5678"))

CLAUDE_DIR = os.path.expanduser("~/.claude")
CREDENTIALS_FILE = os.path.join(CLAUDE_DIR, ".credentials.json")

UPSTREAM = "https://api.anthropic.com"
CCH_SEED = 0x6E52736AC806831E

# CC 版本自动检测
def detect_cc_version():
    import subprocess
    import re
    try:
        out = subprocess.check_output(["claude", "--version"], timeout=5, text=True).strip()
        m = re.search(r'(\d+\.\d+\.\d+)', out)
        main_ver = m.group(1) if m else "2.1.92"
    except:
        main_ver = "2.1.92"

    build_cache = os.path.join(os.path.dirname(__file__), ".cc_build")
    build_num = "190"
    if os.path.exists(build_cache):
        with open(build_cache) as f:
            cached = f.read().strip()
            if cached:
                build_num = cached

    return main_ver, build_num

CC_VERSION, CC_BUILD = detect_cc_version()
CC_FULL_VERSION = f"{CC_VERSION}.{CC_BUILD}"

# ============================================================
# Token 管理
# ============================================================

def load_credentials():
    with open(CREDENTIALS_FILE) as f:
        return json.load(f)["claudeAiOauth"]

def get_access_token():
    cred = load_credentials()
    expires_at = cred.get("expiresAt", 0)
    if time.time() * 1000 > expires_at - 300_000:
        sys.stdout.write("[proxy] Token expiring, refreshing via claude --print...\n")
        sys.stdout.flush()
        os.system('claude --print "ping" > /dev/null 2>&1')
        cred = load_credentials()
    return cred["accessToken"]

# ============================================================
# cch 签名计算
# ============================================================

def compute_cch(body_bytes: bytes) -> str:
    h = xxhash.xxh64(body_bytes, seed=CCH_SEED).intdigest()
    return f"{h & 0xFFFFF:05x}"

# ============================================================
# 被 Anthropic 屏蔽的第三方应用关键词
# ============================================================

BLOCKED_KEYWORDS = [
    ("OpenClaw", "Claude Code"),
    ("openclaw", "claude_code"),
    ("open_claw", "claude_code"),
    ("open-claw", "claude-code"),
]

# 不应被替换的模式（占位保护）
# 替换这些会导致 tool 调用失败、命令执行出错、URL 不可达等问题
import re
_PROTECT_PLACEHOLDER = "__OCPROT_{}_TORPCO__"
_PROTECT_PATTERNS = [
    # 文件路径: /home/xxx/.openclaw/workspace-daliu/, ~/.openclaw/media/
    re.compile(r'(/[\w.~/-]*)\.openclaw(/[\w.~/-]*)'),
    # npm 模块路径: node_modules/openclaw/
    re.compile(r'node_modules/openclaw'),
    # channel 标识符: openclaw-weixin
    re.compile(r'openclaw-weixin'),
]

def sanitize_body(body_str: str) -> str:
    """替换被 Anthropic 屏蔽的第三方应用关键词，但保护路径/命令/URL/标识符不被篡改"""
    # 1. 收集所有需要保护的文本片段
    placeholders = []
    for pattern in _PROTECT_PATTERNS:
        for m in pattern.finditer(body_str):
            placeholders.append(m.group())
    # 去重并按长度降序（先替换长的，避免子串冲突）
    placeholders = sorted(set(placeholders), key=len, reverse=True)
    for i, ph in enumerate(placeholders):
        body_str = body_str.replace(ph, _PROTECT_PLACEHOLDER.format(i))

    # 2. 执行关键词替换
    for old, new in BLOCKED_KEYWORDS:
        body_str = body_str.replace(old, new)

    # 3. 恢复占位符为原始文本
    for i, ph in enumerate(placeholders):
        body_str = body_str.replace(_PROTECT_PLACEHOLDER.format(i), ph)

    return body_str

# ============================================================
# 请求体处理
# ============================================================

# 加载 tool 名称映射表
_MAPPING_FILE = os.path.join(os.path.dirname(__file__), "tool_name_mapping.json")
with open(_MAPPING_FILE) as _f:
    _mapping = json.load(_f)

REMOVE_TOOLS = set(_mapping["_remove"])
OC_TO_CC = {**_mapping["direct"], **_mapping["borrowed"]}
CC_TO_OC = {v: k for k, v in OC_TO_CC.items()}

def replace_tools(body: dict) -> None:
    """替换 tool 名称：移除不需要的，把 OC 名改成 CC 名，保留原始 schema"""
    tools = body.get("tools")
    if not tools:
        return

    new_tools = []
    for t in tools:
        name = t.get("name")
        if name in REMOVE_TOOLS:
            continue
        if name in OC_TO_CC:
            t = {**t, "name": OC_TO_CC[name]}
        new_tools.append(t)

    body["tools"] = new_tools
    sys.stdout.write(f"[proxy] tools: {len(new_tools)} mapped (removed {len(tools) - len(new_tools)}), "
                     f"names={[t['name'] for t in new_tools]}\n")
    sys.stdout.flush()

def inject_system_and_cch(body: dict) -> bytes:
    """注入 Claude Code 的 system prompts + 计算 cch 签名

    核心策略：把 openclaw 的 system prompt 移到第一条 user message 里，
    system 参数只保留标准 Claude Code 格式，避免被 Anthropic 检测。
    """
    # 提取原始 system prompt
    original_system = body.get("system", [])
    if isinstance(original_system, str):
        original_system = [{"type": "text", "text": original_system}]

    # 把原始 system prompt 拼接成文本，移到第一条 user message
    if original_system:
        sys_texts = []
        for block in original_system:
            if isinstance(block, dict) and block.get("text"):
                sys_texts.append(block["text"])
            elif isinstance(block, str):
                sys_texts.append(block)

        if sys_texts:
            combined_sys = "\n\n".join(sys_texts)
            prefix = f"<system_instructions>\n{combined_sys}\n</system_instructions>\n\n"

            # 注入到第一条 user message 的开头
            messages = body.get("messages", [])
            for msg in messages:
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        msg["content"] = prefix + content
                    elif isinstance(content, list):
                        msg["content"] = [{"type": "text", "text": prefix}] + content
                    break

    # system 只保留标准 Claude Code 格式
    billing = {
        "type": "text",
        "text": f"x-anthropic-billing-header: cc_version={CC_FULL_VERSION}; cc_entrypoint=sdk-cli; cch=00000;",
    }
    identity = {
        "type": "text",
        "text": "You are Claude Code, Anthropic's official CLI for Claude.",
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }

    body["system"] = [billing, identity]

    body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    # 关键词替换已禁用——依赖 tool name 映射 + system prompt 移动来绕过检测
    # body_str = sanitize_body(body_str)

    body_bytes = body_str.encode("utf-8")

    cch = compute_cch(body_bytes)
    body_bytes = body_bytes.replace(b"cch=00000", f"cch={cch}".encode("utf-8"), 1)

    return body_bytes

# ============================================================
# 构造请求头
# ============================================================

def build_headers(access_token: str) -> dict:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": f"claude-cli/{CC_VERSION} (external, cli)",
        "X-Claude-Code-Session-Id": str(uuid.uuid4()),
        "x-app": "cli",
        "anthropic-dangerous-direct-browser-access": "true",
        "anthropic-beta": "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,context-1m-2025-08-07,context-management-2025-06-27,prompt-caching-scope-2026-01-05,effort-2025-11-24",
        "anthropic-version": "2023-06-01",
        "X-Stainless-Lang": "js",
        "X-Stainless-Package-Version": "0.80.0",
        "X-Stainless-OS": "Linux",
        "X-Stainless-Arch": "x64",
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": "v24.3.0",
    }

# ============================================================
# Flask 路由
# ============================================================

@app.route("/v1/messages", methods=["POST"])
def proxy_messages():
    try:
        raw = request.get_data(as_text=True)
        body = json.loads(raw)
    except Exception as e:
        return {"error": str(e)}, 400

    access_token = get_access_token()

    if DEBUG:
        if len(raw) > 1000:
            with open("/tmp/proxy_neo_raw.json", "w") as df:
                df.write(raw)

    replace_tools(body)
    body_bytes = inject_system_and_cch(body)
    headers = build_headers(access_token)

    if DEBUG:
        with open("/tmp/proxy_neo_last.json", "wb") as df:
            df.write(body_bytes)

    is_stream = body.get("stream", False)
    if is_stream:
        headers["Accept"] = "text/event-stream"

    sys.stdout.write(f"[proxy] → {UPSTREAM}/v1/messages?beta=true "
          f"model={body.get('model', '?')} stream={is_stream} "
          f"body_size={len(body_bytes)}\n")
    sys.stdout.flush()

    resp = requests.post(
        f"{UPSTREAM}/v1/messages?beta=true",
        data=body_bytes,
        headers=headers,
        stream=is_stream,
        timeout=300,
    )

    sys.stdout.write(f"[proxy] ← status={resp.status_code}\n")
    if resp.status_code >= 400:
        try:
            sys.stdout.write(f"[proxy] ← error: {resp.text[:500]}\n")
        except:
            pass
    sys.stdout.flush()

    def remap_tool_names(data: bytes) -> bytes:
        """把响应中的 CC tool 名替换回 OC tool 名"""
        text = data.decode("utf-8", errors="replace")
        for cc_name, oc_name in CC_TO_OC.items():
            text = text.replace(f'"name":"{cc_name}"', f'"name":"{oc_name}"')
            text = text.replace(f'"name": "{cc_name}"', f'"name": "{oc_name}"')
        return text.encode("utf-8")

    if is_stream:
        def generate():
            for chunk in resp.iter_content(chunk_size=None):
                if chunk:
                    yield remap_tool_names(chunk)
        return Response(
            stream_with_context(generate()),
            status=resp.status_code,
            content_type=resp.headers.get("content-type", "text/event-stream"),
        )
    else:
        excluded = {"transfer-encoding", "content-encoding", "content-length", "connection"}
        resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
        return Response(
            remap_tool_names(resp.content),
            status=resp.status_code,
            headers=resp_headers,
            content_type=resp.headers.get("content-type", "application/json"),
        )

@app.route("/health")
def health():
    try:
        cred = load_credentials()
        remaining = (cred.get("expiresAt", 0) / 1000 - time.time()) / 3600
        return {"status": "ok", "token_hours": round(remaining, 1), "cc_version": CC_FULL_VERSION}
    except Exception as e:
        return {"status": "error", "error": str(e)}, 500

if __name__ == "__main__":
    print(f"=== Claude Max → API 代理网关 v3 ===")
    print(f"CC Version: {CC_FULL_VERSION}")
    print()
    try:
        cred = load_credentials()
        remaining = (cred.get("expiresAt", 0) / 1000 - time.time()) / 3600
        print(f"Subscription: {cred.get('subscriptionType')} ({cred.get('rateLimitTier')})")
        print(f"Token valid for: {remaining:.1f} hours")
        if remaining < 0.1:
            get_access_token()
    except FileNotFoundError:
        print("❌ Claude Code credentials not found!")
        sys.exit(1)
    print()
    print(f"🚀 http://localhost:{PORT}")
    if DEBUG:
        print(f"🔍 DEBUG mode ON (request dumps → /tmp/)")
    print()
    app.run(host="0.0.0.0", port=PORT, debug=False)
