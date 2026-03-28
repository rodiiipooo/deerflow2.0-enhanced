# DeerFlow Desktop Manager

A desktop tool that lets you run and manage DeerFlow directly on your PC through a graphical control panel.

## Features

- **One-click service management** -- Start/stop all DeerFlow services (LangGraph, Gateway, Frontend, Nginx) with a single click
- **Web-based control panel** -- Beautiful dashboard at `http://localhost:2099` for monitoring and configuration
- **API key management** -- Configure OpenAI, Anthropic, Google, DeepSeek, and Tavily API keys through the UI
- **Live service monitoring** -- Real-time health status for all services with auto-refresh
- **Log viewer** -- View service logs directly in the control panel
- **Prerequisite checker** -- Verifies all required tools are installed
- **System tray icon** -- Optional tray integration for quick access (requires `pystray`)
- **Cross-platform** -- Works on Linux, macOS, and Windows

## Quick Start

### 1. Run setup (first time only)

```bash
python desktop/setup.py
```

This will:
- Check prerequisites (Python 3.12+, uv, Node 22+, pnpm, nginx)
- Create configuration files from examples
- Install backend and frontend dependencies
- Create a desktop shortcut for your OS

### 2. Configure API keys

Edit `.env` in the project root, or use the control panel UI after launching:

```bash
OPENAI_API_KEY=sk-your-key-here
TAVILY_API_KEY=tvly-your-key-here
```

### 3. Configure models

Edit `config.yaml` to uncomment and configure at least one model. See the main project README for model configuration details.

### 4. Launch the Desktop Manager

```bash
python desktop/deerflow_desktop.py
```

The control panel opens automatically at `http://localhost:2099`. From there:
1. Click **Start All Services** to boot everything up
2. Click **Open DeerFlow UI** to access the main application at `http://localhost:2026`

## Command Line Options

```
python desktop/deerflow_desktop.py [OPTIONS]

Options:
  --port PORT     Control panel port (default: 2099)
  --headless      Don't open browser automatically
  --prod          Run services in production mode (no hot-reload)
  --dev           Run services in development mode (default)
```

## Architecture

```
Desktop Manager (port 2099)
  ├── Web Dashboard (HTML/JS control panel)
  ├── REST API (/api/status, /api/start, /api/stop, /api/env, /api/logs/*)
  ├── ServiceManager (subprocess management for all DeerFlow services)
  ├── ConfigManager (read/write config.yaml, .env, extensions_config.json)
  └── System Tray (optional, via pystray)

Managed Services:
  ├── LangGraph Server (port 2024)
  ├── Gateway API (port 8001)
  ├── Frontend / Next.js (port 3000)
  └── Nginx Reverse Proxy (port 2026) → unified entry point
```

## Control Panel API

The desktop manager exposes a simple REST API:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Service status, prerequisites, env vars, models |
| `/api/start` | POST | Start all services |
| `/api/stop` | POST | Stop all services |
| `/api/env` | POST | Save API keys to `.env` |
| `/api/logs/{service}` | GET | Get last 200 lines of a service log |

## Optional: System Tray

For a system tray icon with quick-access menu:

```bash
pip install pystray Pillow
```

The tray icon provides:
- Open Control Panel
- Open DeerFlow UI
- Quit (stops all services)

## Files

```
desktop/
├── deerflow_desktop.py   # Main desktop manager application
├── setup.py              # First-time setup script
└── README.md             # This file
```

## Troubleshooting

**Services fail to start**: Check the Logs tab in the control panel for error details. Common issues:
- Missing API keys in `.env`
- No models configured in `config.yaml`
- Port already in use (check with `lsof -i :2024`)

**Nginx won't start**: Ensure nginx is installed and the config at `docker/nginx/nginx.local.conf` exists.

**Frontend takes a long time**: The Next.js dev server can take 30-60 seconds on first start while it compiles.

**Control panel doesn't open**: Manually navigate to `http://localhost:2099` in your browser.
