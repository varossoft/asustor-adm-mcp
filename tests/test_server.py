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


def test_write_tool_hidden_unless_enabled():
    async def run(write):
        mcp = server.build_mcp("", write=write)
        async with Client(mcp) as c:
            return c.instructions, await c.list_tools()

    instr, tools = asyncio.run(run(False))
    assert len(tools) == 12 and "restart_container" not in {t.name for t in tools}
    assert "Write mode is ON" not in instr

    instr, tools = asyncio.run(run(True))
    by_name = {t.name: t for t in tools}
    assert len(tools) == 13
    assert not by_name["restart_container"].annotations.readOnlyHint
    assert all(t.annotations.readOnlyHint for n, t in by_name.items() if n != "restart_container")
    assert "Write mode is ON" in instr
    dumped = by_name["restart_container"].annotations.model_dump(by_alias=True, exclude_none=False)
    assert all(isinstance(dumped.get(h), bool) for h in HINTS)


def test_build_app_refuses_unsafe_write_config():
    from adm_mcp import core

    core.cfg.write_enabled = True
    core.cfg.write_allowlist = frozenset()
    try:
        try:
            server.build_app("x" * 32)
        except ValueError as exc:
            assert "WRITE_ALLOWLIST" in str(exc)
        else:
            raise AssertionError("build_app must refuse ALLOW_WRITE with an empty allowlist")
    finally:
        core.cfg.write_enabled = False
