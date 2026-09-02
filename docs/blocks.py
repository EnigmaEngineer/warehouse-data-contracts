"""Grade the transcripts in a markdown document against the programs that produce them.

A README block that opens with a command and then shows output is a claim: run this and
you get that. Nothing checks the claim. The block is typed by hand, the program moves,
and the two drift apart with no test able to notice because a document is not code.

The approach here is not to re-run the command and diff. Most blocks in this README are
trimmed, indented or elided, so a whole-output comparison would refuse almost every
correct block and get switched off within a day. Instead the printable text is read out
of the program with `ast`, turned into patterns, and each output line is graded only
when it clearly belongs to one of them.

The grading rule is the whole design and it took three tries. The first claimed a line
when the line began with the template's fixed opening text. That misses any template
whose first word is followed by an argument. It graded 10 of 128 lines and found nothing.
The second claimed on the template's opening as a pattern, which graded 73 of 171 and
still missed two lines that had really gone stale.

Both failed the same way, and the way is worth stating because it is easy to walk into. A
claiming rule built out of the template's text cannot claim a line whose drift is in that
text. The rule and the fault were reading the same characters, so the lines most worth
grading were the ones it let through as unrecognised.

So claiming is by vocabulary instead. A template's fixed words are the ones no argument
can supply. A line carrying most of them was meant to be that line, however mangled, and
the full pattern then decides whether it still is. A line no template claims is ungraded
and counted as such, because a check that quietly grades nothing is the failure mode this
repo has hit before.
"""

import ast
import os
import re


COMMAND = re.compile(r"^(?:[A-Z][A-Z0-9_]*=\S+\s+)*(python3?|bash|sh|pip3?)\s")

# A template with two or three fixed words is a sentence fragment that would claim half
# the document. Four is the floor, and a claimed line has to carry most of them.
MIN_FIXED_WORDS = 4
CLAIM = 0.7

# A template with no fixed text is a passthrough and clears every line there is.
MIN_FIXED_CHARS = 3


class Block(object):
    """One fenced block, with where it sits and what it says.

    `command` is the command the block's output belongs to. It is not always inside the
    block. This README writes a command once under a heading and then shows its output in
    several later blocks broken up by prose, so a block with no command of its own
    inherits the last one seen under the same heading. Inheriting across a heading would
    be guessing.
    """

    def __init__(self, start_line, heading, lines):
        self.start_line = start_line
        self.heading = heading
        self.lines = lines
        self.inherited = None

    @property
    def own_command(self):
        """The first non-blank line when it looks like something you would type."""
        for line in self.lines:
            if not line.strip():
                continue
            return line.strip() if COMMAND.match(line.strip()) else None
        return None

    @property
    def command(self):
        return self.own_command or self.inherited

    @property
    def output(self):
        """The lines that are output rather than the command, blanks dropped."""
        body = self.lines
        if self.own_command is not None:
            seen = False
            body = []
            for line in self.lines:
                if not seen:
                    if line.strip():
                        seen = True
                    continue
                body.append(line)
        elif self.inherited is None:
            return []
        return [line for line in body if line.strip()]


def fenced_blocks(text):
    """Every fenced block in the document, in order, with the heading above it."""
    blocks = []
    heading = ""
    open_at = None
    body = []
    for number, line in enumerate(text.split("\n"), start=1):
        if line.startswith("```"):
            if open_at is None:
                open_at = number
                body = []
            else:
                blocks.append(Block(open_at, heading, body))
                open_at = None
            continue
        if open_at is None and line.startswith("#"):
            heading = line.lstrip("#").strip()
        elif open_at is not None:
            body.append(line)
    _carry_commands(blocks)
    return blocks


def _carry_commands(blocks):
    running = None
    heading = object()
    for block in blocks:
        if block.heading != heading:
            heading = block.heading
            running = None
        if block.own_command is not None:
            running = block.own_command
        else:
            block.inherited = running


def transcripts(blocks):
    """The blocks that claim to be a run rather than a table or a snippet."""
    return [b for b in blocks if b.command is not None and b.output]


