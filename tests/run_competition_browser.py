"""Temporary engine fixture for the competitive operator/spectator browser."""
import os
import subprocess
import sys
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app import Handler
from test_competitive import CompetitiveTests

def main():
    fixture=CompetitiveTests();fixture.setUp()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.external_engine=fixture.e
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        env=os.environ|{'TEST_BASE_URL':f'http://127.0.0.1:{server.server_port}',
            'TEST_OPERATOR_TOKEN':fixture.e.operator_token,'TEST_OBSERVER_TOKEN':fixture.new['observer_token']}
        node=ROOT/'.playwright/node_modules/node/bin/node.exe'
        return subprocess.run([str(node),str(ROOT/'tests/competition_browser.cjs')],cwd=ROOT,env=env,timeout=60).returncode
    finally:server.shutdown();thread.join();server.server_close();fixture.doCleanups()

if __name__=='__main__':raise SystemExit(main())
