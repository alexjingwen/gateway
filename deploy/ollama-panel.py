#!/usr/bin/env python3
"""
Ollama Token Server - 管理面板
功能：模型管理、在线聊天 Playground、Token 用量统计、API 信息
纯 Python 标准库，无需额外依赖
"""

import http.server
import socketserver
import urllib.request
import urllib.error
import json
import os
import sys
import time
import socket
from typing import Optional

# ========== 配置 ==========
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:3000")
LISTEN_HOST = os.environ.get("PANEL_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("PANEL_PORT", "8081"))
API_KEY = os.environ.get("OLLAMA_PROXY_API_KEY", "sk-ollama-REPLACE_WITH_YOUR_KEY")
# ==========================

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ollama Token Server 管理面板</title>
<style>
:root {
  --bg: #0f0f0f; --surface: #1a1a2e; --surface2: #16213e; --border: #2a2a4a;
  --text: #e0e0e0; --text-dim: #888; --accent: #4f46e5; --accent2: #7c3aed;
  --green: #10b981; --red: #ef4444; --yellow: #f59e0b; --blue: #3b82f6;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:var(--bg); color:var(--text); height:100vh; display:flex; flex-direction:column; }

/* 顶栏 */
header { background:var(--surface); border-bottom:1px solid var(--border); padding:0 20px; display:flex; align-items:center; height:56px; gap:16px; flex-shrink:0; }
header .logo { font-size:18px; font-weight:700; background:linear-gradient(135deg,var(--accent),var(--accent2)); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
header .status { display:flex; align-items:center; gap:6px; font-size:13px; color:var(--text-dim); }
header .dot { width:8px; height:8px; border-radius:50%; background:var(--green); animation:pulse 2s infinite; }
@keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:0.4;} }
header nav { display:flex; gap:4px; margin-left:auto; }
header nav button { background:none; border:none; color:var(--text-dim); padding:8px 16px; border-radius:8px; cursor:pointer; font-size:14px; transition:all .2s; }
header nav button:hover { background:var(--surface2); color:var(--text); }
header nav button.active { background:var(--accent); color:#fff; }

/* 主区域 */
main { flex:1; overflow:hidden; display:flex; }
.view { display:none; flex:1; overflow:auto; padding:24px; }
.view.active { display:block; }

/* 卡片 */
.card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:20px; margin-bottom:16px; }
.card h2 { font-size:16px; margin-bottom:16px; display:flex; align-items:center; gap:8px; }

/* 仪表盘 */
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:16px; }
.stat { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:20px; }
.stat .label { font-size:12px; color:var(--text-dim); text-transform:uppercase; letter-spacing:1px; margin-bottom:8px; }
.stat .value { font-size:24px; font-weight:700; }
.stat .sub { font-size:13px; color:var(--text-dim); margin-top:4px; word-break:break-all; }
.stat .copy-btn { background:var(--surface2); border:1px solid var(--border); color:var(--text-dim); padding:4px 10px; border-radius:6px; cursor:pointer; font-size:12px; margin-top:8px; transition:all .2s; }
.stat .copy-btn:hover { border-color:var(--accent); color:var(--text); }

/* 模型列表 */
.model-item { display:flex; align-items:center; justify-content:space-between; padding:12px 16px; border-bottom:1px solid var(--border); }
.model-item:last-child { border-bottom:none; }
.model-item .name { font-weight:600; font-size:14px; }
.model-item .meta { font-size:12px; color:var(--text-dim); }
.model-item .badge { background:var(--surface2); padding:2px 8px; border-radius:4px; font-size:11px; color:var(--text-dim); }

