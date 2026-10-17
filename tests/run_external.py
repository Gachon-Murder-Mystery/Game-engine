"""Live HTTP conformance scenario using explicit scripted B/C test doubles.

No teammate imports, LLM calls, user database, or production credentials.
"""
import json
import sys
import tempfile
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import Handler, Store
from engine_client import EngineClient, EngineAPIError
from integration import ExternalEngine, ExternalWatchdog


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory)/'conformance.sqlite3')
        engine = ExternalEngine(path)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.store, server.external_engine = Store(path), engine
        watchdog = ExternalWatchdog(engine)
        server.external_watchdog = watchdog
        watchdog.start()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = EngineClient(f'http://127.0.0.1:{server.server_port}')
            operator = client.bind(operator_token=engine.operator_token)
            members = {aid: operator.call('pool/register', agent_id=aid, model_version='scripted-conformance-double')
                       for aid in ('common-1', 'common-2', 'common-3', 'common-4')}
            created = operator.call('games/create', game_id='http-game-1', seed=42)
            gid = created['game_id']
            gm_identity = client.bind(agent_id=created['controller']['controller_id'],
                pool_token=members[created['controller']['controller_id']]['pool_token'])
            gm = gm_identity.role_client(gm_identity.session())
            aids = [aid for aid in members if aid != created['controller']['controller_id']][:2]
            gm.call('gm/command', operation='PLAN', command_id='setup', map_version=0,
                    activate=[{'agent_id': aid, 'job_id': i, 'personality': 'cooperative'} for i, aid in enumerate(aids)],
                    tasks=[{'task_id': 'first-investigation', 'agent_id': aids[0], 'kind': 'explore', 'room': 0}],
                    alternatives=[{'id': 'split', 'summary': '두 공간을 병렬 조사'},
                                  {'id': 'together', 'summary': '동일 공간의 증거를 대조'}],
                    selected_alternative='split', reason='scripted external GM, not engine planning')
            identities = {aid: client.bind(agent_id=aid, pool_token=members[aid]['pool_token']) for aid in aids}
            players = {aid: identities[aid].role_client(identities[aid].session()) for aid in aids}

            def action(aid, act, payload=None, task_id=None):
                actor = players[aid]
                obs = actor.observe()
                request = actor.action_request(obs, act, payload, task_id)
                result = actor.submit_action(request)
                assert actor.submit_action(request) == result
                return result

            def advance():
                gm.heartbeat()
                for actor in players.values():
                    actor.heartbeat()
                obs = gm.observe()
                return gm.call('gm/command', operation='ADVANCE', command_id=str(uuid.uuid4()), phase_epoch=obs['phase_epoch'])

            # Test-double policy: collect from different rooms, never read hidden engine state.
            for _ in range(40):
                action(aids[0], 'move', {'room': 0})
                action(aids[1], 'move', {'room': 1})
                advance()
                assigned = players[aids[0]].observe()['tasks'][0]
                action(aids[0], 'explore', task_id=assigned['task_id'] if assigned['status'] == 'assigned' else None)
                action(aids[1], 'explore')
                advance()
                action(aids[0], 'pass')
                action(aids[1], 'pass')
                advance()
                if players[aids[1]].observe()['information']:
                    break
            assert players[aids[1]].observe()['information'], 'scripted investigation exhausted its budget'

            refused_once, transferred = False, False
            for _ in range(40):
                for aid in aids:
                    action(aid, 'move', {'room': 0})
                advance()
                question = action(aids[0], 'question', {'target_id': aids[1], 'topic': '자료 대조 요청'})
                inbox = players[aids[1]].call('questions/inbox')['questions']
                assert inbox[0]['question_id'] == question['question_id']
                offer = [x['id'] for x in players[aids[1]].observe()['information']]
                response = dict(question_id=question['question_id'], response_id=str(uuid.uuid4()),
                                offer_ids=offer if refused_once else [], refused=not refused_once)
                players[aids[1]].call('questions/respond', **response)
                done = players[aids[0]].receive_result(question['action_id'])
                assert done['execution_status'] == 'completed'
                assert players[aids[1]].observe()['remaining_actions'] == 1
                refused_once = True
                transferred = bool(done['acquired_information'])
                action(aids[1], 'pass')
                advance()
                action(aids[0], 'pass')
                action(aids[1], 'pass')
                advance()
                if transferred:
                    break
            assert transferred, 'scripted questioning exhausted its budget'
            # A received private clue is not automatically public or copied into GM memory.
            observation = players[aids[0]].observe()
            received_ids = [x['id'] for x in done['acquired_information']]
            assert all(cid in [x['id'] for x in observation['information']] for cid in received_ids)
            assert gm.observe()['shared_information'] == []
            search = players[aids[0]].call('documents/search', query='공식')
            assert search['documents']

            # Runtime-owned generalized update references official feedback, never raw clue text.
            learner = client.bind(agent_id=aids[0], pool_token=members[aids[0]]['pool_token'])
            prior = learner.call('pool/learning/get')
            updated = learner.call('pool/learning/update', expected_version=0,
                experience_event_ids=[prior['experience_events'][-1]['event_id']],
                strategy_state={'tactics': ['ask_before_duplicate_search'], 'weights': {'information': 2}})
            assert updated['learning']['version'] == 1

            old_gm = gm
            succession = operator.call('games/operator', game_id=gid, operation='FAIL_GM', term=created['controller']['term'])
            new_id = succession['controller']['controller_id']
            promoted_session = identities[new_id].session()
            assert promoted_session['role'] == 'gm'
            gm = identities[new_id].role_client(promoted_session)
            assert new_id in aids and gm.observe()['control']['term'] == 2
            assert 'personal_goal' not in json.dumps(gm.observe())
            try:
                old_gm.observe()
                raise AssertionError('old GM retained authority')
            except EngineAPIError as exc:
                assert exc.body['code'] == 'stale_controller'
            try:
                players[new_id].observe()
                raise AssertionError('promoted GM retained player credentials')
            except EngineAPIError as exc:
                assert exc.body['code'] == 'unauthorized'

            before_end = gm.observe()
            operator.call('games/operator', game_id=gid, operation='FINISH')
            learner_id = aids[0]
            eligible = operator.call('pool/list')['agents']
            second_gm = next(a['agent_id'] for a in eligible if a['pool_status'] == 'available' and a['agent_id'] != learner_id)
            second = operator.call('games/create', game_id='http-game-2', seed=43, candidate_ids=[second_gm])
            gm2 = client.bind(game_id=second['game_id'], **second['controller'])
            gm2.call('gm/command', operation='PLAN', command_id='setup2', map_version=0,
                     activate=[{'agent_id': learner_id}])
            actor2 = learner.role_client(learner.session())
            assert actor2.observe()['information'] == []
            assert learner.call('pool/learning/get')['learning']['version'] == 1
            operator.call('games/operator', game_id=second['game_id'], operation='FINISH')
            print(json.dumps({'status': 'PASS', 'runtime': 'scripted-external-test-doubles', 'live_llm': False,
                'checks': ['pool identity and own-role session bootstrap', 'external GM plan and map', 'direct actions and replay',
                           'request/refusal/consent/knowledge change', 'permission-scoped search',
                           'GM promotion/NPC/old-capability fencing', 'official feedback/strategy CAS',
                           'new game clears secrets and retains identity/strategy'],
                'first_game_metrics': before_end['metrics'], 'strategy_version': updated['learning']['version']},
                ensure_ascii=False, indent=2))
        finally:
            watchdog.close()
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == '__main__':
    main()
