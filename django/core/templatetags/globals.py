from typing import Optional

from datetime import datetime

from allauth.socialaccount import providers
from django.conf import settings
from django.template import defaultfilters
from django.utils.timezone import get_current_timezone
from django_jinja import library
from dateutil.parser import parse as datetime_parse
from core.serializer_helpers import YMD_DATETIME_FORMAT
from jinja2 import Markup
from webpack_loader.templatetags import webpack_loader as wl

from ..summarization import summarize
from ..utils import markdown_to_sanitized_html


@library.global_function
def render_bundle(bundle_name, extension=None, config='DEFAULT', attrs=''):
    return wl.render_bundle(bundle_name, extension, config, attrs)


@library.global_function
def now(format_string):
    """
    Simulates the Django https://docs.djangoproject.com/en/1.10/ref/templates/builtins/#now default templatetag
    Usage: {{ now('Y') }}
    """
    tzinfo = get_current_timezone() if settings.USE_TZ else None
    return defaultfilters.date(datetime.now(tz=tzinfo), format_string)


@library.global_function
def is_debug():
    return settings.DEBUG


@library.global_function
def provider_login_url(request, provider_id, process="login"):
    provider = providers.registry.by_id(provider_id, request)
    return provider.get_login_url(request, process=process)


@library.global_function
def summarize_markdown(md):
    return summarize(md, 2)


@library.filter
def markdown(text: str):
    """
    Returns a sanitized HTML string representing the rendered version of the incoming Markdown text.
    :param text: string markdown source text to be converted
    :return: sanitized html string, explicitly marked as safe via jinja2.Markup
    """
    return Markup(markdown_to_sanitized_html(text))


@library.filter
def truncate_midnight(text: Optional[str]):
    if text is None:
        return None

    d = datetime_parse(text)
    if d.minute == 0 and d.second == 0 and d.hour == 0:
        return d.strftime(YMD_DATETIME_FORMAT)
    else:
        return text

# # http://stackoverflow.com/questions/6453652/how-to-add-the-current-query-string-to-an-url-in-a-django-template
# @register.simple_tag
# def query_transform(request, **kwargs):
#     updated = request.GET.copy()
#     for k, v in kwargs.iteritems():
#         updated[k] = v
#
#     return updated.urlencode()
