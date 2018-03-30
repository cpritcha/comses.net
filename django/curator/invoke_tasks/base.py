"""Tasks that are not namespaced go here"""

from invoke import task, call
from django.conf import settings
from .util import dj, env


@task(aliases=['ss'])
def setup_site(ctx, site_name='CoRe @ CoMSES Net', site_domain='www.comses.net'):
    dj(ctx, 'setup_site --site-name="{0}" --site-domain="{1}"'.format(site_name, site_domain))
    if not settings.DEPLOY_ENVIRONMENT.is_production():
        deny_robots(ctx)


@task
def clean(ctx, revert=False):
    ctx.run("find . -name '*.pyc' -o -name 'generated-*' -delete -print")


@task(aliases=['dr'])
def deny_robots(ctx):
    dj(ctx, 'setup_robots_txt --no-allow')


@task(aliases=['cs'])
def collect_static(ctx):
    dj(ctx, 'collectstatic -c --noinput', pty=True)
    ctx.run('touch ./core/wsgi.py')


@task(aliases=['qc'])
def quality_check_openabm_files_with_db(ctx):
    dj(ctx, 'quality_check_files_with_db', pty=True)


@task
def sh(ctx, print_sql=False):
    dj(ctx, 'shell_plus --ipython{}'.format(' --print-sql' if print_sql else ''), pty=True)


@task
def test(ctx, tests=None, coverage=False):
    if tests is not None:
        apps = tests
    else:
        apps = ''
    if coverage:
        ignored = ['*{0}*'.format(ignored_pkg) for ignored_pkg in env['coverage_omit_patterns']]
        coverage_cmd = "coverage run --source={0} --omit={1}".format(','.join(env['coverage_src_patterns']),
                                                                     ','.join(ignored))
    else:
        coverage_cmd = env['python']
    ctx.run("{coverage_cmd} manage.py test {apps}".format(apps=apps, coverage_cmd=coverage_cmd),
            env={'DJANGO_SETTINGS_MODULE': 'core.settings.test', 'HYPOTHESIS_VERBOSITY_LEVEL': 'verbose'})


@task(pre=[call(test, coverage=True)])
def coverage(ctx):
    ctx.run('coverage html')


@task
def server(ctx, ip="0.0.0.0", port=8000):
    dj(ctx, 'runserver {ip}:{port}'.format(ip=ip, port=port), capture=False)