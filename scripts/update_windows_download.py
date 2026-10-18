"""Refresh application files in a verified portable ZIP, retaining its seed/runtime.

Run after building and testing web/dist. No user database is read or copied.
The existing download is replaced only after every output file passes validation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOWNLOAD = ROOT / "downloads/Football-Intelligence-AI-Windows.zip"
PREFIX = "Football-Intelligence-AI-Windows/"
MANIFEST_NAME = PREFIX + "package-manifest.json"
MAX_BYTES = 100 * 1024**2


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def archive_digest(archive: zipfile.ZipFile, name: str) -> str:
    with archive.open(name) as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_archive(path: Path) -> dict:
    """Check paths, CRC, complete manifest coverage and every file's SHA256."""
    with zipfile.ZipFile(path) as archive:
        names: set[str] = set()
        windows_names: set[str] = set()
        for item in archive.infolist():
            name = item.filename
            if not name.startswith(PREFIX) or "\\" in name:
                raise ValueError(f"Unexpected archive path: {name}")
            relative = name[len(PREFIX):]
            parts = PurePosixPath(relative).parts
            if not parts or relative.startswith("/") or any(part in {"..", "."} or ":" in part for part in parts):
                raise ValueError(f"Unsafe archive path: {name}")
            if name.casefold() in windows_names:
                raise ValueError(f"Duplicate Windows archive path: {name}")
            windows_names.add(name.casefold())
            if item.is_dir():
                continue
            if relative.startswith("data/"):
                raise ValueError("Portable packages must not contain a user's writable data folder")
            names.add(name)
        if archive.testzip() is not None:
            raise ValueError("ZIP CRC integrity check failed")
        manifest = json.loads(archive.read(MANIFEST_NAME))
        files = manifest["files"]
        if not isinstance(files, dict) or MANIFEST_NAME in {PREFIX + name for name in files}:
            raise ValueError("Invalid package manifest")
        if names != {MANIFEST_NAME, *(PREFIX + name for name in files)}:
            raise ValueError("Package files do not exactly match the manifest")
        for relative, digest in files.items():
            if archive_digest(archive, PREFIX + relative) != digest:
                raise ValueError(f"File checksum failed: {relative}")
        for relative in (
            "Start.cmd", "Start.vbs", "READ-ME.txt", "app/desktop_launcher.py",
            "app/backend/main.py", "app/backend/models.py", "app/web/dist/index.html",
            "assets/football.seed.sqlite3", "runtime/pythonw.exe",
            "runtime/DLLs/_ssl.pyd", "runtime/DLLs/_sqlite3.pyd",
            "runtime/Lib/site-packages/tzdata/zoneinfo/Asia/Hong_Kong",
            "docs/runtime-provenance.json", "docs/wheel-provenance.json",
            "docs/FRONTEND-THIRD-PARTY-LICENSES.txt",
        ):
            if relative not in files:
                raise ValueError(f"Required portable file is missing: {relative}")
        return manifest


def replacements() -> dict[str, Path]:
    files: dict[str, Path] = {}
    for folder, prefix, suffix in (
        (ROOT / "backend", "app/backend/", ".py"),
        (ROOT / "web/dist", "app/web/dist/", None),
    ):
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if suffix is not None and path.suffix != suffix:
                continue
            if path.is_symlink():
                raise ValueError(f"Application source must not be a symlink: {path}")
            files[prefix + path.relative_to(folder).as_posix()] = path
    for relative, path in {
        "app/desktop_launcher.py": ROOT / "portable/desktop_launcher.py",
        **{name: ROOT / "portable" / name for name in ("Start.cmd", "Start.vbs", "READ-ME.txt")},
        **{"docs/" + name: ROOT / name for name in ("README.md", "DATA_SOURCES.md", "VALIDATION.md")},
    }.items():
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Required source file is missing or linked: {path}")
        files[relative] = path
    if "app/web/dist/index.html" not in files or not any(name.startswith("app/web/dist/assets/") and name.endswith(".js") for name in files):
        raise ValueError("Build the production website before updating the portable download")
    manual_evidence = ROOT / "data/qa/manual-browser-smoke.json"
    if manual_evidence.is_file():
        files["docs/validation/manual-browser-smoke.json"] = manual_evidence
    return files


