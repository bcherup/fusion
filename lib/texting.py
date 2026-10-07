"""Scoped local SIP MESSAGE routing; carrier SMS remains a separate integration."""
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from .common import need

ROUTE = 'pbxctl-internal-chat'
FIELD = '${sip_profile}|${from_user}|${to}'


def expression(domain, profile, extensions):
    need(extensions, 'Choose at least two local extensions for chat')
    numbers = sorted(set(extensions))
    need(len(numbers) >= 2, 'Choose at least two local extensions for chat')
    group = '(?:' + '|'.join(numbers) + ')'
    return '^' + re.escape(profile) + r'\|' + group + r'\|' + group + '@' + re.escape(domain) + '$'


def equivalent_expression(actual, domain, profile, extensions):
    if actual == expression(domain,profile,extensions):return True
    numbers=sorted(set(extensions))
    width=len(numbers[0]);prefix=numbers[0][:-1]
    if not all(len(x)==width and x[:-1]==prefix for x in numbers):return False
    digits=[int(x[-1]) for x in numbers]
    if digits!=list(range(digits[0],digits[-1]+1)):return False
    compact=re.escape(prefix)+'['+str(digits[0])+'-'+str(digits[-1])+']'
    return actual=='^'+re.escape(profile)+r'\|'+compact+r'\|'+compact+'@'+re.escape(domain)+'$'


def inspect_chatplan(path):
    path = Path(path)
    need(path.is_file() and not path.is_symlink(), 'Chatplan file missing or linked: ' + str(path))
    need(path.stat().st_size <= 1024 * 1024, 'Chatplan file unexpectedly large')
    tree = ET.parse(path, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    contexts = [x for x in tree.getroot().iter('context') if x.get('name') == 'public']
    need(len(contexts) == 1, 'Expected exactly one public chatplan context')
    context = contexts[0]
    owned = [x for x in context.findall('extension') if x.get('name') == ROUTE]
    need(len(owned) <= 1, 'Duplicate toolkit chat routes')
    other = [x for x in context.findall('extension') if x.get('name') != ROUTE and
             any(a.get('application') == 'send' and a.get('data') == 'sip' for a in x.iter('action'))]
    return tree, context, owned[0] if owned else None, other


def route_expression(extension):
    if extension is None:return None
    conditions = list(extension)
    if len(conditions) != 1 or conditions[0].tag != 'condition' or conditions[0].get('field') != FIELD:return None
    actions = list(extension.iter('action'))
    if len(actions) != 1 or list(conditions[0]) != actions or actions[0].get('application') != 'send' or actions[0].get('data') != 'sip':return None
    return conditions[0].get('expression')


def change_chatplan(path, domain, profile, settings, change):
    tree, context, owned, other = inspect_chatplan(path)
    enabled = settings['enabled']
    desired = expression(domain, profile, settings['extensions']) if enabled else None
    if not enabled:need(not other, 'An existing custom chat route is still active; review it before disabling chat')
    if enabled:
        # A second send route can bypass the extension allowlist. Preserve it for review.
        need(not other or len(other) == 1 and equivalent_expression(route_expression(other[0]),domain,profile,settings['extensions']),
             'Another SIP chat route exists. Review it before enabling toolkit routing.')
        if other:
            need(owned is None, 'A toolkit route and another SIP route both exist; review before changing')
            context.remove(other[0])  # Adopt an exactly equivalent legacy route; recorded backup permits rollback.
    if owned is not None:
        need(route_expression(owned) is not None, 'Toolkit chat route was edited; review it before changing')
        if enabled and route_expression(owned) == desired:return False
        context.remove(owned)
    if enabled:
        extension = ET.Element('extension', {'name': ROUTE})
        condition = ET.SubElement(extension, 'condition', {'field': FIELD, 'expression': desired})
        ET.SubElement(condition, 'action', {'application': 'send', 'data': 'sip'})
        context.insert(0, extension)
    elif owned is None:return False
    st=Path(path).stat()
    change.file(path, ET.tostring(tree.getroot(), encoding='unicode') + '\n',
                st.st_mode & 0o777, (st.st_uid,st.st_gid))
    return True