/* Playground */
.playground { display:flex; gap:16px; height:100%; }
.playground .sidebar { width:280px; flex-shrink:0; display:flex; flex-direction:column; gap:12px; overflow:auto; }
.playground .chat-area { flex:1; display:flex; flex-direction:column; background:var(--surface); border:1px solid var(--border); border-radius:12px; overflow:hidden; }
.playground .messages { flex:1; overflow-y:auto; padding:16px; }
.playground .input-area { border-top:1px solid var(--border); padding:12px; display:flex; gap:8px; }
.playground .input-area textarea { flex:1; background:var(--bg); border:1px solid var(--border); border-radius:8px; color:var(--text); padding:10px 14px; font-size:14px; resize:none; height:44px; max-height:120px; font-family:inherit; }
.playground .input-area button { background:var(--accent); color:#fff; border:none; padding:0 20px; border-radius:8px; cursor:pointer; font-size:14px; font-weight:600; white-space:nowrap; transition:background .2s; }
.playground .input-area button:hover { background:var(--accent2); }
.playground .input-area button:disabled { opacity:0.5; cursor:not-allowed; }

/* 参数控件 */
.field { margin-bottom:12px; }
.field label { display:block; font-size:12px; color:var(--text-dim); margin-bottom:4px; }
.field select, .field input[type="number"] { width:100%; background:var(--bg); border:1px solid var(--border); border-radius:8px; color:var(--text); padding:8px 12px; font-size:13px; }
.field input[type="range"] { width:100%; accent-color:var(--accent); }
.field .range-val { font-size:12px; color:var(--accent); text-align:right; }

/* 消息气泡 */
.msg { margin-bottom:16px; max-width:85%; }
.msg.user { margin-left:auto; }
.msg.assistant { margin-right:auto; }
.msg .role { font-size:11px; color:var(--text-dim); margin-bottom:4px; text-transform:uppercase; letter-spacing:1px; }
.msg .bubble { padding:12px 16px; border-radius:12px; font-size:14px; line-height:1.6; white-space:pre-wrap; word-break:break-word; }
.msg.user .bubble { background:var(--accent); color:#fff; border-bottom-right-radius:4px; }
.msg.assistant .bubble { background:var(--surface2); border:1px solid var(--border); border-bottom-left-radius:4px; }
.msg .tokens { font-size:11px; color:var(--text-dim); margin-top:4px; }

/* Token 统计 */
.token-bar { display:flex; height:24px; border-radius:6px; overflow:hidden; margin:8px 0; }
.token-bar .seg { display:flex; align-items:center; justify-content:center; font-size:11px; font-weight:600; color:#fff; transition:width .5s; }
.token-bar .prompt { background:var(--blue); }
.token-bar .completion { background:var(--green); }
.token-history { margin-top:16px; }
.token-history table { width:100%; border-collapse:collapse; font-size:13px; }
.token-history th { text-align:left; color:var(--text-dim); padding:8px; border-bottom:1px solid var(--border); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:1px; }
.token-history td { padding:8px; border-bottom:1px solid var(--border); }
.token-history tr:hover { background:var(--surface2); }

/* 滚动条 */
::-webkit-scrollbar { width:6px; height:6px; }
::-webkit-scrollbar-track { background:transparent; }
::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }
::-webkit-scrollbar-thumb:hover { background:var(--text-dim); }

/* 响应式 */
@media (max-width:768px) {
  .playground { flex-direction:column; }
  .playground .sidebar { width:100%; max-height:200px; }
  .grid { grid-template-columns:1fr; }
}

.typing { display:inline-block; }
.typing span { animation:blink 1.4s infinite; }
.typing span:nth-child(2){animation-delay:.2s;}
.typing span:nth-child(3){animation-delay:.4s;}
@keyframes blink{0%,80%,100%{opacity:.2;}40%{opacity:1;}}
</style>
</head>
<body>
<header>
  <div class="logo">Ollama Token Server</div>
  <div class="status"><span class="dot" id="statusDot"></span><span id="statusText">检查中...</span></div>
  <nav>
    <button class="active" onclick="showView('dashboard')">仪表盘</button>
    <button onclick="showView('playground')">Playground</button>
    <button onclick="showView('tokens')">Token 统计</button>
  </nav>
</header>
<main>
  <!-- 仪表盘 -->
  <div class="view active" id="view-dashboard">
    <div class="grid">
      <div class="stat">
        <div class="label">API 端点 (OpenAI 兼容)</div>
        <div class="value" style="font-size:16px;word-break:break-all;">/v1</div>
        <div class="sub">本地: http://localhost:11434/v1</div>
        <div class="sub" id="lanUrl">局域网: 加载中...</div>
        <button class="copy-btn" onclick="copyText('http://localhost:11434/v1')">复制 Base URL</button>
      </div>
      <div class="stat">
        <div class="label">API Key</div>
        <div class="value" style="font-size:14px;word-break:break-all;" id="apiKeyDisplay">••••••••</div>
        <button class="copy-btn" onclick="toggleKey()">显示/隐藏</button>
        <button class="copy-btn" onclick="copyText(document.getElementById('apiKeyHidden').textContent)">复制</button>
        <span id="apiKeyHidden" style="display:none">__API_KEY__</span>
      </div>
      <div class="stat">
        <div class="label">可用模型</div>
        <div class="value" id="modelCount">-</div>
        <div class="sub" id="modelNames">加载中...</div>
      </div>
      <div class="stat">
        <div class="label">Ollama 版本</div>
        <div class="value" id="ollamaVersion">-</div>
        <div class="sub" id="ollamaStatus">检查中...</div>
      </div>
    </div>
    <div class="card">
      <h2>📦 模型列表</h2>
      <div id="modelList"><p style="color:var(--text-dim)">加载中...</p></div>
    </div>
    <div class="card">
      <h2>📋 快速使用指南</h2>
      <div style="font-size:13px;line-height:2;color:var(--text-dim);">
        <div><b style="color:var(--text)">cURL:</b> curl http://localhost:11434/v1/chat/completions -H "Content-Type: application/json" -d '{"model":"gemma4:latest","messages":[...]}'</div>
        <div><b style="color:var(--text)">Python SDK:</b> OpenAI(base_url="http://localhost:11434/v1", api_key="any")</div>
        <div><b style="color:var(--text)">流式输出:</b> 设置 "stream": true 即可，兼容 SSE 格式</div>
        <div><b style="color:var(--text)">Token 计数:</b> 响应中的 usage 字段包含 prompt_tokens / completion_tokens / total_tokens</div>
      </div>
    </div>
  </div>

  <!-- Playground -->
  <div class="view" id="view-playground">
    <div class="playground">
      <div class="sidebar">
        <div class="card" style="margin:0;">
          <h2>⚙️ 参数设置</h2>
          <div class="field">
            <label>模型</label>
            <select id="pgModel"></select>
          </div>
          <div class="field">
            <label>System Prompt</label>
            <textarea id="pgSystem" style="width:100%;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);padding:8px;font-size:13px;height:60px;resize:vertical;font-family:inherit;" placeholder="你是一个友好的助手...">你是一个友好的助手，用中文回答问题。</textarea>
          </div>
          <div class="field">
            <label>Temperature: <span class="range-val" id="pgTempVal">0.7</span></label>
            <input type="range" id="pgTemp" min="0" max="2" step="0.1" value="0.7" oninput="document.getElementById('pgTempVal').textContent=this.value">
          </div>
          <div class="field">
            <label>Max Tokens: <span class="range-val" id="pgMaxVal">500</span></label>
            <input type="range" id="pgMax" min="50" max="4096" step="50" value="500" oninput="document.getElementById('pgMaxVal').textContent=this.value">
          </div>
          <div class="field">
            <label>
              <input type="checkbox" id="pgStream" checked> 流式输出 (Stream)
            </label>
          </div>
          <button class="copy-btn" style="width:100%;padding:8px;" onclick="clearChat()">清空对话</button>
        </div>
      </div>
      <div class="chat-area">
        <div class="messages" id="chatMessages">
          <div class="msg assistant">
            <div class="role">Assistant</div>
            <div class="bubble">你好！我是基于 Ollama 的 AI 助手。在左侧选择模型并输入消息开始对话。</div>
          </div>
        </div>
        <div class="input-area">
          <textarea id="chatInput" placeholder="输入消息... (Enter 发送, Shift+Enter 换行)" onkeydown="handleKey(event)"></textarea>
          <button id="sendBtn" onclick="sendMessage()">发送</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Token 统计 -->
  <div class="view" id="view-tokens">
    <div class="card">
      <h2>📊 Token 用量统计</h2>
      <div id="tokenSummary">
        <div class="grid">
          <div class="stat"><div class="label">总请求数</div><div class="value" id="totalReqs">0</div></div>
          <div class="stat"><div class="label">总 Prompt Tokens</div><div class="value" id="totalPrompt" style="color:var(--blue)">0</div></div>
          <div class="stat"><div class="label">总 Completion Tokens</div><div class="value" id="totalCompletion" style="color:var(--green)">0</div></div>
          <div class="stat"><div class="label">总 Tokens</div><div class="value" id="totalAll" style="color:var(--accent2)">0</div></div>
        </div>
        <div style="margin-top:16px;">
          <div class="label" style="font-size:12px;color:var(--text-dim);margin-bottom:8px;">Token 分布</div>
          <div class="token-bar" id="tokenBar">
            <div class="seg prompt" style="width:50%;">0</div>
            <div class="seg completion" style="width:50%;">0</div>
          </div>
        </div>
      </div>
      <div class="token-history">
        <table>
          <thead><tr><th>时间</th><th>模型</th><th>Prompt</th><th>Completion</th><th>总计</th><th>耗时</th></tr></thead>
          <tbody id="tokenHistory"><tr><td colspan="6" style="color:var(--text-dim);text-align:center;">暂无记录，去 Playground 发送一条消息试试</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>
</main>

<script>
const API_BASE = '';
let tokenHistory = [];
let models = [];
let keyVisible = false;

// 视图切换
function showView(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('header nav button').forEach(b => b.classList.remove('active'));
  document.getElementById('view-' + name).classList.add('active');
  event.target.classList.add('active');
  if (name === 'tokens') updateTokenStats();
}

// 复制文本
function copyText(text) {
  navigator.clipboard.writeText(text).then(() => {
    showToast('已复制到剪贴板');
  });
}
function showToast(msg) {
  const t = document.createElement('div');
  t.textContent = msg;
  t.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--accent);color:#fff;padding:8px 20px;border-radius:8px;font-size:14px;z-index:9999;animation:fadeIn .3s;';
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2000);
}

