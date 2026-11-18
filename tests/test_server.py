import asyncio

from fastmcp import Client

from adm_mcp import server


def test_instructions_without_notes():
    assert server.build_instructions("") == server.BASE_INSTRUCTIONS
    assert server.build_instructions("   ") == server.BASE_INSTRUCTIONS


def test_instructions_with_notes_and_newlines():
    text = server.build_instructions("sda and sdb: data without RAID.\\nmd126 = swap")
    assert "Owner notes about this NAS" in text
    assert "sda and sdb: data without RAID.\nmd126 = swap" in text


def test_client_receives_notes_and_all_tools_are_read_only():
    async def run():
        mcp = server.build_mcp("sda and sdb are not members of any array")
        async with Client(mcp) as c:
            tools = await c.list_tools()
            return c.instructions, tools

    instructions, tools = asyncio.run(run())
    assert "are not members of any array" in instructions
    assert len(tools) == 12
    assert all(t.annotations.readOnlyHint for t in tools)
