import asyncio
from pathlib import Path

import pytest

from src import ai_handler


@pytest.mark.parametrize("name", ["../../victim", "../../../../", r"..\..\victim", "/absolute/path", r"C:\Users\victim", "带空格的任务 / 相机"])
def test_task_names_cannot_escape_image_root(tmp_path, monkeypatch, name):
    root = tmp_path / "images"
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "important.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(ai_handler, "IMAGE_SAVE_DIR", str(root))

    async def download(url, path):
        Path(path).write_text("image", encoding="utf-8")
        return path

    monkeypatch.setattr(ai_handler, "_download_single_image", download)
    paths = asyncio.run(ai_handler.download_all_images("123", ["https://example.com/image.jpg"], name))
    assert len(paths) == 1
    assert Path(paths[0]).parent.parent == root.resolve()
    ai_handler.cleanup_task_images(name)
    assert not Path(paths[0]).exists()
    assert (victim / "important.txt").read_text(encoding="utf-8") == "keep"


def test_cleanup_refuses_symlinked_task_directory(tmp_path, monkeypatch):
    root = tmp_path / "images"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "important.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(ai_handler, "IMAGE_SAVE_DIR", str(root))
    directory = ai_handler._task_image_directory("task")
    try:
        directory.symlink_to(victim, target_is_directory=True)
    except OSError:
        pytest.skip("This Windows environment does not allow creating symlinks")
    ai_handler.cleanup_task_images("task")
    assert (victim / "important.txt").exists()
    with pytest.raises(ValueError):
        asyncio.run(ai_handler.download_all_images("1", ["https://example.com/image.jpg"], "task"))
