import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from connectors import routes as connector_routes
from connectors.routes import _load_label_history


class ConnectorRoutesTests(unittest.TestCase):
    def test_label_history_reads_tail_per_connector(self):
        with tempfile.TemporaryDirectory() as d:
            log_dir = Path(d)
            screen_dir = log_dir / "screen"
            email_dir = log_dir / "email"
            screen_dir.mkdir()
            email_dir.mkdir()

            screen_path = screen_dir / "filtered.jsonl"
            with screen_path.open("w") as f:
                for i in range(100):
                    f.write(json.dumps({
                        "timestamp": i,
                        "text": f"screen {i}",
                        "source_name": "screen",
                        "prediction_event": True,
                    }) + "\n")

            email_path = email_dir / "filtered.jsonl"
            with email_path.open("w") as f:
                for i in range(95, 105):
                    f.write(json.dumps({
                        "timestamp": i,
                        "text": f"email {i}",
                        "source_name": "email",
                        "prediction_event": False,
                    }) + "\n")

            with patch("connectors.routes._tail_lines", wraps=connector_routes._tail_lines) as tail:
                result = _load_label_history(log_dir, 5)

            self.assertEqual([entry["timestamp"] for entry in result], [100, 101, 102, 103, 104])
            self.assertEqual(result[0]["text"], "[email] email 100")
            self.assertEqual(tail.call_count, 2)
            self.assertTrue(all(call.args[1] == 5 for call in tail.call_args_list))

    def test_label_history_skips_malformed_tail_rows(self):
        with tempfile.TemporaryDirectory() as d:
            log_dir = Path(d)
            screen_dir = log_dir / "screen"
            screen_dir.mkdir()
            (screen_dir / "filtered.jsonl").write_text(
                "\n".join([
                    json.dumps({"timestamp": 1, "text": "ok", "prediction_event": True}),
                    "{bad json",
                    json.dumps({"timestamp": 2, "text": "newer", "prediction_event": True}),
                ]) + "\n"
            )

            result = _load_label_history(log_dir, 10)

            self.assertEqual([entry["text"] for entry in result], ["ok", "newer"])


if __name__ == "__main__":
    unittest.main()
