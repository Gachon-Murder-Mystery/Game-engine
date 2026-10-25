"""Acceptance tests for the authoritative v1.3 rules; no LLM or agent policy."""
import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from integration import ExternalEngine, ContractError
from competitive import Rules, SPEC, public_clue
from competitive_rules import DATA, MBTI, MISSIONS, SHEETS, build_case, infer, tier, narrative_options, mission_complete


class CompetitiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name)/'game.db')
        self.now = 1000
        self.e = ExternalEngine(self.path, lambda: self.now)
        self.op = {'operator_token': self.e.operator_token}
        self.pool = self.call('pool/bootstrap', self.op)
        self.new = self.call('games/create', self.op, game_id='case', seed=7)
        self.gm = self.new['controller'] | {'game_id': 'case'}
        self.ids = [a['agent_id'] for a in self.pool['agents'] if a['pool_role'] == 'player'][:3]
        self.activation = [{'agent_id': a, 'character_id': i} for i, a in enumerate(self.ids)]
        self.draw = self.call('gm/mission-candidates', self.gm, activate=self.activation)['mission_candidates']
        for i, item in enumerate(self.activation):
            item['sub_missions'] = self.picks(item, self.draw[item['agent_id']])
        self.plan = dict(command_id='setup', operation='PLAN', map_version=0, activate=self.activation,
            alternatives=[{'id': 'a', 'summary': 'explore'}, {'id': 'b', 'summary': 'balance'}], selected_alternative='a', reason='test external GM')
        self.call('gm/command', self.gm, **self.plan)
        self.creds = self.call('games/credentials', self.op, game_id='case')['agent_credentials']
        self.seq = 0
    def call(self, route, creds=None, **kw): return self.e.handle(route, (creds or {}) | kw)
    def pc(self, aid): return {'game_id': 'case'} | self.creds[aid]
    def state(self):
        with self.e.transaction() as db:
            g = self.e.load(db, 'games', 'case', 'id')
            g['events'] = Rules(self.e).all_events(db,g)
            for kind, field in (('action','receipts'),('question','questions'),('command','commands')):
                for key, raw in db.execute('SELECT key,state FROM competitive_records WHERE game_id=? AND kind=?',('case',kind)):
                    g[field][key] = json.loads(raw)
            return g
    def mutate(self, fn):
        with self.e.transaction() as db:
            g = self.e.load(db, 'games', 'case', 'id'); fn(g); self.e.put(db, 'games', 'case', g, 'id')
    def picks(self, item, draw):
        result = []
        mbti = next(a['fixed_mbti'] for a in self.pool['agents'] if a['agent_id'] == item['agent_id'])
        for t, ids in draw.items():
            mid = ids[0]; m = MISSIONS[mid]; targets = {}; params = {}
            if m.get('quantity'):
                params['N'] = next(int(n) for n in m['quantity'] if tier(mid, item['character_id'], mbti, int(n)) == t)
            if m['designate'] == 'player': targets['player'] = next(a for a in self.ids if a != item['agent_id'])
            if m['designate'] in ('npc', 'character'): targets['character'] = 3
            if m['designate'] == 'place': targets['place'] = 5
            if m['designate'] == 'indirect_clues': targets['clue_ids'] = [x['id'] for x in self.state()['clues'] if x['kind'] == 'indirect'][:3]
            result.append({'mission_id': mid, 'tier': t, 'targets': targets, 'params': params})
        return result
    def error(self, code, fn):
        with self.assertRaises(ContractError) as exc: fn()
        self.assertEqual(exc.exception.code, code)
    def action(self, aid, act, payload=None):
        s = self.call('agent/observe', self.pc(aid)); self.seq += 1
        r = {k: s[k] for k in ('game_id', 'cycle_id', 'phase', 'phase_epoch')}
        r.update(agent_id=aid, action_id='a:'+str(self.seq), action=act, payload=payload or {})
        return self.call('agent/action', self.pc(aid), request=r), r
    def advance(self):
        s = self.call('gm/observe', self.gm)
        return self.call('gm/command', self.gm, command_id='advance:'+str(s['phase_epoch']), operation='ADVANCE', phase_epoch=s['phase_epoch'])
    def interaction(self):
        for a in self.ids: self.action(a, 'move', {'room': 0})
        self.advance()
    def add_clues(self, aid, kind=None, count=8):
        added = []
        def f(g):
            c = g['characters'][str(g['players'][aid]['character_id'])]
            for x in [x for x in g['clues'] if kind is None or x['kind'] == kind][:count]:
                c['information'][x['id']] = public_clue(x); added.append(public_clue(x))
        self.mutate(f); return added
    def question(self, n, npc=False, approach='question'):
        self.interaction()
        self.add_clues(self.ids[1])
        target = 3 if npc else 1
        self.mutate(lambda g: g['characters'][str(target)].update(room=0))
        def fixed(*args, **kw): return {'dice': [6]*n+[1]*(3-n), 'threshold': 4, 'successes': n, 'prepared': False}
        with patch.object(Rules, 'roll', fixed):
            result, request = self.action(self.ids[0], 'question', {'target_id': target, 'approach': approach,
                'hypothesis': {'suspect': 0, 'weapon': 0, 'place': 0}})
        return result, request
    def test_fixed_pool_unique_mbti_and_model(self):
        self.assertEqual(len(self.pool['agents']), 17)
        self.assertEqual({x['fixed_mbti'] for x in self.pool['agents'] if x['pool_role'] == 'player'}, set(MBTI))
        self.assertIsNone(next(x['fixed_mbti'] for x in self.pool['agents'] if x['pool_role'] == 'gm'))
        self.assertEqual(self.call('pool/bootstrap', self.op)['current_gm_id'], self.gm['controller_id'])
        self.error('conflict', lambda: self.call('pool/bootstrap', self.op, model_version='another'))
    def test_twelve_shared_sheets_and_zero_player_inventory(self):
        g = self.state()
        self.assertEqual(len(g['characters']), 12)
        self.assertTrue(all(sum(c['stats'].values()) == 11 for c in g['characters'].values()))
        self.assertEqual(len({c['mbti'] for c in g['characters'].values()}), 12)
        self.assertTrue(all(not self.call('agent/observe', self.pc(a))['information'] for a in self.ids))
        self.assertTrue(all(c['goal'] is None for c in g['characters'].values() if c['mode'] == 'npc'))
        self.assertTrue(all(c['information'] for c in g['characters'].values() if c['mode'] == 'npc'))
    def test_catalog_missions_and_sheet_contract(self):
        c = self.call('catalog')
        self.assertEqual(c['ruleset'], SPEC)
        self.assertEqual(len(c['characters']), 12); self.assertEqual(len(c['missions']), 58)
        self.assertEqual(c['default_rules']['deadline_cycles'], 120)
    def test_initial_only_roles_no_action_tasks(self):
        self.error('conflict', lambda: self.call('gm/command', self.gm, **(self.plan | {'command_id': 'late'})))
        r, req = self.action(self.ids[0], 'stay')
        self.error('conflict', lambda: self.call('agent/action', self.pc(self.ids[0]), request=req | {'action_id': 'dup'}))
        self.assertEqual(self.call('agent/action', self.pc(self.ids[0]), request=req), r)
        self.assertNotIn('tasks', self.call('agent/observe', self.pc(self.ids[0])))
    def test_mission_candidate_difficulty_and_counts(self):
        for item in self.activation:
            self.assertEqual(set(x['tier'] for x in item['sub_missions']), {'하','중','상'})
            self.assertTrue(all(len(x) == 3 for x in self.draw[item['agent_id']].values()))
    def test_missions_initial_invalid_plan_atomic(self):
        self.call('games/operator', self.op, game_id='case', operation='FINISH')
        new = self.call('games/create', self.op, game_id='bad', seed=8)
        gm = new['controller'] | {'game_id':'bad'}
        bad = copy.deepcopy(self.plan); bad['activate'][0]['sub_missions'][0]['mission_id'] = 'invalid'
        self.error('invalid_input', lambda: self.call('gm/command', gm, **bad))
        self.assertEqual(self.call('gm/observe', gm)['active_agents'], [])
    def test_case_solvable_and_timeline_consistent_for_many_seeds(self):
        for seed in range(100):
            truth, timeline, clues = build_case(seed)
            self.assertEqual(timeline['character_rooms'][truth['suspect']], truth['place'])
            self.assertEqual(timeline['weapon_rooms'][truth['weapon']], truth['place'])
            self.assertEqual(sum(c['kind'] == 'definite' for c in clues),25)
            self.assertNotIn(str(truth['suspect']), infer(clues)['suspect'])
    def test_indirect_clue_requires_prior_link(self):
        loc={'id':'loc','kind':'indirect','type':'character_room','character':0,'room':1}
        excluded={'id':'place','kind':'definite','category':'place','candidate':1}
        self.assertEqual(infer([loc])['suspect'], {})
        self.assertEqual(set(infer([loc,excluded])['suspect']['0']), {'loc','place'})
    def test_companion_link_three_clues(self):
        values=[{'id':'a','kind':'indirect','type':'companion','character':0,'other':1},
            {'id':'b','kind':'indirect','type':'character_room','character':1,'room':2},
            {'id':'c','kind':'definite','category':'place','candidate':2}]
        self.assertEqual(set(infer(values)['suspect']['0']), {'a','b','c'})
    def test_truthful_narration_grammar_hides_original(self):
        clue=next(x for x in self.state()['clues'] if x['kind']=='indirect')
        options=narrative_options(clue)
        self.assertGreaterEqual(len(options),2)
        self.assertNotIn('excluded_id',clue)
    def test_default_relationship_is_directed_private_and_reset(self):
        a,b=self.ids[:2]
        self.assertTrue(all(x==50 for x in self.call('agent/observe',self.pc(a))['relationships'].values()))
        with self.e.transaction() as db:
            g=self.e.load(db,'games','case','id'); Rules(self.e).relation(g,0,1,10,'fixture'); self.e.put(db,'games','case',g,'id')
        self.assertEqual(self.call('agent/observe',self.pc(a))['relationships']['1'],65)
        self.assertEqual(self.call('agent/observe',self.pc(b))['relationships']['0'],50)
        self.assertNotIn('relationships',self.call('gm/observe',self.gm))
    def test_relationship_modifies_question_not_pressure_or_mbti_dice(self):
        g=self.state(); c=g['characters']['0']; target=g['characters']['1']; rules=Rules(self.e)
        c['prepared']=False; low=rules.roll(g,c,'question',target,'r')['threshold']
        g['relationships']['0']['1']=80
        high=rules.roll(g,c,'question',target,'r')['threshold']
        self.assertEqual(low-high,1)
        pressure=rules.roll(g,c,'threaten',target,'x')['threshold']
        g['relationships']['0']['1']=0
        self.assertEqual(rules.roll(g,c,'threaten',target,'x')['threshold'],pressure)
    def test_preparation_two_plus_one_dice_and_threshold_clamp(self):
        g=self.state(); r=Rules(self.e); c=g['characters']['0']
        self.assertEqual(len(r.roll(g,c,'explore',None,'a')['dice']),2)
        c['prepared']=True
        self.assertEqual(len(r.roll(g,c,'explore',None,'b')['dice']),3)
        self.assertFalse(c['prepared'])
        self.assertEqual(r.roll(g,c,'explore',None,'c')['threshold'],2)
    def test_npc_moves_only_at_boundary_and_corpse_does_not(self):
        self.mutate(lambda g: g['characters']['3'].update(mode='corpse'))
        before=self.state()['characters']['3']['room']; self.interaction()
        self.assertEqual(self.state()['characters']['3']['room'],before)
        self.assertTrue(all(0<=x['room']<8 for x in self.state()['characters'].values()))
    def test_player_observation_hides_other_mbti_missions_and_case(self):
        view=self.call('agent/observe',self.pc(self.ids[0]))
        self.assertNotIn('truth',view); self.assertNotIn('seed',view)
        self.assertTrue(all('mbti' not in x for x in view['positions']))
        self.assertTrue(all('goal' not in x for x in view['positions']))
    def test_failed_question_is_masked_and_public_pair_only(self):
        result,req=self.question(0)
        self.assertEqual(result['outcome'],'no_information')
        events=self.call('events',self.pc(self.ids[2]))['events']
        pairs=[x for x in events if x['type']=='question_pair']
        self.assertEqual(len(pairs),1)
        self.assertEqual(set(pairs[0]['payload']),{'question_id','asker_id','target_id'})
        self.assertFalse(any(x['type']=='action_completed' for x in events if self.ids[0] in x.get('participants',[])))
    def test_player_response_required_and_no_extra_action_cost(self):
        result,req=self.question(2)
        self.assertEqual(result['execution_status'],'pending')
        q=self.call('questions/inbox',self.pc(self.ids[1]))['questions'][0]
        statements=Rules(self.e).default_response(self.state(),self.state()['questions'][q['question_id']])
        data=dict(question_id=q['question_id'],response_id='response',statements=statements)
        self.call('questions/respond',self.pc(self.ids[1]),**data)
        self.assertEqual(self.call('agent/observe',self.pc(self.ids[1]))['remaining_actions'],1)
        self.assertEqual(self.call('questions/respond',self.pc(self.ids[1]),**data)['committed'],True)
        done=self.call('agent/result',self.pc(self.ids[0]),action_id=req['action_id'])
        self.assertIn(done['outcome'],('acquired','no_information'))
    def test_false_or_under_disclosed_response_rolled_back(self):
        r,_=self.question(3)
        before=self.state()
        self.error('untruthful',lambda:self.call('questions/respond',self.pc(self.ids[1]),question_id=r['question_id'],response_id='bad',
            statements=[{'kind':'definite','clue_id':'not-owned'}]))
        self.assertEqual(before,self.state())
        self.error('disclosure_required',lambda:self.call('questions/respond',self.pc(self.ids[1]),question_id=r['question_id'],response_id='empty',statements=[]))
    def test_response_timeout_enforces_same_disclosure(self):
        r,_=self.question(3)
        self.now+=31
        self.e.tick()
        self.assertEqual(self.state()['questions'][r['question_id']]['status'],'completed')
        self.error('expired',lambda:self.call('questions/respond',self.pc(self.ids[1]),question_id=r['question_id'],response_id='late',statements=[]))
    def test_failed_interrogation_blocks_next_cycle(self):
        self.question(0,approach='interrogate')
        self.assertEqual(self.state()['characters']['1']['blocked']['0'],2)
        self.assertLess(self.state()['relationships']['1']['0'],50)
    def test_threat_failure_exposes_only_possession(self):
        self.mutate(lambda g:g['characters']['0']['information'].update({g['clues'][20]['id']:public_clue(g['clues'][20])}))
        self.question(0,approach='threaten')
        known=list(self.state()['characters']['1']['information'].values())
        self.assertTrue(any(x['kind']=='possession' and 'text' not in x for x in known))
    def test_npc_uses_same_roll_and_responds_immediately(self):
        r,_=self.question(2,npc=True)
        self.assertEqual(r['execution_status'],'completed')
        self.assertEqual(r['roll']['successes'],2)
    def test_batch_deduction_tied_winners_and_wrong_guess_private(self):
        self.interaction()
        for a in self.ids:self.action(a,'pass')
        self.advance(); truth=self.state()['truth']; wrong=truth|{'suspect':(truth['suspect']+1)%12}
        for a,guess,tokens in zip(self.ids,[truth,truth,wrong],[10,10,1]):
            r,_=self.action(a,'accuse',guess|{'output_tokens':tokens})
            self.assertEqual(r['execution_status'],'pending')
        self.assertEqual(self.state()['resolution'],None)
        self.advance(); g=self.state()
        self.assertEqual(g['resolution'],'winner')
        self.assertEqual([g['players'][x]['rank'] for x in self.ids[:2]],[1,1])
        self.assertEqual(g['players'][self.ids[2]]['mode'],'corpse')
        announcement=list(g['announcements'].values())[0]['data']
        self.assertNotIn('guess',announcement['verdicts'][2])
    def test_deadline_and_all_dead_resolutions(self):
        self.mutate(lambda g:g['rules'].update(deadline_cycles=1))
        self.interaction()
        for a in self.ids:self.action(a,'pass')
        self.advance()
        for a in self.ids:self.action(a,'pass')
        self.advance()
        self.assertEqual(self.state()['resolution'],'deadline')
        self.call('gm/command',self.gm,operation='FINISH',command_id='done')
        self.assertTrue(self.state()['ended'])
    def test_announcement_is_engine_authored_and_deduplicated(self):
        self.mutate(lambda g:g['announcements'].update({'warning':{'kind':'deadline_warning','data':{'kind':'deadline_warning','remaining_cycles':10},'published':False}}))
        data=dict(operation='ANNOUNCE',command_id='announce',kind='deadline_warning',ref='warning')
        self.error('invalid_input',lambda:self.call('gm/command',self.gm,**(data|{'text':'forged'})))
        result=self.call('gm/command',self.gm,**data)
        self.assertEqual(self.call('gm/command',self.gm,**data),result)
    def test_promotion_seals_private_memory_and_incumbent_persists(self):
        old=copy.deepcopy(self.gm)
        self.call('games/operator',self.op,game_id='case',operation='FAIL_GM',term=1)
        gm=self.call('games/credentials',self.op,game_id='case')['controller']
        self.error('stale_controller',lambda:self.call('gm/observe',old))
        p=self.state()['players'][gm['controller_id']]
        self.assertEqual(self.chmode(gm['controller_id']),'npc')
        self.assertIsNone(self.state()['characters'][str(p['character_id'])]['goal'])
        self.call('games/operator',self.op,game_id='case',operation='FINISH')
        new=self.call('games/create',self.op,game_id='next',seed=89)
        self.assertEqual(new['controller']['controller_id'],gm['controller_id'])
    def chmode(self,aid):return self.state()['characters'][str(self.state()['players'][aid]['character_id'])]['mode']
    def test_two_watchdogs_one_successor(self):
        self.now+=35
        for a in self.ids:self.call('agent/heartbeat',self.pc(a))
        self.now+=6
        other=ExternalEngine(self.path,lambda:self.now)
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda e:e.tick(),[self.e,other]))
        self.assertEqual(self.state()['term'],2)
        self.assertEqual(sum(x['role']=='gm' and x['status']=='active' for x in self.state()['assignments']),1)
    def test_history_has_monotonic_participation_and_no_reset_mbti(self):
        self.call('games/operator',self.op,game_id='case',operation='FINISH')
        pool=self.call('pool/bootstrap',self.op)['agents']
        a=next(x for x in pool if x['agent_id']==self.ids[0])
        self.assertEqual(a['history'][0]['participation_index'],1)
        self.assertEqual(a['history'][0]['character_id'],0)
    def test_all_58_mission_predicates_are_implemented_and_pure(self):
        g=self.state(); p=g['players'][self.ids[0]]
        for mid,m in MISSIONS.items():
            mission={'mission_id':mid,'tier':'중','targets':{'character':3,'player':self.ids[1],'place':5,'clue_ids':['a','b','c']},'params':{'N':5}}
            before=copy.deepcopy(g)
            self.assertIsInstance(mission_complete(g,self.ids[0],mission),bool,mid)
            self.assertFalse(mission_complete(g,self.ids[0],mission),mid)
            self.assertEqual(before,g)
    def test_relation_mission_checks_incoming_not_outgoing(self):
        g=self.state(); m={'mission_id':'O5','targets':{'character':3},'params':{}}
        g['relationships']['0']['3']=100
        self.assertFalse(mission_complete(g,self.ids[0],m))
        g['relationships']['3']['0']=90
        self.assertTrue(mission_complete(g,self.ids[0],m))
    def test_early_finish_does_not_award_duration_missions(self):
        g=self.state()
        self.assertFalse(mission_complete(g,self.ids[0],{'mission_id':'O7','targets':{},'params':{}}))
    def test_voluntary_share_removed(self):
        self.error('invalid_input',lambda:self.call('agent/share',self.pc(self.ids[0]),message_id='no',information_ids=[]))

    def start_trade(self):
        self.interaction(); g=self.state()
        a=next(x for x in g['clues'] if x['kind']=='definite' and x['category']=='suspect')
        b=next(x for x in g['clues'] if x['kind']=='definite' and x['category']=='weapon')
        self.mutate(lambda g: (g['characters']['0']['information'].update({a['id']:public_clue(a)}),
                              g['characters']['1']['information'].update({b['id']:public_clue(b)})))
        data={'trade_id':'deal','target_id':1,'offer_ids':[a['id']],'requested_category':'weapon','message_id':'proposal'}
        proposed=self.call('trades/propose',self.pc(self.ids[0]),**data)
        self.assertEqual(self.call('trades/propose',self.pc(self.ids[0]),**data),proposed)
        self.call('trades/respond',self.pc(self.ids[1]),trade_id='deal',decision='accept',offer_ids=[b['id']],message_id='accept')
        return a,b
    def test_nonbinding_trade_one_sided_delivery_and_reputation(self):
        a,b=self.start_trade()
        data={'trade_id':'deal','message_id':'give'}
        result=self.call('trades/transfer',self.pc(self.ids[0]),**data)
        self.assertEqual(self.call('trades/transfer',self.pc(self.ids[0]),**data),result)
        self.assertIn(a['id'],self.state()['characters']['1']['information'])
        self.assertNotIn(b['id'],self.state()['characters']['0']['information'])
        self.now+=31; self.e.tick()
        pool=self.call('pool/bootstrap',self.op)['agents']
        recipient=next(x for x in pool if x['agent_id']==self.ids[1])
        self.assertEqual(recipient['negotiation_reputation']['broken'],1)
        self.assertLess(self.state()['relationships']['0']['1'],50)
    def test_mutual_trade_no_extra_phase_action(self):
        self.start_trade()
        for i,a in enumerate(self.ids[:2]):self.call('trades/transfer',self.pc(a),trade_id='deal',message_id='send:'+str(i))
        self.assertTrue(all(self.state()['trades']['deal']['fulfilled'].values()))
        self.assertEqual(self.call('agent/observe',self.pc(self.ids[0]))['remaining_actions'],1)
    def test_trade_counteroffer_and_ownership_validation(self):
        self.interaction(); g=self.state()
        a=next(x for x in g['clues'] if x['kind']=='definite' and x['category']=='suspect')
        b=next(x for x in g['clues'] if x['kind']=='definite' and x['category']=='weapon')
        self.mutate(lambda g:(g['characters']['0']['information'].update({a['id']:public_clue(a)}),g['characters']['1']['information'].update({b['id']:public_clue(b)})))
        self.call('trades/propose',self.pc(self.ids[0]),trade_id='counter',target_id=1,offer_ids=[a['id']],requested_category='weapon',message_id='p')
        self.call('trades/respond',self.pc(self.ids[1]),trade_id='counter',decision='counter',offer_ids=[b['id']],message_id='c')
        self.call('trades/respond',self.pc(self.ids[0]),trade_id='counter',decision='accept',offer_ids=[a['id']],message_id='accept-counter')
        self.assertEqual(self.state()['trades']['counter']['status'],'accepted')
        self.error('unauthorized',lambda:self.call('trades/transfer',self.pc(self.ids[2]),trade_id='counter',message_id='intruder'))
    def test_report_uses_c_metrics_and_not_case_solution(self):
        self.mutate(lambda g:g.update(resolution='deadline'))
        self.call('gm/command',self.gm,operation='FINISH',command_id='legitimate-end')
        event=self.state()['events'][-1]['event_id']
        data={'operation':'REPORT','command_id':'report','metrics':{'completion':1,'balance':None,'exploration':1,'record':1},'evidence':[event]}
        report=self.call('gm/command',self.gm,**data)
        self.assertEqual(report['score'],1)
        self.assertTrue(report['success'])
        self.assertEqual(self.call('gm/command',self.gm,**data),report)
    def test_relationship_graph_scope_and_operator_export(self):
        graph=self.call('relationships/graph',self.pc(self.ids[0]))
        self.assertEqual(len(graph['edges']),11)
        self.assertTrue(all(x['source']==0 for x in graph['edges']))
        exported=self.call('games/export',self.op,game_id='case')
        self.assertEqual(len(exported['relationships']['edges']),132)
        self.assertNotIn('token',json.dumps(exported))
    def test_no_mbti_encoding_in_persistent_identifiers(self):
        self.assertTrue(all(not any(m in a for m in MBTI) for a in self.ids))
    def test_simultaneous_wrong_deductions_all_dead(self):
        self.interaction()
        for a in self.ids:self.action(a,'pass')
        self.advance(); truth=self.state()['truth']; wrong=truth|{'weapon':(truth['weapon']+1)%8}
        for a in self.ids:self.action(a,'accuse',wrong|{'output_tokens':1})
        self.advance();self.assertEqual(self.state()['resolution'],'all_dead')
        self.call('gm/command',self.gm,operation='FINISH',command_id='all-dead')
        self.assertTrue(all(x['final_score']==-20 for x in self.state()['final_feedback'].values()))
    def test_final_feedback_does_not_reveal_mbti(self):
        self.call('games/operator',self.op,game_id='case',operation='FINISH')
        feedback=self.state()['final_feedback']
        self.assertTrue(all('mbti' not in x for x in feedback.values()))
        self.assertTrue(all('target_ei' not in m['targets'] for x in feedback.values() for m in x['sub_missions']))
    def test_relation_reset_and_previous_mission_exclusion(self):
        old_missions={x['mission_id'] for x in self.activation[0]['sub_missions']}
        self.call('games/operator',self.op,game_id='case',operation='FINISH')
        new=self.call('games/create',self.op,game_id='fresh',seed=7)
        gm=new['controller']|{'game_id':'fresh'}
        draw=self.call('gm/mission-candidates',gm,activate=[{'agent_id':a,'character_id':i} for i,a in enumerate(self.ids)])['mission_candidates']
        self.assertFalse(old_missions & {x for v in draw[self.ids[0]].values() for x in v})
    def test_corpse_decay_does_not_remove_other_characters_memory(self):
        self.interaction()
        info=self.add_clues(self.ids[0],count=1)[0]
        self.mutate(lambda g:(g['characters']['3'].update(mode='corpse',information={info['id']:info}),g['characters']['0']['information'].update({info['id']:info})))
        for a in self.ids:self.action(a,'pass')
        self.advance()
        for a in self.ids:self.action(a,'pass')
        self.advance()
        self.assertEqual(self.state()['characters']['3']['information'],{})
        self.assertIn(info['id'],self.state()['characters']['0']['information'])
    def test_no_active_player_falls_back_to_reserve_gm_and_finishes(self):
        for aid in self.ids:self.call('games/operator',self.op,game_id='case',operation='FAIL_PLAYER',agent_id=aid)
        out=self.call('games/operator',self.op,game_id='case',operation='FAIL_GM',term=1)
        gm=out['controller']|{'game_id':'case'}
        self.assertNotIn(gm['controller_id'],self.ids)
        self.call('gm/command',gm,operation='FINISH',command_id='no-player-end')
        self.assertEqual(self.state()['resolution'],'no_players')

    def test_each_mission_has_a_positive_official_fact_fixture(self):
        aid=self.ids[0]
        rooms={'O1':5,'O3':7,'M02':2,'M03':3,'M04':4,'M06':1,'M09':0,'M45':5,'M50':7}
        for mid in MISSIONS:
            g=self.state();p=g['players'][aid];c=g['characters']['0'];room=rooms.get(mid,0)
            g['cycle_id']=10 if mid=='M19' else 60
            for owner in range(1,12):g['relationships'][str(owner)]['0']=90
            if mid=='M17':
                for owner in range(1,4):g['relationships'][str(owner)]['0']=20
            if mid=='M18':g['relationships']['1']['0']=10
            p.update(visited=list(range(8)),first_definite={'id':'secret','cycle':1},relationship_below_30=False,
                     acquired=[{'id':'unique','cycle':1,'room':5}])
            c['information']={f'{cat}{i}':{'id':f'{cat}{i}','kind':'definite','category':cat,'candidate':i} for cat in ('suspect','weapon','place') for i in range(2)}
            c['information'].update({f'i{i}':{'id':f'i{i}','kind':'indirect'} for i in range(4)})
            g['cycle_history']=[]
            for cycle in range(1,41):
                own=cycle%8 if mid=='M25' else room
                positions={str(i):(own+1)%8 for i in range(12)};positions['0']=own
                if mid=='M07':positions['3']=own
                if mid=='M10':
                    for i in range(1,4):positions[str(i)]=own
                g['cycle_history'].append({'cycle':cycle,'rooms':positions,'alive':['0','1','2']})
            acts=[]
            def row(kind,target=None,approach=None,location=0):
                return {'actor':0,'kind':kind,'room':location,'target':target,'target_mode':'npc',
                        'approach':approach,'successes':3,'prepared':True,'alone_players':True,'cycle':1}
            for location in range(8):acts.extend(row('explore',location=location) for _ in range(8))
            for target in range(1,12):
                for approach in ('question','interrogate','threaten'):acts.extend(row('question',target,approach) for _ in range(6))
            if mid in ('O7','M20'):acts=[x for x in acts if x['approach'] not in ('interrogate','threaten')]
            if mid=='M21':acts=[x for x in acts if x['kind']!='explore']
            if mid=='M22':acts=[x for x in acts if x['kind']!='question']
            if mid=='M48':acts.extend(row('question',0,'question')|{'actor':1} for _ in range(6))
            g['activity']=acts
            g['trades']={str(i):{'proposer':aid,'recipient':'peer:'+str(i),'accepted':True,'status':'settled',
                'fulfilled':{aid:True,'peer:'+str(i):True},'first_sender':'peer:'+str(i),'cycle':1} for i in range(3)}
            if mid=='M37':
                for x in g['trades'].values():x['fulfilled'][aid]=False
            if mid=='M27':g['trades']={}
            g['disclosures']=[{'sender':aid,'recipient':'peer:'+str(i),'kind':'response_definite','clue_id':'gift:'+str(i),'cycle':1} for i in range(3)]
            mission={'mission_id':mid,'targets':{'character':3,'player':self.ids[1],'place':5,'clue_ids':['i0','i1','i2']},'params':{'N':5}}
            if mid in ('O5','M15','M07'):mission['targets'].pop('player')
            self.assertTrue(mission_complete(g,aid,mission),mid)

    def test_legacy_mode_cannot_borrow_competitive_pool_members(self):
        self.call('pool/register',self.op,agent_id='legacy-only')
        legacy=self.call('games/create',self.op,game_id='old',mode='legacy')
        self.assertEqual(legacy['controller']['controller_id'],'legacy-only')
        gm=legacy['controller']|{'game_id':'old'}
        self.assertEqual(self.call('gm/observe',gm)['pool'][0]['agent_id'],'legacy-only')
        candidate=next(a['agent_id'] for a in self.call('pool/bootstrap',self.op)['agents'] if a['pool_status']=='available')
        self.error('unauthorized',lambda:self.call('gm/command',gm,operation='PLAN',command_id='steal',map_version=0,activate=[{'agent_id':candidate}]))

    def test_archived_receipts_survive_restart_without_inflating_hot_state(self):
        result,request=self.action(self.ids[0],'stay')
        restored=ExternalEngine(self.path,lambda:self.now)
        self.assertEqual(restored.handle('agent/action',self.pc(self.ids[0])|{'request':request}),result)
        with self.e.transaction() as db:
            raw=self.e.load(db,'games','case','id')
            self.assertEqual(raw['events'],[]);self.assertEqual(raw['receipts'],{});self.assertEqual(raw['commands'],{})
            self.assertGreater(db.execute('SELECT COUNT(*) FROM competitive_events').fetchone()[0],0)

    def test_old_game_secrets_sealed_during_new_assignment_feedback_remains(self):
        self.call('games/operator',self.op,game_id='case',operation='FINISH')
        new=self.call('games/create',self.op,game_id='fresh',seed=9)
        gm=new['controller']|{'game_id':'fresh'}
        rows=[{'agent_id':a,'character_id':i} for i,a in enumerate(self.ids)]
        preview=self.call('gm/mission-candidates',gm,activate=rows)
        draw=preview['mission_candidates']
        for row in rows:
            row['sub_missions']=self.picks(row,draw[row['agent_id']])
            for m in row['sub_missions']:
                if MISSIONS[m['mission_id']]['designate']=='indirect_clues':m['targets']['clue_ids']=preview['indirect_clue_ids'][:3]
        self.call('gm/command',gm,**(self.plan|{'activate':rows}))
        self.error('stale_assignment',lambda:self.call('agent/observe',self.pc(self.ids[0])))
        identity=self.call('pool/credentials',self.op,agent_id=self.ids[0])
        feedback=self.call('pool/feedback',identity,game_id='case')
        self.assertIn(self.ids[0],feedback['feedback'])
        self.assertNotIn('relationships',feedback)

    def test_category_only_trade_and_hypothesis_request(self):
        self.interaction();g=self.state()
        a=next(x for x in g['clues'] if x['kind']=='definite' and x['category']=='suspect')
        b=next(x for x in g['clues'] if x['kind']=='definite' and x['category']=='weapon')
        self.mutate(lambda g:(g['characters']['0']['information'].update({a['id']:public_clue(a)}),g['characters']['1']['information'].update({b['id']:public_clue(b)})))
        proposal=self.call('trades/propose',self.pc(self.ids[0]),trade_id='category',target_id=1,
            offer_category='suspect',requested_hypothesis={'suspect':0,'weapon':b['candidate'],'place':0},message_id='category-p')
        self.assertEqual(proposal['offers'][self.ids[0]],[{'kind':'possession_category','category':'suspect'}])
        self.call('trades/respond',self.pc(self.ids[1]),trade_id='category',decision='accept',offer_ids=[b['id']],message_id='category-a')
        self.call('trades/transfer',self.pc(self.ids[0]),trade_id='category',information_id=a['id'],message_id='category-t')
        self.assertIn(a['id'],self.state()['characters']['1']['information'])

    def test_exhausted_pool_requires_operator_not_an_invented_gm(self):
        for aid in self.ids:self.call('games/operator',self.op,game_id='case',operation='FAIL_PLAYER',agent_id=aid)
        for a in self.call('pool/bootstrap',self.op)['agents']:
            if a['pool_status']=='available':self.call('pool/status',self.op,agent_id=a['agent_id'],pool_status='fault')
        out=self.call('games/operator',self.op,game_id='case',operation='FAIL_GM',term=1)
        self.assertIsNone(out['controller']);self.assertEqual(out['state']['status'],'waiting_for_human')
        self.call('games/operator',self.op,game_id='case',operation='FINISH')
        self.assertTrue(self.state()['ended'])

    def test_definite_and_narration_statement_validation_with_live_inventory(self):
        self.interaction();self.add_clues(self.ids[1],kind='indirect',count=2)
        self.mutate(lambda g:g['characters']['1'].update(room=0))
        with patch.object(Rules,'roll',return_value={'dice':[6,1],'threshold':4,'successes':1,'prepared':False}):
            r,_=self.action(self.ids[0],'question',{'target_id':1,'hypothesis':{'suspect':0,'weapon':0,'place':0}})
        q=self.call('questions/inbox',self.pc(self.ids[1]))['questions'][0]
        option=q['response_options']['indirect'][0]
        self.error('untruthful',lambda:self.call('questions/respond',self.pc(self.ids[1]),question_id=r['question_id'],response_id='lie',
            statements=[{'kind':'indirect','clue_id':option['clue_id'],'text':'범인은 확실히 0이다.'}]))
        self.call('questions/respond',self.pc(self.ids[1]),question_id=r['question_id'],response_id='truth',
            statements=[{'kind':'indirect','clue_id':option['clue_id'],'text':option['narrative_options'][0]}])
        info=self.call('agent/observe',self.pc(self.ids[0]))['information'][0]
        self.assertEqual(info['kind'],'narrated');self.assertNotIn('character',info);self.assertNotIn('room',info)
        other=self.call('agent/observe',self.pc(self.ids[2]));self.assertEqual(other['information'],[])
