"""Slack file I/O for the bot's tool-use path.

Three jobs:
  1. Download files a user attached to a message (reuse the authenticated url_private
     pattern proven in canvas_knowledge._download_canvas_text).
  2. Stage them where tools can reach them — a local temp dir now (TOOL_BACKEND=local),
     or a scratch S3 bucket later (TOOL_BACKEND=lambda). Files are referred to by opaque
     handles (file_1, file_2, …) so the model never sees real paths or S3 keys.
  3. Upload produced artifacts (xlsx, highlighted PDF, …) back into the thread.

Everything degrades gracefully: a download/upload failure is logged and skipped rather than
raised into the agent loop — but it is never swallowed. Staging records what it couldn't
fetch (Staging.skipped) and upload_artifacts returns what it couldn't deliver, so the bot
tells the user instead of quietly behaving as if the file had never existed.
"""

from __future__ import annotations

import os
import shutil
import ssl
import tempfile
import urllib.request
import uuid
from dataclasses import dataclass, field

import certifi

_SSL = ssl.create_default_context(cafile=certifi.where())


def _s3():
    """S3 client with real deadlines. botocore's defaults (60s reads, 5 attempts) can stall
    the agent loop for ~5 minutes per object with nothing to show the user for it."""
    import boto3
    from botocore.config import Config

    return boto3.client("s3", config=Config(connect_timeout=10, read_timeout=60,
                                            retries={"max_attempts": 3}))


# Per-attachment hard cap. Slack's own upload ceiling is ~1 GB but we never want the bot
# pulling a giant file into memory; 25 MB comfortably covers construction PDFs.
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", str(25 * 1024 * 1024)))
SCRATCH_S3_BUCKET = os.environ.get("SCRATCH_S3_BUCKET", "")


@dataclass
class StagedFile:
    handle: str          # "file_1" — what the model refers to
    filename: str        # original name, e.g. "site-plan.pdf"
    mimetype: str
    ref: str             # local absolute path (local) | S3 key (lambda)
    size: int


@dataclass
class Staging:
    backend: str                 # "local" | "lambda"
    root: str                    # local temp dir | S3 key prefix "runs/<uuid>"
    bucket: str | None           # scratch S3 bucket (lambda only)
    files: list[StagedFile] = field(default_factory=list)
    # Attachments we could not stage, as "name (why)". Surfaced to the model and the user —
    # dropping them silently made the bot behave as if nothing had been attached at all.
    skipped: list[str] = field(default_factory=list)
    # True when the files came from earlier in the thread rather than this message, so the
    # model knows to check it's working on the one the user actually meant.
    carried_forward: bool = False

    def by_handle(self) -> dict[str, StagedFile]:
        return {f.handle: f for f in self.files}


