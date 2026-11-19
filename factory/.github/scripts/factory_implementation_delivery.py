"""Conservative mechanical evidence that a PR changes executable behavior.

This check is intentionally not a correctness proof. The independent exact-head
reviewer still has to verify each linked-issue requirement and its tests.
"""

from __future__ import annotations

import ast
import difflib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from tree_sitter import Node

DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
BEHAVIORAL_FILENAMES = frozenset({"dockerfile", "containerfile", "makefile", "gnumakefile"})
SOURCE_SUFFIXES = (
    ".py", ".rs", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".sh", ".sql", ".html", ".css", ".scss", ".vue", ".svelte",
    ".yml", ".yaml", ".json", ".toml", ".ini", ".cfg",
)
TYPE_LINE = re.compile(
    r"(?im)^\s*(?:\*\*)?(?:issue\s+type|deliverable\s+type|"
    r"work\s+type)\s*:(?:\*\*)?\s*(?:\*\*)?"
    r"(docs-only|documentation-only|documentation|investigation|"
    r"planning|prd|acceptance-only|test-only)(?:\*\*)?\s*$"
)
UNPARSED = "__UNPARSED_SOURCE__"
PATCH_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")


def is_implementation_issue(issue_body: str | None) -> bool:
    """Classify using a structured declaration, not arbitrary issue prose.

    Args:
        issue_body: Authoritative linked issue description.

    Returns:
        True by default, or False for an explicitly typed non-code ticket.
    """
    body = issue_body or ""
    match = TYPE_LINE.search(body)
    return match is None


def _is_docs_path(path: str) -> bool:
    """Whether the file can only serve as documentation."""
    normalized = path.lower().strip()
    return normalized.startswith("docs/") or normalized.endswith(DOC_SUFFIXES)


def _is_test_path(path: str) -> bool:
    """Whether a file contains tests rather than deliverable production code."""
    normalized = path.lower()
    name = normalized.rsplit("/", 1)[-1]
    return (
        normalized.startswith(("tests/", "frontend/src/test/", "frontend/src/unit/"))
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
    )


def substantive_changed_files(changed_files: Iterable[str]) -> list[str]:
    """Find candidate behavioral source files, excluding docs and tests.

    Args:
        changed_files: Paths reported by the reviewed PR diff.

    Returns:
        Source file paths that could contain the requested implementation.
    """
    return [
        path for path in changed_files
        if path.strip()
        and (path.lower().endswith(SOURCE_SUFFIXES)
             or path.lower().rsplit("/", 1)[-1] in BEHAVIORAL_FILENAMES
             or path.lower().endswith((".dockerfile", ".containerfile")))
        and not _is_docs_path(path)
        and not _is_test_path(path)
    ]


class _WithoutDocstrings(ast.NodeTransformer):
    """Remove only actual Python documentation expression nodes."""

    def visit(self, node: ast.AST) -> ast.AST:
        """Strip leading docstrings while preserving behavioral string literals.

        Args:
            node: Python syntax node being traversed.

        Returns:
            Node with only documentation expressions removed.
        """
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:]
        return super().visit(node)




def _shell_tree(value: object) -> object:
    """Remove AST positions/comments while retaining literal and executable nodes."""
    if isinstance(value, list):
        cleaned = [_shell_tree(item) for item in value]
        merged: list[object] = []
        for item in cleaned:
            if (isinstance(item, dict) and item.get("Type") == "Lit" and merged
                    and isinstance(merged[-1], dict) and merged[-1].get("Type") == "Lit"):
                previous = merged[-1]
                previous["Value"] = str(previous.get("Value", "")) + str(item.get("Value", ""))
            else:
                merged.append(item)
        return merged
    if not isinstance(value, dict):
        return value
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or key in {"Comments", "Last"}:
            continue
        if (isinstance(item, dict) and set(item) <= {"Offset", "Line", "Col"}
                and isinstance(item.get("Offset"), int)):
            continue
        result[key] = _shell_tree(item)
    if result.get("Type") == "SglQuoted" and not result.get("Dollar"):
        text = result.get("Value")
        if isinstance(text, str) and not any(char in text for char in "\\$`"):
            return {"Type": "QuotedLiteral", "Value": text}
    if result.get("Type") == "DblQuoted" and not result.get("Dollar"):
        parts = result.get("Parts")
        if isinstance(parts, list) and len(parts) == 1 and isinstance(parts[0], dict):
            part = parts[0]
            text = part.get("Value")
            if part.get("Type") == "Lit" and isinstance(text, str):
                if not any(char in text for char in "\\$`"):
                    return {"Type": "QuotedLiteral", "Value": text}
    if "Hdoc" in value:
        # Delimiter spelling is formatting; quotedness controls expansion.
        word = value.get("Word")
        quoted = isinstance(word, dict) and any(
            part.get("Type") != "Lit" for part in word.get("Parts", [])
            if isinstance(part, dict)
        )
        result["Word"] = {"quoted": quoted}
        if value.get("Op") == "<<-":
            result["Op"] = "<<"
            hdoc = value.get("Hdoc")
            if isinstance(hdoc, dict):
                stripped = dict(hdoc)
                parts: list[object] = []
                for part in hdoc.get("Parts", []):
                    if isinstance(part, dict) and part.get("Type") == "Lit":
                        part = dict(part)
                        text = str(part.get("Value", ""))
                        pos = part.get("Pos")
                        if isinstance(pos, dict) and pos.get("Col") == 1:
                            text = text.lstrip("\t")
                        part["Value"] = re.sub(r"\n\t+", "\n", text)
                    parts.append(part)
                stripped["Parts"] = parts
                result["Hdoc"] = _shell_tree(stripped)
    return result


