#!/usr/bin/env python3
"""Separate, scoped Kubernetes fault controller; standard library only."""
import copy, fcntl, hashlib, json, logging, math, os, re, secrets, socket, subprocess, threading, time, uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent
VERSION = '1.0.0'
FAULT = 'rca-scenario.invalid/injected-failure:never'

def now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

class Kubectl:
    def __init__(self, context, namespaces):
        if not context or not namespaces or any(n.startswith('kube-') for n in namespaces):
            raise ValueError('Set an explicit context and application namespaces; kube-* namespaces are excluded.')
        self.context, self.namespaces = context, namespaces

    def run(self, ns, *args):
        if ns not in self.namespaces:
            raise ValueError('Namespace outside configured scope')
        p = subprocess.run(['kubectl', '--context', self.context, '--namespace', ns, '--request-timeout=12s', *args], capture_output=True, text=True, timeout=18)
        if p.returncode:
            # Do not persist arbitrary provider output, which may contain sensitive details.
            raise RuntimeError('Kubernetes request failed. Check context, connectivity and get/patch RBAC for deployments, replicasets and pods.')
        return json.loads(p.stdout)

    def inventory(self):
        apps = []
        for ns in self.namespaces:
            items = self.run(ns, 'get', 'deployments,replicasets,pods', '-o', 'json')['items']
            deps = {x['metadata']['uid']: x for x in items if x['kind'] == 'Deployment'}
            rs = {}
            for x in items:
                if x['kind'] == 'ReplicaSet':
                    rs[x['metadata']['uid']] = next((o['uid'] for o in x['metadata'].get('ownerReferences', []) if o.get('controller')), None)
            for uid, dep in deps.items():
                pods = []
                for p in items:
                    if p['kind'] != 'Pod': continue
                    owner = next((o['uid'] for o in p['metadata'].get('ownerReferences', []) if o.get('controller')), None)
                    if rs.get(owner) != uid: continue
                    statuses = p.get('status', {}).get('containerStatuses', [])
                    reason = next((s.get('state', {}).get('waiting', {}).get('reason') for s in statuses if s.get('state', {}).get('waiting')), None)
                    ready = any(c['type'] == 'Ready' and c['status'] == 'True' for c in p.get('status', {}).get('conditions', []))
                    pods.append({'name': p['metadata']['name'], 'uid': p['metadata']['uid'], 'images': {c['name']: c['image'] for c in p['spec']['containers']}, 'ready': ready, 'reason': reason or ('Running' if ready else p.get('status', {}).get('phase', 'Pending')), 'deleting': bool(p['metadata'].get('deletionTimestamp'))})
                apps.append({'id': ns + '/' + dep['metadata']['name'], 'namespace': ns, 'name': dep['metadata']['name'], 'uid': uid, 'desired': dep['spec'].get('replicas', 1), 'pods': sorted(pods, key=lambda p:p['name'])})
        return apps

    def patch(self, ns, pod, uid, before, after):
        # UID and original-image tests protect against replacement and concurrent edits.
        p = self.run(ns, 'get', 'pod', pod, '-o', 'json')
        ops = [{'op':'test', 'path':'/metadata/uid', 'value':uid}]
        for i, c in enumerate(p['spec']['containers']):
            if c['name'] in after:
                ops += [{'op':'test', 'path':f'/spec/containers/{i}/image', 'value':before[c['name']]}, {'op':'replace', 'path':f'/spec/containers/{i}/image', 'value':after[c['name']]}]
        self.run(ns, 'patch', 'pod', pod, '--type=json', '-p', json.dumps(ops), '-o', 'json')

