"""Setup verification script for the patent-landscape-report skill.

Run this once before using the skill for the first time, and any time the
skill is updated to newer vendor files.

Checks:
    1. Python version (>= 3.11)
    2. Required Python packages (jinja2, google-cloud-bigquery)
    3. google-patent-search skill is installed (for BigQuery mode)
    4. Vendor files present in skill/vendor/; downloads if missing
    5. ~/.claude/skills/patent-landscape-report symlink (optional, recommended)

Prints a summary at the end with any follow-up instructions.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path
from urllib.request import urlopen


SKILL_ROOT = Path(__file__).resolve().parent
VENDOR_DIR = SKILL_ROOT / "vendor"
SCRIPTS_DIR = SKILL_ROOT / "scripts"


# Vendor files to download if missing. Keys: filename. Values: (url, min_bytes)
VENDOR_FILES: dict[str, tuple[str, int]] = {
    "echarts.min.js": (
        "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js",
        900_000,  # sanity check: should be >= 900 KB
    ),
    "world.geo.json": (
        "https://raw.githubusercontent.com/johan/world.geo.json/master/countries.geo.json",
        200_000,  # sanity check: should be >= 200 KB
    ),
}


class Check:
    def __init__(self, name: str):
        self.name = name
        self.status = "pending"
        self.note = ""

    def ok(self, note: str = "") -> None:
        self.status = "ok"
        self.note = note

    def warn(self, note: str) -> None:
        self.status = "warn"
        self.note = note

    def fail(self, note: str) -> None:
        self.status = "fail"
        self.note = note

    def render(self) -> str:
        icon = {"ok": "\u2713", "warn": "!", "fail": "\u2717", "pending": "\u00b7"}[self.status]
        line = f"  [{icon}] {self.name}"
        if self.note:
            line += f"\n      {self.note}"
        return line


def check_python_version() -> Check:
    c = Check("Python 3.9+")
    v = sys.version_info
    if (v.major, v.minor) >= (3, 9):
        c.ok(f"found Python {v.major}.{v.minor}.{v.micro}")
    else:
        c.fail(
            f"found Python {v.major}.{v.minor}.{v.micro}; need 3.9 or newer. "
            "Install a newer Python and re-run."
        )
    return c


def check_jinja2() -> Check:
    c = Check("jinja2 package")
    try:
        import jinja2
        c.ok(f"found jinja2 {jinja2.__version__}")
    except ImportError:
        c.fail("not installed. Run: pip3 install jinja2")
    return c


def check_bigquery() -> Check:
    c = Check("google-cloud-bigquery package (for BigQuery mode)")
    try:
        import google.cloud.bigquery  # type: ignore
        c.ok("installed")
    except ImportError:
        c.warn(
            "not installed. BigQuery search mode will not work. "
            "CSV mode still works. To enable BigQuery: pip3 install google-cloud-bigquery"
        )
    return c


def check_google_patent_search_skill() -> Check:
    c = Check("google-patent-search skill")
    skill_dir = Path(os.path.expanduser("~/.claude/skills/google-patent-search"))
    if not skill_dir.exists():
        c.warn(
            f"not found at {skill_dir}. BigQuery mode relies on it. "
            "Install it or skip BigQuery mode and use CSV inputs only."
        )
        return c
    scripts = skill_dir / "scripts" / "bigquery_client.py"
    if not scripts.exists():
        c.warn(f"found skill folder but missing scripts/bigquery_client.py")
        return c
    c.ok(f"found at {skill_dir}")
    return c


def check_vendor_files(auto_download: bool = True) -> Check:
    c = Check(f"Vendor files in {VENDOR_DIR.name}/")
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)

    missing = []
    for name, (url, min_bytes) in VENDOR_FILES.items():
        path = VENDOR_DIR / name
        if path.exists() and path.stat().st_size >= min_bytes:
            continue
        missing.append(name)
        if auto_download:
            try:
                print(f"    downloading {name} ...")
                with urlopen(url, timeout=60) as resp:
                    data = resp.read()
                if len(data) < min_bytes:
                    raise RuntimeError(
                        f"downloaded file is too small ({len(data)} bytes < {min_bytes})"
                    )
                path.write_bytes(data)
            except Exception as e:
                c.fail(f"failed to download {name} from {url}: {e}")
                return c

    present = [n for n in VENDOR_FILES if (VENDOR_DIR / n).exists()]
    missing_after = [n for n in VENDOR_FILES if not (VENDOR_DIR / n).exists()]
    if missing_after:
        c.fail(f"still missing: {', '.join(missing_after)}")
        return c
    note = ", ".join(f"{n} ({(VENDOR_DIR / n).stat().st_size // 1024} KB)" for n in present)
    if missing:
        note = f"downloaded {', '.join(missing)}; " + note
    c.ok(note)
    return c


def check_skill_symlink() -> Check:
    c = Check("~/.claude/skills/patent-landscape-report symlink")
    target_parent = Path(os.path.expanduser("~/.claude/skills"))
    target_parent.mkdir(parents=True, exist_ok=True)
    target = target_parent / "patent-landscape-report"

    if target.exists() or target.is_symlink():
        if target.is_symlink():
            existing_target = target.resolve()
            if existing_target == SKILL_ROOT.resolve():
                c.ok(f"already pointing at this skill ({target})")
                return c
            else:
                c.warn(
                    f"exists but points to {existing_target}, not {SKILL_ROOT}. "
                    "Leaving it alone; remove manually if you want to relink."
                )
                return c
        else:
            c.warn(
                f"exists as a regular file/directory at {target}. "
                "Remove it manually if you want to symlink this skill there."
            )
            return c

    try:
        target.symlink_to(SKILL_ROOT.resolve(), target_is_directory=True)
        c.ok(f"linked {target} -> {SKILL_ROOT}")
    except OSError as e:
        c.fail(f"could not create symlink: {e}")
    return c


def main() -> int:
    print("patent-landscape-report skill: setup check")
    print(f"  skill root: {SKILL_ROOT}")
    print()

    checks = [
        check_python_version(),
        check_jinja2(),
        check_bigquery(),
        check_google_patent_search_skill(),
        check_vendor_files(auto_download=True),
        check_skill_symlink(),
    ]

    print("Results:")
    for c in checks:
        print(c.render())
    print()

    fails = [c for c in checks if c.status == "fail"]
    warns = [c for c in checks if c.status == "warn"]

    if fails:
        print(f"{len(fails)} check(s) failed. Address the errors above before running the skill.")
        return 1
    if warns:
        print(
            f"{len(warns)} warning(s). Skill will work for {'CSV' if any('bigquery' in w.name.lower() or 'google' in w.name.lower() for w in warns) else 'some'} "
            "workflows but may have limited functionality."
        )
        return 0
    print("All checks passed. The skill is ready to use.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
