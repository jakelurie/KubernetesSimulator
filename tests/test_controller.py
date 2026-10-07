import json, tempfile, unittest
from pathlib import Path
from server import Controller, Demo, FAULT, Kubectl

class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.demo = Demo(); self.c = Controller(self.tmp.name,self.demo,'demo')
    def apply(self, **kw):
        self.c.capture(); self.c.apply({'scenario':'disaster',**kw},'test-run'); self.c.tick()
    def test_disaster_restore_and_trace(self):
        self.apply()
        self.assertEqual(self.c.state['phase'],'faults-observed')
        self.assertEqual(len(self.c.state['journal']),17)
        self.c.restore()
        self.assertEqual(self.c.state['phase'],'normal')
        self.assertFalse(self.c.state['journal'])
        self.assertTrue(all(p['ready'] for a in self.demo.apps for p in a['pods']))
        events = [json.loads(l)['event'] for l in (Path(self.tmp.name)/'trace.jsonl').read_text().splitlines()]
        self.assertIn('fault_injected',events); self.assertIn('recovery_observed',events)
    def test_specific_pod(self):
        a=self.demo.apps[0]; pod=a['pods'][2]
        self.apply(scenario='single',app=a['id'],pod=pod['name'])
        self.assertFalse(pod['ready']); self.assertEqual(len(self.c.state['journal']),1)
    def test_partial(self):
        self.apply(scenario='partial',app='demo/storefront',count=2)
        self.assertEqual(len(self.c.state['journal']),2)
    def test_requires_baseline_and_valid_count(self):
        with self.assertRaises(ValueError): self.c.apply({'scenario':'disaster'},'no-baseline')
        self.c.capture()
        with self.assertRaises(ValueError): self.c.apply({'scenario':'partial','app':'demo/storefront','count':99},'too-many')
        self.assertIsNone(self.c.state['active'])
    def test_does_not_recapture_active(self):
        self.apply()
        with self.assertRaises(ValueError): self.c.capture()
    def test_replacement_reinjected(self):
        self.apply(scenario='single',app='demo/storefront')
        p=self.demo.apps[0]['pods'][0]; p.update(uid='replacement',images={'app':'demo/storefront:stable'},ready=True,reason='Running')
        self.c.tick(); self.assertEqual(p['images']['app'],FAULT)
        self.c.restore(); self.assertTrue(p['ready'])
    def test_restart_preserves_restore(self):
        self.apply()
        c=Controller(self.tmp.name,Demo(),'demo'); c.restore()
        self.assertEqual(c.state['phase'],'normal')
        self.assertTrue(all(p['ready'] for a in c.adapter.apps for p in a['pods']))
    def test_conflict_never_overwritten(self):
        self.apply(scenario='single',app='demo/storefront')
        p=self.demo.apps[0]['pods'][0];p['images']={'app':'new-owner-image'}
        with self.assertRaises(RuntimeError): self.c.restore()
        self.c.tick()
        self.assertEqual(p['images']['app'],'new-owner-image')
        self.assertEqual(self.c.state['phase'],'restoring')
        self.assertTrue(self.c.state['journal'])
    def test_partial_apply_failure_is_recoverable(self):
        original=self.demo.patch
        def fail(ns,pod,uid,before,after):
            if len(self.c.state['journal'])==2: raise RuntimeError('test failure')
            original(ns,pod,uid,before,after)
        self.demo.patch=fail;self.c.capture()
        with self.assertRaises(RuntimeError):self.c.apply({'scenario':'disaster'},'partial-error')
        self.demo.patch=original;self.c.restore()
        self.assertEqual(self.c.state['phase'],'normal')
    def test_scope_required(self):
        for context,nss in [(None,['app']),('test',[]),('test',['kube-system'])]:
            with self.assertRaises(ValueError): Kubectl(context,nss)
    def test_unhealthy_baseline_refused(self):
        self.demo.apps[0]['pods'][0]['ready']=False
        with self.assertRaises(ValueError):self.c.capture()

if __name__=='__main__':unittest.main()
