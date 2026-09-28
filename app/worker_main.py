"""Run with python -m app.worker_main; no HTTP process is required."""
import signal
from . import worker, runtime, store


def main():
    runtime.validate()
    with runtime.QueueLeader() as leader:
        signal.signal(signal.SIGTERM,lambda *_:worker.STOP.set())
        signal.signal(signal.SIGINT,lambda *_:worker.STOP.set())
        thread=worker.start()
        try:
            while thread.is_alive() and not worker.STOP.is_set():
                leader.heartbeat()
                worker.STOP.wait(5)
        finally:
            worker.STOP.set()
            thread.join()
            with store.connect() as c:
                c.execute('UPDATE service_heartbeat SET updated=0 WHERE name=?',('worker',))


if __name__=='__main__':main()
