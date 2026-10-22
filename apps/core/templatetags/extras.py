# 模板扩展过滤器。
from django import template

register = template.Library()


@register.filter
def get_item(mapping, key):
    # 在模板中按 key 读取字典：{{ mapping|get_item:key }}
    try:
        return mapping.get(key, [])
    except AttributeError:
        return []
