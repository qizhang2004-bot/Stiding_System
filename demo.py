from ortools.sat.python import cp_model


workers = [
    {'name': '王磊磊', 'roles': ['班长']},
    {'name': '祁向前', 'roles': ['班长']},
    {'name': '秦湖平', 'roles': ['班长', '皮带']},

    {'name': '杨志磊', 'roles': ['电工']},
    {'name': '韩二波', 'roles': ['电工']},
    {'name': '刘广宏', 'roles': ['电工']},
    {'name': '张建伟', 'roles': ['电工']},

    {'name': '程朝阳', 'roles': ['风水管']},
    # {'name': '李旭鹏', 'roles': ['风水管']},
    {'name': '刘瑶', 'roles': ['风水管']},

    {'name': '王鹏', 'roles': ['盾构机']},
    {'name': '李志中', 'roles': ['盾构机']},

    {'name': '牛国辉', 'roles': ['皮带']},
    {'name': '赵国辉', 'roles': ['皮带']},
    {'name': '苏忠', 'roles': ['皮带']},

    # {'name': '张棋', 'roles': ['普通']},
    {'name': '李坤', 'roles': ['普通']},
    # {'name': '蒋赛阳', 'roles': ['普通']},
    {'name': '王钰杰', 'roles': ['风水管']},
    {'name': '王逸凡', 'roles': ['风水管']},
    {'name': '魏志强', 'roles': ['普通']},
    {'name': '李怀亮', 'roles': ['皮带', '盾构机']},
    {'name': '赵磊', 'roles': ['普通']},
    {'name': '赵兴宇', 'roles': ['皮带', '风水管']},
    {'name': '洪泽文', 'roles': ['普通']}
]

# 时间
days = 30

# 获取岗位人员
def get_workers_by_role(role):
    return [w['name'] for w in workers if role in w['roles']]

# 人员列表
worker_names = [w['name'] for w in workers]
electricians = get_workers_by_role("电工")
water_workers = get_workers_by_role("风水管")
shield_workers = get_workers_by_role("盾构机")
belt_workers = get_workers_by_role("皮带")
leaders = get_workers_by_role("班长")

# 创建模型对象
model = cp_model.CpModel()

# 上班状态
# 1 = 上班
# 0 = 休息
x = {}
for w in worker_names:
    for d in range(days):
        x[w, d] = model.new_bool_var(f"{w}_{d}")

# 上班人数要求
for d in range(days):
    model.add(sum(x[w,d] for w in worker_names) == 13)

# 岗位人数要求
for d in range(days):
    # 电工2人
    model.add(sum(x[w,d] for w in electricians) == 2)
    # 风水管2人
    model.add(sum(x[w,d] for w in water_workers) >= 2)
    # 盾构机1人
    model.add(sum(x[w,d] for w in shield_workers) >= 1)
    # 皮带3人
    model.add(sum(x[w,d] for w in belt_workers) >= 3)
    # 班长1人
    model.add(sum(x[w,d] for w in leaders) >= 1)

# 班次要求
work_count = {}
full_work = []

for w in worker_names:
    # 统计每人的班数
    work_count[w] = sum(x[w,d] for d in range(days))
    # 是否满班
    # 1 = 满班
    # 0 = 未满
    is_full = model.new_bool_var(f'{w}_is_full')
    
        
    if w not in leaders:
        model.add(work_count[w] == 18).only_enforce_if(is_full)
        model.add(work_count[w] <= 17).only_enforce_if(is_full.Not())
    else:
        model.add(work_count[w] == 20).only_enforce_if(is_full)
        model.add(work_count[w] <= 19).only_enforce_if(is_full.Not())
    model.add(work_count[w] >= 10)
    full_work.append(is_full)

# 休息变量
rest = {}

for w in worker_names:
    for d in range(days):
        rest[w,d] = model.new_bool_var(f'{w}_rest_{d}')
        model.add(rest[w,d] == x[w,d].Not())

# 连续上6天后休息4天
# 任意连续10天内最多工作6天
for w in worker_names:
    for start in range(days - 9):
        model.add(
            sum(x[w, d] for d in range(start, start + 10)) <= 6
        )

# 惩罚单独休息一天（上班-休息-上班）
single_rest = []

for w in worker_names:
    for d in range(1, days - 1):
        s = model.new_bool_var(f'{w}_single_rest_{d}')

        model.add(s <= x[w,d-1])
        model.add(s <= rest[w,d])
        model.add(s <= x[w,d+1])

        model.add(
            s >= x[w,d-1] + rest[w,d] + x[w,d+1] - 2
        )

        single_rest.append(s)

# 最大化达到18个班的人数
# model.maximize(sum(full_work))
model.maximize(
    sum(full_work) * 100
    - sum(single_rest) * 10
)

# 求解
solver=cp_model.CpSolver()
status=solver.Solve(model)

# 输出
if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
    print("======排班结果======")
    for d in range(days):
        today=[]
        for w in worker_names:
            if solver.Value(x[w,d]):
                today.append(w)
        print(f"第{d+1}天:", today)
    print("\n======班次数======")
    for w in worker_names:
        print(w, solver.Value(work_count[w]),"班")
else:
    print("无解")
