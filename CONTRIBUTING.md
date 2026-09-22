# Contributing

Use Python 3.12 or newer. From a local checkout:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m unittest discover -s tests -v
```

Use temporary stores and synthetic projects for tests. Never include secrets,
private Knowledge, real project identifiers, databases or personal data in
fixtures, issue reports or pull requests.

For a bug, describe a minimal reproduction, expected behavior, actual behavior
and Python version. Keep pull requests focused and explain the change and its
tests. Discuss changes to retrieval semantics or data boundaries before expanding
them. See SECURITY.md for sensitive reports.
