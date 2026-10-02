"""Checkpoint semantics: content fingerprint, three statuses, single write point."""

from __future__ import annotations

import pytest

from bobanana6.agentloop.state import (
    CHECKPOINT_STATUSES,
    Checkpoint,
    InMemoryCheckpointStore,
    load_checkpoint,
    messages_digest,
    resume,
    save_turn,
)


def transcript(*contents: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": content} for content in contents]


def test_there_are_exactly_three_statuses() -> None:
    assert CHECKPOINT_STATUSES == ("ok", "failed", "never_started")


def test_checkpoint_rejects_an_unknown_status() -> None:
    with pytest.raises(ValueError, match="unknown checkpoint status"):
        Checkpoint(task_id="t", worker_id="w", turn=0, messages_digest="d", status="crashed")


def test_checkpoint_rejects_an_empty_digest() -> None:
    with pytest.raises(ValueError, match="content fingerprint"):
        Checkpoint(task_id="t", worker_id="w", turn=0, messages_digest="")


def test_digest_is_content_based_not_positional() -> None:
    assert messages_digest(transcript("a", "b")) != messages_digest(transcript("a", "c"))
    assert messages_digest(transcript("a", "b")) != messages_digest(transcript("b", "a"))
    assert messages_digest(transcript("a", "b")) == messages_digest(transcript("a", "b"))


def test_digest_ignores_key_insertion_order() -> None:
    first = [{"role": "user", "content": "hi"}]
    second = [{"content": "hi", "role": "user"}]
    assert messages_digest(first) == messages_digest(second)


def test_resume_distinguishes_failed_from_never_started() -> None:
    """Only the status differs, so the status is exactly what resume must read.

    The record fields are deliberately identical here: if resume ignored the
    status and just trusted ``turn`` / ``done_actions``, a never-started
    candidate would look like a failed one and replay actions that never ran.
    """
    common = {
        "task_id": "t",
        "worker_id": "w",
        "turn": 2,
        "messages_digest": "d",
        "done_actions": ("fetch", "extract"),
    }
    failed = Checkpoint(**common, status="failed")
    never = Checkpoint(**common, status="never_started")
    assert failed.status != never.status
    assert resume(never) != resume(failed)
    assert resume(never).start_turn == 0
    assert resume(never).completed_actions == ()


def test_failed_resume_does_not_replay_completed_actions() -> None:
    failed = Checkpoint(
        task_id="t", worker_id="w", turn=2, messages_digest="d", done_actions=("fetch", "extract"), status="failed"
    )
    plan = resume(failed)
    assert plan.start_turn == 2
    assert plan.completed_actions == ("fetch", "extract")  # caller skips these
    assert plan.run is True


def test_never_started_resume_starts_from_the_beginning() -> None:
    never = Checkpoint(task_id="t", worker_id="w", turn=0, messages_digest="d", status="never_started")
    plan = resume(never)
    assert plan.start_turn == 0
    assert plan.completed_actions == ()


def test_ok_resume_advances_past_the_completed_turn() -> None:
    ok = Checkpoint(task_id="t", worker_id="w", turn=2, messages_digest="d", done_actions=("fetch",), status="ok")
    assert resume(ok).start_turn == 3


def test_failed_turn_is_persisted() -> None:
    """A worker that raises on turn 2 still leaves a resumable checkpoint."""
    store = InMemoryCheckpointStore()

    def run_worker(store: InMemoryCheckpointStore, raises_after: int) -> None:
        try:
            for turn in range(1, raises_after + 1):
                save_turn(
                    store,
                    task_id="task-1",
                    worker_id="w",
                    turn=turn,
                    messages=transcript(f"turn {turn}"),
                    done_actions=(f"action-{turn}",),
                    status="ok",
                )
                if turn == raises_after:
                    raise RuntimeError("boom")
        except RuntimeError:
            save_turn(
                store,
                task_id="task-1",
                worker_id="w",
                turn=raises_after,
                messages=transcript(f"turn {raises_after} failed"),
                done_actions=tuple(f"action-{i}" for i in range(1, raises_after + 1)),
                status="failed",
            )

    run_worker(store, raises_after=2)
    checkpoint = load_checkpoint(store, "task-1")
    assert checkpoint is not None
    assert checkpoint.turn == 2
    assert checkpoint.status == "failed"
    assert resume(checkpoint).completed_actions == ("action-1", "action-2")


def test_save_turn_is_the_single_write_point() -> None:
    store = InMemoryCheckpointStore()
    saved = save_turn(
        store, task_id="t", worker_id="w", turn=1, messages=transcript("hi"), done_actions=("a",), status="ok"
    )
    assert load_checkpoint(store, "t") == saved
    assert saved.messages_digest == messages_digest(transcript("hi"))
