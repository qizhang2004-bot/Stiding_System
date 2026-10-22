# 人员导入：文件解析与行级校验预览（不写入数据库）。
# 导入文件为固定 8 列格式（按列顺序解析，表头必须完整）：
#   队组编号, 班组编号, 职工编号, 姓名, 岗位, 班次, 已上班数, 矿上应上最少班数
import csv
import io

from .validators import validate_import_row

HEADERS = ["队组编号", "班组编号", "职工编号", "姓名", "岗位", "班次", "已上班数", "矿上应上最少班数"]
MAX_PREVIEW_ROWS = 200


class ImportFormatError(Exception):
    # 导入文件格式错误
    pass


def parse_import_file(uploaded):
    # 解析上传文件，返回 (数据行列表, 是否截断)。支持 CSV（UTF-8）与 XLSX。
    name = (uploaded.name or "").lower()
    if name.endswith(".csv"):
        return _parse_csv(uploaded)
    if name.endswith(".xlsx"):
        return _parse_xlsx(uploaded)
    raise ImportFormatError("仅支持 .csv 或 .xlsx 文件。")


def _parse_csv(uploaded):
    try:
        text = io.TextIOWrapper(uploaded.file, encoding="utf-8-sig")
        reader = csv.reader(text)
        rows = [row for row in reader]
    except UnicodeDecodeError as exc:
        raise ImportFormatError("文件编码应为 UTF-8。") from exc
    return _rows_to_dicts(rows)


def _parse_xlsx(uploaded):
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise ImportFormatError("缺少 openpyxl，无法解析 .xlsx 文件。") from exc
    workbook = load_workbook(uploaded.file, read_only=True, data_only=True)
    sheet = workbook.active
    rows = [
        [cell if cell is not None else "" for cell in row]
        for row in sheet.iter_rows(values_only=True)
    ]
    return _rows_to_dicts(rows)


def _rows_to_dicts(rows):
    # 第一行为表头；按表头名称映射 8 个字段，数据行按列顺序解析
    if not rows:
        raise ImportFormatError("文件为空。")
    header = [str(c).strip() for c in rows[0]]
    missing = [h for h in HEADERS if h not in header]
    if missing:
        raise ImportFormatError("表头缺少列：" + "、".join(missing))
    index = {h: header.index(h) for h in HEADERS}
    data_rows = []
    truncated = False
    for row in rows[1:]:
        if not row or all(str(c).strip() == "" for c in row):
            continue
        if len(data_rows) >= MAX_PREVIEW_ROWS:
            truncated = True
            break
        data = {h: (str(row[index[h]]).strip() if index[h] < len(row) else "") for h in HEADERS}
        data_rows.append(data)
    if not data_rows:
        raise ImportFormatError("文件中没有数据行。")
    return data_rows, truncated


def preview_import_rows(rows, allowed_org=None):
    # 对解析出的行执行现有行级校验，返回预览结果（不写库）。
    # allowed_org 传入时（队组管理员），仅允许校验属于该队组班组的行。
    ok, failed = [], []
    seen_employee_nos = set()
    for line_no, data in enumerate(rows, start=2):
        errors = validate_import_row(
            organization_code=data["队组编号"],
            team_code=data["班组编号"],
            employee_no=data["职工编号"],
            name=data["姓名"],
            position=data["岗位"],
            shift_name=data["班次"],
            worked_shifts=_to_int(data["已上班数"]),
            min_required_shifts=_to_int(data["矿上应上最少班数"]),
            allowed_org=allowed_org,
        )
        employee_no = str(data["职工编号"]).strip()
        if employee_no and employee_no in seen_employee_nos and not any(
            "职工编号已存在" in e for e in errors
        ):
            errors.append(f"职工编号在文件内重复：{employee_no}。")
        if employee_no:
            seen_employee_nos.add(employee_no)
        if errors:
            failed.append({"line": line_no, "data": data, "errors": errors})
        else:
            ok.append({"line": line_no, "data": data})
    return {"ok": ok, "failed": failed}


def _to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
