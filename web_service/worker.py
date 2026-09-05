"""Single durable queue consumer."""

import logging
import os
import threading

from agents.coordinator import run_coordinator

from .settings import SETTINGS


class Worker:
    def __init__(self, store, runner=run_coordinator, poll_interval=None):
        self.store = store
        self.runner = runner
        self.poll_interval = SETTINGS.queue_poll_interval if poll_interval is None else poll_interval
        self.stop = threading.Event()
        self.wake = threading.Event()

    def once(self):
        task = self.store.claim()
        if task is None:
            return False
        try:
            result = self.runner(
                task["prompt"],
                self.store.history(task),
                on_progress=lambda text: self.store.update_progress(task["id"], text),
                on_message=lambda message: self.store.append_agent_message(task["id"], message),
            )
            self.store.complete(task["id"], result)
        except Exception:
            logging.exception("Task %s failed", task["id"])
            self.store.complete(task["id"], {
                "type": "final",
                "status": "failed",
                "summary": "Внутренняя ошибка выполнения. Подробности сохранены в журнале сервиса.",
            })
        return True

    def run(self):
        while not self.stop.is_set():
            self.wake.clear()
            try:
                worked = self.once()
            except Exception:
                logging.exception("Queue worker failed")
                os._exit(1)
            if not worked:
                self.wake.wait(self.poll_interval)
