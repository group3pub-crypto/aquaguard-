from __future__ import annotations

import re

_MD_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
_MD_RE = re.compile("([" + re.escape(_MD_SPECIAL) + "])")


def escape_md(text: object) -> str:
    """Escape text for Telegram MarkdownV2."""
    return _MD_RE.sub(r"\\\1", str(text))
