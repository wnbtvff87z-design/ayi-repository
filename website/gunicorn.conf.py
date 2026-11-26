import os
from threading import Thread
from time import sleep

from mailer import send_job

bind = '0.0.0.0:' + os.getenv('PORT','8080')
workers = 1
threads = 4
timeout = 40

def post_worker_init(worker):
    app = worker.wsgi
    def run():
        while worker.alive:
            try:
                worked = send_job(app.extensions['leads'],app.config)
                sleep(0.25 if worked else 5)
            except Exception as e:
                worker.log.error('Cola de consultas: %s',type(e).__name__)
                sleep(10)
    Thread(target=run,daemon=True,name='leads-outbox').start()
