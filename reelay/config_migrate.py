"""Config drift between `config_example.yaml` and a live `config.yaml`.

The example is the schema of record: every key the bot reads is in it. A
long-running install's config.yaml falls behind as features land, and the only
signal today arrives at the worst moment -- startup refuses to boot and DMs the
admins a list of missing keys. This turns that into something you run *before*
deploying (`make config-check`) and can fix in one step (`make config-migrate`).

The merge edits the YAML *text* rather than re-dumping a parsed document, so
existing values, key order, formatting and comments all survive untouched, and
each newly added key arrives with the example's comment explaining what it is
for. Anything in config.yaml that isn't in the example is reported and left
alone -- a key we don't recognise is far more likely to be new-and-undocumented
than genuinely dead, and this tool is not the thing that should decide.
"""

import argparse
import difflib
import re
import shutil
import sys
from collections import namedtuple
from datetime import datetime
from pathlib import Path

import yaml

from .definitions import (
    CONFIG_PATH,
    CONFIG_EXAMPLE_PATH,
    OPTIONAL_KEYS,
    flatten_dict,
)

# A block-mapping key: `foo:`, `  foo : bar # comment`. Deliberately does not
# match list items (`- foo`) or comments, so their lines stay part of whichever
# block they sit in.
_KEY_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[^\s#:-][^:]*?)\s*:(?=\s|$)")

# path: slash-joined key path. indent: columns of leading whitespace. start:
# first line of the block, including the comment lines directly above the key --
# that is where the example documents it, and a migrated key is much less
# useful without them. key_line: the `key:` line itself. end: last line of its
# value/children, exclusive of trailing blanks and comments (those introduce
# whatever comes next).
Block = namedtuple("Block", "path indent start key_line end")

Drift = namedtuple("Drift", "missing required optional unknown")


def _is_filler(line):
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def index_blocks(lines):
    """Map every mapping key path in `lines` to the span of text defining it."""
    keys = []  # (line index, indent, path)
    stack = []
    for i, line in enumerate(lines):
        if _is_filler(line):
            continue
        match = _KEY_RE.match(line)
        if not match:
            continue
        indent = len(match.group("indent").expandtabs(4))
        key = match.group("key").strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = "/".join([k for _, k in stack] + [key])
        stack.append((indent, key))
        keys.append((i, indent, path))

    blocks = {}
    for n, (key_line, indent, path) in enumerate(keys):
        end = len(lines) - 1
        for next_line, next_indent, _ in keys[n + 1:]:
            if next_indent <= indent:
                end = next_line - 1
                break
        while end > key_line and _is_filler(lines[end]):
            end -= 1
        start = key_line
        while start > 0 and lines[start - 1].strip().startswith("#"):
            start -= 1
        blocks[path] = Block(path, indent, start, key_line, end)
    return blocks


def _split(text):
    """Lines without endings, plus the line ending to rejoin them with."""
    return text.splitlines(), "\r\n" if "\r\n" in text else "\n"


def _read(path):
    return Path(path).read_text(encoding="utf8")


def _is_optional(path, example_leaves):
    """A block is optional only if nothing required lives under it."""
    if path in OPTIONAL_KEYS:
        return True
    below = [leaf for leaf in example_leaves if leaf == path or leaf.startswith(path + "/")]
    return bool(below) and all(leaf in OPTIONAL_KEYS for leaf in below)


def _child_indent(config_blocks, parent, example_step):
    """The column a new child of `parent` should start at: whatever that
    parent's existing children already use, falling back to the example's own
    nesting step when the parent is still empty."""
    prefix = parent.path + "/"
    children = [block for path, block in config_blocks.items()
                if path.startswith(prefix) and "/" not in path[len(prefix):]]
    if children:
        return min(block.indent for block in children)
    return parent.indent + max(example_step, 2)


def _reindent(lines, shift):
    """Shift a whole chunk sideways. Relative indentation inside it is left
    alone -- the block came from the example already internally consistent."""
    if shift > 0:
        return [" " * shift + line if line.strip() else line for line in lines]
    if shift < 0:
        return [line[-shift:] if not line[:-shift].strip() else line.lstrip()
                for line in lines]
    return list(lines)


def _prune_descendants(paths):
    """Keep only the outermost paths -- a whole missing block is inserted (or
    reported) once, not once per leaf inside it."""
    return [p for p in paths if not any(p != q and p.startswith(q + "/") for q in paths)]


def drift(example_text, config_text):
    """What config.yaml is missing relative to the example, and what it has
    that the example doesn't."""
    example_lines, _ = _split(example_text)
    config_lines, _ = _split(config_text)
    example_blocks = index_blocks(example_lines)
    config_blocks = index_blocks(config_lines)

    example_leaves = set(flatten_dict(yaml.safe_load(example_text) or {}))

    missing = _prune_descendants([p for p in example_blocks if p not in config_blocks])
    missing.sort(key=lambda p: example_blocks[p].key_line)
    unknown = _prune_descendants([p for p in config_blocks if p not in example_blocks])
    unknown.sort(key=lambda p: config_blocks[p].key_line)

    required = [p for p in missing if not _is_optional(p, example_leaves)]
    optional = [p for p in missing if _is_optional(p, example_leaves)]
    return Drift(missing, required, optional, unknown)


def merge(example_text, config_text):
    """config_text with every key it is missing spliced in from the example.

    Returns (merged text, paths inserted). Raises ValueError if the result
    would not parse or would lose a value the user had set.
    """
    example_lines, _ = _split(example_text)
    config_lines, newline = _split(config_text)
    example_blocks = index_blocks(example_lines)
    config_blocks = index_blocks(config_lines)

    # New top-level keys go after the last line that says anything, so trailing
    # blank lines stay trailing.
    append_at = len(config_lines)
    while append_at > 0 and not config_lines[append_at - 1].strip():
        append_at -= 1

    missing = drift(example_text, config_text).missing
    inserts = []  # (line to insert before, order, lines)
    first_append = True
    for order, path in enumerate(missing):
        block = example_blocks[path]
        chunk = example_lines[block.start:block.end + 1]
        if "/" in path:
            parent_path = path.rsplit("/", 1)[0]
            parent = config_blocks[parent_path]
            at = parent.end + 1
            # Match the indentation the target file uses under that parent --
            # a config indented with four spaces stays indented with four.
            step = block.indent - example_blocks[parent_path].indent
            chunk = _reindent(chunk, _child_indent(config_blocks, parent, step) - block.indent)
        else:
            at = append_at
            # Keep the example's own grouping: a documented or multi-line block
            # gets air around it, a bare one-liner joins the run above it.
            if first_append or block.start < block.key_line or block.end > block.key_line:
                chunk = [""] + chunk
            first_append = False
        inserts.append((at, order, chunk))

    merged_lines = list(config_lines)
    # Bottom-up so earlier insertion points stay valid; within one insertion
    # point, reverse order leaves siblings in the example's order.
    for at, _, chunk in sorted(inserts, key=lambda item: (item[0], item[1]), reverse=True):
        merged_lines[at:at] = chunk

    merged_text = newline.join(merged_lines) + newline

    before = yaml.safe_load(config_text) or {}
    try:
        after = yaml.safe_load(merged_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"merge produced invalid YAML: {exc}") from exc
    if not isinstance(after, dict):
        raise ValueError("merge produced a document that is not a mapping")
    after_flat = flatten_dict(after)
    for key, value in flatten_dict(before).items():
        if value is None:
            # An empty `foo:` is a placeholder; gaining the children the example
            # documents under it is the whole point. Nothing is being lost.
            continue
        if key not in after_flat or after_flat[key] != value:
            raise ValueError(f"merge would have changed the existing value of {key}")
    still_missing = drift(example_text, merged_text).missing
    if still_missing:
        raise ValueError(f"merge did not add: {', '.join(still_missing)}")

    return merged_text, missing


def _describe(target, drift_result):
    lines = []
    if drift_result.missing:
        lines.append(f"missing from {target} ({len(drift_result.missing)}):")
        for path in drift_result.missing:
            tag = "optional" if path in drift_result.optional else "required"
            lines.append(f"  {path:<40} ({tag})")
    if drift_result.unknown:
        lines.append(f"not in the example ({len(drift_result.unknown)}) -- left alone:")
        for path in drift_result.unknown:
            lines.append(f"  {path}")
    return lines


def _backup(path):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = Path(f"{path}.{stamp}.bak")
    shutil.copy2(path, backup)
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m reelay.config_migrate",
        description="Compare config.yaml against config_example.yaml, and migrate it forward.")
    parser.add_argument("command", choices=("check", "diff", "apply"),
                        help="check: report drift. diff: show what apply would write. "
                             "apply: merge the missing keys into config.yaml.")
    parser.add_argument("--config", default=CONFIG_PATH, help="path to config.yaml")
    parser.add_argument("--example", default=CONFIG_EXAMPLE_PATH, help="path to config_example.yaml")
    parser.add_argument("--strict", action="store_true",
                        help="check: also fail on missing optional keys and unknown keys")
    parser.add_argument("--no-backup", action="store_true", help="apply: skip the .bak copy")
    args = parser.parse_args(argv)

    example_text = _read(args.example)
    config_path = Path(args.config)

    if not config_path.exists():
        if args.command == "apply":
            config_path.write_text(example_text, encoding="utf8")
            print(f"{config_path} did not exist -- copied {args.example}. "
                  f"Fill in telegram.token before starting.")
            return 0
        print(f"{config_path} does not exist. Run `make config-init` (or "
              f"`cp {args.example} {args.config}`) to start from the example.")
        return 1

    config_text = _read(config_path)
    try:
        yaml.safe_load(config_text)
    except yaml.YAMLError as exc:
        print(f"{config_path} is not valid YAML, so nothing can be compared:\n{exc}",
              file=sys.stderr)
        return 2

    result = drift(example_text, config_text)

    if args.command == "check":
        if not result.missing and not result.unknown:
            print(f"{config_path} is up to date with {args.example}.")
            return 0
        print("\n".join(_describe(config_path, result)))
        print()
        if result.required:
            print(f"{len(result.required)} missing key(s) will stop the bot from starting. "
                  f"Run `make config-migrate`.")
            return 1
        if result.missing:
            print(f"{len(result.missing)} optional key(s) behind the example -- the bot still "
                  f"starts. `make config-migrate` adds them with their documentation.")
        return 1 if args.strict else 0

    if not result.missing:
        print(f"{config_path} is already up to date -- nothing to migrate.")
        return 0

    try:
        merged_text, inserted = merge(example_text, config_text)
    except ValueError as exc:
        # Bail out before touching the file: a config that can't be merged
        # mechanically needs a human, not a best guess at what they meant.
        print(f"cannot migrate {config_path} automatically: {exc}\n"
              f"Add the missing keys by hand from {args.example}:\n  "
              + "\n  ".join(result.missing), file=sys.stderr)
        return 2

    if args.command == "diff":
        sys.stdout.writelines(difflib.unified_diff(
            config_text.splitlines(keepends=True),
            merged_text.splitlines(keepends=True),
            fromfile=str(config_path), tofile=f"{config_path} (migrated)"))
        print(f"\n{len(inserted)} key(s) would be added. Run `make config-migrate` to apply.")
        return 0

    if not args.no_backup:
        print(f"backed up to {_backup(config_path)}")
    config_path.write_text(merged_text, encoding="utf8")
    print(f"added {len(inserted)} key(s) to {config_path}:")
    for path in inserted:
        print(f"  {path}")
    print("\nValues came from the example -- review them (secrets are left blank) "
          "and restart the bot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
