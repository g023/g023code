#!/usr/bin/env bash
#
# g023 Code — setup for Linux, macOS and WSL.
#
# Drop the project in a folder, run this from that folder, and it brings the
# machine up to a working install. Every step checks what is already true and
# does only the part that is missing, so re-running it is both safe and the
# normal way to repair a half-finished setup.
#
#   ./installer.sh                 interactive, recommended
#   ./installer.sh --yes           accept every default, no questions
#   ./installer.sh --key sk-...    supply the API key non-interactively
#   ./installer.sh --uninstall     undo the PATH entry and launcher shim
#
# Nothing outside this folder is touched except the launcher shim in
# ~/.local/bin and, if you agree, one guarded block in your shell rc.

# No -e: every step decides for itself whether a failure is fatal, and several
# checks are expected to fail. No -u either — macOS still ships bash 3.2, where
# an empty array under -u is an error rather than an empty expansion.
set -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${HERE}/.venv"
BIN_DIR="${HOME}/.local/bin"
SHIM="${BIN_DIR}/g023"
MIN_MAJOR=3
MIN_MINOR=11

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
    C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_BAD=$'\033[31m'; C_ACC=$'\033[36m'
else
    C_RESET=""; C_DIM=""; C_BOLD=""; C_OK=""; C_WARN=""; C_BAD=""; C_ACC=""
fi

STEP=0
say()   { printf '%s\n' "$*"; }
step()  { STEP=$((STEP + 1)); printf '\n%s[%d]%s %s%s%s\n' "$C_ACC" "$STEP" "$C_RESET" "$C_BOLD" "$*" "$C_RESET"; }
ok()    { printf '    %s✓%s %s\n' "$C_OK"   "$C_RESET" "$*"; }
skip()  { printf '    %s·%s %s%s%s\n' "$C_DIM" "$C_RESET" "$C_DIM" "$*" "$C_RESET"; }
warn()  { printf '    %s!%s %s\n' "$C_WARN" "$C_RESET" "$*"; }
fail()  { printf '    %s✗%s %s\n' "$C_BAD"  "$C_RESET" "$*"; }
die()   { printf '\n%sSetup stopped.%s %s\n' "$C_BAD" "$C_RESET" "$*" >&2; exit 1; }

WARNINGS=()
note_warning() { WARNINGS+=("$1"); }

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

ASSUME_YES=0
WANT_VENV=auto      # auto | yes | no
API_KEY=""
DO_PATH=1
DO_OPTIONAL=auto    # auto | yes | no
UNINSTALL=0

usage() {
    cat <<'EOF'
g023 Code installer

  ./installer.sh [options]

  -y, --yes            non-interactive; take the recommended default everywhere
      --key KEY        write KEY into K.dat instead of prompting
      --venv           always create a virtualenv in ./.venv
      --no-venv        never create one; install into the current interpreter
      --with-optional  also install the optional extras (vision, browser TLS)
      --no-optional    skip the optional extras
      --no-path        do not add a launcher to ~/.local/bin or touch shell rc
      --uninstall      remove the launcher shim and the shell rc block
  -h, --help           this message

Re-running is safe: each step is skipped when it is already done.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes)        ASSUME_YES=1 ;;
        --key)           shift; API_KEY="${1:-}" ;;
        --key=*)         API_KEY="${1#*=}" ;;
        --venv)          WANT_VENV=yes ;;
        --no-venv)       WANT_VENV=no ;;
        --with-optional) DO_OPTIONAL=yes ;;
        --no-optional)   DO_OPTIONAL=no ;;
        --no-path)       DO_PATH=0 ;;
        --uninstall)     UNINSTALL=1 ;;
        -h|--help)       usage; exit 0 ;;
        *)               usage; die "Unknown option: $1" ;;
    esac
    shift
done

