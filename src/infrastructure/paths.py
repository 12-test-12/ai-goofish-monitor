"""Confine file access to a configured directory, including symlink targets."""

import ntpath
import os


def safe_child_path(directory: str | os.PathLike, filename: str) -> str:
    """Resolve one filename under directory; reject absolute paths and traversal."""
    if (
        not filename or filename in {".", ".."}
        or any(char in filename for char in ("/", "\\", ":", "\0"))
        or ntpath.isabs(filename)
    ):
        raise ValueError("无效的文件名")
    root = os.path.realpath(directory)
    candidate = os.path.realpath(os.path.join(root, filename))
    # Include the separator so a sibling such as logs-backup cannot match logs.
    if not candidate.startswith(os.path.join(root, "")):
        raise ValueError("文件路径超出允许的目录")
    return candidate
