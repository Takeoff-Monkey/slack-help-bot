"""The bot's voice while it's working — a running commentary in the thread, plus a watchdog
that refuses to let the bot go quiet.

Originally a turn posted ":hourglass: _Thinking…_" and then said nothing at all until it had
a final answer. That got fixed by narrating progress into that same placeholder — but editing
one message means every new thought erases the last one. The user watched a single line
flicker between "reading the plans" and "running the extractor" with no way to see what the
bot had actually done, and a snag reported mid-turn was gone the moment the next line landed.

That transcript then overcorrected: every preamble, every "Running X…", every cold-start
retry and every snag became its own message, and a routine job left a column of chatter —
much of it raw errors that read to a teammate as "the bot is broken" when it was in fact
working around them. So the thread now carries at most three things per turn:

  1. the FIRST thing the bot says (it replaces the "Thinking…" placeholder), so the user can
     see what it's about to do and stop it if that's wrong;
  2. one progress line, posted by the watchdog only after 30 seconds of silence and then
     edited in place — "Still working, about halfway there…", "Almost done…". A snag turns it
     into a light "small hiccup, trying another way" instead of an error message;
  3. the final answer, which replaces that progress line so the thread never ends on it.

Everything else the bot thinks along the way still goes to the log, not the thread.

A Reporter owns that commentary for the life of a turn:
  say()      a thought — posted if it's the turn's first, otherwise logged
  waiting()  "still booting, giving it 10 more seconds" — logged; the watchdog covers it
  doing()    what we're busy with, for the log
  stage()    how far along we are, which picks the watchdog's wording
  snag()     something went wrong but we're carrying on — logged, and softens the next tick
  finish()   the final answer; SEALS the turn so nothing can overwrite or follow it
  fail()     the turn died; an honest message instead of an eternal hourglass
  note()     a brand-new message, for problems found after the seal

The watchdog thread wakes every second and, if nothing has been said for `idle_seconds`
(30 by default), says roughly how far along we are. Every write goes through one lock and
re-checks the seal while holding it, so a watchdog tick can never land after the final
answer — the ordering that would otherwise leave "still working…" as the last word in the
thread.
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

# How far along a turn is — the watchdog's wording follows it.
SETTING_UP = "setting up"     # reading the request, fetching files, before any real work
HALFWAY = "halfway"           # a tool is running, or we're inspecting before acting
FINISHING = "finishing"       # the deliverable exists (or the steps are running out)

_PROGRESS = {
    SETTING_UP: "Still working, just getting set up…",
    HALFWAY: "Still working, about halfway there…",
    FINISHING: "Almost done, just a few more steps…",
}
HICCUP_LINE = "Ran into a small hiccup, so I'm trying another way — still making progress…"
LONG_RUN_LINE = ("This one's taking a little longer than usual, but I'm still making "
                 "progress… Say _stop_ if you'd rather I drop it.")

# The one thing a turn says when it can't go on. Always an apology plus a way forward —
# never a bare exception, which reads to a teammate as "the bot is broken".
TRY_AGAIN = "Reply _try again_ and I'll take another run at it, or start a fresh chat if you'd rather."


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
        self._said_first = False      # has the turn's one permanent progress line gone out?

        # The message we're still allowed to overwrite, and whether we may. Only scaffolding
        # is overwritable; a real thought is left alone once it's been said.
        self._tail_ts = None
        self._tail_transient = False

        self._activity = "getting started"
        self._stage = SETTING_UP
        self._hiccup = False

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
        """A thought. The turn's first one is posted (it replaces the placeholder) so the user
        sees what the bot is about to do; every later one only goes to the log — a running
        commentary is exactly the chatter the user asked to be rid of."""
        if activity:
            self.doing(activity)
        with self._lock:
            first, self._said_first = not self._said_first, True
        if first:
            self._write(f"{WORKING} {msg}")
        else:
            self._log.info("status (not posted): %s", msg)

    def waiting(self, msg: str, activity: str | None = None) -> None:
        """Still on the same thought, still waiting — "the sandbox is booting", "giving it 10
        more seconds". Logged only: a cold start is routine, and if it drags on, the watchdog
        already tells the user we're still going."""
        if activity:
            self.doing(activity)
        self._log.info("status (waiting): %s", msg)

    def doing(self, activity: str) -> None:
        """Record what we're busy with, as a phrase ("running `run_code`"). Writes nothing to
        the thread; the watchdog logs it when the turn goes quiet."""
        with self._lock:
            self._activity = activity

    def stage(self, stage: str) -> None:
        """Record how far along we are (SETTING_UP / HALFWAY / FINISHING). Moving on also
        clears a pending hiccup — whatever went wrong is behind us now."""
        with self._lock:
            self._stage = stage
            self._hiccup = False

    def snag(self, msg: str) -> None:
        """Hit a problem but still trying. The details go to the log; the user just gets a
        lighter next progress line ("small hiccup, trying another way"). Most snags are worked
        around before anyone would have read about them, and when one isn't, the final answer
        says so."""
        self._log.info("status (snag): %s", msg)
        with self._lock:
            self._hiccup = True

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
            self.fail(f"Sorry — I stopped before finishing this, and that's on me, not "
                      f"anything you did. {TRY_AGAIN}")

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
        """Caller holds the lock."""
        if self._hiccup:
            line = HICCUP_LINE
        elif self.elapsed() >= LONG_RUN_SECONDS and self._stage != FINISHING:
            line = LONG_RUN_LINE
        else:
            line = _PROGRESS.get(self._stage, _PROGRESS[HALFWAY])
        return f"{WORKING} {line}"

    def _watch(self) -> None:
        while not self._stop.wait(1.0):
            with self._lock:
                if self._sealed:
                    return
                if time.monotonic() - self._last_spoke < self._idle:
                    continue
                self._log.info("status: quiet for %s while %s (%s)",
                               human(time.monotonic() - self._last_spoke), self._activity,
                               self._stage)
                # Transient: the first tick posts the progress line, later ticks edit it only
                # if the wording changed (_raw skips exact repeats), and the answer replaces it.
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
        reporter.fail(f"Sorry — something went wrong on my end and I had to stop. {TRY_AGAIN}")
    finally:
        reporter.close()