def _shell_syntax(source: str) -> str:
    """Parse Bash without execution, using the pinned formatter's typed AST."""
    binary = Path(sys.executable).parent / "shfmt"
    try:
        completed = subprocess.run(
            [str(binary), "-ln", "bash", "--to-json"], input=source,
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Bash parser unavailable; install factory syntax dependencies") from exc
    if completed.returncode:
        return UNPARSED
    lines = source.splitlines()
    shebang = lines[0] if lines and lines[0].startswith("#!") else None
    return json.dumps((shebang, _shell_tree(json.loads(completed.stdout))), sort_keys=True)


def _syntax_node(node: Node, source: bytes) -> object:
    """Compare grammar nodes without comments, positions, or optional separators."""
    if node.type == "comment" or not node.is_named and node.type in {";", "'", '\"'}:
        return None
    if not node.children:
        # Empty grammar roots and roots whose comments are filtered are equal.
        if node.parent is None:
            return (node.type, [])
        return (node.type, source[node.start_byte:node.end_byte].decode())
    children: list[object] = []
    for index, child in enumerate(node.children):
        signature = _syntax_node(child, source)
        if signature is not None:
            children.append((node.field_name_for_child(index), signature))
    if node.type == "arrow_function":
        # TypeScript's recovery grammar accepts an invalid newline after async.
        # Native node boundaries still identify the exact restricted gap,
        # including generic parameters and nested return annotations.
        for index, child in enumerate(node.children[:-1]):
            if child.type == "async":
                following = next(
                    child for child in node.children[index + 1:] if child.type != "comment"
                )
                gap = source[child.end_byte:following.start_byte].decode()
                children.append(("async_line_terminator", any(
                    char in gap for char in "\n\r\u2028\u2029"
                )))
                break
    return (node.type, children)


def _code_tokens(source: str, path: str) -> str:
    """Compare parsed language structure while preserving literal input data."""
    suffix = Path(path.lower()).suffix
    if suffix == ".py":
        try:
            tree = _WithoutDocstrings().visit(ast.parse(source))
        except SyntaxError:
            return UNPARSED
        return ast.dump(tree, include_attributes=False)
    if suffix == ".sh":
        return _shell_syntax(source)
    if suffix in {".css", ".scss", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_css
            import tree_sitter_javascript
            import tree_sitter_typescript
        except ImportError as exc:
            raise RuntimeError(
                "Syntax parsers unavailable; run uv sync or the factory-syntax action"
            ) from exc
        grammars = {
            ".css": tree_sitter_css.language, ".scss": tree_sitter_css.language,
            ".js": tree_sitter_javascript.language, ".jsx": tree_sitter_javascript.language,
            ".mjs": tree_sitter_javascript.language, ".cjs": tree_sitter_javascript.language,
            ".ts": tree_sitter_typescript.language_typescript,
            ".tsx": tree_sitter_typescript.language_tsx,
        }
        parser = Parser(Language(grammars[suffix]()))
        encoded = source.encode()
        root = parser.parse(encoded).root_node
        if root.has_error:
            # A broken/unsupported tree is not enough evidence to distinguish
            # two edits. A repair to a fully parsed tree still has a delta.
            return UNPARSED
        return repr(_syntax_node(root, encoded))
    name = Path(path).name.lower()
    hash_comments = suffix in {".yml", ".yaml", ".toml", ".ini", ".cfg"}
    hash_comments |= name in BEHAVIORAL_FILENAMES
    comments = r"\#[^\n]*" if hash_comments else r"/\*.*?\*/|//[^\n]*"
    if suffix == ".sql":
        comments = r"/\*.*?\*/|--[^\n]*"
    if suffix in {".html", ".vue", ".svelte"}:
        comments += r"|<!--.*?-->"
    pattern = re.compile(
        r"(?P<string>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`)"
        + r"|(?P<comment>" + comments + r")|(?P<space>\s+)"
        + r"|(?P<code>[\w$]+|\|\||&&|<<=?|>>=?|[!<>=]=|[-+*/%]=|::|->>?|[+][+]|--|.)",
        re.DOTALL,
    )
    if suffix in {".yml", ".yaml"} or name in {"makefile", "gnumakefile"}:
        code = "".join(match.group() for match in pattern.finditer(source)
                       if match.lastgroup != "comment")
        return repr([line.rstrip() for line in code.splitlines() if line.strip()])
    return repr([match.group() for match in pattern.finditer(source)
                 if match.lastgroup not in {"comment", "space"}])

def _has_code_delta(patch: str, path: str) -> bool:
    """Compare old/new complete sources, failing closed on incomplete Python."""
    old: list[str] = []
    new: list[str] = []
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("@@"):
            # Production callers provide complete-file context. A partial
            # hunk cannot establish whether its first line is inside a comment.
            if in_hunk or not re.match(r"@@ -[01](?:,\d+)? \+[01](?:,\d+)? @@", line):
                return False
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            new.append(line[1:])
        elif in_hunk and line.startswith("-"):
            old.append(line[1:])
        elif in_hunk and line.startswith(" "):
            old.append(line[1:])
            new.append(line[1:])
    if not in_hunk:
        return False
    try:
        old_syntax = _code_tokens("\n".join(old), path)
        new_syntax = _code_tokens("\n".join(new), path)
        return new_syntax != UNPARSED and old_syntax != new_syntax
    except (SyntaxError, ValueError):
        return False


def _file_patches(diff_text: str) -> dict[str, str]:
    """Split a unified Git diff into authoritative per-file patches."""
    patches: dict[str, str] = {}
    current = ""
    lines: list[str] = []
    for line in diff_text.splitlines():
        header = PATCH_HEADER.match(line)
        if header:
            if current:
                patches[current] = "\n".join(lines)
            current = header.group(2)
            lines = [line]
        elif current:
            lines.append(line)
    if current:
        patches[current] = "\n".join(lines)
    return patches


def pr_delivers_implementation(
    issue_body: str | None,
    changed_files: Iterable[str],
    diff_text: str | None = None,
) -> tuple[bool, str]:
    """Reject non-delivering implementations without judging feature correctness.

    Args:
        issue_body: Exact linked-issue body.
        changed_files: File paths of the current PR head.
        diff_text: Full unified patch for that same PR head, if available.

    Returns:
        Whether the mechanical behavioral-diff prerequisite is satisfied,
        plus an actionable reason. Missing diff never counts as evidence.
    """
    if not is_implementation_issue(issue_body):
        return True, "explicit non-implementation issue type; documentation allowed"
    candidates = substantive_changed_files(changed_files)
    if not candidates:
        return False, "implementation issue has no changed production source files"
    if diff_text is None:
        return False, "unavailable source diff; cannot verify executable changes"
    patches = _file_patches(diff_text)
    for path in candidates:
        if path in patches and _has_code_delta(patches[path], path):
            return True, "production executable code changed; semantic review required"
    return (
        False,
        "only documentation, comments, docstrings or formatting changed "
        "in production source files; executable delivery is unverified",
    )



def _previous_source(current: str, patch: str) -> str:
    """Reverse GitHub's patch against complete head source, validating every hunk."""
    lines = current.splitlines()
    previous: list[str] = []
    cursor = 0
    entries = patch.splitlines()
    index = 0
    while index < len(entries):
        header = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*", entries[index])
        if not header:
            raise RuntimeError("missing or malformed source patch hunk")
        start = int(header[3]) - (int(header[4] or 1) > 0)
        if start < cursor or start > len(lines):
            raise RuntimeError("source patch has invalid hunk coordinates")
        previous.extend(lines[cursor:start])
        cursor = start
        old_count = new_count = 0
        index += 1
        while index < len(entries) and not entries[index].startswith("@@"):
            entry = entries[index]
            index += 1
            if entry.startswith("\\ No newline"):
                continue
            if not entry or entry[0] not in " +-":
                raise RuntimeError("unrecognized source patch line")
            if entry[0] in " +":
                if cursor >= len(lines) or lines[cursor] != entry[1:]:
                    raise RuntimeError("source patch does not match immutable head")
                cursor += 1
                new_count += 1
            if entry[0] in " -":
                previous.append(entry[1:])
                old_count += 1
        if old_count != int(header[2] or 1) or new_count != int(header[4] or 1):
            raise RuntimeError("incomplete source patch; cannot verify delivery")
    previous.extend(lines[cursor:])
    return "\n".join(previous)


def _base_source(
    repo: str, path: str, source_sha: str,
    run: Callable[[list[str]], str],
) -> str:
    """Fetch complete file content from a verified PR merge-base commit.

    GitHub omits the `patch` field for changes too large to inline (a generated
    OpenAPI artifact is the common case). The base commit still holds the whole
    pre-change file. Use the PR merge base, not the moving base-branch tip, so
    changes on main cannot be misattributed to the pull request.

    Args:
        repo: Repository owner/name.
        path: Repository-relative file path.
        source_sha: Verified merge-base commit for this pull request.
        run: GitHub CLI reader that raises RuntimeError on failure.

    Returns:
        The complete file content at the merge-base commit.

    Raises:
        RuntimeError: If the base content cannot be read.
    """
    return run([
        "api", "-H", "Accept: application/vnd.github.raw+json",
        f"repos/{repo}/contents/{quote(path, safe='/')}?ref={source_sha}",
    ])


def _unified_file_diff(path: str, old: str, new: str) -> str:
    """Render one complete unified diff between two whole file versions.

    Args:
        path: Repository-relative file path.
        old: Complete pre-change file content.
        new: Complete post-change file content.

    Returns:
        A unified diff prefixed with the git header the patch parser expects.
    """
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    context_lines = max(len(old_lines), len(new_lines)) + 1
    return (
        f"diff --git a/{path} b/{path}\n" + "".join(difflib.unified_diff(
            [line + "\n" for line in old_lines],
            [line + "\n" for line in new_lines],
            fromfile=f"a/{path}", tofile=f"b/{path}",
            n=context_lines,
        ))
    )


def complete_pr_diff(
    pr_number: int, expected_head: str, repo: str,
    run: Callable[[list[str]], str],
) -> tuple[list[str], str]:
    """Fetch full source context and reconstruct the PR's original base.

    Args:
        pr_number: Open or merged pull request number.
        expected_head: Commit being reviewed.
        repo: Repository owner/name.
        run: GitHub CLI reader that raises RuntimeError on failure.

    Returns:
        Changed paths and a unified diff with complete source context.

    Raises:
        RuntimeError: If the head changes or complete source is unavailable.
    """
    info = json.loads(run(["api", f"repos/{repo}/pulls/{pr_number}"]))
    if info["head"]["sha"] != expected_head:
        raise RuntimeError("PR head changed during implementation inspection")
    base_sha = info.get("base", {}).get("sha")
    merge_base_sha: str | None = None
    files = json.loads(run([
        "api", "--paginate", "--slurp", f"repos/{repo}/pulls/{pr_number}/files",
    ]))
    records = [record for page in files for record in page]
    paths = [record["filename"] for record in records]
    candidates = set(substantive_changed_files(paths))
    patches: list[str] = []
    for record in records:
        path = record["filename"]
        if path not in candidates or record["status"] == "renamed" and not record.get("changes"):
            continue
        status = record["status"]
        new = "" if status == "removed" else run([
            "api", "-H", "Accept: application/vnd.github.raw+json",
            f"repos/{repo}/contents/{quote(path, safe='/')}?ref={expected_head}",
        ])
        patch = record.get("patch")
        if isinstance(patch, str) and patch:
            # Reverse the original PR patch rather than comparing against current
            # main, which can already contain the merged head during closure.
            old = _previous_source(new, patch)
        else:
            # GitHub omits patches for oversized changes. Only a verified merge
            # base identifies the pre-change file: base.sha moves as main advances.
            if not isinstance(base_sha, str) or not base_sha:
                raise RuntimeError(f"source patch unavailable for {path}")
            base_path = record.get("previous_filename") or path
            if status == "added":
                old = ""
            else:
                try:
                    if merge_base_sha is None:
                        comparison = json.loads(run([
                            "api",
                            f"repos/{repo}/compare/{base_sha}...{expected_head}",
                        ]))
                        ancestor = comparison.get("merge_base_commit", {}).get("sha")
                        if not isinstance(ancestor, str) or not ancestor:
                            raise RuntimeError("missing PR merge base")
                        if ancestor == expected_head:
                            raise RuntimeError("PR head is already in base branch")
                        merge_base_sha = ancestor
                    old = _base_source(repo, base_path, merge_base_sha, run)
                except (RuntimeError, ValueError, TypeError, AttributeError) as exc:
                    raise RuntimeError(f"source patch unavailable for {path}") from exc
        patches.append(_unified_file_diff(path, old, new))
    return paths, "\n".join(patches)
