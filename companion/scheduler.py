"""Bounded, fair chat scheduling: parallel chats, serial turns per chat."""
from collections import deque
import queue
import threading


class ChatScheduler:
    def __init__(self, run, parallel=3, capacity=32):
        if not 1 <= parallel <= 8:
            raise ValueError('Parallel chats must be between 1 and 8')
        self.run, self.capacity = run, capacity
        self.pending, self.busy = deque(), set()
        self.condition = threading.Condition()
        self._unfinished = 0
        self.stopping = False
        self.threads = [threading.Thread(target=self._worker, daemon=True) for _ in range(parallel)]
        for thread in self.threads:
            thread.start()

    @property
    def unfinished_tasks(self):
        with self.condition:
            return self._unfinished

    def full(self):
        with self.condition:
            return self._unfinished >= self.capacity

    def put_nowait(self, job):
        with self.condition:
            if self.stopping or self._unfinished >= self.capacity:
                raise queue.Full
            self.pending.append(job)
            self._unfinished += 1
            self.condition.notify_all()

    def _worker(self):
        while True:
            with self.condition:
                while True:
                    job = next((job for job in self.pending if job[2] not in self.busy), None)
                    if job is not None:
                        self.pending.remove(job)
                        self.busy.add(job[2])
                        break
                    if self.stopping and not self.pending:
                        return
                    self.condition.wait()
            try:
                self.run(job)
            finally:
                with self.condition:
                    self.busy.remove(job[2])
                    self._unfinished -= 1
                    self.condition.notify_all()

    def join(self):
        with self.condition:
            self.condition.wait_for(lambda: self._unfinished == 0)

    def shutdown(self):
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
        for thread in self.threads:
            thread.join(timeout=3)
