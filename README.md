# cf-automerge

Turn conda-forge bot automerge on or off across every feedstock you maintain, in one go.

## What it does

1. Finds your feedstocks through your conda-forge GitHub team memberships (each feedstock team is synced from `recipe-maintainers`). Archived repos and ones you can't push to are skipped.
2. Reads `conda-forge.yml` and the recipe in each, and checks whether the feedstock is safe to automerge (see below).
3. Prints a summary: current automerge setting, test runner found, dependency-update setting, and what will change.
4. Asks for confirmation, then commits the change **directly to the default branch** with `[ci skip] ***NO_CI***`, so no builds or uploads are triggered.

No PRs are opened and nothing is cloned. Comments and other keys in `conda-forge.yml` are preserved.

After it runs, new bot PRs (version bumps and migrations) merge automatically once CI is green. Bot PRs that were already open aren't affected; add the `automerge` label to those by hand if you want them merged.

## Safety checks

Automerge merges a bot PR as soon as CI is green, and the build is published right away. Green CI only means something if CI actually tests the package, so automerge is **only enabled where both of these hold**:

1. **The recipe runs upstream tests.** The recipe's test commands or test scripts call a real test runner (`pytest`, `pkg.test()`, `ctest`, `make check`, `cargo test`, `R CMD check`, ...). Recipes that only check imports and `pip check` are skipped.
2. **The bot updates dependencies.** `bot.inspection` in `conda-forge.yml` is `update-grayskull` or `update-all`. Without this, the bot bumps the version but leaves the requirements alone. Tests won't catch that: CI installs the newest version of every dependency, so a raised minimum version upstream goes unnoticed and users on older versions get broken installs.

Feedstocks that fail either check show as `SKIP`. Feedstocks that **already** have automerge on but fail the checks are flagged, and the script prints a ready-made `--mode off --only ...` command to turn them off.

Test detection is a heuristic that reads the recipe text. Check the `tests:` column in the dry-run output, and pass `--no-checks` if you know better for a specific feedstock (combine it with `--only`).

To make a feedstock pass check 2, add this to its `conda-forge.yml`, then rerun:

```yaml
bot:
  inspection: update-grayskull   # pure-python PyPI packages; or update-all
```

## Setup

You need `gh` (GitHub CLI), Python 3, and `ruamel.yaml`.

**conda (recommended)**

```bash
conda env create -f environment.yml
conda activate cf-automerge
```

**pip / Homebrew (macOS)**

```bash
brew install gh
python3 -m pip install --user -r requirements.txt
```

**Auth** (one time):

```bash
gh auth login              # GitHub.com, HTTPS, browser login
gh auth refresh -s read:org
```

`read:org` is needed to list your conda-forge teams.

> macOS has no `python` command by default, so use `python3`.

## Usage

Always dry-run first:

```bash
python3 cf_enable_automerge.py --dry-run
```

Then for real:

```bash
python3 cf_enable_automerge.py
```

| Option | What it does |
|---|---|
| `--mode true` | Automerge all bot PRs (default) |
| `--mode version` | Automerge only version-bump PRs |
| `--mode migration` | Automerge only migration PRs |
| `--mode off` | Remove automerge from `conda-forge.yml` |
| `--dry-run` | Show summary and diffs, push nothing |
| `--only foo-feedstock bar-feedstock` | Limit to specific feedstocks |
| `--no-checks` | Skip the safety checks (use with `--only`) |
| `-y` | Skip the confirmation prompt |
| `-j N` | Parallel scans (default 8) |

Example output:

```
  good-feedstock        off -> true        tests: pytest      deps: update-grayskull
  notests-feedstock     off  SKIP          tests: none        deps: update-all
  nodeps-feedstock      off  SKIP          tests: pytest      deps: default
  unsafe-feedstock      true  (no change)  tests: none        deps: default  <- automerge ON but no upstream tests; bot doesn't update deps

You maintain 4 feedstocks.
  Upstream tests:        2 yes, 2 no
  Bot updates deps:      2 yes, 2 no
  Current automerge:     off=3, true=1
  Will change to 'true': 1
  Skipped (checks fail): 2

1 feedstock(s) have automerge ON but fail the checks. To turn it off:
  python3 cf_enable_automerge.py --mode off --only unsafe-feedstock

Push to 1 feedstocks? [y/N]
```

## Notes

- Automerge relies on the `automerge.yml` workflow in the feedstock. Any feedstock rerendered in the last few years has it; older ones get it on their next rerender.
- Even with both checks passing, keep an eye on the first few automerged PRs. Test suites vary a lot in coverage.
- The script edits only `conda-forge.yml` (`bot.automerge`). It's safe to rerun: feedstocks already at the target setting are skipped.
- To undo, run with `--mode off`.
