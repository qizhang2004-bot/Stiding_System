# OR-Tools CP-SAT 求解入口。
from .data import Assignment, ScheduleSolution


def solve(problem, *, time_limit_seconds=60):
    # 将排班问题交给 OR-Tools CP-SAT 求解
    try:
        from ortools.sat.python import cp_model
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少 ortools 依赖，请先执行：pip install ortools") from exc

    model = cp_model.CpModel()
    employee_count = len(problem.employees)
    date_count = len(problem.dates)
    shift_count = len(problem.shifts)

    # 决策变量：x[e, d, s] = 1 表示员工 e 在日期 d 安排班次 s
    x = {}
    for e in range(employee_count):
        for d in range(date_count):
            for s in range(shift_count):
                x[e, d, s] = model.NewBoolVar(f"x_{e}_{d}_{s}")

    # H1 每人每天至多一个班次
    for e in range(employee_count):
        for d in range(date_count):
            model.Add(sum(x[e, d, s] for s in range(shift_count)) <= 1)

    # H2 岗位最低需求
    for d, day in enumerate(problem.dates):
        for s, shift in enumerate(problem.shifts):
            reqs = problem.requirements.get((day, shift), [])
            min_total = sum(req.min_count for req in reqs)
            if min_total > 0:
                model.Add(sum(x[e, d, s] for e in range(employee_count)) >= min_total)

    # H3 个人禁排（必须休息 / 不可排班）
    for e, d in problem.unavailability:
        if 0 <= e < employee_count and 0 <= d < date_count:
            model.Add(sum(x[e, d, s] for s in range(shift_count)) == 0)

    # H4 夜班上限
    night_index = None
    for s, name in enumerate(problem.shifts):
        if name == "夜班":
            night_index = s
            break
    if night_index is not None:
        for e, cap in problem.max_night_shifts.items():
            if 0 <= e < employee_count:
                model.Add(sum(x[e, d, night_index] for d in range(date_count)) <= cap)

    # 目标：总排班数量最小化（后续可叠加夜班均衡等次级目标，见 objectives.py）
    model.Minimize(
        sum(
            x[e, d, s]
            for e in range(employee_count)
            for d in range(date_count)
            for s in range(shift_count)
        )
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    status = solver.Solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        assignments = []
        for e, emp in enumerate(problem.employees):
            for d, day in enumerate(problem.dates):
                for s, shift in enumerate(problem.shifts):
                    if solver.Value(x[e, d, s]):
                        assignments.append(
                            Assignment(
                                employee_id=emp.id,
                                date=day,
                                shift=shift,
                                position_id=emp.position_id,
                                group_id=emp.group_id,
                            )
                        )
        result_status = "optimal" if status == cp_model.OPTIMAL else "feasible"
        return ScheduleSolution(
            status=result_status, assignments=assignments, objective=solver.ObjectiveValue()
        )

    return ScheduleSolution(status="infeasible")
