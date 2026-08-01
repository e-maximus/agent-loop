"""Entry point: build the GitHub sources, start the queue + pollers + merge
watchers, and run until interrupted. Single asyncio process on the local machine.
"""

from __future__ import annotations

import asyncio
import signal

from .config import load_sources, settings
from .db import tasks
from .github.source import GithubSource
from .llm import make_llm
from .logging import create_logger
from .queue import Queue
from .version import BUILD

log = create_logger("main")


async def main() -> None:
    log.info(f"agent-loop {BUILD} starting…")
    log.info(f"provider: deepseek, model: {settings.deepseek_model}, max turns: {settings.agent_max_turns}")

    orphans = tasks.reset_orphans()
    if orphans:
        log.warn(f"re-queued {orphans} interrupted task(s) to retry from the start")

    source_cfgs = load_sources()
    llm = make_llm()

    # The queue dispatches each task to the source instance that created it.
    sources: dict[str, GithubSource] = {}

    async def runner(task) -> str | None:
        meta = task.meta_dict()
        src = sources.get(meta.get("sourceId"))
        if src is None:
            raise RuntimeError(f"task #{task.id}: no source for sourceId={meta.get('sourceId')}")
        return await src.run(task)

    queue = Queue(runner, concurrency=1)
    sources = {cfg.id: GithubSource(cfg, queue, llm) for cfg in source_cfgs}

    queue.start()
    for s in sources.values():
        await s.start()
    log.info(f"started {len(sources)} source(s): {', '.join(f'{s.id}(github)' for s in sources.values())}")

    queue.poke()  # pick up anything already queued (e.g. before a restart)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    await stop_event.wait()

    log.info("shutting down…")
    for s in sources.values():
        s.stop()
    await queue.stop()


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
