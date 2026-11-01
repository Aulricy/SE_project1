"""HTTP forms and a normally restartable process for isolated system acceptance."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from html.parser import HTMLParser
from multiprocessing import get_context
from urllib.parse import urlsplit
from urllib.request import Request
from uuid import uuid4

from werkzeug.serving import make_server

from app import create_app
from tools.verify_reservation_races import Client, QuietHandler


@dataclass
class Form:
    """Only form controls; no script execution or application internals."""

    action: str
    fields: dict


class FormParser(HTMLParser):
    """Extract existing hidden controls so commands use the actual page version and key."""

    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            self.current = Form(attributes.get("action", ""), {})
            self.forms.append(self.current)
        elif tag == "input" and self.current and attributes.get("type") == "hidden":
            self.current.fields[attributes["name"]] = attributes.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


class AcceptanceClient(Client):
    """Independent browser cookies, server forms and optional real multipart uploads."""

    def fields(self, source, target):
        """Read the target form; never replace its original concurrency controls."""
        status, html, _location = self.request(source)
        assert status == 200, f"Cannot read form source: {source}: {status}"
        forms = FormParser(html).forms
        matches = [form for form in forms if (form.action or urlsplit(source).path) == target]
        assert len(matches) == 1, f"Expected one form for {target}"
        return matches[0].fields

    def submit(self, target, values=None, *, source=None, image=None):
        """Return both the response and exact accepted controls for later replay checks."""
        fields = {**self.fields(source or target, target), **(values or {})}
        return self.post_fields(target, fields, image), fields

    def post_fields(self, target, fields, image=None):
        """Retain the real response code, including controlled business conflicts."""
        if image is None:
            return self.request(target, fields)
        boundary = "acceptance-" + uuid4().hex
        chunks = []
        for name, value in fields.items():
            chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                           f'name="{name}"\r\n\r\n{value}\r\n').encode())
        chunks.extend([
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
             'filename="fixture.png"\r\nContent-Type: image/png\r\n\r\n').encode(),
            image, f"\r\n--{boundary}--\r\n".encode(),
        ])
        request = Request(self.base_url + target, data=b"".join(chunks), headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary,
        })
        return self.send_request(request)

    def image(self, path):
        """Read binary image content and MIME metadata through the authorized route."""
        with self.opener.open(self.base_url + path, timeout=10) as response:
            return response.code, response.headers.get("Content-Type"), response.read()


def _serve(config, ready, stopped):
    """Fresh child process, real application factory, threaded local HTTP server."""
    application = create_app(config)
    server = make_server("127.0.0.1", 0, application, threaded=True, request_handler=QuietHandler)
    server.timeout = 0.2
    ready.put(server.server_port)
    try:
        while not stopped.is_set():
            server.handle_request()
    finally:
        server.server_close()
        application.extensions["database_engine"].dispose()


class ProcessServer(AbstractContextManager):
    """Restart the process normally while preserving its SQLite file and signing key."""

    def __init__(self, config):
        self.config = config
        self.process = None
        self.stopped = None
        self.port = None
        self.pids = []

    @property
    def base_url(self):
        """Use the assigned loopback port; no fixed test port or external service."""
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        """Bound readiness waiting and record actual process IDs as restart evidence."""
        context = get_context("spawn")
        ready = context.Queue()
        self.stopped = context.Event()
        self.process = context.Process(target=_serve, args=(self.config, ready, self.stopped))
        self.process.start()
        try:
            self.port = ready.get(timeout=15)
            self.pids.append(self.process.pid)
        except Exception:
            self.stop()
            raise
        finally:
            ready.close()
            ready.join_thread()

    def stop(self):
        """Allow pending HTTP threads to finish before closing the generated database."""
        if self.process is None:
            return
        self.stopped.set()
        self.process.join(timeout=10)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=5)
            raise RuntimeError("Acceptance server did not stop normally")
        if self.process.exitcode != 0:
            raise RuntimeError("Acceptance server exited unsuccessfully")
        self.process.close()
        self.process = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_exception):
        self.stop()
        return False
