#!/usr/bin/env python3
import argparse
import csv
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

VERSION = "1.0.0"

TAG_FIELDS = [
    # "flat" keyword sets
    ("XMP:Subject", "subject"),
    ("IPTC:Keywords", "keywords"),
    # hierarchical (digiKam often uses lr:HierarchicalSubject)
    ("XMP-lr:HierarchicalSubject", "hierarchical"),
]

DEFAULT_EXTS = {
    # images
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic",
    # common raws (exiftool may write XMP sidecars depending on config/format)
    ".cr2", ".cr3", ".nef", ".arw", ".rw2", ".orf", ".raf", ".dng",
    # videos (tags support varies; you can include or exclude as you prefer)
    ".mp4", ".mov", ".m4v",
}

# Sidecars are commonly used for these formats when direct metadata write isn't supported.
SIDECAR_PRONE_EXTS = {
    ".cr2", ".cr3", ".nef", ".arw", ".rw2", ".orf", ".raf", ".dng",
}


@dataclass
class Change:
    path: str
    field: str
    before: List[str]
    after: List[str]


def run(cmd: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode, p.stdout, p.stderr


def load_map(csv_path: Path, case_insensitive: bool) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    with csv_path.open(newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r, None)
        if not header:
            raise ValueError("Mapping CSV is empty")
        # allow with/without header
        has_header = any(h.lower() in ("old_tag", "old", "from") for h in header)
        if not has_header:
            # first row is data
            if len(header) < 2:
                raise ValueError("Mapping CSV must have two columns: old_tag,new_tag")
            old, new = header[0].strip(), header[1].strip()
            if old and new:
                mapping[old.lower() if case_insensitive else old] = new
        for row in r:
            if not row or len(row) < 2:
                continue
            old, new = row[0].strip(), row[1].strip()
            if not old or not new:
                continue
            key = old.lower() if case_insensitive else old
            mapping[key] = new
    return mapping


def list_media(root: Path, exts: set[str]) -> List[Path]:
    out: List[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() in exts:
            out.append(p)
    return out


def exiftool_read_tags(file_path: Path) -> Dict[str, List[str]]:
    # Read all tag fields in one shot, JSON output
    cmd = ["exiftool", "-json", "-charset", "filename=utf8"]
    for field, _ in TAG_FIELDS:
        cmd.append(f"-{field}")
    cmd.append(str(file_path))

    rc, out, err = run(cmd)
    if rc != 0:
        raise RuntimeError(f"exiftool read failed: {err.strip()}")
    data = json.loads(out)
    if not data:
        return {}
    obj = data[0]
    result: Dict[str, List[str]] = {}
    for field, key in TAG_FIELDS:
        # ExifTool may return a string or a list, or omit
        val = obj.get(field.split(":", 1)[-1])  # sometimes ExifTool shortens keys
        if val is None:
            # try full key
            val = obj.get(field)
        if val is None:
            continue
        if isinstance(val, list):
            result[key] = [str(x) for x in val]
        else:
            result[key] = [str(val)]
    return result


def dedupe_preserve(seq: List[str]) -> List[str]:
    seen = set()
    out = []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def rewrite_tags(
    tags: List[str],
    mapping: Dict[str, str],
    case_insensitive: bool,
    hierarchical: bool = False,
) -> Tuple[List[str], bool]:
    """
    Rewrite tags using mapping.

    - Normal fields (XMP-dc:Subject, IPTC:Keywords): exact-tag replacement.
    - Hierarchical fields (XMP-lr:HierarchicalSubject): replace ONLY the leaf token
      (the last component after the final '|'), preserving the path prefix.
    """
    changed = False
    new_tags: List[str] = []

    for t in tags:
        if not hierarchical:
            key = t.lower() if case_insensitive else t
            if key in mapping:
                new_tags.append(mapping[key])
                changed = True
            else:
                new_tags.append(t)
            continue

        # hierarchical=True
        parts = t.split("|")
        leaf = parts[-1]
        leaf_key = leaf.lower() if case_insensitive else leaf

        if leaf_key in mapping:
            parts[-1] = mapping[leaf_key]
            rewritten = "|".join(parts)
            new_tags.append(rewritten)
            if rewritten != t:
                changed = True
        else:
            new_tags.append(t)

    new_tags = dedupe_preserve(new_tags)

    # If mapping causes a net no-op (e.g. old->same), detect it
    if new_tags == tags:
        changed = False

    return new_tags, changed


def exiftool_write_file(
    file_path: Path,
    updates: Dict[str, List[str]],
    apply: bool,
    verbose: bool,
    allow_sidecar: bool,
) -> None:
    cmd = ["exiftool", "-charset", "filename=utf8", "-overwrite_original", "-P"]
    for field, values in updates.items():
        cmd.append(f"-{field}=")  # clear current values
        for v in values:
            cmd.append(f"-{field}={v}")
    cmd.append(str(file_path))

    if verbose:
        print("[EXIFTOOL]", " ".join(shlex.quote(x) for x in cmd))

    if not apply:
        return

    rc, out, err = run(cmd)
    if rc != 0:
        raise RuntimeError(f"exiftool write failed: {err.strip()}")

    output_lower = (out + "\n" + err).lower()
    if (not allow_sidecar) and ("created xmp sidecar file" in output_lower):
        raise RuntimeError("Refusing sidecar creation (use --allow-sidecar to opt in).")


def print_summary(root: Path, map_path: Path, exts: set[str], dry_run: bool, no_video: bool, case_insensitive: bool) -> None:
    print("=== Retag operation summary ===")
    print(f"Root:        {root}")
    print(f"Mapping CSV: {map_path}")
    print(f"Extensions:  {' '.join(sorted(exts))}")
    print(f"Mode:        {'DRY-RUN' if dry_run else 'APPLY'}")
    print(f"Video files: {'excluded' if no_video else 'included'}")
    print(f"Case match:  {'insensitive' if case_insensitive else 'exact'}")
    print("================================")


def confirm_proceed() -> bool:
    try:
        answer = input("Do you want to proceed? (Y|n) ").strip().lower()
    except EOFError:
        return False
    return answer in ("", "y", "yes")


def main():
    ap = argparse.ArgumentParser(
        description="Rename/normalize tags in media files using exiftool + a mapping CSV.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        add_help=False,
    )
    ap.add_argument("-h", "--help", action="help", help="Show this help message and exit")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}", help="Show program version and exit")
    ap.add_argument("--root", required=True, help="Root folder to scan (e.g. /Volumes/.../Photos or /data/photos)")
    ap.add_argument("--map", required=True, help="CSV mapping file: old_tag,new_tag")
    ap.add_argument("--ext", action="append", default=[], help="Extra file extension to include (repeatable, include dot)")
    ap.add_argument("--no-video", action="store_true", help="Exclude common video extensions")
    ap.add_argument("--dry-run", action="store_true", default=True, help="Do not write changes (default)")
    ap.add_argument("--apply", action="store_true", help="Actually write changes")
    ap.add_argument("--allow-sidecar", action="store_true", help="Allow ExifTool sidecar creation when needed")
    ap.add_argument("--case-insensitive", action="store_true", help="Match old tags case-insensitively")
    ap.add_argument("--report", default="retag_report.csv", help="CSV report output path")
    ap.add_argument("--verbose", action="store_true", help="Verbose logging (shows exiftool commands)")
    args = ap.parse_args()

    if args.apply:
        dry_run = False
    else:
        dry_run = True  # default safe mode

    root = Path(args.root).expanduser().resolve()
    map_path = Path(args.map).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve()

    if not root.is_dir():
        print(f"ERROR: root not found: {root}", file=sys.stderr)
        sys.exit(2)
    if not map_path.is_file():
        print(f"ERROR: map file not found: {map_path}", file=sys.stderr)
        sys.exit(2)

    # Verify exiftool exists
    rc, _, _ = run(["bash", "-lc", "command -v exiftool >/dev/null 2>&1"])
    if rc != 0:
        print("ERROR: exiftool not found in PATH", file=sys.stderr)
        sys.exit(2)

    mapping = load_map(map_path, args.case_insensitive)

    exts = set(DEFAULT_EXTS)
    for e in args.ext:
        exts.add(e.lower() if e.startswith(".") else f".{e.lower()}")
    if args.no_video:
        for v in (".mp4", ".mov", ".m4v"):
            exts.discard(v)

    print_summary(root, map_path, exts, dry_run, args.no_video, args.case_insensitive)
    if not confirm_proceed():
        print("Aborted by user.")
        sys.exit(0)

    files = list_media(root, exts)
    print(f"Files matched: {len(files)}")
    print(f"Sidecar: {'ALLOWED' if args.allow_sidecar else 'DISALLOWED'}")
    print(f"Report: {report_path}")

    changes: List[Change] = []
    errors: List[Tuple[str, str]] = []

    for fp in files:
        try:
            if (not args.allow_sidecar) and fp.suffix.lower() in SIDECAR_PRONE_EXTS and not dry_run:
                raise RuntimeError("Sidecar-prone format blocked by default (use --allow-sidecar to opt in).")

            tags_by_field = exiftool_read_tags(fp)
            updates: Dict[str, List[str]] = {}
            pending_changes: List[Change] = []

            for field, key in TAG_FIELDS:
                before = tags_by_field.get(key, [])
                if not before:
                    continue

                is_hier = field.endswith("HierarchicalSubject")
                after, changed = rewrite_tags(before, mapping, args.case_insensitive, hierarchical=is_hier)

                if not changed:
                    continue

                pending_changes.append(Change(str(fp), field, before, after))
                updates[field] = after

            if updates:
                # Single exiftool write call per file avoids partial per-field updates.
                exiftool_write_file(
                    fp,
                    updates,
                    apply=(not dry_run),
                    verbose=args.verbose,
                    allow_sidecar=args.allow_sidecar,
                )
                changes.extend(pending_changes)
                if args.verbose:
                    print(f"[OK] {fp}")
        except Exception as e:
            errors.append((str(fp), str(e)))

    # Write report
    with report_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["path", "field", "before_json", "after_json"])
        for c in changes:
            w.writerow([
                c.path,
                c.field,
                json.dumps(c.before, ensure_ascii=False),
                json.dumps(c.after, ensure_ascii=False),
            ])

    print(f"Changes: {len(changes)} field-updates")
    if errors:
        print(f"Errors: {len(errors)} (first 10 shown)", file=sys.stderr)
        for p, e in errors[:10]:
            print(f"  - {p}: {e}", file=sys.stderr)
        # keep nonzero exit so you notice
        sys.exit(1)


if __name__ == "__main__":
    main()
