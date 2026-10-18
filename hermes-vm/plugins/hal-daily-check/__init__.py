"""Exactly two Hal worker tools; native creation and host-owned publication."""
import json
import os
from pathlib import Path
import sys

LIB=Path(__file__).resolve().parents[2]/'scripts/hal-daily-check'
if str(LIB) not in sys.path:
    sys.path.insert(0,str(LIB))
from hal_daily_cards import NativeBoard,plan,publish
from hal_daily_check import NativeMail,PROFILE,secret_values,sanitize


def _context(provided=None,publishing=False):
    from hermes_cli.profiles import current_profile_name
    from tools.kanban_tools import _is_dispatcher_owned_worker,_worker_guard,_worker_run_id
    from gateway.session_kanban import _bound_claim_is_live
    if current_profile_name('default')!='hal' or not _is_dispatcher_owned_worker():
        raise ValueError('not a Hal card worker')
    task=_worker_guard('kanban_schedule',{'task_id':provided} if provided else {})
    if not _worker_run_id(task) or (publishing and os.environ.get('HERMES_KANBAN_BOARD')!='hal'):
        raise ValueError('not a production Hal card')
    context=dict(task_id=task,run_id=_worker_run_id(task),claim_lock=os.environ.get('HERMES_KANBAN_CLAIM_LOCK'),db=os.environ.get('HERMES_KANBAN_DB'))
    if not _bound_claim_is_live(context):
        raise ValueError('stale card worker')
    return task


def available():
    try:
        _context();return True
    except Exception:
        return False


def handle_plan(args,**kwargs):
    phase='arguments'
    try:
        if set(args)!={'card_id','choices'}:
            raise ValueError('invalid plan arguments')
        phase='worker context'
        task=_context(args['card_id'])
        phase='credential scrubber'
        if sanitize(args['choices'],secret_values(PROFILE))!=args['choices']:
            raise ValueError('credential in public choices')
        phase='native board'
        return json.dumps(plan(NativeBoard(),task,args['choices']))
    except Exception as exc:
        return json.dumps({'error':'hal daily plan refused: invalid context, public choices or native command','phase':phase,'kind':type(exc).__name__})


def _park(tid):
    from tools.kanban_tools import _handle_schedule
    result=json.loads(_handle_schedule({'task_id':tid,'reason':'hal-daily publication not accepted'}))
    if result.get('status')!='scheduled':
        raise ValueError('native scheduled handoff failed')


def handle_publish(args,**kwargs):
    try:
        if set(args) not in ({'card_id','assessments'},{'card_id','note'}):
            raise ValueError('invalid publication arguments')
        task=_context(args['card_id'],publishing=True)
        board=NativeBoard()
        board.schedule=_park
        drafts=args['assessments'] if 'assessments' in args else args['note']
        try:
            sender=NativeMail(PROFILE)
            secrets=secret_values(PROFILE)
            return json.dumps(publish(board,task,drafts,PROFILE,sender,secrets=secrets))
        except Exception:
            _park(task)
            return json.dumps({'success':False,'scheduled':True})
    except Exception:
        return json.dumps({'error':'hal daily publication refused: invalid context, evidence, storage or native handoff'})


def register(ctx):
    common={'card_id':{'type':'string','description':'Your current dispatcher-owned Hal card ID.'}}
    tools=[('kanban_hal_daily_plan',handle_plan,'Create public-only release readers and one dependent assembly for your batch.',dict(common,choices={'type':'object','description':'Pair-keyed public release choices: releases [{component,release_version,url}], missing [string], or no_public_notes true.'}),['card_id','choices']),
           ('kanban_hal_daily_publish',handle_publish,'Validate complete reader results, refuse failed pairs, save and send one receipt-guarded batch or episode email. A failed send parks your card; stop afterward.',dict(common,assessments={'type':'object','additionalProperties':{'type':'string'}},note={'type':'string'}),['card_id'])]
    for name,handler,description,properties,required in tools:
        result=ctx.register_tool(name=name,toolset='kanban',handler=handler,check_fn=available,schema={'name':name,'description':description,'parameters':{'type':'object','properties':properties,'required':required,'additionalProperties':False}})
        if result is None:
            raise RuntimeError('Hal daily tool registration refused')
