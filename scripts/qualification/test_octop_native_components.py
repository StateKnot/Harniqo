#!/usr/bin/env python3
"""Actual native SDK, no network, no model key, no inference. Component scope only."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from octop_harness import HarnessAgent, HarnessAgentConfig, ProviderConfig, ModelConfig

class NativeComponents(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        cfg=HarnessAgentConfig(workspace_dir=root, backend={'type':'filesystem','root_dir':str(root),'virtual_mode':True},
             providers=[ProviderConfig(id='fixture',base_url='https://example.invalid/v1',api_key='fixture-not-a-real-key',models=[ModelConfig(id='fixture')])],
             default_model='fixture/fixture', memory_enabled=False, memory=(), team_enabled=False,
             ask_user_enabled=False, subagents_auto_load=False, subagents=[], web_search_tools=False,
             todos_enabled=False, model_retry_enabled=False, tool_search_mode='eager',
             interrupt_on={'write_file':{'allowed_decisions':['approve','reject']}})
        self.agent=HarnessAgent(cfg)
    async def asyncTearDown(self):
        await self.agent.aclose()
    async def test_native_empty_checkpoint_history(self):
        self.assertEqual(await self.agent.aget_history('never-used'),[])
    async def test_native_approval_configuration(self):
        self.assertEqual(self.agent._resolve_interrupt_on(),{'write_file':{'allowed_decisions':['approve','reject']}})
    async def test_native_cancel_interrupts_blocked_await(self):
        cancel=asyncio.Event()
        started=asyncio.Event()
        ended=asyncio.Event()
        async def blocked():
            started.set()
            try:
                await asyncio.sleep(60)
                yield 'should-not-finish'
            finally: ended.set()
        received=[]
        async def consume():
            async for chunk in self.agent._iter_until_cancelled(blocked(),cancel): received.append(chunk)
        task=asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(),2)
        cancel.set()
        await asyncio.wait_for(task,2)
        self.assertTrue(ended.is_set())
        self.assertEqual(received,[])

if __name__=='__main__': unittest.main()
