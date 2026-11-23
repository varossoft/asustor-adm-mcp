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


HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def test_every_tool_declares_all_four_hints_as_booleans():
    async def run():
        async with Client(server.build_mcp()) as c:
            return await c.list_tools()

    tools = asyncio.run(run())
    assert len(tools) == 12
    for t in tools:
        assert t.annotations is not None, f"{t.name}: no annotations"
        dumped = t.annotations.model_dump(by_alias=True, exclude_none=False)  # wire names (camelCase)
        for hint in HINTS:
            value = dumped.get(hint)
            assert isinstance(value, bool), f"{t.name}: {hint} missing or not boolean ({value!r})"
