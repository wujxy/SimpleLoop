"""Host-side read-only navigation + scratch writes for the research tools.

The agent speaks one path namespace — the container paths ``/work``,
``/repo``, ``/scratch`` — and this module maps them onto the host roots
those container paths are bind-mounted from. Reads never enter the
container: the bind mounts expose the same bytes on the host, so
navigation (the bulk of research traffic) runs at host speed. Every
resolved path is verified to stay under its root (symlink- and
``..``-safe) before any I/O, and every result is rendered back in the
container namespace so host paths never leak into later shell commands.

Usage errors raise ``ValueError``; the ``ResearchTools`` façade converts
them to ``{"ok": False, "error": ...}`` observations.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

_READ_DEFAULT_LIMIT = 400
_READ_MAX_LIMIT = 2000
_GREP_DEFAULT_MATCHES = 50
_GREP_MAX_MATCHES = 500
_GREP_MAX_CONTEXT = 10
_GREP_TIMEOUT_SECONDS = 30
_GLOB_DEFAULT_LIMIT = 200
_GLOB_MAX_LIMIT = 1000
_WRITE_MAX_CHARS = 100_000


class PathBoundary:
    """Map container paths onto host roots with containment enforcement."""

    def __init__(self, *, work: Path, repo: Path, scratch: Path):
        self.roots = {
            "/work": Path(work).resolve(),
            "/repo": Path(repo).resolve(),
            "/scratch": Path(scratch).resolve(),
        }

    def resolve(self, container_path: str, *, only: str | None = None) -> Path:
        """Resolve a container path to a host path under its root.

        ``only`` restricts the allowed prefix (e.g. ``"/scratch"`` for
        writes). Raises ``ValueError`` on non-strings, unknown or
        disallowed prefixes, relative paths, and ``..``/symlink escapes.
        """
        if not isinstance(container_path, str) or not container_path:
            raise ValueError("path must be a non-empty string")
        for prefix, root in self.roots.items():
            if container_path == prefix or container_path.startswith(
                prefix + "/"
            ):
                if only is not None and prefix != only:
                    raise ValueError(
                        f"path must be under {only}: {container_path!r}"
                    )
                relative = container_path[len(prefix):].lstrip("/")
                candidate = (root / relative).resolve()
                if candidate != root and root not in candidate.parents:
                    raise ValueError(
                        f"path escapes {prefix}: {container_path!r}"
                    )
                return candidate
        raise ValueError(
            "path must be absolute under /work, /repo, or /scratch: "
            f"{container_path!r}"
        )

    def to_container(self, host_path: Path) -> str:
        """Render a host path back in the agent's container namespace."""
        host_path = Path(host_path)
        for prefix, root in self.roots.items():
            if host_path == root:
                return prefix
            if root in host_path.parents:
                return prefix + "/" + host_path.relative_to(root).as_posix()
        raise ValueError(f"host path outside all roots: {host_path}")


