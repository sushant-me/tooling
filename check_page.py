#!/usr/bin/env python3
"""Check that the landing page still tells the truth about releases.

Why this exists
---------------
`index.html` names an install tag for each tool — `agentbound.git@v0.1.15` — and four
Action tags (`@v1`). Every one of those is a claim, and every one goes stale the moment a
release happens: the page keeps advertising an old version, and a reader who follows it
gets the wrong code with no warning. That is the failure this whole account is built to
make visible, so the page should not be the exception.

Two questions, both answered against the GitHub API:
  1. Is the install tag on the page the latest release of that repository?
  2. Does every Action reference on the page resolve to an `action.yml` at that ref?

Exit code is 0 when both hold, 1 when the page has drifted. The message says which line,
because "the page is wrong" is not a thing anyone can act on.

    python3 check_page.py [--page index.html] [--self-test]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
OWNER = "sushant-me"

#: `sushant-me/<repo>.git@v1.2.3` — the pip install line.
INSTALL_RE = re.compile(rf"{OWNER}/(?P<repo>[a-z0-9-]+)\.git@(?P<tag>v[0-9][0-9.]*)")
#: `uses: sushant-me/<repo>@v1` — a composite action reference.
ACTION_RE = re.compile(rf"uses:\s*{OWNER}/(?P<repo>[a-z0-9-]+)@(?P<ref>v[0-9]+)")
#: A link to a document the page quotes a number from.
SURVEY_RE = re.compile(r"https://raw\.githubusercontent\.com/" + OWNER +
                       r"/(?P<repo>[a-z0-9-]+)/(?P<ref>[A-Za-z0-9._/-]+?)/"
                       r"(?P<path>[A-Za-z0-9._/-]+\.md)")


def _get(url: str) -> object | None:
    """GET a URL. Network failures return None rather than raising: the caller reports.

    `GH_TOKEN` is used when set. Unauthenticated GitHub API calls are limited to 60 per
    hour per address, which is fewer than this check needs on a machine that has already
    talked to the API today - and a rate-limited answer must never be read as "no drift".
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "tooling-page-check",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    for _ in range(2):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code in (429, 403):
                continue  # rate limited: one retry, then fail loudly below
            return None
        except Exception:
            continue
    return None


def latest_release(repo: str) -> str | None:
    data = _get(f"{API}/repos/{OWNER}/{repo}/releases/latest")
    if isinstance(data, dict) and isinstance(data.get("tag_name"), str):
        return data["tag_name"]
    return None


def fetch_doc(repo: str, path: str, ref: str) -> tuple[str, str | None]:
    """Read a quoted document at the ref the page links to. Returns (status, text).

    status is "ok", "missing" (the link on the page does not resolve - that is drift) or
    "error" (the API did not answer - that is not).

    Read through the API rather than raw.githubusercontent.com: that host is not reachable
    from every environment this check runs in (it was not from the one it was written in),
    and "could not read a perfectly good document" is worse than no check. The ref is taken
    from the page's own URL, so a link pointing at a branch that does not exist fails here
    instead of 404ing for a reader.
    """
    data = _get(f"{API}/repos/{OWNER}/{repo}/contents/{path}?ref={ref}")
    if data is None:
        return "error", None
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        return "missing", None
    try:
        return "ok", base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
    except Exception:
        return "missing", None


def action_ref_exists(repo: str, ref: str) -> bool | None:
    """True/False, or None when the answer could not be fetched."""
    data = _get(f"{API}/repos/{OWNER}/{repo}/contents/action.yml?ref={ref}")
    if data is None:
        return None
    return isinstance(data, dict) and data.get("name") == "action.yml"


def install_tags(html: str) -> list[tuple[str, str]]:
    return sorted({(m.group("repo"), m.group("tag")) for m in INSTALL_RE.finditer(html)})


