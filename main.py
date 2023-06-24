import argparse

from selenium_moppy import AnswerQuestionnaire


def set_args():
    parser = argparse.ArgumentParser(description="get email and password")
    parser.add_argument("--email", type=str, help="your email", required=True)
    parser.add_argument("--password", type=str, help="your password", required=True)

    args = parser.parse_args()

    return args


def main():
    args = set_args()
    questionnaires = AnswerQuestionnaire(email=args.email, password=args.password)
    questionnaires.save_cookie_as_pickle()
    questionnaires.answer()


main()
