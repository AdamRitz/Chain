import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch, MagicMock

SPEC = importlib.util.spec_from_file_location("benchmark", Path(__file__).with_name("benchmark.py"))
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeResponse:
    status = 200

    def read(self):
        return json.dumps({"result": "0x10"}).encode()


class FakeConnection:
    created = []

    def __init__(self, *args, **kwargs):
        self.sock = None
        self.requests = []
        self.closed = False
        FakeConnection.created.append(self)

    def request(self, *args):
        self.requests.append(args)

    def getresponse(self):
        return FakeResponse()

    def close(self):
        self.closed = True


class BenchmarkTests(unittest.TestCase):
    def test_rpc_reuses_host_connection_and_preserves_group_parameters(self):
        FakeConnection.created.clear()
        with tempfile.TemporaryDirectory() as directory, patch.object(MODULE.ssl, "create_default_context", return_value=MagicMock()), \
                patch.object(MODULE.http.client, "HTTPConnection", FakeConnection):
            rpc = MODULE.Rpc(["127.0.0.1"], 20200, Path(directory), 2, Path(directory), scheme="http")
            try:
                self.assertEqual(rpc.call("127.0.0.1", "getBlockNumber")["result"], "0x10")
                rpc.call("127.0.0.1", "getBlockNumber")
                self.assertEqual(len(FakeConnection.created), 1)
                connection = FakeConnection.created[0]
                self.assertEqual(len(connection.requests), 2)
                self.assertEqual(json.loads(connection.requests[0][2])["params"], ["group0", ""])
            finally:
                rpc.close()
            self.assertTrue(connection.closed)

    def test_fixed_window_count_difference_and_acquisition_span(self):
        a = {"complete": True, "count_min": 1000, "bracket_begin_monotonic": 100,
             "bracket_end_monotonic": 100.02}
        b = {"complete": True, "count_min": 241000, "bracket_begin_monotonic": 130,
             "bracket_end_monotonic": 130.02}
        result = MODULE.boundaries(a, b)
        self.assertAlmostEqual(result["tps"], 8000)
        self.assertAlmostEqual(result["measurement_seconds"], 30)
        self.assertTrue(result["endpoint_span_limit_passed"])
        b["bracket_end_monotonic"] = 131
        self.assertFalse(MODULE.boundaries(a, b)["endpoint_span_limit_passed"])

    def test_incomplete_endpoint_has_no_throughput(self):
        self.assertIsNone(MODULE.boundaries({"complete": False}, {})["tps"])

    def test_balance_reconstruction_uses_sent_indices_not_prefix(self):
        # Only index0 (account0->1) and index2 (account2->3) were submitted.
        offsets = b"".join(struct.pack(">q", value) for value in [1, -1, 3, -1])
        expected, count = MODULE.expected_balances(offsets, 4)
        self.assertEqual(count, 2)
        self.assertEqual(expected, [999999999, 1000000001, 999999999, 1000000001])
        with self.assertRaises(ValueError):
            MODULE.expected_balances(b"x", 4)

    def test_sdk_four_ingress_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            MODULE.sdk_config(path, Path("/certs"), ["1", "2", "3", "4"], 20200, Path("/account"))
            text = path.read_text()
            self.assertIn('peers = ["1:20200", "2:20200", "3:20200", "4:20200"]', text)
            self.assertIn('disableSsl = "false"', text)

    def test_missing_latency_remains_null(self):
        self.assertIsNone(MODULE.percentile([], .95))
        self.assertEqual(MODULE.percentile([100, 200, 300], .95), 300)


if __name__ == "__main__":
    unittest.main()
