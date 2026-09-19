"""Generic TLS SMTP transport for tests and database-independent health alerts."""
from email.message import EmailMessage
import smtplib
import ssl
from .common import need
from .config import email, read_secret
from . import pbx

def validate_ready(s):
    need(s['host'],'SMTP hostname required');email(s['from_address']);email(s['recipient'])
    need(not s['auth'] or s['username'],'SMTP username required')

def send(c,subject,body):
    s=c['smtp'];validate_ready(s)
    message=EmailMessage();message['Subject']=subject
    from email.utils import formataddr
    message['From']=formataddr((s['from_name'],s['from_address']));message['To']=s['recipient'];message.set_content(body)
    context=ssl.create_default_context()
    cls=smtplib.SMTP_SSL if s['security']=='tls' else smtplib.SMTP
    options={'timeout':20}
    if s['security']=='tls':options['context']=context
    with cls(s['host'],s['port'],**options) as smtp:
        smtp.ehlo()
        if s['security']=='starttls':smtp.starttls(context=context);smtp.ehlo()
        if s['auth']:smtp.login(s['username'],read_secret(s['password_file']))
        need(not smtp.send_message(message),'SMTP rejected a recipient')

def configure(db,c,ch):
    s=c['smtp'];validate_ready(s);d=pbx.domain(db,c)
    password=read_secret(s['password_file']) if s['auth'] else ''
    values={'smtp_host':s['host'],'smtp_port':s['port'],
       'smtp_secure':{'starttls':'tls','tls':'ssl','none':'none'}[s['security']],
       'smtp_auth':'true' if s['auth'] else 'false','smtp_username':s['username'],
       'smtp_password':password,'smtp_from':s['from_address'],'smtp_from_name':s['from_name'],
       'smtp_validate_certificate':'true'}
    for k,v in values.items():pbx.domain_setting(db,ch,d,'email',k,v,'numeric' if k=='smtp_port' else 'text')
    pbx.require_columns(db,'v_voicemails',['voicemail_uuid','domain_uuid','voicemail_id','voicemail_mail_to'])
    row=db.one('SELECT voicemail_uuid FROM v_voicemails WHERE domain_uuid='+pbx.literal(d)+' AND voicemail_id='+pbx.literal(c['mailbox']))
    ch.row('v_voicemails','voicemail_uuid',{**row,'voicemail_mail_to':s['recipient']})