def script_for(command, root):
    """The file a command runs, when it is one this repo ships.

    Returns None for anything else. `pip install --dry-run` is a real command in the
    README and there is no file to read templates out of.
    """
    for word in command.split():
        # An environment assignment in front of the command, and a flag with a path in
        # it, are both things that can end in .py without being the program. The first
        # version tried to tell them apart and let `--module=x.py` through as the script.
        if word.startswith("-") or "=" in word:
            continue
        candidate = word.strip("'\"")
        if candidate.endswith(".py") or candidate.endswith(".sh"):
            path = os.path.join(root, candidate)
            return path if os.path.exists(path) else None
    return None


def _placeholders_to_pattern(literal):
    """Escape a template, then let its {} holes match anything."""
    pattern = re.escape(literal)
    # re.escape leaves braces alone on 3.7+, so the holes are still findable.
    pattern = re.sub(r"\\?\{[^{}]*\\?\}", ".*?", pattern)
    return pattern


WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


def _words(text):
    """Words, lowercased, punctuation dropped. Numbers are excluded on purpose.

    A figure in a published line is nearly always the argument rather than the fixed
    part, so counting it would reward a line for carrying a number the template never
    promised.
    """
    return [w.lower() for w in WORD.findall(text)]


def _fixed_words(literal):
    return _words(re.sub(r"\{[^{}]*\}", " ", literal))


def _fixed_length(literal):
    return len(re.sub(r"\{[^{}]*\}", "", literal).strip())


class Template(object):
    def __init__(self, literal, source, line):
        self.literal = literal
        self.source = source
        self.line = line
        self.fixed = _fixed_words(literal)
        self.pattern = re.compile(
            "^\\s*" + _placeholders_to_pattern(literal.strip()) + "\\s*$")

    def graded(self):
        return len(set(self.fixed)) >= MIN_FIXED_WORDS

    def can_clear(self):
        """Whether saying "this line is fine" means anything coming from this template.

        `print("   {}")` is a passthrough. Its pattern matches every line ever written,
        so letting it clear anything makes the whole check vacuous, which is what it did
        before this existed. A template has to say something of its own.
        """
        return _fixed_length(self.literal) >= MIN_FIXED_CHARS

    def score(self, line):
        """The share of this template's fixed words the line still carries."""
        wanted = set(self.fixed)
        if not wanted:
            return 0.0
        return len(wanted & set(_words(line))) / float(len(wanted))

    def claims(self, line):
        return self.graded() and self.score(line) >= CLAIM

    def matches(self, line):
        return self.pattern.match(line.strip()) is not None


def _python_templates(path):
    """Every literal a print() in this file can put on a line.

    Handles the two shapes used here, a plain literal and a literal carrying .format().
    An f-string's fixed parts are kept and its expressions become holes.
    """
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "print"):
            continue
        for arg in node.args:
            literal = _literal_of(arg)
            if literal:
                found.append(Template(literal, path, node.lineno))
    return found


