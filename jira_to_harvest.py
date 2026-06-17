#!/usr/bin/env python3
"""
Jira → Harvest time entry importer.

Opens a browser for Jira SSO login, fetches worklogs for a given month,
then creates matching Harvest time entries.

Usage:
    python jira_to_harvest.py 2026-05
    python jira_to_harvest.py 2026-05 --dry-run

Required env vars:
    HARVEST_TOKEN       Harvest personal access token
    HARVEST_ACCOUNT_ID  Harvest account ID

Install deps:
    pip install playwright requests
    playwright install chromium
"""

import argparse
import calendar
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import APIRequestContext, sync_playwright

_HERE = Path(__file__).parent
load_dotenv(_HERE / ".env")

JIRA_HOST = os.environ["JIRA_HOST"]
HARVEST_API = os.environ.get("HARVEST_API", "https://api.harvestapp.com/v2")
HARVEST_USER_AGENT = os.environ.get("HARVEST_USER_AGENT", "JiraToHarvestImport")
DAILY_HOUR_LIMIT = float(os.environ.get("DAILY_HOUR_LIMIT", "8.0"))


def load_public_holidays() -> set[date]:
    path = _HERE / "config/public_holidays.json"
    raw: dict[str, list[str]] = json.loads(path.read_text())
    return {date.fromisoformat(d) for dates in raw.values() for d in dates}


PUBLIC_HOLIDAYS: set[date] = load_public_holidays()


def holidays_in_range(start: date, end: date) -> list[date]:
    return sorted(d for d in PUBLIC_HOLIDAYS if start <= d <= end)


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------

def harvest_session(token: str, account_id: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Harvest-Account-Id": account_id,
            "Content-Type": "application/json",
            "User-Agent": HARVEST_USER_AGENT,
        }
    )
    return s


def _harvest_paginate(session: requests.Session, url: str, key: str, **params) -> list[dict]:
    results: list[dict] = []
    page = 1
    while True:
        resp = session.get(url, params={**params, "page": page, "per_page": 100})
        if not resp.ok:
            sys.exit(f"Harvest API error {resp.status_code} on GET {url}: {resp.text}")
        resp.raise_for_status()
        data = resp.json()
        results.extend(data[key])
        if data.get("next_page") is None:
            break
        page += 1
    return results


def resolve_project_mapping(session: requests.Session) -> dict[str, tuple[int, int]]:
    path = _HERE / "config/project_mapping.json"
    raw: dict[str, dict] = json.loads(path.read_text())

    assignments = _harvest_paginate(
        session, f"{HARVEST_API}/users/me/project_assignments", "project_assignments"
    )
    projects_by_name: dict[str, tuple[int, dict[str, int]]] = {}
    for pa in assignments:
        p = pa["project"]
        tasks_by_name = {ta["task"]["name"]: ta["task"]["id"] for ta in pa.get("task_assignments", [])}
        projects_by_name[p["name"]] = (p["id"], tasks_by_name)

    mapping: dict[str, tuple[int, int]] = {}
    for key, entry in raw.items():
        if key == "_comment":
            continue
        project_name = entry["harvest_project"]
        task_name = entry["harvest_task"]

        if project_name not in projects_by_name:
            sys.exit(f"Harvest project not found: {project_name!r}")
        project_id, tasks_by_name = projects_by_name[project_name]

        task_id = tasks_by_name.get(task_name)
        if task_id is None:
            available = ", ".join(tasks_by_name)
            sys.exit(f"Task {task_name!r} not found in project {project_name!r}. Available: {available}")

        mapping[key] = (project_id, task_id)

    return mapping


def harvest_project_task(issue_key: str, mapping: dict[str, tuple[int, int]]) -> tuple[int, int]:
    prefix = issue_key.split("-")[0]
    return mapping.get(prefix, mapping["DEFAULT"])


def fetch_existing_entries(
    session: requests.Session, start: date, end: date
) -> tuple[dict[date, float], set[tuple]]:
    entries = _harvest_paginate(
        session,
        f"{HARVEST_API}/time_entries",
        "time_entries",
        **{"from": start.isoformat(), "to": end.isoformat()},
    )
    hours: dict[date, float] = defaultdict(float)
    existing: set[tuple] = set()
    for e in entries:
        d = date.fromisoformat(e["spent_date"])
        hours[d] += e["hours"]
        existing.add((d, e["project"]["id"], e["task"]["id"], e.get("notes") or ""))
    return dict(hours), existing


