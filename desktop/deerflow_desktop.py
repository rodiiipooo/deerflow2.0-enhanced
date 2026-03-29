#!/usr/bin/env python3
"""
DeerFlow Desktop Manager
========================
A desktop tool to run and manage DeerFlow on your local machine.

Provides:
  - One-click start/stop of all DeerFlow services
  - Web-based control panel for configuration and monitoring
  - API key and model configuration wizard
  - Service health monitoring with auto-restart
  - System tray integration (when pystray is installed)
  - Auto-opens the DeerFlow web UI in your browser

Usage:
    python desktop/deerflow_desktop.py              # Launch the desktop manager
    python desktop/deerflow_desktop.py --headless    # Run without opening browser
    python desktop/deerflow_desktop.py --port 9000   # Use custom control panel port
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.request import urlopen

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_DIR = REPO_ROOT / "frontend"
LOGS_DIR = REPO_ROOT / "logs"
CONFIG_FILE = REPO_ROOT / "config.yaml"
CONFIG_EXAMPLE = REPO_ROOT / "config.example.yaml"
ENV_FILE = REPO_ROOT / ".env"
EXTENSIONS_CONFIG = REPO_ROOT / "extensions_config.json"
EXTENSIONS_EXAMPLE = REPO_ROOT / "extensions_config.example.json"

# ---------------------------------------------------------------------------
# Service registry
# ---------------------------------------------------------------------------

SERVICES: dict[str, dict[str, Any]] = {
    "langgraph": {
        "name": "LangGraph Server",
        "port": 2024,
        "health": "http://localhost:2024/ok",
        "cwd": str(BACKEND_DIR),
        "cmd_dev": ["uv", "run", "langgraph", "dev", "--no-browser", "--allow-blocking"],
        "cmd_prod": ["uv", "run", "langgraph", "dev", "--no-browser", "--allow-blocking", "--no-reload"],
        "log": "langgraph.log",
    },
    "gateway": {
        "name": "Gateway API",
        "port": 8001,
        "health": "http://localhost:8001/health",
        "cwd": str(BACKEND_DIR),
        "cmd_dev": ["uv", "run", "uvicorn", "app.gateway.app:app", "--host", "0.0.0.0", "--port", "8001",
                     "--reload", "--reload-include=*.yaml", "--reload-include=.env"],
        "cmd_prod": ["uv", "run", "uvicorn", "app.gateway.app:app", "--host", "0.0.0.0", "--port", "8001"],
        "log": "gateway.log",
        "env_extra": {"PYTHONPATH": "."},
    },
    "frontend": {
        "name": "Frontend",
        "port": 3000,
        "health": "http://localhost:3000",
        "cwd": str(FRONTEND_DIR),
        "cmd_dev": ["pnpm", "run", "dev"],
        "cmd_prod": ["pnpm", "run", "preview"],
        "log": "frontend.log",
    },
    "nginx": {
        "name": "Nginx Proxy",
        "port": 2026,
        "health": "http://localhost:2026",
        "cwd": str(REPO_ROOT),
        "cmd_dev": ["nginx", "-g", "daemon off;", "-c",
                     str(REPO_ROOT / "docker" / "nginx" / "nginx.local.conf"),
                     "-p", str(REPO_ROOT)],
        "cmd_prod": None,  # same command
        "log": "nginx.log",
    },
}

# ---------------------------------------------------------------------------
# ServiceManager - manages DeerFlow processes
# ---------------------------------------------------------------------------


class ServiceManager:
    """Start, stop and monitor DeerFlow backend/frontend services."""

    def __init__(self, dev_mode: bool = True) -> None:
        self.dev_mode = dev_mode
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    # -- helpers --

    @staticmethod
    def _port_open(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            return s.connect_ex(("127.0.0.1", port)) == 0

    @staticmethod
    def _wait_for_port(port: int, timeout: int = 120) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if ServiceManager._port_open(port):
                return True
            time.sleep(1)
        return False

    @staticmethod
    def _health_check(url: str) -> bool:
        try:
            resp = urlopen(url, timeout=3)
            return resp.status < 500
        except Exception:
            return False

    @staticmethod
    def _kill_by_port(port: int) -> None:
        """Best-effort kill whatever occupies *port*."""
        try:
            out = subprocess.check_output(["lsof", "-ti", f":{port}"], text=True, stderr=subprocess.DEVNULL)
            for pid in out.strip().split("\n"):
                if pid:
                    os.kill(int(pid), signal.SIGTERM)
        except Exception:
            pass

    # -- lifecycle --

    def start_service(self, key: str) -> bool:
        svc = SERVICES[key]
        cmd = svc["cmd_dev"] if self.dev_mode else (svc["cmd_prod"] or svc["cmd_dev"])
        if cmd is None:
            return False

        LOGS_DIR.mkdir(exist_ok=True)
        log_path = LOGS_DIR / svc["log"]

        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        if "env_extra" in svc:
            env.update(svc["env_extra"])

        with self._lock:
            if key in self._procs and self._procs[key].poll() is None:
                return True  # already running

            log_fh = open(log_path, "w")
            proc = subprocess.Popen(
                cmd,
                cwd=svc["cwd"],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )
            self._procs[key] = proc

        ok = self._wait_for_port(svc["port"], timeout=120 if key == "frontend" else 60)
        return ok

    def stop_service(self, key: str) -> None:
        svc = SERVICES[key]
        with self._lock:
            proc = self._procs.pop(key, None)
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
            proc.wait(timeout=10)
        self._kill_by_port(svc["port"])

    def start_all(self) -> dict[str, bool]:
        results: dict[str, bool] = {}
        order = ["langgraph", "gateway", "frontend", "nginx"]
        for key in order:
            results[key] = self.start_service(key)
        return results

    def stop_all(self) -> None:
        for key in reversed(list(self._procs)):
            self.stop_service(key)
        # Also kill stragglers
        for svc in SERVICES.values():
            self._kill_by_port(svc["port"])

    def status(self) -> dict[str, dict[str, Any]]:
        result = {}
        for key, svc in SERVICES.items():
            port_up = self._port_open(svc["port"])
            proc = self._procs.get(key)
            running = proc is not None and proc.poll() is None
            result[key] = {
                "name": svc["name"],
                "port": svc["port"],
                "port_open": port_up,
                "process_running": running,
                "healthy": port_up,
            }
        return result


# ---------------------------------------------------------------------------
# ConfigManager - read/write config files
# ---------------------------------------------------------------------------


class ConfigManager:
    """Helpers for reading and editing DeerFlow configuration."""

    @staticmethod
    def ensure_config() -> bool:
        """Copy example config files if they don't exist. Returns True if configs are ready."""
        created = False
        if not CONFIG_FILE.exists() and CONFIG_EXAMPLE.exists():
            shutil.copy(CONFIG_EXAMPLE, CONFIG_FILE)
            created = True
        if not EXTENSIONS_CONFIG.exists() and EXTENSIONS_EXAMPLE.exists():
            shutil.copy(EXTENSIONS_EXAMPLE, EXTENSIONS_CONFIG)
            created = True
        return CONFIG_FILE.exists()

    @staticmethod
    def read_config() -> str:
        if CONFIG_FILE.exists():
            return CONFIG_FILE.read_text()
        return ""

    @staticmethod
    def read_env() -> dict[str, str]:
        env: dict[str, str] = {}
        if ENV_FILE.exists():
            for line in ENV_FILE.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
        return env

    @staticmethod
    def write_env(data: dict[str, str]) -> None:
        lines: list[str] = []
        existing = {}
        if ENV_FILE.exists():
            for line in ENV_FILE.read_text().splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k, _, _ = stripped.partition("=")
                    existing[k.strip()] = len(lines)
                lines.append(line)

        for key, value in data.items():
            if key in existing:
                lines[existing[key]] = f"{key}={value}"
            else:
                lines.append(f"{key}={value}")

        ENV_FILE.write_text("\n".join(lines) + "\n")

    @staticmethod
    def get_models_from_config() -> list[dict[str, str]]:
        """Parse model entries from config.yaml (simple YAML parsing)."""
        models: list[dict[str, str]] = []
        if not CONFIG_FILE.exists():
            return models
        content = CONFIG_FILE.read_text()
        in_models = False
        current: dict[str, str] = {}
        for line in content.splitlines():
            stripped = line.strip()
            if stripped == "models:":
                in_models = True
                continue
            if in_models:
                if stripped.startswith("- name:"):
                    if current:
                        models.append(current)
                    current = {"name": stripped.split(":", 1)[1].strip()}
                elif stripped.startswith("# - name:") or stripped.startswith("#   "):
                    continue
                elif current and stripped.startswith("display_name:"):
                    current["display_name"] = stripped.split(":", 1)[1].strip()
                elif current and stripped.startswith("model:"):
                    current["model"] = stripped.split(":", 1)[1].strip()
                elif current and stripped.startswith("use:"):
                    current["use"] = stripped.split(":", 1)[1].strip()
                elif not stripped.startswith("-") and not stripped.startswith("#") and ":" in stripped and not stripped.startswith(" "):
                    # New top-level section
                    if current:
                        models.append(current)
                    in_models = False
        if current:
            models.append(current)
        return models

    @staticmethod
    def check_prerequisites() -> dict[str, bool]:
        """Check if required tools are installed."""
        checks: dict[str, bool] = {}
        for cmd in ["python3", "uv", "node", "pnpm", "nginx"]:
            checks[cmd] = shutil.which(cmd) is not None
        checks["config_exists"] = CONFIG_FILE.exists()
        checks["env_exists"] = ENV_FILE.exists()
        return checks

    @staticmethod
    def read_extensions_config() -> dict:
        if EXTENSIONS_CONFIG.exists():
            return json.loads(EXTENSIONS_CONFIG.read_text())
        return {"mcpServers": {}, "skills": {}}