# ask <prompt> <default y|n> — honours --yes and non-tty stdin.
ask() {
    local prompt="$1" default="$2" reply hint
    if [ "$ASSUME_YES" = "1" ] || [ ! -t 0 ]; then
        [ "$default" = "y" ]
        return
    fi
    [ "$default" = "y" ] && hint="[Y/n]" || hint="[y/N]"
    printf '    %s %s ' "$prompt" "$hint"
    read -r reply || reply=""
    reply="$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')"   # bash 3.2 has no ${x,,}
    [ -z "$reply" ] && reply="$default"
    [ "$reply" = "y" ] || [ "$reply" = "yes" ]
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

RC_MARK_BEGIN="# >>> g023 code >>>"
RC_MARK_END="# <<< g023 code <<<"

shell_rc() {
    case "$(basename "${SHELL:-/bin/bash}")" in
        zsh)  printf '%s\n' "${HOME}/.zshrc" ;;
        bash) [ -f "${HOME}/.bash_profile" ] && [ ! -f "${HOME}/.bashrc" ] \
                 && printf '%s\n' "${HOME}/.bash_profile" \
                 || printf '%s\n' "${HOME}/.bashrc" ;;
        fish) printf '%s\n' "${HOME}/.config/fish/config.fish" ;;
        *)    printf '%s\n' "${HOME}/.profile" ;;
    esac
}

if [ "$UNINSTALL" = "1" ]; then
    say "${C_BOLD}g023 Code — uninstalling the system hooks${C_RESET}"
    step "Launcher shim"
    if [ -e "$SHIM" ]; then rm -f "$SHIM" && ok "removed ${SHIM}"; else skip "no shim at ${SHIM}"; fi
    step "Shell rc"
    rc="$(shell_rc)"
    if [ -f "$rc" ] && grep -qF "$RC_MARK_BEGIN" "$rc"; then
        tmp="$(mktemp)"
        sed "/${RC_MARK_BEGIN}/,/${RC_MARK_END}/d" "$rc" > "$tmp" && mv "$tmp" "$rc"
        ok "removed the g023 block from ${rc}"
    else
        skip "nothing to remove from ${rc}"
    fi
    say ""
    say "The project folder, ${C_BOLD}.venv${C_RESET}, ${C_BOLD}K.dat${C_RESET} and ${C_BOLD}config.json${C_RESET} were left alone."
    say "Delete the folder to finish removing g023."
    exit 0
fi

# ---------------------------------------------------------------------------

say ""
say "${C_BOLD}g023 Code — setup${C_RESET}"
say "${C_DIM}${HERE}${C_RESET}"

# ---------------------------------------------------------------------------
step "Checking the project layout"
# ---------------------------------------------------------------------------

for required in "g023_code/__init__.py" "g023_code/cli.py" "requirements.txt"; do
    [ -f "${HERE}/${required}" ] || die "${required} is missing — run this from the folder you unpacked g023 into."
done
ok "package, launcher and requirements are all here"

VERSION="$(sed -n 's/^__version__ *= *"\(.*\)"/\1/p' "${HERE}/g023_code/__init__.py" 2>/dev/null)"
[ -n "$VERSION" ] && skip "version ${VERSION}"

# ---------------------------------------------------------------------------
step "Looking for Python ${MIN_MAJOR}.${MIN_MINOR} or newer"
# ---------------------------------------------------------------------------

# Newest first: if several interpreters are installed we would rather build the
# venv on the one with the longest support left.
CANDIDATES=(python3.14 python3.13 python3.12 python3.11 python3 python)
PYTHON=""
for candidate in "${CANDIDATES[@]}"; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c "import sys; sys.exit(0 if sys.version_info[:2] >= (${MIN_MAJOR}, ${MIN_MINOR}) else 1)" 2>/dev/null; then
        PYTHON="$(command -v "$candidate")"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    fail "no interpreter at ${MIN_MAJOR}.${MIN_MINOR}+ found"
    say ""
    say "    Install one, then run this again:"
    say "      Debian/Ubuntu  ${C_ACC}sudo apt install python3 python3-venv python3-pip${C_RESET}"
    say "      Fedora/RHEL    ${C_ACC}sudo dnf install python3 python3-pip${C_RESET}"
    say "      Arch           ${C_ACC}sudo pacman -S python python-pip${C_RESET}"
    say "      macOS          ${C_ACC}brew install python@3.12${C_RESET}  (or python.org)"
    die "Python ${MIN_MAJOR}.${MIN_MINOR}+ is required."
