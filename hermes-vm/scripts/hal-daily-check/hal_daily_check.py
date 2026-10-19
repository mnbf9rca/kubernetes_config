#!/usr/bin/env python3
"""Deterministic morning check; never fetches releases, invokes models or edits HA."""
from __future__ import annotations
import argparse
import asyncio
import contextlib
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

LONDON=ZoneInfo('Europe/London')
PROFILE=Path('/home/hermes/.hermes/profiles/hal')
READS={'get_states','config/entity_registry/list','config/device_registry/list','config_entries/get','trace/list','trace/get','system_log/list','repairs/list_issues','manifest/list'}
HA_READ_SECONDS=60
MAIL_SECONDS=180
AREAS=('batteries','devices','automation runs','recent errors','updates','reported configuration issues')


def now():
    return dt.datetime.now(dt.timezone.utc)


def stamp(value):
    parsed=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp lacks timezone')
    return parsed


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def clean(value,secrets=()):
    text=str(value)
    for secret in sorted((v for v in secrets if isinstance(v,str) and v),key=len,reverse=True):
        text=text.replace(secret,'[redacted]')
    return ''.join(c for c in text if c in '\n\t' or (ord(c)>=32 and ord(c)!=127))


def sanitize(value,secrets=()):
    if isinstance(value,str):
        return clean(value,secrets)
    if isinstance(value,list):
        return [sanitize(x,secrets) for x in value]
    if isinstance(value,dict):
        return {clean(k,secrets):sanitize(v,secrets) for k,v in value.items()}
    return value


def label(value,fallback='device'):
    return ' '.join(clean(value or fallback).split())


def public_url(value):
    if not isinstance(value,str) or any(ord(c)<33 for c in value):
        return None
    try:
        u=urllib.parse.urlsplit(value)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.port not in (None,443):
            return None
        host=u.hostname.lower()
        if '.' not in host or host.endswith(('.local','.localhost','.internal','.lan')):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return value
    except ValueError:
        return None


def read_json(path,default):
    try:
        value=json.loads(Path(path).read_text())
        return value if isinstance(value,type(default)) else default
    except (OSError,ValueError):
        return default


def atomic_json(path,value,secrets=()):
    path=Path(path)
    raw=json.dumps(sanitize(value,secrets),ensure_ascii=False,sort_keys=True)
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


@contextlib.contextmanager
def lock(directory):
    fd=os.open(directory,os.O_RDONLY)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def failed_trace(row,full=None):
    if row.get('not_triggered') or row.get('script_execution')=='running':
        return False
    if row.get('error') is not None:
        return True
    if row.get('script_execution')!='aborted' or full is None:
        return False
    trace=full.get('trace',{})
    if 'trace' in trace:
        full=trace;trace=trace.get('trace',{})
    step=full.get('last_step')
    rows=trace.get(step,[]) if step else [x for bucket in trace.values() if isinstance(bucket,list) for x in bucket]
    if not rows:
        return False
    end=rows[-1]
    return end.get('error') is not None or end.get('result',{}).get('error') is True


def trace_covered(rows,cutoff):
    runs=[r for r in rows if not r.get('not_triggered')]
    if len(runs)<5:
        return True
    try:
        return min(stamp(r['timestamp']['start']) for r in runs)<=cutoff
    except (KeyError,ValueError,TypeError):
        return False


def group_devices(unhealthy,devices,eligible,entry_names,parent_down):
    """Collapse only complete monitored groups; registry cycles never imply a cause."""
    dr={x['id']:x for x in devices}
    bad={x['device_id']:x for x in unhealthy}
    groups=[];used=set()
    subtrees={};ambiguous=False
    for member in eligible:
        cursor=member;seen=set()
        while cursor:
            if cursor in seen or cursor not in dr or len(dr[cursor].get('config_entries',[]))>1:
                ambiguous=True;break
            seen.add(cursor);cursor=dr[cursor].get('via_device_id')
    if ambiguous:
        eligible=set()
    for member in eligible:
        cursor=member;seen=set()
        while cursor in dr:
            parent=dr[cursor].get('via_device_id')
            if not parent or parent in seen:
                break
            seen.add(parent);subtrees.setdefault(('hub',parent),set()).add(member);cursor=parent
    for entry in entry_names:
        members={x for x in eligible if entry in dr.get(x,{}).get('config_entries',[])}
        if members:
            subtrees[('integration',entry)]=members
    ordered=sorted(subtrees.items(),key=lambda x:(x[0][0]!='hub',len(x[1]),x[0][1]))
    for (kind,owner),members in ordered:
        if len(members)<2 or not members<=set(bad) or members&used:
            continue
        rows=[bad[x] for x in sorted(members)]
        name=label(dr.get(owner,{}).get('name') if kind=='hub' else entry_names.get(owner),'shared connection')
        if owner in parent_down:
            line=f'The {name} looks down, taking {len(rows)} devices with it; check its power and connection.'
        else:
            conditions=sorted({r['condition'] for r in rows})
            line=f'All {len(rows)} monitored devices behind {name} are {" or ".join(conditions)}; check their shared connection.'
        groups.append(dict(members=rows,line=line,owner=owner));used|=members
    for member in sorted(set(bad)-used):
        row=bad[member]
        groups.append(dict(members=[row],grouping_unavailable=ambiguous or member not in eligible,line=f'The {label(row.get("name"))} is {row["condition"]}; check its power and connection.'))
    for group in groups:
        group['key']=digest(sorted((r['device_id'],r['condition']) for r in group['members']))
    return groups


