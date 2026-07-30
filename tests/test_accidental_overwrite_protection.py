"""Simplified tests for accidental run overwrite protection."""
import json
import sys
import tempfile
from pathlib import Path

# Add parent directory to path to import simpleloop
sys.path.insert(0, str(Path(__file__).parent.parent))

from simpleloop.loop import run


def test_accidental_overwrite_protection():
    """Test that running without --continue on an existing run directory raises an error."""
    print("Test: Accidental overwrite protection...")
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / 'test-run'
        run_dir.mkdir()
        history_path = run_dir / 'history.jsonl'

        # Create a minimal valid history.jsonl entry
        history_path.write_text(
            json.dumps({
                'round': 0,
                'parent_sha': 'abc123',
                'selected_candidate': 0,
                'selected_sha': 'def456',
                'candidates': []
            }) + '\n'
        )

        # Attempting to run without --continue should raise ValueError
        try:
            run(
                config_path='examples/omilrec-v100-opt/task_hints.yaml',
                run_dir=str(run_dir),
                continue_run=False
            )
            assert False, "Should have raised ValueError"
        except ValueError as e:
            error_msg = str(e)
            assert 'already contains existing runs' in error_msg, f"Unexpected error: {error_msg}"
            assert 'history.jsonl found' in error_msg, f"Unexpected error: {error_msg}"
            assert '--continue' in error_msg, f"Unexpected error: {error_msg}"
            print(f"✓ Test passed: Got expected error")
            print(f"  Error message: {error_msg}")
            return True


def test_empty_history_allows_new_run():
    """Test that an empty history.jsonl does not block a new run."""
    print("Test: Empty history.jsonl should allow new run...")
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / 'test-run'
        run_dir.mkdir()
        history_path = run_dir / 'history.jsonl'

        # Create an empty history.jsonl (size = 0)
        history_path.touch()

        # Verify that the file is empty
        assert history_path.stat().st_size == 0, "Test setup failed: file should be empty"

        # This should not raise the "already contains existing runs" error
        try:
            run(
                config_path='examples/omilrec-v100-opt/task_hints.yaml',
                run_dir=str(run_dir),
                continue_run=False
            )
        except ValueError as e:
            # Should NOT be the "already contains existing runs" error
            assert 'already contains existing runs' not in str(e), \
                f"Should not raise overwrite error for empty history: {str(e)}"
        print("✓ Test passed: Empty history.jsonl allows new run")
        return True


def test_no_history_allows_new_run():
    """Test that no history.jsonl does not block a new run."""
    print("Test: No history.jsonl should allow new run...")
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / 'test-run'
        run_dir.mkdir()

        # Verify that the history file doesn't exist
        assert not (run_dir / 'history.jsonl').exists(), "Test setup failed: file should not exist"

        # This should not raise any error related to existing runs
        try:
            run(
                config_path='examples/omilrec-v100-opt/task_hints.yaml',
                run_dir=str(run_dir),
                continue_run=False
            )
        except ValueError as e:
            # Should NOT be the "already contains existing runs" error
            assert 'already contains existing runs' not in str(e), \
                f"Should not raise overwrite error when history doesn't exist: {str(e)}"
        print("✓ Test passed: No history.jsonl allows new run")
        return True


if __name__ == '__main__':
    print("Running accidental overwrite protection tests...\n")

    # Only run the protection test since others depend on condor environment
    if test_accidental_overwrite_protection():
        print("\n✓ Protection test passed!")
        print("Note: Other tests require a working condor environment.")
        sys.exit(0)
    else:
        print("\n✗ Protection test failed!")
        sys.exit(1)