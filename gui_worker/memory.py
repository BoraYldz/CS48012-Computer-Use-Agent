# Long-term lessons that keep updating across tasks (Lecture 3 Reflexion memory, Lecture 5
# memory write policies: conflict resolution, scoring, consolidation).
#
# How a lesson changes over time:
#   added      - the reflector found a grounded cause in a failed or wasteful run
#   used       - the planner says it applied the lesson; the run's outcome updates its counters
#   revised    - the reflector rewrote it, citing evidence (old text kept in its history)
#   contradicted - a run's screenshots showed the lesson was wrong (one vote, never an instant delete)
#   archived   - by rule: contradicted twice, or followed by failure in most of >= 3 uses;
#                or by hand. Archived lessons leave the prompt but can be restored.
# Counters are updated by code from run outcomes, never by the model's opinion.
"""Self-updating lesson memory shared by all tasks of this worker installation.

    python memory.py list [--archived]   show lessons with their use counters
    python memory.py show L1a2b3c        one lesson with its full change history
    python memory.py archive L1a2b3c     take a lesson out of the prompt (reversible)
    python memory.py restore L1a2b3c     bring an archived lesson back
    python memory.py revert L1a2b3c      undo the last revision of a lesson
    python memory.py flush --all         delete lessons permanently (or give ids)
"""
import argparse
import json
import os
import re
import time
import uuid
from pathlib import Path

# GUI_WORKER_MEMORY lets tests (or a second installation) use a separate file.
MEMORY_PATH = Path(os.environ.get("GUI_WORKER_MEMORY",
                                  Path(__file__).resolve().parent / "memory" / "lessons.json"))
# Active lessons are prompt text, so they stay bounded; the archive keeps the most recent 200.
MAX_ACTIVE = 30
MAX_ARCHIVED = 200
MAX_LESSON_CHARS = 300
# At most this many lessons go into one prompt: all general ones plus those for the current app.
MAX_IN_PROMPT = 20
# Retirement rules (deterministic): see archive_by_rule.
MIN_USES_FOR_RULE = 3
FAILURE_RATIO_LIMIT = 0.67
CONTRADICTION_LIMIT = 2
# Run outcomes remembered so the same run is never counted twice (an external check may arrive
# later and turn a "success" into a "failure").
MAX_LEDGER = 300
OUTCOMES = ("success", "mixed", "failure")


def _path(path):
    return Path(path) if path else MEMORY_PATH


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# One line of printable text: lessons are prompt text, so no control characters or newlines.
def clean_text(text, limit=MAX_LESSON_CHARS):
    text = " ".join(str(text).split())
    text = "".join(c for c in text if c.isprintable())
    return text[:limit].strip()


def _words(text):
    return set(re.findall(r"\w{3,}", str(text).lower()))


# Near-duplicates (same idea, slightly different wording) count as the same lesson.
def similar(a, b, threshold=0.75):
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= threshold


def _new_stats():
    return {"applied": 0, "helped": 0, "failed_after": 0, "contradicted": 0, "last_used": None}


# Older files (version 1) had no status, counters or history.
def _normalize(item):
    item.setdefault("status", "active")
    item.setdefault("app", None)
    item.setdefault("created_at", _now())
    item.setdefault("updated_at", item["created_at"])
    item.setdefault("source", {})
    stats = item.setdefault("stats", _new_stats())
    for key, value in _new_stats().items():
        stats.setdefault(key, value)
    item.setdefault("history", [])
    return item


def _load(path=None):
    try:
        data = json.loads(_path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    lessons = [_normalize(item) for item in data.get("lessons", [])
               if isinstance(item, dict) and isinstance(item.get("text"), str) and item.get("id")]
    ledger = data.get("runs") if isinstance(data.get("runs"), dict) else {}
    return {"version": 2, "lessons": lessons, "runs": ledger}


# Write to a temporary file first, so a crash cannot leave half a JSON file behind.
def _save(data, path=None):
    archived = [item for item in data["lessons"] if item["status"] == "archived"]
    if len(archived) > MAX_ARCHIVED:
        drop = {item["id"] for item in sorted(archived, key=lambda i: i["updated_at"])[:len(archived) - MAX_ARCHIVED]}
        data["lessons"] = [item for item in data["lessons"] if item["id"] not in drop]
    if len(data["runs"]) > MAX_LEDGER:
        keep = sorted(data["runs"], key=lambda run_id: data["runs"][run_id].get("at", ""))[-MAX_LEDGER:]
        data["runs"] = {run_id: data["runs"][run_id] for run_id in keep}
    target = _path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)


def _log(item, op, by, detail=None):
    entry = {"at": _now(), "op": op, "by": by}
    if detail:
        entry.update(detail)
    item["history"] = (item["history"] + [entry])[-20:]
    item["updated_at"] = entry["at"]


