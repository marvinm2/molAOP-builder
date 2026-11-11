"""The Reactome Content Service answers 5xx for minutes at a time; one such
answer stopped the 2026-11-02 rebuild for a week (#297)."""
import pytest

import scripts.download_reactome_annotations as dra


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise dra.requests.exceptions.HTTPError(str(self.status_code))

    def json(self):
        return self._payload


def test_retries_a_server_error_then_succeeds(monkeypatch):
    answers = [_Resp(521), _Resp(503), _Resp(200, [{"stId": "R-HSA-1"}])]
    monkeypatch.setattr(dra, "_RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(dra.requests, "get", lambda *a, **k: answers.pop(0))
    assert dra._get_content_service("/ContentService/x") == [{"stId": "R-HSA-1"}]
    assert answers == []


def test_gives_up_after_the_last_attempt(monkeypatch):
    calls = []
    monkeypatch.setattr(dra, "_RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(dra.requests, "get", lambda *a, **k: calls.append(1) or _Resp(521))
    with pytest.raises(dra.requests.exceptions.HTTPError):
        dra._get_content_service("/ContentService/x")
    assert len(calls) == 3


def test_a_client_error_is_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(dra, "_RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(dra.requests, "get", lambda *a, **k: calls.append(1) or _Resp(404))
    with pytest.raises(dra.requests.exceptions.HTTPError):
        dra._get_content_service("/ContentService/x")
    assert len(calls) == 1