fi
PYVER="$("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
ok "${PYTHON} (${PYVER})"

# ---------------------------------------------------------------------------
step "Choosing where the dependencies go"
# ---------------------------------------------------------------------------

# An "externally managed" interpreter (PEP 668 — the default on Debian, Ubuntu,
# Fedora and Homebrew now) refuses a plain pip install, and rightly so. A venv
# inside the project folder is the answer that needs no sudo and no --break-
# system-packages, so that is what we reach for unless told otherwise.
externally_managed() {
    "$PYTHON" - <<'PY' 2>/dev/null
import sys, sysconfig, pathlib
stdlib = sysconfig.get_path("stdlib")
sys.exit(0 if stdlib and pathlib.Path(stdlib, "EXTERNALLY-MANAGED").exists() else 1)
PY
}

USE_VENV=0
case "$WANT_VENV" in
    yes) USE_VENV=1 ;;
    no)  USE_VENV=0 ;;
    auto)
        if [ -x "${VENV}/bin/python" ]; then
            USE_VENV=1
        elif [ -n "${VIRTUAL_ENV:-}" ]; then
            USE_VENV=0
            skip "already inside a virtualenv (${VIRTUAL_ENV}) — using it"
        elif externally_managed; then
            USE_VENV=1
            skip "this Python is externally managed (PEP 668), so a venv it is"
        else
            USE_VENV=1
        fi
        ;;
esac

if [ "$USE_VENV" = "1" ]; then
    if [ -x "${VENV}/bin/python" ]; then
        skip "reusing the existing virtualenv at ./.venv"
    else
        if ! "$PYTHON" -c "import venv" >/dev/null 2>&1; then
            fail "the venv module is missing from this Python"
            say "    Debian/Ubuntu: ${C_ACC}sudo apt install python3-venv${C_RESET}"
            die "Cannot create a virtualenv."
        fi
        say "    ${C_DIM}creating ./.venv …${C_RESET}"
        "$PYTHON" -m venv "$VENV" || die "venv creation failed."
        ok "created ./.venv"
    fi
    PYTHON="${VENV}/bin/python"
else
    ok "installing into ${PYTHON}"
fi

# pip is not guaranteed to be present even when Python is.
if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    "$PYTHON" -m ensurepip --upgrade >/dev/null 2>&1
fi
"$PYTHON" -m pip --version >/dev/null 2>&1 \
    || die "pip is unavailable for ${PYTHON}. Install it (e.g. apt install python3-pip) and re-run."

PIP=("$PYTHON" -m pip install --disable-pip-version-check --quiet)
if [ "$USE_VENV" = "0" ] && [ -z "${VIRTUAL_ENV:-}" ]; then
    PIP+=(--user)
fi

# ---------------------------------------------------------------------------
step "Installing dependencies"
# ---------------------------------------------------------------------------

# import name : pip requirement : why the user should care
REQUIRED=(
    "rich:rich>=13.7.0:the entire terminal UI"
    "httpx:httpx>=0.27.0:the DeepSeek Responses API client"
    "tiktoken:tiktoken>=0.7.0:token accounting for the context gauge"
    "pygments:pygments>=2.17.0:syntax highlighting"
)
RECOMMENDED=(
    "prompt_toolkit:prompt_toolkit>=3.0.0:tab completion, history, status toolbar"
)
OPTIONAL=(
    "PIL:pillow>=10.0.0:downscales images before vision calls"
    "curl_cffi:curl_cffi>=0.7.0:Chrome TLS fingerprint for FetchUrl"
    "h2:h2>=4.1.0:HTTP/2 for the FetchUrl fallback"
    "brotli:brotli>=1.1.0:decode brotli responses"
    "zstandard:zstandard>=0.22.0:decode zstd responses"
)

