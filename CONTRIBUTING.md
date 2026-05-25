# Contributing

This repository is primarily a personal research project and CV portfolio
piece. It is public so the design choices, methodology, and engineering
workflow can be inspected, not because I am actively soliciting feature
contributions.

That said, the following are welcome:

- **Bug reports** — if something is broken, or a citation is wrong, please
  open an issue. Include the file, line, and what you'd expect.
- **Methodology corrections** — if a paper is misquoted, a formula is wrong,
  or a citation should be added, please open an issue. Methodology fidelity
  matters to me.
- **Doc fixes** — typos, broken links, unclear explanations: pull requests
  welcome.

What I am unlikely to merge:

- Large feature additions or new ML methods. The repository is scoped as a
  research prototype; expanding scope would dilute it.
- Refactors that don't fix a concrete bug. The current structure reflects the
  layered design I documented in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Local development

```powershell
git clone https://github.com/arora-vaibhav/equity-research-factor-forecast-claude.git
cd equity-research-factor-forecast-claude
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
```

On macOS / Linux: `source venv/bin/activate` and forward-slash paths.

## Code style

- Follow the existing style in the file you are editing.
- Tests come first (see [`docs/HOW_TO_RUN_TESTS.md`](docs/HOW_TO_RUN_TESTS.md)).
- No new dependencies without a strong reason; pin versions in `requirements.txt`.

## Issue templates

There are no formal templates. A clear title, a minimal reproduction, and the
version of Python / OS you are on is enough.
