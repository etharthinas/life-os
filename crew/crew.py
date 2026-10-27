#!/usr/bin/env python3
"""crew — run Claude Code sessions on any of your machines, from this one.

Hosts and repos live in ~/.crew/config.toml, account tokens in the macOS
Keychain, and every session runs inside tmux on its target host so it
survives this laptop sleeping or disconnecting. Each session reports its
usage back through a status line and a StopFailure hook; `crew watch`
reads those and moves a session to the next account when it hits a limit.
"""
import argparse
import concurrent.futures as cf
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

HOME = Path(os.environ.get("CREW_HOME", Path.home() / ".crew"))
CONFIG = HOME / "config.toml"
STATE = HOME / "state.json"
KEYCHAIN_SVC = "crew-account-"
REMOTE_PATH = 'export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"'
# Account kind -> env var Claude Code reads the credential from. "login" uses
# whatever account is already logged in on the host.
ENV_VAR = {"oauth": "CLAUDE_CODE_OAUTH_TOKEN", "apikey": "ANTHROPIC_API_KEY"}
WINDOWS = ("five_hour", "seven_day")
AGENT_LABEL = "com.crew.watch"
AGENT_PLIST = Path.home() / "Library/LaunchAgents" / f"{AGENT_LABEL}.plist"
# The grid is a local tmux on its own socket whose panes each attach to one session.
# Its prefix is C-g so C-b still reaches the session's own tmux.
GRID = ["tmux", "-L", "crew-grid", "-f", str(HOME / "grid.tmux.conf")]
GRID_CONF = """\
set -g prefix C-g
unbind C-b
bind C-g send-prefix
set -g mouse on
set -g pane-border-status top
set -g pane-border-format " #{?pane_active,#[bold#,fg=cyan],}#{@label} "
set -g pane-active-border-style fg=cyan
set -g status-style bg=default,fg=colour244
set -g status-left " crew grid "
set -g status-left-style fg=cyan,bold
set -g status-right "click a pane to type · C-g z zoom · C-g d back "
set -g status-right-length 80
set -g window-status-format ""
set -g window-status-current-format ""
"""

# Installed on every host at launch. The status line saves Claude Code's status
# JSON (which carries rate_limits) for the watcher; the hook drops a marker when
# a turn ends on a rate limit.
STATUSLINE_SH = r"""d="$HOME/.crew/usage"; mkdir -p "$d"
in=$(cat)
printf '%s' "$in" > "$d/$CREW_SESSION.tmp" && mv "$d/$CREW_SESSION.tmp" "$d/$CREW_SESSION.json"
pct=$(printf '%s' "$in" | tr -d '\n' | sed -n 's/.*"five_hour"[^}]*"used_percentage": *\([0-9.]*\).*/\1/p')
printf 'crew · %s' "$CREW_ACCOUNT"
[ -n "$pct" ] && printf ' · 5h %.0f%% used' "$pct"
true
"""
LIMIT_HOOK_SH = r"""d="$HOME/.crew/events"; mkdir -p "$d"; cat > "$d/$CREW_SESSION.json"
"""
SESSION_SETTINGS = json.dumps({
    "statusLine": {"type": "command", "command": 'sh "$HOME/.crew/bin/statusline.sh"'},
    "hooks": {"StopFailure": [{"matcher": "rate_limit", "hooks": [
        {"type": "command", "command": 'sh "$HOME/.crew/bin/limit-hook.sh"'}]}]},
}, separators=(",", ":"))

TEMPLATE = """\
# crew config. Hosts are SSH aliases from ~/.ssh/config; ssh = "" means this machine.

[hosts.server1]
ssh = "server1"
repos = ["~/project"]

# Accounts are tried in order; ones without a Keychain token are skipped.
# kind: oauth  (token from `claude setup-token`)
#       apikey (Console API key, pay-as-you-go)
#       login  (whatever account is already logged in on the host)
[[accounts]]
name = "acct1"
kind = "oauth"

[[accounts]]
name = "host-login"
kind = "login"

[watch]
interval_seconds = 20       # how often the watcher checks every host
probe_minutes = 30          # refresh usage of idle accounts this often
resume_prompt = "Continue where you left off. (crew switched this session to a fresh account after a usage limit.)"
"""


class CrewError(Exception):
    pass


def die(msg):
    raise CrewError(msg)


# --- config & state ---------------------------------------------------------

