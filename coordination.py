"""Engine-owned leadership contract, independent of every GM's reasoning.

All mutations must run inside Store.transaction. The DB, not agent consensus,
is the authority for the one current controller and monotonically growing term.
"""
import copy
import json
import secrets
import time


class StaleController(ValueError):
    pass


def event(g, kind, **fields):
    g['events'].append({'seq':len(g['events'])+1, 'game_id':g['id'],
        'action_id':None, 'agent_id':fields.get('controller_id'), 'task_id':None,
        'status':kind, 'shared_evidence_ids':[], 'timestamp':time.time(), **fields})


def initialize(g, now=None, lease=40):
    if 'controller' in g:
        return
    now=time.time() if now is None else now
    g['gm_agents']=[]
    g['gm_commands']={}
    g['engine_runner']=None
    g['runner_sessions']=0
    g['metrics'].setdefault('gm_failovers',0)
    install(g,1,now,lease,'initial')


def install(g, term, now, lease, reason):
    controller={'id':f'gm-{term}', 'term':term, 'status':'active',
        'object_type':'agent',
        'token':secrets.token_urlsafe(32), 'heartbeat':now, 'lease_until':now+lease,
        'event_cursor':0, 'summary':'', 'reason':reason}
    g['gm_agents'].append(controller)
    g['controller']={'id':controller['id'],'term':term}
    event(g,'gm_elected',controller_id=controller['id'],term=term,reason=reason)


def current(g):
    initialize(g)
    return next(a for a in g['gm_agents'] if a['id']==g['controller']['id'])


def failover(g, expected_term, reason='gm_failure', now=None, lease=40):
    """Compare term, retire old actor and elect successor in ONE atomic commit."""
    now=time.time() if now is None else now
    old=current(g)
    if old['term']!=expected_term or g['ended']:
        return False
    old.update(status='npc',object_type='npc', retired_at=now, failure_reason=reason, token='')
    event(g,'gm_retired',controller_id=old['id'],term=old['term'],reason=reason)
    g['metrics']['gm_failovers']+=1
    install(g,expected_term+1,now,lease,reason)
    return True


def credentials(g):
    a=current(g)
    return {'controller_id':a['id'],'term':a['term'],'controller_token':a['token']}


def verify(g, request, now=None):
    now=time.time() if now is None else now
    a=current(g)
    token=request.get('controller_token')
    if (g['ended'] or request.get('controller_id')!=a['id'] or
        type(request.get('term')) is not int or request['term']!=a['term'] or
        a['status']!='active' or now>=a['lease_until'] or
        not isinstance(token,str) or not secrets.compare_digest(token,a['token'])):
        raise StaleController('현재 GM의 유효한 임기와 권한이 필요합니다.')
    return a


def observation(g):
    """GM supervision owns metadata; it never obtains truth or private contents.

    Owner/evidence IDs are exclusive GM supervision data, not player observations.
    A successor reconstructs from this ledger, never from another actor's memory.
    """
    from multi import view
    result=view(g)
    result['supervision']=[{'agent_id':a['id'], 'evidence_ids':[c['id'] for c in a['private']]}
                          for a in g['agents'].values()]
    result['event_cursor']=current(g)['event_cursor']
    return result


def plan(observation, gateway=None):
    """Independent GM agent policy. Engine scheduling never reasons for the GM."""
    busy={t['owner'] for t in observation['tasks'] if t['status']=='assigned'}
    pending=[t for t in observation['tasks'] if t['status']=='pending']
    free=[a['id'] for a in observation['agents'] if a['status']=='active' and a['id'] not in busy]
    proposal={'assignments':[{'task_id':t['id'],'agent_id':aid,'generation':t['generation']}
                            for t,aid in zip(pending,free)],
              'summary':f"공유 증거 {len(observation['shared'])}개, 미배정 작업 {len(pending)}개.",
              'event_cursor':max((e['seq'] for e in observation['events']),default=0)}
    if gateway and gateway.key:
        # Exceptions propagate to the engine watchdog: GM failure triggers succession.
        raw=gateway.request(
            '당신은 수사팀의 GM 조정 에이전트다. 감독용 소유 ID는 공개 증거와 구분한다. '
            '개인 목표·정답을 추측하거나 개인 증거를 공개하지 마라. JSON 객체로 '
            'assignments([{task_id,agent_id,generation}]), summary를 반환한다. '
            'pending 작업만 active이며 현재 작업이 없는 플레이어에게 배정한다.',
            json.dumps({'observation':observation,'baseline':proposal},ensure_ascii=False),700)
        parsed=json.loads(raw)
        if (not isinstance(parsed,dict) or not isinstance(parsed.get('assignments'),list) or
            not isinstance(parsed.get('summary',''),str) or len(parsed.get('summary',''))>1000 or
            any(not isinstance(item,dict) or not isinstance(item.get('task_id'),str) or
                not isinstance(item.get('agent_id'),str) or type(item.get('generation')) is not int
                for item in parsed['assignments'])):
            raise ValueError('GM 응답 형식 오류')
        proposal.update({k:parsed[k] for k in ('assignments','summary') if k in parsed})
    return proposal


