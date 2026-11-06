"""
What the EspoCRM MCP's tools do (server.py makes them MCP tools). Plain functions, no MCP
SDK: they take the Instance (EspoCRM as admin, the bridge's Kobo account) and return
dicts. A tool that can't do what it was asked raises Refused with a message for the agent.

Task-shaped, not raw access: no tool reads or writes arbitrary records. Every change
(create_connection, update_connection, create_entity, grant_access, pause_connection)
takes two calls, the second with the `confirm` token of the first (confirm.py).
"""
import difflib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import BridgeError, Espo, Kobo, parse_template, route_teams  # noqa: E402
import connections  # noqa: E402
import propose as proposer  # noqa: E402
import setup as setup_py  # noqa: E402
from espo_mcp.confirm import Refused, check, digest, mint  # noqa: E402

UID = re.compile(r'^[A-Za-z0-9_-]{6,64}$')
UID_IN_URL = re.compile(r'(?:#/forms/|/api/v2/assets/)([A-Za-z0-9_-]{6,64})(?:[/?#.]|$)')
ENTITY_NAME = re.compile(r'^[A-Z][A-Za-z0-9]{2,40}$')
# Never targets of a connection or of grant_access: the bridge's and the agents' own setup
OFF_LIMITS = {'CKoboConnection', 'CAgent', 'CAgentRoute', 'CAgentRun', 'User', 'Role', 'Team', 'ApiUser'}
BUILT_IN = ['Case', 'Contact', 'Account', 'Lead', 'Task']
STATUS = ['status', 'lastPass', 'lastSuccess', 'waiting', 'oldestWaiting', 'lastError', 'lastCounts']


def admin_espo(env):
    password = env.get('ESPO_ADMIN_PASSWORD') or setup_py.read_secrets(
        Path(env.get('BRIDGE_SECRETS', '/run/bridge.secrets'))).get('ESPO_ADMIN_PASSWORD', '')
    return Espo(env['ESPO_URL'], basic=f'admin:{password}')


class Instance:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.espo = admin_espo(env)
        self.site_url = (env.get('ESPO_SITE_URL') or env['ESPO_URL']).rstrip('/')
        self.kobo_url = env['KOBO_URL'].rstrip('/')
        self.kobo = Kobo(self.kobo_url, Path(env['KOBO_TOKEN_FILE']).read_text().strip())
        self.bridge_name = env.get('BRIDGE_NAME', 'main')
        self.language = env.get('ESPO_LANGUAGE', 'en_US')
        self._kobo_user = None

    def kobo_user(self):
        if self._kobo_user is None:
            self._kobo_user = (self.kobo.call('GET', '/me/') or {}).get('username') or '(unknown)'
        return self._kobo_user

    def link(self, entity, record_id):
        return f'{self.site_url}/#{entity}/view/{record_id}'


def failed(e):
    """An EspoCRM or Kobo error, for the agent."""
    return Refused(f'{e}. Nothing was changed.')


# --- Reading -------------------------------------------------------------------------------

def connection_rows(inst):
    rows = inst.espo.call('GET', f'/{connections.ENTITY}', {
        'where[0][type]': 'equals', 'where[0][attribute]': 'runBy', 'where[0][value]': inst.bridge_name,
        'select': connections.SELECT, 'maxSize': 200, 'orderBy': 'name'})['list']
    return rows


def summary(inst, rec):
    return {'id': rec['id'], 'name': rec['name'], 'active': rec.get('active'),
            'from_file': rec.get('templateFile') or None, 'form': rec.get('koboForm'),
            'makes': rec.get('recordEntity'), 'teams': rec.get('routeTeams'), 'notifies': rec.get('notifyUsers'),
            **{k: rec.get(k) for k in STATUS}, 'link': inst.link(connections.ENTITY, rec['id'])}


def bridge_info(inst):
    """The instance, the bridge's Kobo server and account (who to share forms with), and
    its connections with their status."""
    return {'espocrm': inst.site_url, 'kobo_server': inst.kobo_url, 'kobo_account': inst.kobo_user(),
            'share_forms_with': inst.kobo_user(),
            'connections': [summary(inst, r) for r in connection_rows(inst)]}


