"""Product-level benchmark helpers and report renderers."""

from eidolon_agent.app.benchmark.experience import (
    ExperienceReplayRunner,
    load_replay_scenarios,
    run_replay_files,
    run_replay_scenarios,
)
from eidolon_agent.app.benchmark.reporting import (
    compare_replay_reports,
    load_report,
    render_comparison_markdown,
    render_replay_html,
    render_replay_markdown,
)

__all__ = [
    "ExperienceReplayRunner",
    "compare_replay_reports",
    "load_replay_scenarios",
    "load_report",
    "render_comparison_markdown",
    "render_replay_html",
    "render_replay_markdown",
    "run_replay_files",
    "run_replay_scenarios",
]
