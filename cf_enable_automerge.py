#!/usr/bin/env python3
"""Turn conda-forge bot automerge on/off for every feedstock you maintain.

Automerge is only enabled on feedstocks that are safe for it:
  1. the recipe runs upstream tests (pytest, ctest, make check, ...), not just
     import checks, so a green CI actually means something
  2. the bot updates dependencies itself (bot.inspection: update-grayskull or
     update-all), so version bumps don't merge with stale requirements

Finds feedstocks via your conda-forge team memberships, scans each, prints a
summary, then (after confirmation) commits the conda-forge.yml change straight
to the default branch with [ci skip] so no builds fire.

Requires: gh (authed, with read:org scope), ruamel.yaml
Usage:
    python3 cf_enable_automerge.py --dry-run          # scan + summary + diffs
    python3 cf_enable_automerge.py                    # automerge: true
    python3 cf_enable_automerge.py --mode version     # only version-bump PRs
    python3 cf_enable_automerge.py --mode off         # remove automerge
    python3 cf_enable_automerge.py --only foo-feedstock bar-feedstock
    python3 cf_enable_automerge.py --no-checks        # skip safety checks
"""
import argparse
import base64
import difflib
import json
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from io import StringIO

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

ORG = "conda-forge"
CFG = "conda-forge.yml"
OFF = object()  # sentinel: remove the key
DEP_UPDATE_MODES = ("update-grayskull", "update-all")

RECIPE_FILES = ("meta.yaml", "recipe.yaml")
SCRIPT_EXT = (".py", ".sh", ".bat", ".R", ".r", ".pl")
RUNNERS = [
    ("pytest", r"\bpy\.?test\b"),
    ("unittest", r"\bunittest\b"),
    ("nose", r"\bnosetests\b"),
    ("ctest", r"\bctest\b"),
    ("make check", r"\b(?:make|ninja)\s+(?:check|test)\b"),
    ("meson test", r"\bmeson\s+test\b"),
    ("cargo test", r"\bcargo\s+test\b"),
    ("go test", r"\bgo\s+test\b"),
    ("R tests", r"\bR\s+CMD\s+check\b|\btestthat\b|\btest_check\b"),
    ("pkg.test()", r"\b\w+\.test\("),
]


# ---------- github ----------

def gh(*args, stdin=None, lines=False):
    r = subprocess.run(["gh", "api", *args], capture_output=True, text=True, input=stdin)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    if lines:
        return [l for l in r.stdout.splitlines() if l.strip()]
    return json.loads(r.stdout) if r.stdout.strip() else None


def get_file(repo, path):
    d = gh(f"/repos/{ORG}/{repo}/contents/{path}")
    return base64.b64decode(d["content"]).decode(errors="replace"), d["sha"]


def my_feedstocks():
    teams = gh("--paginate", "/user/teams",
               "--jq", f'.[] | select(.organization.login=="{ORG}") | .slug', lines=True)
    repos = set()
    for t in teams:
        repos.update(gh("--paginate", f"/orgs/{ORG}/teams/{t}/repos", "--jq",
                        '.[] | select(.name|endswith("-feedstock")) '
                        '| select(.archived|not) | select(.permissions.push) | .name',
                        lines=True))
    return sorted(repos)


# ---------- upstream test detection ----------

def strip_comment(line):
    return re.sub(r"(^|\s)#.*$", "", line)


def command_lines(recipe_text):
    """Lines under `commands:` (meta.yaml) or `script:` (recipe.yaml) keys."""
    lines, out, i = recipe_text.splitlines(), [], 0
    while i < len(lines):
        m = re.match(r"^\s*(?:-\s+)?(commands|script)\s*:\s*(.*)$", lines[i])
        if not m:
            i += 1
            continue
        ind = m.start(1)
        if m.group(2) and not m.group(2).startswith(("|", ">")):
            out.append(m.group(2))
        i += 1
        while i < len(lines):
            l = lines[i]
            if not l.strip():
                i += 1
                continue
            li = len(l) - len(l.lstrip())
            if li > ind or (li == ind and l.lstrip().startswith("- ")):
                out.append(l)
                i += 1
            else:
                break
    return out