def error_context(log,snapshot,at):
    logger=str(log.get('name') or log.get('logger') or 'Home Assistant')
    match=re.match(r'(?:homeassistant\.components|custom_components)\.([a-z0-9_]+)',logger)
    manifests=snapshot.get('manifests',[])
    manifest=next((m for m in manifests if (match and m['domain']==match[1]) or any(logger==name or logger.startswith(name+'.') for name in m.get('loggers') or [])),{})
    domain=manifest.get('domain') or (match[1] if match else logger)
    entries=[e for e in snapshot.get('entries',[]) if e.get('domain')==domain and not e.get('disabled_by')]
    name=manifest.get('name') or domain.replace('_',' ').capitalize()
    if not entries and not logger.startswith('custom_components.'):
        name='Home Assistant ('+name+')'
    message=log.get('message')
    if isinstance(message,list):
        message=next((x for x in message if isinstance(x,str) and x.strip()),'')
    message=next((x.strip() for x in message.splitlines() if x.strip()),'') if isinstance(message,str) else ''
    if not message:
        return domain,name,None,None
    sentence=re.split(r'(?<=[.!?])\s+',label(message),maxsplit=1)[0].rstrip('.!?')
    lower=message.casefold()
    address=re.search(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])',message)
    try:
        local_address=bool(address and ipaddress.ip_address(address[0]).is_private)
    except ValueError:
        local_address=False
    if 'websocket' in lower:
        status=re.search(r'\b([45]\d\d)\b',message)
        if 'refused' in lower:
            sentence='its WebSocket connection was refused'
        elif status:
            sentence='its WebSocket connection failed (HTTP '+status[1]+')'
        else:
            sentence='its WebSocket connection failed: '+sentence
    elif local_address and any(word in lower for word in ('connect','error fetching','timeout','timed out')):
        sentence='could not reach the hub at '+address[0]
    elif any(word in lower for word in ('cannot connect','could not connect','connection failed','connection refused','connect call failed','timed out','timeout')):
        host=re.search(r'(?:host |https?://|to )([a-z0-9.-]+\.[a-z]{2,})(?=[:/\s]|$)',lower)
        if host and not host[1].endswith(('.local','.lan','.internal')):
            sentence='could not reach its cloud service'
    try:
        first=dt.datetime.fromtimestamp(log['first_occurred'],dt.timezone.utc).astimezone(LONDON)
        last=dt.datetime.fromtimestamp(log['timestamp'],dt.timezone.utc).astimezone(LONDON)
        count=log['count']
        if not isinstance(count,int) or count<1 or first>last:
            raise ValueError('invalid log timing')
        pattern='%H:%M' if first.date()==last.date()==at.astimezone(LONDON).date() else '%-d %B %H:%M'
        timing='once at '+last.strftime(pattern) if count==1 else f'{count} times between '+first.strftime(pattern)+' and '+last.strftime(pattern)
    except (KeyError,TypeError,ValueError,OverflowError,OSError):
        timing='time and count unknown'
    ids={e['entry_id'] for e in entries}
    entities=[e for e in snapshot.get('entities',[]) if (e.get('config_entry_id') in ids or e.get('platform')==domain) and not e.get('disabled_by')]
    states={s['entity_id']:s['state'] for s in snapshot.get('states',[])}
    available=[states.get(e['entity_id']) for e in entities]
    status='unknown'
    if any(e.get('state') in ('setup_retry','setup_error','migration_error') for e in entries) or 'unavailable' in available:
        status='still failing'
    elif entries and all(e.get('state')=='loaded' for e in entries) and available and all(s is not None for s in available):
        status='working now'
    return domain,name,f'{name}: {sentence}, {timing}; {status}.',status


