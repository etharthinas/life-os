"""Full-screen crew dashboard: usage left per account, sessions on every host."""
import curses
import datetime as dt
import threading
import time

import crew

LIVE_REFRESH = 8          # seconds between tmux polls of every host
AUTO_PROBE_AFTER = 30 * 60  # probe on open when usage is older than this and no watcher runs

GREEN, YELLOW, RED, DIM, HEAD, SEL = range(1, 7)


def until_text(epoch):
    secs = epoch - time.time()
    if secs <= 0:
        return "now"
    if secs < 86400:
        h, m = divmod(int(secs) // 60, 60)
        return f"{h}h{m:02d}m" if h else f"{m}m"
    return f"{dt.datetime.fromtimestamp(epoch):%a %H:%M}"


def ago(epoch):
    secs = int(time.time() - epoch)
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"


class App:
    def __init__(self, scr, cfg):
        self.scr, self.cfg = scr, cfg
        self.live, self.live_at = {}, 0
        self.lock = threading.Lock()
        self.msg, self.msg_at = "", 0
        self.busy = set()
        self.sel = 0
        self.marked = set()   # (host, name) of sessions picked for the grid
        self.wake = threading.Event()
        threading.Thread(target=self.poll_live, daemon=True).start()

    # --- background work -----------------------------------------------------

    def poll_live(self):
        while True:
            live = crew.all_live_sessions(self.cfg)
            with self.lock:
                self.live, self.live_at = live, time.time()
            self.wake.wait(LIVE_REFRESH)
            self.wake.clear()

    def bg(self, label, fn, done=None):
        def run():
            self.busy.add(label)
            try:
                fn()
                if done:
                    self.say(done)
            except crew.CrewError as e:
                self.say(str(e))
            finally:
                self.busy.discard(label)
                self.wake.set()
        threading.Thread(target=run, daemon=True).start()

    def say(self, msg):
        self.msg, self.msg_at = msg, time.time()

    # --- model -----------------------------------------------------------------

    def rows(self, st):
        """Selectable rows: running sessions, then repos that could be started."""
        out = []
        with self.lock:
            live = dict(self.live)
        for host, sessions in live.items():
            for s in sessions or []:
                meta = st["sessions"].get(f"{host}/{s['name']}", {})
                out.append({"kind": "session", "host": host, "name": s["name"], "attached": s["attached"],
                            "repo": meta.get("repo", "?"), "account": meta.get("account", "?")})
        for host, h in self.cfg["hosts"].items():
            if live.get(host) is None:
                continue
            running = {s["name"] for s in live[host]}
            for repo in h.get("repos", []):
                if crew.session_name(repo) not in running:
                    out.append({"kind": "start", "host": host, "repo": repo})
        return out

    def account_rows(self, st):
        out = []
        for a in self.cfg["accounts"]:
            if a.get("kind", "login") == "login":
                keys = sorted(k for k in st["usage"] if k.startswith(f"{a['name']}@"))
                out += [(k, a) for k in keys] or [(a["name"], a)]
            else:
                out.append((a["name"], a))
        return out

    # --- drawing ---------------------------------------------------------------

    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.scr.addnstr(y, x, text, w - x - 1, attr)
            except curses.error:
                pass

    def rule(self, y, title):
        w = self.scr.getmaxyx()[1]
        self.put(y, 0, "─" * (w - 1), curses.color_pair(DIM))
        self.put(y, 2, f" {title} ", curses.A_BOLD)

    def window_cell(self, y, x, label, win):
        """`5h ██████░░░░  62% left ↻ 2h10m` with the bar coloured by what's left."""
        self.put(y, x, label, curses.color_pair(DIM))
        x += len(label) + 1
        if not win or win["resets_at"] <= time.time():
            self.put(y, x, "·" * 10 + "     –", curses.color_pair(DIM))
            return
        left = max(0.0, 100 - win["used"])
        color = curses.color_pair(GREEN if left >= 50 else YELLOW if left >= 20 else RED)
        filled = round(left / 10)
        self.put(y, x, "█" * filled, color)
        self.put(y, x + filled, "░" * (10 - filled), curses.color_pair(DIM))
        self.put(y, x + 11, f"{left:>3.0f}% left", color | curses.A_BOLD)
        self.put(y, x + 21, f"↻ {until_text(win['resets_at'])}", curses.color_pair(DIM))

    def draw(self):
        st = crew.load_state()
        rows = self.rows(st)
        self.sel = max(0, min(self.sel, len(rows) - 1))
        self.scr.erase()
        h, w = self.scr.getmaxyx()

        running = crew.watch_running()
        self.put(0, 1, "crew", curses.A_BOLD | curses.color_pair(HEAD))
        self.put(0, 6, "Claude sessions across your machines", curses.color_pair(DIM))
        status = ("● auto-switch on" if running else "○ auto-switch off (w)")
        right = f"{status}   {dt.datetime.now():%H:%M}"
        self.put(0, w - len(right) - 2, right, curses.color_pair(GREEN if running else YELLOW))

        y = 2
        self.rule(y, "ACCOUNTS · usage left")
        y += 1
        in_use = {}
        for host, sessions in self.live.items():
            for s in sessions or []:
                acct = st["sessions"].get(f"{host}/{s['name']}", {}).get("account")
                in_use[acct] = in_use.get(acct, 0) + 1
        for key, a in self.account_rows(st):
            u = st["usage"].get(key, {})
            self.put(y, 2, key[:22], curses.A_BOLD)
            if a.get("kind") == "apikey":
                self.put(y, 25, "API key · pay as you go", curses.color_pair(DIM))
            elif a.get("kind", "login") == "login" and key == a["name"]:
                self.put(y, 25, "usage shows once a session runs on it", curses.color_pair(DIM))
            elif a.get("kind") == "oauth" and not crew.keychain_get(a["name"]):
                self.put(y, 25, f"no token — crew acct login {a['name']}", curses.color_pair(RED))
            else:
                self.window_cell(y, 25, "5h", u.get("five_hour"))
                self.window_cell(y, 62, "7d", u.get("seven_day"))
            exh = crew.exhausted_until(st, key)
            if exh:
                note, attr = f"EXHAUSTED · back {until_text(exh.timestamp())}", curses.color_pair(RED) | curses.A_BOLD
            elif in_use.get(a["name"]):
                note, attr = f"in use ×{in_use[a['name']]}", curses.color_pair(GREEN)
            else:
                note, attr = "", 0
            if u.get("updated"):
                self.put(y, 99, ago(u["updated"]), curses.color_pair(DIM))
            self.put(y, 109, note, attr)
            y += 1

        y += 1
        loading = "" if self.live_at else "  (connecting to hosts…)"
        self.rule(y, "SESSIONS" + loading)
        y += 1
        unreachable = [hst for hst, s in self.live.items() if s is None]
        for i, r in enumerate(rows):
            if y >= h - 6:
                break
            selected = i == self.sel
            base = curses.color_pair(SEL) if selected else 0
            if selected:
                self.put(y, 0, " " * (w - 1), base)
            self.put(y, 1, "▶" if selected else " ", base)
            if r["kind"] == "session" and (r["host"], r["name"]) in self.marked:
                self.put(y, 2, "✓", base | curses.A_BOLD)
            if r["kind"] == "session":
                self.put(y, 3, "●", base | curses.color_pair(GREEN) if not selected else base)
                self.put(y, 5, f"{r['host']:<11} {r['repo']:<28} {r['account']:<12}", base | curses.A_BOLD)
                self.put(y, 59, "attached" if r["attached"] else "", base)
            else:
                self.put(y, 3, "+", base | curses.color_pair(DIM))
                self.put(y, 5, f"{r['host']:<11} {r['repo']:<28}", base | (0 if selected else curses.color_pair(DIM)))
                self.put(y, 46, "start", base | (0 if selected else curses.color_pair(DIM)))
            y += 1
        if unreachable:
            self.put(y, 3, f"unreachable: {', '.join(unreachable)}", curses.color_pair(RED))
            y += 1

        log = st["log"][-3:]
        if log and y < h - 5:
            y += 1
            self.rule(y, "ACTIVITY")
            y += 1
            for e in log:
                self.put(y, 2, f"{dt.datetime.fromtimestamp(e['ts']):%m-%d %H:%M}  {e['msg']}",
                         curses.color_pair(DIM))
                y += 1

        if self.busy:
            self.put(h - 3, 2, "… " + ", ".join(sorted(self.busy)), curses.color_pair(YELLOW))
        if self.msg and time.time() - self.msg_at < 8:
            self.put(h - 2, 2, self.msg, curses.color_pair(YELLOW) | curses.A_BOLD)
        grid = f"g grid ({len(self.marked)} marked)" if self.marked else "g grid (all)"
        keys = f"⏎ attach/start  space mark  {grid}  n new  s switch  x kill  r refresh  w auto-switch  q quit"
        self.put(h - 1, 0, " " * (w - 1), curses.color_pair(HEAD) | curses.A_REVERSE)
        self.put(h - 1, 1, keys, curses.color_pair(HEAD) | curses.A_REVERSE)
        self.scr.refresh()
        return rows, st

    # --- interaction -----------------------------------------------------------

    def ask(self, prompt):
        h = self.scr.getmaxyx()[0]
        self.put(h - 2, 2, prompt + " [y/N] ", curses.color_pair(RED) | curses.A_BOLD)
        self.scr.refresh()
        self.scr.timeout(-1)
        ch = self.scr.getch()
        self.scr.timeout(500)
        return ch in (ord("y"), ord("Y"))

    def modal(self, title, options):
        """Pick one of options (label strings); returns its index or None."""
        sel = 0
        self.scr.timeout(-1)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                bw = min(w - 4, max(len(title), *(len(o) for o in options)) + 8)
                bh = len(options) + 4
                top, left = (h - bh) // 2, (w - bw) // 2
                win = curses.newwin(bh, bw, top, left)
                win.bkgd(" ", curses.color_pair(0))
                win.box()
                win.addnstr(0, 2, f" {title} ", bw - 4, curses.A_BOLD)
                for i, o in enumerate(options):
                    win.addnstr(2 + i, 2, ("▶ " if i == sel else "  ") + o, bw - 4,
                                curses.color_pair(SEL) if i == sel else 0)
                win.refresh()
                ch = self.scr.getch()
                if ch in (curses.KEY_UP, ord("k")):
                    sel = (sel - 1) % len(options)
                elif ch in (curses.KEY_DOWN, ord("j")):
                    sel = (sel + 1) % len(options)
                elif ch in (10, 13, curses.KEY_ENTER):
                    return sel
                elif ch in (27, ord("q")):
                    return None
        finally:
            self.scr.timeout(500)

    def suspend(self, fn):
        curses.def_prog_mode()
        curses.endwin()
        try:
            fn()
        finally:
            curses.reset_prog_mode()
            self.scr.clear()
            self.wake.set()

    def grid(self, targets):
        try:
            crew.open_grid(self.cfg, targets)
        except crew.CrewError as e:
            self.say(str(e))

    def open_row(self, r, new=False):
        host = r["host"]
        if r["kind"] == "session" and not new:
            self.suspend(lambda: crew.attach(self.cfg, host, r["name"]))
            return
        self.say(f"starting claude in {host}:{r['repo']}…")
        self.draw()
        try:
            name = crew.start_session(self.cfg, host, r["repo"], new=new)
        except crew.CrewError as e:
            self.say(str(e))
            return
        self.suspend(lambda: crew.attach(self.cfg, host, name))

    def switch_row(self, r, st):
        if r["kind"] != "session":
            return self.say("select a running session to switch its account")
        choices = []
        for a in self.cfg["accounts"]:
            if a["name"] == r["account"]:
                continue
            key = crew.usage_key(a, r["host"])
            five = st["usage"].get(key, {}).get("five_hour")
            left = f"{max(0, 100 - five['used']):.0f}% left" if five and five["resets_at"] > time.time() else ""
            exh = " · exhausted" if crew.exhausted_until(st, key) else ""
            choices.append((a["name"], f"{a['name']:<14} {left}{exh}"))
        i = self.modal(f"Move {r['name']} from {r['account']} to…", [c[1] for c in choices])
        if i is None:
            return
        target = choices[i][0]
        self.bg(f"switching {r['name']}",
                lambda: crew.switch_session(self.cfg, r["host"], r["name"], target),
                f"{r['name']} now on {target} (conversation continued)")

    def run(self):
        curses.curs_set(0)
        curses.use_default_colors()
        for pair, color in [(GREEN, curses.COLOR_GREEN), (YELLOW, curses.COLOR_YELLOW), (RED, curses.COLOR_RED),
                            (DIM, 8 if curses.COLORS > 8 else curses.COLOR_WHITE), (HEAD, curses.COLOR_CYAN)]:
            curses.init_pair(pair, color, -1)
        curses.init_pair(SEL, curses.COLOR_BLACK, curses.COLOR_CYAN)
        self.scr.timeout(500)

        st = crew.load_state()
        stale = [a["name"] for a in self.cfg["accounts"] if a.get("kind") == "oauth"
                 and time.time() - st["usage"].get(a["name"], {}).get("updated", 0) > AUTO_PROBE_AFTER]
        if stale and not crew.watch_running():
            self.bg("checking usage", lambda: crew.probe_all(self.cfg, stale))

        while True:
            rows, st = self.draw()
            ch = self.scr.getch()
            r = rows[self.sel] if rows else None
            if ch in (ord("q"), 27):
                return
            elif ch in (curses.KEY_UP, ord("k")):
                self.sel = max(0, self.sel - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                self.sel = min(len(rows) - 1, self.sel + 1)
            elif ch in (10, 13, curses.KEY_ENTER) and r:
                self.open_row(r)
            elif ch == ord(" ") and r and r["kind"] == "session":
                self.marked ^= {(r["host"], r["name"])}
                self.sel = min(len(rows) - 1, self.sel + 1)
            elif ch == ord("g"):
                running = [(x["host"], x["name"]) for x in rows if x["kind"] == "session"]
                targets = [t for t in running if t in self.marked] or running
                if not targets:
                    self.say("no running sessions — start some first")
                else:
                    self.suspend(lambda: self.grid(targets))
            elif ch == ord("n") and r:
                self.open_row(r, new=True)
            elif ch == ord("s") and r:
                self.switch_row(r, st)
            elif ch == ord("x") and r:
                if r["kind"] != "session":
                    self.say("nothing running there")
                elif self.ask(f"kill {r['host']}/{r['name']}?"):
                    self.bg(f"killing {r['name']}", lambda r=r: crew.kill_session(self.cfg, r["host"], r["name"]),
                            f"killed {r['name']}")
            elif ch == ord("r"):
                self.bg("checking usage", lambda: crew.probe_all(self.cfg), "usage refreshed")
                self.wake.set()
            elif ch == ord("w"):
                if crew.watch_running():
                    self.bg("stopping watcher", crew.agent_uninstall, "auto-switch off")
                else:
                    self.bg("starting watcher", crew.agent_install,
                            "auto-switch on — runs in the background, even with this closed")


def run(cfg):
    curses.wrapper(lambda scr: App(scr, cfg).run())
