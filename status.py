"""The bot's voice while it's working — one status message per turn, plus a watchdog that
refuses to let the bot go quiet.

Before this, a turn posted ":hourglass: _Thinking…_" and then said nothing at all until it
had a final answer. If anything in between stalled, or died on a path the one try/except
didn't cover, that placeholder was the last thing the user ever saw: the bot looked busy
forever while it was actually finished or dead.

A Reporter owns that message for the life of a turn:
  say()      a progress line — the model's own preamble, or the loop's ("Running X…")
  doing()    sets the current activity so the watchdog can narrate it with no model call
  snag()     something went wrong but we're carrying on — the user hears about it NOW
  finish()   the final answer; SEALS the message so nothing can overwrite it afterwards
  fail()     the turn died; an honest message instead of an eternal hourglass
  note()     a brand-new message in the thread, for problems found after the seal

The watchdog thread wakes every second and, if nothing has been said for `idle_seconds`
(30 by default), says where we're at and how long it's been. Every write goes through one
lock and re-checks the seal while holding it, so a watchdog tick can never land on top of
the final answer — the ordering that would otherwise turn "here's your file" back into
"still working…".
"""

from __future__ import annotations

import contextlib
import os
import threading
import time

# How long the bot may stay quiet before the watchdog speaks up. The user asked for 30s.
IDLE_SECONDS = int(os.environ.get("STATUS_IDLE_SECONDS", "30"))
# Past this, the watchdog stops saying "still working" and admits it's taking unusually long.
LONG_RUN_SECONDS = int(os.environ.get("STATUS_LONG_RUN_SECONDS", "180"))

THINKING_PLACEHOLDER = ":hourglass_flowing_sand: _Thinking…_"
WORKING = ":hourglass_flowing_sand:"
SNAG = ":warning:"
FAILED = ":x:"


