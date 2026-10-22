"""Private reply rules, native MCP write records, and explicit timed restart intents."""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path
import re
import uuid
import hal_daily_check as d


def store(path):
    if not path.exists():
        return {}
    try:
        value=json.loads(path.read_text())
        if not isinstance(value,dict):
            raise ValueError('not an object')
        return value
    except (OSError,ValueError) as exc:
        raise ValueError('reply records could not be read') from exc


def locked(home):
    directory=Path(home)/'.reply-actions-lock'
    directory.mkdir(mode=0o700,exist_ok=True)
    return d.lock(directory,wait=d.MAIL_SECONDS)


def reply_text(text):
    lines=[]
    for line in text.splitlines():
        if (line.lstrip().startswith('>') or re.match(r'^On .+wrote:\s*$',line,re.I)
                or re.match(r'^(?:From:|[- ]*Original Message[- ]*|Begin forwarded message:)',line,re.I)):
            break
        lines.append(line)
    return '\n'.join(lines).strip()


def require_reply(reply):
    if not all(isinstance(reply.get(k),str) and reply[k].strip() for k in ('message_id','reply_to','sender','instruction')):
        raise ValueError('an explicit operator email reply is required')


def remember_reply(home,reply):
    require_reply(reply)
    with locked(home):
        path=Path(home)/'daily-check-actions.json';saved=store(path)
        saved.setdefault('replies',{})[reply['message_id']]=reply
        d.atomic_json(path,saved)


def active_rules(home,at,expire=False):
    path=Path(home)/'daily-check-rules.json'
    def read():
        saved=store(path);rows=saved.get('rules',{});active={}
        for key,row in rows.items():
            until=row.get('until')
            if not until or at.astimezone(d.LONDON).date()<=dt.date.fromisoformat(until):
                active[key]=row
        if expire and active!=rows:
            saved['rules']=active;d.atomic_json(path,saved)
        return list(active.values())
    if expire:
        with locked(home):
            return read()
    return read()


def rules(home,args,reply,at=None):
    at=at or d.now();require_reply(reply)
    operation=args.get('operation')
    if not set(args)<={'operation','kind','target','name','until'} or operation not in ('add','remove','list'):
        raise ValueError('use add, remove or list')
    if operation=='list':
        return dict(rules=active_rules(home,at))
    kind=args.get('kind');target=args.get('target');until=args.get('until')
    if kind not in ('device','error','update') or not isinstance(target,str) or not target.strip():
        raise ValueError('provide the device ID, integration domain or update entity ID')
    if until:
        dt.date.fromisoformat(until)
    if kind=='error' and not re.fullmatch(r'[a-z0-9_]+',target):
        raise ValueError('use the integration domain')
    if kind=='update' and not re.fullmatch(r'update\.[a-z0-9_]+',target):
        raise ValueError('use the update entity ID')
    key=d.digest([kind,target])
    with locked(home):
        path=Path(home)/'daily-check-rules.json';saved=store(path);rows=saved.setdefault('rules',{})
        request=d.digest([reply['message_id'],operation,key,until])
        if request not in saved.setdefault('requests',[]):
            if operation=='add':
                rows[key]=dict(kind=kind,target=target,name=d.label(args.get('name'),target),until=until or None)
            else:
                rows.pop(key,None)
            saved['requests'].append(request);d.atomic_json(path,saved)
    return dict(rules=active_rules(home,at))


def result_value(value):
    while isinstance(value,str):
        try:
            value=json.loads(value)
        except ValueError:
            return {'message':value}
    if isinstance(value,dict) and set(value)=={'result'}:
        return result_value(value['result'])
    return value if isinstance(value,dict) else {'message':str(value)}


def record_write(home,call_id,tool,args,result,reply,at=None):
    at=at or d.now()
    with locked(home):
        path=Path(home)/'daily-check-actions.json';saved=store(path);rows=saved.setdefault('actions',{})
        key=call_id or uuid.uuid4().hex
        if key not in rows:
            rows[key]=dict(at=at.isoformat(),message_id=reply.get('message_id'),instruction=reply.get('instruction'),
                           sender=reply.get('sender'),tool=tool,arguments=args,result=result_value(result))
            d.atomic_json(path,saved)


