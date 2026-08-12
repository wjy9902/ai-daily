from types import SimpleNamespace

from ai_daily.site_trust import is_trusted_issue


def issue(login: str, labels: list[str], body: str) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(login=login),
        labels=[SimpleNamespace(name=label) for label in labels],
        body=body,
    )


def test_owner_is_always_trusted() -> None:
    assert is_trusted_issue(issue("owner", [], "anything"), "owner")


def test_bot_requires_daily_label_and_valid_marker() -> None:
    valid = "<!-- ai-daily:2026-08-12:v1 -->"
    assert is_trusted_issue(issue("github-actions[bot]", ["Daily"], valid), "owner")
    assert not is_trusted_issue(issue("github-actions[bot]", [], valid), "owner")
    assert not is_trusted_issue(issue("github-actions[bot]", ["Daily"], "none"), "owner")


def test_untrusted_user_cannot_forge_daily_issue() -> None:
    assert not is_trusted_issue(
        issue("attacker", ["Daily"], "<!-- ai-daily:2026-08-12:v1 -->"), "owner"
    )
