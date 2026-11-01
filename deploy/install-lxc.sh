#!/usr/bin/env bash
# Installs CubeBar and its dependencies into a Debian/Ubuntu LXC container.
# Run as root inside the container:  bash cubebar/deploy/install-lxc.sh
#
# Optional environment:
#   CODEXBAR_VERSION  CodexBar CLI release to install (default below)
#   TZ_NAME           timezone for the clock on the cube, e.g. Asia/Tbilisi
set -euo pipefail

CODEXBAR_VERSION="${CODEXBAR_VERSION:-0.83.0}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|aarch64) ;;
  *) echo "unsupported architecture: $ARCH" >&2; exit 1 ;;
esac
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

echo "==> system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  python3 python3-pil curl ca-certificates fonts-dejavu-core tar tzdata >/dev/null

if [[ -n "${TZ_NAME:-}" ]]; then
  ln -sf "/usr/share/zoneinfo/$TZ_NAME" /etc/localtime
  echo "$TZ_NAME" > /etc/timezone
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "==> CodexBar CLI v$CODEXBAR_VERSION ($ARCH)"
# Static musl build: the glibc one needs glibc >= 2.38, newer than Debian 12 ships.
name="CodexBarCLI-v${CODEXBAR_VERSION}-linux-musl-${ARCH}.tar.gz"
base="https://github.com/steipete/CodexBar/releases/download/v${CODEXBAR_VERSION}"
curl -fsSL -o "$tmp/$name" "$base/$name"
curl -fsSL -o "$tmp/$name.sha256" "$base/$name.sha256"
(cd "$tmp" && sha256sum --quiet -c "$name.sha256")
rm -rf /opt/codexbar
mkdir -p /opt/codexbar
tar -xzf "$tmp/$name" -C /opt/codexbar
# The binary looks for its resource bundle next to its real path, so a symlink is enough.
ln -sf /opt/codexbar/codexbar /usr/local/bin/codexbar

# Codex CLI owns ~/.codex/auth.json: CodexBar asks it to refresh expired tokens.
echo "==> Codex CLI"
codex_name="codex-${ARCH}-unknown-linux-musl"
curl -fsSL -o "$tmp/codex.tar.gz" \
  "https://github.com/openai/codex/releases/latest/download/${codex_name}.tar.gz"
tar -xzf "$tmp/codex.tar.gz" -C "$tmp"
install -m 755 "$tmp/$codex_name" /usr/local/bin/codex

# Claude Code owns ~/.claude/.credentials.json the same way.
echo "==> Claude Code"
if ! command -v claude >/dev/null && [[ ! -x "$HOME/.local/bin/claude" ]]; then
  curl -fsSL https://claude.ai/install.sh | bash
fi
grep -qs 'HOME/.local/bin' "$HOME/.bashrc" || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"

echo "==> CubeBar"
mkdir -p /opt/cubebar
install -m 755 "$SRC_DIR/cubebar.py" /opt/cubebar/cubebar.py

codexbar config enable --provider codex >/dev/null
codexbar config enable --provider claude >/dev/null

cat <<EOF

Done. Next steps (as $(whoami) in this container):

  1. Sign in (each prints a link/code to open in a browser on any device):
       claude                       # then /login, then /exit (new shell, or: source ~/.bashrc)
       codex login --device-auth

  2. Check the data:
       codexbar usage

  3. Check the cube and start the service:
       python3 /opt/cubebar/cubebar.py info --host <cube-ip>
       python3 /opt/cubebar/cubebar.py install-service --host <cube-ip> --mirror
       journalctl -u cubebar -f
EOF
