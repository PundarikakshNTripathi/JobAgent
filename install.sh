#!/usr/bin/env bash
# JobAgent installer for Linux and macOS.
#   curl -fsSL https://raw.githubusercontent.com/PundarikakshNTripathi/JobAgent/main/install.sh | bash
# Re-running it updates the code and keeps your data. Unattended: JOBAGENT_DEFAULTS=1 (accept every default).
# Code goes to ~/.jobagent/app (override JOBAGENT_DIR); your personal workspace defaults to ~/JobAgent (override JOBAGENT_HOME).
set -euo pipefail
REPO="${JOBAGENT_REPO:-https://github.com/PundarikakshNTripathi/JobAgent.git}"
APP="${JOBAGENT_DIR:-$HOME/.jobagent/app}"
DEFAULTS="${JOBAGENT_DEFAULTS:-0}"
say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
TTY=/dev/tty; { [ -r /dev/tty ] && [ "$DEFAULTS" != 1 ]; } || TTY=""
ask() { # ask "question" default
  if [ -z "$TTY" ]; then echo "$2"; return; fi
  local a; read -r -p "$1 [$2]: " a < "$TTY" || true; echo "${a:-$2}"
}
have() { command -v "$1" >/dev/null 2>&1; }
pkg_install() { # best effort, asks first
  local what="$1"
  if [ "$(ask "Install $what with your package manager? (y/n)" y)" != y ]; then return 1; fi
  local SUDO="sudo"; [ -n "$TTY" ] || SUDO="sudo -n"   # unattended: never hang on a password prompt
  [ "$(id -u)" = 0 ] && SUDO=""
  if have brew; then brew install "$what"
  elif have apt-get; then $SUDO apt-get update -y -qq && $SUDO apt-get install -y -qq "$what"
  elif have dnf; then $SUDO dnf install -y "$what"
  elif have pacman; then $SUDO pacman -S --noconfirm "$what"
  elif have zypper; then $SUDO zypper install -y "$what"
  else return 1; fi
}

say "JobAgent: Find the role. Reach the people. You press Submit."
say "Installer for $(uname -s) $(uname -m)"
# 1. Python 3.10+
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if have "$c" && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then PY="$(command -v "$c")"; break; fi
done
if [ -z "$PY" ]; then
  warn "Python 3.10 or newer is needed."
  if [ "$(uname -s)" = Darwin ]; then pkg_install python@3.12 || true; else pkg_install python3 || true; pkg_install python3-venv || true; fi
  PY="$(command -v python3 || true)"
  [ -n "$PY" ] || { warn "Install Python 3.10+ (https://www.python.org/downloads/) and run this again."; exit 1; }
fi
say "Python: $PY ($("$PY" -V 2>&1))"
# 2. Code
if have git; then
  if [ -d "$APP/.git" ]; then say "Updating $APP"; git -C "$APP" pull --ff-only; else say "Downloading to $APP"; mkdir -p "$(dirname "$APP")"; git clone --depth 1 "$REPO" "$APP"; fi
else
  say "git not found; downloading a zip"; mkdir -p "$APP"
  curl -fsSL "${REPO%.git}/archive/refs/heads/main.tar.gz" | tar -xz --strip-components=1 -C "$APP"
fi
cd "$APP"
# 3. Virtual environment + packages
if [ ! -x .venv/bin/python ]; then
  # Debian/Ubuntu ship Python without ensurepip unless python3-venv is installed: fall back to a pip-less venv + get-pip (no root needed)
  "$PY" -m venv .venv 2>/dev/null || { rm -rf .venv; "$PY" -m venv --without-pip .venv && curl -fsSL https://bootstrap.pypa.io/get-pip.py | .venv/bin/python - -q; }
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
# 4. A browser for LinkedIn / YC / job boards / form filling
if ! { have google-chrome || have google-chrome-stable || have chromium || have chromium-browser || have microsoft-edge || [ -d "/Applications/Google Chrome.app" ] || [ -d "/Applications/Chromium.app" ]; }; then
  say "No Chrome/Chromium found: installing Playwright's Chromium (used only by JobAgent)"
  if [ "$(uname -s)" = Linux ]; then
    # Playwright's driver and Chromium need a few system libraries that minimal distros lack (root needed, asked first)
    { ldconfig -p 2>/dev/null | grep -q libatomic.so.1; } || pkg_install libatomic1 || warn "libatomic1 missing: install it with your package manager"
    if [ "$(ask "Install Chromium's system libraries (uses sudo)? (y/n)" y)" = y ]; then
      SUDO="sudo"; [ -n "$TTY" ] || SUDO="sudo -n"; [ "$(id -u)" = 0 ] && SUDO=""
      $SUDO "$APP/.venv/bin/python" -m playwright install-deps chromium || warn "Could not install browser libraries; run: sudo $APP/.venv/bin/python -m playwright install-deps chromium"
    fi
  fi
  .venv/bin/python -m playwright install chromium || warn "Chromium download failed; install Google Chrome (or Chromium) and re-run"
fi
have node || warn "Node.js not found (optional: only for the fallback browser mode). https://nodejs.org"
# 5. Workspace location
if [ -z "${JOBAGENT_HOME:-}" ] && [ ! -f .jobagent_home ]; then
  WS="$(ask "Where should your personal workspace live" "$HOME/JobAgent")"
  printf '%s\n' "$WS" > .jobagent_home
fi
# 6. Launcher: `jobagent <command>` (gui, doctor, cycle, ...)
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/jobagent" <<EOF
#!/usr/bin/env bash
exec "$APP/.venv/bin/python" "$APP/agent/run.py" "\${@:-app}"
EOF
chmod +x "$HOME/.local/bin/jobagent"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "Add ~/.local/bin to your PATH to use the 'jobagent' command (e.g. echo 'export PATH=\$HOME/.local/bin:\$PATH' >> ~/.bashrc)";; esac
# 7. Guided setup
say "Setup"
if [ -n "$TTY" ]; then .venv/bin/python agent/run.py init < "$TTY"; else .venv/bin/python agent/run.py init --defaults; fi
.venv/bin/python agent/run.py doctor || true
# 8. Desktop app: app-launcher entry (Linux) or ~/Applications/JobAgent.app (macOS) that opens JobAgent in its own window
.venv/bin/python agent/run.py shortcuts || warn "Could not add the app shortcut; open it with: jobagent app"
say "Done. Open JobAgent from your app launcher, or run:  jobagent      (or: $APP/.venv/bin/python $APP/agent/run.py app)"
