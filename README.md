# retag

Bulk tag rename/normalization tool for WebAlbum media files.

## Status

This repository is now deprecated.

`retag` has been merged into [`indexer2`](/Users/bajanp/Projects/indexer2/README.md) as a maintenance CLI tool. The reason for that merge is that `retag` and `indexer2` are operationally interdependent around the same SQLite database: `retag` updates file metadata, and `indexer2` refreshes the SQLite index to keep it in sync.

For ongoing use and future changes, use the `retag` maintenance CLI inside `indexer2`.

`retag_media.py` reads a two-column CSV mapping file:

- column 1: current tag in media metadata
- column 2: replacement tag

It updates media metadata with `exiftool` and, when run in apply mode, can refresh the WebAlbum/indexer SQLite DB after each successfully changed file.

MariaDB is not modified by this tool.

## What It Updates

The script rewrites these metadata fields:

- `XMP:Subject`
- `IPTC:Keywords`
- `XMP-lr:HierarchicalSubject`

For hierarchical tags it only replaces the leaf name, preserving the prefix path, for example:

- `People|Veronika` -> `People|Baján Veronika (Veronika)`

## Requirements

- `exiftool` available on `PATH`
- `python3`
- `indexer2` checkout available for SQLite refresh
- correct `indexer2` Python interpreter

In this environment the working `indexer2` Python was:

```bash
/Users/bajanp/Projects/indexer2/.venv/bin/python3
```

## Files

- [`retag_media.py`](/Users/bajanp/Projects/retag/retag_media.py)
- [`tag-map.csv`](/Users/bajanp/Projects/retag/tag-map.csv)
- SQLite test/development DB example: [`sqlite/images-2020-2021.db`](/Users/bajanp/Projects/retag/sqlite/images-2020-2021.db)

## Dry Run

Dry-run scans files, shows planned changes, and writes a report/log without modifying files.

```bash
python3 /Users/bajanp/Projects/retag/retag_media.py \
  --root "/data/photos/2020/2020-05-30 iphone+mac iCloud download/jpg" \
  --map /Users/bajanp/Projects/retag/tag-map.csv \
  --report /tmp/retag-dry-run-report.csv \
  --log /tmp/retag-dry-run.log \
  --dry-run \
  --verbose
```

## Apply With SQLite Refresh

This is the normal WebAlbum-compatible mode. After each successfully changed file:

1. metadata is rewritten
2. `indexer2 --refresh-file` is run for that file

```bash
python3 /Users/bajanp/Projects/retag/retag_media.py \
  --root "/data/photos" \
  --map /Users/bajanp/Projects/retag/tag-map.csv \
  --report /tmp/retag-report.csv \
  --log /tmp/retag.log \
  --apply \
  --photos-root /data/photos \
  --sqlite-db /Users/bajanp/Projects/retag/sqlite/images-2020-2021.db \
  --indexer-root /Users/bajanp/Projects/indexer2 \
  --indexer-config /Users/bajanp/Projects/indexer2/config.yaml \
  --indexer-python /Users/bajanp/Projects/indexer2/.venv/bin/python3
```

## Small Live Test Example

One-row mapping file:

```csv
old_tag,new_tag
"Veronika","Baján Veronika (Veronika)"
```

Example command:

```bash
python3 /Users/bajanp/Projects/retag/retag_media.py \
  --root "/data/photos/2020/2020-05-30 iphone+mac iCloud download/jpg" \
  --map /tmp/retag-veronika.csv \
  --report /tmp/retag-live-test-report.csv \
  --log /tmp/retag-live-test.log \
  --apply \
  --photos-root /data/photos \
  --sqlite-db /Users/bajanp/Projects/retag/sqlite/images-2020-2021.db \
  --indexer-root /Users/bajanp/Projects/indexer2 \
  --indexer-config /Users/bajanp/Projects/indexer2/config.yaml \
  --indexer-python /Users/bajanp/Projects/indexer2/.venv/bin/python3 \
  --verbose
```

## Output Files

The CSV report contains:

- `path`
- `rel_path`
- `status`
- `changed_fields`
- `reindexed`
- `message`
- `field`
- `before_json`
- `after_json`

Typical statuses:

- `unchanged`
- `planned`
- `changed`
- `changed_reindex_failed`
- `error`

The log file records:

- run start
- per-file planned/changed/error events
- reindex commands when `--verbose` is used
- final summary

## Notes

- Use `--no-reindex` only if you explicitly want metadata changed without refreshing SQLite.
- RAW formats that may require sidecars are blocked by default unless `--allow-sidecar` is passed.
- If `indexer2` depends on a virtualenv, do not use plain `python3`; pass the virtualenv interpreter explicitly.
