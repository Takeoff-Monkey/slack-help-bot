"""Slack file I/O for the bot's tool-use path.

Three jobs:
  1. Download files a user attached to a message (reuse the authenticated url_private
     pattern proven in canvas_knowledge._download_canvas_text).
  2. Stage them where tools can reach them — a local temp dir now (TOOL_BACKEND=local),
     or a scratch S3 bucket later (TOOL_BACKEND=lambda). Files are referred to by opaque
     handles (file_1, file_2, …) so the model never sees real paths or S3 keys.
  3. Upload produced artifacts (xlsx, highlighted PDF, …) back into the thread.

Everything degrades gracefully: a download/upload failure is logged and skipped, never
raised into the agent loop.
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
            continue
        if size and size > MAX_FILE_BYTES:
            logger.warning("Attachment %r is %d bytes (> cap %d) — skipping", name, size, MAX_FILE_BYTES)
            continue
        try:
            data = _download(url, token)
        except Exception:
            logger.exception("Failed to download attachment %r", name)
            continue
        if len(data) > MAX_FILE_BYTES:
            logger.warning("Attachment %r exceeded cap after download — skipping", name)
            continue

        handle = f"file_{i}"
        if staging.backend == "lambda":
            if not staging.bucket:
                continue
            if s3 is None:
                import boto3
                s3 = boto3.client("s3")
            key = f"{staging.root}/input/{handle}-{name}"
            try:
                s3.put_object(Bucket=staging.bucket, Key=key, Body=data)
            except Exception:
                logger.exception("Failed to upload %r to s3://%s/%s", name, staging.bucket, key)
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
    if not staging.files:
        return ""
    lines = [f"- {f.handle}: {f.filename} ({f.mimetype or 'unknown type'})" for f in staging.files]
    return "Attached files (refer to these by handle in tool inputs):\n" + "\n".join(lines)


def _artifact_bytes(artifact: dict, staging: Staging, logger) -> bytes | None:
    ref = artifact.get("ref")
    if not ref:
        return None
    try:
        if staging.backend == "lambda":
            import boto3
            s3 = boto3.client("s3")
            obj = s3.get_object(Bucket=staging.bucket, Key=ref)
            return obj["Body"].read()
        with open(ref, "rb") as f:
            return f.read()
    except Exception:
        logger.exception("Failed to read artifact %r", ref)
        return None


def upload_artifacts(client, channel: str, thread_ts: str, artifacts: list[dict], staging: Staging, logger) -> int:
    """Upload each produced artifact into the thread via files_upload_v2 (needs
    files:write). Returns the count uploaded. Per-file failures are logged and skipped."""
    uploaded = 0
    for art in artifacts:
        data = _artifact_bytes(art, staging, logger)
        if data is None:
            continue
        try:
            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=data,
                filename=art.get("filename") or "result",
                title=art.get("title") or art.get("filename") or "result",
            )
            uploaded += 1
        except Exception:
            logger.exception("Failed to upload artifact %r to Slack", art.get("filename"))
    return uploaded


def cleanup(stagings: list[Staging], logger) -> None:
    """Best-effort removal of local temp dirs after a turn. S3 scratch objects are left to
    a lifecycle rule on the bucket. Never raises."""
    for st in stagings:
        if st and st.backend == "local" and st.root and os.path.isdir(st.root):
            try:
                shutil.rmtree(st.root, ignore_errors=True)
            except Exception:
                logger.exception("Failed to clean staging dir %s", st.root)
