"""Offline paging regression tests, including the upstream iterator contract."""

from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from docuware.conn import Connection
from docuware.dialogs import SearchDialog, SearchQuery


def make_query():
    """Build the real query/parser with only transport mocked."""
    connection = Connection("https://example.test")
    connection.post_text = Mock(return_value="/results?query=kept&start=0&count=100\n")
    connection.get_json = Mock(return_value={
        "Count": {"Value": 42}, "Items": [{"Title": "one"}, {"Title": "two"}],
        "Links": [{"rel": "next", "href": "/results?start=2"}],
    })
    dialog = SimpleNamespace(client=SimpleNamespace(conn=connection), fields={})
    query = SearchQuery({
        "Links": [{"rel": "dialogExpressionLink", "href": "/query?dialogId=kept"}],
    }, dialog)
    return query, connection


def test_page_sends_offset_limit_and_requests_total():
    """The result request replaces defaults and keeps the total separate."""
    query, connection = make_query()
    result = query.search({}, start=10, count=2)
    params = parse_qs(urlsplit(connection.get_json.call_args.args[0]).query)
    assert params == {"query": ["kept"], "Start": ["10"], "Count": ["2"],
                      "CalculateTotalCount": ["true"]}
    assert (result.total, result.count, result.start, result.page_size) == (42, 42, 10, 2)
    assert [item.title for item in result] == ["one", "two"]
    assert connection.get_json.call_count == 1


def test_default_search_preserves_url_and_lazy_next_links():
    """Old callers still traverse every server page without paging overrides."""
    query, connection = make_query()
    first_page = connection.get_json.return_value
    connection.get_json.side_effect = [first_page, {
        "Count": {"Value": 42}, "Items": [{"Title": "three"}],
    }]
    result = query.search({})
    assert connection.get_json.call_args.args[0] == "/results?query=kept&start=0&count=100"
    assert [item.title for item in result] == ["one", "two", "three"]
    assert connection.get_json.call_args.args[0] == "/results?start=2"
    assert (result.total, result.count, result.page_size) == (42, 42, None)


def test_page_does_not_follow_next_when_server_returns_short_page():
    """A bounded page does not unexpectedly read the remainder of a search."""
    query, connection = make_query()
    result = query.search({}, count=5)
    assert len(list(result)) == 2
    assert connection.get_json.call_count == 1


def test_page_limits_iteration_even_when_server_returns_too_many_items():
    """The requested bound holds even for an oversized server response."""
    query, connection = make_query()
    assert len(list(query.search({}, count=1))) == 1
    assert connection.get_json.call_count == 1


@pytest.mark.parametrize("options", [
    {"start": -1}, {"start": True}, {"start": 1.5}, {"start": "1"},
    {"count": 0}, {"count": -1}, {"count": False}, {"count": "2"},
])
def test_invalid_paging_is_rejected_before_network(options):
    """Invalid page arguments cannot silently turn into an unbounded query."""
    query, connection = make_query()
    with pytest.raises(ValueError):
        query.search({}, **options)
    connection.post_text.assert_not_called()


def test_search_dialog_forwards_paging():
    """The public dialog entry point exposes the same paging parameters."""
    query, connection = make_query()
    cabinet = SimpleNamespace(organization=SimpleNamespace(client=query.dialog.client))
    dialog = SearchDialog({}, cabinet)
    dialog._fields = {}  # Existing lazy-loader cache, no network configuration needed.
    dialog._query = query
    assert dialog.search({}, start=7, count=2).start == 7
    assert "Start=7" in connection.get_json.call_args.args[0]


def test_start_without_count_retains_unbounded_iteration():
    """An offset alone skips earlier results, not later server pages."""
    query, connection = make_query()
    connection.get_json.side_effect = [connection.get_json.return_value, {"Items": []}]
    result = query.search({}, start=10)
    assert result.page_size is None
    assert len(list(result)) == 2
    assert connection.get_json.call_count == 2
