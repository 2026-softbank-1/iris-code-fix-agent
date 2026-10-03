import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit


def add(a, b):
    return a + b


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        args = parse_qs(url.query)
        body = {"status": "ok"} if url.path == "/health" else {"result": add(int(args["a"][0]), int(args["b"][0]))}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