// API Key 显示/隐藏
function toggleKey() {
  keyVisible = !keyVisible;
  const el = document.getElementById('apiKeyDisplay');
  const key = document.getElementById('apiKeyHidden').textContent;
  el.textContent = keyVisible ? key : '••••••••';
}

// 加载模型列表
async function loadModels() {
  try {
    const resp = await fetch(API_BASE + '/v1/models');
    const data = await resp.json();
    models = data.data || [];
    document.getElementById('modelCount').textContent = models.length;
    document.getElementById('modelNames').textContent = models.map(m => m.id).join(', ');
    
    const listEl = document.getElementById('modelList');
    if (models.length === 0) {
      listEl.innerHTML = '<p style="color:var(--text-dim)">未找到模型，请用 ollama pull 拉取模型</p>';
      return;
    }
    
    // 获取模型详细信息
    let detailHTML = '';
    for (const m of models) {
      try {
        const dr = await fetch(API_BASE + '/api/show', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({model: m.id})
        });
        const dd = await dr.json();
        const info = dd.details || {};
        const caps = (dd.capabilities || []).join(', ') || '-';
        detailHTML += `<div class="model-item">
          <div><div class="name">${m.id}</div><div class="meta">${info.parameter_size||'-'} · ${info.quantization_level||'-'} · ${info.family||'-'} · ${caps}</div></div>
          <span class="badge">${((dd.size||0)/1e9).toFixed(1)}GB</span>
        </div>`;
      } catch(e) {
        detailHTML += `<div class="model-item"><div class="name">${m.id}</div><span class="badge">-</span></div>`;
      }
    }
    listEl.innerHTML = detailHTML;
    
    // 填充 Playground 模型选择
    const sel = document.getElementById('pgModel');
    sel.innerHTML = models.map(m => `<option value="${m.id}">${m.id}</option>`).join('');
    
    document.getElementById('statusText').textContent = '运行中';
    document.getElementById('ollamaStatus').textContent = '✅ 在线';
  } catch(e) {
    document.getElementById('statusText').textContent = '离线';
    document.getElementById('statusDot').style.background = 'var(--red)';
    document.getElementById('ollamaStatus').textContent = '❌ 无法连接';
    document.getElementById('modelList').innerHTML = '<p style="color:var(--red)">无法连接 Ollama，请确认服务已启动</p>';
  }
}

