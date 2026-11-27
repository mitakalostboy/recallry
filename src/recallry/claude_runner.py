"""Restricted, version-pinned local Claude Code invocation for opted-in review."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pwd
import shutil
import stat
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from .review_processes import BoundedRunner, ChildSupervisor

TESTED_CLI_VERSION = "2.1.303"
DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT = 120.0
DEFAULT_BUDGET_USD = "0.50"
MAX_STDIN_BYTES = 256 * 1024

# Environment the CLI receives. Nothing else is inherited: no ANTHROPIC_*,
# proxy, Node, editor or parent Claude session variables.
FIXED_ENV = {
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    "LANG": "en_US.UTF-8",
    "NO_COLOR": "1",
    "CLAUDE_CODE_MAX_RETRIES": "0",
    "API_TIMEOUT_MS": "90000",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
}
ACCEPTED_AUTH = {"authMethod": "claude.ai", "apiProvider": "firstParty"}


def claude_candidates(home: Path | None = None) -> tuple[Path, ...]:
    home = home or Path.home()
    return (
        home / ".local" / "bin" / "claude",
        home / ".claude" / "local" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    )


class CLINotFound(RuntimeError):
    pass


def _trusted_executable(path: Path) -> bool:
    try:
        info = path.stat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        return False
    # Refuse group/world-writable binaries and ones owned by another user.
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return False
    return info.st_uid in (0, os.getuid())


def find_claude(explicit: str | os.PathLike | None = None, home: Path | None = None) -> Path:
    """Resolve the Claude CLI to an absolute, trusted executable path."""
    candidates = (Path(explicit),) if explicit else claude_candidates(home)
    for candidate in candidates:
        if not candidate.is_absolute():
            continue
        resolved = candidate.resolve()
        if _trusted_executable(resolved):
            return resolved
    raise CLINotFound("Claude Code CLI was not found at a trusted absolute path")


class ClaudeUnavailable(RuntimeError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def build_env(home: Path, tmpdir: Path) -> dict[str, str]:
    account = pwd.getpwuid(os.getuid()).pw_name
    return {"HOME": str(home), "USER": account, "LOGNAME": account, "TMPDIR": str(tmpdir), **FIXED_ENV}


def encode_schema(schema: dict) -> str:
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))


def encode_payload(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def build_argv(claude: Path, *, system_prompt: str, schema: dict, model: str = DEFAULT_MODEL,
               max_budget_usd: str = DEFAULT_BUDGET_USD) -> list[str]:
    return [
        str(claude), "-p",
        "--safe-mode",
        "--setting-sources", "",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--tools", "",
        "--disallowedTools", "mcp__*",
        "--disable-slash-commands",
        "--permission-prompts", "none",
        "--no-chrome",
        "--no-session-persistence",
        "--model", model,
        "--max-budget-usd", max_budget_usd,
        "--output-format", "json",
        "--json-schema", encode_schema(schema),
        "--system-prompt", system_prompt,
    ]


@dataclass(frozen=True)
class ClaudeRequest:
    """Model, prompt, output schema and stdin passed to the subprocess.

    ``run_request`` recomputes the request digest
    from these exact fields immediately before spawning the CLI.
    """
    model: str
    system_prompt: str
    schema: dict
    stdin: bytes
    max_budget_usd: str = DEFAULT_BUDGET_USD

    @property
    def digest(self) -> str:
        canonical = json.dumps({
            "model": self.model, "max_budget_usd": self.max_budget_usd,
            "system_prompt": self.system_prompt, "json_schema": encode_schema(self.schema),
            "stdin": self.stdin.decode("utf-8"),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def prepare_request(*, system_prompt: str, schema: dict, payload: dict,
                    model: str = DEFAULT_MODEL) -> ClaudeRequest:
    stdin = encode_payload(payload)
    if len(stdin) > MAX_STDIN_BYTES:
        raise ValueError("payload is too large")
    # Deep-copy through JSON so later mutation of the caller's dict cannot alter the request.
    return ClaudeRequest(model, system_prompt, json.loads(encode_schema(schema)), stdin)


class DigestMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaudeResult:
    structured: dict | None
    envelope: dict | None
    returncode: int | None
    timed_out: bool
    cancelled: bool
    elapsed: float
    stderr: str
    error: str | None


class ClaudeCodeRunner:
    def __init__(self, supervisor: ChildSupervisor, *, claude_path: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT, model: str = DEFAULT_MODEL,
                 home: Path | None = None) -> None:
        self.claude = find_claude(claude_path)
        self.home = home or Path(pwd.getpwuid(os.getuid()).pw_dir)
        self.timeout = timeout
        self.model = model
        self.runner = BoundedRunner(supervisor)

    def cancel(self) -> bool:
        return self.runner.cancel()

    def _sandbox(self) -> tuple[Path, Path, Path]:
        base = Path(tempfile.mkdtemp(prefix="recallry-claude-"))
        os.chmod(base, 0o700)
        work, tmp = base / "work", base / "tmp"
        work.mkdir(mode=0o700)
        tmp.mkdir(mode=0o700)
        return base, work, tmp

    def version(self) -> str:
        base, work, tmp = self._sandbox()
        try:
            result = self.runner.run([str(self.claude), "--version"], env=build_env(self.home, tmp),
                                     cwd=work, stdin_data=b"", timeout=20)
        finally:
            shutil.rmtree(base, ignore_errors=True)
        if result.returncode != 0 or result.timed_out or result.cancelled or result.output_limited:
            raise ClaudeUnavailable("Claude Code version check failed")
        return result.stdout.decode("utf-8", "replace").strip()

    def _auth_raw(self) -> dict:
        base, work, tmp = self._sandbox()
        try:
            result = self.runner.run([str(self.claude), "auth", "status"], env=build_env(self.home, tmp),
                                     cwd=work, stdin_data=b"", timeout=20)
        finally:
            shutil.rmtree(base, ignore_errors=True)
        if result.returncode != 0 or result.timed_out or result.cancelled or result.output_limited:
            raise ClaudeUnavailable("Claude Code auth check failed")
        try:
            data = json.loads(result.stdout, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError):
            raise ClaudeUnavailable("Claude Code auth status was not readable") from None
        if not isinstance(data, dict):
            raise ClaudeUnavailable("Claude Code auth status was not readable")
        return data

    def require_tested_version(self) -> str:
        """Only run a Claude Code version validated with this isolation profile."""
        version = self.version().split(" ")[0]
        if version != TESTED_CLI_VERSION:
            raise ClaudeUnavailable(
                f"Claude Code {version} has not been isolation-tested (tested: {TESTED_CLI_VERSION})")
        return version

    def require_subscription_auth(self, *, include_email: bool = False) -> dict:
        """Refuse to run unless the CLI uses the user's claude.ai login (no API key billing)."""
        data = self._auth_raw()
        status = {key: data.get(key) for key in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")}
        if (status.get("loggedIn") is not True or data.get("apiKeySource") not in (None, "none")
                or any(status.get(k) != v for k, v in ACCEPTED_AUTH.items())):
            raise ClaudeUnavailable(
                "Claude Code is not signed in with a claude.ai subscription; "
                "Recallry will not switch to another auth method or provider")
        if include_email:
            status["email"] = data.get("email") if isinstance(data.get("email"), str) else None
        return status

    def run_request(self, request: ClaudeRequest, *, expected_digest: str,
                    launch_guard=nullcontext) -> ClaudeResult:
        if not hmac.compare_digest(request.digest, expected_digest):
            raise DigestMismatch("the request differs from the one the user approved")
        argv = build_argv(self.claude, system_prompt=request.system_prompt, schema=request.schema,
                          model=request.model, max_budget_usd=request.max_budget_usd)
        stdin = request.stdin
        base, work, tmp = self._sandbox()
        try:
            if any(work.iterdir()):
                raise ClaudeUnavailable("working directory is not empty")
            env = build_env(self.home, tmp)
            result = self.runner.run(argv, env=env, cwd=work, stdin_data=stdin, timeout=self.timeout,
                                     launch_guard=launch_guard)
        finally:
            shutil.rmtree(base, ignore_errors=True)
        stderr = result.stderr.decode("utf-8", "replace")[-2000:]
        if result.output_limited:
            return ClaudeResult(None, None, result.returncode, False, result.cancelled,
                                result.elapsed, "", "output_limit")
        if result.timed_out or result.cancelled:
            return ClaudeResult(None, None, result.returncode, result.timed_out, result.cancelled,
                                result.elapsed, stderr, "timeout" if result.timed_out else "cancelled")
        try:
            envelope = json.loads(result.stdout, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError):
            return ClaudeResult(None, None, result.returncode, False, False, result.elapsed, stderr,
                                "output was not JSON")
        error = None
        structured = envelope.get("structured_output") if isinstance(envelope, dict) else None
        if result.returncode != 0 or not isinstance(envelope, dict) or envelope.get("is_error"):
            error = "claude reported an error"
        elif not isinstance(structured, dict):
            error = "structured output missing"
        return ClaudeResult(structured if error is None else None, envelope, result.returncode,
                            False, False, result.elapsed, stderr, error)