def list_teams(inst):
    rows = inst.espo.call('GET', '/Team', {'maxSize': 200, 'orderBy': 'name', 'select': 'name'})['list']
    return {'teams': [r['name'] for r in rows]}


def list_users(inst):
    """People who can be notified (regular and admin users, active), with their teams."""
    rows = inst.espo.call('GET', '/User', {'maxSize': 200, 'orderBy': 'userName',
                                           'select': 'userName,name,type,isActive,teamsNames'})['list']
    return {'users': [{'user': r['userName'], 'name': r.get('name'), 'admin': r.get('type') == 'admin',
                       'teams': sorted((r.get('teamsNames') or {}).values())}
                      for r in rows if r.get('isActive') and r.get('type') in ('regular', 'admin')]}


def entity_names(inst):
    scopes = inst.espo.call('GET', '/Metadata', {'key': 'scopes'}) or {}
    labels = ((inst.espo.call('GET', '/I18n') or {}).get('Global') or {}).get('scopeNames') or {}
    found = {}
    for name, s in scopes.items():
        if name in OFF_LIMITS or not s.get('entity'):
            continue
        if name in BUILT_IN or (s.get('isCustom') and s.get('object', True)):
            found[name] = {'entity': name, 'label': labels.get(name, name), 'custom': bool(s.get('isCustom'))}
    return found


def list_entities(inst):
    """The kinds of record a connection can make: EspoCRM's own (Case, Contact...) and custom ones."""
    return {'entities': sorted(entity_names(inst).values(), key=lambda e: (e['custom'], e['entity']))}


def find_connection(inst, connection):
    rows = connection_rows(inst)
    rec = next((r for r in rows if r['id'] == connection), None) or next(
        (r for r in rows if r['name'].casefold() == str(connection).strip().casefold()), None)
    if rec is None:
        raise Refused(f'No connection {connection!r}: bridge_info lists them, with their ids.')
    return rec


def connection_status(inst, connection):
    """A connection's last pass, what it made, the submissions waiting and the oldest one's reason."""
    rec = find_connection(inst, connection)
    return {**summary(inst, rec), 'template': rec.get('template')}


# --- Proposing -----------------------------------------------------------------------------

def form_uid(form):
    form = str(form).strip()
    found = UID_IN_URL.search(form)
    uid = found.group(1) if found else form
    if not UID.match(uid):
        raise Refused(f'{form[:80]!r} is not a Kobo form uid or link (e.g. aXy3... or https://kf.../#/forms/aXy3.../).')
    return uid


def kobo_form(inst, uid):
    """The form as the bridge's Kobo account sees it: (asset, deployed content)."""
    try:
        asset = inst.kobo.call('GET', f'/api/v2/assets/{uid}/')
    except BridgeError as e:
        if e.status in (403, 404):
            raise Refused(
                f"The bridge's Kobo account ({inst.kobo_user()} on {inst.kobo_url}) can't see the form {uid}. "
                f'With kobo-mcp, share it: share_project {uid} with {inst.kobo_user()}, permission "View '
                'submissions" (and "Edit form" too if the connection keeps a list on the form). Then try again.'
            ) from None
        raise failed(e) from None
    version = asset.get('deployed_version_id')
    if not version:
        raise Refused(f'The form {uid} ("{asset.get("name")}") isn\'t deployed yet: deploy it first (kobo-mcp deploy_form).')
    content = inst.kobo.form(uid, version)
    return asset, {**content, 'settings': {**(content.get('settings') or {}), 'form_title': asset['name']}}


def latest_submission(inst, uid):
    rows = inst.kobo.call('GET', f'/api/v2/assets/{uid}/data/', {'sort': json.dumps({'_id': -1}), 'limit': 1})
    return (rows.get('results') or [None])[0]


def check_entity(inst, entity):
    names = entity_names(inst)
    if entity not in names:
        raise Refused(f'No entity {entity!r} a connection can make. list_entities lists them; create_entity makes '
                      'a new one.')


