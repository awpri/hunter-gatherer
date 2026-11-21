# Several assistants, one library

More than one assistant can use the same library at once: several clients of one
`hunt serve`, or several `hunt-mcp` processes opened on the same directory. This
page says what h/g guarantees when they overlap, what a client sees when two
changes collide, and what has been measured.

## What you can rely on

- **A change to a memory is all-or-nothing and takes effect in one order.**
  Remember, replace, forget, pin and privacy changes are each checked against
  the current record and written in a single SQLite write transaction. That
  holds between threads of one server and between separate processes.
- **A write based on an outdated read is refused, not applied.** If another
  client forgot, corrected or re-labelled a memory after you read it, your
  write does not undo their decision.
- **Forgetting stays forgotten.** Only an explicit restore clears a forgotten
  record. No update, duplicate import or background pass can bring one back.
- **Corrections form one chain.** A memory is replaced by at most one
  correction; the earlier wording stays as linked history.
- **Privacy only tightens by itself.** A correction is never more visible than
  the record it replaces, and marking a record sensitive still applies if that
  record was forgotten or replaced in the meantime.
- **Search results are validated against the records.** A stale index entry can
  hide a memory from similarity search for a moment. It cannot show a forgotten
  or private one.
- **A read returns the record that was asked for.** Clients of one server share
  a database connection; reads take their turn on it like writes do.

These describe h/g's own records. They do not extend to what an assistant does
outside h/g with text it has already read.

## When two changes collide

| Situation | What the later caller gets |
| --- | --- |
| Two clients correct the same memory | The first correction wins. The second receives `error_code: "memory_conflict"` with `conflict.reason: "superseded"` and `conflict.current_id`, the record to look at before correcting again. Nothing is overwritten. |
| The same correction is sent twice (a retry) | The caller gets `replayed: true` when the current replacement is visible and its wording, normalized metadata, citations and recorded author still match. A change to any of them can produce a conflict. |
| A memory is forgotten, then someone tries to correct it | `memory_conflict` with `reason: "deleted"`. Save the corrected fact as a new memory if it should exist. |
| A memory is corrected, then someone forgets the old ID | `memory_conflict` with `reason: "superseded"` and the current ID. Nothing is removed; forget the current record if the fact itself should go. |
| Forget is sent twice | `deleted: true` with `already_forgotten: true`. Nothing changes the second time. |
| The same fact is saved twice, by a retry or by two clients at once | One record. The later caller gets `stored: false` and `duplicate_of`, the ID that holds it. |
| A record is pinned or marked sensitive while a correction is being prepared | The correction is rebuilt from the current record, so it keeps the pin and the privacy. |
| A record is edited after automatic consolidation judged it a duplicate or a contradiction | h/g keeps the edited record and checks the incoming fact against the current library before saving it. |
| A sensitive record changed and the caller did not ask for sensitive access | `Memory not found or unavailable`, the same as for an ID that never existed. |

"The same fact" means identical wording and type, the same workspace scope, and
no source reference that the existing record lacks. Similar but not identical
wording is handled as before: reported as redundant or stored alongside, never
merged or deleted automatically.

Conflicts come back as a structured tool result, flagged as an error, rather
than as an exception message. An assistant can read `conflict.current_id`, open
that record and decide.

## Choosing how assistants connect

| Setup | Behaviour |
| --- | --- |
| **One `hunt serve`, every client over HTTP MCP** | One process, one store. Operations run one at a time and each sees the others' changes immediately. Simplest, and the default recommendation. |
| **One `hunt-mcp --library DIR` process per client** | Each process has its own connection. A fresh search picks up committed changes from the other clients. On a local disk, readers can search while one writer commits. Concurrent writers take turns. |

## How the file is shared

SQLite can save changes in two ways. With the **rollback journal**, a writer
proceeds only when no reader is active and readers wait for the writer, so
processes take turns. With the **write-ahead log**, readers and a writer in
different processes work at the same time.

The write-ahead log is not safe everywhere. The processes sharing it must all
run on one computer, so it does not work on a network drive. And while it is in
use the newest changes sit in a second file, so a service that synchronizes a
folder file by file can deliver a mismatched pair to another device.

h/g chooses each time a library is opened. There is no setting.

| Where the library is | What h/g uses |
| --- | --- |
| A local disk | Write-ahead log |
| A network drive | Rollback journal |
| A folder synchronized by iCloud Drive, Dropbox, OneDrive, Google Drive, Box or a similar service | Rollback journal |
| Storage it cannot identify | Rollback journal |

