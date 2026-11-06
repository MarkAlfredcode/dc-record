"""One definition of where a SAM.gov attachment is in being read (added 2026-11-06).

Before this, sam_docs.py, screen_sam_docs.py and triage_sam.py each kept part of the lifecycle as string literals with
overlapping words ("pending" in one was "retry" in another, a missing record meant "retry" in two places and "queued" in a
third). The lifecycle:

  sam_docs.py (per run, on the notice's attachment list)
    restricted          not public on SAM.gov; never read
    <cached state>      already read by an earlier run (a FINAL state below)
    pending             needs reading this run; then, after the read:
      read              text extracted, handed to screen_sam_docs
      deferred          out of time budget; waits in data/sam-docs-pending.json (QUEUED)
      error             extraction failed; screen_sam_docs records it as RETRY

  screen_sam_docs.py (the cache, data/sam-docs-read.json, field "s")
    (no record)  = RETRY   not read yet, or read but out of screening budget: the pending queue reads it again
    RETRY        read failed; tries again until MAX_ATTEMPTS, then FAILED (also FAILED after QUEUE_DAYS in the queue)
    SCREENED / WAGE_DETERMINATION / BOILERPLATE_ONLY / NO_TEXT   read to the end
    UNSUPPORTED  video/CAD/etc: nothing extractable, never retried
    FAILED       gave up

  triage_sam.py (what an edition says about a notice's files) uses edition_status() below.

Final states never change back. RETRY is the only state a run picks up again.
"""

SCREENED = "screened"
WAGE_DETERMINATION = "wage_determination"
BOILERPLATE_ONLY = "boilerplate_only"
NO_TEXT = "no_text"
UNSUPPORTED = "unsupported"
FAILED = "failed"
RETRY = "retry"

# Per-run labels on a notice's attachment list (sam_docs.py), not stored in the cache.
RESTRICTED = "restricted"
PENDING = "pending"
READ_NOW = "read"
DEFERRED = "deferred"
ERROR = "error"
# Edition-only labels (triage_sam.py).
QUEUED = "queued"
NOT_READ = "not_read"

READ_TO_END = frozenset({SCREENED, WAGE_DETERMINATION, BOILERPLATE_ONLY, NO_TEXT})   # the file's content was read
GAVE_UP = frozenset({UNSUPPORTED, FAILED})
FINAL = READ_TO_END | GAVE_UP

UNREAD_REASON = {
    RESTRICTED: "access-restricted on SAM.gov",
    UNSUPPORTED: "no extractable text (video/CAD/etc.)",
    FAILED: "could not be downloaded or read",
    QUEUED: "queued, read in a later edition",
    RETRY: "read failed, retrying in a later edition",
    NOT_READ: "not read yet",
}


def cache_state(rec):
    """State of a file in the read cache. No record, or a record that never got a status, waits to be read: RETRY."""
    return (rec or {}).get("s") or RETRY


def needs_reading(rec):
    return cache_state(rec) == RETRY


def run_status(public, rec):
    """sam_docs.py: the label on a notice's attachment before this run reads anything."""
    if not public:
        return RESTRICTED
    state = cache_state(rec)
    return state if state in FINAL else PENDING


def edition_status(restricted, in_queue, rec):
    """triage_sam.py: how an edition describes a file. A file with no record that is not waiting in the queue was never
    scheduled, so it says "not read yet" rather than claiming a later edition will read it."""
    if restricted:
        return RESTRICTED
    if in_queue:
        return QUEUED
    if not rec:
        return NOT_READ
    return cache_state(rec)
