---
description: General programming instructions that apply to all code in the project.
---

# General Programming Instructions

## General Principles

- Follow functional programming principles where possible.
- Adhere to Clean Code principles where possible.
- These guidelines are language-agnostic; language-specific examples are called out explicitly.
- Follow repo coding conventions: Python uses PEP 8 and the Ruff rules in pyproject.toml; run `uv run ruff check .` and `uv run pyrefly check` (CI lint/type checks). If adding JS/TS, follow Airbnb style and the project's ESLint/Prettier configs when present.
- Prefer declarative over imperative code.

## Function and Module Design

- Keep functions under 20-30 lines and focused on a single responsibility (fit on one screen when possible).
- Ensure functions and modules have a single responsibility.
- Limit function parameters (no more than 3 if possible).
- Avoid deep nesting; prefer early returns.
- Prefer composition over inheritance.

## Code Style

- Use descriptive and meaningful names.
- Write self-documenting code; add comments for complex algorithms, domain logic, non-obvious trade-offs, public API contracts, and TODO/WORKAROUND explanations; avoid comments that restate clear code.
- Favor immutability: use immutable bindings where supported (e.g., `const` in JS/TS) and avoid unnecessary mutable state.
- Prefer pure functions over side effects.
- Keep files short (preferably under 300-400 lines; exceptions for generated or config files) and split larger files into modules.
- Ensure consistent error handling.
