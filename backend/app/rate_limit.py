from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=[])
LOGIN_LIMIT = "5/minute"
REGISTER_LIMIT = "3/minute"