def command(g, request, now=None, lease=40):
    """Validate all assignments before changing anything, fence even cached commands."""
    a=verify(g,request,now)
    command_id=request.get('command_id')
    if not isinstance(command_id,str) or not 1<=len(command_id)<=100:
        raise ValueError('GM command_id가 필요합니다.')
    encoded=json.dumps(request,sort_keys=True)
    old=g['gm_commands'].get(command_id)
    if old:
        if old['request']!=encoded:raise ValueError('GM command_id 재사용 오류')
        return copy.deepcopy(old['result'])
    assignments=request.get('assignments',[])
    operation=request.get('operation','ASSIGN')
    if operation not in ('ASSIGN','END'):raise ValueError('지원하지 않는 GM 명령입니다.')
    cursor=request.get('event_cursor',a['event_cursor'])
    summary=request.get('summary','')
    if not isinstance(assignments,list) or not isinstance(summary,str) or len(summary)>1000:
        raise ValueError('GM 배정·요약 형식을 확인하세요.')
    if type(cursor) is not int or not a['event_cursor']<=cursor<=len(g['events']):
        raise ValueError('GM 이벤트 순서를 확인하세요.')
    if operation=='END' and assignments:raise ValueError('종료와 작업 배정을 동시에 요청할 수 없습니다.')
    selected=[]; owners=set(); task_ids=set()
    busy={t['owner'] for t in g['tasks'] if t['status']=='assigned'}
    for item in assignments:
        if not isinstance(item,dict):raise ValueError('배정 형식 오류')
        task=next((t for t in g['tasks'] if t['id']==item.get('task_id')),None)
        aid=item.get('agent_id')
        if (not isinstance(aid,str) or aid not in g['agents'] or
            g['agents'][aid]['status']!='active' or aid in busy or aid in owners or
            not task or task['id'] in task_ids or task['status']!='pending' or
            type(item.get('generation')) is not int or task['generation']!=item['generation']):
            raise ValueError('GM 배정 시점의 작업·플레이어 상태가 변경되었습니다.')
        owners.add(aid);task_ids.add(task['id']);selected.append((task,aid))
    for task,aid in selected:
        task.update(status='assigned',owner=aid,generation=task['generation']+1)
        g['ownership'].append({'task_id':task['id'],'owner':aid,'generation':task['generation'],
                              'reason':'gm_assignment','controller_id':a['id'],'term':a['term']})
    now=time.time() if now is None else now
    a.update(heartbeat=now,lease_until=now+lease,event_cursor=cursor,summary=summary)
    result={'controller_id':a['id'],'term':a['term'],'assigned':len(selected),'event_cursor':cursor}
    if operation=='END':
        g.update(ended=True,won=False,status='stopped',finished_at=now)
        event(g,'gm_requested_end',controller_id=a['id'],term=a['term'])
        result['status']='stopped'
    g['gm_commands'][command_id]={'request':encoded,'result':copy.deepcopy(result)}
    return result


def claim_runner(g, runner_id, now=None, lease=5):
    """One engine scheduler per game, including separate Runtime/Store instances."""
    initialize(g)
    now=time.time() if now is None else now
    old=g['engine_runner']
    if old and old['id']!=runner_id and old['lease_until']>now:
        return False
    if (old and (old['id']!=runner_id or old['lease_until']<=now)) or (
            not old and g.get('runner_sessions',0)>0):
        failover(g,current(g)['term'],'engine_runner_recovery',now)
    if not old or old['id']!=runner_id:
        g['runner_sessions']=g.get('runner_sessions',0)+1
    g['engine_runner']={'id':runner_id,'lease_until':now+lease}
    return True