class Demo:
    def __init__(self):
        self.apps = []
        for name, count in [('storefront', 4), ('checkout-api', 3), ('payments', 3), ('inventory', 4), ('notifications', 2), ('orders-worker', 3)]:
            self.apps.append({'id':'demo/'+name, 'namespace':'demo', 'name':name, 'uid':name, 'desired':count, 'pods':[{'name':f'{name}-7c8f-{i+1:03}', 'uid':f'{name}-{i}', 'images':{'app':'demo/'+name+':stable'}, 'ready':True, 'reason':'Running', 'deleting':False} for i in range(count)]})
    def inventory(self): return copy.deepcopy(self.apps)
    def patch(self, ns, pod, uid, before, after):
        p = next(p for a in self.apps for p in a['pods'] if p['uid'] == uid)
        if p['images'] != before: raise RuntimeError('Pod changed since capture')
        p['images'] = copy.deepcopy(after)
        p['ready'] = FAULT not in after.values()
        p['reason'] = 'Running' if p['ready'] else 'ImagePullBackOff'

class Controller:
    def __init__(self, directory, adapter, mode):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.adapter, self.mode = adapter, mode
        self.lock = threading.RLock()
        self.log = logging.getLogger(str(self.directory))
        self.log.setLevel(logging.INFO)
        handler = RotatingFileHandler(self.directory/'trace.jsonl', maxBytes=1_000_000, backupCount=4)
        self.log.addHandler(handler)
        self.file = self.directory/'state.json'
        self.state = {'baseline':None, 'active':None, 'journal':[], 'history':[], 'phase':'normal', 'error':None}
        if self.file.exists(): self.state = json.loads(self.file.read_text())
        if mode == 'demo' and (self.directory/'demo.json').exists(): adapter.apps = json.loads((self.directory/'demo.json').read_text())
        self.trace('startup', mode=mode, phase=self.state['phase'])
    def trace(self, event, **fields):
        try: self.log.info(json.dumps({'time':now(), 'version':VERSION, 'host':socket.gethostname(), 'event':event, **fields}))
        except Exception: pass
    def save(self):
        tmp = self.file.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state)); os.chmod(tmp, 0o600); tmp.replace(self.file)
        if self.mode == 'demo':
            tmp = self.directory/'demo.tmp'; tmp.write_text(json.dumps(self.adapter.apps)); tmp.replace(self.directory/'demo.json')
    def snapshot(self):
        apps = self.adapter.inventory()
        return {'mode':self.mode, 'scope':getattr(self.adapter, 'namespaces', ['demo']), 'context':getattr(self.adapter, 'context', 'Local simulation'), 'version':VERSION, 'apps':apps, **copy.deepcopy(self.state)}
    def capture(self):
        if self.state['active'] or self.state['journal']: raise ValueError('Restore the active scenario before capturing a baseline.')
        apps = self.adapter.inventory()
        if not apps: raise ValueError('No Deployments found in the configured scope.')
        if any(len(a['pods']) != a['desired'] or any(not p['ready'] or p['deleting'] for p in a['pods']) for a in apps):
            raise ValueError('Baseline needs stable, ready Deployments. Wait for existing issues or rollouts to finish.')
        self.state['baseline'] = {'time':now(), 'apps':apps}
        self.save()
    def apply(self, body, rid):
        if self.state['active'] or self.state['journal']: raise ValueError('Restore the current scenario first.')
        if not self.state['baseline']: raise ValueError('Capture a healthy baseline first.')
        kind = body.get('scenario')
        if kind not in ['disaster','partial','single']: raise ValueError('Unknown scenario')
        apps = self.adapter.inventory()
        baseline = {a['id']:a for a in self.state['baseline']['apps']}
        if {a['id'] for a in apps} != set(baseline): raise ValueError('Application scope changed. Capture a new baseline.')
        for a in apps:
            if a['uid'] != baseline[a['id']]['uid'] or a['desired'] != baseline[a['id']]['desired'] or any(not p['ready'] or p['deleting'] for p in a['pods']) or len(a['pods']) != a['desired']:
                raise ValueError('Cluster differs from the healthy baseline. Capture a new baseline.')
        chosen = apps if kind == 'disaster' else [a for a in apps if a['id'] == body.get('app')]
        if not chosen: raise ValueError('Select an application.')
        count = body.get('count', 1)
        if type(count) != int or not 1 <= count <= 1000: raise ValueError('Pod count must be between 1 and 1000.')
        targets = []
        for a in chosen:
            n = math.ceil(len(a['pods'])*0.75) if kind == 'disaster' else (1 if kind == 'single' else count)
            if not n or n > len(a['pods']): raise ValueError('Requested pod count exceeds available pods.')
            pod = body.get('pod') if kind == 'single' else None
            if pod and pod not in [p['name'] for p in a['pods']]: raise ValueError('Selected pod no longer exists.')
            targets.append({'app':a['id'], 'uid':a['uid'], 'count':n, 'pod':pod})
        self.state.update(active={'id':rid, 'scenario':kind, 'started':now(), 'targets':targets, 'expectedRootCause':'Invalid container image injected into selected pods; maintained until reset.'}, phase='applying', error=None)
        self.state['history'] = ([copy.deepcopy(self.state['active'])] + self.state['history'])[:50]
        self.save() # intent and rollback data are durable before each mutation
        self.reconcile()
    def reconcile(self):
        active = self.state['active']
        if not active or self.state['phase'] == 'restoring': return
        apps = {a['id']:a for a in self.adapter.inventory()}
        confirmed = True
        for target in active['targets']:
            a = apps.get(target['app'])
            if not a or a['uid'] != target['uid']: raise RuntimeError('Target Deployment was replaced or removed; restore and recapture baseline.')
            candidates = [p for p in a['pods'] if not p['deleting']]
            candidates.sort(key=lambda p:(FAULT not in p['images'].values(), p['name'] != target['pod'], p['name']))
            if len(candidates) < target['count']: confirmed = False
            for p in candidates[:target['count']]:
                if FAULT not in p['images'].values():
                    entry = {'namespace':a['namespace'], 'pod':p['name'], 'uid':p['uid'], 'images':copy.deepcopy(p['images']), 'scenarioId':active['id']}
                    if not any(e['uid'] == p['uid'] for e in self.state['journal']):
                        self.state['journal'].append(entry); self.save()
                    self.adapter.patch(a['namespace'], p['name'], p['uid'], p['images'], {k:FAULT for k in p['images']})
                    self.trace('fault_injected', correlationId=active['id'], app=a['id'], pod=p['name'])
                    confirmed = False
                if p['ready'] or p['reason'] not in ('ImagePullBackOff','ErrImagePull'): confirmed = False
        self.state['phase'] = 'faults-observed' if confirmed else 'awaiting-failure'
        self.state['error'] = None
        self.save()
    def restore(self):
        self.state['phase'] = 'restoring'; self.save() # disables reinjection even if reset fails
        apps = self.adapter.inventory()
        pods = {p['uid']:p for a in apps for p in a['pods']}
        remaining, errors = [], []
        for entry in self.state['journal']:
            p = pods.get(entry['uid'])
            if not p: continue # replacement pod was never modified by this entry
            try:
                if p['images'] == entry['images']: continue
                injected = {k:FAULT for k in entry['images']}
                if p['images'] != injected: raise RuntimeError('Concurrent image change; reset will not overwrite it.')
                self.adapter.patch(entry['namespace'], entry['pod'], entry['uid'], injected, entry['images'])
                self.trace('pod_restored', correlationId=entry['scenarioId'], pod=entry['pod'])
            except Exception as e: remaining.append(entry); errors.append(str(e))
        self.state['journal'] = remaining
        if not remaining:
            if self.state['active']:
                for h in self.state['history']:
                    if h['id'] == self.state['active']['id']: h['ended'] = now()
            self.state['active'] = None
            self.state['phase'] = 'recovering'
        self.state['error'] = '; '.join(sorted(set(errors))) or None
        self.save()
        if errors: raise RuntimeError(self.state['error'])
        self.check_recovery()
    def check_recovery(self):
        if self.state['phase'] != 'recovering': return
        apps = self.adapter.inventory()
        baseline = self.state['baseline']['apps'] if self.state['baseline'] else []
        if {a['id'] for a in apps} == {a['id'] for a in baseline} and all(len(a['pods']) == a['desired'] and all(p['ready'] and not p['deleting'] for p in a['pods']) for a in apps):
            self.state['phase'] = 'normal'; self.save()
            self.trace('recovery_observed')
    def tick(self):
        with self.lock:
            try:
                if self.state['phase'] == 'restoring': self.restore()
                else: self.reconcile(); self.check_recovery()
            except Exception as e:
                self.state['error'] = str(e); self.save()
                self.trace('reconcile_error', correlationId=(self.state['active'] or {}).get('id'), error=str(e))

