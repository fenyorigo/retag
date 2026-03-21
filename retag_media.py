#!/usr/bin/env python3
import argparse
import csv
import json
import logging
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

VERSION = "1.1.0"

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


@dataclass
class FileResult:
    path: str
    rel_path: str
    status: str
    changed_fields: int
    reindexed: bool
    message: str


def run(cmd: List[str], cwd: Optional[Path] = None) -> Tuple[int, str, str]:
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd) if cwd is not None else None,
    )
    return p.returncode, p.stdout, p.stderr


def setup_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("retag")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


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
    cmd = ["exiftool", "-json", "-charset", "filename=utf8", "-charset", "iptc=utf8"]
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
    cmd = ["exiftool", "-charset", "filename=utf8", "-charset", "iptc=utf8", "-overwrite_original", "-P"]
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


def resolve_rel_path(file_path: Path, photos_root: Path) -> str:
    try:
        return file_path.resolve().relative_to(photos_root.resolve()).as_posix()
    except ValueError as exc:
        raise RuntimeError(
            f"File is outside photos root; cannot build indexer rel_path: {file_path}"
        ) from exc


def reindex_single_file(
    file_path: Path,
    photos_root: Path,
    sqlite_db: Path,
    indexer_root: Path,
    indexer_python: str,
    indexer_config: Path,
    verbose: bool,
    logger: logging.Logger,
) -> str:
    rel_path = resolve_rel_path(file_path, photos_root)
    cmd = [
        indexer_python,
        "-m", "app",
        "--cli",
        "--db", str(sqlite_db),
        "--root", str(photos_root),
        "--config", str(indexer_config),
        "--refresh-file", rel_path,
        "--json",
        "--no-progress",
    ]

    if verbose:
        logger.info("[INDEXER] %s", " ".join(shlex.quote(x) for x in cmd))

    rc, out, err = run(cmd, cwd=indexer_root)
    if rc != 0:
        message = err.strip() or out.strip() or "Indexer single-file refresh failed"
        raise RuntimeError(message)
    return rel_path


def print_summary(
    root: Path,
    map_path: Path,
    exts: set[str],
    dry_run: bool,
    no_video: bool,
    case_insensitive: bool,
    log_path: Path,
    report_path: Path,
    reindex_enabled: bool,
    photos_root: Optional[Path],
    sqlite_db: Optional[Path],
    indexer_root: Optional[Path],
    indexer_config: Optional[Path],
    indexer_python: Optional[str],
) -> None:
    print("=== Retag operation summary ===")
    print(f"Root:        {root}")
    print(f"Mapping CSV: {map_path}")
    print(f"Extensions:  {' '.join(sorted(exts))}")
    print(f"Mode:        {'DRY-RUN' if dry_run else 'APPLY'}")
    print(f"Video files: {'excluded' if no_video else 'included'}")
    print(f"Case match:  {'insensitive' if case_insensitive else 'exact'}")
    print(f"Log file:    {log_path}")
    print(f"Report CSV:  {report_path}")
    print(f"Reindex:     {'enabled' if reindex_enabled else 'disabled'}")
    if reindex_enabled:
        print(f"Photos root: {photos_root}")
        print(f"SQLite DB:   {sqlite_db}")
        print(f"Indexer root:{indexer_root}")
        print(f"Indexer cfg: {indexer_config}")
        print(f"Indexer py:  {indexer_python}")
    print("================================")


