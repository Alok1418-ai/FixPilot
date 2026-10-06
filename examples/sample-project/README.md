# Sample Shop (FixPilot demo project)

A deliberately small, deliberately buggy service used by FixPilot's demo and
evaluation harness.  It is plain standard-library Python so the whole loop runs
anywhere — no installs, no network.

```
app/inventory.py   stock lookups
app/pricing.py     basket maths
app/notes.py       quantity parsing for incoming orders
tests/             unittest suite (one test per behaviour)
reports/           real-world style bug reports, one per defect
```

Run the suite:

```bash
python3 -m unittest discover -v
```

Three defects ship with this project.  Each `reports/bug-*.txt` file reproduces
one of them in the shape a developer would actually paste from a terminal.