def main():
    mode = os.environ.get('CONTROLLER_MODE','demo')
    if mode not in ('demo','kubernetes'): raise ValueError('CONTROLLER_MODE must be demo or kubernetes')
    adapter = Demo() if mode == 'demo' else Kubectl(os.environ.get('KUBE_CONTEXT'), [n.strip() for n in os.environ.get('KUBE_NAMESPACES','').split(',') if n.strip()])
    scope = 'demo' if mode == 'demo' else 'kube-' + hashlib.sha256(json.dumps([adapter.context, sorted(adapter.namespaces)]).encode()).hexdigest()[:16]
    directory = ROOT/'.runtime'/scope
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    process_lock = (directory/'controller.lock').open('a')
    try: fcntl.flock(process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: raise RuntimeError('A controller already owns this scope. Stop it before starting another.')
    ctl = Controller(ROOT/'.runtime'/scope, adapter, mode)
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, code, data, content='application/json'):
            raw = json.dumps(data).encode() if content == 'application/json' else data
            self.send_response(code); self.send_header('Content-Type', content); self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
            self.end_headers(); self.wfile.write(raw)
        def do_GET(self):
            try:
                if self.path == '/api/state':
                    with ctl.lock: data = ctl.snapshot()
                    data['csrf'] = token
                    return self.reply(200,data)
                paths = {'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
                if self.path not in paths: return self.reply(404, {'error':'Not found'})
                f = ROOT/'static'/paths[self.path]
                self.reply(200, f.read_bytes(), {'.html':'text/html; charset=utf-8','.js':'text/javascript','.css':'text/css'}[f.suffix])
            except Exception as e:
                ctl.trace('request_error', error=str(e)); self.reply(503, {'error':str(e)})
        def do_POST(self):
            rid = self.headers.get('X-Correlation-ID','')
            if not re.fullmatch(r'[a-zA-Z0-9-]{1,64}',rid): rid = str(uuid.uuid4())
            start = time.monotonic()
            try:
                if not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),token):
                    return self.reply(403, {'error':'Refresh this page before changing state.'})
                length = int(self.headers.get('Content-Length',0))
                if length > 8192: return self.reply(413, {'error':'Request too large'})
                body = json.loads(self.rfile.read(length) or '{}')
                if not isinstance(body,dict): raise ValueError('Expected a JSON object')
                with ctl.lock:
                    ctl.trace('action', correlationId=rid, action=self.path)
                    if self.path == '/api/capture': ctl.capture()
                    elif self.path == '/api/apply': ctl.apply(body,rid)
                    elif self.path == '/api/reset': ctl.restore()
                    elif self.path == '/api/trace':
                        ctl.trace('browser_view', correlationId=rid, state=str(body.get('state',''))[:80], outcome=str(body.get('outcome',''))[:40])
                    else: return self.reply(404, {'error':'Not found'})
                ctl.trace('action_complete', correlationId=rid, durationMs=round((time.monotonic()-start)*1000), phase=ctl.state['phase'])
                self.reply(200, {'ok':True,'correlationId':rid})
            except Exception as e:
                ctl.trace('action_error', correlationId=rid, error=str(e), durationMs=round((time.monotonic()-start)*1000))
                self.reply(400 if isinstance(e,ValueError) else 503, {'error':str(e),'correlationId':rid})
    def loop():
        while True: time.sleep(5); ctl.tick()
    threading.Thread(target=loop, daemon=True).start()
    server = ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('PORT','4340'))), Handler)
    print(f'Controller {VERSION} listening on {server.server_address}; mode={mode}', flush=True)
    server.serve_forever()

if __name__ == '__main__': main()
