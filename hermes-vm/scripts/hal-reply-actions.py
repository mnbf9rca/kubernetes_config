#!/usr/bin/env python3
"""Native no-agent one-shot: execute only saved explicit operator restart intents."""
from pathlib import Path
import asyncio
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'hal-daily-check'))
import hal_daily_check as daily
import hal_reply_actions as replies


def call_mcp(tool,args):
    from utils import load_yaml_file_readonly
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    url=load_yaml_file_readonly(daily.PROFILE/'config.yaml')['mcp_servers']['HA-MCP']['url']
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
