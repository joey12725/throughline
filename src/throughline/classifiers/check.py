"""Static checks of classifier code and of message classifier specs, before
anything runs. Every problem comes back as a plain sentence with a line
number, so an agent can fix it from the message alone."""

import ast

from .runtime.throughline_classify import ALLOWED_IMPORTS, GRAINS

FORBIDDEN_NAMES = frozenset(
    {
        "open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars",
        "getattr", "setattr", "delattr", "input", "breakpoint", "memoryview", "exit", "quit",
        "help", "__builtins__",
    }
)
MAX_CODE_CHARS = 60000


def check_code(code):
    """{ok, errors: [..], warnings: [..], grain, function}."""
    errors, warnings = [], []
    out = {"ok": False, "errors": errors, "warnings": warnings, "grain": None, "function": None}
    if not isinstance(code, str) or not code.strip():
        errors.append("code is empty")
        return out
    if len(code) > MAX_CODE_CHARS:
        errors.append(f"code is longer than {MAX_CODE_CHARS} characters")
        return out
    try:
        tree = ast.parse(code, filename="classifier.py")
    except SyntaxError as exc:
        errors.append(f"line {exc.lineno}: syntax error: {exc.msg}")
        return out
    candidates = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.level:
                errors.append(f"line {node.lineno}: relative imports are not allowed")
                continue
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                if name.split(".")[0] not in ALLOWED_IMPORTS:
                    allowed = ", ".join(sorted(ALLOWED_IMPORTS - {"throughline_classify"}))
                    errors.append(f"line {node.lineno}: import of {name!r} is not allowed (allowed: {allowed})")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            errors.append(f"line {node.lineno}: {node.id} is not available in a classifier")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            errors.append(f"line {node.lineno}: attributes starting with __ are not allowed ({node.attr})")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            warnings.append(f"line {node.lineno}: global/nonlocal state does not survive between runs")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await)):
            errors.append(f"line {node.lineno}: async code is not supported")
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            grain = None
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(target, ast.Name) and target.id == "classifier":
                    grain = "thread"
                    if isinstance(dec, ast.Call):
                        for kw in dec.keywords:
                            if kw.arg == "grain":
                                if isinstance(kw.value, ast.Constant) and kw.value.value in GRAINS:
                                    grain = kw.value.value
                                else:
                                    errors.append(f"line {dec.lineno}: grain must be one of {', '.join(GRAINS)}")
                            else:
                                errors.append(f"line {dec.lineno}: @classifier takes only grain=")
            if grain or node.name == "classify":
                candidates.append((node, grain or "thread", bool(grain)))
    decorated = [c for c in candidates if c[2]]
    chosen = decorated or candidates
    if len(chosen) != 1:
        errors.append(
            "define exactly one classifier: a top-level function decorated @classifier(grain=...) "
            "(or one function named classify) taking (thread, ctx)"
            + (f"; found {len(chosen)}" if chosen else "")
        )
    else:
        fn, grain, _ = chosen[0]
        out["grain"], out["function"] = grain, fn.name
        params = [a.arg for a in fn.args.args]
        if len(params) != 2:
            errors.append(f"line {fn.lineno}: {fn.name} must take exactly two arguments (thread, ctx)")
        if not any(isinstance(n, ast.Return) and n.value is not None for n in ast.walk(fn)):
            warnings.append(f"line {fn.lineno}: {fn.name} never returns marks; return [] when there is nothing to write")
    out["ok"] = not errors
    return out