If a library is moved, the next opening switches it. A switch needs the file to
itself for a moment. If another process is using the library, h/g carries on in
the mode the file is in and switches once the library is free.

One case is refused instead. A library found in write-ahead mode on a network
drive, or on storage h/g cannot identify, and still held open by something
else, may be in use from another computer. h/g does not open it until that use
ends, and changes nothing.

```bash
hunt storage ./my-library
```

shows the mode in effect, the reason and any warning. It changes nothing.
`hg_status` reports the same under `concurrency`, together with counts of
conflicts, lock waits and index refreshes since the process started. Neither
records IDs, text, queries or paths.

### Backing up an open library

While the database is open, recent commits can live in `library.db-wal`
beside `library.db` and `library.db-shm`. h/g attempts to checkpoint those
commits into the main file. An active reader can delay that checkpoint.
Use `hunt backup create` to take a consistent copy of an open library.

### Where a library should live

Keep a live library on a local disk. A synchronized folder or a network drive
is a risky home whichever way the file is shared: two devices can change the
same file, and a sync client can upload it part-way through a write. `hunt
init` and `hunt storage` warn about both. Move copies between devices with
`hunt backup create`. To use one library from two computers, run `hunt serve`
on one and connect the other to it. Never open one library folder from two
computers at once.

The check recognizes:

- network and virtual filesystems, by type;
- iCloud Drive, including its Desktop & Documents option;
- the folders macOS manages for Dropbox, OneDrive, Google Drive and Box;
- those services' usual folders in the home directory, on any system;
- folders marked by Dropbox, Google Drive or Syncthing.

It does not recognize a sync or backup tool that leaves none of those signs,
and it cannot tell that a folder on a local disk is shared out to another
computer. In the first case, folding the log promptly limits the exposure to
the moments a write is in progress, as with the rollback journal. Nothing
limits it in the second: that is the two-computer case above.

## Searching while others write

A Find page is built from one consistent snapshot of the library. A change made
by another client during the search does not fail it.

A `next_cursor` continues that search while other clients write. The first page
admits the matches it ranked: up to 128 each of memories, passages and file
leads, and up to 256 image-region descriptions. The cursor keeps their
citations in that order, never their text. Later pages are built from that
list instead of by searching again, so a write elsewhere cannot shift what comes
next: no admitted source is skipped or repeated.

Each admitted record is checked against the library when its page is built:

| After the first page, a record was… | Later pages |
| --- | --- |
| forgotten | Withdraw it: it is never delivered. `continuation.withdrawn` counts what a page withdrew. |
| corrected | Withdraw the earlier wording. The correction is a new record (last row). |
| imported again as a newer version of a source | Withdraw the older version's passages and file lead. The new version is a new record. |
| marked sensitive | Withdraw it, unless the search included sensitive records. A continuation must repeat the first call's `include_sensitive`. |
| reworded in place by another tool | Deliver it as it reads now, in the position the first page gave it. |
| added | Leave it out. `continuation.added_since_first_page` says that records were added; ask again to include them. |

Bookkeeping such as saving a collection, access counters or import progress has
no effect on a cursor. `withdrawn` is a count only: it names no record and says
nothing about why one became unavailable.

The library's search page shows these notes beside Load more results.

When a ranking had more matches than were admitted,
`continuation.admitted_limit_reached` is `true`. The cursor ends after the
admitted matches; a narrower question reaches the rest.

A cursor ends with `Find cursor is invalid, expired, or stale; restart Find.`
after 15 minutes, when the server restarts or its configuration changes, once
128 newer cursors have been issued, or when `include_sensitive` differs from the
first call. With connected-source routes enabled (`library_find_relationships`),
it also ends if the connected sources themselves change between pages.

## After a failure

- A memory write that fails changes nothing.
- A call that waits more than 30 seconds for another process returns
  `error_code: "library_busy"` with `retryable: true`. Repeat it. If a memory
  change had in fact been saved, the repeat says so (`duplicate_of`,
  `replayed` or `already_forgotten`) and changes nothing more.
- An import can be resumed: repeat `run` for the same job. Files already
  imported are recognized by their content and not duplicated.
- If a search index could not be updated after a record was saved, the record
  is kept. The index is repaired when the library is next opened, and retried
  before later searches. Until then the record is found by wording and by ID,
  but may be missing from similarity search.
