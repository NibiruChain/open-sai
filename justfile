# Wrapper that makes calling `just` show the command list
[private]
list:
    @just --list

install:
    uv sync --all-groups

test:
    uv run pytest

check:
    uv run ruff check .
    uv run ruff format --check .
    uv run ty check cli/sai_cli

# Build the wheel and sdist and check that both ship the real SKILL.md
build-check:
    uv run python scripts/check_dist.py

# Tag main as sai-cli/vVERSION and push it; dry run unless given --run
release VERSION *FLAGS:
    uv run python scripts/release.py {{VERSION}} {{FLAGS}}
