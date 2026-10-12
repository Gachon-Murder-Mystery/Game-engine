"""Run browser E2E against an isolated in-process HTTP server."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import Handler, Store
from multi import Runtime


def main():
    local_node = ROOT / '.playwright/node_modules/node/bin/node.exe'
    node = str(local_node) if local_node.exists() else shutil.which('node')
    if not node or not (ROOT / '.playwright/node_modules/playwright').is_dir():
        raise SystemExit('Install browser test tools: npm --prefix .playwright install node@22 playwright')
    with tempfile.TemporaryDirectory() as directory:
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.store = Store(str(Path(directory) / 'test.sqlite3'))
        server.runtime = Runtime(server.store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = dict(os.environ, TEST_BASE_URL=f'http://127.0.0.1:{server.server_port}')
        try:
            result = subprocess.run([node, str(ROOT / 'tests/browser.cjs')],
                                    cwd=ROOT, env=env, timeout=90)
        finally:
            server.shutdown()
            thread.join()
            for game_id in list(server.runtime.running):
                with server.store.transaction(game_id) as state:
                    if not state['ended']:
                        state['status'] = 'paused'
            server.runtime.close()
            server.server_close()
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