def load_config():
    if not CONFIG.exists():
        die(f"no config at {CONFIG} — run `crew init`")
    with CONFIG.open("rb") as f:
        cfg = tomllib.load(f)
    cfg.setdefault("hosts", {})
    cfg.setdefault("accounts", [])
    w = cfg.setdefault("watch", {})
    w.setdefault("interval_seconds", 20)
    w.setdefault("probe_minutes", 30)
    w.setdefault("resume_prompt", "Continue where you left off. (crew switched this session "
                                  "to a fresh account after a usage limit.)")
    return cfg


def load_state():
    try:
        st = json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    for k in ("accounts", "sessions", "usage", "watch"):
        st.setdefault(k, {})
    st.setdefault("log", [])
    return st


@contextlib.contextmanager
def state_txn():
    """Read-modify-write the state under a lock; the watcher and UI share it."""
    HOME.mkdir(parents=True, exist_ok=True)
    with open(HOME / "state.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        st = load_state()
        yield st
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2) + "\n")
        os.replace(tmp, STATE)


def log_event(msg):
    with state_txn() as st:
        st["log"] = (st["log"] + [{"ts": int(time.time()), "msg": msg}])[-40:]
    print(f"{dt.datetime.now():%m-%d %H:%M:%S} {msg}", flush=True)


def host_cfg(cfg, host):
    if host not in cfg["hosts"]:
        die(f"unknown host {host!r} (known: {', '.join(cfg['hosts'])})")
    return cfg["hosts"][host]


# --- running things on hosts ------------------------------------------------

def sh(cfg, host, script, *, input=None, tty=False, timeout=30):
    """Run a bash script on host, locally when its ssh alias is empty."""
    script = f"{REMOTE_PATH}; {script}"
    alias = host_cfg(cfg, host).get("ssh", "")
    if alias:
        opts = ["-t"] if tty else ["-o", "BatchMode=yes"]
        argv = ["ssh", *opts, "-o", "ConnectTimeout=8", alias, "bash -c " + shlex.quote(script)]
    else:
        argv = ["bash", "-c", script]
    if tty:
        return subprocess.call(argv)
    try:
        return subprocess.run(argv, input=input, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 255, "", "timed out")


def path_expr(p):
    """Shell expression for a path that may start with ~/."""
    if p == "~":
        return '"$HOME"'
    if p.startswith("~/"):
        return '"$HOME"/' + shlex.quote(p[2:])
    return shlex.quote(p)


def session_name(repo):
    return "crew-" + re.sub(r"[^A-Za-z0-9_-]", "-", Path(repo).name)


def live_sessions(cfg, host):
    """crew-* tmux sessions on host, or None when the host is unreachable."""
    r = sh(cfg, host, "tmux ls -F '#{session_name}|#{session_attached}' 2>/dev/null; true", timeout=15)
    if r.returncode:
        return None
    out = []
    for line in r.stdout.splitlines():
        name, _, attached = line.partition("|")
        if name.startswith("crew-"):
            out.append({"name": name, "attached": attached not in ("", "0")})
    return out


def all_live_sessions(cfg):
    with cf.ThreadPoolExecutor() as ex:
        futs = {h: ex.submit(live_sessions, cfg, h) for h in cfg["hosts"]}
        return {h: f.result() for h, f in futs.items()}


# --- accounts & usage -------------------------------------------------------

