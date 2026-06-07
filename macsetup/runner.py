from __future__ import annotations

import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from .model import CommandResult, CommandSpec
from .ownership import UserIdentity

HOMEBREW_TOOL_DIRS = (
    Path("/opt/homebrew/bin"),
    Path("/opt/homebrew/sbin"),
    Path("/usr/local/bin"),
    Path("/usr/local/sbin"),
)


def display_command(command: CommandSpec) -> str:
    if command.shell is not None:
        return command.shell
    return " ".join(shlex.quote(part) for part in command.argv)


def _path_with_dirs(path: str, directories: tuple[Path, ...]) -> str:
    parts = [part for part in path.split(os.pathsep) if part]
    for directory in reversed(directories):
        text = str(directory)
        if text not in parts:
            parts.insert(0, text)
    return os.pathsep.join(parts)


def _path_with_homebrew_bins(path: str) -> str:
    existing = tuple(
        directory for directory in HOMEBREW_TOOL_DIRS if directory.is_dir()
    )
    return _path_with_dirs(path, existing)


class CommandFailed(RuntimeError):
    def __init__(self, result: CommandResult) -> None:
        super().__init__(f"command failed with {result.returncode}: {result.command}")
        self.result = result


@dataclass
class CommandRunner:
    dry_run: bool = False
    verbose: bool = False
    progress: bool = False
    heartbeat_seconds: float = 5.0
    path_dirs: list[Path] = field(default_factory=list)
    last_interrupted_command: str | None = None

    def which(self, name: str) -> str | None:
        return shutil.which(name, path=self._path(os.environ.get("PATH", "")))

    def add_path_dir(self, directory: Path) -> None:
        expanded = directory.expanduser()
        if expanded not in self.path_dirs:
            self.path_dirs.insert(0, expanded)

    def run(
        self,
        command: CommandSpec,
        *,
        check: bool = False,
        capture: bool = True,
        dry_run: bool | None = None,
        summarize_output: bool = True,
        timeout_seconds: float | None = None,
        heartbeat: bool = True,
    ) -> CommandResult:
        effective_dry_run = self.dry_run if dry_run is None else dry_run
        display = display_command(command)
        if self.verbose or effective_dry_run:
            print(f"$ {display}")
        if effective_dry_run:
            self._state("command.skip", display)
            return CommandResult(command=display, returncode=0, skipped=True)

        env = self._env(command.env)
        started_at = time.monotonic()
        self.last_interrupted_command = None
        self._state("command.start", display)
        needs_controlling_tty = _command_needs_controlling_tty(command)
        process = subprocess.Popen(
            command.shell if command.shell is not None else command.argv,
            shell=command.shell is not None,
            cwd=command.cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            bufsize=1,
            start_new_session=os.name == "posix" and not needs_controlling_tty,
        )
        try:
            stdout, stderr, timed_out = self._wait_for_process(
                process,
                display=display,
                started_at=started_at,
                capture=capture,
                summarize_output=summarize_output,
                timeout_seconds=timeout_seconds,
                heartbeat=heartbeat,
            )
        except KeyboardInterrupt:
            self._state("command.interrupted", display)
            self.last_interrupted_command = display
            self._stop_process(process)
            raise
        result = CommandResult(
            command=display,
            returncode=124
            if timed_out
            else process.returncode
            if process.returncode is not None
            else 1,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
        )
        elapsed = time.monotonic() - started_at
        self._state(
            "command.done", f"exit={result.returncode} elapsed={elapsed:.1f}s {display}"
        )
        if check and not result.ok:
            raise CommandFailed(result)
        return result

    def _env(self, overrides: Mapping[str, str]) -> dict[str, str]:
        env = dict(os.environ)
        env.update(overrides)
        env["PATH"] = self._path(env.get("PATH", ""))
        return env

    def _path(self, path: str) -> str:
        existing_homebrew_dirs = tuple(
            directory for directory in HOMEBREW_TOOL_DIRS if directory.is_dir()
        )
        return _path_with_dirs(path, (*tuple(self.path_dirs), *existing_homebrew_dirs))

    def state(self, state: str, detail: str) -> None:
        self._state(state, detail)

    def sudo_validate(self, *, timeout_seconds: float = 120.0) -> CommandResult:
        self._state("sudo.auth", "validating sudo credentials")
        return self.run(
            CommandSpec(argv=("sudo", "-v"), needs_tty=True),
            check=False,
            capture=False,
            summarize_output=False,
            timeout_seconds=timeout_seconds,
            heartbeat=False,
        )

    def _state(self, state: str, detail: str) -> None:
        if self.progress:
            print(f"STATE    {state:<18} {detail}", flush=True)

    def _wait_for_process(
        self,
        process: subprocess.Popen[str],
        *,
        display: str,
        started_at: float,
        capture: bool,
        summarize_output: bool,
        timeout_seconds: float | None,
        heartbeat: bool,
    ) -> tuple[str, str, bool]:
        deadline = started_at + timeout_seconds if timeout_seconds is not None else None
        timed_out = False
        if not capture:
            next_heartbeat = time.monotonic() + self.heartbeat_seconds
            while process.poll() is None:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    timed_out = True
                    self._state(
                        "command.timeout", f"after={timeout_seconds:.1f}s {display}"
                    )
                    self._stop_process(process)
                    break
                sleep_for = 0.2
                if deadline is not None:
                    sleep_for = min(sleep_for, max(0.01, deadline - now))
                time.sleep(sleep_for)
                now = time.monotonic()
                if heartbeat and now >= next_heartbeat:
                    elapsed = now - started_at
                    self._state("command.running", f"{display} elapsed={elapsed:.0f}s")
                    next_heartbeat = time.monotonic() + self.heartbeat_seconds
            process.wait()
            stderr = _timeout_message(timeout_seconds) if timed_out else ""
            return "", stderr, timed_out

        output_queue: queue.Queue[tuple[str, str | None, bool]] = queue.Queue()
        threads: list[threading.Thread] = []
        streams: list[tuple[str, IO[str]]] = []
        if process.stdout is not None:
            streams.append(("stdout", process.stdout))
        if process.stderr is not None:
            streams.append(("stderr", process.stderr))
        for stream_name, stream in streams:
            thread = threading.Thread(
                target=self._read_stream,
                args=(stream_name, stream, output_queue),
                daemon=True,
            )
            thread.start()
            threads.append(thread)

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        open_streams = len(threads)
        next_heartbeat = time.monotonic() + self.heartbeat_seconds
        drain_deadline: float | None = None
        prompt_active = False

        def handle_output_item(
            stream_name: str, line: str | None, prompt_fragment: bool
        ) -> bool:
            nonlocal open_streams, prompt_active
            if line is None:
                open_streams -= 1
                return False
            if stream_name == "stdout":
                stdout_parts.append(line)
            else:
                stderr_parts.append(line)
            if summarize_output:
                prompt_active = self._progress_output(
                    stream_name, line, prompt_fragment
                ) or (prompt_active and not line.endswith("\n"))
            return True

        while open_streams:
            now = time.monotonic()
            if (
                not timed_out
                and deadline is not None
                and now >= deadline
                and process.poll() is None
            ):
                timed_out = True
                self._state(
                    "command.timeout", f"after={timeout_seconds:.1f}s {display}"
                )
                self._stop_process(process)
                drain_deadline = time.monotonic() + 5
            if timed_out and drain_deadline is not None and now >= drain_deadline:
                break
            wait_until = next_heartbeat
            if deadline is not None and not timed_out:
                wait_until = min(wait_until, deadline)
            if drain_deadline is not None:
                wait_until = min(wait_until, drain_deadline)
            timeout = min(0.5, max(0.01, wait_until - time.monotonic()))
            try:
                stream_name, line, prompt_fragment = output_queue.get(timeout=timeout)
            except queue.Empty:
                now = time.monotonic()
                if (
                    heartbeat
                    and not prompt_active
                    and now >= next_heartbeat
                    and process.poll() is None
                ):
                    pending_output = self._get_output_before_heartbeat(output_queue)
                    if pending_output is not None:
                        stream_name, line, prompt_fragment = pending_output
                    else:
                        elapsed = now - started_at
                        self._state(
                            "command.running", f"{display} elapsed={elapsed:.0f}s"
                        )
                        next_heartbeat = time.monotonic() + self.heartbeat_seconds
                        continue
                else:
                    continue
            if not handle_output_item(stream_name, line, prompt_fragment):
                continue
            if (
                heartbeat
                and not prompt_active
                and time.monotonic() >= next_heartbeat
                and process.poll() is None
            ):
                pending_output = self._get_output_before_heartbeat(output_queue)
                if pending_output is not None:
                    handle_output_item(*pending_output)
                else:
                    elapsed = time.monotonic() - started_at
                    self._state("command.running", f"{display} elapsed={elapsed:.0f}s")
                    next_heartbeat = time.monotonic() + self.heartbeat_seconds

        process.wait()
        for thread in threads:
            thread.join(timeout=1)
        stderr = "".join(stderr_parts)
        if timed_out:
            stderr = _append_line(stderr, _timeout_message(timeout_seconds))
        return "".join(stdout_parts), stderr, timed_out

    def _get_output_before_heartbeat(
        self, output_queue: queue.Queue[tuple[str, str | None, bool]]
    ) -> tuple[str, str | None, bool] | None:
        grace_seconds = min(0.05, max(0.02, self.heartbeat_seconds))
        try:
            return output_queue.get(timeout=grace_seconds)
        except queue.Empty:
            return None

    def _stop_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return
        except OSError:
            process.terminate()
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return
        except OSError:
            process.kill()
        process.wait()

    def _read_stream(
        self,
        stream_name: str,
        stream: IO[str],
        output_queue: queue.Queue[tuple[str, str | None, bool]],
    ) -> None:
        pending = ""
        try:
            while True:
                character = stream.read(1)
                if character == "":
                    break
                pending += character
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    output_queue.put((stream_name, line + "\n", False))
                if pending and self._is_prompt_fragment(stream_name, pending):
                    output_queue.put((stream_name, pending, True))
                    pending = ""
        finally:
            if pending:
                output_queue.put(
                    (
                        stream_name,
                        pending,
                        self._is_prompt_fragment(stream_name, pending),
                    )
                )
            stream.close()
            output_queue.put((stream_name, None, False))

    def _progress_output(
        self, stream_name: str, raw_line: str, prompt_fragment: bool
    ) -> bool:
        if not self.progress:
            return prompt_fragment
        line = self._clean_output_line(raw_line)
        if not line:
            return prompt_fragment
        if prompt_fragment or self._is_prompt_fragment(stream_name, line):
            print(f"PROMPT   {stream_name:<6} {line}", flush=True)
            return True
        if not self._is_progress_line(stream_name, line):
            return False
        print(f"OUTPUT   {stream_name:<6} {line}", flush=True)
        return False

    def _clean_output_line(self, raw_line: str) -> str:
        without_ansi = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw_line)
        return without_ansi.replace("\r", "").strip()

    def _is_progress_line(self, stream_name: str, line: str) -> bool:
        lower = line.lower()
        prefixes = (
            "==>",
            "error:",
            "warning:",
            "fatal:",
            "cloning into",
            "downloading",
            "downloaded",
            "fetching",
            "installing",
            "installed",
            "already installed",
            "already downloaded",
            "upgrading",
            "linking",
            "unlinking",
            "pouring",
            "collecting ",
            "using cached",
            "preparing metadata",
            "getting requirements",
            "successfully built",
            "successfully uninstalled",
            "running setup.py",
            "building wheel",
            "installing collected packages",
            "successfully installed",
            "requirement already satisfied",
            "added ",
            "changed ",
            "removed ",
            "npm err!",
            "npm warn",
        )
        if lower.startswith(prefixes):
            return True
        return bool(
            stream_name == "stderr"
            and any(token in lower for token in ("error", "warning", "failed", "fatal"))
        )

    def _is_prompt_fragment(self, _stream_name: str, fragment: str) -> bool:
        line = self._clean_output_line(fragment).lower()
        if not line:
            return False
        if "authentication required" in line:
            return True
        return bool(("password" in line or "passphrase" in line) and line.endswith(":"))


