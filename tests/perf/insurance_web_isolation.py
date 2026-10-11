"""Reproducible isolation benchmark: do admin PDF registrations slow Web voice/WhatsApp turns?

Starts a throttled fake S3 server, a real gunicorn (same flags as web/Procfile: 2 workers x 4
threads, timeout 60) and measures turn latency alone vs. while admin registrations run.

Usage: INSURANCE_TEST_DATABASE_URL=postgresql://... python tests/perf/insurance_web_isolation.py \
         [--bytes 20000000] [--kbps 1500] [--admin 8] [--mode slow|down|hang]
Limits: single machine, loopback network, fake S3 (not Railway's), proxy for real load.
"""
import argparse, hashlib, http.server, json, os, statistics, subprocess, sys, threading, time, uuid
import urllib.request
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'web'))

ap = argparse.ArgumentParser()
ap.add_argument('--bytes', type=int, default=20_000_000)
ap.add_argument('--kbps', type=int, default=1500)
ap.add_argument('--admin', type=int, default=8)
ap.add_argument('--mode', default='slow')
ap.add_argument('--secs', type=float, default=12)
args = ap.parse_args()

DATA = b'%PDF-1.4\n' + os.urandom(args.bytes - 9)
SHA = hashlib.sha256(DATA).hexdigest()
s3_hits = {'HEAD': 0, 'GET': 0}


class S3(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a): pass

    def _serve(self, method):
        s3_hits[method] += 1
        if args.mode == 'down':
            self.send_response(503); self.send_header('Content-Length', '0'); self.end_headers(); return
        if args.mode == 'hang':
            time.sleep(120); return
        self.send_response(200)
        self.send_header('Content-Length', str(len(DATA))); self.send_header('Content-Type', 'application/pdf')
        self.end_headers()
        if method == 'GET':
            chunk = 64 * 1024
            for i in range(0, len(DATA), chunk):
                try:
                    self.wfile.write(DATA[i:i + chunk]); time.sleep(chunk / (args.kbps * 1000))
                except OSError:
                    return

    def do_HEAD(self): self._serve('HEAD')
    def do_GET(self): self._serve('GET')


class Srv(http.server.ThreadingHTTPServer):
    daemon_threads = True


def main():
    base = os.environ['INSURANCE_TEST_DATABASE_URL']
    schema = 'bench_' + uuid.uuid4().hex[:8]
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
    sep = '&' if '?' in base else '?'
    dsn = f'{base}{sep}options=-csearch_path%3D{schema}'
    from insurance import admin
    os.environ['INSURANCE_ADMIN_TOKEN_KEY'] = 'k' * 40
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        for m in sorted((ROOT / 'web/insurance/migrations').glob('*.sql')):
            c.execute(m.read_text())
        c.execute("INSERT INTO insurance_customers VALUES('INS-BIZ-001','C1','x')")
        c.execute("INSERT INTO insurance_policies VALUES('INS-BIZ-001','POL-1','C1','hogar')")
        c.execute("INSERT INTO insurance_policy_versions(business_id,policy_id,version_id,valid_from) "
                  "VALUES('INS-BIZ-001','POL-1','VER-1','2020-01-01')")
        c.execute("INSERT INTO insurance_authorizations(business_id,customer_id,policy_id,granted_by) "
                  "VALUES('INS-BIZ-001','C1','POL-1','a')")
        c.execute("INSERT INTO insurance_admin_users VALUES('adm','INS-BIZ-001',%s,true)", (admin.token_hmac('tok'),))
    s3 = Srv(('127.0.0.1', 0), S3)
    threading.Thread(target=s3.serve_forever, daemon=True).start()
    env = dict(os.environ, INSURANCE_DATABASE_URL=dsn, INSURANCE_ENABLED='true', INSURANCE_ADMIN_ENABLED='true',
               INSURANCE_CASE_HMAC_KEY='x' * 40, INSURANCE_BUCKET_NAME='b', INSURANCE_BUCKET_REGION='r',
               INSURANCE_BUCKET_ENDPOINT=f'http://127.0.0.1:{s3.server_address[1]}',
               INSURANCE_BUCKET_ACCESS_KEY_ID='a', INSURANCE_BUCKET_SECRET_ACCESS_KEY='s',
               AIRTABLE_TOKEN='x', DATABASE_URL=dsn, PYTHONPATH=str(ROOT / 'web'))
    gun = subprocess.Popen([sys.executable, '-m', 'gunicorn', 'bench_app:app', '--bind', '127.0.0.1:18099',
                            '--workers', '2', '--threads', '4', '--timeout', '60'],
                           cwd=ROOT / 'tests/perf', env=env, stdout=subprocess.DEVNULL, stderr=open('/tmp/gunicorn_bench.log', 'w'))
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen('http://127.0.0.1:18099/health', timeout=2); break
            except Exception:
                time.sleep(0.3)
        else:
            raise SystemExit('Web did not start')
        turn_url = 'http://127.0.0.1:18099/_bench/turn'

        def turns(duration, out):
            end = time.time() + duration
            while time.time() < end:
                t = time.time()
                try:
                    urllib.request.urlopen(turn_url, timeout=70).read(); ok = True
                except Exception:
                    ok = False
                out.append((time.time() - t, ok)); time.sleep(0.05)

        def summarize(label, out):
            lat = sorted(x for x, ok in out if ok)
            fails = sum(not ok for _, ok in out)
            pct = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))] * 1000 if lat else float('nan')
            print(f'{label}: n={len(out)} fail={fails} p50={pct(.5):.0f}ms p95={pct(.95):.0f}ms max={pct(1):.0f}ms')

        base_out = []
        turns(4, base_out); summarize('turn latency, Web idle   ', base_out)
        admin_res = []

        def register(i):
            body = json.dumps({'policy_id': 'POL-1', 'version_id': 'VER-1', 'document_id': f'DOC-{i}', 'sha256': SHA}).encode()
            t = time.time()
            req = urllib.request.Request('http://127.0.0.1:18099/insurance/admin/documents/register', data=body,
                                         headers={'Authorization': 'Bearer ' + 'tok', 'Content-Type': 'application/json'})
            try:
                code = urllib.request.urlopen(req, timeout=100).status
            except urllib.error.HTTPError as e:
                code = e.code
            except Exception as e:
                code = type(e).__name__
            admin_res.append((time.time() - t, code))

        workers = [threading.Thread(target=register, args=(i,)) for i in range(args.admin)]
        load_out = []
        tt = threading.Thread(target=turns, args=(args.secs, load_out))
        for w in workers: w.start()
        time.sleep(0.3); tt.start(); tt.join()
        for w in workers: w.join(timeout=130)
        summarize(f'turn latency, {args.admin} admin registrations in flight', load_out)
        print('admin registrations:', [(round(t, 1), c) for t, c in sorted(admin_res)])
        print('fake S3 requests seen by the bucket:', s3_hits, 'object bytes', len(DATA))
    finally:
        gun.terminate()
        try:
            gun.wait(10)
        except subprocess.TimeoutExpired:
            gun.kill(); gun.wait()
        s3.shutdown()
        with psycopg.connect(base, autocommit=True) as c:
            c.execute(f'DROP SCHEMA "{schema}" CASCADE')


main()