def check_notify(inst, notify):
    users = {u['user'] for u in list_users(inst)['users']}
    unknown = [u for u in notify or [] if u not in users]
    if unknown:
        raise Refused(f'No active user {", ".join(unknown)} to notify: list_users lists them.')


def existing_fields(inst, entity):
    defs = inst.espo.call('GET', '/Metadata', {'key': f'entityDefs.{entity}.fields'}) or {}
    return {name: d.get('type') for name, d in defs.items()}


def propose_connection(inst, form, entity, team=None, notify=None):
    """A template for a Kobo form, what each question becomes, what was left out, and a
    preview of the form's latest submission. Writes nothing."""
    uid = form_uid(form)
    check_entity(inst, entity)
    check_notify(inst, notify)
    asset, content = kobo_form(inst, uid)
    got = proposer.propose(content, entity, team=team, notify=notify or (), existing=existing_fields(inst, entity),
                           language=inst.language)
    warnings = list(got['warnings'])
    teams = set(list_teams(inst)['teams'])
    if team and team not in teams:
        warnings.append(f'team "{team}" doesn\'t exist yet: the bridge will make it. Give its people the team.')
    others = [r['name'] for r in connection_rows(inst) if r.get('koboForm') == asset['name']]
    if others:
        warnings.append(f'the form is already connected ({", ".join(others)}): a second connection to the '
                        'same form and entity would find the same records, not make new ones')
    sub = latest_submission(inst, uid)
    preview = (proposer.preview(parse_template(got['template']), sub, content, inst.kobo_url, uid) if sub else None)
    return {'form': {'uid': uid, 'name': asset['name']}, 'entity': entity, 'template': got['template'],
            'questions': got['fields'], 'left_out': [{'question': q, 'why': w} for q, w in got['left_out']],
            'warnings': warnings,
            'preview': preview or 'No submission yet: make a test one (kobo-mcp create_submissions), then '
                                  'test_connection once the connection is saved.'}


def preview_template(inst, template, form=None):
    """What the form's latest submission would become with this template (as edited). Writes nothing."""
    t = parsed(template)
    uids = [form_uid(form)] if form else list(inst.kobo.projects(t['form']['name']))
    if not uids:
        raise Refused(f'No deployed form named "{t["form"]["name"]}" that the bridge\'s Kobo account '
                      f'({inst.kobo_user()}) can see.')
    _, content = kobo_form(inst, uids[0])
    sub = latest_submission(inst, uids[0])
    if not sub:
        return {'preview': 'No submission yet.'}
    return {'preview': proposer.preview(t, sub, content, inst.kobo_url, uids[0])}


# --- Changing (two calls each) ------------------------------------------------------------

def parsed(template):
    try:
        t = parse_template(template)
    except ValueError as e:
        raise Refused(f'The template is not valid: {e}') from None
    if not (t.get('form') or {}).get('name'):
        raise Refused('The template has no [form] name: the Kobo form it reads.')
    return t


def described(t):
    return {'form': t['form']['name'], 'makes': t['entity'], 'teams': route_teams(t),
            'notifies': sorted({u for r in t['route'] for u in r.get('notify', [])}),
            'fields': len([f for f in t['field'] if f.get('espo')]), 'own_api_user': not (t.get('bridge') or {}).get('user')}


def gate(facts, confirm, would):
    """None when `confirm` approves exactly these facts; else the first call's answer."""
    if confirm:
        check(confirm, facts)
        return None
    return {**would, 'changed': False, 'confirm_required': True, 'confirm': mint(facts),
            'next': 'Show the user this, and call again with `confirm` only once they have agreed.'}