def replaced(relative: str, updates: dict[str, Path]) -> bool:
    # Delete all former hashed frontend assets and removed backend modules.
    return relative.startswith(("app/backend/", "app/web/dist/")) or relative in updates


def update(source: Path, target: Path, features: list[str]) -> dict:
    checksum = source.with_suffix(".sha256")
    expected = checksum.read_text(encoding="utf-8").split()[0]
    source_digest = file_digest(source)
    if len(expected) != 64 or expected.lower() != source_digest:
        raise ValueError("Source ZIP differs from its published SHA256 file")
    manifest = validate_archive(source)
    if manifest.get("portable_profile") != "GitHub compact download":
        raise ValueError("Use the verified compact Windows download as the source")
    updates = replacements()
    original_files = manifest["files"]
    new_files = {name: digest for name, digest in original_files.items() if not replaced(name, updates)}
    new_files.update({name: file_digest(path) for name, path in updates.items()})
    manifest["files"] = dict(sorted(new_files.items()))
    manifest["application_update"] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "features": features,
        "source_zip_sha256": source_digest,
        "retained_original_seed_and_runtime": True,
        "scope": "Application and documentation only; no new platform-validation claim",
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    zip_handle, temporary_name = tempfile.mkstemp(prefix=".windows-update-", suffix=".zip", dir=target.parent)
    os.close(zip_handle)
    temporary = Path(temporary_name)
    checksum_temporary: Path | None = None
    try:
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as output:
            for item in original.infolist():
                relative = item.filename[len(PREFIX):]
                if item.is_dir() or item.filename == MANIFEST_NAME or replaced(relative, updates):
                    continue
                # Preserve license notices, runtime, seed, raw data and provenance.
                with original.open(item) as reader, output.open(item.filename, "w") as writer:
                    while block := reader.read(1024 * 1024):
                        writer.write(block)
            for relative, path in sorted(updates.items()):
                output.write(path, PREFIX + relative)
            output.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
        if temporary.stat().st_size >= MAX_BYTES:
            raise ValueError("Updated ZIP exceeds GitHub's 100 MiB single-file limit; original download was retained")
        checked = validate_archive(temporary)
        if checked != manifest:
            raise ValueError("Updated manifest does not match the generated package")
        retained = [name for name in original_files if not replaced(name, updates)]
        if any(checked["files"][name] != original_files[name] for name in retained):
            raise ValueError("A retained runtime, data or provenance file changed")
        digest = file_digest(temporary)
        checksum_handle, checksum_name = tempfile.mkstemp(prefix=".windows-update-", suffix=".sha256", dir=target.parent)
        checksum_temporary = Path(checksum_name)
        with os.fdopen(checksum_handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{digest}  {target.name}\n")
        # The source ZIP is now closed, which also permits replacement on Windows.
        os.replace(temporary, target)
        os.replace(checksum_temporary, target.with_suffix(".sha256"))
        return {
            "file": str(target), "size_mib": round(target.stat().st_size / 1024**2, 2),
            "sha256": digest, "updated_files": len(updates), "retained_files": len(retained),
            "features": features, "integrity": "CRC and all manifest hashes verified",
        }
    finally:
        temporary.unlink(missing_ok=True)
        if checksum_temporary is not None:
            checksum_temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_DOWNLOAD)
    parser.add_argument("--output", type=Path, help="Defaults to replacing --source after verification")
    parser.add_argument("--feature", action="append", dest="features", help="Feature identifier; may be repeated")
    parser.add_argument("--verify-only", action="store_true", help="Verify the source without changing any package")
    arguments = parser.parse_args()
    if arguments.verify_only:
        manifest = validate_archive(arguments.source)
        digest = file_digest(arguments.source)
        expected = arguments.source.with_suffix(".sha256").read_text(encoding="utf-8").split()[0]
        if expected.lower() != digest:
            raise ValueError("Source ZIP differs from its published SHA256 file")
        print(json.dumps({"file": str(arguments.source), "files": len(manifest["files"]), "sha256": digest, "integrity": "verified"}))
    else:
        print(json.dumps(update(arguments.source, arguments.output or arguments.source, arguments.features or ["manual-match-analysis"])))
