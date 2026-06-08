#!/usr/bin/env bash
# Skill setup script — runs once at install time to prepare runtime dependencies.
# This avoids repeated Pillow installation during each generation session.

set -e

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[restaurant-marketing-material] Installing runtime dependencies..."

if command -v uv &>/dev/null; then
    uv pip install --system -r "$SKILL_DIR/requirements.txt" 2>/dev/null \
        || uv pip install -r "$SKILL_DIR/requirements.txt"
elif command -v pip3 &>/dev/null; then
    pip3 install --break-system-packages -r "$SKILL_DIR/requirements.txt" 2>/dev/null \
        || pip3 install -r "$SKILL_DIR/requirements.txt"
elif command -v pip &>/dev/null; then
    pip install -r "$SKILL_DIR/requirements.txt"
else
    echo "[WARN] No pip/uv found. Please manually install: Pillow>=10"
    exit 0
fi

echo "[restaurant-marketing-material] Setup complete."
