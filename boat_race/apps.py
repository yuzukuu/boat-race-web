from django.apps import AppConfig
import os
import sys

SKIP_COMMANDS = {'migrate', 'makemigrations', 'collectstatic', 'shell',
                 'createsuperuser', 'check', 'test', 'help', 'inspectdb'}


class BoatRaceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'boat_race'

    def ready(self):
        # 管理コマンド実行時はバックグラウンドスレッドを起動しない
        if any(cmd in sys.argv for cmd in SKIP_COMMANDS):
            return
        # dev server: reloader プロセスでは起動しない
        if 'runserver' in sys.argv and os.environ.get('RUN_MAIN') != 'true':
            return
        from boat_race_config.urls import start_background_refresh
        start_background_refresh()
