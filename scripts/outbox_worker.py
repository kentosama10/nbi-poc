"""Background worker: delivers outbox events to the MongoDB timeline.

    python -m scripts.outbox_worker        (stop with Ctrl+C)
"""
import time

from app.timeline import deliver_pending


def main() -> None:
    print("Outbox worker running. Press Ctrl+C to stop.")
    while True:
        try:
            result = deliver_pending(limit=100)
            if result["delivered"] or result["failed"]:
                print(f"delivered={result['delivered']} failed={result['failed']}")
        except Exception as exc:          # keep the worker alive whatever happens
            print(f"Worker error: {type(exc).__name__}: {exc}")
        time.sleep(2)


if __name__ == "__main__":
    main()