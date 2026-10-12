import copy
import json
import tempfile
import threading
import time
import urllib.request
import urllib.error
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import multi
from app import Store,Handler
from http.server import ThreadingHTTPServer


class MultiTests(unittest.TestCase):
    def request(self,g,aid='agent-1',action='INVESTIGATE',**extra):
        multi.allocate(g)
        task=next((t for t in g['tasks'] if t['owner']==aid),None)
        return dict(action_id=f'action-{len(g["actions"])}',agent_id=aid,action=action,
                    task_id=task['id'] if task else None,
                    generation=task['generation'] if task else None,**extra)

    def test_seed_and_variable_count(self):
        for n in (1,3,12):
            a,b=multi.create(n,42),multi.create(n,42)
            self.assertEqual(a['truth'],b['truth'])
            self.assertEqual(a['all_clues'],b['all_clues'])
            self.assertEqual(len(a['agents']),n)
            self.assertEqual([(x['job'],x['trait'],x['goal']) for x in a['agents'].values()],
                             [(x['job'],x['trait'],x['goal']) for x in b['agents'].values()])
        for n in (0,13,True):
            with self.assertRaises(ValueError):multi.create(n)

    def test_private_boundary_and_credentials(self):
        g=multi.create();a=g['agents']['agent-1']
        marker='PRIVATE_SENTINEL';a['goal']=marker;a['private']=[dict(g['all_clues'][0],text=marker)]
        a['decisions']=[{'reason':marker}]
        for payload in (multi.view(g),multi.view(g,'agent-2')):
            encoded=json.dumps(payload)
            self.assertNotIn(marker,encoded)
            self.assertNotIn('truth',payload)
            self.assertNotIn(a['token'],encoded)
            self.assertNotIn(g['gm_token'],encoded)
        self.assertIn(marker,json.dumps(multi.view(g,'agent-1')))
        with self.assertRaises(ValueError):multi.authorize(g,a['token'])
        multi.authorize(g,a['token'],'agent-1')

    def test_idempotence_and_stale_generation(self):
        g=multi.create();req=self.request(g)
        result=multi.execute(g,req);snapshot=copy.deepcopy(g)
        self.assertEqual(multi.execute(g,req),result);self.assertEqual(g,snapshot)
        with self.assertRaises(ValueError):multi.execute(g,dict(req,action='REQUEST'))
        g=multi.create();old=self.request(g);multi.fail(g,'agent-1')
        with self.assertRaises(ValueError):multi.execute(g,old)
        self.assertEqual(len(g['actions']),0)

    def test_sharing_and_private_messages(self):
        g=multi.create();a=g['agents']['agent-1'];a['private']=copy.deepcopy(g['all_clues'][:2])
        refs=[c['id'] for c in a['private']]
        multi.execute(g,self.request(g,action='WITHHOLD',evidence_ids=refs))
        self.assertEqual(g['shared'],[])
        multi.execute(g,self.request(g,action='SHARE_PARTIAL',evidence_ids=refs))
        self.assertEqual(len(g['shared']),1)
        req=self.request(g,action='REQUEST',evidence_ids=[refs[1]],scope='private',recipient='agent-2')
        multi.execute(g,req)
        self.assertEqual(multi.view(g)['messages'],[])
        self.assertEqual(len(multi.view(g,'agent-2')['messages']),1)
        self.assertEqual(multi.view(g,'agent-3')['messages'],[])
        with self.assertRaises(ValueError):
            multi.execute(g,self.request(g,action='VERIFY',evidence_ids=[refs[1]],scope='shared'))
        multi.execute(g,self.request(g,action='SHARE_FULL',evidence_ids=refs))
        self.assertEqual(len(g['shared']),2)

    def test_failure_preserves_shared_and_completed(self):
        g=multi.create();multi.allocate(g)
        g['shared']=copy.deepcopy([c for c in g['all_clues'] if c['source_room']==0])
        multi.refresh(g);before=copy.deepcopy(g['shared'])
        multi.fail(g,'agent-1');multi.fail(g,'agent-2')
        self.assertEqual(g['shared'],before)
        self.assertEqual(g['tasks'][0]['status'],'completed')
        self.assertTrue(any(t['owner']=='agent-3' for t in g['tasks']))
        multi.fail(g,'agent-3');self.assertEqual(g['status'],'waiting_for_human')
        self.assertEqual(len(g['tasks']),8)

    def test_runtime_solution_and_single_survivor(self):
        for n,failures in ((1,[]),(3,['agent-1','agent-2'])):
            with tempfile.TemporaryDirectory() as d:
                store=Store(d+'/state.sqlite');g=multi.create(n,42);multi.allocate(g)
                for aid in failures:multi.fail(g,aid)
                store.save(g);runtime=multi.Runtime(store)
                try:runtime.run(g['id'],cycles=400)
                finally:runtime.close()
                final=store.get(g['id'])
                self.assertTrue(final['won'],final['status'])
                self.assertEqual(len(final['shared']),25)
                self.assertTrue(all(t['status']=='completed' for t in final['tasks']))
                self.assertEqual(Store(d+'/state.sqlite').get(g['id']),final)

    def test_slow_reasoning_does_not_block_other_player(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(d+'/state.sqlite');g=multi.create(2,4);store.save(g)
            runtime=multi.Runtime(store);blocked=threading.Event();release=threading.Event()
            original=multi.decide
            def policy(observation,gateway=None):
                if observation['self']['id']=='agent-1':
                    blocked.set();release.wait(5)
                return original(observation)
            try:
                with patch('multi.decide',side_effect=policy):
                    runtime.start(g['id'],cycles=20)
                    self.assertTrue(blocked.wait(2))
                    # Wait for the second player to execute two actions while the first is held.
                    advanced=threading.Event()
                    original_transaction=store.transaction
                    @contextmanager
                    def transaction(game_id):
                        with original_transaction(game_id) as state:
                            yield state
                            if len(state['agents']['agent-2']['decisions'])>=2:advanced.set()
                    with patch.object(store,'transaction',side_effect=transaction):
                        self.assertTrue(advanced.wait(3))
                    with store.for_game(g['id']):
                        state=store.get(g['id']);multi.fail(state,'agent-1');store.save(state)
                    release.set()
            finally:
                release.set();runtime.close()
            final=store.get(g['id'])
            self.assertEqual(final['agents']['agent-1']['decisions'],[])

    def test_model_gets_only_authorized_observation_and_fallback(self):
        from llm import Zen,GatewayError
        g=multi.create();g['agents']['agent-2']['goal']='PRIVATE_SENTINEL'
        z=Zen('fake')
        with patch.object(z,'request',side_effect=GatewayError('offline')) as call:
            proposed=multi.decide(multi.view(g,'agent-1'),z)
        self.assertNotIn('PRIVATE_SENTINEL',call.call_args.args[1])
        self.assertNotIn('truth',json.loads(call.call_args.args[1])['observation'])
        self.assertIn(proposed['action'],multi.CHOICES)

    def test_live_degradation_preserves_results_and_reaches_ending(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(d+'/state.sqlite');g=multi.create(3,19);store.save(g)
            runtime=multi.Runtime(store);original=store.transaction;failed=[];preserved=set()
            @contextmanager
            def transaction(game_id):
                with original(game_id) as state:
                    yield state
                    if len(state['shared'])>=3 and not failed:
                        preserved.update(c['id'] for c in state['shared'])
                        multi.fail(state,'agent-1');failed.append('agent-1')
                    if len(state['shared'])>=8 and len(failed)==1:
                        preserved.update(c['id'] for c in state['shared'])
                        multi.fail(state,'agent-2');failed.append('agent-2')
            try:
                with patch.object(store,'transaction',side_effect=transaction):runtime.run(g['id'],cycles=400)
            finally:runtime.close()
            final=store.get(g['id'])
            self.assertTrue(final['won']);self.assertEqual(len(failed),2)
            self.assertTrue(preserved<={c['id'] for c in final['shared']})
            self.assertGreater(final['metrics']['reassignments'],0)

    def test_http_contract_auth_export_and_background_run(self):
        with tempfile.TemporaryDirectory() as d:
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            server.store=Store(d+'/state.sqlite');server.runtime=multi.Runtime(server.store)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            root=f'http://127.0.0.1:{server.server_port}'
            def post(path,data):
                request=urllib.request.Request(root+path,json.dumps(data).encode(),
                    {'Content-Type':'application/json','X-GM-Client':'local'})
                with urllib.request.urlopen(request,timeout=10) as response:return json.load(response)
            try:
                with urllib.request.urlopen(root+'/multi') as response:
                    self.assertIn('멀티에이전트'.encode(),response.read())
                created=post('/multi/start',{'n':3,'seed':42})
                gm={'id':created['id'],'token':created['gm_token']}
                wrong=dict(gm,token=created['agent_credentials']['agent-1'])
                with self.assertRaises(urllib.error.HTTPError) as error:post('/multi/export',wrong)
                self.assertEqual(error.exception.code,400)
                old_controller=post('/multi/controller',gm)
                supervised=post('/multi/gm/observe',dict(id=created['id'],**old_controller))
                self.assertIn('supervision',supervised)
                replacement=post('/multi/fail-gm',dict(gm,term=old_controller['term']))
                self.assertEqual(replacement['controller']['term'],2)
                with self.assertRaises(urllib.error.HTTPError):
                    post('/multi/gm/command',{'id':created['id'],'request':dict(old_controller,
                         command_id='stale-end',operation='END')})
                self.assertFalse(post('/multi/observe',gm)['ended'])
                self.assertNotIn('supervision',post('/multi/export',gm))
                multi_state=post('/multi/run',gm)
                for _ in range(150):
                    multi_state=post('/multi/observe',gm)
                    if multi_state['ended']:break
                    time.sleep(.02)
                self.assertTrue(multi_state['won'])
                exported=post('/multi/export',gm)
                self.assertNotIn('truth',exported)
                self.assertNotIn('gm_token',json.dumps(exported))
                personal=post('/multi/observe',dict(gm,agent_id='agent-1',
                             token=created['agent_credentials']['agent-1']))
                self.assertIn('goal',personal['self'])
            finally:
                server.shutdown();thread.join();server.runtime.close();server.server_close()


if __name__=='__main__':unittest.main()
