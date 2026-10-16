"""Cloud-only entrypoint; no desktop services or host processes are created."""
import os
from pathlib import Path
import runpy


def main():
    directory = Path(__file__).resolve().parents[1] / 'var'
    directory.mkdir(parents=True, exist_ok=True)
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        os.chown(directory, 10001, 10001)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    external = os.getenv('FINAGENT_ALLOWED_ORIGIN') or os.getenv('RENDER_EXTERNAL_URL', '')
    if not external.startswith('https://') or any(c.isspace() for c in external):
        raise SystemExit('云端部署必须提供 HTTPS 的 FINAGENT_ALLOWED_ORIGIN 或平台 RENDER_EXTERNAL_URL。')
    os.environ['FINAGENT_ALLOWED_ORIGIN'] = external.rstrip('/')
    os.environ['FINAGENT_SECURE_COOKIE'] = '1'
    # Validate access controls and frontend before touching the persistent bank.
    from app.cloud import create_cloud_app
    create_cloud_app()
    os.environ['FINAGENT_APP_ENTRY'] = 'app.cloud:app_factory'
    runpy.run_path(str(Path(__file__).with_name('container-start.py')), run_name='__main__')


if __name__ == '__main__':
    main()