// 获取 Ollama 版本
async function loadVersion() {
  try {
    const resp = await fetch(API_BASE + '/api/version');
    const data = await resp.json();
    document.getElementById('ollamaVersion').textContent = data.version || '-';
  } catch(e) {}
}

// 获取局域网 IP (通过后端)
async function loadLanIP() {
  try {
    const resp = await fetch(API_BASE + '/api/panel/lan-ip');
    const data = await resp.json();
    document.getElementById('lanUrl').textContent = `局域网: http://${data.ip}:11434/v1`;
  } catch(e) {}
}

// Playground 聊天
let chatMessages = [];
let isStreaming = false;

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

function clearChat() {
  chatMessages = [];
  document.getElementById('chatMessages').innerHTML = '';
}

async function sendMessage() {
  const input = document.getElementById('chatInput');
  const text = input.value.trim();
  if (!text || isStreaming) return;
  
  input.value = '';
  input.style.height = '44px';
  
  const model = document.getElementById('pgModel').value;
  const system = document.getElementById('pgSystem').value;
  const temp = parseFloat(document.getElementById('pgTemp').value);
  const maxTok = parseInt(document.getElementById('pgMax').value);
  const stream = document.getElementById('pgStream').checked;
  
  // 添加用户消息
  chatMessages.push({role:'user', content:text});
  appendMsg('user', text);
  
  // 添加 assistant 占位
  const assistantEl = appendMsg('assistant', '<span class="typing"><span>●</span><span>●</span><span>●</span></span>');
  isStreaming = true;
  document.getElementById('sendBtn').disabled = true;
  
  const msgs = [];
  if (system) msgs.push({role:'system', content:system});
  chatMessages.forEach(m => msgs.push(m));
  
  const startTime = Date.now();
  let fullResponse = '';
  let usage = null;
  
  try {
    if (stream) {
      const resp = await fetch(API_BASE + '/v1/chat/completions', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({model, messages:msgs, temperature:temp, max_tokens:maxTok, stream:true})
      });
      
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      
      while(true) {
        const {done, value} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream:true});
        const lines = buffer.split('\n');
        buffer = lines.pop();
        
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const data = line.slice(6).trim();
          if (data === '[DONE]') continue;
          try {
            const chunk = JSON.parse(data);
            const delta = chunk.choices?.[0]?.delta;
            if (delta?.content) {
              fullResponse += delta.content;
              assistantEl.querySelector('.bubble').innerHTML = escapeHtml(fullResponse);
              scrollToBottom();
            }
            if (delta?.reasoning && !fullResponse) {
              // 显示思维过程（可选）
            }
            if (chunk.usage) usage = chunk.usage;
          } catch(e) {}
        }
      }
      if (!usage) {
        // 流式可能不带 usage，估算
        usage = {prompt_tokens:0, completion_tokens:0, total_tokens:0};
      }
    } else {
      const resp = await fetch(API_BASE + '/v1/chat/completions', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({model, messages:msgs, temperature:temp, max_tokens:maxTok, stream:false})
      });
      const data = await resp.json();
      fullResponse = data.choices?.[0]?.message?.content || '(无回复)';
      // 处理 reasoning 字段
      const reasoning = data.choices?.[0]?.message?.reasoning || '';
      if (!fullResponse && reasoning) {
        for (const m of ['最终回答：','Assistant:','最终答案：']) {
          if (reasoning.includes(m)) { fullResponse = reasoning.split(m)[1].trim(); break; }
        }
        if (!fullResponse) fullResponse = reasoning.slice(-500);
      }
      usage = data.usage || {prompt_tokens:0, completion_tokens:0, total_tokens:0};
      assistantEl.querySelector('.bubble').innerHTML = escapeHtml(fullResponse);
    }
    
    const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
    
    // 添加 token 信息
    const tokEl = document.createElement('div');
    tokEl.className = 'tokens';
    tokEl.textContent = `prompt: ${usage.prompt_tokens} | completion: ${usage.completion_tokens} | total: ${usage.total_tokens} | ${elapsed}s`;
    assistantEl.appendChild(tokEl);
    
    // 保存到历史
    chatMessages.push({role:'assistant', content:fullResponse});
    tokenHistory.unshift({
      time: new Date().toLocaleTimeString(),
      model, prompt: usage.prompt_tokens, completion: usage.completion_tokens,
      total: usage.total_tokens, elapsed: elapsed + 's'
    });
    
  } catch(e) {
    assistantEl.querySelector('.bubble').innerHTML = '<span style="color:var(--red)">请求失败: ' + escapeHtml(e.message) + '</span>';
  } finally {
    isStreaming = false;
    document.getElementById('sendBtn').disabled = false;
    scrollToBottom();
  }
}