def add_action_lines(report,home,cutoff,snapshot,at):
    states={s['entity_id']:s for s in snapshot.get('states',[])}
    entries={e['entry_id']:e for e in snapshot.get('entries',[])}
    saved=store(Path(home)/'daily-check-actions.json')
    for row in saved.get('actions',{}).values():
        when=d.stamp(row['at'])
        if not cutoff<when<=at:
            continue
        args=row['arguments'];result=row['result'];tool=row['tool']
        service=args.get('service') or ('restart' if tool=='ha_restart' else args.get('action'))
        target=args.get('entity_id') or args.get('entity_ids') or (args.get('data') or {}).get('entry_id') or args.get('entry_id')
        targets=target if isinstance(target,list) else ([target] if target else [])
        names=[]
        for key in targets:
            state=states.get(key,{})
            name=(state.get('attributes') or {}).get('title') or (state.get('attributes') or {}).get('friendly_name') or entries.get(key,{}).get('title') or key
            names.append(re.sub(r'(?:\s+(?:update|firmware))+$','',name,flags=re.I))
        name=', '.join(names) or 'Home Assistant'
        clock=when.astimezone(d.LONDON).strftime('%H:%M')
        error=result.get('error') or ((result.get('message') or 'Home Assistant refused the requested action') if result.get('success') is False else None)
        if not row.get('message_id'):
            error='a Home Assistant write ran without a recorded operator reply; check the actions log'
        if error:
            report.setdefault('items',[]).append(f'{name}: the requested action failed at {clock}: {d.label(error)}.')
            continue
        if tool=='hal_reply_rules':
            verb='recorded' if args['operation']=='add' else 'removed'
            line=f'{d.label(args.get("name"),args["target"])}: {verb} the {args["kind"]} rule at {clock} as you asked.'
        elif service=='install':
            current=[states.get(key,{}) for key in targets]
            loaded=bool(current) and all(s.get('state')=='off' and s.get('attributes',{}).get('installed_version') for s in current)
            versions=', '.join(str(s['attributes']['installed_version']) for s in current) if loaded else ''
            line=f'{name}: installed {versions} at {clock} as you asked; it is installed now.' if loaded else f'{name}: requested installation at {clock} as you asked; completion is not yet confirmed.'
        elif service in ('reload','reload_config_entry'):
            line=f'{name}: requested a reload at {clock} as you asked.'
        elif service=='restart':
            line=f'Home Assistant: requested a restart at {clock} as you asked.'
        else:
            line=f'{name}: {d.label(result.get("message"),"the requested action was accepted")} at {clock}, as you asked.'
        report.setdefault('done',[]).append(line)
    for intent in saved.get('scheduled',{}).values():
        when=d.stamp(intent.get('started_at') or intent['run_at'])
        if intent['status'] in ('started','scheduling','schedule failed') and cutoff<when<=at:
            report.setdefault('items',[]).append('Home Assistant: the requested restart has no confirmed result; check it before trying again.')


def schedule_restart(home,run_at,reply,schedule,at=None):
    at=at or d.now();require_reply(reply);when=d.stamp(run_at)
    if when<=at:
        raise ValueError('provide a future restart time with its timezone')
    key=d.digest([reply['message_id'],'restart',when.isoformat()])
    with locked(home):
        path=Path(home)/'daily-check-actions.json';saved=store(path);intents=saved.setdefault('scheduled',{})
        if key in intents:
            return intents[key]
        intent=dict(reply=reply,run_at=when.isoformat(),status='scheduling');intents[key]=intent;d.atomic_json(path,saved)
        try:
            job=schedule(when.isoformat())
        except Exception:
            intent['status']='schedule failed';d.atomic_json(path,saved);raise
        intent.update(status='scheduled',job_id=job['id']);d.atomic_json(path,saved)
        return intent


def run_due(home,call,at=None):
    at=at or d.now()
    with locked(home):
        path=Path(home)/'daily-check-actions.json';saved=store(path)
        for key,intent in saved.get('scheduled',{}).items():
            if intent['status']!='scheduled' or d.stamp(intent['run_at'])>at:
                continue
            require_reply(intent['reply'])
            intent.update(status='started',started_at=at.isoformat());d.atomic_json(path,saved)
            args={'domain':'homeassistant','service':'restart'}
            try:
                result=result_value(call('ha_call_service',args))
            except Exception:
                result={'error':'The restart result is unknown; check Home Assistant before trying again.'}
            saved.setdefault('actions',{})['scheduled:'+key]=dict(at=at.isoformat(),message_id=intent['reply']['message_id'],instruction=intent['reply']['instruction'],sender=intent['reply']['sender'],tool='ha_call_service',arguments=args,result=result)
            intent['status']='finished';d.atomic_json(path,saved)