def _find(data, lesson_id):
    return next((item for item in data["lessons"] if item["id"] == lesson_id), None)


# Laplace-smoothed share of uses that were followed by success; 0.5 for an unused lesson.
def score(item):
    stats = item["stats"]
    return (stats["helped"] + 1) / (stats["applied"] + 2) - 0.25 * stats["contradicted"]


def load_lessons(path=None, include_archived=False):
    """Lesson records, oldest first. Archived ones only when asked."""
    return [item for item in _load(path)["lessons"] if include_archived or item["status"] == "active"]


def lesson_texts(path=None):
    """Text of every active lesson."""
    return [item["text"] for item in load_lessons(path)]


def select_for_prompt(lessons, context_text, limit=MAX_IN_PROMPT):
    """General lessons plus those whose app appears in the goal or window title, best first.

    Lecture 5: only page into working memory what the current task can use.
    """
    words = _words(context_text)
    chosen = [item for item in lessons
              if not item.get("app") or (_words(str(item["app"]).replace("/", " ")) & words)]
    chosen.sort(key=lambda item: (-score(item), item.get("created_at", "")))
    return chosen[:limit]


def add_lesson(text, *, app=None, source=None, by="reflector", path=None):
    """Store one lesson. Returns {"status": "added" | "duplicate" | "full" | "empty", ...}.

    When active memory is full, the weakest lesson that has proven itself bad by rule is archived
    to make room; if none qualifies, the new lesson is refused.
    """
    text = clean_text(text)
    if not text:
        return {"status": "empty"}
    data = _load(path)
    active = [item for item in data["lessons"] if item["status"] == "active"]
    for item in active:
        if similar(item["text"], text):
            return {"status": "duplicate", "lesson": item}
    if len(active) >= MAX_ACTIVE:
        weak = [item for item in active if item["stats"]["applied"] >= MIN_USES_FOR_RULE]
        if not weak:
            return {"status": "full", "limit": MAX_ACTIVE}
        victim = min(weak, key=score)
        victim["status"] = "archived"
        _log(victim, "archived", "rule", {"reason": "memory full; lowest-scoring proven lesson"})
    item = _normalize({"id": "L" + uuid.uuid4().hex[:6], "text": text,
                       "app": clean_text(app, 60) or None if app else None,
                       "source": source or {}})
    _log(item, "added", by, {"run_id": (source or {}).get("run_id")})
    data["lessons"].append(item)
    _save(data, path)
    return {"status": "added", "lesson": item}


def revise_lesson(lesson_id, text, *, evidence=None, by="reflector", run_id=None, path=None):
    """Rewrite an active lesson; the previous text stays in its history (python memory.py revert)."""
    text = clean_text(text)
    data = _load(path)
    item = _find(data, lesson_id)
    if item is None or item["status"] != "active":
        return {"status": "unknown_lesson"}
    if not text or text == item["text"]:
        return {"status": "unchanged", "lesson": item}
    _log(item, "revised", by, {"previous_text": item["text"], "evidence": clean_text(evidence or "", 300),
                               "run_id": run_id})
    item["text"] = text
    # New wording gets a fresh chance: earlier contradictions were about the old text.
    item["stats"]["contradicted"] = 0
    _save(data, path)
    return {"status": "revised", "lesson": item}


def contradict_lesson(lesson_id, *, evidence=None, by="reflector", run_id=None, path=None):
    """Record evidence that a lesson is wrong. Archived only after CONTRADICTION_LIMIT such runs."""
    data = _load(path)
    item = _find(data, lesson_id)
    if item is None or item["status"] != "active":
        return {"status": "unknown_lesson"}
    if any(entry.get("op") == "contradicted" and entry.get("run_id") == run_id and run_id
           for entry in item["history"]):
        return {"status": "already_counted", "lesson": item}
    item["stats"]["contradicted"] += 1
    _log(item, "contradicted", by, {"evidence": clean_text(evidence or "", 300), "run_id": run_id})
    status = "contradicted"
    if item["stats"]["contradicted"] >= CONTRADICTION_LIMIT:
        item["status"] = "archived"
        _log(item, "archived", "rule", {"reason": f"contradicted in {item['stats']['contradicted']} runs"})
        status = "archived"
    _save(data, path)
    return {"status": status, "lesson": item}


# Rule-based retirement from counters; returns the ids archived.
def _archive_by_rule(data):
    archived = []
    for item in data["lessons"]:
        stats = item["stats"]
        if item["status"] != "active" or stats["applied"] < MIN_USES_FOR_RULE:
            continue
        if stats["failed_after"] / stats["applied"] >= FAILURE_RATIO_LIMIT:
            item["status"] = "archived"
            _log(item, "archived", "rule", {"reason": f"followed by failure in {stats['failed_after']} "
                                                      f"of {stats['applied']} uses"})
            archived.append(item["id"])
    return archived