function appendMsg(role, content) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.innerHTML = `<div class="role">${role === 'user' ? 'You' : 'Assistant'}</div><div class="bubble">${content}</div>`;
  document.getElementById('chatMessages').appendChild(div);
  scrollToBottom();
  return div;
}

function scrollToBottom() {
  const el = document.getElementById('chatMessages');
  el.scrollTop = el.scrollHeight;
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
}

// Token 统计更新
function updateTokenStats() {
  const totalReq = tokenHistory.length;
  const totalP = tokenHistory.reduce((s,r) => s+r.prompt, 0);
  const totalC = tokenHistory.reduce((s,r) => s+r.completion, 0);
  const totalA = totalP + totalC;
  
  document.getElementById('totalReqs').textContent = totalReq;
  document.getElementById('totalPrompt').textContent = totalP;
  document.getElementById('totalCompletion').textContent = totalC;
  document.getElementById('totalAll').textContent = totalA;
  
  // Token 分布条
  const bar = document.getElementById('tokenBar');
  if (totalA > 0) {
    const pPct = (totalP/totalA*100).toFixed(0);
    const cPct = (totalC/totalA*100).toFixed(0);
    bar.innerHTML = `<div class="seg prompt" style="width:${pPct}%;">Prompt ${totalP}</div><div class="seg completion" style="width:${cPct}%;">Completion ${totalC}</div>`;
  }
  
  // 历史表
  const histEl = document.getElementById('tokenHistory');
  if (tokenHistory.length === 0) {
    histEl.innerHTML = '<tr><td colspan="6" style="color:var(--text-dim);text-align:center;">暂无记录，去 Playground 发送一条消息试试</td></tr>';
  } else {
    histEl.innerHTML = tokenHistory.map(r => 
      `<tr><td>${r.time}</td><td>${r.model}</td><td style="color:var(--blue)">${r.prompt}</td><td style="color:var(--green)">${r.completion}</td><td><b>${r.total}</b></td><td>${r.elapsed}</td></tr>`
    ).join('');
  }
}

