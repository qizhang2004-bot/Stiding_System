# 管理命令：幂等补齐班组的早/中/晚三条班次配置。
# 用法：python manage.py ensure_shift_configs [--team-id 班组ID]
from django.core.management.base import BaseCommand

from apps.organization.models import Team
from apps.organization.services import ensure_default_shift_configs


class Command(BaseCommand):
    help = "为班组幂等补齐早/中/晚三条班次配置，重复执行不会产生重复记录。"

    def add_arguments(self, parser):
        parser.add_argument("--team-id", type=int, help="仅处理指定班组 ID")

    def handle(self, *args, **options):
        teams = Team.objects.order_by("id")
        if options.get("team_id"):
            teams = teams.filter(pk=options["team_id"])
        created_total = 0
        for team in teams:
            created, _ = ensure_default_shift_configs(team)
            created_total += created
            if created:
                self.stdout.write(
                    self.style.SUCCESS(f"班组 {team.pk}（{team}）：新增 {created} 条班次配置")
                )
        self.stdout.write(f"完成：处理 {teams.count()} 个班组，新增 {created_total} 条配置。")
