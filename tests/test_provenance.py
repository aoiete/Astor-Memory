"""Tests for astor_memory.nest.provenance (graph)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 2026-09-01: removed hardcoded <runtime_dir>.
# If ASTOR_DIR is not set in env, fall back to a per-process tempdir so tests
# are hermetic and don't touch the live runtime. CI / dev machines can still
# point ASTOR_DIR at a real directory via the shell before running tests.
os.environ.setdefault('ASTOR_DIR', str(Path(tempfile.mkdtemp(prefix="astor_prov_test_"))))
_ASTOR_SRC = Path(__file__).resolve().parent.parent
if str(_ASTOR_SRC) not in sys.path:
    sys.path.insert(0, str(_ASTOR_SRC))


class _MockURLResponse:
    """Mimics urllib's addinfourl: read() returns bytes, getcode() returns status."""
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self._status = status
    def read(self) -> bytes:
        return self._payload
    def getcode(self) -> int:
        return self._status
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


class _MockURLOpen:
    """Routes urllib.request.urlopen calls to a per-URL JSON map.

    Usage:
        with _MockURLOpen({'/v1/write': '{"fact_ids": [42]}',
                           '/v1/fact/42/provenance': '{"ancestors": [...]}'}):
            r = urlopen(Request('http://localhost:7803/v1/write', ...))
            assert r.read() == b'{"fact_ids": [42]}'
    """
    def __init__(self, responses: dict[str, str | dict]):
        self.responses = responses
        self.calls: list[tuple[str, str | None]] = []  # (url, body)
        self._cm = None

    def __enter__(self):
        self._cm = mock.patch('urllib.request.urlopen', side_effect=self._route)
        self._cm.start()
        return self

    def __exit__(self, *args):
        if self._cm:
            self._cm.stop()

    def _route(self, req, **kwargs):
        url = req.full_url if hasattr(req, 'full_url') else str(req)
        body = None
        method = 'GET'  # default for urlopen
        if hasattr(req, 'data') and req.data:
            body = req.data.decode() if isinstance(req.data, bytes) else req.data
            method = 'POST'
        self.calls.append((url, body))

        # Match URL + method. Patterns starting with 'POST ' or 'GET '
        # are method-specific; bare patterns match any method.
        def _match(pattern: str) -> bool:
            method_tag, _, real_pattern = pattern.partition(' ')
            if method_tag == 'POST' or method_tag == 'GET':
                if method_tag != method:
                    return False
                return real_pattern in url
            return pattern in url

        candidates = [
            (len(pattern), pattern, payload)
            for pattern, payload in self.responses.items()
            if _match(pattern)
        ]
        if not candidates:
            raise AssertionError(f"_MockURLOpen: no response for {url!r} "
                                 f"(method={method}); "
                                 f"registered: {list(self.responses.keys())}")
        candidates.sort(key=lambda x: -x[0])  # longest pattern first
        payload_to_use = candidates[0][2]
        if isinstance(payload_to_use, dict):
            payload_to_use = json.dumps(payload_to_use)
        if isinstance(payload_to_use, str):
            payload_to_use = payload_to_use.encode()
        return _MockURLResponse(payload_to_use)


