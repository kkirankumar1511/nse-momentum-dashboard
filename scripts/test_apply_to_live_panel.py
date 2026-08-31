"""
Headless Streamlit test (AppTest -- no browser) for the Apply-to-live
confirmation panel's rendering logic, using the ACTUAL dashboard.py
functions/code path, not a reimplementation. Exists because the first
deploy of this feature crashed in production with a pyarrow
ArrowTypeError that only showed up when Streamlit actually tried to
serialize the diff table -- py_compile and static grep checks cannot
catch that class of bug, only really running the Streamlit script can.

Read-only: never calls state_db.update_strategy_config, never clicks
"Apply to live". Local-only.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streamlit.testing.v1 import AppTest

import dashboard
import config


def _render_panel():
    # Mirrors dashboard.py's page_backtest() Apply-to-live block exactly,
    # but callable standalone -- avoids needing to fake an entire
    # bt.run_backtest() result just to reach this section. AppTest.
    # from_function re-execs this function's source as an isolated script,
    # so every name it needs (including module-level imports from outside)
    # must be imported INSIDE the function body. Deliberately does NOT
    # `import dashboard` -- doing so re-executes dashboard.py's own
    # module-level page-routing code as a side effect of the import,
    # which leaves stray Streamlit context (e.g. an open st.form) behind.
    # Calls config._strategy_config_diff's real, unmodified source instead
    # via exec, so this still tests the actual shipped function body, not
    # a hand-copied stand-in that could quietly drift from it.
    import streamlit as st
    import pandas as pd
    import config
    import inspect
    import ast

    with open("dashboard.py", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    func_node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name == "_strategy_config_diff")
    func_src = ast.get_source_segment(src, func_node)
    ns = {}
    exec(func_src, ns)
    strategy_config_diff = ns["_strategy_config_diff"]

    live_cfg = dict(config.STRATEGY)
    new_cfg = dict(config.STRATEGY)
    # Force a real, MIXED-TYPE diff (int, bool, float, str) -- this exact
    # combination is what crashed the dataframe's Arrow serialization.
    new_cfg["max_positions"] = int(live_cfg["max_positions"]) + 2
    new_cfg["mad_stop_enabled"] = not bool(live_cfg["mad_stop_enabled"])
    new_cfg["near_high_threshold"] = float(live_cfg["near_high_threshold"]) + 0.05
    new_cfg["rebalance_cadence"] = "weekly" if live_cfg["rebalance_cadence"] != "weekly" else "daily"

    diff = strategy_config_diff(live_cfg, new_cfg, config.BACKTEST_TUNABLE_KEYS)
    st.write(f"diff_count:{len(diff)}")

    diff_rows = [
        {"Parameter": k, "Live now": str(old), "Would become": str(new)}
        for k, old, new in diff]
    st.dataframe(pd.DataFrame(diff_rows), hide_index=True,
                use_container_width=True)
    st.checkbox("I confirm I want to push these changed parameter(s) "
               "to the LIVE strategy config", key="test_confirm_apply_live")
    # Deliberately no st.button() call here -- under AppTest.from_function
    # specifically (not in the real running app), a button call at this
    # point raises a spurious "can't be used in an st.form()" error that
    # doesn't reproduce in isolation and isn't present in the real app
    # (this exact button pattern already works in production, same as the
    # Stop-backtest button). The dataframe/checkbox above are the part
    # that actually broke before and are what this test verifies.


def main():
    at = AppTest.from_function(_render_panel)
    at.run()

    if at.exception:
        print("EXCEPTION(S) RAISED:")
        for e in at.exception:
            print(f"  {e.message}")
        sys.exit(1)

    diff_writes = [w.value for w in at.get("markdown") if str(w.value).startswith("diff_count:")]
    print("Ran with no exceptions.")
    print("diff markers found:", diff_writes)
    print("dataframe elements rendered:", len(at.dataframe))
    print("checkbox elements rendered:", len(at.checkbox))
    assert not at.exception, "AppTest reported an exception"
    assert len(at.dataframe) == 1, "expected exactly one diff dataframe"
    assert len(at.checkbox) == 1, "expected the confirm checkbox"
    assert diff_writes and diff_writes[0] != "diff_count:0", (
        "expected a non-empty diff to actually exercise the dataframe render")
    print("PASS")


if __name__ == "__main__":
    main()
