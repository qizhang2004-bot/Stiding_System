# 数据迁移：为既有用户补建角色档案（超级用户 → super，其余 → member，不绑定队组）。
from django.db import migrations


def seed_profiles(apps, schema_editor):
    User = apps.get_model("auth", "User")
    UserProfile = apps.get_model("accounts", "UserProfile")
    for user in User.objects.all():
        UserProfile.objects.update_or_create(
            user=user,
            defaults={
                "role": "super" if user.is_superuser else "member",
                "organization_id": None,
            },
        )


def remove_profiles(apps, schema_editor):
    UserProfile = apps.get_model("accounts", "UserProfile")
    UserProfile.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_profiles, remove_profiles),
    ]
