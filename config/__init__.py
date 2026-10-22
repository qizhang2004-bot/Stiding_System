# Django 项目配置包。
try:
    import pymysql

    pymysql.install_as_MySQLdb()
except ImportError:
    pass
