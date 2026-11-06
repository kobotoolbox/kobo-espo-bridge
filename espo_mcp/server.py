"""
The EspoCRM MCP: connecting Kobo forms to EspoCRM from a conversation, next to kobo-mcp
(which does the Kobo side: building, deploying and sharing forms, test submissions).

    python3 -I espo_mcp/server.py           Streamable HTTP on MCP_HOST:MCP_PORT, at /mcp

Needs the MCP SDK (espo_mcp/requirements.txt); the rest of the bridge stays stdlib only.
Settings (env): those of tools.Instance (ESPO_URL, ESPO_SITE_URL, the admin password,
KOBO_URL, KOBO_TOKEN_FILE, BRIDGE_NAME), MCP_KEYS_FILE (keys.py), MCP_PUBLIC_URL (where
clients reach it, e.g. https://espo.example.org/bridge), MCP_HOST (0.0.0.0), MCP_PORT (8000).

Only EspoCRM admins connect, each with their own key: `Authorization: Bearer <key>`
(keys.py add <user>). Every call checks the key's user is still an active admin.
"""
import functools
import logging
import os
import sys
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import anyio
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from espo_mcp import keys, tools  # noqa: E402
from espo_mcp.confirm import Refused  # noqa: E402

log = logging.getLogger('espo_mcp')

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
CHANGE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
WAIT = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

INSTRUCTIONS = """\
Tools for connecting KoboToolbox forms to this EspoCRM, through kobo-espo-bridge: each
submission of a connected form becomes a record (a case, a contact, a site, or a new kind
of record), in a team, with people notified. Use them with kobo-mcp, which builds,
deploys and shares the Kobo forms and makes test submissions.

Start with bridge_info: it names the Kobo server and the bridge's Kobo account, which the
form must be shared with (kobo-mcp share_project, "View submissions"). The prompt
"Connect a Kobo form to EspoCRM" has the steps. propose_connection writes nothing: show
the user its questions table, what was left out and why, its warnings, and the preview,
and change the template as they ask (preview_template shows the result). Every change
(create_connection, update_connection, create_entity, grant_access, pause_connection)
takes two calls: the first shows what would happen and returns a `confirm` token; show
the user exactly that, and call again with the token only once they have agreed. Never
pass a token on the strength of an earlier, vaguer instruction. There is no delete: pause
a connection instead. Never ask for passwords or tokens in the chat."""

CONNECT = """\
Help me connect a Kobo form to EspoCRM{about}.

1. bridge_info (EspoCRM MCP): which Kobo server the bridge reads, and its Kobo account.
2. The form: find it with kobo-mcp (list_projects), or build it with me (preview_form,
   then deploy_form once I'm happy). It has to be deployed.
3. Ask me, one at a time, unless I said already: what each submission should become in
   EspoCRM (list_entities; create_entity for a new kind of record, e.g. "Application"),
   which team owns the records (list_teams), and who's told about each new one
   (list_users).
4. Share the form with the bridge's Kobo account: kobo-mcp share_project, "View
   submissions" (and "Edit form" if the connection keeps a list on the form). Ask me first.
5. propose_connection. Show me what each question becomes as a short table, what was left
   out and why, the warnings, and the preview of the latest submission. Change the
   template as I ask, preview_template again, then create_connection (show me what it
   would create, then confirm).
6. A test: kobo-mcp create_submissions with one dummy entry, then test_connection, and
   show me the record it made (its link).
7. grant_access for the roles of the people who'll work on these records (team level),
   and tell me what's left by hand: giving people the team in EspoCRM."""


