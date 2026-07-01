# jira-to-harvest

Imports your Jira worklogs for a given month into Harvest as time entries. Public holidays are logged automatically. A browser window opens for Jira SSO login; no Jira credentials are stored.

## Requirements

- Python 3.11+
- A [Harvest personal access token](https://id.getharvest.com/developers) and your Harvest account ID

## Installation

```bash
pip install playwright requests
playwright install chromium
```

## Configuration

### Environment variables

Set these before running the script (or put them in a `.env` file loaded by your shell):

| Variable | Description |
|---|---|
| `HARVEST_TOKEN` | Harvest personal access token |
| `HARVEST_ACCOUNT_ID` | Harvest account ID (shown on the token page) |

### Project mapping (`project_mapping.json`)

Maps Jira project key prefixes to Harvest project and task names. The key is the prefix of the Jira issue key — for example `"DOM"` matches all issues like `DOM-123`.

- `DEFAULT` — fallback used for any Jira project not explicitly listed
- `HOLIDAY` — the Harvest project/task used for public holiday entries

```json
{
  "DEFAULT": {
    "harvest_project": "My Harvest Project",
    "harvest_task": "General"
  },
  "PROJ": {
    "harvest_project": "Another Harvest Project",
    "harvest_task": "Development"
  },
  "HOLIDAY": {
    "harvest_project": "Leave",
    "harvest_task": "Public Holiday"
  }
}
```

Project and task names are resolved against the Harvest API at startup. If a name is not found the script exits with the list of available options.

### Public holidays (`public_holidays.json`)

Lists public holiday dates by year. Holidays take precedence over Jira worklogs: if Jira has a worklog on a holiday it is ignored and a holiday entry is created instead. Each holiday is logged as 8 hours against the `HOLIDAY` mapping above.

```json
{
  "2026": [
    "2026-01-01",
    "2026-05-01"
  ]
}
```

Add a new year block when needed.

## Usage

```bash
# Import worklogs for a month (opens browser for Jira login)
python jira_to_harvest.py 2026-05

# Preview what would be imported without posting to Harvest
python jira_to_harvest.py 2026-05 --dry-run
```

## How it works

1. Harvest project/task names are resolved to IDs via the Harvest API.
2. A browser opens to `$JIRA_HOST` for SSO login. The script polls until authentication completes (up to 2 minutes).
3. All Jira issues with worklogs in the requested month are fetched. Worklogs on public holidays are skipped with an info message.
4. A holiday entry (8h) is added for each public holiday in the month.
5. Existing Harvest entries for the month are fetched and the daily total is capped at `$DAILY_LIMIT` hours. If new entries would exceed the cap they are scaled proportionally with a warning.
6. Entries are printed as a summary table. Without `--dry-run` they are posted to Harvest.
