import os
from celery import Celery
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'boat_race_config.settings')
app = Celery('boat_race_config')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
