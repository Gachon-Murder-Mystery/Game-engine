import copy
from contextlib import contextmanager
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app import Store
import coordination as c
import multi


class LeadershipTests(unittest.TestCase):
    def request(self,g,**extra):
        return {'command_id':'cmd-1','assignments':[],**c.credentials(g),**extra}

    def test_atomic_retirement_and_stale_commands_including_replay(self):
        g=multi.create()
        g['shared']=copy.deepcopy([clue for clue in g['all_clues'] if clue['source_room']==0])
        multi.refresh(g);req=self.request(g)
        result=c.command(g,req)
        self.assertEqual(c.command(g,req),result)
        before_shared=copy.deepcopy(g['shared']);before_tasks=copy.deepcopy(g['tasks'])
        self.assertTrue(c.failover(g,1))
        self.assertFalse(c.failover(g,1))
        self.assertEqual(g['controller']['term'],2)
        self.assertEqual(sum(a['status']=='active' for a in g['gm_agents']),1)
        self.assertEqual(g['gm_agents'][0]['status'],'npc')
        self.assertEqual(g['gm_agents'][0]['token'],'')
        with self.assertRaises(c.StaleController):c.command(g,req)
        self.assertEqual(g['shared'],before_shared);self.assertEqual(g['tasks'],before_tasks)
        self.assertEqual(c.command(g,self.request(g,command_id='new'))['term'],2)

    def test_lease_expiry_and_wrong_credentials(self):
        g=multi.create();a=c.current(g)
        req=self.request(g)
        with self.assertRaises(c.StaleController):c.command(g,req,now=a['lease_until'])
        with self.assertRaises(c.StaleController):c.command(g,dict(req,controller_token=g['gm_token']))
        with self.assertRaises(c.StaleController):c.command(g,dict(req,term=True))

    def test_only_successor_can_end_game(self):
        g=multi.create();old=self.request(g,operation='END')
        c.failover(g,1)
        with self.assertRaises(c.StaleController):c.command(g,old)
        self.assertFalse(g['ended'])
        result=c.command(g,self.request(g,operation='END',command_id='end-new'))
        self.assertEqual(result['status'],'stopped');self.assertTrue(g['ended'])
        self.assertFalse(g['won'])

    def test_two_stores_concurrent_election_selects_one_successor(self):
        with tempfile.TemporaryDirectory() as d:
            one=Store(d+'/state.sqlite');two=Store(d+'/state.sqlite')
            g=multi.create();one.save(g);barrier=threading.Barrier(2);results=[]
            def elect(store):
                barrier.wait()
                with store.transaction(g['id']) as state:results.append(c.failover(state,1))
            threads=[threading.Thread(target=elect,args=(s,)) for s in (one,two)]
            for t in threads:t.start()
            for t in threads:t.join(5);self.assertFalse(t.is_alive())
            final=one.get(g['id'])
            self.assertEqual(sorted(results),[False,True])
            self.assertEqual(final['controller']['term'],2)
            self.assertEqual(len(final['gm_agents']),2)
            self.assertEqual(sum(a['status']=='active' for a in final['gm_agents']),1)

    def test_old_gm_cannot_commit_after_new_gm_election_in_other_store(self):
        with tempfile.TemporaryDirectory() as d:
            one=Store(d+'/state.sqlite');two=Store(d+'/state.sqlite');g=multi.create();one.save(g)
            req=self.request(g)
            with two.transaction(g['id']) as state:c.failover(state,1)
            before=one.get(g['id'])
            with self.assertRaises(c.StaleController):
                with one.transaction(g['id']) as state:c.command(state,req)
            self.assertEqual(one.get(g['id']),before)

    def test_gm_supervision_stays_separate_from_shared_and_private_content(self):
        g=multi.create();g['agents']['agent-1']['private']=[dict(g['all_clues'][0],text='SECRET_CONTENT')]
        g['agents']['agent-1']['goal']='SECRET_GOAL'
        obs=c.observation(g)
        self.assertEqual(obs['supervision'][0]['evidence_ids'],[g['all_clues'][0]['id']])
        self.assertNotIn('SECRET_CONTENT',json.dumps(obs));self.assertNotIn('SECRET_GOAL',json.dumps(obs))
        for payload in (multi.view(g),multi.view(g,'agent-2')):
            self.assertNotIn('supervision',payload)
            self.assertNotIn(c.current(g)['token'],json.dumps(payload))
        c.failover(g,1)
        self.assertEqual(c.observation(g)['supervision'],obs['supervision'])

    def test_gm_assignments_are_validated_atomically(self):
        g=multi.create();task=next(t for t in g['tasks'] if t['status']=='pending')
        before=copy.deepcopy(g)
        with self.assertRaises(ValueError):
            c.command(g,self.request(g,assignments=[{'task_id':task['id'],'agent_id':'agent-1',
                                                     'generation':task['generation']}]))
        self.assertEqual(g,before)

    def test_one_engine_runner_and_recovery_fence_old_term(self):
        g=multi.create()
        self.assertTrue(c.claim_runner(g,'runtime-1',now=100))
        old=self.request(g)
        self.assertFalse(c.claim_runner(g,'runtime-2',now=102))
        self.assertTrue(c.claim_runner(g,'runtime-2',now=106))
        self.assertEqual(g['controller']['term'],2)
        with self.assertRaises(c.StaleController):c.command(g,old,now=106)

    def test_two_runtimes_do_not_duplicate_controller_or_dispatch(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(d+'/state.sqlite');g=multi.create(3,42);store.save(g)
            first=multi.Runtime(store);second=multi.Runtime(Store(d+'/state.sqlite'))
            try:
                first.start(g['id']);second.start(g['id'])
                first.running[g['id']].result(timeout=10)
                second.running[g['id']].result(timeout=10)
            finally:first.close();second.close()
            final=store.get(g['id'])
            self.assertTrue(final['won'])
            self.assertEqual(final['runner_sessions'],1)
            self.assertEqual(len(final['gm_agents']),1)
            self.assertEqual(final['metrics']['gm_failovers'],0)

    def test_gm_exception_is_replaced_and_game_finishes(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(d+'/state.sqlite');g=multi.create(3,42);store.save(g)
            runtime=multi.Runtime(store);original=c.plan;seen=[];preserved=set()
            def policy(obs,gateway=None):
                seen.append(obs['controller']['term'])
                if obs['controller']['term']==1 and len(obs['shared'])>=3:
                    preserved.update(clue['id'] for clue in obs['shared'])
                    raise RuntimeError('injected GM crash')
                return original(obs,gateway)
            try:
                with patch('coordination.plan',side_effect=policy):runtime.run(g['id'])
            finally:runtime.close()
            final=store.get(g['id'])
            self.assertTrue(final['won']);self.assertIn(2,seen)
            self.assertEqual(final['gm_agents'][0]['status'],'npc')
            self.assertEqual(final['metrics']['gm_failovers'],1)
            self.assertGreaterEqual(len(preserved),3)
            self.assertTrue(preserved<={clue['id'] for clue in final['shared']})

    def test_unresponsive_gm_does_not_block_players_or_successor(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(d+'/state.sqlite');g=multi.create(3,42);store.save(g)
            runtime=multi.Runtime(store,gm_timeout=.2)
            blocked=threading.Event();release=threading.Event();original=c.plan
            def policy(obs,gateway=None):
                if obs['controller']['term']==1:
                    blocked.set();release.wait(5)
                return original(obs,gateway)
            try:
                with patch('coordination.plan',side_effect=policy):
                    runtime.start(g['id']);self.assertTrue(blocked.wait(2))
                    runtime.running[g['id']].result(timeout=10)
                    final=store.get(g['id'])
                    self.assertTrue(final['won'])
                    self.assertEqual(final['gm_agents'][0]['status'],'npc')
                    self.assertTrue(any(e['status']=='gm_retired' and e['reason']=='gm_timeout'
                                        for e in final['events']))
                    before=copy.deepcopy(final['tasks'])
                    release.set()
                self.assertEqual(store.get(g['id'])['tasks'],before)
            finally:release.set();runtime.close()

    def test_restart_resumes_running_game_from_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(d+'/state.sqlite');g=multi.create(2,42)
            g['status']='running';g['runner_sessions']=1
            g['engine_runner']={'id':'dead-process','lease_until':0}
            # Preserve a completed task and shared results before simulated process loss.
            g['shared']=copy.deepcopy([clue for clue in g['all_clues'] if clue['source_room']==0])
            multi.refresh(g);store.save(g)
            runtime=multi.Runtime(Store(d+'/state.sqlite'))
            try:
                runtime.recover();runtime.running[g['id']].result(timeout=10)
            finally:runtime.close()
            final=store.get(g['id'])
            self.assertTrue(final['won']);self.assertEqual(final['controller']['term'],2)
            self.assertEqual(len(final['shared']),25)
            self.assertEqual(final['gm_agents'][0]['status'],'npc')


if __name__=='__main__':unittest.main()