def human(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


class Reporter:
    """Owns one turn's status message. Thread-safe: the agent loop, its tool worker threads
    and the watchdog all write through here.

    `update(text)` edits the turn's status message. `post(text)` (optional) starts a new
    message in the thread — used only after the status message has been sealed.
    """

    def __init__(self, update, logger, post=None, idle_seconds: int = IDLE_SECONDS):
        self._update = update
        self._post = post
        self._log = logger
        self._idle = idle_seconds

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._started = time.monotonic()
        self._last_spoke = self._started
        self._last_text: str | None = None
        self._sealed = False
        self._nudges = 0

        self._activity = "getting started"
        self._hint: str | None = None

    # ---- lifecycle -------------------------------------------------------------------

    def start(self) -> "Reporter":
        if self._thread is None:
            self._thread = threading.Thread(target=self._watch, name="status", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

    @property
    def sealed(self) -> bool:
        with self._lock:
            return self._sealed

    def elapsed(self) -> float:
        return time.monotonic() - self._started

    # ---- saying things ---------------------------------------------------------------

    def __call__(self, msg: str) -> None:
        """So a Reporter can be passed anywhere the old plain `progress(msg)` callback was."""
        self.say(msg)

    def say(self, msg: str, activity: str | None = None) -> None:
        """A progress line the user sees now. Also resets the watchdog's idle clock."""
        if activity:
            self.doing(activity)
        self._write(f"{WORKING} {msg}")

    def doing(self, activity: str, hint: str | None = None) -> None:
        """Record what we're busy with. Writes nothing — this is what the watchdog narrates
        if the turn then goes quiet, so it should read as a phrase: "waiting for the code
        sandbox to boot", "reading plans.pdf"."""
        with self._lock:
            self._activity = activity
            self._hint = hint

    def snag(self, msg: str) -> None:
        """Hit a problem but still trying. The user hears about obstacles as they happen
        instead of finding out at the end (or never)."""
        self._write(f"{SNAG} {msg}")

    def finish(self, text: str) -> bool:
        """Write the final answer and seal the message. After this nothing else can edit it.
        Returns False if the write itself failed (the caller should post a fresh message)."""
        with self._lock:
            self._stop.set()
            self._sealed = True
            return self._raw(text)

    def fail(self, msg: str) -> None:
        """The turn is over and it did not work. Never let this be swallowed — an unexplained
        stall is exactly what the user complained about."""
        self.finish(f"{FAILED} {msg}")

    def close(self) -> None:
        """Stop the watchdog and, if nothing ever sealed this message, seal it with an honest
        admission. Idempotent — this is what makes an early `return` as safe as a raise."""
        self.stop()
        if not self.sealed:
            self._log.warning("status: turn ended without a final message; sealing a fallback")
            self.fail("I stopped without finishing and without an error to explain why — that's "
                      "a fault on my side, not something you did. Nothing is still running.")

    def note(self, text: str) -> None:
        """A new message in the thread. For things discovered after the answer was sealed
        (e.g. a file failed to upload), which would otherwise vanish silently."""
        if not self._post:
            return
        try:
            self._post(text)
        except Exception:
            self._log.exception("status: could not post follow-up note")

    # ---- internals -------------------------------------------------------------------

    def _write(self, text: str) -> bool:
        with self._lock:
            if self._sealed:
                return False
            return self._raw(text)

    def _raw(self, text: str) -> bool:
        """Caller holds the lock. Skips no-op edits (Slack rate limits are per-workspace)."""
        self._last_spoke = time.monotonic()
        if text == self._last_text:
            return True
        try:
            self._update(text)
            self._last_text = text
            return True
        except Exception:
            self._log.exception("status: could not update the turn's message")
            return False

    def _line(self) -> str:
        with self._lock:
            activity, hint, nudges = self._activity, self._hint, self._nudges
        took = human(self.elapsed())
        if self.elapsed() >= LONG_RUN_SECONDS:
            return (f"{WORKING} This is taking longer than usual — still {activity} ({took}). "
                    f"Say _stop_ if you'd rather I drop it.")
        line = f"{WORKING} Still on it — {activity} ({took})."
        if hint and nudges <= 1:
            line += f" {hint}"
        return line

    def _watch(self) -> None:
        while not self._stop.wait(1.0):
            with self._lock:
                if self._sealed:
                    return
                if time.monotonic() - self._last_spoke < self._idle:
                    continue
                self._nudges += 1
                self._raw(self._line())


@contextlib.contextmanager
def turn(channel: str, thread_ts: str, say, client, logger):
    """One turn = one message = one promise, from the first line to the last.

    The placeholder post lives INSIDE the guard, so there is no window before the Reporter
    exists in which a Slack hiccup can kill the turn and leave the user with nothing at all.
    Every exit — return, raise, even a BaseException — goes through close(), which seals the
    message. That's the point: the guarantee is positional, not something each new call site
    has to remember.
    """
    msg_ts = {"ts": None}
    try:
        posted = say(text=THINKING_PLACEHOLDER, thread_ts=thread_ts)
        msg_ts["ts"] = (posted or {}).get("ts")
    except Exception:
        # Rate limit, not_in_channel, a Slack 5xx. Not fatal: the first status line below
        # becomes a new message instead, and the turn carries on.
        logger.exception("status: could not post the placeholder")

    def update(text):
        if msg_ts["ts"]:
            client.chat_update(channel=channel, ts=msg_ts["ts"], text=text)
        else:
            msg_ts["ts"] = (say(text=text, thread_ts=thread_ts) or {}).get("ts")

    reporter = Reporter(update, logger, post=lambda t: say(text=t, thread_ts=thread_ts))
    reporter.start()
    try:
        yield reporter
    except BaseException as err:
        # Swallowed deliberately: Bolt's default listener error handler only writes to the log,
        # so re-raising here is exactly the silence this whole change exists to remove.
        logger.exception("turn failed")
        reporter.fail(
            f"Something went wrong on my side and I've stopped: `{type(err).__name__}: {err}`. "
            f"Nothing is still running in the background — ask me again and I'll retry."
        )
    finally:
        reporter.close()
