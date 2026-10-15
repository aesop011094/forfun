"""Keep a frozen portable download below GitHub's 100 MiB Git blob limit.

Only unused developer tools, optional Tk UI and dependency tests are omitted.
Scientific libraries, data, native computational DLLs and licenses are retained.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "Football-Intelligence-AI-Windows/"


def omit(relative: str) -> bool:
    path = PurePosixPath(relative)
    parts = path.parts
    # Retain copyright/license notices, including in optional components.
    if "license" in path.name.lower() or "copying" in path.name.lower():
        return False
    if relative.startswith(("runtime/tcl/", "runtime/include/", "runtime/libs/",
                            "runtime/Lib/tkinter/", "runtime/Lib/idlelib/",
                            "runtime/Lib/venv/", "runtime/Lib/ensurepip/",
                            "runtime/Lib/site-packages/pip/")):
        return True
    if relative in {"runtime/DLLs/tcl86t.dll", "runtime/DLLs/tk86t.dll", "runtime/DLLs/_tkinter.pyd"}:
        return True
    if len(parts) >= 5 and parts[:3] == ("runtime", "Lib", "site-packages") and parts[3] in {"numpy", "scipy", "sklearn", "joblib"}:
        return any(part in {"test", "tests"} for part in parts[4:])
    return False


def build(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as original:
        manifest = json.loads(original.read(PREFIX + "package-manifest.json"))
        removed = [name for name in manifest["files"] if omit(name)]
        manifest["files"] = {name: digest for name, digest in manifest["files"].items() if not omit(name)}
        manifest["portable_profile"] = "GitHub compact download"
        manifest["omitted_components"] = ["dependency test suites", "pip/ensurepip/venv developer tools", "unused Tcl/Tk/IDLE", "C development headers/import libraries"]
        manifest["omitted_files"] = len(removed)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as output:
            for item in original.infolist():
                if not item.filename.startswith(PREFIX):
                    raise ValueError("Unexpected package root")
                relative = item.filename[len(PREFIX):]
                if relative == "package-manifest.json" or omit(relative):
                    continue
                data = original.read(item)
                if relative in manifest["files"]:
                    if hashlib.sha256(data).hexdigest() != manifest["files"][relative]:
                        raise ValueError(f"Original file failed checksum: {relative}")
                output.writestr(item.filename, data)
            output.writestr(PREFIX + "package-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    if target.stat().st_size >= 100 * 1024**2:
        raise ValueError("Download exceeds GitHub's single-file limit")
    with zipfile.ZipFile(target) as output:
        if output.testzip() is not None:
            raise ValueError("ZIP integrity check failed")
        for relative, digest in manifest["files"].items():
            if hashlib.sha256(output.read(PREFIX + relative)).hexdigest() != digest:
                raise ValueError(f"Output checksum failed: {relative}")
        for name in ("app/backend/models.py", "assets/football.seed.sqlite3", "runtime/pythonw.exe", "runtime/DLLs/_ssl.pyd", "runtime/DLLs/_sqlite3.pyd", "runtime/Lib/site-packages/tzdata/zoneinfo/Asia/Hong_Kong", "Start.cmd"):
            assert PREFIX + name in output.namelist(), name
    target.with_suffix(".sha256").write_text(hashlib.sha256(target.read_bytes()).hexdigest() + "  " + target.name + "\n", encoding="utf-8")
    print(json.dumps({"file": str(target), "size_mib": round(target.stat().st_size / 1024**2, 2), "retained_files": len(manifest["files"]), "omitted_files": len(removed), "integrity": "all retained checksums matched"}))


if __name__ == "__main__":
    build(Path("/workspace/Football-Intelligence-AI-Windows.zip"), ROOT / "downloads/Football-Intelligence-AI-Windows.zip")
