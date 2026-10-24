#!/usr/bin/env python3
"""Native no-agent one-shot: execute only saved explicit operator restart intents."""
from pathlib import Path
import asyncio
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'hal-daily-check'))
import hal_daily_check as daily
import hal_reply_actions as replies


def call_mcp(tool,args):
    from agent.secret_scope import build_profile_secret_scope, get_secret, reset_secret_scope, set_secret_scope
    from hermes_cli.env_loader import hydrate_profile_secret_sources
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    hydrate_profile_secret_sources(daily.PROFILE)
    scope=set_secret_scope(build_profile_secret_scope(daily.PROFILE),profile_home=str(daily.PROFILE))
    try:
        url=get_secret('HAL_HA_MCP_URL')
        if not url:
            raise ValueError('HAL_HA_MCP_URL is unavailable in the Hal profile secret scope')
    finally:
        reset_secret_scope(scope)
    async def call():
        async with streamable_http_client(url) as streams:
            async with ClientSession(*streams[:2]) as session:
                await session.initialize()
                result=await session.call_tool(tool,args)
                if result.is_error:
                    return {'error':'Home Assistant refused the requested restart.'}
                text='\n'.join(c.text for c in result.content if hasattr(c,'text'))
                return replies.result_value(text)
    return asyncio.run(call())


if __name__=='__main__':
    replies.run_due(daily.PROFILE,call_mcp)
    print('Saved operator restart intents checked; results are in the actions log.')
