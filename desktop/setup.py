#!/usr/bin/env python3
"""
DeerFlow Desktop Setup
======================
Sets up DeerFlow for local desktop use:
  1. Checks prerequisites (Python, uv, Node, pnpm, nginx)
  2. Creates config files from examples
  3. Installs backend + frontend dependencies
  4. Creates a desktop shortcut (Linux .desktop / macOS .command / Windows .bat)
  5. Optionally installs pystray for system tray support

Usage:
    python desktop/setup.py           # Interactive setup
    python desktop/setup.py --skip-deps   # Skip dependency installation
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_DIR = REPO_ROOT / "frontend"

# ANSI colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def info(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}!{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}✗{RESET} {msg}")


def header(msg: str) -> None:
    print(f"\n{BOLD}{CYAN}{msg}{RESET}")


def run(cmd: list[str], cwd: str | Path | None = None, check: bool = True) -> bool:
    try:
        subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def check_prerequisites() -> dict[str, bool]:
    header("Checking prerequisites...")
    checks = {}
    for name, cmd in [
        ("python3", ["python3", "--version"]),
        ("uv", ["uv", "--version"]),
        ("node", ["node", "--version"]),
        ("pnpm", ["pnpm", "--version"]),
        ("nginx", ["nginx", "-v"]),
    ]:
        ok = shutil.which(name) is not None
        checks[name] = ok
        if ok:
            info(f"{name} found")
        else:
            fail(f"{name} not found")
    return checks


def setup_config() -> None:
    header("Setting up configuration files...")

    config_yaml = REPO_ROOT / "config.yaml"
    config_example = REPO_ROOT / "config.example.yaml"
    if not config_yaml.exists() and config_example.exists():
        shutil.copy(config_example, config_yaml)
        info("Created config.yaml from example")
    elif config_yaml.exists():
        info("config.yaml already exists")
    else:
        fail("config.example.yaml not found")

    ext_config = REPO_ROOT / "extensions_config.json"
    ext_example = REPO_ROOT / "extensions_config.example.json"
    if not ext_config.exists() and ext_example.exists():
        shutil.copy(ext_example, ext_config)
        info("Created extensions_config.json from example")
    elif ext_config.exists():
        info("extensions_config.json already exists")

    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        env_file.write_text(
            "# DeerFlow Environment Variables\n"
            "# Add your API keys here:\n"
            "#OPENAI_API_KEY=your-key-here\n"
            "#ANTHROPIC_API_KEY=your-key-here\n"
            "#TAVILY_API_KEY=your-key-here\n"
        )
        info("Created .env template")
    else:
        info(".env already exists")


def install_dependencies(skip: bool = False) -> None:
    if skip:
        warn("Skipping dependency installation (--skip-deps)")
        return

    header("Installing backend dependencies...")
    if run(["uv", "sync"], cwd=BACKEND_DIR):
        info("Backend dependencies installed")
    else:
        fail("Failed to install backend dependencies")

    header("Installing frontend dependencies...")
    if run(["pnpm", "install"], cwd=FRONTEND_DIR):
        info("Frontend dependencies installed")
    else:
        fail("Failed to install frontend dependencies")


def create_launcher() -> Path | None:
    header("Creating desktop launcher...")

    system = platform.system()
    python = sys.executable
    script = SCRIPT_DIR / "deerflow_desktop.py"

    if system == "Linux":
        # Create .desktop file
        desktop_dir = Path.home() / ".local" / "share" / "applications"
        desktop_dir.mkdir(parents=True, exist_ok=True)
        desktop_file = desktop_dir / "deerflow-desktop.desktop"
        desktop_file.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=DeerFlow Desktop\n"
            "Comment=DeerFlow AI Agent Desktop Manager\n"
            f"Exec={python} {script}\n"
            "Terminal=true\n"
            "Categories=Development;Utility;\n"
            "StartupNotify=true\n"
        )
        info(f"Created Linux .desktop entry: {desktop_file}")

        # Also create a shell launcher in the repo
        launcher = REPO_ROOT / "deerflow-desktop.sh"
        launcher.write_text(
            "#!/usr/bin/env bash\n"
            f'cd "{REPO_ROOT}"\n'
            f'exec {python} "{script}" "$@"\n'
        )
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
        info(f"Created shell launcher: {launcher}")
        return launcher

    elif system == "Darwin":
        launcher = REPO_ROOT / "DeerFlow Desktop.command"
        launcher.write_text(
            "#!/usr/bin/env bash\n"
            f'cd "{REPO_ROOT}"\n'
            f'exec {python} "{script}" "$@"\n'
        )
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
        info(f"Created macOS launcher: {launcher}")
        return launcher

    elif system == "Windows":
        launcher = REPO_ROOT / "DeerFlow Desktop.bat"
        launcher.write_text(
            f'@echo off\n'
            f'cd /d "{REPO_ROOT}"\n'
            f'"{python}" "{script}" %*\n'
        )
        info(f"Created Windows launcher: {launcher}")
        return launcher

    else:
        warn(f"Unknown platform: {system}. Create a manual launcher.")
        return None


def install_tray_support() -> None:
    header("System tray support (optional)...")
    try:
        import pystray  # noqa: F401
        info("pystray already installed")
    except ImportError:
        warn("pystray not installed. System tray icon will be disabled.")
        print(f"    To enable it: pip install pystray Pillow")


def main() -> None:
    skip_deps = "--skip-deps" in sys.argv

    print()
    print(f"{BOLD}==========================================")
    print(f"  DeerFlow Desktop Setup")
    print(f"=========================================={RESET}")

    prereqs = check_prerequisites()
    missing = [k for k, v in prereqs.items() if not v]
    if missing:
        print()
        warn(f"Missing prerequisites: {', '.join(missing)}")
        warn("Install them before running DeerFlow.")
        if "uv" in missing:
            print(f"    Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh")
        if "pnpm" in missing:
            print(f"    Install pnpm: npm install -g pnpm")
        if "nginx" in missing:
            print(f"    Install nginx: sudo apt install nginx  (or brew install nginx)")

    setup_config()
    install_dependencies(skip=skip_deps)
    launcher = create_launcher()
    install_tray_support()

    print()
    print(f"{BOLD}{GREEN}==========================================")
    print(f"  Setup Complete!")
    print(f"=========================================={RESET}")
    print()
    print(f"  Next steps:")
    print(f"    1. Edit {REPO_ROOT / '.env'} to add your API keys")
    print(f"    2. Edit {REPO_ROOT / 'config.yaml'} to configure models")
    print(f"    3. Run the desktop manager:")
    print()
    if launcher:
        print(f"       {launcher}")
    print(f"       # or: python {SCRIPT_DIR / 'deerflow_desktop.py'}")
    print()


if __name__ == "__main__":
    main()