def create_connection(inst, name, template, confirm=None):
    """Saves a new connection (after a first call that shows it). The bridge sets it up in its next pass."""
    name = str(name).strip()
    if not name or len(name) > 100:
        raise Refused('A connection needs a name (up to 100 characters).')
    t = parsed(template)
    check_entity(inst, t['entity'])
    check_notify(inst, sorted({u for r in t['route'] for u in r.get('notify', [])}))
    if any(r['name'].casefold() == name.casefold() for r in connection_rows(inst)):
        raise Refused(f'There is a connection called "{name}" already: choose another name, or update_connection.')
    facts = {'action': 'create_connection', 'name': name, 'template': digest(template)}
    first = gate(facts, confirm, {'would_create': {'name': name, **described(t)}})
    if first:
        return first
    try:
        rec = inst.espo.call('POST', f'/{connections.ENTITY}', body={
            'name': name, 'template': template, 'active': True, 'runBy': inst.bridge_name})
    except BridgeError as e:
        raise failed(e) from None
    return {'changed': True, 'created': summary(inst, rec),
            'next': 'The bridge sets it up within a pass (about 30 s). Then make a test submission and call '
                    'test_connection.'}


def update_connection(inst, connection, template=None, name=None, confirm=None):
    """Changes a connection's template or name (after a first call that shows the change)."""
    rec = find_connection(inst, connection)
    if rec.get('templateFile'):
        raise Refused(f'"{rec["name"]}" comes from the template file {rec["templateFile"]}: change the file, not the record.')
    body = {}
    if template is not None and template != rec.get('template'):
        t = parsed(template)
        check_entity(inst, t['entity'])
        body['template'] = template
    if name is not None and name.strip() and name.strip() != rec['name']:
        body['name'] = name.strip()
    if not body:
        return {'changed': False, 'note': 'Nothing to change.'}
    diff = list(difflib.unified_diff((rec.get('template') or '').splitlines(), (template or '').splitlines(),
                                     'before', 'after', lineterm='', n=1)) if 'template' in body else []
    facts = {'action': 'update_connection', 'id': rec['id'], 'was': digest(rec.get('template')),
             'body': digest(body)}
    first = gate(facts, confirm, {'would_change': rec['name'], 'name': body.get('name'),
                                  'template_diff': '\n'.join(diff[:200]) or None})
    if first:
        return first
    try:
        inst.espo.call('PUT', f'/{connections.ENTITY}/{rec["id"]}', body=body)
    except BridgeError as e:
        raise failed(e) from None
    return {'changed': True, 'connection': rec['name'] if 'name' not in body else body['name'],
            'next': 'The bridge sets it up again in its next pass; test_connection shows the result.'}


def pause_connection(inst, connection, confirm=None):
    """Stops a connection polling (its ledger and records stay). There is no delete."""
    rec = find_connection(inst, connection)
    if not rec.get('active'):
        return {'changed': False, 'note': f'"{rec["name"]}" is paused already.'}
    facts = {'action': 'pause_connection', 'id': rec['id']}
    first = gate(facts, confirm, {'would_pause': rec['name'],
                                  'note': 'New submissions wait in Kobo; resume_connection brings them in.'})
    if first:
        return first
    inst.espo.call('PUT', f'/{connections.ENTITY}/{rec["id"]}', body={'active': False})
    return {'changed': True, 'paused': rec['name']}


def resume_connection(inst, connection):
    """Starts a paused connection again: what was submitted meanwhile comes in at its next pass."""
    rec = find_connection(inst, connection)
    if rec.get('active'):
        return {'changed': False, 'note': f'"{rec["name"]}" is running.'}
    inst.espo.call('PUT', f'/{connections.ENTITY}/{rec["id"]}', body={'active': True})
    return {'changed': True, 'resumed': rec['name']}


def create_entity(inst, name, label_singular, label_plural, confirm=None):
    """A new kind of record (e.g. Application), for a connection to make. EspoCRM calls it C<name>."""
    name = str(name).strip()
    if not ENTITY_NAME.match(name):
        raise Refused('The name: a capital letter, then letters or digits, 3 to 41 of them (e.g. "Application").')
    entity = 'C' + name
    scopes = inst.espo.call('GET', '/Metadata', {'key': 'scopes'}) or {}
    if entity in scopes or name in scopes:
        raise Refused(f'There is an entity {entity if entity in scopes else name} already: list_entities.')
    facts = {'action': 'create_entity', 'name': name, 'labels': [label_singular, label_plural]}
    first = gate(facts, confirm, {'would_create': entity, 'label': label_singular, 'plural': label_plural,
                                  'note': 'For the whole instance. Nobody but admins can see its records until '
                                          'grant_access gives a role access.'})
    if first:
        return first
    try:
        inst.espo.call('POST', '/EntityManager/action/createEntity', body={
            'name': name, 'type': 'Base', 'labelSingular': label_singular, 'labelPlural': label_plural,
            'stream': True})  # notes on its records: the bridge's mentions, and follow-ups
    except BridgeError as e:
        raise failed(e) from None
    return {'changed': True, 'entity': entity, 'next': 'Propose a connection that makes it, then grant_access.'}