def find_runners(text):
    text = "\n".join(strip_comment(l) for l in text.splitlines())
    return {name for name, rx in RUNNERS if re.search(rx, text)}


def upstream_tests(repo):
    """Set of test runners found in the recipe (empty = imports-only / none)."""
    listing = gh(f"/repos/{ORG}/{repo}/contents/recipe")
    found = set()
    for f in listing:
        n = f["name"]
        if f["type"] != "file":
            continue
        if n in RECIPE_FILES:
            found |= find_runners("\n".join(command_lines(get_file(repo, f"recipe/{n}")[0])))
        elif n.startswith(("run_test", "test")) and n.endswith(SCRIPT_EXT):
            found |= find_runners(get_file(repo, f"recipe/{n}")[0])
    return found


# ---------- conda-forge.yml ----------

def yaml_rt():
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    return y


def show(v):
    return "off" if v in (None, False) else ("true" if v is True else str(v))


def patch(text, mode):
    """Return (current_automerge, inspection, new_text). new_text None = no change."""
    y = yaml_rt()
    data = (y.load(text) if text.strip() else None) or CommentedMap()
    bot = data.get("bot")
    current = bot.get("automerge") if bot else None
    inspection = bot.get("inspection") if bot else None

    if mode is OFF:
        if current in (None, False):
            return current, inspection, None
        del bot["automerge"]
        if not bot:
            del data["bot"]
    else:
        if current == mode:
            return current, inspection, None
        if bot is None:
            bot = data["bot"] = CommentedMap()
        bot["automerge"] = mode

    buf = StringIO()
    y.dump(data, buf)
    return current, inspection, buf.getvalue()


# ---------- main flow ----------

def scan(repo, mode, checks):
    path = f"/repos/{ORG}/{repo}/contents/{CFG}"
    try:
        try:
            old, sha = get_file(repo, CFG)
        except RuntimeError as e:
            if "404" not in str(e):
                raise
            old, sha = "", None
        current, inspection, new = patch(old, mode)
        r = dict(repo=repo, path=path, old=old, new=new, sha=sha, current=current,
                 inspection=inspection, deps_ok=inspection in DEP_UPDATE_MODES,
                 runners=None, problems=[])
        if checks:
            r["runners"] = upstream_tests(repo)
            if not r["runners"]:
                r["problems"].append("no upstream tests")
            if not r["deps_ok"]:
                r["problems"].append("bot doesn't update deps")
        return r
    except Exception as e:
        return dict(repo=repo, error=str(e))


