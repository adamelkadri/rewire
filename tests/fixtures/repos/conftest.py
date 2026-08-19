"""Keep pytest out of the fixture repositories.

These trees are input data for the analyser, not part of Rewire's own suite.
Their test files import packages that are deliberately not installed, and one
module is deliberately unparseable, so collecting them would fail the run.
"""

collect_ignore_glob = ["*"]