- A statement that fails part-way never leaves the library's shared connection
  stuck in a half-finished transaction.

## Measured

`scripts/benchmark_concurrency.py` runs a mixed workload (Find, continuation,
Open, profile, remember, replace, forget, with deliberate races on shared
records) against a new fictional library. It then inspects the result for
outcomes that must never happen: a forgotten memory active again, a memory with
two corrections, duplicate records, an acknowledged write that is missing, a
sensitive record or its correction no longer sensitive, an in-memory index that
disagrees with the records, a continuation page repeating a source from its
first page, a continuation delivering a memory whose forget or correction had
already been acknowledged, finished writes still waiting in the write-ahead log
once every client is idle, and records missing from a copy of `library.db`
taken by itself.

These timings predate the follow-up fixes for concurrent edits, index repair and
correction retries. One laptop session compared the original implementation with
the first concurrency changes using macOS, Python 3.11 and SQLite 3.53, with
1,000 memories, 120 documents and 150 operations per client using keyword search.
The write-ahead log is what h/g now chooses
on a local disk; before, it had to be set by hand. The rollback journal is what
a library on a network drive or in a synchronized folder gets.

| Clients | Journal | Before | After |
| --- | --- | --- | --- |
| 4 threads, one server | rollback | 58 ops/s; 59–67 of 129 continuations failed. In two of four runs a Find waited 12–17 s and throughput fell to 23–26 ops/s | 63–64 ops/s; nothing failed; slowest call 0.36 s |
| 4 threads, one server | write-ahead | 58 ops/s; 13–17 calls failed on another thread's open transaction; 45–50 continuations failed; 4 MB waiting in the log | 63–64 ops/s; nothing failed; log empty |
| 4 processes | rollback | 96 ops/s; 81% of Find calls failed; 10 duplicates; 387 index entries out of step | 90–92 ops/s; Find p95 111–146 ms; nothing failed; clean |
| 4 processes | write-ahead | 182 ops/s; 78% of Find calls failed; 9 duplicates; 399 out of step; 4 MB waiting in the log | 197–199 ops/s; Find p95 39 ms, slowest call 61 ms; nothing failed; clean; log empty |
| 8 processes | rollback | 98 ops/s; 89% of Find calls failed; 36 duplicates | 98 ops/s; Find p95 210 ms; slowest call 1.7 s; nothing failed; clean |
| 8 processes | write-ahead | 333 ops/s; 98% of Find calls failed; 33 duplicates; 8 MB waiting in the log | 373 ops/s; Find p95 40 ms, slowest call 67 ms; nothing failed; clean; log empty |

"Find failed" was `Library changed during Find; restart Find.` A failed search
returns quickly, which flatters the "before" throughput: the "after" figures
are for calls that all completed. In every "after" run no call failed and the
integrity checks found nothing. Where a range is given, the configuration was
run twice.

Folding the log after each write cost nothing measurable in this workload:
four processes managed 197–199 operations a second with it and 198 without.
Without it, 4 MB of finished writes were still waiting in the log when the
clients went idle. For one client alone, a write took 0.27 ms with the folded
log, 0.36 ms with the rollback journal and 0.13 ms with a log that is never
folded (median of 300).

### One assistant alone

The same workload with a single client shows what the safeguards cost when
nobody else is using the library (300 operations, medians, two runs each in one
session). "Before" used the rollback journal, the only mode it chose itself.

| Call | Before | After, write-ahead log | After, rollback journal |
| --- | --- | --- | --- |
| Whole workload | 47–48 ops/s | 62 ops/s | 61–62 ops/s |
| First page of a search | 28.7–29.3 ms | 29.7–29.8 ms | 29.5–30.0 ms |
| Next page | 28.7–29.2 ms | 11.5 ms | 11.5–11.6 ms |
| Profile | 24.0–24.4 ms | 17.1–17.9 ms | 17.5–17.7 ms |
| Remember | 16.6–16.7 ms | 10.2 ms | 10.6 ms |
| Replace | 0.67–0.74 ms | 0.64–0.72 ms | 0.74–0.80 ms |
| Forget | 0.50–0.52 ms | 0.46–0.49 ms | 0.59–0.63 ms |
| Open two memories | 0.23 ms | 0.25 ms | 0.26 ms |

The last three rows were timed separately in microseconds, three runs each,
because the workload reports to a tenth of a millisecond.

