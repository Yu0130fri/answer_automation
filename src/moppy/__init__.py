# Domain package facade for the Moppy automation domain.
# This file exposes the domain-level API while keeping legacy implementation
# in selenium_moppy for compatibility.
from selenium_moppy.answer_automation import AnswerQuestionnaire

__all__ = ["AnswerQuestionnaire"]