class ResearchFiles:
    """Host-side file operations backing the navigation research tools."""

    def __init__(
        self,
        *,
        work: Path,
        repo: Path,
        scratch: Path,
        cap_chars: int,
    ):
        self.boundary = PathBoundary(work=work, repo=repo, scratch=scratch)
        self.cap_chars = int(cap_chars)

    # -- read_file ----------------------------------------------------

    def read_file(
        self, path: str, *, offset: int = 1, limit: int = _READ_DEFAULT_LIMIT,
    ) -> dict:
        offset = _as_int(offset, "offset", minimum=1)
        limit = _as_int(limit, "limit", minimum=1)
        limit = min(limit, _READ_MAX_LIMIT)
        host = self.boundary.resolve(path)
        if not host.exists():
            raise ValueError(f"file does not exist: {path!r}")
        if not host.is_file():
            raise ValueError(f"not a regular file: {path!r}")
        lines = host.read_text(encoding="utf-8", errors="replace").splitlines()
        window = lines[offset - 1: offset - 1 + limit]
        rendered = "\n".join(
            f"{n:>6}\t{text}"
            for n, text in enumerate(window, start=offset)
        )
        truncated = offset - 1 + len(window) < len(lines)
        if len(rendered) > self.cap_chars:
            rendered = rendered[: self.cap_chars]
            truncated = True
        return {
            "ok": True,
            "path": path,
            "offset": offset,
            "returned_lines": len(window),
            "truncated": truncated,
            "content": rendered,
        }

    # -- grep_files ---------------------------------------------------

    def grep_files(
        self,
        pattern: str,
        *,
        path: str = "/work",
        glob: str | None = None,
        context: int = 0,
        max_matches: int = _GREP_DEFAULT_MATCHES,
    ) -> dict:
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("pattern must be a non-empty string")
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        context = min(_as_int(context, "context", minimum=0),
                      _GREP_MAX_CONTEXT)
        max_matches = min(
            _as_int(max_matches, "max_matches", minimum=1),
            _GREP_MAX_MATCHES,
        )
        host = self.boundary.resolve(path)
        if not host.exists():
            raise ValueError(f"path does not exist: {path!r}")
        rg = shutil.which("rg")
        if rg is not None:
            rows, truncated = self._grep_rg(
                rg, pattern, host, glob=glob, context=context,
                max_matches=max_matches,
            )
        else:
            rows, truncated = self._grep_fallback(
                compiled, host, glob=glob, context=context,
                max_matches=max_matches,
            )
        text = "\n".join(rows)
        if len(text) > self.cap_chars:
            text = text[: self.cap_chars]
            truncated = True
        return {
            "ok": True,
            "matches": len(rows),
            "truncated": truncated,
            "content": text,
        }

    # rg field separators: match rows are framed with \x1f, context rows
    # with \x1e — control characters that cannot appear in real paths, so
    # dashed/coloned filenames parse unambiguously.
    _RG_MATCH_SEP = "\x1f"
    _RG_CONTEXT_SEP = "\x1e"

    def _grep_rg(
        self, rg, pattern, host, *, glob, context, max_matches,
    ) -> tuple[list[str], bool]:
        argv = [
            rg, "--line-number", "--no-heading", "--color", "never",
            "--with-filename", "-C", str(context),
            "--field-match-separator", self._RG_MATCH_SEP,
            "--field-context-separator", self._RG_CONTEXT_SEP,
        ]
        if glob:
            argv.extend(["--glob", glob])
        argv.extend(["--", pattern, str(host)])
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True,
                timeout=_GREP_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError(
                f"grep exceeded {_GREP_TIMEOUT_SECONDS}s — narrow the "
                "path or glob"
            ) from exc
        if proc.returncode not in (0, 1):
            raise ValueError(f"rg failed: {proc.stderr.strip()[:200]}")
        rows: list[str] = []
        truncated = False
        matched = 0
        for line in proc.stdout.splitlines():
            if line == "--":  # rg's context-group separator
                continue
            parsed = self._parse_rg_line(line)
            if parsed is None:
                continue
            file_path, lineno, text, is_match = parsed
            if is_match:
                matched += 1
                if matched > max_matches:
                    truncated = True
                    break
            container = self.boundary.to_container(file_path)
            rows.append(f"{container}:{lineno}:{text}")
        return rows, truncated

    def _parse_rg_line(self, line: str):
        """Split one rg row into (path, lineno, text, is_match)."""
        for separator, is_match in (
            (self._RG_MATCH_SEP, True),
            (self._RG_CONTEXT_SEP, False),
        ):
            head, sep, rest = line.partition(separator)
            if not sep or not head:
                continue
            lineno, sep2, text = rest.partition(separator)
            if not sep2 or not lineno.isdigit():
                continue
            return Path(head), lineno, text, is_match
        return None

    def _grep_fallback(
        self, compiled, host, *, glob, context, max_matches,
    ) -> tuple[list[str], bool]:
        rows: list[str] = []
        matched = 0
        truncated = False
        for file_path in _iter_files(host):
            if glob and not fnmatch.fnmatch(file_path.name, glob):
                continue
            try:
                lines = file_path.read_text(
                    encoding="utf-8", errors="replace",
                ).splitlines()
            except OSError:
                continue
            container = self.boundary.to_container(file_path)
            hit_indexes = [
                i for i, text in enumerate(lines)
                if compiled.search(text)
            ]
            if not hit_indexes:
                continue
            shown: set[int] = set()
            for i in hit_indexes:
                matched += 1
                if matched > max_matches:
                    truncated = True
                    break
                lo = max(0, i - context)
                hi = min(len(lines), i + context + 1)
                for n in range(lo, hi):
                    if n in shown:
                        continue
                    shown.add(n)
                    rows.append(f"{container}:{n + 1}:{lines[n]}")
            if truncated:
                break
        return rows, truncated

    # -- glob_files ---------------------------------------------------

    def glob_files(
        self,
        pattern: str,
        *,
        path: str = "/work",
        limit: int = _GLOB_DEFAULT_LIMIT,
    ) -> dict:
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("pattern must be a non-empty string")
        if pattern.startswith("/") or pattern.split("/")[0] == "..":
            raise ValueError(
                f"pattern must be relative to the root: {pattern!r}"
            )
        limit = min(_as_int(limit, "limit", minimum=1), _GLOB_MAX_LIMIT)
        root = self.boundary.resolve(path)
        if not root.is_dir():
            raise ValueError(f"not a directory: {path!r}")
        matches: list[str] = []
        truncated = False
        try:
            candidates = sorted(root.glob(pattern))
        except (OSError, ValueError) as exc:
            raise ValueError(f"invalid glob pattern: {pattern!r}") from exc
        for candidate in candidates:
            if len(matches) >= limit:
                truncated = True
                break
            matches.append(self.boundary.to_container(candidate))
        return {"ok": True, "matches": matches, "truncated": truncated}

    # -- write_scratch_file -------------------------------------------

    def write_scratch_file(self, path: str, *, content: str) -> dict:
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        if len(content) > _WRITE_MAX_CHARS:
            raise ValueError(
                f"content exceeds {_WRITE_MAX_CHARS} chars: {len(content)}"
            )
        host = self.boundary.resolve(path, only="/scratch")
        host.parent.mkdir(parents=True, exist_ok=True)
        host.write_text(content, encoding="utf-8")
        return {"ok": True, "path": path, "chars": len(content)}


def _as_int(value, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _iter_files(root: Path):
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in sorted(filenames):
            yield Path(dirpath) / name
