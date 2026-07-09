"""Run the autofix/answer pipeline on exactly ONE issue, without starting the
poller or merge-watcher. For live testing a single issue.

    PYTHONPATH=. .venv/bin/python scripts/run_issue.py <issue-number> [source-id]
"""

from __future__ import annotations

import asyncio
import json
import sys

from open_claw.config import load_sources
from open_claw.db import tasks
from open_claw.exec import run
from open_claw.github.source import GithubSource
from open_claw.llm import make_llm


async def main() -> None:
    if len(sys.argv) < 2:
        print("usage: run_issue.py <issue-number> [source-id]", file=sys.stderr)
        sys.exit(2)
    issue_no = int(sys.argv[1])
    source_id = sys.argv[2] if len(sys.argv) > 2 else None

    cfgs = load_sources()
    cfg = next((c for c in cfgs if c.id == source_id), cfgs[0]) if source_id else cfgs[0]
    print(f"source={cfg.id} repo={cfg.repo} container={cfg.container.image if cfg.container else 'host'}")

    res = await run(
        "gh",
        ["issue", "view", str(issue_no), "--repo", cfg.repo, "--json", "number,title,body,url,labels"],
    )
    data = json.loads(res.stdout)
    labels = [l["name"] for l in data.get("labels", [])]
    lb = cfg.labels
    kind = (
        "bug" if lb.bug in labels
        else "feature" if lb.feature in labels
        else "question" if lb.question in labels
        else None
    )
    if not kind:
        print(f"issue #{issue_no} has no routing label ({labels}) — nothing to do", file=sys.stderr)
        sys.exit(1)

    meta = {
        "sourceId": cfg.id, "repo": cfg.repo, "issue": issue_no, "kind": kind,
        "url": data["url"], "body": data.get("body") or "",
    }
    task = tasks.create("github", data["title"], cfg.clone_dir, meta)
    print(f"→ task #{task.id}: {kind} #{issue_no} — {data['title']}")

    src = GithubSource(cfg, queue=None, llm=make_llm())  # type: ignore[arg-type]
    try:
        result = await src.run(task)
        tasks.finish_done(task.id, result)
        print(f"\n✓ DONE\n{result}")
    except Exception as err:
        import traceback

        tasks.finish_failed(task.id, "".join(traceback.format_exception(err)))
        print(f"\n✗ FAILED\n{err}", file=sys.stderr)
        raise


if __name__ == "__main__":
    asyncio.run(main())
