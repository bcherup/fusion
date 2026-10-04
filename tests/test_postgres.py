"""Disposable PostgreSQL integration fixture; opt in only inside test environments."""
import os
import json
from pathlib import Path
import sys
import tempfile
import uuid
from unittest.mock import patch, Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lib.common import Database,run
from lib.config import load_config
from lib.recovery import scratch_restore
from lib import common, features, operations, pbx

def feature_lifecycle(db, temporary):
    domain='11111111-1111-4111-8111-111111111111'
    profile='22222222-2222-4222-8222-222222222222'
    db.execute('''ALTER TABLE v_domains ADD COLUMN domain_uuid uuid;
        UPDATE v_domains SET domain_uuid='''+common.literal(domain)+''';
        CREATE TABLE v_sip_profiles(sip_profile_uuid uuid PRIMARY KEY,sip_profile_name text);
        CREATE TABLE v_sip_profile_settings(sip_profile_setting_uuid uuid PRIMARY KEY,
          sip_profile_uuid uuid,sip_profile_setting_name text,sip_profile_setting_value text,sip_profile_setting_enabled boolean);
        CREATE TABLE v_dialplans(dialplan_uuid uuid PRIMARY KEY,domain_uuid uuid,dialplan_context text,
          dialplan_name text,dialplan_continue boolean,dialplan_destination boolean,dialplan_order integer,
          dialplan_enabled boolean,dialplan_description text,dialplan_xml text);
        CREATE TABLE v_dialplan_details(dialplan_detail_uuid uuid PRIMARY KEY,domain_uuid uuid,
          dialplan_uuid uuid REFERENCES v_dialplans(dialplan_uuid),dialplan_detail_tag text,dialplan_detail_type text,
          dialplan_detail_data text,dialplan_detail_group integer,dialplan_detail_order integer,dialplan_detail_enabled boolean);
        INSERT INTO v_sip_profiles VALUES ('''+common.literal(profile)+''','internal');
        INSERT INTO v_sip_profile_settings VALUES ('''+common.literal(str(uuid.uuid4()))+','+common.literal(profile)+''','tls','true',true);
        INSERT INTO v_dialplans(dialplan_uuid,dialplan_name,dialplan_xml,dialplan_enabled)
          VALUES ('33333333-3333-4333-8333-333333333333','Native route','unchanged',true);
    ''')
    c=load_config(Path(__file__).resolve().parents[1]/'site.example.json')
    native=db.rows("SELECT * FROM v_dialplans WHERE dialplan_name='Native route'")
    with patch.object(common,'BACKUPS',Path(temporary)/'changes'):
        for module, key in [('secure_calling','secure_calling'),('call_volume','call_volume')]:
            c[key]['enabled']=True
            first=common.Change(db,module);getattr(features,module)(db,c,first)
            enabled=db.rows('SELECT * FROM v_dialplans ORDER BY dialplan_uuid')
            enabled_details=db.rows('SELECT * FROM v_dialplan_details ORDER BY dialplan_detail_uuid')
            again=common.Change(db,module);getattr(features,module)(db,c,again)
            assert db.rows('SELECT * FROM v_dialplans ORDER BY dialplan_uuid')==enabled
            assert db.rows('SELECT * FROM v_dialplan_details ORDER BY dialplan_detail_uuid')==enabled_details
            c[key]['enabled']=False
            off=common.Change(db,module);getattr(features,module)(db,c,off)
            assert not any(r['dialplan_enabled'] for r in db.rows("SELECT * FROM v_dialplans WHERE dialplan_description LIKE 'Managed by PBX Toolkit:%'"))
            off.rollback()
            assert db.rows('SELECT * FROM v_dialplans ORDER BY dialplan_uuid')==enabled
            again.rollback();first.rollback()
            assert db.rows("SELECT * FROM v_dialplans WHERE dialplan_name='Native route'")==native
            assert not db.rows('SELECT * FROM v_dialplan_details')
        # A disable operation may leave files untouched, but still owns their integrity checks.
        file=Path(temporary)/'managed-unit';first=common.Change(db,'fixture');first.file(file,'unit contents')
        operations.save_managed(first,None)
        previous={'backup':str(first.path)}
        off=common.Change(db,'fixture-disable');operations.save_managed(off,previous)
        marker=Path(temporary)/'marker.json';common.atomic(marker,json.dumps({'backup':str(off.path)}))
        operations.check_managed(marker,db)
        file.write_text('external edit')
        try:operations.check_managed(marker,db)
        except common.Error:pass
        else:raise AssertionError('Disable lost file ownership checks')
        # A failed activation must undo actual SQL writes and restore the active configuration.
        c['database']=db.name;c['secure_calling']['enabled']=True
        active=Path(temporary)/'active.json';common.atomic(active,json.dumps(c))
        original=active.read_bytes()
        with patch.object(operations.base,'supported'), patch.object(operations,'CONFIG',active), \
             patch.object(operations,'STATE',Path(temporary)/'state'), \
             patch.object(operations,'ROOT',Path(__file__).resolve().parents[1]), \
             patch.object(operations,'run',return_value=Mock(returncode=0,stdout='')), \
             patch.object(operations,'invalidate',side_effect=common.Error('Synthetic activation failure')):
            try:operations.configure(c,['secure-calling'])
            except common.Error:pass
            else:raise AssertionError('Activation failure was ignored')
        assert json.loads(active.read_bytes())==json.loads(original)
        assert db.rows('SELECT * FROM v_dialplans')==native
        assert not (Path(temporary)/'state/secure-calling.json').exists()
    print('PostgreSQL feature enable/reapply/disable/rollback and drift checks: passed')

