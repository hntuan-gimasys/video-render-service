# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI backend that takes a Google Drive folder of videos + subtitles (`.srt`)
+ background music, cuts/merges/burns subtitles with FFmpeg, and returns a single
MP4. Deployed on Cloud Run as a **single instance, no database, no Cloud Storage**
— job state lives in the process's RAM and `/tmp` is a tmpfs (RAM disk).

**`docs/SPEC.md` is the single source of truth** for API contract, options
schema, the FFmpeg pipeline, the error-code table, and env vars. Read it before
changing behavior in `app/`. Do not edit `docs/SPEC.md` — if it looks wrong,
say so instead of "fixing" it. Where code intentionally deviates from SPEC,
there's a comment at that spot explaining why, backed by a real ffmpeg
measurement (grep for these before assuming SPEC is authoritative on a given
line).

The repo's prose (README, SPEC, comments, commit messages) is written in
Vietnamese; identifiers are English. Match whichever language a given file
already uses when editing it.

## Commands

```bash
make install       # venv + pip install -r requirements.txt
make dev           # uvicorn --reload on :8080 (uses .venv)
make test          # pytest -q
make build         # docker build -t video-render-service:local .
make run-docker    # run the image locally with a mounted service-account key
make smoke         # scripts/smoke_test.sh against a running instance
make deploy        # scripts/deploy.sh — deploys to Cloud Run (see warnings below)
```

Run a single test: `.venv/bin/pytest tests/test_ffmpeg_command.py -q` (or
plain `pytest -q -k name` if the venv is already active). `pytest.ini` sets
`asyncio_mode = auto`, so `async def test_...` works without decorators.

Local setup: `cp .env.example .env` and set `API_KEY` (required — `Settings`
fails fast if empty). Docker is closer to prod because it has ffmpeg + fonts
baked in; native `make dev` needs ffmpeg on PATH.

## Architecture

### Request flow

```
POST /api/jobs → validate synchronously (Drive URL shape, options JSON) →
  save uploaded/inline fields to /tmp/jobs/<job_id>/ → 202 with job_id
  → asyncio.create_task(run_job) runs the actual pipeline in the background

GET  /api/jobs/{id}            → status + progress (polled by client)
GET  /api/jobs/{id}/download   → StreamingResponse, HTTP Range support, NO auth
DELETE /api/jobs/{id}          → kill ffmpeg, wipe workspace
```

`run_job` (`app/jobs.py`) pipeline: `prepare_inputs` (download from Drive,
merge clips) → `probe_stage` (ffprobe) → `check_tmp_space` → acquire the
global `asyncio.Semaphore(MAX_CONCURRENT_JOBS)` → build subtitle plan → build
ffmpeg argv → run it, parsing `-progress pipe:1` → finalize (probe output,
optional Drive upload) → mark succeeded. Every exception path funnels through
`AppError` subclasses so `job.error.code` always matches the SPEC §3.6 table;
input files are always deleted in `finally` (`/tmp` is RAM) regardless of
success/failure/cancel.

### Module split — "pure builder" vs "I/O runner" is the recurring pattern

Files were deliberately kept under ~400 lines by splitting **pure/testable
logic** (no I/O, no subprocess, easy to snapshot-test) from the **async I/O
shell** that calls it. When adding a feature, put the decision-making code in
the pure half and keep the I/O half a thin wrapper:

| Pure (no I/O, unit-testable) | I/O / async counterpart |
|---|---|
| `ffmpeg_cmd.py` — builds ffmpeg argv as `list[str]` | `ffmpeg_runner.py` — runs it via `asyncio.create_subprocess_exec`, parses progress |
| `probe_data.py` — `ProbeResult` dataclass + JSON parsing | `ffmpeg_runner.py` — actually invokes `ffprobe` |
| `clips.py` / `clip_syntax.py` / `clip_transitions.py` — decide cut points, crossfade filters | `merge.py` — runs the merge ffmpeg command |
| `job_store.py` — `Job`/`JobStore` data model, janitor/cancel/shutdown lifecycle | `jobs.py` — the actual render pipeline (`run_job`) |
| `models.py` — input schema (`RenderOptions` and friends) | `job_models.py` — output schema (job status/response) |
| `subtitle_style.py` / `subtitle_wrap.py` / `ass_doc.py` / `ass_effects.py` — compute pixel sizes, word-wrap, build `.ass` text | `overlay.py` / `subtitles.py` — orchestrate which file goes where |
| `intake.py` — turn multipart form fields into trusted values | `main.py` — routes call into it |
| `drive_folder.py` — list/match files in a Drive folder | `drive.py` — actual download/upload via googleapiclient |