def assessment_email_time(saved):
    try:
        return stamp(saved['sent_receipt']['accepted_at']).astimezone(LONDON).strftime('%-d %B %H:%M')
    except (KeyError,TypeError,ValueError):
        return 'an unknown date and time'


def already_assessed(updates,pairs):
    lines=[]
    for update in updates:
        saved=pairs.get(update['key'],{})
        if not saved.get('sent_receipt') or not saved.get('assessment'):
            continue
        verdict=next((line.strip() for line in saved['assessment'].splitlines() if line.strip()),'')
        # Older receipts put the verdict and context in one paragraph.
        verdict=re.split(r'(?<=[.!?])\s+',verdict,maxsplit=1)[0].rstrip('.!?')
        title=f'{label(update["name"])} {label(update.get("installed"),"unknown version")} → {label(update.get("available"),"unknown version")}'
        lines.append(f'- **{title}**: {verdict}. Assessed in the email of {assessment_email_time(saved)}.')
    return lines


def findings(snapshot,at,cutoff):
    items=[];gaps=list(snapshot.get('gaps',[]));diagnostics=list(snapshot.get('diagnostics',[]));updates=[];development=[]
    states={s['entity_id']:s for s in snapshot.get('states',[])}
    er={e['entity_id']:e for e in snapshot.get('entities',[])}
    dr={x['id']:x for x in snapshot.get('devices',[])}
    entries={e['entry_id']:e for e in snapshot.get('entries',[])}
    names={eid:label(s.get('attributes',{}).get('friendly_name'),dr.get(er.get(eid,{}).get('device_id'),{}).get('name_by_user') or dr.get(er.get(eid,{}).get('device_id'),{}).get('name') or ('automation' if eid.startswith('automation.') else 'device')) for eid,s in states.items()}
    battery_devices=set()
    for eid,s in states.items():
        a=s.get('attributes',{});registry=er.get(eid,{})
        if registry.get('disabled_by'):
            continue
        if a.get('device_class')=='battery' and eid.startswith(('sensor.','binary_sensor.')):
            value=None
            if eid.startswith('binary_sensor.'):
                value=0 if s['state']=='on' else None
            else:
                try:
                    value=float(s['state'])
                except (ValueError,TypeError):
                    pass
            owner=registry.get('device_id') or eid
            if value is not None and math.isfinite(value) and 0<=value<=10 and owner not in battery_devices:
                name=re.sub(r'(?:\s+battery)+$','',names[eid],flags=re.I)
                items.append(f'The {name} battery is {"flat" if value==0 else "low"}; replace or recharge it.')
                battery_devices.add(owner)
        if eid.startswith('update.'):
            if s['state'] in ('unknown','unavailable'):
                continue
            name=re.sub(r'(?:\s+(?:update|firmware))+$','',label(a.get('title'),names[eid]),flags=re.I)
            installed=a.get('installed_version');target=a.get('latest_version')
            package='/'.join(str(x) for x in (registry.get('platform') or 'update',registry.get('unique_id') or eid))
            if isinstance(installed,str) and re.search(r'(?:[.\-]dev(?:[.0-9]|$))',installed,re.I):
                development.append(dict(package=package,signature=digest([installed,target]),line=f'{name} is running development build {label(installed)}; I will flag a newly offered release.'))
            if isinstance(target,str) and re.search(r'(?:[.\-](?:dev|alpha|beta|rc)|\d(?:a|b|rc)\d)',target,re.I):
                continue
            if s['state']!='on':
                continue
            pair=dict(package=package,installed=installed,available=target)
            updates.append(dict(pair,key=digest(pair),name=name,entity_id=eid,release_url=public_url(a.get('release_url'))))
    monitor=states.get('sensor.offline_devices');unhealthy=[];device_complete=False;eligible=set()
    if monitor:
        try:
            if not -5<=(at-stamp(monitor['last_reported'])).total_seconds()<=300:
                raise ValueError('stale')
            a=monitor['attributes'];raw=a['devices'];counts=a['integrations']
            if not isinstance(raw,list) or not isinstance(counts,dict) or str(len(raw))!=monitor['state']:
                raise ValueError('shape')
            for row in raw:
                source=row['id'];eid=source if source in er else None
                dev=er.get(source,{}).get('device_id') or (source if source in dr else None)
                if not dev:
                    raise ValueError('device identity')
                if row.get('condition','offline') not in ('offline','stale'):
                    raise ValueError('unknown unhealthy condition')
                unhealthy.append(dict(device_id=dev,entity_ids=[eid] if eid else [],condition=row.get('condition','offline'),name=label(row.get('name'),names.get(source,'device')),since=row.get('since'),integration=row.get('integration')))
            # Registry membership is usable only when it agrees with the monitor's own eligible counts.
            for domain,total in counts.items():
                candidates={e.get('device_id') for e in er.values() if not e.get('disabled_by') and e.get('device_id') and (entries.get(e.get('config_entry_id'),{}).get('domain')==domain or e.get('platform')==domain)}
                if len(candidates)==total:
                    eligible|=candidates
            device_complete=True
        except (KeyError,ValueError,TypeError):
            gaps.append('The offline-devices monitor is stale or unreadable, so I could not check for offline devices.');unhealthy=[]
    else:
        gaps.append('The offline-devices monitor is unavailable, so I could not check for offline devices.')
    parent_down={er[eid].get('device_id') for eid,s in states.items() if eid in er and s.get('state')=='unavailable'}
    entry_names={k:label(v.get('title'),v.get('domain')) for k,v in entries.items()}
    situations=group_devices(unhealthy,list(dr.values()),eligible,entry_names,parent_down)
    if any(g.get('grouping_unavailable') for g in situations):
        diagnostics.append('device grouping: complete monitor scope or registry ancestry was unavailable')
    items.extend(g['line'] for g in situations)
    buckets={}
    for row in snapshot.get('traces',[]):
        if row.get('not_triggered'):
            continue
        buckets.setdefault(str(row.get('item_id')),[]).append(row)
    automations={}
    for eid,s in states.items():
        if eid.startswith('automation.'):
            identity=er.get(eid,{}).get('unique_id') or s.get('attributes',{}).get('id')
            if not identity:
                diagnostics.append(f'{names[eid]} has no ID for retained-run lookup')
            else:
                automations[str(identity)]=names[eid]
    for aid in sorted(set(automations)|set(buckets)):
        name=automations.get(aid,'unnamed')
        if aid=='None':
            diagnostics.append('a retained automation run has no automation ID')
        rows=buckets.get(aid,[])
        if not trace_covered(rows,cutoff):
            diagnostics.append(f'{name}: retained run history does not reach the cutoff')
        for row in rows:
            try:
                end=stamp(row.get('timestamp',{}).get('finish') or row['timestamp']['start'])
            except (KeyError,ValueError,TypeError):
                diagnostics.append(f'{name} run timing was unreadable');continue
            if cutoff<end<=at and failed_trace(row,row.get('full')):
                items.append(f'The {name} automation failed; check its trace in Home Assistant.')
    reported_errors=set();resolved=[]
    for log in snapshot.get('logs',[]):
        if str(log.get('level','')).upper() not in ('ERROR','CRITICAL'):
            continue
        logger=str(log.get('name') or log.get('logger') or '')
        if logger.startswith('homeassistant.components.automation'):
            continue
        domain,name,line,status=error_context(log,snapshot,at)
        if domain in reported_errors:
            continue
        if line:
            (resolved if status=='working now' else items).append(line)
            reported_errors.add(domain)
        else:
            diagnostics.append(f'{name}: a recent error had no readable message')
    for entry in entries.values():
        if entry.get('state') in ('setup_error','setup_retry','migration_error') and entry.get('domain') not in reported_errors:
            items.append(f'The {label(entry.get("title"),entry.get("domain"))} integration could not start; check its connection and setup.')
    for issue in snapshot.get('repairs',[]):
        if issue.get('ignored') or issue.get('severity') not in ('warning','error','critical'):
            continue
        domain=label(issue.get('domain'),'Home Assistant')
        automation=(issue.get('translation_placeholders') or {}).get('entity_id')
        if automation in names and any(names[automation] in line and 'automation failed' in line for line in items):
            continue
        breaks=issue.get('breaks_in_ha_version')
        if breaks:
            items.append(f'{domain} reports a configuration change needed before Core {label(breaks)}; review its repair notice.')
        else:
            items.append(f'{domain} reports a configuration issue; review its repair notice.')
    inventory=sorted({e.get('domain') for e in entries.values() if not e.get('disabled_by') and e.get('domain')}|{e['platform'] for e in er.values() if not e.get('disabled_by') and e.get('platform')})
    return dict(development=development,items=sorted(set(items)),resolved=sorted(set(resolved)),updates=sorted(updates,key=lambda x:(x['package'],str(x['available']))),situations=situations,gaps=sorted(set(gaps)),diagnostics=sorted(set(diagnostics)),device_complete=device_complete,inventory=inventory,cutoff=at.isoformat())


