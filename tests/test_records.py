"""
Unit tests for records other than cases, requesters, matching, one team, mentions,
escalation and lists, with Kobo and EspoCRM faked in memory:

    PYTHONPATH=.:tests python3 -m unittest discover -s tests
"""
import csv
import io
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path

import bridge

SUPPORT = """
[form]
name = "Support"

[record]
name = "{category}: {summary}"
details = [["", "description"]]
required = ["email", "summary"]
single_team = true
set = { cSource = "Kobo form" }
attachments = true

[requester]
email = "email"
name = "full_name"
organisation = "org"

[escalation]
after_hours = 1

[[route]]
match = { project = "a" }
team = "Team A"
priority = "Normal"
notify = ["triager"]
escalate = ["lead-a"]

[[route]]
match = { urgent = "yes" }
team = "Urgent desk"
priority = "Urgent"
notify = ["duty"]

[[field]]
kobo = "category"
espo = "cCategory"
label = "Category"
"""

VOLUNTEER = """
[form]
name = "Volunteers"

[record]
entity = "Contact"
match = { emailAddress = "email" }
set = { cProfileType = "Volunteer" }

[[route]]
team = "Pool"

[[field]]
kobo = "full_name"
espo = "name"
type = "personName"

[[field]]
kobo = "email"
espo = "emailAddress"

[[field]]
kobo = "skills"
espo = "cSkills"
type = "multiEnum"
label = "Skills"
options = { wash = "WASH", data = "Data" }
"""

LISTS = """
[[list]]
file = "sites.csv"
entity = "CSite"
team = "Response 1"
where = { status = ["operational", "needs_repair"] }
columns = { name = "siteCode", label = "{siteCode} · {name}", status = "status", teams = "teamsNames" }
forms = ["Site visit"]
"""

FORM = {'survey': [{'type': 'select_one category', 'name': 'category', 'label': 'Category'},
                   {'type': 'select_one org', 'name': 'org', 'label': 'Organisation'}],
        'choices': [{'list_name': 'category', 'name': 'forms', 'label': 'Forms and XLSForm'},
                    {'list_name': 'org', 'name': 'alpha', 'label': 'Org Alpha'}]}
TEAMS = {'Team A': 't-a', 'Urgent desk': 't-u', 'Pool': 't-pool', 'Response 1': 't-r1'}


def template(text, tmp):
    path = Path(tmp) / f'{uuid.uuid4().hex}.toml'
    path.write_text(textwrap.dedent(text))
    return bridge.load_template(path)


def sub(n, **answers):
    return {'_id': n, '_uuid': str(uuid.uuid4()), '_submission_time': '2026-11-05T09:00:00', **answers}


class Espo:
    """Records per entity, finds by any attribute, notes; enough for handle() and friends."""

    def __init__(self):
        self.rows, self.notes, self.posts = {}, [], []

    def find(self, entity, attr, value, select=None):
        if entity == 'Team':
            return {'id': TEAMS[value]} if value in TEAMS else None
        return next((r for r in self.rows.get(entity, []) if str(r.get(attr, '')).lower() == str(value).lower()), None)

    def call(self, method, path, params=None, body=None, headers=None):
        parts = path.strip('/').split('/')
        entity = parts[0]
        if method == 'GET' and entity == 'Metadata':
            return {f: {} for f in ('cKoboAsset', 'cKoboSubmissionId', 'cKoboSubmissionUuid', 'cKoboLink',
                                    'cFollowUpDue', 'cCategory', 'cSkills', 'emailAddress', 'name')}
        if method == 'POST' and entity == 'Note':
            self.notes.append(body)
            return {}
        if method == 'POST':
            self.posts.append((entity, body, headers))
            row = {'id': f'{entity}-{len(self.rows.get(entity, []))}', **body,
                   'createdAt': '2026-11-05 09:00:00'}
            self.rows.setdefault(entity, []).append(row)
            return row
        if method == 'PUT':
            row = next(r for r in self.rows[entity] if r['id'] == parts[1])
            row.update(body)
            return row
        if method == 'GET' and len(parts) == 1:  # a list: escalation or a [[list]]
            rows = self.rows.get(entity, [])
            if params.get('where[0][type]') == 'isNull':
                rows = [r for r in rows if not r.get('assignedUserId') and r.get('cKoboAsset')]
            if params.get('where[0][type]') == 'in':
                wanted = [v for k, v in params.items() if k.startswith('where[0][value]')]
                rows = [r for r in rows if r.get(params['where[0][attribute]']) in wanted]
            if params.get('where[1][type]') == 'linkedWith':
                rows = [r for r in rows if params['where[1][value][0]'] in r.get('teamsIds', [])]
            off = params.get('offset', 0)
            return {'list': rows[off:off + params.get('maxSize', 200)], 'total': len(rows)}
        raise AssertionError((method, path))


