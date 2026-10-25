"""Engine-only contract acceptance tests: external actors are explicit test doubles."""
import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from app import Handler, Store
from engine_client import EngineClient, EngineAPIError, reasoning_inputs
from integration import ExternalEngine, ExternalWatchdog, ContractError, VERSION


class Clock:
    def __init__(self):
        self.now = 1000

    def __call__(self):
        return self.now


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name)/'test.sqlite3')
        self.clock = Clock()
        self.engine = ExternalEngine(self.path, self.clock)
        self.op = {'operator_token': self.engine.operator_token}
        self.members = {}
        for i in range(5):
            a = self.call('pool/register', self.op, agent_id=f'p{i}', model_version='test-double')
            self.members[a['agent_id']] = a
        self.created = self.call('games/create', self.op, game_id='case-one', seed=42)
        self.gm = self.created['controller'] | {'game_id': 'case-one'}
        self.players = [aid for aid in self.members if aid != self.gm['controller_id']][:2]
        self.plan(activate=[{'agent_id': aid, 'job_id': i, 'personal_goal': 'protect_secret'}
                            for i, aid in enumerate(self.players)])
        self.creds = self.call('games/credentials', self.op, game_id='case-one')['agent_credentials']
        self.serial = 0

    def call(self, route, creds=None, **data):
        if route == 'games/create': data.setdefault('mode', 'legacy')
        return self.engine.handle(route, dict(creds or {}) | data)

    def pc(self, aid):
        return self.creds[aid] | {'game_id': 'case-one'}

    def state(self):
        with self.engine.transaction() as db:
            return self.engine.load(db, 'games', 'case-one', 'id')

    def plan(self, **data):
        snapshot = self.call('gm/observe', self.gm)
        return self.call('gm/command', self.gm, command_id='plan:'+str(snapshot['map_version']),
                         operation='PLAN', map_version=snapshot['map_version'], **data)

    def action(self, aid, act, payload=None, **extra):
        obs = self.call('agent/observe', self.pc(aid))
        self.serial += 1
        req = {k: obs[k] for k in ('game_id', 'cycle_id', 'phase', 'phase_epoch')}
        req.update(agent_id=aid, action_id=f'action:{self.serial}', action=act, payload=payload or {})
        req.update(extra)
        return self.call('agent/action', self.pc(aid), request=req), req

    def advance(self):
        s = self.call('gm/observe', self.gm)
        return self.call('gm/command', self.gm, operation='ADVANCE',
                         command_id=f"advance:{s['phase_epoch']}", phase_epoch=s['phase_epoch'])

    def interaction(self):
        for aid in self.players:
            self.action(aid, 'stay')
        self.advance()

    def inject_clue(self, aid):
        # Deterministic official fixture, not a client-supplied assertion.
        with self.engine.transaction() as db:
            g = self.engine.load(db, 'games', 'case-one', 'id')
            info = self.engine.information(g['clues'][0])
            g['players'][aid]['information'][info['id']] = info
            self.engine.put(db, 'games', g['id'], g, 'id')
            return info

    def question(self):
        self.interaction()
        clue = self.inject_clue(self.players[1])
        result, req = self.action(self.players[0], 'question', {'target_id': self.players[1], 'topic': 'alibi'})
        return result, req, clue

    def assert_code(self, code, callback):
        with self.assertRaises(ContractError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_registry_unique_and_private_credentials(self):
        self.assert_code('conflict', lambda: self.call('pool/register', self.op, agent_id='p0'))
        cards = self.call('pool/list', self.op)
        self.assertNotIn('pool_token', json.dumps(cards))
        self.assertNotIn('token', json.dumps(self.call('gm/observe', self.gm)))

    def test_external_mode_does_not_plan_or_call_llm(self):
        created = self.call('games/create', self.op, game_id='untouched', seed=3)
        self.assertEqual(created['state']['status'], 'awaiting_setup')
        self.assertEqual(created['state']['tasks'], [])
        self.engine.tick()
        obs = self.call('gm/observe', created['controller'] | {'game_id': 'untouched'})
        self.assertEqual(obs['tasks'], [])
        self.assertEqual(obs['active_agents'], [])

    def test_agent_cannot_be_reserved_by_two_games(self):
        self.assert_code('invalid_input', lambda: self.call('games/create', self.op,
            game_id='another', candidate_ids=[self.players[0]]))

    def test_cold_start_seed_selection_and_no_secret_observation(self):
        self.assertEqual(self.created['selection']['mode'], 'exploration')
        a = self.call('agent/observe', self.pc(self.players[0]))
        gm = self.call('gm/observe', self.gm)
        for obj in (a, gm):
            for key in ('truth', 'clues', 'seed', 'roll_secret', 'token', 'personal_goal'):
                if key == 'personal_goal' and obj is a:
                    continue
                self.assertNotIn('"'+key+'"', json.dumps(obj))
        self.assertEqual(a['information'], [])

    def test_activation_and_bad_plan_roll_back_pool_and_map(self):
        reserve = [aid for aid in self.members if aid not in self.players and aid != self.gm['controller_id']][0]
        before = self.state()
        self.assert_code('invalid_input', lambda: self.plan(activate=[{'agent_id': reserve}],
            tasks=[{'task_id': 't', 'agent_id': reserve, 'dependencies': ['missing']}]))
        self.assertEqual(before, self.state())
        card = next(a for a in self.call('pool/list', self.op)['agents'] if a['agent_id'] == reserve)
        self.assertEqual(card['pool_status'], 'available')

    def test_plan_records_alternatives_without_engine_choosing(self):
        self.plan(tasks=[{'task_id': 't', 'agent_id': self.players[0], 'kind': 'explore'}],
                  alternatives=[{'id': 'verify', 'summary': '검증'}, {'id': 'search', 'summary': '대체 조사'}],
                  selected_alternative='search', reason='external GM selection')
        s = self.call('gm/observe', self.gm)
        self.assertEqual(s['plans'][-1]['selected_alternative'], 'search')
        self.assertEqual(s['tasks'][0]['owner'], self.players[0])

    def test_task_dependency_cycle_rejected(self):
        self.assert_code('invalid_input', lambda: self.plan(tasks=[
            {'task_id': 'a', 'agent_id': self.players[0], 'dependencies': ['b']},
            {'task_id': 'b', 'agent_id': self.players[1], 'dependencies': ['a']}]))

    def test_action_identity_and_phase_consumption(self):
        result, req = self.action(self.players[0], 'stay')
        self.assertTrue(result['consumed'])
        self.assertEqual(self.call('agent/action', self.pc(self.players[0]), request=req), result)
        self.assert_code('conflict', lambda: self.action(self.players[0], 'stay'))
        self.assert_code('pending', self.advance)
        forged = req | {'agent_id': self.players[1]}
        self.assert_code('unauthorized', lambda: self.call('agent/action', self.pc(self.players[0]), request=forged))

    def test_action_id_content_collision_and_late_phase(self):
        result, req = self.action(self.players[0], 'stay')
        self.assert_code('conflict', lambda: self.call('agent/action', self.pc(self.players[0]),
            request=req | {'payload': {'room': 1}}))
        self.action(self.players[1], 'stay')
        self.advance()
        self.assertEqual(self.call('agent/action', self.pc(self.players[0]), request=req), result)
        self.assert_code('conflict', lambda: self.call('agent/action', self.pc(self.players[0]),
            request=req | {'action_id': 'late'}))

    def test_phase_epoch_is_not_task_generation(self):
        self.plan(tasks=[{'task_id': 'walk', 'agent_id': self.players[0], 'kind': 'move', 'room': 1}])
        r, _ = self.action(self.players[0], 'move', {'room': 1}, task_id='walk', generation=1)
        self.assertEqual(r['room'], 1)
        self.assertEqual(self.state()['tasks']['walk']['status'], 'completed')
        self.assert_code('conflict', lambda: self.plan(tasks=[
            {'task_id': 'walk', 'agent_id': self.players[1], 'kind': 'move', 'generation': 1}]))

    def test_task_dependency_prevents_early_execution(self):
        self.plan(tasks=[{'task_id': 'a', 'agent_id': self.players[0], 'kind': 'move'},
                         {'task_id': 'b', 'agent_id': self.players[1], 'kind': 'move', 'dependencies': ['a']}])
        self.assert_code('pending', lambda: self.action(self.players[1], 'move', {'room': 0}, task_id='b', generation=1))
        self.action(self.players[0], 'move', {'room': 0}, task_id='a', generation=1)
        self.action(self.players[1], 'move', {'room': 0}, task_id='b', generation=1)

    def test_question_pending_and_receiver_does_not_consume_action(self):
        r, req, clue = self.question()
        q = self.call('questions/inbox', self.pc(self.players[1]))['questions'][0]
        self.assertEqual(q['topic'], 'alibi')
        self.assertEqual(r['execution_status'], 'pending')
        self.assertNotIn(self.players[1], self.state()['spent'])
        self.call('questions/respond', self.pc(self.players[1]), question_id=q['question_id'],
                  response_id='reply', offer_ids=[clue['id']], refused=False)
        done = self.call('agent/result', self.pc(self.players[0]), action_id=req['action_id'])
        self.assertEqual(done['execution_status'], 'completed')
        self.assertNotIn(self.players[1], self.state()['spent'])
        self.assertEqual(self.call('agent/action', self.pc(self.players[0]), request=req), done)

    def test_question_refusal_is_not_automatic_evidence_transfer(self):
        r, req, clue = self.question()
        self.call('questions/respond', self.pc(self.players[1]), question_id=r['question_id'],
                  response_id='refusal', offer_ids=[], refused=True)
        done = self.call('agent/result', self.pc(self.players[0]), action_id=req['action_id'])
        self.assertEqual(done['outcome'], 'refused')
        self.assertEqual(done['acquired_information'], [])
        self.assertEqual(self.call('agent/observe', self.pc(self.players[0]))['information'], [])

    def test_response_owner_scope_and_exactly_once(self):
        r, req, clue = self.question()
        data = dict(question_id=r['question_id'], response_id='once', offer_ids=[clue['id']], refused=False)
        self.assert_code('unauthorized', lambda: self.call('questions/respond', self.pc(self.players[0]), **data))
        self.assert_code('unauthorized', lambda: self.call('questions/respond', self.pc(self.players[1]),
            **(data | {'offer_ids': ['unknown']})))
        result = self.call('questions/respond', self.pc(self.players[1]), **data)
        before = self.state()
        self.assertEqual(self.call('questions/respond', self.pc(self.players[1]), **data), result)
        self.assertEqual(before, self.state())
        self.assert_code('conflict', lambda: self.call('questions/respond', self.pc(self.players[1]),
            **(data | {'response_id': 'second'})))

    def test_question_timeout_durable_and_late_response_rejected(self):
        r, req, clue = self.question()
        self.clock.now += 31
        self.engine.tick()
        done = self.call('agent/result', self.pc(self.players[0]), action_id=req['action_id'])
        self.assertEqual(done['outcome'], 'timed_out')
        self.assert_code('expired', lambda: self.call('questions/respond', self.pc(self.players[1]),
            question_id=r['question_id'], response_id='late', offer_ids=[], refused=True))
        restored = ExternalEngine(self.path, self.clock)
        self.assertEqual(restored.handle('agent/result', self.pc(self.players[0]) | {'action_id': req['action_id']}), done)

    def test_question_blocks_phase_advance_until_completion(self):
        r, req, clue = self.question()
        self.action(self.players[1], 'pass')
        self.assert_code('pending', self.advance)
        self.call('questions/respond', self.pc(self.players[1]), question_id=r['question_id'],
                  response_id='no', offer_ids=[], refused=True)
        self.assertEqual(self.advance()['phase'], 'deduction')

    def test_no_normal_response_for_npc_and_no_wait_on_npc(self):
        self.interaction()
        self.inject_clue(self.players[1])
        self.call('games/operator', self.op, game_id='case-one', operation='FAIL_PLAYER', agent_id=self.players[1])
        r, _ = self.action(self.players[0], 'question', {'target_id': self.players[1]})
        self.assertEqual(r['execution_status'], 'completed')
        self.assertEqual(r['response_source'], 'npc_policy')
        self.assert_code('unauthorized', lambda: self.call('questions/inbox', self.pc(self.players[1])))

    def test_partial_disclosure_is_masked_not_full_exclusion(self):
        clue = self.inject_clue(self.players[1])
        self.call('agent/share', self.pc(self.players[1]), message_id='partial',
                  information_ids=[clue['id']], disclosure='partial')
        owner_view = self.call('agent/observe', self.pc(self.players[1]))
        self.assertTrue(owner_view['board'][clue['category']][clue['excluded_id']]['excluded'])
        self.assertEqual(self.call('documents/search', self.pc(self.players[1]), query=clue['text'])['documents'], [clue])
        obs = self.call('agent/observe', self.pc(self.players[0]))
        item = obs['shared_information'][0]
        self.assertTrue(item['redacted'])
        self.assertNotIn('excluded_id', item)
        self.assertNotIn(str(clue['excluded_id']), clue['id'].split(':')[0])
        self.assertFalse(obs['board'][clue['category']][clue['excluded_id']]['excluded'])
        self.call('agent/share', self.pc(self.players[1]), message_id='full', information_ids=[clue['id']])
        obs = self.call('agent/observe', self.pc(self.players[0]))
        self.assertTrue(obs['board'][clue['category']][clue['excluded_id']]['excluded'])

    def test_gm_has_ids_not_private_goal_or_clue_contents(self):
        clue = self.inject_clue(self.players[1])
        gm = self.call('gm/observe', self.gm)
        self.assertIn(clue['id'], json.dumps(gm))
        self.assertNotIn(clue['text'], json.dumps(gm, ensure_ascii=False))
        self.assertNotIn('personal_goal', json.dumps(gm))
        self.assert_code('unauthorized', lambda: self.plan(evidence_ids=[clue['id']]))

    def test_supervisor_events_redact_personal_result_body(self):
        self.interaction()
        self.action(self.players[0], 'explore')
        events = self.call('events', self.gm)['events']
        for e in events:
            if e['type'] == 'action_completed':
                self.assertNotIn('acquired_information', e['payload']['result'])
                self.assertIn('acquired_information_ids', e['payload']['result'])

    def test_player_cannot_read_others_receipts_or_events(self):
        r, req = self.action(self.players[1], 'stay')
        self.assert_code('unauthorized', lambda: self.call('agent/result', self.pc(self.players[0]), action_id=req['action_id']))
        events = self.call('events', self.pc(self.players[0]))
        self.assertFalse(any(e['type'] == 'action_completed' for e in events['events']))
        self.assertEqual(events['event_sequence'], len(self.state()['events']))

    def test_document_search_only_known_records(self):
        clue = self.inject_clue(self.players[0])
        result = self.call('documents/search', self.pc(self.players[0]), query='공식')
        self.assertEqual(result['documents'], [clue])
        self.assertEqual(self.call('documents/search', self.pc(self.players[1]))['documents'], [])
        self.assertEqual(self.state()['events'][-1]['type'], 'document_search')

    def test_fault_releases_only_incomplete_tasks_not_shared_results(self):
        with self.engine.transaction() as db:
            g = self.engine.load(db, 'games', 'case-one', 'id')
            g['rules']['min_players'] = 2
            self.engine.put(db, 'games', g['id'], g, 'id')
        self.plan(tasks=[{'task_id': 'walk', 'agent_id': self.players[0], 'kind': 'move'},
                         {'task_id': 'later', 'agent_id': self.players[0], 'kind': 'explore', 'room': 3}])
        self.action(self.players[0], 'move', {'room': 0}, task_id='walk', generation=1)
        clue = self.inject_clue(self.players[0])
        self.call('agent/share', self.pc(self.players[0]), message_id='pub', information_ids=[clue['id']])
        self.call('games/operator', self.op, game_id='case-one', operation='FAIL_PLAYER', agent_id=self.players[0])
        g = self.state()
        self.assertEqual(g['tasks']['walk']['status'], 'completed')
        self.assertEqual(g['tasks']['later']['status'], 'pending')
        self.assertIsNone(g['tasks']['later']['owner'])
        self.assertIn(clue['id'], g['shared'])
        self.assertIn(clue['id'], g['players'][self.players[0]]['information'])
        self.assertEqual(g['tasks']['later']['generation'], 2)
        self.plan(tasks=[{'task_id': 'later', 'agent_id': self.players[1], 'generation': 2}])
        self.assertEqual(self.state()['tasks']['later']['owner'], self.players[1])
        self.assertEqual(self.state()['tasks']['later']['room'], 3)

    def test_gm_promotes_same_persistent_agent_and_seals_character_memory(self):
        for aid in self.players:
            self.inject_clue(aid)
        old_gm = copy.deepcopy(self.gm)
        out = self.call('games/operator', self.op, game_id='case-one', operation='FAIL_GM', term=self.gm['term'])
        successor = out['controller']
        self.assertIn(successor['controller_id'], self.players)
        self.assertGreater(successor['term'], old_gm['term'])
        promoted = successor['controller_id']
        self.assert_code('unauthorized', lambda: self.call('agent/observe', self.pc(promoted)))
        self.assert_code('stale_controller', lambda: self.call('gm/observe', old_gm))
        view = self.call('gm/observe', successor | {'game_id': 'case-one'})
        self.assertNotIn('personal_goal', json.dumps(view))
        self.assertNotIn('excluded_id', json.dumps(view))
        self.assertEqual(self.state()['players'][promoted]['mode'], 'npc')
        self.assertEqual(sum(a['status'] == 'active' and a['role'] == 'gm' for a in self.state()['assignments']), 1)
        self.assertFalse(any(e['source'] == 'agent:'+promoted and e['relation'] in ('knows', 'controls')
                             for e in view['agent_map']['edges']))

    def test_expired_controller_cached_command_is_fenced(self):
        request = self.gm | dict(operation='PLAN', command_id='cache', map_version=1)
        self.engine.handle('gm/command', request)
        self.clock.now += 41
        self.assert_code('stale_controller', lambda: self.engine.handle('gm/command', request))

    def test_watchdog_succession_and_player_timeout(self):
        self.clock.now += 35
        for aid in self.players:
            self.call('agent/heartbeat', self.pc(aid))
        self.clock.now += 6
        self.engine.tick()
        self.assertEqual(self.state()['term'], 2)
        self.assertIsNotNone(self.state()['controller'])
        self.clock.now += 41
        self.engine.tick()
        self.assertEqual(self.state()['status'], 'waiting_for_human')
        self.assertIsNone(self.state()['controller'])

    def test_two_engine_instances_elect_one_successor(self):
        other = ExternalEngine(self.path, self.clock)
        self.clock.now += 35
        for aid in self.players:
            self.call('agent/heartbeat', self.pc(aid))
        self.clock.now += 6
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda e: e.tick(), [self.engine, other]))
        self.assertEqual(self.state()['term'], 2)
        self.assertEqual(sum(a['role'] == 'gm' and a['status'] == 'active' for a in self.state()['assignments']), 1)

    def test_concurrent_duplicate_action_consumes_once(self):
        obs = self.call('agent/observe', self.pc(self.players[0]))
        req = {k: obs[k] for k in ('game_id', 'cycle_id', 'phase', 'phase_epoch')}
        req.update(agent_id=self.players[0], action_id='same', action='stay', payload={})
        other = ExternalEngine(self.path, self.clock)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda e: e.handle('agent/action', self.pc(self.players[0]) | {'request': req}),
                                    [self.engine, other]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(sum(e['type'] == 'action_completed' for e in self.state()['events']), 1)

    def test_no_successor_operator_can_appoint_and_finish(self):
        for aid in self.players:
            self.call('games/operator', self.op, game_id='case-one', operation='FAIL_PLAYER', agent_id=aid)
        self.call('games/operator', self.op, game_id='case-one', operation='FAIL_GM', term=1)
        self.assertIsNone(self.state()['controller'])
        self.assertEqual(self.state()['status'], 'waiting_for_human')
        reserve = [a for a in self.call('pool/list', self.op)['agents'] if a['pool_status'] == 'available'][0]
        result = self.call('games/operator', self.op, game_id='case-one', operation='APPOINT_GM', agent_id=reserve['agent_id'])
        self.assertEqual(result['controller']['term'], 2)
        self.assertEqual(result['state']['status'], 'awaiting_setup')
        self.call('games/operator', self.op, game_id='case-one', operation='FINISH')
        self.assertTrue(self.state()['ended'])

    def test_repeat_failover_limit_and_operator_override(self):
        with self.engine.transaction() as db:
            g = self.engine.load(db, 'games', 'case-one', 'id')
            g['rules']['max_failovers'] = 1
            self.engine.put(db, 'games', g['id'], g, 'id')
        self.call('games/operator', self.op, game_id='case-one', operation='FAIL_GM', term=1)
        self.call('games/operator', self.op, game_id='case-one', operation='FAIL_GM', term=2)
        self.assertIsNone(self.state()['controller'])
        self.assertEqual(self.state()['status'], 'waiting_for_human')

    def test_operator_pause_resume_freezes_timer(self):
        self.call('games/operator', self.op, game_id='case-one', operation='PAUSE')
        self.clock.now += 100
        self.engine.tick()
        self.assertEqual(self.state()['term'], 1)
        self.assert_code('not_running', lambda: self.action(self.players[0], 'stay'))
        self.call('games/operator', self.op, game_id='case-one', operation='RESUME')
        self.action(self.players[0], 'stay')

    def test_engine_feedback_and_agent_owned_strategy_cas(self):
        self.action(self.players[0], 'stay')
        member = self.members[self.players[0]]
        creds = {'agent_id': member['agent_id'], 'pool_token': member['pool_token']}
        l = self.call('pool/learning/get', creds)
        eid = l['experience_events'][0]['event_id']
        self.assertEqual(l['learning']['competencies']['player']['attempts'], 1)
        strategy = {'tactics': ['ask_before_duplicate_search'], 'weights': {'information': 2}}
        updated = self.call('pool/learning/update', creds, expected_version=0,
                            experience_event_ids=[eid], strategy_state=strategy)
        self.assertEqual(updated['learning']['version'], 1)
        self.assertEqual(updated['learning']['strategy_state'], strategy)
        self.assert_code('conflict', lambda: self.call('pool/learning/update', creds, expected_version=0,
            experience_event_ids=[eid], strategy_state=strategy))
        self.assert_code('conflict', lambda: self.call('pool/learning/update', creds, expected_version=1,
            experience_event_ids=[eid], strategy_state=strategy))
        restored = ExternalEngine(self.path, self.clock)
        self.assertEqual(restored.handle('pool/learning/get', creds)['learning'], updated['learning'])

    def test_long_term_store_rejects_game_secrets_or_forged_feedback(self):
        self.action(self.players[0], 'stay')
        member = self.members[self.players[0]]
        creds = {'agent_id': member['agent_id'], 'pool_token': member['pool_token']}
        eid = self.call('pool/learning/get', creds)['experience_events'][0]['event_id']
        self.assert_code('invalid_input', lambda: self.call('pool/learning/update', creds, expected_version=0,
            experience_event_ids=[eid], strategy_state={'truth': {'suspect': 1}, 'tactics': [], 'weights': {}}))
        wrong_creds = creds | {'agent_id': self.players[1]}
        self.assert_code('unauthorized', lambda: self.call('pool/learning/get', wrong_creds))

    def test_next_game_reuses_identity_strategy_but_not_private_clues(self):
        aid = self.players[0]
        self.inject_clue(aid)
        self.action(aid, 'stay')
        member = self.members[aid]
        lc = {'agent_id': aid, 'pool_token': member['pool_token']}
        feedback = self.call('pool/learning/get', lc)['experience_events'][0]['event_id']
        self.call('pool/learning/update', lc, expected_version=0, experience_event_ids=[feedback],
                  strategy_state={'tactics': ['verify_before_accuse'], 'weights': {}})
        self.call('games/operator', self.op, game_id='case-one', operation='FINISH')
        other = next(x for x in self.members if x != aid)
        created = self.call('games/create', self.op, game_id='case-two', candidate_ids=[other])
        gc = created['controller'] | {'game_id': 'case-two'}
        self.call('gm/command', gc, operation='PLAN', command_id='setup', map_version=0, activate=[{'agent_id': aid}])
        pc = self.call('games/credentials', self.op, game_id='case-two')['agent_credentials'][aid] | {'game_id': 'case-two'}
        obs = self.call('agent/observe', pc)
        self.assertEqual(obs['information'], [])
        self.assertEqual(obs['shared_information'], [])
        self.assertNotEqual(obs['self']['assignment_id'], self.creds[aid]['assignment_id'])
        self.assertEqual(self.call('pool/learning/get', lc)['learning']['version'], 1)

    def test_competency_selection_uses_official_episode_not_claimed_scores(self):
        with self.engine.transaction() as db:
            g = self.engine.load(db, 'games', 'case-one', 'id')
            for i in range(4):
                self.engine.experience(db, g, 'p4', f'official:{i}', 'gm', 'solved', True)
        selected, reason = None, None
        with self.engine.transaction() as db:
            selected, reason = self.engine.select(db, ['p0', 'p4'], 1, 0)
        self.assertEqual(selected, 'p4')
        self.assertEqual(reason['mode'], 'competency')

    def test_b_projection_uses_official_ids_and_redaction(self):
        clue = self.inject_clue(self.players[0])
        notice, obs = reasoning_inputs(self.call('agent/observe', self.pc(self.players[0])))
        self.assertEqual(obs['information'][0]['excluded_id'], clue['excluded_id'])
        self.assertEqual(set(obs['candidates']), {'suspect', 'weapon', 'place'})
        self.assertIsInstance(obs['self']['personality'], dict)
        self.assertIsInstance(obs['self']['personal_goal'], dict)
        self.assertEqual(notice['agent_id'], self.players[0])
        _, legacy = reasoning_inputs(self.call('agent/observe', self.pc(self.players[0])), legacy=True)
        self.assertEqual(legacy['information'][0]['excludes'], clue['excluded_id'])
        self.assertEqual(set(legacy['candidates']), {'suspects', 'weapons', 'places'})

    def test_legacy_store_is_not_migrated_to_external_mode(self):
        store = Store(self.path)
        import multi
        game = multi.create(2, 42)
        store.save(game)
        self.engine.tick()
        self.assertEqual(store.get(game['id'])['kind'], 'multi')

    def test_archive_final_results_and_ended_mutations(self):
        result, req = self.action(self.players[0], 'stay')
        self.call('games/operator', self.op, game_id='case-one', operation='FINISH')
        self.assertEqual(self.call('agent/result', self.pc(self.players[0]), action_id=req['action_id']), result)
        self.assertTrue(self.call('gm/observe', self.gm)['ended'])
        self.assertEqual(self.call('agent/action', self.pc(self.players[0]), request=req), result)
        self.assert_code('not_running', lambda: self.call('agent/action', self.pc(self.players[0]),
            request=req | {'action_id': 'new-ended-action'}))

    def test_http_client_and_error_contract(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.external_engine = self.engine
        server.store = Store(self.path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = EngineClient(f'http://127.0.0.1:{server.server_port}')
        self.assertEqual(client.call('catalog')['schema_version'], VERSION)
        player = client.bind(**self.pc(self.players[0]))
        self.assertEqual(player.observe()['self']['id'], self.players[0])
        with self.assertRaises(EngineAPIError) as caught:
            client.call('pool/list')
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(caught.exception.body['code'], 'unauthorized')

    def test_watchdog_thread_lifecycle(self):
        watchdog = ExternalWatchdog(self.engine, interval=.01)
        watchdog.start()
        self.assertTrue(watchdog.thread.is_alive())
        watchdog.close()
        self.assertFalse(watchdog.thread.is_alive())

    def test_actual_consent_transfer_changes_requester_knowledge(self):
        self.interaction()
        clue = self.inject_clue(self.players[1])
        with patch('integration.random.Random') as rng:
            rng.return_value.randint.return_value = 6
            r, req = self.action(self.players[0], 'question', {'target_id': self.players[1]})
        before = self.call('agent/observe', self.pc(self.players[0]))
        self.call('questions/respond', self.pc(self.players[1]), question_id=r['question_id'],
                  response_id='consent', offer_ids=[clue['id']], refused=False)
        after = self.call('agent/observe', self.pc(self.players[0]))
        self.assertFalse(before['board'][clue['category']][clue['excluded_id']]['excluded'])
        self.assertTrue(after['board'][clue['category']][clue['excluded_id']]['excluded'])
        self.assertEqual(after['information'], [clue])
        self.assertEqual(after['shared_information'], [])
        self.assertNotIn(clue['text'], json.dumps(self.call('gm/observe', self.gm), ensure_ascii=False))

    def test_partial_question_offer_cannot_be_promoted_to_full_fact(self):
        self.interaction()
        clue = self.inject_clue(self.players[1])
        with patch('integration.random.Random') as rng:
            rng.return_value.randint.return_value = 6
            r, req = self.action(self.players[0], 'question', {'target_id': self.players[1]})
        self.call('questions/respond', self.pc(self.players[1]), question_id=r['question_id'],
                  response_id='part', offer_ids=[clue['id']], refused=False, disclosure='partial')
        obs = self.call('agent/observe', self.pc(self.players[0]))
        self.assertNotIn('excluded_id', obs['information'][0])
        self.call('agent/share', self.pc(self.players[0]), message_id='masked-republish', information_ids=[clue['id']])
        self.assertNotIn('excluded_id', self.state()['shared'][clue['id']])

    def test_pending_question_reassignment_fences_late_task_result(self):
        self.interaction()
        self.plan(tasks=[{'task_id': 'interview', 'agent_id': self.players[0], 'kind': 'question'}])
        clue = self.inject_clue(self.players[1])
        r, req = self.action(self.players[0], 'question', {'target_id': self.players[1]}, task_id='interview', generation=1)
        self.plan(tasks=[{'task_id': 'interview', 'agent_id': self.players[1], 'kind': 'question', 'generation': 1}])
        done = self.call('agent/result', self.pc(self.players[0]), action_id=req['action_id'])
        self.assertEqual(done['outcome'], 'stale_task')
        self.assertEqual(done['acquired_information'], [])
        self.assert_code('expired', lambda: self.call('questions/respond', self.pc(self.players[1]),
            question_id=r['question_id'], response_id='stale', offer_ids=[clue['id']], refused=False))

    def test_pause_preserves_pending_question_deadline(self):
        r, req, clue = self.question()
        old_deadline = self.state()['questions'][r['question_id']]['deadline']
        self.call('games/operator', self.op, game_id='case-one', operation='PAUSE')
        self.clock.now += 100
        self.engine.tick()
        self.call('games/operator', self.op, game_id='case-one', operation='RESUME')
        self.assertEqual(self.state()['questions'][r['question_id']]['deadline'], old_deadline+100)
        self.call('questions/respond', self.pc(self.players[1]), question_id=r['question_id'],
                  response_id='resumed', offer_ids=[], refused=True)

    def test_solved_game_gm_finish_receipt_and_official_feedback(self):
        self.interaction()
        for aid in self.players:
            self.action(aid, 'pass')
        self.advance()
        result, req = self.action(self.players[0], 'accuse', self.state()['truth'])
        self.assertEqual(result['outcome'], 'solved')
        data = self.gm | {'command_id': 'finish', 'operation': 'FINISH'}
        done = self.engine.handle('gm/command', data)
        self.assertTrue(done['ended'])
        self.assertEqual(self.engine.handle('gm/command', data), done)
        aid = self.gm['controller_id']
        member = self.members[aid]
        l = self.call('pool/learning/get', {'agent_id': aid, 'pool_token': member['pool_token']})
        self.assertEqual(l['learning']['competencies']['gm']['successes'], 1)

    def test_public_agent_map_and_metrics_do_not_include_private_text(self):
        self.plan(tasks=[{'task_id': 'map-task', 'agent_id': self.players[0], 'kind': 'move'}])
        clue = self.inject_clue(self.players[0])
        view = self.call('gm/observe', self.gm)
        self.assertIn({'source': 'task:map-task', 'relation': 'assignedTo', 'target': 'agent:'+self.players[0]}, view['agent_map']['edges'])
        self.assertNotIn(clue['text'], json.dumps(view['agent_map'], ensure_ascii=False))
        self.assertEqual(view['metrics']['task_completion_rate'], 0)
        self.assertEqual(view['metrics']['duration'], 0)

    def test_capability_recovery_and_version_rejection(self):
        recovered = self.call('pool/credentials', self.op, agent_id=self.players[0])
        self.assertEqual(recovered['pool_token'], self.members[self.players[0]]['pool_token'])
        self.assertEqual(len(self.call('games/list', self.op)['games']), 1)
        self.assert_code('unsupported_version', lambda: self.call('catalog', schema_version='other'))

    def test_cli_operator_uses_same_db_and_does_not_need_gm(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, '-B', 'integration_cli.py', '--data', self.path,
            'call', '--route', 'games/observe', '--json', '{"game_id":"case-one"}'],
            capture_output=True, text=True, encoding='utf-8', timeout=10,
            env=__import__('os').environ | {'PYTHONIOENCODING': 'utf-8'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['game_id'], 'case-one')

    def test_waiting_operator_snapshot_survives_loss_of_all_gm_credentials(self):
        for aid in self.players:
            self.call('games/operator', self.op, game_id='case-one', operation='FAIL_PLAYER', agent_id=aid)
        self.call('games/operator', self.op, game_id='case-one', operation='FAIL_GM', term=1)
        view = self.call('games/observe', self.op, game_id='case-one')
        self.assertIsNone(view['control'])
        self.assertEqual(view['operator_choices'], ['APPOINT_GM', 'FINISH'])

    def test_simultaneous_lease_expiry_does_not_promote_dead_player(self):
        self.clock.now += 41
        self.engine.tick()
        state = self.state()
        self.assertIsNone(state['controller'])
        self.assertEqual(state['status'], 'waiting_for_human')
        self.assertTrue(all(a['mode'] == 'npc' for a in state['players'].values()))

    def test_configured_pool_fallback_appoints_available_common_agent(self):
        with self.engine.transaction() as db:
            g = self.engine.load(db, 'games', 'case-one', 'id')
            g['rules']['successor_scope'] = 'active_then_pool'
            self.engine.put(db, 'games', g['id'], g, 'id')
        self.clock.now += 41
        self.engine.tick()
        state = self.state()
        self.assertIsNotNone(state['controller'])
        self.assertNotIn(state['controller']['controller_id'], self.players)
        self.assertEqual(state['status'], 'awaiting_setup')

    def test_opt_in_supervision_does_not_expose_undiscovered_truth(self):
        clue = self.inject_clue(self.players[0])
        with self.engine.transaction() as db:
            g = self.engine.load(db, 'games', 'case-one', 'id')
            g['rules']['gm_visibility'] = 'acquired_content'
            self.engine.put(db, 'games', g['id'], g, 'id')
        view = self.call('gm/observe', self.gm)
        self.assertEqual(view['supervised_information'][self.players[0]], [clue])
        self.assertEqual(view['supervised_information'][self.players[1]], [])
        self.assertNotIn('truth', view)
        self.assertNotIn('personal_goal', json.dumps(view))

    def test_persistent_runtime_claims_only_its_current_role(self):
        aid = self.players[0]
        persistent = {'agent_id': aid, 'pool_token': self.members[aid]['pool_token']}
        session = self.call('pool/session', persistent)
        self.assertEqual(session['role'], 'player')
        self.assertEqual(session['credentials']['token'], self.creds[aid]['token'])
        self.assert_code('unauthorized', lambda: self.call('pool/session', persistent | {'agent_id': self.players[1]}))
        successor = self.call('games/operator', self.op, game_id='case-one', operation='FAIL_GM', term=1)['controller']
        promoted = successor['controller_id']
        next_session = self.call('pool/session', {'agent_id': promoted, 'pool_token': self.members[promoted]['pool_token']})
        self.assertEqual(next_session['role'], 'gm')
        self.assertEqual(next_session['credentials']['controller_token'], successor['controller_token'])
        self.assertNotIn('token', next_session['credentials'])
        self.assertNotIn('pool_token', next_session['credentials'])
        self.assertEqual(next_session['context_source'], 'engine-gm-observation-only')
        old_gm = self.gm['controller_id']
        self.assertEqual(self.call('pool/session', {'agent_id': old_gm,
            'pool_token': self.members[old_gm]['pool_token']})['role'], 'inactive')

    def test_concurrent_competing_responses_commit_only_one(self):
        r, req, clue = self.question()
        other = ExternalEngine(self.path, self.clock)
        def respond(args):
            engine, response_id = args
            try:
                return engine.handle('questions/respond', self.pc(self.players[1]) |
                    {'question_id': r['question_id'], 'response_id': response_id, 'offer_ids': [], 'refused': True})
            except ContractError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(respond, [(self.engine, 'reply-one'), (other, 'reply-two')]))
        self.assertEqual(sum(isinstance(x, dict) for x in results), 1)
        self.assertIn('conflict', results)
        self.assertEqual(sum(e['type'] == 'question_completed' for e in self.state()['events']), 1)


if __name__ == '__main__':
    unittest.main()
