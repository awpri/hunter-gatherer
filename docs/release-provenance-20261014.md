# Release provenance review

This record began with the source-export review on 2026-10-14 and was updated for
the `0.1.0a1` public alpha. It describes a finite publication boundary; the exact
release manifest and verification reports identify what was actually shipped.
It contains no private corpus names, questions, conversations or credentials.

## Selected content and provenance

- **Runtime Python:** project implementation under `hunter_gatherer`, excluding
  private development evaluation modules. The public distribution is
  `hunter-gatherer`. Project copyright attribution is retained in NOTICE.
- **Fictional demo:** purpose-written Lantern Markdown sources contain versioned
  plans, an owner correction and explicitly unknown outcomes. They do not come
  from a personal library.
- **Tests and retrieval fixtures:** the exporter's explicit allowlists define
  which tests and synthetic fixtures ship. Examples are authored fictional cases
  or generated in temporary directories. Hermetic tests use isolated SQLite and
  hashing by default. Optional semantic measurements use an explicitly selected
  model/cache and are not private-library quality claims. Real-library anchors
  and personal-source fixtures remain excluded.
- **Frontend:** project source, lockfile and rebuilt packaged assets. Geist and
  Geist Mono retain their SIL Open Font License 1.1 notice, identifying the Geist
  Project Authors (2024). Lucide's notice retains ISC attribution and the MIT
  notice for identified Feather derivatives by Cole Bemis. Font files and full
  notices are explicit exporter entries. Their inclusion is not a complete
  audit of every transitive dependency's rights.
- **Documentation:** the public README and guides are maintained directly and
  selected by the exporter, including terminal/MCP setup, the fictional demo,
  compatibility boundaries and release verification. Public guides do not
  reproduce private evaluation reports. The packaged example research skill is
  inert documentation, not an instruction executed during installation.

## License and isolation

The owner selected Apache License 2.0 for hunter/gatherer on 2026-10-20.
[LICENSE](../LICENSE) contains its full text; [NOTICE](../NOTICE) preserves
original project and third-party attribution. This does not relicense dependencies.

The public checkout, preview libraries and exported packages are separate from
existing personal installations. Their databases, credentials and configuration
are not release content. Supported exports preserve source identifiers and
history; installation does not authorize automatic migration.

## Verification boundary

The public repository was established from reviewed exported source rather than
private development history. Release updates must retain that boundary. Export,
hygiene and build scripts do not themselves publish code, switch repository
visibility, migrate a personal library or change an active client's configuration.

For each release, inspect the exact source manifest, wheel/sdist, fresh installed
workflow, backup/restore, packaged assets and platform results. Record skipped or
unverified cases. Narrow scanner success and synthetic test results do not
establish universal security, retrieval quality or rights clearance. See
[release verification](public-release-guide.md) for the concrete checks.
