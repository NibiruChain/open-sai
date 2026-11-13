default:
    @just --list

install:
    uv sync --all-groups

test:
    uv run pytest

check:
    uv run ruff check .
    uv run ruff format --check .
    uv run ty check cli/sai_cli