def validate_reindex_args(
    dry_run: bool,
    root: Path,
    photos_root_raw: str,
    sqlite_db_raw: str,
    indexer_root_raw: str,
    indexer_config_raw: str,
    indexer_python: str,
    no_reindex: bool,
) -> Tuple[bool, Optional[Path], Optional[Path], Optional[Path], Optional[Path], Optional[str]]:
    if dry_run or no_reindex:
        return False, None, None, None, None, None

    photos_root = Path(photos_root_raw or str(root)).expanduser().resolve()
    sqlite_db = Path(sqlite_db_raw).expanduser().resolve() if sqlite_db_raw else None
    indexer_root = Path(indexer_root_raw).expanduser().resolve() if indexer_root_raw else None
    indexer_config = Path(indexer_config_raw).expanduser().resolve() if indexer_config_raw else None
    python_bin = indexer_python.strip() or "python3"

    if not photos_root.is_dir():
        raise ValueError(f"photos root not found: {photos_root}")
    if sqlite_db is None or not sqlite_db.is_file():
        raise ValueError("sqlite DB is required for reindex (--sqlite-db or WA_SQLITE_DB)")
    if indexer_root is None or not indexer_root.is_dir():
        raise ValueError("indexer root is required for reindex (--indexer-root or WA_INDEXER2_ROOT)")
    if indexer_config is None:
        indexer_config = indexer_root / "config.yaml"
    if not indexer_config.is_file():
        raise ValueError(f"indexer config not found: {indexer_config}")

    return True, photos_root, sqlite_db, indexer_root, indexer_config, python_bin


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
    ap.add_argument("--log", default="retag.log", help="Log file output path")
    ap.add_argument("--photos-root", default=os.environ.get("WA_PHOTOS_ROOT", ""), help="WebAlbum/indexer photos root used to build rel_path for reindex")
    ap.add_argument("--sqlite-db", default=os.environ.get("WA_SQLITE_DB", ""), help="SQLite DB path for indexer refresh")
    ap.add_argument("--indexer-root", default=os.environ.get("WA_INDEXER2_ROOT", ""), help="indexer2 project root")
    ap.add_argument("--indexer-config", default=os.environ.get("WA_INDEXER2_CONFIG", ""), help="indexer2 config.yaml path")
    ap.add_argument("--indexer-python", default=os.environ.get("WA_INDEXER2_PYTHON", "python3"), help="Python executable for indexer2")
    ap.add_argument("--no-reindex", action="store_true", help="Skip indexer refresh after successful file updates")
    ap.add_argument("--verbose", action="store_true", help="Verbose logging (shows exiftool commands)")
    args = ap.parse_args()

    if args.apply:
        dry_run = False
    else:
        dry_run = True  # default safe mode

    root = Path(args.root).expanduser().resolve()
    map_path = Path(args.map).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve()
    log_path = Path(args.log).expanduser().resolve()

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

    logger = setup_logger(log_path)

    try:
        (
            reindex_enabled,
            photos_root,
            sqlite_db,
            indexer_root,
            indexer_config,
            indexer_python,
        ) = validate_reindex_args(
            dry_run,
            root,
            args.photos_root,
            args.sqlite_db,
            args.indexer_root,
            args.indexer_config,
            args.indexer_python,
            args.no_reindex,
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    mapping = load_map(map_path, args.case_insensitive)

    exts = set(DEFAULT_EXTS)
    for e in args.ext:
        exts.add(e.lower() if e.startswith(".") else f".{e.lower()}")
    if args.no_video:
        for v in (".mp4", ".mov", ".m4v"):
            exts.discard(v)

    print_summary(
        root,
        map_path,
        exts,
        dry_run,
        args.no_video,
        args.case_insensitive,
        log_path,
        report_path,
        reindex_enabled,
        photos_root,
        sqlite_db,
        indexer_root,
        indexer_config,
        indexer_python,
    )
    if not confirm_proceed():
        print("Aborted by user.")
        sys.exit(0)

    logger.info("Start root=%s map=%s mode=%s reindex=%s", root, map_path, "dry-run" if dry_run else "apply", reindex_enabled)

    files = list_media(root, exts)
    print(f"Files matched: {len(files)}")
    print(f"Sidecar: {'ALLOWED' if args.allow_sidecar else 'DISALLOWED'}")
    print(f"Report: {report_path}")
    print(f"Log: {log_path}")

    changes: List[Change] = []
    results: List[FileResult] = []
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
                if dry_run:
                    results.append(
                        FileResult(
                            path=str(fp),
                            rel_path="",
                            status="planned",
                            changed_fields=len(pending_changes),
                            reindexed=False,
                            message="Would update metadata",
                        )
                    )
                    logger.info("planned path=%s fields=%d", fp, len(pending_changes))
                else:
                    rel_path = ""
                    try:
                        if reindex_enabled:
                            rel_path = reindex_single_file(
                                fp,
                                photos_root,
                                sqlite_db,
                                indexer_root,
                                indexer_python,
                                indexer_config,
                                args.verbose,
                                logger,
                            )
                        results.append(
                            FileResult(
                                path=str(fp),
                                rel_path=rel_path,
                                status="changed",
                                changed_fields=len(pending_changes),
                                reindexed=reindex_enabled,
                                message="Updated metadata" + (" and refreshed SQLite index" if reindex_enabled else ""),
                            )
                        )
                        logger.info(
                            "changed path=%s fields=%d reindexed=%s rel_path=%s",
                            fp,
                            len(pending_changes),
                            reindex_enabled,
                            rel_path or "-",
                        )
                    except Exception as reindex_error:
                        errors.append((str(fp), str(reindex_error)))
                        results.append(
                            FileResult(
                                path=str(fp),
                                rel_path=rel_path,
                                status="changed_reindex_failed",
                                changed_fields=len(pending_changes),
                                reindexed=False,
                                message=f"Metadata updated but reindex failed: {reindex_error}",
                            )
                        )
                        logger.error("reindex_failed path=%s message=%s", fp, reindex_error)
                if args.verbose:
                    print(f"[OK] {fp}")
            else:
                results.append(
                    FileResult(
                        path=str(fp),
                        rel_path="",
                        status="unchanged",
                        changed_fields=0,
                        reindexed=False,
                        message="No matching tags",
                    )
                )
        except Exception as e:
            errors.append((str(fp), str(e)))
            results.append(
                FileResult(
                    path=str(fp),
                    rel_path="",
                    status="error",
                    changed_fields=0,
                    reindexed=False,
                    message=str(e),
                )
            )
            logger.error("error path=%s message=%s", fp, e)

    # Write report
    with report_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["path", "rel_path", "status", "changed_fields", "reindexed", "message", "field", "before_json", "after_json"])
        changes_by_path: Dict[str, List[Change]] = {}
        for c in changes:
            changes_by_path.setdefault(c.path, []).append(c)
        for result in results:
            file_changes = changes_by_path.get(result.path, [])
            if not file_changes:
                w.writerow([
                    result.path,
                    result.rel_path,
                    result.status,
                    result.changed_fields,
                    1 if result.reindexed else 0,
                    result.message,
                    "",
                    "",
                    "",
                ])
                continue
            for idx, c in enumerate(file_changes):
                w.writerow([
                    result.path,
                    result.rel_path,
                    result.status if idx == 0 else "",
                    result.changed_fields if idx == 0 else "",
                    (1 if result.reindexed else 0) if idx == 0 else "",
                    result.message if idx == 0 else "",
                    c.field,
                    json.dumps(c.before, ensure_ascii=False),
                    json.dumps(c.after, ensure_ascii=False),
                ])

    changed_files = sum(1 for r in results if r.status in {"changed", "changed_reindex_failed"})
    planned_files = sum(1 for r in results if r.status == "planned")
    unchanged_files = sum(1 for r in results if r.status == "unchanged")
    logger.info(
        "Done files=%d changed_files=%d planned_files=%d unchanged_files=%d field_updates=%d errors=%d",
        len(results),
        changed_files,
        planned_files,
        unchanged_files,
        len(changes),
        len(errors),
    )
    print(f"Changed files: {changed_files}")
    if planned_files:
        print(f"Planned files: {planned_files}")
    print(f"Unchanged files: {unchanged_files}")
    print(f"Changes: {len(changes)} field-updates")
    print(f"Detailed report: {report_path}")
    print(f"Detailed log: {log_path}")
    if errors:
        print(f"Errors: {len(errors)} (first 10 shown)", file=sys.stderr)
        for p, e in errors[:10]:
            print(f"  - {p}: {e}", file=sys.stderr)
        # keep nonzero exit so you notice
        sys.exit(1)


if __name__ == "__main__":
    main()
