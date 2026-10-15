"""One additive operator request tool; no stock kanban overrides or model routing."""
import json
import logging
import os
from pathlib import Path
import re
from hermes_cli.profiles import current_profile_name
from tools.kanban_tools import (_board, _default_task_id, _worker_run_id,
    _reject_delegated_child_mutation, _is_dispatcher_owned_worker)
from gateway.session_kanban import _bound_claim_is_live
from tools.registry import tool_error

REPORT_ROOT=Path('/home/hermes/.hermes/profiles/security_reviewer/workspace/reports')
BOARD_DB=Path('/home/hermes/.hermes/kanban/boards/security_reviewer/kanban.db')
logger=logging.getLogger(__name__)

def _context():
    if current_profile_name('default') != 'security_reviewer' or not _is_dispatcher_owned_worker():
        raise ValueError('not reviewer worker')
    _reject_delegated_child_mutation('reviewer_tooling_request')
    task=_default_task_id(None)
    run=_worker_run_id(task)
    claim=os.environ.get('HERMES_KANBAN_CLAIM_LOCK')
    if (not task or not re.fullmatch(r't_[a-f0-9]{8,}',task) or not run or not claim
        or os.environ.get('HERMES_KANBAN_BOARD')!='security_reviewer'
        or Path(os.environ.get('HERMES_KANBAN_DB','')).resolve()!=BOARD_DB.resolve()):
        raise ValueError('unbound context')
    return {'task_id':task,'run_id':run,'claim_lock':claim,'db':str(BOARD_DB)}

def available():
    try:
        _context()
        return True
    except Exception:
        return False

def handle_request(args,**kwargs):
    try:
        context=_context()
        if set(args)!={'tool','reason','report_path'}:
            raise ValueError('unexpected fields')
        tool=args['tool']
        reason=args['reason']
        if not isinstance(tool,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_.+-]{0,63}',tool.lower(),re.ASCII):
            raise ValueError('invalid tool')
        tool=tool.lower()
        if not isinstance(reason,str) or not 1<=len(reason.strip())<=4000:
            raise ValueError('invalid reason')
        report=Path(args['report_path'])
        expected=REPORT_ROOT/context['task_id']/'report.md'
        if report!=expected or report.is_symlink() or report.parent.is_symlink() or not report.is_file() or report.resolve()!=expected.absolute():
            raise ValueError('invalid report')
        # Read-only lease/PID proof precedes the native write connection. A stale
        # worker or inherited delegate environment cannot create an operator card.
        if not _bound_claim_is_live(context):
            raise ValueError('stale worker')
        with _board('security_reviewer') as (kb,conn):
            task_id=kb.create_task(conn,title='Tooling request: '+tool,
                body=f'Check needing {tool}: {reason.strip()}\nReview report: {report}\nOperator: update the image or close/archive this request; do not dispatch it.',
                assignee=None,created_by='security_reviewer',initial_status='blocked',
                parents=(),workspace_kind='scratch',project_id='',
                idempotency_key='security-reviewer-tooling:'+tool)
            task=kb.get_task(conn,task_id)
            return json.dumps({'task_id':task_id,'status':task.status})
    except Exception:
        logger.exception('reviewer tooling request refused')
        return tool_error('reviewer_tooling_request refused: invalid context, request or report; nothing was intentionally dispatched')

def register(ctx):
    result=ctx.register_tool(name='reviewer_tooling_request',toolset='kanban',
        handler=handle_request,check_fn=available,
        schema={'name':'reviewer_tooling_request','description':'Create an unassigned blocked operator tooling request for a missing check in your finalized report. Never installs or dispatches.',
                'parameters':{'type':'object','properties':{'tool':{'type':'string'},'reason':{'type':'string'},'report_path':{'type':'string'}},'required':['tool','reason','report_path'],'additionalProperties':False}})
    if result is None:
        raise RuntimeError('reviewer_tooling_request registration refused')
