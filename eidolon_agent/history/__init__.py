"""Centralized conversation history + async fanout to memory & emotion services."""

from eidolon_agent.history.fanout import HistoryFanout
from eidolon_agent.history.manager import HistoryManager

__all__ = ["HistoryFanout", "HistoryManager"]