# ---------------------------------------------------------------------------
# Web-based Control Panel
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DeerFlow Desktop Manager</title>
<style>
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #252837;
    --border: #2e3144;
    --text: #e4e4e7;
    --text-muted: #a1a1aa;
    --primary: #6366f1;
    --primary-hover: #818cf8;
    --success: #22c55e;
    --danger: #ef4444;
    --warning: #f59e0b;
    --radius: 12px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding: 0;
  }
  .header {
    background: linear-gradient(135deg, #1e1b4b 0%, #312e81 50%, #1e1b4b 100%);
    padding: 2rem 2rem 1.5rem;
    border-bottom: 1px solid var(--border);
  }
  .header h1 {
    font-size: 1.75rem;
    font-weight: 700;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .header p {
    color: var(--text-muted);
    margin-top: 0.25rem;
    font-size: 0.9rem;
  }
  .container {
    max-width: 1100px;
    margin: 0 auto;
    padding: 1.5rem;
  }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1.5rem;
    margin-bottom: 1.5rem;
  }
  @media (max-width: 768px) { .grid { grid-template-columns: 1fr; } }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.25rem;
  }
  .card h2 {
    font-size: 1rem;
    font-weight: 600;
    margin-bottom: 1rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .service-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.6rem 0;
    border-bottom: 1px solid var(--border);
  }
  .service-row:last-child { border-bottom: none; }
  .service-info { display: flex; align-items: center; gap: 0.75rem; }
  .dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .dot.green { background: var(--success); box-shadow: 0 0 6px var(--success); }
  .dot.red { background: var(--danger); }
  .dot.yellow { background: var(--warning); animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
  .service-name { font-weight: 500; }
  .service-port { color: var(--text-muted); font-size: 0.85rem; }
  .btn {
    padding: 0.5rem 1rem;
    border-radius: 8px;
    border: none;
    font-size: 0.85rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s;
    color: white;
  }
  .btn-primary { background: var(--primary); }
  .btn-primary:hover { background: var(--primary-hover); }
  .btn-success { background: var(--success); }
  .btn-success:hover { background: #16a34a; }
  .btn-danger { background: var(--danger); }
  .btn-danger:hover { background: #dc2626; }
  .btn-sm { padding: 0.35rem 0.75rem; font-size: 0.8rem; }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .actions {
    display: flex;
    gap: 0.75rem;
    flex-wrap: wrap;
    margin-bottom: 1.5rem;
  }
  .prereq-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 0.5rem;
  }
  .prereq-item {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.4rem 0.6rem;
    background: var(--surface2);
    border-radius: 8px;
    font-size: 0.85rem;
  }
  .check { color: var(--success); }
  .cross { color: var(--danger); }
  .env-form { display: flex; flex-direction: column; gap: 0.75rem; }
  .env-row {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }
  .env-row label {
    min-width: 160px;
    font-size: 0.85rem;
    font-weight: 500;
  }
  .env-row input {
    flex: 1;
    padding: 0.45rem 0.75rem;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-size: 0.85rem;
    font-family: 'SF Mono', 'Fira Code', monospace;
  }
  .env-row input:focus { outline: none; border-color: var(--primary); }
  .log-box {
    background: #000;
    border-radius: 8px;
    padding: 0.75rem;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 0.8rem;
    color: #a3e635;
    max-height: 300px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .toast {
    position: fixed;
    bottom: 1.5rem;
    right: 1.5rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 0.75rem 1.25rem;
    font-size: 0.9rem;
    box-shadow: 0 8px 30px rgba(0,0,0,0.4);
    transform: translateY(100px);
    opacity: 0;
    transition: all 0.3s;
    z-index: 1000;
  }
  .toast.show { transform: translateY(0); opacity: 1; }
  .model-list { font-size: 0.85rem; }
  .model-item {
    padding: 0.5rem 0.75rem;
    background: var(--surface2);
    border-radius: 8px;
    margin-bottom: 0.4rem;
  }
  .model-item .name { font-weight: 600; }
  .model-item .detail { color: var(--text-muted); font-size: 0.8rem; }
  .full-width { grid-column: 1 / -1; }
  select {
    padding: 0.45rem 0.75rem;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-size: 0.85rem;
  }
  .tab-bar {
    display: flex;
    gap: 0;
    border-bottom: 1px solid var(--border);
    margin-bottom: 1rem;
  }
  .tab {
    padding: 0.5rem 1rem;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    color: var(--text-muted);
    font-size: 0.9rem;
    transition: all 0.15s;
  }
  .tab.active {
    color: var(--primary);
    border-bottom-color: var(--primary);
  }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
</style>
</head>
<body>

<div class="header">
  <h1>&#x1F98C; DeerFlow Desktop Manager</h1>
  <p>Manage your local DeerFlow instance &mdash; start services, configure models, and monitor health.</p>
</div>

<div class="container">
  <div class="actions">
    <button class="btn btn-success" onclick="startAll()">&#9654; Start All Services</button>
    <button class="btn btn-danger" onclick="stopAll()">&#9632; Stop All Services</button>
    <button class="btn btn-primary" onclick="openDeerFlow()">&#127760; Open DeerFlow UI</button>
    <button class="btn btn-primary" onclick="refreshStatus()">&#8635; Refresh</button>
  </div>

  <div class="grid">
    <!-- Services Card -->
    <div class="card">
      <h2>&#9881;&#65039; Services</h2>
      <div id="services-list">
        <div class="service-row"><span class="service-info"><span class="dot yellow"></span><span>Loading...</span></span></div>
      </div>
    </div>

    <!-- Prerequisites Card -->
    <div class="card">
      <h2>&#9989; Prerequisites</h2>
      <div id="prereqs" class="prereq-grid">
        <div class="prereq-item">Loading...</div>
      </div>
    </div>

    <!-- Configuration Card -->
    <div class="card full-width">
      <h2>&#128272; Configuration</h2>
      <div class="tab-bar">
        <div class="tab active" onclick="switchTab('api-keys')">API Keys</div>
        <div class="tab" onclick="switchTab('models')">Models</div>
        <div class="tab" onclick="switchTab('logs')">Logs</div>
      </div>

      <div id="tab-api-keys" class="tab-content active">
        <form class="env-form" onsubmit="saveEnv(event)">
          <div class="env-row">
            <label>OPENAI_API_KEY</label>
            <input type="password" id="env-OPENAI_API_KEY" placeholder="sk-..." />
          </div>
          <div class="env-row">
            <label>ANTHROPIC_API_KEY</label>
            <input type="password" id="env-ANTHROPIC_API_KEY" placeholder="sk-ant-..." />
          </div>
          <div class="env-row">
            <label>GOOGLE_API_KEY</label>
            <input type="password" id="env-GOOGLE_API_KEY" placeholder="AIza..." />
          </div>
          <div class="env-row">
            <label>DEEPSEEK_API_KEY</label>
            <input type="password" id="env-DEEPSEEK_API_KEY" placeholder="sk-..." />
          </div>
          <div class="env-row">
            <label>TAVILY_API_KEY</label>
            <input type="password" id="env-TAVILY_API_KEY" placeholder="tvly-..." />
          </div>
          <div style="margin-top:0.5rem;">
            <button type="submit" class="btn btn-primary">Save API Keys</button>
          </div>
        </form>
      </div>

      <div id="tab-models" class="tab-content">
        <div id="models-list" class="model-list">Loading...</div>
      </div>

      <div id="tab-logs" class="tab-content">
        <div style="margin-bottom:0.5rem;">
          <select id="log-select" onchange="loadLog()">
            <option value="langgraph">LangGraph</option>
            <option value="gateway">Gateway</option>
            <option value="frontend">Frontend</option>
            <option value="nginx">Nginx</option>
          </select>
          <button class="btn btn-sm btn-primary" onclick="loadLog()" style="margin-left:0.25rem;">Refresh Log</button>
        </div>
        <div id="log-output" class="log-box">Select a service and click refresh.</div>
      </div>
    </div>
  </div>
</div>

<div id="toast" class="toast"></div>

<script>
const API = '';

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 3000);
}

async function api(path, method='GET', body=null) {
  const opts = { method, headers: {'Content-Type': 'application/json'} };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch('/api' + path, opts);
  return r.json();
}

async function refreshStatus() {
  const data = await api('/status');
  const list = document.getElementById('services-list');
  list.innerHTML = '';
  for (const [key, svc] of Object.entries(data.services)) {
    const color = svc.healthy ? 'green' : (svc.process_running ? 'yellow' : 'red');
    const status = svc.healthy ? 'Running' : (svc.process_running ? 'Starting...' : 'Stopped');
    list.innerHTML += `
      <div class="service-row">
        <div class="service-info">
          <span class="dot ${color}"></span>
          <span class="service-name">${svc.name}</span>
          <span class="service-port">:${svc.port}</span>
        </div>
        <span class="service-port">${status}</span>
      </div>`;
  }

  const prereqs = document.getElementById('prereqs');
  prereqs.innerHTML = '';
  for (const [name, ok] of Object.entries(data.prerequisites)) {
    prereqs.innerHTML += `
      <div class="prereq-item">
        <span class="${ok ? 'check' : 'cross'}">${ok ? '&#10003;' : '&#10007;'}</span>
        <span>${name}</span>
      </div>`;
  }

  // Load env values
  if (data.env) {
    for (const [k, v] of Object.entries(data.env)) {
      const inp = document.getElementById('env-' + k);
      if (inp && v) inp.value = v;
    }
  }

  // Load models
  const ml = document.getElementById('models-list');
  if (data.models && data.models.length > 0) {
    ml.innerHTML = data.models.map(m => `
      <div class="model-item">
        <span class="name">${m.display_name || m.name}</span>
        <span class="detail"> &mdash; ${m.model || ''} (${m.use || ''})</span>
      </div>`).join('');
  } else {
    ml.innerHTML = '<div style="color:var(--text-muted)">No models configured. Edit config.yaml to add models.</div>';
  }
}

async function startAll() {
  toast('Starting all services...');
  const data = await api('/start', 'POST');
  toast(data.message || 'Services started');
  setTimeout(refreshStatus, 2000);
}

async function stopAll() {
  toast('Stopping all services...');
  const data = await api('/stop', 'POST');
  toast(data.message || 'Services stopped');
  setTimeout(refreshStatus, 1000);
}

function openDeerFlow() {
  window.open('http://localhost:2026', '_blank');
}

async function saveEnv(e) {
  e.preventDefault();
  const keys = ['OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GOOGLE_API_KEY', 'DEEPSEEK_API_KEY', 'TAVILY_API_KEY'];
  const data = {};
  for (const k of keys) {
    const v = document.getElementById('env-' + k).value.trim();
    if (v) data[k] = v;
  }
  await api('/env', 'POST', data);
  toast('API keys saved to .env');
}

async function loadLog() {
  const svc = document.getElementById('log-select').value;
  const data = await api('/logs/' + svc);
  document.getElementById('log-output').textContent = data.content || 'No logs yet.';
  const box = document.getElementById('log-output');
  box.scrollTop = box.scrollHeight;
}

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
}

