# Get started

Install h/g, create an empty library, and import the files you want to search.
The base package includes SQLite, text/PDF extraction, the terminal, MCP and a
bundled web interface. It works without a model key, model server or Node.js.
Neural embeddings, speech and OCR are optional.

This guide installs **0.1.0a1**, the first public alpha, from GitHub. You need
Git and **Python 3.11 or newer**, or `uv`, which can install Python for you.
There is no h/g PyPI package or Homebrew formula yet.

## macOS and Linux

```bash
git clone --branch v0.1.0a1 --depth 1 https://github.com/awpri/hunter-gatherer.git
cd hunter-gatherer
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/hunt setup
.venv/bin/hunt init ./my-library
.venv/bin/hunt serve ./my-library --port 8767
```

## Windows PowerShell

```powershell
git clone --branch v0.1.0a1 --depth 1 https://github.com/awpri/hunter-gatherer.git
Set-Location hunter-gatherer
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\hunt.exe setup
.\.venv\Scripts\hunt.exe init .\my-library
.\.venv\Scripts\hunt.exe serve .\my-library --port 8767
```

If `py` is unavailable, use your Python 3.11+ executable. Calling the environment's
executables directly avoids changing PowerShell's activation policy. `init`
requires a new or empty directory; it never replaces an existing library.

### With uv

If you already use [uv](https://docs.astral.sh/uv/getting-started/installation/),
replace the environment/install commands with these from the checkout:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python .
```

On Windows, use `.venv\Scripts\python.exe` for the interpreter path. uv handles
package downloads and caching; it may download Python if the requested version
is not installed. h/g does not need a separate dependency download manager.

### From a release asset

The [alpha release](https://github.com/awpri/hunter-gatherer/releases/tag/v0.1.0a1)
provides the wheel and source archive with checksums. Download the wheel, verify
it against the release checksum list, create a dedicated virtual environment,
then install that local file:

```bash
.venv/bin/python -m pip install ./hunter_gatherer-0.1.0a1-py3-none-any.whl
```

Use the Windows interpreter path above when applicable. The wheel includes the
web interface. Dependencies are resolved by pip or uv; a wheel alone is not an
offline bundle of every dependency.

## Import and ask

Open [http://127.0.0.1:8767/](http://127.0.0.1:8767/) and use **Import** to choose
files. The graph is the main workspace; choose **Ask** in its search bar and enter
one question about your sources. Open a source title to inspect the full passage.

You can also import from a second terminal using the h/g environment:

```bash
hunt import ./notes.md --library ./my-library
hunt import ./documents --library ./my-library --recursive
hunt ask "What did we decide about the launch?" --library ./my-library
hunt ask "What did we decide about the launch?" --server http://127.0.0.1:8767
```

Replace the file paths and question with your own. `--recursive` includes files
inside subdirectories. `--sensitive` marks imported material as sensitive; reads
must then explicitly include it. See the [terminal guide](cli.md) for citations,
structured output and source visibility.

For a practice library, use `hunt demo create ./demo-library`, then
`hunt demo serve ./demo-library --port 8768`. The [demo guide](demo.md) describes
its fictional sources. The demo and your own library are separate directories.

## Optional features

`hunt setup` reports installed packages and installation hints. `setup --json`
provides the same information for tools. It installs nothing, downloads no model,
and does not open a library.

Install extras in the same environment. For example, original PDF page and
region images need:

```bash
.venv/bin/python -m pip install '.[document-view]'
```

Windows: `.\.venv\Scripts\python.exe -m pip install ".[document-view]"`.
With uv: `uv pip install --python .venv/bin/python '.[document-view]'`.
From a wheel, use its local path plus the extra, such as
`'./hunter_gatherer-0.1.0a1-py3-none-any.whl[document-view]'`.

| Extra | Adds |
| --- | --- |
| `document-view` | Original PDF page and region rendering. No OCR or neural model. |
| `semantic` | Sentence-transformer embeddings and their neural runtime. Model weights are separate. |
| `qdrant` | Client for an explicitly configured Qdrant memory-vector index; no server installation. |
| `document-recovery` | PDF layout/OCR recovery; model and OCR-engine setup are separate. |
| `voice` | Local speech input dependencies; audio devices and models still need setup. |
| `tts` / `tts-openai` | Local Piper / configured OpenAI speech output. |
| `desktop` | Native desktop window shell. |

Speech, routing and the broader agent runtime are outside the supported default
library workflow. Installing an extra does not connect a provider or change an
existing library's embedding model.

### Semantic search

Keyword search is the lean default. To create a **new** library with neural
embeddings, first install `.[semantic]`, then choose whether to download the model:

```bash
hunt init ./semantic-library --semantic --download-model
hunt serve ./semantic-library --port 8768
```

Omit `--download-model` if the configured model is already cached locally; without
it, initialization must use the cache and fails if the model is unavailable.
The initial download can be substantial. A running server reuses the loaded
model across questions. Qdrant is not required.

Do not change a populated library's model by editing its settings. Its stored
vectors must match the configured model; changing that model requires a deliberate
backup and index rebuild. A missing model never permits silently switching an
existing semantic library to hashing.

## Stop, restart and recover

Press **Ctrl+C** in the server terminal to stop it. Keep the library directory and
restart with the same `hunt serve ./my-library --port 8767` command. Choose a
different loopback port if it is occupied, and use that port in your browser and
MCP client.

```bash
hunt backup create ./my-backup --library ./my-library
hunt backup verify ./my-backup
hunt init ./restored-library --from-backup ./my-backup
```

Use a new backup destination and a new or empty restore directory. A restore
copies verified data into a separate library; it does not switch clients or
replace the original. Keep backups outside the library directory.

## Configuration and upgrades

`--library DIR` selects that initialized library's settings explicitly. The
separate demo commands ignore normal configuration. Commands using general h/g
settings read `~/.hunter-gatherer/config.env`, or the path named by
`HG_CONFIG_FILE`, and `HG_` environment variables. They do not automatically load
a working-directory `.env` or `PROHARNESS_` settings. [.env.example](../.env.example)
is a reference for advanced configuration, not a required setup step.

The package is `hunter-gatherer`, Python imports use `hunter_gatherer`, and
commands are `hunt`, `hunt-mcp` and `hunt-ui`. Install h/g separately from any
existing ProHarness environment. No original database or configuration is migrated
by installation; see [compatibility and migration](roadmap.md#compatibility-and-migration).

To upgrade, back up first, read the new release notes, and install that version
into the dedicated environment with the same optional extras. Keep library data
outside the environment. There is no Homebrew tap yet; GitHub release artifacts
are the supported alpha distribution path.

Next: [use the library](usage.md), [use the terminal](cli.md), or
[connect your assistant](mcp.md).