def keychain_get(name):
    r = subprocess.run(["security", "find-generic-password", "-a", "crew", "-s", KEYCHAIN_SVC + name, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def account_ready(acct):
    return acct.get("kind", "login") == "login" or keychain_get(acct["name"]) is not None


def usage_key(acct, host):
    """Usage is per account, except host logins, which differ per host."""
    return f"{acct['name']}@{host}" if acct.get("kind", "login") == "login" else acct["name"]


def exhausted_until(st, key):
    until = st["accounts"].get(key, {}).get("exhausted_until")
    if until and dt.datetime.fromisoformat(until) > dt.datetime.now():
        return dt.datetime.fromisoformat(until)
    return None


def five_hour_used(st, key):
    w = st["usage"].get(key, {}).get("five_hour")
    return w["used"] if w and w.get("resets_at", 0) > time.time() else 0


def find_account(cfg, name):
    for a in cfg["accounts"]:
        if a["name"] == name:
            return a
    die(f"unknown account {name!r} (known: {', '.join(a['name'] for a in cfg['accounts'])})")


def pick_account(cfg, st, host, want=None, exclude=()):
    """Explicit choice, else the first usable account in config order, skipping
    ones nearly out of their 5-hour window while any roomier one is left."""
    if want:
        return find_account(cfg, want)
    usable = [a for a in cfg["accounts"] if a["name"] not in exclude
              and not exhausted_until(st, usage_key(a, host)) and account_ready(a)]
    if not usable:
        die("no usable account — every one is exhausted or has no token")
    roomy = [a for a in usable if five_hour_used(st, usage_key(a, host)) < 95]
    return (roomy or usable)[0]


def record_usage(st, key, windows, source):
    """Store {window: {used: 0-100, resets_at: epoch}} and mark full windows exhausted."""
    st["usage"][key] = {**windows, "updated": int(time.time()), "source": source}
    full = [w["resets_at"] for w in windows.values() if w["used"] >= 100 and w["resets_at"] > time.time()]
    if full:
        until = dt.datetime.fromtimestamp(max(full)).isoformat(timespec="minutes")
        st["accounts"].setdefault(key, {})["exhausted_until"] = until


def probe(acct):
    """Ask Claude Code itself for an account's rate-limit windows with a tiny
    Haiku request (~600 tokens). Returns ({window: {used, resets_at}}, status)."""
    token = keychain_get(acct["name"])
    if acct.get("kind") != "oauth" or not token:
        return None, None
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")}
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    cwd = HOME / "probe"
    cwd.mkdir(parents=True, exist_ok=True)
    argv = ["claude", "-p", "ok", "--model", "haiku", "--output-format", "stream-json", "--verbose",
            "--max-turns", "1", "--tools", "", "--setting-sources", "", "--strict-mcp-config",
            "--system-prompt", "Reply ok.", "--no-session-persistence"]
    try:
        r = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return None, None
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "rate_limit_event":
            info = ev["rate_limit_info"]
            windows = {k: {"used": round(v["utilization"] * 100, 1), "resets_at": int(v["resetsAt"])}
                       for k, v in (info.get("unifiedWindows") or {}).items() if k in WINDOWS}
            if info.get("status") == "rejected" and info.get("rateLimitType") in windows:
                windows[info["rateLimitType"]]["used"] = 100.0
            return windows, info.get("status")
    return None, None


def probe_all(cfg, names=None):
    accts = [a for a in cfg["accounts"] if a.get("kind") == "oauth" and (names is None or a["name"] in names)]
    with cf.ThreadPoolExecutor() as ex:
        results = dict(zip([a["name"] for a in accts], ex.map(probe, accts)))
    with state_txn() as st:
        for name, (windows, _status) in results.items():
            if windows is not None:
                record_usage(st, name, windows, "probe")
    return results


def parse_until(s):
    if s is None:
        return dt.datetime.now() + dt.timedelta(hours=5)
    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        h, m = map(int, s.split(":"))
        t = dt.datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
        return t if t > dt.datetime.now() else t + dt.timedelta(days=1)
    return dt.datetime.fromisoformat(s)


# --- sessions ---------------------------------------------------------------

def launch(cfg, host, repo, acct, name, *, resume=False, extra=(), prompt=None):
    """Start claude in tmux session `name` on host, or restart it in place when resume."""
    q = shlex.quote
    env_file = f"~/.crew/run/{name}.env"
    env = {"CREW_SESSION": name, "CREW_ACCOUNT": acct["name"]}
    var = ENV_VAR.get(acct.get("kind", "login"))
    if var:
        token = keychain_get(acct["name"])
        if not token:
            die(f"no token for {acct['name']} in Keychain — `crew acct login {acct['name']}`")
        env[var] = token
    # Credentials travel over stdin into a 0600 file so they never appear in argv.
    stage = (f"umask 077; mkdir -p ~/.crew/run ~/.crew/bin; cat > {env_file}; "
             f"printf %s {q(STATUSLINE_SH)} > ~/.crew/bin/statusline.sh; "
             f"printf %s {q(LIMIT_HOOK_SH)} > ~/.crew/bin/limit-hook.sh")
    r = sh(cfg, host, stage, input="".join(f"export {k}={q(v)}\n" for k, v in env.items()))
    if r.returncode:
        die(f"{host}: could not stage session: {r.stderr.strip()}")

    claude = shlex.join(["claude", "--settings", SESSION_SETTINGS, *(["--continue"] if resume else []),
                         *extra, *([prompt] if prompt else [])])
    inner = (f"cd {path_expr(repo)} || exit 1; f={env_file}; "
             f'if [ -f "$f" ]; then . "$f"; rm -f "$f"; fi; '
             f"rm -f ~/.crew/usage/{name}.json ~/.crew/events/{name}.json; {REMOTE_PATH}; "
             f'{claude}; rc=$?; [ $rc -ne 0 ] && read -p "claude exited ($rc), enter to close "')
    verb = f"respawn-pane -k -t {q(name)}" if resume else f"new-session -d -s {q(name)}"
    # Claude Code's own status line shows the account, so the session's tmux bar is hidden.
    mouse = "" if resume else f"; tmux set-option -t {q(name)} mouse on >/dev/null; tmux set-option -t {q(name)} status off >/dev/null"
    r = sh(cfg, host, f"tmux {verb} bash -c {q(inner)}{mouse}")
    if r.returncode:
        hint = " (install tmux on that host)" if "tmux: command not found" in r.stderr else ""
        die(f"{host}: tmux failed: {r.stderr.strip() or r.stdout.strip()}{hint}")

    with state_txn() as st:
        st["sessions"][f"{host}/{name}"] = {"repo": repo, "account": acct["name"], "args": list(extra),
                                            "started": dt.datetime.now().isoformat(timespec="seconds")}


def start_session(cfg, host, repo, account=None, new=False, extra=()):
    """Start claude for repo on host unless it's already running; return the session name."""
    name = session_name(repo)
    running = live_sessions(cfg, host)
    if running is None:
        die(f"{host} unreachable")
    names = {s["name"] for s in running}
    if name in names and not new:
        return name
    if name in names:
        i = 2
        while f"{name}-{i}" in names:
            i += 1
        name = f"{name}-{i}"
    if sh(cfg, host, f"test -d {path_expr(repo)}").returncode:
        die(f"{host}: {repo} does not exist")
    acct = pick_account(cfg, load_state(), host, account)
    launch(cfg, host, repo, acct, name, extra=extra)
    return name


def switch_session(cfg, host, name, account=None, prompt=None):
    """Restart a session's claude under another account, continuing the conversation."""
    meta = load_state()["sessions"].get(f"{host}/{name}")
    if not meta:
        die(f"no record of {host}/{name} — was it started by crew?")
    acct = pick_account(cfg, load_state(), host, account, exclude=(meta["account"],))
    launch(cfg, host, meta["repo"], acct, name, resume=True, extra=meta.get("args", []), prompt=prompt)
    return meta["account"], acct["name"]


def kill_session(cfg, host, name):
    r = sh(cfg, host, f"tmux kill-session -t {shlex.quote(name)}")
    if r.returncode:
        die(f"{host}: {r.stderr.strip()}")
    with state_txn() as st:
        st["sessions"].pop(f"{host}/{name}", None)


def attach(cfg, host, name):
    return sh(cfg, host, f"tmux attach -t {shlex.quote(name)}", tty=True)


def attach_argv(cfg, host, name):
    """Command that attaches to a session, for running inside a grid pane."""
    script = f"{REMOTE_PATH}; unset TMUX; exec tmux attach -t {shlex.quote(name)}"
    alias = host_cfg(cfg, host).get("ssh", "")
    if alias:
        return ["ssh", "-t", "-o", "ConnectTimeout=8", alias, "bash -c " + shlex.quote(script)]
    return ["bash", "-c", script]


def open_grid(cfg, targets):
    """Tile sessions [(host, name)] in one local window and attach to it."""
    if not targets:
        die("no running sessions to show")
    st = load_state()
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "grid.tmux.conf").write_text(GRID_CONF)
    subprocess.run([*GRID, "kill-server"], capture_output=True)
    cols, rows = shutil.get_terminal_size()
    for i, (host, name) in enumerate(targets):
        meta = st["sessions"].get(f"{host}/{name}", {})
        label = f"{host} · {Path(meta.get('repo', name)).name}"
        cmd = shlex.join(attach_argv(cfg, host, name))
        verb = ["new-session", "-d", "-s", "grid", "-x", str(cols), "-y", str(rows - 1)] if i == 0 \
            else ["split-window", "-t", "grid"]
        r = subprocess.run([*GRID, *verb, "-P", "-F", "#{pane_id}", cmd], capture_output=True, text=True)
        if r.returncode:
            die(f"grid: {r.stderr.strip()} (window too small for {len(targets)} panes?)")
        pane = r.stdout.strip()
        subprocess.run([*GRID, "set-option", "-p", "-t", pane, "@label", label], capture_output=True)
        # Re-tile after every split so later splits still have room.
        subprocess.run([*GRID, "select-layout", "-t", "grid", "tiled"], capture_output=True)
    subprocess.run([*GRID, "select-pane", "-t", "grid.0"], capture_output=True)
    return subprocess.call([*GRID, "attach", "-t", "grid"], env={k: v for k, v in os.environ.items() if k != "TMUX"})


def resolve_repo(cfg, host, repo):
    repos = host_cfg(cfg, host).get("repos", [])
    if repo is None:
        return choose([(r, r) for r in repos], f"{host} repo")
    matches = [r for r in repos if r == repo or Path(r).name == repo]
    return matches[0] if matches else repo


def resolve_session(name):
    return name if name.startswith("crew-") else session_name(name)


def choose(items, prompt):
    if not items:
        die(f"nothing to choose for {prompt}")
    labels = [label for label, _ in items]
    if shutil.which("fzf"):
        r = subprocess.run(["fzf", "--prompt", f"{prompt}> ", "--height", "40%", "--reverse"],
                           input="\n".join(labels), text=True, stdout=subprocess.PIPE)
        if r.returncode:
            sys.exit(130)
        return items[labels.index(r.stdout.rstrip("\n"))][1]
    for i, label in enumerate(labels, 1):
        print(f"{i:>3}  {label}")
    while True:
        try:
            s = input(f"{prompt} [1-{len(items)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(130)
        if s.isdigit() and 1 <= int(s) <= len(items):
            return items[int(s) - 1][1]


# --- watcher ----------------------------------------------------------------

COLLECT = r"""cd ~/.crew 2>/dev/null || exit 0
for f in usage/*.json events/*.json; do
  [ -f "$f" ] || continue
  printf '%s\t%s\t' "$f" "$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f")"; tr -d '\n' < "$f"; echo
done
rm -f events/*.json"""


def collect(cfg, host):
    """Usage snapshots and limit events reported by sessions on host."""
    r = sh(cfg, host, COLLECT, timeout=20)
    if r.returncode:
        return None
    out = []
    for line in r.stdout.splitlines():
        path, _, rest = line.partition("\t")
        mtime, _, body = rest.partition("\t")
        kind, _, fname = path.partition("/")
        try:
            out.append((kind, fname.removesuffix(".json"), int(mtime), json.loads(body or "{}")))
        except (ValueError, json.JSONDecodeError):
            continue
    return out


def notify(msg):
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(msg)} with title \"crew\""],
                   capture_output=True)


