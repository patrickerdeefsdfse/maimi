"""Exercise the candidate retention guard's actual AST, without application I/O."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


class RetentionMediaTests(unittest.TestCase):
    def test_all_native_json_spellings_reach_retention_guard(self):
        source = Path(__file__).resolve().parents[1] / "app/personal_cloud.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        nodes = [node for node in ast.walk(tree) if isinstance(node, ast.If)
                 and "request.headers.get('content-type', '')" in ast.unparse(node.test)
                 and "path.startswith('/api/')" in ast.unparse(node.test)]
        self.assertEqual(len(nodes), 1)
        expression = compile(ast.Expression(body=nodes[0].test), str(source), "eval")
        for media, expected in (
            ("application/json", True),
            ("Application/JSON", True),
            ("APPLICATION/JSON; charset=utf-8", True),
            (" application/json ; charset=utf-8", True),
            ("application/octet-stream", False),
            ("text/json", False),
            ("", False),
        ):
            with self.subTest(media=media):
                request = SimpleNamespace(method="POST", headers={"content-type": media})
                self.assertIs(eval(expression, {"request": request, "path": "/api/workspace/archiveSession"}), expected)


if __name__ == "__main__":
    unittest.main()