def item_bullet(text):
    text=text.strip().removeprefix('- ')
    if text.startswith('The '):
        text=re.sub(r'[A-Za-z]',lambda m:m.group().upper(),text[4:],count=1)
    blocker=re.fullmatch(r'I could not (?:check|read) (.+?) because (.+)',text)
    if blocker:
        area=blocker[1].removeprefix('the ');reason=blocker[2]
        return '- **'+area[:1].upper()+area[1:]+'**: '+reason[:1].upper()+reason[1:]
    if text.startswith('**'):
        return '- '+text
    split=re.search(r':\s|\s(?:is|are|looks|reports|has|have|could|still|automation failed|integration could)\b',text)
    end=split.start() if split and text[:split.start()]!='I' else len(text)
    return '- **'+text[:end]+'**'+text[end:]


def email_sections(needs=(),updates=(),gaps=(),resolved=(),already=()):
    sections=[f'## {heading}\n'+'\n'.join(items) for heading,items in
              [('Needs you',needs),('Resolved overnight',resolved),('Updates',updates),('Could not finish',gaps),('Already assessed',already)] if items]
    return '\n\n'.join(sections) or 'Nothing needs your attention today.'


def email_subject(actions,gaps=False):
    count=f'{actions} '+('thing needs you' if actions==1 else 'things need you')
    if gaps:
        return 'House: I could not finish the check'+('; '+count if actions else '')
    return 'House: '+(count if actions else 'nothing needs you')