// 自动调整输入框高度
document.getElementById('chatInput').addEventListener('input', function() {
  this.style.height = '44px';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});

// 初始化
loadModels();
loadVersion();
loadLanIP();
setInterval(loadModels, 30000); // 30秒刷新
</script>
</body>
</html>"""


def get_lan_ip() -> str:
    """获取本机局域网 IP"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class PanelHandler(http.server.BaseHTTPRequestHandler):
    """管理面板请求处理器"""

    def log_message(self, format, *args):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {format % args}", flush=True)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        # 首页
        if path == "/" or path == "/index.html":
            html = HTML_PAGE.replace("__API_KEY__", API_KEY)
            self._send_html(html)
            return

        # 面板 API: 局域网 IP
        if path == "/api/panel/lan-ip":
            self._send_json({"ip": get_lan_ip()})
            return

        # 代理到 Ollama: /v1/* 和 /api/*
        if path.startswith("/v1/") or path.startswith("/api/"):
            self._proxy("GET")
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path.startswith("/v1/") or path.startswith("/api/"):
            self._proxy("POST")
            return
        self.send_error(404, "Not Found")

    def _proxy(self, method: str):
        url = f"{OLLAMA_HOST}{self.path}"
        content_length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(content_length) if content_length > 0 else b""

        headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
        upstream_auth = os.environ.get("OLLAMA_PROXY_API_KEY", "").strip()
        if upstream_auth:
            headers["Authorization"] = f"Bearer {upstream_auth}"

        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    kl = k.lower()
                    if kl in ("content-length", "transfer-encoding", "connection"):
                        continue
                    self.send_header(k, v)
                self._cors()
                self.end_headers()
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except urllib.error.HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            if err_body:
                self.wfile.write(err_body)
        except Exception as e:
            self._send_json({"error": str(e)}, 502)


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    server = ThreadedServer((LISTEN_HOST, LISTEN_PORT), PanelHandler)
    lan_ip = get_lan_ip()
    print("=" * 55)
    print("  Ollama Token Server 管理面板启动成功")
    print("=" * 55)
    print(f"  本地访问:  http://localhost:{LISTEN_PORT}")
    print(f"  局域网:    http://{lan_ip}:{LISTEN_PORT}")
    print(f"  Ollama:    {OLLAMA_HOST}")
    print(f"  API Key:   {API_KEY[:12]}...")
    print("=" * 55)
    print("  功能: 仪表盘 | Playground | Token 统计")
    print("=" * 55)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 停止中...")
        server.shutdown()


if __name__ == "__main__":
    main()
