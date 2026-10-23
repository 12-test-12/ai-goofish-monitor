"""Verify the frontend handed between CI jobs and stage the portable desktop package."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil


MANIFEST = "frontend-build.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def frontend_files(directory: Path) -> dict[str, str]:
    if not (directory / "index.html").is_file() or not (directory / "assets").is_dir():
        raise ValueError("Frontend build is missing index.html or assets/; run npm run build first")
    root = directory.resolve()
    files = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path != directory / MANIFEST:
            if not path.resolve().is_relative_to(root):
                raise ValueError("Frontend contains a file outside the build directory")
            files[path.relative_to(directory).as_posix()] = sha256(path)
    return files


def record_frontend(directory: Path, revision: str) -> dict:
    manifest = {"source_revision": revision, "files": frontend_files(directory)}
    (directory / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify_frontend(directory: Path, revision: str) -> dict:
    manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("source_revision") != revision:
        raise ValueError("Frontend artifact belongs to a different source revision")
    if manifest.get("files") != frontend_files(directory):
        raise ValueError("Frontend artifact files are missing, changed or unexpected")
    return manifest


def prepare_package(executable: Path, readme: Path, frontend: Path, output: Path, revision: str) -> dict:
    manifest = verify_frontend(frontend, revision)
    if not executable.is_file() or executable.stat().st_size == 0:
        raise ValueError("Desktop executable is missing or empty")
    if not readme.is_file():
        raise ValueError("Portable README is missing")
    # A fresh allowlisted directory prevents build logs or local accounts from
    # accidentally becoming part of the uploaded portable package.
    if output.exists() and any(output.iterdir()):
        raise ValueError("Package output must be empty; choose a new output directory")
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(executable, output / "GoofishMonitor.exe")
    shutil.copy2(readme, output / "README.txt")
    info = {
        "source_revision": revision,
        "frontend_revision": manifest["source_revision"],
        "frontend_manifest_sha256": sha256(frontend / MANIFEST),
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "platform": "Windows-x64",
        "workflow_run_id": os.getenv("GITHUB_RUN_ID"),
    }
    (output / "build-info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    checksums = [f"{sha256(path)}  {path.name}" for path in sorted(output.iterdir())]
    (output / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["record-frontend", "verify-frontend", "prepare"])
    parser.add_argument("--frontend", type=Path, default=Path("dist"))
    parser.add_argument("--revision", default=os.getenv("GITHUB_SHA", "local"))
    parser.add_argument("--executable", type=Path, default=Path("audit/desktop/dist/GoofishMonitor.exe"))
    parser.add_argument("--readme", type=Path, default=Path("docs/DESKTOP_PORTABLE.txt"))
    parser.add_argument("--output", type=Path, default=Path("audit/desktop/package"))
    args = parser.parse_args()
    if args.command == "record-frontend":
        record_frontend(args.frontend, args.revision)
    elif args.command == "verify-frontend":
        verify_frontend(args.frontend, args.revision)
    else:
        prepare_package(args.executable, args.readme, args.frontend, args.output, args.revision)
    print(f"{args.command}: passed (source {args.revision})")


if __name__ == "__main__":
    main()