def record_run(run_id, applied_ids, outcome, path=None):
    """Update use counters after a run. Safe to call again for the same run with a new outcome.

    outcome: "success" (finished, no wasted actions), "mixed" (finished with wasted actions),
    "failure" (unfinished, or an external check failed), or None to skip.
    """
    if outcome not in OUTCOMES or not run_id:
        return {"status": "skipped"}
    data = _load(path)
    known = {item["id"] for item in data["lessons"]}
    applied = sorted({i for i in applied_ids or () if i in known})
    previous = data["runs"].get(run_id)
    if previous and previous.get("outcome") == outcome:
        return {"status": "already_recorded", "applied": previous.get("applied", [])}
    for lesson_id in (previous or {}).get("applied", applied):
        item = _find(data, lesson_id)
        if item is None:
            continue
        stats = item["stats"]
        if previous:
            # Undo the earlier outcome of this same run before applying the new one.
            stats["helped"] -= previous["outcome"] == "success"
            stats["failed_after"] -= previous["outcome"] == "failure"
        else:
            stats["applied"] += 1
            stats["last_used"] = _now()
        stats["helped"] += outcome == "success"
        stats["failed_after"] += outcome == "failure"
    data["runs"][run_id] = {"at": _now(), "outcome": outcome,
                            "applied": (previous or {}).get("applied", applied)}
    archived = _archive_by_rule(data)
    _save(data, path)
    return {"status": "recorded", "applied": data["runs"][run_id]["applied"], "archived": archived}


def set_status(lesson_id, status, *, reason=None, by="person", path=None):
    data = _load(path)
    item = _find(data, lesson_id)
    if item is None:
        return {"status": "unknown_lesson"}
    if item["status"] == status:
        return {"status": "unchanged", "lesson": item}
    item["status"] = status
    if status == "active":
        # A restored lesson starts its rule counters again; its history keeps what happened.
        item["stats"].update(contradicted=0, failed_after=0, helped=0, applied=0)
    _log(item, "restored" if status == "active" else "archived", by, {"reason": reason} if reason else None)
    _save(data, path)
    return {"status": status, "lesson": item}


def revert_lesson(lesson_id, path=None):
    """Undo the most recent revision of a lesson."""
    data = _load(path)
    item = _find(data, lesson_id)
    revisions = [entry for entry in (item or {}).get("history", []) if entry.get("op") == "revised"]
    if item is None or not revisions:
        return {"status": "nothing_to_revert"}
    _log(item, "reverted", "person", {"previous_text": item["text"]})
    item["text"] = revisions[-1]["previous_text"]
    _save(data, path)
    return {"status": "reverted", "lesson": item}


def flush_lessons(ids=None, path=None):
    """Delete the given lessons (active or archived), or all when ids is None. Not reversible."""
    data = _load(path)
    before = len(data["lessons"])
    data["lessons"] = [] if ids is None else [item for item in data["lessons"] if item["id"] not in set(ids)]
    removed = before - len(data["lessons"])
    if removed:
        _save(data, path)
    return removed


def _describe(item):
    stats = item["stats"]
    app = f" [{item['app']}]" if item.get("app") else ""
    return (f"{item['id']}{app} used {stats['applied']}x, helped {stats['helped']}, "
            f"failed after {stats['failed_after']}, contradicted {stats['contradicted']}: {item['text']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--archived", action="store_true")
    for name in ("show", "archive", "restore", "revert"):
        commands.add_parser(name).add_argument("id")
    flush = commands.add_parser("flush")
    flush.add_argument("ids", nargs="*")
    flush.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if args.command == "list":
        lessons = load_lessons(include_archived=True)
        active = [item for item in lessons if item["status"] == "active"]
        print(f"{len(active)}/{MAX_ACTIVE} active lessons in {MEMORY_PATH}")
        for item in active:
            print(" ", _describe(item))
        archived = [item for item in lessons if item["status"] == "archived"]
        if args.archived:
            print(f"{len(archived)} archived:")
            for item in archived:
                reason = next((e.get("reason") for e in reversed(item["history"]) if e.get("op") == "archived"), "")
                print(" ", _describe(item), f"(archived: {reason})")
        elif archived:
            print(f"({len(archived)} archived; add --archived to see them)")
    elif args.command == "show":
        item = next((i for i in load_lessons(include_archived=True) if i["id"] == args.id), None)
        print(json.dumps(item, indent=2, ensure_ascii=False) if item else "unknown lesson")
    elif args.command == "archive":
        print(set_status(args.id, "archived", reason="by hand")["status"])
    elif args.command == "restore":
        print(set_status(args.id, "active", reason="by hand")["status"])
    elif args.command == "revert":
        print(revert_lesson(args.id)["status"])
    else:
        if not args.all and not args.ids:
            parser.error("flush needs lesson ids or --all")
        print("removed", flush_lessons(None if args.all else args.ids))


if __name__ == "__main__":
    main()
