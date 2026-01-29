---
trigger: always_on
globs: *.py
---

# Python Project Instructions

This project adheres to ultra-strict modern Python standards. All code generated must comply with the following rules.

## Environment & Tooling

- **Python Version**: Python 3.14+ (use enhanced pattern matching, and new typing features).
- **Dependency Management**: Always use `uv`. Run commands via `uv run`.
- **Linting & Formatting**: Use `Ruff`.
- **Type Checking**: Use `Pyrefly` (strict mode).
- **Validation**: Use `Pydantic v2+` for all data boundaries (API, Config, DB).

## Strict Typing & Quality

- **No `Any`**: The use of `Any` is strictly forbidden. Use `TypeVar`, `Protocol`, `Generic`, or `Union`/`|` types.
- **Explicit Types**: Every function parameter and return must be typed.
- **Pyrefly Strictness**:
  - `reportUnknownVariableType = true`
  - `reportMissingTypeArgument = true`
  - `reportUnnecessaryTypeIgnoreComment = true`
- **Immutability**: Prefer `dataclasses` with `frozen=True` or `NamedTuple` over standard classes or dicts.

## Functional & Declarative Architecture

- **Functional over OO**: Avoid deep class hierarchies. Use pure functions and composition.
- **Pure Functions**: Logic must be decoupled from side effects. Pass state explicitly.
- **Declarative Style**: Use `match` statements (Structural Pattern Matching) for flow control instead of `if/else` chains.
- **Immutability**: Avoid `list.append()` or `dict.update()`. Use list/dict comprehensions and splats: `new_data = {**old_data, "key": "value"}`.
- **Lazy Evaluation**: Use generators and `itertools` for large data streams.

## Domain-Oriented Folder Structure

Split the project into clear domains. Maintain a flat hierarchy within domains but separate concerns strictly.

## Error Handling

- **No Exceptions for Flow Control**: Never use `try/except` to handle expected business logic paths.
- **Result Pattern**: Return types should use a `Result[T, E]` or `Either` pattern where possible.
- **Early Returns**: Always handle edge cases/errors first (Guard Clauses) and return early.
- **Clean IO**: Isolate IO-bound code into "Service" or "Adapter" layers; keep the core "Logic" pure.

## Interaction Protocol

- **Strict Linting**: After writing, run `uv run ruff check . --fix` and `uv run pyrefly check`.
- **Split Files**: If a file exceeds 200 lines, proactively suggest splitting it into domain sub-modules.
- **Performance**: For CPU-intensive tasks, provide a `Tachyon` profiling script to verify efficiency.
