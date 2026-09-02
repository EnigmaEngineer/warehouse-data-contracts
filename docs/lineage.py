"""Read the warehouse lineage out of the dbt project rather than drawing it by hand.

The architecture block in the README was typed. It went one character out of alignment in
a grammar pass and nothing noticed, which is the small version of the real risk. The large
version is a model gaining a parent and the picture continuing to show the old shape,
because a picture has no way to be wrong.

So the lineage half of the diagram is generated from `ref()` and `source()` in the model
SQL and the README's copy is checked against it. The dbt manifest would be the obvious
input and it is gitignored, which would put the published picture behind a build artefact
nobody can rebuild from the repo alone. The model files are the repo.
"""

import os
import re


REF = re.compile(r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")
SOURCE = re.compile(r"\{\{\s*source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")

MODEL_DIRS = ("models", "snapshots")


def nodes(project):
    """Every model and snapshot, mapped to the parents it selects from.

    A source is a parent too, written `schema.table`, because the whole point of this
    picture is where the warehouse starts.
    """
    return _read(project)[0]


def _read(project):
    """Returns (graph, sources). Sources are kept apart from refs on purpose.

    Both end up in a node's parent list and they are not the same kind of thing. A
    `source()` names something outside the project and is where the picture starts. A
    `ref()` names a model that has to be in here. Pooling them makes a typo in a ref
    indistinguishable from a source, so `ref('slv_serivce_requests')` would draw as a new
    root and the diagram would render happily with a model missing from the middle of it.
    """
    graph = {}
    sources = set()
    for directory in MODEL_DIRS:
        root = os.path.join(project, directory)
        for base, _, files in os.walk(root):
            for name in sorted(files):
                if not name.endswith(".sql"):
                    continue
                path = os.path.join(base, name)
                with open(path, encoding="utf-8") as handle:
                    sql = handle.read()
                named = ["{}.{}".format(a, b) for a, b in SOURCE.findall(sql)]
                sources.update(named)
                graph[name[:-4]] = sorted(set(REF.findall(sql)) | set(named))
    return graph, sources


def sources(project):
    return sorted(_read(project)[1])


def layers(graph, known_sources=None):
    """Group nodes by how far they are from a source.

    Kahn's algorithm with the sources seeded at depth zero. A cycle is impossible in a
    dbt project because dbt refuses one, so this raises rather than carrying a branch
    that cannot be reached.

    `known_sources` is what a parent has to be in to count as a root. Without it any name
    absent from the graph reads as a source, so a mistyped `ref()` becomes a new root
    instead of an error and the picture renders with a model missing.
    """
    parents = dict(graph)
    orphans = {p for ps in graph.values() for p in ps if p not in graph}
    if known_sources is not None:
        unknown = sorted(orphans - set(known_sources))
        if unknown:
            raise ValueError(
                "{} are selected from and are neither a model here nor a source: "
                "used by {}".format(unknown, sorted(
                    n for n, ps in graph.items() if set(ps) & set(unknown))))
    depth = dict((s, 0) for s in sorted(orphans))
    remaining = set(graph)
    while remaining:
        ready = [n for n in remaining if all(p in depth for p in parents[n])]
        if not ready:
            raise ValueError("cycle or missing parent among {}".format(sorted(remaining)))
        for node in ready:
            depth[node] = 1 + max([depth[p] for p in parents[node]] or [0])
            remaining.discard(node)
    grouped = {}
    for node, level in depth.items():
        grouped.setdefault(level, []).append(node)
    return [sorted(grouped[k]) for k in sorted(grouped)]


def render(graph, known_sources=None):
    """An ASCII block, one layer per row, with each node's parents named under it.

    Deliberately not a box drawing. A generated picture with corner characters in it
    invites hand editing when it goes slightly wrong, and hand editing is what this
    replaces.
    """
    lines = []
    for level, group in enumerate(layers(graph, known_sources)):
        label = "source" if level == 0 else "level {}".format(level)
        lines.append("{}:".format(label))
        for node in group:
            parents = graph.get(node, [])
            if parents:
                lines.append("  {} <- {}".format(node, ", ".join(parents)))
            else:
                lines.append("  {}".format(node))
        lines.append("")
    return "\n".join(lines).rstrip()


def published(readme_text, marker):
    """The block sitting under a marker heading in the README, or None."""
    lines = readme_text.split("\n")
    for index, line in enumerate(lines):
        if line.strip() != marker:
            continue
        for start in range(index, len(lines)):
            if lines[start].startswith("```"):
                for end in range(start + 1, len(lines)):
                    if lines[end].startswith("```"):
                        return "\n".join(lines[start + 1:end]).rstrip()
                return None
    return None