`utils.py` is the foundation module (exceptions, JSON logging, file helpers)
and deliberately imports nothing else in `app/` to avoid import cycles.

### Key invariants worth knowing before touching the pipeline

- **Never use `subprocess.run` or `shell=True`.** All process execution goes
  through `asyncio.create_subprocess_exec` with argv as a list — user input
  (paths, filenames) must never be interpolated into a shell string.
- **Coordinate system is pixels, not ASS's default virtual canvas.** For
  `.srt` input the service builds its own `.ass` with `PlayResX/Y` set to the
  actual output frame size (`ass_doc.py`), so `font_size`/`margin_*`/`outline`
  in the API map 1:1 to pixels. User-supplied `.ass` files keep their own
  `PlayResY` and get `force_style` overrides instead — see SPEC §4.1 before
  changing subtitle sizing.
- **Font size follows video width, not height** (`font_px = width × ratio`) —
  using height was a real bug (tall 9:16 video got near-double-size text vs.
  a same-width landscape video).
- **ASS colors are `&HAABBGGRR&`** (reversed byte order from typical hex) —
  `subtitles.py::hex_to_ass_color`.
- **Fast path**: no subtitle burn + no resolution/fps/codec change →
  `-c:v copy`, only audio is re-encoded (10–50x faster). Don't accidentally
  force a re-encode when this path should apply.
- **`amix` needs `normalize=0`** or the original audio gets halved in volume;
  volume is controlled explicitly via the `volume` filter instead.
- **Video with no audio track** needs a synthetic `anullsrc` input or `amix`
  fails outright — always check `ProbeResult.has_audio`.
- **`out_time_ms` from `-progress` is actually microseconds**, not
  milliseconds, despite the name.
- **`music.original_volume=None` vs `0` are different**: `None` means "mute
  original audio only if music was mixed in"; `0` means "always mute," which
  would wrongly silence videos with no music at all.
- Clip merging uses **one ffmpeg filter_complex, not the concat demuxer**
  (sources can differ in codec/resolution/fps) and always
  `scale…force_original_aspect_ratio=decrease` + `pad` — source video is never
  stretched.
- Crossfade transition duration is clamped to ≤50% of the shorter adjacent
  clip so it can't eat most of a short clip.

### Config & deployment constraints (`app/config.py`, `docs/SPEC.md` §1, §7)

Env vars are read via `pydantic-settings` in `Settings` (`app/config.py`);
names match the table in SPEC §7 exactly (`API_KEY`, `WORK_DIR`,
`MAX_CONCURRENT_JOBS`, `JOB_TTL_SECONDS`, etc.). These map directly to hard
Cloud Run constraints — don't relax them casually:

- `--max-instances=1` is required: job state is an in-process RAM dict.
- `--no-cpu-throttling` is required: ffmpeg keeps running in the background
  worker task after the HTTP request has already returned.
- `/tmp` is RAM (tmpfs) — file sizes count against the memory limit, not disk.
- Cloud Run hard-caps request bodies at 32 MiB over HTTP/1 (measured: 31.5 MiB
  passes, 32.0 MiB gets `413` from the Google frontend, before the app even
  sees it) — this is why large/multi-video input goes through a Google Drive
  folder link instead of direct upload, and why `--use-http2` must never be
  set (uvicorn only speaks HTTP/1.1; the flag turns every request into a 502).
- The `/api/jobs/{id}/download` endpoint intentionally has **no API-key auth**
  (needs to work from a plain `<video>` tag/browser); its only protection is
  an unguessable `job_id` plus TTL expiry — treat the download link like a
  one-time password, not a public asset.

`scripts/deploy.sh` has a documented Git Bash/MSYS path-mangling gotcha for
Windows — run it from WSL, real PowerShell/cmd, or Linux/macOS, not Git Bash.

### Testing conventions

- `build_ffmpeg_command()` and friends are tested by comparing generated argv
  against expected values — no real ffmpeg process involved.
- `tests/test_integration*.py` are the exception: skipped via
  `@pytest.mark.skipif(shutil.which("ffmpeg") is None)` and generate sample
  media with `ffmpeg -f lavfi` to run the real pipeline end-to-end.
- Shared fixtures (`default_options`, `tmp_workspace`, `fake_probe_result`,
  `settings`) live in `tests/conftest.py`; API-layer fixtures live in
  `tests/api/conftest.py`.
