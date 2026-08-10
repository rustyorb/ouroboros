"""Evolution scheduler pacing tests."""

import datetime

import supervisor.queue as evolution_queue


def _iso_ago(seconds: int) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - datetime.timedelta(seconds=seconds)).isoformat()


def _setup(monkeypatch, state):
    pending = []
    running = {}
    monkeypatch.setattr(evolution_queue, "PENDING", pending)
    monkeypatch.setattr(evolution_queue, "RUNNING", running)
    monkeypatch.setattr(evolution_queue, "load_state", lambda: dict(state))
    monkeypatch.setattr(evolution_queue, "save_state", lambda _st: None)
    monkeypatch.setattr(evolution_queue, "persist_queue_snapshot", lambda **_kwargs: None)
    monkeypatch.setattr(evolution_queue, "send_with_budget", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(evolution_queue, "budget_remaining", lambda _st: 100.0)
    return pending


def _base_state():
    return {
        "evolution_mode_enabled": True,
        "owner_chat_id": 123,
        "evolution_cycle": 0,
        "evolution_consecutive_failures": 0,
        "last_owner_message_at": "",
        "last_evolution_task_at": "",
    }


def test_recent_owner_message_keeps_evolution_idle(monkeypatch):
    state = _base_state()
    state["last_owner_message_at"] = _iso_ago(evolution_queue.EVOLUTION_QUIET_SEC - 1)
    pending = _setup(monkeypatch, state)

    evolution_queue.enqueue_evolution_task_if_needed()

    assert pending == []


def test_recent_evolution_respects_cooldown(monkeypatch):
    state = _base_state()
    state["last_evolution_task_at"] = _iso_ago(evolution_queue.EVOLUTION_COOLDOWN_SEC - 1)
    pending = _setup(monkeypatch, state)

    evolution_queue.enqueue_evolution_task_if_needed()

    assert pending == []


def test_quiet_queue_enqueues_after_both_gates(monkeypatch):
    state = _base_state()
    old_enough = max(evolution_queue.EVOLUTION_QUIET_SEC,
                     evolution_queue.EVOLUTION_COOLDOWN_SEC) + 1
    state["last_owner_message_at"] = _iso_ago(old_enough)
    state["last_evolution_task_at"] = _iso_ago(old_enough)
    pending = _setup(monkeypatch, state)

    evolution_queue.enqueue_evolution_task_if_needed()

    assert len(pending) == 1
    assert pending[0]["type"] == "evolution"
