#!/usr/bin/env bash

set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

printf '\n=== CAREEROS PROJECT ===\n'
printf 'Location: %s\n' "$PROJECT_DIR"

printf '\n=== DISK SPACE ===\n'
df -h "$PROJECT_DIR"

printf '\n=== SYSTEM PYTHON ===\n'
python3 --version 2>/dev/null || echo "python3 is missing"

printf '\n=== GIT ===\n'
git --version 2>/dev/null || echo "git is missing"

printf '\n=== REPOSITORY STATUS ===\n'
git -C "$PROJECT_DIR" status --short

printf '\n=== DIRECTORY TREE ===\n'
find "$PROJECT_DIR" -maxdepth 3 \
    -path "$PROJECT_DIR/.git" -prune -o \
    -print |
    sed "s|$PROJECT_DIR|.|" |
    sort
