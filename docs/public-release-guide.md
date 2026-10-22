# Release verification

This guide describes the reproducible source and package boundary for the
hunter/gatherer public alpha. For installation and normal use, start with
[Get started](getting-started.md).

The release is `0.1.0a1`, tagged `v0.1.0a1`. It includes the local library, bundled
interface, terminal and MCP workflow. The inherited agent/audio runtime remains
available for advanced use but is not the default alpha experience.

## Source boundary

`scripts/export_public_candidate.py` selects runtime Python under
`hunter_gatherer`, reviewed tests/docs, fictional fixtures and packaged frontend
assets. `PUBLIC_CANDIDATE_MANIFEST.json` records selected file hashes and sizes.
The exporter excludes local configuration, credentials, agent state, personal
databases, private corpora and development evaluation modules.

The public repository starts from reviewed source, not the original project's
private Git history. Future releases can use this repository's reviewed history;
do not import private development history as part of a release update. The
[provenance record](release-provenance-20261014.md) describes attribution and
fixture origins. Pattern scanning is a limited check, not comprehensive privacy
or rights clearance.

## Rebuild the interface

From `frontend/`:

```bash
npm ci
npm test
npm run build
```

Commit the rebuilt packaged interface with its source changes. Verify installed
assets after rebuilding; fonts and icon notices must accompany them. Users do
not need Node to run the resulting package.

## Export and build

Run from the repository root. Use new destinations outside the checkout:

```bash
python3 scripts/export_public_candidate.py --destination ../public-candidate
python3 scripts/check_publication_hygiene.py ../public-candidate
python3 -m venv ../hg-build-env
../hg-build-env/bin/python -m pip install build
../hg-build-env/bin/python -m build ../public-candidate --outdir ../hg-release-dist
```

The build produces an sdist and wheel. Scan both artifacts as well as the clean
export. Run hygiene checks before tests create bytecode/caches in the candidate.
On Windows, use `Scripts\python.exe` for virtual-environment executables.

## Test the installed artifact

Install the exact wheel into a fresh environment and run from outside the source
checkout. Example, continuing from the previous commands:

```bash
python3 -m venv ../hg-check-env
../hg-check-env/bin/python -m pip install '../hg-release-dist/hunter_gatherer-0.1.0a1-py3-none-any.whl[dev,document-view]'
cd ..
hg-check-env/bin/python -m pytest -q public-candidate/tests
hg-check-env/bin/python -I -B public-candidate/scripts/smoke_packaged_demo.py --require-lean --output packaged-demo.json
hg-check-env/bin/python -I -B public-candidate/scripts/smoke_personal_library.py --output personal-library.json
hg-check-env/bin/python -m pip freeze > resolved-dependencies.txt
```

The synthetic tests exercise source/access rules, citations, document history,
collections, continuation, configuration isolation, original images, backup and
restore. The demo smoke checks the installed fictional workflow. The personal
library smoke starts from an empty directory, imports files and exercises the
installed CLI/API/MCP and recovery path. Keep their reports with the artifact.

Also install the wheel without test/image extras in a fresh base environment.
Confirm that `hunt setup`, personal library creation, import and text search work
without Torch, sentence-transformers, Qdrant or cloud SDKs. Optional semantic
experiments should use a separate environment and explicitly configured cached
weights; do not make routine CI download models implicitly.

The [CI workflow](https://github.com/awpri/hunter-gatherer/actions/workflows/packaged-demo.yml)
checks frontend source/assets plus packaged installs on Linux, macOS and Windows
with Python 3.11–3.14. Require the exact release commit's checks to pass or state
unverified cases explicitly. Resolved dependency reports describe tested
environments; they are not a universal cross-platform lockfile.

## Search and interface walkthrough

Evaluate documents beyond Lantern. Include paraphrases, negation, conflicting
versions, plans versus completed outcomes and questions with no supported answer.
Judge retrieval and excerpt selection separately. Preserve exact quotations and
qualifications; a matching title or selected sentence is not claim verification.
Record the corpus, backend/model, settings and measured results instead of
extrapolating from a few examples.

Manually check the installed browser workflow: create/import, Ask, context/source
opening, copying citations, saving a collection, restarting and restoring a
backup. Check keyboard focus, light/dark themes, reduced motion and narrow screens.
Automation cannot establish that every visual interaction is usable.

## Compatibility checks

- New tools/resources use `hg_*` and `hg://`; clients must update old tool names.
- Existing bracket citations retain their IDs and source versions.
- Readers accept older collection markers and compact formats without changing
  source text, hashes, reference identities or recorded followups.
- `HG_` settings and h/g's config directory remain separate from ProHarness.
- The real-library launcher uses its explicit manifest, not ambient provider
  credentials or another installation's database.
- Backup/restore uses a new destination and preserves the selected model identity.

Do not migrate or switch an existing personal installation as part of package
verification. Use synthetic data or an explicitly authorized separate copy.

## Publish the alpha

Prepare the exact wheel/sdist, checksum list, source manifest, dependency reports
and verification results. Release notes should identify the source commit,
supported workflow, installation instructions, known limits and relevant changes.
Include an actual interface screenshot. Ensure the repository's private security
reporting channel matches [SECURITY.md](../SECURITY.md).

Publish a tagged **prerelease** on GitHub only after verifying those artifacts.
GitHub is the alpha distribution channel; do not advertise a PyPI package or
Homebrew formula until those distributions exist. Verify the public repository,
release downloads, documentation links and fresh installation after publishing.

## License

Project source is licensed under [Apache License 2.0](../LICENSE).
[NOTICE](../NOTICE) retains original ProHarness and bundled third-party
attribution. Dependency and asset licenses retain their own terms. The original
ProHarness installation and live personal library are separate from this release.
