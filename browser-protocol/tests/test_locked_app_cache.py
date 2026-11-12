"""A cached archive still has to prove the exact pinned bytes; failures never retry."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock

from locked_app_cache import archives, cache_key, populate


class LockedAppCacheTests(unittest.TestCase):
    payload = b'generated locked app archive fixture'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.stack = {'apps': {
            'spreed': {'source': 'archive', 'url': 'https://releases.invalid/generated.tar.gz',
                       'version': '24.0.5', 'sha256': self.digest},
            'app_api': {'source': 'bundled', 'version': '34.0.0', 'sha256': 'b' * 64}}}

    def test_cold_download_is_pinned_and_warm_read_never_uses_the_network(self):
        fetch = Mock(side_effect=lambda *_args, **_kwargs: io.BytesIO(self.payload))
        cold = populate(self.stack, self.cache, fetch)
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(cold[0]['cacheHit'])
        self.assertEqual(cold[0]['bytes'], len(self.payload))
        self.assertEqual((self.cache / (self.digest + '.tar.gz')).read_bytes(), self.payload)
        offline = Mock(side_effect=AssertionError('warm cache must work offline'))
        warm = populate(self.stack, self.cache, offline, offline=True)
        offline.assert_not_called()
        self.assertTrue(warm[0]['cacheHit'])
        self.assertEqual(warm[0]['sha256'], self.digest)
        self.assertEqual(len(warm), 1, 'bundled app verification stays in the original harness')
        json.dumps(warm)

    def test_offline_miss_refuses_without_a_request(self):
        fetch = Mock()
        with self.assertRaisesRegex(ValueError, 'offline cache entry missing'):
            populate(self.stack, self.cache, fetch, offline=True)
        fetch.assert_not_called()
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_a_corrupt_cache_fails_instead_of_redownloading_or_repairing_it(self):
        target = self.cache / (self.digest + '.tar.gz')
        target.write_bytes(b'corrupt')
        fetch = Mock()
        with self.assertRaisesRegex(ValueError, 'cached archive checksum mismatch'):
            populate(self.stack, self.cache, fetch)
        fetch.assert_not_called()
        self.assertEqual(target.read_bytes(), b'corrupt')

    def test_bad_download_never_becomes_a_cache_entry(self):
        fetch = Mock(return_value=io.BytesIO(b'wrong bytes'))
        with self.assertRaisesRegex(ValueError, 'download checksum mismatch'):
            populate(self.stack, self.cache, fetch)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_http_504_is_visible_after_one_request_and_leaves_no_partial_file(self):
        fetch = Mock(side_effect=urllib.error.HTTPError('https://releases.invalid/generated.tar.gz', 504, 'Gateway Time-out', {}, None))
        with self.assertRaises(urllib.error.HTTPError) as error:
            populate(self.stack, self.cache, fetch)
        self.assertEqual(error.exception.code, 504)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_a_stream_read_failure_is_not_retried_or_cached(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = [b'partial', OSError('stream disconnected')]
        fetch = Mock(return_value=response)
        with self.assertRaisesRegex(OSError, 'stream disconnected'):
            populate(self.stack, self.cache, fetch)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_exact_cache_key_changes_with_locked_bytes_or_url(self):
        for field, value in [('sha256', 'a' * 64), ('url', 'https://other.invalid/generated.tar.gz')]:
            changed = copy.deepcopy(self.stack)
            changed['apps']['spreed'][field] = value
            self.assertNotEqual(cache_key(changed), cache_key(self.stack))
        reordered = {'apps': dict(reversed(list(self.stack['apps'].items())))}
        self.assertEqual(cache_key(reordered), cache_key(self.stack))

    def test_invalid_lock_or_non_https_source_fails_before_network_io(self):
        for field, value in [('sha256', '../outside'), ('source', 'unlocked'),
                             ('url', 'http://releases.invalid/generated.tar.gz'),
                             ('url', 'https://user:password@releases.invalid/generated.tar.gz'),
                             ('url', 'https://:@releases.invalid/generated.tar.gz')]:
            changed = copy.deepcopy(self.stack)
            changed['apps']['spreed'][field] = value
            with self.assertRaises(ValueError):
                archives(changed)