def render(report,assessments,notes,at):
    lines=list(report.get('items',[]));gaps=list(report.get('gaps',[]));pairs=assessments.get('pairs',{})
    for situation in report.get('situations',[]):
        root=report.get('situation_roots',{}).get(situation['key'])
        saved=notes.get('pairs',{}).get(root,{})
        if saved.get('sent_receipt') and saved.get('observed_at') and situation['line'] in lines:
            when=stamp(saved['observed_at']).astimezone(LONDON).strftime('%-d %B')
            index=lines.index(situation['line']);lines[index]+=f' Hal looked at this on {when}.'
    needs=[item_bullet(line) for line in lines];updates=[]
    for update in report.get('updates',[]):
        saved=pairs.get(update['key'],{});target=label(update.get('available'),'unknown version')
        title=f'{update["name"]} {label(update.get("installed"),"unknown version")} → {target}'
        link=update.get('release_url') or 'No release link supplied.'
        if saved.get('outcome')=='no_public_notes':
            needs.append(f'- **{title}**: no release notes to assess; review the release link.\n  {link}')
        elif saved.get('assessment') and saved.get('sent_receipt'):
            when=assessment_email_time(saved)
            needs.append(f'- **{title}**: still waiting to be installed; assessed in the email of {when}. Review that assessment before installing.\n  {link}')
        else:
            updates.append(f'- **{title}**: assessment pending.\n  {link}')
    updates.extend(item_bullet(line) for line in report.get('notices',[]))
    return email_subject(len(needs),bool(gaps)),email_sections(needs,updates,[item_bullet(label(x)) for x in sorted(set(gaps))],resolved=[item_bullet(line) for line in report.get('resolved',[])])


class HA:
    def __init__(self,url,token):
        self.url=url.rstrip('/');self.token=token;self.denied=False;self.deadline=None
    def read(self,command,**args):
        if command not in READS:
            raise ValueError('HA mutation refused')
        if self.denied:
            raise PermissionError('HA authentication refused')
        try:
            async def bounded():
                remaining=HA_READ_SECONDS if self.deadline is None else min(HA_READ_SECONDS,max(0,self.deadline-time.monotonic()))
                return await asyncio.wait_for(self._read(command,args),remaining)
            return asyncio.run(bounded())
        except Exception as exc:
            if isinstance(exc,PermissionError) or getattr(exc,'status',None) in (401,403):
                self.denied=True
                raise PermissionError('HA authentication refused') from None
            raise
    async def _read(self,command,args):
        import aiohttp
        timeout=aiohttp.ClientTimeout(total=60)
        url=self.url.replace('https://','wss://',1).replace('http://','ws://',1)+'/api/websocket'
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(url,max_msg_size=32*1024*1024) as ws:
                await ws.receive_json()
                await ws.send_json({'type':'auth','access_token':self.token})
                if (await ws.receive_json()).get('type')!='auth_ok':
                    raise PermissionError('HA authentication refused')
                await ws.send_json(dict(id=1,type=command,**args))
                response=await ws.receive_json()
                if response.get('success') is not True:
                    if response.get('error',{}).get('code') in ('unauthorized','auth_invalid'):
                        raise PermissionError('HA authentication refused')
                    raise ValueError('HA read unavailable')
                return response['result']


