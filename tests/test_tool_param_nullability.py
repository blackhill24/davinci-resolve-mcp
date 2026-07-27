"""#143 finding 7: an optional param must be annotated Optional, not bare.

A tool that declares ``composite_mode: str = None`` emits the self-contradictory
schema ``{"type": "string", "default": null}``, and the SDK's pydantic arg model
rejects an explicit ``null`` *before the body runs*. That matters because these
bodies' entire contract is "None means not set" — and hosts that fill unused
optional properties with explicit nulls (OpenAI-style strict function calling
does exactly this) then get an opaque pydantic type error instead of the
operation. ``set_timeline_item_composite(timeline_item_id="X",
composite_mode=null, opacity=0.5)`` could not set opacity at all.

The sweep is the real test: this class is mechanical and recurs, so pin the
whole surface rather than the eight tools that happened to have it.
"""

from __future__ import annotations

import ast
import os
import unittest

_SRC_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"
)


def _is_nullable_annotation(annotation: ast.expr) -> bool:
    """Whether the annotation already admits None."""
    text = ast.unparse(annotation)
    return "Optional" in text or "None" in text or text == "Any"


def _decorated_as_mcp_entry_point(node: ast.AST) -> bool:
    for decorator in getattr(node, "decorator_list", []):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr in ("tool", "resource"):
            return True
        if isinstance(target, ast.Name) and target.id in ("tool", "resource"):
            return True
    return False


def _offenders():
    """(file, line, tool, param, annotation) for every non-nullable `= None`."""
    found = []
    for root, _dirs, files in os.walk(_SRC_ROOT):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as handle:
                try:
                    tree = ast.parse(handle.read())
                except SyntaxError:  # pragma: no cover
                    continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not _decorated_as_mcp_entry_point(node):
                    continue
                args = node.args
                positional = args.args
                offset = len(positional) - len(args.defaults)
                pairs = [
                    (positional[offset + i], default)
                    for i, default in enumerate(args.defaults)
                ]
                pairs += [
                    (arg, default)
                    for arg, default in zip(args.kwonlyargs, args.kw_defaults)
                    if default is not None
                ]
                for arg, default in pairs:
                    if not (isinstance(default, ast.Constant) and default.value is None):
                        continue
                    if arg.annotation is None or _is_nullable_annotation(arg.annotation):
                        continue
                    found.append((
                        os.path.relpath(path, os.path.dirname(_SRC_ROOT)),
                        node.lineno,
                        node.name,
                        arg.arg,
                        ast.unparse(arg.annotation),
                    ))
    return found


class ToolParamNullabilityTest(unittest.TestCase):
    def test_no_tool_param_is_non_nullable_with_a_none_default(self):
        offenders = _offenders()
        report = "\n".join(
            f"  {f}:{line} {tool}({param}: {ann} = None) -> Optional[{ann}]"
            for f, line, tool, param, ann in offenders
        )
        self.assertEqual(
            [], offenders,
            "params defaulting to None must be annotated Optional[...]; an "
            "explicit null is otherwise rejected before the body runs:\n" + report,
        )

    def test_the_sweep_would_catch_a_regression(self):
        # Guard the guard: a detector that silently matches nothing is worse
        # than no detector.
        module = ast.parse(
            "@mcp.tool()\n"
            "def t(a: str, b: str = None, c: Optional[str] = None, d: int = 3):\n"
            "    pass\n"
        )
        node = module.body[0]
        self.assertTrue(_decorated_as_mcp_entry_point(node))
        args = node.args
        offset = len(args.args) - len(args.defaults)
        flagged = [
            args.args[offset + i].arg
            for i, default in enumerate(args.defaults)
            if isinstance(default, ast.Constant)
            and default.value is None
            and args.args[offset + i].annotation is not None
            and not _is_nullable_annotation(args.args[offset + i].annotation)
        ]
        self.assertEqual(["b"], flagged)


class ExplicitNullIsAcceptedTest(unittest.TestCase):
    """End-to-end on the tool the issue used as its worked example."""

    def test_set_timeline_item_composite_accepts_an_explicit_null(self):
        from mcp.server.fastmcp.utilities.func_metadata import func_metadata

        from src.granular import timeline_item

        fn = timeline_item.set_timeline_item_composite
        fn = getattr(fn, "__wrapped__", fn)
        meta = func_metadata(fn)

        schema = meta.arg_model.model_json_schema()["properties"]["composite_mode"]
        self.assertIn("anyOf", schema, f"schema still non-nullable: {schema}")

        # The case that used to raise ValidationError before the body ran.
        model = meta.arg_model.model_validate(
            {"timeline_item_id": "X", "composite_mode": None, "opacity": 0.5}
        )
        self.assertIsNone(model.composite_mode)
        self.assertEqual(0.5, model.opacity)


if __name__ == "__main__":
    unittest.main()