def _literal_of(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "format":
            return _literal_of(node.func.value)
    if isinstance(node, ast.JoinedStr):
        parts = []
        for piece in node.values:
            # A Constant inside an f-string is always a string, so there is nothing to
            # check beyond the node type. Asserting it as well was a dead clause.
            if isinstance(piece, ast.Constant):
                parts.append(piece.value)
            else:
                parts.append("{}")
        return "".join(parts)
    return None


ECHO = re.compile(r'^\s*echo\s+"([^"]*)"')
ASSIGN = re.compile(r'^\s*([A-Z][A-Z0-9_]*)="([^"]*)"\s*$')
SHELL_VAR = re.compile(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?')


def _shell_templates(path):
    """Every echo in a shell script, with literal variables filled in.

    A variable assigned a literal earlier in the file is substituted, because that is
    where the interesting content lives. `EXPECTED="pull check ..."` in dag_smoke.sh is
    the DAG's task list and it is exactly the thing a README goes stale on. Anything
    else becomes a hole.
    """
    literals = {}
    found = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            assigned = ASSIGN.match(line)
            if assigned and "$" not in assigned.group(2):
                literals[assigned.group(1)] = assigned.group(2)
                continue
            said = ECHO.match(line)
            if not said:
                continue
            text = SHELL_VAR.sub(
                lambda m: literals.get(m.group(1), "{}"), said.group(1))
            found.append(Template(text, path, number))
    return found


def templates(path):
    if path.endswith(".sh"):
        return _shell_templates(path)
    return _python_templates(path)


def damages(line):
    """Realistic ways a published line goes stale, for measuring what the check catches.

    Chosen from what has really happened to this README rather than from what is easy to
    generate. A comma removed by a grammar pass, a word dropped, a word changed, and a
    list that has gained an item. Reordering and pure number changes are left out: the
    first is not a thing anyone does to a transcript by hand, and the second is out of
    scope by construction, since a number is an argument and matches anything.
    """
    text = line.strip()
    out = []
    first = text.find(",")
    if first != -1:
        dropped = text[:first] + text[first + 1:]
        out.append(("comma removed", dropped))
    words = text.split()
    if len(words) > 3:
        out.append(("word dropped", " ".join(words[:2] + words[3:])))
        out.append(("word changed", " ".join(
            words[:2] + ["elsewhere"] + words[3:])))
        out.append(("word inserted", " ".join(
            words[:2] + ["extra"] + words[2:])))
    return out


def holdout(text, root):
    """Damage every graded line one at a time and count what comes back.

    The claiming threshold was chosen so the lines already known to be stale came out
    above it. That is fitting a number to the cases in front of you. This is the arm that
    can fail: lines the check currently passes, broken on purpose, one damage at a time.
    """
    baseline = report(text, root)
    caught = 0
    total = 0
    still_legal = 0
    missed = []
    for verdict in baseline["verdicts"]:
        known = templates(verdict.script)
        for line in verdict.graded:
            for label, broken in damages(line):
                total += 1
                owners = [t for t in known if t.claims(broken)]
                cleared = any(t.matches(broken) for t in known if t.can_clear())
                if owners and not cleared:
                    caught += 1
                elif cleared:
                    # The damage landed inside an argument, so the broken line is still
                    # something the program can print. Counting it as a miss would be
                    # asking the check to know what a value should be, which it cannot
                    # and does not claim to. Counted apart rather than hidden.
                    still_legal += 1
                else:
                    missed.append((label, line.strip(), broken))
    return {
        "total": total,
        "caught": caught,
        "still_legal": still_legal,
        "missed": missed,
        "detectable": total - still_legal,
    }


class Verdict(object):
    def __init__(self, block, script):
        self.block = block
        self.script = script
        self.graded = []
        self.drifted = []
        self.ungraded = []


def grade(block, root):
    """Grade one transcript against the script its command names.

    Returns None when the command is not one of ours, which is a real answer and not a
    pass. The caller counts those.
    """
    script = script_for(block.command, root)
    if script is None:
        return None
    known = templates(script)
    verdict = Verdict(block, script)
    for line in block.output:
        owners = [t for t in known if t.claims(line)]
        if not owners:
            verdict.ungraded.append(line)
            continue
        verdict.graded.append(line)
        # Clearing a line and claiming one are different jobs, so they use different
        # sets. "{} rows over {} partitions" has three fixed words and is too vague to
        # claim anything, and it is still perfectly able to say this line is fine. Only
        # asking the claimants got a correct line reported as drift, because a wordier
        # neighbour in the same script claimed it first.
        if not any(t.matches(line) for t in known if t.can_clear()):
            # Report against the closest one. Several templates can claim a line and the
            # nearest is the one a reader needs to see beside it.
            verdict.drifted.append(
                (line, max(owners, key=lambda t: t.score(line))))
    return verdict


def report(text, root):
    """Grade the whole document. The counts are the point as much as the failures."""
    blocks = fenced_blocks(text)
    runs = transcripts(blocks)
    verdicts = []
    unreadable = []
    for block in runs:
        verdict = grade(block, root)
        if verdict is None:
            unreadable.append(block)
        else:
            verdicts.append(verdict)
    return {
        "blocks": len(blocks),
        "transcripts": len(runs),
        "unreadable": unreadable,
        "verdicts": verdicts,
        "graded_lines": sum(len(v.graded) for v in verdicts),
        "output_lines": sum(len(v.block.output) for v in verdicts),
        "drifted": [(v, line, t) for v in verdicts for line, t in v.drifted],
    }