def collect_ha(client):
    client.deadline=time.monotonic()+2700
    data=dict(gaps=[],diagnostics=[])
    area_names=dict(states='batteries, devices and updates',entities='device and update identities',devices='device grouping',entries='integration setup',traces='automation runs',logs='recent errors',repairs='reported configuration issues')
    for field,command in [('states','get_states'),('entities','config/entity_registry/list'),('devices','config/device_registry/list'),('entries','config_entries/get'),('traces','trace/list'),('logs','system_log/list'),('repairs','repairs/list_issues')]:
        try:
            value=client.read(command,**({'domain':'automation'} if field=='traces' else {}))
            if field=='entries' and isinstance(value,dict):
                value=value['entries']
            if field=='repairs' and isinstance(value,dict):
                value=value['issues']
            if not isinstance(value,list):
                raise ValueError('unexpected HA response')
            data[field]=value
        except Exception as exc:
            reason='the sixty-second wait for Home Assistant timed out' if isinstance(exc,TimeoutError) else 'Home Assistant did not provide the data'
            data[field]=[];data['gaps'].append('I could not check '+area_names[field]+' because '+reason+'.')
    try:
        domains=sorted({e['domain'] for e in data['entries']}|{m[1] for log in data['logs'] if (m:=re.match(r'(?:homeassistant\.components|custom_components)\.([a-z0-9_]+)',str(log.get('name') or log.get('logger') or '')))})
        data['manifests']=client.read('manifest/list',integrations=domains) if domains else []
        if not isinstance(data['manifests'],list):
            raise ValueError('unexpected HA manifests')
    except Exception:
        data['manifests']=[];data['diagnostics'].append('Home Assistant did not provide integration display names')
    for row in data['traces']:
        if row.get('script_execution')=='aborted' and not row.get('not_triggered') and not row.get('error'):
            try:
                row['full']=client.read('trace/get',domain='automation',item_id=row['item_id'],run_id=row['run_id'])
            except Exception:
                data['diagnostics'].append('automation runs: an aborted run could not be classified')
    return data


class Daily:
    def __init__(self,home,collect,board,send,push,clock=now,secrets=()):
        self.home=Path(home);self.collect=collect;self.board=board;self.send=send;self.push=push;self.clock=clock;self.secrets=secrets
    def run(self,trial=False):
        at=self.clock();saved=read_json(self.home/'daily-check-state.json',{}) if trial else {}
        cutoff=stamp(saved['accepted_cutoff']) if saved.get('accepted_cutoff') else at-dt.timedelta(days=1)
        r=findings(self.collect(),at,cutoff)
        if trial:
            r['notices']=[x['line'] for x in r.get('development',[]) if saved.get('development_notices',{}).get(x['package'])!=x['signature']]
        subject,body=render(r,read_json(self.home/'daily-check-assessments.json',{}),read_json(self.home/'daily-check-device-notes.json',{}),at)
        subject,body=clean(subject,self.secrets),clean(body,self.secrets)
        if trial:
            subject='[trial] '+subject
            try:
                result=self.send(subject,body)
            except Exception:
                result={}
            accepted=bool(result.get('success') and not result.get('skipped') and not result.get('partial_success'))
            print(clean(json.dumps(dict(success=accepted,message_id=result.get('message_id') if accepted else None,sent_at=self.clock().isoformat(),subject=subject),ensure_ascii=False),self.secrets))
            return 0 if accepted else 1
        print(subject+'\n'+body);return 0
    def _push(self,status,verdict):
        try:
            self.push(status,verdict)
        except Exception:
            print('verdict=heartbeat-failed')
    def _production(self):
        path=self.home/'daily-check-state.json';saved=sanitize(read_json(path,{}),self.secrets)
        at=self.clock();day=at.astimezone(LONDON).date().isoformat()
        if saved.get('day')==day and saved.get('receipt'):
            self._push('up','already-accepted');return 0
        if saved.get('day')!=day or not saved.get('subject'):
            cutoff=stamp(saved['accepted_cutoff']) if saved.get('accepted_cutoff') else at-dt.timedelta(days=1)
            try:
                report=findings(self.collect(),at,cutoff)
            except Exception:
                report=dict(items=[],updates=[],situations=[],gaps=[f'I could not check {area} because the Home Assistant data could not be read.' for area in AREAS],device_complete=False,inventory=[],cutoff=at.isoformat())
            notice_updates={}
            for line in report.get('diagnostics',[]):
                print(clean('Diagnostic: '+line,self.secrets))
            acknowledged=saved.get('development_notices',{})
            for notice in report.get('development',[]):
                if acknowledged.get(notice['package'])!=notice['signature']:
                    report.setdefault('notices',[]).append(notice['line']);notice_updates[notice['package']]=notice['signature']
            report=sanitize(report,self.secrets)
            assessments=sanitize(read_json(self.home/'daily-check-assessments.json',{}),self.secrets)
            notes=read_json(self.home/'daily-check-device-notes.json',{})
            if self.board:
                try:
                    self.board.ensure(report,assessments,notes,day,at)
                except Exception as exc:
                    blocker="I could not read the task board, so I could not check Hal's open work."
                    if isinstance(exc,subprocess.TimeoutExpired):
                        blocker+=' The three-minute wait for the task board timed out.'
                    report['gaps'].append(blocker)
            subject,body=render(report,assessments,notes,at)
            saved=sanitize(dict(day=day,subject=subject,body=body,cutoff=report['cutoff'],accepted_cutoff=saved.get('accepted_cutoff'),receipt=None,notice_updates=notice_updates,development_notices=saved.get('development_notices',{})),self.secrets)
            try:
                atomic_json(path,saved,self.secrets)
            except Exception:
                self._push('down','state-failed');return 1
        try:
            result=self.send(saved['subject'],saved['body'])
        except Exception:
            result={}
        if not result.get('success') or result.get('skipped') or result.get('partial_success'):
            self._push('down','send-failed');return 1
        saved['receipt']={'accepted_at':at.isoformat(),'message_id':result.get('message_id')}
        saved['accepted_cutoff']=saved['cutoff']
        saved.setdefault('development_notices',{}).update(saved.get('notice_updates',{}))
        try:
            atomic_json(path,saved,self.secrets)
        except Exception:
            self._push('down','state-failed');return 1
        self._push('up','accepted');return 0


