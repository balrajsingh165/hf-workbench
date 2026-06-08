#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SOURCE_SKILL="${SCRIPT_DIR}/SKILL.md"

if [[ ! -f "${SOURCE_SKILL}" ]]; then
  echo "Missing source skill file: ${SOURCE_SKILL}" >&2
  exit 1
fi

# .agents/skills and .factory/skills are symlinks to skills/, so they
# already see the canonical SKILL.md. Only .claude/skills needs a copy.
TARGET_ROOTS=(
  "${PROJECT_ROOT}/.claude"
)

echo "Installing agent-journal skill from ${SOURCE_SKILL}"
echo "(.agents/skills and .factory/skills are symlinks to skills/ — no copy needed)"

for root in "${TARGET_ROOTS[@]}"; do
  target_dir="${root}/skills/agent-journal"
  mkdir -p "${target_dir}"
  cp "${SOURCE_SKILL}" "${target_dir}/SKILL.md"
  echo "Installed ${target_dir}/SKILL.md"
done
