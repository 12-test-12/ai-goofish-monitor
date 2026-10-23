import json

import pytest

from scripts.ci.desktop_package import MANIFEST, prepare_package, record_frontend, sha256, verify_frontend


@pytest.fixture
def frontend(tmp_path):
    directory = tmp_path / "dist"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text('<script src="/assets/accounts.js"></script>', encoding="utf-8")
    (directory / "assets" / "accounts.js").write_text("// compiled QR login UI", encoding="utf-8")
    record_frontend(directory, "commit-a")
    return directory


def test_frontend_handoff_requires_the_same_revision(frontend):
    assert verify_frontend(frontend, "commit-a")["source_revision"] == "commit-a"
    with pytest.raises(ValueError, match="different source revision"):
        verify_frontend(frontend, "commit-b")


@pytest.mark.parametrize("mutation", ["change", "remove", "extra"])
def test_frontend_handoff_rejects_changed_missing_or_extra_files(frontend, mutation):
    asset = frontend / "assets" / "accounts.js"
    if mutation == "change":
        asset.write_text("// stale JSON-only UI", encoding="utf-8")
    elif mutation == "remove":
        asset.unlink()
    else:
        (frontend / "unwanted.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="files are missing, changed or unexpected"):
        verify_frontend(frontend, "commit-a")


def test_package_contains_only_deliverables_with_verifiable_checksums(frontend, tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    executable = build / "GoofishMonitor.exe"
    executable.write_bytes(b"MZ-test-only")
    (build / ".env").write_text("PRIVATE=test-only", encoding="utf-8")
    (build / "state.json").write_text("{}", encoding="utf-8")
    readme = tmp_path / "portable.txt"
    readme.write_text("portable instructions", encoding="utf-8")
    output = tmp_path / "package"
    info = prepare_package(executable, readme, frontend, output, "commit-a")
    assert {path.name for path in output.iterdir()} == {
        "GoofishMonitor.exe", "README.txt", "build-info.json", "SHA256SUMS.txt",
    }
    assert info["source_revision"] == info["frontend_revision"] == "commit-a"
    assert info["frontend_manifest_sha256"] == sha256(frontend / MANIFEST)
    assert json.loads((output / "build-info.json").read_text()) == info
    for line in (output / "SHA256SUMS.txt").read_text().splitlines():
        expected, filename = line.split("  ")
        assert sha256(output / filename) == expected


def test_package_does_not_reuse_a_directory_containing_old_files(frontend, tmp_path):
    executable = tmp_path / "GoofishMonitor.exe"
    executable.write_bytes(b"MZ-test-only")
    readme = tmp_path / "portable.txt"
    readme.write_text("instructions", encoding="utf-8")
    output = tmp_path / "package"
    output.mkdir()
    stale = output / "old.exe"
    stale.write_bytes(b"old-build")
    with pytest.raises(ValueError, match="output must be empty"):
        prepare_package(executable, readme, frontend, output, "commit-a")
    assert stale.read_bytes() == b"old-build"
