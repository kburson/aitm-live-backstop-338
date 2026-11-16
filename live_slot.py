"""Read the actual GitHub schedule using fixed public, read-only production routes."""

from datetime import datetime, timedelta, timezone
import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PRODUCTION = "kburson/aitm"
REF = "refs/heads/trunk"
REPO_ID = 1450406435
WORKFLOW_PATH = ".github/workflows/scheduled-full-shadow.yml"
DURATION = timedelta(minutes=45)
DAY = timedelta(days=1)


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("time zone missing")
    return result.astimezone(timezone.utc)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def schedule_slot(now):
    slot = now.replace(hour=3, minute=47, second=0, microsecond=0)
    return slot if now >= slot else slot - DAY


def scheduled_route(workflow_id, slot):
    query = urlencode({"branch": "trunk", "event": "schedule",
                       "created": ">=" + (instant(slot) - DAY).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "per_page": 100})
    return f"actions/workflows/{workflow_id}/runs?{query}"


def observe(facts, scenario, now):
    """Refuse any stale target, non-current window or on-time scheduled run."""
    prod = scenario["production"]
    repo, branch, workflow = (facts[name] for name in ("repository", "branch", "workflow"))
    require(repo["id"] == prod["id"] == REPO_ID
            and repo["full_name"] == prod["repository"] == PRODUCTION
            and repo["private"] is False
            and repo["default_branch"] == branch["name"] == "trunk"
            and prod["ref"] == REF and branch["protected"] is True
            and branch["commit"]["sha"] == prod["sha"]
            and re.fullmatch(r"[0-9a-f]{40}", prod["sha"]),
            "production protected source moved or disagrees")
    require(workflow["id"] == scenario["scheduled_workflow_id"]
            and workflow["path"] == WORKFLOW_PATH and workflow["state"] == "active",
            "production scheduled workflow moved or disagrees")
    slot = instant(scenario["slot"])
    deadline = instant(scenario["deadline"])
    require(slot == schedule_slot(now) and deadline == slot + DURATION and deadline <= now,
            "admission does not identify the current elapsed slot")
    prior_late = []
    current_late = []
    seen = set()
    for run in facts["scheduled_runs"]:
        require(run["event"] == "schedule" and run["path"] == WORKFLOW_PATH
                and run["workflow_id"] == workflow["id"] and run["head_branch"] == "trunk"
                and run["repository"]["id"] == run["head_repository"]["id"] == REPO_ID
                and run["repository"]["full_name"] == run["head_repository"]["full_name"] == PRODUCTION
                and re.fullmatch(r"[0-9a-f]{40}", run["head_sha"])
                and type(run["id"]) is int and run["id"] > 0 and run["id"] not in seen
                and type(run["run_attempt"]) is int and run["run_attempt"] > 0,
                "native scheduled registration identity disagrees")
        seen.add(run["id"])
        created = instant(run["created_at"])
        require(slot - DAY <= created <= now, "native registration outside the requested interval")
        require(not slot <= created <= deadline, "real current slot has an on-time registration")
        if slot - DAY + DURATION < created < slot:
            prior_late.append(run["id"])
        elif deadline < created:
            current_late.append(run["id"])
    return {
        "status": "missed", "repository": PRODUCTION, "workflow_id": workflow["id"],
        "slot": scenario["slot"], "deadline": scenario["deadline"],
        "protected_current_sha": prod["sha"], "on_time_run_ids": [],
        "prior_slot_late_run_ids": sorted(prior_late), "current_slot_late_run_ids": sorted(current_late),
    }


def public_get(path):
    """Hosted runner deliberately sends no credential to the production API."""
    request = Request(f"https://api.github.com/repos/{PRODUCTION}/{path}".rstrip("/"), headers={
        "Accept": "application/vnd.github+json", "User-Agent": "aitm-338-shadow-recovery",
        "Cache-Control": "no-cache",
    })
    with urlopen(request, timeout=20) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
        require(response.status == 200 and len(raw) <= 8 * 1024 * 1024, "incomplete production response")
        return json.loads(raw)


def public_observation(scenario):
    w = scenario["scheduled_workflow_id"]
    facts = {"repository": public_get(""), "branch": public_get("branches/trunk"),
             "workflow": public_get(f"actions/workflows/{w}")}
    page = public_get(scheduled_route(w, scenario["slot"]))
    facts["scheduled_runs"] = page["workflow_runs"]
    require(page["total_count"] == len(facts["scheduled_runs"]) and page["total_count"] < 100,
            "public native scheduled listing incomplete")
    read_at = datetime.now(timezone.utc)
    return {"read_at": read_at.isoformat(), "facts": facts,
            "observation": observe(facts, scenario, read_at)}
