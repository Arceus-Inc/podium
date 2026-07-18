"""CommentsFacade — the task's comment thread (task-anchored engine messages) as podium DTOs.

Read the thread; let the human on the board join it. A posted comment travels the engine's own
delivery path (message row + coalesced wake), so the recipient's next beat reads it — the board
participates in the same coordination channel as the agents (OM-3)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from podium.control._observe import UnknownTaskError

if TYPE_CHECKING:
    from chorus.ledger import Ledger, Message, Task


class UndeliverableCommentError(ValueError):
    """The task has no assignee, parent assignee, or creator to notify."""


class CommentView(BaseModel):
    """One comment on the thread, oldest first."""

    model_config = ConfigDict(frozen=True)

    id: str
    author: str  # employee slug or human user id
    body: str
    created_at: str | None


def _view(message: Message) -> CommentView:
    return CommentView(
        id=message.id,
        author=message.from_employee_id or message.from_user_id or "?",
        body=message.body,
        created_at=message.created_at.isoformat() if message.created_at else None,
    )


class CommentsFacade:
    """Pure delegation to the engine's message table + delivery path; translation only."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def _task_or_raise(self, task_id: str) -> Task:
        try:
            uuid.UUID(task_id)  # engine task ids are uuid text; anything else can't exist
        except ValueError:
            raise UnknownTaskError(task_id) from None
        task = self._ledger.tasks.get(task_id)
        if task is None:
            raise UnknownTaskError(task_id)
        return task

    def thread(self, task_id: str) -> list[CommentView]:
        """The task's comments, oldest first — read or unread; the thread is shared context."""
        self._task_or_raise(task_id)
        return [_view(m) for m in self._ledger.messages.for_task(task_id)]

    def post(self, task_id: str, *, body: str, by_user: str) -> CommentView:
        """The human joins the thread; the engine's recipient resolution + wake delivery apply."""
        from chorus.ledger import Message
        from chorus.lifecycle import deliver_message
        from chorus_tools._comment import _recipient

        task = self._task_or_raise(task_id)
        recipient = _recipient(self._ledger, task, author="")  # a user never matches a slug
        if recipient is None:
            raise UndeliverableCommentError(task_id)
        message = Message(
            id=str(uuid.uuid4()),
            from_user_id=by_user,
            to_employee_id=recipient,
            task_id=task.id,
            body=body,
        )
        deliver_message(self._ledger, message)
        stored = self._ledger.messages.get(message.id)
        return _view(stored if stored is not None else message)


__all__ = ["CommentView", "CommentsFacade", "UndeliverableCommentError", "UnknownTaskError"]
