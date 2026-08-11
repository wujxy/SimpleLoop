import signal

import pytest

from simpleloop import processes


def test_registry_terminates_each_registered_process_group(monkeypatch):
    sent = []
    registry = processes.ChildProcessRegistry()
    monkeypatch.setattr(
        processes.os, "killpg",
        lambda pid, sig: sent.append((pid, sig)),
    )
    registry.register(11)
    registry.register(22)

    registry.terminate_all()

    assert set(sent) == {
        (11, signal.SIGTERM),
        (22, signal.SIGTERM),
    }


def test_unregister_prevents_termination(monkeypatch):
    sent = []
    registry = processes.ChildProcessRegistry()
    monkeypatch.setattr(
        processes.os, "killpg",
        lambda pid, sig: sent.append((pid, sig)),
    )
    registry.register(11)
    registry.unregister(11)

    registry.terminate_all()

    assert sent == []


def test_sigterm_handler_reaps_children_and_interrupts_run(monkeypatch):
    calls = []
    prior = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(
        processes.CHILD_PROCESSES, "terminate_all",
        lambda: calls.append("terminate"),
    )

    with processes.run_signal_handlers():
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)

    assert calls == ["terminate"]
    assert signal.getsignal(signal.SIGTERM) is prior
