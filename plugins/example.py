"""
An example NEON plugin. Rename it, edit it, or delete it -- it is only here to show the shape.

Everything is optional: a plugin can be nothing but a COMMANDS list.
"""

NAME = "Example"
DESCRIPTION = "Counts things you tell it to count, and says hello."

_counts = {}


def _hello(match, text):
    return "Hello from a plugin. Edit plugins/example.py to make me useful."


def _count(match, text):
    what = match.group("what").strip()
    _counts[what] = _counts.get(what, 0) + 1
    return f"That is {_counts[what]} for {what}."


def _total(match, text):
    what = match.group("what").strip()
    return f"{_counts.get(what, 0)} for {what}."


# Each entry is (regular expression, function). The first one whose pattern matches wins;
# return None from a handler to say "not mine after all" and let the assistant carry on.
COMMANDS = [
    (r"^(?:say )?hello plugin$", _hello),
    (r"^count (?:a|an|one)? ?(?P<what>.+)$", _count),
    (r"^how many (?P<what>.+?) have i counted\??$", _total),
]


def setup(api):
    """Run once when the plugin loads. `api` has: say, run, config, data_dir, log."""
    api.log.info("example plugin ready")
