# Release notes

## 0.1.0a3 — several assistants, one library

Several assistants can share a library on one computer through `hunt serve` or
separate MCP processes. If two assistants correct the same memory, the first
correction wins. The other gets a `memory_conflict` and, when visible, the ID
of the current record. Retrying the same correction or forget is safe. Concurrent
saves of the same wording, type, scope and evidence keep one record. Privacy and retention changes survive competing edits.
[Concurrent use and conflict results](docs/concurrency.md)

### Search and sources

- Find builds each page from a database snapshot. **Load more results** continues
  through the citations admitted on the first page, even while another client
  writes. Later pages recheck access and show when a result was withdrawn or
  newer records are available. Run a fresh search to include new records.
- In-memory search picks up another client's saves and label changes. SQLite
  vector writes commit with their memory changes when using sqlite-vec. Pending
  updates to persistent indexes can be retried after an interruption or Qdrant
  outage. Concurrent reads also receive the requested records, and sqlite-vec
  keyword searches avoid a deadlock found under load.
- With neural embeddings, Find and profile can reuse cached vectors across
  repeated queries. Find prepares query and detail embeddings before taking
  the shared store lock.
- Memory listings, corrections and history now emit full-length memory
  citations. Older short citations still resolve when unambiguous and visible.
  Fitted PDF page images accept PDFium build labels containing `@`.

### Storage and hosting

- h/g selects SQLite's write-ahead log on recognized local disks and prefers
  the rollback journal on network, synchronized or unidentified storage.
  Local readers can continue while a writer commits. Writers still take turns.
  `hunt storage ./my-library` shows the mode and its reason. `hg_status`
  reports lock waits, conflicts and index repairs. Calls that exceed SQLite's
  lock wait can return a retryable `library_busy` or HTTP 503.
  [Storage commands](docs/cli.md)
- Hosts can give authenticated callers separate permissions for sensitive
  reads, writes, imports and administration, restrict imports to allowed
  folders, and attach a trusted author label. The host supplies sign-in.
  Permissions apply to tool actions across the library. The default local
  installation retains its existing access. [Hosting guide](docs/hosting.md)

### Upgrading from 0.1.0a2

- Existing libraries open in place and keep their record IDs, source text and
  history. Opening the new version adds index-maintenance tables. A persistent
  index can need a one-time repair. Upgrade every process that writes to a
  shared library. Use `hunt backup create` for a live backup, since committed
  writes may still be in SQLite's companion files.
- A library with a registered Qdrant collection requires a coordinated
  collection migration to change embedding models. Local-only indexes can
  still be rebuilt with a different model. [Index recovery and limits](docs/concurrency.md#limits)
- Python callers should handle `MemoryConflictError` from `MemoryStore.add()`
  or `update()` on stale records. Use `mutate()` to apply an edit to the current
  version. `delete()` returns whether it forgot an active record.
- Tool callers supplying `include_sensitive` should pass a boolean. Private
  corrections through the library interface require explicit opt-in, including
  when citing private evidence. The host reserves `metadata.recorded_by`.
  Pass import roots as a collection of paths, such as `[Path("/srv/uploads")]`.
  [MCP usage](docs/mcp.md)

## 0.1.0a2 — consistent h/g namespaces

- Use the current `hg_*` tools, `hg://` resource URIs and h/g export markers
  throughout the package, documentation and examples.
- Remove alternate resource, tool and serialized-format aliases. Libraries or
  saved exports from a differently named installation require an explicit
  migration before opening them with this version.
- Preserve current collection revisions, record IDs, exact quotations, original
  file hashes and source visibility checks.

Install this alpha into its own environment. Existing personal libraries and
configuration are never migrated automatically. The earlier synthetic retrieval
evaluation remains labeled with the version it measured.

## 0.1.0a1 — first public alpha

hunter/gatherer is a local evidence library with a quote-first browser interface,
terminal commands and six MCP tool families. This alpha is distributed through
GitHub under Apache 2.0.

### Included

- Create a separate library with `hunt init`, import selected files with
  `hunt import`, and open the browser interface with `hunt serve`.
- Ask from the graph search bar or `hunt ask`; open original sources, inspect
  quotation context, copy citations and organize collections.
- Use `hunt ask --server` to reuse a running library and its loaded model;
  add `--json` for structured output.
- Connect an assistant through `hg_find`, `hg_open`, `hg_memory`,
  `hg_collection`, `hg_import_documents` and `hg_status`.
- Start with local keyword search and lightweight hashing. Neural embeddings,
  Qdrant, PDF rendering, OCR and audio components are optional dependencies.
- Back up a library and restore it into a new directory, retaining citations
  and archived original files.
- Configure themes, density, reduced motion and private-record access.
- Match common regular plurals and accented spellings, and focus count questions
  on quantities tied to the requested thing. Managed libraries present results
  in the same question-focused order used to select their quotations.

### Naming and compatibility

The distribution is `hunter-gatherer`, the Python package is `hunter_gatherer`,
configuration uses `HG_*`, and new resource URIs use `hg://`. Configured stores
default to `~/.hunter-gatherer`. Managed `--library` workflows use their own
manifest and ignore ambient configuration and agent skills.

Collections, evidence exports and resource URIs use h/g identifiers. Exact source
citations retain their record IDs. Clients connecting to h/g must use its current
tool names and configuration. No existing personal installation or database is
migrated automatically.

### Known limitations

- Ask retrieves quotations rather than generating an answer. Search ranking and
  excerpt emphasis are heuristic; a matching source may not answer the question.
- Historical-version questions require explicit source/version navigation.
  Read the [small synthetic retrieval evaluation](docs/retrieval-evaluation-v0.1.0a1.md)
  for measured results and limitations.
- The supported library service is local and single-user. Optional agent,
  routing, voice and OCR components are advanced features, not required for Ask.
- GitHub is the only release channel for this alpha. PyPI, Homebrew, a dependency
  download manager and automatic personal-library migration are deferred.

See [installation](docs/getting-started.md), [terminal usage](docs/cli.md),
[MCP setup](docs/mcp.md) and [release verification](docs/public-release-guide.md).
Bug reports and forks are welcome; pull requests are by invitation.
