# Contributing

Bug reports and pull requests are welcome.

## Setup

```sh
uv sync
make check   # compile, ruff, and the unit tests
```

`make check` is the same command CI runs on Python 3.10, 3.12, and 3.13.

## Pull requests

- Keep a change focused on one behaviour.
- Add a test next to the code you changed. The Textual tests use `run_test`, so
  they exercise real key handling.
- Keep the README in step with user-visible keys and flags.
