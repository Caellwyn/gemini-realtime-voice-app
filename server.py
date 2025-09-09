import http.server
import socketserver

PORT = 8000

class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

with socketserver.TCPServer(("", PORT), NoCacheHandler) as httpd:
    print("serving at port", PORT)
    print("Open http://localhost:8000/index.html in your browser.")
    httpd.serve_forever()