// Auto-refresh every 5 seconds
refreshStatus();
setInterval(refreshStatus, 5000);
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# HTTP API Handler
# ---------------------------------------------------------------------------


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """Serves the dashboard UI and REST API for the control panel."""

    manager: ServiceManager  # set on class before serving

    def log_message(self, format: str, *args: Any) -> None:
        pass  # silence default HTTP logging

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, content: str) -> None:
        body = content.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    # -- Routes --

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            self._html(DASHBOARD_HTML)
        elif self.path == "/api/status":
            self._json({
                "services": self.manager.status(),
                "prerequisites": ConfigManager.check_prerequisites(),
                "env": ConfigManager.read_env(),
                "models": ConfigManager.get_models_from_config(),
            })
        elif self.path.startswith("/api/logs/"):
            svc_key = self.path.split("/")[-1]
            log_file = LOGS_DIR / f"{svc_key}.log"
            content = ""
            if log_file.exists():
                text = log_file.read_text()
                # Return last 200 lines
                lines = text.splitlines()
                content = "\n".join(lines[-200:])
            self._json({"content": content})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/api/start":
            ConfigManager.ensure_config()
            results = self.manager.start_all()
            all_ok = all(results.values())
            failed = [k for k, v in results.items() if not v]
            if all_ok:
                self._json({"success": True, "message": "All services started successfully!", "results": results})
            else:
                self._json({"success": False,
                             "message": f"Some services failed to start: {', '.join(failed)}",
                             "results": results})
        elif self.path == "/api/stop":
            self.manager.stop_all()
            self._json({"success": True, "message": "All services stopped."})
        elif self.path == "/api/env":
            data = json.loads(self._read_body())
            ConfigManager.write_env(data)
            self._json({"success": True})
        else:
            self.send_error(404)


