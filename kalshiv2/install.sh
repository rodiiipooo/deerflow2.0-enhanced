#!/usr/bin/env bash
# Install KalshiV2 dependencies
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Installing KalshiV2 dependencies..."
pip install -r "$SCRIPT_DIR/requirements.txt"
echo ""
echo "Done! Run from the project root:"
echo "  python -m kalshiv2 status"
echo "  python -m kalshiv2 --demo"
echo "  python -m kalshiv2 --dry-run"
