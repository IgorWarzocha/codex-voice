#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${HOME}/.local/bin"
BIN_PATH="${BIN_DIR}/codex-voice"
HYPR_DIR="${HOME}/.config/hypr/conf.d"
HYPR_CONF="${HYPR_DIR}/codex-voice.conf"

mkdir -p "${BIN_DIR}" "${HYPR_DIR}"

cat > "${BIN_PATH}" <<EOF
#!/usr/bin/env bash
exec python3 "${ROOT_DIR}/linux/codex_voice.py" "\$@"
EOF
chmod +x "${BIN_PATH}"

cat > "${HYPR_CONF}" <<'EOF'
# Codex Voice for Omarchy / Hyprland
# Hold Control+M to record, release to transcribe and insert.
bind = CTRL, M, exec, codex-voice start
bindr = CTRL, M, exec, codex-voice stop

# Optional manual cancel.
bind = CTRL SHIFT, M, exec, codex-voice cancel
EOF

if command -v hyprctl >/dev/null 2>&1; then
  hyprctl reload >/dev/null
fi

printf 'Installed wrapper: %s\n' "${BIN_PATH}"
printf 'Installed Hyprland config: %s\n' "${HYPR_CONF}"
printf 'Probe result:\n'
"${BIN_PATH}" probe