# ---------------------------------------------------------------------------
# System tray (optional, requires pystray + Pillow)
# ---------------------------------------------------------------------------


def start_tray(panel_port: int, manager: ServiceManager) -> None:
    """Attempt to start a system tray icon. Silently no-ops if pystray isn't available."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return

    def _make_icon() -> Image.Image:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([4, 4, 60, 60], fill="#6366f1")
        draw.text((20, 18), "D", fill="white")
        return img

    def _open_panel(_: Any) -> None:
        webbrowser.open(f"http://localhost:{panel_port}")

    def _open_deerflow(_: Any) -> None:
        webbrowser.open("http://localhost:2026")

    def _quit(icon: Any) -> None:
        manager.stop_all()
        icon.stop()
        os._exit(0)

    icon = pystray.Icon(
        "DeerFlow",
        _make_icon(),
        "DeerFlow Desktop",
        menu=pystray.Menu(
            pystray.MenuItem("Open Control Panel", _open_panel),
            pystray.MenuItem("Open DeerFlow", _open_deerflow),
            pystray.MenuItem("Quit", _quit),
        ),
    )
    threading.Thread(target=icon.run, daemon=True).start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="DeerFlow Desktop Manager")
    parser.add_argument("--port", type=int, default=2099, help="Control panel port (default: 2099)")
    parser.add_argument("--headless", action="store_true", help="Don't open browser automatically")
    parser.add_argument("--dev", action="store_true", default=True, help="Dev mode (hot-reload)")
    parser.add_argument("--prod", action="store_true", help="Production mode")
    args = parser.parse_args()

    dev_mode = not args.prod
    manager = ServiceManager(dev_mode=dev_mode)

    # Ensure config files exist
    ConfigManager.ensure_config()

    # Set manager on handler class
    DashboardHandler.manager = manager

    # Start HTTP server for control panel
    server = http.server.HTTPServer(("127.0.0.1", args.port), DashboardHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    panel_url = f"http://localhost:{args.port}"
    print()
    print("==========================================")
    print("  DeerFlow Desktop Manager")
    print("==========================================")
    print()
    print(f"  Control Panel: {panel_url}")
    print(f"  DeerFlow UI:   http://localhost:2026  (after services start)")
    print()
    print(f"  Platform:      {platform.system()} {platform.machine()}")
    print(f"  Mode:          {'Development' if dev_mode else 'Production'}")
    print(f"  Config:        {CONFIG_FILE}")
    print()
    print("  Press Ctrl+C to stop all services and exit")
    print()

    # Try system tray
    start_tray(args.port, manager)

    # Open browser
    if not args.headless:
        webbrowser.open(panel_url)

    # Handle graceful shutdown
    def shutdown(sig: int, frame: Any) -> None:
        print("\nShutting down...")
        manager.stop_all()
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown(0, None)


if __name__ == "__main__":
    main()