def load_credentials(home,heartbeat=True):
    import io
    from utils import load_yaml_file_readonly
    from agent.secret_scope import load_env_file
    from agent.secret_sources.registry import apply_all
    cfg=load_yaml_file_readonly(home/'config.yaml')
    source=dict(cfg['daily_check']['runner_secrets']['onepassword'])
    refs=dict(source['env'])
    if heartbeat:
        refs['HAL_DAILY_CHECK_PUSH_TOKEN']=cfg['secrets']['onepassword']['env']['HAL_DAILY_CHECK_PUSH_TOKEN']
    source['env']=refs
    # Resolve into a private mapping: neither agents nor child processes inherit HA credentials.
    values=dict(os.environ)
    for path in (home/'.op.env',home/'.env'):
        if path.exists():
            values.update(load_env_file(path))
    for key in refs:
        values.pop(key,None)
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        apply_all({'onepassword':source},home,environ=values)
    required=('HAL_DAILY_RUNNER_HASS_URL','HAL_DAILY_RUNNER_HASS_TOKEN')+(('HAL_DAILY_CHECK_PUSH_TOKEN',) if heartbeat else ())
    if any(not values.get(key) or values[key].startswith(('op://','${')) for key in required):
        raise ValueError('credentials unavailable')
    return {key:values[key] for key in (*required,'OP_SERVICE_ACCOUNT_TOKEN') if values.get(key)}


def secret_values(home):
    from utils import load_yaml_file_readonly
    cfg=load_yaml_file_readonly(home/'config.yaml')
    keys=set(cfg.get('secrets',{}).get('onepassword',{}).get('env',{}))|{'OP_SERVICE_ACCOUNT_TOKEN'}
    values=[os.environ[k] for k in keys if os.environ.get(k)]
    for server in cfg.get('mcp_servers',{}).values():
        url=server.get('url')
        if isinstance(url,str):
            values.append(url)
            path=urllib.parse.urlsplit(url).path
            if '/api/webhook/' in path:
                values.append(path.split('/api/webhook/',1)[1].split('/',1)[0])
    return tuple(values)


