#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent D33 vector oracle; neither desktop imports this as production policy."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Policy:
    def __init__(self, others=True):
        self.others = others
        self.source = 'browser'
        self.missing = self.ended = self.closed = None
        self.stopped = False

    def step(self, value):
        now, page, count = value['atMs'], value['pageState'], value['sourceCount']
        if self.stopped:
            return []
        if not value.get('sameTab', True):
            page, count = None, 0
        if page == 'closed' and self.closed is None:
            self.closed = now
        if self.closed is not None:
            reason = 'tabClosed' if now - self.closed >= 2000 else None
        elif self.missing is not None and now - self.missing >= 120000:
            reason = 'pageGone'  # Enforce deadline before accepting a delayed return.
        else:
            reason = None
            if page == 'present' and count > 0:
                self.missing = self.ended = None
                if self.source == 'system' and self.others and value.get('restoreReady', True):
                    self.source = 'browser'
                    return ['restore']
                return []
            if page != 'present' or self.missing is not None:
                self.ended = None
                if self.missing is None:
                    self.missing = now
                if now - self.missing >= 2000 and self.source != 'system' and self.others:
                    self.source = 'system'
                    return ['fallback']
            else:
                if self.ended is None:
                    self.ended = now
                if now - self.ended >= 2000:
                    reason = 'callEnded'
        if reason:
            self.stopped = True
            return ['stop:' + reason]
        return []


class WirePolicy:
    """One tab's effective evidence before passing it to the lifecycle oracle."""
    def __init__(self, tab):
        self.tab = tab
        self.page = None
        self.count = 0
        self.heard = False
        self.policy = Policy()

    def invalidate(self):
        self.count = 0
        self.heard = False

    def step(self, value):
        packet = value['packet']
        if packet is not None:
            if packet['type'] == 'meetings':
                row = next((row for row in packet['meetings'] if row['tabId'] == self.tab), None)
                self.count = row['sourceCount'] if row else 0
                self.heard = row.get('heard') is True if row else False
                self.page = row.get('pageState') if row else None
            elif packet['type'] == 'status':
                rows = packet.get('sessions', [packet])
                row = next((row for row in rows if row['tabId'] == self.tab), None)
                if row is not None and 'pageState' in row:
                    self.page = row['pageState']
                    if self.page in ('navigating', 'closed'):
                        self.invalidate()
        return self.policy.step(dict(atMs=value['atMs'], pageState=self.page, sourceCount=self.count))


def check_wire():
    from jsonschema import Draft202012Validator
    validator = Draft202012Validator(json.loads((ROOT / 'schemas/control-extension.schema.json').read_text()))
    value = json.loads((ROOT / 'conformance/control/call-lifecycle-wire-vectors.json').read_text())
    assert value['version'] == 1
    validator.validate(value['hello'])
    names = set()
    for case in value['cases']:
        assert case['name'] not in names
        names.add(case['name'])
        policy = WirePolicy(case['tabId'])
        previous = -1
        for step in case['steps']:
            assert step['atMs'] >= previous
            previous = step['atMs']
            if step['packet'] is not None:
                validator.validate(step['packet'])
            assert policy.step(step) == step['expect'], (case['name'], step)
            assert dict(pageState=policy.page, sourceCount=policy.count, heard=policy.heard) == step['expectEvidence'], (case['name'], step)
            assert dict(missingAtMs=policy.policy.missing, endedAtMs=policy.policy.ended) == step['expectTimers'], (case['name'], step)
        print('PASS D33 lifecycle wire:', case['name'])


def check():
    value = json.loads((ROOT / 'conformance/control/call-lifecycle-vectors.json').read_text())
    assert value['constants'] == dict(CALL_END_GRACE=2, BROWSER_RETURN_GRACE=120)
    names = set()
    for case in value['cases']:
        assert case['name'] not in names
        names.add(case['name'])
        policy = Policy(case.get('others', True))
        previous = -1
        for step in case['steps']:
            assert step['atMs'] >= previous
            previous = step['atMs']
            assert policy.step(step) == step['expect'], (case['name'], step)
        print('PASS D33 lifecycle:', case['name'])
    check_wire()


if __name__ == '__main__':
    check()
