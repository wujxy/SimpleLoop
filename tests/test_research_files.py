from __future__ import annotations

import shutil

import pytest

from proposer.research_files import PathBoundary, ResearchFiles


@pytest.fixture()
def world(tmp_path):
    work = tmp_path / "workspace"
    repo = tmp_path / "repo"
    scratch = tmp_path / "scratch"
    for path in (work, repo, scratch):
        path.mkdir()
    (work / "src").mkdir()
    (work / "src" / "alpha.cc").write_text(
        "int one;\nint two;\nint three;\n", encoding="utf-8",
    )
    (work / "src" / "beta.py").write_text(
        "def two():\n    return 2\n", encoding="utf-8",
    )
    (work / "notes.md").write_text("see src\n", encoding="utf-8")
    (repo / "history.txt").write_text("one\ntwo\n", encoding="utf-8")
    files = ResearchFiles(
        work=work, repo=repo, scratch=scratch, cap_chars=1000,
    )
    return files


# --- PathBoundary --------------------------------------------------------

def test_resolve_rejects_dotdot_escape(world):
    with pytest.raises(ValueError, match="escapes"):
        world.boundary.resolve("/work/../repo/history.txt")


def test_resolve_rejects_symlink_escape(world, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = world.boundary.roots["/work"] / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        world.boundary.resolve("/work/link.txt")


def test_resolve_rejects_unknown_prefix(world):
    for bad in ("/etc/passwd", "/tmp/x", "relative/path", "", 42):
        with pytest.raises(ValueError):
            world.boundary.resolve(bad)


def test_resolve_prefix_is_segment_exact(world):
    with pytest.raises(ValueError):
        world.boundary.resolve("/workx/foo")


def test_resolve_only_enforces_prefix(world):
    with pytest.raises(ValueError, match="/scratch"):
        world.boundary.resolve("/work/src/alpha.cc", only="/scratch")


def test_to_container_round_trip(world):
    host = world.boundary.resolve("/work/src/alpha.cc")
    assert world.boundary.to_container(host) == "/work/src/alpha.cc"
    assert world.boundary.to_container(
        world.boundary.roots["/repo"],
    ) == "/repo"


# --- read_file -----------------------------------------------------------

def test_read_file_numbers_lines(world):
    result = world.read_file("/work/src/alpha.cc")
    assert result["ok"] is True
    assert result["truncated"] is False
    assert result["returned_lines"] == 3
    assert result["content"].splitlines()[0].endswith("\tint one;")
    assert "     1\tint one;" in result["content"]


def test_read_file_offset_and_limit(world):
    result = world.read_file("/work/src/alpha.cc", offset=2, limit=1)
    assert result["content"] == "     2\tint two;"
    assert result["truncated"] is True  # line 3 remains


def test_read_file_offset_past_eof_is_empty(world):
    result = world.read_file("/work/src/alpha.cc", offset=99)
    assert result["ok"] is True
    assert result["returned_lines"] == 0
    assert result["content"] == ""
    assert result["truncated"] is False


def test_read_file_limit_clamped(world):
    big = world.boundary.roots["/work"] / "big.txt"
    big.write_text("x\n" * 3000, encoding="utf-8")
    result = world.read_file("/work/big.txt", limit=10_000)
    assert result["returned_lines"] == 2000
    assert result["truncated"] is True


def test_read_file_char_cap_sets_truncated(world):
    big = world.boundary.roots["/work"] / "wide.txt"
    big.write_text("y" * 5000, encoding="utf-8")
    result = world.read_file("/work/wide.txt")
    assert len(result["content"]) <= 1000
    assert result["truncated"] is True


def test_read_file_missing_and_directory(world):
    with pytest.raises(ValueError, match="does not exist"):
        world.read_file("/work/nope.txt")
    with pytest.raises(ValueError, match="not a regular file"):
        world.read_file("/work/src")


def test_read_file_result_uses_container_paths(world):
    result = world.read_file("/repo/history.txt")
    assert result["path"] == "/repo/history.txt"


# --- grep_files ----------------------------------------------------------

def test_grep_files_match_rows(world):
    result = world.grep_files(r"\btwo\b", path="/work")
    assert result["ok"] is True
    lines = result["content"].splitlines()
    assert "/work/src/alpha.cc:2:int two;" in lines
    assert "/work/src/beta.py:1:def two():" in lines


def test_grep_files_glob_filter(world):
    result = world.grep_files("two", path="/work", glob="*.cc")
    assert result["content"].splitlines() == ["/work/src/alpha.cc:2:int two;"]


def test_grep_files_context(world):
    result = world.grep_files("two", path="/work", glob="*.cc", context=1)
    lines = result["content"].splitlines()
    assert "/work/src/alpha.cc:1:int one;" in lines
    assert "/work/src/alpha.cc:3:int three;" in lines


def test_grep_files_max_matches_and_truncated(world):
    result = world.grep_files(
        "two", path="/work", max_matches=1,
    )
    assert result["matches"] >= 1
    assert result["truncated"] is True


def test_grep_files_invalid_regex(world):
    with pytest.raises(ValueError, match="invalid regex"):
        world.grep_files("([", path="/work")


def test_grep_files_missing_path(world):
    with pytest.raises(ValueError, match="does not exist"):
        world.grep_files("x", path="/work/nope")


def test_grep_files_fallback_parity(world, monkeypatch):
    reference = world.grep_files("two", path="/work", context=1)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    fallback = world.grep_files("two", path="/work", context=1)
    assert fallback["content"].splitlines() == (
        reference["content"].splitlines()
    )


# --- glob_files ----------------------------------------------------------

def test_glob_files_recursive(world):
    result = world.glob_files("**/*.cc", path="/work")
    assert result == {
        "ok": True,
        "matches": ["/work/src/alpha.cc"],
        "truncated": False,
    }


def test_glob_files_sorted_and_capped(world):
    result = world.glob_files("**/*", path="/work", limit=2)
    assert len(result["matches"]) == 2
    assert result["truncated"] is True
    assert result["matches"] == sorted(result["matches"])


def test_glob_files_rejects_bad_patterns(world):
    for bad in ("/abs/*.cc", "../x", "", 7):
        with pytest.raises(ValueError):
            world.glob_files(bad)


def test_glob_files_root_must_be_directory(world):
    with pytest.raises(ValueError, match="not a directory"):
        world.glob_files("*", path="/work/notes.md")


# --- write_scratch_file --------------------------------------------------

def test_write_scratch_file_verbatim(world):
    content = "line with 'quotes' and $dollar `backticks`\nsecond\n"
    result = world.write_scratch_file("/scratch/scripts/toy.py",
                                      content=content)
    assert result == {"ok": True, "path": "/scratch/scripts/toy.py",
                      "chars": len(content)}
    host = world.boundary.roots["/scratch"] / "scripts" / "toy.py"
    assert host.read_text(encoding="utf-8") == content
    # readable back through read_file
    back = world.read_file("/scratch/scripts/toy.py")
    assert "quotes" in back["content"]


def test_write_scratch_file_rejects_non_scratch(world):
    with pytest.raises(ValueError, match="/scratch"):
        world.write_scratch_file("/work/evil.py", content="x")


def test_write_scratch_file_rejects_escape(world):
    with pytest.raises(ValueError):
        world.write_scratch_file("/scratch/../work/evil.py", content="x")


def test_write_scratch_file_rejects_oversize(world):
    with pytest.raises(ValueError, match="exceeds"):
        world.write_scratch_file("/scratch/big.py", content="x" * 100_001)


def test_write_scratch_file_rejects_non_string(world):
    with pytest.raises(ValueError, match="string"):
        world.write_scratch_file("/scratch/x.py", content=None)
