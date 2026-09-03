# docuware-client

This is a client library for the REST API of [DocuWare][1] DMS. Since
[DocuWare's documentation][2] regarding the REST API is very sparse (at the
time these lines were written), this client serves only a part of the API's
functionality.

Please keep in mind: **This software is not related to DocuWare.** It is a work
in progress, may yield unexpected results, and almost certainly contains bugs.

> ⚠️ Starting with version 0.5.0, OAuth2 authentication is the new default.
> Unless you explicitly request cookie authentication with
> `dw.login(..., cookie_auth=True)`, OAuth2 will be used. OAuth2 authentication
> has been available since DocuWare 7.10, and
> [cookie authentication will be discontinued](https://start.docuware.com/blog/product-news/docuware-sdk-discontinuation-of-cookie-authentication)
> with DocuWare 7.11.


## Usage

First you have to log in and create a persistent session:

```python
import json
import pathlib
import docuware

dw = docuware.Client("http://localhost")
session = dw.login("username", "password", "organization")
with open(".session", "w") as f:
    json.dump(session, f)
```

From then on you have to reuse the session, otherwise you will be locked out of
the DocuWare service for a period of time (10 minutes or even longer). As the
session cookie may change on subsequent logins, update the session file on
every login.

```python
session_file = pathlib.Path(".session")
if session_file.exists():
    with open(session_file) as f:
        session = json.load(f)
else:
    session = None
dw = docuware.Client("http://localhost")
session = dw.login("username", "password", "organization", saved_session=session)
with open(session_file, "w") as f:
    json.dump(session, f)
```

Iterate over the organizations and file cabinets:

```python
for org in dw.organizations:
    print(org)
    for fc in org.file_cabinets:
        print("   ", fc)
```

If you already know the ID or name of the objects, you can also access them
directly.

```python
org = dw.organization("1")
fc = org.file_cabinet("Archive")
```

Now some examples of how to search for documents. First you need a search
dialog:

```python
# Let's use the first one:
dlg = fc.search_dialog()
# Or a specific search dialog:
dlg = fc.search_dialog("Default search dialog")
```

Each search term consists of a field name and a search pattern. Each search
dialog knows its fields:

```python
for field in dlg.fields.values():
    print("Id    =", field.id)
    print("Length=", field.length)
    print("Name  =", field.name)
    print("Type  =", field.type)
    print("-------")
```

Let's search for some documents:

```python
# Search for DOCNO equal to '123456':
for result in dlg.search("DOCNO=123456"):
    print(result)
# Search for two patterns alternatively:
for result in dlg.search(["DOCNO=123456", "DOCNO=654321"], operation=docuware.OR):
    print(result)
# Search for documents in a date range (01-31 January 2023):
for result in dlg.search("DWSTOREDATETIME=2023-01-01T00:00:00,2023-02-01T00:00:00")
    print(result)
```

Please note that search terms may also contain metacharacters such as `*`, `(`,
`)`, which may need to be escaped when searching for these characters
themselves.

```python
for result in dlg.search("DOCTYPE=Invoice \\(incoming\\)"):
    print(result)
```

Search terms can be as simple as a single string, but can also be more complex.
The following two queries are equivalent:

```python
dlg.search(["FIELD1=TERM1,TERM2", "FIELD2=TERM3"])
dlg.search({"FIELD1": ["TERM1", "TERM2"], "FIELD2": ["TERM3"]})
```

The result of a search is always an iterator over the search results, even if
no result was obtained. Each individual search result holds a `document`
attribute, which gives access to the document in the archive. The document
itself can be downloaded as a whole or only individual attachments.

```python
for result in dlg.search("DOCNO=123456"):
    doc = result.document
    # Download the complete document ...
    data, content_type, filename = doc.download(keep_annotations=True)
    docuware.write_binary_file(data, filename)
    # ... or individual attachments (or sections, as DocuWare calls them)
    for att in doc.attachments:
        data, content_type, filename = att.download()
        docuware.write_binary_file(data, filename)
```

Create data entry in file cabinet:
```python
data = {
    "FIELD1": "value1",
    "FIELD2": "value2",
}
response = fc.create_data_entry(data)
```

_Subject to rewrite:_ Update data fields of document. The search parameter must
return a single document. Use a loop to execute this function on multiple
documents:

```python
fields = {
    "FIELD1": "value1",
    "FIELD2": 99999
}
response = fc.update_data_entry(["FIELD1=TERM1,TERM2", "FIELD2=TERM3"], user_fields)
```

Delete documents:

```python
dlg = fc.search_dialog()
for result in dlg.search(["FIELD1=TERM1,TERM2", "FIELD2=TERM3"]):
    document = result.document
    document.delete()
```

Users and groups of an organisation can be accessed and managed:

```python
# Iterate over the list of users and groups:
for user in org.users:
    print(user)
for group in org.groups:
    print(group)

# Find a specific user:
user = org.users["John Doe"]  # or: org.users.get("John Doe")

# Add a user to a group:
group = org.groups["Managers"]  # or: org.groups.get("Managers")
group.add_user(user)
# or
user.add_to_group(group)

# Deactivate user:
user.active = False # or True to activate user

# Create a new user:
user = docuware.User(first_name="John", last_name="Doe")
org.users.add(user, password="123456")
```


## ExpoDocs fork additions

This fork keeps the `docuware` import and the `docuware-client` distribution.
Version `0.5.2+expodocs.1` is consumed by a full Git commit SHA, not PyPI.
The matching `v0.5.2+expodocs.1` tag is reserved for the externally reviewed
commit; a candidate branch SHA is not yet a reviewed dependency pin.

### Explicit search pages

```python
page = dlg.search({"DOCNO": "123456"}, start=20, count=10)
items = list(page)
print(page.total, page.start, page.page_size)
```

`start` is a non-negative offset and `count` is a positive page size or `None`.
Explicit paging places `Start`, `Count`, and `CalculateTotalCount=true` on the
result-link GET, replacing existing defaults but preserving unrelated query
parameters. `total` exposes DocuWare's `Count.Value`; the existing `count`
attribute retains its historical meaning (total, not page size).
An explicit `count` bounds iteration to that response and never follows its
next link. Omit `count` to retain the original lazy traversal of all next links.
If a server returns fewer than requested, the page contains fewer items.

### Streamed files

```python
from pathlib import Path

dw.conn.stream_to_file(download_url, Path("download.pdf"), expected_size=12345)
```

The caller supplies a trusted URL and destination; the parent directory must
already exist. Downloads use `stream=True`, 1 MiB chunks, a 10-second connect
timeout and a 60-second read timeout. `expected_size` is an optional non-negative
byte count. When present, Content-Length is checked even if expected_size is
also supplied; missing Content-Length is allowed. Only successful, validated
downloads atomically replace the destination. Failures preserve an existing
file and remove the temporary sibling. Symlink destinations are rejected.
The server must honor `Accept-Encoding: identity`: encoded responses are
rejected to avoid comparing compressed Content-Length to decoded bytes.
Existing `get_bytes()` and document/attachment buffered downloads are unchanged.

### Token expiry and re-login

```python
if dw.is_token_expired():
    # Apply application-level login spacing and lockout policy before this call.
    saved_state = dw.relogin()
```

The same methods are exposed on `dw.conn`. OAuth saved state includes the
original Unix `acquired_at` time and numeric `expires_in` lifetime. Restoring
valid state neither resets its age nor reauthenticates. Missing, malformed,
future-dated, or expired metadata is conservatively treated as expired;
legacy saved states without lifetime metadata need one fresh login.
`relogin()` always makes exactly one authentication attempt with retained
credentials. A failed attempt clears stale bearer state. HTTP authentication
failures raise `AccountError` with the upstream `status_code`; malformed token
responses raise `AccountError` without a status code. Transport failures retain
their Requests exception class (such as `Timeout` or `ConnectionError`) so
callers can distinguish network failures from credential rejection. All these
errors use fixed safe messages without request objects or provider body details.
**Do not treat every AccountError as bad credentials**: provider 5xx and
malformed responses are not credential rejection.
There is no proactive background refresh or extra retry loop. The pre-existing
one retry after a resource returns 401/403 remains; callers still own spacing.
Cookie authentication has no access-token expiry and reports `False`.

### Fork checks and current lint limitation

```console
poetry install
poetry run pytest
poetry run pylint --errors-only docuware tests
poetry run pylint docuware tests
poetry build
```

CI tests Python 3.9 and 3.12, gates on tests and Pylint errors, and builds both
wheel and sdist. The full, unrestricted Pylint command still reports inherited
convention/refactor/warning debt; it is **not a clean lint pass**. CI publishes
that unrestricted report as an artifact using `--exit-zero`, separately from
the blocking errors-only gate. WO-1.1 acceptance criterion 1 therefore remains
partially unmet pending a reviewer decision on that pre-existing baseline;
this change does not hide it with global lint suppressions or broad cleanup.
The new checks are offline and do not claim verification against a live
DocuWare tenant.

References: [DocuWare paging limits](https://support.docuware.com/en-us/knowledgebase/article/KBA-36909),
[calculated totals](https://developer.docuware.com/dotNet_API_Reference/PlatformServerClient/DocuWare.Platform.ServerClient.ResultListQuery.html),
[OAuth discovery](https://support.docuware.com/en-us/knowledgebase/article/KBA-37505),
and [Requests streaming/cleanup](https://requests.readthedocs.io/en/stable/user/advanced/#body-content-workflow).

## CLI usage

This package also includes a simple CLI program for collecting information
about the archive and searching and downloading documents or attachments.

First you need to log in:

```console
$ dw-client login --url http://localhost/ --username "Doe, John" --password FooBar --organization "Doe Inc."
```

The credentials and the session cookie are stored in the `.credentials` and
`.session` files in the current directory.

Of course, `--help` will give you a list of all options:

```console
$ dw-client --help
```

Some search examples (Bash shell syntax):

```console
$ dw-client search --file-cabinet Archive Customer=Foo\*
$ dw-client search --file-cabinet Archive DocNo=123456 "DocType=Invoice \\(incoming\\)"
$ dw-client search --file-cabinet Archive DocDate=2022-02-14
```

Downloading documents:

```console
$ dw-client search --file-cabinet Archive Customer=Foo\* --download document --annotations
```

Downloading attachments (or sections):

```console
$ dw-client search --file-cabinet Archive DocNo=123456 --download attachments
```

Some information about your DocuWare installation:

```console
$ dw-client info
```

Listing all organizations, file cabinets and dialogs at once:

```console
$ dw-client list
```

A more specific list, only one file cabinet:

```console
$ dw-client list --file-cabinet Archive
```

You can also display a (partial) selection of the contents of individual fields:

```console
$ dw-client list --file-cabinet Archive --dialog custom --field DocNo
```


## Further reading

* Entry point to [DocuWare's official documentation][2] of the REST API.
* Notable endpoint: `/DocuWare/Platform/Content/PlatformLinkModel.pdf`


## License

This work is released under the BSD 3 license. You may use and redistribute
this software as long as the copyright notice is preserved.


[1]: https://docuware.com/
[2]: https://developer.docuware.com/rest/index.html
