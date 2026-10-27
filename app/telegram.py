"""Telegram bot via long polling (no public URL or webhook needed). Run with `python -m app.telegram`.

Commands:  /t <todo>   /todos   /done <id>   /log <text>   (plain text = a log entry)
"""

import html
import time

import httpx

from . import capture, config, journal

HELP = (
    "<b>life-os</b>\n"
    "/t anything — Claude sorts it into todos\n"
    "/todos — list open todos\n"
    "/done 12 — mark todo 12 done\n"
    "/log text — journal entry (plain messages are logged too)"
)


class Bot:
    def __init__(self, token: str):
        self.http = httpx.Client(base_url=f"https://api.telegram.org/bot{token}/", timeout=60)

    def call(self, method: str, **params):
        r = self.http.post(method, json=params)
        r.raise_for_status()
        return r.json()["result"]

    def send(self, chat_id: int, text: str):
        self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")


def format_todo(t: dict) -> str:
    bits = [f"<code>{t['id']}</code> {html.escape(t['title'])}"]
    if t["due_on"]:
        bits.append(t["due_on"].strftime("%m/%d"))
    if t["project"]:
        bits.append(f"#{html.escape(t['project'])}")
    if t["priority"] == 1:
        bits.append("❗")
    return " · ".join(bits)


def handle(text: str) -> str:
    cmd, _, arg = text.strip().partition(" ")
    cmd = cmd.split("@")[0].lower()  # /t@my_bot in group chats
    arg = arg.strip()
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/t":
        if not arg:
            return "Usage: /t buy milk tomorrow, call mom fri"
        cid = capture.scratch(arg, source="telegram")
        capture.process(cid)
        c = capture.load_capture(cid)
        lines = ["Added:", *map(format_todo, c["todos"])] if c["todos"] else ["Nothing to add."]
        if c["note"]:
            lines.append(f"❓ {html.escape(c['note'])}")
        return "\n".join(lines)
    if cmd == "/todos":
        todos = capture.open_todos()
        return "\n".join(map(format_todo, todos)) if todos else "Nothing open."
    if cmd == "/done":
        if not arg.isdigit():
            return "Usage: /done 12"
        t = capture.set_todo_status(int(arg), "done")
        return f"Done: {html.escape(t['title'])}" if t else f"No todo {arg}."
    if cmd == "/log":
        if not arg:
            return "Usage: /log text"
        journal.append(arg, source="telegram")
        return "Logged."
    if cmd.startswith("/"):
        return HELP
    journal.append(text, source="telegram")
    return "Logged."


def run() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    bot = Bot(config.TELEGRAM_BOT_TOKEN)
    offset = None
    print("telegram bot polling")
    while True:
        try:
            updates = bot.call("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
        except httpx.HTTPError as e:
            print("poll error:", e)
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            chat_id, text = msg.get("chat", {}).get("id"), msg.get("text")
            if chat_id is None or not text:
                continue
            if str(chat_id) != config.TELEGRAM_CHAT_ID:
                # Ignore strangers, but tell the owner their id during first-time setup.
                if not config.TELEGRAM_CHAT_ID:
                    bot.send(chat_id, f"Set TELEGRAM_CHAT_ID={chat_id} and restart the bot.")
                continue
            try:
                reply = handle(text)
            except Exception as e:  # keep polling even if one message fails
                print("handler error:", repr(e))
                reply = "Something went wrong; check the bot log."
            bot.send(chat_id, reply)


if __name__ == "__main__":
    run()
