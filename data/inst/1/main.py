from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("PPanel demo instance is alive! python 3.11 in WSL docker".encode())

HTTPServer(("0.0.0.0", 8000), H).serve_forever()