Four calls got slower, all by a fraction of a millisecond:

- **The first page of a search, by about 0.5 ms (2%).** Timed alone, 320
  searches a run, it took 27.6–27.9 ms before and 28.1–29.0 ms after. About
  0.35 ms of that is the price of not sharing prepared statements between
  threads: each statement is prepared again every time it runs. It means a
  thread that reads the connection without taking its turn still cannot
  receive another thread's row. On Python 3.14, four threads reading by ID
  through one connection with shared statements and no lock got the wrong row
  for 58,660 of 338,252 reads.
- **Open, by about 0.02 ms**, about half of it for the same reason.
- **Replace and forget with the rollback journal, by up to 0.1 ms.** With the
  write-ahead log they are no slower than before.

Refusing a write based on an outdated read costs a search nothing. A search
turns every memory into an object and writes none of them, so reading a record
only keeps the row it came from. Its digest is worked out when a write
compares it.

### Continuing a search while others write

The same workload asks for a second page after about half of its searches, and
40% of its operations write. Before cursors kept their admitted matches, many of
those continuations failed with `Find cursor is invalid, expired, or stale;
restart Find.` Same machine and workload, 129 continuations per run, three runs
before that change and two after:

| Clients | Journal | Continuations that failed, before | After |
| --- | --- | --- | --- |
| 4 threads, one server | rollback | 23–30 (18–23%) | 0 |
| 4 threads, one server | write-ahead | 27–35 (21–27%) | 0 |
| 4 processes | rollback | 97–116 (75–90%) | 0 |
| 4 processes | write-ahead | 83–95 (64–74%) | 0 |

In all of these runs every integrity count was zero, including the two
continuation checks: no second page repeated a source from its first page, and
none delivered a memory after its forget or correction had been acknowledged.
The runs in the table above agree: no continuation failed, including 256 of
them with eight processes.

A later page is built from the cursor's list instead of a new search, so it
costs less than a first page. With four processes it takes 14–18 ms at the
median, against 32–45 ms for a first page. In one busy server, where every call
also waits its turn, it takes 30–43 ms against about 50 ms.

A separate check ran four threads reading memories by ID through one store,
12,000 reads in all. On Python 3.14 the earlier code returned the wrong record
or raised for 3,503 of them; on Python 3.11 for none. With this work both
return the requested record every time.

Read these as a comparison under one workload on one machine, not as capacity
figures. The stress runs did not happen to hit the forgotten-memory race this
work started from; that race, and the others in the table above, are covered
by tests that force the exact order of events
(`tests/test_concurrent_memory.py`, `tests/test_store_transactions.py`,
`tests/test_find_continuation.py`, `tests/test_storage_location.py`). Passing
tests and clean runs show these cases behave as described. They are not a proof
that no other interleaving can go wrong.

To repeat it:

```bash
python scripts/benchmark_concurrency.py --mode both --journal delete,wal --clients 4
```

Without `--journal` it measures the mode h/g chooses for the temporary folder
it runs in.

## Limits

- One server process handles one operation at a time. Concurrency there means
  correct turn-taking, not parallel speed. Four processes on a local disk were
  about three times faster than four clients of one server in the run above.
- With the rollback journal and many busy processes, a call can wait seconds
  for its turn, and after 30 seconds it returns `library_busy`. With the
  write-ahead log only two writers wait for each other.
- A cursor delivers only what its first page admitted, as described above.
  Records added later need a new search.
- An older h/g version that opens the same library uses whichever mode the file
  is in, but does not fold the log and still has the faults this work fixed.
  Update every installation that shares a library.
- Each process keeps its own in-memory search index. It catches up with other
  processes when it next searches, at the cost of re-reading memory texts and
  embedding the new ones.
- With SQLite vector search, a change to a memory and its vector commits
  together when written through that backend. For a persistent index that
  still needs updating, h/g records the pending work in SQLite alongside the
  memory change. That lets it retry after a restart, including when Qdrant
  was unavailable at the time of the write.
- A library that has registered a Qdrant collection requires a coordinated
  collection migration to change embedding models. Local rebuilds can still
  use the same model. A library using only local indexes can rebuild with a
  different model.
- Imported documents and collections already had their own transactions,
  revisions and idempotency keys. This page is about memories and search.
- Access counters and other statistics may lose an occasional update under
  contention. They are not part of any record.
