# Contributing to findr

Thank you for helping. Bug reports, measurements from real machines and pull requests are all welcome.

## Before you start
- **Never put real file names, paths, people or companies in issues, commits, code or tests.** Describe them generically ("a contract PDF in a downloads folder"). findr searches private files, and its repository must not leak any.
- Read [docs/LESSONS.md](docs/LESSONS.md) before a larger change. It lists what went wrong in the field and why.

## Setting up
```bash
git clone https://github.com/quantumaikr/findr && cd findr
bash install.sh            # Windows: powershell -ExecutionPolicy Bypass -File install.ps1
.venv/bin/python -m pytest -q tests --ignore=tests/test_e2e.py
```
The tests need no GPU and no models and take under a minute.

## Reporting a bug
Say what you did, what you expected and what happened, with the findr version, the operating system, whether the PC has a GPU, and about how many files are indexed. `findr doctor` prints most of this. For slow answers, `tools/field_check.py` gives numbers without file names.

## Changing how search ranks
A change to ranking needs numbers, because a change that helps one PC often hurts another.
- `tools/real_eval.py`: questions about your own files, written before any search. Prints counts and ranks only.
- `tools/stage_value.py` (with `STAGE_TRACE=1`): what each stage of a search changed and what it cost.
- `tools/lane_recall.py`: whether the answer is among the candidates at all.
- `tools/same_answers.py`: the same questions must give the same answers before and after a change that is not meant to alter them, and between two runs.

Report before and after on at least two different PCs or index kinds (a GPU and a CPU-only PC, for example). Do not tune on the questions you report.

## Pull requests
- Keep one purpose per pull request. Say why first, then what, then what you measured or tested, and what you did not check.
- New behaviour comes with a test. A new switch (environment variable) comes with a test and a default, and says in a comment where its value came from.
- Run the tests before you open the pull request. The Windows, macOS and Linux checks run on release pull requests (branches named `release-*`).
- Write code, comments and commit messages in English. User-facing text is Korean and English.

## Conduct and security
See the [code of conduct](CODE_OF_CONDUCT.md). Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## License
By contributing you agree that your contribution is licensed under the Apache License 2.0.
