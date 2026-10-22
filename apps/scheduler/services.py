# 排班记录业务逻辑。
import uuid

from .models import ScheduleRecord


def new_schedule_batch_id():
    # 生成新排班批次号：每生成一版排班结果时调用一次
    return uuid.uuid4()


def create_schedule_record(
    *,
    schedule_batch_id,
    organization,
    team,
    shift_config,
    employee,
    work_date,
    shift_type,
    **extra,
):
    # 创建排班记录并执行一致性校验（队组/班组/班次配置/职工归属）
    record = ScheduleRecord(
        schedule_batch_id=schedule_batch_id,
        organization=organization,
        team=team,
        shift_config=shift_config,
        employee=employee,
        work_date=work_date,
        shift_type=shift_type,
        **extra,
    )
    record.full_clean()
    record.save()
    return record
