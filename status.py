"""The bot's voice while it's working — a running commentary in the thread, plus a watchdog
that refuses to let the bot go quiet.

Originally a turn posted ":hourglass: _Thinking…_" and then said nothing at all until it had
a final answer. That got fixed by narrating progress into that same placeholder — but editing
one message means every new thought erases the last one. The user watched a single line
flicker between "reading the plans" and "running the extractor" with no way to see what the
bot had actually done, and a snag reported mid-turn was gone the moment the next line landed.

So a turn now reads as a transcript. There are two kinds of line:

  permanent  a new thought — say(), snag(), finish(), fail(). Each one is its OWN message, so
             it stays put and the thread reads back as a record of what happened.
  transient  scaffolding that restates the thought we're already on — the "Thinking…"
             placeholder, the watchdog's "still on it (42s)" ticker, waiting() for "the
             sandbox is still booting". These overwrite the previous transient line and are
             themselves overwritten by the next real thought, so a ticker never piles up and
             never outlives what it was ticking about.

That's what makes the first real line replace "Thinking…" (the placeholder is transient) while
the second one starts a new message.

A Reporter owns that commentary for the life of a turn:
  say()      a new thought — the model's own preamble, or the loop's ("Running X…")
  waiting()  we're still on the same thought, just still waiting — replaces, doesn't stack
  doing()    sets the current activity so the watchdog can narrate it with no model call
  snag()     something went wrong but we're carrying on — the user hears about it NOW
  finish()   the final answer; SEALS the turn so nothing can overwrite or follow it
  fail()     the turn died; an honest message instead of an eternal hourglass
  note()     a brand-new message, for problems found after the seal

The watchdog thread wakes every second and, if nothing has been said for `idle_seconds`
(30 by default), says where we're at and how long it's been. Every write goes through one
lock and re-checks the seal while holding it, so a watchdog tick can never land after the
final answer — the ordering that would otherwise leave "still working…" as the last word in
the thread.
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


def is_progress_line(text: str) -> bool:
    """True for the progress lines a turn leaves behind in the thread — the placeholder, the
    "Running X…" narration, the watchdog's ticker. They are the bot thinking out loud, not
    part of the conversation, so thread history skips them.

    Deliberately does NOT match snags or final answers: those are real things the bot told the
    user, and a follow-up ("why did that fail?") needs them. Nothing that ends a turn may start
    with WORKING, or it would be filtered out of its own thread's history.
    """
    t = (text or "").strip()
    return not t or t == THINKING_PLACEHOLDER or t.startswith(WORKING)


class Reporter:
    """Owns one turn's commentary. Thread-safe: the agent loop, its tool worker threads and
    the watchdog all write through here.

    Callbacks: `post(text)` starts a new message in the thread and returns its ts (or None);
    `update(ts, text)` edits one we already posted.
    """

    def __init__(self, post, update, logger, idle_seconds: int = IDLE_SECONDS):
        self._post = post
        self._update = update
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

        # The message we're still allowed to overwrite, and whether we may. Only scaffolding
        # is overwritable; a real thought is left alone once it's been said.
        self._tail_ts = None
        self._tail_transient = False

        self._activity = "getting started"
        self._hint: str | None = None

    # ---- lifecycle -------------------------------------------------------------------

    def open(self, text: str = THINKING_PLACEHOLDER) -> "Reporter":
        """Post the placeholder as overwritable scaffolding, so the turn's first real line
        replaces it instead of following it. Failure here is not fatal: with no tail to edit,
        that first line simply becomes a new message and the turn carries on."""
        with self._lock:
            self._tail_transient = True
            try:
                self._tail_ts = self._post(text)
                self._last_text = text
            except Exception:
                # Rate limit, not_in_channel, a Slack 5xx.
                self._log.exception("status: could not post the placeholder")
        return self

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
        """A new thought, as its own message in the thread. Also resets the idle clock."""
        if activity:
            self.doing(activity)
        self._write(f"{WORKING} {msg}")

    def waiting(self, msg: str, activity: str | None = None) -> None:
        """Still on the same thought, still waiting — "the sandbox is booting", "giving it 10
        more seconds". Replaces the previous transient line rather than stacking up a column of
        near-identical messages, and the next real thought replaces it in turn."""
        if activity:
            self.doing(activity)
        self._write(f"{WORKING} {msg}", transient=True)

    def doing(self, activity: str, hint: str | None = None) -> None:
        """Record what we're busy with. Writes nothing — this is what the watchdog narrates
        if the turn then goes quiet, so it should read as a phrase: "waiting for the code
        sandbox to boot", "reading plans.pdf"."""
        with self._lock:
            self._activity = activity
            self._hint = hint

    def snag(self, msg: str) -> None:
        """Hit a problem but still trying. Its own message, so it's still there at the end of
        the turn — the user hears about obstacles as they happen, and can still see them
        afterwards next to the result they explain."""
        self._write(f"{SNAG} {msg}")

    def finish(self, text: str) -> bool:
        """Write the final answer and seal the turn. It replaces a trailing ticker if there is
        one (so the thread never ends on "still working…") but never a real thought. After this
        nothing else can write. Returns False if the write itself failed (the caller should
        post a fresh message)."""
        with self._lock:
            self._stop.set()
            self._sealed = True
            return self._raw(text)

    def fail(self, msg: str) -> None:
        """The turn is over and it did not work. Never let this be swallowed — an unexplained
        stall is exactly what the user complained about."""
        self.finish(f"{FAILED} {msg}")

    def close(self) -> None:
        """Stop the watchdog and, if nothing ever sealed this turn, seal it with an honest
        admission. Idempotent — this is what makes an early `return` as safe as a raise."""
        self.stop()
        if not self.sealed:
            self._log.warning("status: turn ended without a final message; sealing a fallback")
            self.fail("I stopped without finishing and without an error to explain why — that's "
                      "a fault on my side, not something you did. Nothing is still running.")

    def note(self, text: str) -> None:
        """A new message in the thread, allowed even after the seal. For things discovered
        after the answer was final (e.g. a file failed to upload), which would otherwise
        vanish silently."""
        try:
            self._post(text)
        except Exception:
            self._log.exception("status: could not post follow-up note")

    # ---- internals -------------------------------------------------------------------

    def _write(self, text: str, transient: bool = False) -> bool:
        with self._lock:
            if self._sealed:
                return False
            return self._raw(text, transient)

    def _raw(self, text: str, transient: bool = False) -> bool:
        """Caller holds the lock. Overwrites the tail if it's scaffolding, otherwise starts a
        new message. Skips an exact repeat (Slack rate limits are per-workspace)."""
        self._last_spoke = time.monotonic()
        if text == self._last_text and transient == self._tail_transient:
            return True
        if self._tail_transient and self._tail_ts:
            try:
                self._update(self._tail_ts, text)
                self._last_text, self._tail_transient = text, transient
                return True
            except Exception:
                # Deleted message, or Slack having a moment. The line still has to reach the
                # user, so fall through and start a new message instead of dropping it.
                self._log.exception("status: could not edit the current message; posting instead")
                self._tail_ts = None
        try:
            ts = self._post(text)
        except Exception:
            self._log.exception("status: could not post to the thread")
            return False
        self._tail_ts, self._last_text, self._tail_transient = ts, text, transient
        return True

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
                # Transient: the first tick starts the ticker, later ticks just move its clock
                # on, and the next real thought (or the answer) takes its place.
                self._raw(self._line(), transient=True)


@contextlib.contextmanager
def turn(channel: str, thread_ts: str, say, client, logger):
    """One turn = one commentary = one promise, from the first line to the last.

    The placeholder post lives INSIDE the guard, so there is no window before the Reporter
    exists in which a Slack hiccup can kill the turn and leave the user with nothing at all.
    Every exit — return, raise, even a BaseException — goes through close(), which seals the
    turn. That's the point: the guarantee is positional, not something each new call site has
    to remember.
    """
    def post(text):
        return ((say(text=text, thread_ts=thread_ts) or {}).get("ts"))

    def update(ts, text):
        client.chat_update(channel=channel, ts=ts, text=text)

    reporter = Reporter(post, update, logger)
    reporter.open()
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
