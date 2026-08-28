from pathlib import Path


def _windows(timer: str) -> list[str]:
    return [
        line.removeprefix("OnCalendar=*-*-* ").strip()
        for line in Path(f"ops/systemd/{timer}").read_text(encoding="utf-8").splitlines()
        if line.startswith("OnCalendar=")
    ]


def test_persona_windows_all_follow_the_last_base_window() -> None:
    """A persona edition freezes the day's upstream marker.

    ``_persona_date_is_frozen`` refuses a base publication whose date already
    has a matching persona edition, so a persona run scheduled before a base
    window silently makes that window's upgrade unpublishable. The two timers
    have no other coupling, which is exactly why this needs asserting.
    """

    base = _windows("ai-daily.timer")
    persona = _windows("ai-daily-persona.timer")
    assert base and persona

    assert min(persona) > max(base), (
        f"persona runs at {min(persona)} but the last base window is "
        f"{max(base)}; that base window could never publish an upgrade"
    )


def test_daily_workflow_no_longer_publishes_on_a_schedule() -> None:
    """Publication moved to the self-hosted timer; two schedules would collide.

    The workflow is kept, and kept dispatchable, so the old path remains one
    click away if the new host has to be abandoned. What must not come back is
    the cron: that would have GitHub and the server publishing the same day
    from different pipelines.
    """

    workflow = Path(".github/workflows/daily.yml").read_text()
    assert "schedule:" not in workflow
    assert "cron:" not in workflow
    assert "workflow_dispatch:" in workflow


def test_site_workflow_is_reusable_and_locked() -> None:
    workflow = Path(".github/workflows/generate_site.yml").read_text()
    assert "workflow_call:" in workflow
    assert "uv sync --frozen --no-dev" in workflow
    assert "requirements.txt" not in workflow
    assert "python main.py ${{ github.repository }}" in workflow
    assert "python main.py ${{ github.token }}" not in workflow
    assert "ZOLA_VERSION: v0.23.3" in workflow
    assert "EVEN_THEME_COMMIT: 56015feedb5b3d6a7b74c077568449892cf8b458" in workflow
    assert 'git -C output/themes/even checkout --detach "$EVEN_THEME_COMMIT"' in workflow
    assert "*x86_64-unknown-linux-gnu*" in workflow
    assert "'*x86_64-unknown-linux*'" not in workflow


def test_zola_config_uses_current_highlighting_schema() -> None:
    config = Path("config.toml").read_text()
    assert 'description = "每日 AI 前沿技术情报，由 AI 辅助创作"' in config
    assert "[markdown.highlighting]" in config
    assert 'theme = "gruvbox-dark-medium"' in config
    assert "highlight_code" not in config


def test_recovery_does_not_call_model_when_issue_exists() -> None:
    workflow = Path(".github/workflows/daily.yml").read_text()
    recovery_start = workflow.index('elif [ "$SCHEDULE" = "5 21 * * *" ]')
    recovery_end = workflow.index('elif [ "${MANUAL_MODE:-publish}"', recovery_start)
    recovery = workflow[recovery_start:recovery_end]
    assert "ai-daily issue-exists" in recovery
    assert recovery.index("ai-daily issue-exists") < recovery.index("ai-daily run")


def test_benchmark_workflow_uses_secrets_and_fixed_dataset() -> None:
    workflow = Path(".github/workflows/benchmark_models.yml").read_text()
    assert "DASHSCOPE_API_KEY: ${{ secrets.DASHSCOPE_API_KEY }}" in workflow
    assert "DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}" in workflow
    assert "tests/evals/judge-golden.json" in workflow
    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
