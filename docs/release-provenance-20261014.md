# Release provenance review — 2026-10-14

This is a finite local review of the exporter allowlist, not publication approval.
The final manifest and build/install/restore results must accompany the exact
candidate. This document deliberately contains no private corpus names, source
paths, questions, conversations or credential values.

## Selected content and provenance

- Runtime Python: project implementation selected under `proharness`, excluding
  evaluation modules. The owner selected Apache 2.0 for hunter/gatherer on
  2026-10-20. Original ProHarness attribution is retained in NOTICE.
- Demo Markdown: purpose-written fictional Lantern project inputs with versioned
  plans, an owner correction and explicitly unknown outcomes. No user library
  documents are used.
- Tests: the exporter's `PUBLIC_TESTS` tuple is the precise reviewed boundary.
  Configuration values, garden/recipe/store examples, recovery documents and
  Lantern inputs are inline synthetic cases or generated in temporary directories.
  They use the shared hermetic fixture, hashing embeddings and temporary SQLite
  stores. Optional SQLite-vector tests may skip without their extra. The export
  does not include real-library evaluation anchors or personal-source fixtures.
- Frontend: project source and lockfile plus current packaged assets. Geist and
  Geist Mono have a bundled SIL Open Font License 1.1 notice identifying the Geist
  Project Authors (2024). Lucide's bundled notice includes ISC attribution and
  the retained MIT notice for identified Feather derivatives by Cole Bemis.
  Both font files and these full notices are explicit exporter entries. The
  notices were inspected locally; this does not establish a complete dependency
  rights audit or ownership of every generated frontend bundle.
- Documentation: this guide, this provenance record, the fictional demo guide,
  coordination README and example research skill were reviewed as self-contained
  instructions. The exported README derives from the public guide rather than
  carrying links to excluded development reports. Public docs do not reproduce
  private evaluation results. The example skill is inert packaged text.

## License and remaining gates

The root LICENSE contains the official Apache License 2.0 text, selected by the
owner for hunter/gatherer on 2026-10-20. NOTICE retains original project and
third-party attribution. This choice does not relicense upstream dependencies.
The hunter/gatherer checkout, preview data and release export are separate from
the original ProHarness installation; the original project remains unchanged.

Before publication, verify the exact exported wheel/sdist and fresh installation,
backup/restore and asset resolution; execute the prepared platform matrix or
state its unverified cells; obtain owner approval for release. Narrow scanner
success is only a triage result. Publishing existing Git history remains outside
this route: use a fresh repository from the reviewed export without importing
private development history. No release, staging, push or history deletion is
performed by these preparation scripts.
