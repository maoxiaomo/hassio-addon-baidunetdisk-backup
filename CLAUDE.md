# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository purpose

A Home Assistant Supervisor Add-on that uploads HA `.tar` backups in `/backup` to Baidu Netdisk via the official OAuth 2.0 xpan API (AList-compatible client credentials). The entire add-on lives under [baidu-netdisk-backup/](baidu-netdisk-backup/); the repo root only carries Home Assistant store metadata ([repository.yaml](repository.yaml), [README.md](README.md)).

## Build & run

There is no test suite, lint config, or local build script. The add-on is shipped as a Docker image built by Home Assistant Supervisor from [baidu-netdisk-backup/Dockerfile](baidu-netdisk-backup/Dockerfile) (Alpine + `python3` + `py3-requests`, no `pip` / no `requirements.txt`).

To smoke-test locally without HA:

```bash
cd baidu-netdisk-backup
REFRESH_TOKEN=122.xxxxxxx python3 main.py    # reads token from env when /data/options.json is absent
```

Runtime expects two paths that only exist inside HA:
- `/data/options.json` — add-on config (mirrors [baidu-netdisk-backup/config.yaml](baidu-netdisk-backup/config.yaml) schema)
- `/backup` — host-mounted directory of `.tar` files to upload

Bump `version:` in both [baidu-netdisk-backup/config.yaml](baidu-netdisk-backup/config.yaml) and the banner in [baidu-netdisk-backup/main.py](baidu-netdisk-backup/main.py) when releasing.

## Architecture

Four single-purpose modules, all flat under [baidu-netdisk-backup/](baidu-netdisk-backup/):

- **[main.py](baidu-netdisk-backup/main.py)** — entry point. Loads config, runs an initial sync, then enters `schedule_loop` which sleeps until the hour parsed from `schedule` (cron string — only the hour field is honored, not full cron semantics) and re-runs `run_sync_cycle` daily.
- **[client.py](baidu-netdisk-backup/client.py)** — `BaiduClient`. OAuth 2.0 token lifecycle (cached to `/data/baidu_token.json`), sliced upload (precreate → superfile2 chunks → create-merge), `_batch_filemanager` for delete/move (the only batched op path; both `delete_remote_files` and `move_remote_files` route through it), `list_remote_files` with pagination, `create_remote_dir`. Also owns an upload-dedup cache at `/data/upload_cache.json` keyed by `name:size:mtime` so re-runs skip already-uploaded files locally.
- **[sync.py](baidu-netdisk-backup/sync.py)** — `sync_all_backups`: enumerate `/backup/*.tar`, oldest first, upload each through `BaiduClient`.
- **[retention.py](baidu-netdisk-backup/retention.py)** — two retention modes selected by `retention.use_folders`:
  - **Flat mode** (`cleanup_remote_backups`): all backups live in `upload_path/`. Keep newest N per day/week/month bucket via `_compute_retention_keep_paths`, delete the rest.
  - **Folder mode** (`retention_folder_mode`): backups uploaded to `upload_path/每日/`, then *promoted* (moved) into `每周/` and `每月/` according to bucket policy, then per-folder counts enforced. Phase ordering matters — monthly promotion runs before weekly so the daily cache is consumed in priority order. `migrate_old_dirs` is a one-shot migration from the historical English `daily/weekly/monthly` directory names; runs every cycle but no-ops once empty.
- After a folder-mode cycle, `generate_manifest` writes a summary `清单文件.txt` and uploads it to `upload_path/`.

### Cross-cutting things to know

- **Backup filename parsing.** Retention bucketing relies on `_BACKUP_TS_RE` in [retention.py:17](baidu-netdisk-backup/retention.py#L17) matching HA's `_YYYY-MM-DD_HH.MM_` pattern. Files without that pattern fall back to `server_mtime`; if neither works the file is silently ignored by retention.
- **Config shape duality.** [main.py:60-72](baidu-netdisk-backup/main.py#L60-L72) accepts retention either nested (`retention: {daily, weekly, monthly, use_folders}`) or flat (`retention_daily`, `retention_weekly`, `retention_monthly`, `retention_use_folders`). [config.yaml](baidu-netdisk-backup/config.yaml) declares both shapes in `schema:`. Keep both code paths working when changing config.
- **Directory names are Chinese (`每日 / 每周 / 每月`)** in folder mode. They are hardcoded in `retention_folder_mode` and `generate_manifest`. `migrate_old_dirs` exists specifically to move users off the prior English names — do not remove it.
- **OAuth credentials are AList's public client.** `CLIENT_ID` / `CLIENT_SECRET` / `REDIRECT_URI` in [client.py:26-28](baidu-netdisk-backup/client.py#L26-L28) are intentionally the public AList credentials; users obtain a `refresh_token` via the AList callback URL documented in README. Do not treat these as secrets.
- **Comments reference "Issue N" markers** (e.g. "Issue 12: skip already-uploaded files"). These refer to a prior code-review pass (see commit `7e2aed9`), not an issue tracker.
