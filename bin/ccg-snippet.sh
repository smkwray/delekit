# Paste into ~/.zshrc or ~/.bashrc.
# Defines ccg: the same Claude Code launch as your normal alias, but routed
# through the local CLIProxyAPI gateway so one session can mix model families
# (Claude parent + tandy-* subagents on the GPT profiles).
#
# Leave your existing alias untouched: it stays the direct-to-Anthropic fallback,
# and keeps claude.ai connectors working.
#
# ccg is a SUBSHELL function -- ccg() ( ... ), not ccg() { ... }. The gateway
# variables must not survive the call, or a later plain `claude` in the same
# terminal keeps routing through the proxy without saying so.
#
# device.env lives outside the synced kit. Both platform paths are probed:
#   macOS/Linux:        ${XDG_CONFIG_HOME:-$HOME/.config}/delekit/device.env
#   Windows (git-bash): $HOME/AppData/Local/delekit/device.env

ccg() (
  # A stray API key would outrank the gateway token; drop it for this launch.
  unset -v ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL \
           ANTHROPIC_MODEL ANTHROPIC_SMALL_FAST_MODEL \
           ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL \
           ANTHROPIC_DEFAULT_HAIKU_MODEL CLAUDE_CODE_SUBAGENT_MODEL

  # claudex parses device.env strictly and applies the gateway defaults.
  if command -v claudex >/dev/null 2>&1; then
    exec claudex --dangerously-skip-permissions "$@"
  fi

  _dev="${DELEGATE_DEVICE_ENV:-${XDG_CONFIG_HOME:-$HOME/.config}/delekit/device.env}"
  [ -f "$_dev" ] || _dev="$HOME/AppData/Local/delekit/device.env"
  if [ -f "$_dev" ]; then
    set -a
    . "$_dev"
    set +a
  else
    echo "ccg: delekit device.env not found; launching without the gateway." >&2
  fi
  exec claude --dangerously-skip-permissions "$@"
)