def _timeout_message(timeout_seconds: float | None) -> str:
    if timeout_seconds is None:
        return "command timed out"
    return f"command timed out after {timeout_seconds:.1f}s"


def _append_line(existing: str, line: str) -> str:
    if not existing:
        return line
    if existing.endswith("\n"):
        return existing + line
    return existing + "\n" + line


def _command_needs_controlling_tty(command: CommandSpec) -> bool:
    if command.needs_tty:
        return True
    if command.argv:
        return Path(command.argv[0]).name == "sudo"
    if command.shell:
        return re.search(r"(^|[;&|]\s*)sudo(?:\s|$)", command.shell) is not None
    return False


@dataclass
class LocalContext:
    home: Path
    repo_root: Path
    backup_root: Path
    runner: CommandRunner
    dry_run: bool = False
    allow_bootstrap: bool = False
    allow_privileged: bool = False
    enable_dns_blocklist: bool = True
    state_root: Path | None = None
    state_owner: UserIdentity | None = None

    def command_exists(self, name: str) -> bool:
        return self.runner.which(name) is not None

    def brew_prefix(self) -> Path | None:
        brew = self.runner.which("brew")
        if brew is None:
            return None
        result = self.runner.run(
            CommandSpec(argv=(brew, "--prefix")),
            capture=True,
            dry_run=False,
        )
        if not result.ok:
            return None
        prefix = result.stdout.strip()
        if not prefix:
            return None
        return Path(prefix)
