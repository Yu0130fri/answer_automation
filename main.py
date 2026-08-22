import argparse
import os
from pathlib import Path

from selenium_moppy import AnswerQuestionnaire

# Prefer python-dotenv if available
try:
    from dotenv import load_dotenv
    _HAS_DOTENV = True
except Exception:
    _HAS_DOTENV = False


def _load_dotenv(path: Path) -> dict:
    """Simple fallback .env loader used only if python-dotenv not available."""
    env = {}
    if not path.exists():
        return env
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                env[key] = val
    except Exception:
        return {}
    return env


def set_args():
    parser = argparse.ArgumentParser(description="get email and password")
    parser.add_argument("--email", type=str, help="your email", required=False)
    parser.add_argument("--password", type=str, help="your password", required=False)
    parser.add_argument(
        "--threads",
        type=int,
        default=3,
        help="parallel Chrome workers to run in background (default: 3)",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="discard saved in-progress state and start fresh",
    )

    args = parser.parse_args()

    # Prefer python-dotenv when available
    if _HAS_DOTENV:
        # load environment from .env into os.environ
        try:
            load_dotenv()
        except Exception:
            pass

    # Load .env fallback if python-dotenv is not present
    env_path = Path(".env")
    env = _load_dotenv(env_path) if not _HAS_DOTENV else {}

    # Priority: CLI args > environment variables > .env fallback
    if not args.email:
        args.email = os.environ.get("EMAIL") or env.get("EMAIL")
    if not args.password:
        args.password = os.environ.get("PASSWORD") or env.get("PASSWORD")

    # If still missing, fail
    if not args.email or not args.password:
        parser.error("email and password must be provided via --email/--password, environment variables, or a .env file with EMAIL and PASSWORD entries")

    return args


def main():
    args = set_args()
    questionnaires = AnswerQuestionnaire(email=args.email, password=args.password)
    if args.reset_state:
        questionnaires.clear_resume_state()

    try:
        if args.threads > 1:
            questionnaires.run_parallel(worker_count=args.threads)
            return
        questionnaires.run()
    except KeyboardInterrupt:
        print("中断しました。次回実行時に自動で再開されます。", flush=True)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