def _download(url: str, token: str) -> bytes:
    """Authenticated GET of a Slack url_private (needs files:read). Mirrors
    canvas_knowledge._download_canvas_text."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, context=_SSL, timeout=60) as r:
        return r.read()


def stage_attachments(event_files: list[dict], token: str, backend: str, logger) -> Staging:
    """Download each attachment and stage it for the configured backend. Returns a
    Staging whose .files carry handles the model can reference. Files that fail to
    download or exceed MAX_FILE_BYTES are skipped (logged)."""
    if backend == "lambda":
        if not SCRATCH_S3_BUCKET:
            logger.error("TOOL_BACKEND=lambda but SCRATCH_S3_BUCKET is unset — cannot stage files")
        staging = Staging(backend="lambda", root=f"runs/{uuid.uuid4().hex}", bucket=SCRATCH_S3_BUCKET or None)
        s3 = None
    else:
        staging = Staging(backend="local", root=tempfile.mkdtemp(prefix="tmbot-"), bucket=None)
        s3 = None

    for i, f in enumerate(event_files, start=1):
        url = f.get("url_private_download") or f.get("url_private")
        name = f.get("name") or f.get("title") or f"file_{i}"
        size = int(f.get("size") or 0)
        if not url:
            logger.warning("Attachment %r has no url_private — skipping", name)
            staging.skipped.append(f"{name} (Slack gave me no download link)")
            continue
        if size and size > MAX_FILE_BYTES:
            logger.warning("Attachment %r is %d bytes (> cap %d) — skipping", name, size, MAX_FILE_BYTES)
            staging.skipped.append(f"{name} (it's {size // (1024 * 1024)} MB, over my {MAX_FILE_BYTES // (1024 * 1024)} MB limit)")
            continue
        try:
            data = _download(url, token)
        except Exception:
            logger.exception("Failed to download attachment %r", name)
            staging.skipped.append(f"{name} (the download from Slack failed)")
            continue
        if len(data) > MAX_FILE_BYTES:
            logger.warning("Attachment %r exceeded cap after download — skipping", name)
            staging.skipped.append(f"{name} (over my {MAX_FILE_BYTES // (1024 * 1024)} MB limit)")
            continue

        handle = f"file_{i}"
        if staging.backend == "lambda":
            if not staging.bucket:
                staging.skipped.append(f"{name} (no scratch bucket configured)")
                continue
            if s3 is None:
                s3 = _s3()
            key = f"{staging.root}/input/{handle}-{name}"
            try:
                s3.put_object(Bucket=staging.bucket, Key=key, Body=data)
            except Exception:
                logger.exception("Failed to upload %r to s3://%s/%s", name, staging.bucket, key)
                staging.skipped.append(f"{name} (couldn't stage it for the tools)")
                continue
            ref = key
        else:
            ref = os.path.join(staging.root, f"{handle}-{name}")
            with open(ref, "wb") as out:
                out.write(data)

        staging.files.append(
            StagedFile(handle=handle, filename=name, mimetype=f.get("mimetype") or "", ref=ref, size=len(data))
        )
        logger.info("Staged attachment %s (%s, %d bytes) as %s", handle, name, len(data), ref)

    return staging


def attachments_for_prompt(staging: Staging) -> str:
    """A line injected into the user turn so the model knows what files exist and how to
    name them in tool inputs. Never leaks paths/keys."""
    parts = []
    if staging.files:
        lines = [f"- {f.handle}: {f.filename} ({f.mimetype or 'unknown type'})" for f in staging.files]
        header = "Attached files (refer to these by handle in tool inputs):"
        if staging.carried_forward:
            header = ("Files carried over from EARLIER in this thread (the user did not attach "
                      "anything to this message). If it isn't obvious this is the file they mean, "
                      "ask them first with `ask_user`:")
        parts.append(header + "\n" + "\n".join(lines))
    if staging.skipped:
        parts.append("I could NOT get hold of these attachments — tell the user, plainly, that "
                     "they didn't come through:\n" + "\n".join(f"- {s}" for s in staging.skipped))
    return "\n\n".join(parts)


def _artifact_bytes(artifact: dict, staging: Staging, logger) -> bytes | None:
    ref = artifact.get("ref")
    if not ref:
        # Every other skip in this file logs; this one used to drop the file in silence.
        logger.error("Artifact %r has no storage ref — cannot upload it", artifact.get("filename"))
        return None
    try:
        if staging.backend == "lambda":
            obj = _s3().get_object(Bucket=staging.bucket, Key=ref)
            return obj["Body"].read()
        with open(ref, "rb") as f:
            return f.read()
    except Exception:
        logger.exception("Failed to read artifact %r", ref)
        return None


def upload_artifacts(client, channel: str, thread_ts: str, artifacts: list[dict],
                     staging: Staging, logger, seen: set | None = None) -> list[dict]:
    """Upload each produced artifact into the thread via files_upload_v2 (needs files:write).

    Returns the ARTIFACTS it could not deliver — not just their names — because a file that
    fails here is deliberately left out of ``seen`` and so gets retried by the caller's final
    sweep. The caller reconciles the two by ref: only something still missing at the end is
    worth telling the user about. (It has to tell them about those: the bot's own reply
    cheerfully says "here's your spreadsheet" whether or not it arrived.) Pass a shared
    ``seen`` set to dedupe across incremental + final calls so a file is never posted twice."""
    failed: list[dict] = []
    for art in artifacts:
        ref = art.get("ref")
        name = art.get("filename") or "result"
        if seen is not None and ref in seen:
            continue
        data = _artifact_bytes(art, staging, logger)
        if data is None:
            failed.append(art)
            continue
        try:
            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=data,
                filename=name,
                title=art.get("title") or name,
            )
            if seen is not None and ref:
                seen.add(ref)
            logger.info("Uploaded artifact %r to thread", name)
        except Exception:
            logger.exception("Failed to upload artifact %r to Slack", name)
            failed.append(art)
    return failed


def _delete_s3_prefix(bucket: str, prefix: str) -> int:
    """Delete every object under bucket/prefix. Best-effort; returns count deleted."""
    s3 = _s3()
    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix.rstrip("/") + "/"):
        objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if not objs:
            continue
        s3.delete_objects(Bucket=bucket, Delete={"Objects": objs, "Quiet": True})
        deleted += len(objs)
    return deleted


def cleanup(stagings: list[Staging], logger) -> None:
    """Tear down a turn's scratch space. Local: rmtree the temp dir. Lambda: delete every S3
    object under the run prefix (input + all tool/sandbox outputs) so the bucket doesn't
    accumulate — the output is already delivered to Slack, so nothing needs to persist.
    Needs s3:ListBucket + s3:DeleteObject. Never raises."""
    for st in stagings:
        if not st:
            continue
        if st.backend == "local":
            if st.root and os.path.isdir(st.root):
                try:
                    shutil.rmtree(st.root, ignore_errors=True)
                except Exception:
                    logger.exception("Failed to clean staging dir %s", st.root)
        elif st.backend == "lambda" and st.bucket and st.root:
            try:
                n = _delete_s3_prefix(st.bucket, st.root)
                logger.info("Cleared %d S3 object(s) under s3://%s/%s/", n, st.bucket, st.root)
            except Exception:
                logger.exception("Failed to clear S3 prefix s3://%s/%s/ (a lifecycle rule "
                                 "is a good backstop)", st.bucket, st.root)


def write_trace(staging: Staging, text: str, name: str, logger) -> None:
    """Optionally persist a run trace for debugging. Writes to s3://bucket/logs/<name> under
    the lambda backend (a prefix the per-run cleanup does NOT touch). No-op for local (the
    trace is already on stdout / heroku logs). Gated by the caller (TRACE_TO_S3). Never raises."""
    if staging.backend != "lambda" or not staging.bucket:
        return
    try:
        _s3().put_object(
            Bucket=staging.bucket, Key=f"logs/{name}", Body=text.encode("utf-8"),
            ContentType="text/plain",
        )
        logger.info("Wrote run trace to s3://%s/logs/%s", staging.bucket, name)
    except Exception:
        logger.exception("Failed to write run trace to S3")
