from os import path as opath, getenv
from logging import FileHandler, StreamHandler, INFO, basicConfig, error as log_error, info as log_info
from subprocess import run as srun
from dotenv import load_dotenv
import shutil

if opath.exists("log.txt"):
    with open("log.txt", 'r+') as f:
        f.truncate(0)

basicConfig(format="[%(asctime)s] [%(name)s | %(levelname)s] - %(message)s [%(filename)s:%(lineno)d]",
            datefmt="%m/%d/%Y, %H:%M:%S %p",
            handlers=[FileHandler('log.txt'), StreamHandler()],
            level=INFO)

load_dotenv('cluster.env', override=True)

UPSTREAM_REPO   = getenv("UPSTREAM_REPO",   "")
UPSTREAM_BRANCH = getenv("UPSTREAM_BRANCH", "main")

# Files that must never be overwritten by upstream pulls —
# these contain our enhanced features.
PROTECTED_FILES = [
    "worker.py",
    "cluster.py",
    "run.py",
    "app/routes/routes.py",
    "app/templates/cluster.html",
    "app/static/js/cluster.js",
    "app/static/css/cluster.css",
    "requirements.txt",
    "Dockerfile",
]

if UPSTREAM_REPO:
    # Back up protected files
    backups = {}
    for pf in PROTECTED_FILES:
        if opath.exists(pf):
            with open(pf, 'rb') as f:
                backups[pf] = f.read()

    if opath.exists('.git'):
        srun(["rm", "-rf", ".git"])

    update = srun([f"git init -q \
                     && git config --global user.email botclusters@local \
                     && git config --global user.name botclusters \
                     && git add . \
                     && git commit -sm update -q \
                     && git remote add origin {UPSTREAM_REPO} \
                     && git fetch origin -q \
                     && git reset --hard origin/{UPSTREAM_BRANCH} -q"], shell=True)

    if update.returncode == 0:
        log_info('Successfully updated with latest commit from UPSTREAM_REPO')
    else:
        log_error('Something went wrong while updating — check UPSTREAM_REPO')

    # Restore protected files so our enhanced features survive the pull
    restored = 0
    for pf, content in backups.items():
        import os
        os.makedirs(opath.dirname(pf), exist_ok=True) if opath.dirname(pf) else None
        with open(pf, 'wb') as f:
            f.write(content)
        restored += 1
    if restored:
        log_info(f'Restored {restored} protected enhanced files after upstream pull')

else:
    log_info('UPSTREAM_REPO not set — skipping update, running local code as-is')
