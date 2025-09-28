"""Gunicorn yapılandırma dosyası."""
from gevent import monkey
monkey.patch_all()

# Sunucu ayarları
bind = "0.0.0.0:10000"
workers = 1
worker_class = "gevent"
timeout = 300

# Loglama
loglevel = "info"
accesslog = "-"
errorlog = "-"
