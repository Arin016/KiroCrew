"""Shared TYPE_CHECKING imports for dashboard modules."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_claw.context import ContextBuilder
    from kiro_claw.cron import CronService
    from kiro_claw.history import ConversationLog, HistoryConsolidator
    from kiro_claw.learn import LessonStore
    from kiro_claw.session import SessionManager
    from kiro_claw.subagent import SubagentManager
    from kiro_claw.taskrunner import TaskRunner

__all__ = [
    "ContextBuilder",
    "CronService",
    "ConversationLog",
    "HistoryConsolidator",
    "LessonStore",
    "SessionManager",
    "SubagentManager",
    "TaskRunner",
]