has_module() { "$PYTHON" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$1') else 1)" 2>/dev/null; }

# collect_missing <array name> -> fills MISSING_SPECS / MISSING_NAMES
MISSING_SPECS=(); MISSING_NAMES=()
collect_missing() {
    MISSING_SPECS=(); MISSING_NAMES=()
    local entry mod spec
    for entry in "$@"; do
        mod="${entry%%:*}"; spec="${entry#*:}"; spec="${spec%%:*}"
        if has_module "$mod"; then
            skip "${mod} already present"
        else
            MISSING_SPECS+=("$spec"); MISSING_NAMES+=("$mod")
        fi
    done
}

install_specs() {
    local label="$1"; shift
    [ $# -eq 0 ] && return 0
    say "    ${C_DIM}pip install ${*} …${C_RESET}"
    if "${PIP[@]}" "$@"; then
        ok "${label}: $*"
        return 0
    fi
    return 1
}

collect_missing "${REQUIRED[@]}"
if [ ${#MISSING_SPECS[@]} -gt 0 ]; then
    install_specs "installed" "${MISSING_SPECS[@]}" || die "Could not install the required packages: ${MISSING_SPECS[*]}"
fi

collect_missing "${RECOMMENDED[@]}"
if [ ${#MISSING_SPECS[@]} -gt 0 ]; then
    install_specs "installed" "${MISSING_SPECS[@]}" \
        || { warn "prompt_toolkit failed to install — the input line falls back to a plain prompt"
             note_warning "prompt_toolkit is missing: no tab completion or history."; }
fi

collect_missing "${OPTIONAL[@]}"
if [ ${#MISSING_SPECS[@]} -gt 0 ]; then
    want_optional=0
    case "$DO_OPTIONAL" in
        yes) want_optional=1 ;;
        no)  want_optional=0 ;;
        auto)
            say "    ${C_DIM}optional extras not installed: ${MISSING_NAMES[*]}${C_RESET}"
            ask "Install the optional extras (vision + browser-grade fetching)?" y && want_optional=1
            ;;
    esac
    if [ "$want_optional" = "1" ]; then
        # One at a time: curl_cffi needs wheels that do not exist for every
        # platform, and one unbuildable extra must not sink the other four.
        for i in "${!MISSING_SPECS[@]}"; do
            install_specs "installed" "${MISSING_SPECS[$i]}" \
                || warn "${MISSING_NAMES[$i]} could not be installed — g023 runs without it"
        done
    else
        skip "skipping optional extras (install later with: ${PYTHON} -m pip install -r requirements.txt)"
    fi
fi

# ---------------------------------------------------------------------------
step "API key"
# ---------------------------------------------------------------------------

KEY_FILE="${HERE}/K.dat"
PLACEHOLDERS=("YOUR_DEEPSEEK_API_KEY_HERE" "sk-REPLACE-WITH-YOUR-DEEPSEEK-API-KEY")

key_is_placeholder() {
    local value="$1" p
    [ -z "$value" ] && return 0
    for p in "${PLACEHOLDERS[@]}"; do [ "$value" = "$p" ] && return 0; done
    return 1
}

CURRENT_KEY=""
[ -f "$KEY_FILE" ] && CURRENT_KEY="$(head -n1 "$KEY_FILE" | tr -d '\r' | xargs 2>/dev/null)"

if [ -n "$API_KEY" ]; then
    printf '%s\n' "$API_KEY" > "$KEY_FILE"
    chmod 600 "$KEY_FILE"
    ok "wrote the key you passed into K.dat"
elif ! key_is_placeholder "$CURRENT_KEY"; then
    chmod 600 "$KEY_FILE" 2>/dev/null
    ok "K.dat already holds a key (${CURRENT_KEY:0:6}…${CURRENT_KEY: -4})"
else
    # An env var is a legitimate way to hold the key during a scripted install.
    if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
        printf '%s\n' "$DEEPSEEK_API_KEY" > "$KEY_FILE"
        chmod 600 "$KEY_FILE"
        ok "took the key from \$DEEPSEEK_API_KEY"
    elif [ "$ASSUME_YES" = "1" ] || [ ! -t 0 ]; then
        [ -f "$KEY_FILE" ] || { printf '%s\n' "${PLACEHOLDERS[1]}" > "$KEY_FILE"; chmod 600 "$KEY_FILE"; }
        warn "no key yet — put one on the first line of ${KEY_FILE}"
        note_warning "K.dat has no real key. g023 will not start until you add one (https://platform.deepseek.com/)."
    else
        say "    ${C_DIM}Get one at https://platform.deepseek.com/ — leave blank to add it later.${C_RESET}"
        printf '    DeepSeek API key: '
        read -r entered || entered=""
        entered="$(printf '%s' "$entered" | xargs 2>/dev/null)"
        if [ -n "$entered" ]; then
            printf '%s\n' "$entered" > "$KEY_FILE"
            chmod 600 "$KEY_FILE"
            ok "saved to K.dat (permissions 600)"
        else
            [ -f "$KEY_FILE" ] || { printf '%s\n' "${PLACEHOLDERS[1]}" > "$KEY_FILE"; chmod 600 "$KEY_FILE"; }
            warn "left blank — put your key on the first line of ${KEY_FILE} before running g023"
            note_warning "K.dat has no real key yet."
        fi
    fi
fi

# ---------------------------------------------------------------------------
step "Configuration"
# ---------------------------------------------------------------------------

CONFIG_FILE="${HERE}/config.json"
if [ -f "$CONFIG_FILE" ]; then
    if "$PYTHON" -c "import json,sys; json.load(open(sys.argv[1]))" "$CONFIG_FILE" 2>/dev/null; then
        skip "config.json is present and valid — left untouched"
    else
        cp "$CONFIG_FILE" "${CONFIG_FILE}.broken"
        warn "config.json was not valid JSON; saved as config.json.broken and rewriting defaults"
        rm -f "$CONFIG_FILE"
    fi
fi

if [ ! -f "$CONFIG_FILE" ]; then
    cat > "$CONFIG_FILE" <<'JSON'
{
  "verbose": "low",
  "auto_compact": true,
  "vision_backend": "none",
  "vision_model": null,
  "vision_host": null,
  "vision_max_image_dim": 1024,
  "vision_timeout": 180,
  "orchestrator_model": "deepseek-v4-flash",
  "subagent_model": "deepseek-v4-flash",
  "reasoning_effort": "high",
  "thinking_enabled": true,
  "show_tool_timing": true,
  "show_context_bar": true,
  "permission_default": "ask",
  "vision_num_ctx": 4096,
  "vision_keep_alive": "5m"
}
JSON
    ok "wrote default config.json"
fi

if [ ! -x "${HERE}/g023.sh" ]; then
    chmod +x "${HERE}/g023.sh" && ok "made g023.sh executable"
else
    skip "g023.sh is already executable"
fi
chmod +x "${HERE}/installer.sh" 2>/dev/null

# ---------------------------------------------------------------------------
step "Vision backend (optional)"
# ---------------------------------------------------------------------------

# Vision is off by default and stays off — this step only reports what is
# available so /vision has something to offer when the user wants it.
if command -v ollama >/dev/null 2>&1; then
    ok "ollama found at $(command -v ollama)"
    if curl -fsS --max-time 2 "${OLLAMA_HOST:-http://localhost:11434}/api/tags" >/dev/null 2>&1; then
        ok "the daemon is answering — enable image analysis in g023 with ${C_BOLD}/vision${C_RESET}"
    else
        skip "daemon not responding; start it with 'ollama serve', then use /vision"
    fi
else
    skip "no ollama on this machine — image analysis stays off (everything else works)"
    skip "install it from https://ollama.com if you want /vision"
fi

# ---------------------------------------------------------------------------
step "Making 'g023' runnable from anywhere"
# ---------------------------------------------------------------------------

if [ "$DO_PATH" = "0" ]; then
    skip "--no-path given; launch with ${HERE}/g023.sh"
else
    install_shim=1
    if [ -e "$SHIM" ] && ! grep -qF "$HERE" "$SHIM" 2>/dev/null; then
        warn "${SHIM} exists and points somewhere else"
        ask "Overwrite it to point at this install?" y || install_shim=0
    fi

    if [ "$install_shim" = "1" ]; then
        mkdir -p "$BIN_DIR"
        # A shim rather than a symlink: g023.sh resolves G023_HOME from its own
        # location, and a symlink would resolve to the link's directory on the
        # platforms where readlink -f is not available.
        cat > "$SHIM" <<EOF
#!/usr/bin/env bash
# Generated by g023 installer.sh — safe to delete.
exec "${HERE}/g023.sh" "\$@"
EOF
        chmod +x "$SHIM"
        ok "installed ${SHIM}"

        case ":${PATH}:" in
            *":${BIN_DIR}:"*)
                ok "${BIN_DIR} is already on PATH — type ${C_BOLD}g023${C_RESET} in any project folder"
                ;;
            *)
                rc="$(shell_rc)"
                if [ -f "$rc" ] && grep -qF "$RC_MARK_BEGIN" "$rc"; then
                    skip "${rc} already has the g023 PATH block — open a new shell to pick it up"
                elif ask "Add ${BIN_DIR} to PATH in ${rc}?" y; then
                    mkdir -p "$(dirname "$rc")"
                    if [ "$(basename "$rc")" = "config.fish" ]; then
                        {
                            printf '\n%s\n' "$RC_MARK_BEGIN"
                            printf 'fish_add_path %s\n' "$BIN_DIR"
                            printf '%s\n' "$RC_MARK_END"
                        } >> "$rc"
                    else
                        {
                            printf '\n%s\n' "$RC_MARK_BEGIN"
                            printf 'export PATH="%s:$PATH"\n' "$BIN_DIR"
                            printf '%s\n' "$RC_MARK_END"
                        } >> "$rc"
                    fi
                    ok "added to ${rc} — run ${C_BOLD}source ${rc}${C_RESET} or open a new terminal"
                else
                    skip "left PATH alone; run ${SHIM} directly"
                    note_warning "${BIN_DIR} is not on PATH, so plain 'g023' will not resolve yet."
                fi
                ;;
        esac
    fi
fi

# ---------------------------------------------------------------------------
step "Verifying the install"
# ---------------------------------------------------------------------------

if PYTHONPATH="${HERE}${PYTHONPATH:+:$PYTHONPATH}" G023_HOME="$HERE" \
   "$PYTHON" -c "import g023_code, g023_code.cli, g023_code.api, g023_code.orchestrator" 2>/tmp/g023_import_err; then
    ok "the package imports cleanly on ${PYVER}"
else
    fail "importing g023_code failed:"
    sed 's/^/      /' /tmp/g023_import_err >&2
    rm -f /tmp/g023_import_err
    die "Setup did not complete."
fi
rm -f /tmp/g023_import_err

# ---------------------------------------------------------------------------

say ""
say "${C_OK}${C_BOLD}g023 Code ${VERSION:-} is set up.${C_RESET}"
say ""
if [ ${#WARNINGS[@]} -gt 0 ]; then
    say "${C_WARN}Before it will run:${C_RESET}"
    for w in "${WARNINGS[@]}"; do say "  ${C_WARN}!${C_RESET} ${w}"; done
    say ""
fi
say "Start it from whatever project you want it to work on:"
if [ "$DO_PATH" = "1" ] && [ -x "$SHIM" ]; then
    say "  ${C_ACC}cd ~/some/project && g023${C_RESET}"
else
    say "  ${C_ACC}cd ~/some/project && ${HERE}/g023.sh${C_RESET}"
fi
say ""
say "${C_DIM}The folder you launch from is the project root; K.dat and config.json${C_RESET}"
say "${C_DIM}always stay here in ${HERE}.${C_RESET}"
say "${C_DIM}First things to try: /help · /status · /tools · /vision${C_RESET}"
say ""
