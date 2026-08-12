from pathlib import Path


def test_daily_workflow_contains_all_beijing_schedule_conversions() -> None:
    workflow = Path(".github/workflows/daily.yml").read_text()
    assert 'cron: "20 20 * * *"' in workflow
    assert 'cron: "5 21 * * *"' in workflow
    assert 'cron: "45 21 * * *"' in workflow
    assert "DASHSCOPE_API_KEY: ${{ secrets.DASHSCOPE_API_KEY }}" in workflow
    assert "DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}" in workflow


def test_site_workflow_is_reusable_and_locked() -> None:
    workflow = Path(".github/workflows/generate_site.yml").read_text()
    assert "workflow_call:" in workflow
    assert "uv sync --frozen --no-dev" in workflow
    assert "requirements.txt" not in workflow
    assert "python main.py ${{ github.repository }}" in workflow
    assert "python main.py ${{ github.token }}" not in workflow


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
