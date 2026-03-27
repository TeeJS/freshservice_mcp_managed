import asyncio
import re
from freshservice_mcp.server import (
    READONLY_TOOLS,
    ALLOWED_WRITE_TOOLS,
    DISABLED_WRITE_TOOLS,
    _ACTIVE_TOOLS,
    mcp,
    # Read-only tools for integration testing
    get_ticket_fields,
    get_tickets,
    filter_tickets,
    get_ticket_by_id,
    list_all_ticket_conversation,
    list_service_items,
    get_requested_items,
    get_changes,
    filter_changes,
    get_change_by_id,
    list_change_fields,
    list_change_approval_groups,
    view_change_approval,
    list_change_approvals,
    view_change_note,
    list_change_notes,
    view_change_task,
    get_change_tasks,
    view_change_time_entry,
    list_change_time_entries,
    get_all_products,
    get_products_by_id,
    get_all_requesters,
    get_requester_id,
    list_all_requester_fields,
    filter_requesters,
    get_agent,
    get_all_agents,
    get_agent_fields,
    filter_agents,
    get_all_agent_groups,
    getAgentGroupById,
    get_all_requester_groups,
    get_requester_groups_by_id,
    list_requester_group_members,
    get_all_canned_response,
    get_canned_response,
    list_all_canned_response_folder,
    list_canned_response_folder,
    get_all_solution_category,
    get_solution_category,
    get_list_of_solution_folder,
    get_solution_folder,
    get_list_of_solution_article,
    get_solution_article,
    list_all_workspaces,
    get_workspace,
)

# ============================================================
# Allowlist Verification Tests
# These tests verify the tool sets are correctly configured.
# They do NOT require a Freshservice connection.
# ============================================================

def test_sets_do_not_overlap():
    """Verify no tool appears in more than one set."""
    ro_and_aw = READONLY_TOOLS & ALLOWED_WRITE_TOOLS
    ro_and_dw = READONLY_TOOLS & DISABLED_WRITE_TOOLS
    aw_and_dw = ALLOWED_WRITE_TOOLS & DISABLED_WRITE_TOOLS

    errors = []
    if ro_and_aw:
        errors.append(f"READONLY_TOOLS & ALLOWED_WRITE_TOOLS overlap: {ro_and_aw}")
    if ro_and_dw:
        errors.append(f"READONLY_TOOLS & DISABLED_WRITE_TOOLS overlap: {ro_and_dw}")
    if aw_and_dw:
        errors.append(f"ALLOWED_WRITE_TOOLS & DISABLED_WRITE_TOOLS overlap: {aw_and_dw}")

    if errors:
        print("FAIL: " + "; ".join(errors))
    else:
        print("PASS: No overlaps between tool sets")

def test_all_decorated_functions_are_categorized():
    """Verify every @allowed_tool() function is in exactly one set."""
    with open("src/freshservice_mcp/server.py", "r") as f:
        content = f.read()

    func_names = set(re.findall(r'@allowed_tool\(\)\s*\nasync def (\w+)', content))
    all_sets = READONLY_TOOLS | ALLOWED_WRITE_TOOLS | DISABLED_WRITE_TOOLS

    uncategorized = func_names - all_sets
    extra = all_sets - func_names

    errors = []
    if uncategorized:
        errors.append(f"Functions not in any set: {uncategorized}")
    if extra:
        errors.append(f"Set entries with no matching function: {extra}")

    if errors:
        print("FAIL: " + "; ".join(errors))
    else:
        print(f"PASS: All {len(func_names)} functions accounted for in sets")

def test_active_tools_equals_readonly_plus_allowed():
    """Verify _ACTIVE_TOOLS is correctly computed."""
    expected = READONLY_TOOLS | ALLOWED_WRITE_TOOLS
    if _ACTIVE_TOOLS == expected:
        print("PASS: _ACTIVE_TOOLS = READONLY_TOOLS | ALLOWED_WRITE_TOOLS")
    else:
        diff = _ACTIVE_TOOLS.symmetric_difference(expected)
        print(f"FAIL: _ACTIVE_TOOLS mismatch, diff: {diff}")

def test_disabled_tools_not_in_active():
    """Verify no disabled write tool is in the active set."""
    leaked = DISABLED_WRITE_TOOLS & _ACTIVE_TOOLS
    if leaked:
        print(f"FAIL: Disabled tools found in _ACTIVE_TOOLS: {leaked}")
    else:
        print("PASS: No disabled write tools are active")

def test_set_sizes():
    """Report the size of each set for quick verification."""
    total = len(READONLY_TOOLS) + len(ALLOWED_WRITE_TOOLS) + len(DISABLED_WRITE_TOOLS)
    print(f"INFO: READONLY_TOOLS={len(READONLY_TOOLS)}, "
          f"ALLOWED_WRITE_TOOLS={len(ALLOWED_WRITE_TOOLS)}, "
          f"DISABLED_WRITE_TOOLS={len(DISABLED_WRITE_TOOLS)}, "
          f"Total={total}")


# ============================================================
# Integration Tests (Read-Only)
# These require FRESHSERVICE_APIKEY and FRESHSERVICE_DOMAIN
# environment variables to be set.
# ============================================================

async def test_get_ticket_by_id_integration():
    ticket_id = 861
    result = await get_ticket_by_id(ticket_id)
    print(result)

async def test_filter_tickets_integration():
    query = "priority:3"
    result = await filter_tickets(query)
    print(result)

async def test_filter_requesters_integration():
    query = "primary_email:'vijay.r@effy.co.in'"
    include_agents = True
    result = await filter_requesters(query, include_agents)
    print(result)

async def test_filter_agents_integration():
    query = "first_name:John"
    agents = await filter_agents(query)
    print(agents)

async def test_list_all_workspaces_integration():
    result = await list_all_workspaces()
    print(result)

async def test_get_all_agents_integration():
    result = await get_all_agents()
    print(result)


if __name__ == "__main__":
    # --- Allowlist verification (no API connection needed) ---
    print("=" * 60)
    print("ALLOWLIST VERIFICATION TESTS")
    print("=" * 60)
    test_sets_do_not_overlap()
    test_all_decorated_functions_are_categorized()
    test_active_tools_equals_readonly_plus_allowed()
    test_disabled_tools_not_in_active()
    test_set_sizes()
    print()

    # --- Integration tests (uncomment as needed, requires API credentials) ---
    # print("=" * 60)
    # print("INTEGRATION TESTS (requires API credentials)")
    # print("=" * 60)
    # asyncio.run(test_get_ticket_by_id_integration())
    # asyncio.run(test_filter_tickets_integration())
    # asyncio.run(test_filter_requesters_integration())
    # asyncio.run(test_filter_agents_integration())
    # asyncio.run(test_list_all_workspaces_integration())
    # asyncio.run(test_get_all_agents_integration())