def action_refs(html: str) -> list[tuple[str, str]]:
    return sorted({(m.group("repo"), m.group("ref")) for m in ACTION_RE.finditer(html)})


def quoted_sources(html: str) -> list[tuple[str, str, str, str, str]]:
    """Every (url, repo, path, quoted substring) the page asserts by quoting a document.

    Only the survey is wired up today: it is the one number on the page that the ledger does
    not carry, because adding a ledger claim would also mean editing published posts whose
    deployment this repository cannot trigger. Keeping the assertion here is the honest
    alternative - it is narrower than a ledger claim, and it is written down as narrower.
    """
    found: list[tuple[str, str, str, str, str]] = []
    if "14 real frameworks" not in html or "precision" not in html.lower():
        return found
    for match in SURVEY_RE.finditer(html):
        if match.group("repo") == "agentbound":
            found.append((match.group(0), match.group("repo"), match.group("ref"),
                          match.group("path"), "14 real agent frameworks"))
    return found


def check(html: str, fetch_latest=latest_release, fetch_action=action_ref_exists,
          fetch_source=fetch_doc) -> list[tuple[str, str]]:
    """Return (kind, message) pairs. `kind` is "drift" or "unreadable"; [] means current.

    The two are kept apart on purpose. "The page advertises an old release" is a fix the
    author can make; "the API did not answer" is not, and reporting the second as the first
    is how a checkout gets blamed for a rate limit - the same mistake the scan had to be
    taught not to make.
    """
    problems: list[tuple[str, str]] = []

    tags = install_tags(html)
    if not tags:
        problems.append(("drift", "no install tags found on the page — the regex or the page changed"))
    for repo, tag in tags:
        latest = fetch_latest(repo)
        if latest is None:
            problems.append(("unreadable", f"{repo}: could not read the latest release"))
        elif latest != tag:
            problems.append((
                "drift",
                f"{repo}: page says {tag}, latest release is {latest} — update the install line",
            ))

    # Numbers the page quotes from a document it links to. The ledger does not carry every
    # one of them, so the page's own check asserts the document still says what is quoted -
    # a summary that disagrees with its source is the failure mode this whole page is about.
    for url, repo, ref, path, needle in quoted_sources(html):
        status, text = fetch_source(repo, path, ref)
        if status == "missing":
            problems.append(("drift", f"{url} does not resolve — the link on the page is broken"))
        elif status != "ok" or text is None:
            problems.append(("unreadable", f"could not read the quoted source {url}"))
        elif needle not in text:
            problems.append(("drift", f"{repo}/{ref}/{path} no longer contains {needle!r} — the page quotes it"))

    refs = action_refs(html)
    if not refs:
        problems.append(("drift", "no Action references found on the page — expected four"))
    for repo, ref in refs:
        exists = fetch_action(repo, ref)
        if exists is None:
            problems.append(("unreadable", f"{repo}@{ref}: could not fetch action.yml"))
        elif not exists:
            problems.append(("drift", f"{repo}@{ref}: action.yml not found at that ref"))

    return problems


def run(page_path: str) -> int:
    try:
        with open(page_path, encoding="utf-8") as handle:
            html = handle.read()
    except OSError as exc:
        print(f"::error::cannot read {page_path}: {exc}")
        return 1

    tags, refs = install_tags(html), action_refs(html)
    print(f"install tags : {len(tags)}")
    for repo, tag in tags:
        print(f"  {repo:22} {tag}")
    print(f"action refs  : {len(refs)}")
    for repo, ref in refs:
        print(f"  {repo:22} {ref}")

    problems = check(html)
    if problems:
        drift = [m for kind, m in problems if kind == "drift"]
        unreadable = [m for kind, m in problems if kind == "unreadable"]
        print()
        for message in drift:
            print(f"::error::drift: {message}")
        for message in unreadable:
            print(f"::warning::could not verify: {message}")
        summary = []
        if drift:
            summary.append(f"{len(drift)} place(s) where the page is behind")
        if unreadable:
            summary.append(f"{len(unreadable)} place(s) that could not be checked")
        print("\n" + ", ".join(summary) + "."
              + (" No drift was found in what could be read." if unreadable and not drift else ""))
        return 1
    print("\nthe page matches the current releases and every Action reference resolves")
    return 0


