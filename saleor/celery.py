from celery import Celery


app = Celery("saleor")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
