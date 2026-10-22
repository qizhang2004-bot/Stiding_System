# 公共业务服务。
from apps.personnel.models import Employee


def get_active_employees():
    # 返回当前在岗职工列表
    return Employee.objects.filter(is_active=True).select_related("team", "shift_config")
