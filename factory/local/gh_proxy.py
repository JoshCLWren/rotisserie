#!/usr/bin/env python3
"""Host-side gh proxy for local factory runs.

The container has no GitHub token and no real gh. Its gh shim posts argv here.
Reads run against GitHub with the host's gh login once, then replay from the
fixture cache. Writes never reach GitHub: they are logged and answered with a
synthetic success. Label writes update a local overlay that later reads see.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

READ_SUBCOMMANDS = {
    "pr": {"view", "list", "checks", "diff", "status"},
    "issue": {"view", "list", "status"},
    "run": {"list", "view"},
    "repo": {"view"},
    "workflow": {"list", "view"},
    "label": {"list"},
    "release": {"list", "view"},
    "search": {"issues", "prs", "code", "commits", "repos"},
}
WRITE_FLAGS = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}
OUTPUT_FLAGS = {"--jq", "-q", "--template", "-t"}
OWNER_RE = re.compile(r"^factory:(unowned|local|[1-9][0-9]*)$")
STAGES = {
    "factory:building",
    "factory:review",
    "factory:changes-requested",
    "factory:ci",
    "factory:ready",
    "factory:blocked",
}


VALUE_FLAGS = {
    "-X",
    "--method",
    "-H",
    "--header",
    "-f",
    "-F",
    "--field",
    "--raw-field",
    "--input",
    "--jq",
    "-q",
    "--template",
    "-t",
    "--cache",
    "--hostname",
    "--preview",
    "-p",
}


def endpoint_of(argv: list[str]) -> str:
    rest, index = argv[1:], 0
    while index < len(rest):
        arg = rest[index]
        if arg in VALUE_FLAGS:
            index += 2
            continue
        if not arg.startswith("-"):
            return arg
        index += 1
    return ""


class State:
    def __init__(self, workdir: Path, offline: bool, seed: dict[int, list[str]]):
        self.fixtures = workdir / "fixtures"
        self.fixtures.mkdir(parents=True, exist_ok=True)
        self.writes = workdir / "writes.jsonl"
        self.misses = workdir / "misses.txt"
        self.offline = offline
        self.lock = threading.Lock()
        self.labels: dict[int, list[str]] = {}
        self.seed = seed
        self.run_id = ""
        self.worker = ""
        self.repo = ""


def is_read(argv: list[str]) -> bool:
    if not argv:
        return False
    group = argv[0]
    if group == "api":
        rest = argv[1:]
        method = "GET"
        for index, arg in enumerate(rest):
            if arg in ("-X", "--method") and index + 1 < len(rest):
                method = rest[index + 1].upper()
            elif arg.startswith("--method="):
                method = arg.split("=", 1)[1].upper()
        endpoint = endpoint_of(argv)
        if endpoint == "graphql":
            query = " ".join(rest)
            return "mutation" not in query
        if method != "GET":
            return False
        return not any(
            arg in WRITE_FLAGS or arg.startswith(("-f", "-F"))
            for arg in rest
            if arg not in ("-X", "--method")
        )
    return len(argv) > 1 and argv[1] in READ_SUBCOMMANDS.get(group, set())


def split_output_flags(argv: list[str]) -> tuple[list[str], list[str]]:
    base, output, skip = [], [], False
    for index, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        if arg in OUTPUT_FLAGS and index + 1 < len(argv):
            output += [arg, argv[index + 1]]
            skip = True
        elif any(arg.startswith(flag + "=") for flag in OUTPUT_FLAGS):
            output.append(arg)
        else:
            base.append(arg)
    return base, output


def target_number(argv: list[str]) -> int | None:
    if argv[0] == "api":
        endpoint = endpoint_of(argv)
        match = re.search(r"/(?:issues|pulls)/([0-9]+)(?:/labels)?(?:\?|$)", endpoint)
        return int(match[1]) if match else None
    if argv[0] in ("pr", "issue") and len(argv) > 2 and argv[2].isdigit():
        return int(argv[2])
    return None


def apply_labels(state: State, number: int | None, text: str) -> str:
    if number is None:
        return text
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return text
    with state.lock:
        if number not in state.labels:
            current = [
                label["name"] if isinstance(label, dict) else label
                for label in (value.get("labels", []) if isinstance(value, dict) else value)
                if isinstance(value, (dict, list))
            ]
            if number in state.seed:
                current = [
                    name for name in current if not OWNER_RE.fullmatch(name) and name not in STAGES
                ] + state.seed[number]
            state.labels[number] = current
        names = list(state.labels[number])
    if isinstance(value, dict) and "labels" in value:
        value["labels"] = [{"name": name} for name in names]
    elif isinstance(value, list) and all(
        isinstance(item, dict) and "name" in item for item in value
    ):
        value = [{"name": name} for name in names]
    else:
        return text
    return json.dumps(value)


def run_jq(output: list[str], text: str) -> tuple[int, str, str]:
    if not output:
        return 0, text, ""
    flag, expr = (output[0], output[1]) if len(output) > 1 else output[0].split("=", 1)
    if flag in ("--template", "-t"):
        return 2, "", "local gh proxy: --template is not supported\n"
    result = subprocess.run(["jq", "-r", expr], input=text, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def synthetic_run(state: State, base: list[str]) -> str | None:
    if not state.run_id or base[0] != "api":
        return None
    endpoint = endpoint_of(base)
    if not re.search(rf"actions/runs/{state.run_id}(?:\?|$)", endpoint):
        return None
    return json.dumps(
        {
            "id": int(state.run_id),
            "status": "in_progress",
            "conclusion": None,
            "display_title": f"Factory {state.worker} · fixed-model entry",
            "name": "Fixed Model Factory Entry",
            "event": "workflow_dispatch",
            "html_url": f"https://github.com/local/proxy/actions/runs/{state.run_id}",
            "path": ".github/workflows/free-model-factory-entry.yml",
            "head_branch": "main",
            "repository": {"full_name": state.repo},
            "head_repository": {"full_name": state.repo},
        }
    )


def read(state: State, argv: list[str]) -> tuple[int, str, str]:
    base, output = split_output_flags(argv)
    fake = synthetic_run(state, base)
    if fake is not None:
        return run_jq(output, fake)
    if base[0] in ("pr", "issue") and "--json" not in base and len(base) > 1 and base[1] == "view":
        base = argv
        output = []
    key = hashlib.sha256(json.dumps(base).encode()).hexdigest()[:24]
    path = state.fixtures / f"{key}.json"
    if path.exists():
        cached = json.loads(path.read_text())
    else:
        if state.offline:
            with state.lock, state.misses.open("a") as handle:
                handle.write(" ".join(base) + "\n")
            return 4, "", f"local gh proxy: no saved response for: gh {' '.join(base)}\n"
        result = subprocess.run(["gh", *base], capture_output=True, text=True, timeout=120)
        cached = {
            "argv": base,
            "code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr[-2000:],
        }
        path.write_text(json.dumps(cached))
    if cached["code"] != 0:
        return cached["code"], cached["stdout"], cached["stderr"]
    text = apply_labels(state, target_number(base), cached["stdout"])
    return run_jq(output, text)


def label_write(state: State, argv: list[str], stdin: str) -> str | None:
    number = target_number(argv)
    if number is None:
        return None
    with state.lock:
        names = state.labels.setdefault(number, [])
        if argv[0] in ("issue", "pr") and argv[1] == "edit":
            for index, arg in enumerate(argv):
                if arg == "--add-label":
                    names.extend(n for n in argv[index + 1].split(",") if n not in names)
                elif arg == "--remove-label":
                    drop = set(argv[index + 1].split(","))
                    names[:] = [n for n in names if n not in drop]
            return ""
        endpoint = endpoint_of(argv)
        method = "GET"
        for index, arg in enumerate(argv):
            if arg in ("-X", "--method"):
                method = argv[index + 1].upper()
        fields = [
            argv[index + 1]
            for index, arg in enumerate(argv[:-1])
            if arg in ("-f", "-F", "--field", "--raw-field")
        ]
        given = [field.split("=", 1)[1] for field in fields if field.startswith("labels[]=")]
        if stdin.strip():
            try:
                given += json.loads(stdin).get("labels", [])
            except (json.JSONDecodeError, AttributeError):
                pass
        if re.search(r"/labels/[^/]+$", endpoint) and method == "DELETE":
            gone = endpoint.rsplit("/", 1)[1].replace("%3A", ":").replace("%2F", "/")
            names[:] = [n for n in names if n != gone]
        elif endpoint.endswith("/labels") and method == "PUT":
            names[:] = given
        elif endpoint.endswith("/labels") and method == "POST":
            names.extend(n for n in given if n not in names)
        else:
            return None
        return json.dumps([{"name": n} for n in names])


def write(state: State, argv: list[str], stdin: str) -> tuple[int, str, str]:
    with state.lock, state.writes.open("a") as handle:
        handle.write(json.dumps({"argv": argv, "stdin": stdin[:4000]}) + "\n")
    labels = label_write(state, argv, stdin)
    if labels is not None:
        return 0, labels + ("\n" if labels else ""), ""
    if argv[:2] == ["pr", "create"]:
        return 0, "https://github.com/local/proxy/pull/999999\n", ""
    if argv[:2] in (["pr", "comment"], ["issue", "comment"]):
        return 0, "https://github.com/local/proxy/issues/0#issuecomment-1\n", ""
    if argv[0] == "api":
        return 0, "{}\n", ""
    return 0, "", ""


class Handler(BaseHTTPRequestHandler):
    state: State

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        argv, stdin = body["argv"], body.get("stdin", "")
        try:
            code, out, err = (read if is_read(argv) else lambda s, a: write(s, a, stdin))(
                self.state, argv
            )
        except Exception as exc:  # surfaced to the caller, never swallowed
            code, out, err = 5, "", f"local gh proxy error: {exc}\n"
        payload = json.dumps({"code": code, "stdout": out, "stderr": err}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--offline", action="store_true", help="never call GitHub; fail on a missing response"
    )
    parser.add_argument(
        "--seed", default="{}", help='JSON {"number": ["label", ...]} owner and stage labels'
    )
    parser.add_argument("--run-id", default="", help="local run id answered with a synthetic run")
    parser.add_argument("--worker", default="")
    parser.add_argument("--repo", default="")
    args = parser.parse_args()
    seed = {int(number): labels for number, labels in json.loads(args.seed).items()}
    Handler.state = State(args.workdir, args.offline, seed)
    Handler.state.run_id, Handler.state.worker = args.run_id, args.worker
    Handler.state.repo = args.repo
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(
        f"local gh proxy on :{args.port} ({'offline' if args.offline else 'read-through'})",
        file=sys.stderr,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
