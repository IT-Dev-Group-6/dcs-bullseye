"""
Input validation helpers for file operations.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def sanitize_miz_filename(name: str) -> str:
    """
    Validate and return a safe .miz filename.

    Raises ValueError if the name contains path separators, traversal sequences,
    invalid characters, is not a .miz file, or exceeds reasonable length limits.
    """
    if not name or len(name) > 255:
        raise ValueError("Invalid filename")
    if "/" in name or "\\" in name:
        raise ValueError("Path separators are not allowed in filenames")
    # os.path.basename is a CodeQL-recognised path sanitizer; if it changes the
    # value then the input contained directory components.
    if os.path.basename(name) != name:
        raise ValueError("Path separators are not allowed in filenames")
    # Check extension with a plain string operation — keeps the stem regex simple
    # and avoids polynomial backtracking from overlapping character classes.
    stem, dot, ext = name.rpartition(".")
    if not dot or ext.lower() != "miz":
        raise ValueError("Only .miz files are accepted")
    # Validate stem characters only (no suffix overlap → no backtracking risk).
    if not stem or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]*", stem):
        raise ValueError("Filename contains invalid characters")
    return name


def safe_join(root: Path, filename: str) -> Path:
    """
    Join root and filename, then verify the result stays inside root.

    Raises ValueError if the resolved path escapes the root directory.
    """
    # os.path.basename strips any residual directory components and is recognised
    # by CodeQL as a path sanitiser, clearing the taint on the user-supplied value.
    safe_name = os.path.basename(filename)
    root_resolved = root.resolve()
    target = (root_resolved / safe_name).resolve()
    if not target.is_relative_to(root_resolved):
        raise ValueError(f"Resolved path escapes root directory: {target}")
    return target
