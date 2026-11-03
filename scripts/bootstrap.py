"""One-command setup: wait for the databases, then create tables, data, users, model.

    python -m scripts.bootstrap                 first-time setup (safe to rerun)
    python -m scripts.bootstrap --reload        wipe and reload the data (users survive)
    python -m scripts.bootstrap --demo-data     also run HIT checks on 200 applications
    python -m scripts.bootstrap --retrain       retrain the model even if one exists

Training takes 3 to 8 minutes the first time. ALL DATA IS SYNTHETIC.
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_FILE = ROOT / "ml" / "artifacts" / "hit_model.json"


def run(module: str, *args: str) -> None:
    """Run `python -m <module>` in the project root; stop if it fails."""
    print(f"\n>>> python -m {module} {' '.join(args)}".rstrip(), flush=True)
    result = subprocess.run([sys.executable, "-m", module, *args], cwd=ROOT)
    if result.returncode != 0:
        sys.exit(f"\nStep failed: {module} (exit code {result.returncode}). "
                 "Fix the problem above, then rerun this script.")


def wait_for_databases(tries: int = 36, pause: int = 5) -> None:
    """Poll the health check until all four databases answer (up to 3 minutes)."""
    for attempt in range(1, tries + 1):
        code = subprocess.run([sys.executable, "scripts/health_check.py"],
                              cwd=ROOT, capture_output=True).returncode
        if code == 0:
            print("All 4 databases are healthy.")
            return
        print(f"Waiting for the databases ({attempt}/{tries})...", flush=True)
        time.sleep(pause)
    sys.exit("The databases did not become healthy. Run: docker compose ps")


def main() -> None:
    reload_ = "--reload" in sys.argv
    demo = "--demo-data" in sys.argv
    retrain = "--retrain" in sys.argv

    if not (ROOT / ".env").exists():
        sys.exit("Missing .env. Run: Copy-Item .env.example .env   (macOS: cp .env.example .env)")

    wait_for_databases()
    run("scripts.init_db")
    run("scripts.generate_data")
    run("scripts.load_data", *(["--reload"] if reload_ else []))
    run("scripts.seed_users")

    if retrain or not MODEL_FILE.exists():
        print("\nTraining the model (3 to 8 minutes, it runs thousands of searches)...")
        run("scripts.train_model")
    else:
        print("\nModel already trained (use --retrain to redo it).")

    if demo:
        run("scripts.seed_hit_checks")

    print("\nSetup complete. Next: start the API, then the dashboard (see README.md).")


if __name__ == "__main__":
    main()