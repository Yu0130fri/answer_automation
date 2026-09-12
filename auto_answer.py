import argparse
import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# ensure src/ is on sys.path so domain package is used
ROOT = Path(__file__).absolute().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from moppy import AnswerQuestionnaire

parser = argparse.ArgumentParser(description="Answer Automation (convenience wrapper)")
parser.add_argument("--email", type=str, help="your email", required=False)
parser.add_argument("--password", type=str, help="your password", required=False)
parser.add_argument("--threads", type=int, default=3, help="number of parallel workers")
parser.add_argument("--reset-state", action="store_true", help="discard saved state and start fresh")

if __name__ == "__main__":
    args = parser.parse_args()

    load_dotenv()

    # Prefer CLI arguments over values loaded from .env.
    if not args.email:
        args.email = os.environ.get("EMAIL")
    if not args.password:
        args.password = os.environ.get("PASSWORD")

    if not args.email or not args.password:
        parser.error("email and password must be provided via args or environment variables (.env)")

    aq = AnswerQuestionnaire(email=args.email, password=args.password)
    if args.reset_state:
        aq.clear_resume_state()

    if args.threads > 1:
        aq.run_parallel(worker_count=args.threads)
    else:
        aq.run()