def apply(r, msg):
    body = {"message": msg, "content": base64.b64encode(r["new"].encode()).decode()}
    if r["sha"]:
        body["sha"] = r["sha"]
    gh("-X", "PUT", r["path"], "--input", "-", stdin=json.dumps(body))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["true", "version", "migration", "off"], default="true",
                   help="target bot.automerge (default: true = all bot PRs; off = remove it)")
    p.add_argument("--dry-run", action="store_true", help="show diffs, change nothing")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before pushing")
    p.add_argument("--only", nargs="+", metavar="FEEDSTOCK", help="limit to these repos")
    p.add_argument("--no-checks", action="store_true",
                   help="enable automerge even without upstream tests / dep updates")
    p.add_argument("-j", "--jobs", type=int, default=8, help="parallel scans (default 8)")
    a = p.parse_args()
    mode = {"true": True, "off": OFF}.get(a.mode, a.mode)
    target = "off" if mode is OFF else show(mode)
    msg = ("Disable bot automerge" if mode is OFF else f"Set bot automerge: {target}") \
        + " [ci skip] ***NO_CI***"
    checks = not a.no_checks

    print("Finding your feedstocks...", flush=True)
    repos = a.only or my_feedstocks()
    print(f"Scanning {len(repos)} feedstocks...\n", flush=True)
    with ThreadPoolExecutor(a.jobs) as ex:
        results = list(ex.map(lambda r: scan(r, mode, checks), repos))

    errors = [r for r in results if "error" in r]
    ok = [r for r in results if "error" not in r]

    # gate: don't enable automerge where checks fail
    skipped = []
    if mode is not OFF and checks:
        for r in ok:
            if r["new"] is not None and r["problems"]:
                r["new"], r["skipped"] = None, True
                skipped.append(r)
    todo = [r for r in ok if r["new"] is not None]
    unsafe_on = [r for r in ok if checks and r["problems"]
                 and r["current"] not in (None, False) and not (mode is OFF and r["new"])]

    w = max([len(r["repo"]) for r in results] + [10]) + 2
    for r in results:
        if "error" in r:
            print(f"  {r['repo']:{w}s} ERROR: {r['error']}")
            continue
        cur = show(r["current"])
        if r.get("skipped"):
            state = f"{cur}  SKIP"
        elif r["new"] is None:
            state = f"{cur}  (no change)"
        else:
            state = f"{cur} -> {target}"
        info = ""
        if checks:
            tests = ", ".join(sorted(r["runners"])) or "none"
            info = f"tests: {tests:18s} deps: {r['inspection'] or 'default'}"
        flag = "  <- automerge ON but " + "; ".join(r["problems"]) if r in unsafe_on else ""
        print(f"  {r['repo']:{w}s} {state:22s} {info}{flag}")

    current = Counter(show(r["current"]) for r in ok)
    print(f"\nYou maintain {len(repos)} feedstocks.")
    if checks:
        print(f"  Upstream tests:        {sum(bool(r['runners']) for r in ok)} yes, "
              f"{sum(not r['runners'] for r in ok)} no")
        print(f"  Bot updates deps:      {sum(r['deps_ok'] for r in ok)} yes, "
              f"{sum(not r['deps_ok'] for r in ok)} no")
    print("  Current automerge:     " + ", ".join(f"{k}={v}" for k, v in sorted(current.items())))
    print(f"  Will change to '{target}': {len(todo)}")
    if skipped:
        print(f"  Skipped (checks fail): {len(skipped)}")
    if errors:
        print(f"  Errors:                {len(errors)}")

    if unsafe_on:
        print(f"\n{len(unsafe_on)} feedstock(s) have automerge ON but fail the checks. To turn it off:")
        print("  python3 cf_enable_automerge.py --mode off --only "
              + " ".join(r["repo"] for r in unsafe_on))
    if skipped and any("bot doesn't update deps" in r["problems"] and r["runners"] for r in skipped):
        print("\nTip: feedstocks with tests but no dep updates can be fixed by adding to conda-forge.yml:\n"
              "  bot:\n    inspection: update-grayskull   # pure-python PyPI packages\n"
              "  (or update-all), then rerun this script.")

    if not todo:
        print("\nNothing to do.")
        return
    if a.dry_run:
        print("\n--- diffs ---")
        for r in todo:
            sys.stdout.writelines(difflib.unified_diff(
                r["old"].splitlines(True), r["new"].splitlines(True),
                f"{r['repo']}/{CFG}", f"{r['repo']}/{CFG}"))
        print("\nDry run, nothing pushed.")
        return
    if not a.yes and input(f"\nPush to {len(todo)} feedstocks? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        return

    failed = 0
    for i, r in enumerate(todo, 1):
        try:
            apply(r, msg)
            status = "done"
        except Exception as e:
            status, failed = f"ERROR: {e}", failed + 1
        print(f"  [{i}/{len(todo)}] {r['repo']:{w}s} {status}")
    print(f"\n{len(todo) - failed} updated, {failed} failed.")
    sys.exit(1 if failed or errors else 0)


if __name__ == "__main__":
    main()
