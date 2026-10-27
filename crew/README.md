# crew

Run Claude Code on any of your machines from this MacBook. You can see how much usage each
account has left, and sessions move to the next account automatically when one hits its limit.

- `crew.py`: core, CLI and watcher. `tui.py`: dashboard. Python 3.11+, stdlib only.
- `~/.crew/config.toml`: hosts, repos, account order, watcher settings
- `~/.crew/state.json`: sessions, usage per account, exhausted accounts, activity log
- `~/.crew/watch.log`: background watcher output

## Dashboard

Run `crew` with no arguments.

```
⏎ attach to a session / start a repo      (Ctrl-b d detaches back to the dashboard)
space  mark sessions     g  open marked sessions (or all running) in a grid
n  start another session in the same repo
s  move the selected session to another account (conversation continues)
x  kill session          r  re-check usage now
w  turn auto-switch on/off                q  quit
```

## Grid view

`crew grid` tiles every running session in one window, and
`crew grid doojeong/frozen-road-pages mba/kelisor` tiles just those. Each pane is the live
session, labelled with host and repo. Click a pane to type into it; `C-g z` zooms one pane
full-screen and back; `C-g d` closes the grid. Closing the grid never stops the sessions.
The grid uses `C-g` as its prefix so `C-b` still goes to the session's own tmux.

## How usage and auto-switch work

- Each session crew starts gets a status line and a `StopFailure` hook, passed with `--settings`.
  The status line saves Claude Code's `rate_limits` (5-hour and 7-day used %, reset time) to
  `~/.crew/usage/` on its host. The hook drops a marker in `~/.crew/events/` when a turn ends on
  a rate limit.
- The watcher is a LaunchAgent (`com.crew.watch`) that checks every host every 20 s. A session
  whose account is at 100% or hit a rate limit is marked exhausted until its reset time. The
  session is then restarted in place on the next account with `claude --continue` plus a short
  "continue where you left off" prompt, and you get a macOS notification.
- For accounts not in use, the watcher runs a ~600-token Haiku request every 30 min and reads the
  `rate_limit_event` it returns. It also runs one right after an exhausted account's reset time.
- Accounts are chosen in config order, skipping exhausted ones and preferring any with more than
  5% of the 5-hour window left.

## CLI

```
crew open doojeong frozen-road-pages [-a acct2] [-d] [-n] [-- claude args]
crew ls  |  crew attach HOST REPO  |  crew kill HOST REPO
crew switch HOST REPO [-a acct] [--until 18:00]
crew usage                     # probe all accounts and print usage left
crew acct ls | login NAME | set NAME | exhaust NAME [--until HH:MM] | reset NAME
crew watch install | uninstall | status      (crew watch --once runs one check)
crew doctor
```

## Notes

- Sessions use crew's status line, which replaces your own status line in those sessions only.
- The first time a session opens a folder, Claude Code asks whether you trust it. Attach and
  answer once per folder.
- Adding a host: make it reachable by SSH alias, install tmux and claude, add a `[hosts.<name>]`
  block, then run `crew doctor`.
