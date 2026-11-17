#!/usr/bin/env bash
set -e

python -c "
import os, http.server, socketserver
port = int(os.environ.get('PORT', 8080))
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'ok')
    def log_message(self, *args): pass
socketserver.TCPServer(('0.0.0.0', port), Handler).serve_forever()
" &

exec celery -A contentgraph_backend.celery_app worker \
  --loglevel=info \
  --concurrency=1 \
  --pool=threads \
  --without-gossip \
  --without-mingle \
  --without-heartbeat