class Records(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.support = bridge.with_form(template(SUPPORT, self.tmp.name), bridge.form_choices(FORM))
        self.counts = {k: 0 for k in ('created', 'skipped', 'existing', 'merged', 'notified')}

    def tearDown(self):
        self.tmp.cleanup()

    def handle(self, t, s, espo):
        bridge.handle({**t, 'missing': []}, s, 'http://kf', 'aX', TEAMS, espo, self.counts)

    def test_one_team_the_highest_priority_route(self):
        body = bridge.to_record(self.support, sub(1, project='a', urgent='yes', email='x@y.z', summary='s'),
                                'http://kf', 'aX', TEAMS)
        self.assertEqual((body['teamsIds'], body['priority']), (['t-u'], 'Urgent'))
        both = {**self.support, 'record': {**self.support['record'], 'single_team': False}}
        body = bridge.to_record(both, sub(1, project='a', urgent='yes', email='x@y.z', summary='s'), 'http://kf', 'aX', TEAMS)
        self.assertEqual(body['teamsIds'], ['t-a', 't-u'])

    def test_name_constants_attachment_links(self):
        s = sub(1, project='a', email='x@y.z', summary='Deploy fails', category='forms', description='It says...',
                _attachments=[{'media_file_basename': 'shot.png', 'download_url': 'http://kf/media/1'}])
        body = bridge.to_record(self.support, s, 'http://kf', 'aX', TEAMS)
        self.assertEqual(body['name'], 'Forms and XLSForm: Deploy fails')
        self.assertEqual(body['cSource'], 'Kobo form')
        self.assertEqual(body['description'], 'It says...\nAttachments (in Kobo):\n- shot.png: http://kf/media/1')

    def test_required_answer_missing_is_refused_before_anything_is_written(self):
        espo = Espo()
        with self.assertRaises(bridge.BridgeError) as e:
            self.handle(self.support, sub(1, project='a', email='x@y.z', summary=''), espo)
        self.assertTrue(e.exception.rejected)
        self.assertIn('required answer missing: summary', str(e.exception))
        self.assertEqual(espo.rows, {})

    def test_requester_contact_and_account_made_once_with_the_records_teams(self):
        espo = Espo()
        self.handle(self.support, sub(1, project='a', email='Amina@Example.org', full_name='Amina J. Hassan',
                                      org='alpha', summary='one'), espo)
        self.handle(self.support, sub(2, project='a', email='amina@example.org', full_name='Amina Hassan',
                                      org='alpha', summary='two'), espo)
        self.assertEqual(len(espo.rows['Contact']), 1)
        self.assertEqual(len(espo.rows['Account']), 1)
        contact, account = espo.rows['Contact'][0], espo.rows['Account'][0]
        self.assertEqual((contact['firstName'], contact['lastName'], contact['emailAddress']),
                         ('Amina J.', 'Hassan', 'amina@example.org'))
        self.assertEqual(account['name'], 'Org Alpha')  # the choice's label, not its name
        self.assertEqual((contact['accountId'], contact['teamsIds']), (account['id'], ['t-a']))
        self.assertTrue(all(c['contactId'] == contact['id'] and c['accountId'] == account['id'] for c in espo.rows['Case']))
        # EspoCRM's own duplicate check is by name: skipped, the email was looked up
        self.assertEqual(next(h for e, _, h in espo.posts if e == 'Contact'), {'X-Skip-Duplicate-Check': 'true'})

    def test_an_existing_contact_in_another_team_gets_this_team_too(self):
        espo = Espo()
        espo.rows['Contact'] = [{'id': 'c1', 'emailAddress': 'a@b.c', 'teamsIds': ['t-other']}]
        self.handle(self.support, sub(1, project='a', email='a@b.c', summary='s'), espo)
        self.assertEqual(espo.rows['Contact'][0]['teamsIds'], ['t-a', 't-other'])

    def test_new_record_mentions_its_routes_notify_users(self):
        espo = Espo()
        self.handle(self.support, sub(1, project='a', urgent='yes', email='a@b.c', summary='s'), espo)
        # one team: only the chosen route's people
        self.assertEqual([n['post'] for n in espo.notes], ['New from Kobo, please look at it: @duty'])
        self.assertEqual(self.counts['notified'], 1)

    def test_contact_matched_by_email_is_updated_not_duplicated(self):
        t = bridge.with_form(template(VOLUNTEER, self.tmp.name), {})
        espo = Espo()
        self.handle(t, sub(1, full_name='Vera Volunteer', email='vera@example.org', skills='wash'), espo)
        self.handle(t, sub(2, full_name='Vera Volunteer', email='VERA@example.org', skills='wash data'), espo)
        self.assertEqual(len(espo.rows['Contact']), 1)
        vera = espo.rows['Contact'][0]
        self.assertEqual((vera['firstName'], vera['lastName'], vera['cSkills'], vera['cProfileType']),
                         ('Vera', 'Volunteer', ['wash', 'data'], 'Volunteer'))
        self.assertEqual((self.counts['created'], self.counts['merged']), (1, 1))
        self.assertIn('matched on emailAddress', espo.notes[-1]['post'])
        self.assertNotIn('description', vera)  # not a case: no description, status or priority
        self.assertNotIn('priority', vera)


class Escalation(unittest.TestCase):
    def test_unassigned_after_the_wait_mentions_the_lead_once(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        t = template(SUPPORT, d.name)
        ledger = bridge.Ledger(Path(d.name) / 'bridge.sqlite')
        espo = Espo()
        espo.rows['Case'] = [{'id': 'k1', 'teamsIds': ['t-a'], 'cKoboAsset': 'aX', 'status': 'New'},
                             {'id': 'k2', 'teamsIds': ['t-a'], 'cKoboAsset': 'aX', 'assignedUserId': 'u1'},
                             {'id': 'k3', 'teamsIds': ['t-a'], 'cKoboAsset': 'aX', 'status': 'Closed'}]
        self.assertEqual(bridge.escalate(t, espo, ledger, now=10_000), 1)
        self.assertEqual(espo.notes[0]['parentId'], 'k1')
        self.assertTrue(espo.notes[0]['post'].endswith('@lead-a'))
        self.assertEqual(bridge.escalate(t, espo, ledger, now=20_000), 0)


class Lists(unittest.TestCase):
    def test_csv_has_the_listed_columns_only_filtered_by_team_and_where(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        t = template(LISTS, d.name)
        self.assertNotIn('form', t)
        espo = Espo()
        espo.rows['CSite'] = [
            {'id': 's1', 'siteCode': 'EQ1-1', 'name': 'Well, north', 'status': 'operational', 'teamsIds': ['t-r1'],
             'teamsNames': {'t-r1': 'Response 1'}, 'equipment': 'secret serial'},
            {'id': 's2', 'siteCode': 'EQ2-1', 'name': 'Other', 'status': 'operational', 'teamsIds': ['t-r2']},
            {'id': 's3', 'siteCode': 'EQ1-2', 'name': 'Old', 'status': 'decommissioned', 'teamsIds': ['t-r1']}]
        data = bridge.list_csv(t['list'][0], bridge.list_rows(t['list'][0], espo, TEAMS)).decode()
        rows = list(csv.DictReader(io.StringIO(data)))
        self.assertEqual(rows, [{'name': 'EQ1-1', 'label': 'EQ1-1 · Well, north', 'status': 'operational',
                                 'teams': 'Response 1'}])
        self.assertNotIn('secret', data)

    def test_uploaded_when_changed_only(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        ledger = bridge.Ledger(Path(d.name) / 'bridge.sqlite')

        class Kobo:
            uploads = []

            def projects(self, name):
                return {'aVisit': 'v1'}

            def replace_media(self, uid, filename, data, description=None):
                self.uploads.append((uid, filename, data))
        t = template(LISTS, d.name)
        espo, kobo = Espo(), Kobo()
        espo.rows['CSite'] = [{'id': 's1', 'siteCode': 'A', 'name': 'a', 'status': 'operational', 'teamsIds': ['t-r1']}]
        self.assertEqual(bridge.sync_lists(t, kobo, espo, ledger, 1000, 0, TEAMS), 1)
        self.assertEqual(bridge.sync_lists(t, kobo, espo, ledger, 1001, 0, TEAMS), 0)
        espo.rows['CSite'][0]['status'] = 'needs_repair'
        self.assertEqual(bridge.sync_lists(t, kobo, espo, ledger, 1002, 0, TEAMS), 1)
        self.assertEqual([u[1] for u in kobo.uploads], ['sites.csv', 'sites.csv'])


if __name__ == '__main__':
    unittest.main()


class Media(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        text = SUPPORT.replace('attachments = true', 'media = "attachments"\nmedia_max_mb = 0.001')
        self.t = bridge.with_form(template(text, self.tmp.name), bridge.form_choices(FORM))

    def kobo(self):
        k = bridge.Kobo('https://kf.example.org', 'tok')
        k.got = []

        def download(url, max_bytes, k=k):
            k.got.append(url)
            data = b'x' * (2000 if 'big' in url else 10)
            return None if len(data) > max_bytes else (data, 'image/jpeg')
        k.download = download
        return k

    def att(self, name, deleted=False):
        return {'media_file_basename': name, 'filename': f'u/attachments/{name}', 'mimetype': 'image/jpeg',
                'is_deleted': deleted, 'download_url': f'https://kf.example.org/api/v2/assets/aX/data/1/attachments/{name}/'}

    def test_files_named_by_answers_are_copied_onto_the_record(self):
        espo, kobo, counts = Espo(), self.kobo(), {k: 0 for k in ('created', 'existing', 'merged', 'notified', 'skipped')}
        s = sub(1, project='a', email='a@b.c', summary='s', photo='a.jpg',
                visits=[{'visits/photo': 'r.jpg'}], big_photo='big.jpg',
                _attachments=[self.att('a.jpg'), self.att('r.jpg'), self.att('big.jpg'),
                              self.att('gone.jpg', deleted=True), self.att('old.jpg')])
        bridge.handle({**self.t, 'missing': []}, s, 'http://kf', 'aX', TEAMS, espo, counts, kobo=kobo)
        uploads = [b for e, b, _ in espo.posts if e == 'Attachment']
        self.assertEqual([u['name'] for u in uploads], ['a.jpg', 'r.jpg'])  # in a repeat group too
        self.assertTrue(all(u['relatedType'] == 'Case' and u['field'] == 'attachments'
                            and u['file'].startswith('data:image/jpeg;base64,') for u in uploads))
        case = espo.rows['Case'][0]
        self.assertEqual(case['attachmentsIds'], ['Attachment-0', 'Attachment-1'])
        self.assertIn('Too big to copy (in Kobo): big.jpg', case['description'])
        self.assertEqual(len(kobo.got), 3)  # not the deleted one, not the one no answer names

    def test_only_from_this_kobo(self):
        k = bridge.Kobo('https://kf.example.org', 'tok')
        with self.assertRaises(bridge.BridgeError) as e:
            k.download('https://evil.example.com/x.jpg', 100)
        self.assertTrue(e.exception.rejected)
