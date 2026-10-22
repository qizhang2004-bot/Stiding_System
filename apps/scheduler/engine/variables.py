# 决策变量定义（排班引擎）。
#
# 决策变量：
#   x[e, d, s] ∈ {0, 1}
#   表示员工 e（employees 下标）在日期 d（dates 下标）是否承担班次 s（shifts 下标）。
#
# 变量维度：
#   - 员工维度：len(problem.employees)
#   - 日期维度：len(problem.dates)
#   - 班次维度：len(problem.shifts)
#   变量总数 = 员工数 × 日期数 × 班次数
#
# 扩展变量（预留）：
#   y[e, d, s, p] ∈ {0, 1}：员工 e 在日期 d 的班次 s 中承担岗位 p，
#   用于按岗位细分需求时使用。


def variable_count(problem):
    # 决策变量总数
    return len(problem.employees) * len(problem.dates) * len(problem.shifts)