def nat_lifecycle(db,temporary):
    c=load_config(Path(__file__).resolve().parents[1]/'site.example.json')
    c['provider_cidrs']=['192.0.2.10/32']
    internal=pbx.profile(db,c['internal_profile']);external=str(uuid.uuid4());acl=str(uuid.uuid4())
    db.execute('''CREATE TABLE v_access_controls(access_control_uuid uuid PRIMARY KEY,access_control_name text,access_control_default text);
        CREATE TABLE v_access_control_nodes(access_control_node_uuid uuid PRIMARY KEY,access_control_uuid uuid,node_type text,node_cidr text,node_domain text);
        INSERT INTO v_access_controls VALUES ('''+common.literal(acl)+','+common.literal(c['provider_acl'])+''','deny');
        INSERT INTO v_sip_profiles VALUES ('''+common.literal(external)+','+common.literal(c['external_profile'])+''');
        INSERT INTO v_sip_profile_settings VALUES ('''+common.literal(str(uuid.uuid4()))+','+common.literal(external)+''','apply-inbound-acl','''+common.literal(c['provider_acl'])+''',true);
    ''')
    for existing in (False,True):
        setting_id=str(uuid.uuid4())
        if existing:
            db.execute('INSERT INTO v_sip_profile_settings VALUES ('+common.literal(setting_id)+','+common.literal(internal)+",'aggressive-nat-detection','true',false)")
        before=db.rows('SELECT * FROM v_sip_profile_settings ORDER BY sip_profile_setting_uuid')
        with patch.object(common,'BACKUPS',Path(temporary)/'nat-changes'),patch.object(pbx,'run'),patch.object(Path,'is_file',return_value=True):
            first=common.Change(db,'nat');first.file=Mock();pbx.hardening(db,c,first)
            enabled=db.rows('SELECT * FROM v_sip_profile_settings ORDER BY sip_profile_setting_uuid')
            nat=[r for r in enabled if r['sip_profile_setting_name']=='aggressive-nat-detection']
            assert len(nat)==1 and nat[0]['sip_profile_uuid']==internal
            assert nat[0]['sip_profile_setting_value']=='true' and nat[0]['sip_profile_setting_enabled'] is True
            if existing:assert nat[0]['sip_profile_setting_uuid']==setting_id
            assert [r for r in enabled if r['sip_profile_uuid']==external]==[r for r in before if r['sip_profile_uuid']==external]
            again=common.Change(db,'nat-repeat');again.file=Mock();pbx.hardening(db,c,again)
            assert db.rows('SELECT * FROM v_sip_profile_settings ORDER BY sip_profile_setting_uuid')==enabled
            again.rollback();first.rollback()
            assert db.rows('SELECT * FROM v_sip_profile_settings ORDER BY sip_profile_setting_uuid')==before
    print('PostgreSQL NAT defaults: fresh/disabled setting, reapply, carrier isolation and rollback passed')

def diagnostic_inventory(db,temporary):
    from lib import diagnostics
    from test_diagnostics import fixture_probe
    db.execute('''CREATE TABLE v_extensions(domain_uuid uuid,extension text,enabled boolean,hold_music text);
        CREATE TABLE v_ring_groups(domain_uuid uuid,ring_group_extension text,ring_group_name text,ring_group_ringback text);
        CREATE TABLE v_gateways(gateway_uuid uuid,gateway text,profile text,enabled boolean,register_transport text);
        CREATE TABLE v_domain_settings(domain_uuid uuid,domain_setting_category text,domain_setting_subcategory text,domain_setting_value text,domain_setting_enabled boolean);
        CREATE TABLE v_default_settings(default_setting_category text,default_setting_subcategory text,default_setting_value text,default_setting_enabled boolean);
        CREATE TABLE v_voicemails(domain_uuid uuid,voicemail_id text,voicemail_transcription_enabled boolean,voicemail_mail_to text);
        CREATE SEQUENCE readonly_probe;
        INSERT INTO v_default_settings VALUES ('email','smtp_host','smtp.example.com',true);
    ''')
    readonly=diagnostics.ReadDatabase(db.name)
    try:readonly.rows("SELECT nextval('readonly_probe')")
    except common.Error:pass
    else:raise AssertionError('Read-only transaction permitted a sequence write')
    with patch.object(diagnostics,'STATE',Path(temporary)/'no-state'):
        scanner=diagnostics.Scanner(db=readonly,probe=fixture_probe)
        for method in ('discover','profiles','phones','dialplan','gateways','voicemail'):
            getattr(scanner,method)()
        assert not any(f['severity']=='unknown' for f in scanner.r['findings'])
        assert any(row['observed']=='smtp.example.com' for s in scanner.r['sections'] for row in s['rows'])
    print('PostgreSQL inventory projections and enforced read-only transactions: passed')