def grant_access(inst, entity, role, edit=True, create=False, confirm=None):
    """Lets a role's users see (and edit, or create) the records of an entity in their own
    teams. Team level only: never all records."""
    check_entity(inst, entity)
    found = inst.espo.find('Role', 'name', role)
    if not found:
        names = [r['name'] for r in inst.espo.call('GET', '/Role', {'maxSize': 200, 'select': 'name'})['list']]
        raise Refused(f'No role {role!r}. The roles: {", ".join(sorted(names))}.')
    have = inst.espo.call('GET', f'/Role/{found["id"]}')
    data = dict(have.get('data') or {})
    want = {'create': 'yes' if create else 'no', 'read': 'team', 'edit': 'team' if edit else 'no',
            'delete': 'no', 'stream': 'team'}
    if data.get(entity) == want:
        return {'changed': False, 'note': f'{role} has that access to {entity} already.'}
    facts = {'action': 'grant_access', 'role': found['id'], 'entity': entity, 'want': want,
             'was': digest(data.get(entity))}
    first = gate(facts, confirm, {'role': role, 'entity': entity, 'now': data.get(entity) or 'no access',
                                  'would_be': want,
                                  'note': "Its users see the records of their own teams only. Give people the "
                                          "connection's team in EspoCRM (Administration, Users)."})
    if first:
        return first
    inst.espo.call('PUT', f'/Role/{found["id"]}', body={'data': {**data, entity: want}})
    return {'changed': True, 'role': role, 'entity': entity, 'access': want}


# --- Testing -------------------------------------------------------------------------------

def test_connection(inst, connection, wait_seconds=90):
    """Waits for the bridge's next pass of this connection (at most wait_seconds), then
    reports the newest record it made, or why submissions are waiting."""
    rec = find_connection(inst, connection)
    if not rec.get('active'):
        raise Refused(f'"{rec["name"]}" is paused: resume_connection first.')
    before, deadline = rec.get('lastPass') or '', time.time() + min(max(int(wait_seconds), 10), 240)
    while time.time() < deadline:
        time.sleep(5)
        rec = find_connection(inst, connection)
        if (rec.get('lastPass') or '') > before:
            break
    else:
        return {**summary(inst, rec), 'note': 'No pass in that time: is the bridge service running? '
                                              '(deploy/bridges.sh logs)'}
    out = summary(inst, rec)
    try:
        t = parse_template(rec.get('template') or '')
        uids = list(inst.kobo.projects(t['form']['name']))
    except (ValueError, KeyError, BridgeError):
        return out
    if uids:
        params = {'where[0][type]': 'in', 'where[0][attribute]': 'cKoboAsset',
                  **{f'where[0][value][{i}]': uid for i, uid in enumerate(uids)},
                  'orderBy': 'createdAt', 'order': 'desc', 'maxSize': 1, 'select': 'id,name,createdAt'}
        try:
            rows = inst.espo.call('GET', f'/{t["entity"]}', params)['list']
        except BridgeError:
            rows = []
        out['newest_record'] = ({'name': rows[0].get('name'), 'made': rows[0].get('createdAt'),
                                 'link': inst.link(t['entity'], rows[0]['id'])} if rows else None)
    if rec.get('waiting'):
        out['note'] = (f'{rec["waiting"]} submission(s) waiting; the oldest because: {rec.get("lastError")}. '
                       'They are retried by themselves, and at once after the template or the form changes.')
    return out
