"""Claude, through the Claude Code CLI (`claude -p`), on the owner's subscription.

Auth: CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) in the environment. Locally, the CLI's
own login works too.
"""

import json
import shutil
import subprocess
import tempfile


class ClaudeError(RuntimeError):
    pass


def available() -> bool:
    return shutil.which("claude") is not None


def ask(prompt: str, system: str, schema: dict, model: str = "sonnet", timeout: int = 120) -> dict:
    """One tool-less turn whose answer must match `schema`. Raises ClaudeError on any failure."""
    cmd = ["claude", "-p", "--model", model, "--tools", "", "--no-session-persistence",
           "--output-format", "json", "--system-prompt", system, "--json-schema", json.dumps(schema)]
    try:
        # An empty working directory: no CLAUDE.md or project settings get picked up.
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ClaudeError(repr(e)) from e
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise ClaudeError(f"exit {proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}") from None
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise ClaudeError(str(out.get("result") or out.get("subtype"))[:500])
    return out["structured_output"]