class NativeMail:
    def __init__(self,home):
        from utils import load_yaml_file_readonly
        self.home=home
        cfg=load_yaml_file_readonly(home/'config.yaml')
        self.recipient=cfg.get('daily_check',{}).get('recipient')
        if not isinstance(self.recipient,str) or '@' not in self.recipient or any(ord(c)<=32 for c in self.recipient):
            raise ValueError('daily-check recipient not configured')
    def __call__(self,subject,body):
        payload=json.dumps(dict(schema='hal.email-verdict.v1',subject=subject,body=body),ensure_ascii=False)
        cli=shutil.which('hermes') or '/home/hermes/.local/bin/hermes'
        result=subprocess.run([cli,'-p','hal','send','--to','hal_email_verdict:'+self.recipient,'--file','-','--json'],input=payload,text=True,capture_output=True,timeout=MAIL_SECONDS)
        if result.returncode:
            return {'success':False}
        reply=json.loads(result.stdout)
        accepted=(reply.get('success') is True and reply.get('platform')=='hal_email_verdict' and reply.get('chat_id')==self.recipient and not reply.get('skipped') and not reply.get('partial_success'))
        return dict(success=accepted,message_id=reply.get('message_id') if accepted else None)


def kuma_push(token,status,verdict):
    if verdict not in {'accepted','already-accepted','send-failed','credentials-failed','state-failed','timeout','internal-failed'}:
        raise ValueError('invalid heartbeat verdict')
    if not token:
        raise ValueError('heartbeat unavailable')
    query=urllib.parse.urlencode(dict(status=status,msg='verdict='+verdict))
    url='https://uptime.cynexia.com/api/push/'+urllib.parse.quote(token,safe='')+'?'+query
    request=urllib.request.Request(url,headers={'User-Agent':'Hal-Daily-Check/1'})
    with urllib.request.urlopen(request,timeout=30) as response:
        if json.load(response).get('ok') is not True:
            raise ValueError('heartbeat refused')


class BoardProcess:
    """A process timeout can stop native SQLite busy retries, unlike a Python timer."""
    def __init__(self,home,deadline=lambda:None):
        self.home=home;self.deadline=deadline
    def ensure(self,report,assessments,notes,day,at):
        payload=dict(report=report,assessments=assessments,notes=notes,day=day,at=at.isoformat())
        result=subprocess.run([sys.executable,__file__,'--ensure-cards'],input=json.dumps(payload),text=True,capture_output=True,timeout=min(180,max(0.001,self.deadline()-time.monotonic())) if self.deadline() else 180)
        if result.returncode:
            raise ValueError('board work unavailable')
        report.clear();report.update(json.loads(result.stdout))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument('--production',action='store_true')
    modes.add_argument('--trial',action='store_true',help='collect and email once with [trial] prefix; no state, cards, heartbeat or daily lock')
    parser.add_argument('--fixture',type=Path)
    modes.add_argument('--ensure-cards',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    if args.fixture and (args.production or args.trial or args.ensure_cards):
        parser.error('fixture input is preview-only')
    if args.fixture:
        snapshot=json.loads(args.fixture.read_text())
        at=now();subject,body=render(findings(snapshot,at,at-dt.timedelta(days=1)),{},{},at)
        print(subject+'\n'+body);return 0
    if os.environ.get('HERMES_HOME')!=str(PROFILE):
        print('verdict=credentials-failed');return 1
    sys.path.insert(0,'/home/hermes/.hermes/hermes-agent')
    if args.ensure_cards:
        from hal_daily_cards import NativeBoard,Tickets
        payload=json.load(sys.stdin)
        Tickets(NativeBoard(board='hal')).ensure(payload['report'],payload['assessments'],payload['notes'],payload['day'],stamp(payload['at']))
        print(json.dumps(payload['report']));return 0
    token='';verdict='credentials-failed'
    try:
        with lock(PROFILE) if args.production else contextlib.nullcontext():
            def timed_out(signum,frame):
                raise TimeoutError('fifty-minute outer hang guard')
            signal.signal(signal.SIGALRM,timed_out)
            signal.alarm(3000)
            try:
                credentials=load_credentials(PROFILE,heartbeat=not args.trial);token=credentials.get('HAL_DAILY_CHECK_PUSH_TOKEN','')
                client=HA(credentials['HAL_DAILY_RUNNER_HASS_URL'],credentials['HAL_DAILY_RUNNER_HASS_TOKEN'])
                app=Daily(PROFILE,lambda:collect_ha(client),None if args.trial else BoardProcess(PROFILE,lambda:client.deadline),NativeMail(PROFILE),lambda s,v:kuma_push(token,s,v),secrets=(*secret_values(PROFILE),*credentials.values()))
                verdict='internal-failed'
                return app._production() if args.production else app.run(trial=args.trial)
            finally:
                signal.alarm(0)
    except BlockingIOError:
        print('verdict=busy');return 0
    except Exception as exc:
        if isinstance(exc,TimeoutError):verdict='timeout'
        if args.production:
            with contextlib.suppress(Exception):kuma_push(token,'down',verdict)
        print('verdict='+verdict);return 1



if __name__=='__main__':
    raise SystemExit(main())
