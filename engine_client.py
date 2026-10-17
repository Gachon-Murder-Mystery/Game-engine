"""Dependency-free external agent tool client. This contains no agent policy."""
import json
import urllib.error
import urllib.request


class EngineAPIError(Exception):
    def __init__(self, status, body):
        self.status, self.body = status, body
        super().__init__(f"{status} {body.get('code', '')}: {body.get('error', '')}")


class EngineClient:
    def __init__(self, base_url='http://127.0.0.1:8765', credentials=None, timeout=10):
        self.base_url = base_url.rstrip('/')
        self.credentials = dict(credentials or {})
        self.timeout = timeout

    def bind(self, **credentials):
        """Separate clients for operator / persistent identity / game role."""
        return EngineClient(self.base_url, credentials, self.timeout)

    def call(self, route, **data):
        request = urllib.request.Request(self.base_url+'/integration/v1/'+route,
            json.dumps(self.credentials | data, ensure_ascii=False, allow_nan=False).encode(),
            {'Content-Type': 'application/json', 'X-GM-Client': 'local'})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            with exc:
                raise EngineAPIError(exc.code, json.load(exc)) from exc

    def observe(self):
        return self.call('gm/observe' if 'controller_token' in self.credentials else 'agent/observe')

    def heartbeat(self):
        return self.call('gm/heartbeat' if 'controller_token' in self.credentials else 'agent/heartbeat')

    def session(self):
        """Use a pool identity client; rebuild role context on context_key change."""
        return self.call('pool/session')

    def role_client(self, session):
        if not session.get('credentials'):
            raise ValueError('no active engine-assigned role')
        return self.bind(**session['credentials'])

    def submit_action(self, request):
        return self.call('agent/action', request=request)

    def receive_result(self, action_id):
        return self.call('agent/result', action_id=action_id)

    def answer_question(self, question_id, response_id, offer_ids, refused, disclosure='full'):
        return self.call('questions/respond', question_id=question_id, response_id=response_id,
                         offer_ids=offer_ids, refused=refused, disclosure=disclosure)

    def action_request(self, observation, action, payload=None, task_id=None, action_id=None):
        """Build a request from a recorded observation, not from newer hidden state."""
        import uuid
        request = {k: observation[k] for k in ('game_id', 'cycle_id', 'phase', 'phase_epoch')}
        request.update(agent_id=observation['self']['id'], action_id=action_id or str(uuid.uuid4()),
                       action=action, payload=dict(payload or {}))
        if task_id is not None:
            task = next((t for t in observation['tasks'] if t['task_id'] == task_id), None)
            if task is None:
                raise ValueError('task is not assigned in this observation')
            request.update(task_id=task_id, generation=task['generation'])
        return request


def reasoning_inputs(observation, task_id=None, legacy=False):
    """Project official observations for B.sync/decide, without inventing facts.

    Default matches B d06c650: singular candidate keys and dictionary profile
    tags. legacy=True supports the older plural pools/kind/excludes projection.
    Partial clues stay redacted. No belief or policy implementation is patched.
    """
    import copy
    obs = copy.deepcopy(observation)
    if legacy:
        obs['candidates'] = {'suspects': obs['candidates']['suspect'],
                             'weapons': obs['candidates']['weapon'], 'places': obs['candidates']['place']}
        for collection in ('information', 'shared_information'):
            for item in obs[collection]:
                item['kind'] = 'location' if item['category'] == 'place' else item['category']
                if 'excluded_id' in item:
                    item['excludes'] = item['excluded_id']
    else:
        for field in ('personality', 'personal_goal'):
            value = obs['self'][field]
            if isinstance(value, str):
                obs['self'][field] = {'id': value, 'name': value}
    notice = {k: copy.deepcopy(obs[k]) for k in
              ('game_id', 'cycle_id', 'phase', 'phase_epoch', 'self', 'occupants',
               'positions', 'shared_information', 'allowed_actions', 'remaining_actions')}
    notice['agent_id'] = obs['self']['id']
    notice['task_id'] = task_id
    return notice, obs
