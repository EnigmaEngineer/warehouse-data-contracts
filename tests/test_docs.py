"""Checks for the two modules that judge what the README says.

The fixtures here carry the defects that were really found, not invented ones. A block
whose command sits in an earlier block, a line whose drift is inside the text a claiming
rule would key on, and a passthrough print that matches everything. Each of those cost a
rewrite of the grading rule and each is pinned below.
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from docs import blocks, lineage  # noqa: E402


DOC = """
# A title

## Running it

```
python scripts/demo.py
```

Some prose in between, which is how the real README is written.

```
loaded 4 rows from the file
the table holds 6 partitions, and 2025-01-07 is absent
```

## Something else

```
loaded 9 rows from the file
```
"""

SCRIPT = '''
def main():
    print("   {}".format("banner"))
    print("loaded {} rows from the file".format(4))
    print("   the table holds {} partitions, and {} is {}".format(6, "x", "y"))
    print("{} rows over {} partitions".format(1, 2))
'''


def _repo(script_name="demo.py", script=SCRIPT):
    root = tempfile.mkdtemp(prefix="wdc-docs-")
    os.makedirs(os.path.join(root, "scripts"))
    with open(os.path.join(root, "scripts", script_name), "w", encoding="utf-8") as fh:
        fh.write(script)
    return root


def check_a_block_with_no_command_inherits_the_one_above_it():
    found = blocks.fenced_blocks(DOC)
    runs = blocks.transcripts(found)
    # Three blocks, and the first is the command on its own with no output.
    assert len(found) == 3
    commands = [b.command for b in runs]
    assert commands == ["python scripts/demo.py"], commands


def check_inheritance_stops_at_the_next_heading():
    """The last block sits under a different heading, so it belongs to nothing.

    Without this the command would carry all the way down the document and every later
    block would be graded against a script it has nothing to do with.
    """
    found = blocks.fenced_blocks(DOC)
    last = found[-1]
    assert last.heading == "Something else"
    assert last.command is None, last.command
    assert last.output == []


def check_a_drift_inside_the_claiming_text_is_still_caught():
    """The defect the first two grading rules both missed.

    The published line has lost the comma that sits in the template. A rule that decides
    whether a line belongs to a template by reading that same stretch of text cannot
    claim this line, so it goes unrecognised rather than reported.
    """
    root = _repo()
    doc = DOC.replace(
        "the table holds 6 partitions, and 2025-01-07 is absent",
        "the table holds 6 partitions and 2025-01-07 is absent")
    result = blocks.report(doc, root)
    drifted = [line.strip() for _, line, _ in result["drifted"]]
    assert drifted == ["the table holds 6 partitions and 2025-01-07 is absent"], drifted


def check_the_matching_line_is_not_reported():
    """Run it over the answer key first. A gate that refuses correct output gets removed."""
    root = _repo()
    result = blocks.report(DOC, root)
    assert result["drifted"] == [], result["drifted"]
    assert result["graded_lines"] >= 2, result["graded_lines"]


def check_a_passthrough_print_cannot_clear_a_line():
    """`print("   {}")` matches every line there is.

    Letting it say a line is fine made the whole check vacuous. It cleared both real
    drifts in the README while reporting a healthy graded count, which is the worst
    shape available: a number that says the check is working.
    """
    passthrough = blocks.Template("   {}", "x.py", 1)
    assert passthrough.matches("literally anything at all")
    assert not passthrough.can_clear()


def check_a_template_too_vague_to_claim_can_still_clear():
    """The other half of that asymmetry, and it was a real false positive.

    "{} rows over {} partitions" has three fixed words, which is under the floor for
    claiming a line. A wordier neighbour in the same file claimed a correct line and
    reported it as drift because only claimants were asked to clear it.
    """
    vague = blocks.Template("{} rows over {} partitions", "x.py", 1)
    assert not vague.graded()
    assert vague.can_clear()
    assert vague.matches("179314 rows over 14 partitions")


def check_the_thresholds_sit_where_they_say_they_do():
    """Fixtures either side of each floor, because a fixture in the middle pins nothing.

    Every threshold in this module survived a mutation until these existed. Three fixed
    words has to be too few and four has to be enough, or the number in the constant is
    decoration.
    """
    three = blocks.Template("{} rows over {} partitions", "x.py", 1)
    four = blocks.Template("{} rows over {} judged partitions", "x.py", 1)
    assert not three.graded()
    assert four.graded()

    # And the clearing floor, which counts characters rather than words.
    assert not blocks.Template("  {} {}", "x.py", 1).can_clear()
    assert blocks.Template("  at: {} {}", "x.py", 1).can_clear()

    # A line carrying exactly the claiming share is claimed, one word short is not.
    ten = blocks.Template(
        "alpha bravo charlie delta echo foxtrot golf hotel india juliet", "x.py", 1)
    assert ten.claims("alpha bravo charlie delta echo foxtrot golf and nothing else")
    assert not ten.claims("alpha bravo charlie delta echo golf and nothing else")


def check_a_block_reports_the_line_it_starts_on():
    """The number a reader uses to find the block. Nothing was asserting it."""
    found = blocks.fenced_blocks(DOC)
    assert [b.start_line for b in found] == [6, 12, 19], [b.start_line for b in found]


def check_a_shell_template_reports_the_line_it_is_on():
    root = tempfile.mkdtemp(prefix="wdc-docs-line-")
    os.makedirs(os.path.join(root, "scripts"))
    path = os.path.join(root, "scripts", "s.sh")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# a comment\nMSG=\"hello there world now\"\necho \"$MSG\"\n")
    found = blocks.templates(path)
    assert [t.line for t in found] == [3], [t.line for t in found]


def check_an_f_string_keeps_its_fixed_halves():
    """No fixture had an f-string, so the branch that reads one was never run.

    A mutant turning its `and` into an `or` fed a non-string node into a join and would
    have crashed on the first script that used one.
    """
    root = _repo(script='def m():\n    n = 3\n    print(f"held {n} rows for the day")\n')
    found = blocks.templates(os.path.join(root, "scripts", "demo.py"))
    assert [t.literal for t in found] == ["held {} rows for the day"], found
    assert found[0].matches("held 12 rows for the day")
    assert not found[0].matches("held 12 rows for the week")


def check_a_flag_carrying_a_path_is_not_mistaken_for_the_script():
    """Both halves of the word filter, with a fixture for each.

    `WDC_DUCKDB=/tmp/wh.duckdb bash scripts/demo.sh` puts an assignment in front, and
    `--module=other.py` puts a .py inside a flag. Either one taken as the program means
    the block is graded against the wrong file, or against nothing.
    """
    root = _repo("demo.sh", "echo \"nothing much happens here\"\n")
    assert blocks.script_for(
        "WDC_DUCKDB=/tmp/wh.duckdb bash scripts/demo.sh", root) == os.path.join(
            root, "scripts", "demo.sh")
    assert blocks.script_for("python --module=scripts/demo.sh other", root) is None


def check_a_command_for_a_script_we_do_not_ship_is_not_a_pass():
    """A block running something else has to be counted, not skipped silently."""
    root = _repo()
    doc = DOC.replace("python scripts/demo.py", "pip install --dry-run dbt-core")
    result = blocks.report(doc, root)
    assert len(result["unreadable"]) == 1, result["unreadable"]
    assert result["verdicts"] == []


def check_shell_variables_assigned_a_literal_are_filled_in():
    """The stale task list was inside a shell variable.

    dag_smoke.sh holds the DAG's task list in EXPECTED and echoes it. Leaving the
    variable as a hole would make the published line match whatever it said.
    """
    root = tempfile.mkdtemp(prefix="wdc-docs-sh-")
    os.makedirs(os.path.join(root, "scripts"))
    path = os.path.join(root, "scripts", "smoke.sh")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('EXPECTED="pull check load_raw"\n'
                 'echo "ran $PARTITION, tasks: $EXPECTED"\n')
    found = blocks.templates(path)
    literals = [t.literal for t in found]
    assert literals == ["ran {}, tasks: pull check load_raw"], literals
    assert found[0].matches("ran 2025-01-13, tasks: pull check load_raw")
    assert not found[0].matches("ran 2025-01-13, tasks: pull check feed_checks load_raw")


PROJECT_SQL = {
    "models/staging/stg_x.sql": "select * from {{ source('raw', 'thing') }}",
    "models/silver/slv_x.sql": "select * from {{ ref('stg_x') }}",
    "models/gold/gold_a.sql": "select * from {{ ref('slv_x') }}",
    "models/gold/gold_b.sql": (
        "with a as (select * from {{ ref('slv_x') }}), "
        "b as (select * from {{ ref('gold_a') }}) select * from a join b"),
    "snapshots/snap_x.sql": "select * from {{ ref('stg_x') }}",
}


def _project():
    root = tempfile.mkdtemp(prefix="wdc-lineage-")
    for name, sql in PROJECT_SQL.items():
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(sql)
    return root


def check_a_model_with_two_parents_lands_below_both():
    """The edge the typed diagram was missing.

    gold_b selects from slv_x and from gold_a, so it cannot sit beside gold_a. A picture
    drawn by hand put the real one there for as long as the picture existed.
    """
    root = _project()
    graph = lineage.nodes(root)
    assert graph["gold_b"] == ["gold_a", "slv_x"], graph["gold_b"]
    rows = lineage.layers(graph, lineage.sources(root))
    assert rows[0] == ["raw.thing"]
    assert rows[-1] == ["gold_b"], rows[-1]
    assert "gold_a" in rows[-2]


def check_a_missing_parent_raises_rather_than_dropping_the_node():
    """A node whose parent is not in the project would otherwise vanish from the picture.

    Silently omitting it is the worst answer, because the diagram would still render and
    would still look complete.
    """
    root = _project()
    with open(os.path.join(root, "models", "gold", "gold_c.sql"), "w",
              encoding="utf-8") as fh:
        fh.write("select * from {{ ref('gold_a') }}")
    graph = lineage.nodes(root)
    del graph["gold_a"]
    try:
        lineage.layers(graph, lineage.sources(root))
    except ValueError as raised:
        assert "gold_c" in str(raised), str(raised)
        return
    raise AssertionError("a missing parent was accepted")


def check_the_published_block_is_read_by_its_heading():
    text = "## Other\n\n```\nnot this one\n```\n\n## Wanted\n\n```\nthis one\n```\n"
    assert lineage.published(text, "## Wanted") == "this one"
    assert lineage.published(text, "## Absent") is None


def check_an_empty_block_reads_as_empty_rather_than_missing():
    """The shortest block there is, which is the one the scan can step over.

    A search for the closing fence that starts one line too far walks past it on a block
    with nothing in it, and an empty lineage section then reports as no section at all.
    Those are different problems and a reader would chase the wrong one.
    """
    assert lineage.published("## Wanted\n\n```\n```\n", "## Wanted") == ""


def check_the_shipped_lineage_matches_the_shipped_readme():
    """The check the script runs, run here too, so a model change fails the suite."""
    project = os.path.join(ROOT, "dbt")
    graph = lineage.nodes(project)
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
        text = fh.read()
    block = lineage.published(text, "## Lineage, read out of the models")
    assert block is not None, "the README has no lineage block"
    assert block == lineage.render(graph, lineage.sources(project))


def check_damaged_lines_are_caught_at_the_published_rate():
    """The arm that can fail, pinned so a change to the rule has to move this number.

    A threshold chosen so the known bad lines land above it is fitted to its own cases.
    This breaks every line the check currently passes and counts what comes back. The
    floor is set under today's 169 of 171 rather than at it, because the corpus grows as
    the README does and pinning an exact count would fail on the next paragraph.
    """
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
        text = fh.read()
    held = blocks.holdout(text, ROOT)
    assert held["total"] > 150, held["total"]
    assert held["detectable"] > 120, held["detectable"]
    rate = held["caught"] / float(held["detectable"])
    assert rate > 0.95, (rate, held["missed"])


def check_a_damage_landing_in_an_argument_is_not_counted_as_a_miss():
    """The two outcomes have to stay apart or the rate above means nothing.

    A word changed inside a `{}` leaves a line the program can still print. Folding it
    into the miss column would understate the check, and folding it into the caught
    column would overstate it. It gets its own number.
    """
    root = _repo()
    held = blocks.holdout(DOC, root)
    assert held["still_legal"] > 0, held
    assert held["detectable"] == held["total"] - held["still_legal"]


def check_the_shipped_readme_has_no_drifted_transcript():
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
        text = fh.read()
    result = blocks.report(text, ROOT)
    assert result["drifted"] == [], [
        (line, t.literal) for _, line, t in result["drifted"]]
    # Grading nothing would pass the line above. The count is what stops that.
    assert result["graded_lines"] >= 40, result["graded_lines"]