def handle_limit(cfg, host, name, meta, trigger_mtime):
    acct = find_account(cfg, meta["account"])
    key = usage_key(acct, host)
    with state_txn() as st:
        st["sessions"][f"{host}/{name}"]["handled"] = trigger_mtime
        if not exhausted_until(st, key):
            u = st["usage"].get(key, {})
            resets = [u[w]["resets_at"] for w in WINDOWS if w in u and u[w]["resets_at"] > time.time()]
            until = dt.datetime.fromtimestamp(min(resets)) if resets else dt.datetime.now() + dt.timedelta(hours=1)
            st["accounts"].setdefault(key, {})["exhausted_until"] = until.isoformat(timespec="minutes")
    try:
        old, new = switch_session(cfg, host, name, prompt=cfg["watch"]["resume_prompt"])
    except CrewError as e:
        log_event(f"{host}/{name}: {acct['name']} hit its limit and could not switch: {e}")
        notify(f"{host}/{name}: {acct['name']} hit its limit, no account left to switch to")
        return
    log_event(f"{host}/{name}: {old} hit its limit, switched to {new}")
    notify(f"{host}/{name}: switched {old} → {new}")


def watch_tick(cfg):
    st = load_state()
    with cf.ThreadPoolExecutor() as ex:
        reports = dict(zip(cfg["hosts"], ex.map(lambda h: collect(cfg, h), cfg["hosts"])))
    triggers = []
    with state_txn() as st:
        st["watch"].update(pid=os.getpid(), tick=int(time.time()),
                           unreachable=[h for h, r in reports.items() if r is None])
        for host, items in reports.items():
            for kind, name, mtime, body in items or []:
                meta = st["sessions"].get(f"{host}/{name}")
                if not meta:
                    continue
                acct = next((a for a in cfg["accounts"] if a["name"] == meta["account"]), None)
                if not acct:
                    continue
                hit = kind == "events"
                rl = body.get("rate_limits") if kind == "usage" else None
                if rl:
                    windows = {w: {"used": float(rl[w]["used_percentage"]), "resets_at": int(rl[w]["resets_at"])}
                               for w in WINDOWS if w in rl}
                    if mtime > st["usage"].get(usage_key(acct, host), {}).get("updated", 0):
                        record_usage(st, usage_key(acct, host), windows, f"{host}/{name}")
                    hit = any(w["used"] >= 100 for w in windows.values())
                if hit and mtime > meta.get("handled", 0):
                    triggers.append((host, name, dict(meta), mtime))
    for t in triggers:
        handle_limit(cfg, *t)

    # Refresh idle accounts' usage now and then, and right after their reset passes.
    stale = []
    for a in cfg["accounts"]:
        u, exh = st["usage"].get(a["name"], {}), st["accounts"].get(a["name"], {}).get("exhausted_until")
        reset_passed = exh and dt.datetime.fromisoformat(exh) <= dt.datetime.now()
        if a.get("kind") == "oauth" and (time.time() - u.get("updated", 0) > cfg["watch"]["probe_minutes"] * 60
                                         or reset_passed):
            stale.append(a["name"])
    if stale:
        probe_all(cfg, stale)
        with state_txn() as st:
            for name in stale:
                if not exhausted_until(st, name):
                    st["accounts"].get(name, {}).pop("exhausted_until", None)


