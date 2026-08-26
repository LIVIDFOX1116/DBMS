"""
Manual promotion gate: dev -> main.

Run this by hand when you believe dev is ready to become the new main:

    python scripts/promote_to_main.py

It runs the full test suite against the current working tree and only
pushes to origin/main -- as a fast-forward of the local dev branch -- if
every test passes. Nothing touches main on failure. This is deliberately
not automated in CI: promoting to main is a human decision, confirming
it's safe to do so is not.
"""
import subprocess
import sys


def run(cmd):
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd)


def main():
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if branch != "dev":
        print(f"[ABORT] You're on '{branch}', not 'dev'. Checkout dev first -- "
              "this promotes dev's current commit to main, not whatever branch "
              "happens to be checked out.")
        sys.exit(1)

    status = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=True,
    ).stdout
    if status.strip():
        print("[ABORT] Working tree has uncommitted changes. Commit or stash them "
              "first -- the test run must reflect exactly what gets pushed.")
        sys.exit(1)

    print("Running test suite against dev before allowing a push to main...\n")
    result = run([sys.executable, "-m", "pytest", "tests/", "-v"])
    if result.returncode != 0:
        print("\n[ABORT] Test suite failed. main was NOT touched.")
        sys.exit(result.returncode)

    print("\n[OK] All tests passed. Pushing dev -> origin/main (fast-forward only)...\n")
    push = run(["git", "push", "origin", "dev:main"])
    if push.returncode != 0:
        print("\n[FAILED] Push rejected -- main has commits dev doesn't, so this "
              "isn't a fast-forward. Merge/rebase dev on top of main and rerun.")
        sys.exit(push.returncode)

    print("\nmain is now up to date with dev.")


if __name__ == "__main__":
    main()
