"""Install bundled code-owned contracts before validating saved or new inputs."""
from __future__ import annotations

from threading import RLock

_lock = RLock()
_ready = False
_installing = False


def ensure_builtin_contracts() -> None:
    global _ready, _installing
    with _lock:
        if _ready or _installing:
            return
        _installing = True
        try:
            from sheltersim.contracts.presence import register_presence_contract
            from sheltersim.policies.registry import register_agents_social_policies
            register_presence_contract()
            register_agents_social_policies()
            _ready = True
        finally:
            _installing = False
