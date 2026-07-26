"""Bounded, read-only repository evidence for MCP and voice agents.

The service deliberately exposes operations, not commands. Search and file reads
use Python filesystem APIs; Git summaries use fixed argv with ``shell=False``.
Repository text is untrusted, redacted, and capped before it leaves the daemon.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .contracts import (
    RepositoryContextItem,
    RepositoryContextQuery,
    RepositoryContextResponse,
)
from .security import sanitize_untrusted_text
from .settings import Settings

_MAX_FILE_BYTES = 256 * 1024
_MAX_SCAN_BYTES = 8 * 1024 * 1024
_MAX_SCAN_FILES = 2_000
_MAX_SCAN_ENTRIES = 5_000
_MAX_GIT_OUTPUT_BYTES = 128 * 1024
_GIT_TRUNCATION_MARKER = "[git output truncated]"
_MAX_RESPONSE_CHARS = 6_000
_READ_CHUNK_LINES = 8
_GIT_TIMEOUT_SECONDS = 5

_SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".hotline",
        ".mypy_cache",
        ".next",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "target",
        "venv",
    }
)
_DENIED_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
    }
)
_DENIED_SUFFIXES = frozenset(
    {
        ".der",
        ".jks",
        ".key",
        ".p12",
        ".pfx",
        ".pkcs12",
        ".pem",
    }
)
_TEXT_SUFFIXES = frozenset(
    {
        "",
        ".c",
        ".cfg",
        ".conf",
        ".cpp",
        ".cs",
        ".css",
        ".csv",
        ".dockerfile",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".md",
        ".mjs",
        ".php",
        ".properties",
        ".ps1",
        ".py",
        ".rb",
        ".rs",
        ".scss",
        ".sh",
        ".sql",
        ".svg",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_TEST_FILE_RE = re.compile(
    r"(?i)(?:^|[/\\])(?:tests?|specs?)(?:[/\\]|$)|"
    r"(?:^|[/\\])(?:test_[^/\\]+|[^/\\]+[._](?:test|spec))\."
)
_PYTHON_TEST_RE = re.compile(r"(?m)^\s*(?:async\s+)?def\s+test_[A-Za-z0-9_]+\s*\(")
_JS_TEST_RE = re.compile(r"(?m)\b(?:it|test)\s*\(")
_STRUCTURED_SECRET_RE = re.compile(
    r"""(?ix)
    (?P<prefix>
      (?<![A-Z0-9_])
      (?:export\s+)?
      ["']?
      (?:
        [A-Z_][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|
          PASSPHRASE)[A-Z0-9_]* |
        [A-Z_][A-Z0-9_]*_KEY[A-Z0-9_]* |
        API_KEY|ACCESS_KEY|PRIVATE_KEY|DATABASE_URL|DB_URL|PASSPHRASE|DSN|
          CONNECTION_STRING
      )
      ["']?\s*[:=]\s*
    )
    (?P<value>
      "(?:\\.|[^"\r\n])*" |
      '(?:\\.|[^'\r\n])*' |
      [^\s,;&}\]]+
    )
    """
)
_CREDENTIALED_URL_RE = re.compile(r"(?i)\b(?P<scheme>[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^@\s/]+@")


@dataclass(frozen=True)
class WorkspaceRoot:
    path: Path
    label: str
    reference: str


@dataclass
class ScanBudget:
    entries: int = 0
    truncated: bool = False


class RepositoryContextService:
    """Resolve allowlisted roots and return only bounded repository evidence."""

    def __init__(
        self,
        settings: Settings,
        *,
        known_secrets: Iterable[str] = (),
    ) -> None:
        configured = settings.workspace_roots
        candidates = configured or (
            (settings.codex_app_server_cwd,) if settings.codex_app_server_cwd else ()
        )
        roots: list[WorkspaceRoot] = []
        seen: set[Path] = set()
        for candidate in candidates:
            if not candidate.is_absolute():
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not resolved.is_dir() or resolved in seen or not _is_git_repository_root(resolved):
                continue
            seen.add(resolved)
            roots.append(
                WorkspaceRoot(
                    path=resolved,
                    label=resolved.name or "workspace",
                    reference=_workspace_reference(resolved),
                )
            )
        self._roots = tuple(roots)
        self._known_secrets = tuple(value for value in known_secrets if value)
        self._git_executable = _resolve_git_executable(
            settings.hotline_git_bin,
            self._roots,
        )

    @property
    def workspaces(self) -> tuple[WorkspaceRoot, ...]:
        return self._roots

    def query(
        self,
        request: RepositoryContextQuery,
        *,
        forced_workspace: str | None = None,
    ) -> RepositoryContextResponse:
        root = self._resolve_workspace(request.workspace, forced_workspace=forced_workspace)
        if request.operation == "status":
            summary, items, truncated = self._status(root, request.max_results)
        elif request.operation == "diff":
            summary, items, truncated = self._diff(root, request.path, request.max_results)
        elif request.operation == "search":
            assert request.query is not None
            summary, items, truncated = self._search(
                root,
                request.query,
                request.path,
                request.max_results,
            )
        elif request.operation == "read":
            assert request.path is not None
            summary, items, truncated = self._read(
                root,
                request.path,
                request.line_start,
                request.line_count,
                request.max_results,
            )
        else:
            summary, items, truncated = self._tests(root, request.max_results)
        return self._bounded_response(root, request, summary, items, truncated)

    def _resolve_workspace(
        self,
        requested: str | None,
        *,
        forced_workspace: str | None,
    ) -> WorkspaceRoot:
        if not self._roots:
            raise ValueError("no repository workspace is configured")
        forced = self._match_workspace(forced_workspace) if forced_workspace else None
        if forced_workspace and forced is None:
            raise PermissionError("event workspace is not allowlisted")
        if forced is not None:
            if requested is not None and self._match_workspace(requested) != forced:
                raise PermissionError("request cannot leave the event workspace")
            return forced
        if requested is None:
            if len(self._roots) != 1:
                raise ValueError("workspace is required when multiple roots are configured")
            return self._roots[0]
        match = self._match_workspace(requested)
        if match is None:
            raise PermissionError("workspace is not allowlisted")
        return match

    def _match_workspace(self, value: str | None) -> WorkspaceRoot | None:
        if value is None:
            return None
        matches = [
            root for root in self._roots if value in {root.label, root.reference, str(root.path)}
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    def _status(
        self,
        root: WorkspaceRoot,
        max_results: int,
    ) -> tuple[str, list[RepositoryContextItem], bool]:
        output = self._git(root.path, "status", "--short", "--branch", "--untracked-files=normal")
        if output is None:
            return "Workspace is not a readable Git repository.", [], False
        lines, git_truncated = _git_lines(output)
        if not lines:
            return "Git status is clean; branch information was unavailable.", [], False
        branch = lines[0] if lines[0].startswith("## ") else "branch unavailable"
        changes = lines[1:] if lines[0].startswith("## ") else lines
        items = [RepositoryContextItem(kind="status", text=line) for line in changes[:max_results]]
        summary = (
            f"{branch[3:] if branch.startswith('## ') else branch}. "
            f"{len(changes)} changed or untracked path"
            f"{'' if len(changes) == 1 else 's'}."
        )
        return summary, items, git_truncated or len(changes) > max_results

    def _diff(
        self,
        root: WorkspaceRoot,
        requested_path: str | None,
        max_results: int,
    ) -> tuple[str, list[RepositoryContextItem], bool]:
        pathspec = self._validated_pathspec(root.path, requested_path)
        args = ("diff", "--no-ext-diff", "--no-textconv", "--stat", "--", pathspec)
        unstaged = self._git(root.path, *args)
        staged = self._git(
            root.path,
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-textconv",
            "--stat",
            "--",
            pathspec,
        )
        status = self._git(
            root.path,
            "status",
            "--short",
            "--untracked-files=normal",
            "--",
            pathspec,
        )
        if unstaged is None or staged is None or status is None:
            return "Workspace is not a readable Git repository.", [], False
        unstaged_lines, unstaged_truncated = _git_lines(unstaged)
        staged_lines, staged_truncated = _git_lines(staged)
        status_lines, status_truncated = _git_lines(status)
        evidence: list[str] = []
        evidence.extend(f"unstaged: {line}" for line in unstaged_lines)
        evidence.extend(f"staged: {line}" for line in staged_lines)
        untracked = [line for line in status_lines if line.startswith("?? ")]
        evidence.extend(f"untracked: {line[3:]}" for line in untracked)
        summary = (
            "No staged or unstaged diff was found."
            if not evidence
            else f"Diff summary contains {len(evidence)} bounded status/stat line(s)."
        )
        items = [RepositoryContextItem(kind="diff", text=line) for line in evidence[:max_results]]
        return (
            summary,
            items,
            unstaged_truncated
            or staged_truncated
            or status_truncated
            or len(evidence) > max_results,
        )

    def _search(
        self,
        root: WorkspaceRoot,
        query: str,
        requested_path: str | None,
        max_results: int,
    ) -> tuple[str, list[RepositoryContextItem], bool]:
        base = self._resolve_repo_path(root.path, requested_path or ".", require_exists=True)
        needle = query.casefold()
        results: list[RepositoryContextItem] = []
        scanned_bytes = 0
        scanned_files = 0
        truncated = False
        budget = ScanBudget()
        for file_path in self._iter_text_files(root.path, base, budget):
            scanned_files += 1
            if scanned_files > _MAX_SCAN_FILES:
                truncated = True
                break
            try:
                size = file_path.stat().st_size
            except OSError:
                continue
            if scanned_bytes + size > _MAX_SCAN_BYTES:
                truncated = True
                break
            scanned_bytes += size
            text = self._read_text_file(file_path, root.path)
            if text is None:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if needle not in line.casefold():
                    continue
                results.append(
                    RepositoryContextItem(
                        kind="match",
                        path=file_path.relative_to(root.path).as_posix(),
                        line=line_number,
                        text=line.strip(),
                    )
                )
                if len(results) >= max_results:
                    truncated = True
                    break
            if len(results) >= max_results:
                break
        summary = (
            f"Found {len(results)} bounded literal match"
            f"{'' if len(results) == 1 else 'es'} for the query."
        )
        return summary, results, truncated or budget.truncated

    def _read(
        self,
        root: WorkspaceRoot,
        requested_path: str,
        line_start: int,
        line_count: int,
        max_results: int,
    ) -> tuple[str, list[RepositoryContextItem], bool]:
        requested_file = self._resolve_repo_path(
            root.path,
            requested_path,
            require_exists=True,
        )
        file_path = self._resolve_allowed_file(requested_file, root.path)
        if file_path is None:
            raise PermissionError("file is not readable repository text")
        text = self._read_text_file(file_path, root.path)
        if text is None:
            raise PermissionError("file is binary, oversized, or unreadable")
        lines = text.splitlines()
        selected = lines[line_start - 1 : line_start - 1 + line_count]
        chunks: list[RepositoryContextItem] = []
        relative = file_path.relative_to(root.path).as_posix()
        for offset in range(0, len(selected), _READ_CHUNK_LINES):
            chunk_lines = selected[offset : offset + _READ_CHUNK_LINES]
            first_line = line_start + offset
            rendered = "\n".join(
                f"{first_line + index}: {line}" for index, line in enumerate(chunk_lines)
            )
            chunks.append(
                RepositoryContextItem(
                    kind="file",
                    path=relative,
                    line=first_line,
                    text=rendered,
                )
            )
        truncated = len(chunks) > max_results or line_start - 1 + line_count < len(lines)
        chunks = chunks[:max_results]
        summary = (
            f"Read {len(selected)} line{'' if len(selected) == 1 else 's'} "
            f"from {relative}, starting at line {line_start}."
        )
        return summary, chunks, truncated

    def _tests(
        self,
        root: WorkspaceRoot,
        max_results: int,
    ) -> tuple[str, list[RepositoryContextItem], bool]:
        test_files: list[tuple[str, int]] = []
        total_cases = 0
        scanned_files = 0
        scanned_bytes = 0
        truncated = False
        budget = ScanBudget()
        for file_path in self._iter_text_files(root.path, root.path, budget):
            scanned_files += 1
            if scanned_files > _MAX_SCAN_FILES:
                truncated = True
                break
            try:
                size = file_path.stat().st_size
            except OSError:
                continue
            if scanned_bytes + size > _MAX_SCAN_BYTES:
                truncated = True
                break
            scanned_bytes += size
            relative = file_path.relative_to(root.path).as_posix()
            if not _TEST_FILE_RE.search(relative):
                continue
            text = self._read_text_file(file_path, root.path)
            if text is None:
                continue
            cases = (
                len(_PYTHON_TEST_RE.findall(text))
                if file_path.suffix.casefold() == ".py"
                else len(_JS_TEST_RE.findall(text))
            )
            total_cases += cases
            test_files.append((relative, cases))
        changed = self._git(
            root.path,
            "status",
            "--short",
            "--untracked-files=normal",
        )
        changed_tests = []
        git_truncated = False
        if changed is not None:
            changed_lines, git_truncated = _git_lines(changed)
            for line in changed_lines:
                candidate = line[3:].strip().strip('"') if len(line) > 3 else ""
                if candidate and _TEST_FILE_RE.search(candidate):
                    changed_tests.append(candidate)
        items = [
            RepositoryContextItem(
                kind="test",
                path=path,
                text=f"{cases} statically discovered test case(s).",
            )
            for path, cases in test_files[:max_results]
        ]
        summary = (
            f"Static inventory found {len(test_files)} test file(s) and "
            f"{total_cases} test case(s); {len(changed_tests)} test file(s) are changed. "
            "This operation does not execute tests and does not claim pass or fail."
        )
        return (
            summary,
            items,
            truncated or budget.truncated or git_truncated or len(test_files) > max_results,
        )

    def _bounded_response(
        self,
        root: WorkspaceRoot,
        request: RepositoryContextQuery,
        summary: str,
        items: list[RepositoryContextItem],
        truncated: bool,
    ) -> RepositoryContextResponse:
        safe_summary = self._sanitize(summary, 1_900)
        safe_workspace = self._sanitize(root.label, 180)
        bounded: list[RepositoryContextItem] = []
        used = len(safe_summary) + len(safe_workspace) + len(root.reference) + 500
        for item in items[: request.max_results]:
            safe_text = self._sanitize(item.text, 1_100)
            safe_path = self._sanitize(item.path, 480) if item.path is not None else None
            remaining = _MAX_RESPONSE_CHARS - used
            if remaining < 64:
                truncated = True
                break
            if len(safe_text) > remaining:
                safe_text = self._sanitize(safe_text, max(64, remaining))
                truncated = True
            bounded.append(item.model_copy(update={"path": safe_path, "text": safe_text}))
            used += len(safe_text) + len(safe_path or "") + 100
        response = RepositoryContextResponse(
            workspace=safe_workspace,
            workspace_ref=root.reference,
            operation=request.operation,
            summary=safe_summary,
            items=bounded,
            truncated=truncated or len(items) > len(bounded),
        )
        # Count actual serialized metadata and JSON overhead rather than only
        # evidence text. Dropping a trailing item preserves a simple hard cap.
        while len(response.model_dump_json()) > _MAX_RESPONSE_CHARS and response.items:
            response.items.pop()
            response.truncated = True
        if len(response.model_dump_json()) > _MAX_RESPONSE_CHARS:
            overflow = len(response.model_dump_json()) - _MAX_RESPONSE_CHARS
            target = max(64, len(response.summary) - overflow - 32)
            response.summary = self._sanitize(response.summary, target)
            response.truncated = True
        return response

    def _resolve_repo_path(
        self,
        root: Path,
        requested: str,
        *,
        require_exists: bool,
    ) -> Path:
        candidate = Path(requested)
        if candidate.is_absolute() or candidate.drive:
            raise PermissionError("repository path must be relative")
        try:
            resolved = (root / candidate).resolve(strict=require_exists)
        except (OSError, RuntimeError) as exc:
            raise ValueError("repository path does not exist") from exc
        if not resolved.is_relative_to(root):
            raise PermissionError("repository path leaves the allowlisted workspace")
        relative_parts = resolved.relative_to(root).parts
        if any(part.casefold() in _SKIP_DIRECTORIES for part in relative_parts):
            raise PermissionError("repository path is excluded")
        return resolved

    def _validated_pathspec(self, root: Path, requested: str | None) -> str:
        if requested is None:
            return "."
        # A diff may name a deleted path, so validate lexically without requiring
        # the target to still exist.
        resolved = self._resolve_repo_path(root, requested, require_exists=False)
        return resolved.relative_to(root).as_posix() or "."

    def _iter_text_files(
        self,
        root: Path,
        base: Path,
        budget: ScanBudget,
    ) -> Iterator[Path]:
        if base.is_file():
            resolved = self._resolve_allowed_file(base, root)
            if resolved is not None:
                yield resolved
            return
        pending = [base]
        while pending and not budget.truncated:
            current = pending.pop()
            try:
                with os.scandir(current) as scanner:
                    entries: list[os.DirEntry[str]] = []
                    for entry in scanner:
                        budget.entries += 1
                        if budget.entries > _MAX_SCAN_ENTRIES:
                            budget.truncated = True
                            break
                        entries.append(entry)
            except OSError:
                continue
            for entry in sorted(entries, key=lambda item: item.name.casefold(), reverse=True):
                candidate = Path(entry.path)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if (
                            entry.name.casefold() not in _SKIP_DIRECTORIES
                            and self._is_safe_walk_directory(candidate, root)
                        ):
                            pending.append(candidate)
                    elif entry.is_file(follow_symlinks=False):
                        resolved = self._resolve_allowed_file(candidate, root)
                        if resolved is not None:
                            yield resolved
                except OSError:
                    continue

    def _is_safe_walk_directory(self, candidate: Path, root: Path) -> bool:
        """Reject symlinks, Windows junctions/reparse points, and outside targets."""

        try:
            metadata = candidate.lstat()
            if candidate.is_symlink():
                return False
            is_junction = getattr(candidate, "is_junction", None)
            if callable(is_junction) and is_junction():
                return False
            attributes = getattr(metadata, "st_file_attributes", 0)
            if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                return False
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        return resolved.is_dir() and resolved.is_relative_to(root)

    def _resolve_allowed_file(self, candidate: Path, root: Path) -> Path | None:
        try:
            candidate_metadata = candidate.lstat()
            candidate_attributes = getattr(candidate_metadata, "st_file_attributes", 0)
            if candidate.is_symlink() or candidate_attributes & getattr(
                stat,
                "FILE_ATTRIBUTE_REPARSE_POINT",
                0,
            ):
                return None
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not resolved.is_file() or not resolved.is_relative_to(root):
            return None
        relative = resolved.relative_to(root)
        if any(part.casefold() in _SKIP_DIRECTORIES for part in relative.parts[:-1]):
            return None
        name = resolved.name.casefold()
        if (
            name in _DENIED_NAMES
            or name.startswith(".env.")
            or resolved.suffix.casefold() in _DENIED_SUFFIXES
            or resolved.suffix.casefold() not in _TEXT_SUFFIXES
        ):
            return None
        try:
            return resolved if resolved.stat().st_size <= _MAX_FILE_BYTES else None
        except OSError:
            return None

    def _read_text_file(self, path: Path, root: Path) -> str | None:
        resolved = self._resolve_allowed_file(path, root)
        if resolved is None:
            return None
        try:
            before = resolved.stat()
        except OSError:
            return None
        flags = os.O_RDONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(resolved, flags)
        except OSError:
            return None
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size > _MAX_FILE_BYTES
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or not _opened_path_is_expected(descriptor, resolved, root)
            ):
                return None
            chunks: list[bytes] = []
            received = 0
            while received <= _MAX_FILE_BYTES:
                chunk = os.read(
                    descriptor,
                    min(64 * 1024, _MAX_FILE_BYTES + 1 - received),
                )
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
            data = b"".join(chunks)
        except OSError:
            return None
        finally:
            os.close(descriptor)
        if len(data) > _MAX_FILE_BYTES or b"\x00" in data:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def _git(self, root: Path, *arguments: str) -> str | None:
        if self._git_executable is None:
            return None
        command = [
            str(self._git_executable),
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.quotepath=false",
            f"--work-tree={root}",
            "-C",
            str(root),
            *arguments,
        ]
        environment = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_EXTERNAL_DIFF": "",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        if os.name == "nt":
            for name in ("SystemRoot", "WINDIR", "TEMP", "TMP"):
                value = os.environ.get(name)
                if value:
                    environment[name] = value
        creation_flags = (
            subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            if os.name == "nt"
            else 0
        )
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                shell=False,
                creationflags=creation_flags,
            )
        except OSError:
            return None
        assert process.stdout is not None
        output = bytearray()
        exceeded = threading.Event()

        def read_stdout() -> None:
            while True:
                try:
                    chunk = process.stdout.read(8 * 1024)
                except OSError:
                    return
                if not chunk:
                    return
                remaining = _MAX_GIT_OUTPUT_BYTES - len(output)
                if remaining <= 0:
                    exceeded.set()
                    process.kill()
                    return
                output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    exceeded.set()
                    process.kill()
                    return

        reader = threading.Thread(
            target=read_stdout,
            name="agent-hotline-git-output",
            daemon=True,
        )
        reader.start()
        try:
            return_code = process.wait(timeout=_GIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            reader.join(timeout=1)
            return None
        reader.join(timeout=1)
        if reader.is_alive():
            process.kill()
            return None
        if return_code != 0 and not exceeded.is_set():
            return None
        decoded = bytes(output).decode("utf-8", errors="replace")
        if exceeded.is_set():
            decoded += f"\n{_GIT_TRUNCATION_MARKER}"
        return decoded

    def _sanitize(self, text: str, max_chars: int) -> str:
        redacted = _STRUCTURED_SECRET_RE.sub(
            lambda match: f"{match.group('prefix')}[REDACTED]",
            text,
        )
        redacted = _CREDENTIALED_URL_RE.sub(
            lambda match: f"{match.group('scheme')}[REDACTED]@",
            redacted,
        )
        return sanitize_untrusted_text(
            redacted,
            max_chars=max_chars,
            known_secrets=self._known_secrets,
        )


def _workspace_reference(root: Path) -> str:
    digest = hashlib.sha256(os.fsencode(root)).hexdigest()[:16]
    return f"workspace_{digest}"


def _git_lines(output: str) -> tuple[list[str], bool]:
    lines = [line for line in output.splitlines() if line.strip()]
    truncated = _GIT_TRUNCATION_MARKER in lines
    return [line for line in lines if line != _GIT_TRUNCATION_MARKER], truncated


def _is_git_repository_root(candidate: Path) -> bool:
    marker = candidate / ".git"
    try:
        marker_stat = marker.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(marker_stat.st_mode) or stat.S_ISLNK(marker_stat.st_mode):
        # Gitfiles (linked worktrees/submodules) and symlinked metadata are not
        # supported by the demo boundary. Git metadata must live under the
        # allowlisted root itself.
        return False
    is_junction = getattr(marker, "is_junction", None)
    try:
        if callable(is_junction) and is_junction():
            return False
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(marker_stat, "st_file_attributes", 0)
    if reparse_flag and file_attributes & reparse_flag:
        return False
    try:
        resolved = marker.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return os.path.normcase(str(resolved)) == os.path.normcase(str(marker.absolute()))


def _resolve_git_executable(
    configured: Path | None,
    roots: tuple[WorkspaceRoot, ...],
) -> Path | None:
    if configured is not None:
        if not configured.is_absolute():
            return None
        candidate = configured
    else:
        discovered = shutil.which("git", path=os.environ.get("PATH", ""))
        if not discovered:
            return None
        candidate = Path(discovered)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_file() or not resolved.is_absolute():
        return None
    expected_name = "git.exe" if os.name == "nt" else "git"
    if resolved.name.casefold() != expected_name:
        return None
    if any(resolved.is_relative_to(root.path) for root in roots):
        return None
    return resolved


def _opened_path_is_expected(descriptor: int, expected: Path, root: Path) -> bool:
    if os.name != "nt":
        proc_link = Path(f"/proc/self/fd/{descriptor}")
        if proc_link.exists():
            try:
                opened_path = proc_link.resolve(strict=True)
            except (OSError, RuntimeError):
                return False
            return opened_path == expected and opened_path.is_relative_to(root)
        # O_NOFOLLOW plus the before/open fstat identity check covers platforms
        # without procfs.
        return bool(getattr(os, "O_NOFOLLOW", 0))
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_final_path = kernel32.GetFinalPathNameByHandleW
        get_final_path.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        get_final_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32_768)
        length = get_final_path(
            wintypes.HANDLE(msvcrt.get_osfhandle(descriptor)),
            buffer,
            len(buffer),
            0,
        )
        if not length or length >= len(buffer):
            return False
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        opened_path = Path(value).resolve(strict=True)
    except (AttributeError, ImportError, OSError, RuntimeError, ValueError):
        return False
    return opened_path == expected and opened_path.is_relative_to(root)


__all__ = ["RepositoryContextService", "WorkspaceRoot"]