def cap_entries_to_daily_limit(
    entries: list[dict], existing_hours: dict[date, float]
) -> list[dict]:
    by_date: dict[date, list[dict]] = defaultdict(list)
    for e in entries:
        by_date[e["spent_date"]].append(e)

    result: list[dict] = []
    for day, day_entries in sorted(by_date.items()):
        already = existing_hours.get(day, 0.0)
        remaining = max(0.0, DAILY_HOUR_LIMIT - already)
        new_total = sum(e["hours"] for e in day_entries)

        if already >= DAILY_HOUR_LIMIT:
            print(f"  WARNING {day}: already {already:.2f}h in Harvest — skipping {new_total:.2f}h of new entries")
            continue

        if new_total > remaining:
            scale = remaining / new_total
            print(
                f"  WARNING {day}: {already:.2f}h existing + {new_total:.2f}h new "
                f"would exceed {DAILY_HOUR_LIMIT}h — scaling new entries by {scale:.2f}"
            )
            for e in day_entries:
                result.append({**e, "hours": round(e["hours"] * scale, 2)})
        else:
            result.extend(day_entries)

    return result


def harvest_create_entry(
    session: requests.Session,
    project_id: int,
    task_id: int,
    spent_date: date,
    hours: float,
    notes: str,
) -> tuple[int, dict]:
    body: dict = {
        "project_id": project_id,
        "task_id": task_id,
        "spent_date": spent_date.isoformat(),
        "hours": round(hours, 2),
    }
    if notes:
        body["notes"] = notes
    resp = session.post(f"{HARVEST_API}/time_entries", json=body)
    return resp.status_code, resp.json()


# ---------------------------------------------------------------------------
# Jira — all calls via playwright APIRequestContext (shares browser session)
# ---------------------------------------------------------------------------

def jira_get_current_user(api: APIRequestContext) -> str:
    resp = api.get(f"{JIRA_HOST}/rest/api/2/myself")
    assert resp.ok, f"GET /myself failed: {resp.status} {resp.text()}"
    data = resp.json()
    return data.get("name") or data.get("emailAddress", "")


def jira_search_issues_with_worklogs(
    api: APIRequestContext, username: str, start: date, end: date
) -> list[dict]:
    jql = (
        f'worklogAuthor = "{username}" '
        f'AND worklogDate >= "{start.isoformat()}" '
        f'AND worklogDate <= "{end.isoformat()}"'
    )
    issues: list[dict] = []
    start_at = 0
    page_size = 100

    while True:
        resp = api.get(
            f"{JIRA_HOST}/rest/api/2/search",
            params={
                "jql": jql,
                "fields": "summary",
                "startAt": str(start_at),
                "maxResults": str(page_size),
            },
        )
        assert resp.ok, f"GET /search failed: {resp.status} {resp.text()}"
        data = resp.json()
        issues.extend(data["issues"])
        if start_at + page_size >= data["total"]:
            break
        start_at += page_size

    return issues


