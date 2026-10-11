"""
Network-wide stop and route optimisation for the school bus fleet.

    config.py         every weight, limit and assumption
    model.py          Problem / Node / Vehicle / Solution - plain data
    build_problem.py  workbooks + coordinates -> Problem (+ today's plan)
    matrix.py         stop-to-stop travel times: haversine | osrm | google
    evaluate.py       one scorer for today's plan and any proposal
    solver.py         construction + local search + ruin-and-recreate
    consolidate.py    nearby stops that could become one
    coords.py         Google Maps coordinate template export / import
    report.py         plan.json, summary.md, change lists
    demo.py           synthetic network for testing before real data exists

Entry point: pipeline/run_optimiser.py
"""