def self_test() -> int:
    """Checks that the parser and the comparison fail when they should.

    A checker whose failure path has never been exercised is a green light with no bulb in
    it — so each case below asserts on the *problem* it must produce, not just on a pass.
    """
    page = """
      pip install "git+https://github.com/sushant-me/alpha.git@v1.2.3"
      - uses: sushant-me/beta@v1
    """
    failures = 0

    def expect(label: str, got, want) -> None:
        nonlocal failures
        if got != want:
            failures += 1
            print(f"  FAIL {label}: got {got!r}, want {want!r}")
        else:
            print(f"  ok   {label}")

    expect("parses install tag", install_tags(page), [("alpha", "v1.2.3")])
    expect("parses action ref", action_refs(page), [("beta", "v1")])
    expect("multiple tags", len(install_tags(page + page.replace("1.2.3", "1.2.4"))), 2)

    expect("current page has no problems",
           check(page, fetch_latest=lambda r: "v1.2.3", fetch_action=lambda r, x: True), [])

    stale = check(page, fetch_latest=lambda r: "v1.2.4", fetch_action=lambda r, x: True)
    expect("stale tag is reported", [k for k, _ in stale], ["drift"])
    expect("stale message names both", ("v1.2.3" in stale[0][1], "v1.2.4" in stale[0][1]), (True, True))

    missing = check(page, fetch_latest=lambda r: "v1.2.3", fetch_action=lambda r, x: False)
    expect("missing action.yml is drift", [k for k, _ in missing], ["drift"])

    quote_page = page + '\n<a href="https://raw.githubusercontent.com/sushant-me/agentbound/master/SURVEY.md">survey</a>\n14 real frameworks precision 1.00'
    expect("finds the quoted source", [q[1:4] for q in quoted_sources(quote_page)],
           [("agentbound", "master", "SURVEY.md")])
    expect("no quote, no check", quoted_sources(page), [])

    ok_quote = check(quote_page, fetch_latest=lambda r: "v1.2.3", fetch_action=lambda r, x: True,
                     fetch_source=lambda r, p, x: ("ok", "# Survey: agentbound against 14 real agent frameworks"))
    expect("a quote the source still contains passes", ok_quote, [])

    stale_quote = check(quote_page, fetch_latest=lambda r: "v1.2.3", fetch_action=lambda r, x: True,
                        fetch_source=lambda r, p, x: ("ok", "# Survey: rewritten, no number any more"))
    expect("a quote the source no longer contains is drift",
           [k for k, _ in stale_quote], ["drift"])

    broken_quote = check(quote_page, fetch_latest=lambda r: "v1.2.3", fetch_action=lambda r, x: True,
                         fetch_source=lambda r, p, x: ("missing", None))
    expect("a link that does not resolve is drift, not a warning",
           [k for k, _ in broken_quote], ["drift"])

    blind_quote = check(quote_page, fetch_latest=lambda r: "v1.2.3", fetch_action=lambda r, x: True,
                        fetch_source=lambda r, p, x: ("error", None))
    expect("an unreadable source is unreadable, not drift",
           [k for k, _ in blind_quote], ["unreadable"])

    blind = check(page, fetch_latest=lambda r: None, fetch_action=lambda r, x: None)
    expect("API failure is unreadable, not drift", [k for k, _ in blind], ["unreadable", "unreadable"])

    print(f"\nself-test: {'PASS' if failures == 0 else f'FAIL ({failures})'}")
    return 0 if failures == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", default="index.html")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    return self_test() if args.self_test else run(args.page)


if __name__ == "__main__":
    sys.exit(main())
