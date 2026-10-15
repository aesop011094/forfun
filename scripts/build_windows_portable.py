"""Assemble already verified Windows binaries; do not cross-compile or install."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DOWNLOADS = ROOT / "data/windows-downloads"
STAGING = ROOT / "data/windows-build/Football-Intelligence-AI-Windows"


def safe_parts(name: str) -> tuple[str, ...]:
    parts = Path(name.replace("\\", "/")).parts
    if not parts or any(part in {"..", "."} for part in parts) or Path(name).is_absolute() or ":" in parts[0]:
        raise ValueError(f"Unsafe archive path: {name}")
    return parts


def assemble() -> Path:
    runtime_record = json.loads((DOWNLOADS / "runtime-provenance.json").read_text())
    archive = DOWNLOADS / runtime_record["source"].rsplit("/", 1)[1]
    if hashlib.sha256(archive.read_bytes()).hexdigest() != runtime_record["sha256"]:
        raise ValueError("Python runtime checksum differs from verified release")
    wheel_records = json.loads((DOWNLOADS / "wheel-provenance.json").read_text())
    # Clear only our generated staging tree, never application/user data.
    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir(parents=True)
    runtime = STAGING / "runtime"
    runtime.mkdir()
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            parts = safe_parts(member.name)
            if parts[0] != "python" or member.isdir():
                continue
            if not member.isfile():
                raise ValueError("Unexpected runtime link or special file")
            if member.name.endswith(".pdb") or "__pycache__" in parts:
                continue  # Debug symbols are not needed to run the program.
            target = runtime.joinpath(*parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as stream, target.open("wb") as dest:
                shutil.copyfileobj(stream, dest)
    site = runtime / "Lib/site-packages"
    site.mkdir(parents=True, exist_ok=True)
    for record in wheel_records:
        wheel = DOWNLOADS / "wheels" / record["filename"]
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError(f"Wheel checksum differs: {wheel.name}")
        if "win_amd64" not in wheel.name and "none-any.whl" not in wheel.name:
            raise ValueError(f"Not a Windows x64 or pure Python wheel: {wheel.name}")
        with zipfile.ZipFile(wheel) as source:
            for item in source.infolist():
                if item.is_dir():
                    continue
                parts = safe_parts(item.filename)
                if parts[0].endswith(".data"):
                    if len(parts) < 3 or parts[1] not in {"purelib", "platlib"}:
                        continue  # CLI scripts and development headers unused.
                    parts = parts[2:]
                target = site.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read(item))
    app = STAGING / "app"
    app.mkdir()
    shutil.copytree(ROOT / "backend", app / "backend", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "web/dist", app / "web/dist")
    shutil.copy2(ROOT / "portable/desktop_launcher.py", app / "desktop_launcher.py")
    for name in ("Start.vbs", "Start.cmd", "READ-ME.txt"):
        shutil.copy2(ROOT / "portable" / name, STAGING / name)
    assets = STAGING / "assets"
    assets.mkdir()
    with closing(sqlite3.connect(ROOT / "data/football.db")) as source, closing(sqlite3.connect(assets / "football.seed.sqlite3")) as dest:
        source.backup(dest)
    shutil.copytree(ROOT / "data/raw", assets / "raw")
    docs = STAGING / "docs"
    docs.mkdir()
    for name in ("README.md", "DATA_SOURCES.md", "VALIDATION.md"):
        shutil.copy2(ROOT / name, docs / name)
    for name in ("runtime-provenance.json", "wheel-provenance.json", "SHA256SUMS"):
        shutil.copy2(DOWNLOADS / name, docs / name)
    notices = []
    for dependency in ("react", "react-dom", "scheduler", "lucide-react"):
        license_file = ROOT / "web/node_modules" / dependency / "LICENSE"
        notices.append(f"{dependency}\n{'=' * len(dependency)}\n" + license_file.read_text(encoding="utf-8"))
    (docs / "FRONTEND-THIRD-PARTY-LICENSES.txt").write_text("\n\n".join(notices), encoding="utf-8")
    evidence = ROOT / "data/qa"
    if evidence.exists():
        shutil.copytree(evidence, docs / "validation", ignore=shutil.ignore_patterns("*.png"))
    manifest = {
        "name": "Football Intelligence AI", "platform": "Windows 10/11 x64",
        "python": runtime_record["python"], "runtime_verified": True,
        "wheel_count": len(wheel_records), "contains_paid_services": False,
        "files": {str(path.relative_to(STAGING)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sorted(STAGING.rglob("*")) if path.is_file()},
    }
    (STAGING / "package-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return STAGING


def make_zip(folder: Path, output: Path) -> None:
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as dest:
        for path in folder.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                dest.write(path, str(Path(folder.name) / path.relative_to(folder)))
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError("Final ZIP integrity check failed")
    output.with_suffix(".sha256").write_text(hashlib.sha256(output.read_bytes()).hexdigest() + "  " + output.name + "\n")
    print(json.dumps({"archive": str(output), "size_mb": round(output.stat().st_size / 1024**2, 1), "integrity": "passed"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging-only", action="store_true")
    parser.add_argument("--zip-only", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("/workspace/Football-Intelligence-AI-Windows.zip"))
    args = parser.parse_args()
    folder = STAGING if args.zip_only else assemble()
    print(f"Staging ready: {folder}")
    if not args.staging_only:
        make_zip(folder, args.output)
