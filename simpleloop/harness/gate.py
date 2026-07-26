"""Gate: deterministic pre-commit diff safety check.

The only thing the gate does is reject a commit whose diff touches a frozen path
or a path outside editable_paths. This is a hard safety net that must NOT be
delegated to the judger LLM (it would miss an agent quietly editing tests/**).

Returns (ok, violations). ok=False => the harness skips the commit, this round's
sha is None, and the judger is told the rejection reason.
"""
from __future__ import annotations

from fnmatch import fnmatch


def check_diff(changed_paths: list[str], editable: list[str], frozen: list[str]) -> tuple[bool, list[str]]:
    """Return (ok, violations). A violation is a human-readable reason per bad path."""
    violations: list[str] = []
    for path in changed_paths:
        if _matches_any(path, frozen):
            violations.append(f"{path}: touches a frozen path")
        elif not _matches_any(path, editable):
            violations.append(f"{path}: outside editable_paths")
    return (len(violations) == 0, violations)


def _matches_any(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if fnmatch(path, pat):
            return True
        # support ** globs minimally (fnmatch treats ** like *)
        if "/**/" in pat and fnmatch(path, pat.replace("/**/", "/")):
            return True
        if pat.startswith("**/") and fnmatch(path, pat[3:]):
            return True
        if pat.endswith("/**") and (path == pat[:-3].rstrip("/") or path.startswith(pat[:-3])):
            return True
    return False
