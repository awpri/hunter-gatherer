# Get started

Try h/g with a separate fictional library before connecting personal files.
The base install includes local SQLite, text/PDF extraction, the terminal, MCP
and a bundled web interface. No model key, model server or Node.js is needed for
the demo. Neural embeddings, speech and OCR are optional downloads.

Source installation is currently supported; no h/g PyPI package or Homebrew
formula has been released. You need Git and **Python 3.11 or newer**, or `uv`
which can install Python for you. While this repository is private, your GitHub
account needs access to clone it.

## macOS and Linux

```bash
git clone https://github.com/awpri/hunter-gatherer.git
cd hunter-gatherer
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/hunt setup
.venv/bin/hunt demo create ./demo-library
.venv/bin/hunt ask "What is the Lantern pilot's weekly review schedule?" --demo-library ./demo-library
.venv/bin/hunt demo serve ./demo-library --port 8767
```

## Windows PowerShell

```powershell
git clone https://github.com/awpri/hunter-gatherer.git
Set-Location hunter-gatherer
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\hunt.exe setup
.\.venv\Scripts\hunt.exe demo create .\demo-library
.\.venv\Scripts\hunt.exe ask "What is the Lantern pilot's weekly review schedule?" --demo-library .\demo-library
.\.venv\Scripts\hunt.exe demo serve .\demo-library --port 8767
```

If `py` is unavailable, use the path to your Python 3.11+ executable. Calling the
environment's executables directly avoids changing PowerShell's activation
policy. The demo directory must be new or empty.

### With uv

If you already use [uv](https://docs.astral.sh/uv/getting-started/installation/),
replace the two environment/install commands above with these from the checkout:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python .
```

On Windows, the second command is:

```powershell
uv pip install --python .venv\Scripts\python.exe .
```

uv can download the requested Python if needed and installs packages into the
chosen environment. It handles dependency downloads and caching; h/g's setup
command reports which features are installed. See uv's
[environment](https://docs.astral.sh/uv/pip/environments/) and
[package](https://docs.astral.sh/uv/pip/packages/) guides.

## Ask your first question

The terminal command above returns quoted sources. Add `--json` for structured
output, or follow a citation with `hunt open`. See the [terminal guide](cli.md).

For the web interface, open [http://127.0.0.1:8767/](http://127.0.0.1:8767/).
Choose **Ask** in the graph's search bar and enter:

> What is the Lantern pilot's weekly review schedule?

Read the quotation and open its source title to check the passage in context.
Then try “Who owns the Lantern pilot now, and who owned it before?” The fictional
documents include revisions and a corrected memory. More examples are in the
[demo guide](demo.md).

## Optional features

Run `.venv/bin/hunt setup` (Windows: `.\.venv\Scripts\hunt.exe setup`) to see
installed capabilities and installation hints. `setup --json` provides the same
report for tools. It does not install packages, download models or open a library.

Install only the extras you need from the source checkout, using that same
environment. For example, to add original PDF page and region images:

```bash
.venv/bin/python -m pip install '.[document-view]'
```

Windows: `.\.venv\Scripts\python.exe -m pip install ".[document-view]"`.
With uv, use `uv pip install --python .venv/bin/python '.[document-view]'`
(substitute the Windows interpreter path there). Multiple extras can be combined
as `'.[semantic,document-view]'`.

| Extra | Adds | Needed for the fictional text demo? |
| --- | --- | --- |
| `document-view` | Original PDF page/region rendering. No OCR or model weights. | No |
| `semantic` | Sentence-transformer embeddings and their neural runtime. The selected model may need a separate download when used. | No |
| `qdrant` | Client for a configured Qdrant vector backend. | No |
| `document-recovery` | Optional PDF layout/OCR recovery; requires separate model setup. | No |
| `voice` | Local speech input dependencies; audio-device and model setup still apply. | No |
| `tts` | Local Piper speech output dependencies. | No |
| `tts-openai` | SDK for explicitly configured OpenAI speech output. | No |
| `desktop` | Native desktop window shell. | No |

The [dependency declarations](../pyproject.toml) also list optional storage
experiments and development tools. Installing an extra enables code; it does
not configure a provider, change a library's embedding model or make an
experimental component part of the supported demo workflow. A missing semantic
dependency is an installation error, not permission to substitute hashing for an
existing library's embeddings.

## Stop, restart and update

Press **Ctrl+C** in the server terminal to stop it. Keep `demo-library` and use the
same `demo serve` command to restart. Run `demo create` only for a new, empty
directory. If port 8767 is occupied, choose another port and use it in both the
browser and MCP client.

After updating a source checkout, reinstall it into the same dedicated environment
using the same install command and extras. Keep library data separate from the
environment; package installation is not a database migration. For problems,
check the [platform CI](https://github.com/awpri/hunter-gatherer/actions/workflows/packaged-demo.yml)
and include your OS, Python version and `hunt setup` report in a bug report.

## Keep installations separate

`hunt` is the h/g command. The Python package, settings and MCP tool names still
use `proharness` for compatibility. Install h/g and ProHarness into **different
virtual environments**; do not replace an existing installation.

The demo's `create`, `serve`, and `ask/open --demo-library` paths ignore ambient
environment configuration and `.env` files. Their database, preserved originals
and manifest live in the chosen directory. The deterministic hashing embeddings
demonstrate the workflow; they are not a semantic retrieval benchmark.

Ordinary `hunt serve` and normal configured-library commands use the inherited
settings. Personal-library deployment needs explicit storage, embedding and
provider configuration; the fictional demo does not configure it for you.

## Homebrew

There is no `brew install hunter-gatherer` formula yet. A maintained tap is a
sensible next distribution step after the first public versioned release. It
needs a stable source archive, verified checksums, pinned dependency resources,
an isolated Python environment and install/upgrade tests. Homebrew documents
[third-party taps](https://docs.brew.sh/Taps) and
[formula authoring](https://docs.brew.sh/Formula-Cookbook).

For now, Homebrew users can install **uv** with `brew install uv`, then follow the
source setup above; this installs the package manager, not h/g. That uv install
method is documented [by Astral](https://docs.astral.sh/uv/getting-started/installation/#homebrew).

Next: [use the library](usage.md), [use the terminal](cli.md), or
[connect your assistant](mcp.md).
