"""Domain tests enter through the real router before exercising a tool.

The fake provider's capability_ready boundary is simulated here; shared host
transition sequencing is exercised separately in test_theme_preferences.py.
"""
import base64
from types import SimpleNamespace
from projects_hub.live_capabilities import BUNDLES
from projects_hub.store import StoreError


def activate(adapter, session, capability):
    if not session.state.get('_current_utterance'):
        adapter.input(session, {'activity_start': True})
        adapter.input(session, {'audio_base64': base64.b64encode(b'\x01\x00' * 320).decode()})
        adapter.input(session, {'activity_end': True})
    spec = adapter.resolve_capability(session, {
        'name': 'activate_capability', 'args': {'capability': capability, 'intent': 'Fixture accepted domain request'},
    })
    if spec is None:
        raise StoreError('TOOL_NOT_AVAILABLE', 'Router rejected capability')
    names = {f['name'] for f in spec['configuration']['functions']}
    if capability == 'core':
        from projects_hub.live_adapter import _startup_functions
        owner = 'owner_development' in adapter._allowed_capabilities(session)
        assert names == {
            f['name'] for f in _startup_functions(owner_development=owner)
        }
    else:
        assert names == {'activate_capability', *BUNDLES[capability]}
    assert len(names) <= (9 if capability == 'core' else 6)
    session.capability = spec['capability']
    return spec


def bundle_setup(adapter, initialized, capability):
    session = SimpleNamespace(state=initialized['state'], capability='core')
    spec = activate(adapter, session, capability)
    return spec['configuration'], session


async def execute(adapter, session, call):
    capability = next((key for key, names in BUNDLES.items() if call['name'] in names), None)
    if capability and capability in adapter._allowed_capabilities(session) and getattr(session, 'capability', 'core') != capability:
        activate(adapter, session, capability)
    return await adapter.execute_tool(session, call)