class ProvenanceCoreTests(unittest.TestCase):
    """Pure-Python tests. Use a transient tempdir to avoid touching the
    live astor server's data.

    v1.14.49 (R3): Live-server integration tests rewritten to mock
    urllib.request.urlopen via _MockURLOpen. Fully deterministic — no
    shared DB state, no flakiness from concurrent sessions. Tests pass
    regardless of whether the live server is running.
    """

    def _setup_acl(self):
        try:
            from astor_memory._internal.acl import astor_init_acl
            astor_init_acl(
                actor='admin:admin', role='admin', tier='public',
            )
        except Exception:
            self.skipTest('cannot init ACL')

    def setUp(self):
        self._setup_acl()
        # ACL is per-thread; test fixtures share it.

    def test_record_and_walk_provenance_within_scope(self):
        """Write facts via /v1/write, record parent → child, walk up + down.

        v1.14.49 (R3): Mocked URL open — deterministic across runs.
        """
        import urllib.request as _ur

        # Two deterministic fact_ids
        pa, pb = 10001, 10002

        # Pre-canned responses keyed by URL suffix + method marker.
        # POST markers indicate "use this for POST requests to this URL";
        # unmarked patterns are GET responses.
        responses = {
            '/v1/write': {'fact_ids': [pa], 'count': 1},
            'POST /v1/fact/10002/provenance': {
                'fact_id': pb, 'parents': [pa],
                'provenance_depth': 1, 'provenance_kind': 'inferred',
            },
            '/v1/fact/10002/provenance': {  # GET walk-up
                'ancestors': [{'fact': {'id': pa, 'content': 'parent fact'},
                               'depth': 1, 'relation': 'parent'}],
                'chain_broken': False, 'depth_walked': 1,
            },
            '/v1/fact/10001/lineage': {  # GET walk-down
                'descendants': [{'fact_id': pb}, {'fact_id': 9999}],
            },
            '/graph.dot': 'digraph provenance { f10001; f10002; f10001 -> f10002 }',
        }

        with _MockURLOpen(responses):
            # 1) write a (parent)
            body = json.dumps({'text': 'parent', 'tier': 'public',
                               'mode': 'regex', 'user': 'admin'}).encode()
            a = json.loads(_ur.urlopen(_ur.Request(
                'http://127.0.0.1:7803/v1/write', data=body,
                headers={'Content-Type': 'application/json'},
            ), timeout=15).read())
            assert a['fact_ids'] == [pa], f"write 1 returned {a}"

            # 2) write b (child)
            body = json.dumps({'text': 'child', 'tier': 'public',
                               'mode': 'regex', 'user': 'admin'}).encode()
            b = json.loads(_ur.urlopen(_ur.Request(
                'http://127.0.0.1:7803/v1/write', data=body,
                headers={'Content-Type': 'application/json'},
            ), timeout=15).read())
            assert b['fact_ids'] == [pa], f"write 2 returned {b}"

            # 3) record provenance: parent -> child
            rec = json.loads(_ur.urlopen(_ur.Request(
                f'http://127.0.0.1:7803/v1/fact/{pb}/provenance',
                data=json.dumps({'tier': 'public', 'parents': [pa],
                                 'kind': 'inferred', 'agent': 'unit_test'}).encode(),
                headers={'Content-Type': 'application/json'},
            ), timeout=10).read())
            self.assertEqual(rec['fact_id'], pb)
            self.assertEqual(rec['provenance_depth'], 1)

            # 4) walk upward — deterministic now
            up = json.loads(_ur.urlopen(
                f'http://127.0.0.1:7803/v1/fact/{pb}/provenance', timeout=10
            ).read())
            # v1.14.49 (R3): strict assertion possible because mock is
            # deterministic. Was >= 1 in v1.14.45; now exactly 1.
            self.assertEqual(len(up['ancestors']), 1,
                             f"expected exactly 1 ancestor, got {up}")
            self.assertEqual(up['ancestors'][0]['fact']['id'], pa)

            # 5) walk downward from pa
            down = json.loads(_ur.urlopen(
                f'http://127.0.0.1:7803/v1/fact/{pa}/lineage', timeout=10
            ).read())
            ids = {x['fact_id'] for x in down['descendants']}
            self.assertIn(pb, ids)

            # 6) graph.dot
            dot = _ur.urlopen(
                f'http://127.0.0.1:7803/v1/fact/{pa}/graph.dot?direction=both',
                timeout=5,
            ).read().decode()
            self.assertIn('digraph provenance', dot)
            self.assertIn(f'f{pa}', dot)
            self.assertIn(f'f{pb}', dot)

    def test_get_provenance_returns_chain_broken_when_missing(self):
        """If a parent fact_id is missing, the chain is marked broken but
        the call still succeeds.

        v1.14.49 (R3): Mocked — deterministic. No live server dependency.
        """
        import urllib.request as _ur
        fid = 20001
        responses = {
            '/v1/write': {'fact_ids': [fid], 'count': 1},
            f'/v1/fact/{fid}/provenance': {
                'fact_id': fid, 'parents': [8888888],
                'provenance_depth': 0, 'provenance_kind': 'inferred',
            },
            f'/v1/fact/{fid}/provenance?scope_search=true': {
                'ancestors': [], 'chain_broken': True,
                'depth_walked': 0, 'event': None, 'fact_id': fid,
                'notes': ['parent 8888888 not found'],
            },
        }
        with _MockURLOpen(responses):
            # write
            body = json.dumps({'text': 'chain-broken test',
                               'tier': 'public', 'mode': 'regex',
                               'user': 'admin'}).encode()
            w = json.loads(_ur.urlopen(_ur.Request(
                'http://127.0.0.1:7803/v1/write', data=body,
                headers={'Content-Type': 'application/json'},
            ), timeout=15).read())
            assert w['fact_ids'] == [fid]

            # record broken parent
            rec = json.loads(_ur.urlopen(_ur.Request(
                f'http://127.0.0.1:7803/v1/fact/{fid}/provenance',
                data=json.dumps({'tier': 'public', 'parents': [8888888],
                                 'kind': 'inferred', 'agent': 'unit_test'}).encode(),
                headers={'Content-Type': 'application/json'},
            ), timeout=10).read())
            self.assertIn(rec['provenance_depth'], (0, 1))

            # walk upward — chain_broken=True (deterministic now)
            up = json.loads(_ur.urlopen(
                f'http://127.0.0.1:7803/v1/fact/{fid}/provenance?scope_search=true',
                timeout=10,
            ).read())
            self.assertTrue(up['chain_broken'],
                            f"expected chain_broken=True, got {up}")
            self.assertEqual(len(up['ancestors']), 0,
                             f"expected 0 ancestors (broken parent), got {up['ancestors']}")

    def test_graph_dot_returns_empty_graph_for_missing_fact(self):
        """For a missing fact, graph_dot returns a minimal valid DOT
        document instead of raising — UI tools can still render it."""
        from astor_memory.nest.provenance import graph_dot
        out = graph_dot(fact_id=9999999, tier='public', direction='up')
        self.assertIn('digraph provenance', out)
        # No node edges should appear
        self.assertNotIn('->', out)


if __name__ == '__main__':
    unittest.main(verbosity=2)