def quick_phone_lifecycle(db,temporary):
    from lib import quick_volume as quick
    domain=db.one('SELECT domain_uuid FROM v_domains')['domain_uuid']
    db.execute('INSERT INTO v_extensions VALUES ('+common.literal(domain)+",'1000',true,NULL),("+common.literal(domain)+",'1001',true,NULL); INSERT INTO v_ring_groups VALUES ("+common.literal(domain)+",'600','Office',NULL);")
    directory=Path(temporary)/'quick';directory.mkdir()
    state=directory/'state';active=directory/'site.json';helper=directory/'lib/call-volume.lua'
    c=load_config(Path(__file__).resolve().parents[1]/'site.example.json');c['database']=db.name
    common.atomic(active,json.dumps(c));original=active.read_bytes()
    native=db.rows('SELECT * FROM v_dialplans ORDER BY dialplan_uuid')
    with patch.object(quick,'CONFIG',active),patch.object(quick,'STATE',state),patch.object(common,'BACKUPS',directory/'backups'), \
         patch.object(quick,'HELPER',helper),patch.object(quick,'invalidate') as refresh:
        def volume():return quick.Volume(database=db.name,db=db)
        first=volume();plan=first.plan('call-volume',write=-1)
        first.apply(plan,plan['token'])
        assert helper.read_bytes()==(quick.SOURCE/'assets/call-volume.lua').read_bytes()
        assert volume().current('call-volume')['write_level']==-1
        after=json.loads(active.read_bytes());expected={**c,'call_volume':plan['config']['call_volume']}
        assert after==expected
        rules=db.rows("SELECT dialplan_name,dialplan_xml FROM v_dialplans WHERE dialplan_description LIKE 'Managed by PBX Toolkit:%'")
        assert len(rules)==2 and any(str(helper) in r['dialplan_xml'] for r in rules)
        assert any('1000|1001|600' in r['dialplan_xml'] for r in rules)
        for kwargs in ({'write':-2},{'restore':True}):
            v=volume();p=v.plan('call-volume',**kwargs);v.apply(p,p['token'])
        assert volume().current('call-volume')['write_level']==0
        assert not db.rows("SELECT * FROM v_dialplans WHERE dialplan_enabled=true AND dialplan_description LIKE 'Managed by PBX Toolkit:%'")
        # Activation failure must restore previous real rows, helper, state and preferences.
        before_rows=db.rows('SELECT * FROM v_dialplans ORDER BY dialplan_uuid');before_site=active.read_bytes()
        marker=(state/'call-volume.json').read_bytes()
        refresh.side_effect=[common.Error('Synthetic activation failure'),None]
        v=volume();p=v.plan('call-volume',read=-1)
        try:v.apply(p,p['token'])
        except common.Error:pass
        else:raise AssertionError('Quick volume activation failure was ignored')
        assert db.rows('SELECT * FROM v_dialplans ORDER BY dialplan_uuid')==before_rows
        assert active.read_bytes()==before_site and (state/'call-volume.json').read_bytes()==marker
        assert [r for r in db.rows('SELECT * FROM v_dialplans') if r['dialplan_name']=='Native route']==native
        # Refuse a manual gain edit rather than reporting stale levels or overwriting it.
        db.execute("UPDATE v_dialplan_details SET dialplan_detail_data='read 4' WHERE dialplan_detail_type='set_audio_level' AND dialplan_detail_data LIKE 'read %'")
        try:volume().current('call-volume')
        except common.Error:pass
        else:raise AssertionError('Quick volume ignored changed dialplan details')
    print('PostgreSQL direct phone volume: discovery, enable, adjust, disable, rollback, helper and drift checks passed')


if os.environ.get('PBXCTL_INTEGRATION')!='1':raise SystemExit('Set PBXCTL_INTEGRATION=1 only in an isolated test environment')
name='pbxctl_test_'+uuid.uuid4().hex[:12];created=False
try:
    run(['runuser','-u','postgres','--','createdb',name]);created=True
    db=Database(name);db.execute("CREATE TABLE v_domains(domain_name text); INSERT INTO v_domains VALUES ('voip.example.com');")
    with tempfile.TemporaryDirectory() as temp:
        db.dump(Path(temp)/'database.dump')
        scratch_restore(temp,load_config(Path(__file__).resolve().parents[1]/'site.example.json'))
        feature_lifecycle(db,temp)
        nat_lifecycle(db,temp)
        diagnostic_inventory(db,temp)
        quick_phone_lifecycle(db,temp)
    print('PostgreSQL custom dump and isolated scratch restore: passed')
finally:
    if created:run(['runuser','-u','postgres','--','dropdb','--force',name])
