# Contributing to hunter/gatherer

hunter/gatherer is a maintainer-led project. Bug reports, suggestions and forks
are welcome. Pull requests are accepted by invitation only: open an issue to
describe the problem or proposed change, then wait for an explicit invitation
from the maintainer before submitting a pull request. This applies to code and
documentation changes alike.

An invitation is permission to propose a change, not a promise to merge it.
Unsolicited pull requests may be closed without review. Responses, reviews and
support are provided as time permits, with no guaranteed turnaround.

You are welcome to develop and share your own fork under the Apache-2.0 license
without seeking an invitation. The development guidance below is for both forks
and invited contributions.

## Set up a development environment

Follow [Get started](docs/getting-started.md), then install development tools in
the same dedicated virtual environment:

```bash
.venv/bin/python -m pip install -e '.[dev,document-view]'
```

In Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,document-view]"
```

Keep development data in a separate directory. Use fictional documents for
reproductions and tests; do not include personal library databases, credentials
or copied private source material in a contribution.

## Work on the web interface

The interface uses React and TypeScript. From `frontend/`:

```bash
npm ci
npm run dev
```

The development server uses port 5174 and proxies the separate backend on port
8767. Create a separate demo library, then start its backend with
`hunt demo serve ./demo-library --port 8767`. Preserve keyboard operation,
accessible names, light/dark themes and
reduced-motion preferences when changing the interface.

## Check your change

Run the checks relevant to the behavior you changed. For Python:

```bash
.venv/bin/python -m pytest tests
```

For the interface, from `frontend/`:

```bash
npm test
npm run build
```

Use `.\.venv\Scripts\python.exe` for the Python command on Windows. The frontend
build produces bundled assets used by the installed package. Packaging changes
also need the installed-artifact workflow described in the
[release verification guide](docs/public-release-guide.md).

The repository's tests use synthetic fixtures. Some older evaluation commands
refer to private evaluation modules excluded from this source release; those are
not prerequisites for contributing. Check the live CI results instead of
assuming an untested platform or optional subsystem works.

## Preserve the evidence contract

- Keep exact source text and citations separate from summaries, annotations and discovery hints.
- Retain document identity, versions and current access checks when opening or exporting sources.
- Make output limits and unavailable content observable without turning every result into a warning wall.
- Keep writing examples separate from factual support.
- Prefer optional integrations over making a model provider necessary for browsing the library.

## Submit an invited pull request

Link the issue or discussion where the maintainer invited the pull request.
Explain the problem, the resulting behavior and how you checked it. Include a
small fictional reproduction for a bug, and screenshots for visible UI changes
when useful. Keep unrelated refactors separate.

The public source exporter uses an explicit file selection. If you add a module,
document or test that belongs in source distributions, update its selection in
`scripts/export_public_candidate.py` and verify the exported candidate. The
manifest records selected file hashes and sizes.

Project source is Apache-2.0; retain the license and third-party notices with any
bundled assets.

See the [security policy](SECURITY.md) before sharing vulnerability details.
