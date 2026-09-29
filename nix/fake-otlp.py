"""Stands in for the server's Alloy in the VM test: records each OTLP POST's path."""
import http.server


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with open("/run/otlp-paths", "a") as log:
            log.write(self.path + "\n")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, *args):
        pass


http.server.HTTPServer(("127.0.0.1", 4318), Handler).serve_forever()