def run_tool(fn):
    """The tool in a worker thread (they wait on EspoCRM and Kobo; test_connection on a
    pass), its Refused as the tool's error (a ToolError: the SDK shows the agent its
    message, where any other exception is only "Error executing tool")."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
        except Refused as e:
            raise ToolError(str(e)) from None
        except tools.BridgeError as e:
            raise ToolError(f'{e}. Nothing was changed.') from None
    return wrapper


class Keys:
    """The SDK's token verifier: the bearer is an admin's MCP key (keys.py)."""

    def __init__(self, espo, resource):
        self.admins, self.resource = keys.Admins(espo), resource

    async def verify_token(self, token):
        user = await anyio.to_thread.run_sync(self.admins.who, token)
        if not user:
            return None
        log.info('call by %s', user)
        return AccessToken(token=f'key:{user}', client_id='espo-admin', scopes=[], resource=self.resource, subject=user)


Connection = Annotated[str, Field(description='The connection: its id, or its name as bridge_info lists it.')]
Confirm = Annotated[str | None, Field(
    description="The `confirm` token from this tool's own previous call, passed back once the user has seen what "
                'it would do and agreed. Leave it out on the first call: that call changes nothing.')]
Entity = Annotated[str, Field(description='What each submission becomes: an entity from list_entities, e.g. Case, '
                                          'Contact, CSite, or one made with create_entity (C<name>).')]
Template = Annotated[str, Field(description="The connection's template (TOML), as propose_connection gives it, "
                                            'with the changes the user asked for.')]


def build_server(inst=None, public_url=None):
    inst = inst or tools.Instance()
    public_url = (public_url or os.environ.get('MCP_PUBLIC_URL') or inst.site_url + '/bridge').rstrip('/')
    server = MCPServer(
        name='espocrm-kobo', instructions=INSTRUCTIONS,
        token_verifier=Keys(inst.espo, public_url + '/mcp'),
        auth=AuthSettings(issuer_url=public_url, resource_server_url=public_url + '/mcp',
                          validate_token_resource=True))

    def tool(annotations):
        def register(fn):
            server.tool(annotations=annotations)(run_tool(fn))
            return fn
        return register

    @tool(READ)
    def bridge_info() -> dict:
        """This EspoCRM, the Kobo server the bridge reads and the bridge's Kobo account (share forms with it),
        and the connections with their status."""
        return tools.bridge_info(inst)

    @tool(READ)
    def list_teams() -> dict:
        """The teams records can go to."""
        return tools.list_teams(inst)

    @tool(READ)
    def list_users() -> dict:
        """The people who can be notified, with their teams."""
        return tools.list_users(inst)

    @tool(READ)
    def list_entities() -> dict:
        """The kinds of record a connection can make: EspoCRM's own (Case, Contact, ...) and custom ones."""
        return tools.list_entities(inst)

    @tool(READ)
    def propose_connection(
            form: Annotated[str, Field(description='The Kobo form: its uid, or a link to it.')],
            entity: Entity,
            team: Annotated[str | None, Field(description='The team that owns the records (list_teams).')] = None,
            notify: Annotated[list[str] | None, Field(description='Users told of each new record (list_users).')] = None,
    ) -> dict:
        """A template for the form: what each question becomes, what was left out and why, warnings, and a preview
        of the form's latest submission. Writes nothing."""
        return tools.propose_connection(inst, form, entity, team, notify)

    @tool(READ)
    def preview_template(template: Template,
                         form: Annotated[str | None, Field(description='The form uid, if several have its name.')] = None
                         ) -> dict:
        """What the form's latest submission would become with this template. Writes nothing."""
        return tools.preview_template(inst, template, form)

    @tool(CHANGE)
    def create_connection(name: Annotated[str, Field(description='A name for people, e.g. "Grant applications".')],
                          template: Template, confirm: Confirm = None) -> dict:
        """Saves a new connection; the bridge sets it up within a pass. Two calls: see `confirm`."""
        return tools.create_connection(inst, name, template, confirm)

    @tool(CHANGE)
    def update_connection(connection: Connection, template: Template | None = None,
                          name: Annotated[str | None, Field(description='A new name.')] = None,
                          confirm: Confirm = None) -> dict:
        """Changes a connection's template or name (the first call shows the change). Not for template files."""
        return tools.update_connection(inst, connection, template, name, confirm)

    @tool(CHANGE)
    def pause_connection(connection: Connection, confirm: Confirm = None) -> dict:
        """Stops a connection polling; its records and ledger stay. There is no delete."""
        return tools.pause_connection(inst, connection, confirm)

    @tool(CHANGE)
    def resume_connection(connection: Connection) -> dict:
        """Starts a paused connection again."""
        return tools.resume_connection(inst, connection)

    @tool(CHANGE)
    def create_entity(name: Annotated[str, Field(description='Its name: a capital letter, then letters or digits, '
                                                             'e.g. "Application". EspoCRM calls it C<name>.')],
                      label_singular: Annotated[str, Field(description='e.g. "Application"')],
                      label_plural: Annotated[str, Field(description='e.g. "Applications"')],
                      confirm: Confirm = None) -> dict:
        """A new kind of record for a connection to make. For the whole instance. Two calls."""
        return tools.create_entity(inst, name, label_singular, label_plural, confirm)

    @tool(CHANGE)
    def grant_access(entity: Entity,
                     role: Annotated[str, Field(description="An EspoCRM role, e.g. 'Internal Staff'.")],
                     edit: Annotated[bool, Field(description='May they edit the records (of their teams)?')] = True,
                     create: Annotated[bool, Field(description='May they create records by hand?')] = False,
                     confirm: Confirm = None) -> dict:
        """Lets a role's users see (and edit) the records of their own teams, never all. Two calls."""
        return tools.grant_access(inst, entity, role, edit, create, confirm)

    @tool(WAIT)
    def test_connection(connection: Connection,
                        wait_seconds: Annotated[int, Field(ge=10, le=240, description='How long to wait for a pass.')]
                        = 90) -> dict:
        """Waits for the bridge's next pass of the connection, then shows the newest record it made (a link), or
        why submissions are waiting. Make a test submission first (kobo-mcp create_submissions)."""
        return tools.test_connection(inst, connection, wait_seconds)

    @tool(READ)
    def connection_status(connection: Connection) -> dict:
        """A connection's status, last pass, waiting submissions and the oldest one's reason, and its template."""
        return tools.connection_status(inst, connection)

    def connect_a_form(form: Annotated[str | None, Field(description='The form, if you know it.')] = None) -> str:
        """Connect a Kobo form to EspoCRM: the steps across kobo-mcp and this server."""
        return CONNECT.format(about=f' ({form})' if form else '')

    server.prompt(name='connect_a_form', title='Connect a Kobo form to EspoCRM')(connect_a_form)
    return server, public_url


def transport_security(public_url):
    """Host and Origin checks (DNS rebinding): only the public URL's."""
    public = urlsplit(public_url)
    return TransportSecuritySettings(allowed_hosts=[public.netloc, '127.0.0.1:*', 'localhost:*'],
                                     allowed_origins=[f'{public.scheme}://{public.netloc}'])


def http_app(inst=None, public_url=None):
    server, public_url = build_server(inst, public_url)
    return server.streamable_http_app(stateless_http=True, host=os.environ.get('MCP_HOST', '0.0.0.0'),
                                      transport_security=transport_security(public_url))


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    uvicorn.run(http_app(), host=os.environ.get('MCP_HOST', '0.0.0.0'), port=int(os.environ.get('MCP_PORT', '8000')),
                access_log=False)


if __name__ == '__main__':
    main()
