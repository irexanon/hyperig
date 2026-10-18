from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class AppContext:
    """Shared handles passed to modules."""

    cfg: Any
    ig: Any
    store: Any
    bridge: Any = None
    modules: Any = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
