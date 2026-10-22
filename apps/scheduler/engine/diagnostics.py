# 无解原因诊断。


def diagnose(problem):
    # 对无可行解的问题给出可能原因与建议（启发式检查）
    reasons = []
    for (day, shift), reqs in problem.requirements.items():
        min_total = sum(req.min_count for req in reqs)
        if min_total <= 0:
            continue
        available = len(problem.employees) - sum(
            1 for (e, d) in problem.unavailability if d == day
        )
        if min_total > available:
            reasons.append(f"{day} {shift} 最低需求 {min_total} 人，但当天可用员工仅 {available} 人。")
    for e, d in problem.unavailability:
        if 0 <= e < len(problem.employees) and all(
            (e, day) in problem.unavailability for day in problem.dates
        ):
            reasons.append(f"员工 {problem.employees[e].name} 在整个周期内均被禁止排班。")
    if not reasons:
        reasons.append("需求与约束组合冲突，建议检查岗位需求配置与个人规则（如夜班上限）。")
    return reasons