def watch_running():
    w = load_state()["watch"]
    if time.time() - w.get("tick", 0) > 120 or not w.get("pid"):
        return False
    try:
        os.kill(w["pid"], 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def agent_install():
    HOME.mkdir(parents=True, exist_ok=True)
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    path = ":".join([str(Path.home() / ".local/bin"), "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin",
                     "/usr/sbin", "/sbin"])
    args = "".join(f"<string>{a}</string>" for a in (sys.executable, str(Path(__file__).resolve()), "watch"))
    AGENT_PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{AGENT_LABEL}</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{HOME}/watch.log</string>
  <key>StandardErrorPath</key><string>{HOME}/watch.log</string>
</dict></plist>
""")
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{AGENT_LABEL}"], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", domain, str(AGENT_PLIST)], capture_output=True, text=True)
    if r.returncode:
        die(f"launchctl bootstrap failed: {r.stderr.strip()}")


def agent_uninstall():
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"], capture_output=True)
    AGENT_PLIST.unlink(missing_ok=True)
    with state_txn() as st:
        st["watch"]["tick"] = 0


# --- commands ---------------------------------------------------------------

def cmd_init(args):
    if CONFIG.exists():
        die(f"{CONFIG} already exists")
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(TEMPLATE)
    print(f"wrote {CONFIG}")


def cmd_ui(args):
    if not sys.stdout.isatty():
        return cmd_ls(args)
    import tui
    tui.run(load_config())


def cmd_open(args):
    cfg = load_config()
    host = args.host or choose([(h, h) for h in cfg["hosts"]], "host")
    repo = resolve_repo(cfg, host, args.repo)
    name = start_session(cfg, host, repo, args.account, args.new, args.claude_args)
    meta = load_state()["sessions"].get(f"{host}/{name}", {})
    print(f"{host}: {name} in {repo} as {meta.get('account', '?')}")
    if not args.detach:
        sys.exit(attach(cfg, host, name))


def cmd_ls(args):
    cfg, st = load_config(), load_state()
    rows = []
    for host, sessions in all_live_sessions(cfg).items():
        if sessions is None:
            rows.append((host, "(unreachable)", "", "", ""))
            continue
        for s in sessions:
            meta = st["sessions"].get(f"{host}/{s['name']}", {})
            rows.append((host, s["name"], meta.get("repo", "?"), meta.get("account", "?"),
                         "attached" if s["attached"] else ""))
    if not rows:
        print("no sessions")
        return
    for r in [("HOST", "SESSION", "REPO", "ACCOUNT", "")] + rows:
        print(f"{r[0]:<12} {r[1]:<32} {r[2]:<28} {r[3]:<12} {r[4]}")


def cmd_grid(args):
    cfg = load_config()
    live = all_live_sessions(cfg)
    if args.targets:
        targets = []
        for t in args.targets:
            host, _, sess = t.partition("/")
            if not sess:
                die(f"{t!r}: use HOST/REPO")
            targets.append((host, resolve_session(sess)))
    else:
        targets = [(h, s["name"]) for h, ss in live.items() for s in ss or []]
    sys.exit(open_grid(cfg, targets))


def cmd_attach(args):
    sys.exit(attach(load_config(), args.host, resolve_session(args.session)))


def cmd_kill(args):
    name = resolve_session(args.session)
    kill_session(load_config(), args.host, name)
    print(f"{args.host}: killed {name}")


def cmd_switch(args):
    cfg = load_config()
    name = resolve_session(args.session)
    if args.until is not None or args.mark:
        meta = load_state()["sessions"].get(f"{args.host}/{name}")
        if meta:
            key = usage_key(find_account(cfg, meta["account"]), args.host)
            with state_txn() as st:
                st["accounts"].setdefault(key, {})["exhausted_until"] = \
                    parse_until(args.until).isoformat(timespec="minutes")
    old, new = switch_session(cfg, args.host, name, args.account)
    print(f"{args.host}: {name} switched {old} -> {new} (conversation continued)")


def fmt_window(w):
    if not w or w["resets_at"] <= time.time():
        return "-"
    return f"{max(0, 100 - w['used']):.0f}% left, resets {dt.datetime.fromtimestamp(w['resets_at']):%a %H:%M}"


def cmd_acct_ls(args):
    cfg, st = load_config(), load_state()
    print(f"{'ACCOUNT':<12} {'KIND':<7} {'TOKEN':<8} {'5-HOUR':<30} {'7-DAY':<30} STATUS")
    for a in cfg["accounts"]:
        kind = a.get("kind", "login")
        token = "-" if kind == "login" else ("yes" if keychain_get(a["name"]) else "MISSING")
        u = st["usage"].get(a["name"], {})
        until = exhausted_until(st, a["name"])
        status = f"exhausted until {until:%a %H:%M}" if until else "available"
        print(f"{a['name']:<12} {kind:<7} {token:<8} {fmt_window(u.get('five_hour')):<30} "
              f"{fmt_window(u.get('seven_day')):<30} {status}")


def cmd_acct_set(args):
    acct = find_account(load_config(), args.name)
    if acct.get("kind", "login") == "login":
        die(f"{args.name} is kind 'login' and needs no token")
    hint = "`claude setup-token` output" if acct["kind"] == "oauth" else "API key"
    print(f"Paste the {hint} for {args.name} (input hidden).")
    # -w last with no value makes `security` prompt, keeping the secret out of argv.
    sys.exit(subprocess.call(["security", "add-generic-password", "-U", "-a", "crew",
                              "-s", KEYCHAIN_SVC + args.name, "-l", f"crew: {args.name}", "-w"]))


def cmd_acct_login(args):
    """Mint a long-lived token via browser login, then store it."""
    acct = find_account(load_config(), args.name)
    if acct.get("kind") != "oauth":
        die(f"{args.name} is kind {acct.get('kind', 'login')!r}; use `crew acct set` for API keys")
    print(f"Log in as the account you want for {args.name} when the browser opens.")
    if subprocess.call(["claude", "setup-token"]):
        die("claude setup-token failed")
    cmd_acct_set(args)


def cmd_acct_exhaust(args):
    find_account(load_config(), args.name)
    until = parse_until(args.until)
    with state_txn() as st:
        st["accounts"].setdefault(args.name, {})["exhausted_until"] = until.isoformat(timespec="minutes")
    print(f"{args.name} marked exhausted until {until:%a %H:%M}")


def cmd_acct_reset(args):
    find_account(load_config(), args.name)
    with state_txn() as st:
        st["accounts"].pop(args.name, None)
    print(f"{args.name} marked available")


def cmd_usage(args):
    cfg = load_config()
    print("probing accounts…", file=sys.stderr)
    probe_all(cfg)
    cmd_acct_ls(args)


def cmd_watch(args):
    if args.action == "install":
        agent_install()
        print(f"watcher installed as LaunchAgent {AGENT_LABEL}; log: {HOME}/watch.log")
    elif args.action == "uninstall":
        agent_uninstall()
        print("watcher removed")
    elif args.action == "status":
        print("running" if watch_running() else "not running")
        for e in load_state()["log"][-10:]:
            print(f"  {dt.datetime.fromtimestamp(e['ts']):%m-%d %H:%M}  {e['msg']}")
    else:
        log_event("watcher started")
        while True:
            cfg = load_config()
            try:
                watch_tick(cfg)
            except CrewError as e:
                print(f"tick failed: {e}", flush=True)
            if args.once:
                return
            time.sleep(cfg["watch"]["interval_seconds"])


def cmd_doctor(args):
    cfg = load_config()
    probe_cmd = ("echo tmux: $(tmux -V 2>/dev/null || echo MISSING); "
                 "echo claude: $(claude --version 2>/dev/null || echo MISSING)")

    def check(host):
        h = cfg["hosts"][host]
        r = sh(cfg, host, probe_cmd + "".join(
            f"; test -d {path_expr(p)} && echo 'repo {p}: ok' || echo 'repo {p}: MISSING'"
            for p in h.get("repos", [])), timeout=20)
        if r.returncode:
            return [f"unreachable ({(r.stderr.strip().splitlines() or ['?'])[-1]})"]
        return r.stdout.splitlines()

    with cf.ThreadPoolExecutor() as ex:
        results = dict(zip(cfg["hosts"], ex.map(check, cfg["hosts"])))
    for host, lines in results.items():
        print(f"{host} ({cfg['hosts'][host].get('ssh') or 'local'})")
        for line in lines:
            print(f"  {line}")
    print(f"\nwatcher: {'running' if watch_running() else 'not running'}\n")
    cmd_acct_ls(args)


def main():
    p = argparse.ArgumentParser(prog="crew", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("init", help="write a starter config").set_defaults(fn=cmd_init)
    sub.add_parser("ui", help="full-screen dashboard (also the default)").set_defaults(fn=cmd_ui)
    sub.add_parser("ls", help="list sessions on every host").set_defaults(fn=cmd_ls)
    sub.add_parser("doctor", help="check hosts, repos, watcher and accounts").set_defaults(fn=cmd_doctor)
    sub.add_parser("usage", help="refresh and show usage left on every account").set_defaults(fn=cmd_usage)

    o = sub.add_parser("open", help="start (or attach to) claude in a repo")
    o.add_argument("host", nargs="?")
    o.add_argument("repo", nargs="?", help="configured repo, its basename, or a path")
    o.add_argument("-a", "--account")
    o.add_argument("-d", "--detach", action="store_true", help="start without attaching")
    o.add_argument("-n", "--new", action="store_true", help="start another session even if one is running")
    o.set_defaults(fn=cmd_open)  # `crew open HOST REPO -- ARGS` passes ARGS to claude

    for name, fn, hlp in [("attach", cmd_attach, "attach to a session"),
                          ("kill", cmd_kill, "end a session")]:
        s = sub.add_parser(name, help=hlp)
        s.add_argument("host")
        s.add_argument("session", help="session name or repo basename")
        s.set_defaults(fn=fn)

    g = sub.add_parser("grid", help="view several sessions at once, tiled")
    g.add_argument("targets", nargs="*", help="HOST/REPO ... (default: every running session)")
    g.set_defaults(fn=cmd_grid)

    s = sub.add_parser("switch", help="move a session to another account, keeping the conversation")
    s.add_argument("host")
    s.add_argument("session")
    s.add_argument("-a", "--account", help="target account (default: next available)")
    s.add_argument("--mark", action="store_true", help="mark the current account exhausted for 5h")
    s.add_argument("--until", help="mark the current account exhausted until HH:MM or ISO time")
    s.set_defaults(fn=cmd_switch)

    w = sub.add_parser("watch", help="auto-switch accounts on usage limits")
    w.add_argument("action", nargs="?", choices=["run", "install", "uninstall", "status"], default="run")
    w.add_argument("--once", action="store_true", help="run a single check and exit")
    w.set_defaults(fn=cmd_watch)

    a = sub.add_parser("acct", help="manage accounts").add_subparsers(dest="acct_cmd", required=True)
    a.add_parser("ls").set_defaults(fn=cmd_acct_ls)
    for name, fn in [("login", cmd_acct_login), ("set", cmd_acct_set), ("exhaust", cmd_acct_exhaust),
                     ("reset", cmd_acct_reset)]:
        s = a.add_parser(name)
        s.add_argument("name")
        if name == "exhaust":
            s.add_argument("--until", help="HH:MM or ISO time (default: 5h from now)")
        s.set_defaults(fn=fn)

    # Everything after `--` goes to claude untouched.
    argv = sys.argv[1:]
    split = argv.index("--") if "--" in argv else len(argv)
    args = p.parse_args(argv[:split])
    args.claude_args = argv[split + 1:]
    try:
        (getattr(args, "fn", None) or cmd_ui)(args)
    except CrewError as e:
        print(f"crew: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
