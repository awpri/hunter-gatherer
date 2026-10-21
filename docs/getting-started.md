# Get started

Try h/g with a separate fictional library before connecting personal files.
This path uses local SQLite and a bundled web interface. You do not need a model
provider key, a running model server or Node.js.

You do need **Git and Python 3.11 or newer**. Package installation uses the internet
and includes substantial dependencies. The `document-view` extra below enables
original PDF page and region images; leave it out for a text-only installation.

## macOS and Linux

```bash
git clone https://github.com/awpri/hunter-gatherer.git
cd hunter-gatherer
python3 -m venv .venv
.venv/bin/python -m pip install '.[document-view]'
.venv/bin/proharness demo create ./demo-library
.venv/bin/proharness demo serve ./demo-library --port 8767
```

## Windows PowerShell

```powershell
git clone https://github.com/awpri/hunter-gatherer.git
Set-Location hunter-gatherer
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[document-view]"
.\.venv\Scripts\proharness.exe demo create .\demo-library
.\.venv\Scripts\proharness.exe demo serve .\demo-library --port 8767
```

If `py` is unavailable, use the path to your Python 3.11+ executable for the
environment-creation command. Calling the environment's executables directly
avoids needing to change PowerShell's activation policy.

Check the [current platform checks](https://github.com/awpri/hunter-gatherer/actions/workflows/packaged-demo.yml)
if installation or startup fails. Include your OS and Python version when
[reporting a problem](https://github.com/awpri/hunter-gatherer/issues/new/choose).

## Ask your first question

Open [http://127.0.0.1:8767/](http://127.0.0.1:8767/). In the graph's search bar,
choose **Ask** and enter:

> What is the Lantern pilot's weekly review schedule?

Read the quotation, open its source title and check the passage in context.
Then try:

> Who owns the Lantern pilot now, and who owned it before?

The fictional documents contain a revision and a corrected memory so you can
inspect both current and historical information. See the [demo guide](demo.md)
for all three practice questions.

## Stop and restart

Press **Ctrl+C** in the server terminal to stop it. Keep the `demo-library`
directory and use the same `demo serve` command to restart. Run `demo create`
only for a new, empty directory; it deliberately refuses an existing library.
If port 8767 is occupied, choose another port and use that port in your browser
and MCP client.

This dedicated launcher ignores ambient environment configuration and `.env`
files. Its database, preserved originals and manifest live in the chosen demo
directory. It uses deterministic hashing embeddings to demonstrate the workflow;
the demo is not a semantic retrieval benchmark.

## Keep installations separate

h/g retains the Python package, CLI and MCP names `proharness` for compatibility.
Use a dedicated virtual environment and do not install h/g and ProHarness in the
same environment. Ordinary `proharness serve` reads normal ProHarness settings;
it is **not** a substitute for this isolated `demo serve` command.

The demo is intended for fictional practice data. Personal-library deployment,
model configuration and optional runtime components need their own explicit
configuration; the demo does not configure them for you.

Next: [use the library](usage.md) or [connect your assistant](mcp.md).