def jira_get_worklogs(
    api: APIRequestContext,
    issue_key: str,
    username: str,
    start: date,
    end: date,
) -> list[dict]:
    resp = api.get(f"{JIRA_HOST}/rest/api/2/issue/{issue_key}/worklog")
    assert resp.ok, f"GET /worklog for {issue_key} failed: {resp.status} {resp.text()}"

    entries = []
    for wl in resp.json().get("worklogs", []):
        if wl.get("author", {}).get("name", "") != username:
            continue
        wl_date = date.fromisoformat(wl["started"][:10])
        if start <= wl_date <= end:
            entries.append(
                {
                    "date": wl_date,
                    "hours": wl["timeSpentSeconds"] / 3600,
                    "issue_key": issue_key,
                }
            )
    return entries


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_month(value: str) -> tuple[date, date]:
    try:
        year, month = map(int, value.split("-"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"Expected YYYY-MM, got: {value!r}")
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import Jira worklogs into Harvest for a given month."
    )
    parser.add_argument("month", help="Month to import (YYYY-MM), e.g. 2026-05")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch from Jira and print entries without posting to Harvest.",
    )
    args = parser.parse_args()

    try:
        start, end = parse_month(args.month)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))

    harvest_token = os.environ.get("HARVEST_TOKEN", "")
    harvest_account_id = os.environ.get("HARVEST_ACCOUNT_ID", "")
    missing = [k for k, v in {"HARVEST_TOKEN": harvest_token, "HARVEST_ACCOUNT_ID": harvest_account_id}.items() if not v]
    if missing:
        sys.exit(f"Missing required env vars: {', '.join(missing)}")

    harvest = harvest_session(harvest_token, harvest_account_id)
    print("Resolving Harvest project/task names …")
    jira_to_harvest = resolve_project_mapping(harvest)
    print("Project mapping resolved.\n")

    all_entries: list[dict] = []
    session_path = _HERE / ".jira_session.json"

    # Step 1 — browser login + Jira API calls
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)

        # Try cached session first.
        cached = session_path.exists()
        context = browser.new_context(
            storage_state=str(session_path) if cached else None
        )

        authenticated = False
        if cached:
            resp = context.request.get(f"{JIRA_HOST}/rest/api/2/myself")
            if resp.ok:
                print("Using cached Jira session.\n")
                authenticated = True
            else:
                print("Cached Jira session expired — opening browser for login …")
                context.close()
                session_path.unlink(missing_ok=True)
                context = browser.new_context()

        if not authenticated:
            page = context.new_page()
            if not cached:
                print("Opening browser for Jira login …")
            page.goto(f"{JIRA_HOST}/login.jsp")
            print("Please log in to Jira in the browser window.")
            print("Waiting until authentication is complete …\n")

            for _ in range(60):
                resp = context.request.get(f"{JIRA_HOST}/rest/api/2/myself")
                if resp.ok:
                    break
                page.wait_for_timeout(2_000)
            else:
                sys.exit("Timed out waiting for Jira login (2 minutes).")

            context.storage_state(path=str(session_path))
            print("Login complete. Session cached for future runs.\n")

        print("Fetching data from Jira …\n")

        api = context.request
        username = jira_get_current_user(api)
        print(f"Logged in as: {username}")
        print(f"Fetching worklogs for [{start} → {end}] …\n")

        issues = jira_search_issues_with_worklogs(api, username, start, end)
        print(f"Found {len(issues)} issue(s) with worklogs.\n")

        for issue in issues:
            key = issue["key"]
            summary = issue["fields"]["summary"]
            for wl in jira_get_worklogs(api, key, username, start, end):
                if wl["date"] in PUBLIC_HOLIDAYS:
                    print(f"  INFO  {wl['date']}  {key} logged in Jira on a public holiday — using holiday entry instead")
                    continue
                project_id, task_id = harvest_project_task(key, jira_to_harvest)
                all_entries.append(
                    {
                        "spent_date": wl["date"],
                        "project_id": project_id,
                        "task_id": task_id,
                        "hours": wl["hours"],
                        "notes": f"{key} - {summary}",
                    }
                )

        browser.close()

    # Add public holiday entries for the month.
    holiday_project_id, holiday_task_id = jira_to_harvest["HOLIDAY"]
    for hday in holidays_in_range(start, end):
        all_entries.append(
            {
                "spent_date": hday,
                "project_id": holiday_project_id,
                "task_id": holiday_task_id,
                "hours": 8.0,
                "notes": "Public holiday",
            }
        )

    all_entries.sort(key=lambda e: e["spent_date"])

    if not all_entries:
        print("No worklogs found — nothing to import.")
        return

    # Deduplicate against entries already in Harvest, then apply 8h/day cap.
    print("Checking existing Harvest entries …")
    existing_hours, existing_sigs = fetch_existing_entries(harvest, start, end)

    deduped: list[dict] = []
    for e in all_entries:
        sig = (e["spent_date"], e["project_id"], e["task_id"], e.get("notes", ""))
        if sig in existing_sigs:
            print(f"  SKIP  {e['spent_date']}  {e['notes']} (already in Harvest)")
        else:
            deduped.append(e)
    all_entries = deduped

    all_entries = cap_entries_to_daily_limit(all_entries, existing_hours)
    print()

    if not all_entries:
        print("All entries were dropped by the daily cap — nothing to import.")
        return

    # Step 2 — print summary
    print(f"{'Date':<12} {'Hours':>6}  Notes")
    print("-" * 70)
    for e in all_entries:
        print(f"{e['spent_date'].isoformat():<12} {e['hours']:>6.2f}h  {e['notes']}")
    print(f"\nTotal: {len(all_entries)} entries")

    if args.dry_run:
        print("\nDry run — nothing posted to Harvest.")
        return

    # Step 3 — post to Harvest
    print()
    created = failed = 0

    for entry in all_entries:
        status, body = harvest_create_entry(
            harvest,
            entry["project_id"],
            entry["task_id"],
            entry["spent_date"],
            entry["hours"],
            entry["notes"],
        )
        tag = "OK  " if status == 201 else "FAIL"
        print(f"{tag}  {entry['spent_date']}  {entry['hours']:5.2f}h  {entry['notes']}")
        if status != 201:
            msg = body.get("message") or body.get("error") or str(body)
            print(f"      HTTP {status}: {msg}")
            failed += 1
        else:
            created += 1

        time.sleep(0.2)  # stay within Harvest's 100 req/15 s rate limit

    print(f"\nDone — {created} created, {failed} failed.\n")


if __name__ == "__main__":
    main()
