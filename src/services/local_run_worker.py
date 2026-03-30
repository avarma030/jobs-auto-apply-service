from __future__ import annotations

import asyncio
import os
import uuid

from loguru import logger

from src.database.db import Database
from src.services.run_executor import execute_next_run


class LocalRunWorker:
    def __init__(
        self,
        db: Database,
        *,
        poll_interval_seconds: float = 5.0,
        worker_id: str | None = None,
    ) -> None:
        self._db = db
        self._poll_interval_seconds = poll_interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._wakeup = asyncio.Event()
        self._stopped = asyncio.Event()
        self.worker_id = worker_id or f"local:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped.clear()
        self._wakeup.set()
        self._task = asyncio.create_task(self._run_loop())

    def notify(self) -> None:
        self._wakeup.set()

    async def stop(self) -> None:
        self._stopped.set()
        self._wakeup.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run_loop(self) -> None:
        logger.info(f"[LocalRunWorker] Worker started ({self.worker_id})")
        try:
            while not self._stopped.is_set():
                drained = 0
                while not self._stopped.is_set():
                    ran = await execute_next_run(db=self._db, worker_id=self.worker_id)
                    if not ran:
                        break
                    drained += 1
                if self._stopped.is_set():
                    break
                if drained == 0:
                    try:
                        await asyncio.wait_for(
                            self._wakeup.wait(),
                            timeout=self._poll_interval_seconds,
                        )
                    except asyncio.TimeoutError:
                        pass
                    self._wakeup.clear()
                else:
                    self._wakeup.clear()
                    self._wakeup.set()
        finally:
            logger.info(f"[LocalRunWorker] Worker stopped ({self.worker_id})")
