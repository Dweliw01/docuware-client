"""Stream downloads without buffering, leaking responses, or damaging files."""

from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from docuware.conn import Connection, OAuth2Authenticator
from docuware.errors import AccountError, ResourceError


class ChunkResponse:
    """A response that fails loudly if an implementation buffers its body."""

    def __init__(self, chunks=(), headers=None, status=200):
        self.chunks = chunks
        self.headers = headers or {}
        self.status_code = status
        self.closed = False

    @property
    def content(self):
        """Full buffering must never be used by streamed downloads."""
        raise AssertionError("Response.content was accessed")

    def iter_content(self, chunk_size):
        """Yield bounded chunks, optionally simulating a network failure."""
        assert chunk_size == 1024 * 1024
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        """Record deterministic cleanup on every outcome."""
        self.closed = True


def make_connection(response):
    """Keep the real Connection implementation, replacing only HTTP transport."""
    connection = Connection("https://example.test")
    connection.session.get = Mock(return_value=response)
    return connection


def test_stream_writes_chunks_and_replaces_existing_file(tmp_path):
    """A validated body atomically replaces the old destination."""
    response = ChunkResponse([b"abc", b"", b"def"], {"Content-Length": "6"})
    connection = make_connection(response)
    destination = tmp_path / "download.bin"
    destination.write_bytes(b"old")
    assert connection.stream_to_file("/file", destination, expected_size=6) == destination
    assert destination.read_bytes() == b"abcdef"
    assert response.closed
    assert list(tmp_path.iterdir()) == [destination]
    options = connection.session.get.call_args.kwargs
    assert options["stream"] is True
    assert options["headers"]["Accept-Encoding"] == "identity"


@pytest.mark.parametrize("headers,expected", [({}, None), ({}, 3), ({"Content-Length": "3"}, None)])
def test_stream_allows_missing_length_when_not_known(tmp_path, headers, expected):
    """Chunked responses without Content-Length can use an optional expected size."""
    response = ChunkResponse([b"abc"], headers)
    destination = tmp_path / "download.bin"
    make_connection(response).stream_to_file("/file", destination, expected_size=expected)
    assert destination.read_bytes() == b"abc"
    assert response.closed


@pytest.mark.parametrize("chunks,headers,expected", [
    ([b"abc"], {"Content-Length": "4"}, None),
    ([b"abc"], {"Content-Length": "2"}, None),
    ([b"abc"], {"Content-Length": "3"}, 4),
    ([b"abc"], {}, 4),
    ([b"abc"], {}, 2),
    ([b"abc"], {"Content-Length": "-1"}, None),
    ([b"abc"], {"Content-Length": "invalid"}, None),
    ([b"abc"], {"Content-Length": "3.0"}, None),
    ([b"abc"], {"Content-Length": "٣"}, None),
    ([b"abc"], {"Content-Encoding": "gzip", "Content-Length": "3"}, None),
    ([b"abc", requests.ConnectionError("interrupted")], {}, None),
])
def test_stream_failure_preserves_destination_and_cleans_up(tmp_path, chunks, headers, expected):
    """HTTP framing or transport errors leave no partially published file."""
    response = ChunkResponse(chunks, headers)
    destination = tmp_path / "download.bin"
    destination.write_bytes(b"original")
    with pytest.raises((ResourceError, requests.ConnectionError)):
        make_connection(response).stream_to_file("/file", destination, expected_size=expected)
    assert destination.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [destination]
    assert response.closed


@pytest.mark.parametrize("expected", [-1, True, "3", 3.5])
def test_invalid_expected_size_never_starts_download(tmp_path, expected):
    """Callers cannot supply an ambiguous byte limit."""
    connection = make_connection(ChunkResponse())
    with pytest.raises(ValueError):
        connection.stream_to_file("/file", tmp_path / "download", expected_size=expected)
    connection.session.get.assert_not_called()


def test_empty_stream_and_zero_length(tmp_path):
    """Zero is a valid expected size for an empty file."""
    response = ChunkResponse([], {"Content-Length": "0"})
    destination = tmp_path / "empty"
    make_connection(response).stream_to_file("/file", destination, expected_size=0)
    assert destination.read_bytes() == b""
    assert response.closed


def test_http_failure_closes_response_without_creating_file(tmp_path):
    """Non-success status is visible and never published as document content."""
    response = ChunkResponse(status=500)
    with pytest.raises(ResourceError):
        make_connection(response).stream_to_file("/file", tmp_path / "download")
    assert response.closed
    assert not list(tmp_path.iterdir())


def test_write_failure_cleans_temporary_file(tmp_path, monkeypatch):
    """An unsuccessful atomic publish preserves the old file and cleans staging."""
    response = ChunkResponse([b"new"])
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    monkeypatch.setattr(Path, "replace", Mock(side_effect=PermissionError("denied")))
    with pytest.raises(PermissionError):
        make_connection(response).stream_to_file("/file", destination)
    assert destination.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [destination]
    assert response.closed


def test_temporary_creation_failure_closes_response(tmp_path):
    """Missing parent directories do not leak HTTP connections."""
    response = ChunkResponse([b"abc"])
    with pytest.raises(FileNotFoundError):
        make_connection(response).stream_to_file("/file", tmp_path / "missing" / "file")
    assert response.closed


def test_symlink_destination_is_not_followed(tmp_path):
    """A destination link cannot redirect downloads into an unrelated file."""
    original = tmp_path / "original"
    original.write_bytes(b"safe")
    destination = tmp_path / "link"
    try:
        destination.symlink_to(original)
    except OSError:
        pytest.skip("Host does not permit creating symlinks")
    connection = make_connection(ChunkResponse([b"unsafe"]))
    with pytest.raises(ValueError, match="Symlink"):
        connection.stream_to_file("/file", destination)
    assert original.read_bytes() == b"safe"
    connection.session.get.assert_not_called()


def test_directory_destination_is_preserved(tmp_path):
    """Atomic publication cannot overwrite a directory or leave a staging file."""
    destination = tmp_path / "directory"
    destination.mkdir()
    response = ChunkResponse([b"abc"])
    with pytest.raises(OSError):
        make_connection(response).stream_to_file("/file", destination)
    assert destination.is_dir()
    assert list(tmp_path.iterdir()) == [destination]
    assert response.closed


def test_auth_failure_closes_first_stream(tmp_path):
    """One failed authentication attempt cannot leak the unread 401 response."""
    response = ChunkResponse(status=401)
    connection = make_connection(response)
    connection.authenticator = OAuth2Authenticator("user", "password")
    connection.authenticator.authenticate = Mock(side_effect=AccountError("rejected"))
    with pytest.raises(AccountError):
        connection.stream_to_file("/file", tmp_path / "download")
    assert response.closed
    assert connection.session.get.call_count == 1
    assert not list(tmp_path.iterdir())


def test_buffered_download_contract_unchanged():
    """Existing callers still receive bytes, content type, and filename."""
    response = Mock(status_code=200, content=b"abc", headers={
        "Content-Type": "application/pdf", "Content-Length": "3",
        "Content-Disposition": 'attachment; filename="doc.pdf"',
    })
    connection = make_connection(response)
    assert connection.get_bytes("/file") == (b"abc", "application/pdf", "doc.pdf")
    assert "stream" not in connection.session.get.call_args.kwargs
