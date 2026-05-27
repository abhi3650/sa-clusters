from os import path as opath, getenv
from logging import FileHandler, StreamHandler, INFO, basicConfig, error as log_error, info as log_info
from subprocess import run as srun
from dotenv import load_dotenv

if opath.exists("log.txt"):
    with open("log.txt", 'r+') as f:
        f.truncate(0)

basicConfig(
    format="[%(asctime)s] [%(name)s | %(levelname)s] - %(message)s [%(filename)s:%(lineno)d]",
    datefmt="%m/%d/%Y, %H:%M:%S %p",
    handlers=[FileHandler('log.txt'), StreamHandler()],
    level=INFO
)

load_dotenv('cluster.env', override=True)

# Set UPSTREAM_REPO to your own fork URL to enable auto-updates.
# Leave it empty (or unset) to skip updates entirely and run as-is.
UPSTREAM_REPO = getenv("UPSTREAM_REPO", "")
UPSTREAM_BRANCH = getenv("UPSTREAM_BRANCH", "main")

if not UPSTREAM_REPO:
    log_info("UPSTREAM_REPO not set — skipping auto-update, running current code.")
else:
    log_info(f"Updating from {UPSTREAM_REPO} branch {UPSTREAM_BRANCH}")
    if opath.exists('.git'):
        srun(["rm", "-rf", ".git"])

    update = srun(
        f"git init -q"
        f" && git config --global user.email bot@botclusters.local"
        f" && git config --global user.name botclusters"
        f" && git add ."
        f" && git commit -sm update -q"
        f" && git remote add origin {UPSTREAM_REPO}"
        f" && git fetch origin -q"
        f" && git reset --hard origin/{UPSTREAM_BRANCH} -q",
        shell=True
    )

    if update.returncode == 0:
        log_info('Successfully updated from UPSTREAM_REPO')
    else:
        log_error('Update failed — running current code instead.')
