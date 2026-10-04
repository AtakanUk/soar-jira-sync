from __future__ import annotations

from clients.jira_client import JiraClient
from models.jira_models import JiraTransition


class TransitionService:
    def __init__(self, jira: JiraClient) -> None:
        self.jira = jira

    def find_closing_transition(self, issue_key: str) -> JiraTransition | None:
        transitions = self.jira.get_transitions(issue_key)
        for transition in transitions:
            if transition.to_category == "done":
                return transition
        closed_names = set(self.jira.closed_status_names)
        for transition in transitions:
            if transition.to_status.lower() in closed_names:
                return transition
        for transition in transitions:
            if any(keyword in transition.name.lower() for keyword in ("close", "done", "resolve")):
                return transition
        return None

    def find_transition_for_status(
        self, issue_key: str, target_status: str
    ) -> JiraTransition | None:
        target = target_status.lower()
        for transition in self.jira.get_transitions(issue_key):
            if transition.to_status.lower() == target or transition.name.lower() == target:
                return transition
        return None

    def close_issue(self, issue_key: str) -> bool:
        transition = self.find_closing_transition(issue_key)
        if not transition:
            return False
        self.jira.transition_issue(issue_key, transition.id)
        return True

    def transition_to(self, issue_key: str, target_status: str) -> bool:
        transition = self.find_transition_for_status(issue_key, target_status)
        if not transition:
            return False
        self.jira.transition_issue(issue_key, transition.id)
        return True
