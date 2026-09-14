"""Two-agent architecture matching the X guide: watcher + executor.

Watcher: WS/REST books → opportunities queue
Executor: consume queue → both-legs-or-neither → merge
"""

from .watcher_agent import WatcherAgent, OpportunityEvent
from .executor_agent import ExecutorAgent

__all__ = ["WatcherAgent", "ExecutorAgent", "OpportunityEvent"]
